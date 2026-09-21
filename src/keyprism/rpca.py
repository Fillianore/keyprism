#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism RPCA: Robust PCA via chunked inexact ALM / ADMM (Phase 2)

Pure algorithms only — numpy arrays in, numpy arrays out, zero IO.

RPCA (Candès et al. 2011) splits a matrix ``X`` into a low-rank part ``L``
(here: the sustained harmonic content of a magnitude spectrogram) plus a
sparse part ``S`` (here: broadband transients), solving

    min  ||L||_* + lam * ||S||_1   s.t.  L + S = X .

Solver: inexact ALM (Lin–Chen–Ma 2010), i.e. one ADMM iteration is an SVD
singular-value-thresholding update for ``L`` and an element-wise soft
threshold for ``S``, followed by the dual ascent step. Three standard
convergence refinements are used (all locked by tests):

- two-stage mu schedule: ``mu`` grows geometrically (``mu = mu * rho``)
  while the relative constraint residual is still above ``freeze_tol``
  (forces ``L + S = X`` home fast), then freezes — unchecked growth would
  shrink the SVT/soft thresholds ``1/mu`` and ``lam/mu`` towards zero,
  which silently cancels both regularizers and lets ``L`` absorb ``S``
  (the split then satisfies the constraint but not the objective);
- residual monitoring with early stopping
  (``||X - L - S||_F / ||X||_F <= tol``);
- scale invariance by construction: the input is normalized by its
  spectral norm, the solver runs on the canonical O(1) matrix with the
  canonical ``lam = 1 / sqrt(max(m, n))`` and IALM initialization
  ``mu0 = 1.25 * ||X||_F / ||X||_2``, and the outputs are rescaled — raw
  STFT magnitudes are ~1e-4..1e-1, and the absolute thresholds of a naive
  fixed-``mu = 1`` loop would zero everything out on such data.

``lam``/``mu``, when passed explicitly, apply to the internally
normalized matrix (i.e. they are relative quantities).

Full-track discipline: an SVD on the whole spectrogram would be OOM-prone
and pointless, so the track is processed in overlapping frame chunks and
the per-chunk ``L``/``S`` are stitched by linear cross-fade (overlap-add):
inside each overlap zone the two raw solutions are blended with
complementary ramps summing to 1, which removes the energy steps a hard
``np.concatenate`` would leave at chunk seams. One generator drives BOTH
consumers — :func:`rpca_full_track` (reference/stitch into full matrices,
tests and small inputs) and the stems orchestrator, which finalizes row
blocks as they complete and never holds full-track matrices in RAM.
"""

import numpy as np

__all__ = [
    "rpca_decompose", "rpca_full_track", "iter_rpca_blocks",
    "plan_chunks",
]


def plan_chunks(n_frames: int, chunk_frames: int, overlap_frames: int):
    """Chunk plan ``[(a, b), ...]``: overlapping spans covering
    ``[0, n_frames)`` with ``hop = chunk_frames - overlap_frames``."""
    chunk_frames = int(chunk_frames)
    overlap_frames = int(overlap_frames)
    if not 1 <= overlap_frames < chunk_frames:
        raise ValueError("需要 1 <= overlap_frames < chunk_frames")
    hop = chunk_frames - overlap_frames
    return [(a, min(a + chunk_frames, n_frames))
            for a in range(0, max(n_frames, 1), hop)]


def rpca_decompose(mag, lam=None, mu=None, max_iter=40, rho=1.5,
                   freeze_tol=1e-3, tol=1e-6):
    """Inexact ALM (ADMM) decomposition of ONE block.

    ``mag`` is a 2-D magnitude block ``(m, n)``. Returns ``(L, S)``
    float64 in the input's scale. ``lam`` defaults to the canonical
    ``1/sqrt(max(m, n))`` and ``mu`` to the IALM initialization
    ``1.25 * ||X||_F / ||X||_2`` — both interpreted on the internally
    normalized matrix (module docstring). ``rho`` is the geometric mu
    growth factor applied while the relative residual exceeds
    ``freeze_tol`` (two-stage schedule; ``rho <= 1`` disables growth);
    the loop stops early once ``||X - L - S||_F / ||X||_F`` drops below
    ``tol``.
    """
    X = np.asarray(mag, dtype=np.float64)
    if X.ndim != 2:
        raise ValueError("mag 形状须为 (m, n) 二维矩阵")
    m, n = X.shape
    x_norm = float(np.linalg.norm(X))
    if x_norm <= 0.0:
        return np.zeros_like(X), np.zeros_like(X)

    # Scale-invariant solve: canonical O(1) matrix, canonical schedule
    sigma1 = float(np.linalg.svd(X, compute_uv=False)[0])
    if sigma1 <= 0.0:
        return np.zeros_like(X), np.zeros_like(X)
    X = X / sigma1
    x_norm = float(np.linalg.norm(X))

    if lam is None:
        lam = 1.0 / np.sqrt(max(m, n))
    if mu is None:
        mu = 1.25 * x_norm  # ||X/||X||_2||_F / 1
    mu = float(mu)
    lam = float(lam)
    max_iter = int(max_iter)

    L = np.zeros_like(X)
    S = np.zeros_like(X)
    Y = np.zeros_like(X)
    for _ in range(max_iter):
        # 1. L update: SVD + singular value thresholding
        U, s, Vt = np.linalg.svd(X - S + Y / mu, full_matrices=False)
        s_thresh = np.maximum(s - 1.0 / mu, 0.0)
        L = (U * s_thresh) @ Vt
        # 2. S update: element-wise soft thresholding
        diff = X - L + Y / mu
        S = np.sign(diff) * np.maximum(np.abs(diff) - lam / mu, 0.0)
        # 3. Dual variable update
        Y = Y + mu * (X - L - S)
        # Two-stage penalty: grow mu only while the constraint is loose
        resid = float(np.linalg.norm(X - L - S))
        if rho > 1.0 and resid / x_norm > freeze_tol:
            mu = min(mu * float(rho), 1e7)
        # Early stopping on the reconstruction residual
        if resid <= tol * x_norm:
            break
    return L * sigma1, S * sigma1


def iter_rpca_blocks(mag, chunk_frames=400, overlap_frames=80, **kwargs):
    """Stream finalized ``(a, b, L_block, S_block)`` row blocks.

    ``mag`` only needs row slicing (full array or read-only memmap —
    slices are taken per chunk, never the whole matrix). Each yielded
    block ``[a, b)`` carries the cross-faded (final) low-rank / sparse
    rows: raw per-chunk solutions are blended with complementary linear
    ramps inside every overlap zone, so a downstream consumer can emit
    blocks as they complete without ever materializing the full track.
    Keyword arguments are forwarded to :func:`rpca_decompose`.
    """
    n_frames = mag.shape[0]
    n_bins = mag.shape[1]
    plan = plan_chunks(n_frames, chunk_frames, overlap_frames)
    overlap_frames = min(int(overlap_frames), chunk_frames - 1)

    prev = None  # (a, b, L_raw, S_raw) of the previous chunk
    for i, (a, b) in enumerate(plan):
        L_raw, S_raw = rpca_decompose(np.asarray(mag[a:b], dtype=np.float64),
                                      **kwargs)
        # Finalize rows [a, next_a): covered only by chunks <= i
        next_a = plan[i + 1][0] if i + 1 < len(plan) else n_frames
        rows = next_a - a
        L_out = np.empty((rows, n_bins), dtype=np.float64)
        S_out = np.empty((rows, n_bins), dtype=np.float64)
        L_out[:rows] = L_raw[:rows]
        S_out[:rows] = S_raw[:rows]
        if prev is not None:
            pa, pb, pL, pS = prev
            # Rows shared with the previous chunk, cross-faded with
            # complementary ramps ((j+1)/(O+1) vs (O-j)/(O+1) -> sum 1)
            o = min(overlap_frames, b - a, pb - pa, rows)
            if o > 0:
                w = ((np.arange(o, dtype=np.float64) + 1.0)
                     / (o + 1.0))[:, None]
                L_out[:o] = w * L_out[:o] + (1.0 - w) * pL[a - pa:a - pa + o]
                S_out[:o] = w * S_out[:o] + (1.0 - w) * pS[a - pa:a - pa + o]
        yield a, next_a, L_out, S_out
        prev = (a, b, L_raw, S_raw)


def rpca_full_track(mag, chunk_frames=400, overlap_frames=80, **kwargs):
    """Convenience wrapper: stitch :func:`iter_rpca_blocks` into full
    ``(L, S)`` matrices of the input's shape.

    Only for tests / small inputs — the stems pipeline consumes the
    generator directly so no full-track magnitude-sized buffers are kept.
    """
    X = np.asarray(mag)
    n_frames, n_bins = X.shape
    L = np.empty((n_frames, n_bins), dtype=np.float64)
    S = np.empty((n_frames, n_bins), dtype=np.float64)
    for a, b, Lb, Sb in iter_rpca_blocks(X, chunk_frames, overlap_frames,
                                         **kwargs):
        L[a:b] = Lb
        S[a:b] = Sb
    return L, S

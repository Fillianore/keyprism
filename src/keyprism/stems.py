#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism stems: classic source separation -> stem WAVs, disk-cached
(Phase 2)

Position: sits beside ``analyze`` / ``transcribe`` (never modifies them)
and is called by ``server``; composes ``hpss`` + ``rpca`` + the Phase 0
``transform`` ISTFT. Dependency direction stays one-way:
``server -> stems -> {analyze (cache access), transform, hpss, rpca}``.

Stems are served ON DEMAND through /api/stems (+ per-file /api/stem) —
the data.json payload keeps ``"stems": null`` (owned by ``payload.py``,
untouched). Results cache under the analysis entry, version-tagged like
the Phase 1 note caches:

    <entry>/stems/<STEMS_VERSION>/<method>/<stem>.wav  (+ status.json)

so bumping ``STEMS_VERSION`` invalidates previous results automatically.
A cache hit re-reads ``status.json``; the WAVs themselves are streamed
verbatim by the server.

Streaming discipline (AGENTS.md: the full complex STFT is never in RAM —
and no full-track mask either). One pass over ``stft.npy`` row blocks:

    memmap rows [a, b) -> magnitude (+ real context rows from neighbouring
    blocks so the HPSS medians and the RPCA overlap-add stay full-track
    exact) -> masks -> X_stem = mask * X_complex  (the real mask multiplies
    the COMPLEX STFT: the original phase is preserved, only magnitudes are
    re-weighted) -> per-stem streaming ISTFT, keeping the last
    ``win//hop`` masked rows as tail across steps so every emitted sample
    has ALL of its overlap-add contributors inside the buffer — the
    concatenation of emitted ranges is then sample-exact versus a
    hypothetical full-matrix ISTFT -> PCM16 WAV written incrementally via
    soundfile; atomic directory swap on completion.

Methods (per spec): ``hpss`` (median-filter masks), ``rpca`` (ADMM
low-rank/sparse masks) and ``combined`` (agreement-weighted fusion: the
geometric mean of the two harmonic estimates vs of the two percussive
estimates, Wiener-normalized — Stem 1 = Harmonic+LowRank,
Stem 2 = Percussive+Sparse).

Determinism: fixed tiling, pure per-block functions, no randomness —
two cold runs produce byte-identical WAVs.
"""

import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf

from . import analyze
from .hpss import HPSS_EPS, hpss_context_frames, hpss_masks
from .rpca import iter_rpca_blocks, plan_chunks
from .transform import istft_complex

__all__ = [
    "STEMS_VERSION", "METHODS", "STEM_SPECS", "compute_stems", "get_stems",
    "stems_dir", "stem_status_path", "stem_file_path", "load_status",
]

#: Version tag of the stem pipeline; part of the cache path (bump to
#: invalidate previously cached stems everywhere).
STEMS_VERSION = "stems_v1"

#: Query values accepted by /api/stems.
METHODS = ("hpss", "rpca", "combined")

#: method -> stem keys in canonical order (file names under
#: <entry>/stems/<STEMS_VERSION>/<method>/).
STEM_SPECS = {
    "hpss": ("harmonic", "percussive"),
    "rpca": ("lowrank", "sparse"),
    "combined": ("harmonic", "percussive"),
}

#: Row-block size for the plain HPSS tiling (the RPCA tiling is set by
#: ``iter_rpca_blocks`` defaults: 400-frame chunks, 80-frame overlap).
HPSS_CHUNK_FRAMES = 512


def stems_dir(entry: Path, method: str) -> Path:
    """``<entry>/stems/<STEMS_VERSION>/<method>`` — version-tagged."""
    _check_method(method)
    return Path(entry) / "stems" / STEMS_VERSION / method


def stem_status_path(entry: Path, method: str) -> Path:
    return stems_dir(entry, method) / "status.json"


def stem_file_path(entry: Path, method: str, key: str) -> Path:
    if method not in STEM_SPECS or key not in STEM_SPECS[method]:
        raise KeyError(f"未知 stem: {method}/{key}")
    return stems_dir(entry, method) / f"{key}.wav"


def _check_method(method: str) -> None:
    if method not in METHODS:
        raise ValueError(f"未知分离方法: {method} (可用: {', '.join(METHODS)})")


def load_status(entry: Path, method: str) -> dict | None:
    """Cached status dict when the method's stem files all exist."""
    _check_method(method)
    try:
        status = json.loads(
            stem_status_path(entry, method).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if status.get("version") != STEMS_VERSION or \
            status.get("method") != method or \
            status.get("stems") != list(STEM_SPECS[method]):
        return None
    for key in STEM_SPECS[method]:
        if not stem_file_path(entry, method, key).is_file():
            return None
    return status


class _MagView:
    """Row-slicing magnitude view over the complex64 memmap — gives
    :func:`iter_rpca_blocks` array-like access without materializing the
    full-track magnitude matrix (row slices are taken per chunk only)."""

    def __init__(self, mm):
        self._mm = mm
        self.shape = mm.shape

    def __getitem__(self, item):
        return np.abs(np.asarray(self._mm[item])).astype(np.float64)


def _iter_work_blocks(mm, method: str, rpca_kwargs: dict):
    """Yield ``(a, b, mask1, mask2, is_last)`` work blocks of finalized
    rows.

    ``hpss``: plain tiling; masks are made full-track-exact by loading
    ``hpss_context_frames()`` real rows of context from the memmap around
    each block (the medians' zero padding then only affects rows outside
    the block — exactly like a full-track call). ``rpca``/``combined``:
    the RPCA overlap-add generator drives the tiling and provides
    cross-faded L/S; HPSS context rows are loaded around each finalized
    block; ``combined`` fuses the two mask families per cell.
    """
    n_frames = mm.shape[0]
    ctx = hpss_context_frames()

    def hpss_block(a, b):
        mag_ctx = _MagView(mm)[max(0, a - ctx):min(n_frames, b + ctx)]
        m1, m2 = hpss_masks(mag_ctx)
        off = a - max(0, a - ctx)
        return m1[off:off + (b - a)], m2[off:off + (b - a)]

    if method == "hpss":
        for a in range(0, n_frames, HPSS_CHUNK_FRAMES):
            b = min(a + HPSS_CHUNK_FRAMES, n_frames)
            m1, m2 = hpss_block(a, b)
            yield a, b, m1, m2, b == n_frames
        return

    for i, (a, b, Lb, Sb) in enumerate(
            iter_rpca_blocks(_MagView(mm), **rpca_kwargs)):
        last = b == n_frames
        if method == "rpca":
            denom = Lb * Lb + Sb * Sb + HPSS_EPS
            yield a, b, Lb * Lb / denom, Sb * Sb / denom, last
            continue
        m1h, m2h = hpss_block(a, b)
        # combined: agreement-weighted Wiener fusion
        #   stem 1 = Harmonic+LowRank, stem 2 = Percussive+Sparse
        ml = Lb * Lb / (Lb * Lb + Sb * Sb + HPSS_EPS)
        ms = Sb * Sb / (Lb * Lb + Sb * Sb + HPSS_EPS)
        t1 = np.sqrt(m1h * ml)
        t2 = np.sqrt(m2h * ms)
        denom = t1 + t2 + HPSS_EPS
        yield a, b, t1 / denom, t2 / denom, last


def compute_stems(entry_dir, method: str = "combined", progress=None) -> dict:
    """Separate the entry's cached STFT into stems and cache them as WAVs.

    Returns the status dict (with ``cached`` flag and elapsed seconds).
    Streams the memmap in row blocks; peak RAM is bounded by the block
    size, never the track length. ``progress(done, total)`` is invoked
    per work block (server-side progress polling for long runs).
    """
    _check_method(method)
    entry = Path(entry_dir)
    hit = analyze.load_entry(entry)
    if hit is None:
        raise ValueError("分析缓存缺失: STFT 未就绪")
    cached = load_status(entry, method)
    if cached is not None:
        return {**cached, "cached": True}

    started = time.time()
    mm, meta = hit
    sr, win, hop = int(meta["sr"]), int(meta["win"]), int(meta["hop"])
    n_frames = mm.shape[0]
    total_samples = max(1, int(round(float(meta["duration"]) * sr)))
    keys = STEM_SPECS[method]
    tail = win // hop  # masked rows carried across steps (contributors span)

    out_dir = stems_dir(entry, method)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir.parent / f"{method}.tmp-{os.getpid()}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    try:
        total_steps = (n_frames + HPSS_CHUNK_FRAMES - 1) // HPSS_CHUNK_FRAMES \
            if method == "hpss" else len(plan_chunks(n_frames, 400, 80))
        writers = {
            key: sf.SoundFile(str(tmp_dir / f"{key}.wav"), "w",
                              samplerate=sr, channels=1, subtype="PCM_16")
            for key in keys
        }
        written = {key: 0 for key in keys}
        tails = {key: None for key in keys}
        try:
            for step, (a, b, m1, m2, is_last) in enumerate(
                    _iter_work_blocks(mm, method, rpca_kwargs={})):
                Z = np.asarray(mm[a:b], dtype=np.complex128)
                use_tail = 0 if a == 0 else min(tail, tails[keys[0]].shape[0])
                for key, mask in ((keys[0], m1), (keys[1], m2)):
                    Y = mask * Z
                    if use_tail:
                        Y = np.concatenate([tails[key], Y], axis=0)
                    # rows [a - use_tail, b) -> finalized sample range
                    A = a - use_tail
                    y = istft_complex(Y, win=win, hop=hop, center=True)
                    s0 = 0 if a == 0 else a * hop - win // 2
                    s1 = total_samples if is_last else b * hop - win // 2
                    g_lo = max(s0 - A * hop, 0)
                    g_hi = min(s1 - A * hop, y.shape[0])
                    y = y[g_lo:max(g_hi, g_lo)]
                    if is_last or written[key] + y.shape[0] > total_samples:
                        y = y[:max(total_samples - written[key], 0)]
                    writers[key].write(np.clip(y, -1.0, 1.0))
                    written[key] += y.shape[0]
                    tails[key] = Y[-tail:]
                if progress is not None:
                    progress(step + 1, total_steps)
        finally:
            for w in writers.values():
                w.close()

        status = {
            "version": STEMS_VERSION,
            "method": method,
            "stems": list(keys),
            "sr": sr,
            "win": win,
            "hop": hop,
            "n_frames": n_frames,
            "duration": round(written[keys[0]] / float(sr), 6),
            "elapsed_sec": round(time.time() - started, 3),
            "created_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
        }
        (tmp_dir / "status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2),
            encoding="utf-8")
        if out_dir.exists():
            shutil.rmtree(out_dir)
        os.replace(tmp_dir, out_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return {**status, "cached": False}


def get_stems(entry: Path, method: str = "combined", progress=None,
              lock=None) -> dict:
    """Status dict for ``method`` with double-checked locking around
    compute so concurrent requests share one computation."""
    cached = load_status(entry, method)
    if cached is not None:
        return {**cached, "cached": True}
    with (lock or threading.Lock()):
        cached = load_status(entry, method)  # re-check inside the lock
        if cached is not None:
            return {**cached, "cached": True}
        return compute_stems(entry, method, progress=progress)

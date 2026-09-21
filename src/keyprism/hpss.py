#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism HPSS: median-filter Harmonic-Percussive Source Separation
(Phase 2)

Pure algorithms only — numpy arrays in, numpy arrays out, zero IO (same
discipline as ``dsp`` / ``transform`` / ``salience`` / ``decode``).

Given a magnitude spectrogram ``mag`` of shape ``(n_frames, n_bins)``
(time-major, the layout of the Phase 0 cached ``stft.npy``), two median
filtered estimates are formed:

- harmonic estimate ``H = medfilt2d(mag, (Kt, 1))``: a long median along
  TIME flattens percussive transients while horizontal harmonic lines
  (stable pitch content) survive;
- percussive estimate ``P = medfilt2d(mag, (1, Kf))``: a long median along
  FREQUENCY within one frame flattens narrow-band harmonic peaks while
  broadband transients survive.

Kernel tuples follow the librosa ``(freq, time)`` convention in the public
signature (``win_harm=(1, 31)`` means 31 frames of temporal context); they
are transposed internally to match the time-major matrix layout.

Soft Wiener masks (Fitzgerald 2010): ``M_h = H^2 / (H^2 + P^2 + eps)`` and
``M_p = P^2 / (H^2 + P^2 + eps)`` with ``eps = 1e-10`` guarding silent
cells. The masks are real-valued in [0, 1] and are meant to be applied to
the COMPLEX STFT (preserving the original phase) by the stems orchestrator.

Chunking note for callers: the temporal medians reach ``Kt // 2`` frames
across the chunk boundary, so a chunked caller must feed real magnitude
rows from the neighbouring chunks (plus the natural zero padding at the
track edges, which matches a full-track ``medfilt2d``) and keep only the
interior rows of the result — ``stems.py`` does exactly that, which makes
the streamed masks bit-equal to a hypothetical full-track computation.
"""

import numpy as np
from scipy.signal import medfilt2d

__all__ = ["hpss_masks", "HPSS_EPS", "hpss_context_frames"]

#: Numerical floor inside the Wiener mask denominators (spec constant).
HPSS_EPS = 1e-10


def _odd(k: int, what: str) -> int:
    k = int(k)
    if k < 1 or k % 2 == 0:
        raise ValueError(f"{what} 必须为正奇数: {k}")
    return k


def hpss_context_frames(win_harm=(1, 31)) -> int:
    """Temporal context (frames) a chunked caller must provide on each
    side so interior rows equal the full-track median filter."""
    return _odd(win_harm[1], "win_harm[1]") // 2


def hpss_masks(mag, win_harm=(1, 31), win_perc=(31, 1)):
    """Wiener HPSS soft masks of a magnitude spectrogram.

    ``mag`` is ``(n_frames, n_bins)`` time-major (e.g. ``|stft.npy|``).
    ``win_harm`` / ``win_perc`` follow the librosa ``(freq, time)`` kernel
    convention (defaults: 31 frames of temporal smoothing for the harmonic
    estimate, 31 bins of frequency smoothing for the percussive estimate);
    all components must be odd.

    Returns ``(mask_harm, mask_perc)``, two float64 matrices of the same
    shape with ``mask_harm + mask_perc <= 1`` (sum is ``1 - eps/(H^2+P^2+eps)``,
    i.e. 1 everywhere but in near-silence).
    """
    m = np.asarray(mag, dtype=np.float64)
    if m.ndim != 2:
        raise ValueError("mag 形状须为 (n_frames, n_bins)")
    kt = _odd(win_harm[1], "win_harm[1]")   # temporal extent -> rows (time)
    kf = _odd(win_perc[0], "win_perc[0]")   # spectral extent -> cols (freq)
    harm = medfilt2d(m, kernel_size=(kt, 1))
    perc = medfilt2d(m, kernel_size=(1, kf))
    h2 = harm * harm
    p2 = perc * perc
    denom = h2 + p2 + HPSS_EPS
    return h2 / denom, p2 / denom

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism onset detection: band-limited spectral flux with adaptive
threshold and refractory period (Phase 1)

Pure algorithms only — numpy arrays in, numpy arrays out, zero IO.

Algorithm (preset-driven, see ``tracks.TrackPreset``):

1. Positive magnitude flux restricted to ``preset.onset_band_hz``:
       flux[t] = sum over band bins of max(0, mag[t] - mag[t-1])
2. Adaptive threshold: moving median over a 1.5 s centered window
       + 0.15 * moving standard deviation (same window).
3. Peak picking: flux must be a local maximum over a +/-30 ms neighborhood
   and exceed the threshold; a picked peak enforces a refractory period of
   ``preset.min_ioi_s`` (earlier peak wins, first come first kept —
   deterministic).

Memory discipline: the full magnitude matrix is never required. The
production path (``transcribe``) streams the cached STFT row chunks through
:func:`flux_chunk` (carrying only the previous frame's magnitude row) and
calls :func:`pick_onset_frames` on the resulting flux array (a float array
of ``n_frames`` — trivially small). :func:`detect_onsets` is the
convenience wrapper over both for in-memory magnitude matrices.
"""

import numpy as np
from scipy.ndimage import median_filter, uniform_filter1d

from .tracks import TrackPreset

__all__ = [
    "detect_onsets", "flux_chunk", "pick_onset_frames",
]

#: Seconds -> algorithm constants
_THRESHOLD_HALF_WINDOW_S = 0.75  # 1.5 s centered median window
_THRESHOLD_STD_FACTOR = 0.15
_LOCAL_MAX_HALF_WINDOW_S = 0.03


def _band_bins(sr: int, win: int, preset: TrackPreset) -> tuple:
    """[lo, hi) rfft bin range of ``preset.onset_band_hz`` (clamped)."""
    bins = win // 2 + 1
    lo = int(np.floor(preset.onset_band_hz[0] * win / sr))
    hi = int(np.ceil(preset.onset_band_hz[1] * win / sr))
    return max(1, min(lo, bins - 1)), max(2, min(hi, bins))


def flux_chunk(mag: np.ndarray, prev_row, sr: int, win: int,
               preset: TrackPreset) -> np.ndarray:
    """Positive magnitude flux of a frame slice ``mag`` (frames, bins).

    ``prev_row`` is the magnitude row preceding the slice (``None`` at the
    timeline start -> the first frame's flux is 0). Returns a float64 array
    of ``len(mag)`` values; successive chunks must be fed with the last row
    of the previous chunk (``mag[-1]``) so flux is continuous across chunk
    boundaries. Pure function: chunk content alone determines the output
    given the same predecessor row.
    """
    m = np.asarray(mag, dtype=np.float64)
    lo, hi = _band_bins(sr, win, preset)
    band = m[:, lo:hi]
    if prev_row is None:
        prev = np.concatenate([band[:1] * 0.0, band[:-1]], axis=0)
    else:
        prev = np.concatenate(
            [np.asarray(prev_row, dtype=np.float64)[None, lo:hi],
             band[:-1]], axis=0)
    return np.maximum(band - prev, 0.0).sum(axis=1)


def _adaptive_threshold(flux: np.ndarray, sr: int, hop: int) -> np.ndarray:
    """Moving median (1.5 s) + 0.15 * moving std, centered, edge-padded."""
    half = max(1, int(round(_THRESHOLD_HALF_WINDOW_S * sr / hop)))
    size = 2 * half + 1
    med = median_filter(flux, size=size, mode="nearest")
    mean = uniform_filter1d(flux, size=size, mode="nearest")
    mean_sq = uniform_filter1d(flux * flux, size=size, mode="nearest")
    std = np.sqrt(np.maximum(mean_sq - mean * mean, 0.0))
    return med + _THRESHOLD_STD_FACTOR * std


def pick_onset_frames(flux: np.ndarray, sr: int, hop: int,
                      preset: TrackPreset) -> list:
    """Peak picking over a full flux array; returns sorted frame indices.

    Deterministic: leftmost frame of a plateau wins (>= left, > right) and
    the refractory period keeps the earlier peak."""
    flux = np.asarray(flux, dtype=np.float64)
    n = flux.shape[0]
    if n < 3 or not np.isfinite(flux).all() or flux.max() <= 0:
        return []
    thr = _adaptive_threshold(flux, sr, hop)
    half = max(1, int(round(_LOCAL_MAX_HALF_WINDOW_S * sr / hop)))
    refractory = max(1, int(round(preset.min_ioi_s * sr / hop)))
    onsets = []
    last = -refractory
    for f in range(1, n - 1):
        v = flux[f]
        if v <= thr[f]:
            continue
        lo = max(0, f - half)
        hi = min(n, f + half + 1)
        window = flux[lo:hi]
        # >= against the left half, > against the right half (plateau ->
        # leftmost frame)
        if v < window[:f - lo].max(initial=0.0) or \
                v <= window[f - lo + 1:].max(initial=0.0):
            continue
        if f - last < refractory:
            continue
        onsets.append(f)
        last = f
    return onsets


def detect_onsets(mag: np.ndarray, sr: int, hop: int,
                  preset: TrackPreset) -> list:
    """Onset frame indices from an in-memory magnitude matrix.

    Convenience path for small inputs and tests; the production streaming
    equivalent is ``flux_chunk`` + ``pick_onset_frames`` (identical math)."""
    m = np.asarray(mag, dtype=np.float64)
    if m.ndim != 2:
        raise ValueError("mag 形状须为 (frames, bins)")
    parts = []
    prev = None
    for a in range(0, m.shape[0], 512):
        parts.append(flux_chunk(m[a:a + 512], prev, sr,
                                2 * (m.shape[1] - 1), preset))
        prev = m[min(a + 512, m.shape[0]) - 1]
    return pick_onset_frames(np.concatenate(parts), sr, hop, preset)

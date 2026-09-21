#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism transform layer: complex STFT/ISTFT, dB magnitude, peak refine

Phase 0 foundation: the complex-valued STFT becomes the internal source of
truth for later source-separation and transcription work. Pure algorithms
only — numpy arrays in, numpy arrays out; the only import side effect is
scipy's window factory.

Numeric contract (locked by tests/test_transform.py):
- Framing and scaling are byte-identical to the legacy
  ``scipy.signal.stft(x, fs=sr, window="hann", nperseg=win,
  noverlap=win*3//4, padded=True, boundary="zeros")`` call that
  ``keyprism.dsp.stft_power`` used before the extraction:
  periodic hann, ``win//2`` zeros of boundary padding on both sides
  (center=True), tail padding so no frame is clipped, and 'spectrum'
  scaling (divide by ``win.sum()``).
- hop defaults to ``win - win*3//4`` (== ``win//4`` for power-of-two
  windows), matching the legacy ``noverlap = nperseg*3//4`` derivation.
- ``magnitude_db`` reproduces the payload dB semantics exactly:
  power-domain ``10*log10(m/peak)`` floored at 1e-12 and clamped to
  ``[-db_range, 0]``.

Memory discipline: ``iter_stft_chunks`` streams windowed frame blocks so the
analysis cache can fill a memmap chunk by chunk; the one-shot helpers here
are convenience wrappers sized for small inputs, not the pipeline path.
"""

import numpy as np
from scipy import signal

__all__ = [
    "stft_complex", "stft_pair", "istft_complex", "magnitude_db",
    "parabolic_peak", "iter_stft_chunks", "n_frames_of", "default_hop",
]

DEFAULT_WIN = 8192
CHUNK_FRAMES = 512  # frame block size for streamed STFT/ISTFT (<= 2048)


def default_hop(win: int) -> int:
    """Legacy hop: ``nperseg - nperseg*3//4`` (== win//4 for even win)."""
    return win - win * 3 // 4


_default_hop = default_hop  # internal alias


def _window(window, win: int) -> np.ndarray:
    """Periodic window (fftbins=True), what scipy.signal.stft uses."""
    return signal.get_window(window, win, fftbins=True)


def n_frames_of(n: int, win: int, hop: int, center: bool) -> int:
    """Number of STFT frames for an n-sample signal under the legacy
    framing (boundary + tail padding included)."""
    if center:
        n += win  # win//2 zeros on both sides
    if n < win:
        raise ValueError(f"win {win} 大于输入长度 {n}")
    nadd = (-(n - win) % hop) % win
    return 1 + (n + nadd - win) // hop


def iter_stft_chunks(x, *, win=DEFAULT_WIN, hop=None, window="hann",
                     center=True, chunk_frames=CHUNK_FRAMES):
    """Yield ``(start_frame, Z_chunk)`` row blocks of the float64 complex
    STFT without ever materializing the full matrix.

    Z_chunk is ``(k, win//2+1)`` complex128 (frames × freq bins) with
    ``k <= chunk_frames`` — the natural row-chunk shape for filling a
    ``(n_frames, win//2+1)`` memmap. Concatenating the yielded blocks in
    order reproduces the full STFT bit for bit (same per-row pocketfft
    transforms as a batched call).
    """
    hop = _default_hop(win) if hop is None else int(hop)
    if not 1 <= hop < win:
        raise ValueError(f"hop 必须在 [1, win) 内: {hop}")
    w = _window(window, win)
    scale = w.sum()
    x = np.ascontiguousarray(x, dtype=np.float64)
    if center:
        x = np.concatenate([np.zeros(win // 2, dtype=np.float64), x,
                            np.zeros(win // 2, dtype=np.float64)])
    nadd = (-(x.shape[0] - win) % hop) % win
    if nadd:
        x = np.concatenate([x, np.zeros(nadd, dtype=np.float64)])
    if x.shape[0] < win:
        raise ValueError(f"win {win} 大于输入长度 {x.shape[0]}")
    frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop]
    for c0 in range(0, frames.shape[0], chunk_frames):
        block = frames[c0:c0 + chunk_frames] * w  # (k, win) float64
        yield c0, np.fft.rfft(block, axis=1) / scale


def _collect(x, *, win, hop, window, center) -> np.ndarray:
    parts = []
    for _, chunk in iter_stft_chunks(x, win=win, hop=hop, window=window,
                                     center=center, chunk_frames=CHUNK_FRAMES):
        parts.append(chunk)
    return np.concatenate(parts, axis=0) if parts else \
        np.empty((0, win // 2 + 1), dtype=np.complex128)


def stft_complex(x, *, win=DEFAULT_WIN, hop=None, window="hann",
                 center=True) -> np.ndarray:
    """Complex STFT -> complex64 ``(n_frames, win//2+1)`` (time, freq).

    Time-major so each frame is one contiguous memmap row."""
    return _collect(x, win=win, hop=hop, window=window,
                    center=center).astype(np.complex64)


def stft_pair(x, sr, *, win=DEFAULT_WIN, hop=None, window="hann",
              center=True):
    """``(freqs, Z float64-complex128)`` in the legacy freq-major
    ``(win//2+1, n_frames)`` layout — the bit-exact core used by
    ``dsp.stft_power`` (freqs == ``np.fft.rfftfreq(win, 1/sr)``)."""
    hop = _default_hop(win) if hop is None else int(hop)
    z = _collect(x, win=win, hop=hop, window=window, center=center)
    return np.fft.rfftfreq(win, 1.0 / float(sr)), z.T


def istft_complex(X, *, win, hop=None, window="hann", center=True,
                  length=None) -> np.ndarray:
    """Inverse STFT via overlap-add with squared-window normalization.

    ``X`` is ``(n_frames, win//2+1)`` as produced by ``stft_complex`` /
    ``stft_pair``. Exact (to float precision) for windows whose square
    satisfies COLA at the chosen hop — hann at hop = win//4. Edge frames
    have a partial normalization envelope; pass ``length`` (the original
    sample count) to trim to the valid region, or compare interior samples
    only.
    """
    X = np.asarray(X)
    if X.ndim != 2:
        raise ValueError("X 形状须为 (n_frames, win//2+1)")
    hop = _default_hop(win) if hop is None else int(hop)
    w = _window(window, win)
    scale = w.sum()
    w2 = w * w
    n_frames = X.shape[0]
    out_len = win + hop * (n_frames - 1)
    out = np.zeros(out_len, dtype=np.float64)
    norm = np.zeros(out_len, dtype=np.float64)
    for c0 in range(0, n_frames, CHUNK_FRAMES):
        block = np.ascontiguousarray(X[c0:c0 + CHUNK_FRAMES],
                                     dtype=np.complex128)
        segs = np.fft.irfft(block, n=win, axis=1) * (scale * w)
        for i, seg in enumerate(segs):
            pos = (c0 + i) * hop
            out[pos:pos + win] += seg
            norm[pos:pos + win] += w2
    y = np.zeros(out_len, dtype=np.float64)
    np.divide(out, norm, out=y, where=norm > 1e-12 * max(norm.max(), 1.0))
    if center:
        half = win // 2
        y = y[half:out_len - (win - half)]
        if length is not None:
            y = y[:length]
    elif length is not None:
        y = y[:length]
    return y


def magnitude_db(X, db_range=70.0, peak=None) -> np.ndarray:
    """Legacy dB semantics on a power-domain matrix (e.g. ``|Z|**2`` or a
    semitone aggregate): ``10*log10(max(m/peak, 1e-12))`` clamped to
    ``[-db_range, 0]``.

    ``peak`` defaults to the matrix max; the payload's cross-channel joint
    peak (unified normalization across mix/left/right) is passed explicitly
    by callers that need it. Silence raises, exactly like the payload layer.
    """
    m = np.asarray(X, dtype=np.float64)
    if peak is None:
        peak = m.max()
    if peak <= 0:
        raise ValueError("音频为静音")
    return np.maximum(10.0 * np.log10(np.maximum(m / peak, 1e-12)), -db_range)


def parabolic_peak(mag_row, k):
    """Quadratic interpolation around bin ``k`` of a magnitude row.

    Returns ``(delta, height)``: ``delta`` is the sub-bin peak offset in
    ``(-0.5, 0.5)`` relative to ``k`` and ``height`` the interpolated peak
    value. At the row edges the offset is 0 and the raw value is returned.
    (Implemented for Phase 2 transcription; unused this phase.)
    """
    row = np.asarray(mag_row, dtype=np.float64)
    if k <= 0 or k >= row.shape[0] - 1:
        return 0.0, float(row[k])
    a, b, c = row[k - 1], row[k], row[k + 1]
    denom = a - 2.0 * b + c
    if denom == 0.0:
        return 0.0, float(b)
    delta = 0.5 * (a - c) / denom
    return float(delta), float(b - 0.25 * (a - c) * delta)

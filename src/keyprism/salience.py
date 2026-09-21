#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism salience layer: loudness weighting + harmonic-stack pitch
salience for monophonic transcription (Phase 1)

Pure algorithms only — numpy arrays in, numpy arrays out, zero IO (same
discipline as ``dsp`` / ``transform``).

Pipeline position: cached complex64 STFT frames -> |Z| magnitude ->
A-weighted magnitude (:func:`apply_loudness_weighting`, applied once per
track computation) -> per-pitch harmonic salience with subharmonic
suppression (:func:`compute_salience`). Both functions are pure functions
of a frame slice: chunk boundaries never change any returned value, which
is what keeps the two-pass global normalization in ``transcribe``
deterministic and chunk-order independent.

Formulas (documented per the Phase 1 spec):

- A-weighting (simplified: the standard IEC 61672 single-formula response
  with its dB curve compressed to 1/4, normalized to 0 dB at 1 kHz):

      R(f)    = 12194^2 * f^4 /
                ((f^2 + 20.6^2)
                 * sqrt((f^2 + 107.7^2) * (f^2 + 737.9^2))
                 * (f^2 + 12194^2))
      A(f)    = R(f) / R(1000 Hz)         (standard linear response)
      gain(f) = A(f) ** (1/4)             (compressed magnitude weight)

  Why the 1/4 compression: full A-weighting spans ~18 dB across the four
  octaves of a preset range (e.g. C3..C6), but the salience is normalized
  by ONE global min-max pair and competes against a fixed REST emission —
  quiet low notes would always fall below the REST threshold. Compressing
  the dB response to 4.5 dB keeps a meaningful low-frequency roll-off
  (kick / rumble rejection in the onset band) while letting the whole
  preset range decode.

- Harmonic salience on the semitone grid of ``preset.midi_range``
  (internally extended 12 semitones downwards for the suppression term):

      E(f, t)  = mean of the +/-1-bin neighborhood of the nearest bin of f
      S(f0, t) = sum_{k=1..n_harm} w_k * E(k*f0, t),   w_k = 1/k
      S'(f0,t) = max(0, S(f0, t) - alpha * S(f0/2, t))
                 (f0/2 below the grid -> no suppression)

- Global min-max normalization to [0, 1] is a two-pass step over chunks
  owned by the orchestrator (``transcribe``): pass 1 tracks exact min/max
  (commutative, order independent), pass 2 applies (x - min) / (max - min)
  element-wise. Element-wise arithmetic cannot depend on chunk order, so
  the result is byte-deterministic.
"""

import numpy as np

from .tracks import TrackPreset

__all__ = [
    "a_weighting_gain", "apply_loudness_weighting", "compute_salience",
    "pitch_grid", "A_WEIGHT_CURVES",
]

#: Loudness curves supported by :func:`apply_loudness_weighting` (registry
#: style like MONO_TRACKS: a new curve is one new entry + one gain function
#: branch, nothing else).
A_WEIGHT_CURVES = ("a",)

#: dB-response compression exponent applied to the standard A curve (see
#: the module docstring for why full A-weighting cannot be used as-is).
_A_COMPRESSION = 1.0 / 4.0

_A_REF_HZ = 1000.0


def _a_weight_raw(freqs: np.ndarray) -> np.ndarray:
    """Un-normalized A-weighting response R(f) (linear magnitude scale)."""
    f2 = np.asarray(freqs, dtype=np.float64) ** 2
    num = 12194.0 ** 2 * f2 * f2
    den = ((f2 + 20.6 ** 2)
           * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
           * (f2 + 12194.0 ** 2))
    return num / den


def a_weighting_gain(freqs: np.ndarray) -> np.ndarray:
    """Per-bin linear magnitude gain of the (1/4-compressed) A-weighting
    curve, 1.0 at 1 kHz.

    Pure function of the frequency array; see the module docstring for the
    formula."""
    raw = _a_weight_raw(freqs)
    standard = raw / _a_weight_raw(np.array([_A_REF_HZ]))[0]
    return standard ** _A_COMPRESSION


def apply_loudness_weighting(mag: np.ndarray, sr: int, curve: str = "a"):
    """Apply a per-bin loudness gain to a magnitude matrix.

    ``mag`` is ``(frames, bins)`` — the |Z| of an rfft STFT (so
    ``win = 2*(bins-1)`` and ``freqs = rfftfreq(win, 1/sr)``). Returns a
    float64 weighted copy; the gain is applied once per track computation,
    before any track salience (never to the cached STFT artifact itself).
    """
    if curve not in A_WEIGHT_CURVES:
        raise ValueError(f"未知响度曲线: {curve} (可用: {A_WEIGHT_CURVES})")
    m = np.asarray(mag, dtype=np.float64)
    if m.ndim != 2:
        raise ValueError("mag 形状须为 (frames, bins)")
    bins = m.shape[1]
    freqs = np.fft.rfftfreq(2 * (bins - 1), 1.0 / float(sr))
    return m * a_weighting_gain(freqs)[None, :]


def pitch_grid(preset: TrackPreset) -> np.ndarray:
    """Semitone grid of the preset, extended 12 semitones downwards.

    The extension provides S(f0/2) for the subharmonic suppression of the
    lowest in-range pitches; only the preset's own range is returned to
    callers of :func:`compute_salience`."""
    lo, hi = preset.midi_range
    return np.arange(lo - 12, hi + 1, dtype=np.int64)


def _harmonic_bin_indices(preset: TrackPreset, sr: int, win: int):
    """Nearest rfft bin of every k*f0 on the extended pitch grid.

    Returns ``(idx, valid)``: bin index clipped into [1, bins-2] so the
    +/-1-bin neighborhood is always in range, and a boolean mask marking
    harmonics at/above Nyquist (energy absent -> E = 0)."""
    grid = pitch_grid(preset)
    freqs = 440.0 * 2.0 ** ((grid[None, :] - 69.0) / 12.0) \
        * np.arange(1, preset.n_harm + 1, dtype=np.float64)[:, None]
    nyquist = sr / 2.0
    valid = freqs < nyquist
    idx = np.rint(freqs * win / float(sr)).astype(np.int64)
    idx = np.clip(idx, 1, win // 2 + 1 - 2)
    return idx, valid


def compute_salience(mag: np.ndarray, sr: int, win: int, hop: int,
                     preset: TrackPreset) -> np.ndarray:
    """Harmonic salience S' for one frame slice.

    ``mag`` is the loudness-weighted magnitude ``(frames, bins)`` of a
    consecutive frame slice (any slice of the full timeline — this function
    never looks across chunks). Returns float32 ``(frames, n_pitches)`` on
    the preset's semitone grid, NOT yet globally normalized (the two-pass
    min-max normalization is the orchestrator's job; see module docstring).

    ``hop`` is accepted for API completeness with the Phase 1 spec — the
    salience itself is frame-local and does not use it.
    """
    m = np.asarray(mag, dtype=np.float64)
    if m.ndim != 2:
        raise ValueError("mag 形状须为 (frames, bins)")
    if preset.harm_weight != "1/k":
        raise ValueError(f"未知谐波权重: {preset.harm_weight}")
    grid = pitch_grid(preset)
    n_p = grid.shape[0]
    idx, valid = _harmonic_bin_indices(preset, sr, win)

    frames = m.shape[0]
    s_ext = np.zeros((frames, n_p), dtype=np.float64)
    # E(k*f0, t): mean of the +/-1-bin neighborhood, summed with 1/k weight
    for j in range(n_p):
        acc = np.zeros(frames, dtype=np.float64)
        for k in range(preset.n_harm):
            if not valid[k, j]:
                continue
            b = idx[k, j]
            acc += m[:, b - 1:b + 2].mean(axis=1) / (k + 1.0)
        s_ext[:, j] = acc

    # Subharmonic suppression: S' = max(0, S - alpha * S(f0/2)); the 12
    # leading grid columns are the extended part (their f0/2 lies below the
    # grid and is not returned, but they suppress nothing either — the
    # first 12 returned pitches suppress against these, the extended ones
    # themselves get no suppression term).
    alpha = float(preset.subharmonic_alpha)
    s = s_ext[:, 12:] - alpha * s_ext[:, :n_p - 12]
    np.maximum(s, 0.0, out=s)
    return s.astype(np.float32)

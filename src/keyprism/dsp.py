#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism DSP core: audio -> STFT -> piano semitone aggregate spectrum

Pure algorithm layer, no IO whatsoever: numpy arrays in, numpy arrays out.
Includes STFT, semitone aggregation, subband subdivision, time downsampling
and BPM estimation.
"""

import numpy as np
from scipy import signal
from scipy.ndimage import convolve1d

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MIDI_MIN = 21   # A0, lowest piano note
MIDI_MAX = 108  # C8, highest piano note

TIME_RATES = [5, 10, 15, 30]  # time columns per second options
SUB_OPTIONS = [1, 5, 10]      # subbands per semitone options


def midi_to_freq(m):
    """MIDI note number -> frequency (Hz), A4=440Hz"""
    return 440.0 * 2.0 ** ((np.asarray(m, dtype=float) - 69.0) / 12.0)


def note_name(midi: int) -> str:
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def stft_power(x: np.ndarray, sr: int, nperseg: int):
    """STFT -> (frequency array, power spectrogram [freq, time])"""
    f, _, Z = signal.stft(
        x, fs=sr, window="hann", nperseg=nperseg, noverlap=nperseg * 3 // 4,
        padded=True, boundary="zeros",
    )
    return f, np.abs(Z) ** 2


def power_to_pitch_bins(freqs: np.ndarray, power: np.ndarray) -> np.ndarray:
    """Accumulate the linear-frequency power spectrum into the 88 piano
    semitone bins"""
    n_notes = MIDI_MAX - MIDI_MIN + 1
    out = np.empty((n_notes, power.shape[1]), dtype=power.dtype)
    for i, m in enumerate(range(MIDI_MIN, MIDI_MAX + 1)):
        lo = midi_to_freq(m - 0.5)
        hi = midi_to_freq(m + 0.5)
        band = (freqs >= lo) & (freqs < hi)
        if band.any():
            out[i] = power[band].sum(axis=0)
        else:  # band narrower than the FFT resolution: take the nearest bin
            idx = int(np.argmin(np.abs(freqs - midi_to_freq(m))))
            out[i] = power[idx]
    return out


def downsample_max(spec: np.ndarray, max_cols: int = 3600) -> np.ndarray:
    """Compress excess time columns by block-wise max, preserving transient
    peaks"""
    if spec.shape[1] <= max_cols:
        return spec
    block = int(np.ceil(spec.shape[1] / max_cols))
    pad = (-spec.shape[1]) % block
    padded = np.pad(spec, ((0, 0), (0, pad)), mode="constant")
    return padded.reshape(padded.shape[0], -1, block).max(axis=2)


def estimate_bpm(power: np.ndarray, hop: float) -> tuple[float, float]:
    """Estimate BPM and first-beat offset via spectral-flux autocorrelation
    (rough initial values, meant for manual frontend fine-tuning)"""
    mag = np.sqrt(power)
    flux = np.maximum(np.diff(mag, axis=1), 0.0).sum(axis=0)
    if flux.size < 64 or flux.max() <= 0:
        return 120.0, 0.0
    flux = flux - flux.mean()
    n = flux.size
    # FFT autocorrelation (Wiener)
    ac = np.fft.irfft(np.abs(np.fft.rfft(flux, 2 * n)) ** 2)[: n // 2]
    if ac.max() <= 0:
        return 120.0, 0.0
    ac /= ac.max()
    bpms = np.arange(50.0, 240.5, 0.5)
    lags = 60.0 / bpms / hop  # lag in frames for each candidate BPM
    vals = np.interp(lags, np.arange(ac.size), ac)
    prior = np.where((bpms >= 70) & (bpms <= 180), 1.0, 0.7)  # common-range weighting
    bpm = float(bpms[np.argmax(vals * prior)])
    # Phase: comb filter over the beat pulse train to locate the first beat
    period_f = 60.0 / bpm / hop
    xs = np.arange(flux.size)
    best_off, best_score = 0.0, -np.inf
    for ph in np.arange(0, period_f, max(period_f / 48.0, 0.5)):
        idx = ph + np.arange(int((flux.size - ph) / period_f)) * period_f
        if idx.size == 0:
            continue
        score = float(np.interp(idx, xs, flux).sum())
        if score > best_score:
            best_score, best_off = score, float(ph)
    return round(bpm, 1), round(best_off * hop, 3)


def pitch_bins_sub(freqs: np.ndarray, power: np.ndarray, sub: int):
    """Split each semitone band geometrically into sub subbands (sub=1 is
    plain semitone aggregation)"""
    if sub == 1:
        return power_to_pitch_bins(freqs, power)
    rows = []
    for m in range(MIDI_MIN, MIDI_MAX + 1):
        f_lo = midi_to_freq(m - 0.5)
        f_hi = midi_to_freq(m + 0.5)
        edges = f_lo * (f_hi / f_lo) ** (np.arange(sub + 1) / sub)
        for k in range(sub):
            band = (freqs >= edges[k]) & (freqs < edges[k + 1])
            if band.any():
                rows.append(power[band].sum(axis=0))
            else:
                idx = int(np.argmin(np.abs(freqs - np.sqrt(edges[k] * edges[k + 1]))))
                rows.append(power[idx])
    return np.vstack(rows)


def widen_rows(mat: np.ndarray, sub: int) -> np.ndarray:
    """Mild energy widening along the subband row axis (triangular kernel,
    total width about one semitone).

    With more subbands each one covers only a very narrow frequency range and
    the fragmented energy becomes hard to read; convolving with a triangular
    kernel along the frequency axis in the power domain makes the information
    visually continuous."""
    if sub <= 1:
        return mat
    half = max(1, sub // 2)
    k = np.concatenate([np.arange(1, half + 1), np.arange(half, 0, -1)])
    k = k / k.sum()
    return convolve1d(mat, k, axis=0, mode="nearest")


def spec_matrix(x: np.ndarray, sr: int, window: int, max_cols: int, sub: int):
    """Mono signal -> (88*sub x time) power matrix and column spacing (after
    downsampling)"""
    freqs, power = stft_power(x, sr, window)
    frames = power.shape[1]
    hop_frame = len(x) / sr / max(frames - 1, 1)
    pitch = widen_rows(pitch_bins_sub(freqs, power, sub), sub)
    m = downsample_max(pitch, max_cols)
    block = max(1, int(np.ceil(frames / max_cols)))  # frames per column
    return m, hop_frame * block, power

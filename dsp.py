#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism DSP 核心: 音频 -> STFT -> 钢琴半音聚合频谱"""

import numpy as np
from scipy import signal

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MIDI_MIN = 21   # A0, 钢琴最低音
MIDI_MAX = 108  # C8, 钢琴最高音


def midi_to_freq(m):
    """MIDI 音符号 -> 频率 (Hz), A4=440Hz"""
    return 440.0 * 2.0 ** ((np.asarray(m, dtype=float) - 69.0) / 12.0)


def note_name(midi: int) -> str:
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def stft_power(x: np.ndarray, sr: int, nperseg: int):
    """STFT -> (频率数组, 功率谱 [freq, time])"""
    f, _, Z = signal.stft(
        x, fs=sr, window="hann", nperseg=nperseg, noverlap=nperseg * 3 // 4,
        padded=True, boundary="zeros",
    )
    return f, np.abs(Z) ** 2


def power_to_pitch_bins(freqs: np.ndarray, power: np.ndarray) -> np.ndarray:
    """把线性频率功率谱累加到 88 个钢琴半音格"""
    n_notes = MIDI_MAX - MIDI_MIN + 1
    out = np.empty((n_notes, power.shape[1]), dtype=power.dtype)
    for i, m in enumerate(range(MIDI_MIN, MIDI_MAX + 1)):
        lo = midi_to_freq(m - 0.5)
        hi = midi_to_freq(m + 0.5)
        band = (freqs >= lo) & (freqs < hi)
        if band.any():
            out[i] = power[band].sum(axis=0)
        else:  # 频带比 FFT 分辨率还窄时, 取最近的一个 bin
            idx = int(np.argmin(np.abs(freqs - midi_to_freq(m))))
            out[i] = power[idx]
    return out


def downsample_max(spec: np.ndarray, max_cols: int = 3600) -> np.ndarray:
    """时间列数过多时按块取最大值压缩, 保留瞬态峰值"""
    if spec.shape[1] <= max_cols:
        return spec
    block = int(np.ceil(spec.shape[1] / max_cols))
    pad = (-spec.shape[1]) % block
    padded = np.pad(spec, ((0, 0), (0, pad)), mode="constant")
    return padded.reshape(padded.shape[0], -1, block).max(axis=2)

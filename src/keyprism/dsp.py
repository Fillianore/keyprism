#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism DSP 核心: 音频 -> STFT -> 钢琴半音聚合频谱

纯算法层, 不做任何 IO: 输入 numpy 数组, 输出 numpy 数组。
包含 STFT、半音聚合、子带细分、时间降采样与 BPM 估计。
"""

import numpy as np
from scipy import signal
from scipy.ndimage import convolve1d

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MIDI_MIN = 21   # A0, 钢琴最低音
MIDI_MAX = 108  # C8, 钢琴最高音

TIME_RATES = [5, 10, 15, 30]  # 每秒时间列数选项
SUB_OPTIONS = [1, 5, 10]      # 每半音子带数选项


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


def estimate_bpm(power: np.ndarray, hop: float) -> tuple[float, float]:
    """频谱通量自相关估计 BPM 与首拍偏移 (粗略初值, 供前端手动微调)"""
    mag = np.sqrt(power)
    flux = np.maximum(np.diff(mag, axis=1), 0.0).sum(axis=0)
    if flux.size < 64 or flux.max() <= 0:
        return 120.0, 0.0
    flux = flux - flux.mean()
    n = flux.size
    # FFT 自相关 (Wiener)
    ac = np.fft.irfft(np.abs(np.fft.rfft(flux, 2 * n)) ** 2)[: n // 2]
    if ac.max() <= 0:
        return 120.0, 0.0
    ac /= ac.max()
    bpms = np.arange(50.0, 240.5, 0.5)
    lags = 60.0 / bpms / hop  # 每个候选 BPM 对应的滞后帧数
    vals = np.interp(lags, np.arange(ac.size), ac)
    prior = np.where((bpms >= 70) & (bpms <= 180), 1.0, 0.7)  # 常见区间加权
    bpm = float(bpms[np.argmax(vals * prior)])
    # 相位: 梳状滤波, 对齐节拍脉冲串找首拍位置
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
    """把每个半音频带几何等分为 sub 个子带 (sub=1 即普通半音聚合)"""
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
    """子带行方向的轻度能量展宽 (三角核, 总宽约一个半音)。

    子带变多后每个子带只覆盖极窄频段, 能量碎片化难以分辨;
    在功率域沿频率轴做三角核卷积, 让信息在视觉上连续起来。"""
    if sub <= 1:
        return mat
    half = max(1, sub // 2)
    k = np.concatenate([np.arange(1, half + 1), np.arange(half, 0, -1)])
    k = k / k.sum()
    return convolve1d(mat, k, axis=0, mode="nearest")


def spec_matrix(x: np.ndarray, sr: int, window: int, max_cols: int, sub: int):
    """单声道信号 -> (88*sub x 时间) 功率矩阵与列距 (降采样后)"""
    freqs, power = stft_power(x, sr, window)
    frames = power.shape[1]
    hop_frame = len(x) / sr / max(frames - 1, 1)
    pitch = widen_rows(pitch_bins_sub(freqs, power, sub), sub)
    m = downsample_max(pitch, max_cols)
    block = max(1, int(np.ceil(frames / max_cols)))  # 每列包含的帧数
    return m, hop_frame * block, power

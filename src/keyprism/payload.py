#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism 前端契约层: 分析结果 -> data.json payload

职责: 把解码后的 PCM 变成前端直接消费的 JSON 结构——
三通道量化频谱 (uint8 base64)、包络图 (matplotlib 渲染 data URL)、
色标表、BPM/曲目元信息。前端 data.json 字段的唯一权威定义处。
"""

import base64
import io
import os
from pathlib import Path

import numpy as np

from .audio_io import KEYPRISM_HOME, browser_safe_audio, load_channels
from .dsp import (
    MIDI_MAX, MIDI_MIN, SUB_OPTIONS, TIME_RATES, estimate_bpm, note_name,
    spec_matrix, stft_power,
)

# matplotlib 的字体/配置缓存收进工作区, 不污染用户配置目录
# (必须在 import matplotlib 之前设置)
os.environ.setdefault("MPLCONFIGDIR", str(KEYPRISM_HOME / "cache" / "matplotlib"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

INIT_VIEW_SEC = 15.0  # 频谱区默认视窗宽度
COLORMAPS = ["Inferno", "Magma", "Plasma", "Viridis", "Cividis", "Turbo"]


def build_colorscale(name: str = "inferno", n: int = 33) -> list:
    cmap = matplotlib.colormaps[name](np.linspace(0.0, 1.0, n))
    cs = [[round(i / (n - 1), 4),
           f"rgb({int(r * 255)},{int(g * 255)},{int(b * 255)})"]
          for i, (r, g, b, _) in enumerate(cmap)]
    # 下限锚点强制纯黑: 静默区沉入深色背景, 凸显有效频谱。
    # 只改首锚点颜色, 与第二锚点之间仍是平滑线性过渡, 无断层。
    cs[0][1] = "#000000"
    return cs


def build_envelope_dataurl(spec_db: np.ndarray) -> str:
    env = spec_db.max(axis=0)
    fig = plt.figure(figsize=(8, 0.6), dpi=80)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.imshow(env[None, :], aspect="auto", cmap="inferno",
              interpolation="nearest")
    ax.axis("off")
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def compute_specs(data2d: np.ndarray, sr: int, dur: float, rate: int,
                  sub: int, db_range: float, window: int) -> dict:
    """按分辨率参数计算三通道频谱 (统一峰值归一化)"""
    rate = min(max(rate, TIME_RATES[0]), TIME_RATES[-1])
    if sub not in SUB_OPTIONS:
        sub = 1
    max_cols = max(60, int(round(rate * dur)))
    n_ch = data2d.shape[1]
    channels = {
        "mix": data2d.mean(axis=1),
        "left": data2d[:, 0],
        "right": data2d[:, 1] if n_ch > 1 else data2d[:, 0].copy(),
    }
    specs = {}
    hop = dur / 1000.0
    for name, sig in channels.items():
        m, hop, _ = spec_matrix(sig, sr, window, max_cols, sub)
        specs[name] = m
    peak = max(m.max() for m in specs.values())
    if peak <= 0:
        raise ValueError("音频为静音")
    quant = {}
    envelopes = {}
    for name, m in specs.items():
        spec_db = np.maximum(
            10.0 * np.log10(np.maximum(m / peak, 1e-12)), -db_range)
        q = np.clip((spec_db + db_range) / db_range, 0.0, 1.0)
        quant[name] = base64.b64encode(
            (q * 255.0).round().astype(np.uint8).tobytes()).decode()
        envelopes[name] = build_envelope_dataurl(spec_db)
    n_rows, n_cols = specs["mix"].shape
    return {
        "specs": quant,
        "envelopes": envelopes,
        "nCols": n_cols,
        "hopSec": round(hop, 6),
        "rate": rate,
        "sub": sub,
    }


def analyze(path: Path, start: float, end: float | None, window: int,
            db_range: float, rate: int, sub: int,
            api_base: str | None = None, preloaded: tuple | None = None,
            name: str | None = None) -> dict:
    """完整分析: 解码 (或复用预解码数据) -> BPM -> 三通道频谱 -> payload"""
    if preloaded is not None:
        data2d, sr, dur = preloaded
    else:
        data2d, sr, dur = load_channels(path, start, end)
    print(f"[1/3] 已加载 {dur:.1f}s @ {sr} Hz, {data2d.shape[1]} 声道")

    # BPM 用混合声道全分辨率 STFT 估计 (一次性)
    freqs, mix_power = stft_power(data2d.mean(axis=1), sr, window)
    hop_full = dur / max(mix_power.shape[1] - 1, 1)
    bpm, beat_offset = estimate_bpm(mix_power, hop_full)
    print(f"[2/3] 估计 BPM {bpm} (首拍偏移 {beat_offset * 1000:.0f}ms)")

    res = compute_specs(data2d, sr, dur, rate, sub, db_range, window)
    print(f"[3/3] 分辨率 {res['rate']} 列/s x {res['sub']} 子带/半音 "
          f"-> {88 * res['sub']} 行 x {res['nCols']} 列 x 3 通道")

    notes = list(range(MIDI_MIN, MIDI_MAX + 1))
    audio_name = browser_safe_audio(path)

    payload = {
        "file": name or path.name,
        "durationSec": round(dur, 3),
        "dbRange": db_range,
        "window": window,
        "offsetSec": start,
        "endSec": end,
        "initViewSec": min(INIT_VIEW_SEC, dur),
        "noteLabels": [note_name(m) for m in notes],
        "cTickIdx": [i for i, m in enumerate(notes) if m % 12 == 0],
        "colorscales": {name: build_colorscale(name.lower())
                        for name in COLORMAPS},
        "defaultCmap": "Inferno",
        "timeRates": TIME_RATES,
        "subOptions": SUB_OPTIONS,
        "defaultRate": res["rate"],
        "defaultSub": res["sub"],
        "apiBase": api_base,
        "audioFile": audio_name,
        "bpm": bpm,
        "beatOffsetSec": beat_offset,
        **res,
    }
    return payload

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism frontend contract layer: analysis results -> data.json payload

Responsibility: turn decoded PCM into the JSON structure the frontend
consumes directly — three-channel quantized spectra (uint8 base64), envelope
images (matplotlib-rendered data URLs), color scales, BPM/track metadata.
Single source of truth for the frontend's data.json fields.
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

# Keep matplotlib's font/config cache inside the workspace so it never
# pollutes the user's config directory
# (must be set before import matplotlib)
os.environ.setdefault("MPLCONFIGDIR", str(KEYPRISM_HOME / "cache" / "matplotlib"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

INIT_VIEW_SEC = 15.0  # default spectrogram viewport width
COLORMAPS = ["Inferno", "Magma", "Plasma", "Viridis", "Cividis", "Turbo"]


def build_colorscale(name: str = "inferno", n: int = 33) -> list:
    cmap = matplotlib.colormaps[name](np.linspace(0.0, 1.0, n))
    cs = [[round(i / (n - 1), 4),
           f"rgb({int(r * 255)},{int(g * 255)},{int(b * 255)})"]
          for i, (r, g, b, _) in enumerate(cmap)]
    # Lower anchor pinned to pure black: silence sinks into the dark
    # background, highlighting the effective spectrum.
    # Only the first anchor color changes; the transition to the second anchor
    # stays smoothly linear, with no discontinuity.
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
    """Compute the three-channel spectra at the given resolution parameters
    (unified peak normalization)"""
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


def finalize_payload(path: Path, dur: float, bpm: float, beat_offset: float,
                     res: dict, *, start: float, end: float | None,
                     window: int, db_range: float, api_base: str | None = None,
                     name: str | None = None) -> dict:
    """Assemble the data.json payload from computed analysis results.

    The tail of ``analyze``, split out so the staged orchestrator
    (``keyprism.analyze``) can reuse it without re-deriving BPM or spectra.
    Field set and values are identical to the historical inline assembly,
    plus the ``notes`` / ``stems`` reservation fields (always null in
    Phase 0; reserved for note-level transcription / source separation).
    """
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
        # Reserved for future phases — always null in Phase 0:
        #   notes: note-level transcription (Phase 1)
        #   stems: separated source stems (Phase 2)
        "notes": None,
        "stems": None,
        **res,
    }
    return payload


def analyze(path: Path, start: float, end: float | None, window: int,
            db_range: float, rate: int, sub: int,
            api_base: str | None = None, preloaded: tuple | None = None,
            name: str | None = None) -> dict:
    """Full analysis: decode (or reuse preloaded data) -> BPM ->
    three-channel spectra -> payload"""
    if preloaded is not None:
        data2d, sr, dur = preloaded
    else:
        data2d, sr, dur = load_channels(path, start, end)
    print(f"[1/3] 已加载 {dur:.1f}s @ {sr} Hz, {data2d.shape[1]} 声道")

    # BPM is estimated from a full-resolution STFT of the mix channel (once)
    freqs, mix_power = stft_power(data2d.mean(axis=1), sr, window)
    hop_full = dur / max(mix_power.shape[1] - 1, 1)
    bpm, beat_offset = estimate_bpm(mix_power, hop_full)
    print(f"[2/3] 估计 BPM {bpm} (首拍偏移 {beat_offset * 1000:.0f}ms)")

    res = compute_specs(data2d, sr, dur, rate, sub, db_range, window)
    print(f"[3/3] 分辨率 {res['rate']} 列/s x {res['sub']} 子带/半音 "
          f"-> {88 * res['sub']} 行 x {res['nCols']} 列 x 3 通道")

    return finalize_payload(path, dur, bpm, beat_offset, res, start=start,
                            end=end, window=window, db_range=db_range,
                            api_base=api_base, name=name)

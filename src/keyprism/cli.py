#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Piano spectral analysis - CLI entry: analyze audio and emit data files
for the Vite frontend.

Package layering (see each module's header docstring for details):
    dsp       pure algorithms: STFT / semitone aggregation / downsampling / BPM
    audio_io  decode chain: sndfile -> PyAV fallback, browser-safe
              transcoding, path constants
    payload   frontend contract: single source of truth for data.json fields
    server    HTTP service: /api/ping /api/spec /api/upload
    cli       this module: command-line entry (python -m keyprism)

Output goes to frontend/public/:
  data.json   metadata + spectrum matrices (uint8 base64) + color scales +
              envelope images
  audio.<ext> copy of the original audio (for frontend playback)

Usage:
    python -m keyprism                    # loads assets/demo.m4a by default
    python -m keyprism song.mp3 --start 30 --end 90
    python -m keyprism --serve 9630       # long-running API mode
"""

import argparse
import json
import sys
from pathlib import Path

from .audio_io import DEMO_AUDIO, PUBLIC_DIR
from .dsp import SUB_OPTIONS, TIME_RATES
from .payload import analyze
from .server import run_server


def main(argv=None):
    ap = argparse.ArgumentParser(description="钢琴频谱分析后端")
    ap.add_argument("input", nargs="?", default=str(DEMO_AUDIO),
                    help="输入音频, 未指定时默认 assets/demo.m4a "
                         "(mp3/wav/flac/ogg/m4a/aac/wma/opus/aiff ...)")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--window", type=int, default=8192,
                    help="STFT 窗长 (默认 8192)")
    ap.add_argument("--db-range", type=float, default=70.0)
    ap.add_argument("--rate", type=int, default=15, choices=TIME_RATES,
                    help="每秒时间列数 (默认 15)")
    ap.add_argument("--sub", type=int, default=1, choices=SUB_OPTIONS,
                    help="每半音子带数 (默认 1)")
    ap.add_argument("--serve", type=int, default=None, metavar="PORT",
                    help="常驻服务端口, 支持前端随时切换分辨率 (如 9630)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="serve 模式监听地址 (默认 127.0.0.1, "
                         "局域网访问用 0.0.0.0)")
    args = ap.parse_args(argv)

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"文件不存在: {src}  (未指定参数时默认加载 assets/demo.m4a)")
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

    if args.serve:
        run_server(src, args.serve, args.host, args.start, args.end,
                   args.window, args.db_range, args.rate, args.sub)
        return

    payload = analyze(src, args.start, args.end, args.window,
                      args.db_range, args.rate, args.sub)
    out = PUBLIC_DIR / "data.json"
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    audio_mb = (PUBLIC_DIR / payload["audioFile"]).stat().st_size / 1024 / 1024
    print(f"已输出: {out.resolve()}")
    print(f"已输出: {(PUBLIC_DIR / payload['audioFile']).resolve()} "
          f"({audio_mb:.1f} MB)")
    print("前端: cd frontend && npm run dev  (静态模式, 切换分辨率需 --serve)")


if __name__ == "__main__":
    main()

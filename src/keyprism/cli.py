#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""钢琴频谱分析 - CLI 入口: 分析音频并输出数据文件供 Vite 前端使用

包内分层 (详见各模块头注释):
    dsp       纯算法: STFT / 半音聚合 / 降采样 / BPM
    audio_io  解码链: sndfile -> PyAV 回退, 浏览器兼容转存, 路径常量
    payload   前端契约: data.json 字段的唯一权威定义
    server    HTTP 服务: /api/ping /api/spec /api/upload
    cli       本模块: 命令行入口 (python -m keyprism)

输出到 frontend/public/:
  data.json   元信息 + 频谱矩阵(uint8 base64) + 色标 + 包络图
  audio.<ext> 原始音频副本 (供前端播放)

用法:
    python -m keyprism                    # 无参数时默认加载 assets/demo.m4a
    python -m keyprism 歌曲.mp3 --start 30 --end 90
    python -m keyprism --serve 9630       # 常驻 API 模式
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

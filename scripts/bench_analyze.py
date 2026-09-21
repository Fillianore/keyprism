#!/usr/bin/env python
"""Wall-clock benchmark for the full demo.m4a analysis pipeline.

Times the legacy analysis path (keyprism.payload.analyze — available before
and after the Phase 0 refactor) and, when the staged orchestrator exists
(keyprism.analyze, Phase 0+), its cold (cache miss) and warm (cache hit)
runs against a scratch KEYPRISM_HOME so measurements are hermetic.

Usage:
    uv run python scripts/bench_analyze.py [audio] [--window 8192]
        [--rate 15] [--sub 1] [--repeat N]

Each path is timed `--repeat` times and the best (min) wall time is
reported; stdout ends with a BENCH-SUMMARY line for easy diffing.
"""

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from keyprism import audio_io
from keyprism.payload import analyze


def time_once(fn, *args, **kwargs):
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    return time.perf_counter() - t0, result


def main():
    ap = argparse.ArgumentParser(description="KeyPrism analysis benchmark")
    ap.add_argument("audio", nargs="?", default=str(audio_io.DEMO_AUDIO))
    ap.add_argument("--window", type=int, default=8192)
    ap.add_argument("--rate", type=int, default=15)
    ap.add_argument("--sub", type=int, default=1)
    ap.add_argument("--db-range", type=float, default=70.0)
    ap.add_argument("--repeat", type=int, default=2)
    args = ap.parse_args()
    src = Path(args.audio)
    if not src.exists():
        sys.exit(f"文件不存在: {src}")

    results = {}

    # --- legacy path (pre/post refactor baseline) ------------------------
    legacy_pub = Path(tempfile.mkdtemp(prefix="keyprism-bench-legacy-"))
    old_pub = audio_io.PUBLIC_DIR
    audio_io.PUBLIC_DIR = legacy_pub
    times = []
    payload = None
    for _ in range(max(1, args.repeat)):
        dt, payload = time_once(analyze, src, 0.0, None, args.window,
                                args.db_range, args.rate, args.sub)
        times.append(dt)
    audio_io.PUBLIC_DIR = old_pub
    shutil.rmtree(legacy_pub, ignore_errors=True)
    results["legacy (payload.analyze)"] = min(times)
    print(f"[bench] legacy payload.analyze : {min(times):7.3f} s  (best of {args.repeat})")

    # --- orchestrator path (Phase 0+; cold + warm) -----------------------
    try:
        from keyprism import analyze as orch
    except ImportError:
        print("[bench] orchestrator keyprism.analyze not available "
              "(pre-refactor baseline)")
        _summary(results, None, None, payload)
        return

    scratch = Path(tempfile.mkdtemp(prefix="keyprism-bench-home-"))
    real_home = audio_io.KEYPRISM_HOME
    audio_io.KEYPRISM_HOME = scratch
    quiet = lambda *_a, **_k: None  # noqa: E731
    try:
        # cold: fresh scratch workspace -> guaranteed cache miss
        times = []
        payload_o = None
        for _ in range(max(1, args.repeat)):
            shutil.rmtree(scratch / "cache", ignore_errors=True)
            dt, (payload_o, _) = time_once(
                orch.run_analysis, src, start=0.0, end=None,
                window=args.window, db_range=args.db_range,
                rate=args.rate, sub=args.sub, on_stage=quiet)
            times.append(dt)
        cold = min(times)
        results["orchestrator cold (cache miss)"] = cold
        print(f"[bench] orchestrator cold     : {cold:7.3f} s  (best of {args.repeat})")

        # warm: same workspace -> stft.npy cache hit
        times = []
        for _ in range(max(1, args.repeat)):
            dt, (payload_w, _) = time_once(
                orch.run_analysis, src, start=0.0, end=None,
                window=args.window, db_range=args.db_range,
                rate=args.rate, sub=args.sub, on_stage=quiet)
            times.append(dt)
        warm = min(times)
        results["orchestrator warm (cache hit)"] = warm
        print(f"[bench] orchestrator warm     : {warm:7.3f} s  (best of {args.repeat})")
        print(f"[bench] cold payload == warm payload: "
              f"{'yes' if payload_o == payload_w else 'NO'}")
        print(f"[bench] orchestrator cold payload == legacy payload: "
              f"{'yes' if payload_o == payload else 'NO'}")
    finally:
        audio_io.KEYPRISM_HOME = real_home
        shutil.rmtree(scratch, ignore_errors=True)

    _summary(results, legacy_payload=payload)


def _summary(results, legacy_payload=None):
    dur = legacy_payload["durationSec"] if legacy_payload else None
    parts = [f"{k}={v:.3f}s" for k, v in results.items()]
    if dur:
        parts.append(f"duration={dur:.1f}s")
    print("BENCH-SUMMARY " + " ".join(parts))


if __name__ == "__main__":
    main()

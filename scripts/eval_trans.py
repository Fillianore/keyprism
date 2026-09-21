#!/usr/bin/env python
"""Offline evaluation of the Phase 1 monophonic transcription.

Compares /api/notes-style output against reference MIDI files and prints
per-track precision / recall / F1 (onset-window + pitch-exact matching,
with overlap-aware numbers when mir_eval is installed — an OPTIONAL
dependency: ``uv sync --extra eval``; without it a built-in greedy matcher
is used). NOT executed in CI; tests never require mir_eval.

Usage (manual):
    # single audio + reference MIDI
    uv run python scripts/eval_trans.py song.wav song_reference.mid \
        [--track bass|lead|both] [--window 8192]

    # dataset directory: pairs of audio.<ext> + <stem>.mid
    uv run python scripts/eval_trans.py --dataset ~/datasets/slakh_mono \
        [--track both]

The transcription cache is redirected into a scratch directory so the
evaluation never evicts the user's analysis cache. Aggregate results end
with an EVAL-SUMMARY line for easy diffing/telemetry.
"""

import argparse
import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import mido  # noqa: E402
import numpy as np  # noqa: E402

AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus",
              ".wma", ".aiff"}
ONSET_TOL = 0.05     # seconds, standard note-tracking tolerance
PITCH_TOL = 0.5      # semitones


def _import_keyprism():
    # Scratch workspace first: audio_io.KEYPRISM_HOME is a module attribute
    # read at call time by the cache layer, so a late rebind is enough and
    # keeps the user's real cache untouched.
    scratch = tempfile.mkdtemp(prefix="keyprism-eval-")
    from keyprism import audio_io

    audio_io.KEYPRISM_HOME = audio_io.Path(scratch)
    return scratch


def midi_to_notes(path: Path) -> list:
    """Reference notes [(pitch, start_s, end_s)] from the first tempo."""
    mid = mido.MidiFile(file=io.BytesIO(path.read_bytes()))
    us_per_beat = 500000
    for msg in mid.tracks[0]:
        if msg.type == "set_tempo":
            us_per_beat = msg.tempo
            break
    sec_per_tick = us_per_beat / 1e6 / mid.ticks_per_beat
    notes = []
    for tr in mid.tracks:
        t = 0
        active = {}
        for msg in tr:
            t += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                active.setdefault(msg.note, []).append(t)
            elif msg.type in ("note_off", "note_on"):
                for on in active.pop(msg.note, ()):
                    start = on * sec_per_tick
                    notes.append((msg.note, start,
                                  max(start, t * sec_per_tick)))
    return sorted(notes)


def match_builtin(ref, est) -> tuple:
    """Greedy pitch-exact/onset-window matcher -> (tp, fp, fn)."""
    used = [False] * len(est)
    tp = 0
    for pitch, s, e in ref:
        best = None
        for i, (p2, s2, e2) in enumerate(est):
            if used[i] or abs(p2 - pitch) > PITCH_TOL:
                continue
            if abs(s2 - s) > ONSET_TOL:
                continue
            ov = min(e, e2) - max(s, s2)
            if ov > 0 and (best is None or ov > best[1]):
                best = (i, ov)
        if best is not None:
            used[best[0]] = True
            tp += 1
    return tp, used.count(False), len(ref) - tp


def match_mir_eval(ref, est) -> tuple:
    """mir_eval transcription matching -> (tp, fp, fn)."""
    from mir_eval import transcription as met

    def arr(notes):
        if not notes:
            return np.zeros((0, 2)), np.zeros(0)
        return (np.array([[s, e] for _, s, e in notes]),
                np.array([p for p, _, _ in notes], dtype=float))

    ref_i, ref_p = arr(ref)
    est_i, est_p = arr(est)
    p, r, f, _ = met.precision_recall_f1_overlap(
        ref_i, ref_p, est_i, est_p,
        onset_tolerance=ONSET_TOL, pitch_tolerance=PITCH_TOL)
    return p, r, f


def evaluate_pair(audio: Path, ref_midi: Path, tracks: list, window: int,
                  use_mir: bool) -> dict:
    from keyprism import audio_io, transcribe

    cur = {}
    data2d, sr, dur = audio_io.load_channels(audio, 0.0, None)
    cur = {"data2d": data2d, "sr": sr, "dur": dur}
    entry = transcribe.locate_entry(cur, window=window, db_range=70.0,
                                    rate=15, sub=1, start=0.0, end=None)
    if entry is None:
        raise ValueError(f"无法为 {audio.name} 建立分析缓存")
    ref = midi_to_notes(ref_midi)
    results = {}
    for track in tracks:
        body, _ = transcribe.get_notes(entry, track)
        est = [(n["pitch"], n["start"], n["end"])
               for n in body["notes"][track]]
        if use_mir:
            try:
                p, r, f = match_mir_eval(ref, est)
            except ImportError:
                use_mir = False
                p, r, f = match_builtin(ref, est)
        else:
            tp, fp, fn = match_builtin(ref, est)
            p = tp / (tp + fp) if tp + fp else 0.0
            r = tp / (tp + fn) if tp + fn else 0.0
            f = 2 * p * r / (p + r) if p + r else 0.0
        results[track] = (p, r, f, len(ref), len(est))
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="单音轨转写评测 (Phase 1)")
    ap.add_argument("audio", nargs="?", help="单文件模式: 音频路径")
    ap.add_argument("reference", nargs="?", help="单文件模式: 参考 MIDI")
    ap.add_argument("--dataset", help="数据集目录 (音频 + 同名 .mid 成对)")
    ap.add_argument("--track", default="both",
                    choices=["bass", "lead", "both"])
    ap.add_argument("--window", type=int, default=8192)
    args = ap.parse_args()
    if not args.dataset and not (args.audio and args.reference):
        ap.error("需要 <audio> <reference> 或 --dataset 目录")

    scratch = _import_keyprism()
    try:
        use_mir = True
        try:
            import mir_eval  # noqa: F401
        except ImportError:
            use_mir = False
            print("[i] mir_eval 未安装, 使用内置匹配器 "
                  "(uv sync --extra eval 可启用)")

        tracks = ["bass", "lead"] if args.track == "both" else [args.track]
        pairs = []
        if args.dataset:
            root = Path(args.dataset).expanduser()
            for audio in sorted(root.rglob("*")):
                if audio.suffix.lower() in AUDIO_EXTS:
                    midi = audio.with_suffix(".mid")
                    if not midi.exists():
                        midi = audio.with_suffix(".midi")
                    if midi.exists():
                        pairs.append((audio, midi))
            if not pairs:
                print(f"数据集中未找到 音频+MIDI 成对文件: {root}")
                return 1
        else:
            pairs = [(Path(args.audio), Path(args.reference))]

        agg = {t: [0.0, 0.0, 0.0, 0] for t in tracks}
        for audio, midi in pairs:
            res = evaluate_pair(audio, midi, tracks, args.window, use_mir)
            for t, (p, r, f, n_ref, n_est) in res.items():
                print(f"{audio.name} [{t}] P={p:.3f} R={r:.3f} F1={f:.3f} "
                      f"(ref {n_ref} / est {n_est} notes)")
                agg[t][0] += p
                agg[t][1] += r
                agg[t][2] += f
                agg[t][3] += 1
        for t, (sp, sr_, sf, n) in agg.items():
            if n:
                print(f"== {t}: mean P={sp / n:.3f} R={sr_ / n:.3f} "
                      f"F1={sf / n:.3f} over {n} file(s)")
        matcher = "mir_eval" if use_mir else "builtin"
        print(f"EVAL-SUMMARY tracks={'/'.join(agg)} files={len(pairs)} "
              f"matcher={matcher} window={args.window} "
              + " ".join(f"{t}_F1={agg[t][2] / max(agg[t][3], 1):.3f}"
                         for t in tracks))
        return 0
    finally:
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

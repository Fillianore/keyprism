#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism polyphonic transcription: Basic Pitch on a DL stem (Phase 3)

Position: sits beside ``transcribe`` (never modifies it) and is called by
``server``; reads the DL stem WAVs written by ``dlsep`` and caches its
own versioned note JSON under the analysis entry. Dependency direction
stays one-way: ``server -> poly_transcribe -> {dlsep (DLMissingError),
audio_io}``. The ``basic-pitch`` dependency is OPTIONAL (guarded import);
without it the flag ``BP_AVAILABLE`` is False and the server answers 501
— the injection seam ``predict_fn=`` keeps the conversion + merging
pipeline fully testable without the library.

Frame-based pitch trackers (Basic Pitch included) stutter on sustained
notes: tiny confidence wobbles split one long chord into dozens of
adjacent ~50 ms fragments. :func:`merge_fragmented_notes` is the
mandatory post-processing pass — same pitch + gap below
``merge_gap_s`` (default 50 ms) collapses into one note (end extended,
confidences folded) — otherwise MIDI export and the frontend canvas
both drown in garbage rectangles.

Cache layout (auto-invalidating via version bump, same rule as
MONO_VERSION / DL_STEMS_VERSION):

    <entry>/notes/<POLY_VERSION>/notes_poly_<stem>.json
"""

import json
import threading
from pathlib import Path

import numpy as np

try:  # optional: polyphonic transcription backend
    import basic_pitch  # noqa: F401

    BP_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via the flag in tests
    basic_pitch = None
    BP_AVAILABLE = False

from .dlsep import DLMissingError, resample

__all__ = [
    "POLY_VERSION", "POLY_TRACKS", "AUDIO_SR", "BP_AVAILABLE",
    "DLMissingError", "merge_fragmented_notes", "transcribe_polyphonic",
    "get_poly_notes", "cached_poly_notes_path",
]

#: Version tag of the polyphonic transcription pipeline; part of the
#: cache path (bump to invalidate previously cached files everywhere).
POLY_VERSION = "poly_v1"

#: DL stems eligible for polyphonic transcription (demucs_6 stems).
POLY_TRACKS = ("piano", "guitar", "other")

#: Basic Pitch's internal model rate; inputs are resampled to this.
AUDIO_SR = 22050

#: Frame-level notes below this duration are dropped (post-merge).
MIN_NOTE_S = 0.03


def merge_fragmented_notes(notes: list, gap_s: float = 0.05) -> list:
    """Collapse same-pitch fragments separated by a gap < ``gap_s``.

    ``notes`` are dicts with at least ``pitch``/``start``/``end``/``conf``
    (times in seconds, any order). Same-pitch neighbours with
    ``note_B.start - note_A.end < gap_s`` become one note spanning
    ``A.start..max(A.end, B.end)``; ``conf`` folds to the max (the
    strongest detection of the merged span) and overlapping fragments
    (negative gap) merge too. Deterministic: sort by (pitch, start),
    single left-to-right pass.
    """
    out = []
    for n in sorted(
            (dict(pitch=int(n["pitch"]), start=float(n["start"]),
                  end=float(n["end"]), conf=float(n.get("conf", 0.0)))
             for n in notes),
            key=lambda n: (n["pitch"], n["start"])):
        if out:
            prev = out[-1]
            if prev["pitch"] == n["pitch"] and \
                    n["start"] - prev["end"] < gap_s:
                prev["end"] = max(prev["end"], n["end"])
                prev["conf"] = max(prev["conf"], n["conf"])
                continue
        out.append(n)
    return out


def _event_fields(ev) -> tuple:
    """Tolerant reader for one raw Basic Pitch note event: accepts
    attribute objects (namedtuples), (start, end, pitch, amplitude)
    sequences and dicts across library versions."""
    if isinstance(ev, dict):
        return (ev.get("start"), ev.get("end"), ev.get("pitch"),
                ev.get("amplitude", ev.get("conf", 0.0)))
    if all(hasattr(ev, a) for a in ("start", "end", "pitch")):
        return (ev.start, ev.end, ev.pitch, getattr(ev, "amplitude", 0.0))
    try:
        start, end, pitch, amp = ev
        return start, end, pitch, amp
    except (TypeError, ValueError) as e:
        raise ValueError(f"无法解析音符事件: {ev!r}") from e


def _convert_raw(raw, merge_gap_s: float) -> list:
    """Raw Basic Pitch output -> merged NoteEvent dicts.

    Accepts the ``predict`` dict (uses ``note-events``), a bare event
    list, or ``{"<stem>": [...]}``-shaped output — whatever the backend
    version produces."""
    if isinstance(raw, dict):
        raw = raw.get("note-events", raw.get("note_events"))
        if isinstance(raw, dict):  # {"instrument": [events]}
            raw = next(iter(raw.values())) if raw else []
    notes = []
    for ev in raw or []:
        start, end, pitch, amp = _event_fields(ev)
        try:
            p = int(pitch)
        except (TypeError, ValueError):
            p = int(np.asarray(pitch).reshape(-1)[0])
        notes.append({"pitch": p, "start": float(start), "end": float(end),
                      "conf": float(min(max(float(amp), 0.0), 1.0))})
    merged = merge_fragmented_notes(notes, gap_s=merge_gap_s)
    return [n for n in merged if n["end"] - n["start"] >= MIN_NOTE_S]


def default_predict_fn():
    """Build the real Basic Pitch callable (lazy import, singleton
    model). Raises :class:`DLMissingError` when the library is absent."""
    if not BP_AVAILABLE:
        raise DLMissingError(
            "basic-pitch 未安装: 请安装 DL 依赖 (uv sync --extra dl)")
    from basic_pitch import ICASSP_2022_MODEL_PATH
    from basic_pitch.inference import predict

    def run(x: np.ndarray, sr: int):
        # basic_pitch expects its own model rate for array input
        return predict(ICASSP_2022_MODEL_PATH, resample(x, sr, AUDIO_SR))

    return run


def transcribe_polyphonic(pcm: np.ndarray, sr: int, *, predict_fn=None,
                          merge_gap_s: float = 0.05) -> list:
    """Mono PCM in, merged polyphonic note dicts out.

    ``predict_fn(x, sr) -> raw`` defaults to Basic Pitch; passing a stub
    keeps this testable without the DL extra (see module docstring).
    The merge pass runs on the backend output in every case — fragmented
    frame predictions never reach the cache or the frontend."""
    x = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if x.shape[0] == 0:
        return []
    if predict_fn is None:
        predict_fn = default_predict_fn()
    return _convert_raw(predict_fn(x, int(sr)), merge_gap_s)


# --------------------------------------------------------------- cache

def notes_dir(entry: Path) -> Path:
    """``<entry>/notes/<POLY_VERSION>`` — version-tagged."""
    return Path(entry) / "notes" / POLY_VERSION


def cached_poly_notes_path(entry: Path, stem: str) -> Path:
    return notes_dir(entry) / f"notes_poly_{stem}.json"


def _read_cached(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _store_notes(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def transcribe_stem(entry: Path, wav_path: Path, stem: str,
                    *, predict_fn=None) -> dict:
    """Full compute path for one DL stem: read WAV -> Basic Pitch ->
    merge -> cache -> body. Raises when the stem has not been computed."""
    import soundfile as sf

    path = Path(wav_path)
    if not path.is_file():
        raise ValueError(
            f"stem 尚未计算: {path.name} (先请求 DL 分轨)")
    pcm, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = pcm.mean(axis=1)
    events = transcribe_polyphonic(mono, sr, predict_fn=predict_fn)
    body = {"track": stem, "method": "poly",
            "notes": {stem: events}}
    _store_notes(cached_poly_notes_path(Path(entry), stem), body)
    return body


def get_poly_notes(entry: Path, stem: str, wav_path: Path,
                   lock=None) -> tuple:
    """Notes body for one DL stem with double-checked locking around
    compute so concurrent requests share one computation."""
    path = cached_poly_notes_path(entry, stem)
    body = _read_cached(path)
    if body is not None:
        return body, True
    with (lock or threading.Lock()):
        body = _read_cached(path)  # re-check inside the lock
        if body is None:
            body = transcribe_stem(entry, wav_path, stem)
        return body, False

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism transcription orchestration: cached STFT -> notes -> disk cache
(Phase 1)

Position: sits beside ``analyze`` (never modifies it) and is called by
``server``; composes ``tracks`` / ``salience`` / ``onset`` / ``decode`` /
``midi_io``. Dependency direction stays one-way:
``server -> transcribe -> {analyze (cache access), salience/onset/decode,
tracks, midi_io}``.

Notes are served ON DEMAND through /api/notes and /api/midi — the data.json
payload keeps ``"notes": null`` (its contract is owned by ``payload.py``
and stays untouched; see the Phase 1 decision in AGENTS.md). Per-track
results cache under the analysis entry:

    <entry>/notes/<MONO_VERSION>/notes_<track>.json

so bumping ``MONO_VERSION`` (or changing presets/pipeline numerics) invalidates
previous results automatically. A cache hit serves the file bytes verbatim.

Compute path (per track) — memmap discipline per AGENTS.md, the full
complex STFT is never in RAM:

    pass 1, in <=512-frame row chunks of ``stft.npy`` (mmap views):
      - |Z| chunk -> onset flux (positive, onset band, carry the previous
        chunk's last row across boundaries)
      - |Z| chunk -> A-weighted -> harmonic salience S' (float32), stored
        into a preallocated float32 matrix (``n_frames x n_pitches`` — not
        the complex STFT, three orders of magnitude smaller; the Phase 0
        ban targets complex matrices only) while tracking the exact global
        min/max (commutative -> chunk-order independent)
    pass 2: in-place element-wise min-max normalization to [0, 1]
      (identical float ops per element for any chunking -> deterministic)
    then: peak picking over the flux array -> Viterbi decode -> notes.

Determinism: fixed chunk size, pure per-chunk functions, exact min/max,
stable ties in decoding -> two cold runs produce byte-identical JSON.
"""

import json
import re
import threading
from pathlib import Path

import numpy as np

from . import analyze
from .decode import NoteEvent, decode_monophonic
from .midi_io import export_midi
from .onset import flux_chunk, pick_onset_frames
from .salience import a_weighting_gain, compute_salience
from .tracks import MONO_TRACKS, MONO_VERSION, get_preset

__all__ = [
    "locate_entry", "get_notes", "compute_midi", "midi_filename",
    "cached_notes_path", "transcribe_track",
]

#: Row-chunk size for streaming the cached STFT (Phase 0 discipline <= 2048)
CHUNK_FRAMES = 512

_NOTES_LOCK = threading.Lock()


def locate_entry(cur: dict, *, window: int, db_range: float, rate: int,
                 sub: int, start: float, end) -> Path | None:
    """Cache entry directory of the currently loaded track.

    ``cur`` is the server track state ({data2d, sr, dur, ...}); the params
    mirror ``analyze.stft_stage`` exactly (same key scheme). If the entry
    is missing (e.g. evicted), the STFT stage is recomputed through
    ``analyze.stft_stage`` so notes always work against a valid artifact.
    Returns ``None`` when ``cur`` itself is missing."""
    if not cur:
        return None
    data2d, sr, dur = cur["data2d"], cur["sr"], cur["dur"]
    hop = analyze.default_hop(window)
    params = analyze.params_dict(sr=sr, win=window, hop=hop, window="hann",
                                 center=True, rate=rate, sub=sub,
                                 db_range=db_range, start=start, end=end)
    entry = analyze.entry_dir(analyze.pcm16_fingerprint(data2d),
                              analyze.params_fingerprint(params))
    if analyze.load_entry(entry) is None:
        analyze.stft_stage(data2d, sr, dur, window=window, rate=rate,
                           sub=sub, db_range=db_range, start=start, end=end)
    return entry


def notes_dir(entry: Path) -> Path:
    """``<entry>/notes/<MONO_VERSION>`` — version-tagged, auto-invalidating."""
    return Path(entry) / "notes" / MONO_VERSION


def cached_notes_path(entry: Path, track: str) -> Path:
    return notes_dir(entry) / f"notes_{track}.json"


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


def transcribe_track(entry: Path, track: str) -> dict:
    """Full compute path for one track: salience -> onsets -> decode ->
    cache -> body. Raises when the STFT artifact is unreadable."""
    hit = analyze.load_entry(Path(entry))
    if hit is None:
        raise ValueError("分析缓存缺失: STFT 未就绪")
    mm, meta = hit
    sr, win, hop = int(meta["sr"]), int(meta["win"]), int(meta["hop"])
    hop_sec = hop / float(sr)
    preset = get_preset(track)
    n_frames, n_bins = mm.shape
    grid_lo, grid_hi = preset.midi_range
    n_p = grid_hi - grid_lo + 1
    gain = a_weighting_gain(np.fft.rfftfreq(win, 1.0 / float(sr)))

    sal = np.empty((n_frames, n_p), dtype=np.float32)
    gmin = np.float32(np.inf)
    gmax = np.float32(-np.inf)
    flux_parts = []
    prev_row = None
    for a in range(0, n_frames, CHUNK_FRAMES):
        b = min(a + CHUNK_FRAMES, n_frames)
        mag = np.abs(mm[a:b])  # float32 chunk (view -> copy, bounded)
        flux_parts.append(flux_chunk(mag, prev_row, sr, win, preset))
        prev_row = mag[-1]
        chunk = compute_salience(mag * gain[None, :], sr, win, hop, preset)
        sal[a:b] = chunk
        gmin = min(gmin, chunk.min())
        gmax = max(gmax, chunk.max())

    # Element-wise min-max normalize to [0, 1]: identical per-element float
    # ops for any chunk order/size -> byte-deterministic.
    span = gmax - gmin
    if span > 0:
        sal -= gmin
        sal /= span
    else:
        sal[:] = np.float32(0.0)

    flux = np.concatenate(flux_parts) if flux_parts else np.zeros(0)
    onsets = pick_onset_frames(flux, sr, hop, preset)
    events = decode_monophonic(sal, onsets, hop_sec, preset)
    body = {"track": track,
            "notes": {track: [ev.as_dict() for ev in events]}}
    _store_notes(cached_notes_path(entry, track), body)
    return body


def get_notes(entry: Path, track: str,
              lock=None) -> tuple:
    """Notes body for ``track`` (a registry name or "both") plus whether it
    was fully served from cache. Double-checked locking around compute so
    concurrent requests share one computation."""
    names = track_names_for(track)
    out = {}
    cached_all = True
    for name in names:
        path = cached_notes_path(entry, name)
        body = _read_cached(path)
        if body is None:
            cached_all = False
            with (lock or _NOTES_LOCK):
                body = _read_cached(path)  # re-check inside the lock
                if body is None:
                    body = transcribe_track(entry, name)
        out[name] = body["notes"][name]
    selection = track if track in MONO_TRACKS else "both"
    return {"track": selection, "notes": out}, cached_all


def track_names_for(track: str) -> list:
    """Registry names covered by a track query value (validates it)."""
    if track == "both":
        return list(MONO_TRACKS)
    get_preset(track)  # raises KeyError for unknown names
    return [track]


def midi_filename(display_name: str, track: str) -> str:
    """Attachment file name: keyprism_<slug>_<track>.mid."""
    stem = Path(display_name or "track").stem
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or "track"
    return f"keyprism_{slug}_{track}.mid"


def compute_midi(entry: Path, track: str, display_name: str,
                 lock=None) -> tuple:
    """(midi bytes, file name) for a track query; notes come from the
    per-track cache (computed on demand), tempo/offset from entry meta."""
    body, _ = get_notes(entry, track, lock=lock)
    meta = analyze.load_entry(Path(entry))[1]
    events = {name: [NoteEvent(**d) for d in body["notes"][name]]
              for name in body["notes"]}
    data = export_midi(events, meta.get("bpm"),
                       meta.get("beat_offset_sec") or 0.0)
    return data, midi_filename(display_name, track)

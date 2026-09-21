#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism MIDI export: note events -> Standard MIDI File bytes (Phase 1)

Pure conversion, no IO (note dicts in, bytes out). Format rules:

- 480 ticks per quarter note; tempo = microseconds per quarter from ``bpm``
  (non-finite / <= 0 falls back to 120).
- A single input track produces SMF type 0 (one MTrk holding the tempo meta
  followed by its notes); several input tracks produce SMF type 1 with a
  leading tempo-only conductor track and one note track per preset (in
  registry order, each named after the preset).
- Note times are written at (time - offset), i.e. the exported file aligns
  its beat grid with the detected first beat (``meta.beat_offset_sec``);
  negative times clamp to tick 0. Times quantize to ticks
  (tick = seconds * tpq * bpm / 60); note_off always follows note_on even
  for zero-length notes.
- velocity = 64 + round(63 * conf)  (conf in [0, 1] -> 64..127).

Deterministic: registry-ordered tracks, stable sort with note_off before
note_on at equal ticks, no randomness.
"""

import io
import math

import mido

from .tracks import track_names

__all__ = ["export_midi"]

TPQ = 480
DEFAULT_BPM = 120.0


def _tempo_bpm(bpm) -> float:
    try:
        b = float(bpm)
        if math.isfinite(b) and b > 0:
            return b
    except (TypeError, ValueError):
        pass
    return DEFAULT_BPM


def _sec_to_tick(seconds: float, bpm: float) -> int:
    return int(round(seconds * TPQ * bpm / 60.0))


def export_midi(tracks: dict, bpm: float, offset: float) -> bytes:
    """Serialize ``{track_name: [NoteEvent, ...]}`` to SMF bytes.

    ``tracks`` keys are registry track names (unknown names are rejected —
    registry-driven by construction); ``offset`` is the beat offset in
    seconds that note times are shifted by (see module docstring)."""
    names = [n for n in track_names() if n in tracks]
    if not names:
        raise ValueError("没有可导出的音轨")
    unknown = set(tracks) - set(names)
    if unknown:
        raise KeyError(f"未知音轨: {', '.join(sorted(unknown))}")

    bpm = _tempo_bpm(bpm)
    midi_type = 0 if len(names) == 1 else 1
    mid = mido.MidiFile(type=midi_type, ticks_per_beat=TPQ)
    tempo_msg = mido.MetaMessage(
        "set_tempo", tempo=mido.bpm2tempo(bpm), time=0)

    def fill_notes(track: mido.MidiTrack, name: str) -> None:
        track.append(mido.MetaMessage("track_name", name=name, time=0))
        events = []  # (abs_tick, order, message)
        for ev in tracks[name]:
            pitch = int(ev.pitch)
            vel = min(127, max(1, 64 + int(round(63 * float(ev.conf)))))
            on_t = max(0, _sec_to_tick(float(ev.start) - float(offset), bpm))
            off_t = max(0, _sec_to_tick(float(ev.end) - float(offset), bpm))
            off_t = max(off_t, on_t + 1)  # zero-length notes still end
            # order 0/1: at equal ticks note_off sorts before note_on
            events.append((on_t, 1, mido.Message(
                "note_on", note=pitch, velocity=vel, time=0)))
            events.append((off_t, 0, mido.Message(
                "note_off", note=pitch, velocity=0, time=0)))
        events.sort(key=lambda e: (e[0], e[1]))
        last = 0
        for tick, _order, msg in events:
            msg.time = tick - last
            last = tick
            track.append(msg)

    if midi_type == 0:
        only = mido.MidiTrack()
        only.append(tempo_msg)
        fill_notes(only, names[0])
        mid.tracks.append(only)
    else:
        mid.tracks.append(mido.MidiTrack([tempo_msg]))
        for name in names:
            t = mido.MidiTrack()
            fill_notes(t, name)
            mid.tracks.append(t)

    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()

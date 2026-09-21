#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism monophonic track presets: the single registry that drives the
whole transcription pipeline (Phase 1)

Every algorithm in ``salience`` / ``onset`` / ``decode`` / ``transcribe`` /
``midi_io`` is parameterized exclusively by a :class:`TrackPreset` from
``MONO_TRACKS`` — there are deliberately no per-instrument constants
anywhere else. Adding a future instrument MUST require only a new registry
entry; if you find yourself adding an ``if track == ...`` branch in an
algorithm module, the design is broken — extend the preset schema instead.

``MONO_VERSION`` tags the transcription result format AND takes part in the
on-disk cache path (``<entry>/notes/<MONO_VERSION>/notes_<track>.json``).
Bump it whenever preset values, salience/onset/decode numerics or the note
JSON schema change in a way that must invalidate previously cached note
files — stale results are then never served and recompute automatically.

Layer position: pure data + lookup, zero IO (same discipline as
``dsp`` / ``transform``), imported downward by all transcription modules.
"""

from dataclasses import dataclass

__all__ = ["TrackPreset", "MONO_TRACKS", "MONO_VERSION", "get_preset",
           "track_names"]

#: Version tag of the monophonic transcription pipeline. Part of the notes
#: cache path (see module docstring).
MONO_VERSION = "mono_v1"


@dataclass(frozen=True)
class TrackPreset:
    """Everything the pipeline knows about one monophonic track.

    Field semantics (used by ``salience`` / ``onset`` / ``decode``):

    - ``midi_range``          inclusive MIDI pitch search grid
    - ``onset_band_hz``       (lo, hi) frequency band for onset flux
    - ``n_harm``              harmonics summed in the salience function
    - ``harm_weight``         weighting of harmonic k (only "1/k" today)
    - ``subharmonic_alpha``   S' = max(0, S - alpha * S(f0/2)) suppression
    - ``w_pitch``             Viterbi transition cost per semitone moved
    - ``w_octave_jump``       Viterbi cost per octave for octave-multiple
                              jumps (|dp| >= 12 and |dp| % 12 == 0)
    - ``w_rest_in/out``       Viterbi cost of REST -> pitch / pitch -> REST
    - ``w_rest_stay``         Viterbi per-frame cost of staying in REST
    - ``rest_emission``       constant emission cost of the REST state
    - ``min_ioi_s``           onset refractory period (seconds)
    - ``min_note_s``          minimum emitted note duration (seconds)
    - ``color``               frontend overlay color (mirror the value in
                              ``frontend/src/notes.js`` — the frontend does
                              not read Python data)
    """

    name: str
    midi_range: tuple  # (lo, hi), inclusive
    onset_band_hz: tuple  # (lo, hi)
    n_harm: int
    harm_weight: str  # "1/k"
    subharmonic_alpha: float
    w_pitch: float
    w_octave_jump: float
    w_rest_in: float
    w_rest_out: float
    w_rest_stay: float
    rest_emission: float
    min_ioi_s: float
    min_note_s: float
    color: str


#: The registry. Extension rule: ONE new entry, nothing else (module
#: docstring).
MONO_TRACKS: dict = {
    "bass": TrackPreset(
        name="bass",
        midi_range=(28, 67),      # E1..G4
        onset_band_hz=(30.0, 250.0),
        n_harm=10,
        harm_weight="1/k",
        subharmonic_alpha=0.6,
        w_pitch=0.8,
        w_octave_jump=1.5,
        w_rest_in=2.0,
        w_rest_out=2.0,
        w_rest_stay=0.3,
        rest_emission=0.55,
        min_ioi_s=0.09,
        min_note_s=0.10,
        color="#5AC8FA",
    ),
    "lead": TrackPreset(
        name="lead",
        midi_range=(48, 84),      # C3..C6
        onset_band_hz=(200.0, 4000.0),
        n_harm=5,
        harm_weight="1/k",
        subharmonic_alpha=0.5,
        w_pitch=1.2,
        w_octave_jump=3.0,
        w_rest_in=2.5,
        w_rest_out=2.5,
        w_rest_stay=0.3,
        rest_emission=0.55,
        min_ioi_s=0.08,
        min_note_s=0.10,
        color="#FFB74D",
    ),
}

#: Track selections accepted by /api/notes and /api/midi ("both" = every
#: registry track).
TRACK_QUERY_VALUES = ("bass", "lead", "both")


def get_preset(track: str) -> TrackPreset:
    """Registry lookup with a clear error for unknown track names."""
    try:
        return MONO_TRACKS[track]
    except KeyError:
        raise KeyError(
            f"未知音轨: {track} (可用: {', '.join(MONO_TRACKS)})") from None


def track_names() -> list:
    """Registry track names in canonical (insertion) order."""
    return list(MONO_TRACKS)

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism monophonic decoding: Viterbi over (preset pitches + REST)
(Phase 1)

Pure algorithms only — numpy arrays in, plain note dicts out, zero IO.

State space: one state per pitch in ``preset.midi_range`` plus a REST state
(index ``n_pitches``, last). Costs (all preset-driven, see
``tracks.TrackPreset``):

- emission(s, t)   = 1 - S'(s, t)          (pitch states; S' normalized)
- emission(REST,t) = preset.rest_emission  (constant)
- transition(p -> q), dp = |p - q| (all weights are PER-SECOND rates,
  multiplied by the frame spacing ``hop_sec`` so decoder behavior is a
  function of physical time, not frame counts — changing sr/win rescales
  per-frame costs exactly proportionally and nothing drifts):
      dp == 0                        -> 0                       (stay)
      dp >= 12 and dp % 12 == 0      -> w_octave_jump * (dp / 12)
      otherwise                      -> w_pitch * dp
      REST -> pitch                  -> w_rest_in
      pitch -> REST                  -> w_rest_out
      REST -> REST                   -> w_rest_stay             (per frame)

With the default preset values the balance works out to: REST beats a
zero-salience pitch by ~0.43 per second of silence (so sub-second gaps
become rests), while a voiced frame beats REST by ~0.55 (so real notes pay
the small rest entry/exit cost back within ~0.2 s).

Post-processing (in order): same-pitch segments separated by a REST gap of
at most 60 ms are merged; notes shorter than ``preset.min_note_s`` are
dropped; segment boundaries within +/-1 frame of a detected onset are
snapped to it; confidence = mean normalized S' over the note span, clipped
to [0, 1].
"""

import dataclasses
import numpy as np

from .tracks import TrackPreset

__all__ = ["NoteEvent", "decode_monophonic"]

#: Transition weights are per-second rates: the per-frame cost is
#: ``weight * hop_sec`` (see the module docstring).

#: Transition-cost discount applied... not to transitions: onsets only snap
#: boundaries in post-processing (kept explicit here to document that the
#: Viterbi itself is onset-free by design).
ONSET_SNAP_FRAMES = 1

#: REST gap at or below this many seconds merges two same-pitch segments
MERGE_GAP_S = 0.06


@dataclasses.dataclass(frozen=True)
class NoteEvent:
    """One detected monophonic note (times in seconds from segment start)."""
    pitch: int
    start: float
    end: float
    conf: float

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _transition_matrix(n_states: int, hop_sec: float,
                       preset: TrackPreset) -> np.ndarray:
    """Full cost matrix T[s, q] of moving from state s to state q.

    Weights are per-second rates multiplied by the physical frame spacing
    ``hop_sec`` (module docstring) — frame-rate invariant by construction."""
    t = np.empty((n_states, n_states), dtype=np.float64)
    n_p = n_states - 1  # REST is last
    for s in range(n_states):
        for q in range(n_states):
            if s == n_p and q == n_p:
                c = preset.w_rest_stay          # REST -> REST (per frame)
            elif s == n_p:
                c = preset.w_rest_in            # REST -> pitch
            elif q == n_p:
                c = preset.w_rest_out           # pitch -> REST
            elif s == q:
                c = 0.0                         # pitch stay
            else:
                dp = abs(s - q)
                if dp >= 12 and dp % 12 == 0:
                    c = preset.w_octave_jump * (dp // 12)
                else:
                    c = preset.w_pitch * dp
            t[s, q] = c * hop_sec
    return t


def decode_monophonic(salience: np.ndarray, onsets, hop_sec: float,
                      preset: TrackPreset) -> list:
    """Decode one monophonic track.

    ``salience`` is the globally normalized S' matrix (frames, n_pitches)
    of the preset's pitch grid; ``onsets`` the detected onset frame indices
    (used for boundary snapping only); ``hop_sec`` the STFT frame spacing
    in seconds. Returns a time-sorted list of :class:`NoteEvent`,
    deterministic, no randomness.
    """
    sal = np.asarray(salience, dtype=np.float64)
    if sal.ndim != 2:
        raise ValueError("salience 形状须为 (frames, n_pitches)")
    n_p = sal.shape[1]
    n_states = n_p + 1
    n_frames = sal.shape[0]
    if n_frames == 0:
        return []

    emission = np.empty((n_frames, n_states), dtype=np.float64)
    emission[:, :n_p] = 1.0 - sal
    emission[:, n_p] = float(preset.rest_emission)

    t = _transition_matrix(n_states, hop_sec, preset)
    onset_frames = sorted(int(f) for f in onsets)

    # Viterbi (min-cost): backpointer to the cheapest predecessor state
    back = np.empty((n_frames, n_states), dtype=np.int16)
    v = emission[0].copy()
    for f in range(1, n_frames):
        cand = v[:, None] + t  # (from, to)
        best = np.argmin(cand, axis=0)  # ties -> lowest from-state index
        back[f] = best
        v = cand[best, np.arange(n_states)] + emission[f]

    states = [int(np.argmin(v))]
    for f in range(n_frames - 1, 0, -1):
        states.append(int(back[f, states[-1]]))
    states.reverse()

    # --- segments of consecutive equal pitch states (REST excluded) -----
    segments = []  # (pitch, f0, f1) frames [f0, f1)
    run_p, run_start = None, 0
    for f, st in enumerate(states):
        if st != run_p:
            if run_p is not None and run_p != n_p:
                segments.append((run_p, run_start, f))
            run_p, run_start = st, f
    if run_p is not None and run_p != n_p:
        segments.append((run_p, run_start, n_frames))

    # --- merge same-pitch segments across short REST gaps ---------------
    merged = []
    for seg in segments:
        if merged:
            prev_p, prev_a, prev_b = merged[-1]
            gap_s = (seg[1] - prev_b) * hop_sec
            if prev_p == seg[0] and gap_s <= MERGE_GAP_S + 1e-9:
                merged[-1] = (prev_p, prev_a, seg[2])
                continue
        merged.append(seg)

    # --- snap boundaries to onsets, build events -------------------------
    def snap(frame):
        for o in onset_frames:
            if abs(o - frame) <= ONSET_SNAP_FRAMES:
                return o
        return frame

    events = []
    for pitch, a, b in merged:
        a, b = snap(a), snap(b)
        dur = (b - a) * hop_sec
        if dur < preset.min_note_s:
            continue
        conf = float(np.mean(sal[a:max(b, a + 1), pitch]))
        events.append(NoteEvent(
            pitch=preset.midi_range[0] + int(pitch),
            start=round(a * hop_sec, 4),
            end=round(b * hop_sec, 4),
            conf=round(min(max(conf, 0.0), 1.0), 4),
        ))
    return events

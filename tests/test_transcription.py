"""Phase 1 monophonic transcription suite: synthetic-audio note recovery,
octave robustness, kick immunity, preset separation, dropout merging, MIDI
round-trip, HTTP notes/midi endpoints (cache + errors) and byte
determinism.

Fixtures are built locally (seeded; harmonic stacks = sum 1/k*sin, 100 ms
attack/decay envelopes) — no external dataset is required. The production
path is exercised through ``transcribe`` reading a real cached ``stft.npy``
memmap, so the chunk discipline is under test too.
"""

import io
import json
import threading
import urllib.error
import urllib.request

import mido
import numpy as np
import pytest

from helpers import SR, make_wav
from keyprism import analyze, audio_io, transcribe
from keyprism.decode import NoteEvent, decode_monophonic
from keyprism.midi_io import export_midi
from keyprism.onset import detect_onsets
from keyprism.salience import (
    apply_loudness_weighting, a_weighting_gain, compute_salience,
)
from keyprism.server import make_server
from keyprism.tracks import MONO_TRACKS, MONO_VERSION
from keyprism.transform import iter_stft_chunks, n_frames_of, stft_complex

WIN = 8192
HOP = WIN // 4
ATTACK = 0.1  # seconds, envelope attack/decay per the Phase 1 spec


# ------------------------------------------------------------ fixtures

def _env(n: int, sr: int) -> np.ndarray:
    """100 ms linear attack + sustain + 100 ms linear release."""
    e = np.ones(n)
    a = min(n, int(ATTACK * sr))
    e[:a] = np.linspace(0.0, 1.0, a)
    e[n - a:] = np.linspace(1.0, 0.0, a)
    return e


def synth_note(midi: int, dur: float, sr: int = SR, amp: float = 0.5,
               stack: int = 1) -> np.ndarray:
    """One note: pure sine (stack=1) or harmonic stack sum 1/k*sin."""
    f0 = 440.0 * 2.0 ** ((midi - 69) / 12.0)
    t = np.arange(int(dur * sr)) / sr
    x = np.zeros_like(t)
    for k in range(1, stack + 1):
        x += amp / k * np.sin(2 * np.pi * k * f0 * t)
    return x * _env(t.shape[0], sr)


def build_line(notes, sr: int = SR, stack: int = 1, amp: float = 0.5,
               overlay=None) -> np.ndarray:
    """Place (midi, start, dur) notes on silence; `overlay` adds
    (start, samples) extras mixed on top."""
    total = max(int((s + d) * sr) for _, s, d in notes)
    if overlay:
        total = max(total, max(int(s * sr) + e.shape[0]
                               for s, e in overlay))
    x = np.zeros(total + 1)
    for midi, s, d in notes:
        seg = synth_note(midi, d, sr, amp=amp, stack=stack)
        i = int(s * sr)
        x[i:i + seg.shape[0]] += seg
    for start, extra in (overlay or []):
        i = int(start * sr)
        x[i:i + extra.shape[0]] += extra
    peak = np.abs(x).max()
    if peak > 0.95:
        x *= 0.95 / peak
    return x


def kick(at: float, sr: int = SR, dur: float = 0.08, amp: float = 0.5,
         seed: int = 7) -> np.ndarray:
    """Broadband noise burst, 80 ms exponential decay (deterministic)."""
    rng = np.random.default_rng(seed)
    n = int(dur * sr)
    return amp * rng.standard_normal(n) * np.exp(-np.arange(n) / (sr * 0.02))


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Relocate the whole workspace so tests never touch ~/.keyprism."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(audio_io, "KEYPRISM_HOME", h)
    return h


def make_entry(x: np.ndarray, sr: int = SR, win: int = WIN) -> object:
    """Store the STFT of a mono signal as a real cache entry (the same
    artifact the production pipeline reads back through a memmap)."""
    hop = win // 4
    entry = analyze.entry_dir("pcm_test", "par_test")
    n_frames = n_frames_of(x.shape[0], win, hop, True)
    analyze.store_stft(entry, iter_stft_chunks(x, win=win, hop=hop),
                       n_frames, win // 2 + 1)
    analyze.store_meta(entry, {
        "sr": sr, "win": win, "hop": hop, "n_frames": n_frames,
        "n_bins": win // 2 + 1, "duration": round(x.shape[0] / sr, 6),
        "bpm": 120.0, "beat_offset_sec": 0.0,
    })
    return entry


def pipeline_notes(x: np.ndarray, track: str, win: int = WIN):
    """Full production path: cached stft.npy -> notes body."""
    entry = make_entry(x, win=win)
    body, _ = transcribe.get_notes(entry, track)
    return body["notes"][track]


# ------------------------------------------------------------ matching

def count_matched(detected, ground_truth, tol_start=0.15, min_overlap=0.4):
    """Greedy matcher: pitch-exact, onset within tol_start, overlap check.
    Returns (matched_gt_count, false_positive_count)."""
    used = [False] * len(detected)
    hits = 0
    for midi, start, dur in ground_truth:
        best = None
        for i, n in enumerate(detected):
            if used[i] or n["pitch"] != midi:
                continue
            if abs(n["start"] - start) > tol_start:
                continue
            overlap = (min(n["end"], start + dur)
                       - max(n["start"], start))
            if overlap >= min_overlap * dur and \
                    (best is None or overlap > best[1]):
                best = (i, overlap)
        if best is not None:
            used[best[0]] = True
            hits += 1
    return hits, used.count(False)


# ------------------------------------------------------- salience units

def test_salience_peaks_at_true_pitch():
    sr, win = SR, WIN
    p = MONO_TRACKS["bass"]
    t = np.arange(sr) / sr
    Z = stft_complex(0.5 * np.sin(2 * np.pi * 110.0 * t), win=win)
    magw = apply_loudness_weighting(np.abs(Z), sr)
    S = compute_salience(magw, sr, win, win // 4, p)
    frame = S[S.shape[0] // 2]
    assert p.midi_range[0] + int(np.argmax(frame)) == 45  # A2
    assert S.dtype == np.float32 and S.shape[1] == 40


def test_salience_suppresses_octave_up():
    sr, win = SR, WIN
    p = MONO_TRACKS["bass"]
    t = np.arange(sr) / sr
    f0 = 98.0  # G2
    x = (0.5 * sum(np.sin(2 * np.pi * k * f0 * t) / k
                   for k in range(1, 11))
         + 0.3 * np.sin(2 * np.pi * 2 * f0 * t))
    Z = stft_complex(x, win=win)
    magw = apply_loudness_weighting(np.abs(Z), sr)
    S = compute_salience(magw, sr, win, win // 4, p)
    frame = S[S.shape[0] // 2]
    lo = p.midi_range[0]
    assert frame[43 - lo] > frame[55 - lo]  # true G2 beats its octave


def test_salience_chunk_invariant():
    """Per-frame salience is a pure function of the frame slice: chunked
    computation reproduces the one-shot result exactly."""
    sr, win = SR, WIN
    p = MONO_TRACKS["lead"]
    x = build_line([(60, 0.1, 0.5), (64, 0.9, 0.5)], sr=sr, stack=3)
    Z = stft_complex(x, win=win)
    magw = apply_loudness_weighting(np.abs(Z), sr)
    full = compute_salience(magw, sr, win, win // 4, p)
    parts = [compute_salience(magw[a:a + 37], sr, win, win // 4, p)
             for a in range(0, magw.shape[0], 37)]
    assert np.array_equal(full, np.concatenate(parts, axis=0))


def test_loudness_weighting_shape():
    sr = SR
    bins = WIN // 2 + 1
    freqs = np.fft.rfftfreq(WIN, 1.0 / sr)
    gain = a_weighting_gain(freqs)
    assert gain.shape == (bins,)
    assert float(a_weighting_gain(np.array([1000.0]))[0]) \
        == pytest.approx(1.0, abs=1e-12)          # 0 dB at exactly 1 kHz
    k1k = int(np.rint(1000.0 * WIN / sr))
    assert gain[k1k] == pytest.approx(1.0, abs=1e-3)  # nearest 1 kHz bin
    assert 0.5 < gain[int(np.rint(100.0 * WIN / sr))] < 0.75  # lows rolled off
    assert gain[int(np.rint(3000.0 * WIN / sr))] > 1.0   # presence boosted
    mag = np.ones((3, bins))
    w = apply_loudness_weighting(mag, sr)
    assert w.shape == mag.shape and np.allclose(w[0], gain)


# ---------------------------------------------------------------- onsets

def test_onsets_on_tone_bursts_and_silence():
    sr = SR
    x = build_line([(45, 0.2, 0.5), (48, 1.2, 0.5), (50, 2.2, 0.5)], sr=sr)
    entry = make_entry(x)
    mm, meta = analyze.load_entry(entry)
    win = int(meta["win"])
    mag = np.abs(np.asarray(mm))
    onsets = detect_onsets(mag, int(meta["sr"]), int(meta["hop"]),
                           MONO_TRACKS["bass"])
    hop_s = meta["hop"] / meta["sr"]
    for want in (0.2, 1.2, 2.2):
        assert any(abs(f * hop_s - want) <= 0.15 for f in onsets), \
            (want, [round(f * hop_s, 2) for f in onsets])
    assert detect_onsets(np.zeros_like(mag), SR, HOP,
                         MONO_TRACKS["bass"]) == []


# ------------------------------------------------------- note recovery 1

BASS_LINE = [(45, 0.5, 0.5), (48, 1.4, 0.5), (50, 2.3, 0.5),
             (45, 3.2, 0.5), (43, 4.1, 0.5)]


def test_bass_pure_sine_recall_full():
    x = build_line(BASS_LINE)
    notes = pipeline_notes(x, "bass")
    hits, fp = count_matched(notes, BASS_LINE)
    assert hits == len(BASS_LINE)      # recall 1.0
    assert fp == 0 and len(notes) == len(BASS_LINE)


# ------------------------------------------------ octave-up distractor 2

def test_harmonic_stack_with_octave_tone_no_octave_up():
    f0s = [45, 48, 50, 45, 43]
    notes_spec = []
    overlay = []
    for i, (midi, s, d) in enumerate(BASS_LINE):
        notes_spec.append((midi, s, d))
        f0 = 440.0 * 2.0 ** ((midi - 69) / 12.0)
        t = np.arange(int(d * SR)) / SR
        overlay.append((s, 0.3 * np.sin(2 * np.pi * 2 * f0 * t)
                        * _env(t.shape[0], SR)))
    x = build_line(notes_spec, stack=10, overlay=overlay)
    notes = pipeline_notes(x, "bass")
    hits, fp = count_matched(notes, BASS_LINE)
    assert hits == len(BASS_LINE)
    assert fp == 0
    # explicitly: nothing an octave above any ground-truth pitch
    gt_octaves = {m + 12 for m, _, _ in BASS_LINE}
    assert all(n["pitch"] not in gt_octaves for n in notes)


# ------------------------------------------------------------ kicks    3

def test_bass_line_with_kicks_zero_kick_notes():
    overlay = []
    t = 1.05
    while t < 4.6:
        overlay.append((t, kick(t, seed=int(t * 100) % 997)))
        t += 0.5
    x = build_line(BASS_LINE, stack=5, overlay=overlay)
    notes = pipeline_notes(x, "bass")
    hits, fp = count_matched(notes, BASS_LINE)
    assert hits == len(BASS_LINE)
    assert fp == 0  # every kick fell in a gap: no note may come from it


# ------------------------------------------------------- lead melody   4

LEAD_MELODY = [(m, 0.2 + 0.65 * i, 0.5) for i, m in enumerate(
    [48, 50, 52, 53, 55, 57, 59, 60, 62, 64, 65, 67, 69, 71, 72, 74, 76,
     77, 79, 81, 83, 84])]


def test_lead_sine_melody_recall_full():
    x = build_line(LEAD_MELODY, amp=0.45)
    notes = pipeline_notes(x, "lead")
    hits, fp = count_matched(notes, LEAD_MELODY)
    assert hits == len(LEAD_MELODY)
    assert fp == 0 and len(notes) == len(LEAD_MELODY)


# ------------------------------------------------- preset separation   5

BASS_HALF = [(40, 0.3, 0.8), (43, 1.4, 0.8), (45, 2.5, 0.8)]
LEAD_HALF = [(60, 0.3, 0.5), (62, 0.85, 0.5), (64, 1.4, 0.5),
             (67, 1.95, 0.5), (64, 2.5, 0.5), (62, 3.05, 0.5)]


def test_bass_and_lead_recover_own_notes_only():
    bass = build_line(BASS_HALF, stack=10, amp=0.5)
    lead = build_line(LEAD_HALF, amp=0.4)
    n = max(bass.shape[0], lead.shape[0])
    x = np.zeros(n)
    x[:bass.shape[0]] += bass
    x[:lead.shape[0]] += lead
    entry = make_entry(x)
    body, _ = transcribe.get_notes(entry, "both")
    bass_det = body["notes"]["bass"]
    lead_det = body["notes"]["lead"]

    hits, fp = count_matched(bass_det, BASS_HALF)
    assert hits == len(BASS_HALF) and fp == 0
    hits, fp = count_matched(lead_det, LEAD_HALF)
    assert hits == len(LEAD_HALF) and fp == 0

    # zero notes of the other preset's pitch set (±1 semitone tolerance
    # allows at most one stray)
    lead_set = {m for m, _, _ in LEAD_HALF}
    bass_set = {m for m, _, _ in BASS_HALF}
    stray_in_bass = sum(1 for n in bass_det
                        if any(abs(n["pitch"] - m) <= 1 for m in lead_set))
    stray_in_lead = sum(1 for n in lead_det
                        if any(abs(n["pitch"] - m) <= 1 for m in bass_set))
    assert stray_in_bass <= 1 and stray_in_lead == 0


# ------------------------------------------------------ dropout merge  6

def test_dropout_40ms_merges_to_single_note():
    sr, win = SR, 2048  # hop 512 -> 23 ms frames: a 40 ms dropout is 1-2
    hop = win // 4      # frames, inside the 60 ms merge gap
    x = build_line([(45, 0.2, 0.7)], sr=sr)
    entry = make_entry(x, sr=sr, win=win)
    mm, meta = analyze.load_entry(entry)
    mag = np.abs(np.asarray(mm))
    magw = apply_loudness_weighting(mag, sr)
    S = compute_salience(magw, sr, win, hop, MONO_TRACKS["bass"]).astype(
        np.float64)
    S /= max(S.max(), 1e-12)
    hop_s = hop / sr
    # zero the salience for 40 ms mid-span (0.40 s .. 0.44 s)
    a = int(np.ceil(0.40 / hop_s))
    b = int(np.ceil(0.44 / hop_s))
    assert 0 < a < b < S.shape[0]
    S[a:b] = 0.0
    events = decode_monophonic(S, [], hop_s, MONO_TRACKS["bass"])
    assert len(events) == 1
    ev = events[0]
    assert ev.pitch == 45
    assert ev.start == pytest.approx(0.2, abs=0.12)
    assert ev.end == pytest.approx(0.9, abs=0.12)
    assert 0.0 <= ev.conf <= 1.0


# --------------------------------------------------------- MIDI      7

def _abs_notes(mid: mido.MidiFile):
    out = []
    for tr in mid.tracks:
        t = 0
        for msg in tr:
            t += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                out.append((msg.note, t))
    return out


def test_midi_round_trip_type0_and_type1():
    notes = [NoteEvent(pitch=45, start=0.5, end=1.0, conf=0.9),
             NoteEvent(pitch=48, start=1.4, end=2.0, conf=0.5)]
    lead_notes = [NoteEvent(pitch=72, start=0.25, end=0.75, conf=1.0)]
    bpm, offset = 120.0, 0.0

    data0 = export_midi({"bass": notes}, bpm, offset)
    mid0 = mido.MidiFile(file=io.BytesIO(data0))
    assert mid0.type == 0 and mid0.ticks_per_beat == 480
    got = _abs_notes(mid0)
    assert sorted(p for p, _ in got) == [45, 48]
    for pitch, tick in got:
        want = round(dict((n.pitch, n.start) for n in notes)[pitch]
                     * 480 * bpm / 60.0)
        assert tick == want  # within one tick (exact here)

    data1 = export_midi({"bass": notes, "lead": lead_notes}, bpm, offset)
    mid1 = mido.MidiFile(file=io.BytesIO(data1))
    assert mid1.type == 1 and len(mid1.tracks) == 3  # tempo + 2 note tracks
    pitches = sorted(p for p, _ in _abs_notes(mid1))
    assert pitches == [45, 48, 72]
    tempos = [m for tr in mid1.tracks for m in tr if m.type == "set_tempo"]
    assert len(tempos) == 1 and tempos[0].tempo == mido.bpm2tempo(120.0)


def test_midi_offset_shifts_and_bad_bpm_falls_back():
    notes = [NoteEvent(pitch=60, start=1.0, end=1.5, conf=0.8)]
    data = export_midi({"lead": notes}, 0.0, 0.5)  # bpm<=0 -> 120 fallback
    mid = mido.MidiFile(file=io.BytesIO(data))
    onsets = _abs_notes(mid)
    # (1.0 - 0.5) s at 120 bpm, 480 tpq -> 480 ticks
    assert onsets == [(60, 480)]


# ---------------------------------------------------------- HTTP      8

@pytest.fixture()
def srv(tmp_path, monkeypatch):
    """Server on a temp workspace + ephemeral port (window 2048 for
    speed), auto-shutdown at test end."""
    pub = tmp_path / "pub"
    pub.mkdir()
    monkeypatch.setattr(audio_io, "PUBLIC_DIR", pub)
    monkeypatch.setattr(audio_io, "UPLOAD_DIR", tmp_path / "uploads")
    home = tmp_path / "home"     # already created by the autouse fixture
    monkeypatch.setattr(audio_io, "KEYPRISM_HOME", home)
    wav = tmp_path / "t.wav"
    make_wav(wav, seconds=1.5)
    server = make_server(wav, 0, "127.0.0.1", 0.0, None, 2048, 70.0, 5, 1)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    yield {
        "base": f"http://127.0.0.1:{server.server_address[1]}",
        "home": home,
        "pub": pub,
        "wav": wav,
    }
    server.shutdown()
    server.server_close()
    th.join(timeout=5)


def get_raw(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_api_notes_schema_and_cache_hit(srv):
    base = srv["base"]
    code, raw, _ = get_raw(f"{base}/api/notes?track=bass")
    assert code == 200
    body = json.loads(raw.decode())
    assert body["track"] == "bass"
    assert set(body["notes"]) == {"bass"}
    for n in body["notes"]["bass"]:
        assert isinstance(n["pitch"], int)
        assert isinstance(n["start"], float)
        assert isinstance(n["end"], float)
        assert isinstance(n["conf"], float)

    cached = list(srv["home"].glob(
        f"cache/analysis/*/*/notes/{MONO_VERSION}/notes_bass.json"))
    assert len(cached) == 1
    mtime = cached[0].stat().st_mtime_ns
    code2, raw2, _ = get_raw(f"{base}/api/notes?track=bass")
    assert code2 == 200 and raw2 == raw          # served verbatim
    assert cached[0].stat().st_mtime_ns == mtime  # file untouched


def test_api_notes_both_and_unknown_track(srv):
    base = srv["base"]
    code, raw, _ = get_raw(f"{base}/api/notes?track=both")
    assert code == 200
    body = json.loads(raw.decode())
    assert body["track"] == "both"
    assert set(body["notes"]) == {"bass", "lead"}
    code, raw, _ = get_raw(f"{base}/api/notes?track=nope")
    assert code == 400 and "error" in json.loads(raw.decode())


def test_api_midi_download_and_errors(srv):
    base = srv["base"]
    code, raw, headers = get_raw(f"{base}/api/midi?track=lead")
    assert code == 200
    assert headers["Content-Type"] == "audio/midi"
    assert "attachment" in headers.get("Content-Disposition", "")
    assert headers["Content-Disposition"].endswith('lead.mid"')
    mid = mido.MidiFile(file=io.BytesIO(raw))  # re-opens cleanly
    assert mid.type == 0
    code, raw, _ = get_raw(f"{base}/api/midi?track=nope")
    assert code == 400 and "error" in json.loads(raw.decode())


# --------------------------------------------------- determinism      9

def test_two_cold_runs_byte_identical(home):
    x = build_line([(45, 0.3, 0.6), (48, 1.2, 0.6)], stack=5)
    entry = make_entry(x)
    body1, cached1 = transcribe.get_notes(entry, "both")
    assert cached1 is False
    raw1 = transcribe.cached_notes_path(entry, "bass").read_bytes()
    raw1_lead = transcribe.cached_notes_path(entry, "lead").read_bytes()

    # cold: drop every note cache file, recompute from scratch
    for p in (entry / "notes" / MONO_VERSION).glob("*.json"):
        p.unlink()
    body2, cached2 = transcribe.get_notes(entry, "both")
    assert cached2 is False
    raw2 = transcribe.cached_notes_path(entry, "bass").read_bytes()
    raw2_lead = transcribe.cached_notes_path(entry, "lead").read_bytes()
    assert raw1 == raw2 and raw1_lead == raw2_lead
    assert body1 == body2

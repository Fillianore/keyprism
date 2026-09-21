"""Phase 3 DL workspace tests: graceful degradation, Demucs chunking
math, note merging, background-task plumbing, frontend canvas sync.

The DL dependencies are OPTIONAL: every test here runs in BOTH
environments (with and without ``uv sync --extra dl``) because the
models are stubbed/injected — the availability flags are monkeypatched
explicitly, so a machine with onnxruntime installed takes exactly the
same code paths.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from keyprism import audio_io, dlsep, poly_transcribe
from keyprism import server as server_mod
from helpers import make_wav
from keyprism.server import make_server


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    """Server on an isolated workspace (KEYPRISM_HOME included: DL stems
    and note caches land under tmp) + ephemeral port."""
    pub = tmp_path / "pub"
    pub.mkdir()
    monkeypatch.setattr(audio_io, "PUBLIC_DIR", pub)
    monkeypatch.setattr(audio_io, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(audio_io, "KEYPRISM_HOME", tmp_path / "home")
    wav = tmp_path / "t.wav"
    make_wav(wav, seconds=1.5)
    server = make_server(wav, 0, "127.0.0.1", 0.0, None, 2048, 70.0, 5, 1)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield {
        "base": f"http://127.0.0.1:{server.server_address[1]}",
        "pub": pub,
        "wav": wav,
    }
    server.shutdown()
    server.server_close()
    t.join(timeout=5)


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def post(url, data: bytes = b""):
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


# ------------------------------------------------- graceful degradation

def test_demucs_501_when_dl_missing(srv, monkeypatch):
    """Without onnxruntime the DL endpoints answer 501 Not Implemented —
    never a 500 and never a crash."""
    monkeypatch.setattr(server_mod, "DL_AVAILABLE", False)
    code, body = post(f"{srv['base']}/api/stems?method=demucs_4")
    assert code == 501 and "dl" in body["error"].lower()
    code, body = get(f"{srv['base']}/api/notes?track=piano&method=poly")
    assert code == 501 and "dl" in body["error"].lower()


def test_poly_501_when_basic_pitch_missing(srv, monkeypatch):
    """basic-pitch can be missing independently of onnxruntime."""
    monkeypatch.setattr(server_mod, "DL_AVAILABLE", True)
    monkeypatch.setattr(server_mod, "POLY_AVAILABLE", False)
    code, body = get(f"{srv['base']}/api/notes?track=piano&method=poly")
    assert code == 501


def test_demucs_validation(srv, monkeypatch):
    """Route validation on the DL endpoints (flags on: the deps are
    absent but the server pretends they exist)."""
    monkeypatch.setattr(server_mod, "DL_AVAILABLE", True)
    monkeypatch.setattr(server_mod, "POLY_AVAILABLE", True)
    code, body = post(f"{srv['base']}/api/stems?method=nope")
    assert code == 400
    code, body = get(f"{srv['base']}/api/stems?method=demucs_4")
    assert code == 404 and "尚未计算" in body["error"]
    code, body = get(f"{srv['base']}/api/ping")
    assert code == 200 and "capabilities" in body


# -------------------------------------------------------- chunking math

def test_plan_chunks_edges():
    assert dlsep.plan_chunks(0, 10, 2) == []
    assert dlsep.plan_chunks(5, 10, 2) == [(0, 5)]
    assert dlsep.plan_chunks(10, 10, 2) == [(0, 10)]
    assert dlsep.plan_chunks(11, 10, 2) == [(0, 10), (1, 11)]
    # hop = chunk - overlap; tail chunk pulled back to end exactly (the
    # prior chunk is kept so every seam keeps at least the nominal
    # overlap for proper cross-fading)
    assert dlsep.plan_chunks(25, 10, 4) == \
        [(0, 10), (6, 16), (12, 22), (15, 25)]
    with pytest.raises(ValueError):
        dlsep.plan_chunks(100, 10, 10)
    with pytest.raises(ValueError):
        dlsep.plan_chunks(100, 10, -1)


def test_hann_cola_perfect_reconstruction():
    w = dlsep.hann_cola(480)
    # strictly positive: single-coverage regions divide out exactly
    assert w.min() > 0.0
    # 50% overlap pair sum is exactly 1 (fade_out + fade_in == 1)
    assert np.allclose(w[:240] + w[240:480], 1.0, atol=1e-12)


def test_ola_separate_exact_length_and_reconstruction():
    """100 s synthetic signal, 10 s chunks with 1 s overlap: every stem
    has EXACTLY the input length (no missing/extra edge samples) and a
    separation whose stems sum to the input is reconstructed perfectly
    through the cross-fade."""
    sr = 8000
    n = 100 * sr
    t = np.arange(n) / sr
    x = (0.4 * np.sin(2 * np.pi * 220 * t)
         + 0.2 * np.sin(2 * np.pi * 2000 * t)).astype(np.float32)

    calls = []

    def perfect_split(chunk):
        # a model whose stems sum to its input (identity separation)
        calls.append(chunk.shape[0])
        return np.stack([chunk, np.zeros_like(chunk)], axis=0)

    out = dlsep.ola_separate(x, perfect_split, chunk_sec=10.0,
                             overlap_sec=1.0, sr=sr)
    # 100 s / (10 s - 1 s) hop -> 11 chunks, first+last exact tiling
    assert len(calls) == 11
    assert all(c == 10 * sr for c in calls)
    assert set(out) == {"stem0", "stem1"}
    for row in out.values():
        assert row.shape[0] == n  # exact length: edges included
    assert np.allclose(out["stem0"] + out["stem1"], x, atol=1e-5)

    # proportional model: a weighted average of c*x over every chunk is
    # c*x itself — sharp proof that the blend weights sum to 1 at EVERY
    # sample, head/tail single-coverage regions included
    c = 0.37
    out2 = dlsep.ola_separate(
        x, lambda ch: np.tile(c * ch, (3, 1)), chunk_sec=10.0,
        overlap_sec=1.0, sr=sr)
    for row in out2.values():
        assert np.allclose(row, c * x, atol=1e-5)
        assert row.shape[0] == n


def test_separator_separate_contract():
    """DemucsSeparator.separate: mono PCM in -> {stem: array} out with
    the registry stem keys, injected infer (no ONNX needed)."""
    sr = dlsep.TARGET_SR
    x = np.zeros(int(3.5 * sr), dtype=np.float32)

    def infer(chunk):
        return np.stack([chunk, -chunk, chunk * 0.5, np.zeros_like(chunk)])

    sep = dlsep.DemucsSeparator("demucs_4", infer=infer)
    out = sep.separate(x, sr, chunk_sec=1.0, overlap_sec=0.1)
    assert list(out) == list(dlsep.STEM_SPECS["demucs_4"])
    for row in out.values():
        assert row.shape[0] == x.shape[0]
    with pytest.raises(ValueError):
        dlsep.DemucsSeparator("demucs_9")
    if not dlsep.ORT_AVAILABLE:  # with the [dl] extra this would download
        with pytest.raises(dlsep.DLMissingError):
            dlsep.DemucsSeparator("demucs_4")  # no onnxruntime in this env


# --------------------------------------------------------- note merging

def test_merge_fragmented_notes():
    """10 same-pitch fragments with 30 ms gaps -> ONE merged note."""
    frags = [{"pitch": 60, "start": 1.0 + i * 0.13, "end": 1.0 + i * 0.13 + 0.1,
              "conf": 0.5 + 0.01 * i} for i in range(10)]
    merged = poly_transcribe.merge_fragmented_notes(frags, gap_s=0.05)
    assert len(merged) == 1
    assert merged[0]["pitch"] == 60
    assert merged[0]["start"] == pytest.approx(1.0)
    assert merged[0]["end"] == pytest.approx(1.0 + 9 * 0.13 + 0.1)
    assert merged[0]["conf"] == pytest.approx(0.59)


def test_merge_respects_pitch_and_gap():
    notes = [
        {"pitch": 60, "start": 0.0, "end": 0.2, "conf": 0.8},
        {"pitch": 61, "start": 0.21, "end": 0.4, "conf": 0.8},   # other pitch
        {"pitch": 60, "start": 0.30, "end": 0.5, "conf": 0.8},   # 100 ms gap
        {"pitch": 60, "start": 0.55, "end": 0.7, "conf": 0.8},   # 50 ms gap
        {"pitch": 60, "start": 0.72, "end": 0.9, "conf": 0.8},   # 20 ms gap
    ]
    merged = poly_transcribe.merge_fragmented_notes(notes, gap_s=0.05)
    assert [(n["pitch"], round(n["start"], 2), round(n["end"], 2))
            for n in merged] == [
        (60, 0.0, 0.2), (60, 0.30, 0.5), (60, 0.55, 0.9), (61, 0.21, 0.4)]


def test_transcribe_polyphonic_merges_a_chord():
    """A synthetic long chord reported as per-frame fragments comes out
    as exactly ONE note per chord pitch (merge runs in every case)."""
    sr = 22050

    def fragmented_chord_predict(x, sr):
        # simulate a frame-based backend: three sustained pitches, each
        # chopped into 50 ms fragments with 10 ms confidence gaps
        events = []
        for pitch in (60, 64, 67):
            t = 0.5
            while t < 2.5:
                events.append({"start": t, "end": t + 0.05,
                               "pitch": pitch, "amplitude": 0.7})
                t += 0.06
        return {"note-events": events}

    pcm = np.zeros(sr, dtype=np.float32)  # audio content is irrelevant:
    notes = poly_transcribe.transcribe_polyphonic(   # the stub is the model
        pcm, sr, predict_fn=fragmented_chord_predict)
    pitches = sorted(n["pitch"] for n in notes)
    assert pitches == [60, 64, 67]
    for n in notes:
        assert n["start"] == pytest.approx(0.5)
        assert n["end"] == pytest.approx(0.5 + 33 * 0.06 + 0.05)


def test_convert_raw_accepts_library_shapes():
    # attribute objects (namedtuple-like), tuples, dicts all convert
    class Ev:
        start, end, pitch, amplitude = 0.1, 0.4, 72, 0.9

    raw = {"note-events": [Ev(), (0.11, 0.41, 72, 0.8),  # merges with Ev
                           {"start": 1.0, "end": 1.3, "pitch": 55,
                            "amplitude": 0.6}]}
    notes = poly_transcribe._convert_raw(raw, merge_gap_s=0.05)
    assert len(notes) == 2
    assert (notes[0]["pitch"], notes[0]["end"]) == (55, pytest.approx(1.3))
    assert (notes[1]["pitch"], notes[1]["end"]) == (72, pytest.approx(0.41))


# ------------------------------------------- background task + DL cache

class FakeSeparator:
    """Stands in for the ONNX DemucsSeparator (no weights needed)."""

    def __init__(self, variant):
        self.variant = variant
        self.stems = dlsep.STEM_SPECS[variant]

    def separate(self, pcm, sr, chunk_sec=10.0, overlap_sec=1.0,
                 progress=None):
        n = int(len(pcm) * dlsep.TARGET_SR / sr)
        out = {}
        for i, key in enumerate(self.stems):
            t = np.arange(n) / dlsep.TARGET_SR
            out[key] = 0.3 * np.sin(2 * np.pi * (200 + 100 * i) * t)
        if progress is not None:
            progress(1, 1)
        return out


def wait_task(base, task_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, body = get(f"{base}/api/task/{task_id}")
        assert code == 200
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.05)
    raise AssertionError("task did not finish in time")


def test_demucs_task_flow_and_cache(srv, monkeypatch):
    monkeypatch.setattr(server_mod, "DL_AVAILABLE", True)
    monkeypatch.setattr(server_mod, "POLY_AVAILABLE", True)
    monkeypatch.setattr(dlsep, "get_separator", FakeSeparator)

    # start the background separation
    code, body = post(f"{srv['base']}/api/stems?method=demucs_4")
    assert code == 200
    assert body["status"] == "started"
    assert body["status_url"] == f"/api/task/{body['task_id']}"

    # poll to completion (never blocks an HTTP connection)
    done = wait_task(srv["base"], body["task_id"])
    assert done["status"] == "done"
    assert [s["key"] for s in done["stems"]] == \
        list(dlsep.STEM_SPECS["demucs_4"])
    assert done["duration"] == pytest.approx(1.5, abs=0.1)

    # the cache now serves GET (and a second POST short-circuits)
    code, got = get(f"{srv['base']}/api/stems?method=demucs_4")
    assert code == 200 and got["cached"] is True
    assert len(got["stems"]) == 4
    code, again = post(f"{srv['base']}/api/stems?method=demucs_4")
    assert code == 200 and again.get("cached") is True

    # per-stem WAV download streams from the cache
    url = got["stems"][3]["url"].split(srv["base"])[-1]
    with urllib.request.urlopen(f"{srv['base']}{url}", timeout=10) as r:
        assert r.status == 200
        assert r.headers["Content-Type"] == "audio/wav"
        magic = r.read(4)
    assert magic == b"RIFF"

    # status/versioned files live under the isolated workspace
    found = list((audio_io.KEYPRISM_HOME / "cache").rglob(
        "dl_v1/demucs_4/vocals.wav"))
    assert found and (found[0].parent / "status.json").is_file()

    # unknown task id -> 404
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{srv['base']}/api/task/deadbeef",
                               timeout=10)
    assert e.value.code == 404


def test_poly_notes_end_to_end_with_stub_predict(srv, monkeypatch):
    """demucs_4 stems (fake model) -> Basic Pitch stub (fragmented
    frames) -> merged poly notes served + cached; wrong stem/source
    combos fail with 400/409, never 500."""
    monkeypatch.setattr(server_mod, "DL_AVAILABLE", True)
    monkeypatch.setattr(server_mod, "POLY_AVAILABLE", True)
    monkeypatch.setattr(dlsep, "get_separator", FakeSeparator)

    def fragmented_predict(x, sr):
        events = [{"start": 0.1 + i * 0.06, "end": 0.14 + i * 0.06,
                   "pitch": 64, "amplitude": 0.8}
                  for i in range(20)]
        return {"note-events": events}

    monkeypatch.setattr(poly_transcribe, "default_predict_fn",
                        lambda: fragmented_predict)

    code, body = post(f"{srv['base']}/api/stems?method=demucs_4")
    assert code == 200
    wait_task(srv["base"], body["task_id"])  # the fake separation finishes

    # piano is not a demucs_4 stem (explicit wrong source)
    code, body = get(f"{srv['base']}/api/notes?track=piano&method=poly"
                     "&source=demucs_4")
    assert code == 400
    # default source for piano is demucs_6, which has not been computed
    code, body = get(f"{srv['base']}/api/notes?track=piano&method=poly")
    assert code == 409
    # the poly registry rejects monophonic-preset names
    code, body = get(f"{srv['base']}/api/notes?track=bass&method=poly")
    assert code == 400
    # no demucs_6 stems computed yet
    code, body = get(f"{srv['base']}/api/notes?track=other&method=poly"
                     "&source=demucs_6")
    assert code == 409

    # the real path: other stem -> stubbed Basic Pitch -> merged notes
    code, body = get(f"{srv['base']}/api/notes?track=other&method=poly"
                     "&source=demucs_4")
    assert code == 200
    notes = body["notes"]["other"]
    assert len(notes) == 1  # 20 fragments -> ONE merged note
    assert notes[0]["pitch"] == 64
    assert notes[0]["end"] == pytest.approx(0.1 + 19 * 0.06 + 0.04)
    assert body["cached"] is False

    # second call is a pure cache hit
    code, body = get(f"{srv['base']}/api/notes?track=other&method=poly"
                     "&source=demucs_4")
    assert code == 200 and body["cached"] is True
    cached = list((audio_io.KEYPRISM_HOME / "cache").rglob(
        "notes/poly_v1/notes_poly_other.json"))
    assert cached  # cached under the analysis entry, version-tagged


# ----------------------------------------- frontend canvas sync (source)

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "src"


def strip_js_comments(src: str) -> str:
    """Blank out /*...*/ and //... comments so a docstring mentioning a
    forbidden API is not mistaken for its usage."""
    import re

    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def test_lanes_js_canvas_sync_contract():
    """lanes.js must draw notes on a dedicated canvas synced to the
    plotly xaxis range — NEVER layout.shapes (SVG dies with 6 poly
    tracks), and only visible-window notes may be drawn (spatial index,
    not a full-array scan per frame)."""
    src = strip_js_comments(
        (FRONTEND / "lanes.js").read_text(encoding="utf-8"))
    assert "layout.shapes" not in src
    assert "createElement('canvas')" in src
    assert "plotly_relayout" in src          # zoom/pan -> canvas redraw
    assert "xaxis.range" in src              # range drives the transform
    # spatial index: binary search over sorted starts, not filter-all
    assert "lowerBound" in src and "maxDur" in src


def test_lanes_js_is_wired_into_the_app():
    main = (FRONTEND / "main.js").read_text(encoding="utf-8")
    assert "initLanes" in main
    html = (FRONTEND.parent / "index.html").read_text(encoding="utf-8")
    assert 'id="lanesPanel"' in html and "lanesToggle" in html

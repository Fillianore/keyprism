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
    monkeypatch.setattr(server_mod, "POLY_AVAILABLE", False)
    code, body = post(f"{srv['base']}/api/stems?method=demucs_4")
    assert code == 501
    assert body["code"] == "dl_not_installed"
    assert "uv sync --extra dl" in body["error"]
    code, body = get(f"{srv['base']}/api/notes?track=piano&method=poly")
    assert code == 501 and body["code"] == "dl_not_installed"
    # the capability reason matches the 501 body verbatim
    _, ping = get(f"{srv['base']}/api/ping")
    caps = ping["capabilities"]
    assert caps["dl"] is False and caps["poly"] is False
    assert caps["poly_reason"] == body["error"]


def test_poly_501_when_basic_pitch_missing(srv, monkeypatch):
    """basic-pitch can be missing independently of onnxruntime: the poly
    501 then names the Python >= 3.12 limitation — a DIFFERENT code and
    message than the install-remedy body (Phase 3.7 I4)."""
    monkeypatch.setattr(server_mod, "DL_AVAILABLE", True)
    monkeypatch.setattr(server_mod, "POLY_AVAILABLE", False)
    code, body = get(f"{srv['base']}/api/notes?track=piano&method=poly")
    assert code == 501
    assert body["code"] == "poly_unavailable"
    assert "3.12" in body["error"]
    assert "uv sync" not in body["error"]

    # the two 501 bodies differ (codes AND prose) and ping agrees
    monkeypatch.setattr(server_mod, "DL_AVAILABLE", False)
    code, dl_body = get(f"{srv['base']}/api/notes?track=piano&method=poly")
    assert code == 501 and dl_body["code"] == "dl_not_installed"
    assert dl_body["error"] != body["error"]
    _, ping = get(f"{srv['base']}/api/ping")
    caps = ping["capabilities"]
    assert caps["poly"] is False
    assert caps["poly_reason"] == dl_body["error"]


def test_poly_reason_absent_when_available(srv, monkeypatch):
    """poly_reason only exists when poly is off (the frontend keys on
    presence, not truthiness)."""
    monkeypatch.setattr(server_mod, "DL_AVAILABLE", True)
    monkeypatch.setattr(server_mod, "POLY_AVAILABLE", True)
    _, ping = get(f"{srv['base']}/api/ping")
    assert "poly_reason" not in ping["capabilities"]


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


def test_separator_fixed_segment_defaults():
    """Session-based separation runs the chunker at the export's fixed
    segment (the reference ONNX graphs embed the STFT and only accept
    SEGMENT_SAMPLES inputs) with a 50% COLA-exact overlap; an injected
    infer keeps the generic 10 s / 1 s defaults; explicit args win."""
    sr = dlsep.TARGET_SR
    seen = []

    def sep_with_recording_infer():
        s = dlsep.DemucsSeparator(
            "demucs_4",
            infer=lambda ch: (seen.append(ch.shape[0]),
                              np.stack([ch] * 4))[1])
        # pretend the model has the fixed segment (as the real session
        # does) WITHOUT constructing an InferenceSession
        s._segment = dlsep.SEGMENT_SAMPLES
        return s

    x = np.zeros(int(20 * sr), dtype=np.float32)
    out = sep_with_recording_infer().separate(x, sr)
    assert all(c == dlsep.SEGMENT_SAMPLES for c in seen)  # 7.8 s chunks
    assert len(seen) >= 3                                 # 50% overlap
    for row in out.values():
        assert row.shape[0] == x.shape[0]

    seen.clear()
    dlsep.DemucsSeparator(
        "demucs_4", infer=lambda ch: (seen.append(ch.shape[0]),
                                      np.stack([ch] * 4))[1]
    ).separate(x, sr)
    assert all(c == int(10.0 * sr) for c in seen)  # generic default

    seen.clear()
    dlsep.DemucsSeparator(
        "demucs_4", infer=lambda ch: (seen.append(ch.shape[0]),
                                      np.stack([ch] * 4))[1]
    ).separate(x, sr, chunk_sec=2.0, overlap_sec=0.5)
    assert all(c == int(2.0 * sr) for c in seen)  # explicit args win


def test_pad_segment_short_tail():
    """A sub-segment tail is zero-padded to the fixed model segment and
    the valid length reported back; longer inputs pass through."""
    x = np.ones(1000, dtype=np.float32)
    padded, valid = dlsep._pad_segment(x, 343980)
    assert valid == 1000 and padded.shape == (343980,)
    assert padded[:1000].sum() == 1000 and padded[1000:].sum() == 0
    y, valid2 = dlsep._pad_segment(np.ones(343980, dtype=np.float32),
                                   343980)
    assert valid2 == 0 and y.shape == (343980,)
    z, valid3 = dlsep._pad_segment(x, 0)  # flexible backend: no padding
    assert valid3 == 0 and z.shape == (1000,)


# ------------------------------------------------- 6-stem contract (3.10)

def test_demucs_6_registry_matches_reference_export():
    """3.10: demucs_6 resolves to the REAL 6-stem Hub export
    (StemSplitio/htdemucs-6s-onnx — the 3.5 "none exists" conclusion was
    wrong) and the registry mirrors its source order
    drums/bass/other/vocals/guitar/piano, guitar BEFORE piano: the
    separator zips model output rows with this list positionally."""
    assert dlsep.STEM_SPECS["demucs_6"] == \
        ("drums", "bass", "other", "vocals", "guitar", "piano")
    spec = dlsep._VARIANTS["demucs_6"]
    assert spec["repo"] == "StemSplitio/htdemucs-6s-onnx"
    assert spec["file"] == "htdemucs_6s.onnx"
    # provenance facts for the 3.11 license audit (weights stay on the
    # Hub, never in-repo)
    assert spec["license"] == "mit"
    assert "StemSplitio/htdemucs-6s-onnx" in spec["license_url"]


def test_demucs_6_separate_returns_six_registry_stems():
    """Injected 6-row infer -> separate() keys EXACTLY the demucs_6
    registry in order (guitar before piano)."""
    sr = dlsep.TARGET_SR

    def infer(chunk):
        return np.stack([chunk * (i + 1) / 6.0 for i in range(6)])

    out = dlsep.DemucsSeparator("demucs_6", infer=infer).separate(
        np.zeros(int(3.0 * sr), dtype=np.float32), sr,
        chunk_sec=1.0, overlap_sec=0.1)
    assert list(out) == ["drums", "bass", "other", "vocals",
                         "guitar", "piano"]
    assert len(out) == 6


class _FakeSession:
    """The slice of ort.InferenceSession that _run_session/_session_io
    use: one 'mix' [1, 2, T] input and a fixed-stem-count output."""

    class _In:
        name = "mix"
        shape = [1, 2, "T"]

    def __init__(self, stems, fail=False):
        self._stems = stems
        self._fail = fail

    def get_inputs(self):
        return [self._In()]

    def run(self, out_names, feed):
        if self._fail:
            raise RuntimeError(
                "[ONNXRuntimeError] Pad reflect pad width > input dim")
        t = list(feed.values())[0]
        return [np.zeros((1, self._stems, 2, t.shape[-1]),
                         dtype=np.float32)]


def test_zero_probe_fails_fast_on_mislabeled_export():
    """The zero-probe contract check (segment length + stem count) is
    kept at session load: a mislabelled 6-stem export (the smank
    htdemucs_6s actually ships 4 sources) fails immediately with an
    actionable message instead of failing a 40 s background job."""
    sep = dlsep.DemucsSeparator(
        "demucs_6", infer=lambda c: np.zeros((6, c.shape[0]),
                                             dtype=np.float32))
    sep._io = ("mix", 2)
    sep._segment = dlsep.SEGMENT_SAMPLES

    sep._session = _FakeSession(stems=4)  # mislabelled export
    with pytest.raises(ValueError, match="4 个 stem"):
        sep._probe()

    sep._session = _FakeSession(stems=6, fail=True)  # wrong segment
    with pytest.raises(ValueError, match="固定分段"):
        sep._probe()

    sep._session = _FakeSession(stems=6)  # correct export: probe passes
    sep._probe()


def test_demucs6_download_records_provenance(tmp_path, monkeypatch):
    """Auto-download of the 6-stem model records provenance (repo id,
    revision, commit, etag, license URL) into the model-cache metadata —
    the 3.11 license audit reads this, and no weights land in-repo."""
    monkeypatch.setattr(audio_io, "KEYPRISM_HOME", tmp_path / "home")
    monkeypatch.setattr(dlsep, "HF_AVAILABLE", True)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    monkeypatch.delenv("KEYPRISM_DEMUCS6_REPO", raising=False)
    seen = {}

    def fake_download_to(url, dest, progress=None, urlopen=None, meta=None):
        seen["url"] = url
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"FAKEONNX")
        if meta is not None:
            meta["etag"] = '"abc123"'
            meta["commit"] = "49df9b6989cf2150840ea65b0bef77a2e471b678"
        return dest

    monkeypatch.setattr(dlsep, "_download_to", fake_download_to)
    sep = dlsep.DemucsSeparator("demucs_6", infer=lambda c: c)
    p = sep._resolve_model_file()
    assert p.name == "htdemucs_6s.onnx"
    assert seen["url"] == (
        "https://huggingface.co/StemSplitio/htdemucs-6s-onnx/resolve/main/"
        "htdemucs_6s.onnx")

    body = json.loads(
        (tmp_path / "home" / "models" / "demucs" / "provenance.json")
        .read_text(encoding="utf-8"))
    entry = body["htdemucs_6s.onnx"]
    assert entry["variant"] == "demucs_6"
    assert entry["repo"] == "StemSplitio/htdemucs-6s-onnx"
    assert entry["revision"] == "main"
    assert entry["commit"] == "49df9b6989cf2150840ea65b0bef77a2e471b678"
    assert entry["license"] == "mit"
    assert "StemSplitio/htdemucs-6s-onnx" in entry["license_url"]
    assert entry["source"] == "auto-download"
    assert "/StemSplitio/htdemucs-6s-onnx/resolve/main/" in entry["source_url"]
    assert entry["bytes"] == 8

    # resolving the cached file again preserves the original download
    # facts (etag/commit/source_url) and only refreshes the timestamp
    sep._resolve_model_file()
    body = json.loads(
        (tmp_path / "home" / "models" / "demucs" / "provenance.json")
        .read_text(encoding="utf-8"))
    again = body["htdemucs_6s.onnx"]
    assert again["source"] == "cache"
    assert again["commit"] == entry["commit"]
    assert again["etag"] == entry["etag"]
    assert again["source_url"] == entry["source_url"]


def test_lanes_demucs_6_six_lane_contract():
    """Frontend source contract: demucs_6 renders SIX lanes (registry
    mirror in export source order) and the poly Notes chip stays on the
    poly-capable lanes (piano/guitar/other)."""
    lanes = strip_js_comments(
        (FRONTEND / "lanes.js").read_text(encoding="utf-8"))
    assert "'drums', 'bass', 'other', 'vocals', 'guitar', 'piano'" in lanes
    assert "POLY_LANES.has(lane.key)" in lanes
    # one lane row per registry stem (the render loop is registry-driven)
    assert "for (const lane of state.lanes) buildLaneRow" in lanes


# ------------------------------------------------- model download (3.8)

class _FakeResp:
    """Minimal urlopen() response: Content-Length + chunked reads."""

    def __init__(self, chunks, total):
        self._chunks = list(chunks)
        self.headers = {"Content-Length": str(total)}

    def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _BrokenResp:
    """A response that dies mid-stream (network drop)."""

    headers = {"Content-Length": "100"}

    def read(self, n=-1):
        raise OSError("connection reset")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_to_streams_and_renames_atomically(tmp_path):
    """_download_to: bytes land in <dest>.part, progress reports
    (bytes_done, bytes_total, speed_mbps) with the final call at
    100%, and the .part file is atomically renamed into place — the
    model cache never holds a partial file."""
    dest = tmp_path / "models" / "demucs" / "htdemucs.onnx"
    payload = [b"A" * 50, b"B" * 50]
    seen = []

    dlsep._download_to(
        "https://example.test/x.onnx", dest,
        progress=lambda d, t, s: seen.append((d, t, s)),
        urlopen=lambda req, timeout: _FakeResp(payload, 100))

    assert dest.read_bytes() == b"A" * 50 + b"B" * 50
    assert not dest.with_name(dest.name + ".part").exists()
    assert seen and seen[-1][:2] == (100, 100)   # final call: 100%
    assert seen[-1][2] >= 0.0                    # speed in MB/s
    assert all(t == 100 for _, t, _ in seen)
    dones = [d for d, _, _ in seen]
    assert dones == sorted(dones)                # monotonic bytes


def test_download_to_failure_leaves_no_partial(tmp_path):
    """An interrupted download removes the .part file and never
    creates the destination — retrying cannot serve a corrupt ONNX."""
    dest = tmp_path / "m.onnx"
    with pytest.raises(OSError):
        dlsep._download_to(
            "https://example.test/x.onnx", dest, progress=None,
            urlopen=lambda req, timeout: _BrokenResp())
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_task_reports_downloading_phase(srv, monkeypatch):
    """3.8 D3 contract: while the model weights download the task
    answers status:"downloading" with bytes_done / bytes_total /
    speed_mbps via /api/task/{id}, then flips to running/done for the
    inference phase — no extra clicks, same poll endpoint."""
    release = threading.Event()

    class DownloadingSeparator:
        def __init__(self, variant, download_progress=None):
            self.stems = dlsep.STEM_SPECS[variant]
            self._dl = download_progress

        def separate(self, pcm, sr, chunk_sec=10.0, overlap_sec=1.0,
                     progress=None):
            if self._dl is not None:
                self._dl(1_000_000, 4_000_000, 2.5)
                release.wait(timeout=5.0)
            out = {k: np.zeros(10) for k in self.stems}
            if progress is not None:
                progress(1, 1)
            return out

    monkeypatch.setattr(server_mod, "DL_AVAILABLE", True)
    monkeypatch.setattr(server_mod, "POLY_AVAILABLE", True)
    monkeypatch.setattr(dlsep, "get_separator", DownloadingSeparator)

    code, body = post(f"{srv['base']}/api/stems?method=demucs_4")
    assert code == 200 and body["status"] == "started"

    deadline = time.time() + 5.0
    phase = None
    while time.time() < deadline:
        code, tb = get(f"{srv['base']}/api/task/{body['task_id']}")
        assert code == 200
        if tb["status"] == "downloading":
            phase = tb
            break
        time.sleep(0.02)
    assert phase is not None, "task never reported the downloading phase"
    assert phase["bytes_done"] == 1_000_000
    assert phase["bytes_total"] == 4_000_000
    assert phase["speed_mbps"] == 2.5

    release.set()
    done = wait_task(srv["base"], body["task_id"])
    assert done["status"] == "done"
    assert "bytes_done" not in done  # download fields leave with the phase


# -------------------------------------------- execution providers (3.10)

def test_provider_chain_prefers_gpu(monkeypatch):
    """Probe order CUDA -> DirectML -> CoreML -> CPU, intersected with
    the compiled-in providers: a plain [dl] install (CPU-only build)
    silently falls back to CPU, never an error."""
    monkeypatch.delenv("KEYPRISM_ORT_PROVIDERS", raising=False)
    pc = dlsep.provider_chain
    assert pc(available=["CUDAExecutionProvider",
                         "CPUExecutionProvider"]) == \
        ["CUDAExecutionProvider", "CPUExecutionProvider"]
    assert pc(available=["DmlExecutionProvider",
                         "CPUExecutionProvider"]) == \
        ["DmlExecutionProvider", "CPUExecutionProvider"]
    assert pc(available=["CoreMLExecutionProvider",
                         "CPUExecutionProvider"]) == \
        ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    assert pc(available=["CPUExecutionProvider"]) == ["CPUExecutionProvider"]
    assert pc(available=[]) == []
    # several GPUs compiled in: the first entry of the preference wins
    assert pc(available=["CoreMLExecutionProvider",
                         "CUDAExecutionProvider",
                         "CPUExecutionProvider"])[0] == \
        "CUDAExecutionProvider"


def test_provider_chain_env_override(monkeypatch):
    """KEYPRISM_ORT_PROVIDERS="CUDA,CPU" overrides the preference
    (comma list, order = preference, aliases accepted); EPs the build
    lacks are silently dropped."""
    av = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    monkeypatch.setenv("KEYPRISM_ORT_PROVIDERS", "CUDA,CPU")
    assert dlsep.provider_chain(available=av) == \
        ["CUDAExecutionProvider", "CPUExecutionProvider"]
    # order = preference: CPU first is honored verbatim
    monkeypatch.setenv("KEYPRISM_ORT_PROVIDERS", "CPU,CUDA")
    assert dlsep.provider_chain(available=av) == \
        ["CPUExecutionProvider", "CUDAExecutionProvider"]
    # requested-but-missing EP: silently dropped -> CPU
    monkeypatch.setenv("KEYPRISM_ORT_PROVIDERS", "CUDA")
    assert dlsep.provider_chain(
        available=["CPUExecutionProvider"]) == ["CPUExecutionProvider"]
    # short alias
    monkeypatch.setenv("KEYPRISM_ORT_PROVIDERS", "DirectML")
    assert dlsep.provider_chain(
        available=["DmlExecutionProvider", "CPUExecutionProvider"]) == \
        ["DmlExecutionProvider", "CPUExecutionProvider"]
    # override= beats the env var (test seam)
    monkeypatch.setenv("KEYPRISM_ORT_PROVIDERS", "CUDA")
    assert dlsep.provider_chain(
        available=["CoreMLExecutionProvider", "CPUExecutionProvider"],
        override="coreml") == \
        ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    # the override replaces the default probe entirely: an unlisted-but-
    # available GPU is NOT picked (user preference wins); CPU remains
    monkeypatch.setenv("KEYPRISM_ORT_PROVIDERS", "CUDA,DirectML")
    assert dlsep.provider_chain(
        available=["CoreMLExecutionProvider",
                   "CPUExecutionProvider"]) == ["CPUExecutionProvider"]
    # an all-unknown override on a CPU-less fake build falls back to the
    # default probe
    assert dlsep.provider_chain(
        available=["CoreMLExecutionProvider"],
        override="CUDA") == ["CoreMLExecutionProvider"]


class _FakeOrt:
    """Stands in for the onnxruntime module inside _load_session:
    compiled-in provider list (get_available_providers) vs the ACTIVE
    readback (session.get_providers()), plus an optional failure set
    simulating a GPU EP that is compiled in but unusable."""

    class GraphOptimizationLevel:
        ORT_ENABLE_ALL = "all"

    class SessionOptions:
        def __init__(self):
            self.graph_optimization_level = None
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0

    def __init__(self, active, available=None, fail_on=None):
        self._active = list(active)
        self._available = list(available if available is not None
                               else active)
        self._fail_on = set(fail_on or ())
        self.requests = []

    def get_available_providers(self):
        return list(self._available)

    def InferenceSession(self, path, sess_options=None, providers=None):
        self.requests.append(list(providers or []))
        if set(providers or []) & self._fail_on:
            raise RuntimeError("CUDA error: driver/runtime mismatch")

        class _Sess:
            def get_providers(inner_self):  # noqa: N805
                return list(self._active)

        return _Sess()


def test_load_session_selects_providers_and_reads_back_active(
        tmp_path, monkeypatch):
    """The session is created with the probed chain and the ACTIVE
    provider list (session.get_providers() readback — ops may fall back
    to CPU inside the graph) is recorded for /api/ping."""
    fake = _FakeOrt(active=["CUDAExecutionProvider",
                            "CPUExecutionProvider"],
                    available=["CUDAExecutionProvider",
                               "CPUExecutionProvider"])
    monkeypatch.setattr(dlsep, "ort", fake)
    monkeypatch.setattr(dlsep, "_ACTIVE_PROVIDERS", [])
    monkeypatch.delenv("KEYPRISM_ORT_PROVIDERS", raising=False)
    model = tmp_path / "m.onnx"
    model.write_bytes(b"x")
    dlsep._load_session(model, threads=0)
    assert fake.requests == \
        [["CUDAExecutionProvider", "CPUExecutionProvider"]]
    assert dlsep.active_providers() == \
        ["CUDAExecutionProvider", "CPUExecutionProvider"]


def test_load_session_silent_cpu_fallback_when_gpu_unusable(
        tmp_path, monkeypatch):
    """A GPU EP that is compiled in but fails at session creation
    (driver/runtime mismatch) retries CPU-only — never a crash — and
    the active readback then reports plain CPU."""
    fake = _FakeOrt(active=["CPUExecutionProvider"],
                    available=["CUDAExecutionProvider",
                               "CPUExecutionProvider"],
                    fail_on={"CUDAExecutionProvider"})
    monkeypatch.setattr(dlsep, "ort", fake)
    monkeypatch.setattr(dlsep, "_ACTIVE_PROVIDERS", [])
    monkeypatch.delenv("KEYPRISM_ORT_PROVIDERS", raising=False)
    model = tmp_path / "m2.onnx"
    model.write_bytes(b"x")
    dlsep._load_session(model, threads=0)  # must not raise
    assert fake.requests == [
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
        ["CPUExecutionProvider"],
    ]
    assert dlsep.active_providers() == ["CPUExecutionProvider"]

    # a CPU-only request that still fails is a real error (re-raised)
    fake2 = _FakeOrt(active=[], available=["CPUExecutionProvider"],
                     fail_on={"CPUExecutionProvider"})
    monkeypatch.setattr(dlsep, "ort", fake2)
    model3 = tmp_path / "m3.onnx"
    model3.write_bytes(b"x")
    with pytest.raises(RuntimeError):
        dlsep._load_session(model3, threads=0)
    assert fake2.requests == [["CPUExecutionProvider"]]


def test_ping_reports_ort_providers(srv, monkeypatch):
    """/api/ping capabilities.ort_providers: the ACTIVE EP chain (badge
    data source); empty without the DL extra."""
    monkeypatch.setattr(server_mod, "DL_AVAILABLE", True)
    monkeypatch.setattr(dlsep, "_ACTIVE_PROVIDERS",
                        ["CUDAExecutionProvider", "CPUExecutionProvider"])
    _, ping = get(f"{srv['base']}/api/ping")
    assert ping["capabilities"]["ort_providers"] == \
        ["CUDAExecutionProvider", "CPUExecutionProvider"]
    monkeypatch.setattr(server_mod, "DL_AVAILABLE", False)
    _, ping = get(f"{srv['base']}/api/ping")
    assert ping["capabilities"]["ort_providers"] == []


def test_gpu_extras_declared():
    """[dl-cuda] / [dl-directml] extras exist (plain [dl] stays
    CPU-only onnxruntime)."""
    txt = (Path(__file__).resolve().parent.parent / "pyproject.toml") \
        .read_text(encoding="utf-8")
    assert "dl-cuda" in txt and "onnxruntime-gpu" in txt
    assert "dl-directml" in txt and "onnxruntime-directml" in txt


def test_provider_badge_contract():
    """Frontend source contract: the lanes panel renders a GPU/CPU
    provider badge from capabilities.ort_providers with a full-chain
    tooltip, and both i18n dicts carry the keys."""
    lanes = strip_js_comments(
        (FRONTEND / "lanes.js").read_text(encoding="utf-8"))
    assert "ort_providers" in lanes
    assert "providerBadge" in lanes
    assert "CUDAExecutionProvider" in lanes
    assert "DmlExecutionProvider" in lanes
    i18n = strip_js_comments(
        (FRONTEND / "i18n.js").read_text(encoding="utf-8"))
    for key in ("providerGpu", "providerCpu", "providerTip"):
        assert i18n.count(f"{key}:") >= 2


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

    def __init__(self, variant, download_progress=None):
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


def test_demucs_method_alias(srv, monkeypatch):
    """``method=demucs`` is the documented alias of the canonical
    ``demucs_4`` and answers exactly the fixed 4-stem registry on the
    task result, the cache GET and the per-stem download."""
    monkeypatch.setattr(server_mod, "DL_AVAILABLE", True)
    monkeypatch.setattr(server_mod, "POLY_AVAILABLE", True)
    monkeypatch.setattr(dlsep, "get_separator", FakeSeparator)

    code, body = post(f"{srv['base']}/api/stems?method=demucs")
    assert code == 200 and body["status"] == "started"
    done = wait_task(srv["base"], body["task_id"])
    assert done["status"] == "done"
    assert done["method"] == "demucs_4"
    assert [s["key"] for s in done["stems"]] == \
        list(dlsep.STEM_SPECS["demucs_4"])

    code, got = get(f"{srv['base']}/api/stems?method=demucs")
    assert code == 200
    assert got["method"] == "demucs_4" and got["cached"] is True
    assert [s["key"] for s in got["stems"]] == \
        ["drums", "bass", "other", "vocals"]

    url = got["stems"][0]["url"].split(srv["base"])[-1]
    with urllib.request.urlopen(f"{srv['base']}{url}", timeout=10) as r:
        assert r.status == 200 and r.read(4) == b"RIFF"


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


def test_shared_pitch_mapping_contract():
    """3.9.1 D: pitch->pixel is ONE shared definition (geometry.js:
    keyRangeUnits / rowCenterUnit / unitToPlotFraction). The master
    yaxis is built from keyRangeUnits + rowCenterUnit, the overlay
    canvases map their bands through unitToPlotFraction — no
    per-overlay re-implementation. The <=1 px overlay-vs-master row
    alignment runs as a Node test (test:geometry) wired into npm + CI."""
    geo = strip_js_comments(
        (FRONTEND / "geometry.js").read_text(encoding="utf-8"))
    for fn in ("keyRangeUnits", "rowCenterUnit", "unitToPlotFraction"):
        assert f"export function {fn}" in geo
    spec_src = strip_js_comments(
        (FRONTEND / "spectrogram.js").read_text(encoding="utf-8"))
    assert "keyRangeUnits(" in spec_src and "rowCenterUnit(" in spec_src
    layers = strip_js_comments(
        (FRONTEND / "layers.js").read_text(encoding="utf-8"))
    assert "unitToPlotFraction(" in layers
    assert "m * sub + (sub - 1) / 2" not in layers  # no re-implementation
    script = FRONTEND.parent / "scripts" / "test-geometry.mjs"
    assert script.is_file()
    assert "unitToPlotFraction" in script.read_text(encoding="utf-8")
    pkg = json.loads((FRONTEND.parent / "package.json").read_text(
        encoding="utf-8"))
    assert pkg["scripts"]["test:geometry"] == \
        "node scripts/test-geometry.mjs"
    assert "npm run test:geometry" in pkg["scripts"]["test"]
    repo = Path(__file__).resolve().parent.parent
    ci = (repo / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8")
    assert "npm run test:geometry" in ci


def test_layer_intensity_direction_contract():
    """3.9.1 gamma-direction fix: the layer intensity transform lives in
    ONE pure function (stemspec.js intensityValue) applying the MASTER's
    highlight-γ direction (v^(1/γ) — bigger γ = brighter, because the
    master warps colorscale anchors by p^γ). The direction is locked by a
    Node test (test:intensity) wired into npm + CI."""
    stemspec = strip_js_comments(
        (FRONTEND / "stemspec.js").read_text(encoding="utf-8"))
    assert "export function intensityValue" in stemspec
    assert "Math.pow(t, 1 / Math.max(0.01, gamma))" in stemspec
    # the naive v^γ (direction-inverted vs the master) must not survive
    assert "Math.pow(t, g)" not in stemspec
    script = FRONTEND.parent / "scripts" / "test-intensity.mjs"
    assert script.is_file()
    src = script.read_text(encoding="utf-8")
    assert "bigger γ" in src and "intensityValue" in src
    pkg = json.loads((FRONTEND.parent / "package.json").read_text(
        encoding="utf-8"))
    assert pkg["scripts"]["test:intensity"] == \
        "node scripts/test-intensity.mjs"
    assert "npm run test:intensity" in pkg["scripts"]["test"]
    repo = Path(__file__).resolve().parent.parent
    ci = (repo / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8")
    assert "npm run test:intensity" in ci


def test_stem_spec_shared_basis_contract():
    """3.9.1 C frontend side: the stem-spec client asserts the master's
    dB basis (getStemSpec expect.dbRange), and both consumers — the lane
    [Wave|Spec] view and the overlay layers — pass data.dbRange so an
    overlaid stem cannot render on a foreign scale."""
    stemspec = strip_js_comments(
        (FRONTEND / "stemspec.js").read_text(encoding="utf-8"))
    assert "expect.dbRange" in stemspec
    assert "stem spec: dbRange" in stemspec  # mismatch throws
    for name in ("lanes.js", "layers.js"):
        src = strip_js_comments(
            (FRONTEND / name).read_text(encoding="utf-8"))
        assert "dbRange: data.dbRange" in src


def test_frontend_mixer_contract():
    """stems.js and lanes.js must share the MixerState (mixer.js): one
    master GainNode into a brickwall limiter before the destination,
    strips default MUTED (the mix keeps playing), the destructive-solo
    state machine (pressMute/pressSolo) driven from controls.js with
    buttons rendering strictly from state, make-up gain staging, the
    matrix shown via .dimmed rows, and the fixed per-method stem
    contract validated on both panels."""
    mixer = strip_js_comments(
        (FRONTEND / "mixer.js").read_text(encoding="utf-8"))
    assert "createGain" in mixer and "connect(ctx.destination)" in mixer
    assert "MASTER_CEILING" in mixer
    assert "muted: true" in mixer   # strip default (makeStrip)
    assert "muted: false" in mixer  # mix strip default
    assert "stripAudible" in mixer and "mixAudible" in mixer
    # Phase 3.7: destructive-solo transitions + gain staging + limiter
    assert "pressSolo" in mixer and "pressMute" in mixer
    assert "createDynamicsCompressor" in mixer
    assert "makeupDb" in mixer and "bufferPeak" in mixer
    controls = strip_js_comments(
        (FRONTEND / "controls.js").read_text(encoding="utf-8"))
    assert "mixer.pressMute(model)" in controls
    assert "mixer.pressSolo(model)" in controls
    for name in ("stems.js", "lanes.js"):
        src = strip_js_comments(
            (FRONTEND / name).read_text(encoding="utf-8"))
        assert "MixerState" in src and "mixer.js" in src
        assert "dimmed" in src          # matrix feedback on the rows
        assert "makeStrip" in src       # strips born muted
        assert "mixer.route" in src     # master-bus wiring after load
        assert "mixer.apply" in src     # one matrix application point
        assert "makeupDb" in src        # fader default = make-up gain
    # both panels enforce the fixed stem lists client-side too
    stems_src = strip_js_comments(
        (FRONTEND / "stems.js").read_text(encoding="utf-8"))
    assert "EXPECTED_STEMS" in stems_src
    lanes_src = strip_js_comments(
        (FRONTEND / "lanes.js").read_text(encoding="utf-8"))
    assert "'drums', 'bass', 'other', 'vocals'" in lanes_src


def test_mixer_transition_table_is_wired():
    """The FULL transition-table test (T1–T5, I1, snapshot restore,
    mix-duck rule) runs as a Node script wired into npm scripts and
    CI — mixer.js imports nothing, so it runs on pure Node fakes."""
    script = FRONTEND.parent / "scripts" / "test-mixer.mjs"
    assert script.is_file()
    src = script.read_text(encoding="utf-8")
    for token in ("T1", "T2", "T3", "T4", "T5", "pressSolo",
                  "pressMute", "invariantOk", "mixAudible"):
        assert token in src
    pkg = json.loads((FRONTEND.parent / "package.json").read_text(
        encoding="utf-8"))
    assert pkg["scripts"]["test:mixer"] == "node scripts/test-mixer.mjs"
    repo = Path(__file__).resolve().parent.parent
    ci = (repo / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8")
    assert "npm run test:mixer" in ci


def test_frontend_ui_polish_contract():
    """Phase 3.6 UI polish contracts: shared row factory + track icons,
    cached peak envelopes drawn at load, per-lane decode placeholders,
    lazy precise DL-availability toasts (never auto-triggered), mixer
    tooltips, and the i18n key-completeness guard wired into npm
    scripts and CI."""
    assert (FRONTEND / "trackIcons.js").is_file()
    assert (FRONTEND / "controls.js").is_file()
    assert (FRONTEND / "toast.js").is_file()
    assert (FRONTEND.parent / "scripts" / "check-i18n.mjs").is_file()

    lanes = strip_js_comments(
        (FRONTEND / "lanes.js").read_text(encoding="utf-8"))
    stems = strip_js_comments(
        (FRONTEND / "stems.js").read_text(encoding="utf-8"))
    for src in (lanes, stems):
        assert "trackRow" in src and "controls.js" in src
        assert "suppressionTip" in src      # D6: dimmed rows get tooltips
    assert "buildEnvelope" in lanes         # D2: cached peak envelope
    assert "decodeFailed" in lanes          # D2: per-lane placeholder
    assert "drawAll()" in lanes             # D2: immediate first paint
    assert "showToast" in lanes             # D5: dismissible toast
    assert "polyNeedsDL" in lanes and "polyNeedsBP" in lanes
    assert "await state.capsReady" in lanes  # D5: availability is lazy
    # the Notes button is only ever added to poly-eligible lanes (D4)
    assert "POLY_LANES.has(lane.key)" in lanes

    pkg = json.loads((FRONTEND.parent / "package.json").read_text(
        encoding="utf-8"))
    assert pkg["scripts"]["check:i18n"] == "node scripts/check-i18n.mjs"
    repo = Path(__file__).resolve().parent.parent
    ci = (repo / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8")
    assert "npm run check:i18n" in ci


def test_frontend_first_contact_contract():
    """3.8 first-contact UX contracts: envelopes draw from the envelope's
    OWN bucket timing in every mute state (the D1 root-cause fix: the
    old code read lane.bucketSec -> undefined -> NaN indexes -> black
    lanes), the AI-Separation On button answers a click even when the
    [dl] extra is missing (toast + disabled + tooltip, never a silent
    no-op) and spins while working, and the task poll renders the
    two-phase download/inference progress."""
    lanes = strip_js_comments(
        (FRONTEND / "lanes.js").read_text(encoding="utf-8"))
    # D1: the column index reads the envelope's own bucketSec (the lane
    # object never carries it) and muting dims instead of hiding
    assert "p.bucketSec" in lanes and "lane.bucketSec" not in lanes
    assert "stripAudible(lane) ? 1 : 0.35" in lanes
    assert "scheduleDraw()" in lanes  # repaint on mute/solo changes
    # D2: click-time capability gate -> toast + disabled + tooltip
    assert "await state.capsReady" in lanes
    assert "showToast(t('dlNeedsExtra'))" in lanes
    assert "classList.add('loading')" in lanes  # spinner, never dead
    # D3: two-phase progress UI in the poll loop
    assert "lanesDownloading" in lanes
    assert "'downloading'" in lanes
    assert "bytes_done" in lanes and "speed_mbps" in lanes
    # both language dicts carry the new keys (check-i18n enforces parity)
    i18n = strip_js_comments(
        (FRONTEND / "i18n.js").read_text(encoding="utf-8"))
    for key in ("dlNeedsExtra", "lanesDownloading"):
        assert i18n.count(f"{key}:") >= 2

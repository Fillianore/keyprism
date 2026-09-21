"""Frontend contract layer tests: the payload structure is the single source
of truth for data.json"""

import base64

import numpy as np
import pytest

from keyprism import audio_io
from helpers import make_m4a, make_wav
from keyprism.payload import analyze, compute_specs


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    pub = tmp_path / "pub"
    pub.mkdir()
    monkeypatch.setattr(audio_io, "PUBLIC_DIR", pub)
    return pub


def test_compute_specs_clamps_resolution(tmp_path):
    p = tmp_path / "t.wav"
    make_wav(p, seconds=2.0)
    data, sr, dur = audio_io.load_channels(p, 0.0, None)
    res = compute_specs(data, sr, dur, rate=99, sub=7, db_range=70.0,
                        window=2048)
    assert res["rate"] == 60  # clamped to the TIME_RATES ceiling
    assert res["sub"] == 1    # invalid sub falls back to 1


def test_compute_specs_quantization_shape(tmp_path):
    p = tmp_path / "t.wav"
    make_wav(p, seconds=2.0)
    data, sr, dur = audio_io.load_channels(p, 0.0, None)
    res = compute_specs(data, sr, dur, rate=5, sub=1, db_range=70.0,
                        window=2048)
    assert set(res["specs"]) == {"mix", "left", "right"}
    for ch, b64 in res["specs"].items():
        raw = base64.b64decode(b64)
        assert len(raw) == 88 * res["sub"] * res["nCols"]  # uint8 matrix
        assert max(raw) > 0  # not silent
    assert set(res["envelopes"]) == {"mix", "left", "right"}
    assert res["envelopes"]["mix"].startswith("data:image/png;base64,")


def test_compute_specs_silence_raises():
    data = np.zeros((22050, 2))
    with pytest.raises(ValueError, match="静音"):
        compute_specs(data, 22050, 1.0, 5, 1, 70.0, 2048)


def test_analyze_payload_contract(tmp_path):
    p = tmp_path / "t.wav"
    make_wav(p, seconds=2.0)
    d = analyze(p, 0.0, None, 2048, 70.0, 5, 1)
    # not a single field the frontend main.js depends on may be missing
    for key in ("file", "durationSec", "dbRange", "offsetSec", "endSec",
                "initViewSec", "noteLabels", "cTickIdx", "colorscales",
                "defaultCmap", "timeRates", "subOptions", "defaultRate",
                "defaultSub", "audioFile", "bpm", "beatOffsetSec",
                "specs", "envelopes", "nCols"):
        assert key in d, f"payload missing field {key}"
    assert d["file"] == "t.wav"
    assert len(d["noteLabels"]) == 88
    assert d["noteLabels"][0] == "A0" and d["noteLabels"][-1] == "C8"
    assert d["durationSec"] == pytest.approx(2.0, abs=0.05)
    assert d["initViewSec"] == pytest.approx(2.0, abs=0.05)  # shorter than the default viewport
    assert len(d["colorscales"]) == 6
    cs = d["colorscales"]["Inferno"]
    assert cs[0][1] == "#000000"  # lower anchor pure black
    assert 30 <= d["bpm"] <= 300
    assert d["timeRates"] == [15, 30, 60] and d["subOptions"] == [1, 5, 10]
    assert d["apiBase"] is None


def test_analyze_preloaded_and_display_name(tmp_path):
    p = tmp_path / "t.m4a"
    make_m4a(p, seconds=1.5)
    data, sr, dur = audio_io.load_channels(p, 0.0, None)
    d = analyze(p, 0.0, None, 2048, 70.0, 5, 1, api_base="http://x:1",
                preloaded=(data, sr, dur), name="我的歌.m4a")
    assert d["file"] == "我的歌.m4a"  # display name decoupled from disk path
    assert d["apiBase"] == "http://x:1"
    assert d["audioFile"] == "audio.wav"  # m4a transcoded


def test_analyze_m4a_end_to_end(tmp_path, isolated_dirs):
    p = tmp_path / "t.m4a"
    make_m4a(p, seconds=1.5)
    d = analyze(p, 0.0, None, 2048, 70.0, 5, 1)
    assert d["durationSec"] == pytest.approx(1.5, abs=0.2)
    assert (isolated_dirs / "audio.wav").exists()

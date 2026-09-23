"""Frontend contract layer tests: the payload structure is the single source
of truth for data.json"""

import base64

import numpy as np
import pytest

from keyprism import audio_io
from helpers import make_m4a, make_wav
from keyprism.payload import (
    analyze,
    compute_specs,
    joint_spec_peak,
    stem_spec_payload,
)


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


# ------------------------------------------------- stem spec basis (3.9.1)

def _master_basis(tmp_path, gain=1.0):
    """(data2d, sr, master peak) for a stereo track whose channels are
    identical, so mix == left == right and the joint peak is the mix
    channel's semitone-matrix max."""
    p = tmp_path / "t.wav"
    make_wav(p, seconds=2.0)
    data, sr, dur = audio_io.load_channels(p, 0.0, None)
    data = data * gain
    return data, sr, joint_spec_peak(data, sr, 2048, 60, 1)


def test_stem_spec_payload_global_basis_not_inflated(tmp_path):
    """3.9.1 C: a stem at HALF the master's amplitude must quantize to
    -6.02 dB against the master joint peak — NOT to full scale (which is
    what the pre-3.9.1 own-peak normalization produced). gain=0/screen
    overlay brightness therefore matches the master's view of the stem."""
    data, sr, peak = _master_basis(tmp_path)
    stem = data.mean(axis=1) * 0.5  # exact -6.02 dB (power) below the peak
    spec = stem_spec_payload(stem, sr, 2048, 70.0, peak_ref=peak)
    assert spec["basis"] == "mix_joint_peak"
    assert spec["peak_ref"] == pytest.approx(peak, rel=1e-5)
    raw = np.frombuffer(base64.b64decode(spec["spec"]), dtype=np.uint8)
    loudest = raw.max() / 255.0  # normalized dB over [0, 1]
    # q = 255*(10*log10(0.25) + 70)/70 = 255*(70-6.0206)/70 ~ 233.1/255
    expect = (10.0 * np.log10(0.25) + 70.0) / 70.0
    assert loudest == pytest.approx(expect, abs=1.5 / 255)
    assert loudest < 0.97  # clearly NOT the own-peak full scale


def test_stem_spec_payload_own_peak_reference_full_scale(tmp_path):
    """The contrast case: with peak_ref == the stem's OWN joint peak the
    loudest cell quantizes to full scale — pinning the two bases apart so
    the 3.9.1 change is a tested behavior, not an accident."""
    data, sr, _ = _master_basis(tmp_path)
    stem = data.mean(axis=1)
    own = joint_spec_peak(np.stack([stem, stem], axis=1), sr, 2048, 60, 1)
    spec = stem_spec_payload(stem, sr, 2048, 70.0, peak_ref=own)
    raw = np.frombuffer(base64.b64decode(spec["spec"]), dtype=np.uint8)
    assert raw.max() >= 254  # own peak == full scale


def test_stem_spec_payload_rejects_bad_reference(tmp_path):
    data, sr, _ = _master_basis(tmp_path)
    stem = data.mean(axis=1)
    with pytest.raises(ValueError, match="peak_ref"):
        stem_spec_payload(stem, sr, 2048, 70.0, peak_ref=0.0)
    with pytest.raises(ValueError, match="peak_ref"):
        stem_spec_payload(stem, sr, 2048, 70.0, peak_ref=None)


def test_joint_spec_peak_matches_compute_specs(tmp_path):
    """The shared master basis is EXACTLY the peak data.json is quantized
    against (single source of truth for the dB scale): it equals the max
    over the three channels' semitone matrices, and the loudest quantized
    mix cell sits at 10*log10(mix_max / peak) dB."""
    from keyprism.dsp import spec_matrix

    p = tmp_path / "t.wav"
    make_wav(p, seconds=2.0)
    data, sr, dur = audio_io.load_channels(p, 0.0, None)
    peak = joint_spec_peak(data, sr, 2048, 60, 1)
    matrices = [
        spec_matrix(sig, sr, 2048, 60, 1)[0]
        for sig in (data.mean(axis=1), data[:, 0], data[:, -1])
    ]
    assert peak == pytest.approx(max(m.max() for m in matrices), rel=1e-9)
    assert peak > 0
    # the data.json quantization is dB-normalized by the SAME peak
    res = compute_specs(data, sr, dur, 15, 1, 70.0, 2048)
    raw = np.frombuffer(base64.b64decode(res["specs"]["mix"]),
                        dtype=np.uint8)
    mix_db = raw.max() / 255.0 * 70.0 - 70.0
    assert mix_db == pytest.approx(
        10.0 * np.log10(matrices[0].max() / peak), abs=0.05)

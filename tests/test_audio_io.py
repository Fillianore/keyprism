"""Audio IO layer tests: decode-chain fallback and browser-safe transcoding"""

import numpy as np
import pytest

from keyprism import audio_io
from helpers import make_m4a, make_wav


@pytest.fixture(autouse=True)
def isolated_public_dir(tmp_path, monkeypatch):
    """Point browser_safe_audio's output directory at a temp dir so
    frontend/public stays untouched"""
    pub = tmp_path / "pub"
    pub.mkdir()
    monkeypatch.setattr(audio_io, "PUBLIC_DIR", pub)
    return pub


def test_load_channels_wav(tmp_path):
    p = tmp_path / "t.wav"
    make_wav(p, seconds=2.0)
    data, sr, dur = audio_io.load_channels(p, 0.0, None)
    assert sr == 22050
    assert data.shape[1] == 2  # multichannel preserved
    assert dur == pytest.approx(2.0, abs=0.05)


def test_load_channels_slice(tmp_path):
    p = tmp_path / "t.wav"
    make_wav(p, seconds=4.0)
    data, sr, dur = audio_io.load_channels(p, 1.0, 3.0)
    assert dur == pytest.approx(2.0, abs=0.05)
    # matches the corresponding slice of a full read (1s = 22050 frames,
    # 2s = 44100 frames)
    full, _, _ = audio_io.load_channels(p, 0.0, None)
    np.testing.assert_allclose(data, full[22050:66150], atol=1e-9)


def test_load_channels_start_beyond_end(tmp_path):
    p = tmp_path / "t.wav"
    make_wav(p, seconds=1.0)
    with pytest.raises(ValueError, match="超出音频长度"):
        audio_io.load_channels(p, 5.0, None)


def test_load_channels_av_fallback_m4a(tmp_path):
    """libsndfile cannot handle m4a; must fall back to a full PyAV decode"""
    p = tmp_path / "t.m4a"
    make_m4a(p, seconds=1.5)
    data, sr, dur = audio_io.load_channels(p, 0.0, None)
    assert sr == 22050
    assert data.shape[1] == 2
    assert dur == pytest.approx(1.5, abs=0.2)


def test_load_channels_garbage(tmp_path):
    p = tmp_path / "bad.mp3"
    p.write_bytes(b"\x00" * 2048)
    with pytest.raises(Exception):
        audio_io.load_channels(p, 0.0, None)


def test_browser_safe_audio_direct_copy(tmp_path, isolated_public_dir):
    p = tmp_path / "t.wav"
    make_wav(p)
    name = audio_io.browser_safe_audio(p)
    assert name == "audio.wav"
    assert (isolated_public_dir / "audio.wav").exists()


def test_browser_safe_audio_transcode(tmp_path, isolated_public_dir):
    """m4a is transcoded to PCM WAV so the browser's decodeAudioData can
    decode it"""
    p = tmp_path / "t.m4a"
    make_m4a(p)
    name = audio_io.browser_safe_audio(p)
    assert name == "audio.wav"
    import soundfile as sf
    info = sf.info(str(isolated_public_dir / "audio.wav"))
    assert info.frames > 0

"""Analysis cache tests: hash stability & param sensitivity, chunked
memmap writes, LRU eviction, and the staged-analysis cache hits.

The cache root is read from ``audio_io.KEYPRISM_HOME`` at call time, so
pointing that attribute at a temp dir relocates the whole workspace.
"""

import json
import os

import numpy as np
import pytest

from keyprism import analyze, audio_io
from keyprism.transform import iter_stft_chunks, stft_complex
from helpers import SR, make_wav, tone_stereo


@pytest.fixture(autouse=True)
def cache_home(tmp_path, monkeypatch):
    """Relocate the whole workspace so tests never touch ~/.keyprism."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(audio_io, "KEYPRISM_HOME", home)
    return home


def tiny_params(**over):
    params = dict(sr=SR, win=2048, hop=512, window="hann", center=True,
                  rate=15, sub=1, db_range=70.0, start=0.0, end=None)
    params.update(over)
    return params


# ------------------------------------------------------- hashes / keys

def test_pcm16_fingerprint_stable_and_sensitive():
    data = tone_stereo(0.5)
    a = analyze.pcm16_fingerprint(data)
    assert a == analyze.pcm16_fingerprint(data.copy())
    assert len(a) == 16
    changed = data.copy()
    changed[1000, 0] += 0.01
    assert a != analyze.pcm16_fingerprint(changed)


def test_params_fingerprint_canonical_and_sensitive():
    base = tiny_params()
    h = analyze.params_fingerprint(base)
    assert h == analyze.params_fingerprint(dict(reversed(list(base.items()))))
    assert len(h) == 16
    for field, value in [
        ("sr", 44100), ("win", 4096), ("hop", 1024), ("window", "hamming"),
        ("center", False), ("rate", 30), ("sub", 5), ("db_range", 60.0),
        ("start", 1.0), ("end", 2.0),
    ]:
        assert h != analyze.params_fingerprint(tiny_params(**{field: value}))


def test_entry_dir_layout():
    d = analyze.entry_dir("abcd", "0123")
    assert d == audio_io.KEYPRISM_HOME / "cache" / "analysis" / "abcd" / "0123"


# ------------------------------------------------- chunked memmap store

def test_chunked_memmap_write_matches_in_memory_stft():
    x = tone_stereo(1.2)[:, 0]  # not a multiple of the chunk size
    n_frames = analyze.n_frames_of(len(x), 2048, 512, True)
    entry = analyze.entry_dir("pc", "pa")
    chunks = iter_stft_chunks(x, win=2048, hop=512)
    analyze.store_stft(entry, chunks, n_frames, 2048 // 2 + 1)
    analyze.store_meta(entry, {"sr": SR, "win": 2048, "hop": 512,
                               "n_frames": n_frames, "n_bins": 1025,
                               "duration": 1.2, "params": tiny_params()})
    mm, meta = analyze.load_entry(entry)
    assert meta["n_frames"] == n_frames and meta["n_bins"] == 1025
    ref = stft_complex(x, win=2048)
    assert np.array_equal(np.asarray(mm), ref)  # byte-equal complex64 rows


def test_store_flushes_per_chunk_and_is_atomic():
    x = tone_stereo(0.8)[:, 0]
    entry = analyze.entry_dir("pc2", "pa2")
    n_frames = analyze.n_frames_of(len(x), 2048, 512, True)
    flushed = []
    def counting_iter():
        for c0, chunk in iter_stft_chunks(x, win=2048, hop=512):
            flushed.append(c0)
            yield c0, chunk
    analyze.store_stft(entry, counting_iter(), n_frames, 1025)
    analyze.store_meta(entry, {"sr": SR, "win": 2048, "hop": 512,
                               "n_frames": n_frames, "n_bins": 1025,
                               "duration": 0.8, "params": tiny_params()})
    assert flushed and flushed[0] == 0
    assert not list(entry.glob("*.tmp"))
    assert (entry / "stft.npy").is_file()
    meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
    assert meta["created_at"]


def test_load_entry_rejects_incomplete():
    entry = analyze.entry_dir("pc3", "pa3")
    assert analyze.load_entry(entry) is None
    entry.mkdir(parents=True)
    (entry / "stft.npy").write_bytes(b"garbage")
    assert analyze.load_entry(entry) is None


# ----------------------------------------------------------- iter_chunks

def test_iter_chunks_yields_row_slices_covering_all_frames():
    x = tone_stereo(25.0)[:, 0]  # long enough for several 10 s slices
    entry = analyze.entry_dir("pc4", "pa4")
    n_frames = analyze.n_frames_of(len(x), 2048, 512, True)
    analyze.store_stft(entry, iter_stft_chunks(x, win=2048, hop=512),
                       n_frames, 1025)
    analyze.store_meta(entry, {"sr": SR, "win": 2048, "hop": 512,
                               "n_frames": n_frames, "n_bins": 1025,
                               "duration": 25.0, "params": tiny_params()})
    slices = list(analyze.iter_chunks(entry / "stft.npy"))
    # 10 s at 22050 Hz with hop 512 -> ceil(10*SR/512) frames per slice
    rows = -(-10 * SR // 512)
    assert all(s.shape[1] == 1025 for s in slices)
    assert sum(s.shape[0] for s in slices) == n_frames
    assert slices[0].shape[0] == rows
    full = np.load(entry / "stft.npy", mmap_mode="r")
    assert np.array_equal(np.concatenate(list(slices), axis=0),
                          np.asarray(full))


# ----------------------------------------------------------------- LRU

def _store_entries(count, tag="t"):
    for i in range(count):
        entry = analyze.entry_dir(f"pcm{i}", f"par{i}{tag}")
        analyze.store_stft(entry, iter(()), 1, 1025)
        analyze.store_meta(entry, {"n_frames": 1, "n_bins": 1025})
    return [analyze.entry_dir(f"pcm{i}", f"par{i}{tag}") for i in range(count)]


def test_lru_eviction_default_cap():
    entries = _store_entries(analyze.CACHE_MAX_ENTRIES_DEFAULT + 3)
    removed = analyze.evict_old()
    assert len(removed) == 3
    surviving = [d for d in entries if d.is_dir()]
    assert len(surviving) == analyze.CACHE_MAX_ENTRIES_DEFAULT
    assert all(not d.parent.is_dir() or any(d.parent.iterdir())
               for d in entries if not d.is_dir())  # no empty pcm dirs kept


def test_lru_eviction_env_override_and_precedence():
    _store_entries(5)
    # env beats the default
    os.environ["KEYPRISM_CACHE_MAX_ENTRIES"] = "2"
    try:
        analyze.evict_old()
        assert len(list(analyze.cache_root().glob("*/*"))) == 2
    finally:
        del os.environ["KEYPRISM_CACHE_MAX_ENTRIES"]
    # invalid value falls back to the default cap (all 5 entries: no eviction)
    os.environ["KEYPRISM_CACHE_MAX_ENTRIES"] = "bogus"
    try:
        assert analyze.max_entries() == analyze.CACHE_MAX_ENTRIES_DEFAULT
    finally:
        del os.environ["KEYPRISM_CACHE_MAX_ENTRIES"]


def test_evict_keeps_most_recent_entries():
    import time
    entries = _store_entries(3)
    time.sleep(0.02)
    newest = analyze.entry_dir("pcm_new", "par_new")
    analyze.store_stft(newest, iter(()), 1, 1025)
    analyze.store_meta(newest, {"n_frames": 1})
    os.environ["KEYPRISM_CACHE_MAX_ENTRIES"] = "1"
    try:
        analyze.evict_old()
    finally:
        del os.environ["KEYPRISM_CACHE_MAX_ENTRIES"]
    assert newest.is_dir()
    assert not any(d.is_dir() for d in entries)


# ----------------------------------------------------- staged analysis

@pytest.fixture()
def isolated_pub(tmp_path, monkeypatch):
    pub = tmp_path / "pub"
    pub.mkdir()
    monkeypatch.setattr(audio_io, "PUBLIC_DIR", pub)
    return pub


@pytest.fixture()
def wav_file(tmp_path):
    p = tmp_path / "t.wav"
    make_wav(p, seconds=2.0)
    return p


def test_run_analysis_payload_matches_legacy(wav_file, isolated_pub):
    from keyprism.payload import analyze as legacy_analyze
    payload, (data2d, sr, dur) = analyze.run_analysis(
        wav_file, window=2048, rate=5, sub=1)
    pre = audio_io.load_channels(wav_file, 0.0, None)
    legacy = legacy_analyze(wav_file, 0.0, None, 2048, 70.0, 5, 1,
                            preloaded=pre)
    assert payload == legacy  # includes bpm/beat_offset/quantized bytes


def test_second_run_skips_stft_compute(wav_file, isolated_pub, monkeypatch):
    counter = {"n": 0}
    real = analyze._compute_and_store

    def counting(*a, **k):
        counter["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(analyze, "_compute_and_store", counting)
    p1, _ = analyze.run_analysis(wav_file, window=2048, rate=5, sub=1)
    assert counter["n"] == 1
    stfts = list(analyze.cache_root().glob("*/*/stft.npy"))
    assert len(stfts) == 1
    mtime1 = stfts[0].stat().st_mtime_ns
    p2, _ = analyze.run_analysis(wav_file, window=2048, rate=5, sub=1)
    assert counter["n"] == 1  # stft stage served from cache
    assert stfts[0].stat().st_mtime_ns == mtime1  # stft.npy untouched
    assert p2 == p1  # cache-hit payload identical to the fresh one


def test_param_change_causes_recompute(wav_file, isolated_pub, monkeypatch):
    counter = {"n": 0}
    real = analyze._compute_and_store

    def counting(*a, **k):
        counter["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(analyze, "_compute_and_store", counting)
    analyze.run_analysis(wav_file, window=2048, rate=5, sub=1)
    analyze.run_analysis(wav_file, window=2048, rate=5, sub=1)
    assert counter["n"] == 1  # same params -> cache hit
    analyze.run_analysis(wav_file, window=2048, rate=30, sub=5)
    assert counter["n"] == 2  # different params -> separate key, recompute
    assert len(list(analyze.cache_root().glob("*/*/stft.npy"))) == 2


def test_console_progress_reproduces_legacy_lines(wav_file, isolated_pub,
                                                  capsys):
    analyze.run_analysis(wav_file, window=2048, rate=5, sub=1)
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("[1/3] 已加载 2.0s @ 22050 Hz, 2 声道")
    assert "[2/3] 估计 BPM " in out[1] and "首拍偏移" in out[1]
    assert out[2] == "[3/3] 分辨率 15 列/s x 1 子带/半音 -> 88 行 x 44 列 x 3 通道"

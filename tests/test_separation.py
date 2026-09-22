"""Phase 2 classic source separation suite: HPSS synthetic separation,
RPCA (ADMM) recovery + convergence, chunked-vs-full RPCA equivalence,
masked-ISTFT phase/sum round-trip, streaming stem synthesis against the
full-matrix reference, cache/determinism and the /api/stems + /api/stem
HTTP endpoints.

Fixtures are built locally (seeded) — no external dataset is required.
The production path is exercised through ``stems`` reading a real cached
``stft.npy`` memmap, so the streaming/chunk discipline is under test too.
"""

import io
import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest
import soundfile as sf

from helpers import SR
from keyprism import analyze, audio_io, stems, transcribe
from keyprism.hpss import hpss_masks
from keyprism.rpca import iter_rpca_blocks, plan_chunks, rpca_decompose, \
    rpca_full_track
from keyprism.server import make_server
from keyprism.transform import istft_complex, iter_stft_chunks, n_frames_of, \
    stft_complex

WIN = 2048
HOP = WIN // 4


# ------------------------------------------------------------ fixtures

@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Relocate the whole workspace so tests never touch ~/.keyprism."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(audio_io, "KEYPRISM_HOME", h)
    return h


def make_entry(x: np.ndarray, sr: int = SR, win: int = WIN):
    """Store the STFT of a mono signal as a real cache entry (the same
    artifact the production pipeline reads back through a memmap)."""
    hop = win // 4
    entry = analyze.entry_dir("pcm_sep", "par_sep")
    n_frames = n_frames_of(x.shape[0], win, hop, True)
    analyze.store_stft(entry, iter_stft_chunks(x, win=win, hop=hop),
                       n_frames, win // 2 + 1)
    analyze.store_meta(entry, {
        "sr": sr, "win": win, "hop": hop, "n_frames": n_frames,
        "n_bins": win // 2 + 1, "duration": round(x.shape[0] / sr, 6),
        "bpm": 120.0, "beat_offset_sec": 0.0,
    })
    return entry


def tone_plus_clicks(seconds: float = 2.0, sr: int = SR) -> np.ndarray:
    """Slow sine sweep (harmonic line) + periodic dirac clicks (broadband
    transients), all within [-1, 1]."""
    t = np.arange(int(sr * seconds)) / sr
    x = 0.4 * np.sin(2 * np.pi * (400 + 150 * t) * t)
    for tc in np.arange(0.2, seconds - 0.05, 0.3):
        x[int(tc * sr)] += 0.9
    return x


# ---------------------------------------------------------------- HPSS

def test_hpss_separates_tone_from_clicks():
    sr, win, hop = SR, 1024, 256
    x = tone_plus_clicks(2.0, sr)
    mag = np.abs(stft_complex(x, win=win, hop=hop).astype(np.complex128))
    mh, mp = hpss_masks(mag)
    assert mh.shape == mag.shape and mp.shape == mag.shape
    assert np.all(mh >= 0.0) and np.all(mh <= 1.0)
    assert np.all(mp >= 0.0) and np.all(mp <= 1.0)
    # Wiener masks sum to ~1 in every non-silent cell
    assert np.all(mh + mp <= 1.0 + 1e-9)

    fps = sr / hop
    band_lo = int(400 * win / sr) - 3
    band_hi = int(700 * win / sr) + 3
    out = np.arange(int(1000 * win / sr), mag.shape[1])  # above sweep skirts
    # harmonic: sweep-band energy assigned to H dominates P
    seg = slice(10, mag.shape[0] - 10)
    band = slice(band_lo, band_hi)
    eh = (mh[seg, band] * mag[seg, band]).sum()
    ep = (mp[seg, band] * mag[seg, band]).sum()
    assert eh > 5.0 * ep
    # percussive: click frames are broadband — nearly every bin's mass is
    # assigned to P; above the sweep's spectral skirts the click energy
    # goes to P by an overwhelming margin
    for tc in (0.5, 1.1, 1.7):
        f = int(tc * fps)
        assert mp[f].mean() > 0.9
        e_h_out = (mh[f][out] * mag[f][out]).sum()
        e_p_out = (mp[f][out] * mag[f][out]).sum()
        assert e_p_out > 100.0 * e_h_out


def test_hpss_rejects_even_kernels():
    mag = np.ones((32, 16))
    with pytest.raises(ValueError):
        hpss_masks(mag, win_harm=(1, 30))
    with pytest.raises(ValueError):
        hpss_masks(mag, win_perc=(30, 1))


# ---------------------------------------------------------------- RPCA

def _synth_lowrank_sparse(seed, m, n, rank, dens, scale=1.0):
    rng = np.random.default_rng(seed)
    L0 = rng.standard_normal((m, rank)) @ rng.standard_normal((rank, n))
    S0 = np.zeros((m, n))
    mk = rng.random((m, n)) < dens
    S0[mk] = rng.standard_normal(mk.sum())
    return (L0 + S0) * scale, L0 * scale, S0 * scale


def test_rpca_recovers_lowrank_and_sparse():
    X, L0, S0 = _synth_lowrank_sparse(42, 120, 100, 3, 0.03)
    L, S = rpca_decompose(X, max_iter=100, tol=1e-10)
    assert np.linalg.norm(L - L0) / np.linalg.norm(L0) < 1e-3
    assert np.linalg.norm(S - S0) / np.linalg.norm(S0) < 1e-3
    assert np.linalg.norm(X - L - S) / np.linalg.norm(X) < 1e-8


def test_rpca_scale_invariant():
    """The solver normalizes internally: STFT-scale (1e-4) data decomposes
    as well as O(1) data, with identical relative accuracy."""
    X1, L1, _ = _synth_lowrank_sparse(7, 120, 100, 3, 0.03)
    X2, L2, _ = _synth_lowrank_sparse(7, 120, 100, 3, 0.03, scale=1e-4)
    e1 = np.linalg.norm(rpca_decompose(X1)[0] - L1) / np.linalg.norm(L1)
    e2 = np.linalg.norm(rpca_decompose(X2)[0] - L2) / np.linalg.norm(L2)
    assert e1 < 1e-3 and e2 < 1e-3
    assert e1 == pytest.approx(e2, rel=0.5)


def test_rpca_admm_convergence_monotone_enough():
    """ADMM convergence stats: relative residual and split error shrink
    with the iteration budget (documented behaviour, see report)."""
    X, L0, _ = _synth_lowrank_sparse(3, 120, 100, 3, 0.03)
    norm_L0 = np.linalg.norm(L0)
    errs = []
    for iters in (2, 5, 10, 20, 40):
        L, _ = rpca_decompose(X, max_iter=iters, tol=0.0)
        errs.append(np.linalg.norm(L - L0) / norm_L0)
    assert errs[-1] < 1e-3 <= errs[0]
    assert errs == sorted(errs) or errs[-1] < errs[2]  # net convergence


def test_rpca_chunked_matches_full():
    """Overlap-add chunked RPCA == whole-matrix RPCA on a repeating
    low-rank pattern + sparse outliers (within numerical tolerance)."""
    rng = np.random.default_rng(5)
    base = rng.standard_normal((4, 96))
    mag = np.tile(base, (30, 1))  # 120 x 96 rank-4 repeating pattern
    mag += (rng.random(mag.shape) < 0.02) * rng.standard_normal(
        mag.shape) * 2.0
    kw = dict(max_iter=80, tol=1e-10)
    Lw, Sw = rpca_decompose(mag, **kw)
    Lf, Sf = rpca_full_track(mag, chunk_frames=50, overlap_frames=10, **kw)
    assert np.linalg.norm(Lf - Lw) / np.linalg.norm(Lw) < 5e-2
    assert np.linalg.norm(Sf - Sw) / np.linalg.norm(Sw) < 5e-2
    # what the stems pipeline actually consumes is the derived Wiener
    # mask — cross-fade localization differences mostly cancel there
    ml_w = Lw * Lw / (Lw * Lw + Sw * Sw + 1e-10)
    ml_f = Lf * Lf / (Lf * Lf + Sf * Sf + 1e-10)
    assert np.abs(ml_w - ml_f).mean() < 1e-3
    # and the streaming generator tiles the track exactly once
    cover = np.zeros(mag.shape[0])
    for a, b, Lb, Sb in iter_rpca_blocks(mag, 50, 10, **kw):
        cover[a:b] += 1
        assert Lb.shape == (b - a, mag.shape[1])
    assert np.all(cover == 1)


def test_rpca_plan_and_validation():
    plan = plan_chunks(120, 50, 10)
    assert plan[0][0] == 0 and plan[-1][1] == 120
    assert all(a < b for a, b in plan)
    with pytest.raises(ValueError):
        plan_chunks(100, 10, 10)  # overlap >= chunk
    zeros = np.zeros((8, 6))
    L, S = rpca_decompose(zeros)
    assert L.shape == (8, 6) and not L.any() and not S.any()
    with pytest.raises(ValueError):
        rpca_decompose(np.ones(4))


# ---------------------------------------------------- ISTFT round-trip

def test_masked_istft_roundtrip_phase_and_sum():
    """Mask * complex STFT -> ISTFT -> STFT: phase is preserved and the
    complementary masks reassemble the original signal."""
    sr, win, hop = SR, 1024, 256
    x = tone_plus_clicks(1.5, sr)
    N = x.shape[0]
    Z = stft_complex(x, win=win, hop=hop).astype(np.complex128)
    mh, mp = hpss_masks(np.abs(Z))
    y_h = istft_complex(mh * Z, win=win, hop=hop, center=True, length=N)
    y_p = istft_complex(mp * Z, win=win, hop=hop, center=True, length=N)
    # masks sum to ~1, and ISTFT is linear: stems reassemble the mix
    # (the deficit is the eps floor active only in near-silent cells)
    interior = slice(win // 2, N - win // 2)
    np.testing.assert_allclose(
        y_h[interior] + y_p[interior], x[interior], atol=5e-4)
    # phase consistency: re-analysis of the harmonic stem keeps the
    # original phase where the mask passes significant energy
    Z2 = stft_complex(y_h, win=win, hop=hop).astype(np.complex128)
    lo, hi = Z.shape[0] // 4, 3 * Z.shape[0] // 4
    m2 = np.abs(mh[lo:hi] * Z[lo:hi]) > 1e-2 * np.abs(Z[lo:hi]).max()
    phase_err = np.abs(np.angle(Z2[lo:hi][m2] * np.conj(Z[lo:hi][m2])))
    assert np.quantile(phase_err, 0.99) < 0.05
    # magnitude ratio approximates the mask in the interior
    ratio = np.abs(Z2[lo:hi][m2]) / np.abs(Z[lo:hi][m2])
    assert np.abs(ratio - mh[lo:hi][m2]).mean() < 0.05


# ------------------------------------- streaming stems vs full matrix

def test_stems_streaming_matches_full_matrix_reference():
    """The streamed (chunked) pipeline equals full-matrix masks + full
    ISTFT: HPSS masks bit-equal, PCM16 output within quantization."""
    sr = SR
    x = tone_plus_clicks(4.0, sr)  # > 2 HPSS blocks, > 1 RPCA chunk
    entry = make_entry(x, sr=sr, win=WIN)
    mm = np.load(entry / "stft.npy", mmap_mode="r")
    mag = np.abs(np.asarray(mm))
    m1_full, m2_full = hpss_masks(mag)
    Z = np.asarray(mm)
    N = x.shape[0]

    for a, b, m1b, m2b, _last in stems._iter_work_blocks(mm, "hpss", {}):
        assert np.array_equal(m1b, m1_full[a:b])
        assert np.array_equal(m2b, m2_full[a:b])

    status = stems.compute_stems(entry, "hpss")
    assert status["cached"] is False
    assert status["stems"] == ["harmonic", "percussive"]
    for key, ref in (("harmonic", m1_full), ("percussive", m2_full)):
        path = stems.stem_file_path(entry, "hpss", key)
        got, file_sr = sf.read(path, dtype="float64")
        assert file_sr == sr
        want = istft_complex(ref * Z, win=WIN, hop=HOP, center=True,
                             length=N)
        assert got.shape[0] == N
        # PCM16 quantization is the only permitted difference
        assert np.max(np.abs(got - want)) <= 1.0 / 32767 + 1e-6


def test_stems_all_methods_cache_and_determinism():
    x = tone_plus_clicks(3.0, SR)
    entry = make_entry(x, win=WIN)
    for method in stems.METHODS:
        st = stems.get_stems(entry, method, lock=threading.Lock())
        assert st["cached"] is False
        assert len(st["stems"]) == 2
        st2 = stems.get_stems(entry, method, lock=threading.Lock())
        assert st2["cached"] is True
        for key in st["stems"]:
            assert stems.stem_file_path(entry, method, key).is_file()
    # determinism: cold recompute reproduces byte-identical WAVs
    import hashlib
    raw = {
        (m, k): stems.stem_file_path(entry, m, k).read_bytes()
        for m in ("hpss",) for k in ("harmonic", "percussive")
    }
    import shutil
    shutil.rmtree(entry / "stems" / stems.STEMS_VERSION / "hpss")
    stems.compute_stems(entry, "hpss")
    for (m, k), data in raw.items():
        assert stems.stem_file_path(entry, m, k).read_bytes() == data


def test_stems_requires_entry_and_valid_method(tmp_path):
    with pytest.raises(ValueError):
        stems.compute_stems(tmp_path / "missing", "hpss")
    x = tone_plus_clicks(1.0, SR)
    entry = make_entry(x, win=WIN)
    with pytest.raises(ValueError):
        stems.compute_stems(entry, "nope")


# ---------------------------------------------------------------- HTTP

@pytest.fixture()
def srv(tmp_path, monkeypatch):
    """Server on a temp workspace + ephemeral port (window 2048 for
    speed), auto-shutdown at test end."""
    pub = tmp_path / "pub"
    pub.mkdir()
    monkeypatch.setattr(audio_io, "PUBLIC_DIR", pub)
    monkeypatch.setattr(audio_io, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(audio_io, "KEYPRISM_HOME",
                        tmp_path / "home")  # autouse fixture made it
    wav = tmp_path / "t.wav"
    sf.write(wav, tone_plus_clicks(1.5, SR), SR, subtype="PCM_16")
    server = make_server(wav, 0, "127.0.0.1", 0.0, None, WIN, 70.0, 5, 1)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    yield {
        "base": f"http://127.0.0.1:{server.server_address[1]}",
        "home": tmp_path / "home",
        "pub": pub,
        "wav": wav,
    }
    server.shutdown()
    server.server_close()
    th.join(timeout=5)


def get_raw(url):
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_api_stems_computes_and_caches(srv):
    base = srv["base"]
    code, raw, headers = get_raw(f"{base}/api/stems?method=combined")
    assert code == 200
    body = json.loads(raw.decode())
    assert body["method"] == "combined"
    assert body["cached"] is False
    assert [s["key"] for s in body["stems"]] == ["harmonic", "percussive"]
    assert all(s["url"].startswith("http://") for s in body["stems"])
    assert body["sample_rate"] == SR
    assert body["duration"] == pytest.approx(1.5, abs=0.01)

    # stems cached under the analysis entry, version-tagged
    cached = list(srv["home"].glob(
        f"cache/analysis/*/*/stems/{stems.STEMS_VERSION}/combined/*.wav"))
    assert len(cached) == 2

    code2, raw2, _ = get_raw(f"{base}/api/stems?method=combined")
    assert code2 == 200
    body2 = json.loads(raw2.decode())
    assert body2["cached"] is True
    assert body2["stems"] == body["stems"]


def test_api_stems_fixed_names_per_method(srv):
    """Contract: the stems list is the FIXED per-method registry, never
    dependent on file length or chunking — hpss/rpca answer exactly two
    canonical keys regardless of the input."""
    base = srv["base"]
    code, raw, _ = get_raw(f"{base}/api/stems?method=hpss")
    assert code == 200
    body = json.loads(raw.decode())
    assert body["method"] == "hpss"
    assert [s["key"] for s in body["stems"]] == ["harmonic", "percussive"]

    code, raw, _ = get_raw(f"{base}/api/stems?method=rpca")
    assert code == 200
    body = json.loads(raw.decode())
    assert body["method"] == "rpca"
    assert [s["key"] for s in body["stems"]] == ["lowrank", "sparse"]

    # the registry answers, not the cache: the versioned on-disk cache
    # holds exactly the fixed stem WAVs per method
    for method in ("hpss", "rpca"):
        cached = list(srv["home"].glob(
            f"cache/analysis/*/*/stems/{stems.STEMS_VERSION}/{method}/*.wav"))
        assert len(cached) == len(stems.STEM_SPECS[method])
        assert {p.stem for p in cached} == set(stems.STEM_SPECS[method])


def test_api_stem_download_is_valid_wav(srv):
    base = srv["base"]
    _, _, _ = get_raw(f"{base}/api/stems?method=hpss")  # ensure computed
    code, raw, headers = get_raw(
        f"{base}/api/stem?method=hpss&name=harmonic")
    assert code == 200
    assert headers["Content-Type"] == "audio/wav"
    assert "attachment" in headers.get("Content-Disposition", "")
    assert headers["Content-Disposition"].endswith('harmonic.wav"')
    got, file_sr = sf.read(file=io.BytesIO(raw), dtype="float64")
    assert file_sr == SR and got.shape[0] > SR  # > 1 s of valid PCM
    assert np.max(np.abs(got)) <= 1.0  # clipped PCM16 range


def test_api_stems_progress_and_errors(srv):
    base = srv["base"]
    code, raw, _ = get_raw(f"{base}/api/stems?method=rpca&progress=1")
    assert code == 200
    body = json.loads(raw.decode())
    assert body["method"] == "rpca" and "done" in body and "total" in body

    code, raw, _ = get_raw(f"{base}/api/stems?method=bogus")
    assert code == 400 and "error" in json.loads(raw.decode())
    code, raw, _ = get_raw(f"{base}/api/stem?method=hpss&name=nope")
    assert code == 400
    # /api/stem before compute -> 404
    code, raw, _ = get_raw(f"{base}/api/stem?method=combined&name=harmonic")
    assert code == 404 and "error" in json.loads(raw.decode())

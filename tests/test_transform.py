"""Complex STFT/ISTFT foundation tests: round-trip, equivalence against a
naive reference STFT, Parseval energy, byte-level determinism.

The naive reference below is the legacy numeric contract in explicit form:
periodic hann, win//2 zeros of boundary padding on both sides (center=True),
tail padding so every frame is complete, and 'spectrum' scaling (divide by
win.sum()) — exactly what keyprism.dsp.stft_power produced through
scipy.signal.stft before the transform.py extraction.
"""

import io

import numpy as np
import pytest
from scipy import signal

from keyprism import dsp
from keyprism.transform import (
    istft_complex, magnitude_db, parabolic_peak, stft_complex, stft_pair,
)
from helpers import SR, tone_stereo

WIN = 2048
HOP = WIN - WIN * 3 // 4  # legacy: win//4 for power-of-two windows


def legacy_pad(x, win=WIN, hop=HOP, center=True):
    """The legacy framing padding: boundary zeros + tail pad (explicit)."""
    x = np.asarray(x, dtype=np.float64)
    if center:
        x = np.concatenate([np.zeros(win // 2), x, np.zeros(win // 2)])
    nadd = (-(x.shape[0] - win) % hop) % win
    if nadd:
        x = np.concatenate([x, np.zeros(nadd)])
    return x


def naive_stft(x, win=WIN, hop=HOP, center=True):
    """Naive reference STFT (float64 complex, time-major (n_frames, bins)),
    mirroring the legacy scipy.signal.stft conventions frame by frame."""
    x = legacy_pad(x, win, hop, center)
    win_f = signal.get_window("hann", win, fftbins=True)
    n_frames = 1 + (x.shape[0] - win) // hop
    out = np.empty((n_frames, win // 2 + 1), dtype=np.complex128)
    scale = win_f.sum()
    for i in range(n_frames):
        seg = x[i * hop:i * hop + win] * win_f
        out[i] = np.fft.rfft(seg) / scale
    return out


def sine(seconds=1.0, freq=440.0):
    t = np.arange(int(SR * seconds)) / SR
    return 0.5 * np.sin(2 * np.pi * freq * t)


def chirp_signal(seconds=1.0):
    t = np.arange(int(SR * seconds)) / SR
    return signal.chirp(t, f0=100.0, f1=4000.0, t1=seconds, method="linear")


def seeded_noise(seconds=1.0):
    return np.random.default_rng(42).standard_normal(int(SR * seconds)) * 0.2


def snr(ref, test):
    err = ref - test
    return 10.0 * np.log10((ref ** 2).sum() / max((err ** 2).sum(), 1e-300))


# ---------------------------------------------------------------- equivalence

def test_stft_matches_naive_reference():
    for x in (sine(), chirp_signal(), seeded_noise()):
        z = stft_complex(x, win=WIN)
        ref = naive_stft(x)
        assert z.shape == ref.shape
        assert z.dtype == np.complex64
        assert np.max(np.abs(z - ref)) < 1e-6


def test_stft_core_bit_identical_to_legacy_scipy_call():
    """The float64 core must equal the exact legacy scipy.signal.stft call
    (bit for bit) — this is the numeric contract of the magnitude/dB path."""
    x = tone_stereo(1.0)[:, 0]
    f_ref, _, z_ref = signal.stft(
        x, fs=SR, window="hann", nperseg=WIN, noverlap=WIN * 3 // 4,
        padded=True, boundary="zeros")
    f, z = stft_pair(x, SR, win=WIN)
    assert np.array_equal(f, f_ref)
    assert np.array_equal(z, z_ref)


def test_dsp_stft_power_bit_identical_to_legacy():
    """dsp.stft_power (now a façade) must return the same bits as the
    original scipy-based implementation, which is recomputed here."""
    x = tone_stereo(1.0)[:, 0]
    _, _, z_ref = signal.stft(
        x, fs=SR, window="hann", nperseg=WIN, noverlap=WIN * 3 // 4,
        padded=True, boundary="zeros")
    freqs, power = dsp.stft_power(x, SR, WIN)
    assert np.array_equal(power, np.abs(z_ref) ** 2)
    assert np.array_equal(freqs, np.fft.rfftfreq(WIN, 1.0 / SR))


def test_hop_default_is_legacy_and_explicit_hop_is_honored():
    x = sine(0.5)
    z_default = stft_complex(x, win=WIN)
    z_explicit = stft_complex(x, win=WIN, hop=HOP)
    assert np.array_equal(z_default, z_explicit)
    # 0.5s @ 22050 Hz, win 2048 hop 512: legacy framing math
    assert z_default.shape[0] == 1 + (len(x) + WIN - WIN + HOP - 1) // HOP


# ----------------------------------------------------------------- round trip

@pytest.mark.parametrize("sig", [
    sine(), sine(1.0, 880.0), chirp_signal(), seeded_noise(),
])
def test_roundtrip_snr(sig):
    x = sig
    z = stft_complex(x, win=WIN)
    y = istft_complex(z, win=WIN, hop=HOP, length=len(x))
    edge = WIN  # skip boundary frames where the overlap-add norm is partial
    assert snr(x[edge:-edge], y[edge:-edge]) >= 60.0


def test_istft_interior_is_nearly_exact():
    x = chirp_signal()
    z = stft_complex(x, win=WIN)
    y = istft_complex(z, win=WIN, hop=HOP, length=len(x))
    edge = WIN
    scale = np.max(np.abs(x[edge:-edge]))
    assert np.max(np.abs(x[edge:-edge] - y[edge:-edge])) / scale < 1e-6


def test_istft_handles_center_false_and_explicit_hop():
    x = sine(0.5)
    z = stft_complex(x, win=WIN, hop=WIN // 2, center=False)
    y = istft_complex(z, win=WIN, hop=WIN // 2, center=False,
                      length=len(x))
    edge = WIN
    assert snr(x[edge:-edge], y[edge:-edge]) >= 60.0


def test_istft_default_length_trims_boundary_padding():
    x = sine(0.5)
    z = stft_complex(x, win=WIN)
    y = istft_complex(z, win=WIN, hop=HOP)
    assert y.shape[0] == HOP * (z.shape[0] - 1)


# ------------------------------------------------------------------- Parseval

def test_parseval_energy_with_window_normalization():
    """Frame energy identity incl. the 1/win.sum() spectrum scaling:
    sum((x*w)^2) == (S^2/win) * (|X0|^2 + |Xnyq|^2 + 2*sum_inner |Xk|^2)."""
    x = chirp_signal()
    ref = naive_stft(x)  # time-major (n_frames, bins)
    win_f = signal.get_window("hann", WIN, fftbins=True)
    scale = win_f.sum()
    padded = legacy_pad(x)
    for k in (0, ref.shape[0] // 2, ref.shape[0] - 1):
        seg = padded[k * HOP:k * HOP + WIN]
        e_time = float(((seg * win_f) ** 2).sum())
        spec = ref[k]
        e_freq = (scale ** 2 / WIN) * (
            np.abs(spec[0]) ** 2 + np.abs(spec[-1]) ** 2
            + 2.0 * (np.abs(spec[1:-1]) ** 2).sum())
        assert e_freq == pytest.approx(e_time, rel=1e-9)


# ---------------------------------------------------------------- determinism

def test_byte_level_determinism():
    x = seeded_noise(0.7)
    a = stft_complex(x, win=WIN)
    b = stft_complex(x, win=WIN)
    assert a.tobytes() == b.tobytes()
    bufs = []
    for arr in (a, b):
        bio = io.BytesIO()
        np.save(bio, arr, allow_pickle=False)
        bufs.append(bio.getvalue())
    assert bufs[0] == bufs[1]
    ya = istft_complex(a, win=WIN, hop=HOP, length=len(x))
    yb = istft_complex(b, win=WIN, hop=HOP, length=len(x))
    assert ya.tobytes() == yb.tobytes()


# --------------------------------------------------------------- magnitude_db

def legacy_db(m, peak, db_range=70.0):
    """The exact dB formula payload.compute_specs has always used."""
    return np.maximum(10.0 * np.log10(np.maximum(m / peak, 1e-12)), -db_range)


def test_magnitude_db_matches_legacy_semantics_bitwise():
    m = np.abs(np.random.default_rng(7).random((88, 50)))
    peak = float(m.max() * 1.37)  # explicit cross-channel joint peak
    out = magnitude_db(m, db_range=70.0, peak=peak)
    assert np.array_equal(out, legacy_db(m, peak))
    out2 = magnitude_db(m)  # default peak = matrix max
    assert np.array_equal(out2, legacy_db(m, m.max()))
    assert np.isclose(out2.max(), 0.0)  # the peak maps to 0 dB


def test_magnitude_db_silence_raises():
    with pytest.raises(ValueError, match="静音"):
        magnitude_db(np.zeros((4, 4)))


# ------------------------------------------------------------- parabolic peak

def test_parabolic_peak_exact_parabola_and_formula():
    row = np.array([0.0, 1.0, 4.0, 5.0, 4.0, 1.0, 0.0])
    delta, height = parabolic_peak(row, 3)
    assert delta == pytest.approx(0.0, abs=1e-12)
    assert height == pytest.approx(5.0, abs=1e-12)
    # analytic check of the standard formula on an asymmetric triple
    a, b, c = 1.0, 2.0, 0.5
    row2 = np.array([9.0, a, b, c, 9.0])
    delta2, height2 = parabolic_peak(row2, 2)
    assert delta2 == pytest.approx((a - c) / (2.0 * (a - 2.0 * b + c)))
    assert height2 == pytest.approx(b - 0.25 * (a - c) * delta2)


def test_parabolic_peak_edges_and_bin_alignment():
    row = np.array([3.0, 1.0, 2.0])
    assert parabolic_peak(row, 0) == (0.0, 3.0)
    assert parabolic_peak(row, 2) == (0.0, 2.0)
    # a windowed sinusoid at an exact FFT bin must not shift
    n = 4096
    t = np.arange(n) / SR
    x = np.sin(2 * np.pi * (SR * 100 / n) * t)  # exactly bin 100
    spec = np.abs(np.fft.rfft(x * signal.get_window("hann", n)))
    delta, _ = parabolic_peak(spec, 100)
    assert abs(delta) < 1e-9

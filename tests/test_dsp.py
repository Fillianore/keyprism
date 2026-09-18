"""DSP pure-algorithm layer tests: numpy arrays in and out, no IO"""

import numpy as np

from keyprism.dsp import (
    MIDI_MAX, MIDI_MIN, downsample_max, estimate_bpm, pitch_bins_sub,
    power_to_pitch_bins, spec_matrix, stft_power, widen_rows,
)
from helpers import SR, click_track, tone_stereo


def test_stft_power_shape():
    x = tone_stereo(1.0)[:, 0]
    freqs, power = stft_power(x, SR, 2048)
    assert freqs.shape[0] == power.shape[0]
    assert power.shape[1] > 10
    assert (power >= 0).all()


def test_power_to_pitch_bins_rows():
    x = tone_stereo(1.0)[:, 0]
    freqs, power = stft_power(x, SR, 2048)
    bins = power_to_pitch_bins(freqs, power)
    assert bins.shape[0] == MIDI_MAX - MIDI_MIN + 1  # 88 semitones
    assert bins.shape[1] == power.shape[1]


def test_pitch_bins_sub_rows():
    x = tone_stereo(1.0)[:, 0]
    freqs, power = stft_power(x, SR, 2048)
    for sub in (1, 5, 10):
        bins = pitch_bins_sub(freqs, power, sub)
        assert bins.shape[0] == 88 * sub


def test_widen_rows_preserves_shape_and_energy_scale():
    m = np.random.default_rng(0).random((88 * 5, 40))
    out = widen_rows(m, 5)
    assert out.shape == m.shape
    # normalized kernel adds no energy; nearest clamping at the edges allows
    # a tiny deviation
    assert np.isclose(out.sum(), m.sum(), rtol=1e-3)
    assert (widen_rows(m, 1) == m).all()  # sub=1 is the identity


def test_downsample_max():
    m = np.random.default_rng(1).random((88, 300))
    out = downsample_max(m, 100)
    assert out.shape == (88, 100)
    assert out.max() >= m.max() - 1e-12  # max keeps peaks
    assert (downsample_max(m, 500) == m).all()  # identity when no compression needed


def test_spec_matrix_shapes():
    x = tone_stereo(2.0)[:, 0]
    m, hop, power = spec_matrix(x, SR, 2048, 30, 1)
    assert m.shape[0] == 88
    assert m.shape[1] <= 30 * 2 + 1
    assert 0 < hop <= 1.0 / 5 + 1e-9  # column spacing no finer than 5 cols/s
    assert power.shape[1] > 0


def test_estimate_bpm_click_track():
    x = click_track(bpm=120.0, seconds=12.0)
    freqs, power = stft_power(x, SR, 2048)
    hop = len(x) / SR / max(power.shape[1] - 1, 1)
    bpm, offset = estimate_bpm(power, hop)
    assert 105 <= bpm <= 135  # 120 BPM pulse train; tolerance covers octave errors
    assert 0 <= offset < 60.0 / bpm + 1e-6


def test_estimate_bpm_silence_fallback():
    power = np.zeros((10, 100))
    bpm, offset = estimate_bpm(power, 0.01)
    assert bpm == 120.0 and offset == 0.0

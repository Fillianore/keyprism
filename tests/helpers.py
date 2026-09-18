"""Test helpers: generate small real audio files (wav / m4a)"""

import av
import numpy as np
import soundfile as sf

SR = 22050


def tone_stereo(seconds: float = 2.0, sr: int = SR) -> np.ndarray:
    """440/880Hz stereo sine, non-zero stable energy, suitable for the
    analysis pipeline"""
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    sig = 0.4 * np.sin(2 * np.pi * 440 * t) + 0.3 * np.sin(2 * np.pi * 880 * t)
    return np.stack([sig, np.roll(sig, 100)], axis=1)


def make_wav(path, seconds: float = 2.0, sr: int = SR) -> None:
    sf.write(path, tone_stereo(seconds, sr), sr, subtype="PCM_16")


def make_m4a(path, seconds: float = 2.0, sr: int = SR) -> None:
    """AAC/M4A container: unsupported by libsndfile, forces the PyAV
    fallback path"""
    stereo = tone_stereo(seconds, sr)
    cont = av.open(str(path), "w")
    s = cont.add_stream("aac", rate=sr)
    s.layout = "stereo"
    for i in range(0, len(stereo), 1024):
        arr = np.ascontiguousarray(stereo[i:i + 1024].T.astype(np.float32))
        frm = av.AudioFrame.from_ndarray(arr, format="fltp", layout="stereo")
        frm.sample_rate = sr
        frm.pts = None
        for p in s.encode(frm):
            cont.mux(p)
    for p in s.encode(None):
        cont.mux(p)
    cont.close()


def click_track(bpm: float = 120.0, seconds: float = 12.0,
                sr: int = SR) -> np.ndarray:
    """Evenly spaced decaying pulse train (metronome), a deterministic input
    for BPM estimation"""
    x = np.zeros(int(sr * seconds))
    period = 60.0 / bpm
    t_burst = np.arange(int(0.05 * sr)) / sr
    burst = np.sin(2 * np.pi * 1000 * t_burst) * np.exp(-t_burst * 80)
    for k in range(int(seconds / period)):
        i = int(k * period * sr)
        x[i:i + len(burst)] += burst
    return x

"""测试辅助: 生成小型真实音频文件 (wav / m4a)"""

import av
import numpy as np
import soundfile as sf

SR = 22050


def tone_stereo(seconds: float = 2.0, sr: int = SR) -> np.ndarray:
    """440/880Hz 双声道正弦, 能量非零且稳定, 适合分析管线"""
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    sig = 0.4 * np.sin(2 * np.pi * 440 * t) + 0.3 * np.sin(2 * np.pi * 880 * t)
    return np.stack([sig, np.roll(sig, 100)], axis=1)


def make_wav(path, seconds: float = 2.0, sr: int = SR) -> None:
    sf.write(path, tone_stereo(seconds, sr), sr, subtype="PCM_16")


def make_m4a(path, seconds: float = 2.0, sr: int = SR) -> None:
    """AAC/M4A 容器: libsndfile 不支持, 强制走 PyAV 回退路径"""
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
    """等间隔衰减脉冲串 (节拍器), BPM 估计的确定性输入"""
    x = np.zeros(int(sr * seconds))
    period = 60.0 / bpm
    t_burst = np.arange(int(0.05 * sr)) / sr
    burst = np.sin(2 * np.pi * 1000 * t_burst) * np.exp(-t_burst * 80)
    for k in range(int(seconds / period)):
        i = int(k * period * sr)
        x[i:i + len(burst)] += burst
    return x

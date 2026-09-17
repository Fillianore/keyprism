#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""钢琴频谱分析 - 后端: 分析音频并输出数据文件供 Vite 前端使用

输出到 frontend/public/:
  data.json   元信息 + 频谱矩阵(uint8 base64) + 色标 + 包络图
  audio.<ext> 原始音频副本 (供前端播放)

用法:
    python backend.py 歌曲.mp3
    python backend.py 歌曲.mp3 --max-cols 3000 --db-range 60
    python backend.py 歌曲.mp3 --start 30 --end 90
"""

import argparse
import base64
import io
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import convolve1d

from dsp import (  # noqa: E402
    MIDI_MAX, MIDI_MIN, downsample_max, midi_to_freq, note_name,
    power_to_pitch_bins, stft_power,
)

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

PUBLIC_DIR = Path(__file__).resolve().parent / "frontend" / "public"
INIT_VIEW_SEC = 15.0  # 频谱区默认视窗宽度
COLORMAPS = ["Inferno", "Magma", "Plasma", "Viridis", "Cividis", "Turbo"]


def build_colorscale(name: str = "inferno", n: int = 33) -> list:
    cmap = matplotlib.colormaps[name](np.linspace(0.0, 1.0, n))
    cs = [[round(i / (n - 1), 4),
           f"rgb({int(r * 255)},{int(g * 255)},{int(b * 255)})"]
          for i, (r, g, b, _) in enumerate(cmap)]
    # 下限锚点强制纯黑: 静默区沉入深色背景, 凸显有效频谱。
    # 只改首锚点颜色, 与第二锚点之间仍是平滑线性过渡, 无断层。
    cs[0][1] = "#000000"
    return cs


def build_envelope_dataurl(spec_db: np.ndarray) -> str:
    env = spec_db.max(axis=0)
    fig = plt.figure(figsize=(8, 0.6), dpi=80)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.imshow(env[None, :], aspect="auto", cmap="inferno",
              interpolation="nearest")
    ax.axis("off")
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def estimate_bpm(power: np.ndarray, hop: float) -> tuple[float, float]:
    """频谱通量自相关估计 BPM 与首拍偏移 (粗略初值, 供前端手动微调)"""
    mag = np.sqrt(power)
    flux = np.maximum(np.diff(mag, axis=1), 0.0).sum(axis=0)
    if flux.size < 64 or flux.max() <= 0:
        return 120.0, 0.0
    flux = flux - flux.mean()
    n = flux.size
    # FFT 自相关 (Wiener)
    ac = np.fft.irfft(np.abs(np.fft.rfft(flux, 2 * n)) ** 2)[: n // 2]
    if ac.max() <= 0:
        return 120.0, 0.0
    ac /= ac.max()
    bpms = np.arange(50.0, 240.5, 0.5)
    lags = 60.0 / bpms / hop  # 每个候选 BPM 对应的滞后帧数
    vals = np.interp(lags, np.arange(ac.size), ac)
    prior = np.where((bpms >= 70) & (bpms <= 180), 1.0, 0.7)  # 常见区间加权
    bpm = float(bpms[np.argmax(vals * prior)])
    # 相位: 梳状滤波, 对齐节拍脉冲串找首拍位置
    period_f = 60.0 / bpm / hop
    xs = np.arange(flux.size)
    best_off, best_score = 0.0, -np.inf
    for ph in np.arange(0, period_f, max(period_f / 48.0, 0.5)):
        idx = ph + np.arange(int((flux.size - ph) / period_f)) * period_f
        if idx.size == 0:
            continue
        score = float(np.interp(idx, xs, flux).sum())
        if score > best_score:
            best_score, best_off = score, float(ph)
    return round(bpm, 1), round(best_off * hop, 3)


def _load_via_av(path: Path) -> tuple[np.ndarray, int]:
    """PyAV (ffmpeg) 解码 libsndfile 不支持的容器 (m4a/aac 等),
    返回 (data[n, ch] float64, samplerate)"""
    import av

    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        sr = stream.rate
        chunks = []
        for packet in container.demux(stream):
            for frame in packet.decode():
                if frame.format.is_planar:
                    chunks.append(frame.to_ndarray())  # (ch, n)
                else:
                    a = frame.to_ndarray()  # (1, n*ch)
                    ch = len(frame.layout.channels)
                    chunks.append(a.reshape(ch, -1))
        data = np.concatenate(chunks, axis=1).T.astype(np.float64)
    return data, sr


def browser_safe_audio(path: Path) -> str:
    """保证浏览器 decodeAudioData 一定能解: mp3/wav/ogg/flac 直接复制,
    其他容器 (m4a/aac/alac...) 用 PyAV 解码转存 PCM WAV"""
    ext = path.suffix.lower()
    if ext in ('.mp3', '.wav', '.ogg', '.flac'):
        name = f'audio{ext}'
        shutil.copyfile(path, PUBLIC_DIR / name)
        return name
    data, sr = _load_via_av(path)
    sf.write(PUBLIC_DIR / 'audio.wav', data, sr, subtype='PCM_16')
    return 'audio.wav'


def load_channels(path: Path, start: float, end: float | None):
    """读取原始多声道数据 (不做单声道混合), 返回 (data[n, ch], sr, 时长)"""
    try:
        with sf.SoundFile(str(path)) as f:
            sr = f.samplerate
            total = len(f)
            s = int(start * sr)
            e = total if end is None else min(int(end * sr), total)
            if s >= total:
                raise ValueError(
                    f"--start {start}s 超出音频长度 {total / sr:.1f}s")
            f.seek(s)
            data = f.read(e - s, dtype="float64", always_2d=True)
        return data, sr, (e - s) / sr
    except sf.LibsndfileError:
        # libsndfile 不支持该容器 (如 m4a), 回退 PyAV 全量解码后切片
        full, sr = _load_via_av(path)
        s = int(start * sr)
        e = len(full) if end is None else min(int(end * sr), len(full))
        if s >= len(full):
            raise ValueError(
                f"--start {start}s 超出音频长度 {len(full) / sr:.1f}s")
        return full[s:e], sr, (e - s) / sr


def pitch_bins_sub(freqs: np.ndarray, power: np.ndarray, sub: int):
    """把每个半音频带几何等分为 sub 个子带 (sub=1 即普通半音聚合)"""
    if sub == 1:
        return power_to_pitch_bins(freqs, power)
    rows = []
    for m in range(MIDI_MIN, MIDI_MAX + 1):
        f_lo = midi_to_freq(m - 0.5)
        f_hi = midi_to_freq(m + 0.5)
        edges = f_lo * (f_hi / f_lo) ** (np.arange(sub + 1) / sub)
        for k in range(sub):
            band = (freqs >= edges[k]) & (freqs < edges[k + 1])
            if band.any():
                rows.append(power[band].sum(axis=0))
            else:
                idx = int(np.argmin(np.abs(freqs - np.sqrt(edges[k] * edges[k + 1]))))
                rows.append(power[idx])
    return np.vstack(rows)


def widen_rows(mat: np.ndarray, sub: int) -> np.ndarray:
    """子带行方向的轻度能量展宽 (三角核, 总宽约一个半音)。

    子带变多后每个子带只覆盖极窄频段, 能量碎片化难以分辨;
    在功率域沿频率轴做三角核卷积, 让信息在视觉上连续起来。"""
    if sub <= 1:
        return mat
    half = max(1, sub // 2)
    k = np.concatenate([np.arange(1, half + 1), np.arange(half, 0, -1)])
    k = k / k.sum()
    return convolve1d(mat, k, axis=0, mode="nearest")


def spec_matrix(x: np.ndarray, sr: int, window: int, max_cols: int, sub: int):
    """单声道信号 -> (88*sub x 时间) 功率矩阵与列距 (降采样后)"""
    freqs, power = stft_power(x, sr, window)
    frames = power.shape[1]
    hop_frame = len(x) / sr / max(frames - 1, 1)
    pitch = widen_rows(pitch_bins_sub(freqs, power, sub), sub)
    m = downsample_max(pitch, max_cols)
    block = max(1, int(np.ceil(frames / max_cols)))  # 每列包含的帧数
    return m, hop_frame * block, power


TIME_RATES = [5, 10, 15, 30]  # 每秒时间列数选项
SUB_OPTIONS = [1, 5, 10]      # 每半音子带数选项


def compute_specs(data2d: np.ndarray, sr: int, dur: float, rate: int,
                  sub: int, db_range: float, window: int) -> dict:
    """按分辨率参数计算三通道频谱 (统一峰值归一化)"""
    rate = min(max(rate, TIME_RATES[0]), TIME_RATES[-1])
    if sub not in SUB_OPTIONS:
        sub = 1
    max_cols = max(60, int(round(rate * dur)))
    n_ch = data2d.shape[1]
    channels = {
        "mix": data2d.mean(axis=1),
        "left": data2d[:, 0],
        "right": data2d[:, 1] if n_ch > 1 else data2d[:, 0].copy(),
    }
    specs = {}
    hop = dur / 1000.0
    for name, sig in channels.items():
        m, hop, _ = spec_matrix(sig, sr, window, max_cols, sub)
        specs[name] = m
    peak = max(m.max() for m in specs.values())
    if peak <= 0:
        raise ValueError("音频为静音")
    quant = {}
    envelopes = {}
    for name, m in specs.items():
        spec_db = np.maximum(
            10.0 * np.log10(np.maximum(m / peak, 1e-12)), -db_range)
        q = np.clip((spec_db + db_range) / db_range, 0.0, 1.0)
        quant[name] = base64.b64encode(
            (q * 255.0).round().astype(np.uint8).tobytes()).decode()
        envelopes[name] = build_envelope_dataurl(spec_db)
    n_rows, n_cols = specs["mix"].shape
    return {
        "specs": quant,
        "envelopes": envelopes,
        "nCols": n_cols,
        "hopSec": round(hop, 6),
        "rate": rate,
        "sub": sub,
    }


def analyze(path: Path, start: float, end: float | None, window: int,
            db_range: float, rate: int, sub: int,
            api_base: str | None = None) -> dict:
    data2d, sr, dur = load_channels(path, start, end)
    print(f"[1/3] 已加载 {dur:.1f}s @ {sr} Hz, {data2d.shape[1]} 声道")

    # BPM 用混合声道全分辨率 STFT 估计 (一次性)
    freqs, mix_power = stft_power(data2d.mean(axis=1), sr, window)
    hop_full = dur / max(mix_power.shape[1] - 1, 1)
    bpm, beat_offset = estimate_bpm(mix_power, hop_full)
    print(f"[2/3] 估计 BPM {bpm} (首拍偏移 {beat_offset * 1000:.0f}ms)")

    res = compute_specs(data2d, sr, dur, rate, sub, db_range, window)
    print(f"[3/3] 分辨率 {res['rate']} 列/s x {res['sub']} 子带/半音 "
          f"-> {88 * res['sub']} 行 x {res['nCols']} 列 x 3 通道")

    notes = list(range(MIDI_MIN, MIDI_MAX + 1))
    audio_name = browser_safe_audio(path)

    payload = {
        "file": path.name,
        "durationSec": round(dur, 3),
        "dbRange": db_range,
        "window": window,
        "offsetSec": start,
        "endSec": end,
        "initViewSec": min(INIT_VIEW_SEC, dur),
        "noteLabels": [note_name(m) for m in notes],
        "cTickIdx": [i for i, m in enumerate(notes) if m % 12 == 0],
        "colorscales": {name: build_colorscale(name.lower())
                        for name in COLORMAPS},
        "defaultCmap": "Inferno",
        "timeRates": TIME_RATES,
        "subOptions": SUB_OPTIONS,
        "defaultRate": res["rate"],
        "defaultSub": res["sub"],
        "apiBase": api_base,
        "audioFile": audio_name,
        "bpm": bpm,
        "beatOffsetSec": beat_offset,
        **res,
    }
    return payload


def run_server(path: Path, port: int, start: float, end: float | None,
               window: int, db_range: float, rate: int, sub: int):
    """常驻服务: 前端可随时请求新分辨率的频谱"""
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    data2d, sr, dur = load_channels(path, start, end)
    api_base = f"http://localhost:{port}"
    payload = analyze(path, start, end, window, db_range, rate, sub, api_base)
    out = PUBLIC_DIR / "data.json"
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"已输出: {out.resolve()} (apiBase={api_base})")

    state = {"busy": False, "cache": {}}

    class Handler(BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/api/ping":
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._cors()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if u.path != "/api/spec":
                self.send_error(404)
                return
            q = urllib.parse.parse_qs(u.query)
            r = int(q.get("rate", [str(rate)])[0])
            s = int(q.get("sub", [str(sub)])[0])
            key = (r, s)
            if key in state["cache"]:
                res = state["cache"][key]
            else:
                try:
                    res = compute_specs(data2d, sr, dur, r, s,
                                        db_range, window)
                except Exception as e:  # noqa: BLE001
                    body = json.dumps({"error": str(e)}).encode()
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self._cors()
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if len(state["cache"]) > 3:  # 最多缓存 4 组分辨率
                    state["cache"].clear()
                state["cache"][key] = res
            body = json.dumps(res).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):  # noqa: A002
            pass

    print(f"服务已启动: {api_base}  (Ctrl+C 退出)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(description="钢琴频谱分析后端")
    ap.add_argument("input", help="输入音频 (mp3/wav/flac/ogg ...)")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--window", type=int, default=8192,
                    help="STFT 窗长 (默认 8192)")
    ap.add_argument("--db-range", type=float, default=70.0)
    ap.add_argument("--rate", type=int, default=15, choices=TIME_RATES,
                    help="每秒时间列数 (默认 15)")
    ap.add_argument("--sub", type=int, default=1, choices=SUB_OPTIONS,
                    help="每半音子带数 (默认 1)")
    ap.add_argument("--serve", type=int, default=None, metavar="PORT",
                    help="常驻服务端口, 支持前端随时切换分辨率 (如 8800)")
    args = ap.parse_args(argv)

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"文件不存在: {src}")
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

    if args.serve:
        run_server(src, args.serve, args.start, args.end, args.window,
                   args.db_range, args.rate, args.sub)
        return

    payload = analyze(src, args.start, args.end, args.window,
                      args.db_range, args.rate, args.sub)
    out = PUBLIC_DIR / "data.json"
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    audio_mb = (PUBLIC_DIR / payload["audioFile"]).stat().st_size / 1024 / 1024
    print(f"已输出: {out.resolve()}")
    print(f"已输出: {(PUBLIC_DIR / payload['audioFile']).resolve()} "
          f"({audio_mb:.1f} MB)")
    print("前端: cd frontend && npm run dev  (静态模式, 切换分辨率需 --serve)")


if __name__ == "__main__":
    main()

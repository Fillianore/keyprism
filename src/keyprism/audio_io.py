#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism 音频 IO 层: 解码链与浏览器兼容转存

解码优先级: libsndfile (soundfile) 直读, 失败回退 PyAV (ffmpeg),
覆盖 mp3/wav/ogg/flac 与 m4a/aac/wma/opus/aiff 等主流容器。

本模块同时持有路径常量:
- PUBLIC_DIR    前端静态产物目录 (data.json / audio.*)
- KEYPRISM_HOME 工作区 (上传暂存等), 可用环境变量重定向
"""

import os
import shutil
from pathlib import Path

import numpy as np
import soundfile as sf
def _repo_root() -> Path:
    """从本文件向上定位仓库根 (含 pyproject.toml 的目录)。

    src layout 下包在 src/keyprism/, 层数可能变化;
    按 pyproject.toml 探测比数 parent 层数更稳健。
    """
    for cand in Path(__file__).resolve().parents:
        if (cand / "pyproject.toml").exists():
            return cand
    return Path(__file__).resolve().parent.parent.parent


# 前端静态产物目录: 仓库根/frontend/public
# (uv 默认以 editable 方式安装本项目, __file__ 始终指向源码树)
PUBLIC_DIR = _repo_root() / "frontend" / "public"

# 演示音频: 无参数启动时的默认输入 (仓库根/assets)
DEMO_AUDIO = _repo_root() / "assets" / "demo.m4a"

# 工作区: 上传暂存 / matplotlib 缓存等产物默认集中于此,
# 可用环境变量 KEYPRISM_HOME 重定向 (启动脚本亦读取 ~/.keyprism/config.env)
KEYPRISM_HOME = Path(
    os.environ.get("KEYPRISM_HOME") or Path.home() / ".keyprism"
).expanduser()
UPLOAD_DIR = KEYPRISM_HOME / "uploads"  # 上传音频暂存
MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 上传大小上限 512MB


def _load_via_av(path: Path) -> tuple[np.ndarray, int]:
    """PyAV (ffmpeg) 解码 libsndfile 不支持的容器 (m4a/aac 等),
    返回 (data[n, ch] float64, samplerate)"""
    import av

    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError("文件中没有音轨")
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

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism audio IO layer: decode chain and browser-safe transcoding

Decode priority: libsndfile (soundfile) reads directly, falling back to
PyAV (ffmpeg) on failure, covering mp3/wav/ogg/flac and common containers
such as m4a/aac/wma/opus/aiff.

This module also owns the path constants:
- PUBLIC_DIR    frontend static output directory (data.json / audio.*)
- KEYPRISM_HOME workspace (upload staging etc.), relocatable via env var
"""

import os
import shutil
from pathlib import Path

import numpy as np
import soundfile as sf
def _repo_root() -> Path:
    """Locate the repo root (the directory containing pyproject.toml) by
    walking up from this file.

    Under the src layout the package sits in src/keyprism/ and the depth can
    change; probing for pyproject.toml is more robust than counting parents.
    """
    for cand in Path(__file__).resolve().parents:
        if (cand / "pyproject.toml").exists():
            return cand
    return Path(__file__).resolve().parent.parent.parent


# Frontend static output directory: repo root/frontend/public
# (uv installs this project editable by default, so __file__ always points
# into the source tree)
PUBLIC_DIR = _repo_root() / "frontend" / "public"

# Demo audio: default input when started without arguments (repo root/assets)
DEMO_AUDIO = _repo_root() / "assets" / "demo.m4a"

# Workspace: upload staging / matplotlib cache and similar artifacts
# consolidate here by default; relocatable via the KEYPRISM_HOME env var
# (the launch scripts also read ~/.keyprism/config.env)
KEYPRISM_HOME = Path(
    os.environ.get("KEYPRISM_HOME") or Path.home() / ".keyprism"
).expanduser()
UPLOAD_DIR = KEYPRISM_HOME / "uploads"  # upload staging
MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # upload size cap: 512MB


def _load_via_av(path: Path) -> tuple[np.ndarray, int]:
    """Decode containers libsndfile cannot handle (m4a/aac etc.) with
    PyAV (ffmpeg); returns (data[n, ch] float64, samplerate)"""
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
    """Guarantee the browser's decodeAudioData can always decode:
    mp3/wav/ogg/flac are copied directly; other containers (m4a/aac/alac...)
    are decoded with PyAV and transcoded to PCM WAV"""
    ext = path.suffix.lower()
    if ext in ('.mp3', '.wav', '.ogg', '.flac'):
        name = f'audio{ext}'
        shutil.copyfile(path, PUBLIC_DIR / name)
        return name
    data, sr = _load_via_av(path)
    sf.write(PUBLIC_DIR / 'audio.wav', data, sr, subtype='PCM_16')
    return 'audio.wav'


def load_channels(path: Path, start: float, end: float | None):
    """Read raw multichannel data (no mono downmix); returns (data[n, ch], sr,
    duration)"""
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
        # libsndfile cannot handle this container (e.g. m4a); fall back to a
        # full PyAV decode and slice afterwards
        full, sr = _load_via_av(path)
        s = int(start * sr)
        e = len(full) if end is None else min(int(end * sr), len(full))
        if s >= len(full):
            raise ValueError(
                f"--start {start}s 超出音频长度 {len(full) / sr:.1f}s")
        return full[s:e], sr, (e - s) / sr

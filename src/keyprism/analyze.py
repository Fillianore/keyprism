#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism analysis cache: content-addressed complex-STFT artifacts

Phase 0 foundation for the staged analysis pipeline. The full-resolution
mix-channel complex STFT is stored as a memmap-able artifact under the user
workspace so later phases (source separation, transcription) and repeated
analyses of the same track can read it back without recomputing:

    $KEYPRISM_HOME/cache/analysis/<pcm16>/<params16>/
        stft.npy    complex64 (n_frames, win//2+1), filled + flushed in
                    row chunks of at most 2048 frames
        meta.json   sr, win, hop, n_frames, n_bins, duration, params,
                    created_at (plus derived fields such as bpm)

Key scheme:
- pcm16    = sha256(canonical PCM16 bytes of the decoded analysis
             segment)[:16]; canonical PCM16 = row-interleaved int16 of the
             clipped float samples
- params16 = sha256(canonical JSON of {sr, win, hop, window, center, rate,
             sub, db_range, start, end})[:16]

Discipline: the full complex STFT is NEVER held in RAM — writes go through
``np.lib.format.open_memmap`` in bounded chunks, reads go through
``np.load(..., mmap_mode="r")`` row slices only. Eviction is LRU by stft
mtime, capped by ``KEYPRISM_CACHE_MAX_ENTRIES`` (default 8; the usual
env > config.env > default precedence applies because config.env is
materialized into the environment by the launch scripts).

The cache root is derived from ``audio_io.KEYPRISM_HOME`` at call time (module
attribute, never a value binding) so tests can relocate the whole workspace.
"""

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import audio_io
from .transform import default_hop, n_frames_of

CACHE_DIRNAME = "cache/analysis"
CACHE_MAX_ENTRIES_DEFAULT = 8
STORE_CHUNK_FRAMES = 512  # memmap fill granularity (hard cap: 2048)
STFT_DTYPE = np.complex64


def cache_root() -> Path:
    """Analysis cache root under the (runtime) workspace."""
    return audio_io.KEYPRISM_HOME / CACHE_DIRNAME


def max_entries() -> int:
    """LRU capacity: KEYPRISM_CACHE_MAX_ENTRIES env > default 8."""
    raw = os.environ.get("KEYPRISM_CACHE_MAX_ENTRIES", "").strip()
    try:
        return max(1, int(raw)) if raw else CACHE_MAX_ENTRIES_DEFAULT
    except ValueError:
        return CACHE_MAX_ENTRIES_DEFAULT


def pcm16_fingerprint(data2d: np.ndarray) -> str:
    """sha256[:16] of the decoded segment as canonical PCM16 bytes
    (row-interleaved int16 of the clipped float samples)."""
    pcm = (np.clip(np.asarray(data2d, dtype=np.float64), -1.0, 1.0)
           * 32767.0).astype("<i2")
    return hashlib.sha256(np.ascontiguousarray(pcm).tobytes()).hexdigest()[:16]


def params_dict(*, sr, win, hop, window, center, rate, sub, db_range, start,
                end) -> dict:
    """Canonical analysis parameter dict (JSON-serializable, sorted keys)."""
    return {
        "center": bool(center),
        "db_range": float(db_range),
        "end": None if end is None else float(end),
        "hop": int(hop),
        "rate": int(rate),
        "sr": int(sr),
        "start": float(start),
        "sub": int(sub),
        "win": int(win),
        "window": str(window),
    }


def params_fingerprint(params: dict) -> str:
    """sha256[:16] of the canonical JSON serialization of the params."""
    blob = json.dumps(params, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def entry_dir(pcm16: str, params16: str, root: Path | None = None) -> Path:
    return (root or cache_root()) / pcm16 / params16


def store_stft(entry: Path, stft_chunk_iter, n_frames: int,
               n_bins: int) -> None:
    """Fill ``stft.npy`` via open_memmap, chunk by chunk (<= 2048 frames),
    flushing each chunk; atomic rename into place."""
    entry.mkdir(parents=True, exist_ok=True)
    tmp = entry / "stft.npy.tmp"
    for stale in entry.glob("*.tmp"):
        stale.unlink(missing_ok=True)
    try:
        mm = np.lib.format.open_memmap(tmp, mode="w+", dtype=STFT_DTYPE,
                                       shape=(n_frames, n_bins))
        for c0, chunk in stft_chunk_iter:
            z64 = chunk.astype(STFT_DTYPE)
            mm[c0:c0 + z64.shape[0]] = z64
            mm.flush()
        mm.flush()
        del mm
        os.replace(tmp, entry / "stft.npy")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def store_meta(entry: Path, meta: dict) -> None:
    """Write ``meta.json`` atomically (created_at added if missing)."""
    meta = dict(meta)
    meta.setdefault("created_at",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"))
    tmp = entry / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, entry / "meta.json")


def load_entry(entry: Path):
    """Return ``(memmap_r, meta)`` for a valid entry, else ``None``.

    The memmap is opened read-only; consumers must slice rows, never copy
    the whole array."""
    stft_path = entry / "stft.npy"
    meta_path = entry / "meta.json"
    if not (stft_path.is_file() and meta_path.is_file()):
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        mm = np.load(stft_path, mmap_mode="r")
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if mm.dtype != STFT_DTYPE or mm.ndim != 2:
        return None
    return mm, meta


def iter_chunks(path: Path, seconds: float = 10.0, sr: int | None = None,
                hop: int | None = None):
    """Yield consecutive row slices of a stored ``stft.npy`` (memmap views,
    never a full copy), ``seconds`` of audio per slice. ``sr``/``hop``
    default to the sibling meta.json values."""
    mm = np.load(path, mmap_mode="r")
    meta = json.loads((Path(path).parent / "meta.json").read_text(
        encoding="utf-8"))
    hop = meta["hop"] if hop is None else int(hop)
    sr = meta["sr"] if sr is None else int(sr)
    rows = max(1, int(round(seconds * sr / hop)))
    for a in range(0, mm.shape[0], rows):
        yield mm[a:a + rows]


def evict_old(root: Path | None = None) -> list[str]:
    """LRU-evict entries beyond ``KEYPRISM_CACHE_MAX_ENTRIES`` (by stft.npy
    mtime); returns the removed entry dir names."""
    root = root or cache_root()
    if not root.is_dir():
        return []
    entries = [d for d in root.glob("*/*") if d.is_dir()]

    def mtime(d: Path) -> float:
        f = d / "stft.npy"
        try:
            return f.stat().st_mtime
        except OSError:
            return d.stat().st_mtime

    entries.sort(key=mtime)
    removed = []
    for d in entries[:-max_entries()] if len(entries) > max_entries() else []:
        shutil.rmtree(d, ignore_errors=True)
        removed.append(f"{d.parent.name}/{d.name}")
        try:
            d.parent.rmdir()  # drop empty pcm parent
        except OSError:
            pass
    return removed

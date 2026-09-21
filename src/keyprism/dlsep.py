#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism DL separation: Demucs via ONNX Runtime, chunked overlap-add
(Phase 3)

Position: sits beside ``stems`` (never modifies it) and is called by
``server``; owns the DL stem disk cache. Dependency direction stays
one-way: ``server -> dlsep -> {analyze (entry paths), audio_io}``. The
heavy dependencies (onnxruntime, huggingface_hub) are OPTIONAL: both
imports are guarded and everything below works without them —
- the availability flags (``ORT_AVAILABLE`` / ``HF_AVAILABLE``) let the
  server answer 501 Not Implemented instead of crashing (graceful
  degradation, Phase 3 contract);
- the pure chunking/blend math is dependency-free and testable through
  an injected ``infer_fn`` (no model needed to verify the OLA pipeline).

Streaming discipline (AGENTS.md memory rule, Phase 3 edition): the full
track is NEVER handed to the model. ``separate`` slices the PCM into
``chunk_sec`` windows with ``overlap_sec`` of overlap, runs one ONNX
session call per chunk, and cross-fades the chunk outputs with a
strictly-positive Hann window normalized by the accumulated window sum
(exact reconstruction — see ``_hann_cola`` / ``separate`` docstrings).
Peak RAM is bounded by one chunk plus the session, never the track
length; the ONNX session itself is a per-model SINGLETON (re-loading a
session per request is a memory leak).

Stem cache mirrors the Phase 2 layout, version-tagged independently:

    <entry>/stems/<DL_STEMS_VERSION>/<method>/<stem>.wav  (+ status.json)

bumping ``DL_STEMS_VERSION`` invalidates previously cached DL stems
everywhere (same rule as STEMS_VERSION / MONO_VERSION). Writes go to a
``.tmp-<pid>`` directory with an atomic ``os.replace`` so a crashed
compute never leaves half-written stems.

Model discovery (``~/.keyprism/models/demucs/``, relocatable via
``KEYPRISM_HOME``): an explicit ``KEYPRISM_DEMUCS4_FILE`` /
``KEYPRISM_DEMUCS6_FILE`` path wins, then the spec'd file name, then any
``*.onnx`` already in the model dir, and only then is the model
auto-downloaded from the (env-overridable) Hugging Face repo of the
variant spec. Any Demucs ONNX export with a ``(1, C, T)`` float input
and stems output is accepted; the I/O adapter normalizes 3D/4D, batched
or listed outputs to ``(n_stems, T)`` mono.
"""

import json
import os
import shutil
import threading
from datetime import datetime, timezone
from math import gcd
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

try:  # optional: inference backend
    import onnxruntime as ort

    ORT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via the flag in tests
    ort = None
    ORT_AVAILABLE = False

try:  # optional: model auto-download
    from huggingface_hub import hf_hub_download

    HF_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via the flag in tests
    hf_hub_download = None
    HF_AVAILABLE = False

from . import audio_io

__all__ = [
    "DL_STEMS_VERSION", "DL_METHODS", "STEM_SPECS", "TARGET_SR",
    "DLMissingError", "ORT_AVAILABLE", "HF_AVAILABLE",
    "plan_chunks", "hann_cola", "resample", "ola_separate",
    "DemucsSeparator", "get_separator", "dl_stems_dir", "dl_status_path",
    "dl_stem_path", "load_status", "write_stems", "model_dir",
]

#: Version tag of the DL stem pipeline; part of the cache path (bump to
#: invalidate previously cached DL stems everywhere).
DL_STEMS_VERSION = "dl_v1"

#: Query values accepted by /api/stems for DL separation.
DL_METHODS = ("demucs_4", "demucs_6")

#: method -> stem keys in canonical (htdemucs) order; the file names
#: under <entry>/stems/<DL_STEMS_VERSION>/<method>/.
STEM_SPECS = {
    "demucs_4": ("drums", "bass", "other", "vocals"),
    "demucs_6": ("drums", "bass", "other", "vocals", "piano", "guitar"),
}

#: Demucs models are trained at 44.1 kHz; input is resampled, stems are
#: written at this rate (the frontend schedules by seconds, so mixed
#: sample rates across mix/stay lanes stay sample-accurate).
TARGET_SR = 44100

#: Variant specs: Hugging Face repo + file for the auto-download path.
#: Both are env-overridable (KEYPRISM_DEMUCS4_REPO/_FILE and the 6-stem
#: twins) because ONNX exports of htdemucs/htdemucs_6s are community
#: artifacts; any compatible export can be dropped into the model dir.
_VARIANTS = {
    "demucs_4": {
        "repo": "Xenova/htdemucs-onnx",
        "file": "htdemucs.onnx",
    },
    "demucs_6": {
        "repo": "Xenova/htdemucs_6s-onnx",
        "file": "htdemucs_6s.onnx",
    },
}

#: Raised when the optional DL dependencies or the model weights are
#: missing (the server maps this to 501 / a clear error, never a 500).
class DLMissingError(RuntimeError):
    pass


def model_dir() -> Path:
    """DL model directory under the (runtime) workspace."""
    return audio_io.KEYPRISM_HOME / "models" / "demucs"


# ----------------------------------------------------------- chunk math

def plan_chunks(total: int, chunk: int, overlap: int) -> list:
    """Tile ``[0, total)`` into windows of exactly ``chunk`` samples.

    Consecutive windows advance by ``chunk - overlap``; the final window
    is pulled back to end exactly at ``total`` (so every window has the
    same length and the tail is covered without padding). Returns a list
    of ``(start, end)`` with ``end - start == chunk`` (only shorter when
    ``total < chunk`` itself). Raises on degenerate parameters."""
    if chunk <= 0 or overlap < 0 or overlap >= chunk:
        raise ValueError("需要 0 <= overlap < chunk")
    if total <= 0:
        return []
    if total <= chunk:
        return [(0, total)]
    hop = chunk - overlap
    starts = list(range(0, total - chunk + 1, hop))
    if starts[-1] + chunk < total:
        starts.append(total - chunk)
    return [(s, s + chunk) for s in starts]


def hann_cola(length: int) -> np.ndarray:
    """Cross-fade window for overlap-add blending.

    The PERIODIC Hann window evaluated at sample centers
    (``0.5 - 0.5*cos(2*pi*(i+0.5)/L)``): strictly positive everywhere
    (no zero endpoints), so a region covered by a single chunk divides
    out to exactly that chunk's output (``w*y/w == y`` — no 0/0 at the
    track edges), and at 50% overlap the pair sum is exactly 1
    (``cos(t) + cos(t+pi) == 0``), i.e. the classic
    ``out = c1*fade_out + c2*fade_in`` with ``fade_out + fade_in == 1``.
    """

    i = np.arange(length, dtype=np.float64)
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * (i + 0.5) / float(length))


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Polyphase resample (last-axis time) with a rational-ratio
    resample_poly; input may be 1D or 2D (channels, samples)."""
    x = np.asarray(x)
    if sr_in == sr_out:
        return x.astype(np.float32, copy=False)
    g = gcd(int(sr_in), int(sr_out))
    return resample_poly(
        x.astype(np.float64), sr_out // g, sr_in // g, axis=-1,
    ).astype(np.float32)


def ola_separate(pcm: np.ndarray, infer, *, chunk_sec: float = 10.0,
                 overlap_sec: float = 1.0, sr: int = TARGET_SR,
                 progress=None) -> dict:
    """Chunked inference + Hann overlap-add blending (pure numpy).

    ``pcm`` is mono float32 at ``sr``; ``infer(x: (T,) float32)`` must
    return either ``(n_stems, T)`` or ``(n_stems, 1, T)``-like model
    output for the SAME samples it received (this is the injection seam
    the tests use instead of a real ONNX session).

    Blending math (the anti-click core):
      - each chunk output y_k is weighted by ``w = hann_cola(chunk)``
        and accumulated; the window itself is accumulated into ``wsum``;
      - the final output is ``sum_k w*y_k / sum_k w`` — a per-sample
        convex combination of the overlapping chunk predictions
        (weights sum to 1), so no seam click and no energy step;
      - the window is strictly positive, so single-coverage regions
        (track head/tail beyond any overlap partner) reproduce the one
        covering chunk exactly.
    Returns ``{stem_key: ndarray (n,) float64}`` with the exact input
    length ``n`` — every sample of the input is covered, edges included.
    """
    x = np.asarray(pcm, dtype=np.float32).reshape(-1)
    n = x.shape[0]
    chunk = max(1, int(round(chunk_sec * sr)))
    overlap = min(chunk - 1, max(0, int(round(overlap_sec * sr))))
    chunks = plan_chunks(n, chunk, overlap)
    if not chunks:
        return {}
    stems = None
    acc = None
    # float32 accumulators: half the RAM of float64 for full-track OLA
    # (a 5-min 6-stem track holds ~300 MB here, not ~600); the blend
    # stays exact well inside the tests' 1e-5 reconstruction tolerance
    wsum = np.zeros(n, dtype=np.float32)
    total = len(chunks)
    for done, (a, b) in enumerate(chunks, start=1):
        y = np.asarray(infer(x[a:b]), dtype=np.float32)
        if y.ndim == 3:  # (S, C, T) -> mono downmix
            y = y.mean(axis=1, dtype=np.float32)
        y = y.reshape(-1, b - a)
        if stems is None:  # first chunk fixes the stem count
            stems = [f"stem{i}" for i in range(y.shape[0])]
            acc = {k: np.zeros(n, dtype=np.float32) for k in stems}
        w = hann_cola(b - a).astype(np.float32)
        for k, row in zip(stems, y):
            acc[k][a:b] += w * row
        wsum[a:b] += w
        if progress is not None:
            progress(done, total)
    # wsum is strictly positive wherever any window covers (hann_cola has
    # no zeros), so plain division is safe; max() only guards empty tails
    return {k: acc[k] / np.maximum(wsum, np.float32(1e-30))
            for k in stems}


# ------------------------------------------------------------ separator

_SESSIONS: dict = {}
_SESSIONS_LOCK = threading.Lock()


class DemucsSeparator:
    """Chunked Demucs separation over one cached ONNX session.

    The session is a per-model-file SINGLETON (module-level cache): the
    weights stay loaded across requests — re-creating an
    ``InferenceSession`` per call would spike RAM and leak. Constructing
    without onnxruntime raises :class:`DLMissingError`; passing
    ``infer=`` replaces the session entirely (tests / alternate
    backends).
    """

    def __init__(self, variant: str = "demucs_4", infer=None,
                 threads: int | None = None):
        if variant not in STEM_SPECS:
            raise ValueError(
                f"未知 DL 分离方法: {variant} (可用: {', '.join(DL_METHODS)})")
        self.variant = variant
        self.stems = list(STEM_SPECS[variant])
        self._infer = infer
        self._session = None
        self._io = None
        if infer is None:
            if not ORT_AVAILABLE:
                raise DLMissingError(
                    "onnxruntime 未安装: 请安装 DL 依赖 (uv sync --extra dl)")
            path = self._resolve_model_file()
            self._session = _load_session(path, threads)
            self._io = _session_io(self._session)

    # -- model discovery -------------------------------------------------

    def _env(self, key: str) -> str:
        suffix = "4" if self.variant == "demucs_4" else "6"
        return os.environ.get(f"KEYPRISM_DEMUCS{suffix}_{key}", "").strip()

    def _resolve_model_file(self) -> Path:
        d = model_dir()
        explicit = self._env("FILE")
        if explicit:
            p = Path(explicit).expanduser()
            if p.is_file():
                return p
        spec = _VARIANTS[self.variant]
        p = d / spec["file"]
        if p.is_file():
            return p
        if d.is_dir():
            onnx = sorted(d.glob("*.onnx"))
            if onnx:
                return onnx[0]
        # last resort: auto-download from the variant's HF repo
        if not HF_AVAILABLE:
            raise DLMissingError(
                f"未找到 Demucs 模型且 huggingface_hub 未安装: 请将 ONNX 模型放到 "
                f"{d} 或安装 DL 依赖 (uv sync --extra dl)")
        repo = self._env("REPO") or spec["repo"]
        try:
            d.mkdir(parents=True, exist_ok=True)
            return Path(hf_hub_download(
                repo_id=repo, filename=spec["file"], local_dir=str(d)))
        except Exception as e:  # noqa: BLE001 - network/404/... all degrade
            raise DLMissingError(
                f"Demucs 模型下载失败 ({repo}/{spec['file']}): {e}; "
                f"可手动放置 ONNX 模型到 {d}") from e

    # -- inference ---------------------------------------------------------

    def _run_session(self, x: np.ndarray) -> np.ndarray:
        """One ONNX call: (T,) mono float32 in -> (S, T) float32 out."""
        sess, (name, in_ch) = self._session, self._io
        t = np.asarray(x, dtype=np.float32).reshape(1, -1)
        if in_ch == 2:  # model wants stereo; feed mono on both channels
            t = np.concatenate([t, t], axis=0).reshape(1, 2, -1)
        else:
            t = t.reshape(1, 1, -1)
        outs = sess.run(None, {name: t})
        return _normalize_output(outs, t.shape[-1])

    def separate(self, pcm: np.ndarray, sr: int, chunk_sec: float = 10.0,
                 overlap_sec: float = 1.0, *, progress=None) -> dict:
        """Mono PCM in, ``{stem: ndarray}`` out at :data:`TARGET_SR`.

        Resamples to 44.1 kHz (model rate) when needed, then streams
        chunk -> session -> Hann overlap-add (see :func:`ola_separate`
        for the blend math and the memory discipline)."""
        x = np.asarray(pcm, dtype=np.float32).reshape(-1)
        if x.shape[0] == 0:
            return {k: np.zeros(0) for k in self.stems}
        x = resample(x, sr, TARGET_SR)
        infer = self._infer if self._infer is not None else self._run_session
        out = ola_separate(
            x, infer, chunk_sec=chunk_sec, overlap_sec=overlap_sec,
            sr=TARGET_SR, progress=progress)
        return {k: out[f"stem{i}"] for i, k in enumerate(self.stems)}


def _normalize_output(outs, n_samples: int) -> np.ndarray:
    """Normalize whatever the export produced to ``(S, T)`` (mono)."""
    if isinstance(outs, (list, tuple)):
        arrs = [np.asarray(o) for o in outs if np.asarray(o).ndim >= 2]
        if len(arrs) == 1:
            arr = arrs[0]
        else:  # one tensor per stem: (1, C, T) or (C, T) each
            arr = np.stack([a.reshape(a.shape[-3:]).mean(axis=-2)
                            if a.ndim >= 2 else a.reshape(-1)
                            for a in arrs], axis=0)
    else:
        arr = np.asarray(outs)
    arr = np.squeeze(arr)
    if arr.ndim == 3:  # (S, C, T) -> mono
        arr = arr.mean(axis=1)
    if arr.ndim == 1:  # single-stem export
        arr = arr.reshape(1, -1)
    if arr.ndim != 2:
        raise ValueError(f"无法解析模型输出形状: {np.asarray(outs).shape}")
    return arr[..., :n_samples].astype(np.float32, copy=False)


def _load_session(path: Path, threads: int | None):
    """Cached session loader (singleton per resolved path+mtime)."""
    key = (str(path.resolve()), int(path.stat().st_mtime))
    with _SESSIONS_LOCK:
        hit = _SESSIONS.get(key)
        if hit is not None:
            return hit
        if threads is None:
            try:
                threads = int(os.environ.get("KEYPRISM_DL_THREADS", ""))
            except ValueError:
                threads = 0
        opts = ort.SessionOptions()
        opts.graph_optimization_level = \
            ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if threads and threads > 0:
            opts.intra_op_num_threads = threads
            opts.inter_op_num_threads = 1
        sess = ort.InferenceSession(str(path), sess_options=opts,
                                    providers=["CPUExecutionProvider"])
        _SESSIONS[key] = sess
        return sess


def _session_io(sess):
    inp = sess.get_inputs()[0]
    in_ch = 1
    shape = inp.shape or []
    if len(shape) >= 2 and isinstance(shape[1], int) and shape[1] > 0:
        in_ch = shape[1]
    return inp.name, in_ch


def get_separator(variant: str = "demucs_4", *, infer=None,
                  threads: int | None = None) -> DemucsSeparator:
    """Separator factory (session singleton lives inside the class)."""
    return DemucsSeparator(variant, infer=infer, threads=threads)


# ----------------------------------------------------------- stem cache

def dl_stems_dir(entry: Path, method: str) -> Path:
    """``<entry>/stems/<DL_STEMS_VERSION>/<method>`` — version-tagged."""
    _check_method(method)
    return Path(entry) / "stems" / DL_STEMS_VERSION / method


def dl_status_path(entry: Path, method: str) -> Path:
    return dl_stems_dir(entry, method) / "status.json"


def dl_stem_path(entry: Path, method: str, key: str) -> Path:
    if method not in STEM_SPECS or key not in STEM_SPECS[method]:
        raise KeyError(f"未知 stem: {method}/{key}")
    return dl_stems_dir(entry, method) / f"{key}.wav"


def _check_method(method: str) -> None:
    if method not in DL_METHODS:
        raise ValueError(
            f"未知 DL 分离方法: {method} (可用: {', '.join(DL_METHODS)})")


def load_status(entry: Path, method: str) -> dict | None:
    """Cached status dict when the method's DL stem files all exist."""
    _check_method(method)
    try:
        status = json.loads(
            dl_status_path(entry, method).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if status.get("version") != DL_STEMS_VERSION or \
            status.get("method") != method or \
            status.get("stems") != list(STEM_SPECS[method]):
        return None
    for key in STEM_SPECS[method]:
        if not dl_stem_path(entry, method, key).is_file():
            return None
    return status


def write_stems(entry: Path, method: str, stems: dict, sr: int,
                duration: float, elapsed_sec: float) -> dict:
    """Write ``{key: mono float array}`` as PCM16 WAVs + status.json,
    atomically (tmp dir -> os.replace, mirroring stems.py)."""
    import soundfile as sf

    _check_method(method)
    keys = STEM_SPECS[method]
    missing = [k for k in keys if k not in stems]
    if missing:
        raise ValueError(f"模型输出缺少 stem: {', '.join(missing)}")
    out_dir = dl_stems_dir(entry, method)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir.parent / f"{method}.tmp-{os.getpid()}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    try:
        for key in keys:
            y = np.clip(np.asarray(stems[key], dtype=np.float64), -1.0, 1.0)
            sf.write(str(tmp_dir / f"{key}.wav"), y, sr, subtype="PCM_16")
        status = {
            "version": DL_STEMS_VERSION,
            "method": method,
            "stems": list(keys),
            "sr": int(sr),
            "duration": round(float(duration), 6),
            "elapsed_sec": round(float(elapsed_sec), 3),
            "created_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
        }
        (tmp_dir / "status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2),
            encoding="utf-8")
        if out_dir.exists():
            shutil.rmtree(out_dir)
        os.replace(tmp_dir, out_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return status

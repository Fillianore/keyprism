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
variant spec. The demucs_6 default repo is ``StemSplitio/htdemucs-6s-onnx``
(the first real 6-stem ONNX export on the Hub — the 3.5 conclusion that
none existed was wrong; its output order is
``drums/bass/other/vocals/guitar/piano``, which
:data:`STEM_SPECS` mirrors). The download streams to ``<file>.part``
with a byte progress callback ``(bytes_done, bytes_total,
speed_mbps)`` — the server pipes it into the background-task registry
so the UI can show "downloading x.x / y.y MB" — and only ``os.replace``s
the ``.part`` file into place when complete, so an interrupted download
can never leave a corrupt cache entry. ``HF_ENDPOINT`` relocates the
endpoint (mirror networks). Every resolved model file gets a
provenance entry (repo id, revision, commit, license URL) merged into
``<model_dir>/provenance.json`` for the license audit — weights are
NEVER redistributed in-repo. Any Demucs ONNX export with a ``(1, C, T)``
float input and stems output is accepted; the I/O adapter normalizes
3D/4D, batched or listed outputs to ``(n_stems, T)`` mono.
"""

import ctypes
import json
import os
import shutil
import threading
import time
import urllib.request
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

# ------------------------------------------------- execution providers
#
# The ONNX session picks its execution provider (EP) through
# ``provider_chain``: a GPU preference probed against what the installed
# onnxruntime build ACTUALLY ships (``ort.get_available_providers()``).
# A plain ``[dl]`` extra installs CPU-only onnxruntime — the GPU builds
# are separate extras (``[dl-cuda]`` = onnxruntime-gpu, ``[dl-directml]``
# = onnxruntime-directml) — and a missing extra is a SILENT CPU
# fallback, never an error. ``KEYPRISM_ORT_PROVIDERS`` (comma list,
# order = preference, e.g. "CUDA,CPU") overrides the probe chain.

#: The CPU EP is always the last resort (every build ships it).
CPU_PROVIDER = "CPUExecutionProvider"

#: Default preference: NVIDIA CUDA, then DirectML (any-GPU on Windows),
#: then Apple CoreML.
_PROVIDER_PREFERENCE = (
    "CUDAExecutionProvider",
    "DmlExecutionProvider",
    "CoreMLExecutionProvider",
)

#: ``KEYPRISM_ORT_PROVIDERS`` accepts short names or raw ORT names,
#: case-insensitively.
_PROVIDER_ALIASES = {
    "cuda": "CUDAExecutionProvider",
    "cudaexecutionprovider": "CUDAExecutionProvider",
    "directml": "DmlExecutionProvider",
    "dml": "DmlExecutionProvider",
    "dmlexecutionprovider": "DmlExecutionProvider",
    "coreml": "CoreMLExecutionProvider",
    "coremlexecutionprovider": "CoreMLExecutionProvider",
    "cpu": "CPUExecutionProvider",
    "cpuexecutionprovider": "CPUExecutionProvider",
}


def ort_available_providers() -> list:
    """Providers compiled into the installed onnxruntime build
    (``[]`` without onnxruntime — probing never raises)."""
    if not ORT_AVAILABLE:
        return []
    try:
        return [str(p) for p in ort.get_available_providers()]
    except Exception:  # noqa: BLE001 - a broken install degrades to CPU
        return []


_PIP_CUDA_PRELOADED = False

#: Sonames the CUDA EP needs in-process: the DT_NEEDED list of
#: ``libonnxruntime_providers_cuda.so`` (cublas/cudart/curand) plus the
#: libraries ORT dlopens at runtime (cuDNN 9). cu13 wheels provide all
#: of them; a complete system CUDA 13 + cuDNN 9 install also does.
_CUDA13_SONAMES = (
    "libcublasLt.so.13",
    "libcublas.so.13",
    "libcudart.so.13",
    "libcurand.so.10",
    "libcudnn.so.9",
)

#: Driver CUDA runtime API version the cu13 wheels require (13.0),
#: as reported by ``cuDriverGetVersion`` (e.g. 13020 == "13.2").
_CUDA13_MIN_DRIVER = 13000


def _soname_loadable(soname: str) -> bool:
    """True when ``soname`` already resolves in this process environment
    (system CUDA / LD_LIBRARY_PATH / a previously loaded copy). Never
    raises — a missing library is exactly the False answer."""
    try:
        ctypes.CDLL(soname)
        return True
    except OSError:
        return False


def _cuda13_driver_ok() -> bool:
    """True when an NVIDIA driver is present and its CUDA runtime API
    version satisfies the cu13 wheels (WSL included: ``libcuda.so.1``
    is the host-driver shim there). Never raises — anything unusable
    (no driver, init failure, probe mismatch) is a plain False, and the
    caller then leaves the environment completely untouched."""
    try:
        lib = ctypes.CDLL("libcuda.so.1")
        if lib.cuInit(0) != 0:
            return False
        ver = ctypes.c_int(0)
        if lib.cuDriverGetVersion(ctypes.byref(ver)) != 0:
            return False
        return ver.value >= _CUDA13_MIN_DRIVER
    except Exception:  # noqa: BLE001 - no driver / probe broke → False
        return False


def _preload_pip_cuda_libs() -> None:
    """Fill missing CUDA/cuDNN runtime libraries from pip ``nvidia-*``
    wheels — and ONLY then.

    Since ORT 1.23 the GPU build targets CUDA 13, whose runtime
    libraries ship as pip wheels (``nvidia-cublas`` /
    ``nvidia-cuda-runtime`` / ``nvidia-curand`` / ``nvidia-cudnn-cu13``)
    instead of a system toolkit. Those wheels are NOT on the default
    dlopen search path: the CUDA EP's plugin then fails with
    ``libcublasLt.so.13: cannot open shared object file`` and the
    session silently degrades to CPU. ``ort.preload_dlls()`` (added in
    1.21) dlopens them from site-packages by absolute path so every
    later resolution — the plugin's DT_NEEDED list and the runtime
    ``dlopen("libcudnn.so.9")`` — succeeds.

    Pre-checks, in order (the answer to "don't clobber my environment"):

    1. **System environment wins.** When every required soname already
       resolves, nothing is preloaded at all — ORT keeps using the
       system CUDA exactly as installed. (dlopen dedupes by soname: a
       library already loaded can never be replaced, so preloading can
       only ever FILL gaps, never override a resolvable system library.
       With a partial system install the loaded system copies stay and
       pip wheels supply the rest — ABI-stable within one soname.)
    2. **Host must support CUDA 13.** Without an NVIDIA driver (or with
       one older than 13.0) the cu13 wheels are dead weight either way:
       skip the preload and leave the CPU fallback / any system CUDA 12
       stack untouched.

    Guarded throughout (no ORT / pre-1.21 ORT / any probe or preload
    error): a missing GPU stack must degrade to CPU, never raise. Runs
    once per process — probes and dlopens are cheap but pointless to
    repeat, and the answer cannot change mid-process."""
    global _PIP_CUDA_PRELOADED
    if _PIP_CUDA_PRELOADED or not ORT_AVAILABLE:
        return
    _PIP_CUDA_PRELOADED = True
    try:
        if not hasattr(ort, "preload_dlls"):
            return
        if all(_soname_loadable(s) for s in _CUDA13_SONAMES):
            return  # complete system CUDA: respect it, touch nothing
        if not _cuda13_driver_ok():
            return  # no / too-old NVIDIA driver: wheels would be dead weight
        ort.preload_dlls()
    except Exception:  # noqa: BLE001 - no CUDA deps → CPU fallback
        pass


def provider_chain(available: list | None = None,
                   override: str | None = None) -> list:
    """The provider list handed to ``InferenceSession`` (ordered).

    Default: CUDA -> DirectML -> CoreML, intersected with ``available``
    (``ort.get_available_providers()`` when not given), and
    ``CPUExecutionProvider`` ALWAYS appended at the END (3.10.2 D1:
    with CPU in the list, ORT runs any op that lacks a GPU kernel on
    CPU instead of failing the whole session with CUDA error 9 /
    NOT_IMPLEMENTED on e.g. a Conv node). ``KEYPRISM_ORT_PROVIDERS=
    "CUDA,CPU"`` replaces the preference part (comma list, order =
    preference; short names or raw ORT names); tokens naming providers
    this build lacks are silently dropped. ``override=`` is the test
    seam (wins over the env var)."""
    if available is None:
        available = ort_available_providers()
    avset = {str(p) for p in available}
    if override is None:
        override = os.environ.get("KEYPRISM_ORT_PROVIDERS", "")
    prefs = []
    for token in str(override or "").split(","):
        token = token.strip()
        if token:
            prefs.append(_PROVIDER_ALIASES.get(token.lower(), token))
    if not prefs:
        prefs = list(_PROVIDER_PREFERENCE)
    chain = []
    for p in prefs:
        if p in avset and p not in chain:
            chain.append(p)
    if CPU_PROVIDER not in chain:
        chain.append(CPU_PROVIDER)
    return chain


#: ACTIVE providers of the most recently loaded session, read back via
#: ``session.get_providers()``: an EP can be REQUESTED yet partially
#: fall back to CPU (demucs' embedded STFT ops may run on CPU), so the
#: ping capability reports this readback, not the requested chain.
_ACTIVE_PROVIDERS: list = []
_ACTIVE_LOCK = threading.Lock()


def active_providers() -> list:
    """The EP chain to advertise via /api/ping: the loaded session's
    ``get_providers()`` readback once a model has been loaded, else the
    selected (requested) chain."""
    with _ACTIVE_LOCK:
        if _ACTIVE_PROVIDERS:
            return list(_ACTIVE_PROVIDERS)
    return provider_chain()


# -------------------------------------------------- device selector (3.10.2)

#: Values accepted by /api/stems ``device`` (and by the frontend
#: Device dropdown).
DEVICE_CHOICES = ("auto", "gpu", "cpu")


def providers_for_device(device: str, available: list | None = None) -> list:
    """Device-selector routing -> the provider list for
    ``InferenceSession``.

    ``auto``: the probe chain (:func:`provider_chain` — GPU preference,
    KEYPRISM_ORT_PROVIDERS override, CPU always last). ``cpu``: forced
    pure CPU, bypassing all GPU probing. ``gpu``: force the first
    available GPU EP by preference (CUDA, then DirectML, then CoreML)
    with CPU appended; when the build ships no GPU EP at all this raises
    ValueError("GPU requested but no GPU provider available.") which the
    server maps to a 400 (the frontend also disables the GPU option from
    ``/api/ping``'s available list). A forced gpu/cpu choice wins over
    the KEYPRISM_ORT_PROVIDERS env override. ``available=`` is the test
    seam."""
    if available is None:
        available = ort_available_providers()
    avset = {str(p) for p in available}
    if device == "cpu":
        return [CPU_PROVIDER]
    if device == "gpu":
        gpus = [p for p in _PROVIDER_PREFERENCE if p in avset]
        if not gpus:
            raise ValueError(
                "GPU requested but no GPU provider available. "
                f"(可用执行提供者: {', '.join(sorted(avset)) or '无'})")
        return [gpus[0], CPU_PROVIDER]
    return provider_chain(available=available)

try:  # optional [dl] extra: its presence gates the auto-download path
    import huggingface_hub  # noqa: F401  (availability flag only)

    HF_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via the flag in tests
    HF_AVAILABLE = False

from . import audio_io, payload

__all__ = [
    "DL_STEMS_VERSION", "DL_METHODS", "STEM_SPECS", "TARGET_SR",
    "SEGMENT_SEC", "SEGMENT_SAMPLES",
    "QUALITY_SHIFTS", "DEFAULT_QUALITY",
    "CPU_PROVIDER", "ORT_AVAILABLE", "HF_AVAILABLE",
    "DLMissingError", "TaskCancelled",
    "plan_chunks", "chunk_windows", "hann_cola", "resample",
    "ola_separate", "shift_passes", "shifts_for_quality",
    "provider_chain", "ort_available_providers", "active_providers",
    "DEVICE_CHOICES", "providers_for_device",
    "DemucsSeparator", "get_separator", "dl_stems_dir", "dl_status_path",
    "dl_stem_path", "load_status", "write_stems", "model_dir",
    "dl_spec_path", "get_stem_spec",
]

#: Version tag of the DL stem pipeline; part of the cache path (bump to
#: invalidate previously cached DL stems everywhere).
DL_STEMS_VERSION = "dl_v1"

#: Query values accepted by /api/stems for DL separation.
DL_METHODS = ("demucs_4", "demucs_6")

#: method -> stem keys in the CANONICAL OUTPUT ORDER of the reference
#: ONNX exports (the separator zips model output rows with this list, so
#: the order must match the graph's source order, not a UI preference):
#: StemSplitio/htdemucs-6s-onnx emits
#: ``drums/bass/other/vocals/guitar/piano`` (guitar BEFORE piano — 3.5
#: shipped the reversed order against the mislabelled smank export and
#: never got past the zero-probe, so no valid demucs_6 cache can exist).
#: These names are also the file names under
#: <entry>/stems/<DL_STEMS_VERSION>/<method>/.
STEM_SPECS = {
    "demucs_4": ("drums", "bass", "other", "vocals"),
    "demucs_6": ("drums", "bass", "other", "vocals", "guitar", "piano"),
}

#: Demucs models are trained at 44.1 kHz; input is resampled, stems are
#: written at this rate (the frontend schedules by seconds, so mixed
#: sample rates across mix/stay lanes stay sample-accurate).
TARGET_SR = 44100

#: The reference ONNX export family fixes the INTERNAL segment length
#: (7.8 s @ 44.1 kHz = 343980 samples; the STFT/iSTFT lives inside the
#: graph and its reflect pads are sized for exactly this input length —
#: any other length fails with a Pad/Reshape error). Session-based
#: separation therefore runs the generic chunker at exactly this chunk
#: size with a 50% overlap (the periodic Hann is then COLA-exact:
#: fade_out + fade_in == 1 at every sample) and zero-pads the single
#: short chunk a sub-segment track produces (a track tail is genuinely
#: silent — padding is exact there). Override with KEYPRISM_DL_SEGMENT
#: (samples) for an export with a different fixed segment.
SEGMENT_SEC = 7.8
SEGMENT_SAMPLES = 343980

#: Variant specs: Hugging Face repo + file for the auto-download path,
#: plus the license pointer recorded into the model-cache provenance
#: metadata (the 3.11 license audit; weights are never redistributed
#: in-repo). Repos are env-overridable (KEYPRISM_DEMUCS4_REPO/_FILE and
#: the 6-stem twins) because ONNX exports of htdemucs/htdemucs_6s are
#: community artifacts; any compatible export can be dropped into the
#: model dir. Graph contract of the default repos: input
#: ``mix [1, 2, T]`` float32, single output ``sources|stems
#: [1, n_stems, 2, T]`` (STFT/iSTFT embedded, opset 17) — exactly what
#: ``_normalize_output`` accepts. The previous 4-stem default
#: (Xenova/htdemucs-onnx) no longer exists on the Hub; the previous
#: 6-stem default (smank's ``htdemucs_6s.onnx``) actually shipped 4
#: sources and was replaced in 3.10 by the real 6-stem export.
_VARIANTS = {
    "demucs_4": {
        "repo": "smank/htdemucs-onnx",
        "file": "htdemucs.onnx",
        "license": "unknown (community export; see repo)",
        "license_url": "https://huggingface.co/smank/htdemucs-onnx/blob/main/LICENSE",
    },
    "demucs_6": {
        "repo": "StemSplitio/htdemucs-6s-onnx",
        "file": "htdemucs_6s.onnx",
        "license": "mit",
        "license_url":
            "https://huggingface.co/StemSplitio/htdemucs-6s-onnx/blob/main/LICENSE",
    },
}

#: Raised when the optional DL dependencies or the model weights are
#: missing (the server maps this to 501 / a clear error, never a 500).
class DLMissingError(RuntimeError):
    pass


#: Raised inside a background job when its cooperative cancel flag was
#: observed (download loop: per byte-chunk; inference: per chunk /
#: per shift pass). The server maps it to task status "cancelled" —
#: partial .part downloads are removed and no stem cache is written.
#: NOTE: ort ``session.run()`` itself is uninterruptible; cancellation
#: takes effect at the NEXT chunk/pass boundary (at most one fixed
#: model segment, ~7.8 s, later).
class TaskCancelled(RuntimeError):
    pass


def model_dir() -> Path:
    """DL model directory under the (runtime) workspace."""
    return audio_io.KEYPRISM_HOME / "models" / "demucs"


def _hf_endpoint() -> str:
    """Hugging Face base URL (``HF_ENDPOINT`` relocates it; mirror
    networks are the documented workaround for an unreachable Hub)."""
    return os.environ.get("HF_ENDPOINT", "").rstrip("/") or \
        "https://huggingface.co"


def _download_to(url: str, dest: Path, progress=None,
                 urlopen=None, meta: dict | None = None,
                 cancelled=None) -> Path:
    """Stream ``url`` to ``dest`` via a ``<dest>.part`` temp file.

    Byte progress is reported as ``progress(bytes_done, bytes_total,
    speed_mbps)`` at most every ~200 ms (speed is an EWMA of the
    per-interval rate, so the display stays readable through bursts).
    The bytes land in ``.part`` and are ``os.replace``d into place only
    on success — an interrupted download (crash, Ctrl+C, network drop)
    removes the partial file and can never poison the model cache with
    a truncated ONNX. ``urlopen`` is the injection seam for tests (same
    pattern as ``infer=``); the real path uses stdlib urllib, keeping
    the downloader dependency-free. When ``meta`` is given, the
    response's provenance headers (``ETag``, ``X-Repo-Commit`` — both
    set by the HF resolve endpoint) are recorded into it for the model
    provenance metadata. ``cancelled`` is the cooperative cancel probe
    (3.10.3): checked per byte-chunk, raising :class:`TaskCancelled`
    (the ``.part`` file is removed by the existing cleanup path)."""
    opener = urlopen or urllib.request.urlopen
    part = dest.with_name(dest.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url, headers={"User-Agent": "keyprism_dlsep"})
    speed = 0.0
    last_done = 0
    last_t = time.time()
    try:
        with opener(req, timeout=30) as resp, open(part, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            if meta is not None:
                for h in ("ETag", "X-Repo-Commit"):
                    v = resp.headers.get(h)
                    if v:
                        meta[{"ETag": "etag",
                              "X-Repo-Commit": "commit"}[h]] = v
            done = 0
            while True:
                if cancelled is not None and cancelled():
                    raise TaskCancelled(
                        "模型下载已取消: " + url)
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                dt = now - last_t
                if progress is not None and dt >= 0.2:
                    inst = ((done - last_done) / 1e6 / dt
                            if dt > 0 and done > last_done else 0.0)
                    speed = inst if speed <= 0 else \
                        0.7 * speed + 0.3 * inst
                    last_t, last_done = now, done
                    progress(done, total, speed)
        os.replace(part, dest)  # atomic: dest is never partial
        if meta is not None:
            meta["bytes"] = done
        if progress is not None:  # final 100% sample
            progress(done, total, speed)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return dest


def _provenance_path(model_file: Path) -> Path:
    """Model-cache provenance metadata: ONE ``provenance.json`` in the
    model dir, keyed by file name (the 3.11 license audit reads repo id,
    revision, commit and license URL from here — the weights themselves
    are never redistributed in-repo)."""
    return model_file.parent / "provenance.json"


def _record_provenance(model_file: Path, *, variant: str,
                       repo: str | None, revision: str,
                       license_name: str | None, license_url: str | None,
                       source: str, source_url: str | None = None,
                       meta: dict | None = None) -> None:
    """Merge one entry into ``provenance.json`` (atomic write, never
    fatal: provenance bookkeeping must not break model resolution).

    ``source`` distinguishes how the file arrived: ``auto-download``,
    ``cache`` (previously downloaded, resolved from the variant spec's
    path — the original download facts are preserved and only the
    recorded_at timestamp refreshes), ``env:<VAR>`` (explicit override
    path) or ``model-dir glob`` (an anonymous local ONNX picked up by
    the fallback; repo facts are unknown there)."""
    path = _provenance_path(model_file)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(body, dict):
            body = {}
    except (OSError, ValueError):
        body = {}
    entry = {
        "variant": variant,
        "repo": repo,
        "revision": revision,
        "file": model_file.name,
        "license": license_name,
        "license_url": license_url,
        "source": source,
        "bytes": int(model_file.stat().st_size),
        "recorded_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
    }
    if source_url:
        entry["source_url"] = source_url
    if meta:
        for k in ("etag", "commit"):
            if meta.get(k):
                entry[k] = meta[k]
    if source == "cache":
        prev = body.get(model_file.name)
        if isinstance(prev, dict):
            # keep the ORIGINAL download facts (etag/commit/source_url);
            # only prove the file was re-resolved under this variant
            entry.update({k: prev[k] for k in
                          ("etag", "commit", "source_url", "downloaded_at")
                          if prev.get(k)})
    body[model_file.name] = entry
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # read-only workspace etc. — provenance is best-effort


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
                 progress=None, cancelled=None) -> dict:
    """Chunked inference + Hann overlap-add blending (pure numpy).

    ``pcm`` is mono float32 at ``sr``; ``infer(x: (T,) float32)`` must
    return either ``(n_stems, T)`` or ``(n_stems, 1, T)``-like model
    output for the SAME samples it received (this is the injection seam
    the tests use instead of a real ONNX session).

    ``cancelled`` (3.10.3) is the cooperative cancel probe: checked
    before each chunk, raising :class:`TaskCancelled`. ort ``run()`` is
    uninterruptible, so this bounds the abort latency at one chunk.

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
    chunks = chunk_windows(x.shape[0], chunk_sec, overlap_sec, sr)
    if not chunks:
        return {}
    stems = None
    acc = None
    # float32 accumulators: half the RAM of float64 for full-track OLA
    # (a 5-min 6-stem track holds ~300 MB here, not ~600); the blend
    # stays exact well inside the tests' 1e-5 reconstruction tolerance
    wsum = np.zeros(x.shape[0], dtype=np.float32)
    total = len(chunks)
    for done, (a, b) in enumerate(chunks, start=1):
        if cancelled is not None and cancelled():
            raise TaskCancelled(
                f"分离已取消 (chunk {done}/{total})")
        y = np.asarray(infer(x[a:b]), dtype=np.float32)
        if y.ndim == 3:  # (S, C, T) -> mono downmix
            y = y.mean(axis=1, dtype=np.float32)
        y = y.reshape(-1, b - a)
        if stems is None:  # first chunk fixes the stem count
            stems = [f"stem{i}" for i in range(y.shape[0])]
            acc = {k: np.zeros(x.shape[0], dtype=np.float32) for k in stems}
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


def chunk_windows(n: int, chunk_sec: float, overlap_sec: float,
                  sr: int) -> list:
    """Sample-space chunk plan (``(start, end)`` windows): the ONE math
    shared by :func:`ola_separate` and the shifts wrapper in
    ``DemucsSeparator.separate`` (which needs the chunk count up front to
    scale the progress denominator with the pass count). Identical to
    what ola_separate computes internally — locked by the chunking
    tests."""
    chunk = max(1, int(round(chunk_sec * sr)))
    overlap = min(chunk - 1, max(0, int(round(overlap_sec * sr))))
    return plan_chunks(n, chunk, overlap)


# ------------------------------------------------- quality tiers (3.10)

#: Inference quality tiers: demucs' ``shifts`` augmentation applied
#: EXTERNALLY around the per-chunk infer (the ONNX graph is untouched):
#: for each extra pass the input chunk is circularly shifted, inferred,
#: shifted back, and all passes are averaged. fast = 1 pass (no
#: shifts), balanced = 2 passes, best = 3 — compute scales ~linearly
#: with the pass count, chunking/OLA math is untouched.
QUALITY_SHIFTS = {"fast": 0, "balanced": 1, "best": 2}

DEFAULT_QUALITY = "balanced"


def shifts_for_quality(quality: str) -> int:
    """Quality tier name -> shift count (0/1/2 extra passes)."""
    try:
        return QUALITY_SHIFTS[quality]
    except (KeyError, TypeError):
        raise ValueError(
            f"未知质量档位: {quality} "
            f"(可用: {', '.join(QUALITY_SHIFTS)})") from None


def shift_passes(infer, x: np.ndarray, passes: int,
                 on_pass=None) -> np.ndarray:
    """demucs ``shifts`` augmentation around ONE infer call.

    For pass ``s`` the input is circularly shifted by an evenly spaced
    offset (``s/passes`` of the length), inferred, shifted back, and the
    passes are averaged (float32). The wrapping is external to the graph
    (chunking/OLA untouched); a linear, shift-equivariant backend
    returns its single-pass output unchanged, so the equivalence is
    testable with an injected linear fake-infer. ``on_pass(p, t)`` fires
    after each completed pass (per-pass progress)."""
    x = np.asarray(x, dtype=np.float32)
    n = x.shape[-1]
    passes = int(passes)
    outs = []
    for s in range(passes):
        k = (n * s) // passes
        y = np.asarray(infer(np.roll(x, -k, axis=-1) if k else x),
                       dtype=np.float32)
        if k:
            y = np.roll(y, k, axis=-1)
        outs.append(y)
        if on_pass is not None:
            on_pass(s + 1, passes)
    if len(outs) == 1:
        return outs[0]
    return np.mean(np.stack(outs, axis=0), axis=0, dtype=np.float32)


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
                 threads: int | None = None,
                 download_progress=None,
                 device_providers: list | None = None,
                 cancelled=None):
        if variant not in STEM_SPECS:
            raise ValueError(
                f"未知 DL 分离方法: {variant} (可用: {', '.join(DL_METHODS)})")
        self.variant = variant
        self.stems = list(STEM_SPECS[variant])
        self._infer = infer
        self._session = None
        self._io = None
        # Providers the CURRENT session was built for (the runtime
        # EP-failure retry only fires while a GPU EP is still in play)
        self._session_providers: list = []
        # Fixed model segment length in samples (0 = unknown/flexible:
        # injected infer backends accept whatever the chunker produces)
        self._segment = 0
        # Device-selector routing (3.10.2): forced provider list from
        # providers_for_device (None = auto / probe chain)
        self._device_providers = \
            list(device_providers) if device_providers else None
        # Cooperative cancel probe (3.10.3): checked per byte-chunk of a
        # model download and handed to separate() for the inference loops
        self._cancelled = cancelled
        self._path = None
        self._threads = threads
        if infer is None:
            if not ORT_AVAILABLE:
                raise DLMissingError(
                    "onnxruntime 未安装: 请安装 DL 依赖 (uv sync --extra dl)")
            path = self._resolve_model_file(
                download_progress=download_progress,
                cancelled=cancelled)
            self._path = path
            self._session = _load_session(path, threads,
                                          providers=self._device_providers)
            self._session_providers = list(self._device_providers) \
                if self._device_providers else provider_chain()
            self._io = _session_io(self._session)
            try:
                self._segment = int(
                    os.environ.get("KEYPRISM_DL_SEGMENT", ""))
            except ValueError:
                self._segment = SEGMENT_SAMPLES
            self._probe()

    # -- model discovery -------------------------------------------------

    def _env(self, key: str) -> str:
        suffix = "4" if self.variant == "demucs_4" else "6"
        return os.environ.get(f"KEYPRISM_DEMUCS{suffix}_{key}", "").strip()

    def _record(self, path: Path, *, source: str, spec: dict | None = None,
                repo: str | None = None, source_url: str | None = None,
                meta: dict | None = None) -> None:
        """Provenance bookkeeping for one resolved model file (never
        fatal — a read-only workspace must still separate)."""
        if spec is None:
            license_name = license_url = None
        else:
            license_name = spec.get("license")
            license_url = spec.get("license_url")
        try:
            _record_provenance(
                path, variant=self.variant, repo=repo, revision="main",
                license_name=license_name, license_url=license_url,
                source=source, source_url=source_url, meta=meta)
        except OSError:
            pass

    def _resolve_model_file(self, download_progress=None,
                            cancelled=None) -> Path:
        d = model_dir()
        spec = _VARIANTS[self.variant]
        explicit = self._env("FILE")
        if explicit:
            p = Path(explicit).expanduser()
            if p.is_file():
                self._record(p, source=f"env:KEYPRISM_DEMUCS"
                                       f"{'4' if self.variant == 'demucs_4' else '6'}"
                                       f"_FILE", spec=spec,
                             repo=self._env("REPO") or None)
                return p
        p = d / spec["file"]
        if p.is_file():
            # previously downloaded (or hand-placed at the spec'd name):
            # provenance merges into the original download entry
            self._record(p, source="cache", spec=spec, repo=spec["repo"])
            return p
        # auto-download BEFORE the anonymous glob fallback: the model dir
        # may hold an UNRELATED export under a different name (first real
        # 3.10 deployment: a 4-stem htdemucs.onnx from an earlier
        # demucs_4 install), and the glob would serve it — the zero-probe
        # then rejects it with a confusing stem-count error instead of
        # ever fetching the right model. The glob stays as the manual
        # escape hatch (offline hosts, custom exports) and the failure
        # path of the download.
        last_err: Exception | None = None
        if HF_AVAILABLE:
            repo = self._env("REPO") or spec["repo"]
            url = f"{_hf_endpoint()}/{repo}/resolve/main/{spec['file']}"
            meta: dict = {}
            try:
                dest = _download_to(url, d / spec["file"],
                                    progress=download_progress, meta=meta,
                                    cancelled=cancelled)
            except Exception as e:  # noqa: BLE001 - network/404/... degrade
                if isinstance(e, TaskCancelled):
                    raise
                last_err = e
            else:
                self._record(dest, source="auto-download", spec=spec,
                             repo=repo, source_url=url, meta=meta)
                return dest
        if d.is_dir():
            onnx = sorted(d.glob("*.onnx"))
            if onnx:
                self._record(onnx[0], source="model-dir glob")
                return onnx[0]
        if not HF_AVAILABLE:
            raise DLMissingError(
                f"未找到 Demucs 模型且 huggingface_hub 未安装: 请将 ONNX 模型放到 "
                f"{d} 或安装 DL 依赖 (uv sync --extra dl)")
        raise DLMissingError(
            f"Demucs 模型下载失败 ({spec['repo']}/{spec['file']}): "
            f"{last_err}; 可手动放置 ONNX 模型到 {d}")

    # -- inference ---------------------------------------------------------

    def _probe(self) -> None:
        """One zero-input probe validating the graph contract up front.

        The two known bad-export failure modes — a segment length the
        embedded reflect pads reject, and an output stem count that
        disagrees with the variant registry (e.g. a mislabelled
        ``htdemucs_6s`` shipping 4 sources) — surface here as immediate,
        actionable errors instead of a background task failing ~40 s
        into the separation. Costs one silent inference (~2 s CPU) per
        model load."""
        if self._segment <= 0:
            return
        try:
            y = self._run_session(np.zeros(self._segment,
                                           dtype=np.float32))
        except Exception as e:  # noqa: BLE001 - surface with context
            raise ValueError(
                f"ONNX 模型与固定分段不兼容 "
                f"({self._segment} 样本): {e}; 可用 KEYPRISM_DL_SEGMENT "
                f"或 KEYPRISM_DEMUCS{4 if self.variant == 'demucs_4' else 6}"
                f"_FILE 指定兼容导出") from e
        if y.shape[0] != len(self.stems):
            raise ValueError(
                f"模型输出 {y.shape[0]} 个 stem, 但 {self.variant} 需要 "
                f"{len(self.stems)} (该 ONNX 导出与 {self.variant} 不匹配; "
                f"可用 KEYPRISM_DEMUCS"
                f"{4 if self.variant == 'demucs_4' else 6}_FILE 指定兼容导出)")

    def _run_session(self, x: np.ndarray) -> np.ndarray:
        """One ONNX call: (T,) mono float32 in -> (S, T) float32 out.

        When the export fixes the segment length (``self._segment``), a
        shorter input is zero-padded up to the segment (see
        :func:`_pad_segment`) and the output trimmed back to the valid
        region.

        Runtime EP-failure retry (3.10.2 D1): a GPU EP can load fine yet
        fail on FIRST INFERENCE when a node lacks a kernel for it (CUDA
        error 9 / NOT_IMPLEMENTED on e.g. a Conv node) — the failure
        surfaces at ``session.run()``, not at session creation. While a
        GPU EP is still in play, such a failure evicts the cached
        session and rebuilds it pure-CPU once, then retries the call;
        the rest of the task (and every later request) runs on CPU."""
        try:
            return self._session_run(x)
        except Exception as e:  # noqa: BLE001 - classified below
            if not self._retry_cpu_on_ep_failure(e):
                raise
            return self._session_run(x)

    def _retry_cpu_on_ep_failure(self, e: Exception) -> bool:
        """True when the failure looks like a GPU EP falling short at
        run time ("NOT IMPLEMENTED" / "CUDA" in the message) AND the
        current session still targets a GPU EP — in that case rebuild
        the session pure-CPU (evicting the singleton) and let the caller
        retry once. A pure-CPU session has nowhere to fall back to, so
        its errors are re-raised verbatim."""
        msg = str(e).lower()
        if "not implemented" not in msg and "cuda" not in msg:
            return False
        if not any(p != CPU_PROVIDER for p in self._session_providers):
            return False
        print(f"[dlsep] GPU 执行提供者推理失败, 回退到纯 CPU 重建会话 "
              f"({type(e).__name__}: {e})", flush=True)
        self._session = _load_session(self._path, threads=self._threads,
                                      providers=[CPU_PROVIDER],
                                      force_reload=True)
        self._session_providers = [CPU_PROVIDER]
        return True

    def _session_run(self, x: np.ndarray) -> np.ndarray:
        sess, (name, in_ch) = self._session, self._io
        x, valid = _pad_segment(x, self._segment)
        t = x.reshape(1, -1)
        if in_ch == 2:  # model wants stereo; feed mono on both channels
            t = np.concatenate([t, t], axis=0).reshape(1, 2, -1)
        else:
            t = t.reshape(1, 1, -1)
        outs = sess.run(None, {name: t})
        y = _normalize_output(outs, t.shape[-1])
        return y[..., :valid] if valid else y

    def separate(self, pcm: np.ndarray, sr: int, chunk_sec: float | None = None,
                 overlap_sec: float | None = None, *, shifts: int | None = None,
                 progress=None, cancelled=None) -> dict:
        """Mono PCM in, ``{stem: ndarray}`` out at :data:`TARGET_SR`.

        Resamples to 44.1 kHz (model rate) when needed, then streams
        chunk -> session -> Hann overlap-add (see :func:`ola_separate`
        for the blend math and the memory discipline).

        Chunking defaults depend on the backend: when the model fixes a
        segment length (:data:`SEGMENT_SAMPLES`, set for session-based
        separators), chunks are EXACTLY that segment with a 50% overlap
        (COLA-exact cross-fade) because the reference exports embed the
        STFT and only accept that input length; generic backends
        (injected ``infer``, flexible exports) keep the 10 s / 1 s
        defaults. Explicit arguments always win.

        ``shifts`` (demucs quality tiers, 3.10): 0 extra passes (fast,
        default), 1 (balanced) or 2 (best); see :func:`shift_passes`.
        The wrapping is external to the per-chunk infer — chunking/OLA
        math is untouched — and the progress denominator scales with the
        pass count (per-pass updates).

        ``cancelled`` (3.10.3): cooperative cancel probe, checked per
        shift pass and per chunk (see :func:`ola_separate` — ort
        ``run()`` itself is uninterruptible)."""
        x = np.asarray(pcm, dtype=np.float32).reshape(-1)
        if x.shape[0] == 0:
            return {k: np.zeros(0) for k in self.stems}
        if self._segment > 0:
            d_chunk = self._segment / float(TARGET_SR)  # exact segment
            d_overlap = d_chunk / 2.0                   # 50%: COLA-exact
        else:
            d_chunk, d_overlap = 10.0, 1.0
        if chunk_sec is None:
            chunk_sec = d_chunk
        if overlap_sec is None:
            overlap_sec = d_overlap
        if shifts is None:
            shifts = 0
        shifts = int(shifts)
        if shifts < 0:
            raise ValueError("shifts 必须 >= 0")
        passes = shifts + 1
        x = resample(x, sr, TARGET_SR)
        base = self._infer if self._infer is not None else self._run_session
        if passes > 1:
            # shifts wrap the per-chunk infer (OLA untouched); the
            # progress denominator scales with the pass count and every
            # completed pass advances it (progress granularity = pass)
            total_units = len(chunk_windows(
                x.shape[0], chunk_sec, overlap_sec, TARGET_SR)) * passes
            counter = {"done": 0}

            def infer(cx: np.ndarray) -> np.ndarray:
                def on_pass(_p: int, _t: int) -> None:
                    counter["done"] += 1
                    if cancelled is not None and cancelled():
                        raise TaskCancelled(
                            f"分离已取消 (pass {_p}/{_t})")
                    if progress is not None:
                        progress(counter["done"], total_units)

                return shift_passes(base, cx, passes, on_pass=on_pass)

            out = ola_separate(
                x, infer, chunk_sec=chunk_sec, overlap_sec=overlap_sec,
                sr=TARGET_SR, cancelled=cancelled)
        else:
            out = ola_separate(
                x, base, chunk_sec=chunk_sec, overlap_sec=overlap_sec,
                sr=TARGET_SR, progress=progress, cancelled=cancelled)
        missing = [i for i in range(len(self.stems))
                   if f"stem{i}" not in out]
        if missing:
            raise ValueError(
                f"模型输出缺少 stem {missing} "
                f"(该 ONNX 导出与 {self.variant} 不匹配)")
        return {k: out[f"stem{i}"] for i, k in enumerate(self.stems)}


def _pad_segment(x: np.ndarray, seg: int) -> tuple:
    """Zero-pad a short chunk up to the fixed model segment.

    Returns ``(padded, valid)``: ``padded`` is the input to feed the
    model, ``valid`` the number of leading samples that carry real
    content (== ``len(x)`` when padding happened, 0 when none was
    needed). A sub-segment track tail is genuinely silent, so the
    padding is exact — the output is trimmed back to ``valid``."""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    if seg <= 0 or x.shape[0] >= seg:
        return x, 0
    valid = x.shape[0]
    return np.concatenate(
        [x, np.zeros(seg - valid, dtype=np.float32)]), valid


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


#: Singleton sessions keyed by (path, mtime, providers tuple): the
#: providers are part of the key so a device switch (auto vs forced
#: CPU) can never silently reuse a session built for another chain.
_SESSION_META: dict = {}


def _load_session(path: Path, threads: int | None = None,
                  providers: list | None = None,
                  force_reload: bool = False):
    """Cached session loader (singleton per path+mtime+providers).

    ``providers`` overrides :func:`provider_chain` (the Device selector's
    forced gpu/cpu routing); ``force_reload`` evicts every session for
    this path first (the runtime EP-failure retry path). Providers come
    from :func:`provider_chain` when not given — GPU preference
    intersected with the installed build, CPU always last. When a
    requested GPU EP makes session creation fail anyway (driver/runtime
    mismatch — the EP can be compiled in yet unusable on this host), the
    load silently retries CPU-only: a missing/broken GPU stack must
    never crash the server. The ACTIVE provider list
    (``session.get_providers()`` — individual ops may still fall back to
    CPU inside the graph) is recorded for :func:`active_providers` /
    the /api/ping capability."""
    if not providers:
        providers = provider_chain()
    else:
        providers = list(providers)
    resolved = str(path.resolve())
    key = (resolved, int(path.stat().st_mtime), tuple(providers))
    with _SESSIONS_LOCK:
        if force_reload:
            for k in [k for k in _SESSIONS if k[0] == resolved]:
                _SESSIONS.pop(k, None)
                _SESSION_META.pop(k, None)
        hit = _SESSIONS.get(key)
        if hit is not None:
            return hit
        if threads is None:
            try:
                threads = int(os.environ.get("KEYPRISM_DL_THREADS", ""))
            except ValueError:
                threads = 0
        _preload_pip_cuda_libs()
        opts = ort.SessionOptions()
        opts.graph_optimization_level = \
            ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if threads and threads > 0:
            opts.intra_op_num_threads = threads
            opts.inter_op_num_threads = 1
        try:
            sess = ort.InferenceSession(str(path), sess_options=opts,
                                        providers=providers)
        except Exception:  # noqa: BLE001 - GPU advertised but unusable
            if providers == [CPU_PROVIDER]:
                raise
            print(f"[dlsep] 会话创建在 {providers} 上失败, 回退纯 CPU",
                  flush=True)
            sess = ort.InferenceSession(str(path), sess_options=opts,
                                        providers=[CPU_PROVIDER])
        with _ACTIVE_LOCK:
            _ACTIVE_PROVIDERS[:] = list(sess.get_providers())
        # 3.10.4 D4: make the EP chain observable in the backend log —
        # requested vs ACTIVE (ops may fall back to CPU inside the
        # graph), so a device switch shows the rebuilt session's chain
        print(f"[dlsep] ONNX 会话 {path.name}: 请求 {providers}, "
              f"激活 {list(sess.get_providers())}", flush=True)
        _SESSIONS[key] = sess
        _SESSION_META[key] = {"providers": list(providers)}
        return sess


def _session_io(sess):
    inp = sess.get_inputs()[0]
    in_ch = 1
    shape = inp.shape or []
    if len(shape) >= 2 and isinstance(shape[1], int) and shape[1] > 0:
        in_ch = shape[1]
    return inp.name, in_ch


def get_separator(variant: str = "demucs_4", *, infer=None,
                  threads: int | None = None,
                  download_progress=None,
                  device_providers: list | None = None,
                  cancelled=None) -> DemucsSeparator:
    """Separator factory (session singleton lives inside the class).

    ``download_progress(bytes_done, bytes_total, speed_mbps)`` is only
    consulted on the real (session-based) path when the model weights
    still need downloading — the server forwards it into the task
    registry's ``downloading`` phase (3.8 D3). ``device_providers`` is
    the Device-selector routing (3.10.2): a forced provider list from
    :func:`providers_for_device` (None = auto / probe chain).
    ``cancelled`` is the cooperative cancel probe (3.10.3) used by the
    model download and handed to :meth:`DemucsSeparator.separate`."""
    return DemucsSeparator(variant, infer=infer, threads=threads,
                           download_progress=download_progress,
                           device_providers=device_providers,
                           cancelled=cancelled)


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
                duration: float, elapsed_sec: float,
                quality: str | None = None) -> dict:
    """Write ``{key: mono float array}`` as PCM16 WAVs + status.json,
    atomically (tmp dir -> os.replace, mirroring stems.py).

    ``quality`` (3.10) is recorded in status.json — the server serves
    the cache only when the requested quality tier matches (a tier
    switch recomputes instead of serving a mismatched cached render)."""
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
            "quality": quality or DEFAULT_QUALITY,
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


# ------------------------------------------------- stem spectrogram (3.9)

def dl_spec_path(entry: Path, method: str, key: str) -> Path:
    """Cached per-stem spectrogram JSON next to its WAV (Phase 3.9);
    lives inside the version-tagged DL stems dir so bumping
    ``DL_STEMS_VERSION`` invalidates specs together with their stems."""
    _check_method(method)
    return dl_stems_dir(entry, method) / f"spec_{key}.json"


def get_stem_spec(entry: Path, method: str, key: str, *, window: int,
                  db_range: float, peak_ref: float, rate: int = 30,
                  lock=None) -> dict:
    """Quantized semitone spectrogram of one DL stem WAV (Phase 3.9),
    disk-cached as ``spec_<stem>.json`` (same layout as stems.py's twin).

    Normalization basis (3.9.1 C): the MASTER's mix joint peak
    (``peak_ref``), not the stem's own peak; cached bodies carry the
    ``basis`` tag and pre-3.9.1 own-peak files recompute. Needs only the
    cached WAV — no onnxruntime — so the lane [Wave|Spec] view and overlay
    layers keep working in any environment where the stems were computed.
    A missing WAV raises FileNotFoundError which the server maps to 404
    with the request hint. Compute runs under ``lock`` with the usual
    double-check."""
    _check_method(method)
    if key not in STEM_SPECS[method]:
        raise KeyError(f"未知 stem: {method}/{key}")
    cache = dl_spec_path(entry, method, key)
    body = _load_spec_cache(cache, method, key, rate)
    if body is not None:
        return body
    with (lock or threading.Lock()):
        body = _load_spec_cache(cache, method, key, rate)  # re-check
        if body is not None:
            return body
        wav = dl_stem_path(entry, method, key)
        if not wav.is_file():
            raise FileNotFoundError(
                f"{method} 分轨尚未计算: 先请求 POST /api/stems?method="
                f"{method}")
        import soundfile as sf

        x, sr = sf.read(str(wav), dtype="float64", always_2d=True)
        mono = x.mean(axis=1)
        spec = payload.stem_spec_payload(mono, sr, window, db_range,
                                         peak_ref, rate=rate)
        body = {"method": method, "stem": key,
                "duration": round(float(x.shape[0] / sr), 6), **spec,
                "version": DL_STEMS_VERSION}
        tmp = cache.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, cache)
        return {**body, "cached": False}


def _load_spec_cache(cache: Path, method: str, key: str, rate: int) -> dict | None:
    """Valid cached spec body or None (same guard as stems.py: version +
    stem identity + request rate + the master-joint-peak ``basis`` tag;
    window/db_range are constant per analysis entry)."""
    try:
        body = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if body.get("version") == DL_STEMS_VERSION and \
            body.get("method") == method and body.get("stem") == key and \
            body.get("rate") == int(rate) and \
            body.get("basis") == "mix_joint_peak":
        return {**body, "cached": True}
    return None

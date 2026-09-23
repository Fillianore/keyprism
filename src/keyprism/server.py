#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism HTTP service layer: long-running API + upload-based track switch

The only module coupled to stdlib httpd:
- GET  /api/ping           health check + DL capability flags
- GET  /api/spec?rate&sub  recompute the spectrum at a given resolution (cached)
- GET  /api/notes?track=&method=  transcription notes: monophonic presets
                           (default) or method=poly (Basic Pitch on a DL
                           stem, Phase 3)
- GET  /api/midi?track=    the monophonic notes exported as a Standard MIDI File
- GET  /api/stems?method=  classic stems (hpss/rpca/combined) computed
                           synchronously; DL stems (demucs|demucs_4|
                           demucs_6) served from their cache, &progress=1
                           polls; the stems list is always the FIXED
                           per-method registry (never file-dependent)
- POST /api/stems?method=demucs|demucs_4|demucs_6
                           start DL separation as a background task;
                           returns {"task_id", "status_url"} (501 without
                           the optional [dl] dependencies)
- GET  /api/task/{id}      background-task status/progress/result
- GET  /api/stem?method&name  one computed stem as a WAV download
- GET  /api/stem_spec?method=&stem=&rate=
                           quantized semitone spectrogram of one computed
                           stem (Phase 3.9: lane [Wave|Spec] view + master-
                           plot overlay layers); disk-cached beside the WAV
- POST /api/upload?name=   upload audio bytes, analyze and switch tracks

make_server() returns an unstarted ThreadingHTTPServer so tests can
start/shutdown it in a thread; run_server() is the blocking entry.

DL tasks (Phase 3) run on a single-worker ThreadPoolExecutor: one heavy
inference at a time (RAM ceiling), progress reported into an in-memory
task registry polled via /api/task/{id} — long separations never hold a
browser HTTP connection open.
"""

import json
import re
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import audio_io, dlsep, poly_transcribe, stems, transcribe
from .analyze import run_analysis
from .payload import compute_specs, joint_spec_peak
from .tracks import TRACK_QUERY_VALUES

#: Graceful-degradation flags (Phase 3): the DL dependencies are
#: optional; when absent the DL endpoints answer 501 instead of the
#: server crashing. Module-level so tests can flip them.
DL_AVAILABLE = dlsep.ORT_AVAILABLE
POLY_AVAILABLE = poly_transcribe.BP_AVAILABLE

#: Distinct 501 bodies (Phase 3.7 I4): onnxruntime missing is fixed by
#: installing the extra; basic-pitch missing on Python >= 3.12 is a
#: hard upstream limitation that no install can fix — the two must
#: never share a message. ``code`` lets the frontend match the body
#: without parsing prose.
_DL_NOT_INSTALLED = ("DL 依赖未安装: 请执行 uv sync --extra dl "
                     "(需要 onnxruntime / basic-pitch)")
_POLY_UNAVAILABLE = ("多音转录在 Python ≥3.12 不可用（basic-pitch 限制）；"
                     "分离功能不受影响")


def _poly_reason() -> str:
    """Why polyphonic transcription is off: the [dl] extra remedy when
    nothing DL is installed, else the Python >= 3.12 limitation."""
    if not DL_AVAILABLE:
        return _DL_NOT_INSTALLED
    return _POLY_UNAVAILABLE

#: Query alias: "demucs" always means the canonical 4-stem variant (the
#: frontend only ever sends the explicit names; the alias exists so a
#: manual/older client cannot get an ambiguous answer)
_DL_METHOD_ALIAS = {"demucs": "demucs_4"}


def _norm_method(method: str) -> str:
    return _DL_METHOD_ALIAS.get(method, method)


def make_server(path: Path, port: int, host: str, start: float,
                end: float | None, window: int, db_range: float, rate: int,
                sub: int) -> ThreadingHTTPServer:
    """Build the server instance (not started): returns after the initial
    analysis, ready for serve_forever/shutdown"""
    # A wildcard address is not directly reachable from the browser; apiBase
    # falls back to localhost
    shown = "localhost" if host in ("", "0.0.0.0", "::") else host
    api_base = f"http://{shown}:{port}"

    def emit(payload: dict) -> Path:
        out = audio_io.PUBLIC_DIR / "data.json"
        out.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        return out

    def load_current(src: Path, preload: tuple | None = None,
                     name: str | None = None):
        """Run the staged analysis (decode/stft/aggregate/payload, with the
        complex-STFT disk cache), emit data.json, return (track state,
        payload)"""
        payload, (data2d, sr, dur) = run_analysis(
            src, start=start, end=end, window=window, db_range=db_range,
            rate=rate, sub=sub, api_base=api_base, name=name,
            preloaded=preload)
        emit(payload)
        cur = {"path": src, "name": name or src.name, "data2d": data2d,
               "sr": sr, "dur": dur,
               "rate": payload["defaultRate"], "sub": payload["defaultSub"]}
        return cur, payload

    state = {
        "busy": False,   # an upload/analysis of a new track is running
        "cache": {},     # (rate, sub) -> compute_specs result
        "lock": threading.Lock(),
        "notes_lock": threading.Lock(),  # serializes note computations
        "stems_lock": threading.Lock(),  # serializes stem computations
        "stems_progress": {},  # method -> {"done": n, "total": m}
        "dl_lock": threading.Lock(),  # serializes poly-note computations
        "executor": ThreadPoolExecutor(  # ONE DL job at a time (RAM ceiling)
            max_workers=1, thread_name_prefix="keyprism-dl"),
        "tasks": {},     # task_id -> {"status", "progress", "result", ...}
        "tasks_lock": threading.Lock(),
        "cur": None,     # current track: {path, name, data2d, sr, dur, ...}
    }
    state["cur"], _ = load_current(path)
    print(f"已输出: {(audio_io.PUBLIC_DIR / 'data.json').resolve()} "
          f"(apiBase={api_base})")

    def start_task(fn) -> dict:
        """Register a background task and run ``fn(task)`` on the
        single-worker executor (one heavy DL job at a time). The task
        dict carries status/progress/result plus a cooperative
        ``cancel_flag`` (3.10.3): POST /api/task/{id}/cancel flips it
        and the job aborts at the next chunk/pass/download boundary by
        raising ``dlsep.TaskCancelled`` → status "cancelled" (no stem
        cache written). Finished tasks are pruned to the newest 32 so
        the registry cannot grow unbounded."""
        task_id = uuid.uuid4().hex
        task = {"id": task_id, "status": "running", "progress": 0.0,
                "result": None, "error": None, "cancel_flag": False}
        with state["tasks_lock"]:
            for k in [k for k, v in state["tasks"].items()
                      if v["status"] in ("done", "error", "cancelled")][:-32]:
                state["tasks"].pop(k, None)
            state["tasks"][task_id] = task

        def run():
            try:
                task["result"] = fn(task)
                task["status"] = "done"
            except dlsep.TaskCancelled as e:  # cooperative stop (3.10.3)
                task["status"] = "cancelled"
                task["error"] = None
                print(f"[任务] {task_id[:8]} 已取消: {e}")
            except Exception as e:  # noqa: BLE001 - surfaced verbatim
                task["status"] = "error"
                task["error"] = str(e)

        state["executor"].submit(run)
        return task

    class Handler(BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")

        def _api_base(self) -> str:
            """Externally reachable base URL resolved from the BOUND
            address at call time (the constructor arg may be port 0 =
            ephemeral; reading the socket afterwards always yields the
            real port)."""
            host, port = self.server.server_address[:2]
            if host in ("", "0.0.0.0", "::"):
                host = "localhost"
            return f"http://{host}:{port}"

        def _json(self, code: int, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/api/ping":
                caps = {
                    "dl": DL_AVAILABLE,
                    "poly": POLY_AVAILABLE,
                    "dl_methods": list(dlsep.DL_METHODS)
                    if DL_AVAILABLE else [],
                    # active ORT execution providers (session readback
                    # once a model is loaded, else the selected chain):
                    # drives the frontend's GPU/CPU badge (3.10)
                    "ort_providers": dlsep.active_providers()
                    if DL_AVAILABLE else [],
                    # providers compiled into this onnxruntime build
                    # (3.10.2): lets the Device dropdown disable the GPU
                    # option when no GPU EP exists
                    "ort_providers_available": dlsep.ort_available_providers()
                    if DL_AVAILABLE else [],
                }
                if not caps["poly"]:
                    # precise reason: install remedy vs the Python >= 3.12
                    # basic-pitch limitation (drives the Notes toast)
                    caps["poly_reason"] = _poly_reason()
                self._json(200, {"ok": True, "capabilities": caps})
                return
            if u.path.startswith("/api/task/"):
                self._task_status(u)
                return
            if u.path in ("/api/notes", "/api/midi"):
                self._notes_or_midi(u)
                return
            if u.path == "/api/stems":
                self._stems(u)
                return
            if u.path == "/api/stem":
                self._stem_wav(u)
                return
            if u.path == "/api/stem_spec":
                self._stem_spec(u)
                return
            if u.path != "/api/spec":
                self.send_error(404)
                return
            cur = state["cur"]
            q = urllib.parse.parse_qs(u.query)
            r = int(q.get("rate", [str(cur["rate"])])[0])
            s = int(q.get("sub", [str(cur["sub"])])[0])
            key = (r, s)
            if key in state["cache"]:
                res = state["cache"][key]
            else:
                try:
                    res = compute_specs(cur["data2d"], cur["sr"], cur["dur"],
                                        r, s, db_range, window)
                except Exception as e:  # noqa: BLE001
                    self._json(500, {"error": str(e)})
                    return
                if len(state["cache"]) > 3:  # cache at most 4 resolutions
                    state["cache"].clear()
                state["cache"][key] = res
            self._json(200, res)

        def _notes_entry(self, cur):
            """Analysis cache entry of the current track (recomputes the
            STFT stage through the cache if it was evicted)."""
            return transcribe.locate_entry(
                cur, window=window, db_range=db_range, rate=cur["rate"],
                sub=cur["sub"], start=start, end=end)

        def _master_peak(self, cur):
            """The master spectrogram's normalization basis (3.9.1 C): the
            joint mix/left/right peak of the semitone power matrices at the
            track's payload resolution — the exact value data.json was
            quantized against. Computed once per track (lazily, on the
            first stem-spec request) and cached in the track state, so
            every stem spec shares the master's dB scale."""
            p = cur.get("spec_peak")
            if p is None:
                max_cols = max(60, int(round(cur["rate"] * cur["dur"])))
                p = joint_spec_peak(cur["data2d"], cur["sr"], window,
                                    max_cols, cur["sub"])
                cur["spec_peak"] = p
            return p

        def _poly_notes(self, u, q):
            """GET /api/notes?track=piano|guitar|other&method=poly[&source=]

            Basic Pitch polyphonic transcription of a DL stem, cached
            under the analysis entry (Phase 3)."""
            if not POLY_AVAILABLE:
                if DL_AVAILABLE:
                    self._json(501, {"code": "poly_unavailable",
                                     "error": _POLY_UNAVAILABLE})
                else:
                    self._json(501, {"code": "dl_not_installed",
                                     "error": _DL_NOT_INSTALLED})
                return
            track = (q.get("track", ["piano"])[0] or "").strip()
            if track not in poly_transcribe.POLY_TRACKS:
                self._json(400, {
                    "error": f"poly 方法不支持音轨: {track} "
                             f"(可用: {', '.join(poly_transcribe.POLY_TRACKS)})"})
                return
            source = (q.get("source", ["demucs_6"])[0] or "demucs_6").strip()
            if source not in dlsep.DL_METHODS:
                self._json(400, {
                    "error": f"未知 DL 分离方法: {source} "
                             f"(可用: {', '.join(dlsep.DL_METHODS)})"})
                return
            cur = state["cur"]
            entry = self._notes_entry(cur)
            try:
                stem_path = dlsep.dl_stem_path(entry, source, track)
            except KeyError:
                self._json(400, {
                    "error": f"{source} 没有 stem {track} "
                             f"(可用: {', '.join(dlsep.STEM_SPECS[source])})"})
                return
            if not stem_path.is_file():
                self._json(409, {
                    "error": f"DL 分轨尚未计算: 先请求 "
                             f"POST /api/stems?method={source}"})
                return
            try:
                body, cached = poly_transcribe.get_poly_notes(
                    entry, track, stem_path, lock=state["dl_lock"])
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)})
                return
            self._json(200, {**body, "cached": cached})

        def _task_status(self, u):
            """GET /api/task/{id}: background-task poll (non-blocking).

            A DL task in its model-download phase (3.8 D3) reports
            ``status:"downloading"`` plus ``bytes_done`` / ``bytes_total``
            / ``speed_mbps``; inference keeps the classic ``running`` +
            fractional ``progress`` shape."""
            task_id = u.path.rsplit("/", 1)[-1]
            with state["tasks_lock"]:
                task = state["tasks"].get(task_id)
                if task is None:
                    self._json(404, {"error": "未知任务"})
                    return
                body = {"id": task_id, "status": task["status"],
                        "progress": round(float(task["progress"]), 4)}
                if task["status"] == "downloading":
                    body["bytes_done"] = int(task.get("bytes_done") or 0)
                    body["bytes_total"] = int(task.get("bytes_total") or 0)
                    body["speed_mbps"] = round(
                        float(task.get("speed_mbps") or 0.0), 2)
                elif task["status"] == "done":
                    body.update(task["result"] or {})
                elif task["status"] == "error":
                    body["error"] = task["error"]
            self._json(200, body)

        def _cancel_task(self, u):
            """POST /api/task/{id}/cancel (3.10.3): flip the task's
            cooperative cancel flag; the job aborts at the next
            chunk/pass/download boundary and reports status
            "cancelled" (ort ``run()`` is uninterruptible, so the stop
            is bounded by one model segment, ~7.8 s). Cancelling an
            already-finished task is a harmless no-op answered with its
            final status."""
            task_id = u.path[len("/api/task/"):-len("/cancel")]
            with state["tasks_lock"]:
                task = state["tasks"].get(task_id)
                if task is None:
                    self._json(404, {"error": "未知任务"})
                    return
                task["cancel_flag"] = True
                status = task["status"]
            self._json(200, {"id": task_id, "status": status,
                             "cancelling": True})

        def _stem_urls(self, method: str, keys) -> list:
            """Canonical /api/stems entry list: exactly one URL per
            registered stem key, in registry order."""
            return [
                {"key": key,
                 "url": f"{self._api_base()}/api/stem?method={method}"
                        f"&name={urllib.parse.quote(key)}"}
                for key in keys
            ]

        def _dl_stems_urls(self, method: str, status: dict) -> list:
            return self._stem_urls(method, dlsep.STEM_SPECS[method])

        def _stems_dl_get(self, u, method: str):
            """GET /api/stems?method=demucs_4|demucs_6: serve the DL
            stem cache (computation is started via POST, never here)."""
            status = dlsep.load_status(self._notes_entry(state["cur"]),
                                       method)
            if status is None:
                self._json(404, {
                    "error": f"{method} 分轨尚未计算: 用 "
                             f"POST /api/stems?method={method} 启动"})
                return
            self._json(200, {
                "method": method,
                "cached": True,
                "duration": status.get("duration"),
                "sample_rate": status.get("sr"),
                "stems": self._dl_stems_urls(method, status),
            })

        def _notes_or_midi(self, u):
            cur = state["cur"]
            if cur is None:
                self._json(409, {"error": "暂无已加载的曲目"})
                return
            q = urllib.parse.parse_qs(u.query)
            method = (q.get("method", ["mono"])[0] or "mono").strip()
            if method == "poly":
                self._poly_notes(u, q)
                return
            if method != "mono":
                self._json(400, {"error": f"未知转录方法: {method} "
                                          "(可用: mono, poly)"})
                return
            track = (q.get("track", ["both"])[0] or "").strip()
            if track not in TRACK_QUERY_VALUES:
                self._json(400, {
                    "error": f"未知音轨: {track} "
                             f"(可用: {', '.join(TRACK_QUERY_VALUES)})"})
                return
            entry = self._notes_entry(cur)
            try:
                if u.path == "/api/notes":
                    body, _ = transcribe.get_notes(
                        entry, track, lock=state["notes_lock"])
                    self._json(200, body)
                    return
                data, fname = transcribe.compute_midi(
                    entry, track, cur.get("name") or cur["path"].name,
                    lock=state["notes_lock"])
                self.send_response(200)
                self.send_header("Content-Type", "audio/midi")
                self._cors()
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)})

        def _stems(self, u):
            """GET /api/stems?method=hpss|rpca|combined|demucs[&_4|_6]
            [&progress=1]

            Computes (or serves from the entry cache) the separated stems
            of the current track and returns their download URLs; with
            ``progress=1`` returns immediately with the live progress of a
            running computation instead of blocking on it.

            Contract: the ``stems`` list is derived from the FIXED
            per-method registry (stems.STEM_SPECS / dlsep.STEM_SPECS) —
            exactly hpss=['harmonic','percussive'],
            rpca=['lowrank','sparse'], combined=['harmonic','percussive'],
            demucs_4=['drums','bass','other','vocals'],
            demucs_6=[... 6 keys]. The list NEVER depends on file length
            or chunking; a status.json that disagrees with the registry is
            treated as a cache miss by the compute layer and rewritten."""
            cur = state["cur"]
            if cur is None:
                self._json(409, {"error": "暂无已加载的曲目"})
                return
            q = urllib.parse.parse_qs(u.query)
            method = _norm_method(
                (q.get("method", ["combined"])[0] or "combined").strip())
            if method in dlsep.DL_METHODS:
                self._stems_dl_get(u, method)
                return
            if method not in stems.METHODS:
                self._json(400, {
                    "error": f"未知分离方法: {method} "
                             f"(可用: {', '.join(stems.METHODS)})"})
                return
            if (q.get("progress", ["0"])[0] or "").strip() not in \
                    ("", "0", "false"):
                prog = state["stems_progress"].get(
                    method, {"done": 0, "total": 0})
                self._json(200, {"method": method, **prog})
                return
            entry = self._notes_entry(cur)

            def progress(done, total):
                state["stems_progress"][method] = {"done": done,
                                                   "total": total}

            try:
                status = stems.get_stems(entry, method, progress=progress,
                                         lock=state["stems_lock"])
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)})
                return
            self._json(200, {
                "method": method,
                "cached": bool(status.get("cached")),
                "elapsed_sec": status.get("elapsed_sec"),
                "duration": status.get("duration"),
                "sample_rate": status.get("sr"),
                "stems": self._stem_urls(method, stems.STEM_SPECS[method]),
            })

        def _start_dl_stems(self, u):
            """POST /api/stems?method=demucs|demucs_4|demucs_6
            [&quality=fast|balanced|best]: start DL separation as a
            background task (501 without the optional [dl] dependencies,
            200 with the cached list when already computed AT the
            requested quality tier — a tier switch recomputes instead of
            serving a mismatched cached render)."""
            if not DL_AVAILABLE:
                self._json(501, {"code": "dl_not_installed",
                                 "error": _DL_NOT_INSTALLED})
                return
            cur = state["cur"]
            if cur is None:
                self._json(409, {"error": "暂无已加载的曲目"})
                return
            q = urllib.parse.parse_qs(u.query)
            method = _norm_method((q.get("method", [""])[0] or "").strip())
            if method not in dlsep.DL_METHODS:
                self._json(400, {
                    "error": f"未知 DL 分离方法: {method} "
                             f"(可用: {', '.join(dlsep.DL_METHODS)})"})
                return
            quality = (q.get("quality", [dlsep.DEFAULT_QUALITY])[0] or
                       dlsep.DEFAULT_QUALITY).strip()
            try:
                shifts = dlsep.shifts_for_quality(quality)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            # 3.10.2 Device selector: auto (probe chain) / gpu (forced,
            # 400 when the build ships no GPU EP) / cpu (pure CPU)
            device = (q.get("device", ["auto"])[0] or "auto").strip()
            if device not in dlsep.DEVICE_CHOICES:
                self._json(400, {
                    "error": f"未知设备: {device} "
                             f"(可用: {', '.join(dlsep.DEVICE_CHOICES)})"})
                return
            try:
                device_providers = dlsep.providers_for_device(device)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            # 3.10.3 Restart: force=1 bypasses the stems cache and
            # recomputes (write_stems atomically overwrites the entry)
            force = (q.get("force", ["0"])[0] or "").strip() in \
                ("1", "true")
            entry = self._notes_entry(cur)
            cached = None if force else dlsep.load_status(entry, method)
            if cached is not None and \
                    cached.get("quality", dlsep.DEFAULT_QUALITY) == quality:
                self._json(200, {
                    "method": method, "cached": True,
                    "quality": cached.get("quality",
                                          dlsep.DEFAULT_QUALITY),
                    "duration": cached.get("duration"),
                    "sample_rate": cached.get("sr"),
                    "stems": self._dl_stems_urls(method, cached)})
                return

            def job(task):
                # mono float32 of the decoded segment; the separator
                # resamples to the model rate internally, per-chunk
                mono = cur["data2d"].mean(axis=1).astype("float32")

                def download_progress(done, total, speed_mbps):
                    # 3.8 D3: first-run model weights download — the
                    # task enters the "downloading" phase until the
                    # bytes are in place (.part -> atomic rename inside
                    # dlsep), then inference flips it back to "running"
                    task["status"] = "downloading"
                    task["bytes_done"] = int(done)
                    task["bytes_total"] = int(total)
                    task["speed_mbps"] = round(float(speed_mbps), 2)

                def progress(done, total):
                    task["status"] = "running"
                    task["progress"] = done / float(total)

                started = time.time()
                # cooperative cancel probe (3.10.3): the download loop
                # checks it per byte-chunk, inference per chunk/pass
                def cancelled():
                    return bool(task.get("cancel_flag"))

                sep = dlsep.get_separator(
                    method, download_progress=download_progress,
                    device_providers=device_providers,
                    cancelled=cancelled)
                task["status"] = "running"
                # 3.10 quality tier -> demucs shifts (per-pass progress;
                # the denominator already includes every pass)
                out = sep.separate(mono, cur["sr"], shifts=shifts,
                                   progress=progress,
                                   cancelled=cancelled)
                status = dlsep.write_stems(
                    entry, method, out, dlsep.TARGET_SR, cur["dur"],
                    time.time() - started, quality=quality)
                return {
                    "method": method,
                    "quality": status.get("quality",
                                          dlsep.DEFAULT_QUALITY),
                    "duration": status.get("duration"),
                    "sample_rate": status.get("sr"),
                    "stems": self._dl_stems_urls(method, status),
                }

            task = start_task(job)
            self._json(200, {
                "task_id": task["id"],
                "status_url": f"/api/task/{task['id']}",
                "status": "started",
            })

        def _stem_wav(self, u):
            """GET /api/stem?method=..&name=.. : one stem as a WAV
            attachment, streamed verbatim from the entry cache."""
            cur = state["cur"]
            if cur is None:
                self._json(409, {"error": "暂无已加载的曲目"})
                return
            q = urllib.parse.parse_qs(u.query)
            method = _norm_method((q.get("method", [""])[0] or "").strip())
            name = (q.get("name", [""])[0] or "").strip()
            is_dl = method in dlsep.DL_METHODS
            specs = dlsep.STEM_SPECS if is_dl else stems.STEM_SPECS
            if method not in (stems.METHODS + dlsep.DL_METHODS) or \
                    name not in specs.get(method, ()):
                self._json(400, {
                    "error": f"未知 stem: {method}/{name} "
                             f"(可用: {specs})"})
                return
            entry = self._notes_entry(cur)
            path = (dlsep.dl_stem_path(entry, method, name) if is_dl
                    else stems.stem_file_path(entry, method, name))
            if not path.is_file():
                hint = (f"DL 分轨尚未计算: 先请求 "
                        f"POST /api/stems?method={method}")
                self._json(404, {"error": hint})
                return
            stem_slug = re.sub(r"[^A-Za-z0-9_-]+", "_",
                               Path(cur.get("name") or cur["path"].name)
                               .stem).strip("_") or "track"
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self._cors()
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="keyprism_{stem_slug}_'
                f'{method}_{name}.wav"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _stem_spec(self, u):
            """GET /api/stem_spec?method=&stem=&rate= : quantized semitone
            spectrogram of one computed stem (Phase 3.9), for the lane
            [Wave|Spec] view and the master-plot overlay layers.

            Reuses the Phase 0 aggregate pipeline (``spec_matrix``, sub=1
            -> 88 piano semitone rows, the same row space as the master
            heatmap's y axis). Normalization basis (3.9.1 C): the master's
            mix joint peak (``_master_peak``), so overlay brightness
            matches the bass/energy share seen in the master spectrum
            instead of being inflated to the stem's own full scale; the
            response carries ``peak_ref`` + ``basis`` for the frontend to
            assert. Needs only the stem WAV from the cache — no
            onnxruntime — and 404s with the compute hint when the
            separation has not run yet."""
            cur = state["cur"]
            if cur is None:
                self._json(409, {"error": "暂无已加载的曲目"})
                return
            q = urllib.parse.parse_qs(u.query)
            method = _norm_method(
                (q.get("method", [""])[0] or "").strip())
            name = (q.get("stem", [""])[0] or "").strip()
            try:
                rate = int(q.get("rate", ["30"])[0])
            except ValueError:
                rate = 30
            is_dl = method in dlsep.DL_METHODS
            specs = dlsep.STEM_SPECS if is_dl else stems.STEM_SPECS
            if method not in specs or name not in specs.get(method, ()):
                self._json(400, {
                    "error": f"未知 stem: {method}/{name} (可用: {specs})"})
                return
            entry = self._notes_entry(cur)
            fn = dlsep.get_stem_spec if is_dl else stems.get_stem_spec
            lock = state["dl_lock"] if is_dl else state["stems_lock"]
            try:
                body = fn(entry, method, name, window=window,
                          db_range=db_range,
                          peak_ref=self._master_peak(cur), rate=rate,
                          lock=lock)
            except FileNotFoundError as e:
                self._json(404, {"error": str(e)})
                return
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)})
                return
            self._json(200, body)

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/api/stems":
                self._start_dl_stems(u)
                return
            if u.path.startswith("/api/task/") and \
                    u.path.endswith("/cancel"):
                self._cancel_task(u)
                return
            if u.path != "/api/upload":
                self.send_error(404)
                return
            if state["busy"]:
                self._json(409, {"error": "已有导入任务进行中, 请稍候"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                self._json(411, {"error": "缺少请求体 (Content-Length)"})
                return
            if length > audio_io.MAX_UPLOAD_BYTES:
                self._json(413, {"error": "文件过大 (上限 512MB)"})
                return
            q = urllib.parse.parse_qs(u.query)
            raw = (q.get("name", [""])[0] or "").strip()
            fname = Path(raw.replace("\\", "/")).name or "upload"
            with state["lock"]:
                state["busy"] = True
            dest = audio_io.UPLOAD_DIR / f"{int(time.time() * 1000)}_{fname}"
            try:
                audio_io.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                remaining = length
                with open(dest, "wb") as f:
                    while remaining > 0:
                        chunk = self.rfile.read(min(remaining, 1 << 20))
                        if not chunk:
                            raise ValueError("上传中断: 数据不完整")
                        f.write(chunk)
                        remaining -= len(chunk)
                try:
                    cur, payload = load_current(dest, name=fname)
                except Exception as e:  # noqa: BLE001
                    msg = str(e).replace(str(dest), fname)
                    raise ValueError(f"无法解析该音频 ({msg})") from e
                with state["lock"]:
                    state["cur"] = cur
                    state["cache"].clear()
                # Keep only the most recent files in the staging directory
                olds = sorted(audio_io.UPLOAD_DIR.glob("*_*"),
                              key=lambda p: p.name)
                for old in olds[:-4]:
                    old.unlink(missing_ok=True)
                audio_mb = (audio_io.PUBLIC_DIR / payload["audioFile"]) \
                    .stat().st_size / 1024 / 1024
                print(f"[上传] {fname}: {cur['dur']:.1f}s @ {cur['sr']} Hz "
                      f"-> audio {audio_mb:.1f} MB, 已切换曲目")
                self._json(200, payload)
            except Exception as e:  # noqa: BLE001
                dest.unlink(missing_ok=True)
                self._json(500, {"error": str(e)})
            finally:
                with state["lock"]:
                    state["busy"] = False

        def log_message(self, fmt, *a):  # noqa: A002
            pass

    return ThreadingHTTPServer((host, port), Handler)


def run_server(path: Path, port: int, host: str, start: float,
               end: float | None, window: int, db_range: float, rate: int,
               sub: int):
    """Long-running service: the frontend can request spectra at new
    resolutions at any time and upload audio to switch tracks"""
    srv = make_server(path, port, host, start, end, window, db_range,
                      rate, sub)
    api_base = f"http://{srv.server_address[0]}:{srv.server_address[1]}"
    if srv.server_address[0] in ("", "0.0.0.0", "::"):
        api_base = f"http://localhost:{srv.server_address[1]}"
    print(f"服务已启动: {api_base}  (Ctrl+C 退出)")
    srv.serve_forever()

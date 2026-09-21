#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism HTTP service layer: long-running API + upload-based track switch

The only module coupled to stdlib httpd:
- GET  /api/ping           health check
- GET  /api/spec?rate&sub  recompute the spectrum at a given resolution (cached)
- POST /api/upload?name=   upload audio bytes, analyze and switch tracks

make_server() returns an unstarted ThreadingHTTPServer so tests can
start/shutdown it in a thread; run_server() is the blocking entry.
"""

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import audio_io
from .analyze import run_analysis
from .payload import compute_specs


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
        cur = {"path": src, "data2d": data2d, "sr": sr, "dur": dur,
               "rate": payload["defaultRate"], "sub": payload["defaultSub"]}
        return cur, payload

    state = {
        "busy": False,   # an upload/analysis of a new track is running
        "cache": {},     # (rate, sub) -> compute_specs result
        "lock": threading.Lock(),
        "cur": None,     # current track: {path, data2d, sr, dur, rate, sub}
    }
    state["cur"], _ = load_current(path)
    print(f"已输出: {(audio_io.PUBLIC_DIR / 'data.json').resolve()} "
          f"(apiBase={api_base})")

    class Handler(BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")

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
                self._json(200, {"ok": True})
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

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
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

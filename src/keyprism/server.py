#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism HTTP 服务层: 常驻 API + 上传切换曲目

唯一与 stdlib httpd 耦合的模块:
- GET  /api/ping           健康检查
- GET  /api/spec?rate&sub  重算指定分辨率频谱 (带缓存)
- POST /api/upload?name=   上传音频字节流, 解析并切换当前曲目

make_server() 返回未启动的 ThreadingHTTPServer, 便于测试中
以线程启动/shutdown; run_server() 为阻塞入口。
"""

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import audio_io
from .payload import analyze, compute_specs


def make_server(path: Path, port: int, host: str, start: float,
                end: float | None, window: int, db_range: float, rate: int,
                sub: int) -> ThreadingHTTPServer:
    """构建服务实例 (不启动): 初始分析完成后返回, 可 serve_forever/shutdown"""
    # 通配地址无法被浏览器直接访问, apiBase 回落到 localhost
    shown = "localhost" if host in ("", "0.0.0.0", "::") else host
    api_base = f"http://{shown}:{port}"

    def emit(payload: dict) -> Path:
        out = audio_io.PUBLIC_DIR / "data.json"
        out.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        return out

    def load_current(src: Path, preload: tuple | None = None,
                     name: str | None = None):
        """解码 (或复用预解码数据), 输出 data.json, 返回 (当前曲目状态, payload)"""
        data2d, sr, dur = preload if preload is not None \
            else audio_io.load_channels(src, start, end)
        payload = analyze(src, start, end, window, db_range, rate, sub,
                          api_base, preloaded=(data2d, sr, dur), name=name)
        emit(payload)
        cur = {"path": src, "data2d": data2d, "sr": sr, "dur": dur,
               "rate": payload["defaultRate"], "sub": payload["defaultSub"]}
        return cur, payload

    state = {
        "busy": False,   # 正在上传/分析新曲目
        "cache": {},     # (rate, sub) -> compute_specs 结果
        "lock": threading.Lock(),
        "cur": None,     # 当前曲目: {path, data2d, sr, dur, rate, sub}
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
                if len(state["cache"]) > 3:  # 最多缓存 4 组分辨率
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
                # 暂存目录只保留最近几个文件
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
    """常驻服务: 前端可随时请求新分辨率的频谱, 也可上传新音频切换曲目"""
    srv = make_server(path, port, host, start, end, window, db_range,
                      rate, sub)
    api_base = f"http://{srv.server_address[0]}:{srv.server_address[1]}"
    if srv.server_address[0] in ("", "0.0.0.0", "::"):
        api_base = f"http://localhost:{srv.server_address[1]}"
    print(f"服务已启动: {api_base}  (Ctrl+C 退出)")
    srv.serve_forever()

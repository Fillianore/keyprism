"""HTTP 服务层测试: 临时端口起真实服务, 覆盖 ping/spec/upload 全路由"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from keyprism import audio_io
from helpers import make_wav
from keyprism.server import make_server


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    """临时目录 + 临时端口的服务实例, 测试结束自动 shutdown"""
    pub = tmp_path / "pub"
    pub.mkdir()
    monkeypatch.setattr(audio_io, "PUBLIC_DIR", pub)
    monkeypatch.setattr(audio_io, "UPLOAD_DIR", tmp_path / "uploads")
    wav = tmp_path / "t.wav"
    make_wav(wav, seconds=1.5)
    server = make_server(wav, 0, "127.0.0.1", 0.0, None, 2048, 70.0, 5, 1)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield {
        "base": f"http://127.0.0.1:{server.server_address[1]}",
        "pub": pub,
        "uploads": tmp_path / "uploads",
        "wav": wav,
    }
    server.shutdown()
    server.server_close()
    t.join(timeout=5)


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.status, json.loads(r.read().decode())


def post(url, data: bytes):
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_ping(srv):
    code, body = get(f"{srv['base']}/api/ping")
    assert code == 200 and body == {"ok": True}


def test_spec(srv):
    code, res = get(f"{srv['base']}/api/spec?rate=5&sub=1")
    assert code == 200
    assert set(res["specs"]) == {"mix", "left", "right"}
    assert res["rate"] == 5 and res["sub"] == 1
    # 同参数第二次命中缓存, 结果一致
    _, res2 = get(f"{srv['base']}/api/spec?rate=5&sub=1")
    assert res2 == res


def test_spec_writes_data_json(srv):
    assert (srv["pub"] / "data.json").exists()
    d = json.loads((srv["pub"] / "data.json").read_text(encoding="utf-8"))
    assert d["apiBase"].startswith("http://")


def test_upload_switches_track(srv):
    body = srv["wav"].read_bytes()
    code, payload = post(
        f"{srv['base']}/api/upload?name=newsong.wav", body)
    assert code == 200
    assert payload["file"] == "newsong.wav"  # 显示名为用户文件名
    # data.json 已切换, spec 反映新曲目
    d = json.loads((srv["pub"] / "data.json").read_text(encoding="utf-8"))
    assert d["file"] == "newsong.wav"
    _, res = get(f"{srv['base']}/api/spec?rate=5&sub=1")
    assert res["nCols"] == payload["nCols"]
    # 暂存目录只保留上传副本
    assert any(p.name.endswith("newsong.wav") for p in srv["uploads"].glob("*"))


def test_upload_garbage_returns_500(srv):
    code, body = post(
        f"{srv['base']}/api/upload?name=bad.mp3", b"\x00" * 2048)
    assert code == 500
    assert "无法解析" in body["error"]
    # 失败的上传不留暂存文件
    assert not list(srv["uploads"].glob("*bad.mp3"))


def test_upload_empty_body_returns_411(srv):
    code, body = post(f"{srv['base']}/api/upload?name=x.mp3", b"")
    assert code == 411


def test_upload_name_sanitized(srv):
    """路径穿越的文件名被剥离为纯文件名"""
    body = srv["wav"].read_bytes()
    code, payload = post(
        f"{srv['base']}/api/upload?name=..%2F..%2Fevil.wav", body)
    assert code == 200
    assert "/" not in payload["file"] and "\\" not in payload["file"]


def test_options_preflight(srv):
    req = urllib.request.Request(
        f"{srv['base']}/api/upload", method="OPTIONS")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 204
        assert r.headers["Access-Control-Allow-Origin"] == "*"
        assert "POST" in r.headers["Access-Control-Allow-Methods"]


def test_unknown_routes(srv):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{srv['base']}/api/nothing", timeout=10)
    assert e.value.code == 404
    # send_error 返回 HTML, 不走 JSON 解析路径
    req = urllib.request.Request(
        f"{srv['base']}/api/nothing", data=b"x", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 404

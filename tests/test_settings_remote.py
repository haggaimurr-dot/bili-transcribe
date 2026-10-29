import threading

import httpx
import pytest

from app import settings
from app.asr import ASROptions, remote
from app.errors import AppError, RemoteUnavailable


def test_cookie_file(tmp_env):
    s = settings.update({"sessdata": "abc123SESS", "bili_jct": "jct"})
    assert s.cookie_source == "sessdata"
    text = (tmp_env / "cookies.txt").read_text(encoding="utf-8")
    assert text.startswith("# Netscape HTTP Cookie File")
    line = [x for x in text.splitlines() if "SESSDATA" in x][0].split("\t")
    assert line[0] == ".bilibili.com" and line[5] == "SESSDATA" and line[6] == "abc123SESS"
    assert settings.cookie_dict() == {"SESSDATA": "abc123SESS", "bili_jct": "jct"}
    pub = settings.public(settings.load())
    assert pub["sessdata"] == "****SESS" and pub["cookie_configured"]
    settings.update({"buvid3": "ABCD-infoc"})
    assert settings.cookie_dict()["buvid3"] == "ABCD-infoc"


def test_masked_value_not_overwritten(tmp_env):
    settings.update({"remote_token": "secret-token"})
    settings.update({"remote_token": "****oken", "remote_url": "http://x:1"})
    s = settings.load()
    assert s.remote_token == "secret-token" and s.remote_url == "http://x:1"


def test_filter_cookie_upload(tmp_env):
    raw = "\n".join([
        "# Netscape HTTP Cookie File",
        ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tv1",
        "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t0\tbili_jct\tv2",
        ".google.com\tTRUE\t/\tFALSE\t0\tNID\tzzz",
        "broken line",
    ])
    settings.save_cookie_upload(raw)
    assert settings.load().cookie_source == "file"
    assert settings.cookie_dict() == {"SESSDATA": "v1", "bili_jct": "v2"}
    with pytest.raises(AppError):
        settings.filter_cookie_text(".google.com\tTRUE\t/\tFALSE\t0\tNID\tzzz")


@pytest.fixture
def ogg(tmp_path, monkeypatch):
    f = tmp_path / "a.wav"
    f.write_bytes(b"0" * 5000)
    from app import bili

    monkeypatch.setattr(bili, "to_opus", lambda p: p)
    return f


def _run(ogg):
    return remote.RemoteASR().transcribe(ogg, ASROptions(duration_ms=10000), lambda *a, **k: None,
                                         threading.Event(), lambda m: None)


def test_remote_unreachable(tmp_env, ogg):
    settings.update({"remote_url": "http://127.0.0.1:1", "remote_token": "t"})
    with pytest.raises(RemoteUnavailable):
        _run(ogg)


def test_remote_401_and_ok(tmp_env, ogg, monkeypatch):
    settings.update({"remote_url": "http://worker", "remote_token": "t"})
    calls = {}

    def fake_post(url, content, headers, timeout):
        body = b"".join(content)
        calls["auth"] = headers["Authorization"]
        calls["len"] = (len(body), int(headers["Content-Length"]))
        return httpx.Response(calls.get("status", 401), json={"detail": "x"})

    monkeypatch.setattr(remote.httpx, "post", fake_post)
    with pytest.raises(AppError) as e:
        _run(ogg)
    assert e.value.code == "REMOTE_UNAUTHORIZED"
    assert calls["auth"] == "Bearer t"
    assert calls["len"][0] == calls["len"][1]

    def ok_post(url, content, headers, timeout):
        b"".join(content)
        return httpx.Response(200, json={"source": "asr_remote", "model": "paraformer-zh", "duration_ms": 0,
                                         "sentences": [{"start": 0, "end": 1, "text": "好"}]})

    monkeypatch.setattr(remote.httpx, "post", ok_post)
    r = _run(ogg)
    assert r["source"] == "asr_remote" and r["duration_ms"] == 10000

    monkeypatch.setattr(remote.httpx, "post", lambda *a, **k: (b"".join(k["content"]), httpx.Response(502))[1])
    with pytest.raises(RemoteUnavailable):
        _run(ogg)

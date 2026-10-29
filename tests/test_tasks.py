"""任务流水线集成测试：B 站和识别都用替身，验证状态机、字幕优先、远程退回本地、导出。"""
import json

import pytest

from app import bili, db, export, tasks
from app.errors import RemoteUnavailable


@pytest.fixture
def env(tmp_env, monkeypatch):
    monkeypatch.setattr(export, "RESULTS_DIR", tmp_env / "results")
    monkeypatch.setattr(tasks, "RESULTS_DIR", tmp_env / "results")
    monkeypatch.setattr(tasks, "TASK_LOG_DIR", tmp_env)
    wav = tmp_env / "a.wav"
    wav.write_bytes(b"x")
    monkeypatch.setattr(bili, "download_audio", lambda *a, **k: wav)
    monkeypatch.setattr(bili, "probe_duration_ms", lambda p: 10000)
    monkeypatch.setattr(bili, "remove_audio", lambda *a: None)
    return tmp_env


def _entry(subs=None):
    return {"title": "视频", "uploader": "UP", "duration": 10, "subtitles": subs or {}}


def _make(force_asr=False, backend="local"):
    m = tasks.TaskManager()
    meta = {"title": "视频", "uploader": "UP", "parts": [{"index": 1, "title": "视频", "duration": 10}]}
    tid = m.create(bvid="BV1GJ411x7h7", parts=[1], meta=meta,
                   opts={"force_asr": force_asr, "backend": backend, "model": "paraformer-zh", "hotwords": [],
                         "spk": False})[0]
    return m, tid


def _run(m, tid):
    m.q.get()
    t = db.get_task(tid)
    m._run(t, tasks.TaskLog(tid))
    m._finish(tid, "done")
    return db.get_task(tid), db.get_result(tid)


class FakeASR:
    calls = []

    def __init__(self, fail=False):
        self.fail = fail

    def transcribe(self, wav, opts, prog, cancel, log):
        FakeASR.calls.append(self.fail)
        if self.fail:
            raise RemoteUnavailable("down")
        return {"source": "asr_local", "model": "paraformer-zh", "duration_ms": opts.duration_ms,
                "sentences": [{"start": 0, "end": 900, "text": "识别结果。"}]}


def test_subtitle_first(env, monkeypatch):
    sub = json.dumps({"body": [{"from": 0, "to": 1, "content": "字幕内容"}]}, ensure_ascii=False)
    monkeypatch.setattr(bili, "part_info", lambda v, p: _entry({"zh-CN": [{"ext": "json", "data": sub}]}))
    monkeypatch.setattr(tasks, "get_backend", lambda n: pytest.fail("不应走识别"))
    m, tid = _make()
    t, r = _run(m, tid)
    assert t["status"] == "done" and t["source"] == "subtitle_cc"
    assert r["sentences"][0]["text"] == "字幕内容"
    files = sorted(p.suffix for p in (env / "results" / str(tid)).iterdir())
    assert files == [".md", ".srt", ".txt"]


def test_force_asr(env, monkeypatch):
    monkeypatch.setattr(bili, "part_info", lambda v, p: _entry({"zh-CN": [{"data": "{}"}]}))
    monkeypatch.setattr(tasks, "get_backend", lambda n: FakeASR())
    m, tid = _make(force_asr=True)
    t, r = _run(m, tid)
    assert t["source"] == "asr_local" and t["duration_ms"] == 10000
    assert r["sentences"][0]["text"] == "识别结果。"


def test_remote_fallback(env, monkeypatch):
    FakeASR.calls = []
    monkeypatch.setattr(bili, "part_info", lambda v, p: _entry())
    monkeypatch.setattr(tasks, "get_backend", lambda n: FakeASR(fail=True))
    monkeypatch.setattr(tasks, "LocalASR", lambda: FakeASR())
    m, tid = _make(backend="remote")
    t, r = _run(m, tid)
    assert FakeASR.calls == [True, False]
    assert t["backend"] == "local"
    log = (env / f"{tid}.log").read_text(encoding="utf-8")
    assert "已切换本地识别" in log


def test_cancel_queued(env):
    m, tid = _make()
    m.cancel(tid)
    assert db.get_task(tid)["status"] == "cancelled"


def test_interrupted_on_restart(env):
    m, tid = _make()
    db.update_task(tid, status="transcribing")
    assert db.mark_interrupted() == 1
    t = db.get_task(tid)
    assert t["status"] == "failed" and t["error"]["code"] == "INTERRUPTED"

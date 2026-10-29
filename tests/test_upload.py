"""本地上传：接口校验、任务流水线（跳过 B 站）、导出不带 B 站链接、删除时清理上传文件。"""
import pytest
from fastapi.testclient import TestClient

from app import api, bili, db, export, tasks
from app.config import LOCAL_ID


@pytest.fixture
def env(tmp_env, monkeypatch):
    up = tmp_env / "uploads"
    monkeypatch.setattr(api, "UPLOAD_DIR", up)
    monkeypatch.setattr(export, "RESULTS_DIR", tmp_env / "results")
    monkeypatch.setattr(tasks, "RESULTS_DIR", tmp_env / "results")
    monkeypatch.setattr(tasks, "TASK_LOG_DIR", tmp_env)
    monkeypatch.setattr(tasks, "CACHE_DIR", tmp_env)
    monkeypatch.setattr(bili, "to_wav", lambda src, dst: dst.write_bytes(src.read_bytes()))
    monkeypatch.setattr(bili, "probe_duration_ms", lambda p: 5000)
    monkeypatch.setattr(bili, "part_info", lambda *a: pytest.fail("本地文件不应访问 B 站"))

    class FakeASR:
        def transcribe(self, wav, opts, prog, cancel, log):
            assert opts.hotwords == ["犬瘟热"]
            return {"source": "asr_local", "model": "paraformer-zh", "duration_ms": opts.duration_ms,
                    "sentences": [{"start": 0, "end": 900, "text": "本地文件。"}]}

    monkeypatch.setattr(tasks, "get_backend", lambda n: FakeASR())
    m = tasks.TaskManager()
    monkeypatch.setattr(api, "manager", m)
    from fastapi import FastAPI

    from app.main import app_error_handler
    from app.errors import AppError

    app = FastAPI()
    app.include_router(api.router)
    app.add_exception_handler(AppError, app_error_handler)
    return TestClient(app), m, up


def test_upload_rejects_bad_ext(env):
    client, _, _ = env
    r = client.post("/api/upload", files={"file": ("a.exe", b"xx")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "BAD_FILE"


def test_upload_pipeline_and_delete(env):
    client, m, up = env
    r = client.post("/api/upload", files={"file": ("我的录音.m4a", b"fake-audio")},
                    data={"hotwords": '["犬瘟热"]', "backend": "local", "model": "fun-asr-nano"})
    assert r.status_code == 200
    tid = r.json()["ids"][0]
    t = db.get_task(tid)
    assert t["bvid"] == LOCAL_ID and t["url"] == "" and t["title"] == "我的录音"
    assert t["model"] == "paraformer-zh"  # 本地后端强制 paraformer
    assert len(list(up.iterdir())) == 1

    m.q.get()
    m._run(t, tasks.TaskLog(tid))
    t, res = db.get_task(tid), db.get_result(tid)
    assert res["sentences"][0]["text"] == "本地文件。" and t["duration_ms"] == 5000

    txt, md = export.to_txt(t, res), export.to_md(t, res)
    assert "bilibili" not in txt and "bilibili" not in md
    assert "> 文件：我的录音.m4a" in md and "\\[00:00\\]" in md

    # 重试共用上传文件：删掉一个任务时不删文件，全部删掉才删
    new_id = m.retry(tid)
    m.delete(tid)
    assert len(list(up.iterdir())) == 1
    m.delete(new_id)
    assert list(up.iterdir()) == []

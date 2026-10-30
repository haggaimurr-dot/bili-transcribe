"""等待队列：调整顺序（上移/下移/置顶/置底）后，工作线程按新顺序取任务。"""
import sqlite3
import threading

import pytest

from app import db, tasks
from app.errors import AppError


@pytest.fixture
def mgr(tmp_env, monkeypatch):
    monkeypatch.setattr(tasks, "TASK_LOG_DIR", tmp_env)
    return tasks.TaskManager()  # 不启动工作线程，测试里手动 q.get()


def _make(m, n=4):
    meta = {"title": "t", "uploader": None, "parts": [{"index": i, "title": f"P{i}", "duration": 1} for i in range(1, n + 1)]}
    return m.create(bvid="BV1GJ411x7h7", parts=list(range(1, n + 1)), meta=meta, opts={})


def _order(m):
    return [t["id"] for t in m.queue()]


def test_default_is_fifo(mgr):
    ids = _make(mgr)
    assert _order(mgr) == ids
    assert [t["position"] for t in mgr.queue()] == [1, 2, 3, 4]
    assert [mgr.q.get() for _ in ids] == ids


def test_swap_up_down(mgr):
    a, b, c, d = _make(mgr)
    mgr.move(c, "up")
    assert _order(mgr) == [a, c, b, d]
    mgr.move(a, "down")
    assert _order(mgr) == [c, a, b, d]
    # 已经在最前/最后时不变
    mgr.move(c, "up")
    mgr.move(d, "down")
    assert _order(mgr) == [c, a, b, d]
    assert [mgr.q.get() for _ in range(4)] == [c, a, b, d]


def test_top_bottom(mgr):
    a, b, c, d = _make(mgr)
    mgr.move(d, "top")
    mgr.move(a, "bottom")
    assert _order(mgr) == [d, b, c, a]
    mgr.move(c, "top")
    assert _order(mgr) == [c, d, b, a]
    assert mgr.q.get() == c


def test_move_after_some_taken(mgr):
    a, b, c, d = _make(mgr)
    assert mgr.q.get() == a
    db.update_task(a, status="transcribing")  # 正在执行的不在等待队列里
    with pytest.raises(AppError) as e:
        mgr.move(a, "down")
    assert e.value.code == "NOT_QUEUED"
    mgr.move(d, "top")
    assert [mgr.q.get() for _ in range(3)] == [d, b, c]


def test_cancelled_is_skipped(mgr):
    a, b, c, _ = _make(mgr)
    mgr.cancel(a)
    mgr.move(c, "top")
    assert mgr.q.get() == c
    assert mgr.q.get() == b


def test_bad_action_and_missing(mgr):
    a, *_ = _make(mgr)
    with pytest.raises(AppError):
        mgr.move(a, "sideways")
    with pytest.raises(AppError) as e:
        mgr.move(9999, "up")
    assert e.value.code == "NOT_FOUND"


def test_new_task_goes_to_end_after_reorder(mgr):
    a, b, c, d = _make(mgr)
    mgr.move(d, "top")
    (e,) = _make(mgr, 1)
    assert _order(mgr) == [d, a, b, c, e]


def test_get_blocks_until_put(mgr):
    got = []
    th = threading.Thread(target=lambda: got.append(mgr.q.get()), daemon=True)
    th.start()
    th.join(0.2)
    assert not got
    (a,) = _make(mgr, 1)
    th.join(2)
    assert got == [a]


def test_migration_to_v3(tmp_path, monkeypatch):
    path = tmp_path / "v2.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, bvid TEXT, part INTEGER, title TEXT,
            part_title TEXT, uploader TEXT, duration_ms INTEGER DEFAULT 0, url TEXT, status TEXT, stage TEXT,
            percent REAL DEFAULT 0, message TEXT, source TEXT, backend TEXT, model TEXT, opts_json TEXT,
            error_json TEXT, created_at REAL, finished_at REAL, platform TEXT);
        INSERT INTO tasks(bvid, part, status, platform) VALUES('BV1', 1, 'queued', 'bilibili'),
                                                              ('BV2', 1, 'queued', 'bilibili');
        PRAGMA user_version=2;
    """)
    c.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "_conn", None)
    db.init()
    assert [t["id"] for t in db.queued_tasks()] == [1, 2]
    assert db._db().execute("PRAGMA user_version").fetchone()[0] == 3
    db._conn.close()

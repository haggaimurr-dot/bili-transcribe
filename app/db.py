"""SQLite 访问层：任务、结果、设置。"""
import json
import sqlite3
import threading
import time

from .config import DB_PATH

SCHEMA_VERSION = 3
QUEUE_KEY = "COALESCE(queue_order, id)"  # 等待队列的排序键

TASK_COLUMNS = [
    "id", "platform", "bvid", "part", "title", "part_title", "uploader", "duration_ms", "url",
    "status", "stage", "percent", "message", "source", "backend", "model",
    "opts_json", "error_json", "created_at", "finished_at",
]
ACTIVE_STATUSES = ("queued", "parsing", "subtitle", "downloading", "transcribing", "exporting")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def init() -> None:
    with _lock:
        c = _db()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bvid TEXT, part INTEGER, title TEXT, part_title TEXT, uploader TEXT,
                duration_ms INTEGER DEFAULT 0, url TEXT,
                status TEXT, stage TEXT, percent REAL DEFAULT 0, message TEXT,
                source TEXT, backend TEXT, model TEXT,
                opts_json TEXT, error_json TEXT,
                created_at REAL, finished_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
            CREATE TABLE IF NOT EXISTS results(task_id INTEGER PRIMARY KEY, json TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value_json TEXT);
            """
        )
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        if ver < 2:
            # v2：加 platform 列（bilibili / youtube / local），旧数据按 bvid 回填
            cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
            if "platform" not in cols:
                c.execute("ALTER TABLE tasks ADD COLUMN platform TEXT")
            c.execute("UPDATE tasks SET platform = CASE WHEN bvid='LOCAL' THEN 'local' ELSE 'bilibili' END "
                      "WHERE platform IS NULL")
        if ver < 3:
            # v3：加 queue_order 列，等待中的任务按它从小到大执行；为空时按 id
            cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
            if "queue_order" not in cols:
                c.execute("ALTER TABLE tasks ADD COLUMN queue_order REAL")
        if ver < SCHEMA_VERSION:
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        c.commit()


def _row(r: sqlite3.Row | dict | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["opts"] = json.loads(d.pop("opts_json") or "{}")
    d["error"] = json.loads(d.pop("error_json") or "null")
    return d


def insert_task(**fields) -> int:
    fields.setdefault("created_at", time.time())
    fields.setdefault("status", "queued")
    fields.setdefault("stage", "queued")
    if "opts" in fields:
        fields["opts_json"] = json.dumps(fields.pop("opts"), ensure_ascii=False)
    keys = list(fields)
    with _lock:
        c = _db()
        cur = c.execute(
            f"INSERT INTO tasks({','.join(keys)}) VALUES({','.join('?' * len(keys))})",
            [fields[k] for k in keys],
        )
        c.commit()
        return cur.lastrowid


def update_task(task_id: int, **fields) -> None:
    if "error" in fields:
        fields["error_json"] = json.dumps(fields.pop("error"), ensure_ascii=False)
    if not fields:
        return
    sets = ",".join(f"{k}=?" for k in fields)
    with _lock:
        c = _db()
        c.execute(f"UPDATE tasks SET {sets} WHERE id=?", [*fields.values(), task_id])
        c.commit()


def get_task(task_id: int) -> dict | None:
    with _lock:
        return _row(_db().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def list_tasks(q: str = "", status: str = "", page: int = 1, size: int = 20, platform: str = "") -> dict:
    where, args = [], []
    if platform:
        where.append("platform=?")
        args.append(platform)
    if q:
        where.append("(title LIKE ? OR part_title LIKE ? OR bvid LIKE ?)")
        args += [f"%{q}%"] * 3
    if status == "active":
        where.append(f"status IN ({','.join('?' * len(ACTIVE_STATUSES))})")
        args += list(ACTIVE_STATUSES)
    elif status:
        where.append("status=?")
        args.append(status)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    with _lock:
        c = _db()
        total = c.execute(f"SELECT COUNT(*) FROM tasks {w}", args).fetchone()[0]
        rows = c.execute(
            f"SELECT * FROM tasks {w} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*args, size, (max(page, 1) - 1) * size],
        ).fetchall()
    return {"total": total, "page": page, "size": size, "items": [_row(r) for r in rows]}


def delete_task(task_id: int) -> None:
    with _lock:
        c = _db()
        c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        c.execute("DELETE FROM results WHERE task_id=?", (task_id,))
        c.commit()


def count_opts_containing(fragment: str) -> int:
    with _lock:
        return _db().execute("SELECT COUNT(*) FROM tasks WHERE instr(opts_json, ?) > 0", (fragment,)).fetchone()[0]


def queued_tasks() -> list[dict]:
    """等待中的任务，按执行顺序排列；每条带 order（实际排序值）。"""
    with _lock:
        rows = _db().execute(
            f"SELECT *, {QUEUE_KEY} AS q_order FROM tasks WHERE status='queued' ORDER BY {QUEUE_KEY}, id"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        order = d.pop("q_order")
        t = _row(d)
        t["order"] = order
        out.append(t)
    return out


def next_queued(candidates: set[int]) -> int | None:
    """在给定的 id 里挑排序最靠前、且仍在等待中的任务。"""
    if not candidates:
        return None
    ids = list(candidates)
    with _lock:
        r = _db().execute(
            f"SELECT id FROM tasks WHERE status='queued' AND id IN ({','.join('?' * len(ids))}) "
            f"ORDER BY {QUEUE_KEY}, id LIMIT 1", ids).fetchone()
    return r[0] if r else None


def set_queue_order(pairs: list[tuple[int, float]]) -> None:
    with _lock:
        c = _db()
        c.executemany("UPDATE tasks SET queue_order=? WHERE id=?", [(o, i) for i, o in pairs])
        c.commit()


def mark_interrupted() -> int:
    """服务重启：把未结束的任务标记为失败。"""
    err = json.dumps({"code": "INTERRUPTED", "message": "服务重启，任务中断", "hint": "点「重试」重新执行"},
                     ensure_ascii=False)
    with _lock:
        c = _db()
        cur = c.execute(
            f"UPDATE tasks SET status='failed', error_json=?, finished_at=? "
            f"WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
            [err, time.time(), *ACTIVE_STATUSES],
        )
        c.commit()
        return cur.rowcount


def set_result(task_id: int, result: dict) -> None:
    with _lock:
        c = _db()
        c.execute("INSERT OR REPLACE INTO results(task_id, json) VALUES(?,?)",
                  (task_id, json.dumps(result, ensure_ascii=False)))
        c.commit()


def get_result(task_id: int) -> dict | None:
    with _lock:
        r = _db().execute("SELECT json FROM results WHERE task_id=?", (task_id,)).fetchone()
    return json.loads(r[0]) if r else None


def all_settings() -> dict:
    with _lock:
        rows = _db().execute("SELECT key, value_json FROM settings").fetchall()
    return {k: json.loads(v) for k, v in rows}


def set_settings(values: dict) -> None:
    with _lock:
        c = _db()
        c.executemany("INSERT OR REPLACE INTO settings(key, value_json) VALUES(?,?)",
                      [(k, json.dumps(v, ensure_ascii=False)) for k, v in values.items()])
        c.commit()

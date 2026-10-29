import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """把数据库和 Cookie 文件指到临时目录。"""
    from app import db, settings

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(settings, "COOKIES_PATH", tmp_path / "cookies.txt")
    db.init()
    yield tmp_path
    if db._conn:
        db._conn.close()

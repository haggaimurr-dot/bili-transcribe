"""路径与常量。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE_DIR = DATA / "cache"
RESULTS_DIR = DATA / "results"
LOG_DIR = DATA / "logs"
TASK_LOG_DIR = LOG_DIR / "tasks"
DB_PATH = DATA / "app.db"
COOKIES_PATH = DATA / "cookies.txt"
WEB_DIR = ROOT / "web"

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_TRIES = 11  # 8765–8775

MAX_DURATION_MS = 6 * 3600 * 1000
PARSE_CACHE_TTL = 600
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def ensure_dirs() -> None:
    for d in (DATA, CACHE_DIR, RESULTS_DIR, LOG_DIR, TASK_LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)

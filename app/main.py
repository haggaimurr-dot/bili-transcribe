"""入口：python -m app.main"""
import asyncio
import logging
import shutil
import socket
import sys
import threading
import webbrowser
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # 让 common/ 可导入

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app import db  # noqa: E402
from app.api import router  # noqa: E402
from app.config import DEFAULT_PORT, HOST, LOG_DIR, PORT_TRIES, WEB_DIR, ensure_dirs  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.events import bus  # noqa: E402
from app.tasks import manager  # noqa: E402


def setup_logging() -> None:
    ensure_dirs()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)


def check_deps() -> list[str]:
    import yt_dlp

    log = logging.getLogger("startup")
    missing = [b for b in ("ffmpeg", "ffprobe") if not shutil.which(b)]
    if missing:
        log.warning("缺少 %s，请安装：winget install Gyan.FFmpeg", "、".join(missing))
    log.info("yt-dlp 版本 %s", yt_dlp.version.__version__)
    return missing


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus.bind(asyncio.get_running_loop())
    db.init()
    manager.start()
    yield


app = FastAPI(title="bili-transcribe", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(AppError)
async def app_error_handler(_: Request, e: AppError):
    return JSONResponse(status_code=e.status, content={"error": e.to_dict()})


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


def pick_port() -> int:
    for port in range(DEFAULT_PORT, DEFAULT_PORT + PORT_TRIES):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"端口 {DEFAULT_PORT}–{DEFAULT_PORT + PORT_TRIES - 1} 都被占用")


def main() -> None:
    setup_logging()
    check_deps()
    port = pick_port()
    url = f"http://{HOST}:{port}/"
    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning")
    server = uvicorn.Server(config)

    def open_when_ready():
        import time

        while not server.started:
            time.sleep(0.2)
        logging.getLogger("startup").info("服务已启动：%s", url)
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)

    threading.Thread(target=open_when_ready, daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()

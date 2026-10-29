"""HTTP 接口。"""
import asyncio
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from . import bili, db, export, platforms, settings
from .asr import LANGS, REMOTE_MODELS, remote
from .asr import local as local_asr
from .config import LOCAL_ID, MEDIA_EXTS, UPLOAD_DIR
from .errors import AppError
from .events import ALL, bus
from .tasks import FINAL, manager

router = APIRouter(prefix="/api")


# ---------------- 解析 ----------------

class ParseIn(BaseModel):
    text: str


@router.post("/parse")
def parse(body: ParseIn):
    return platforms.parse(body.text)


# ---------------- 任务 ----------------

class TaskIn(BaseModel):
    text: str  # 原始链接，后端重新规范化，避免信任前端的 bvid
    parts: list[int] = Field(default_factory=list)
    force_asr: bool = False
    hotwords: list[str] = Field(default_factory=list)
    spk: bool = False
    backend: str = "local"
    model: str = "paraformer-zh"
    lang: str = "auto"  # auto | zh | en | ja | ko | yue


def _check_backend(backend: str, model: str) -> str:
    """校验后端和模型，返回中文识别实际使用的模型（非中文由任务按语言改用 sensevoice）。"""
    if backend not in ("local", "remote"):
        raise AppError("BAD_REQUEST", "未知的识别后端")
    if backend == "local" or model == "sensevoice":
        return "paraformer-zh"
    if model not in REMOTE_MODELS:
        raise AppError("BAD_REQUEST", f"远程不支持模型 {model}")
    return model


def _check_lang(lang: str) -> str:
    lang = (lang or "auto").lower()
    if lang not in LANGS:
        raise AppError("BAD_REQUEST", f"不支持的识别语言 {lang}", "支持 auto / zh / en / ja / ko / yue")
    return lang


@router.post("/tasks")
def create_tasks(body: TaskIn):
    meta = platforms.parse(body.text)
    valid = {p["index"] for p in meta["parts"]}
    parts = [p for p in dict.fromkeys(body.parts) if p in valid] or [meta["parts"][0]["index"]]
    body.model = _check_backend(body.backend, body.model)
    body.lang = _check_lang(body.lang)
    opts = body.model_dump(exclude={"text", "parts"})
    ids = manager.create(bvid=meta["bvid"], parts=parts, meta=meta, opts=opts, platform=meta["platform"])
    return {"ids": ids}


@router.post("/upload")
async def upload_task(file: UploadFile = File(...), hotwords: str = Form("[]"), spk: bool = Form(False),
                      backend: str = Form("local"), model: str = Form("paraformer-zh"), lang: str = Form("zh")):
    """上传本地音视频文件直接转录，不经过 B 站。"""
    name = Path(file.filename or "upload").name
    ext = Path(name).suffix.lower()
    if ext not in MEDIA_EXTS:
        raise AppError("BAD_FILE", f"不支持的文件类型 {ext or '（无扩展名）'}",
                       "支持常见视频（mp4/mkv/flv…）和音频（mp3/m4a/wav…）")
    model = _check_backend(backend, model)
    try:
        words = [str(w) for w in json.loads(hotwords)]
    except (ValueError, TypeError):
        words = hotwords.split()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dst = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
    with open(dst, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    if dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        raise AppError("BAD_FILE", "上传的文件是空的")
    lang = _check_lang(lang)
    opts = {"force_asr": True, "hotwords": words, "spk": spk, "backend": backend, "model": model,
            "lang": "zh" if lang == "auto" else lang, "upload_path": str(dst), "filename": name}
    meta = {"title": Path(name).stem, "uploader": None, "parts": [{"index": 1, "title": None, "duration": 0}]}
    ids = manager.create(bvid=LOCAL_ID, parts=[1], meta=meta, opts=opts)
    return {"ids": ids}


@router.get("/tasks")
def list_tasks(q: str = "", status: str = "", page: int = 1, size: int = 20, platform: str = ""):
    return db.list_tasks(q, status, page, min(max(size, 1), 100), platform)


def _task_or_404(task_id: int) -> dict:
    t = db.get_task(task_id)
    if not t:
        raise AppError("NOT_FOUND", "任务不存在", status=404)
    return t


@router.get("/tasks/{task_id}")
def get_task(task_id: int):
    t = _task_or_404(task_id)
    return {"task": t, "result": db.get_result(task_id)}


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int):
    _task_or_404(task_id)
    manager.cancel(task_id)
    return {"ok": True}


class RetryIn(BaseModel):
    force_asr: bool | None = None


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int, body: RetryIn | None = None):
    return {"id": manager.retry(task_id, body.force_asr if body else None)}


@router.delete("/tasks/{task_id}")
def delete_task(task_id: int):
    _task_or_404(task_id)
    manager.delete(task_id)
    return {"ok": True}


class IdsIn(BaseModel):
    ids: list[int]


@router.post("/tasks/delete")
def delete_tasks(body: IdsIn):
    for i in body.ids:
        if db.get_task(i):
            manager.delete(i)
    return {"ok": True}


@router.get("/tasks/{task_id}/log", response_class=PlainTextResponse)
def task_log(task_id: int):
    from .config import TASK_LOG_DIR

    p = TASK_LOG_DIR / f"{task_id}.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _download(content: bytes | str, name: str, media: str) -> Response:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return Response(content, media_type=media, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


MEDIA = {"txt": "text/plain; charset=utf-8", "srt": "application/x-subrip; charset=utf-8",
         "md": "text/markdown; charset=utf-8"}


@router.get("/tasks/{task_id}/export")
def export_task(task_id: int, fmt: str = "txt"):
    if fmt not in export.FORMATS:
        raise AppError("BAD_REQUEST", "格式只支持 txt / srt / md")
    t = _task_or_404(task_id)
    r = db.get_result(task_id)
    if not r:
        raise AppError("NOT_READY", "任务还没有结果")
    return _download(export.render(fmt, t, r), export.filename(t, fmt), MEDIA[fmt])


class ZipIn(BaseModel):
    task_ids: list[int]
    fmt: str = "txt"


@router.post("/export/zip")
def export_zip(body: ZipIn):
    if body.fmt not in (*export.FORMATS, "all"):
        raise AppError("BAD_REQUEST", "格式只支持 txt / srt / md / all")
    pairs = []
    for i in body.task_ids:
        t, r = db.get_task(i), db.get_result(i)
        if t and r:
            pairs.append((t, r))
    if not pairs:
        raise AppError("NOT_READY", "选中的任务都没有结果")
    return _download(export.build_zip(pairs, body.fmt), "transcripts.zip", "application/zip")


# ---------------- 进度推送 ----------------

def _sse(data: dict, event: str = "message") -> dict:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False)}


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: int, request: Request):
    t = _task_or_404(task_id)
    q = bus.subscribe(task_id)

    async def gen():
        try:
            yield _sse({"type": "task", "task": t})
            if t["status"] in FINAL:
                yield _sse({"type": "end", "id": task_id, "status": t["status"]}, "end")
                return
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=30)
                except asyncio.TimeoutError:
                    continue
                if ev.get("type") == "end":
                    yield _sse(ev, "end")
                    return
                yield _sse(ev)
        finally:
            bus.unsubscribe(task_id, q)

    return EventSourceResponse(gen(), ping=15)


@router.get("/events")
async def all_events(request: Request):
    q = bus.subscribe(ALL)

    async def gen():
        try:
            yield _sse({"type": "hello"})
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=30)
                except asyncio.TimeoutError:
                    continue
                yield _sse(ev)
        finally:
            bus.unsubscribe(ALL, q)

    return EventSourceResponse(gen(), ping=15)


# ---------------- 设置 ----------------

@router.get("/settings")
def get_settings():
    return settings.public(settings.load())


@router.put("/settings")
def put_settings(patch: dict):
    return settings.public(settings.update(patch))


@router.post("/settings/cookies")
async def upload_cookies(file: UploadFile = File(...)):
    raw = (await file.read()).decode("utf-8", errors="replace")
    settings.save_cookie_upload(raw)
    return settings.public(settings.load())


@router.delete("/settings/cookies")
def delete_cookies():
    settings.clear_cookies()
    return settings.public(settings.load())


@router.get("/settings/cookies/check")
def check_cookies():
    return settings.check_login()


@router.post("/settings/youtube-cookies")
async def upload_youtube_cookies(file: UploadFile = File(...)):
    raw = (await file.read()).decode("utf-8", errors="replace")
    settings.save_youtube_cookie(raw)
    return settings.public(settings.load())


@router.delete("/settings/youtube-cookies")
def delete_youtube_cookies():
    settings.clear_youtube_cookie()
    return settings.public(settings.load())


class ProxyIn(BaseModel):
    proxy: str | None = None


@router.post("/settings/proxy/check")
def check_proxy(body: ProxyIn):
    return settings.check_proxy(body.proxy)


# ---------------- 远程 / 状态 / 维护 ----------------

class HealthIn(BaseModel):
    url: str | None = None
    token: str | None = None


@router.get("/remote/health")
def remote_health():
    return remote.health()


@router.post("/remote/health")
def remote_health_test(body: HealthIn):
    token = body.token
    if token is not None and token.startswith("****"):
        token = None  # 掩码表示沿用已保存的 Token
    return remote.health(body.url, token)


@router.get("/status")
def status():
    import yt_dlp

    s = settings.load()
    return {
        "model": local_asr.state,
        "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        "ytdlp": yt_dlp.version.__version__,
        "cookie_configured": settings.cookie_file() is not None,
        "remote_configured": bool(s.remote_url and s.remote_token),
        "youtube_proxy": bool(s.youtube_proxy.strip()),
        "youtube_cookie": settings.youtube_cookie_file() is not None,
        "current_task": manager.current,
        "queued": manager.q.qsize(),
    }


@router.post("/maintenance/clear-cache")
def clear_cache():
    return {"removed": bili.clear_cache()}


@router.post("/maintenance/update-ytdlp")
def update_ytdlp():
    uv = shutil.which("uv")
    cmd = ([uv, "pip", "install", "-U", "yt-dlp", "--python", sys.executable] if uv
           else [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"])
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if r.returncode != 0:
        raise AppError("UPDATE_FAILED", "更新失败", (r.stderr or r.stdout)[-500:])
    return {"ok": True, "output": (r.stdout + r.stderr)[-500:], "hint": "重启服务后生效"}

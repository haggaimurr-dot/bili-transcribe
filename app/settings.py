"""设置读写、Cookie 文件管理。"""
import threading
import time

import httpx
from pydantic import BaseModel, Field

from . import db
from .config import COOKIES_PATH, UA
from .errors import AppError

SECRET_FIELDS = ("sessdata", "bili_jct", "dedeuserid", "remote_token")


class HotwordGroup(BaseModel):
    name: str
    words: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    cookie_source: str = "none"  # none | sessdata | file
    sessdata: str = ""
    bili_jct: str = ""
    dedeuserid: str = ""
    default_backend: str = "local"  # local | remote
    default_model: str = "paraformer-zh"
    default_spk: bool = False
    default_force_asr: bool = False
    auto_delete_audio: bool = True
    remote_url: str = "http://127.0.0.1:9000"
    remote_token: str = ""
    hotword_groups: list[HotwordGroup] = Field(default_factory=list)
    rtf_local: float = 0.1
    rtf_remote: float = 0.01


_lock = threading.Lock()
_listeners = []  # Cookie 变化时的回调（清解析缓存）


def on_cookie_change(fn) -> None:
    _listeners.append(fn)


def load() -> Settings:
    return Settings(**db.all_settings())


def save(s: Settings) -> None:
    db.set_settings(s.model_dump())


def mask(v: str) -> str:
    return ("****" + v[-4:]) if v else ""


def public(s: Settings) -> dict:
    d = s.model_dump()
    for k in SECRET_FIELDS:
        d[k] = mask(d[k])
    d["cookie_configured"] = s.cookie_source != "none" and COOKIES_PATH.exists()
    return d


def update(patch: dict) -> Settings:
    """前端提交的设置；值为掩码（****xxxx）的保密字段视为未修改。"""
    with _lock:
        cur = load()
        data = cur.model_dump()
        for k, v in patch.items():
            if k not in data:
                continue
            if k in SECRET_FIELDS and isinstance(v, str) and v.startswith("****"):
                continue
            data[k] = v
        new = Settings(**data)
        cookie_changed = any(getattr(new, k) != getattr(cur, k) for k in ("sessdata", "bili_jct", "dedeuserid"))
        if cookie_changed:
            if new.sessdata:
                write_cookie_file(new.sessdata, new.bili_jct, new.dedeuserid)
                new.cookie_source = "sessdata"
            else:
                COOKIES_PATH.unlink(missing_ok=True)
                new.cookie_source = "none"
        save(new)
    if cookie_changed:
        _notify()
    return new


def _notify() -> None:
    for fn in _listeners:
        fn()


def update_rtf(kind: str, measured: float) -> None:
    with _lock:
        s = load()
        key = f"rtf_{kind}"
        setattr(s, key, round(0.7 * getattr(s, key) + 0.3 * measured, 4))
        save(s)


# ---------------- Cookie ----------------

def write_cookie_file(sessdata: str, bili_jct: str = "", dedeuserid: str = "") -> None:
    expiry = int(time.time()) + 180 * 86400
    lines = ["# Netscape HTTP Cookie File", "# 由 bili-transcribe 生成"]
    for name, value in (("SESSDATA", sessdata), ("bili_jct", bili_jct), ("DedeUserID", dedeuserid)):
        value = value.strip()
        if value:
            lines.append("\t".join([".bilibili.com", "TRUE", "/", "FALSE", str(expiry), name, value]))
    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def filter_cookie_text(text: str) -> str:
    """校验上传的 cookies.txt，只保留 bilibili.com 的条目。"""
    kept = []
    for line in text.splitlines():
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain = parts[0].removeprefix("#HttpOnly_")
        if domain.lstrip(".").endswith("bilibili.com"):
            kept.append(line)
    if not kept:
        raise AppError("INVALID_COOKIE", "没有找到有效的 bilibili.com Cookie",
                       "请用浏览器扩展导出 Netscape 格式的 cookies.txt")
    return "# Netscape HTTP Cookie File\n" + "\n".join(kept) + "\n"


def save_cookie_upload(text: str) -> None:
    content = filter_cookie_text(text)
    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_PATH.write_text(content, encoding="utf-8")
    with _lock:
        s = load()
        s.cookie_source = "file"
        s.sessdata = s.bili_jct = s.dedeuserid = ""
        save(s)
    _notify()


def clear_cookies() -> None:
    COOKIES_PATH.unlink(missing_ok=True)
    with _lock:
        s = load()
        s.cookie_source = "none"
        s.sessdata = s.bili_jct = s.dedeuserid = ""
        save(s)
    _notify()


def cookie_file() -> str | None:
    """供 yt-dlp 使用的 cookiefile 路径；未配置返回 None。"""
    if load().cookie_source != "none" and COOKIES_PATH.exists():
        return str(COOKIES_PATH)
    return None


def cookie_dict() -> dict:
    out = {}
    if not cookie_file():
        return out
    for line in COOKIES_PATH.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 7 and not (line.startswith("#") and not line.startswith("#HttpOnly_")):
            out[parts[5]] = parts[6]
    return out


def check_login() -> dict:
    cookies = cookie_dict()
    if not cookies:
        return {"configured": False, "is_login": False}
    try:
        r = httpx.get("https://api.bilibili.com/x/web-interface/nav", cookies=cookies,
                      headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"}, timeout=8)
        data = r.json().get("data") or {}
    except Exception as e:  # noqa: BLE001
        return {"configured": True, "is_login": False, "error": f"校验请求失败：{e}"}
    if data.get("isLogin"):
        return {"configured": True, "is_login": True, "uname": data.get("uname", ""),
                "vip": bool(data.get("vipStatus"))}
    return {"configured": True, "is_login": False, "error": "Cookie 无效或已过期，请重新获取 SESSDATA"}

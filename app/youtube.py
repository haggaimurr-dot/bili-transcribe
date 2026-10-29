"""YouTube：链接解析、字幕、音频下载。只有这里的请求走代理（settings.youtube_proxy）。"""
import json
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import yt_dlp
from yt_dlp.utils import DownloadError

from . import bili, settings
from .config import UA
from .errors import AppError

ID_RE = re.compile(r"^[0-9A-Za-z_-]{11}$")
URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/[^\s\"'<>，。]*", re.I)

# 自动字幕逐词滚动，按停顿/时长重新合成句子
AUTO_GAP_MS = 1200  # 相邻两个词开始时间相差超过这个值，视为停顿
AUTO_MAX_MS = 8000  # 没有标点时，单句最长时长
AUTO_WORD_MS = 1000  # 估算单个词的最长发音时间
_END = set(".?!。？！")


# ---------------- 链接规范化 ----------------

def find_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None


def normalize(text: str) -> dict:
    """从任意文本里取出 YouTube 视频 ID，返回 {vid, start, url}。"""
    raw = find_url(text)
    if not raw:
        raise AppError("INVALID_URL", "没有识别到 YouTube 视频链接")
    u = urlparse(raw if raw.lower().startswith("http") else "https://" + raw)
    host = u.netloc.lower()
    qs = parse_qs(u.query)
    vid = None
    if host.endswith("youtu.be"):
        vid = u.path.strip("/").split("/")[0]
    elif u.path == "/watch":
        vid = (qs.get("v") or [None])[0]
    else:
        m = re.match(r"^/(?:shorts|live|embed|v|e)/([^/?#]+)", u.path)
        if m:
            vid = m.group(1)
    if not vid or not ID_RE.match(vid):
        if "list" in qs:
            raise AppError("INVALID_URL", "暂不支持整个播放列表", "请打开列表里的某个视频，复制它的链接")
        raise AppError("INVALID_URL", "没有识别到 YouTube 视频 ID", "支持 watch?v=、youtu.be、shorts、live 链接")
    start = _parse_t((qs.get("t") or qs.get("start") or [""])[0])
    return {"vid": vid, "start": start, "url": watch_url(vid)}


def _parse_t(t: str) -> int:
    if not t:
        return 0
    if t.isdigit():
        return int(t)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", t)
    if not m:
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def watch_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def time_url(vid: str, ms: int | None = None) -> str:
    return watch_url(vid) + (f"&t={int(ms) // 1000}s" if ms is not None else "")


def cache_key(vid: str) -> str:
    return f"yt_{vid}"


# ---------------- yt-dlp ----------------

def _ydl_opts(**extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "retries": 3, "fragment_retries": 3, "socket_timeout": 20,
            "js_runtimes": {"node": {}}}  # 解 YouTube 的 JS 挑战（配合 yt-dlp-ejs）
    proxy = settings.youtube_proxy()
    if proxy:
        opts["proxy"] = proxy
    cf = settings.youtube_cookie_file()
    if cf:
        opts["cookiefile"] = cf
    opts.update(extra)
    return opts


COOKIE_HINT = "在「设置 → YouTube」上传浏览器导出的 cookies.txt 后重试"


def map_error(e: Exception) -> AppError:
    msg = str(e)
    low = msg.lower()
    if "not a bot" in low or "sign in to confirm" in low:
        return AppError("YT_BOT_CHECK", "YouTube 要求登录验证（疑似机器人）", COOKIE_HINT + "，或换一个代理节点")
    if "age" in low and ("restricted" in low or "confirm your age" in low or "inappropriate" in low):
        return AppError("YT_AGE_RESTRICTED", "该视频有年龄限制", COOKIE_HINT)
    if "members-only" in low or "join this channel" in low or "members only" in low:
        return AppError("YT_MEMBERS_ONLY", "该视频仅限频道会员观看")
    if "private video" in low:
        return AppError("YT_PRIVATE", "这是私享视频，无法访问")
    if "live event will begin" in low or "this live event" in low or "premieres in" in low:
        return AppError("YT_LIVE_NOT_ENDED", "直播还没结束，暂不能转录", "等直播结束、回放生成后再试")
    if "video unavailable" in low or "has been removed" in low or "does not exist" in low:
        return AppError("VIDEO_NOT_FOUND", "视频不存在、已删除或在当前地区不可用")
    if ("proxy" in low or "unable to connect" in low or "timed out" in low or "connection" in low
            or "getaddrinfo" in low or "10060" in msg or "10061" in msg):
        if not settings.youtube_proxy():
            return AppError("PROXY_FAILED", "连不上 YouTube", "国内需要代理：在「设置 → YouTube」填代理地址，例如 http://127.0.0.1:7897")
        return AppError("PROXY_FAILED", "通过代理连接 YouTube 失败", "检查代理软件是否开着、端口是否正确，可在设置页点「测试代理」")
    return AppError("DOWNLOAD_FAILED", "获取 YouTube 视频失败：" + msg.replace("ERROR: ", "")[:200],
                    "可尝试更新下载组件、换代理节点或配置 Cookie")


def _extract(url: str) -> dict:
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True)) as ydl:
            return ydl.sanitize_info(ydl.extract_info(url, download=False))
    except DownloadError as e:
        raise map_error(e) from e


_cache = bili._ParseCache()
settings.on_cookie_change(_cache.clear)


def info(vid: str) -> dict:
    hit = _cache.get(vid)
    if hit:
        return hit
    data = _extract(watch_url(vid))
    if data.get("is_live") or data.get("live_status") in ("is_live", "is_upcoming"):
        raise AppError("YT_LIVE_NOT_ENDED", "直播还没结束，暂不能转录", "等直播结束、回放生成后再试")
    _cache.put(vid, data)
    return data


def norm_lang(code: str | None) -> str | None:
    """YouTube 语言代码 → 主语言：en-US→en，zh-Hans→zh，yue-HK→yue。"""
    if not code:
        return None
    code = code.removesuffix("-orig")
    return code.split("-")[0].lower()


_HAN = re.compile(r"[一-鿿]")
_KANA_HANGUL = re.compile(r"[぀-ヿ가-힯]")


def guess_lang(data: dict) -> str | None:
    """视频语言：优先用 YouTube 标注的 language；没标注时，标题是中文（有汉字、没有假名和谚文）就当中文。"""
    if data.get("language"):
        return data["language"]
    title = data.get("title") or ""
    if len(_HAN.findall(title)) >= 2 and not _KANA_HANGUL.search(title):
        return "zh"
    return None


def available_subs(data: dict) -> dict:
    """前端展示用：人工字幕语言列表、是否有原语言自动字幕。"""
    manual = list((data.get("subtitles") or {}).keys())
    manual = [k for k in manual if k != "live_chat"]
    auto = data.get("automatic_captions") or {}
    return {"manual": manual, "auto": bool(_pick_auto(auto, data.get("language")))}


def parse(text: str) -> dict:
    """POST /api/parse 的实现，结构和 B 站一致（parts 只有 1 个）。"""
    n = normalize(text)
    data = info(n["vid"])
    subs = available_subs(data)
    return {
        "platform": "youtube", "bvid": n["vid"], "title": data.get("title"),
        "uploader": data.get("channel") or data.get("uploader"), "cover": data.get("thumbnail"),
        "language": guess_lang(data), "language_tagged": bool(data.get("language")), "requested_p": None,
        "parts": [{"index": 1, "title": data.get("title"), "duration": data.get("duration") or 0,
                   "subtitles": subs["manual"] + (["自动字幕"] if subs["auto"] else [])}],
        "subs": subs,
    }


# ---------------- 字幕 ----------------

def _pick_auto(auto: dict, lang: str | None) -> str | None:
    """自动字幕只取原语言版本：优先 {lang}-orig，其次 {lang}；不取机器翻译出来的其他语言。"""
    if not auto:
        return None
    if lang:
        for k in (f"{lang}-orig", lang):
            if k in auto:
                return k
        base = norm_lang(lang)
        for k in auto:
            if k.endswith("-orig") and norm_lang(k) == base:
                return k
    origs = [k for k in auto if k.endswith("-orig")]
    return origs[0] if len(origs) == 1 else None


def pick_subtitle(data: dict):
    """人工字幕：视频原语言 → 中文 → 英文；然后原语言自动字幕。返回 (kind, lang, source) 或 None。"""
    manual = {k: v for k, v in (data.get("subtitles") or {}).items() if k != "live_chat"}
    lang = data.get("language")
    base = norm_lang(lang)
    prefs = []
    if lang:
        prefs += [lang] + [k for k in manual if norm_lang(k) == base]
    prefs += [k for k in manual if norm_lang(k) == "zh"]
    prefs += [k for k in manual if norm_lang(k) == "en"]
    for k in prefs:
        if k in manual:
            return "manual", k, "subtitle_yt"
    if manual and not lang and len(manual) == 1:
        return "manual", next(iter(manual)), "subtitle_yt"
    auto_key = _pick_auto(data.get("automatic_captions") or {}, lang)
    if auto_key:
        return "auto", auto_key, "subtitle_yt_auto"
    return None


def fetch_subtitle(data: dict, kind: str, lang: str) -> list[dict]:
    tracks = (data.get("subtitles") if kind == "manual" else data.get("automatic_captions")) or {}
    items = tracks.get(lang) or []
    by_ext = {}
    for it in items:
        by_ext.setdefault(it.get("ext"), it)
    for ext in ("json3", "srv3", "vtt", "srt"):
        it = by_ext.get(ext)
        if not it:
            continue
        raw = it.get("data") or _get(it["url"])
        if ext == "json3":
            sents = parse_json3(raw)
        elif ext == "srv3":
            continue  # 有 json3 时不会走到这里；srv3 是 XML，这里不解析
        elif ext == "vtt":
            sents = parse_vtt(raw)
        else:
            sents = bili.parse_srt(raw)
        if kind == "auto":
            sents = merge_auto(sents)
        return _dedup(sents)
    return []


def _get(url: str) -> str:
    r = httpx.get(url, proxy=settings.youtube_proxy(), timeout=20, headers={"User-Agent": UA})
    r.raise_for_status()
    return r.text


def parse_json3(raw: str) -> list[dict]:
    """YouTube json3：events[].tStartMs / dDurationMs / segs[].utf8（自动字幕的 segs 是逐词的）。"""
    data = json.loads(raw)
    out = []
    for ev in data.get("events") or []:
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue
        start = int(ev["tStartMs"])
        dur = int(ev.get("dDurationMs") or 0)
        words = []
        for s in segs:
            t = s.get("utf8", "")
            if t == "\n":
                continue
            words.append({"t": t, "off": int(s.get("tOffsetMs") or 0)})
        text = "".join(w["t"] for w in words).replace("\n", " ").strip()
        if not text:
            continue
        out.append({"start": start, "end": start + dur, "text": text,
                    "_words": [(start + w["off"], w["t"]) for w in words if w["t"].strip()]})
    return out


_VTT_TIME = re.compile(r"(\d+:)?(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d+:)?(\d{2}):(\d{2})\.(\d{3})")
_VTT_TAG = re.compile(r"<[^>]+>")


def parse_vtt(raw: str) -> list[dict]:
    out = []
    for block in re.split(r"\r?\n\s*\r?\n", raw.strip()):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            m = _VTT_TIME.search(line)
            if not m:
                continue
            g = m.groups()
            start = _ms(g[0], g[1], g[2], g[3])
            end = _ms(g[4], g[5], g[6], g[7])
            text = " ".join(_VTT_TAG.sub("", x).strip() for x in lines[i + 1:] if x.strip())
            if text:
                out.append({"start": start, "end": end, "text": text})
            break
    # 自动字幕的 vtt 每条会重复上一条的内容（滚动显示），去掉前缀重复
    cleaned = []
    for s in out:
        if cleaned and s["text"].startswith(cleaned[-1]["text"]) and s["text"] != cleaned[-1]["text"]:
            s = {**s, "text": s["text"][len(cleaned[-1]["text"]):].strip()}
        if s["text"]:
            cleaned.append(s)
    return cleaned


def _ms(h, m, s, ms) -> int:
    return ((int((h or "0:")[:-1] or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def _is_cjk(ch: str) -> bool:
    return "぀" <= ch <= "ヿ" or "㐀" <= ch <= "鿿" or "가" <= ch <= "힯"


def merge_auto(sents: list[dict]) -> list[dict]:
    """自动字幕：拆成逐词，再按停顿、句末标点或累计时长合成句子。

    自动字幕的 event 是滚动显示的，dDurationMs 会一直延续到后面几行出现，不能当作说话结束时间；
    所以每个词的结束时间取「下一个词的开始」（最多 1 秒），停顿用相邻两个词的开始时间之差判断。
    """
    words = []
    for s in sents:
        if s.get("_words"):
            words += s["_words"]
        else:
            words.append((s["start"], s["text"]))
    words = sorted((t, w.strip()) for t, w in words if w.strip())
    if not words:
        return []
    out, cur, prev_t = [], None, None
    for i, (t, w) in enumerate(words):
        nxt = words[i + 1][0] if i + 1 < len(words) else None
        w_end = min(nxt, t + AUTO_WORD_MS) if nxt is not None else t + AUTO_WORD_MS
        if cur and t - prev_t > AUTO_GAP_MS:
            out.append(cur)
            cur = None
        prev_t = t
        if cur is None:
            cur = {"start": t, "end": w_end, "text": w}
        else:
            sep = "" if (_is_cjk(cur["text"][-1]) and _is_cjk(w[0])) else " "
            cur["text"] += sep + w
            cur["end"] = max(cur["end"], w_end)
        if w[-1] in _END or cur["end"] - cur["start"] > AUTO_MAX_MS:
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return out


# 只有音效标记的行：[Music] [音乐] [Applause] [♪♪♪] ♪ 等
_NOISE = re.compile(r"^[\[(（【]?\s*(music|applause|laughter|音乐|掌声|笑声|[♪♫\s])+\s*[\])）】]?$", re.I)


def _dedup(sents: list[dict]) -> list[dict]:
    out = []
    for s in sents:
        s = {"start": int(s["start"]), "end": int(s["end"]), "text": re.sub(r"\s+", " ", s["text"]).strip()}
        if not s["text"] or _NOISE.match(s["text"]):
            continue
        if out and out[-1]["text"] == s["text"]:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
            continue
        out.append(s)
    return out


# ---------------- 音频 ----------------

def download_audio(vid: str, progress_cb, cancel_event: threading.Event, log) -> Path:
    return bili.fetch_audio(watch_url(vid), cache_key(vid),
                            _ydl_opts(format="bestaudio[ext=m4a]/bestaudio/best"),
                            progress_cb, cancel_event, log, map_error)

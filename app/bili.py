"""B 站：链接解析、字幕、音频下载与预处理。"""
import glob
import json
import re
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path

import httpx
import yt_dlp
from yt_dlp.utils import DownloadCancelled, DownloadError

from . import settings
from .config import CACHE_DIR, MAX_DURATION_MS, PARSE_CACHE_TTL, UA
from .errors import AppError, Cancelled, map_ytdlp_error

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
AV_RE = re.compile(r"(?<![0-9A-Za-z])av(\d+)", re.I)
SHORT_RE = re.compile(r"(?:https?://)?(?:b23\.tv|bili2233\.cn)/[0-9A-Za-z]+")
P_RE = re.compile(r"[?&]p=(\d+)")

CC_LANGS = ("zh-CN", "zh-Hans", "zh", "zh-Hant", "zh-TW", "zh-HK")


# ---------------- 链接规范化 ----------------

def normalize(text: str, expand=None) -> dict:
    """从任意文本中提取视频 ID，返回 {vid, p, url}。"""
    text = (text or "").strip()
    found = _find_id(text)
    if not found:
        m = SHORT_RE.search(text)
        if m:
            final = (expand or expand_short)(m.group(0))
            found = _find_id(final)
    if not found:
        raise AppError("INVALID_URL", "没有识别到 B 站视频链接",
                       "支持 BV 号、av 号、b23.tv 短链、bilibili.com 链接或分享文案")
    vid, p = found
    return {"vid": vid, "p": p, "url": f"https://www.bilibili.com/video/{vid}"}


def _find_id(text: str):
    m = BV_RE.search(text)
    if m:
        vid = m.group(0)
    else:
        m = AV_RE.search(text)
        if not m:
            return None
        vid = "av" + m.group(1)
    pm = P_RE.search(text[m.end():m.end() + 200])
    return vid, (int(pm.group(1)) if pm else None)


def expand_short(url: str) -> str:
    if not url.startswith("http"):
        url = "https://" + url
    try:
        r = httpx.get(url, follow_redirects=True, timeout=5, headers={"User-Agent": UA})
        return str(r.url)
    except httpx.HTTPError as e:
        raise AppError("INVALID_URL", f"短链展开失败：{e}", "检查网络，或直接粘贴 BV 号") from e


def part_url(vid: str, p: int) -> str:
    return f"https://www.bilibili.com/video/{vid}?p={p}"


# ---------------- 视频信息（带缓存） ----------------

class _ParseCache:
    def __init__(self, maxsize=64):
        self.data: OrderedDict = OrderedDict()
        self.maxsize = maxsize
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            item = self.data.get(key)
            if not item or time.time() - item[0] > PARSE_CACHE_TTL:
                self.data.pop(key, None)
                return None
            self.data.move_to_end(key)
            return item[1]

    def put(self, key, value):
        with self.lock:
            self.data[key] = (time.time(), value)
            self.data.move_to_end(key)
            while len(self.data) > self.maxsize:
                self.data.popitem(last=False)

    def clear(self):
        with self.lock:
            self.data.clear()


_cache = _ParseCache()
settings.on_cookie_change(_cache.clear)


def _ydl_opts(**extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "retries": 3,
            "fragment_retries": 3, "socket_timeout": 20}
    cf = settings.cookie_file()
    if cf:
        opts["cookiefile"] = cf
    opts.update(extra)
    return opts


def _extract(url: str) -> dict:
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True)) as ydl:
            return ydl.sanitize_info(ydl.extract_info(url, download=False))
    except DownloadError as e:
        raise map_ytdlp_error(e) from e


def subtitle_langs(subs: dict | None) -> list[str]:
    return [k for k in (subs or {}) if k != "danmaku"]


def _part_from_entry(entry: dict, index: int) -> dict:
    return {
        "index": index,
        "title": entry.get("title") or f"P{index}",
        "duration": entry.get("duration") or 0,
        "subtitles": subtitle_langs(entry.get("subtitles")),
    }


def parse(text: str) -> dict:
    """POST /api/parse 的实现。"""
    n = normalize(text)
    key = n["vid"] + (f"?p={n['p']}" if n["p"] else "")
    hit = _cache.get(key)
    if hit:
        return hit["summary"]
    info = _extract(part_url(n["vid"], n["p"]) if n["p"] else n["url"])
    entries = info.get("entries")
    if entries:
        entries = [e for e in entries if e]
        parts = [_part_from_entry(e, i + 1) for i, e in enumerate(entries)]
        first = entries[0]
        title = info.get("title") or first.get("title")
        uploader = info.get("uploader") or first.get("uploader")
        cover = info.get("thumbnail") or first.get("thumbnail")
        for i, e in enumerate(entries):
            _cache.put(f"{n['vid']}?p={i + 1}", {"entry": e, "summary": None})
    else:
        p = n["p"] or 1
        parts = [_part_from_entry(info, p)]
        title, uploader, cover = info.get("title"), info.get("uploader"), info.get("thumbnail")
        _cache.put(f"{n['vid']}?p={p}", {"entry": info, "summary": None})
    summary = {"bvid": n["vid"], "title": title, "uploader": uploader, "cover": cover, "parts": parts,
               "requested_p": n["p"]}
    _cache.put(key, {"entry": info, "summary": summary})
    return summary


def part_info(vid: str, p: int) -> dict:
    """任务执行时取单个分P的完整信息（含字幕数据）。"""
    hit = _cache.get(f"{vid}?p={p}")
    if hit:
        return hit["entry"]
    info = _extract(part_url(vid, p))
    if info.get("entries"):
        es = [e for e in info["entries"] if e]
        info = es[min(p, len(es)) - 1] if len(es) > 1 else es[0]
    _cache.put(f"{vid}?p={p}", {"entry": info, "summary": None})
    return info


# ---------------- 字幕 ----------------

def pick_subtitle(subs: dict | None):
    """按 人工 CC 中文 → AI 中文 → 其他中文 选字幕，返回 (lang, source) 或 None。"""
    langs = subtitle_langs(subs)
    for lang in CC_LANGS:
        if lang in langs:
            return lang, "subtitle_cc"
    if "ai-zh" in langs:
        return "ai-zh", "subtitle_ai"
    for lang in langs:
        if "zh" in lang.lower():
            return lang, ("subtitle_ai" if lang.startswith("ai-") else "subtitle_cc")
    return None


def fetch_subtitle(subs: dict, lang: str) -> list[dict]:
    items = subs.get(lang) or []
    raw = None
    for it in items:
        if it.get("data"):
            raw = it["data"]
            break
    if raw is None:
        for it in items:
            if it.get("url"):
                url = it["url"]
                url = ("https:" + url) if url.startswith("//") else url
                r = httpx.get(url, timeout=15, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                              cookies=settings.cookie_dict())
                r.raise_for_status()
                raw = r.text
                break
    if raw is None:
        return []
    return parse_subtitle_text(raw)


def parse_subtitle_text(raw: str) -> list[dict]:
    raw = raw.lstrip("﻿").strip()
    if raw.startswith("{"):
        body = json.loads(raw).get("body") or []
        sents = [{"start": int(round(float(b["from"]) * 1000)), "end": int(round(float(b["to"]) * 1000)),
                  "text": str(b.get("content", "")).strip()} for b in body]
    else:
        sents = parse_srt(raw)
    out = []
    for s in sents:
        if not s["text"]:
            continue
        if out and out[-1]["text"] == s["text"]:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
            continue
        out.append(s)
    return out


_SRT_TIME = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def parse_srt(raw: str) -> list[dict]:
    out = []
    for block in re.split(r"\r?\n\s*\r?\n", raw.strip()):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            m = _SRT_TIME.search(line)
            if m:
                g = m.groups()
                start = _ms(*g[:4])
                end = _ms(*g[4:])
                text = " ".join(x.strip() for x in lines[i + 1:] if x.strip())
                out.append({"start": start, "end": end, "text": text})
                break
    return out


def _ms(h, m, s, frac) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0")[:3])


# ---------------- 音频下载与预处理 ----------------

def cache_key(vid: str, p: int) -> str:
    return f"{vid}_p{p}"


def download_audio(vid: str, p: int, progress_cb, cancel_event: threading.Event, log) -> Path:
    return fetch_audio(part_url(vid, p), cache_key(vid, p), _ydl_opts(format="bestaudio/best"),
                       progress_cb, cancel_event, log, map_ytdlp_error)


def fetch_audio(url: str, key: str, ydl_opts: dict, progress_cb, cancel_event: threading.Event, log,
                map_error) -> Path:
    """用 yt-dlp 只下载音频并转成 16k 单声道 wav（B 站和 YouTube 共用）。"""
    wav = CACHE_DIR / f"{key}.wav"
    if wav.exists() and wav.stat().st_size > 1000:
        log("已有音频缓存，跳过下载")
        progress_cb(100)
        return wav

    def hook(d):
        if cancel_event.is_set():
            raise DownloadCancelled()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                progress_cb(min(99.0, d.get("downloaded_bytes", 0) * 100 / total))

    opts = {**ydl_opts, "outtmpl": str(CACHE_DIR / f"{key}.%(ext)s"), "progress_hooks": [hook], "overwrites": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            reqs = info.get("requested_downloads") or []
            src = Path(reqs[0]["filepath"]) if reqs else Path(ydl.prepare_filename(info))
    except DownloadCancelled as e:
        _cleanup_parts(key)
        raise Cancelled() from e
    except DownloadError as e:
        _cleanup_parts(key)
        if cancel_event.is_set():
            raise Cancelled() from e
        raise map_error(e) from e
    progress_cb(100)
    log(f"音频下载完成：{src.name}（{src.stat().st_size / 1e6:.1f} MB）")
    to_wav(src, wav)
    if src != wav:
        src.unlink(missing_ok=True)
    return wav


def _cleanup_parts(key: str) -> None:
    for f in glob.glob(str(CACHE_DIR / f"{key}.*.part")) + glob.glob(str(CACHE_DIR / f"{key}.*.ytdl")):
        Path(f).unlink(missing_ok=True)


def _run_ffmpeg(args: list[str]) -> None:
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        tail = "\n".join(r.stderr.strip().splitlines()[-20:])
        raise AppError("FFMPEG_FAILED", "音频转换失败", tail)


def to_wav(src: Path, dst: Path) -> None:
    tmp = dst.with_suffix(".tmp.wav")
    _run_ffmpeg(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                 "-vn", "-ac", "1", "-ar", "16000", str(tmp)])
    tmp.replace(dst)


def to_opus(src: Path) -> Path:
    dst = src.with_suffix(".ogg")
    if not dst.exists():
        _run_ffmpeg(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                     "-c:a", "libopus", "-b:a", "32k", str(dst)])
    return dst


def probe_duration_ms(path: Path) -> int:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        ms = int(float(r.stdout.strip()) * 1000)
    except ValueError:
        ms = 0
    if ms <= 0:
        raise AppError("FFMPEG_FAILED", "无法读取音频时长", "音频文件可能已损坏，请重试")
    if ms > MAX_DURATION_MS:
        raise AppError("DOWNLOAD_FAILED", "音频超过 6 小时上限")
    return ms


def remove_audio(vid: str, p: int) -> None:
    remove_audio_key(cache_key(vid, p))


def remove_audio_key(key: str) -> None:
    for f in glob.glob(str(CACHE_DIR / f"{glob.escape(key)}.*")):
        Path(f).unlink(missing_ok=True)


def clear_cache() -> int:
    n = 0
    for f in CACHE_DIR.glob("*"):
        if f.is_file():
            f.unlink(missing_ok=True)
            n += 1
    return n

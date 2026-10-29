"""平台层：按链接判断来源（B 站 / YouTube），统一解析入口和跳转链接。"""
from . import bili, youtube
from .config import LOCAL_ID
from .errors import AppError

BILIBILI, YOUTUBE, LOCAL = "bilibili", "youtube", "local"
LABEL = {BILIBILI: "B站", YOUTUBE: "YouTube", LOCAL: "本地文件"}


def detect(text: str) -> str:
    if youtube.find_url(text):
        return YOUTUBE
    t = text or ""
    if bili.BV_RE.search(t) or bili.AV_RE.search(t) or bili.SHORT_RE.search(t) or "bilibili.com" in t:
        return BILIBILI
    raise AppError("INVALID_URL", "没有识别到 B 站或 YouTube 视频链接",
                   "支持 BV 号、b23.tv 短链、bilibili.com 链接，以及 youtube.com / youtu.be 链接")


def parse(text: str) -> dict:
    if detect(text) == YOUTUBE:
        return youtube.parse(text)
    r = bili.parse(text)
    return {**r, "platform": BILIBILI}


def platform_of(task: dict) -> str:
    p = task.get("platform")
    if p:
        return p
    return LOCAL if task.get("bvid") == LOCAL_ID else BILIBILI


def video_url(platform: str, vid: str, part: int = 1) -> str:
    if platform == YOUTUBE:
        return youtube.watch_url(vid)
    if platform == LOCAL:
        return ""
    return bili.part_url(vid, part)


def task_url(task: dict) -> str:
    return video_url(platform_of(task), task["bvid"], task.get("part") or 1)


def time_url(task: dict, ms: int) -> str:
    """跳到视频某个时间点的链接；本地文件返回空串。"""
    p = platform_of(task)
    if p == YOUTUBE:
        return youtube.time_url(task["bvid"], ms)
    if p == LOCAL:
        return ""
    return f"{bili.part_url(task['bvid'], task.get('part') or 1)}&t={int(ms) // 1000}"

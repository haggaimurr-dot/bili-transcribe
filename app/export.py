"""结果导出：txt / srt / md。"""
import io
import re
import zipfile

from .config import LOCAL_ID, RESULTS_DIR

SOURCE_LABEL = {
    "subtitle_cc": "B站 CC 字幕",
    "subtitle_ai": "B站 AI 字幕",
    "asr_local": "本地识别",
    "asr_remote": "远程 GPU 识别",
}
FORMATS = ("txt", "srt", "md")
PARA_GAP_MS = 2000
SRT_MAX_CHARS = 40
_SPLIT_RE = re.compile(r"(?<=[，。！？；、,.!?;])")


def fmt_clock(ms: int) -> str:
    s = max(0, int(ms)) // 1000
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_srt_time(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def paragraphs(sentences: list[dict]) -> list[list[dict]]:
    """间隔超过 2 秒或换了说话人就分段。"""
    paras: list[list[dict]] = []
    for s in sentences:
        if paras:
            last = paras[-1][-1]
            if s["start"] - last["end"] <= PARA_GAP_MS and s.get("spk") == last.get("spk"):
                paras[-1].append(s)
                continue
        paras.append([s])
    return paras


def _join(texts: list[str]) -> str:
    out = ""
    for t in texts:
        if out and re.match(r"[A-Za-z0-9]", t[:1]) and re.search(r"[A-Za-z0-9]$", out):
            out += " "
        out += t
    return out


def _spk_name(spk) -> str:
    return f"说话人{int(spk) + 1}"


def task_url(task: dict) -> str:
    """B 站任务返回视频地址；本地上传返回空串。"""
    if task["bvid"] == LOCAL_ID:
        return ""
    return f"https://www.bilibili.com/video/{task['bvid']}?p={task['part']}"


def display_title(task: dict) -> str:
    t = task.get("title") or task.get("bvid") or ""
    pt = task.get("part_title")
    return f"{t} - {pt}" if pt and pt != t else t


def to_txt(task: dict, result: dict) -> str:
    lines = [display_title(task)]
    if task_url(task):
        lines.append(task_url(task))
    lines += [f"来源：{SOURCE_LABEL.get(result['source'], result['source'])}", ""]
    for p in paragraphs(result["sentences"]):
        text = _join([s["text"] for s in p])
        if "spk" in p[0]:
            text = f"{_spk_name(p[0]['spk'])}：{text}"
        lines += [text, ""]
    return "\n".join(lines).rstrip() + "\n"


def split_long(s: dict, limit: int = SRT_MAX_CHARS) -> list[dict]:
    """超过 limit 字的句子按标点拆开，时间按字数比例分配。"""
    text = s["text"]
    if len(text) <= limit:
        return [s]
    pieces, buf = [], ""
    for seg in [x for x in _SPLIT_RE.split(text) if x]:
        if buf and len(buf) + len(seg) > limit:
            pieces.append(buf)
            buf = ""
        while len(seg) > limit:  # 没有标点的超长片段硬切
            pieces.append(seg[:limit])
            seg = seg[limit:]
        buf += seg
    if buf:
        pieces.append(buf)
    total = sum(len(p) for p in pieces)
    dur = max(0, s["end"] - s["start"])
    out, cur = [], s["start"]
    for i, p in enumerate(pieces):
        end = s["end"] if i == len(pieces) - 1 else cur + round(dur * len(p) / total)
        d = {**s, "start": cur, "end": end, "text": p}
        out.append(d)
        cur = end
    return out


def to_srt(task: dict, result: dict) -> str:
    items = []
    for s in result["sentences"]:
        items += split_long(s)
    blocks = []
    for i, s in enumerate(items, 1):
        text = s["text"]
        if "spk" in s:
            text = f"[{_spk_name(s['spk'])}] {text}"
        blocks.append(f"{i}\n{fmt_srt_time(s['start'])} --> {fmt_srt_time(s['end'])}\n{text}\n")
    return "\n".join(blocks)


def to_md(task: dict, result: dict) -> str:
    url = task_url(task)
    meta = [
        f"> 链接：{url}" if url else f"> 文件：{task.get('opts', {}).get('filename') or task.get('title')}",
        f"> UP 主：{task.get('uploader') or '-'}",
        f"> 时长：{fmt_clock(result.get('duration_ms') or task.get('duration_ms') or 0)}",
        f"> 来源：{SOURCE_LABEL.get(result['source'], result['source'])}",
    ]
    if result["source"].startswith("asr"):
        meta.append(f"> 模型：{result.get('model', '')}")
    lines = [f"# {display_title(task)}", "", "  \n".join(meta), ""]
    no_ts = result.get("no_timestamp")
    for p in paragraphs(result["sentences"]):
        text = _join([s["text"] for s in p])
        prefix = ""
        if not no_ts and url:
            sec = p[0]["start"] // 1000
            prefix = f"[\\[{fmt_clock(p[0]['start'])}\\]]({url}&t={sec}) "
        elif not no_ts:
            prefix = f"\\[{fmt_clock(p[0]['start'])}\\] "
        if "spk" in p[0]:
            prefix += f"**{_spk_name(p[0]['spk'])}**："
        lines += [prefix + text, ""]
    return "\n".join(lines).rstrip() + "\n"


RENDER = {"txt": to_txt, "srt": to_srt, "md": to_md}


def render(fmt: str, task: dict, result: dict) -> str:
    return RENDER[fmt](task, result)


_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_filename(name: str, limit: int = 80) -> str:
    name = _BAD.sub("_", name).strip(" ._") or "untitled"
    return name[:limit]


def filename(task: dict, fmt: str) -> str:
    return f"{safe_filename(task.get('title') or task['bvid'])}_P{task['part']}.{fmt}"


def write_all(task: dict, result: dict) -> None:
    d = RESULTS_DIR / str(task["id"])
    d.mkdir(parents=True, exist_ok=True)
    for fmt in FORMATS:
        (d / filename(task, fmt)).write_text(render(fmt, task, result), encoding="utf-8")


def build_zip(pairs: list[tuple[dict, dict]], fmt: str) -> bytes:
    buf = io.BytesIO()
    used = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for task, result in pairs:
            fmts = FORMATS if fmt == "all" else (fmt,)
            for f in fmts:
                name = filename(task, f)
                if name in used:
                    name = f"{task['id']}_{name}"
                used.add(name)
                z.writestr(name, render(f, task, result))
    return buf.getvalue()

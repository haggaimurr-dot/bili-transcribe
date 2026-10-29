"""FunASR 输出 → 统一结果结构。本地 LocalASR 和远程 worker 共用，保证两边一致。"""
import re

MAX_HOTWORDS = 100
MAX_HOTWORD_LEN = 10


def clean_hotwords(words) -> tuple[str, list[str]]:
    """去重、截断，返回 (空格拼接的热词串, 提示信息列表)。"""
    if isinstance(words, str):
        words = words.split()
    seen, out, notes = set(), [], []
    for w in words or []:
        w = str(w).strip()
        if not w or w in seen:
            continue
        if len(w) > MAX_HOTWORD_LEN:
            notes.append(f"热词「{w}」超过 {MAX_HOTWORD_LEN} 字，已截断")
            w = w[:MAX_HOTWORD_LEN]
        seen.add(w)
        out.append(w)
    if len(out) > MAX_HOTWORDS:
        notes.append(f"热词超过 {MAX_HOTWORDS} 个，只使用前 {MAX_HOTWORDS} 个")
        out = out[:MAX_HOTWORDS]
    return " ".join(out), notes


def sentences_from_funasr(res) -> tuple[list[dict], bool]:
    """返回 (sentences, no_timestamp)。"""
    item = (res or [{}])[0] if isinstance(res, list) else (res or {})
    info = item.get("sentence_info") or []
    sents = []
    for s in info:
        text = str(s.get("text", "")).strip()
        if not text:
            continue
        d = {"start": int(s.get("start", 0)), "end": int(s.get("end", 0)), "text": text}
        if "spk" in s and s["spk"] is not None:
            d["spk"] = int(s["spk"])
        sents.append(d)
    if sents:
        return sents, False
    text = str(item.get("text", "")).strip()
    return ([{"start": 0, "end": 0, "text": text}] if text else []), True


SENSEVOICE_LANGS = ("zh", "en", "ja", "ko", "yue")
_TAG_RE = re.compile(r"<\|[^|]*\|>")
_END_PUNCT = set(".?!。？！")
_GAP_MS = 1500  # 两个字之间停顿超过这个值，即使没有标点也断句
_MAX_SENT_MS = 15000  # 单句超过这个时长，遇到逗号就断


def strip_tags(text: str) -> str:
    """去掉 SenseVoice 输出里的 <|en|><|NEUTRAL|><|Speech|><|withitn|> 等标签。"""
    return _TAG_RE.sub("", text or "").strip()


def sentences_from_sensevoice(res) -> tuple[list[dict], bool]:
    """SenseVoice（output_timestamp=True）→ 句子。

    words 是逐字/逐词 token，timestamp 与之一一对应；句子文本从带 ITN 的整段文本里按位置截取，
    这样「50」不会被拆成「5 0」，韩语、英文的空格也保持原样。
    """
    sents: list[dict] = []
    items = res if isinstance(res, list) else [res or {}]
    for item in items:
        text = strip_tags(str(item.get("text", "")))
        words = item.get("words") or []
        stamps = item.get("timestamp") or []
        if not text:
            continue
        if not words or len(words) != len(stamps):
            sents.append({"start": 0, "end": 0, "text": text})
            continue
        pos, cur = 0, None  # cur = [首字起点, 末字终点, 文本起始下标, 文本结束下标]
        for w, (st, en) in zip(words, stamps):
            w = strip_tags(str(w))
            if not w:
                continue
            idx = text.find(w, pos)
            if idx < 0:  # 对不上（ITN 改写等），只更新时间
                if cur:
                    cur[1] = en
                continue
            if cur and st - cur[1] > _GAP_MS:
                sents.append(_cut(text, cur))
                cur = None
            if cur is None:
                cur = [st, en, idx, idx + len(w)]
            else:
                cur[1], cur[3] = en, idx + len(w)
            pos = idx + len(w)
            is_end = w[-1] in _END_PUNCT or (w[-1] in ",，" and cur[1] - cur[0] > _MAX_SENT_MS)
            if is_end:
                sents.append(_cut(text, cur))
                cur = None
        if cur:
            cur[3] = max(cur[3], len(text.rstrip()))  # 末尾没对上的字符并入最后一句
            sents.append(_cut(text, cur))
    sents = [s for s in sents if s["text"]]
    no_ts = bool(sents) and all(s["end"] == 0 for s in sents)
    return sents, no_ts


def _cut(text: str, cur: list) -> dict:
    return {"start": int(cur[0]), "end": int(cur[1]), "text": text[cur[2]:cur[3]].strip()}


def make_result(source: str, model: str, duration_ms: int, sentences: list[dict],
                no_timestamp: bool = False) -> dict:
    r = {"source": source, "model": model, "duration_ms": int(duration_ms), "sentences": sentences}
    if no_timestamp:
        r["no_timestamp"] = True
    return r

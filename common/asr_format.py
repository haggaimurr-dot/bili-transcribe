"""FunASR 输出 → 统一结果结构。本地 LocalASR 和远程 worker 共用，保证两边一致。"""

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


def make_result(source: str, model: str, duration_ms: int, sentences: list[dict],
                no_timestamp: bool = False) -> dict:
    r = {"source": source, "model": model, "duration_ms": int(duration_ms), "sentences": sentences}
    if no_timestamp:
        r["no_timestamp"] = True
    return r

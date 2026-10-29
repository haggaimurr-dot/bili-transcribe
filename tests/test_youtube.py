"""YouTube 接入：链接、平台识别、字幕选择与解析、SenseVoice 切句、语言路由、数据库迁移、流水线。"""
import json
import sqlite3

import pytest

from app import bili, db, export, platforms, tasks, youtube
from app.errors import AppError
from common.asr_format import sentences_from_sensevoice, strip_tags

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize("text,start", [
    (f"https://www.youtube.com/watch?v={VID}", 0),
    (f"https://youtu.be/{VID}?t=42", 42),
    (f"https://m.youtube.com/watch?v={VID}&list=PL123&index=2", 0),
    (f"https://www.youtube.com/shorts/{VID}", 0),
    (f"https://www.youtube.com/live/{VID}?si=abc", 0),
    (f"https://www.youtube.com/embed/{VID}", 0),
    (f"https://music.youtube.com/watch?v={VID}&t=1m30s", 90),
    (f"看看这个 youtube.com/watch?v={VID}，很好笑", 0),
])
def test_normalize(text, start):
    n = youtube.normalize(text)
    assert n["vid"] == VID and n["start"] == start
    assert n["url"] == f"https://www.youtube.com/watch?v={VID}"


def test_normalize_errors():
    with pytest.raises(AppError) as e:
        youtube.normalize("https://www.youtube.com/playlist?list=PL123")
    assert "播放列表" in e.value.message
    with pytest.raises(AppError):
        youtube.normalize("https://www.youtube.com/watch?v=short")


def test_detect():
    assert platforms.detect(f"https://youtu.be/{VID}") == "youtube"
    assert platforms.detect("BV1GJ411x7h7") == "bilibili"
    assert platforms.detect("https://b23.tv/AbCd123") == "bilibili"
    with pytest.raises(AppError):
        platforms.detect("https://vimeo.com/123")


def test_time_url_and_filename():
    yt = {"platform": "youtube", "bvid": VID, "part": 1, "title": "Never/Gonna"}
    bl = {"platform": "bilibili", "bvid": "BV1GJ411x7h7", "part": 2, "title": "t"}
    lo = {"platform": None, "bvid": "LOCAL", "part": 1, "title": "t"}
    assert platforms.time_url(yt, 61500) == f"https://www.youtube.com/watch?v={VID}&t=61s"
    assert platforms.time_url(bl, 61500) == "https://www.bilibili.com/video/BV1GJ411x7h7?p=2&t=61"
    assert platforms.time_url(lo, 1000) == ""
    assert export.filename(yt, "srt") == "Never_Gonna.srt"
    assert export.filename(bl, "srt") == "t_P2.srt"


def _info(manual=(), auto=(), lang="en"):
    return {"language": lang,
            "subtitles": {k: [{"ext": "json3", "data": "{}"}] for k in manual},
            "automatic_captions": {k: [{"ext": "json3", "data": "{}"}] for k in auto}}


def test_pick_subtitle():
    # 人工字幕：原语言优先
    assert youtube.pick_subtitle(_info(["de", "en-US", "zh-Hans"])) == ("manual", "en-US", "subtitle_yt")
    # 原语言没有人工字幕：中文 > 英文
    assert youtube.pick_subtitle(_info(["en", "zh-Hant"], lang="ja")) == ("manual", "zh-Hant", "subtitle_yt")
    # 没有人工字幕：原语言自动字幕，不取机翻
    assert youtube.pick_subtitle(_info([], ["zh-Hans", "en-orig", "en"])) == ("auto", "en-orig", "subtitle_yt_auto")
    assert youtube.pick_subtitle(_info([], ["zh-Hans", "fr"], lang="en")) is None
    # 没标语言：只有一个 -orig 时用它
    assert youtube.pick_subtitle(_info([], ["ja-orig", "en"], lang=None)) == ("auto", "ja-orig", "subtitle_yt_auto")
    assert youtube.pick_subtitle(_info([], [], lang="en")) is None


def test_guess_lang_and_noise():
    assert youtube.guess_lang({"language": "en-US", "title": "中文标题"}) == "en-US"
    assert youtube.guess_lang({"language": None, "title": "【科普】地震是怎么发生的"}) == "zh"
    assert youtube.guess_lang({"language": None, "title": "日本の料理 レシピ"}) is None
    assert youtube.guess_lang({"language": None, "title": "Cooking 101"}) is None
    s = youtube._dedup([{"start": 0, "end": 1, "text": "[♪♪♪]"}, {"start": 1, "end": 2, "text": "[Music]"},
                        {"start": 2, "end": 3, "text": "♪ We're no strangers ♪"}])
    assert [x["text"] for x in s] == ["♪ We're no strangers ♪"]


def test_json3_manual_and_auto_merge():
    manual = {"events": [
        {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "Hello there."}]},
        {"tStartMs": 2000, "dDurationMs": 1000, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 3000, "dDurationMs": 1500, "segs": [{"utf8": "General Kenobi!"}]},
    ]}
    s = youtube.parse_json3(json.dumps(manual))
    assert [(x["start"], x["end"], x["text"]) for x in s] == [(0, 2000, "Hello there."), (3000, 4500, "General Kenobi!")]

    auto = {"events": [
        {"tStartMs": 0, "dDurationMs": 1800, "segs": [{"utf8": "we're"}, {"utf8": " no", "tOffsetMs": 300},
                                                       {"utf8": " strangers", "tOffsetMs": 700}]},
        {"tStartMs": 1800, "dDurationMs": 900, "segs": [{"utf8": " to", "tOffsetMs": 0}, {"utf8": " love", "tOffsetMs": 400}]},
        {"tStartMs": 5000, "dDurationMs": 1000, "segs": [{"utf8": "you", "tOffsetMs": 0}, {"utf8": " know", "tOffsetMs": 300}]},
    ]}
    merged = youtube.merge_auto(youtube.parse_json3(json.dumps(auto)))
    assert [m["text"] for m in merged] == ["we're no strangers to love", "you know"]
    assert merged[0]["start"] == 0 and merged[1]["start"] == 5000


def test_auto_merge_rolling_events_do_not_overlap():
    # 真实自动字幕：每个 event 的 dDurationMs 会一直延续到后面的行出现（这里每个都长 6 秒）
    events = []
    t = 0
    for line in ["so today we", "are going to", "cook pasta"]:
        segs = [{"utf8": (" " if j else "") + w, "tOffsetMs": j * 350} for j, w in enumerate(line.split())]
        events.append({"tStartMs": t, "dDurationMs": 6000, "segs": segs})
        t += 1100
    events.append({"tStartMs": 9000, "dDurationMs": 6000, "segs": [{"utf8": "next"}, {"utf8": " part", "tOffsetMs": 300}]})
    merged = youtube.merge_auto(youtube.parse_json3(json.dumps({"events": events})))
    assert [m["text"] for m in merged] == ["so today we are going to cook pasta", "next part"]
    assert all(a["end"] <= b["start"] for a, b in zip(merged, merged[1:]))
    assert merged[0]["end"] == 2550 + 1000  # 最后一个词 pasta 在 2550ms 开始，最多算 1 秒，而不是 event 的 6 秒


def test_split_long_keeps_english_words():
    s = {"start": 0, "end": 10000, "text": "the most important tip I have is before you do anything read the recipe first"}
    parts = export.split_long(s, 40)
    assert all(len(p["text"]) <= 40 for p in parts)
    assert " ".join(p["text"] for p in parts) == s["text"]


def test_vtt_rolling_dedup():
    raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nhello <c>world</c>\n\n"
           "00:00:02.000 --> 00:00:04.000\nhello world how are you\n")
    s = youtube.parse_vtt(raw)
    assert [x["text"] for x in s] == ["hello world", "how are you"]


def test_sensevoice_sentences():
    res = [{"text": "<|en|><|NEUTRAL|><|Speech|><|withitn|>Hello world. I have 50 pieces!",
            "words": ["Hello", "world", ".", "I", "have", "5", "0", "pieces", "!"],
            "timestamp": [[100, 300], [350, 600], [600, 620], [3000, 3100], [3150, 3300],
                          [3400, 3450], [3450, 3500], [3600, 3900], [3900, 3920]]}]
    sents, no_ts = sentences_from_sensevoice(res)
    assert not no_ts
    assert sents == [{"start": 100, "end": 620, "text": "Hello world."},
                     {"start": 3000, "end": 3920, "text": "I have 50 pieces!"}]


def test_sensevoice_gap_split_and_fallback():
    res = [{"text": "<|ja|>うちの中学 は", "words": ["う", "ち", "の", "中", "学", "は"],
            "timestamp": [[0, 50], [60, 100], [110, 150], [3000, 3050], [3060, 3100], [3110, 3150]]}]
    sents, _ = sentences_from_sensevoice(res)
    assert [s["text"] for s in sents] == ["うちの", "中学 は"]
    sents, no_ts = sentences_from_sensevoice([{"text": "<|zh|>只有文本"}])
    assert no_ts and sents[0]["text"] == "只有文本"
    assert strip_tags("<|yue|><|HAPPY|>你好") == "你好"


@pytest.mark.parametrize("opts,platform,vlang,expect", [
    ({"lang": "auto", "model": "paraformer-zh"}, "bilibili", None, ("zh", "paraformer-zh")),
    ({"lang": "auto", "model": "fun-asr-nano"}, "local", None, ("zh", "fun-asr-nano")),
    ({"lang": "auto", "model": "paraformer-zh"}, "youtube", "en-US", ("en", "sensevoice")),
    ({"lang": "auto", "model": "paraformer-zh"}, "youtube", "zh-Hans", ("zh", "paraformer-zh")),
    ({"lang": "auto", "model": "paraformer-zh"}, "youtube", None, ("auto", "sensevoice")),
    ({"lang": "auto", "model": "paraformer-zh"}, "youtube", "fr", ("auto", "sensevoice")),
    ({"lang": "ja", "model": "paraformer-zh"}, "bilibili", None, ("ja", "sensevoice")),
    ({"lang": "zh", "model": "sensevoice"}, "youtube", "en", ("zh", "paraformer-zh")),
    ({}, "bilibili", None, ("zh", "paraformer-zh")),
])
def test_resolve_asr(opts, platform, vlang, expect):
    assert tasks.resolve_asr(opts, platform, vlang, lambda m: None) == expect


@pytest.mark.parametrize("msg,code", [
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "YT_BOT_CHECK"),
    ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "YT_PRIVATE"),
    ("ERROR: [youtube] x: Join this channel to get access to members-only content", "YT_MEMBERS_ONLY"),
    ("ERROR: [youtube] x: This live event will begin in 3 hours", "YT_LIVE_NOT_ENDED"),
    ("ERROR: [youtube] x: Video unavailable", "VIDEO_NOT_FOUND"),
    ("ERROR: Unable to download webpage: <urlopen error [WinError 10060]>", "PROXY_FAILED"),
])
def test_map_error(tmp_env, msg, code):
    assert youtube.map_error(Exception(msg)).code == code


def test_migration_v1_to_v2(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, bvid TEXT, part INTEGER, title TEXT,
            part_title TEXT, uploader TEXT, duration_ms INTEGER DEFAULT 0, url TEXT, status TEXT, stage TEXT,
            percent REAL DEFAULT 0, message TEXT, source TEXT, backend TEXT, model TEXT, opts_json TEXT,
            error_json TEXT, created_at REAL, finished_at REAL);
        INSERT INTO tasks(bvid, part, status) VALUES('BV1GJ411x7h7', 1, 'done'), ('LOCAL', 1, 'done');
        PRAGMA user_version=1;
    """)
    c.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "_conn", None)
    db.init()
    assert [t["platform"] for t in db.list_tasks()["items"]] == ["local", "bilibili"]
    assert db.list_tasks(platform="local")["total"] == 1
    db.init()  # 再跑一次不报错
    db._conn.close()


# ---------------- 流水线 ----------------

@pytest.fixture
def env(tmp_env, monkeypatch):
    monkeypatch.setattr(export, "RESULTS_DIR", tmp_env / "results")
    monkeypatch.setattr(tasks, "RESULTS_DIR", tmp_env / "results")
    monkeypatch.setattr(tasks, "TASK_LOG_DIR", tmp_env)
    wav = tmp_env / "a.wav"
    wav.write_bytes(b"x")
    monkeypatch.setattr(youtube, "download_audio", lambda *a, **k: wav)
    monkeypatch.setattr(bili, "probe_duration_ms", lambda p: 10000)
    monkeypatch.setattr(bili, "remove_audio_key", lambda k: None)
    return tmp_env


def _yt_info(subs=None, lang="en"):
    return {"title": "Video", "channel": "Chan", "duration": 10, "language": lang,
            "subtitles": subs or {}, "automatic_captions": {}}


def _run(force_asr=False, lang="auto"):
    m = tasks.TaskManager()
    meta = {"title": "Video", "uploader": "Chan", "parts": [{"index": 1, "title": "Video", "duration": 10}]}
    tid = m.create(bvid=VID, parts=[1], meta=meta, platform="youtube",
                   opts={"force_asr": force_asr, "backend": "local", "model": "paraformer-zh", "lang": lang,
                         "hotwords": [], "spk": False})[0]
    m.q.get()
    m._run(db.get_task(tid), tasks.TaskLog(tid))
    return db.get_task(tid), db.get_result(tid)


def test_pipeline_youtube_subtitle(env, monkeypatch):
    j = json.dumps({"events": [{"tStartMs": 0, "dDurationMs": 900, "segs": [{"utf8": "Hi."}]}]})
    monkeypatch.setattr(youtube, "info", lambda v: _yt_info({"en": [{"ext": "json3", "data": j}]}))
    monkeypatch.setattr(tasks, "get_backend", lambda n: pytest.fail("不应走识别"))
    t, r = _run()
    assert t["platform"] == "youtube" and t["url"] == f"https://www.youtube.com/watch?v={VID}"
    assert t["source"] == "subtitle_yt" and r["sentences"][0]["text"] == "Hi."
    md = export.to_md(t, r)
    assert "频道：Chan" in md and f"watch?v={VID}&t=0s" in md


def test_pipeline_youtube_asr_routes_sensevoice(env, monkeypatch):
    seen = {}

    class Fake:
        def transcribe(self, wav, opts, prog, cancel, log):
            seen["model"], seen["lang"] = opts.model, opts.lang
            return {"source": "asr_local", "model": opts.model, "duration_ms": 10000,
                    "sentences": [{"start": 0, "end": 900, "text": "Hello."}]}

    monkeypatch.setattr(youtube, "info", lambda v: _yt_info(lang="en-US"))
    monkeypatch.setattr(tasks, "get_backend", lambda n: Fake())
    t, r = _run(force_asr=True)
    assert seen == {"model": "sensevoice", "lang": "en"}
    assert t["model"] == "sensevoice"

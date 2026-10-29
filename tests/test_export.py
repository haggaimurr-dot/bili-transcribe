from app import export
from common.asr_format import clean_hotwords, sentences_from_funasr

TASK = {"id": 1, "bvid": "BV1GJ411x7h7", "part": 2, "title": "测试视频", "part_title": None,
        "uploader": "UP", "duration_ms": 65000}


def result(sents, **kw):
    return {"source": "asr_local", "model": "paraformer-zh", "duration_ms": 65000, "sentences": sents, **kw}


def test_srt_time():
    assert export.fmt_srt_time(0) == "00:00:00,000"
    assert export.fmt_srt_time(3723004) == "01:02:03,004"
    assert export.fmt_clock(65000) == "01:05"
    assert export.fmt_clock(3723004) == "1:02:03"


def test_paragraphs_gap_and_speaker():
    s = [
        {"start": 0, "end": 1000, "text": "一。"},
        {"start": 1500, "end": 2000, "text": "二。"},
        {"start": 5000, "end": 6000, "text": "三。"},  # 间隔 3s → 新段
        {"start": 6100, "end": 7000, "text": "四。", "spk": 1},  # 换说话人 → 新段
    ]
    paras = export.paragraphs(s)
    assert [len(p) for p in paras] == [2, 1, 1]


def test_split_long():
    text = "这是一个很长的句子，" * 6  # 60 字
    s = {"start": 0, "end": 6000, "text": text}
    parts = export.split_long(s, 40)
    assert len(parts) >= 2
    assert all(len(p["text"]) <= 40 for p in parts)
    assert "".join(p["text"] for p in parts) == text
    assert parts[0]["start"] == 0 and parts[-1]["end"] == 6000
    assert all(a["end"] == b["start"] for a, b in zip(parts, parts[1:]))


def test_split_long_no_punct():
    s = {"start": 0, "end": 1000, "text": "啊" * 95}
    parts = export.split_long(s, 40)
    assert [len(p["text"]) for p in parts] == [40, 40, 15]


def test_txt_and_md():
    r = result([{"start": 0, "end": 1000, "text": "你好。"}, {"start": 1200, "end": 2000, "text": "Hello"},
                {"start": 1200, "end": 2000, "text": "world"}])
    txt = export.to_txt(TASK, r)
    assert "测试视频" in txt and "你好。Hello world" in txt
    md = export.to_md(TASK, r)
    assert md.startswith("# 测试视频")
    assert "?p=2&t=0" in md


def test_srt_render_with_speaker():
    r = result([{"start": 0, "end": 1000, "text": "你好", "spk": 0}])
    srt = export.to_srt(TASK, r)
    assert srt.startswith("1\n00:00:00,000 --> 00:00:01,000\n[说话人1] 你好")


def test_safe_filename():
    assert export.safe_filename('a/b:c*?"<>|d') == "a_b_c_d"
    assert export.filename(TASK, "srt") == "测试视频_P2.srt"


def test_zip():
    data = export.build_zip([(TASK, result([{"start": 0, "end": 1, "text": "x"}]))], "all")
    import io
    import zipfile

    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert sorted(names) == ["测试视频_P2.md", "测试视频_P2.srt", "测试视频_P2.txt"]


def test_hotwords():
    hot, notes = clean_hotwords(["犬瘟热", "犬瘟热", " ", "一二三四五六七八九十十一"])
    assert hot == "犬瘟热 一二三四五六七八九十"
    assert notes
    hot, notes = clean_hotwords([str(i) for i in range(150)])
    assert len(hot.split()) == 100 and notes


def test_sentences_from_funasr():
    res = [{"text": "全文", "sentence_info": [{"start": 10, "end": 900, "text": "你好。", "spk": 1}]}]
    sents, no_ts = sentences_from_funasr(res)
    assert sents == [{"start": 10, "end": 900, "text": "你好。", "spk": 1}] and not no_ts
    sents, no_ts = sentences_from_funasr([{"text": "只有全文"}])
    assert no_ts and sents[0]["text"] == "只有全文"

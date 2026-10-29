"""worker 的 Fun-ASR-Nano 输出转换：用 GPU 上实测的输出结构（2026-10-30 test_nano.py）。"""
from worker.server import nano_segment_sentences


def _ts(chars, step=0.2, start=0.4):
    return [{"token": c, "start_time": round(start + i * step, 2), "end_time": round(start + i * step + 0.1, 2),
             "score": 0.99} for i, c in enumerate(chars)]


def test_split_by_punctuation_with_offset():
    text = "大家好。今天聊聊Uniswap！"
    # Nano 的 timestamps 只含文字 token，不含标点
    tokens = list("大家好今天聊聊") + ["Uniswap"]
    res = [{"key": "0", "text": text, "timestamps": _ts(tokens)}]
    sents = nano_segment_sentences(res, beg=10_000, end=20_000)
    assert [s["text"] for s in sents] == ["大家好。", "今天聊聊Uniswap！"]
    assert sents[0]["start"] == 10_400  # 段起点 + 0.4s
    assert sents[1]["start"] == 10_400 + 600 and sents[1]["end"] <= 20_000


def test_no_timestamps_falls_back_to_segment():
    res = [{"key": "0", "text": "国务院发展研究中心"}]
    assert nano_segment_sentences(res, 120, 7900) == [{"start": 120, "end": 7900, "text": "国务院发展研究中心"}]


def test_real_sample_without_punctuation():
    text = "国务院发展研究中心市场经济研究所副所长邓郁松认为"
    sents = nano_segment_sentences([{"text": text, "timestamps": _ts(list(text))}], 120, 7900)
    assert "".join(s["text"] for s in sents) == text
    assert sents[0]["start"] == 120 + 400


def test_empty():
    assert nano_segment_sentences([{"text": ""}], 0, 1000) == []
    assert nano_segment_sentences([], 0, 1000) == []

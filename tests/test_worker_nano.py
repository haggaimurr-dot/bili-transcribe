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


def test_parallel_pool_keeps_order_and_returns_replicas(monkeypatch):
    """多副本并行：结果按 VAD 顺序拼回、确实并发、每份模型同一时刻只被一个线程用、用完全部归还。"""
    import queue
    import threading
    import time

    import numpy as np
    from funasr.utils import load_utils

    from worker import server

    segs = [[i * 1000, i * 1000 + 800] for i in range(12)]
    monkeypatch.setattr(server, "vad_segments", lambda p: segs)
    monkeypatch.setattr(server, "_load", lambda k: None)
    monkeypatch.setattr(load_utils, "load_audio_text_image_video", lambda p, fs: np.zeros(16000 * 13, "float32"))
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    class Fake:
        def __init__(self, name):
            self.name, self.busy = name, False

        def generate(self, **kw):
            assert not self.busy, "同一份模型被两个线程同时使用"
            self.busy = True
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            idx = int(kw["input"][0].rsplit("\\", 1)[-1].rsplit("/", 1)[-1].split(".")[0])
            with lock:
                state["now"] -= 1
            self.busy = False
            return [{"text": f"第{idx}段。"}]

    pool = queue.Queue()
    for i in range(3):
        pool.put((Fake(i), None))
    monkeypatch.setattr(server, "_nano_pool", pool)

    sents = server.transcribe_nano("x.wav", "", "zh")
    assert [s["text"] for s in sents] == [f"第{i}段。" for i in range(12)]
    assert [s["start"] for s in sents] == [s[0] for s in segs]
    assert state["peak"] >= 2  # 确实并行
    assert pool.qsize() == 3  # 副本全部归还

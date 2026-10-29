import json

import pytest

from app import bili
from app.errors import AppError


@pytest.mark.parametrize("text,vid,p", [
    ("BV1GJ411x7h7", "BV1GJ411x7h7", None),
    ("https://www.bilibili.com/video/BV1GJ411x7h7/?spm_id_from=333&p=3", "BV1GJ411x7h7", 3),
    ("https://m.bilibili.com/video/BV1GJ411x7h7?p=2", "BV1GJ411x7h7", 2),
    ("【标题】一个视频 https://www.bilibili.com/video/BV1GJ411x7h7 分享自哔哩哔哩", "BV1GJ411x7h7", None),
    ("av170001", "av170001", None),
    ("https://www.bilibili.com/video/av170001?p=5", "av170001", 5),
])
def test_normalize(text, vid, p):
    n = bili.normalize(text)
    assert n["vid"] == vid
    assert n["p"] == p
    assert n["url"] == f"https://www.bilibili.com/video/{vid}"


def test_normalize_short_link():
    n = bili.normalize("【某视频】 https://b23.tv/AbCd123 ",
                       expand=lambda u: "https://www.bilibili.com/video/BV1GJ411x7h7?p=2&share=1")
    assert n == {"vid": "BV1GJ411x7h7", "p": 2, "url": "https://www.bilibili.com/video/BV1GJ411x7h7"}


def test_normalize_invalid():
    with pytest.raises(AppError) as e:
        bili.normalize("https://www.youtube.com/watch?v=xxx")
    assert e.value.code == "INVALID_URL"


def test_pick_subtitle_order():
    assert bili.pick_subtitle({"danmaku": [], "ai-zh": [], "zh-CN": []}) == ("zh-CN", "subtitle_cc")
    assert bili.pick_subtitle({"danmaku": [], "ai-zh": []}) == ("ai-zh", "subtitle_ai")
    assert bili.pick_subtitle({"zh-Hant": [], "en-US": []}) == ("zh-Hant", "subtitle_cc")
    assert bili.pick_subtitle({"danmaku": [], "en-US": []}) is None
    assert bili.pick_subtitle(None) is None


def test_parse_subtitle_json():
    raw = json.dumps({"body": [
        {"from": 0.5, "to": 2.25, "content": "大家好"},
        {"from": 2.25, "to": 3.0, "content": "大家好"},
        {"from": 3.0, "to": 4.0, "content": " "},
        {"from": 4.0, "to": 6.5, "content": "今天聊聊"},
    ]}, ensure_ascii=False)
    s = bili.parse_subtitle_text(raw)
    assert s == [{"start": 500, "end": 3000, "text": "大家好"}, {"start": 4000, "end": 6500, "text": "今天聊聊"}]


def test_parse_subtitle_srt():
    raw = "1\n00:00:01,200 --> 00:00:03,450\n第一句\n\n2\n01:02:03,004 --> 01:02:05,000\n第二句\n第二行\n"
    s = bili.parse_subtitle_text(raw)
    assert s[0] == {"start": 1200, "end": 3450, "text": "第一句"}
    assert s[1]["start"] == 3723004 and s[1]["text"] == "第二句 第二行"

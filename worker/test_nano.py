"""在 AutoDL 上验证 Fun-ASR-Nano：加载、识别、打印原始输出结构和耗时。

用法（仓库根目录、激活 .venv-worker 后）：
    python worker/test_nano.py                 # 用 ModelScope 自带示例音频
    python worker/test_nano.py /path/to/a.wav  # 用自己的音频
"""
import glob
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MODELSCOPE_CACHE", "/root/autodl-tmp/modelscope")

from worker import server  # noqa: E402


def short(v, n=300):
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + f"…（共 {len(s)} 字符）"


def main():
    if len(sys.argv) > 1:
        wav = sys.argv[1]
    else:
        hits = glob.glob(os.environ["MODELSCOPE_CACHE"] + "/**/asr_example*.wav", recursive=True)
        if not hits:
            sys.exit("没找到示例音频，请传一个音频路径")
        wav = hits[0]
    print("音频：", wav)
    t0 = time.time()
    m = server._load("nano")
    print(f"加载用时 {time.time() - t0:.1f}s")

    t0 = time.time()
    res = m.generate(**server.nano_kwargs(wav, "", "zh"))
    print(f"识别用时 {time.time() - t0:.1f}s")
    items = res if isinstance(res, list) else [res]
    print(f"返回 {len(items)} 项")
    for i, it in enumerate(items[:3]):
        print(f"--- 第 {i} 项，字段：{list(it.keys()) if isinstance(it, dict) else type(it)}")
        if isinstance(it, dict):
            for k, v in it.items():
                print(f"  {k}: {short(v)}")
    sents, no_ts = server.sentences_from_nano(res)
    print(f"转换后 {len(sents)} 句，no_timestamp={no_ts}")
    for s in sents[:3]:
        print("  ", s)


if __name__ == "__main__":
    main()

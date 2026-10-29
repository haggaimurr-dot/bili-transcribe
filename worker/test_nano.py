"""在 AutoDL 上验证 Fun-ASR-Nano：加载、识别、打印原始输出结构和耗时。

用法（仓库根目录、激活 .venv-worker 后）：
    python worker/test_nano.py                 # 用 ModelScope 自带示例音频
    python worker/test_nano.py /path/to/a.wav  # 用自己的音频
    python worker/test_nano.py /path/to/a.wav --wait-replicas  # 等所有副本加载完再测（测并行提速）
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

    segs = server.vad_segments(wav)
    print(f"VAD 切出 {len(segs)} 段：{short(segs)}")

    def show_raw(res):
        items = res if isinstance(res, list) else [res]
        print(f"第 1 段原始输出：{len(items)} 项")
        for it in items[:1]:
            print(f"  字段：{list(it.keys()) if isinstance(it, dict) else type(it)}")
            if isinstance(it, dict):
                for k, v in it.items():
                    print(f"  {k}: {short(v)}")

    if len(sys.argv) > 2 and sys.argv[2] == "--wait-replicas":
        while server._nano_count["loaded"] < server.NANO_REPLICAS and not server._nano_count["error"]:
            time.sleep(2)
    print(f"Nano 副本：已加载 {server._nano_count['loaded']} / 目标 {server.NANO_REPLICAS}"
          + (f"（{server._nano_count['error']}）" if server._nano_count["error"] else ""))
    t0 = time.time()
    sents = server.transcribe_nano(wav, "", "zh", on_raw=show_raw)
    print(f"识别用时 {time.time() - t0:.1f}s，共 {len(sents)} 句")
    for s in sents[:5]:
        print("  ", s)


if __name__ == "__main__":
    main()

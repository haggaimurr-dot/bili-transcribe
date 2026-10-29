"""AutoDL GPU 上运行的 asr-worker。

启动：ASR_TOKEN=xxx uvicorn worker.server:app --host 127.0.0.1 --port 9000（在仓库根目录执行）
"""
import asyncio
import contextlib
import os
import queue
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import torch  # noqa: E402
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from common.asr_format import (clean_hotwords, make_result, sentences_from_funasr,  # noqa: E402
                               sentences_from_sensevoice)

TOKEN = os.environ.get("ASR_TOKEN", "")
DEVICE = os.environ.get("ASR_DEVICE", "cuda:0" if torch.cuda.is_available() else "cpu")
NANO_MODEL = os.environ.get("NANO_MODEL", "FunAudioLLM/Fun-ASR-Nano-2512")
# Fun-ASR 官方仓库（https://github.com/FunAudioLLM/Fun-ASR），Nano 的模型代码 model.py 在仓库根目录，setup.sh 负责 clone
FUN_ASR_DIR = os.environ.get("FUN_ASR_DIR", "/root/autodl-tmp/Fun-ASR")
NANO_LANG = {"zh": "中文", "en": "英文", "ja": "日文"}
NANO_MAX_SEG_MS = 30000  # VAD 单段最长 30 秒（官方长音频示例的取值）
# Nano 不支持批量解码，只能逐段跑；加载多份模型、多段并行来提速（每份约 3.5 GB 显存）
NANO_REPLICAS = max(1, int(os.environ.get("NANO_REPLICAS", "3")))
PRELOAD_SPK = os.environ.get("PRELOAD_SPK", "1") == "1"
PRELOAD_NANO = os.environ.get("PRELOAD_NANO", "1") == "1"  # 启动时加载全部 Nano 副本（约 2～3 分钟），首个请求不用等

_models: dict[str, object] = {}
_model_lock = threading.Lock()
_gpu_lock = asyncio.Lock()
_busy = {"value": False}

# Nano 模型池：元素是 (模型, CUDA stream)；借出去用完再放回，保证一份模型同时只被一个线程用
_nano_pool: "queue.Queue[tuple]" = queue.Queue()
_nano_count = {"loaded": 0, "error": ""}
_nano_build_lock = threading.Lock()
_nano_executor = ThreadPoolExecutor(max_workers=NANO_REPLICAS, thread_name_prefix="nano")


def _load(key: str):
    if key in _models:
        return _models[key]
    with _model_lock:
        if key in _models:
            return _models[key]
        from funasr import AutoModel

        common = dict(device=DEVICE, disable_update=True, disable_pbar=True, log_level="ERROR")
        if key == "paraformer":
            m = AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc", **common)
        elif key == "paraformer+spk":
            m = AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc", spk_model="cam++",
                          **common)
        elif key == "sensevoice":
            m = AutoModel(model="iic/SenseVoiceSmall", vad_model="fsmn-vad",
                          vad_kwargs={"max_single_segment_time": 30000}, **common)
        elif key == "nano":
            # 所有副本加载完才开始推理：transformers 加载 bf16 的 Qwen3 时会临时改进程全局的默认 dtype，
            # 边加载边推理会让推理线程新建的张量变成 bf16，报 "mat1 and mat2 must have the same dtype"（问题 #021）
            m = _new_nano()
            _add_nano(m)
            for i in range(1, NANO_REPLICAS):
                try:
                    _add_nano(_new_nano())
                except Exception as e:  # noqa: BLE001  显存不够等情况：用已加载的副本继续跑
                    _nano_count["error"] = f"第 {i + 1} 份 Nano 加载失败：{e}"
                    print(_nano_count["error"], flush=True)
                    break
            print(f"Nano 副本已加载：{_nano_count['loaded']} 份", flush=True)
        elif key == "vad":
            m = AutoModel(model="fsmn-vad", max_single_segment_time=NANO_MAX_SEG_MS, **common)
        else:
            raise KeyError(key)
        _models[key] = m
        return m


def _new_nano():
    """加载一份 Fun-ASR-Nano。官方用法：trust_remote_code + remote_code 指向 Fun-ASR 仓库的 model.py。
    不挂 vad_model：FunASR 内置 VAD 把切好的段以 tensor 传给 model.py，它的 data_load_speech
    处理不了（UnboundLocalError: data_src），所以分段由 transcribe_nano 自己做、按文件路径逐段送。"""
    from funasr import AutoModel

    code = os.path.join(FUN_ASR_DIR, "model.py")
    if not os.path.exists(code):
        raise RuntimeError(f"缺少 {code}，请先执行 bash worker/setup.sh（会 clone Fun-ASR 仓库）")
    with _nano_build_lock:
        if FUN_ASR_DIR not in sys.path:
            sys.path.insert(0, FUN_ASR_DIR)  # model.py 会 import 同目录下的其他模块
        return AutoModel(model=NANO_MODEL, trust_remote_code=True, remote_code=code, device=DEVICE,
                         disable_update=True, disable_pbar=True, log_level="ERROR")


def _add_nano(m) -> None:
    stream = torch.cuda.Stream() if DEVICE.startswith("cuda") else None
    _nano_pool.put((m, stream))
    _nano_count["loaded"] += 1


def _model_key(model: str, spk: bool) -> str:
    if model == "fun-asr-nano":
        return "nano"
    if model == "sensevoice":
        return "sensevoice"
    return "paraformer+spk" if spk else "paraformer"


app = FastAPI(title="asr-worker")


@app.middleware("http")
async def auth(request: Request, call_next):
    if TOKEN and request.headers.get("authorization") != f"Bearer {TOKEN}":
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)


@app.on_event("startup")
async def preload():
    await run_in_threadpool(_load, "paraformer")
    if PRELOAD_SPK:
        await run_in_threadpool(_load, "paraformer+spk")
    if PRELOAD_NANO and os.path.exists(os.path.join(FUN_ASR_DIR, "model.py")):
        await run_in_threadpool(_load, "vad")
        await run_in_threadpool(_load, "nano")


@app.get("/health")
def health():
    info = {"device": DEVICE, "models": list(_models), "busy": _busy["value"],
            "nano_replicas": _nano_count["loaded"], "nano_replicas_target": NANO_REPLICAS}
    if _nano_count["error"]:
        info["nano_error"] = _nano_count["error"]
    if torch.cuda.is_available():
        info["gpu"] = torch.cuda.get_device_name(0)
        info["mem_used_mb"] = int(torch.cuda.memory_allocated(0) / 1e6)
        info["mem_total_mb"] = int(torch.cuda.get_device_properties(0).total_memory / 1e6)
    else:
        info["gpu"] = "cpu"
    return info


def _run(model_name: str, spk: bool, path: str, hotwords: str, lang: str = "zh") -> dict:
    key = _model_key(model_name, spk)
    m = _load(key)
    hot, _ = clean_hotwords(hotwords)
    kwargs = {"input": path, "batch_size_s": 300}
    if key == "sensevoice":
        res = m.generate(input=path, language=lang or "auto", use_itn=True, batch_size_s=60, output_timestamp=True)
        sents, no_ts = sentences_from_sensevoice(res)
        return make_result("asr_remote", "sensevoice", 0, sents, no_ts)
    if key == "nano":
        sents = transcribe_nano(path, hot, lang)
        return make_result("asr_remote", "fun-asr-nano", 0, sents, False)
    res = m.generate(hotword=hot or None, sentence_timestamp=True, **kwargs)
    sents, no_ts = sentences_from_funasr(res)
    return make_result("asr_remote", "paraformer-zh", 0, sents, no_ts)


def nano_kwargs(path: str, hot: str, lang: str) -> dict:
    """Fun-ASR-Nano 单段的 generate 参数（官方示例：input 为路径列表、cache={}、batch_size=1、hotwords、language、itn）。
    Nano 不支持批量解码（model.py: batch decoding is not implemented）。"""
    kw = {"input": [path], "cache": {}, "batch_size": 1, "itn": True}
    if hot:
        kw["hotwords"] = hot.split()
    if lang in NANO_LANG:
        kw["language"] = NANO_LANG[lang]
    return kw


def vad_segments(path: str) -> list[list[int]]:
    """fsmn-vad 切分，返回 [[起点ms, 终点ms], ...]。"""
    res = _load("vad").generate(input=path)
    return [[int(b), int(e)] for b, e in (res[0].get("value") or []) if e > b]


def _nano_generate(seg: str, hot: str, lang: str):
    """从池里借一份 Nano 识别一段，用完放回。每份模型有自己的 CUDA stream，多线程时 GPU 上能重叠执行。"""
    m, stream = _nano_pool.get()
    try:
        ctx = torch.cuda.stream(stream) if stream is not None else contextlib.nullcontext()
        with ctx:
            res = m.generate(**nano_kwargs(seg, hot, lang))
        if stream is not None:
            stream.synchronize()
        return res
    finally:
        _nano_pool.put((m, stream))


def transcribe_nano(path: str, hot: str, lang: str, on_raw=None) -> list[dict]:
    """自己做 VAD，把每段写成临时 wav，按路径送 Nano；多段在模型池上并行，结果按原顺序拼回。
    每段的 VAD 起止就是这一句的时间戳（有逐字时间戳时再细分）。"""
    import soundfile as sf
    from funasr.utils.load_utils import load_audio_text_image_video

    _load("nano")  # 确保至少有一份在池里
    audio = load_audio_text_image_video(path, fs=16000)
    audio = audio.numpy() if hasattr(audio, "numpy") else audio
    with tempfile.TemporaryDirectory() as tmp:
        segs = vad_segments(path)
        files = []
        for i, (beg, end) in enumerate(segs):
            seg = os.path.join(tmp, f"{i}.wav")
            sf.write(seg, audio[beg * 16:end * 16], 16000)
            files.append(seg)
        results = list(_nano_executor.map(lambda f: _nano_generate(f, hot, lang), files))
    if on_raw and results:
        on_raw(results[0])
    sents = []
    for (beg, end), res in zip(segs, results):
        sents += nano_segment_sentences(res, beg, end)
    return sents


def nano_segment_sentences(res, beg: int, end: int) -> list[dict]:
    """一段 VAD 的 Nano 输出 → 句子。
    有逐字 timestamps（秒，相对本段起点）就按标点/停顿细分（复用 SenseVoice 的切句逻辑）；没有就整段一句。"""
    it = (res[0] if res else {}) or {}
    text = str(it.get("text", "")).strip()
    if not text:
        return []
    ts = [t for t in (it.get("timestamps") or []) if isinstance(t, dict) and "token" in t]
    if ts:
        item = {"text": text, "words": _attach_punct(text, [str(t["token"]) for t in ts]),
                "timestamp": [[beg + int(float(t["start_time"]) * 1000), beg + int(float(t["end_time"]) * 1000)]
                              for t in ts]}
        sents, no_ts = sentences_from_sensevoice([item])
        if sents and not no_ts:
            return sents
    return [{"start": beg, "end": end, "text": text}]


_PUNCT = set("，。！？；、,.!?;:：")


def _attach_punct(text: str, tokens: list[str]) -> list[str]:
    """Nano 的 timestamps 只有文字 token；把紧跟其后的标点并进去，切句时才能识别句末。"""
    out, pos = [], 0
    for tok in tokens:
        idx = text.find(tok, pos)
        if idx < 0:
            out.append(tok)
            continue
        j = idx + len(tok)
        while j < len(text) and text[j] in _PUNCT:
            j += 1
        out.append(text[idx:j])
        pos = j
    return out


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...), hotwords: str = Form(""), spk: str = Form("0"),
                     model: str = Form("paraformer-zh"), lang: str = Form("zh")):
    if model not in ("paraformer-zh", "fun-asr-nano", "sensevoice"):
        raise HTTPException(400, f"不支持的模型 {model}")
    if lang not in ("auto", "zh", "en", "ja", "ko", "yue"):
        raise HTTPException(400, f"不支持的语言 {lang}")
    suffix = Path(file.filename or "audio.ogg").suffix or ".ogg"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)
        async with _gpu_lock:
            _busy["value"] = True
            t0 = time.time()
            try:
                result = await run_in_threadpool(_run, model, spk == "1", tmp, hotwords, lang)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(500, f"识别失败：{e}") from e
            finally:
                _busy["value"] = False
        result["elapsed_s"] = round(time.time() - t0, 2)
        return result
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

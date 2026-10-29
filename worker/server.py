"""AutoDL GPU 上运行的 asr-worker。

启动：ASR_TOKEN=xxx uvicorn worker.server:app --host 127.0.0.1 --port 9000（在仓库根目录执行）
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
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
PRELOAD_SPK = os.environ.get("PRELOAD_SPK", "1") == "1"

_models: dict[str, object] = {}
_model_lock = threading.Lock()
_gpu_lock = asyncio.Lock()
_busy = {"value": False}


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
            # 官方用法：trust_remote_code + remote_code 指向 Fun-ASR 仓库的 model.py；长音频配 fsmn-vad 分段
            code = os.path.join(FUN_ASR_DIR, "model.py")
            if not os.path.exists(code):
                raise RuntimeError(f"缺少 {code}，请先执行 bash worker/setup.sh（会 clone Fun-ASR 仓库）")
            if FUN_ASR_DIR not in sys.path:
                sys.path.insert(0, FUN_ASR_DIR)  # model.py 会 import 同目录下的其他模块
            m = AutoModel(model=NANO_MODEL, trust_remote_code=True, remote_code=code, vad_model="fsmn-vad",
                          vad_kwargs={"max_single_segment_time": 30000}, **common)
        else:
            raise KeyError(key)
        _models[key] = m
        return m


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


@app.get("/health")
def health():
    info = {"device": DEVICE, "models": list(_models), "busy": _busy["value"]}
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
        res = m.generate(**nano_kwargs(path, hot, lang))
        sents, no_ts = sentences_from_nano(res)
        return make_result("asr_remote", "fun-asr-nano", 0, sents, no_ts)
    res = m.generate(hotword=hot or None, sentence_timestamp=True, **kwargs)
    sents, no_ts = sentences_from_funasr(res)
    return make_result("asr_remote", "paraformer-zh", 0, sents, no_ts)


def nano_kwargs(path: str, hot: str, lang: str) -> dict:
    """Fun-ASR-Nano 的 generate 参数（官方示例：cache={}、batch_size=1、hotwords 列表、language、itn）。
    Nano 不支持批量解码（model.py: batch decoding is not implemented），VAD 切出的段要一段一段送：
    batch_size_s=1 让每批只装一段（每段都长于 1 秒）。"""
    kw = {"input": [path], "cache": {}, "batch_size": 1, "batch_size_s": 1, "itn": True}
    if hot:
        kw["hotwords"] = hot.split()
    if lang in NANO_LANG:
        kw["language"] = NANO_LANG[lang]
    return kw


def sentences_from_nano(res) -> tuple[list[dict], bool]:
    """Nano 输出 → 句子。优先用 sentence_info；没有就每段一句；都没有时间就整段文本。
    真实输出结构以 worker/test_nano.py 在 GPU 上的结果为准。"""
    sents, no_ts = sentences_from_funasr(res)
    if not no_ts:
        return sents, False
    items = res if isinstance(res, list) else [res or {}]
    texts = [str(r.get("text", "")).strip() for r in items if r.get("text")]
    return ([{"start": 0, "end": 0, "text": "".join(texts)}] if texts else []), True


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

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

from common.asr_format import clean_hotwords, make_result, sentences_from_funasr  # noqa: E402

TOKEN = os.environ.get("ASR_TOKEN", "")
DEVICE = os.environ.get("ASR_DEVICE", "cuda:0" if torch.cuda.is_available() else "cpu")
NANO_MODEL = os.environ.get("NANO_MODEL", "FunAudioLLM/Fun-ASR-Nano-2512")
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
        elif key == "nano":
            # Fun-ASR-Nano 基于 LLM；时间戳能力需在部署时验证（见实施文档）
            m = AutoModel(model=NANO_MODEL, vad_model="fsmn-vad", vad_kwargs={"max_single_segment_time": 30000},
                          trust_remote_code=True, **common)
        else:
            raise KeyError(key)
        _models[key] = m
        return m


def _model_key(model: str, spk: bool) -> str:
    if model == "fun-asr-nano":
        return "nano"
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


def _run(model_name: str, spk: bool, path: str, hotwords: str) -> dict:
    key = _model_key(model_name, spk)
    m = _load(key)
    hot, _ = clean_hotwords(hotwords)
    kwargs = {"input": path, "batch_size_s": 300}
    if key == "nano":
        if hot:
            kwargs["hotwords"] = hot.split()
        res = m.generate(**kwargs)
    else:
        res = m.generate(hotword=hot or None, sentence_timestamp=True, **kwargs)
    sents, no_ts = sentences_from_funasr(res)
    if key == "nano" and no_ts and isinstance(res, list) and len(res) > 1:
        # 多段 VAD 结果拼接
        texts = [str(r.get("text", "")).strip() for r in res if r.get("text")]
        sents = [{"start": 0, "end": 0, "text": "".join(texts)}]
    return make_result("asr_remote", "fun-asr-nano" if key == "nano" else "paraformer-zh", 0, sents, no_ts)


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...), hotwords: str = Form(""), spk: str = Form("0"),
                     model: str = Form("paraformer-zh")):
    if model not in ("paraformer-zh", "fun-asr-nano"):
        raise HTTPException(400, f"不支持的模型 {model}")
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
                result = await run_in_threadpool(_run, model, spk == "1", tmp, hotwords)
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

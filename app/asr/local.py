"""本地 CPU 识别：FunASR paraformer-zh + fsmn-vad + ct-punc（可选 cam++）。"""
import os
import threading
import time

from common.asr_format import clean_hotwords, make_result, sentences_from_funasr

from .. import settings
from ..errors import AppError
from .base import ASRBackend, ASROptions, EstimateTicker

LOCAL_MODELS = ("paraformer-zh",)

_models: dict[bool, object] = {}
_load_lock = threading.Lock()
state = {"status": "idle", "message": ""}  # idle | loading | ready | error


def _load(spk: bool, log=None):
    if spk in _models:
        return _models[spk]
    with _load_lock:
        if spk in _models:
            return _models[spk]
        state.update(status="loading", message="正在下载/加载识别模型（首次约 2 GB）")
        if log:
            log(state["message"] + ("，含说话人模型" if spk else ""))
        t0 = time.time()
        try:
            import torch
            from funasr import AutoModel

            torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))
            kwargs = dict(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc",
                          device="cpu", disable_update=True, disable_pbar=True, log_level="ERROR")
            if spk:
                kwargs["spk_model"] = "cam++"
            _models[spk] = AutoModel(**kwargs)
        except Exception as e:  # noqa: BLE001
            state.update(status="error", message=f"模型加载失败：{e}")
            raise AppError("MODEL_LOAD_FAILED", f"模型加载失败：{e}", "检查网络（首次需从 ModelScope 下载模型）") from e
        state.update(status="ready", message="")
        if log:
            log(f"模型加载完成，用时 {time.time() - t0:.0f}s")
        return _models[spk]


class LocalASR(ASRBackend):
    name = "local"

    def transcribe(self, audio_path, opts: ASROptions, progress_cb, cancel_event, log) -> dict:
        if opts.model not in LOCAL_MODELS:
            log(f"本地不支持 {opts.model}，改用 paraformer-zh")
        model = _load(opts.spk, log)
        hot, notes = clean_hotwords(opts.hotwords)
        for n in notes:
            log(n)
        rtf = settings.load().rtf_local
        expected = opts.duration_ms / 1000 * rtf
        log(f"开始本地识别，预计 {expected:.0f}s（RTF {rtf}）")
        t0 = time.time()
        with EstimateTicker(progress_cb, expected):
            res = model.generate(input=str(audio_path), batch_size_s=300, hotword=hot or None,
                                 sentence_timestamp=True)
        elapsed = time.time() - t0
        if opts.duration_ms > 0:
            measured = elapsed / (opts.duration_ms / 1000)
            settings.update_rtf("local", measured)
            log(f"识别完成，用时 {elapsed:.0f}s，实测 RTF {measured:.3f}")
        sents, no_ts = sentences_from_funasr(res)
        if no_ts:
            log("未返回句级时间戳，结果只有整段文本")
        return make_result("asr_local", "paraformer-zh", opts.duration_ms, sents, no_ts)

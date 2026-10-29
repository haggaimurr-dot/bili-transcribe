"""本地 CPU 识别：中文用 FunASR paraformer-zh + fsmn-vad + ct-punc（可选 cam++）；
英日韩粤用 SenseVoiceSmall + fsmn-vad。"""
import os
import threading
import time

from common.asr_format import (clean_hotwords, make_result, sentences_from_funasr,
                               sentences_from_sensevoice)

from .. import settings
from ..errors import AppError
from .base import ASRBackend, ASROptions, EstimateTicker

LOCAL_MODELS = ("paraformer-zh", "sensevoice")
SENSEVOICE_ID = "iic/SenseVoiceSmall"

_models: dict[str, object] = {}  # paraformer / paraformer+spk / sensevoice
_load_lock = threading.Lock()
state = {"status": "idle", "message": ""}  # idle | loading | ready | error

_LOAD_MSG = {
    "paraformer": "正在下载/加载中文识别模型（首次约 2 GB）",
    "paraformer+spk": "正在下载/加载中文识别模型和说话人模型（首次约 2 GB）",
    "sensevoice": "正在下载/加载多语言模型 SenseVoice（首次约 900 MB）",
}


def _load(key: str, log=None):
    if key in _models:
        return _models[key]
    with _load_lock:
        if key in _models:
            return _models[key]
        state.update(status="loading", message=_LOAD_MSG[key])
        if log:
            log(state["message"])
        t0 = time.time()
        try:
            import torch
            from funasr import AutoModel

            torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))
            common = dict(device="cpu", disable_update=True, disable_pbar=True, log_level="ERROR")
            if key == "sensevoice":
                _models[key] = AutoModel(model=SENSEVOICE_ID, vad_model="fsmn-vad",
                                         vad_kwargs={"max_single_segment_time": 30000}, **common)
            else:
                kwargs = dict(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc", **common)
                if key == "paraformer+spk":
                    kwargs["spk_model"] = "cam++"
                _models[key] = AutoModel(**kwargs)
        except Exception as e:  # noqa: BLE001
            state.update(status="error", message=f"模型加载失败：{e}")
            raise AppError("MODEL_LOAD_FAILED", f"模型加载失败：{e}", "检查网络（首次需从 ModelScope 下载模型）") from e
        state.update(status="ready", message="")
        if log:
            log(f"模型加载完成，用时 {time.time() - t0:.0f}s")
        return _models[key]


class LocalASR(ASRBackend):
    name = "local"

    def transcribe(self, audio_path, opts: ASROptions, progress_cb, cancel_event, log) -> dict:
        if opts.model == "sensevoice":
            return self._sensevoice(audio_path, opts, progress_cb, log)
        if opts.model not in LOCAL_MODELS:
            log(f"本地不支持 {opts.model}，改用 paraformer-zh")
        model = _load("paraformer+spk" if opts.spk else "paraformer", log)
        hot, notes = clean_hotwords(opts.hotwords)
        for n in notes:
            log(n)
        res = self._timed(lambda: model.generate(input=str(audio_path), batch_size_s=300, hotword=hot or None,
                                                 sentence_timestamp=True),
                          opts, progress_cb, log, "rtf_local")
        sents, no_ts = sentences_from_funasr(res)
        if no_ts:
            log("未返回句级时间戳，结果只有整段文本")
        return make_result("asr_local", "paraformer-zh", opts.duration_ms, sents, no_ts)

    def _sensevoice(self, audio_path, opts: ASROptions, progress_cb, log) -> dict:
        if opts.hotwords:
            log("SenseVoice 不支持热词，已忽略")
        if opts.spk:
            log("SenseVoice 不支持区分说话人，已忽略")
        model = _load("sensevoice", log)
        res = self._timed(lambda: model.generate(input=str(audio_path), language=opts.lang or "auto", use_itn=True,
                                                 batch_size_s=60, output_timestamp=True),
                          opts, progress_cb, log, "rtf_sensevoice")
        sents, no_ts = sentences_from_sensevoice(res)
        if no_ts:
            log("未返回时间戳，结果只有整段文本")
        return make_result("asr_local", "sensevoice", opts.duration_ms, sents, no_ts)

    @staticmethod
    def _timed(fn, opts: ASROptions, progress_cb, log, rtf_key: str):
        rtf = getattr(settings.load(), rtf_key)
        expected = opts.duration_ms / 1000 * rtf
        log(f"开始本地识别（{opts.model}{'，' + opts.lang if opts.model == 'sensevoice' else ''}），"
            f"预计 {expected:.0f}s（RTF {rtf}）")
        t0 = time.time()
        with EstimateTicker(progress_cb, expected):
            res = fn()
        elapsed = time.time() - t0
        if opts.duration_ms > 0:
            measured = elapsed / (opts.duration_ms / 1000)
            settings.update_rtf(rtf_key.removeprefix("rtf_"), measured)
            log(f"识别完成，用时 {elapsed:.0f}s，实测 RTF {measured:.3f}")
        return res

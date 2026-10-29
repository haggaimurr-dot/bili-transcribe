from .base import ASRBackend, ASROptions
from .local import LocalASR
from .remote import RemoteASR

# fun-asr-nano 暂不开放：worker 上缺 model.py，推理会卡住或 500（问题文档 #006/#014）
REMOTE_MODELS = ("paraformer-zh", "sensevoice")
LANGS = ("auto", "zh", "en", "ja", "ko", "yue")


def get_backend(name: str) -> ASRBackend:
    return RemoteASR() if name == "remote" else LocalASR()


__all__ = ["ASRBackend", "ASROptions", "LocalASR", "RemoteASR", "REMOTE_MODELS", "LANGS", "get_backend"]

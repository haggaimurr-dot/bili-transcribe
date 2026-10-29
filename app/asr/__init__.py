from .base import ASRBackend, ASROptions
from .local import LocalASR
from .remote import RemoteASR

REMOTE_MODELS = ("paraformer-zh", "fun-asr-nano")


def get_backend(name: str) -> ASRBackend:
    return RemoteASR() if name == "remote" else LocalASR()


__all__ = ["ASRBackend", "ASROptions", "LocalASR", "RemoteASR", "REMOTE_MODELS", "get_backend"]

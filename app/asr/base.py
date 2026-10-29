"""识别后端统一接口。"""
import threading
import time
from dataclasses import dataclass, field


@dataclass
class ASROptions:
    hotwords: list[str] = field(default_factory=list)
    spk: bool = False
    model: str = "paraformer-zh"
    duration_ms: int = 0


class ASRBackend:
    name = "base"

    def transcribe(self, audio_path, opts: ASROptions, progress_cb, cancel_event: threading.Event,
                   log) -> dict:
        raise NotImplementedError


class EstimateTicker:
    """没有真实进度时，按预计耗时把进度从 lo 推到 hi（最多 95%）。"""

    def __init__(self, progress_cb, expected_s: float, lo: float = 0, hi: float = 95, interval: float = 1.0):
        self.cb, self.expected = progress_cb, max(expected_s, 1.0)
        self.lo, self.hi, self.interval = lo, hi, interval
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self.started = 0.0

    def __enter__(self):
        self.started = time.time()
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=2)

    def _run(self):
        while not self._stop.wait(self.interval):
            el = time.time() - self.started
            frac = min(el / self.expected, 1.0)
            pct = self.lo + (self.hi - self.lo) * frac
            self.cb(pct, eta=max(0, int(self.expected - el)))

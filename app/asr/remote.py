"""远程 GPU 识别：调用 AutoDL 上的 asr-worker。"""
import os
import threading
import time
import uuid

import httpx

from .. import settings
from ..errors import AppError, RemoteUnavailable
from .base import ASRBackend, ASROptions, EstimateTicker

CHUNK = 256 * 1024
FIRST_LOAD_TIMEOUT = 1200  # 秒；远程模型首次下载加载的额外等待时间


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def health(url: str | None = None, token: str | None = None) -> dict:
    s = settings.load()
    url = (url or s.remote_url or "").rstrip("/")
    token = s.remote_token if token is None else token
    if not url:
        return {"online": False, "configured": False}
    t0 = time.time()
    try:
        r = httpx.get(f"{url}/health", headers=_headers(token), timeout=5)
    except httpx.HTTPError as e:
        return {"online": False, "configured": True, "error": f"连接失败：{type(e).__name__}"}
    if r.status_code == 401:
        return {"online": False, "configured": True, "error": "Token 错误"}
    if r.status_code != 200:
        return {"online": False, "configured": True, "error": f"HTTP {r.status_code}"}
    d = r.json()
    d.update(online=True, configured=True, latency_ms=int((time.time() - t0) * 1000))
    return d


class _Multipart:
    """手写 multipart 流，便于统计上传进度。"""

    def __init__(self, path, fields: dict, on_progress, on_done):
        self.boundary = uuid.uuid4().hex
        self.path = path
        head = b""
        for k, v in fields.items():
            head += (f"--{self.boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                     f"{v}\r\n").encode("utf-8")
        head += (f"--{self.boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f"filename=\"audio.ogg\"\r\nContent-Type: audio/ogg\r\n\r\n").encode()
        self.head = head
        self.tail = f"\r\n--{self.boundary}--\r\n".encode()
        self.size = os.path.getsize(path)
        self.length = len(head) + self.size + len(self.tail)
        self.on_progress, self.on_done = on_progress, on_done

    def __iter__(self):
        yield self.head
        sent = 0
        with open(self.path, "rb") as f:
            while chunk := f.read(CHUNK):
                sent += len(chunk)
                self.on_progress(sent / self.size)
                yield chunk
        yield self.tail
        self.on_done()


class RemoteASR(ASRBackend):
    name = "remote"

    def transcribe(self, audio_path, opts: ASROptions, progress_cb, cancel_event, log) -> dict:
        from ..bili import to_opus

        s = settings.load()
        base = (s.remote_url or "").rstrip("/")
        if not base:
            raise RemoteUnavailable("未配置远程地址")
        ogg = to_opus(audio_path)
        log(f"上传音频 {ogg.stat().st_size / 1e6:.1f} MB 到 {base}")
        uploaded = threading.Event()
        body = _Multipart(ogg, {"hotwords": " ".join(opts.hotwords), "spk": "1" if opts.spk else "0",
                                "model": opts.model, "lang": opts.lang},
                          lambda f: progress_cb(f * 20), uploaded.set)
        expected = opts.duration_ms / 1000 * s.rtf_remote + 5
        read_timeout = max(120.0, opts.duration_ms / 1000 / 10)
        h = health()
        if h.get("online"):
            key = {"fun-asr-nano": "nano", "sensevoice": "sensevoice"}.get(
                opts.model, "paraformer+spk" if opts.spk else "paraformer")
            if key not in h.get("models", []):
                # 模型首次使用要在 worker 上现场下载加载，可能好几分钟
                read_timeout += FIRST_LOAD_TIMEOUT
                expected += FIRST_LOAD_TIMEOUT / 3
                log(f"远程还没加载 {opts.model}，首次使用需要下载加载模型，最多等 {FIRST_LOAD_TIMEOUT // 60} 分钟")
            if h.get("busy"):
                log("远程 worker 正在处理其他请求，需要排队")
        ticker_holder = {}

        def start_ticker():
            uploaded.wait()
            t = EstimateTicker(progress_cb, expected, lo=20, hi=95)
            ticker_holder["t"] = t.__enter__()

        th = threading.Thread(target=start_ticker, daemon=True)
        th.start()
        t0 = time.time()
        try:
            r = httpx.post(f"{base}/transcribe", content=body,
                           headers={**_headers(s.remote_token),
                                    "Content-Type": f"multipart/form-data; boundary={body.boundary}",
                                    "Content-Length": str(body.length)},
                           timeout=httpx.Timeout(read_timeout, connect=5.0))
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as e:
            raise RemoteUnavailable(f"远程请求失败：{type(e).__name__}") from e
        finally:
            uploaded.set()
            th.join(timeout=2)
            if "t" in ticker_holder:
                ticker_holder["t"].__exit__(None, None, None)
        if r.status_code == 401:
            raise AppError("REMOTE_UNAUTHORIZED", "远程 Token 错误", "在「设置」里核对 worker 的 ASR_TOKEN")
        if r.status_code >= 500:
            try:
                detail = r.json().get("detail", "")
            except ValueError:
                detail = r.text[:300]
            log(f"远程返回 HTTP {r.status_code}：{detail}")
            raise RemoteUnavailable(f"远程服务错误 HTTP {r.status_code}")
        if r.status_code != 200:
            detail = r.json().get("detail", r.text) if r.headers.get("content-type", "").startswith(
                "application/json") else r.text
            raise AppError("REMOTE_FAILED", f"远程识别失败：{detail}")
        result = r.json()
        elapsed = time.time() - t0
        if opts.duration_ms > 0:
            settings.update_rtf("remote", elapsed / (opts.duration_ms / 1000))
        log(f"远程识别完成，用时 {elapsed:.0f}s，模型 {result.get('model')}")
        result["source"] = "asr_remote"
        result["duration_ms"] = opts.duration_ms
        return result

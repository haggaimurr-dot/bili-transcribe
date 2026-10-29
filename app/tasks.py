"""任务队列与工作线程。"""
import json
import logging
import queue
import re
import threading
import time
import traceback
from pathlib import Path

from common.asr_format import SENSEVOICE_LANGS

from . import bili, db, export, platforms, settings, youtube
from .asr import ASROptions, LocalASR, get_backend
from .config import CACHE_DIR, LOCAL_ID, RESULTS_DIR, TASK_LOG_DIR
from .errors import AppError, Cancelled, RemoteUnavailable
from .events import bus

log = logging.getLogger("tasks")

STAGE_LABEL = {
    "queued": "排队中", "parsing": "解析", "subtitle": "查找字幕", "downloading": "下载音频",
    "transcribing": "识别", "exporting": "导出", "done": "完成", "failed": "失败", "cancelled": "已取消",
}
FINAL = ("done", "failed", "cancelled")


def _mask_secrets(text: str) -> str:
    s = settings.load()
    for v in (s.sessdata, s.bili_jct, s.remote_token):
        if v and len(v) > 4:
            text = text.replace(v, "****" + v[-4:])
    text = re.sub(r"(://)[^/@\s:]+:[^/@\s]+@", r"\1****@", text)  # 代理地址里的账号密码
    return re.sub(r"(SESSDATA=)[^;\s]+", r"\1****", text)


def resolve_asr(opts: dict, platform: str, video_lang: str | None, log) -> tuple[str, str]:
    """按任务的识别语言和视频原语言决定 (lang, model)。

    中文用 paraformer-zh（或远程 fun-asr-nano）；英日韩粤用 sensevoice；
    lang=auto 时，YouTube 看视频的 language 字段，B 站和本地文件默认中文。
    """
    want = (opts.get("lang") or "auto").lower()
    model = opts.get("model") or "paraformer-zh"
    if want == "auto":
        vl = youtube.norm_lang(video_lang) if platform == platforms.YOUTUBE else None
        if platform != platforms.YOUTUBE:
            want = "zh"
        elif vl in SENSEVOICE_LANGS:
            want = vl
            log(f"视频原语言 {video_lang}，按 {want} 识别")
        elif vl is None:
            want = "auto"
            log("视频没有标注语言，交给 SenseVoice 自动判断语种")
        else:
            want = "auto"
            log(f"视频原语言 {video_lang} 不在支持范围（中英日韩粤），识别效果可能很差，建议改用 YouTube 自带字幕")
    if want == "zh":
        return "zh", (model if model in ("paraformer-zh", "fun-asr-nano") else "paraformer-zh")
    return want, "sensevoice"


class TaskLog:
    def __init__(self, task_id: int):
        self.path = TASK_LOG_DIR / f"{task_id}.log"

    def __call__(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {_mask_secrets(msg)}\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)


class Progress:
    """阶段进度上报：节流写库 + 推送事件。"""

    def __init__(self, task_id: int, stage: str):
        self.task_id, self.stage = task_id, stage
        self.last_pct, self.last_t = -1.0, 0.0

    def __call__(self, pct: float, eta: int | None = None, message: str | None = None) -> None:
        pct = round(min(max(pct, 0), 100), 1)
        now = time.time()
        if pct - self.last_pct < 1 and now - self.last_t < 2 and message is None:
            return
        self.last_pct, self.last_t = pct, now
        fields = {"percent": pct}
        if message is not None:
            fields["message"] = message
        db.update_task(self.task_id, **fields)
        bus.publish(self.task_id, {"type": "progress", "id": self.task_id, "stage": self.stage,
                                   "percent": pct, "eta": eta, "message": message})


class TaskManager:
    def __init__(self):
        self.q: queue.Queue[int] = queue.Queue()
        self.cancels: dict[int, threading.Event] = {}
        self.current: int | None = None
        self.thread = threading.Thread(target=self._loop, name="task-worker", daemon=True)

    # ---------- 对外 ----------

    def start(self) -> None:
        n = db.mark_interrupted()
        if n:
            log.info("重启恢复：%d 个未完成任务已标记为失败", n)
        self.thread.start()

    def create(self, *, bvid: str, parts: list[int], meta: dict, opts: dict, platform: str | None = None) -> list[int]:
        ids = []
        platform = platform or (platforms.LOCAL if bvid == LOCAL_ID else platforms.BILIBILI)
        part_titles = {p["index"]: p for p in meta.get("parts", [])}
        for n in parts:
            pt = part_titles.get(n, {})
            tid = db.insert_task(
                platform=platform, bvid=bvid, part=n, title=meta.get("title") or bvid,
                part_title=pt.get("title") if len(part_titles) > 1 else None,
                uploader=meta.get("uploader"), duration_ms=int((pt.get("duration") or 0) * 1000),
                url=platforms.video_url(platform, bvid, n),
                backend=opts.get("backend"), model=opts.get("model"),
                opts=opts, message="",
            )
            ids.append(tid)
            self.cancels[tid] = threading.Event()
            self.q.put(tid)
            self._emit(tid)
        return ids

    def cancel(self, task_id: int) -> None:
        t = db.get_task(task_id)
        if not t or t["status"] in FINAL:
            return
        ev = self.cancels.setdefault(task_id, threading.Event())
        ev.set()
        if t["status"] == "queued":
            self._finish(task_id, "cancelled")

    def retry(self, task_id: int, force_asr: bool | None = None) -> int:
        t = db.get_task(task_id)
        if not t:
            raise AppError("NOT_FOUND", "任务不存在", status=404)
        opts = dict(t["opts"])
        if force_asr is not None:
            opts["force_asr"] = force_asr
        meta = {"title": t["title"], "uploader": t["uploader"],
                "parts": [{"index": t["part"], "title": t["part_title"], "duration": (t["duration_ms"] or 0) / 1000}]}
        new_id = self.create(bvid=t["bvid"], parts=[t["part"]], meta=meta, opts=opts,
                             platform=platforms.platform_of(t))[0]
        if t["part_title"]:
            db.update_task(new_id, part_title=t["part_title"])
        return new_id

    def delete(self, task_id: int) -> None:
        self.cancel(task_id)
        t = db.get_task(task_id)
        db.delete_task(task_id)
        upload = t and t["opts"].get("upload_path")
        if upload and not db.count_opts_containing(json.dumps(upload, ensure_ascii=False)):
            Path(upload).unlink(missing_ok=True)  # 重试任务共用同一个上传文件，最后一个删掉时再删
        d = RESULTS_DIR / str(task_id)
        if d.exists():
            for f in d.iterdir():
                f.unlink(missing_ok=True)
            d.rmdir()
        (TASK_LOG_DIR / f"{task_id}.log").unlink(missing_ok=True)
        bus.publish(task_id, {"type": "deleted", "id": task_id})

    # ---------- 内部 ----------

    def _emit(self, task_id: int) -> None:
        t = db.get_task(task_id)
        if t:
            bus.publish(task_id, {"type": "task", "task": t})

    def _stage(self, task_id: int, stage: str, message: str = "") -> Progress:
        db.update_task(task_id, status=stage, stage=stage, percent=0, message=message)
        self._emit(task_id)
        return Progress(task_id, stage)

    def _finish(self, task_id: int, status: str, error: dict | None = None) -> None:
        fields = {"status": status, "finished_at": time.time()}
        if status == "done":
            fields.update(stage="done", percent=100)
        if error is not None:
            fields["error"] = error
        db.update_task(task_id, **fields)
        self._emit(task_id)
        bus.publish(task_id, {"type": "end", "id": task_id, "status": status})

    def _loop(self) -> None:
        while True:
            tid = self.q.get()
            t = db.get_task(tid)
            if not t or t["status"] != "queued":
                continue
            self.current = tid
            tl = TaskLog(tid)
            try:
                self._run(t, tl)
                self._finish(tid, "done")
                tl("任务完成")
            except Cancelled:
                tl("任务已取消")
                self._finish(tid, "cancelled")
            except AppError as e:
                tl(f"失败：[{e.code}] {e.message} {e.hint}")
                self._finish(tid, "failed", e.to_dict())
            except Exception as e:  # noqa: BLE001
                tl("异常：" + traceback.format_exc())
                log.exception("task %s crashed", tid)
                self._finish(tid, "failed", {"code": "INTERNAL", "message": f"内部错误：{e}", "hint": "查看任务日志"})
            finally:
                self.current = None
                self.cancels.pop(tid, None)

    def _check(self, cancel: threading.Event) -> None:
        if cancel.is_set():
            raise Cancelled()

    def _run(self, t: dict, tl: TaskLog) -> None:
        tid, vid, p, opts = t["id"], t["bvid"], t["part"], t["opts"]
        cancel = self.cancels.setdefault(tid, threading.Event())
        tl(f"开始任务 {vid} P{p}，参数 {opts}")
        upload = opts.get("upload_path")
        platform = platforms.platform_of(t)
        video_lang = None
        if upload:
            result, wav = None, self._prepare_upload(tid, Path(upload), tl)
        elif platform == platforms.YOUTUBE:
            result, wav, video_lang = self._prepare_youtube(t, tl, cancel)
        else:
            result, wav = self._prepare_bili(t, tl, cancel)
        self._check(cancel)

        if result is None:
            duration_ms = bili.probe_duration_ms(wav)
            db.update_task(tid, duration_ms=duration_ms)
            self._check(cancel)
            lang, model = resolve_asr(opts, platform, video_lang, tl)
            result = self._transcribe(tid, wav, {**opts, "model": model, "lang": lang}, duration_ms, cancel, tl)
            if settings.load().auto_delete_audio:
                if upload:  # 只删转换出的 wav/ogg，上传的原文件留着供重试
                    wav.unlink(missing_ok=True)
                    wav.with_suffix(".ogg").unlink(missing_ok=True)
                elif platform == platforms.YOUTUBE:
                    bili.remove_audio_key(youtube.cache_key(vid))
                else:
                    bili.remove_audio(vid, p)

        # ⑤ 导出
        self._stage(tid, "exporting")
        db.set_result(tid, result)
        db.update_task(tid, source=result["source"], model=result.get("model"),
                       duration_ms=result.get("duration_ms") or db.get_task(tid)["duration_ms"])
        export.write_all(db.get_task(tid), result)
        tl(f"已导出到 data/results/{tid}/，共 {len(result['sentences'])} 句")

    def _prepare_upload(self, tid: int, src: Path, tl: TaskLog) -> Path:
        """本地上传：跳过解析、字幕和下载，直接转成 16k wav。"""
        prog = self._stage(tid, "downloading", "转换音频")
        if not src.exists():
            raise AppError("UPLOAD_MISSING", "上传的文件已不存在", "请重新上传")
        wav = CACHE_DIR / f"upload_{tid}.wav"
        bili.to_wav(src, wav)
        prog(100)
        tl(f"已把上传文件 {src.name} 转为 16k 单声道 wav")
        return wav

    def _prepare_bili(self, t: dict, tl: TaskLog, cancel: threading.Event):
        """B 站：解析 → 字幕优先 → 下载音频。返回 (字幕结果或 None, wav 或 None)。"""
        tid, vid, p, opts = t["id"], t["bvid"], t["part"], t["opts"]

        # ① 解析
        self._stage(tid, "parsing")
        info = bili.part_info(vid, p)
        duration_ms = int((info.get("duration") or 0) * 1000)
        upd = {"uploader": info.get("uploader") or t["uploader"]}
        if duration_ms:
            upd["duration_ms"] = duration_ms
        db.update_task(tid, **upd)
        self._check(cancel)

        # ② 字幕优先
        result = None
        if not opts.get("force_asr"):
            self._stage(tid, "subtitle")
            subs = info.get("subtitles") or {}
            picked = bili.pick_subtitle(subs)
            if picked:
                lang, source = picked
                tl(f"找到字幕 {lang}，直接使用")
                try:
                    sents = bili.fetch_subtitle(subs, lang)
                except Exception as e:  # noqa: BLE001
                    tl(f"字幕下载失败：{e}，改走转录")
                    sents = []
                if sents:
                    result = {"source": source, "model": f"subtitle:{lang}",
                              "duration_ms": duration_ms, "sentences": sents}
            else:
                tl("没有可用字幕" + ("" if settings.cookie_file() else "（未配置 Cookie，配置后可能能拿到 AI 字幕）"))
        self._check(cancel)
        if result is not None:
            return result, None

        # ③ 下载音频
        prog = self._stage(tid, "downloading")
        return None, bili.download_audio(vid, p, prog, cancel, tl)

    def _prepare_youtube(self, t: dict, tl: TaskLog, cancel: threading.Event):
        """YouTube：解析 → 字幕优先（人工 > 原语言自动字幕）→ 下载音频。返回 (字幕结果, wav, 视频语言)。"""
        tid, vid, opts = t["id"], t["bvid"], t["opts"]
        self._stage(tid, "parsing")
        if settings.youtube_proxy():
            tl("YouTube 请求走代理")
        info = youtube.info(vid)
        duration_ms = int((info.get("duration") or 0) * 1000)
        lang = youtube.guess_lang(info)
        upd = {"uploader": info.get("channel") or info.get("uploader") or t["uploader"]}
        if duration_ms:
            upd["duration_ms"] = duration_ms
        db.update_task(tid, **upd)
        if info.get("language"):
            tl(f"视频原语言：{lang}")
        else:
            tl("视频没有标注语言" + ("，标题是中文，按中文处理" if lang == "zh" else ""))
        self._check(cancel)

        if not opts.get("force_asr"):
            self._stage(tid, "subtitle")
            picked = youtube.pick_subtitle(info)
            if picked:
                kind, sub_lang, source = picked
                tl(f"找到{'人工' if kind == 'manual' else '自动'}字幕 {sub_lang}，直接使用")
                try:
                    sents = youtube.fetch_subtitle(info, kind, sub_lang)
                except Exception as e:  # noqa: BLE001
                    tl(f"字幕下载失败：{e}，改走转录")
                    sents = []
                if sents:
                    return ({"source": source, "model": f"subtitle:{sub_lang}", "duration_ms": duration_ms,
                             "sentences": sents}, None, lang)
            else:
                tl("没有可用字幕（人工字幕和原语言自动字幕都没有）")
        self._check(cancel)

        prog = self._stage(tid, "downloading")
        return None, youtube.download_audio(vid, prog, cancel, tl), lang

    def _transcribe(self, tid: int, wav: Path, opts: dict, duration_ms: int, cancel: threading.Event,
                    tl: TaskLog) -> dict:
        # ④ 识别
        backend_name = opts.get("backend") or settings.load().default_backend
        model = opts.get("model") or "paraformer-zh"
        asr_opts = ASROptions(hotwords=opts.get("hotwords") or [], spk=bool(opts.get("spk")),
                              model=model, duration_ms=duration_ms, lang=opts.get("lang") or "zh")
        db.update_task(tid, model=model)
        prog = self._stage(tid, "transcribing", "正在识别" if backend_name == "local" else "上传到远程 GPU")
        backend = get_backend(backend_name)
        try:
            result = backend.transcribe(wav, asr_opts, prog, cancel, tl)
        except RemoteUnavailable as e:
            tl(f"远程不可用（{e.message}），已切换本地识别")
            if asr_opts.model == "fun-asr-nano":
                asr_opts.model = "paraformer-zh"
            db.update_task(tid, backend="local", model=asr_opts.model)
            prog = self._stage(tid, "transcribing", "远程不可用，已切换本地")
            result = LocalASR().transcribe(wav, asr_opts, prog, cancel, tl)
        self._check(cancel)
        return result


manager = TaskManager()

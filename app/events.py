"""工作线程 → SSE 的事件总线。"""
import asyncio
import threading
from collections import defaultdict

ALL = "*"


class EventBus:
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self.lock = threading.Lock()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def subscribe(self, key) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self.lock:
            self.subs[str(key)].add(q)
        return q

    def unsubscribe(self, key, q: asyncio.Queue) -> None:
        with self.lock:
            self.subs[str(key)].discard(q)

    def publish(self, task_id, event: dict) -> None:
        """可在任意线程调用。"""
        if self.loop is None:
            return
        with self.lock:
            targets = list(self.subs.get(str(task_id), ())) + list(self.subs.get(ALL, ()))
        for q in targets:
            self.loop.call_soon_threadsafe(_put, q, event)


def _put(q: asyncio.Queue, event: dict) -> None:
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        pass


bus = EventBus()

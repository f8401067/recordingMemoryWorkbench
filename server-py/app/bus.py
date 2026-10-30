from __future__ import annotations

import queue
import threading
from typing import Any

from .db import now_iso


class Bus:
    """进程内事件总线，用于把转写/纪要状态推给 SSE 订阅者。"""

    def __init__(self) -> None:
        self._subs: dict[str, list[queue.Queue]] = {}
        self._lock = threading.Lock()

    def subscribe(self, key: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.setdefault(key, []).append(q)
        return q

    def unsubscribe(self, key: str, q: queue.Queue) -> None:
        with self._lock:
            items = self._subs.get(key)
            if not items:
                return
            if q in items:
                items.remove(q)
            if not items:
                self._subs.pop(key, None)

    def publish(self, key: str, event: str, data: Any) -> None:
        msg = {"event": event, "data": data, "at": now_iso()}
        with self._lock:
            targets = list(self._subs.get(key, []))
        for q in targets:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


bus = Bus()


def emit_recording(recording_id: str, event: str, data: Any) -> None:
    bus.publish(f"rec:{recording_id}", event, data)

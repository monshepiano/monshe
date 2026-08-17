"""In-process event bus for SSE / UI live updates."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from queue import Empty, Queue
from typing import Any

from .config import EVENTS_PATH, new_id


class EventBus:
    def __init__(self, history: int = 200):
        self._subs: list[Queue] = []
        self._lock = threading.Lock()
        self._hist: deque[dict] = deque(maxlen=history)

    def subscribe(self) -> Queue:
        q: Queue = Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def emit(self, kind: str, **payload: Any) -> dict:
        ev = {
            "id": new_id("ev"),
            "kind": kind,
            "ts": time.time(),
            **payload,
        }
        with self._lock:
            self._hist.append(ev)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except Exception:
                pass
        try:
            with EVENTS_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return ev

    def history(self) -> list[dict]:
        with self._lock:
            return list(self._hist)

    def wait(self, q: Queue, timeout: float = 25.0) -> dict | None:
        try:
            return q.get(timeout=timeout)
        except Empty:
            return None


BUS = EventBus()

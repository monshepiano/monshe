"""Шина событий: агент публикует, интерфейс слушает через SSE."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List

_subscribers: List[asyncio.Queue] = []
_history: List[Dict[str, Any]] = []
_HISTORY_LIMIT = 300


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    if q in _subscribers:
        _subscribers.remove(q)


def recent(limit: int = 50) -> List[Dict[str, Any]]:
    return _history[-limit:]


def publish(kind: str, **payload: Any) -> Dict[str, Any]:
    event = {"kind": kind, "ts": time.time(), **payload}
    _history.append(event)
    if len(_history) > _HISTORY_LIMIT:
        del _history[: len(_history) - _HISTORY_LIMIT]
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        unsubscribe(q)
    return event


def sse(event: Dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

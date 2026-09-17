"""Локальная JSONL-телеметрия задержек без содержимого пользовательских данных."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import LOG_DIR

_PATH = LOG_DIR / "latency.jsonl"
_LOCK = threading.Lock()
_MAX_BYTES = 5 * 1024 * 1024


def emit(operation: str, *, duration_ms: Optional[float] = None,
         status: str = "ok", provider: str = "", model: str = "",
         retry: int = 0, ttft_ms: Optional[float] = None, **fields: Any) -> None:
    """Дописывает одну bounded JSON-строку; ошибка логов не ломает продукт."""
    row: Dict[str, Any] = {
        "wall_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "wall_unix": round(time.time(), 3),
        "operation": str(operation or "unknown"),
        "status": str(status or "unknown"),
        "provider": str(provider or ""),
        "model": str(model or ""),
        "retry": int(retry or 0),
    }
    if duration_ms is not None:
        row["duration_ms"] = round(float(duration_ms), 2)
    if ttft_ms is not None:
        row["ttft_ms"] = round(float(ttft_ms), 2)
    for key, value in fields.items():
        if value is not None and isinstance(value, (str, int, float, bool)):
            row[str(key)] = value
    try:
        payload = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _LOCK:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            if _PATH.exists() and _PATH.stat().st_size > _MAX_BYTES:
                old = Path(str(_PATH) + ".1")
                try:
                    old.unlink(missing_ok=True)
                    _PATH.replace(old)
                except Exception:
                    pass
            with _PATH.open("a", encoding="utf-8") as fh:
                fh.write(payload)
    except Exception:
        pass


class Span:
    """Monotonic operation timer with optional TTFT and idempotent finish."""

    def __init__(self, operation: str, **fields: Any) -> None:
        self.operation = operation
        self.fields = fields
        self.started = time.monotonic()
        self.ttft_ms: Optional[float] = None
        self.retry = 0
        self._finished = False

    def first_token(self) -> None:
        if self.ttft_ms is None:
            self.ttft_ms = (time.monotonic() - self.started) * 1000

    def retried(self) -> None:
        self.retry += 1

    def finish(self, status: str = "ok", **fields: Any) -> None:
        if self._finished:
            return
        self._finished = True
        merged = dict(self.fields)
        merged.update(fields)
        emit(self.operation,
             duration_ms=(time.monotonic() - self.started) * 1000,
             status=status,
             retry=self.retry,
             ttft_ms=self.ttft_ms,
             **merged)

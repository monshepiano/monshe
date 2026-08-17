"""Pick the cheapest model that can do the job. Upgrade on failure."""

from __future__ import annotations

import re
from typing import Any, Callable

from . import llm
from .config import MODELS
from .events import BUS

HARD_HINTS = re.compile(
    r"(план|агент|разбери|исследуй|напиши код|рефактор|архитектур|"
    r"много шаг|mission|computer.?use|купи|автоматиз|сравни варианты|"
    r"юридич|анализ рынка|докажи|почему не работает)",
    re.I,
)
VISION_HINTS = re.compile(r"(картинк|изображен|фото|скрин|камера|видео|ocr|распозна)", re.I)


def classify(prompt: str, has_image: bool, agent: bool) -> str:
    if has_image or VISION_HINTS.search(prompt or ""):
        return "vision"
    if agent or HARD_HINTS.search(prompt or "") or len(prompt or "") > 900:
        return "strong"
    return "cheap"


def _chain(start: str) -> list[str]:
    # Try cheap first, then alternatives, then backups. Never jump to the
    # most expensive model unless the task actually needs it.
    if start == "vision":
        return ["vision", "strong", "reason", "backup_reason", "backup_cheap"]
    if start == "strong":
        return ["strong", "reason", "cheap_alt", "backup_reason", "backup_cheap"]
    return ["cheap", "cheap_alt", "backup_cheap", "strong", "backup_reason"]


def run(
    prompt_meta: dict,
    messages: list[dict],
    tools: list[dict] | None,
    on_token: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    start = classify(
        prompt_meta.get("text") or "",
        bool(prompt_meta.get("has_image")),
        bool(prompt_meta.get("agent")),
    )
    errors: list[str] = []
    for key in _chain(start):
        if key not in MODELS:
            continue
        spec = MODELS[key]
        BUS.emit("orchestrator", text=f"Пробую {spec['label']}", model=key)
        try:
            out = llm.complete(key, messages, tools=tools, on_token=on_token)
            out["picked"] = key
            return out
        except Exception as e:
            errors.append(f"{spec['label']}: {e}")
            BUS.emit("thought", text=f"{spec['label']} недоступна, переключаюсь…")
            continue
    raise llm.LLMError("Все модели недоступны:\n" + "\n".join(errors))

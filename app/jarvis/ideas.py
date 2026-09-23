"""Подсказки на пустом экране нового диалога.

Главное требование: новый диалог открывается МГНОВЕННО, а невидимая
персонализация не конкурирует с ответами. Подсказки строятся локально из
недавних тем и дополняются встроенным набором; сетевых вызовов здесь нет.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List

from . import db
from .config import DATA_DIR

COUNT = 6                      # две ровные строки по три карточки
_REFRESH = 6 * 3600            # как часто обновлять
_FILE = DATA_DIR / "ideas.json"

# Запасной набор: показывается, пока личных подсказок ещё нет.
FALLBACK: List[Dict[str, str]] = [
    {"title": "Что нового?", "prompt": "Найди в интернете 5 главных новостей за сегодня и сделай короткую сводку"},
    {"title": "Собери таблицу", "prompt": "Собери таблицу цен на интересующий меня товар в российских магазинах и сохрани в Excel"},
    {"title": "Каждое утро", "prompt": "Каждый день в 9:00 присылай мне погоду и курс доллара в Telegram"},
    {"title": "Нарисуй", "prompt": "Нарисуй логотип для кофейни в стиле неон-минимализм"},
    {"title": "Разбери файл", "prompt": "Я пришлю документ — вытащи из него главное и сделай выжимку по пунктам"},
    {"title": "Наведи порядок", "prompt": "Загляни в мою песочницу, разложи файлы по папкам и скажи, что можно удалить"},
]


def _read() -> Dict[str, Any]:
    try:
        return json.loads(_FILE.read_text("utf-8"))
    except Exception:
        return {}


def _write(data: Dict[str, Any]) -> None:
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILE.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    except Exception:
        pass


def _clean(items: Any) -> List[Dict[str, str]]:
    """Оставить только пригодные пары «заголовок + запрос»."""
    out: List[Dict[str, str]] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        title = " ".join(str(it.get("title") or "").split())[:26]
        prompt = " ".join(str(it.get("prompt") or "").split())[:150]
        if title and len(prompt) > 12:
            out.append({"title": title, "prompt": prompt})
    return out


def _local_personalized() -> List[Dict[str, str]]:
    """Сделать несколько свежих follow-up карточек без скрытого LLM-запроса."""
    out: List[Dict[str, str]] = []
    seen = set()
    for asked in db.recent_user_messages(days=30, limit=12):
        text = " ".join(str(asked or "").split()).strip()
        token = text.casefold()
        if (len(text) < 16 or token in seen or
                re.search(r"\b(?:парол\w*|password|token|secret|api[ _-]?key|cvv)\b", token)):
            continue
        seen.add(token)
        words = re.findall(r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё-]*", text)
        title = " ".join(words[:3]).capitalize()[:26] or "Продолжить тему"
        prompt = ("Вернись к этой задаче и предложи следующий конкретный шаг: «%s»" %
                  text[:96])[:150]
        out.append({"title": title, "prompt": prompt})
        if len(out) >= 3:
            break
    return out


def current() -> List[Dict[str, str]]:
    """Готовые подсказки — мгновенно, без обращения к модели."""
    fresh = _local_personalized()
    saved = _clean(_read().get("items"))
    combined = fresh + saved + FALLBACK
    out: List[Dict[str, str]] = []
    seen = set()
    for item in combined:
        if item["prompt"] in seen:
            continue
        seen.add(item["prompt"])
        out.append(item)
        if len(out) >= COUNT:
            break
    return out


def refresh(force: bool = False) -> List[Dict[str, str]]:
    """Обновить крошечный локальный кэш; сети и LLM здесь нет by design."""
    data = _read()
    if not force and time.time() - float(data.get("at") or 0) < _REFRESH:
        return current()
    items = (_local_personalized() + FALLBACK)[:COUNT]
    _write({"at": time.time(), "items": items})
    return current()


def refresh_async(force: bool = False) -> bool:
    """Обновление локально и дёшево — отдельный поток не нужен."""
    data = _read()
    if not force and time.time() - float(data.get("at") or 0) < _REFRESH:
        return False
    refresh(force=force)
    return True

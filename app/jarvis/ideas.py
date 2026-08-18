"""Подсказки на пустом экране нового диалога.

Главное требование: новый диалог открывается МГНОВЕННО. Поэтому подсказки
никогда не считаются в момент открытия — они лежат готовыми в файле и
пересчитываются в фоне (при старте и раз в несколько часов). Если готовых
ещё нет или модель недоступна, показываем встроенный набор: экран всегда
заполнен, ждать не приходится.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Dict, List

from . import db, llm
from .config import CONFIG, DATA_DIR

COUNT = 6                      # две ровные строки по три карточки
_REFRESH = 6 * 3600            # как часто обновлять
_FILE = DATA_DIR / "ideas.json"
_LOCK = threading.Lock()
_BUSY = False

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


def current() -> List[Dict[str, str]]:
    """Готовые подсказки — мгновенно, без обращения к модели."""
    saved = _clean(_read().get("items"))
    if len(saved) >= COUNT:
        return saved[:COUNT]
    # добиваем запасными, чтобы вторая строка не пустовала
    seen = {i["prompt"] for i in saved}
    return (saved + [f for f in FALLBACK if f["prompt"] not in seen])[:COUNT]


def _prompt_source() -> str:
    facts = db.recall(limit=20)
    asked = db.recent_user_messages(days=30, limit=40)
    parts = []
    if facts:
        parts.append("Факты о пользователе:\n" +
                     "\n".join("- %s: %s" % (m["key"], m["value"]) for m in facts))
    if asked:
        parts.append("О чём он просил за последний месяц:\n" +
                     "\n".join("- " + a for a in asked[:30]))
    return "\n\n".join(parts)


def refresh(force: bool = False) -> List[Dict[str, str]]:
    """Пересчитать подсказки. Вызывается в фоне, результат кладётся в файл."""
    global _BUSY
    data = _read()
    if not force and time.time() - float(data.get("at") or 0) < _REFRESH:
        return current()
    with _LOCK:
        if _BUSY:
            return current()
        _BUSY = True
    try:
        source = _prompt_source()
        if not source.strip():
            return current()          # нечего персонализировать — оставляем как есть
        old = [i["prompt"] for i in _clean(data.get("items"))]
        avoid = ("\n\nНЕ повторяй эти формулировки:\n" + "\n".join("- " + o for o in old)) if old else ""
        out = llm.chat([
            {"role": "system", "content":
             "Ты — ассистент JARVIS. По фактам о пользователе и его недавним запросам придумай "
             "РОВНО %d новых идей, что он мог бы поручить тебе прямо сейчас. Идеи должны быть "
             "конкретными, полезными и разными: опирайся на его дела и привычки, а не на общие "
             "фразы. Не повторяй буквально его прошлые запросы — предлагай следующий шаг или "
             "то, до чего он ещё не дошёл. Ответь ТОЛЬКО массивом JSON вида "
             '[{"title":"2-3 слова","prompt":"готовый запрос от первого лица"}]. '
             "Без пояснений и markdown." % COUNT},
            {"role": "user", "content": source + avoid},
        ], tier="base", max_tokens=700, temperature=0.9).get("content", "")
        items = _clean(_parse(out))
        if len(items) >= 3:
            _write({"at": time.time(), "items": items[:COUNT]})
    except Exception:
        pass
    finally:
        _BUSY = False
    return current()


def _parse(text: str) -> Any:
    """Достать JSON-массив из ответа модели, даже если он в ```-блоке."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.I)
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return []
    return []


def refresh_async(force: bool = False) -> None:
    """Обновление всегда в фоне: интерфейс не должен ждать модель."""
    if not CONFIG.get("auto.enabled", True):
        return
    threading.Thread(target=refresh, args=(force,), daemon=True).start()

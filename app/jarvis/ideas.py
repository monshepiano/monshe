"""Подсказки на пустом экране нового диалога.

Главное требование: новый диалог открывается МГНОВЕННО, а невидимая
персонализация не конкурирует с ответами. Подсказки строятся локально из
недавних тем и дополняются встроенным набором; сетевых вызовов здесь нет.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Dict, List

from . import db
from .config import DATA_DIR

COUNT = 6                      # две ровные строки по три карточки
_REFRESH = 6 * 3600            # как часто обновлять локальный кэш
_AI_REFRESH = 6 * 3600         # AY: как часто ИИ пересматривает плитки
_FILE = DATA_DIR / "ideas.json"

# AY: фоновая ИИ-генерация — один поток на процесс, без гонок
_AI_LOCK = threading.Lock()
_AI_RUNNING = False

# Запасной набор: показывается, пока личных подсказок ещё нет.
# AZ: на плитке — НАЗВАНИЕ и ОПИСАНИЕ, что произойдёт; сам запрос (prompt)
# пользователь увидит в поле ввода уже после клика по плитке.
FALLBACK: List[Dict[str, str]] = [
    {"title": "Что нового?",
     "desc": "Найду главные новости дня в интернете и соберу короткую сводку",
     "prompt": "Найди в интернете 5 главных новостей за сегодня и сделай короткую сводку"},
    {"title": "Собери таблицу",
     "desc": "Пройду по магазинам, соберу цены в ровную таблицу и сохраню её в Excel",
     "prompt": "Собери таблицу цен на интересующий меня товар в российских магазинах и сохрани в Excel"},
    {"title": "Каждое утро",
     "desc": "Настрою расписанное задание: погода и курс доллара будут приходить к 9:00",
     "prompt": "Каждый день в 9:00 присылай мне погоду и курс доллара в Telegram"},
    {"title": "Нарисую",
     "desc": "Придумаю и нарисую картинку по описанию — покажу, что получилось",
     "prompt": "Нарисуй логотип для кофейни в стиле неон-минимализм"},
    {"title": "Разберу файл",
     "desc": "Прочитаю присланный документ, вытащу главное и соберу выжимку по пунктам",
     "prompt": "Я пришлю документ — вытащи из него главное и сделай выжимку по пунктам"},
    {"title": "Наведу порядок",
     "desc": "Загляну в песочницу, разложу файлы по папкам и подскажу, что можно удалить",
     "prompt": "Загляни в мою песочницу, разложи файлы по папкам и скажи, что можно удалить"},
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
        # AZ: описание — что БУДЕТ ПРОИСХОДИТЬ; старым записям без него
        # на плитке останется сам запрос
        desc = " ".join(str(it.get("desc") or "").split())[:140]
        prompt = " ".join(str(it.get("prompt") or "").split())[:150]
        if title and len(prompt) > 12:
            out.append({"title": title, "desc": desc, "prompt": prompt})
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
        desc = "Вернусь к недавней задаче, предложу и сделаю следующий конкретный шаг"
        prompt = ("Вернись к этой задаче и предложи следующий конкретный шаг: «%s»" %
                  text[:96])[:150]
        out.append({"title": title, "desc": desc, "prompt": prompt})
        if len(out) >= 3:
            break
    return out


def current() -> List[Dict[str, str]]:
    """Готовые подсказки — мгновенно, без обращения к модели."""
    data = _read()
    fresh = _local_personalized()
    saved = _clean(data.get("items"))
    # AY: ИИ-придуманные плитки идут первыми — это живые продолжения
    # тем пользователя, а не встроенный запас
    combined = (saved + fresh + FALLBACK) if data.get("source") == "ai" \
        else (fresh + saved + FALLBACK)
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
    _write({"at": time.time(), "items": items, "source": "local"})
    return current()


def refresh_async(force: bool = False) -> bool:
    """Обновление локально и дёшево — отдельный поток не нужен."""
    data = _read()
    if not force and time.time() - float(data.get("at") or 0) < _REFRESH:
        return False
    refresh(force=force)
    return True


def _ai_personalized() -> List[Dict[str, str]]:
    """AY: плитки придумывает ИИ по недавним темам пользователя.

    Джарвис сам предлагает, о чём поговорить: часть плиток — естественное
    продолжение недавних задач, часть — новые полезные дела. Вызов
    дешёвой nano-модели; сбой честно оставляет локальный набор."""
    from . import llm
    topics: List[str] = []
    for asked in db.recent_user_messages(days=14, limit=8):
        text = " ".join(str(asked or "").split())
        if len(text) >= 8:
            topics.append(text[:120])
    raw = llm.chat([
        {"role": "system",
         "content": "Ты генератор стартовых плиток-подсказок для экрана "
                    "нового диалога с персональным ИИ-агентом JARVIS." +
                    (" Пользователь недавно спрашивал о: %s."
                     % "; ".join(topics[:6]) if topics else "") +
                    " Придумай ШЕСТЬ плиток для нового диалога: две-три — "
                    "живое продолжение недавних тем, остальные — новые "
                    "полезные дела (поиск в интернете, файлы, картинки, "
                    "расписание). Для каждой — от первого лица, по-русски: "
                    "заголовок до 3 слов, описание из 12-16 слов о том, что "
                    "именно произойдёт (без общих слов), и полный запрос "
                    "одной фразой до 14 слов. Ответь ТОЛЬКО JSON-массивом из "
                    "шести объектов {\"title\": \"...\", \"desc\": \"...\", "
                    "\"prompt\": \"...\"}."},
        {"role": "user",
         "content": "Недавние темы: %s" % ("; ".join(topics) or "нет данных")},
    ], tier="nano", max_tokens=500, temperature=0.8, timeout=8,
       operation="welcome_ideas")
    content = raw.get("content") if isinstance(raw, dict) else str(raw)
    match = re.search(r"\[.*\]", str(content or ""), re.S)
    if not match:
        return []
    data = json.loads(match.group(0))
    return _clean(data)


def refresh_ai_async() -> bool:
    """AY: фоновая ИИ-генерация плиток; True — она запущена сейчас.

    Экран нового диалога по-прежнему открывается МГНОВЕННО с локальным
    набором; ИИ-плитки готовятся в фоне и подменяются на экране повторным
    запросом клиента, как только готовы. Кэш живёт _AI_REFRESH часов."""
    global _AI_RUNNING
    data = _read()
    if data.get("source") == "ai" and \
            time.time() - float(data.get("at") or 0) < _AI_REFRESH:
        return False

    def work() -> None:
        global _AI_RUNNING
        try:
            items = _ai_personalized()
            if items:
                _write({"at": time.time(), "items": items, "source": "ai"})
        except Exception:
            pass
        finally:
            with _AI_LOCK:
                _AI_RUNNING = False

    with _AI_LOCK:
        if _AI_RUNNING:
            return True
        _AI_RUNNING = True
    threading.Thread(target=work, daemon=True,
                     name="jarvis-ideas").start()
    return True

"""AUTO: фоновый исполнитель задач, расписания и проактивные подсказки."""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from . import agent, db, llm
from .config import CONFIG
from .tools import media

_STOP = threading.Event()
_WORKER: Optional[threading.Thread] = None
_RUNNING: Dict[str, bool] = {}


# ------------------------------------------------------------------ расписание
_UNIT_FACTOR = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "с": 1, "сек": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "мин": 60,
    "h": 3600, "hour": 3600, "hours": 3600, "ч": 3600, "час": 3600,
    "d": 86400, "day": 86400, "days": 86400, "д": 86400, "день": 86400,
}

MIN_INTERVAL = 5  # чаще пяти секунд не запускаем


def parse_schedule(text: str) -> Optional[float]:
    """'every 10s' | 'every 30m' | 'in 15s' | 'daily 09:00' → следующий запуск (timestamp).

    Возвращает None для разовых задач без расписания.
    """
    if not text:
        return None
    t = re.sub(r"\s+", " ", text.strip().lower())
    now = time.time()
    # повтор: every N <unit>
    m = re.match(r"(?:every|каждые|каждый|раз в)\s*(\d+)?\s*([a-zа-я]+)", t)
    if m and (m.group(1) or m.group(2) in _UNIT_FACTOR):
        unit = m.group(2)
        factor = _UNIT_FACTOR.get(unit)
        if factor:
            value = int(m.group(1) or 1)
            return now + max(MIN_INTERVAL, value * factor)
    # разово: in N <unit> / через N <unit>
    m = re.match(r"(?:in|after|через|спустя)\s*(\d+)\s*([a-zа-я]+)", t)
    if m:
        factor = _UNIT_FACTOR.get(m.group(2))
        if factor:
            return now + max(1, int(m.group(1)) * factor)
    # ежедневно в HH:MM
    m = re.match(r"(?:daily|ежедневно|каждый день)\s*(?:в\s*)?(\d{1,2}):(\d{2})", t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        target = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(days=1)
        return target.timestamp()
    # просто время HH:MM
    m = re.fullmatch(r"(?:at|в)?\s*(\d{1,2}):(\d{2})", t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        target = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(days=1)
        return target.timestamp()
    return None


def is_repeating(text: str) -> bool:
    """Повторяется ли расписание (every/daily) или это однократный отложенный запуск."""
    t = (text or "").strip().lower()
    return bool(re.match(r"(?:every|каждые|каждый|раз в|daily|ежедневно)\b", t))


def describe_schedule(text: str) -> str:
    """Человеческое описание расписания для UI."""
    t = (text or "").strip().lower()
    if not t:
        return "разово"
    m = re.match(r"(?:every|каждые|каждый|раз в)\s*(\d+)?\s*([a-zа-я]+)", t)
    if m and _UNIT_FACTOR.get(m.group(2)):
        secs = int(m.group(1) or 1) * _UNIT_FACTOR[m.group(2)]
        return "каждые " + _human_secs(secs)
    m = re.match(r"(?:in|after|через|спустя)\s*(\d+)\s*([a-zа-я]+)", t)
    if m and _UNIT_FACTOR.get(m.group(2)):
        return "через " + _human_secs(int(m.group(1)) * _UNIT_FACTOR[m.group(2)])
    m = re.search(r"(\d{1,2}):(\d{2})", t)
    if m:
        return "ежедневно в %s:%s" % (m.group(1).rjust(2, "0"), m.group(2))
    return t


def _human_secs(secs: int) -> str:
    if secs < 60:
        return "%d сек" % secs
    if secs < 3600:
        return "%d мин" % (secs // 60)
    if secs < 86400:
        return "%d ч" % (secs // 3600)
    return "%d дн" % (secs // 86400)


def in_quiet_hours() -> bool:
    quiet = CONFIG.get("auto.quiet_hours", [1, 8]) or [1, 8]
    hour = datetime.now().hour
    start, end = int(quiet[0]), int(quiet[1])
    return start <= hour < end if start < end else (hour >= start or hour < end)


# --------------------------------------------------------------- исполнение
def _norm(text: str) -> str:
    """Грубая нормализация фразы: для сравнения задач между собой."""
    return re.sub(r"[^\w]+", " ", (text or "").lower()).strip()


def has_similar_pending(text: str, chat_id: str = "") -> bool:
    """Уже есть незавершённая задача с тем же смыслом из этого же диалога?

    Спасает от ситуации, когда пользователь повторил просьбу, а модель ещё и
    сама вызвала schedule_task — и в AUTO появлялось два-три клона.
    """
    want = _norm(text)
    if not want:
        return False
    for task in db.list_tasks():
        if task.get("status") not in ("queued", "scheduled", "running"):
            continue
        if chat_id and task.get("chat_id") and task.get("chat_id") != chat_id:
            continue
        if _norm(task.get("prompt", "")) == want:
            return True
    return False


def execute_task(task_id: str) -> None:
    task = db.get_task(task_id)
    if not task or _RUNNING.get(task_id):
        return
    _RUNNING[task_id] = True
    db.update_task(task_id, status="running", progress=0.05)
    db.append_task_event(task_id, {"type": "status", "text": "Задача запущена"})
    try:
        result = agent.run_headless(task["prompt"], task_id=task_id, agent_mode=True,
                                    chat_id=task.get("chat_id") or "")
        content = result.get("content") or "Задача выполнена."
        files = result.get("files") or []
        db.update_task(task_id, status="done", progress=1.0, result=content)
        db.append_task_event(task_id, {"type": "done", "text": "Готово"})
        db.notify("AUTO: " + task["title"], content[:300], "success")
        # пишем ответ прямо в диалог, откуда задачу поставили, — пользователь
        # просил «напиши мне», значит сообщение должно появиться в переписке
        if task.get("chat_id"):
            db.add_message(task["chat_id"], "assistant", content,
                           {"task_id": task_id, "from_auto": True,
                            "files": files, "title": task.get("title", "")})
        _telegram_report(task["title"], content, files)

        schedule = task.get("schedule") or ""
        if is_repeating(schedule):
            nxt = parse_schedule(schedule)
            if nxt:
                db.update_task(task_id, status="scheduled", next_run=nxt, progress=0)
    except Exception as exc:
        db.update_task(task_id, status="error", result="Ошибка: %s" % exc)
        db.append_task_event(task_id, {"type": "error", "text": str(exc)[:300]})
        db.notify("AUTO: ошибка в задаче", "%s — %s" % (task["title"], exc), "error")
    finally:
        _RUNNING.pop(task_id, None)


def _telegram_report(title: str, content: str, files: List[Dict[str, Any]]) -> None:
    conf = CONFIG.get("telegram", {}) or {}
    if not conf.get("enabled") or not conf.get("bot_token") or not conf.get("chat_id"):
        return
    text = "<b>JARVIS · задача выполнена</b>\n<b>%s</b>\n\n%s" % (title, content[:3000])
    media.send_telegram(text, silent=in_quiet_hours())
    for f in files[:3]:
        if f.get("name"):
            media.telegram_send_file(f["name"], caption=title)


# --------------------------------------------------------------- проактивность
_LAST_PROACTIVE = 0.0


def proactive_tick() -> None:
    """Раз в несколько часов JARVIS сам предлагает полезное действие."""
    global _LAST_PROACTIVE
    if not CONFIG.get("auto.proactive", True) or in_quiet_hours():
        return
    if time.time() - _LAST_PROACTIVE < 4 * 3600:
        return
    _LAST_PROACTIVE = time.time()
    memories = db.recall(limit=25)
    if not memories:
        return
    facts = "\n".join("- %s: %s" % (m["key"], m["value"]) for m in memories[:20])
    try:
        out = llm.chat([
            {"role": "system", "content":
             "Ты — проактивный ассистент JARVIS. На основе фактов о пользователе предложи ОДНО "
             "конкретное полезное действие прямо сейчас (1-2 предложения, по-русски). "
             "Если полезного нет — ответь ровно 'NONE'."},
            {"role": "user", "content": facts},
        ], tier="nano", max_tokens=200, temperature=0.7).get("content", "").strip()
        if out and "NONE" not in out.upper():
            db.notify("Идея от JARVIS", out[:400], "info")
            conf = CONFIG.get("telegram", {}) or {}
            if conf.get("enabled"):
                media.send_telegram("💡 <b>JARVIS</b>\n" + out[:900], silent=True)
    except Exception:
        pass


# -------------------------------------------------------------------- worker
def _loop() -> None:
    while not _STOP.is_set():
        nearest = None
        try:
            if CONFIG.get("auto.enabled", True):
                now_ts = time.time()
                for task in db.list_tasks(limit=60):
                    if _STOP.is_set():
                        break
                    status = task.get("status")
                    if status == "queued":
                        threading.Thread(target=execute_task, args=(task["id"],), daemon=True).start()
                        time.sleep(0.2)
                    elif status == "scheduled":
                        nxt = task.get("next_run") or 0
                        if nxt <= now_ts:
                            # запускаем СРАЗУ, а не «ставим в очередь и ждём
                            # следующего круга» — иначе задача опаздывает
                            # на целый тик сверх назначенного времени
                            db.update_task(task["id"], status="queued")
                            threading.Thread(target=execute_task, args=(task["id"],),
                                             daemon=True).start()
                        else:
                            left = nxt - now_ts
                            nearest = left if nearest is None else min(nearest, left)
                # проактивные идеи ходят в сеть: в отдельном потоке, иначе
                # медленный ответ модели задерживает все напоминания
                threading.Thread(target=proactive_tick, daemon=True).start()
        except Exception:
            pass
        # тик подстраивается под ближайшую задачу: секундные напоминания не опаздывают
        base_tick = max(2, int(CONFIG.get("auto.tick_seconds", 10)))
        wait = base_tick if nearest is None else max(0.25, min(base_tick, nearest))
        _STOP.wait(wait)


def start() -> None:
    global _WORKER
    if _WORKER and _WORKER.is_alive():
        return
    _STOP.clear()
    _WORKER = threading.Thread(target=_loop, name="jarvis-auto", daemon=True)
    _WORKER.start()


def stop() -> None:
    _STOP.set()


def create_background_task(title: str, prompt: str, schedule: str = "",
                           chat_id: str = "") -> Dict[str, Any]:
    """Создаёт задачу AUTO с учётом расписания (отложенные не стартуют сразу)."""
    task = db.create_task(title=title or "Фоновая задача", prompt=prompt,
                          mode="auto", schedule=schedule or "", chat_id=chat_id)
    nxt = parse_schedule(schedule)
    if nxt and nxt > time.time() + 0.5:
        db.update_task(task["id"], status="scheduled", next_run=nxt)
        task = db.get_task(task["id"]) or task
    return task


# ------------------------------------------------ распознавание «в фон?»
_UNIT_WORDS = {
    "сек": 1, "секунд": 1, "секунды": 1, "секунду": 1, "сек.": 1, "s": 1, "sec": 1,
    "мин": 60, "минут": 60, "минуты": 60, "минуту": 60, "мин.": 60, "m": 60,
    "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600, "h": 3600,
    "день": 86400, "дня": 86400, "дней": 86400, "сутки": 86400, "суток": 86400, "d": 86400,
    "недел": 604800,
}


def _unit_secs(word: str) -> Optional[int]:
    w = (word or "").lower().strip(".")
    for key in sorted(_UNIT_WORDS, key=len, reverse=True):
        if w.startswith(key):
            return _UNIT_WORDS[key]
    return None


def _fmt_every(secs: int) -> str:
    if secs % 86400 == 0:
        return "every %dd" % (secs // 86400)
    if secs % 3600 == 0:
        return "every %dh" % (secs // 3600)
    if secs % 60 == 0:
        return "every %dm" % (secs // 60)
    return "every %ds" % secs


def _fmt_in(secs: int) -> str:
    if secs % 86400 == 0:
        return "in %dd" % (secs // 86400)
    if secs % 3600 == 0:
        return "in %dh" % (secs // 3600)
    if secs % 60 == 0:
        return "in %dm" % (secs // 60)
    return "in %ds" % secs


_NUM_WORDS = {"пол": 0.5, "один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3,
              "четыре": 4, "пять": 5, "десять": 10, "пятнадцать": 15, "двадцать": 20,
              "тридцать": 30, "сорок": 40, "полчаса": 0}


def detect_schedule(text: str) -> str:
    """Достаёт расписание из обычной фразы пользователя."""
    t = re.sub(r"\s+", " ", (text or "").lower())

    # «каждый день», «ежедневно», «каждое утро» → daily HH:MM
    if any(w in t for w in ("ежедневно", "каждый день", "каждые сутки", "каждое утро",
                            "каждый вечер", "по утрам", "по вечерам")):
        hm = re.search(r"(?:в|к)\s*(\d{1,2})[:.](\d{2})", t)
        if hm:
            return "daily %s:%s" % (hm.group(1).rjust(2, "0"), hm.group(2))
        if "вечер" in t:
            return "daily 20:00"
        return "daily 09:00"

    # «каждые N единиц» / «раз в N единиц» / «каждый час»
    m = re.search(r"(?:кажд\w*|раз в|каждые)\s+(?:(\d+)\s*)?([а-яё]+)", t)
    if m:
        secs = _unit_secs(m.group(2))
        if secs:
            value = int(m.group(1) or 1)
            return _fmt_every(max(5, value * secs))

    # «через N единиц» / «спустя N единиц»
    m = re.search(r"(?:через|спустя)\s+(\d+)\s*([а-яёa-z.]+)", t)
    if m:
        secs = _unit_secs(m.group(2))
        if secs:
            return _fmt_in(max(1, int(m.group(1)) * secs))
    m = re.search(r"(?:через|спустя)\s+(полчаса|полминуты|минуту|час|день|сутки|неделю)", t)
    if m:
        word = m.group(1)
        table = {"полчаса": 1800, "полминуты": 30, "минуту": 60, "час": 3600,
                 "день": 86400, "сутки": 86400, "неделю": 604800}
        return _fmt_in(table[word])

    # «в 18:30» / «завтра в 9:00»
    m = re.search(r"\bв\s*(\d{1,2})[:.](\d{2})\b", t)
    if m and any(w in t for w in ("напомни", "напиши", "разбуди", "пришли", "сообщи")):
        return "daily %s:%s" % (m.group(1).rjust(2, "0"), m.group(2))
    return ""


_REMIND_WORDS = ("напомни", "напоминай", "разбуди", "напиши мне", "пришли мне",
                 "сообщи мне", "предупреди", "оповести", "уведоми")
_WATCH_WORDS = ("следи", "мониторь", "отслеживай", "проверяй", "наблюдай", "держи в курсе")
_BG_WORDS = ("в фоне", "фоном", "в фоновом режиме", "по расписанию", "регулярно",
             "потом пришли", "собери отчёт", "автоматически")


def should_background(text: str) -> Dict[str, Any]:
    """JARVIS сам решает, уходит ли задача в фон."""
    t = re.sub(r"\s+", " ", (text or "").lower())
    schedule = detect_schedule(t)

    if schedule:
        if is_repeating(schedule):
            reason = "регулярная задача по расписанию"
        else:
            reason = "отложенное напоминание"
        return {"background": True, "reason": reason, "schedule": schedule}

    if any(k in t for k in _WATCH_WORDS):
        return {"background": True, "reason": "нужно следить за изменениями",
                "schedule": "every 1h"}
    if any(k in t for k in _REMIND_WORDS):
        return {"background": True, "reason": "напоминание", "schedule": ""}
    if any(k in t for k in _BG_WORDS):
        return {"background": True, "reason": "длительная фоновая задача", "schedule": ""}
    if len(t) > 400 and any(k in t for k in ("исследуй", "проанализируй рынок",
                                             "составь подборку", "собери")):
        return {"background": True, "reason": "объёмное исследование", "schedule": ""}
    return {"background": False, "reason": "", "schedule": ""}

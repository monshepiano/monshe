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
def parse_schedule(text: str) -> Optional[float]:
    """'every 30m' | 'every 2h' | 'daily 09:00' | 'once' → следующий запуск (timestamp)."""
    if not text:
        return None
    t = text.strip().lower()
    now = time.time()
    m = re.match(r"every\s+(\d+)\s*(m|min|h|hour|d|day)", t)
    if m:
        value = int(m.group(1))
        unit = m.group(2)
        factor = {"m": 60, "min": 60, "h": 3600, "hour": 3600, "d": 86400, "day": 86400}[unit]
        return now + value * factor
    m = re.match(r"daily\s+(\d{1,2}):(\d{2})", t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        target = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(days=1)
        return target.timestamp()
    return None


def in_quiet_hours() -> bool:
    quiet = CONFIG.get("auto.quiet_hours", [1, 8]) or [1, 8]
    hour = datetime.now().hour
    start, end = int(quiet[0]), int(quiet[1])
    return start <= hour < end if start < end else (hour >= start or hour < end)


# --------------------------------------------------------------- исполнение
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
        _telegram_report(task["title"], content, files)

        schedule = task.get("schedule") or ""
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
        try:
            if CONFIG.get("auto.enabled", True):
                for task in db.list_tasks(limit=60):
                    if _STOP.is_set():
                        break
                    status = task.get("status")
                    if status == "queued":
                        threading.Thread(target=execute_task, args=(task["id"],), daemon=True).start()
                        time.sleep(1)
                    elif status == "scheduled" and (task.get("next_run") or 0) <= time.time():
                        db.update_task(task["id"], status="queued")
                proactive_tick()
        except Exception:
            pass
        _STOP.wait(max(10, int(CONFIG.get("auto.tick_seconds", 30))))


def start() -> None:
    global _WORKER
    if _WORKER and _WORKER.is_alive():
        return
    _STOP.clear()
    _WORKER = threading.Thread(target=_loop, name="jarvis-auto", daemon=True)
    _WORKER.start()


def stop() -> None:
    _STOP.set()


def should_background(text: str) -> Dict[str, Any]:
    """JARVIS сам решает, уходит ли задача в фон."""
    t = (text or "").lower()
    triggers = ["каждый день", "каждые", "раз в час", "следи", "мониторь", "напоминай",
                "в фоне", "фоном", "потом пришли", "собери отчёт", "по расписанию", "регулярно"]
    if any(k in t for k in triggers):
        schedule = ""
        if "каждый день" in t or "ежедневно" in t:
            schedule = "daily 09:00"
        elif "раз в час" in t or "каждый час" in t:
            schedule = "every 1h"
        elif re.search(r"кажд\w+\s+(\d+)\s*мин", t):
            schedule = "every %sm" % re.search(r"кажд\w+\s+(\d+)\s*мин", t).group(1)
        return {"background": True, "reason": "регулярная или длительная задача", "schedule": schedule}
    if len(t) > 400 and any(k in t for k in ("исследуй", "проанализируй рынок", "составь подборку", "собери")):
        return {"background": True, "reason": "объёмное исследование", "schedule": ""}
    return {"background": False, "reason": "", "schedule": ""}

"""Фоновые процессы: очередь задач, расписание, проактивность, приём команд из телеграма."""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Dict, Optional

from . import config, events, store

_queue: "asyncio.Queue[str]" = asyncio.Queue()
_tasks: list[asyncio.Task] = []
_tg_offset: Optional[int] = None


def schedule_new(title: str, goal: str, schedule: str = "once",
                 delay_minutes: int = 0, chat_id: str = "", source: str = "agent") -> str:
    next_run = time.time() + delay_minutes * 60 if delay_minutes else None
    tid = store.create_task(title, goal, chat_id=chat_id, source=source,
                            schedule=schedule if schedule != "once" else "",
                            next_run=next_run)
    if next_run:
        store.update_task(tid, status="scheduled")
        events.publish("task_scheduled", task_id=tid, title=title, next_run=next_run)
    else:
        enqueue(tid)
    return tid


def enqueue(task_id: str) -> None:
    store.update_task(task_id, status="pending")
    try:
        _queue.put_nowait(task_id)
    except asyncio.QueueFull:
        pass
    events.publish("task_queued", task_id=task_id)


def _interval(schedule: str) -> Optional[float]:
    s = (schedule or "").strip().lower()
    if s in ("hourly", "часовой"):
        return 3600
    if s in ("daily", "ежедневно"):
        return 86400
    if s in ("weekly", "еженедельно"):
        return 604800
    m = re.match(r"every:(\d+)([mhd])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return n * {"m": 60, "h": 3600, "d": 86400}[unit]
    return None


# ------------------------------------------------------------------ воркеры
async def worker_loop() -> None:
    from . import agent
    while True:
        task_id = await _queue.get()
        try:
            await agent.run_task(task_id)
            t = store.get_task(task_id)
            if t and t.get("schedule"):
                iv = _interval(t["schedule"])
                if iv:
                    store.update_task(task_id, next_run=time.time() + iv, status="scheduled")
        except Exception as e:
            events.publish("task_failed", task_id=task_id, error=str(e))
        finally:
            _queue.task_done()


async def scheduler_loop() -> None:
    while True:
        try:
            now = time.time()
            for t in store.due_tasks(now):
                store.update_task(t["id"], next_run=None)
                enqueue(t["id"])
        except Exception:
            pass
        await asyncio.sleep(20)


async def proactive_loop() -> None:
    """Раз в час Джарвис сам смотрит, не пора ли что-то предложить."""
    await asyncio.sleep(120)
    while True:
        try:
            if config.get("proactive"):
                await _proactive_tick()
        except Exception:
            pass
        await asyncio.sleep(3600)


async def _proactive_tick() -> None:
    from . import router
    mem = store.recall(limit=25)
    if not mem:
        return
    tasks = store.list_tasks(10)
    prompt = (
        "Ты — Джарвис, фоновая проактивная проверка. Вот что ты знаешь о хозяине:\n" +
        "\n".join(f"- {m['kind']}/{m['key']}: {m['value']}" for m in mem) +
        "\n\nПоследние задачи: " + json.dumps(
            [{"t": t["title"], "s": t["status"]} for t in tasks], ensure_ascii=False) +
        "\n\nЕсть ли что-то полезное, о чём стоит напомнить или что стоит проверить прямо сейчас? "
        "Если да — ответь JSON {\"notify\": true, \"title\": \"...\", \"body\": \"...\"}. "
        "Если ничего важного — ответь {\"notify\": false}. Только JSON."
    )
    try:
        res = await router.complete([{"role": "user", "content": prompt}],
                                    tier="light", max_tokens=300)
        m = re.search(r"\{.*\}", res.text or "", re.S)
        if not m:
            return
        data = json.loads(m.group(0))
        if data.get("notify"):
            from .tools import telegram
            n = store.add_notification(data.get("title", "Джарвис"),
                                       data.get("body", ""), "info",
                                       {"type": "proactive"})
            events.publish("notification", **n)
            if telegram.enabled():
                await telegram.send(f"💡 <b>{n['title']}</b>\n{n['body']}")
    except Exception:
        pass


async def telegram_loop() -> None:
    """Приём сообщений из телеграма: команды и подтверждения с телефона."""
    global _tg_offset
    from . import agent
    from .tools import telegram
    while True:
        try:
            if not telegram.enabled():
                await asyncio.sleep(20)
                continue
            data = await telegram.get_updates(_tg_offset, timeout=25)
            for upd in data.get("result", []):
                _tg_offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("channel_post") or {}
                text = (msg.get("text") or "").strip()
                chat = str((msg.get("chat") or {}).get("id", ""))
                allowed = str(config.get("telegram_chat_id") or "")
                if allowed and chat != allowed:
                    continue
                if not text:
                    continue
                await _handle_tg(text, agent, telegram)
        except Exception:
            await asyncio.sleep(5)
        await asyncio.sleep(1)


async def _handle_tg(text: str, agent, telegram) -> None:
    low = text.lower()
    if low.startswith(("/yes", "/да", "да ")) or low.startswith(("/no", "/нет", "нет ")):
        approve = low.startswith(("/yes", "/да", "да "))
        parts = text.split()
        aid = parts[1] if len(parts) > 1 else ""
        if not aid:
            pend = store.pending_approvals()
            aid = pend[0]["id"] if pend else ""
        if aid and agent.resolve_approval(aid, approve):
            await telegram.send("✅ Подтверждено" if approve else "🚫 Отклонено")
        else:
            await telegram.send("Нет ожидающих подтверждений")
        return
    if low.startswith("/task "):
        goal = text[6:].strip()
        tid = schedule_new(goal[:48], goal, source="telegram")
        await telegram.send(f"🚀 Задача принята в работу: <code>{tid}</code>")
        return
    if low.startswith("/status"):
        tasks = store.list_tasks(5)
        lines = [f"• {t['title']} — {t['status']}" for t in tasks] or ["пусто"]
        await telegram.send("<b>Задачи</b>\n" + "\n".join(lines))
        return
    if low in ("/start", "/help"):
        await telegram.send(
            "Я Джарвис. Команды:\n"
            "/task <цель> — фоновая задача\n"
            "/status — статус задач\n"
            "/yes, /no — подтвердить/отклонить действие\n"
            "Или просто напишите вопрос.")
        return
    # обычный вопрос
    chats = store.list_chats(1)
    chat_id = chats[0]["id"] if chats else store.create_chat("Телеграм")
    store.add_message(chat_id, "user", text)
    events.publish("telegram_in", text=text)
    try:
        answer = await agent.chat(chat_id, text)
    except Exception as e:
        answer = f"Ошибка: {e}"
    await telegram.send(answer[:3800])


def start() -> None:
    loop = asyncio.get_event_loop()
    _tasks.append(loop.create_task(worker_loop()))
    _tasks.append(loop.create_task(worker_loop()))   # два параллельных исполнителя
    _tasks.append(loop.create_task(scheduler_loop()))
    _tasks.append(loop.create_task(proactive_loop()))
    _tasks.append(loop.create_task(telegram_loop()))


async def stop() -> None:
    for t in _tasks:
        t.cancel()
    _tasks.clear()

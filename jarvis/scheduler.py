"""
Фоновая жизнь Джарвиса.

- Выполняет запланированные задачи (расписания).
- Проактивно просыпается по «сердцебиению» и решает, есть ли что-то полезное.
- Результаты кладёт в события и шлёт в Telegram.
"""
from __future__ import annotations

import asyncio
import time

from . import agent, llm, memory, tools
from .config import config

_running = False
_task: asyncio.Task | None = None
BUS_HOOK = None  # устанавливается сервером, чтобы транслировать события в UI


async def _emit(payload: dict) -> None:
    if BUS_HOOK:
        await BUS_HOOK(payload)


async def _run_scheduled(item: dict) -> None:
    memory.add_event("background", f"Фоновая задача: {item['title']}", item["prompt"])
    await _emit({"type": "background_started", "title": item["title"]})
    try:
        await agent.run_agent(
            item["prompt"], emit=_emit, session="background", background=True
        )
    except Exception as e:
        memory.add_event("error", f"Ошибка фоновой задачи: {item['title']}", str(e))


async def _proactive_check() -> None:
    """Джарвис сам думает, не нужно ли что-то сделать/напомнить."""
    if not config.get("background", "proactive", default=True):
        return
    if not config.has_llm:
        return
    facts = memory.recall(20)
    recent = memory.list_tasks(5)
    ctx = "\n".join(f"- {f['key']}: {f['value']}" for f in facts) or "пока ничего"
    tasks_ctx = "\n".join(f"- {t['title']} ({t['status']})" for t in recent) or "нет"
    try:
        res = await llm.complete(
            [{"role": "system",
              "content": "Ты — проактивный ассистент. По контексту реши, есть ли "
                         "СЕЙЧАС что-то по-настоящему полезное, о чём стоит "
                         "напомнить пользователю. Без навязчивости: в 90% случаев "
                         "правильный ответ — ничего не делать. Ответь JSON: "
                         "{\"act\": false} или {\"act\": true, \"message\": \"...\"}"},
             {"role": "user",
              "content": f"Время: {time.strftime('%Y-%m-%d %H:%M')}\n"
                         f"Известно о пользователе:\n{ctx}\n\n"
                         f"Последние задачи:\n{tasks_ctx}"}],
            model=config.get("models", "cheap"), temperature=0.4, max_tokens=300,
        )
        data = llm.extract_json(res.text) or {}
        if data.get("act") and data.get("message"):
            msg = str(data["message"])[:600]
            memory.add_event("proactive", "Джарвис предлагает", msg)
            await tools.send_telegram_raw(f"💡 {msg}", silent=True)
            await _emit({"type": "proactive", "text": msg})
    except Exception:
        pass


async def _loop() -> None:
    global _running
    _running = True
    last_proactive = 0.0
    while _running:
        try:
            now = time.time()
            for item in memory.list_schedules():
                if not item.get("enabled"):
                    continue
                if item.get("next_run", 0) and now >= item["next_run"]:
                    every = int(item.get("every_minutes") or 0)
                    if every:
                        memory.update_schedule(item["id"], next_run=now + every * 60)
                    else:
                        memory.update_schedule(item["id"], enabled=0)
                    asyncio.create_task(_run_scheduled(item))

            hb = int(config.get("background", "heartbeat_minutes", default=30)) * 60
            if hb and now - last_proactive > hb:
                last_proactive = now
                asyncio.create_task(_proactive_check())
        except Exception:
            pass
        await asyncio.sleep(20)


def start() -> None:
    global _task
    if not config.get("background", "enabled", default=True):
        return
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop())


def stop() -> None:
    global _running
    _running = False
    if _task:
        _task.cancel()

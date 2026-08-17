"""
Telegram-бот: уведомления и управление Джарвисом с телефона.

Работает через long polling — не нужен белый IP и вебхуки.
Включается автоматически, если в настройках указан bot_token.
"""
from __future__ import annotations

import asyncio

import httpx

from . import agent, memory
from .config import config

_task: asyncio.Task | None = None
_running = False


async def _api(method: str, **params) -> dict:
    token = config.get("telegram", "bot_token", default="")
    if not token:
        return {}
    async with httpx.AsyncClient(timeout=70) as c:
        r = await c.post(f"https://api.telegram.org/bot{token}/{method}", json=params)
        return r.json() if r.status_code < 400 else {}


async def _reply(chat_id: str | int, text: str) -> None:
    await _api("sendMessage", chat_id=chat_id, text=text[:4000],
               parse_mode="Markdown")


async def _handle(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if not text or not chat_id:
        return

    allowed = str(config.get("telegram", "chat_id", default="")).strip()
    if not allowed:
        # Первый написавший становится владельцем — удобно для настройки.
        config.set(chat_id, "telegram", "chat_id")
        await _reply(chat_id, "✅ Готово, Сэр. Я запомнил этот чат. "
                              "Теперь можете ставить мне задачи прямо отсюда.")
        return
    if chat_id != allowed:
        return

    if text.startswith("/start"):
        await _reply(chat_id, "Джарвис на связи. Просто напишите задачу — "
                              "я выполню её и отчитаюсь.\n\n"
                              "/tasks — активные задачи\n/status — состояние")
        return
    if text.startswith("/tasks"):
        rows = memory.list_tasks(8)
        body = "\n".join(f"• {t['title']} — _{t['status']}_" for t in rows) or "Пусто."
        await _reply(chat_id, "*Задачи:*\n" + body)
        return
    if text.startswith("/status"):
        await _reply(chat_id, "Работаю штатно. Фоновый режим активен.")
        return

    if not config.get("telegram", "allow_commands", default=True):
        return

    await _reply(chat_id, "Принял. Работаю…")
    memory.add_message("telegram", "user", text)

    async def emit(_payload: dict) -> None:
        return None

    try:
        res = await agent.run_agent(text, emit=emit, session="telegram",
                                    background=True)
        if not res.get("ok"):
            await _reply(chat_id, f"⚠️ Не получилось: {res.get('error')}")
    except Exception as e:
        await _reply(chat_id, f"⚠️ Ошибка: {e}")


async def _loop() -> None:
    global _running
    _running = True
    offset = 0
    while _running:
        try:
            data = await _api("getUpdates", offset=offset, timeout=50)
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                asyncio.create_task(_handle(upd))
        except Exception:
            await asyncio.sleep(5)


def start() -> None:
    global _task
    tg = config.get("telegram", default={}) or {}
    if not tg.get("enabled") or not tg.get("bot_token"):
        return
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop())


def stop() -> None:
    global _running
    _running = False
    if _task:
        _task.cancel()

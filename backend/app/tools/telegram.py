"""Телеграм: уведомления и приём команд с телефона."""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from .. import config

API = "https://api.telegram.org/bot{token}/{method}"


def enabled() -> bool:
    return bool(config.get("telegram_enabled") and config.get("telegram_bot_token")
                and config.get("telegram_chat_id"))


async def call(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    token = (config.get("telegram_bot_token") or "").strip()
    if not token:
        return {"ok": False, "error": "Не указан токен бота"}
    try:
        async with httpx.AsyncClient(timeout=40) as cl:
            r = await cl.post(API.format(token=token, method=method), json=payload)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def send(text: str, chat_id: str = "", silent: bool = False) -> Dict[str, Any]:
    """Отправить сообщение в телеграм."""
    cid = chat_id or (config.get("telegram_chat_id") or "").strip()
    if not cid:
        return {"ok": False, "error": "Не указан chat_id"}
    return await call("sendMessage", {
        "chat_id": cid, "text": text[:4000], "parse_mode": "HTML",
        "disable_notification": silent, "disable_web_page_preview": True})


async def send_document(path: str, caption: str = "", chat_id: str = "") -> Dict[str, Any]:
    token = (config.get("telegram_bot_token") or "").strip()
    cid = chat_id or (config.get("telegram_chat_id") or "").strip()
    if not (token and cid):
        return {"ok": False, "error": "Телеграм не настроен"}
    from .sandbox import root
    p = (root() / path.lstrip("/")).resolve()
    if not p.exists():
        return {"ok": False, "error": "Файл не найден"}
    try:
        async with httpx.AsyncClient(timeout=180) as cl:
            with open(p, "rb") as f:
                r = await cl.post(API.format(token=token, method="sendDocument"),
                                  data={"chat_id": cid, "caption": caption[:900]},
                                  files={"document": (p.name, f.read())})
        return r.json()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def get_updates(offset: Optional[int] = None, timeout: int = 25) -> Dict[str, Any]:
    token = (config.get("telegram_bot_token") or "").strip()
    if not token:
        return {"ok": False, "result": []}
    payload: Dict[str, Any] = {"timeout": timeout}
    if offset is not None:
        payload["offset"] = offset
    try:
        async with httpx.AsyncClient(timeout=timeout + 15) as cl:
            r = await cl.post(API.format(token=token, method="getUpdates"), json=payload)
        return r.json()
    except Exception:
        return {"ok": False, "result": []}


async def whoami() -> Dict[str, Any]:
    return await call("getMe", {})

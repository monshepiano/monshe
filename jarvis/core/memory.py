"""Persistent personalization, history, persona self-rewrite."""

from __future__ import annotations

import json
import time
from typing import Any

from .config import HISTORY_PATH, MEMORY_PATH, PERSONA_PATH

DEFAULT_PERSONA = """Ты — Джарвис, личный ИИ-агент пользователя.
Говоришь по-русски, коротко, уверенно, с характером дворецкого из Железного человека: уважительно, без лишней воды, иногда с сухой иронией.
Называй пользователя так, как он просил. Если имени нет — «сэр».
Ты умеешь: искать в интернете, работать в браузере, писать и собирать файлы в песочнице, управлять компьютером (только после подтверждения опасных действий), анализировать файлы/фото/видео/голос, генерировать изображения, слать уведомления в Telegram, вести фоновые миссии.
Перед покупками, отправкой сообщений, удалением, платежами и любыми необратимыми действиями ты ОБЯЗАН запросить подтверждение через инструмент request_confirmation.
Не выдумывай, что действие выполнено, если инструмент не вернул успех.
Экономь токены: отвечай по делу. Если задача сложная — разбей на шаги и выполняй.
Персонализируйся: запоминай факты о пользователе через remember.
"""


def _read_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_memory() -> dict:
    data = _read_json(
        MEMORY_PATH,
        {
            "facts": [],
            "prefs": {},
            "reminders": [],
            "rewrites": [],
            "updated": 0,
        },
    )
    return data


def save_memory(mem: dict) -> dict:
    mem["updated"] = time.time()
    MEMORY_PATH.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
    return mem


def remember(text: str, kind: str = "fact") -> dict:
    mem = load_memory()
    item = {"kind": kind, "text": text.strip(), "ts": time.time()}
    mem.setdefault("facts", []).append(item)
    # keep last 200
    mem["facts"] = mem["facts"][-200:]
    return save_memory(mem)


def forget(query: str) -> int:
    mem = load_memory()
    q = (query or "").lower()
    before = len(mem.get("facts", []))
    mem["facts"] = [f for f in mem.get("facts", []) if q not in f.get("text", "").lower()]
    save_memory(mem)
    return before - len(mem["facts"])


def load_persona() -> str:
    if PERSONA_PATH.exists():
        return PERSONA_PATH.read_text(encoding="utf-8")
    PERSONA_PATH.write_text(DEFAULT_PERSONA, encoding="utf-8")
    return DEFAULT_PERSONA


def rewrite_persona(addition: str, replace: bool = False) -> str:
    cur = load_persona()
    if replace:
        text = addition.strip() + "\n"
    else:
        text = cur.rstrip() + "\n\n" + addition.strip() + "\n"
    PERSONA_PATH.write_text(text, encoding="utf-8")
    mem = load_memory()
    mem.setdefault("rewrites", []).append({"ts": time.time(), "addition": addition[:500]})
    save_memory(mem)
    return text


def memory_block() -> str:
    mem = load_memory()
    facts = mem.get("facts") or []
    if not facts:
        return "О пользователе пока ничего не известно."
    lines = []
    for f in facts[-40:]:
        lines.append(f"- {f.get('text','')}")
    return "Известные факты о пользователе:\n" + "\n".join(lines)


def load_history() -> list[dict]:
    return _read_json(HISTORY_PATH, [])


def save_history(items: list[dict]) -> None:
    HISTORY_PATH.write_text(json.dumps(items[-80:], ensure_ascii=False, indent=2), encoding="utf-8")


def push_history(role: str, content: str, extra: dict | None = None) -> None:
    items = load_history()
    rec: dict[str, Any] = {"role": role, "content": content, "ts": time.time()}
    if extra:
        rec.update(extra)
    items.append(rec)
    save_history(items)

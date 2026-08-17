"""Agent loop: plan → tools → answer. Also a local fallback brain."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from . import memory, orchestrator, tools
from .config import load_settings
from .events import BUS
from .llm import LLMError

MAX_ROUNDS = 8


def _system_prompt(agent_mode: bool) -> str:
    s = load_settings()
    name = s.get("owner_name") or "сэр"
    persona = memory.load_persona()
    facts = memory.memory_block()
    mode = (
        "Режим АГЕНТ включён: разбей задачу на шаги, объявляй каждый шаг, "
        "вызывай инструменты, работай до результата. Не спрашивай очевидное."
        if agent_mode
        else "Обычный диалог. Инструменты используй, когда они реально нужны."
    )
    return (
        f"{persona}\n\nИмя пользователя: {name}\n{facts}\n\n{mode}\n"
        "Когда рисуешь картинку — после generate_image скажи путь.\n"
        "Когда работаешь с компьютером: сначала screenshot, потом действуй.\n"
        "Отвечай на русском, если пользователь не попросил иначе."
    )


def _parse_tool_calls(msg: dict) -> list[dict]:
    calls = msg.get("tool_calls") or []
    out = []
    for c in calls:
        fn = c.get("function") or {}
        name = fn.get("name") or c.get("name") or ""
        raw = fn.get("arguments") or c.get("arguments") or "{}"
        if isinstance(raw, dict):
            args = raw
        else:
            try:
                args = json.loads(raw) if raw else {}
            except Exception:
                args = {"_raw": raw}
        out.append({"id": c.get("id") or name, "name": name, "args": args})
    return out


def _fallback_plan(text: str, agent: bool) -> list[dict]:
    """If no LLM is reachable, still do useful work."""
    t = text.lower()
    steps = []
    if re.search(r"(найди|поиск|что такое|кто такой|курс|погода|новост)", t):
        steps.append(("web_search", {"query": text}))
    if re.search(r"(открой сайт|зайди на|прочитай http)", t) or text.startswith("http"):
        url = text if text.startswith("http") else ""
        m = re.search(r"https?://\S+", text)
        if m:
            url = m.group(0)
        if url:
            steps.append(("fetch_url", {"url": url}))
    if re.search(r"(нарисуй|сгенерируй картин|изобрази)", t):
        steps.append(("generate_image", {"prompt": text}))
    if re.search(r"(скрин|что на экране)", t):
        steps.append(("screenshot", {}))
    if re.search(r"(запомни|меня зовут|я люблю|я живу)", t):
        steps.append(("remember", {"text": text}))
    if re.search(r"(файлы|песочниц)", t):
        steps.append(("list_files", {}))
    if re.search(r"(время|который час|дата)", t):
        steps.append(("now_info", {}))
    if not steps and agent:
        steps.append(("web_search", {"query": text}))
    return [{"name": n, "args": a} for n, a in steps]


def _fallback_speak(user: str, observations: list[str]) -> str:
    s = load_settings()
    who = s.get("owner_name") or "сэр"
    t = (user or "").lower()
    if observations:
        body = "\n\n".join(observations)
        return (
            f"{who}, отработал. Сеть больших моделей сейчас молчит, "
            "поэтому взял локальный контур — результат ниже.\n\n"
            f"{body}"
        )
    if re.search(r"привет|здравств|добр|hey|hello|кто ты|что ты|умеешь|help|возможн", t):
        return (
            f"{who}, я Джарвис. Личный агент, не чат-бот.\n\n"
            "• Диалог — обычные вопросы, поиск, файлы\n"
            "• Агент — кнопка AGENT у поля ввода: дроблю задачу и делаю сам\n"
            "• Фон — галочка «в фон»: уходите, я доделаю и мигну в уведомлениях / Telegram\n"
            "• Песочница — пишу и собираю файлы, архив скачивается одной кнопкой\n"
            "• Computer-use — экран, клики, приложения. Покупка, сообщение, удаление — только после вашего «подтверждаю»\n"
            "• Камера и голос — картина на стене, «купи такую мне»\n"
            "• Картинки, разбор фото/видео/голоса, память под вас, самопереработка правил\n\n"
            "Модели: Cloud.ru Foundation Models (дешёвый GigaChat на мелочи, сильнее — на сложное). "
            "DeepSeek — запасной контур. VPN не нужен.\n\n"
            "Скажите задачу как есть. Например: «найди три чайника как на фото и открой Ozon»."
        )
    return (
        f"Слушаю, {who}. Повторите задачу чуть предметнее — или включите AGENT, "
        "и я сам разложу её на шаги."
    )


def run_dialog(
    user_text: str,
    *,
    agent: bool = False,
    attachments: list[dict] | None = None,
    history: list[dict] | None = None,
    background: bool = False,
    on_delta: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    attachments = attachments or []
    history = history or []
    mission_id = None
    if agent:
        mission_id = f"ms_{int(time.time())}"
        BUS.emit(
            "mission",
            mission_id=mission_id,
            title=user_text[:80],
            status="running",
            steps=[],
            background=background,
        )

    sys = _system_prompt(agent)
    messages: list[dict[str, Any]] = [{"role": "system", "content": sys}]
    for h in history[-16:]:
        if h.get("role") in {"user", "assistant"} and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"][:4000]})

    content: Any = user_text
    has_image = any(a.get("kind") == "image" for a in attachments)
    if attachments:
        blocks: list[dict] = [{"type": "text", "text": user_text}]
        notes = []
        for a in attachments:
            if a.get("kind") == "image" and a.get("data_url"):
                blocks.append({"type": "image_url", "image_url": {"url": a["data_url"]}})
            notes.append(f"вложение: {a.get('name')} ({a.get('kind')})")
            if a.get("text_preview"):
                notes.append(a["text_preview"][:3000])
        if any(b.get("type") == "image_url" for b in blocks):
            content = blocks
        else:
            content = user_text + "\n\n" + "\n".join(notes)
    messages.append({"role": "user", "content": content})

    steps_log: list[dict] = []
    final_text = ""
    picked = None
    used_llm = True

    try:
        for rnd in range(MAX_ROUNDS):
            BUS.emit("thought", text=f"Шаг рассуждения {rnd + 1}")
            out = orchestrator.run(
                {"text": user_text, "has_image": has_image, "agent": agent},
                messages,
                tools.SCHEMAS if (agent or rnd == 0) else tools.SCHEMAS,
                on_token=None,
            )
            picked = out.get("picked")
            calls = _parse_tool_calls(out)
            text = (out.get("text") or "").strip()
            if text:
                BUS.emit("thought", text=text[:240])
            if not calls:
                final_text = text or "Готово."
                if on_delta:
                    on_delta(final_text)
                break
            # append assistant tool call message
            messages.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": out.get("tool_calls") or [],
                }
            )
            for c in calls:
                BUS.emit(
                    "step",
                    mission_id=mission_id,
                    name=c["name"],
                    args=c["args"],
                    status="run",
                )
                result = tools.run_tool(c["name"], c["args"])
                steps_log.append({"name": c["name"], "args": c["args"], "result": result[:1000]})
                BUS.emit(
                    "step",
                    mission_id=mission_id,
                    name=c["name"],
                    status="done",
                    result=result[:500],
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": c["id"],
                        "name": c["name"],
                        "content": result[:6000],
                    }
                )
        else:
            final_text = final_text or "Достиг лимита шагов. Вот что успел сделать."
    except LLMError as e:
        used_llm = False
        BUS.emit("thought", text=f"Модели недоступны: {e}")
        plan = _fallback_plan(user_text, agent)
        observations = []
        for c in plan:
            BUS.emit("step", mission_id=mission_id, name=c["name"], status="run")
            result = tools.run_tool(c["name"], c["args"])
            steps_log.append({**c, "result": result[:1000]})
            observations.append(f"[{c['name']}]\n{result[:1500]}")
            BUS.emit("step", mission_id=mission_id, name=c["name"], status="done")
        # If user just greets / chats, speak fallback
        final_text = _fallback_speak(user_text, observations)
        if on_delta:
            on_delta(final_text)

    if mission_id:
        BUS.emit(
            "mission",
            mission_id=mission_id,
            title=user_text[:80],
            status="done",
            steps=steps_log,
            background=background,
        )
        if background:
            BUS.emit(
                "notify",
                level="ok",
                title="Миссия завершена",
                body=user_text[:120],
                auto_open=True,
            )
            # telegram ping if configured
            try:
                if load_settings().get("telegram_token"):
                    tools.telegram_send(f"Джарвис: миссия готова — {user_text[:80]}")
            except Exception:
                pass

    memory.push_history("user", user_text if isinstance(user_text, str) else str(user_text))
    memory.push_history("assistant", final_text, {"model": picked, "steps": len(steps_log)})
    return {
        "text": final_text,
        "steps": steps_log,
        "model": picked,
        "agent": agent,
        "fallback": not used_llm,
        "mission_id": mission_id,
    }

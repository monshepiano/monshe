"""
Ядро агента.

Джарвис работает так:
  1. Понимает задачу и решает — ответить сразу или запустить агентский режим.
  2. В агентском режиме составляет ПЛАН из шагов.
  3. Выполняет шаги циклом «мысль -> инструмент -> результат».
  4. Перед опасным действием останавливается и спрашивает подтверждение.
  5. Отдаёт итог и, если нужно, файлы + уведомление в Telegram.

Всё транслируется наружу событиями (для интерфейса: мысли, шаги, терминал).
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from . import llm, memory, tools
from .config import config, SANDBOX


# ---------------------------------------------------------------------------
# Системный промпт — личность и правила
# ---------------------------------------------------------------------------

def build_system_prompt(*, agentic: bool = False) -> str:
    persona = config.get("persona", default={}) or {}
    style = persona.get("style", "")
    user_name = persona.get("user_name", "Сэр")

    facts = memory.recall(40)
    facts_block = ""
    if facts:
        facts_block = "\n\nЧто ты знаешь о пользователе:\n" + "\n".join(
            f"- {f['key']}: {f['value']}" for f in facts
        )

    skills = tools.load_skills()
    skills_block = ""
    if skills:
        skills_block = "\n\nТвои сохранённые навыки:\n" + "\n".join(
            f"- {s['name']}: {s['description']}" for s in skills
        )

    base = f"""{style}
Обращение к пользователю: {user_name}.
Текущее время: {time.strftime('%Y-%m-%d %H:%M', time.localtime())}.
{facts_block}{skills_block}

Правила:
- Отвечай на русском, если пользователь не просит иначе.
- Не выдумывай факты. Если нужна свежая информация — ищи в интернете.
- Если узнал о пользователе что-то устойчиво важное — сохрани через remember.
- Форматируй ответы в Markdown: заголовки, списки, таблицы, блоки кода.
- Будь кратким там, где хватает пары фраз, и подробным там, где это важно."""

    if agentic:
        base += """

Ты в АГЕНТСКОМ режиме — работаешь самостоятельно, шаг за шагом:
- На каждом шаге либо вызывай инструмент, либо давай финальный ответ.
- Не спрашивай пользователя без крайней необходимости — действуй сам.
- Проверяй результаты: если инструмент вернул ошибку, придумай другой путь.
- Файлы для пользователя создавай через write_file в песочнице.
- Когда задача выполнена — дай развёрнутый итог с выводами и ссылками
  на созданные файлы. Начни финальный ответ словом ГОТОВО."""
    return base


# ---------------------------------------------------------------------------
# События для интерфейса
# ---------------------------------------------------------------------------

Emit = Callable[[dict], Any]


@dataclass
class Approval:
    id: str
    task_id: str
    tool: str
    args: dict
    created: float = field(default_factory=time.time)
    decision: str = ""          # approve | reject | always
    event: asyncio.Event = field(default_factory=asyncio.Event)


PENDING: dict[str, Approval] = {}
ALWAYS_ALLOW: set[str] = set()


def resolve_approval(approval_id: str, decision: str) -> bool:
    ap = PENDING.get(approval_id)
    if not ap:
        return False
    ap.decision = decision
    if decision == "always":
        ALWAYS_ALLOW.add(ap.tool)
    ap.event.set()
    return True


def describe_action(name: str, args: dict) -> str:
    """Человеческое описание того, что собирается сделать агент."""
    a = args or {}
    return {
        "run_shell": f"Выполнить в терминале: {a.get('command', '')}",
        "delete_file": f"Удалить файл: {a.get('path', '')}",
        "send_telegram": f"Отправить в Telegram: {str(a.get('text', ''))[:120]}",
        "http_request": f"{a.get('method', 'GET')} запрос к {a.get('url', '')}",
        "browser_act": f"Действие в браузере: {a.get('instruction', '')}",
        "self_edit": "Изменить собственный код",
    }.get(name, f"{name}({json.dumps(a, ensure_ascii=False)[:160]})")


# ---------------------------------------------------------------------------
# Классификация: нужен ли агентский режим
# ---------------------------------------------------------------------------

AGENT_TRIGGERS = (
    "найди", "собери", "сделай", "составь", "проверь", "изучи", "исследуй",
    "сравни", "напиши файл", "создай", "скачай", "посчитай", "проанализируй",
    "подготовь", "отчёт", "отчет", "презентац", "план ", "мониторь",
    "каждый день", "каждое утро", "напомни", "закажи", "купи", "отправь",
)


async def needs_agent(text: str, has_files: bool = False) -> bool:
    low = (text or "").lower()
    if any(t in low for t in AGENT_TRIGGERS):
        return True
    if len(low) > 400:
        return True
    return False


# ---------------------------------------------------------------------------
# Планировщик
# ---------------------------------------------------------------------------

async def make_plan(goal: str, context: str = "") -> list[dict]:
    prompt = f"""Разбей задачу пользователя на 2-6 конкретных выполнимых шагов.

Задача: {goal}
{context}

Доступные инструменты: {', '.join(tools.REGISTRY.keys())}

Ответь ТОЛЬКО JSON-массивом:
[{{"step": 1, "title": "краткое название шага", "detail": "что именно сделать"}}]"""

    try:
        res = await llm.complete(
            [{"role": "system", "content": "Ты — планировщик задач. Отвечаешь только JSON."},
             {"role": "user", "content": prompt}],
            mode="plan", temperature=0.3, max_tokens=1200,
        )
        data = llm.extract_json(res.text)
        # Модели часто оборачивают массив: {"steps": [...]} или {"plan": [...]}
        if isinstance(data, dict):
            for key in ("steps", "plan", "шаги", "items"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if isinstance(data, list) and data:
            return [
                {"step": i + 1,
                 "title": str(s.get("title", f"Шаг {i+1}"))[:120],
                 "detail": str(s.get("detail", ""))[:400],
                 "status": "pending"}
                for i, s in enumerate(data[:6]) if isinstance(s, dict)
            ]
    except Exception:
        pass
    return [{"step": 1, "title": "Выполнить задачу", "detail": goal, "status": "pending"}]


# ---------------------------------------------------------------------------
# Главный агентский цикл
# ---------------------------------------------------------------------------

async def run_agent(
    goal: str,
    *,
    emit: Emit,
    session: str = "default",
    task_id: str | None = None,
    background: bool = False,
    attachments: list[dict] | None = None,
    history: list[dict] | None = None,
) -> dict:
    """Выполняет задачу автономно. Все события идут через `emit`."""
    max_steps = int(config.get("safety", "max_steps", default=24))
    deadline = time.time() + int(config.get("safety", "max_seconds", default=900))

    if not task_id:
        title = goal[:80] + ("…" if len(goal) > 80 else "")
        task_id = memory.create_task(title, goal, session, background)

    await _emit(emit, {"type": "task_started", "task_id": task_id, "goal": goal})

    # 1. План
    await _emit(emit, {"type": "status", "text": "Составляю план…", "task_id": task_id})
    plan = await make_plan(goal)
    memory.update_task(task_id, plan=plan, status="running")
    memory.add_step(task_id, "thought", "План составлен", {"plan": plan})
    await _emit(emit, {"type": "plan", "task_id": task_id, "plan": plan})

    # 2. Диалог агента
    messages: list[dict] = [{"role": "system", "content": build_system_prompt(agentic=True)}]
    if history:
        messages.extend(history[-8:])

    plan_text = "\n".join(f"{s['step']}. {s['title']} — {s['detail']}" for s in plan)
    user_content: Any = f"""Задача: {goal}

Твой план:
{plan_text}

Выполняй план шаг за шагом, используя инструменты. Когда всё сделано —
дай финальный ответ, начав со слова ГОТОВО."""

    if attachments:
        parts: list[dict] = [{"type": "text", "text": user_content}]
        has_images = False
        for att in attachments:
            if att.get("kind") == "image":
                parts.append({"type": "image_url",
                              "image_url": {"url": att["data_url"]}})
                has_images = True
            else:
                parts.append({"type": "text",
                              "text": f"\n\n--- Файл {att['name']} ---\n{att['text'][:12000]}"})
        user_content = parts if has_images else "\n".join(
            p["text"] for p in parts if p["type"] == "text")

    messages.append({"role": "user", "content": user_content})

    tool_schemas = tools.schemas()
    final_text = ""
    steps_done = 0

    while steps_done < max_steps and time.time() < deadline:
        steps_done += 1
        await _emit(emit, {"type": "thinking", "task_id": task_id,
                           "step": steps_done})
        try:
            res = await llm.complete(
                messages, mode="agent", tools=tool_schemas,
                temperature=0.4, max_tokens=3000,
                has_images=isinstance(user_content, list),
            )
        except llm.LLMError as e:
            memory.add_step(task_id, "error", str(e))
            await _emit(emit, {"type": "error", "task_id": task_id, "text": str(e)})
            memory.update_task(task_id, status="failed", result=str(e))
            return {"ok": False, "error": str(e), "task_id": task_id}

        await _emit(emit, {"type": "model", "task_id": task_id,
                           "model": res.model, "reason": res.reason,
                           "provider": res.provider})

        # Финальный ответ без инструментов
        if not res.tool_calls:
            final_text = res.text
            if res.text:
                memory.add_step(task_id, "thought", "Ответ модели",
                                {"text": res.text[:2000]})
            break

        messages.append({
            "role": "assistant",
            "content": res.text or "",
            "tool_calls": res.tool_calls,
        })
        if res.text:
            await _emit(emit, {"type": "thought", "task_id": task_id,
                               "text": res.text[:1200]})

        for call in res.tool_calls:
            fn = call.get("function", {}) or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}

            human = describe_action(name, args)
            await _emit(emit, {"type": "tool_start", "task_id": task_id,
                               "tool": name, "args": args, "human": human})
            memory.add_step(task_id, "tool", human, {"tool": name, "args": args})

            # --- Подтверждение опасных действий ---
            need_confirm = tools.is_dangerous(name) and name not in ALWAYS_ALLOW
            if background and config.get("safety", "auto_approve_in_background",
                                         default=False):
                need_confirm = False

            if need_confirm:
                ap = Approval(uuid.uuid4().hex[:10], task_id, name, args)
                PENDING[ap.id] = ap
                memory.update_task(task_id, status="waiting_approval")
                await _emit(emit, {
                    "type": "approval_request", "task_id": task_id,
                    "approval_id": ap.id, "tool": name, "args": args,
                    "human": human,
                })
                if background:
                    await tools.send_telegram_raw(
                        f"⚠️ Джарвис просит подтверждение:\n\n{human}\n\n"
                        f"Открой интерфейс, чтобы разрешить."
                    )
                try:
                    await asyncio.wait_for(ap.event.wait(), timeout=1800)
                except asyncio.TimeoutError:
                    ap.decision = "reject"
                PENDING.pop(ap.id, None)
                memory.update_task(task_id, status="running")

                if ap.decision == "reject":
                    result = {"ok": False,
                              "error": "Пользователь отклонил это действие. "
                                       "Найди другой способ или заверши задачу."}
                    await _emit(emit, {"type": "tool_result", "task_id": task_id,
                                       "tool": name, "result": result,
                                       "rejected": True})
                    messages.append({
                        "role": "tool", "tool_call_id": call.get("id", ""),
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                    continue
                await _emit(emit, {"type": "approval_granted",
                                   "task_id": task_id, "tool": name})

            # --- Выполнение ---
            started = time.time()
            result = await tools.execute(name, args)
            elapsed = round(time.time() - started, 2)

            memory.add_step(task_id, "result", f"{name} → {'ok' if result.get('ok') else 'ошибка'}",
                            {"tool": name, "result": _trim(result)})
            await _emit(emit, {"type": "tool_result", "task_id": task_id,
                               "tool": name, "result": _trim(result),
                               "elapsed": elapsed})

            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": json.dumps(_trim(result), ensure_ascii=False)[:8000],
            })

        # Отмечаем прогресс плана (грубо: по числу выполненных инструментов)
        done_idx = min(steps_done - 1, len(plan) - 1)
        for i, s in enumerate(plan):
            s["status"] = "done" if i < done_idx else ("active" if i == done_idx else "pending")
        memory.update_task(task_id, plan=plan)
        await _emit(emit, {"type": "plan", "task_id": task_id, "plan": plan})

    if not final_text:
        final_text = ("Достиг лимита шагов. Вот что удалось сделать — "
                      "загляни в журнал задачи.")

    for s in plan:
        s["status"] = "done"
    memory.update_task(task_id, status="done", result=final_text, plan=plan)
    memory.add_step(task_id, "result", "Задача завершена", {"text": final_text[:2000]})

    files = _new_files()
    await _emit(emit, {"type": "task_done", "task_id": task_id,
                       "text": final_text, "files": files, "plan": plan})

    if background and config.get("telegram", "notify_on_task_done", default=True):
        await tools.send_telegram_raw(f"✅ *{goal[:60]}*\n\n{final_text[:3000]}")
    memory.add_event("task_done", goal[:80], final_text[:500])

    return {"ok": True, "task_id": task_id, "text": final_text, "files": files}


def _trim(obj: Any, limit: int = 6000) -> Any:
    """Обрезаем гигантские результаты, чтобы не раздувать контекст."""
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) <= limit:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and len(v) > 2500:
                out[k] = v[:2500] + " …[обрезано]"
            else:
                out[k] = v
        return out
    return s[:limit]


def _new_files(seconds: int = 3600) -> list[dict]:
    if not SANDBOX.exists():
        return []
    cutoff = time.time() - seconds
    out = []
    for p in sorted(SANDBOX.rglob("*")):
        if p.is_file() and p.stat().st_mtime > cutoff and not p.name.startswith("_run_"):
            rel = str(p.relative_to(SANDBOX))
            out.append({"path": rel, "size": p.stat().st_size,
                        "url": f"/api/files/download/{rel}"})
    return out[-12:]


async def _emit(emit: Emit, payload: dict) -> None:
    payload.setdefault("ts", time.time())
    res = emit(payload)
    if asyncio.iscoroutine(res):
        await res


# ---------------------------------------------------------------------------
# Быстрый чат (без агента) — со стримингом
# ---------------------------------------------------------------------------

async def chat_stream(
    text: str,
    *,
    session: str = "default",
    attachments: list[dict] | None = None,
) -> AsyncIterator[dict]:
    msgs: list[dict] = [{"role": "system", "content": build_system_prompt()}]
    for m in memory.history(session, 20):
        if m["role"] in ("user", "assistant") and m["content"]:
            msgs.append({"role": m["role"], "content": m["content"]})

    content: Any = text
    has_images = False
    if attachments:
        parts: list[dict] = [{"type": "text", "text": text}]
        for att in attachments:
            if att.get("kind") == "image":
                parts.append({"type": "image_url",
                              "image_url": {"url": att["data_url"]}})
                has_images = True
            else:
                parts.append({"type": "text",
                              "text": f"\n\n--- Файл {att['name']} ---\n{att['text'][:12000]}"})
        content = parts if has_images else "\n".join(
            p["text"] for p in parts if p.get("type") == "text")

    msgs.append({"role": "user", "content": content})

    buf = []
    async for chunk in llm.stream(msgs, mode="chat", has_images=has_images):
        if chunk["type"] == "delta":
            buf.append(chunk["text"])
        yield chunk

    answer = "".join(buf)
    if answer:
        memory.add_message(session, "assistant", answer)
        asyncio.create_task(_maybe_learn(text, answer))


async def _maybe_learn(user_text: str, answer: str) -> None:
    """Тихо извлекает устойчивые факты о пользователе — персонализация."""
    if len(user_text) < 25:
        return
    try:
        res = await llm.complete(
            [{"role": "system",
              "content": "Извлеки из сообщения пользователя устойчивые факты о нём "
                         "(имя, город, профессия, предпочтения, техника, привычки). "
                         "Только то, что пригодится надолго. Ответь JSON-объектом "
                         "{\"ключ\": \"значение\"} или {} если фактов нет."},
             {"role": "user", "content": user_text[:1500]}],
            model=config.get("models", "cheap"), temperature=0.1, max_tokens=300,
        )
        data = llm.extract_json(res.text)
        if isinstance(data, dict):
            for k, v in list(data.items())[:5]:
                if k and v and len(str(v)) < 200:
                    memory.remember(str(k), str(v), "auto")
    except Exception:
        pass

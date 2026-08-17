"""Ядро агента: диалог с инструментами + автономная работа по шагам."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

from . import config, events, router, store
from .tools import registry, telegram

MAX_TOOL_ROUNDS = 12

_pending_events: Dict[str, asyncio.Event] = {}
_pending_results: Dict[str, str] = {}


# ------------------------------------------------------------------ промпты
def system_prompt(mode: str = "chat") -> str:
    cfg = config.load()
    mem = store.recall(limit=30)
    mem_txt = "\n".join(f"- {m['kind']}/{m['key']}: {m['value']}" for m in mem) or "- пока пусто"
    name = cfg.get("user_name") or "хозяин"
    base = f"""{cfg.get('persona')}

Ты — Джарвис, персональный ИИ-агент {name}. Ты работаешь в собственной песочнице на сервере:
у тебя есть интернет, браузер, файловая система, генерация изображений, зрение и телеграм.

ЧТО ТЫ ЗНАЕШЬ О ХОЗЯИНЕ:
{mem_txt}

ПРАВИЛА:
1. Отвечай по-русски, живо и по делу. Без канцелярита и лишних извинений.
2. Нужны свежие факты — сначала web_search, потом ответ. Никогда не выдумывай цены, даты, новости.
3. Заметил новый устойчивый факт о хозяине — вызови remember.
4. Опасные действия (покупки, удаление, отправка данных, команды) выполняй только после
   подтверждения — система сама спросит, просто вызывай инструмент.
5. Файлы, отчёты, картинки складывай в песочницу и упоминай имя файла — хозяин их скачает.
6. Не выдумывай результат вызова инструмента — используй только то, что реально вернулось.
"""
    if mode == "planner":
        base += """
СЕЙЧАС ТЫ ПЛАНИРОВЩИК. Разбей цель на 2-6 конкретных выполнимых шагов.
Ответь ТОЛЬКО JSON-массивом строк, без пояснений. Пример:
["Найти в интернете актуальные цены", "Сравнить три варианта", "Собрать отчёт в файл"]
"""
    elif mode == "worker":
        base += """
СЕЙЧАС ТЫ ИСПОЛНИТЕЛЬ ОДНОГО ШАГА. Выполни шаг инструментами и кратко отчитайся,
что сделано и какой получен результат. Без воды.
"""
    return base


# ------------------------------------------------------------------ подтверждения
async def request_approval(tool: str, args: Dict[str, Any], reason: str,
                           task_id: str = "", timeout: float = 900) -> bool:
    aid = store.create_approval(task_id, tool, args, reason)
    ev = asyncio.Event()
    _pending_events[aid] = ev
    events.publish("approval_request", approval_id=aid, tool=tool, args=args,
                   reason=reason, task_id=task_id)
    n = store.add_notification("Нужно подтверждение",
                               f"{tool}: {reason}", "warn",
                               {"type": "approval", "id": aid})
    events.publish("notification", **n)
    if telegram.enabled():
        await telegram.send(
            f"⚠️ <b>Нужно подтверждение</b>\nДействие: <code>{tool}</code>\n"
            f"{reason}\n\n<code>{json.dumps(args, ensure_ascii=False)[:500]}</code>\n\n"
            f"Ответьте: /yes {aid} или /no {aid}")
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        store.decide_approval(aid, "timeout")
        return False
    finally:
        _pending_events.pop(aid, None)
    return _pending_results.pop(aid, "denied") == "approved"


def resolve_approval(approval_id: str, approved: bool) -> bool:
    a = store.get_approval(approval_id)
    if not a or a["status"] != "pending":
        return False
    store.decide_approval(approval_id, "approved" if approved else "denied")
    _pending_results[approval_id] = "approved" if approved else "denied"
    ev = _pending_events.get(approval_id)
    if ev:
        ev.set()
    events.publish("approval_resolved", approval_id=approval_id, approved=approved)
    return True


# ------------------------------------------------------------------ цикл инструментов
async def run_tools_loop(messages: List[Dict[str, Any]], *, task_id: str = "",
                         tier: str = "", max_rounds: int = MAX_TOOL_ROUNDS) -> str:
    """Диалог с моделью, пока она вызывает инструменты. Возвращает финальный текст."""
    tools = registry.tool_specs()
    final_text = ""
    for _ in range(max_rounds):
        events.publish("thinking", task_id=task_id)
        res = await router.complete(messages, tools=tools, tier=tier, task_id=task_id)
        if res.text:
            final_text = res.text
        if not res.tool_calls:
            break
        for call in res.tool_calls:
            args = call.arguments if isinstance(call.arguments, dict) else {}
            events.publish("tool_start", tool=call.name, args=args, task_id=task_id)
            reason = registry.is_dangerous(call.name, args)
            if reason:
                ok = await request_approval(call.name, args, reason, task_id)
                if not ok:
                    out = {"ok": False, "error": "Пользователь отклонил действие"}
                    events.publish("tool_end", tool=call.name, result=out, task_id=task_id)
                    messages.append({"role": "assistant", "content":
                                     f"Вызов {call.name}({json.dumps(args, ensure_ascii=False)[:300]})"})
                    messages.append({"role": "tool", "name": call.name,
                                     "tool_call_id": call.id,
                                     "content": json.dumps(out, ensure_ascii=False)})
                    continue
            try:
                out = await registry.execute(call.name, args, task_id=task_id)
            except Exception as e:
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            events.publish("tool_end", tool=call.name, result=_trim(out), task_id=task_id)
            messages.append({"role": "assistant",
                             "content": f"Вызов {call.name}({json.dumps(args, ensure_ascii=False)[:300]})"})
            messages.append({"role": "tool", "name": call.name, "tool_call_id": call.id,
                             "content": json.dumps(out, ensure_ascii=False)[:6000]})
    return final_text or "Готово."


def _trim(obj: Any, limit: int = 1200) -> Any:
    s = json.dumps(obj, ensure_ascii=False)
    if len(s) <= limit:
        return obj
    return {"preview": s[:limit] + "…"}


# ------------------------------------------------------------------ чат
async def chat(chat_id: str, user_text: str, attachments: Optional[List[str]] = None) -> str:
    history = store.get_messages(chat_id, limit=24)
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt("chat")}]
    for m in history:
        if m["role"] in ("user", "assistant"):
            messages.append({"role": m["role"], "content": m["content"]})
    text = user_text
    if attachments:
        text += "\n\nПрикреплённые файлы (в песочнице): " + ", ".join(attachments)
        text += "\nДля изображений используй инструмент look."
    messages.append({"role": "user", "content": text})

    events.publish("chat_start", chat_id=chat_id)
    answer = await run_tools_loop(messages, tier="")
    store.add_message(chat_id, "assistant", answer)
    events.publish("chat_end", chat_id=chat_id, text=answer)

    # авто-название диалога
    ch = [c for c in store.list_chats(200) if c["id"] == chat_id]
    if ch and ch[0]["title"] in ("Новый диалог", ""):
        store.rename_chat(chat_id, user_text.strip()[:48] or "Диалог")
    return answer


# ------------------------------------------------------------------ автономная задача
async def plan(goal: str) -> List[str]:
    messages = [{"role": "system", "content": system_prompt("planner")},
                {"role": "user", "content": f"Цель: {goal}"}]
    res = await router.complete(messages, tier="medium", temperature=0.2, max_tokens=700)
    steps = _parse_steps(res.text)
    return steps or [goal]


def _parse_steps(text: str) -> List[str]:
    m = re.search(r"\[.*\]", text or "", re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            return [str(s).strip() for s in arr if str(s).strip()][:8]
        except Exception:
            pass
    lines = [re.sub(r"^\s*[-*\d.)]+\s*", "", l).strip()
             for l in (text or "").splitlines() if l.strip()]
    return [l for l in lines if len(l) > 4][:8]


async def run_task(task_id: str) -> None:
    task = store.get_task(task_id)
    if not task:
        return
    store.update_task(task_id, status="running")
    events.publish("task_start", task_id=task_id, title=task["title"])
    try:
        steps = await plan(task["goal"])
        store.update_task(task_id, plan=json.dumps(steps, ensure_ascii=False))
        for old in store.list_steps(task_id):
            store.update_step(old["id"], status="stale")
        step_ids = [store.add_step(task_id, i, s) for i, s in enumerate(steps)]
        events.publish("task_plan", task_id=task_id, steps=steps)

        transcript: List[str] = []
        for i, (sid, title) in enumerate(zip(step_ids, steps)):
            store.update_step(sid, status="running")
            events.publish("step_start", task_id=task_id, step_id=sid, idx=i, title=title)
            messages = [
                {"role": "system", "content": system_prompt("worker")},
                {"role": "user", "content":
                    f"Общая цель: {task['goal']}\n"
                    f"План: {json.dumps(steps, ensure_ascii=False)}\n"
                    f"Что уже сделано:\n" + ("\n".join(transcript) or "— ничего") +
                    f"\n\nВыполни шаг {i + 1}: {title}"},
            ]
            out = await run_tools_loop(messages, task_id=task_id, tier="medium", max_rounds=8)
            transcript.append(f"Шаг {i + 1} ({title}): {out[:800]}")
            store.update_step(sid, status="done", output=out[:4000])
            events.publish("step_end", task_id=task_id, step_id=sid, idx=i, output=out[:1500])

        summary_msgs = [
            {"role": "system", "content": system_prompt("chat")},
            {"role": "user", "content":
                f"Задача «{task['title']}» выполнена. Вот ход работы:\n" +
                "\n".join(transcript) +
                "\n\nНапиши краткий итог для хозяина: что сделано, что получилось, "
                "какие файлы созданы. 3-8 предложений."},
        ]
        summary = await router.complete(summary_msgs, tier="medium", max_tokens=900)
        result = summary.text or "\n".join(transcript)
        store.update_task(task_id, status="done", result=result)
        events.publish("task_done", task_id=task_id, result=result)

        n = store.add_notification(f"Задача выполнена: {task['title']}", result[:400], "success",
                                   {"type": "task", "id": task_id})
        events.publish("notification", **n)
        if telegram.enabled():
            await telegram.send(f"✅ <b>{task['title']}</b>\n\n{result[:3000]}")
        if task.get("chat_id"):
            store.add_message(task["chat_id"], "assistant",
                              f"[Фоновая задача «{task['title']}» завершена]\n\n{result}")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        store.update_task(task_id, status="failed", result=err)
        events.publish("task_failed", task_id=task_id, error=err)
        n = store.add_notification(f"Задача не удалась: {task['title']}", err[:300], "alert",
                                   {"type": "task", "id": task_id})
        events.publish("notification", **n)
        if telegram.enabled():
            await telegram.send(f"❌ <b>{task['title']}</b>\n{err[:600]}")

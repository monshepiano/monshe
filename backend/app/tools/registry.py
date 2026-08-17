"""Реестр инструментов агента + слой безопасности (подтверждения)."""
from __future__ import annotations

import base64
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List

from .. import config, events, store
from . import browser, sandbox, telegram, web

# Инструменты, которые считаются опасными и требуют подтверждения пользователя.
DANGEROUS = {
    "shell", "run_python", "delete_file", "browser_act", "send_telegram",
    "send_file_telegram", "self_edit",
}
# Ключевые слова, при которых даже «безопасный» инструмент требует подтверждения.
DANGER_PATTERNS = re.compile(
    r"(rm\s+-rf|mkfs|shutdown|reboot|:\(\)\{|dd\s+if=|curl[^|]*\|\s*(ba)?sh|"
    r"оплат|купить|заказ|перевод денег|payment|checkout|password|пароль)", re.I)


def tool_specs() -> List[Dict[str, Any]]:
    """Описание инструментов для модели (JSON Schema)."""
    return [
        {
            "name": "web_search",
            "description": "Поиск в интернете. Используй, когда нужны свежие факты, цены, новости.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "поисковый запрос"},
                "limit": {"type": "integer", "description": "сколько результатов, по умолчанию 6"},
            }, "required": ["query"]},
        },
        {
            "name": "open_page",
            "description": "Открыть страницу по ссылке и прочитать её текст.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string"},
            }, "required": ["url"]},
        },
        {
            "name": "browser_act",
            "description": ("Реальный браузер в песочнице: действия open/click/type/press/read/"
                            "screenshot. Нужен для сайтов с авторизацией и кнопками."),
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["open", "click", "type", "press",
                                                       "read", "scroll", "screenshot"]},
                "url": {"type": "string"}, "selector": {"type": "string"},
                "text": {"type": "string"},
            }, "required": ["action"]},
        },
        {
            "name": "write_file",
            "description": "Создать/перезаписать файл в песочнице.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "content": {"type": "string"},
            }, "required": ["path", "content"]},
        },
        {
            "name": "read_file",
            "description": "Прочитать файл из песочницы.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                           "required": ["path"]},
        },
        {
            "name": "list_files",
            "description": "Список файлов в песочнице.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
        {
            "name": "delete_file",
            "description": "Удалить файл или папку в песочнице.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                           "required": ["path"]},
        },
        {
            "name": "make_zip",
            "description": "Упаковать файлы песочницы в zip-архив.",
            "parameters": {"type": "object", "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
                "name": {"type": "string"}}, "required": ["paths"]},
        },
        {
            "name": "shell",
            "description": "Выполнить команду в песочнице (bash). Требует подтверждения.",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string"}}, "required": ["command"]},
        },
        {
            "name": "run_python",
            "description": "Выполнить python-код в песочнице (расчёты, обработка данных).",
            "parameters": {"type": "object", "properties": {
                "code": {"type": "string"}}, "required": ["code"]},
        },
        {
            "name": "generate_image",
            "description": "Сгенерировать изображение по описанию и сохранить в песочницу.",
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string"}, "name": {"type": "string"}},
                "required": ["prompt"]},
        },
        {
            "name": "look",
            "description": ("Посмотреть на изображение/кадр с камеры и описать, что там. "
                            "path — файл из песочницы или загруженный пользователем."),
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "question": {"type": "string"}},
                "required": ["path"]},
        },
        {
            "name": "send_telegram",
            "description": "Отправить сообщение в телеграм пользователю.",
            "parameters": {"type": "object", "properties": {
                "text": {"type": "string"}}, "required": ["text"]},
        },
        {
            "name": "send_file_telegram",
            "description": "Отправить файл из песочницы в телеграм.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "caption": {"type": "string"}},
                "required": ["path"]},
        },
        {
            "name": "remember",
            "description": ("Запомнить факт о пользователе или его предпочтение "
                            "(имя, город, привычки, стиль общения)."),
            "parameters": {"type": "object", "properties": {
                "key": {"type": "string"}, "value": {"type": "string"},
                "kind": {"type": "string", "enum": ["profile", "preference", "fact", "skill"]}},
                "required": ["key", "value"]},
        },
        {
            "name": "recall",
            "description": "Вспомнить, что известно о пользователе.",
            "parameters": {"type": "object", "properties": {"kind": {"type": "string"}}},
        },
        {
            "name": "schedule_task",
            "description": ("Поставить задачу на фон/расписание. schedule: 'once', 'hourly', "
                            "'daily', 'every:15m'. Джарвис выполнит её сам и пришлёт результат."),
            "parameters": {"type": "object", "properties": {
                "title": {"type": "string"}, "goal": {"type": "string"},
                "schedule": {"type": "string"}, "delay_minutes": {"type": "integer"}},
                "required": ["title", "goal"]},
        },
        {
            "name": "notify",
            "description": "Показать уведомление в интерфейсе (и в телеграме).",
            "parameters": {"type": "object", "properties": {
                "title": {"type": "string"}, "body": {"type": "string"},
                "level": {"type": "string", "enum": ["info", "success", "warn", "alert"]}},
                "required": ["title"]},
        },
        {
            "name": "now",
            "description": "Текущие дата и время.",
            "parameters": {"type": "object", "properties": {}},
        },
    ]


def is_dangerous(name: str, args: Dict[str, Any]) -> str:
    """Возвращает причину, если действие требует подтверждения, иначе пустую строку."""
    if not config.get("confirm_dangerous"):
        return ""
    blob = json.dumps(args, ensure_ascii=False)
    if name in DANGEROUS:
        if name == "browser_act" and args.get("action") in ("open", "read", "screenshot", "scroll"):
            if not DANGER_PATTERNS.search(blob):
                return ""
        if name == "run_python" and not DANGER_PATTERNS.search(blob):
            return ""  # обычные расчёты безопасны
        return f"Действие «{name}» может повлиять на систему или отправить данные наружу"
    if DANGER_PATTERNS.search(blob):
        return "В параметрах обнаружены чувствительные операции (оплата/удаление/пароли)"
    return ""


async def execute(name: str, args: Dict[str, Any], *, task_id: str = "") -> Dict[str, Any]:
    """Выполнить инструмент. Проверка подтверждений — уровнем выше (agent.py)."""
    from .. import router  # локальный импорт, чтобы избежать цикла

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {"query": args}
    args = args or {}

    if name == "web_search":
        return await web.search(str(args.get("query", "")), int(args.get("limit", 6) or 6))
    if name == "open_page":
        return await web.fetch(str(args.get("url", "")))
    if name == "browser_act":
        return await browser.act(str(args.get("action", "open")), str(args.get("url", "")),
                                 str(args.get("selector", "")), str(args.get("text", "")))
    if name == "write_file":
        return sandbox.write_file(str(args.get("path", "note.txt")), str(args.get("content", "")))
    if name == "read_file":
        return sandbox.read_file(str(args.get("path", "")))
    if name == "list_files":
        return sandbox.list_files(str(args.get("path", ".")))
    if name == "delete_file":
        return sandbox.delete_file(str(args.get("path", "")))
    if name == "make_zip":
        return sandbox.make_zip(list(args.get("paths") or []), str(args.get("name", "bundle.zip")))
    if name == "shell":
        return await sandbox.run(str(args.get("command", "")))
    if name == "run_python":
        return await sandbox.run_python(str(args.get("code", "")))
    if name == "generate_image":
        giga = router.gigachat()
        try:
            data = await giga.generate_image(str(args.get("prompt", "")))
        except Exception as e:
            return {"ok": False, "error": f"Генерация изображения недоступна: {e}"}
        fname = str(args.get("name") or "image.jpg")
        if not fname.lower().endswith((".jpg", ".jpeg", ".png")):
            fname += ".jpg"
        p = sandbox.root() / fname
        p.write_bytes(data)
        events.publish("artifact", path=fname, media="image", task_id=task_id)
        return {"ok": True, "path": fname, "note": "Изображение сохранено в песочнице"}
    if name == "look":
        giga = router.gigachat()
        path = str(args.get("path", ""))
        cand = sandbox.root() / path.lstrip("/")
        if not cand.exists():
            cand = config.UPLOADS / Path(path).name
        if not cand.exists():
            return {"ok": False, "error": "Файл не найден"}
        try:
            answer = await giga.vision(str(args.get("question") or "Что изображено? Опиши подробно."),
                                       str(cand))
            return {"ok": True, "description": answer}
        except Exception as e:
            return {"ok": False, "error": f"Распознавание недоступно: {e}"}
    if name == "send_telegram":
        res = await telegram.send(str(args.get("text", "")))
        return {"ok": bool(res.get("ok")), "result": str(res)[:300]}
    if name == "send_file_telegram":
        res = await telegram.send_document(str(args.get("path", "")), str(args.get("caption", "")))
        return {"ok": bool(res.get("ok")), "result": str(res)[:300]}
    if name == "remember":
        store.remember(str(args.get("kind", "fact")), str(args.get("key", "")),
                       str(args.get("value", "")))
        events.publish("memory", key=args.get("key"), value=args.get("value"))
        return {"ok": True, "remembered": args.get("key")}
    if name == "recall":
        return {"ok": True, "memory": store.recall(str(args.get("kind", "")))}
    if name == "schedule_task":
        from ..scheduler import schedule_new
        tid = schedule_new(str(args.get("title", "Задача")), str(args.get("goal", "")),
                           str(args.get("schedule", "once")),
                           int(args.get("delay_minutes", 0) or 0))
        return {"ok": True, "task_id": tid, "note": "Задача поставлена в фон"}
    if name == "notify":
        n = store.add_notification(str(args.get("title", "")), str(args.get("body", "")),
                                   str(args.get("level", "info")))
        events.publish("notification", **n)
        if telegram.enabled():
            await telegram.send(f"<b>{n['title']}</b>\n{n['body']}")
        return {"ok": True}
    if name == "now":
        now = dt.datetime.now()
        return {"ok": True, "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                "weekday": now.strftime("%A")}
    return {"ok": False, "error": f"Неизвестный инструмент: {name}"}

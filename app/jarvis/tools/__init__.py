"""Реестр инструментов JARVIS: схемы для function-calling + диспетчер вызовов."""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

from . import media, system, web

# risk: safe | caution | danger
#   safe    — выполняется сразу
#   caution — выполняется сразу, но пишется в ленту (или спрашивает, если включено)
#   danger  — всегда требует подтверждения (оплата, удаление, отправка сообщений, computer-use)

TOOLS: Dict[str, Dict[str, Any]] = {}


def register(name: str, fn: Callable, description: str, params: Dict[str, Any],
             risk: str = "safe", group: str = "base", label: str = "") -> None:
    TOOLS[name] = {
        "fn": fn,
        "risk": risk,
        "group": group,
        "label": label or name,
        "schema": {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": params,
                               "required": [k for k, v in params.items() if v.pop("_required", False)]},
            },
        },
    }


def S(desc: str, required: bool = False, enum: List[str] | None = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": "string", "description": desc}
    if enum:
        out["enum"] = enum
    if required:
        out["_required"] = True
    return out


def I(desc: str, required: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": "integer", "description": desc}
    if required:
        out["_required"] = True
    return out


def B(desc: str) -> Dict[str, Any]:
    return {"type": "boolean", "description": desc}


# ----------------------------------------------------------------- ИНТЕРНЕТ
register("web_search", web.web_search,
         "Поиск в интернете. Используй всегда, когда нужны свежие факты, цены, новости, ссылки.",
         {"query": S("поисковый запрос", True), "count": I("сколько результатов, по умолчанию 6")},
         "safe", "web", "Поиск в интернете")

register("open_url", web.open_url,
         "Открыть веб-страницу и прочитать её текст. Это браузер агента на сервере.",
         {"url": S("адрес страницы", True)}, "safe", "web", "Открыть страницу")

register("deep_research", web.deep_research,
         "Глубокое исследование: поиск + чтение нескольких источников. Для сложных вопросов.",
         {"query": S("тема исследования", True), "pages": I("сколько страниц прочитать (1-5)")},
         "safe", "web", "Исследование")

register("download_file", web.download_file,
         "Скачать файл по ссылке в песочницу.",
         {"url": S("ссылка на файл", True), "filename": S("имя файла")},
         "caution", "web", "Скачивание")

register("http_request", web.http_request,
         "Произвольный HTTP-запрос к API (GET/POST).",
         {"url": S("адрес", True), "method": S("метод", False, ["GET", "POST", "PUT", "DELETE"]),
          "body": S("тело запроса"), "headers_json": S("заголовки в JSON")},
         "caution", "web", "HTTP-запрос")

# ---------------------------------------------------------------- ПЕСОЧНИЦА
register("write_file", system.write_file,
         "Создать/перезаписать файл в песочнице (код, документ, отчёт, csv).",
         {"path": S("имя файла, например report.md", True), "content": S("содержимое", True)},
         "safe", "sandbox", "Запись файла")

register("read_file", system.read_file, "Прочитать файл из песочницы.",
         {"path": S("имя файла", True)}, "safe", "sandbox", "Чтение файла")

register("list_files", system.list_files, "Список файлов в песочнице.",
         {"subdir": S("подпапка (необязательно)")}, "safe", "sandbox", "Файлы")

register("delete_file", system.delete_file, "Удалить файл или папку в песочнице.",
         {"path": S("что удалить", True)}, "danger", "sandbox", "Удаление файла")

register("run_python", system.run_python,
         "Выполнить python-код в песочнице: расчёты, анализ данных, генерация файлов.",
         {"code": S("код на python", True)}, "caution", "sandbox", "Python")

register("run_shell", system.run_shell,
         "Выполнить команду в терминале песочницы.",
         {"command": S("команда", True)}, "danger", "sandbox", "Терминал")

register("sandbox_info", system.sandbox_info,
         "Посмотреть состояние песочницы этого диалога: имя, сколько файлов, размер, список файлов.",
         {}, "safe", "sandbox", "Песочница")

register("sandbox_clear", system.sandbox_clear,
         "Полностью очистить песочницу этого диалога — удалить все файлы. Только по прямой просьбе пользователя.",
         {"confirm": S("подтверждение словом да")}, "danger", "sandbox", "Очистка песочницы")

register("sandbox_rename", system.sandbox_rename,
         "Переименовать песочницу этого диалога (дать ей понятное название).",
         {"name": S("новое название", True)}, "safe", "sandbox", "Имя песочницы")

register("make_archive", system.make_archive,
         "Упаковать файлы песочницы в zip, чтобы прислать пользователю.",
         {"paths_csv": S("файлы через запятую, пусто = всё"), "archive_name": S("имя архива")},
         "safe", "sandbox", "Архив")

# ------------------------------------------------------------- COMPUTER-USE
register("screenshot", system.screenshot,
         "Сделать снимок экрана компьютера пользователя, чтобы увидеть, что происходит.",
         {}, "caution", "computer", "Снимок экрана")

register("mouse_click", system.mouse_click,
         "Кликнуть мышью по координатам экрана (computer-use).",
         {"x": I("координата X", True), "y": I("координата Y", True),
          "button": S("left или right"), "double": B("двойной клик")},
         "danger", "computer", "Клик мышью")

register("mouse_move", system.mouse_move, "Переместить курсор в точку экрана.",
         {"x": I("X", True), "y": I("Y", True)}, "danger", "computer", "Движение курсора")

register("mouse_scroll", system.mouse_scroll,
         "Прокрутить колесом мыши: amount отрицательный — вниз, положительный — вверх.",
         {"amount": I("шаги прокрутки"), "horizontal": I("горизонтальная прокрутка")},
         "danger", "computer", "Прокрутка")

register("mouse_drag", system.mouse_drag,
         "Перетащить мышью из одной точки экрана в другую.",
         {"x1": I("откуда X", True), "y1": I("откуда Y", True),
          "x2": I("куда X", True), "y2": I("куда Y", True)},
         "danger", "computer", "Перетаскивание")

register("type_text", system.type_text, "Напечатать текст в активном окне компьютера.",
         {"text": S("что напечатать", True)}, "danger", "computer", "Ввод текста")

register("press_key", system.press_key,
         "Нажать клавишу (enter, tab, escape, стрелки) с модификаторами (command, shift).",
         {"key": S("клавиша", True), "modifiers": S("модификаторы через запятую")},
         "danger", "computer", "Нажатие клавиши")

register("open_app", system.open_app,
         "Открыть приложение или ссылку на компьютере пользователя (Safari, Telegram, ozon.ru).",
         {"name": S("имя приложения или URL", True)}, "danger", "computer", "Открыть приложение")

register("screen_info", system.screen_info, "Узнать размер экрана (нужно перед кликами).",
         {}, "safe", "computer", "Параметры экрана")

register("system_info", system.system_info, "Информация о системе и времени.",
         {}, "safe", "base", "Система")

# -------------------------------------------------------------------- МЕДИА
register("generate_image", media.generate_image,
         "Сгенерировать изображение по текстовому описанию.",
         {"prompt": S("описание картинки", True), "width": I("ширина"), "height": I("высота"),
          "style": S("стиль, например cinematic, 3d render")},
         "safe", "media", "Генерация изображения")

register("analyze_image", media.analyze_image,
         "Посмотреть на изображение (файл, кадр камеры, скриншот) и ответить на вопрос о нём.",
         {"image_ref": S("путь к файлу или data-url", True), "question": S("что нужно понять")},
         "safe", "media", "Анализ изображения")

register("analyze_video", media.analyze_video,
         "Разобрать видео: кадры + речь.",
         {"path": S("путь к видео", True), "question": S("что нужно понять"), "frames": I("сколько кадров")},
         "safe", "media", "Анализ видео")

register("transcribe_audio", media.transcribe_audio,
         "Распознать речь из аудиофайла.",
         {"path_or_data_url": S("путь к аудио", True), "language": S("язык, ru по умолчанию")},
         "safe", "media", "Распознавание речи")

register("send_telegram", media.send_telegram,
         "Отправить сообщение/уведомление пользователю в Telegram.",
         {"text": S("текст сообщения", True), "silent": B("без звука")},
         "danger", "media", "Telegram")

register("telegram_send_file", media.telegram_send_file,
         "Отправить файл из песочницы в Telegram.",
         {"path": S("файл в песочнице", True), "caption": S("подпись")},
         "danger", "media", "Файл в Telegram")


# --------------------------------------------------------------- ПАМЯТЬ/AUTO
def _remember(key: str, value: str, kind: str = "fact") -> Dict[str, Any]:
    from .. import db
    db.remember(kind or "fact", key, value)
    return {"ok": True, "saved": {"key": key, "value": value}}


def _recall(kind: str = "") -> Dict[str, Any]:
    from .. import db
    items = db.recall(kind)
    return {"ok": True, "memories": [{"key": i["key"], "value": i["value"], "kind": i["kind"]} for i in items]}


def _schedule_task(title: str, prompt: str, schedule: str = "") -> Dict[str, Any]:
    from .. import db
    task = db.create_task(title=title, prompt=prompt, mode="auto", schedule=schedule or "")
    db.notify("Задача в фоне: " + title, prompt[:200], "info")
    return {"ok": True, "task_id": task["id"], "title": title,
            "note": "Задача отправлена во вкладку AUTO и выполняется в фоне."}


register("remember", _remember,
         "Запомнить факт о пользователе (предпочтения, имя, привычки) для персонализации.",
         {"key": S("короткий ключ", True), "value": S("что запомнить", True),
          "kind": S("тип: fact/preference/person/project")},
         "safe", "memory", "Запомнить")

register("recall", _recall, "Вспомнить сохранённые факты о пользователе.",
         {"kind": S("тип памяти (необязательно)")}, "safe", "memory", "Вспомнить")

register("schedule_task", _schedule_task,
         "Отправить задачу в фон (вкладка AUTO). Используй для долгих задач, мониторинга, "
         "напоминаний. schedule: 'every 30m', 'every 2h', 'daily 09:00' или пусто для разового.",
         {"title": S("короткое название", True), "prompt": S("что именно сделать", True),
          "schedule": S("расписание")},
         "safe", "auto", "Фоновая задача")


# ------------------------------------------------------------------ ДИСПЕТЧЕР
def schemas(groups: List[str] | None = None) -> List[Dict[str, Any]]:
    out = []
    for name, tool in TOOLS.items():
        if groups and tool["group"] not in groups:
            continue
        out.append(tool["schema"])
    return out


def risk_of(name: str) -> str:
    return TOOLS.get(name, {}).get("risk", "danger")


def label_of(name: str) -> str:
    return TOOLS.get(name, {}).get("label", name)


def call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    tool = TOOLS.get(name)
    if not tool:
        return {"ok": False, "error": "неизвестный инструмент: " + name}
    fn = tool["fn"]
    try:
        clean = {k: v for k, v in (args or {}).items() if v is not None}
        return fn(**clean)
    except TypeError as exc:
        return {"ok": False, "error": "неверные аргументы (%s): %s" % (name, exc)}
    except Exception as exc:
        return {"ok": False, "error": "ошибка инструмента %s: %s" % (name, exc)}

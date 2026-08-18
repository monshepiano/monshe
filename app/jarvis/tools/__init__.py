"""Реестр инструментов JARVIS: схемы для function-calling + диспетчер вызовов."""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Tuple

from . import media, system, web

# risk: safe | caution | danger
#   safe    — выполняется сразу
#   caution — выполняется сразу, но пишется в ленту (или спрашивает, если включено)
#   danger  — всегда требует подтверждения (оплата, удаление, отправка сообщений, computer-use)

TOOLS: Dict[str, Dict[str, Any]] = {}


def register(name: str, fn: Callable, description: str, params: Dict[str, Any],
             risk: str = "safe", group: str = "base", label: str = "",
             silent: bool = False) -> None:
    """silent=True — служебный шаг (агент «смотрит на экран»). Такие шаги не
    показываются в ленте, не пишутся в историю и не требуют подтверждения.
    Единственное место, где это знание объявлено: раньше оно было продублировано
    в agent.py, server.py и app.js, и каждый новый инструмент приходилось
    прописывать в трёх местах."""
    TOOLS[name] = {
        "fn": fn,
        "risk": risk,
        "group": group,
        "silent": bool(silent),
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
         {}, "caution", "computer", "Снимок экрана", silent=True)

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
         {}, "safe", "computer", "Параметры экрана", silent=True)

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


def _forget(key: str, kind: str = "") -> Dict[str, Any]:
    from .. import db
    n = db.forget_by_key(key, kind)
    if not n:
        return {"ok": False, "error": "факта с названием «%s» в памяти нет" % key}
    return {"ok": True, "forgotten": key}


def _recall(kind: str = "") -> Dict[str, Any]:
    from .. import db
    items = db.recall(kind)
    return {"ok": True, "memories": [{"key": i["key"], "value": i["value"], "kind": i["kind"]} for i in items]}


def _schedule_task(title: str, prompt: str, schedule: str = "") -> Dict[str, Any]:
    from .. import auto, db, sandbox
    schedule = (schedule or "").strip()
    # модель могла прислать расписание словами — нормализуем
    if schedule and auto.parse_schedule(schedule) is None:
        schedule = auto.detect_schedule(schedule) or ""
    chat_id = ""
    try:
        chat_id = sandbox.current_chat() or ""
    except Exception:
        chat_id = ""
    task = auto.create_background_task(title=title, prompt=prompt, schedule=schedule, chat_id=chat_id)
    human = auto.describe_schedule(schedule)
    # Без уведомления: о постановке задачи пользователь уже узнаёт из карточки
    # «В фоне» в диалоге и из вкладки AUTO. Третий раз повторять незачем.
    return {"ok": True, "task_id": task["id"], "title": title, "schedule": schedule,
            "when": human,
            "note": "Задача создана во вкладке AUTO (%s). Результат придёт уведомлением." % human}


register("remember", _remember,
         "Запомнить факт о пользователе (предпочтения, имя, привычки) для персонализации. "
         "Этим же инструментом факт ИСПРАВЛЯЕТСЯ: вызови с тем же key и новым value — "
         "старое значение заменится.",
         {"key": S("короткий ключ", True), "value": S("что запомнить", True),
          "kind": S("тип: fact/preference/person/project")},
         "safe", "memory", "Запомнить")

register("forget", _forget,
         "Удалить факт из памяти по его названию (key). Используй, когда пользователь "
         "просит забыть что-то или факт устарел и заменять его нечем.",
         {"key": S("название факта", True), "kind": S("тип памяти (необязательно)")},
         "safe", "memory", "Забыть")

register("recall", _recall, "Вспомнить сохранённые факты о пользователе.",
         {"kind": S("тип памяти (необязательно)")}, "safe", "memory", "Вспомнить")


# ------------------------------------------------------------------ ДИАЛОГ
def _ask_user(**_kw) -> Dict[str, Any]:
    """Заглушка: настоящее ожидание ответа живёт в agent.py.

    Инструмент особенный — он не «вычисляет результат», а останавливает работу
    и ждёт нажатия кнопки в интерфейсе. Такое умеет только агент, у которого
    есть chat_id и поток событий. Здесь объявлена лишь схема для модели.
    """
    return {"ok": False, "error": "ask_user выполняется агентом"}


register("ask_user", _ask_user,
         "Задать пользователю уточняющий вопрос с готовыми вариантами ответа. "
         "Вызывай, когда для правильного выполнения не хватает одной детали "
         "(какой из вариантов, куда сохранить, делать ли следующий шаг). "
         "Лучше один короткий вопрос с кнопками, чем догадка. "
         "Не спрашивай о том, что уже сказано, и не задавай больше одного вопроса подряд.",
         {"question": S("сам вопрос, коротко и по-русски", True),
          "options": S("варианты ответа через | например: Да|Нет", True)},
         "safe", "base", "Уточняющий вопрос")

register("schedule_task", _schedule_task,
         "Отправить задачу в фон (вкладка AUTO). ОБЯЗАТЕЛЬНО вызывай для просьб вида "
         "'напомни', 'напиши мне через N минут', 'проверяй каждый день', 'следи за', "
         "'пришли утром', а также для долгих задач и мониторинга.",
         {"title": S("короткое название задачи", True),
          "prompt": S("что именно сделать, когда придёт время", True),
          "schedule": S("когда: 'in 10s', 'in 5m', 'in 2h', 'every 30m', 'every 1h', "
                        "'every 2d', 'daily 09:00'; пусто — выполнить сразу в фоне")},
         "safe", "auto", "Фоновая задача")


# ------------------------------------------------------------------ ДИСПЕТЧЕР
def schemas(groups: List[str] | None = None) -> List[Dict[str, Any]]:
    out = []
    for name, tool in TOOLS.items():
        if groups and tool["group"] not in groups:
            continue
        out.append(tool["schema"])
    return out


def group_names(group: str) -> List[str]:
    """Имена инструментов одной группы."""
    return [n for n, t in TOOLS.items() if t.get("group") == group]


def is_silent(name: str) -> bool:
    """Служебный ли это шаг (не показывать пользователю)."""
    return bool((TOOLS.get(name) or {}).get("silent"))


def silent_names() -> List[str]:
    return [n for n, t in TOOLS.items() if t.get("silent")]


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


# ------------------------------------------- ПЕРЕХВАТ ТЕКСТОВЫХ ВЫЗОВОВ
# Некоторые модели вместо структурного tool_call печатают вызов текстом:
#   function schedule_task({"title": "…"})
#   function callopen_url("https://…")
#   <tool_call>{"name": "web_search", "arguments": {"query": "…"}}</tool_call>
# Такое нельзя показывать пользователю — надо распознать и выполнить.

_PREFIX_RE = (
    r"(?:<\s*tool_call\s*>\s*|```(?:json|tool_code|python|tool)?\s*)?"
    r"(?:(?:functions?|tool_call|tool|инструмент)\s*[.:>=]?\s*)?"
    r"(?:call\s*[.:]?\s*)?"
)


def _param_order(name: str) -> List[str]:
    props = TOOLS.get(name, {}).get("schema", {}).get("function", {}).get("parameters", {})
    required = list(props.get("required") or [])
    keys = list((props.get("properties") or {}).keys())
    return required + [k for k in keys if k not in required]


def _split_top(text: str) -> List[str]:
    """Делит строку аргументов по запятым верхнего уровня."""
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    quote = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _lit(value: str) -> Any:
    v = (value or "").strip()
    if not v:
        return ""
    try:
        return json.loads(v)
    except Exception:
        pass
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    low = v.lower()
    if low in ("true", "да"):
        return True
    if low in ("false", "нет"):
        return False
    if low in ("null", "none"):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def _parse_args(name: str, raw: str) -> Dict[str, Any]:
    raw = (raw or "").strip().rstrip(";")
    if not raw:
        return {}
    # чистый JSON-объект
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                if set(obj.keys()) <= {"name", "arguments", "parameters"} and (
                        "arguments" in obj or "parameters" in obj):
                    inner = obj.get("arguments") or obj.get("parameters") or {}
                    if isinstance(inner, str):
                        try:
                            inner = json.loads(inner)
                        except Exception:
                            inner = {}
                    return inner if isinstance(inner, dict) else {}
                return obj
        except Exception:
            pass
    order = _param_order(name)
    out: Dict[str, Any] = {}
    positional = 0
    for piece in _split_top(raw):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.+)$", piece, re.S)
        if m and m.group(1) in order:
            out[m.group(1)] = _lit(m.group(2))
        else:
            if positional < len(order):
                out[order[positional]] = _lit(piece)
            positional += 1
    return out


def _balanced(text: str, start: int) -> int:
    """Индекс закрывающей скобки для открывающей в позиции start (или -1)."""
    depth = 0
    quote = ""
    i = start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


_CALL_STARTS = ("<tool_call", "```json", "```tool", "```python", "functions.", "functions ",
                "function ", "function.", "functioncall", "function call", "call ",
                "tool_call", "tool:", '{"name"', "{'name'", "{\"tool\"")


def looks_like_call_prefix(text: str) -> bool:
    """Похоже ли начало ответа на псевдо-вызов инструмента (для придержки стрима)."""
    s = (text or "").lstrip()
    if not s:
        return True
    head = s[:64].lower()
    for p in _CALL_STARTS:
        if head.startswith(p) or p.startswith(head):
            return True
    for name in TOOLS:
        low = name.lower()
        if head.startswith(low) or low.startswith(head):
            return True
    return False


_FENCE_RE = re.compile(r"```[A-Za-z0-9_+-]*\n.*?```", re.S)
_INLINE_RE = re.compile(r"`[^`\n]+`")


def _looks_like_call(body: str) -> bool:
    b = (body or "").strip()
    if b.startswith("{") and b.endswith("}"):
        return bool(re.search(r"\"(?:name|tool|function)\"\s*:", b))
    m = re.match(_PREFIX_RE + r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", b)
    return bool(m and m.group(1) in TOOLS)


def _mask_code(text: str) -> Tuple[str, Dict[str, str]]:
    """Прячет блоки кода, чтобы не принять пример кода за вызов инструмента."""
    masked: Dict[str, str] = {}
    counter = [0]

    def hide(match: "re.Match[str]") -> str:
        blob = match.group(0)
        inner = blob
        if blob.startswith("```"):
            inner = blob.split("\n", 1)[1][:-3] if "\n" in blob else ""
            if _looks_like_call(inner):
                return "\n" + inner.strip() + "\n"
        counter[0] += 1
        key = "\x00CODE%d\x00" % counter[0]
        masked[key] = blob
        return key

    out = _FENCE_RE.sub(hide, text)
    out = _INLINE_RE.sub(hide, out)
    return out, masked


def _unmask(text: str, masked: Dict[str, str]) -> str:
    for key, blob in masked.items():
        text = text.replace(key, blob)
    return text


def parse_text_calls(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Находит в тексте псевдо-вызовы инструментов.

    Возвращает (текст без вызовов, список {name, args}).
    """
    if not text:
        return "", []
    if "(" not in text and "{" not in text:
        return text, []
    found: List[Dict[str, Any]] = []
    out, masked = _mask_code(text)

    # 1) JSON-конверт: {"name": "web_search", "arguments": {...}}
    env = re.compile(r"\{\s*\"(?:name|tool|function)\"\s*:\s*\"([A-Za-z_][A-Za-z0-9_]*)\"")
    guard = 0
    while guard < 8:
        guard += 1
        m = env.search(out)
        if not m or m.group(1) not in TOOLS:
            break
        close_idx = _balanced(out, m.start())
        if close_idx < 0:
            break
        blob = out[m.start():close_idx + 1]
        try:
            obj = json.loads(blob)
        except Exception:
            break
        name = m.group(1)
        inner = obj.get("arguments")
        if inner is None:
            inner = obj.get("parameters")
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except Exception:
                inner = {}
        if not isinstance(inner, dict):
            inner = {k: v for k, v in obj.items() if k not in ("name", "tool", "function", "type")}
        found.append({"name": name, "args": inner})
        out = out[:m.start()] + out[close_idx + 1:]

    # 2) синтаксис вызова: function name(...) / name({...})
    names = sorted(TOOLS.keys(), key=len, reverse=True)
    pattern = re.compile(_PREFIX_RE + r"(" + "|".join(re.escape(n) for n in names) + r")\s*(\(|\{)")
    guard = 0
    pos = 0
    while guard < 8:
        guard += 1
        m = pattern.search(out, pos)
        if not m:
            break
        prefix = out[m.start():m.start(1)]
        before = out[m.start() - 1] if m.start() > 0 else " "
        if not prefix.strip() and (before.isalnum() or before == "_"):
            pos = m.start(1) + 1
            continue
        open_idx = m.start(2)
        close_idx = _balanced(out, open_idx)
        if close_idx < 0:
            break
        raw = out[open_idx + 1:close_idx] if m.group(2) == "(" else out[open_idx:close_idx + 1]
        name = m.group(1)
        found.append({"name": name, "args": _parse_args(name, raw)})
        tail = out[close_idx + 1:]
        tail = re.sub(r"^\s*(</\s*tool_call\s*>|```|;)", "", tail)
        out = out[:m.start()] + tail
        pos = 0

    if found:
        out = re.sub(r"</?\s*tool_call\s*>", "", out)
        out = re.sub(r"```[a-z_]*\s*```", "", out)
        out = re.sub(r"^\s*```[a-z_]*\s*$", "", out, flags=re.M)
    return _unmask(out, masked).strip(), found

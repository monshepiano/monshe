"""Агентское ядро JARVIS: цикл «мысль → инструмент → наблюдение → ответ».

Генерирует поток событий для UI:
  status, thinking, model, delta, tool_start, tool_result, approval, plan, step, file, done, error
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable, Dict, Generator, Iterable, List, Optional

from . import db, llm, orchestrator, sandbox, telemetry, tools
from .config import CONFIG


# Лимиты шагов лежат в конфиге (agent.max_steps_chat / max_steps_agent).
# Раньше они были константами здесь, а в конфиге болтался неиспользуемый
# computer_use.max_steps — правка настройки не меняла ничего.
def _max_steps(agent_mode: bool) -> int:
    key = "agent.max_steps_agent" if agent_mode else "agent.max_steps_chat"
    try:
        return max(1, int(CONFIG.get(key, 18 if agent_mode else 6)))
    except Exception:
        return 18 if agent_mode else 6


def _now_str() -> str:
    return time.strftime("%d.%m.%Y %H:%M")


def _fact_value(value: str, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n,.;:!?—–-")[:limit]


_MEMORY_SECRET = re.compile(
    r"\b(?:парол\w*|password|api[ _-]?key|secret|token|cvv|номер\s+карт\w*|"
    r"паспорт\w*)\b", re.IGNORECASE,
)


def extract_obvious_memories(text: str) -> List[Dict[str, str]]:
    """Локально извлечь явно сказанные устойчивые факты по грамматике фразы.

    Здесь намеренно нет словарей городов, блюд, устройств или других тематик.
    Мы распознаём отношения первого лица (нравится, зовут, живу, работаю) и
    явную просьбу запомнить; значение всегда остаётся дословным фрагментом.
    Это устраняет отдельный скрытый LLM-вызов, который мог конкурировать с
    первым токеном следующего ответа.
    """
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw or _MEMORY_SECRET.search(raw):
        return []
    facts: List[Dict[str, str]] = []

    def add(kind: str, relation: str, candidate: str, limit: int = 120,
            distinct_key: bool = False) -> None:
        value = re.split(
            r"\s+(?:и|а|но|and|but)\s+(?=(?:я|мне|меня|мой|моя|мои|у\s+меня|"
            r"работаю|учусь|живу|пользуюсь|использую|i|my|work|study|live|use)\b)",
            candidate, maxsplit=1, flags=re.IGNORECASE,
        )[0]
        value = _fact_value(value, limit)
        if not value:
            return
        key = "%s: %s" % (relation, value[:42]) if distinct_key else relation
        fact = {"kind": kind, "key": key, "value": value}
        if fact not in facts:
            facts.append(fact)

    preferences = (
        ("Нравится", r"\b(?:я\s+)?(?:люблю|обожаю|предпочитаю)\s+([^,.!?]{2,100})"),
        ("Нравится", r"\bмне\s+нрав(?:ится|ятся)\s+([^,.!?]{2,100})"),
        ("Не нравится", r"\b(?:я\s+)?(?:не\s+люблю|не\s+переношу|избегаю)\s+([^,.!?]{2,100})"),
        ("Нравится", r"\b(?:i\s+)?(?:love|prefer|like)\s+([^,.!?]{2,100})"),
        ("Не нравится", r"\b(?:i\s+)?(?:dislike|avoid|cannot tolerate)\s+([^,.!?]{2,100})"),
    )
    for relation, pattern in preferences:
        for found in re.finditer(pattern, raw, re.IGNORECASE):
            # Положительный шаблон способен начать совпадение внутри «не люблю».
            before = raw[max(0, found.start() - 12):found.start()].casefold()
            if relation == "Нравится" and re.search(r"\b(?:не|not)\s*$", before):
                continue
            # Заголовок описывает ОТНОШЕНИЕ, value хранит дословный объект.
            # Включать объект ещё и в key («Предпочтение: обезьянок») означало
            # показывать одно и то же дважды. Разные предпочтения различает
            # writer identity по value, а не искусственно раздутый заголовок.
            add("preference", relation, found.group(1), 100)

    # Это грамматические отношения, не тематические словари значений. Стабильный
    # relation key позволяет честно заменить устаревшее имя/работу/место вместо
    # накопления противоречащих карточек.
    durable = (
        ("person", "Имя / обращение", r"\b(?:меня\s+зовут|называй\s+меня|обращайся\s+ко\s+мне\s+как)\s+([^,.!?]{1,80})"),
        ("person", "Имя / обращение", r"\b(?:my\s+name\s+is|call\s+me)\s+([^,.!?]{1,80})"),
        # Русские формы ниже уже однозначно от первого лица, поэтому второе
        # «я» после связки не обязательно: «я люблю X и работаю Y» — две clauses.
        ("person", "Работа", r"\b(?:я\s+)?работаю\s+([^,.!?]{2,120})"),
        ("person", "Обучение", r"\b(?:я\s+)?учусь\s+([^,.!?]{2,120})"),
        ("person", "Место проживания", r"\b(?:я\s+)?живу\s+([^,.!?]{2,120})"),
        ("fact", "Основной инструмент", r"\b(?:я\s+)?(?:пользуюсь|использую)\s+([^,.!?]{2,120})"),
        # В английском bare verb не кодирует лицо, поэтому разрешаем опущенное
        # I только непосредственно после coordinating conjunction.
        ("person", "Work", r"(?:\bi\s+|\b(?:and|but)\s+)work\s+([^,.!?]{2,120})"),
        ("person", "Study", r"(?:\bi\s+|\b(?:and|but)\s+)study\s+([^,.!?]{2,120})"),
        ("person", "Location", r"(?:\bi\s+|\b(?:and|but)\s+)live\s+([^,.!?]{2,120})"),
        ("fact", "Primary tool", r"(?:\bi\s+|\b(?:and|but)\s+)use\s+([^,.!?]{2,120})"),
    )
    for kind, relation, pattern in durable:
        for found in re.finditer(pattern, raw, re.IGNORECASE):
            add(kind, relation, found.group(1))

    # Явная команда памяти — общий escape hatch для любого полезного факта,
    # которому не нужна новая тема в коде («учти, у меня ...»). Не создаём рядом
    # вторую generic-карточку, если та же clause уже разобрана грамматически.
    directives = (
        r"\b(?:запомни|учти)(?:\s*[:,]?\s*(?:что\s+)?)?([^.!?]{2,180})",
        r"\b(?:remember|keep\s+in\s+mind)(?:\s+that)?\s+([^.!?]{2,180})",
    )
    for pattern in directives:
        for found in re.finditer(pattern, raw, re.IGNORECASE):
            clause = _fact_value(found.group(1), 180)
            if not clause:
                continue
            known = any(fact["value"].casefold() in clause.casefold() for fact in facts)
            if not known:
                add("fact", "Явный факт", clause, 180, distinct_key=True)

    return facts[:8]

def remember_obvious_facts(text: str) -> List[Dict[str, Any]]:
    saved = []
    for fact in extract_obvious_memories(text):
        saved.append(db.remember(fact["kind"], fact["key"], fact["value"], 1.15))
    return saved


def remember_semantic_facts(text: str) -> List[Dict[str, Any]]:
    """Compatibility entry point for the local, verbatim memory writer.

    Semantic extraction used to make a hidden 25-second nano request after SSE.
    A quick next message could then contend with that request on the provider.
    This fallback intentionally performs no network I/O and persists only
    proven first-person grammar.
    """
    return remember_obvious_facts(text)


_MEMORY_REPAIR_LOCK = threading.Lock()
_MEMORY_REPAIR_DONE = False


def repair_legacy_automatic_memories() -> int:
    """Один раз убрать мусор второго writer из уже существующей базы.

    До этой версии локальный parser сначала сохранял корректный факт, а затем
    модель в том же turn могла вызвать ``remember`` ещё несколько раз. У старой
    таблицы нет origin, но есть надёжная причинная граница: timestamp user
    message → timestamp следующего user message. Если первая реплика содержит
    доказанную first-person grammar, preference/model-записи из её короткого
    окна заменяются ровно теми дословными фактами, которые parser способен
    воспроизвести. Ручные старые записи вне такого окна не трогаются; удалённый
    пользователем факт не воскресает, потому что без существующей строки окно
    не создаёт replacement.
    """
    global _MEMORY_REPAIR_DONE
    with _MEMORY_REPAIR_LOCK:
        if _MEMORY_REPAIR_DONE:
            return 0
        _MEMORY_REPAIR_DONE = True
        try:
            messages = db.query(
                "SELECT id,chat_id,content,created_at FROM messages WHERE role='user' "
                "ORDER BY created_at,id"
            )
            # Следующая реплика считается внутри того же диалога: параллельный
            # camera/другой chat не должен преждевременно обрезать causal window.
            next_in_chat: Dict[str, float] = {}
            following: Dict[str, float] = {}
            for message in reversed(messages):
                chat_id = str(message.get("chat_id") or "")
                next_in_chat[str(message.get("id") or "")] = following.get(chat_id, 0)
                following[chat_id] = float(message.get("created_at") or 0)
            rows = db.query("SELECT * FROM memory")
            remove_ids = set()
            replacements: List[Dict[str, str]] = []
            for message in messages:
                facts = extract_obvious_memories(message.get("content", ""))
                if not facts:
                    continue
                started = float(message.get("created_at") or 0)
                next_at = next_in_chat.get(str(message.get("id") or "")) or started + 600
                ended = min(next_at, started + 600)
                in_window = []
                fact_tokens = [
                    [token for token in re.findall(r"\w+", fact["value"].casefold())
                     if len(token) >= 4]
                    for fact in facts
                ]
                for row in rows:
                    event_at = max(float(row.get("created_at") or 0),
                                   float(row.get("updated_at") or 0))
                    if not (started - 0.25 <= event_at < ended):
                        continue
                    kind = str(row.get("kind") or "").casefold()
                    row_tokens = [token for token in re.findall(
                        r"\w+", (str(row.get("key") or "") + " " +
                                  str(row.get("value") or "")).casefold()) if len(token) >= 4]
                    related = any(
                        a.startswith(b) or b.startswith(a)
                        for group in fact_tokens for a in group for b in row_tokens
                    )
                    if kind == "preference" or related:
                        in_window.append(row["id"])
                if in_window:
                    remove_ids.update(in_window)
                    replacements.extend(facts)
            for mem_id in remove_ids:
                db.forget(mem_id)
            seen = set()
            for fact in replacements:
                sig = (fact["kind"], fact["key"], fact["value"])
                if sig in seen:
                    continue
                seen.add(sig)
                db.remember(*sig, 1.15)
            return len(remove_ids)
        except Exception:
            # Миграция качества не имеет права мешать запуску интерфейса.
            return 0


# Длинный общий prompt — плохое место для протокола интерфейса: после истории,
# изображения и результатов инструментов даже сильная модель иногда забывает
# правило, а на следующем текстовом ходе vision-напоминания уже вовсе не было.
# Поэтому у КАЖДОЙ актуальной пользовательской реплики есть один короткий
# nearby-контракт. Это не preflight и не второй LLM-вызов: system-сообщение едет
# в том же запросе непосредственно перед user message. Классы закрытые — не
# список тем, который пришлось бы бесконечно дополнять частными заплатками.
PROACTIVE_UI_CONTRACT = """КОНТРАКТ ЭТОГО ХОДА — ЖИВОЙ ВЫБОР:
Показывай интерактивные controls только когда человеку действительно есть что
выбирать. Блок ```ui ОБЯЗАТЕЛЕН в любом из трёх случаев:
1) в творческой/созидательной просьбе не задан важный вариант (концепция, стиль,
   формат, объём, сложность или набор возможностей) и можно назвать 2–4 варианта;
2) ты просишь выбрать между двумя или более уже названными вариантами;
3) предлагаешь несколько равноправных настроек или путей продолжения.
В случае 1 сначала покажи 2–4 КОНТЕКСТНЫХ варианта и дождись следующей реплики:
не выбирай молча и не запускай generate/create до выбора. Варианты не оформляй
обычным Markdown-списком. Пример корректного протокола:

```ui
tiles Стиль: вариант 1 | вариант 2 | вариант 3
```

Если честные конечные варианты перечислить нельзя, задай один короткий обычный
вопрос и остановись: человек ответит в основном composer. Никогда не создавай
`text`/`area` только ради второго свободного поля ввода. Интерфейс сам добавит
«Свой вариант» к конечному списку и отправку. Не добавляй button «Сгенерировать»,
«Отправить» или «Поехали». Если пользователь уже задал все существенные
параметры — сразу выполняй просьбу без панели. НЕ показывай ui для
фактического вопроса, сводки новостей/погоды, отчёта о уже выполненном действии
или простого продолжения с заданными параметрами: там полезного выбора нет."""


# Дополнение только для кадра объединяется с общим nearby-контрактом в ОДНОМ
# system message. Оно не пропадает на следующем текстовом ходе: базовые правила
# выше сервер добавляет заново к каждой реплике.
VISION_UI_CONTRACT = """\n\nДОПОЛНЕНИЕ ДЛЯ ИЗОБРАЖЕНИЯ:
Если пользователь просит творчески преобразовать изображение (например, сделать
мем, стилизацию, постер или аватар), но не задал стиль/концепцию, НЕ создавай
изображение на этом ходу. Дай 3–4 уместных ИМЕННО ДЛЯ ЭТОГО КАДРА варианта через
`tiles Стиль: ...` в единственном блоке ```ui и дождись выбора. Если стиль уже
явно задан, ui-блок не нужен — сразу выполняй просьбу."""


def turn_ui_contract(has_image: bool = False) -> str:
    """Единственный nearby-контракт актуального user turn."""
    return PROACTIVE_UI_CONTRACT + (VISION_UI_CONTRACT if has_image else "")


_UI_FENCE = re.compile(
    r"```\s*ui\s*\n([\s\S]*?)```", re.IGNORECASE
)
_CREATIVE_IMAGE_REQUEST = re.compile(
    r"(?:сдел(?:ай|ать)|созд(?:ай|ать)|преврат(?:и|ить)|нарис(?:уй|овать)|"
    r"сгенерир(?:уй|овать)|оформ(?:и|ить))[^\n]{0,60}"
    r"(?:мем|постер|аватар|обложк|стикер|картинк|изображени|иллюстрац|стилиз)",
    re.IGNORECASE,
)
_CREATIVE_CHOICE_GIVEN = re.compile(
    r"(?:в\s+стиле|стиль\s*:|концепц(?:ия|ию|ии)\s*:|с\s+(?:подписью|текстом)|"
    r"(?:про|на\s+тему|сюжет)\s+\S|[«\"].{2,}[»\"])", re.IGNORECASE,
)


def needs_creative_image_choice(user_text: str, has_image: bool = False) -> bool:
    """Нужна ли обязательная развилка до творческой обработки кадра.

    Это не тематический список для всех ответов. Граница узкая и закрытая:
    входной кадр + просьба создать новый visual artifact + отсутствие уже
    указанной концепции. Обычный анализ фото и конкретное «в стиле X» проходят
    сразу. На следующем ходе выбранная плитка уже является параметром, а не
    новым create-request, поэтому генерация также не блокируется.
    """
    text = str(user_text or "").strip()
    return bool(has_image and _CREATIVE_IMAGE_REQUEST.search(text)
                and not _CREATIVE_CHOICE_GIVEN.search(text))


def has_choice_ui(text: str) -> bool:
    """True только для закрытого ui-fence, в котором реально есть варианты.

    Разделитель проверяется внутри того же fence: вертикальная черта в обычном
    тексте после пустого ```ui блока не должна случайно открыть gate.
    """
    return any("|" in match.group(1) for match in _UI_FENCE.finditer(str(text or "")))


# Свободные text/area не считаются полезным интерактивом: основной composer уже
# даёт ровно такой ввод. UI нужен лишь там, где он выражает настоящий выбор или
# специализированное значение, которое обычной строкой задавать неудобно.
_UI_CONTROL = re.compile(
    r"^\s*(?:tiles|multi|rank|slider|number|rate|toggle|date|color|button)\s+\S",
    re.IGNORECASE | re.MULTILINE,
)
_REPLY_REQUEST = re.compile(
    r"\b(?:уточни(?:те)?|выбери(?:те)?|выбрать|выбира(?:й|ешь|ете)|"
    r"подскажи(?:те)?|ответь(?:те)?|напиши(?:те)?|укажи(?:те)?|"
    r"скажи(?:те)?|что\s+предпочита(?:ешь|ете)|какой\s+вариант)\b",
    re.IGNORECASE,
)
_OPTION_LINE = re.compile(
    r"^\s*(?:#{1,4}\s*)?(?:[-*•]|\d+[.)])\s+(.+?)\s*$",
    re.IGNORECASE,
)
_USER_REQUESTS_ALTERNATIVES = re.compile(
    r"\b(?:предложи(?:те)?|придумай(?:те)?|назови(?:те)?|перечисли(?:те)?|дай(?:те)?)"
    r"\b.{0,55}\b(?:вариант|иде|способ|пример)",
    re.IGNORECASE | re.DOTALL,
)
_OPTIONAL_FOLLOWUP = re.compile(
    r"^(?:хочешь|хотите|могу\s+ли\s+я|нужно\s+ли\s+ещ[её])\b",
    re.IGNORECASE,
)


def _clean_option(raw: str) -> str:
    """Короткая подпись плитки из обычного Markdown-пункта модели."""
    bold = re.search(r"\*\*(.+?)\*\*", raw)
    item = bold.group(1) if bold else raw
    item = re.sub(r"[*_`#]", "", item).strip().rstrip(".;")
    item = re.sub(r"^вариант\s*\d+\s*[:.)-]?\s*", "", item, flags=re.IGNORECASE)
    if len(item) > 76:
        item = re.split(r"\s+[—–-]\s+|:\s+", item, maxsplit=1)[0].strip()
    return item.replace("|", "/")[:76].strip()


def _listed_options(lines: List[str], start: int = 0) -> List[str]:
    options: List[str] = []
    for raw in lines[max(0, start):]:
        match = _OPTION_LINE.match(raw)
        if not match:
            continue
        item = _clean_option(match.group(1))
        if 2 <= len(item) <= 76 and item not in options:
            options.append(item)
        if len(options) == 6:
            break
    return options


def combined_reply_ui_spec(text: str) -> str:
    """Собрать controls из всех model fences в одну физическую панель.

    Контракт просит один ``ui`` block, но provider иногда повторяет fence для
    каждого вопроса. Раньше frontend получал только последний block и терял
    часть preflight. Здесь модельная разметка сходится к одной панели, а точные
    повторения строк не создают дублей controls.
    """
    controls: List[str] = []
    for match in _UI_FENCE.finditer(str(text or "")):
        for raw_line in match.group(1).splitlines():
            line = raw_line.strip()
            if line and _UI_CONTROL.match(line) and line not in controls:
                controls.append(line)
    return "\n".join(controls)


def has_interactive_ui(text: str) -> bool:
    """Есть ли внутри ui-fence хотя бы один реально поддерживаемый control."""
    return bool(combined_reply_ui_spec(text))


def needs_reply_ui(text: str, user_text: str = "") -> bool:
    """Ответ модели ждёт реплику — значит, обычного Markdown недостаточно.

    Проверяется структура последних строк, а не тема запроса. Вопросы внутри
    нумерованного материала не считаются обращением к человеку. Отдельный
    закрытый класс — когда пользователь сам попросил список альтернатив: такой
    список является готовым результатом, а не скрытым уточнением.
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    prose = _UI_FENCE.sub("", raw).strip()
    if not prose:
        return has_interactive_ui(raw)
    lines = [line.strip() for line in prose.splitlines() if line.strip()]
    if not lines:
        return False
    tail = lines[-10:]
    is_option = lambda line: bool(_OPTION_LINE.match(line))
    for index, line in enumerate(tail):
        clean = re.sub(r"[*_`]", "", line).strip()
        direct_question = (clean.rstrip().endswith("?") and not is_option(line)
                           and not _OPTIONAL_FOLLOWUP.search(clean))
        direct_request = bool(_REPLY_REQUEST.search(clean)) and not is_option(line)
        if (direct_question or direct_request) and all(is_option(x) for x in tail[index + 1:]):
            return True

    # Модели часто пишут «Вот варианты:» и замолкают без вопросительного знака.
    # Если это не запрошенный пользователем каталог идей, явная лексика выбора
    # плюс 2+ Markdown-пункта означает ожидание решения и обязана стать UI.
    options = _listed_options(lines)
    choice_cue = bool(re.search(r"\b(?:на\s+выбор|выбрать|выбор|вариант(?:а|ы|ов)?)\b", prose,
                                re.IGNORECASE))
    return bool(len(options) >= 2 and choice_cue
                and not _USER_REQUESTS_ALTERNATIVES.search(str(user_text or "")))


def reply_ui_fallback(text: str = "") -> str:
    """Создать tiles только из настоящего списка вариантов.

    Если вариантов нет, возвращаем пустую строку: обычный вопрос уже имеет
    composer под ним, и второе text/area поле было лишним дубликатом.
    """
    question = ""
    lines = str(text or "").splitlines()
    anchor = -1
    choice_question = False
    for index, raw in enumerate(lines):
        line = re.sub(r"[*_`]", "", raw).strip()
        if ((line.endswith("?") or _REPLY_REQUEST.search(line)) and len(line) <= 140):
            anchor = index
            question = line.rstrip("?:").replace(":", " —")
            choice_question = bool(re.search(
                r"\b(?:выб|вариант|предпочита|подходит|остановимся)\w*\b", line,
                re.IGNORECASE))
    options = _listed_options(lines, anchor + 1)
    if len(options) < 2 and choice_question:
        options = _listed_options(lines)
    if len(options) >= 2:
        label = question or "Выбери вариант"
        return "```ui\ntiles %s: %s\n```" % (label, " | ".join(options))
    return ""


def contextual_choice_fallback(text: str = "") -> str:
    """Последний детерминированный рубеж, если модель дважды забыла UI.

    Сначала переиспользуем её собственные короткие пункты — они уже описывают
    конкретный кадр. Общие варианты нужны лишь при полностью пустом/сломавшемся
    ответе; даже они привязаны к деталям и героям именно текущего кадра.
    """
    options: List[str] = []
    for raw in str(text or "").splitlines():
        match = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$", raw)
        if not match:
            continue
        item = re.sub(r"[*_`]", "", match.group(1)).strip().rstrip(".;")
        if 2 <= len(item) <= 72 and item not in options:
            options.append(item)
        if len(options) == 4:
            break
    if len(options) < 2:
        options = [
            "Ироничная подпись к главной детали кадра",
            "Диалог между героями или объектами кадра",
            "Киноафиша по сцене в кадре",
        ]
    return ("Выбери направление — сначала согласуем идею, затем я создам результат.\n\n"
            "```ui\ntiles Стиль: " + " | ".join(options[:4]) + "\n```")


def build_system_prompt(agent_mode: bool = False, computer_use: bool = False) -> str:
    user = CONFIG.get("user", {}) or {}
    memories = db.recall(limit=40)
    mem_lines = "\n".join("- [%s] %s: %s" % (m["kind"], m["key"], m["value"]) for m in memories[:30])
    who = []
    if user.get("name"):
        who.append("Имя пользователя: %s." % user["name"])
    if user.get("city"):
        who.append("Город: %s." % user["city"])
    if user.get("about"):
        who.append("О пользователе: %s" % user["about"])

    base = f"""Ты — JARVIS, личный ИИ-агент пользователя (как у Тони Старка).
Сегодня {_now_str()}. Кратко, по делу, с лёгкой ноткой уверенного дворецкого-инженера.
ЯЗЫК — РУССКИЙ ВЕЗДЕ И ВСЕГДА: ответ, ход мыслей (reasoning), планы, названия шагов,
пояснения к действиям, заголовки и тексты уведомлений. Даже размышляя «про себя»,
думай по-русски. Английский допустим только внутри кода, команд, путей и имён файлов.
Обращайся к пользователю на «вы» только если он сам так пишет; по умолчанию — дружелюбно на «ты».

{' '.join(who)}

ТЫ УМЕЕШЬ ДЕЙСТВОВАТЬ, а не только говорить. У тебя есть инструменты:
• интернет: web_search, open_url, deep_research, download_file, http_request;
• песочница на сервере: write_file, read_file, list_files, run_python, run_shell, make_archive (файлы можно прислать пользователю);
• управление песочницей: sandbox_info (что внутри), delete_file (убрать лишнее), sandbox_clear (стереть всё), sandbox_rename (дать имя);
• медиа: generate_image, analyze_image, analyze_video, transcribe_audio;
• память: явно сказанные факты система сохраняет локально до твоего запуска; тебе доступны recall и forget;
• диалог: ask_user — задать короткий уточняющий вопрос с кнопками-вариантами;
• компьютер пользователя: screenshot, screen_info, mouse_click, mouse_move, mouse_scroll, mouse_drag, type_text, press_key, open_app.

ПРАВИЛА:
1. Нужны свежие данные, цены, новости, факты после твоего обучения — обязательно вызывай web_search/open_url. Не выдумывай.
   У ТЕБЯ ЕСТЬ ДОСТУП В ИНТЕРНЕТ. Запрещено отвечать «у меня нет доступа к интернету»,
   «я не могу получить новости» или «инструменты не настроены» — это неправда.
   Спросили новости/погоду/курс/цену — молча вызови web_search и дай результат.
   Не переспрашивай «какая тема вас интересует», если просьба понятна: сначала найди, потом уточняй.
2. Не спрашивай разрешения на безопасные шаги — просто делай. Опасные действия система сама поставит на подтверждение.
3. Если пользователь просит файл (отчёт, таблицу, код, презентацию) — создай его в песочнице и укажи, что он готов к скачиванию.
4. Ссылайся на источники ссылками, когда искал в интернете.
5. Форматируй ответ markdown: заголовки, списки, **жирный**, таблицы, ```блоки кода```.
6. Явные личные факты (предпочтения, имена, работа) уже сохраняет входной локальный parser —
   не пересказывай их и не создавай вторую запись. Если пользователь прямо просит забыть факт,
   вызови forget с существующим key. Не выдумывай категории памяти и не сохраняй вводные слова.
7. Не выдумывай результаты инструментов: если инструмент вернул ошибку — честно скажи и предложи обход.
8. Развилка, где ты обязан ОСТАНОВИТЬСЯ и без ответа не можешь работать дальше
   (куда сохранить файл, продолжать ли рискованный путь) — вызови ask_user
   с 2-4 вариантами через |. Он ставит работу на паузу, поэтому используй его
   только когда пауза действительно нужна.
   Если же ты просто ПРЕДЛАГАЕШЬ выбор, а ответить можешь и дальше — не зови
   ask_user, а вставь блок ui из правила 10 прямо в текст ответа.
   Когда ответ очевиден из просьбы — не спрашивай, а делай. Максимум один вопрос подряд.
9. Песочница у каждого диалога своя. Просят «удали файл», «почисти песочницу», «сотри всё» — делай это инструментами delete_file / sandbox_clear, а не отговорками. Просят «назови песочницу» — sandbox_rename.
10. ЖИВЫЕ ЭЛЕМЕНТЫ УПРАВЛЕНИЯ. Предлагаешь варианты на выбор или величину,
   которую надо подобрать, — не описывай их словами и не нумеруй списком,
   а дай пользователю настоящие органы управления прямо в ответе.
   Для этого вставь блок кода с языком ui. Точно так, дословно:

   ```ui
   tiles Формат: PDF | Word | Markdown
   slider Громкость 0..100 = 40
   toggle Уведомления = on
   ```

   Строки (выбирай тот тип, который ТОЧНО отвечает на вопрос):
   tiles Подпись: A | B | C — выбор ОДНОГО варианта;
   multi Подпись: A | B | C — выбор НЕСКОЛЬКИХ сразу;
   rank Подпись: A | B | C — расставить по важности (ответ — порядок);
   slider Подпись мин..макс [step шаг] [unit ед] = начальное — плавная величина;
   number Подпись = начальное — точное число (мин..макс можно не указывать);
   rate Подпись 1..5 = 0 — оценка звёздами «насколько»;
   toggle Подпись = on|off — да/нет;
   text Подпись = подсказка — короткий ввод в одну строку;
   area Подпись = подсказка — длинный ответ в несколько строк;
   date Подпись = 2026-08-18 — дата; color Подпись = #00c8f0 — цвет;
   Кнопку «Отправить»/«Сгенерировать»/«Поехали» добавлять НЕ НУЖНО и НЕЛЬЗЯ:
   интерфейс сам поставит её там, где она требуется. Лишняя кнопка в списке
   выглядит как второй, не работающий способ подтвердить выбор.
   Пользователь покрутит и пришлёт итог одним сообщением, ты продолжишь.
   ЖЕЛЕЗНОЕ правило: если ты в тексте предлагаешь выбрать (скорость, уровень,
   формат, вариант) — блок ui обязан быть в ЭТОМ ЖЕ сообщении. Написать
   «выбери скорость» и не дать органов управления нельзя: выбирать будет нечем.
   Правила: слово ui после кавычек обязательно, по одному элементу на строку,
   максимум 4 элемента. Бери РАЗНЫЕ типы, а не четыре плитки подряд:
   один и тот же вопрос, заданный четырьмя одинаковыми плитками, читается
   как анкета, а разные органы управления — как живой пульт.

   КОГДА ЭТО ОБЯЗАТЕЛЬНО. Проверь себя перед ответом: собираешься ли ты
   сейчас выбрать за пользователя что-то, что он мог бы выбрать сам?
   Любая просьба «сделай/напиши/собери X» часто имеет несколько
   равноправных решений — размер, сложность, стиль, оформление, набор
   возможностей. Не выбирай молча и не спрашивай только словами: покажи блок
   ui. Если вариантов нет, но ответ человека всё равно нужен, используй text
   или area. Обычный текстовый вопрос без control не завершает такой ход.
   Если без выбора получится существенно другой результат, сначала дождись
   ответа пользователя и только потом создавай; не запускай генератор заранее.
   Пример: просят игру — дай выбрать размер поля, скорость, оформление.
   Просят текст — объём, тон, формат.
   Без блока отвечай, только когда решение действительно одно: короткий
   фактический вопрос, продолжение уже начатой работы или случай, когда
   пользователь сам задал все параметры.

КАК ВЫЗЫВАТЬ ИНСТРУМЕНТЫ (это критично):
Инструмент вызывается ТОЛЬКО штатным механизмом function calling твоего API.
НИКОГДА не печатай вызов текстом в ответ пользователю. Запрещены строки вида
«function call open_url("…")», «schedule_task(…)», «tool_call», а также
JSON-описание вызова прямо в тексте. Любые их варианты запрещены.
Если хочешь применить инструмент — примени его, а не описывай.
После того как инструмент вернул результат, напиши пользователю нормальный
человеческий ответ по этому результату. Пустой ответ недопустим: если инструмент
не сработал, скажи об этом словами.

ФОН И AUTO:
Ты никогда не решаешь сам, что задача «слишком долгая» для текущего диалога,
и не пытаешься поставить её в AUTO. Явные напоминания, расписания, мониторинг
и просьбы «выполни в фоне» маршрутизирует сервер ДО твоего запуска. Всё, что
пришло тебе, выполни прямо сейчас в этом диалоге — в том числе игру, код,
исследование или большой файл.
"""
    if not computer_use:
        base += """
ГРАНИЦА КОМПЬЮТЕРА:
Режим «Компьютер» сейчас ВЫКЛЮЧЕН. run_shell/run_python работают headless внутри
песочницы: запрещено использовать в них open, osascript, webbrowser, NSWorkspace
или иной способ показывать внешнее приложение, окно, файл либо URL на компьютере.
Если без внешнего окна нельзя — система сначала отдельно спросит пользователя.
"""
    if computer_use:
        base += """
РЕЖИМ УПРАВЛЕНИЯ КОМПЬЮТЕРОМ — ЖЕЛЕЗНОЕ ПРАВИЛО:
Курсор и клавиатура двигаются ТОЛЬКО вызовом инструментов. Текст ответа ничего не делает.

ЗАПРЕЩЕНО писать «сейчас перемещу курсор», «нажал», «открыл», «кликнул», если ты
не вызвал соответствующий инструмент и не увидел его результат. Это ложь, а не работа.
Никогда не описывай содержимое экрана по памяти или догадке — только по свежему скриншоту.
Ты не видишь картинки сам: единственный источник правды об экране — поле screen
в результате screenshot. Нет screenshot — нет знания об экране.

ПОРЯДОК ДЕЙСТВИЙ:
1. screen_info — узнать размер экрана.
2. screenshot — вернёт поле screen: словесную карту экрана с координатами
   элементов. Это твои глаза, читай её внимательно.
3. Взять из карты координаты нужного элемента. Если сказано, что снимок
   уменьшен, — пересчитать координаты, как там указано.
4. Вызвать mouse_move / mouse_click / type_text / press_key / open_app.
5. Снова screenshot — убедиться, что получилось. Не получилось — поправить и повторить.

Каждый шаг — отдельный вызов инструмента. Между шагами коротко говори, что видишь.
Итог сообщай только после того, как последний скриншот подтвердил результат.
Если инструмент вернул ошибку (нет прав, не macOS) — честно скажи об этом
и объясни, что включить в Системных настройках, вместо выдуманного успеха.
"""
    if agent_mode:
        base += """
АГЕНТСКИЙ РЕЖИМ — ЕДИНЫЙ PREFLIGHT:
До первого инструмента просмотри задачу ЦЕЛИКОМ и одним решением определи ВСЕ
критически недостающие параметры. Если они есть, задай их В ОДНОМ сообщении и
ОДНОМ ```ui блоке: отдельная строка control на каждый независимый вопрос (не
больше 4). «Свой вариант» добавляет интерфейс — не дублируй его строкой. Не запускай
инструменты, не строй план и останови этот turn. Нельзя спрашивать один параметр,
получать ответ, а затем выдавать следующую панель: некритичное выбери разумно.
Если критических пробелов нет, не устраивай анкету — сразу работай.
Когда данных достаточно, пользователь больше не участвует: выполни работу инструментами
по шагам, не останавливаясь на промежуточные вопросы. Некритичные пробелы закрой разумным
допущением и укажи его в финале. Заверши развёрнутым итогом: что сделано, что найдено,
какие файлы созданы.
"""
    if mem_lines:
        base += "\nЧТО ТЫ ЗНАЕШЬ О ПОЛЬЗОВАТЕЛЕ:\n" + mem_lines + "\n"
    return base


def _tool_groups(computer_use: bool) -> List[str]:
    # AUTO маршрутизирует только сервер, до запуска модели. Интерактивному
    # агенту и исполнителю уже созданной фоновой задачи группа ``auto`` не
    # выдаётся вообще: иначе снова появляются два источника решения и модель
    # может самовольно отправить обычную долгую работу в фон.
    groups = ["web", "sandbox", "media", "memory", "base"]
    if computer_use and CONFIG.get("computer_use.enabled", True):
        groups.append("computer")
    return groups


def _tool_source(tool_name: str, args: Optional[Dict[str, Any]] = None) -> str:
    args = args or {}
    return str(args.get("command") or args.get("code") or "")


def opens_external_ui(tool_name: str, args: Optional[Dict[str, Any]] = None) -> bool:
    """Может ли вызов вывести окно/URL за пределами интерфейса JARVIS.

    На Mac универсальный LaunchServices-вход — ``open``; также закрываем
    AppleScript, webbrowser/NSWorkspace и Linux/Windows launchers. Это проверка
    механизма запуска, а не бесконечный список приложений, поэтому Safari,
    Preview и ещё не существующее приложение проходят одну границу.
    Обычный Python ``open(file)`` намеренно не совпадает.
    """
    if tool_name == "open_app":
        return True
    if tool_name not in {"run_shell", "run_python"}:
        return False
    source = _tool_source(tool_name, args)
    patterns = (
        r"(?:^|[;&|]\s*|\n\s*)(?:sudo\s+)?(?:/usr/bin/)?open(?:\s|$)",
        r"['\"](?:sudo\s+)?(?:/usr/bin/)?open\s+",
        r"['\"](?:/usr/bin/)?open['\"]",
        r"\b(?:osascript|xdg-open)\b",
        r"\bgio\s+open\b",
        r"(?:^|[;&|]\s*|\n\s*)start\s+",
        r"\b(?:webbrowser\s*\.|NSWorkspace\b)",
        r"/Applications/[^\n]+\.app(?:/Contents/MacOS/)?",
    )
    return any(re.search(pattern, source, re.IGNORECASE) for pattern in patterns)


def opens_terminal(tool_name: str, args: Optional[Dict[str, Any]] = None) -> bool:
    """Распознать штатные пути, способные открыть видимое окно Terminal."""
    args = args or {}
    if tool_name == "open_app":
        # Нормализация принадлежит только имени приложения и не связана с
        # memory writer: пользовательские значения памяти храним дословно.
        target = re.sub(r"[\s._-]+", "", str(args.get("name") or "").strip().casefold())
        if target.endswith("app"):
            target = target[:-3]
        return target in {"terminal", "терминал", "iterm", "iterm2", "warp"}
    if tool_name not in {"run_shell", "run_python"}:
        return False
    payload = _tool_source(tool_name, args).casefold()
    terminal_name = r"(?:terminal|терминал|iterm2?|warp)"
    patterns = (
        rf"\bopen\s+(?:[^\n;&|]*\s)?-a\s+['\"]?{terminal_name}\b",
        rf"tell\s+application\s+['\"]{terminal_name}['\"]",
        rf"application\s*\(\s*['\"]{terminal_name}['\"]\s*\)",
        rf"\b(?:xdg-open|start)\b[^\n;&|]*\b{terminal_name}\b",
    )
    return any(re.search(pattern, payload, re.IGNORECASE) for pattern in patterns)


def approval_style(tool_name: str, args: Optional[Dict[str, Any]] = None,
                   computer_use: bool = False) -> str:
    """Визуальный тон вопроса: появление окна — permission, не угроза."""
    if opens_terminal(tool_name, args) or (
            not computer_use and opens_external_ui(tool_name, args)):
        return "permission"
    return "danger"


def needs_approval(tool_name: str, args: Optional[Dict[str, Any]] = None,
                   computer_use: bool = False) -> Optional[str]:
    """Возвращает причину, если нужно подтверждение пользователя."""
    # Вне явно включённого режима «Компьютер» любое внешнее окно принадлежит
    # пользователю. Проверяем до dispatch — приложение не успеет мелькнуть.
    if opens_terminal(tool_name, args):
        return "открытие приложения «Терминал»"
    if not computer_use and opens_external_ui(tool_name, args):
        return "открытие окна или приложения вне режима «Компьютер»"
    risk = tools.risk_of(tool_name)
    safety = CONFIG.get("safety", {}) or {}
    if risk == "safe":
        return None
    mapping = {
        "delete_file": ("confirm_delete", "удаление данных"),
        "run_shell": ("confirm_shell", "выполнение команды в терминале"),
        "send_telegram": ("confirm_send_message", "отправка сообщения от твоего имени"),
        "telegram_send_file": ("confirm_send_message", "отправка файла в мессенджер"),
        "mouse_click": ("confirm_computer_use", "управление мышью на твоём компьютере"),
        "mouse_move": ("confirm_computer_use", "управление курсором"),
        "mouse_scroll": ("confirm_computer_use", "прокрутка на твоём компьютере"),
        "mouse_drag": ("confirm_computer_use", "перетаскивание мышью"),
        "type_text": ("confirm_computer_use", "ввод текста на твоём компьютере"),
        "press_key": ("confirm_computer_use", "нажатие клавиш на твоём компьютере"),
        "open_app": ("confirm_computer_use", "запуск приложения на твоём компьютере"),
    }
    # screenshot и screen_info — это «глаза» агента, а не действие: они ничего
    # не меняют. Спрашивать санкцию на каждый кадр — значит сделать
    # computer-use неработоспособным, поэтому они проходят молча.
    flag, reason = mapping.get(tool_name, ("", "потенциально опасное действие"))
    if flag and not safety.get(flag, True):
        return None
    if risk == "caution" and safety.get("auto_approve_readonly", True) and tool_name not in mapping:
        return None
    if risk == "danger" or flag:
        return reason
    return None


def detect_payment_intent(args: Dict[str, Any]) -> bool:
    blob = json.dumps(args, ensure_ascii=False).lower()
    return any(word in blob for word in ("оплат", "купить", "payment", "checkout", "оформить заказ", "картой"))


def _describe_screen(data_url: str, shot: Dict[str, Any]) -> str:
    """Показать кадр зрительной модели и получить словесную карту экрана.

    Управляющая модель инструментов «слепая»: сама картинку она не увидит.
    Поэтому глаза и руки разделены — vision-модель описывает, что где лежит,
    в пикселях, а решение о клике принимает основная модель.
    """
    w = shot.get("width") or 0
    h = shot.get("height") or 0
    prompt = (
        "Снимок экрана, %sx%s px. Ответь ТЕЛЕГРАФНО, без вступлений.\n"
        "Строка 1: активное окно.\n"
        "Далее — только строки вида «Подпись — (x, y)» для кликабельных "
        "элементов и пунктов списков. Максимум 25 строк, самое важное.\n"
        "Координаты — центр элемента в пикселях снимка от левого верхнего угла. "
        "Не выдумывай то, чего не видишь."
    ) % (w or "?", h or "?")
    try:
        seen = llm.vision(prompt, data_url)
    except Exception as exc:
        return "не удалось рассмотреть экран: %s" % exc
    if not (seen or "").strip():
        return "экран получен, но зрительная модель ничего не описала"
    scale = shot.get("scale")
    tail = ""
    if scale and scale != 1:
        tail = ("\nВНИМАНИЕ: снимок уменьшен. Координаты выше даны в пикселях снимка; "
                "перед кликом умножь их на %.4g, чтобы получить координаты реального экрана."
                % (1.0 / float(scale)))
    return seen.strip() + tail


# Действия, реально трогающие компьютер. Служебные «глаза» (silent) сюда не
# входят по определению — источник истины один: реестр инструментов.
COMPUTER_TOOLS = {n for n in tools.group_names("computer") if not tools.is_silent(n)}

# «сейчас нажму», «кликнул», «открыл окно» — заявка на действие
_ACTION_CLAIM = re.compile(
    r"(перемещ|навед|нажал|нажим|кликн|щёлкн|щелкн|открыл|открыва|печата|ввёл|ввел|"
    r"переключ|прокрут|скролл|курсор)", re.I)


def _claims_action(text: str) -> bool:
    return bool(_ACTION_CLAIM.search(text or ""))


def suggest_replies(user_text: str, answer: str) -> List[str]:
    """Мгновенные локальные продолжения — никакого второго облачного запроса.

    Эти кнопки появляются уже после foreground. Их прежняя nano-генерация могла
    занимать до 20 секунд, конкурировала с новым сообщением и часто возвращала
    невалидный JSON. Универсальные разговорные действия полезнее нестабильной
    псевдоперсонализации; содержимое реплик никуда не логируется.
    """
    del user_text
    span = telemetry.Span("reply_suggestions", source="local")
    if not (answer or "").strip():
        span.finish("empty")
        return []
    items = ["Расскажи подробнее", "Покажи на примере", "Предложи следующий шаг"]
    span.finish("ok", count=len(items))
    return items


# Служебные отметки шагов плана: их видит фронт, но не пользователь.
# [ШАГ 3] — модель сама объявила номер; [ШАГ ГОТОВ] — модель закрыла текущий.
_STEP_MARK = re.compile(r"\[\s*ШАГ\s*(?:\d+|ГОТОВ)\s*\]\s*")


def _step_text(item: Any) -> str:
    """Достать человеческую формулировку шага из чего угодно.

    Модель может отдать строку, а может — объект вида
    {"step": 1, "action": "..."} или {"описание": "..."}. Раньше такой объект
    молча приводился к строке, и в карточке плана у пользователя оказывалось
    «{\'step\': 1, \'action\': ...}» вместо текста шага.
    """
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        # берём первое осмысленное текстовое поле, не гадая по именам ключей:
        # служебные номера отсеиваем по типу, а не по списку названий
        for v in item.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(item, list):
        parts = [_step_text(x) for x in item]
        return "; ".join(p for p in parts if p)
    if item is None or isinstance(item, bool):
        return ""
    return str(item).strip()


def parse_plan_steps(text: str) -> List[str]:
    """Разобрать ответ планировщика.

    ПОЧЕМУ ЗДЕСЬ НЕ ОДИН json.loads. Раньше план доставался единственным
    способом: найти /\[.*\]/ и скормить json.loads. Это работало ровно до тех
    пор, пока модель отвечала идеально. А она вероятностная: то допишет
    пояснение и в текст попадут ДВА массива (жадный поиск склеит их и разбор
    рухнет), то отдаст массив объектов, то одинарные кавычки, то упрётся в
    лимит токенов и оборвёт хвост на середине. Любой сбой давал либо пустой
    план, либо карточку с сырым JSON внутри — это и был «некорректно
    отобразился план».
    Формат ответа гарантировать нельзя, поэтому разбор идёт лесенкой: от
    строгого к терпимому, и последним рубежом — обычный нумерованный список.
    """
    text = (text or "").strip()
    if not text:
        return []

    # ограду ```json ... ``` снимаем сразу: внутри неё обычно чистый ответ
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()

    def clean(items: Any) -> List[str]:
        if not isinstance(items, list):
            return []
        out = []
        for it in items:
            t = _step_text(it)
            if t:
                out.append(t[:200])
        return out[:8]

    # 1. Текст целиком — корректный JSON.
    try:
        got = clean(json.loads(text))
        if got:
            return got
    except Exception:
        pass

    # 2. Ищем сбалансированный массив, а не «от первой скобки до последней»:
    #    жадный поиск склеивал два разных массива в один битый кусок.
    for start in (m.start() for m in re.finditer(r"\[", text)):
        depth, in_str, esc_ch = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if esc_ch:
                esc_ch = False
                continue
            if ch == "\\":
                esc_ch = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    chunk = text[start:i + 1]
                    for candidate in (chunk, chunk.replace("'", '"')):
                        try:
                            got = clean(json.loads(candidate))
                            if got:
                                return got
                        except Exception:
                            continue
                    break

    # 3. Ответ оборвался на полуслове (кончились токены): вытаскиваем те
    #    строки в кавычках, что успели прийти, — лучше неполный план, чем
    #    никакого.
    quoted = re.findall(r'"([^"\n]{3,200})"', text)
    if len(quoted) >= 2:
        return [q.strip() for q in quoted][:8]

    # 4. Модель ответила обычным нумерованным или маркированным списком.
    lines = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        ln = re.sub(r"^[-*\u2022]\s+", "", ln)
        ln = re.sub(r"^\d+[.)]\s*", "", ln)
        if ln and ln != raw.strip() or re.match(r"^\d+[.)]", raw.strip()):
            lines.append(ln[:200])
    if len(lines) >= 2:
        return lines[:8]
    return []


class Agent:
    """Один прогон агента (чат-ответ или фоновая задача)."""

    def __init__(self, chat_id: str = "", task_id: str = "", agent_mode: bool = False,
                 computer_use: bool = False, approvals_auto: bool = False,
                 visible_plan: bool = True,
                 cancel_check: Optional[Callable[[], bool]] = None) -> None:
        self.chat_id = chat_id
        self.task_id = task_id
        self.agent_mode = agent_mode
        self.computer_use = computer_use
        self.approvals_auto = approvals_auto
        # AUTO исполняется без чата: semantic planner там был невидим, но всё
        # равно создавал отдельный облачный запрос перед каждым заданием.
        self.visible_plan = visible_plan
        self.cancel_check = cancel_check
        self.sandbox_id = chat_id or ""
        self.created_files: List[Dict[str, Any]] = []
        self.used_tools: List[str] = []
        self.model_used = ""
        self.plan_len = 0          # сколько шагов в плане (0 — плана нет)
        self.plan_at = 0           # какой шаг идёт сейчас
        self.plan_steps: List[str] = []   # формулировки шагов для prompt/UI
        self.show_thinking = False # показывать ли ход мыслей (решается по ходу)

    def _cancelled(self) -> bool:
        try:
            return bool(self.cancel_check and self.cancel_check())
        except Exception:
            return False

    def _advance_plan(self, target: int) -> List[Dict[str, Any]]:
        """Продвинуть UI-план до реальной границы, не вызывая ради неё LLM."""
        events: List[Dict[str, Any]] = []
        target = min(max(0, int(target)), self.plan_len)
        while self.plan_at and self.plan_at < target:
            self.plan_at += 1
            events.append({"type": "plan_step", "step": self.plan_at})
        return events

    # ------------------------------------------------------------ approvals
    def _wait_approval(self, tool_name: str, args: Dict[str, Any], reason: str,
                       timeout: int = 300, style: str = "danger") -> Dict[str, Any]:
        if style == "permission":
            risk = "notice"
        else:
            risk = "critical" if detect_payment_intent(args) or tool_name == "delete_file" else "high"
        approval = db.create_approval(tool_name, args, risk, reason, self.chat_id, self.task_id)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._cancelled():
                db.decide_approval(approval["id"], "expired")
                return {**approval, "status": "cancelled"}
            fresh = db.get_approval(approval["id"])
            if fresh and fresh.get("status") in ("approved", "rejected"):
                return fresh
            time.sleep(0.6)
        db.decide_approval(approval["id"], "expired")
        return {**approval, "status": "expired"}

    # ------------------------------------------------------- вопрос к юзеру
    def _wait_answer(self, question: str, options: List[str],
                     timeout: int = 300) -> Dict[str, Any]:
        """Задать вопрос и дождаться нажатия кнопки в интерфейсе.

        Механика та же, что у подтверждений: запись в БД + опрос её статуса.
        Так ответ переживает обрыв SSE и работает из любой вкладки.
        """
        record = db.create_question(self.chat_id, question, options)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._cancelled():
                return {**record, "status": "cancelled", "answer": ""}
            fresh = db.get_question(record["id"])
            if fresh and fresh.get("status") == "answered":
                return fresh
            time.sleep(0.5)
        return {**record, "status": "expired", "answer": ""}

    # ---------------------------------------------------- результат вызова
    @staticmethod
    def _append_tool_result(convo: List[Dict[str, Any]], call: Dict[str, Any], name: str,
                            result: Any, from_text: bool) -> None:
        # Скриншот — картинка, а модель, которая умеет вызывать инструменты,
        # обычно не умеет смотреть. Поэтому кадр сначала «переводит в слова»
        # зрительная модель, и управляющая модель получает готовые координаты.
        if name == "screenshot" and isinstance(result, dict) and result.get("ok"):
            data_url = result.pop("data_url", "")
            if data_url:
                result["screen"] = _describe_screen(data_url, result)

        payload = json.dumps(result, ensure_ascii=False)
        if len(payload) > 14000:
            payload = payload[:14000] + "…(обрезано)"
        if from_text:
            convo.append({
                "role": "user",
                "content": ("[Система] Результат инструмента %s:\n%s\n\n"
                            "Продолжай. Никогда не печатай вызовы инструментов текстом — "
                            "используй только штатный механизм вызова функций."
                            % (name, payload)),
            })
        else:
            convo.append({"role": "tool", "tool_call_id": call.get("id", ""),
                          "name": name, "content": payload})


    # ------------------------------------------------------------- planning
    def make_plan(self, task: str, starting_tools: Optional[List[str]] = None) -> List[str]:
        """Построить семантический план уже доказанной автономной работы.

        Это намеренно НЕ preflight: ``run`` вызывает планировщик только после
        того, как основная модель вернула первый разрешённый non-ask_user tool
        call. Поэтому обычный ответ и уточнение не платят за второй запрос и не
        получают фиктивный план. Локальные три шаблона удалены: именно они
        превращали почти любую задачу в один и тот же fallback. Ошибка/пустой
        ответ означает отсутствие карточки, а не подстановку общих фраз.
        """
        text = re.sub(r"\s+", " ", str(task or "")).strip()
        if not text:
            return []
        tools_hint = ", ".join(str(x) for x in (starting_tools or []) if x) or "не указан"
        try:
            result = llm.chat([
                {"role": "system", "content":
                 "Ты лаконичный планировщик автономного AI-агента. Разбей именно "
                 "эту задачу на 3–6 конкретных, различимых и проверяемых шагов на "
                 "русском. Называй предмет и результат задачи, не используй общие "
                 "заглушки вроде «разобрать задачу», «выполнить действия», "
                 "«представить итог». Не выдумывай уже полученные результаты. "
                 "Ответь ТОЛЬКО JSON-массивом строк без markdown и пояснений."},
                {"role": "user", "content":
                 "Задача: %s\nПервый выбранный агентом инструмент: %s" % (text, tools_hint)},
            ], tier="nano", max_tokens=320, temperature=0.2, timeout=5,
               operation="planner")
            steps = parse_plan_steps(result.get("content", ""))
            return steps if 3 <= len(steps) <= 6 else []
        except Exception:
            return []

    # ------------------------------------------------------------------ run
    def run(self, messages: List[Dict[str, Any]], user_text: str = "",
            has_image: bool = False, require_ui_choice: bool = False,
            preflight_resolved: bool = False) -> Generator[Dict[str, Any], None, None]:
        # каждый диалог работает в своей песочнице
        sandbox.set_chat(self.sandbox_id)
        route = orchestrator.choose_tier(
            user_text, has_image=has_image, agent_mode=self.agent_mode, has_tools=True,
            computer_use=self.computer_use)
        tier = route["tier"]
        social_only = orchestrator.is_social_only(user_text)
        # Социальный gate сильнее положения AGENT-тумблера. «Привет» остаётся
        # одной обычной репликой: без плана, кухни и последующего reply UI.
        score = float(route.get("score") or 0)
        verbose = bool(not social_only and
                       (self.agent_mode or self.computer_use or score >= 0.30))
        yield {"type": "route", "tier": tier, "reason": route["reason"],
               "score": score, "verbose": verbose}

        # Управление компьютером без разрешения системы невозможно: macOS
        # молча гасит клики, и агент бесконечно «нажимает» впустую. Проверяем
        # ДО работы и честно говорим, что включить, — одним сообщением.
        if self.computer_use:
            from .tools import system as _sys
            if _sys.IS_MAC and not _sys.accessibility_ok():
                yield {"type": "delta", "text": _sys._NO_ACCESS_HINT}
                yield {"type": "done", "content": _sys._NO_ACCESS_HINT,
                       "files": [], "tools": []}
                return

        available = tools.schemas(_tool_groups(self.computer_use))
        # Автопамять имеет ровно одного владельца: локальный грамматический
        # extractor на входе сообщения. Раньше та же реплика одновременно
        # отдавалась модели с remember — она успевала добавить «кстати»,
        # выдуманную категорию и пересказ того же preference. Удаляем второй
        # writer физически из schemas и runtime allowlist; recall/forget остаются.
        available = [schema for schema in available
                     if (schema.get("function") or {}).get("name") != "remember"]
        if self.agent_mode:
            # AGENT preflight живёт в одном обычном response turn как единая
            # multi-control ui-панель. Блокирующий singleton ask_user оставлял
            # старый SSE ждать в БД, а каждый ответ пользователя запускал новый
            # run — отсюда цепочка конфликтующих ответов на скриншоте. В AGENT
            # этот tool физически не выдаётся модели; ожидание завершает turn.
            available = [schema for schema in available
                         if (schema.get("function") or {}).get("name") != "ask_user"]
        if not route.get("offer_tools", True):
            # оркестратор отдал реплику дешёвой модели именно потому, что
            # инструменты тут не нужны — не суём их ей в руки
            available = []
        # Не доверяем одному лишь списку schemas: некоторые модели способны
        # напечатать/галлюцинировать вызов функции, которой в нём нет. Перед
        # фактическим dispatch сверяемся с тем же закрытым набором. Это и есть
        # архитектурная граница: schedule_task остаётся серверным механизмом и
        # не может быть выполнен ни интерактивным, ни headless-агентом.
        allowed_tool_names = {
            (t.get("function") or {}).get("name") for t in available
            if (t.get("function") or {}).get("name")
        }
        max_steps = _max_steps(self.agent_mode)

        # Первый model turn — единственная достоверная граница между «начинаю
        # автономную работу» и «мне не хватает данных». До разрешённого
        # non-ask_user tool call существует только pending-decision: самого плана
        # и отдельного LLM-запроса ещё нет. События работы придерживаются, затем
        # semantic plan выходит перед ними. Обычный текст/уточнение просто снимают
        # pending и никогда не получают фиктивную карточку.
        plan: List[str] = []
        plan_pending = bool(self.visible_plan and self.agent_mode and user_text and not social_only)
        plan_announced = False
        deferred_work_events: List[Dict[str, Any]] = []

        def announce_plan() -> List[Dict[str, Any]]:
            nonlocal plan_announced
            if plan_announced or not plan:
                return []
            plan_announced = True
            self.plan_len = len(plan)
            self.plan_steps = list(plan)
            self.plan_at = 1
            return [
                {"type": "plan", "steps": list(plan)},
                {"type": "plan_step", "step": 1},
            ]

        def abandon_unstarted_plan() -> None:
            """Уточнение/обычный текст не являются автономным выполнением."""
            nonlocal plan, plan_pending, plan_announced
            if plan_announced:
                return
            plan = []
            plan_pending = False
            plan_announced = False
            self.plan_len = 0
            self.plan_at = 0
            self.plan_steps = []
            deferred_work_events.clear()

        # Сложность задачи не угадываем по теме: берём измеримые признаки.
        # Остальным включателем служит сам ход работы — см. ниже, шаг >= 2.
        self.show_thinking = bool(not social_only and (self.agent_mode or self.computer_use))

        convo = list(messages)
        final_text = ""
        reply_ui_sent = False
        retried_claim = False          # ловушку вранья взводим один раз за прогон
        preflight_retry = False        # после общей панели цепочка вопросов запрещена
        choice_failures = 0            # максимум одна перепроверка model output
        seen_calls: Dict[str, int] = {}   # защита от зацикливания на одном вызове
        # Идемпотентность дорогой генерации: LLM нередко повторяет тот же
        # generate_image в следующем ходе. Результат переиспользуем для модели,
        # но второй provider-call и второй file event не создаём.
        completed_calls: Dict[str, Dict[str, Any]] = {}
        # Reasoning сначала копится невидимо. Если весь ход мыслей оказался
        # одной короткой фразой, карточка вообще не создаётся. После достижения
        # порога накопленное показывается целиком, дальнейшие chunks идут живьём.
        thinking_pending: List[str] = []
        thinking_visible = False
        thinking_min_chars = 90

        for step in range(max_steps):
            if self._cancelled():
                return
            # phase="think" — это то самое ожидание перед первым словом ответа.
            # Фронт по нему показывает мигающий курсор вместо крутилки; угадывать
            # состояние по тексту статуса он не должен.
            if step == 0:
                yield {"type": "status", "text": "Думаю", "phase": "think"}
            else:
                # Новый модельный ход после инструмента — естественная граница
                # проверки результата. План двигается здесь, но дополнительный
                # облачный вызов только ради смены сегмента не создаётся.
                if self.used_tools and self.plan_at and self.plan_at < self.plan_len:
                    for progress in self._advance_plan(self.plan_at + 1):
                        yield progress
                # дошли до второго хода — значит одним ответом не обошлось:
                # это ровно тот случай, когда показать мысли уместно
                self.show_thinking = True
                yield {"type": "status", "text": "Проверяю результат" if self.used_tools
                       else "Продолжаю работу"}
            acc_text: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            stream_failed = None
            # «шлюз»: пока начало ответа похоже на текстовый вызов инструмента,
            # ничего не показываем пользователю — иначе в чат попадёт мусор
            # вида function schedule_task({...}). Пока plan ещё не объявлен,
            # также удерживаем первый результат целиком: сначала нужно понять,
            # не является ли он уточняющим вопросом.
            gate_open = False
            defer_plan_decision = bool(plan_pending and not plan_announced)

            for event in llm.chat_stream(
                    convo, tier=tier, tools=available,
                    operation="auto_model" if self.task_id else "foreground_model"):
                if self._cancelled():
                    return
                etype = event.get("type")
                if etype == "model":
                    self.model_used = event.get("model", "")
                    yield {"type": "model", "model": event.get("model"), "tier": tier}
                elif etype == "reasoning":
                    # МЫСЛИ НУЖНЫ НЕ ВСЕГДА. Даже в сложном режиме одна короткая
                    # служебная фраза не заслуживает отдельной карточки.
                    if self.show_thinking:
                        piece = str(event.get("text") or "")
                        if thinking_visible:
                            if piece:
                                out = {"type": "thinking", "text": piece}
                                if defer_plan_decision:
                                    deferred_work_events.append(out)
                                else:
                                    yield out
                        elif piece:
                            thinking_pending.append(piece)
                            if len("".join(thinking_pending).strip()) >= thinking_min_chars:
                                thinking_visible = True
                                out = {"type": "thinking", "text": "".join(thinking_pending)}
                                if defer_plan_decision:
                                    deferred_work_events.append(out)
                                else:
                                    yield out
                                thinking_pending = []
                elif etype == "delta":
                    acc_text.append(event["text"])
                    # Модель отмечает начало шага строкой [ШАГ N]. Ловим её в
                    # накопленном тексте: так прогресс плана — ФАКТ от самой
                    # модели, а не догадка фронта по числу вызовов инструментов.
                    if self.plan_len:
                        # модель может сама объявить номер шага — верим ей,
                        # если она обогнала наш счётчик
                        for mark in re.finditer(r"\[\s*ШАГ\s*(\d+)\s*\]", "".join(acc_text)):
                            n = int(mark.group(1))
                            if 0 < n <= self.plan_len and n > self.plan_at:
                                for progress in self._advance_plan(n):
                                    yield progress
                    if gate_open:
                        if not defer_plan_decision:
                            yield {"type": "delta", "text": event["text"]}
                    else:
                        joined = "".join(acc_text)
                        if not tools.looks_like_call_prefix(joined):
                            gate_open = True
                            if not defer_plan_decision:
                                yield {"type": "delta", "text": joined}
                elif etype == "tool_partial":
                    # Имя функции приходит раньше полного JSON/конца model turn.
                    # Не держим его за plan gate: tool-card ещё не открывается,
                    # но статус сразу меняется с абстрактного «Думаю» на честное
                    # «Готовлю поиск/файл». Это убирает длинное ложное ощущение,
                    # будто AGENT всё ещё не решил, что делать.
                    yield {"type": "tool_hint", "name": event.get("name", ""),
                           "group": tools.group_of(event.get("name", ""))}
                elif etype == "done":
                    tool_calls = event.get("tool_calls") or []
                    if event.get("reasoning") and not acc_text:
                        pass
                elif etype == "error":
                    stream_failed = event.get("error")

            if stream_failed and not acc_text and not tool_calls:
                higher = orchestrator.escalate(tier)
                if higher:
                    tier = higher
                    yield {"type": "status", "text": "Переключаюсь на резервную модель"}
                    continue
                yield {"type": "error", "error": stream_failed}
                return

            text_piece = _STEP_MARK.sub("", "".join(acc_text))
            from_text = False

            # модель напечатала вызов инструмента текстом — распознаём и выполняем
            if text_piece.strip():
                cleaned, text_calls = tools.parse_text_calls(text_piece)
                if text_calls:
                    from_text = True
                    if gate_open:
                        # уже что-то показали — стираем и перерисовываем. Пока
                        # решается судьба plan, сырой текст вообще не выходил.
                        if not defer_plan_decision:
                            yield {"type": "reset"}
                        gate_open = False
                    text_piece = cleaned
                    if cleaned:
                        gate_open = True
                        if not defer_plan_decision:
                            yield {"type": "delta", "text": cleaned}
                    for i, tc in enumerate(text_calls):
                        tool_calls.append({
                            "id": "txt_%d_%d" % (step, i),
                            "type": "function",
                            "function": {"name": tc["name"],
                                         "arguments": json.dumps(tc["args"], ensure_ascii=False)},
                        })

            # ЛОВУШКА ВРАНЬЯ. В режиме управления компьютером модель любит
            # написать «сейчас нажму» / «переключил диалог», не вызвав ни одного
            # инструмента. Раньше мы это замечали ТОЛЬКО в самом конце и просто
            # дописывали извинение — то есть фиксировали провал вместо того,
            # чтобы его исправить. Теперь возвращаем модель к работе прямо в
            # цикле: заявка на действие без вызова — это не ответ.
            # Условие намеренно НЕ опирается на список глаголов: любая попытка
            # перечислить формы («нажал», «нажму», «щёлкну»...) неизбежно
            # дырявая. Правило закрытое: в режиме управления компьютером ответ
            # без единого действия — подозрителен, и мы даём модели ровно один
            # шанс исправиться. Если действие и правда не требовалось, она
            # просто повторит ответ.
            if (self.computer_use and not tool_calls and text_piece.strip()
                    and not any(t in COMPUTER_TOOLS for t in self.used_tools)
                    and not retried_claim):
                retried_claim = True
                if gate_open:
                    if not defer_plan_decision:
                        yield {"type": "reset"}
                    gate_open = False
                if defer_plan_decision:
                    deferred_work_events.clear()
                    thinking_pending = []
                    thinking_visible = False
                yield {"type": "status", "text": "Проверяю, что действие выполнено"}
                convo.append({"role": "assistant", "content": text_piece})
                convo.append({"role": "user", "content":
                              "Ты ответил текстом, но не вызвал ни одного инструмента, "
                              "поэтому на компьютере НИЧЕГО не произошло. "
                              "Не описывай действия словами. Сейчас же вызови нужный "
                              "инструмент (screenshot, чтобы увидеть экран, затем "
                              "mouse_click / type_text / press_key). Если действие "
                              "на компьютере не требовалось — просто повтори свой "
                              "ответ без изменений."})
                continue

            # HARD UI GATE ДЛЯ ТВОРЧЕСКОГО КАДРА.
            # Prompt задаёт желаемое поведение, но не является границей
            # исполнения: модель всё ещё способна сразу вызвать generate_image.
            # Поэтому до dispatch проверяем измеримый контракт результата. Если
            # нет ui-fence с вариантами, ни прямой generate_image, ни текстовый
            # ответ не завершают этот ход. Ровно один retry получает тот же кадр;
            # после повторного нарушения выдаём детерминированную панель. Так
            # изображение физически невозможно создать раньше выбора.
            tries_to_generate = any(
                (call.get("function") or {}).get("name") == "generate_image"
                for call in tool_calls
            )
            choice_present = has_choice_ui(text_piece)

            # Панель и generate_image в ОДНОМ model turn — ещё не выбор
            # пользователя. Как только корректная панель появилась, этот run
            # всегда заканчивается без dispatch каких-либо tools. Следующий run
            # начнётся только после клика/«Своего варианта» и уже сможет создать
            # изображение с явно выбранной концепцией. Это физическая граница,
            # а не надежда, что модель послушается слова «дождись».
            if require_ui_choice and choice_present:
                if defer_plan_decision:
                    abandon_unstarted_plan()
                    gate_open = False
                final_text = text_piece
                if not gate_open and text_piece:
                    yield {"type": "delta", "text": text_piece}
                    gate_open = True
                panel_spec = combined_reply_ui_spec(text_piece)
                if panel_spec:
                    reply_ui_sent = True
                    yield {"type": "reply_ui", "spec": panel_spec}
                break

            missing_required_choice = bool(
                require_ui_choice and (not tool_calls or tries_to_generate)
            )
            if missing_required_choice:
                choice_failures += 1
                if gate_open:
                    if not defer_plan_decision:
                        yield {"type": "reset"}
                    gate_open = False
                if defer_plan_decision:
                    deferred_work_events.clear()
                    thinking_pending = []
                    thinking_visible = False
                if choice_failures >= 2:
                    if defer_plan_decision:
                        abandon_unstarted_plan()
                    final_text = contextual_choice_fallback(text_piece)
                    yield {"type": "delta", "text": final_text}
                    panel_spec = combined_reply_ui_spec(final_text)
                    if panel_spec:
                        reply_ui_sent = True
                        yield {"type": "reply_ui", "spec": panel_spec}
                    break
                if text_piece.strip():
                    convo.append({"role": "assistant", "content": text_piece})
                convo.append({
                    "role": "user",
                    "content": (
                        "СТОП: это творческая обработка приложенного кадра без "
                        "выбранной концепции. Ты не имеешь права вызывать "
                        "generate_image и не должен отвечать обычным списком. "
                        "Сейчас верни только короткий контекстный вопрос и один "
                        "```ui блок с `tiles Стиль: A | B | C`, опираясь на "
                        "реальные детали кадра. Затем остановись и дождись выбора."),
                })
                continue

            # Ответ на единую AGENT-панель закрывает preflight физически, а не
            # только формулировкой prompt. Если provider всё же пытается начать
            # второй раунд вопросов, не выпускаем этот текст в SSE и один раз
            # возвращаем модель к автономной работе. Повторное нарушение
            # завершается честной ошибкой, но никогда новой конфликтующей панелью.
            repeats_preflight = bool(
                self.agent_mode and preflight_resolved and
                (has_interactive_ui(text_piece) or needs_reply_ui(text_piece, user_text))
            )
            if repeats_preflight:
                if gate_open:
                    if not defer_plan_decision:
                        yield {"type": "reset"}
                    gate_open = False
                deferred_work_events.clear()
                thinking_pending = []
                thinking_visible = False
                if preflight_retry:
                    abandon_unstarted_plan()
                    final_text = ("Не удалось начать автономное выполнение: модель повторно "
                                  "запросила уже собранные параметры. Попробуй ещё раз — "
                                  "новая панель не была открыта.")
                    yield {"type": "delta", "text": final_text}
                    break
                preflight_retry = True
                convo.append({"role": "assistant", "content": text_piece})
                convo.append({"role": "system", "content":
                              "НАРУШЕНИЕ PREFLIGHT: пользователь уже ответил на единую "
                              "панель. Не задавай вопросов и не печатай ui. Прямо сейчас "
                              "вызови нужные инструменты и выполни исходную задачу; все "
                              "оставшиеся мелочи реши разумными допущениями."})
                continue

            # ОБЩИЙ HARD GATE ДЛЯ УТОЧНЕНИЙ. Prompt помогает модели выбрать
            # хороший control, но больше не является единственной защитой. Если
            # короткий ответ фактически просит реплику пользователя, обычный
            # текстовый вопрос дополняется рабочим ui-fence детерминированно.
            # В AGENT это также немедленно завершает run: нельзя продолжать план,
            # сделав вид, будто вопрос уже получил ответ.
            if not social_only and needs_reply_ui(text_piece, user_text):
                if defer_plan_decision:
                    abandon_unstarted_plan()
                    gate_open = False
                if not has_interactive_ui(text_piece):
                    panel = reply_ui_fallback(text_piece)
                    if panel:
                        addition = ("\n\n" if text_piece.strip() else "") + panel
                        text_piece += addition
                        if gate_open:
                            yield {"type": "delta", "text": addition}
                        else:
                            yield {"type": "delta", "text": text_piece}
                            gate_open = True
                    elif not gate_open:
                        # Свободный ответ человек напишет в composer. Обычный
                        # вопрос показываем, но второй input не создаём.
                        yield {"type": "delta", "text": text_piece}
                        gate_open = True
                elif not gate_open:
                    yield {"type": "delta", "text": text_piece}
                    gate_open = True
                # Отдельный UI-event нужен только настоящему выбору. Fence из
                # одного text/area намеренно игнорируется как дубль composer.
                panel_spec = combined_reply_ui_spec(text_piece)
                if panel_spec:
                    reply_ui_sent = True
                    yield {"type": "reply_ui", "spec": panel_spec}
                final_text = text_piece
                break

            # ask_user — не выполнение первого пункта, а запрос недостающих
            # данных. Если в ходе есть только такой tool, планировщик не
            # вызывается. После ответа реальный tool ещё должен доказать автономность.
            call_names = {
                (call.get("function") or {}).get("name", "") for call in tool_calls
            }
            authorized_names = call_names & allowed_tool_names
            autonomous_names = sorted(authorized_names - {"ask_user"})
            asks_only = bool("ask_user" in authorized_names and not autonomous_names)
            if defer_plan_decision and asks_only:
                # ask_user ставит работу на паузу. Никакого semantic-planner
                # вызова и никакой карточки пока нет; pending сохраняется, чтобы
                # после ответа первый реальный tool в ЭТОМ run доказал автономность.
                deferred_work_events.clear()
                thinking_pending = []
                thinking_visible = False
                gate_open = False
                if text_piece:
                    yield {"type": "delta", "text": text_piece}
                    gate_open = True

            # Доказательство автономности: основная модель выбрала хотя бы один
            # разрешённый non-question tool. Только здесь платим за semantic plan
            # и выпускаем его раньше накопленного reasoning/text/tool_hint.
            elif defer_plan_decision and autonomous_names:
                plan_pending = False
                plan = self.make_plan(user_text, autonomous_names)
                if plan:
                    for plan_event in announce_plan():
                        yield plan_event
                for work_event in deferred_work_events:
                    yield work_event
                deferred_work_events.clear()
                if text_piece:
                    yield {"type": "delta", "text": text_piece}
                    gate_open = True

            elif defer_plan_decision and not tool_calls:
                # Обычный ответ: planner не нужен, фиктивной карточки нет. При
                # этом длинное reasoning относится к самому ответу и не должно
                # исчезать вместе с pending-решением о плане.
                held_reasoning = list(deferred_work_events)
                abandon_unstarted_plan()
                for held_event in held_reasoning:
                    yield held_event

            # шлюз так и не открылся, а вызовов нет — показываем придержанный текст
            if not gate_open and text_piece and not tool_calls:
                yield {"type": "delta", "text": text_piece}
                gate_open = True

            if not tool_calls:
                # Текстовый результат закрывает оставшиеся фазы одним
                # естественным model turn. Сегменты всё равно проходят по порядку,
                # но мы больше не платим за пустые «перейди к шагу N» запросы.
                if self.plan_at and self.plan_at < self.plan_len:
                    for progress in self._advance_plan(self.plan_len):
                        yield progress
                final_text = text_piece
                break

            # Первый реальный вызов инструмента означает переход от разбора к
            # выполнению. Это честная граница второго сегмента без model turn.
            if self.plan_at and self.plan_at < min(2, self.plan_len):
                for progress in self._advance_plan(2):
                    yield progress

            # модель решила вызвать инструменты
            if from_text:
                # вызов был напечатан текстом: у модели нет полноценного tool-протокола,
                # поэтому результаты вернём обычным системным сообщением
                convo.append({"role": "assistant",
                              "content": text_piece or "Вызываю инструменты."})
            else:
                convo.append({
                    "role": "assistant",
                    "content": text_piece or None,
                    "tool_calls": tool_calls,
                })
            if text_piece.strip():
                final_text = text_piece

            for call in tool_calls:
                if self._cancelled():
                    return
                fn = call.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                if not isinstance(args, dict):
                    args = {}

                # Function-calling и распознавание текстовых вызовов сходятся
                # здесь. Никакой из этих путей не вправе обойти набор схем,
                # реально выданный модели в данном прогоне.
                if name not in allowed_tool_names:
                    self._append_tool_result(convo, call, name, {
                        "ok": False,
                        "error": "Этот инструмент недоступен в текущем диалоге.",
                    }, from_text)
                    continue

                # Модель может залипнуть, повторяя один и тот же вызов с теми же
                # аргументами. Для генерации повтор нельзя даже dispatch-ить:
                # это не только лишняя цена, но и второй файл в одном ответе.
                # Полная canonical JSON-строка: обрезка до 300 знаков делала два
                # разных длинных промпта «одним вызовом» и ошибочно съедала второй.
                sig = name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)
                if name == "generate_image" and sig in completed_calls:
                    self._append_tool_result(convo, call, name, completed_calls[sig], from_text)
                    continue
                seen_calls[sig] = seen_calls.get(sig, 0) + 1
                if seen_calls[sig] > 2:
                    self._append_tool_result(convo, call, name, {
                        "ok": False,
                        "error": "Этот вызов с теми же аргументами уже повторялся. "
                                 "Результат не изменится. Смени подход или дай ответ.",
                    }, from_text)
                    continue

                # Уточняющий вопрос исполняет сам агент: инструменту нужно
                # остановиться и дождаться нажатия кнопки, а не вернуть значение.
                if name == "ask_user":
                    options = [o.strip() for o in
                               str(args.get("options") or "").split("|") if o.strip()]
                    question = str(args.get("question") or "").strip()
                    if not question or len(options) < 2:
                        self._append_tool_result(convo, call, name, {
                            "ok": False,
                            "error": "нужен непустой question и минимум два варианта "
                                     "в options через |",
                        }, from_text)
                        continue
                    self.used_tools.append(name)
                    record = self._wait_answer(question, options[:5])
                    yield {"type": "question", "id": record["id"],
                           "question": question, "options": options[:5],
                           "answer": record.get("answer", ""),
                           "status": record.get("status")}
                    answered = record.get("status") == "answered"
                    self._append_tool_result(convo, call, name, {
                        "ok": answered,
                        "answer": record.get("answer", ""),
                    } if answered else {
                        "ok": False,
                        "error": "Пользователь не ответил. Действуй по самому "
                                 "разумному варианту и скажи, какой выбрал.",
                    }, from_text)
                    continue

                self.used_tools.append(name)
                yield {"type": "tool_start", "id": call.get("id"), "name": name,
                       "label": tools.label_of(name), "args": args,
                       "group": tools.group_of(name),
                       "risk": tools.risk_of(name)}

                external_without_computer = bool(
                    not self.computer_use and opens_external_ui(name, args))
                reason = needs_approval(name, args, computer_use=self.computer_use)
                # approvals_auto используется у headless AUTO для обычных
                # серверных шагов, но не является тайным разрешением выводить
                # GUI на Mac. Внешнее окно без включённого «Компьютера» всегда
                # проходит через видимый вопрос до dispatch.
                if reason and (external_without_computer or not self.approvals_auto):
                    style = approval_style(name, args, computer_use=self.computer_use)
                    yield {"type": "status", "text": "Жду твоего разрешения"
                           if style == "permission" else "Жду твоего подтверждения"}
                    yield {"type": "approval_wait", "tool": name, "label": tools.label_of(name),
                           "args": args, "reason": reason, "style": style}
                    decision = self._wait_approval(name, args, reason, style=style)
                    yield {"type": "approval_done", "status": decision.get("status")}
                    if decision.get("status") != "approved":
                        result: Dict[str, Any] = {
                            "ok": False,
                            "error": "Пользователь отклонил действие" if decision.get("status") == "rejected"
                            else "Время ожидания подтверждения истекло",
                        }
                        self._append_tool_result(convo, call, name, result, from_text)
                        yield {"type": "tool_result", "id": call.get("id"), "name": name, "result": result}
                        continue

                started = time.time()
                result = tools.call(name, args)

                # Генерация изображения теперь целиком выполняется backend-инструментом
                # через официальный GigaChat API. Поэтому file/tool_result не могут
                # обогнать скачивание и сохранение JPG даже в AUTO-задаче.
                elapsed = round(time.time() - started, 2)
                if self._cancelled():
                    return

                if (name == "generate_image" and isinstance(result, dict) and result.get("ok")):
                    completed_calls[sig] = dict(result)

                if isinstance(result, dict) and result.get("download_url"):
                    file_info = {"name": result.get("path") or result.get("name"),
                                 "url": result["download_url"],
                                 "size": result.get("size", 0),
                                 "kind": "image" if str(result.get("path", "")).lower().endswith(
                                     (".png", ".jpg", ".jpeg", ".gif", ".webp")) else "file"}
                    self.created_files.append(file_info)
                    yield {"type": "file", **file_info}

                yield {"type": "tool_result", "id": call.get("id"), "name": name,
                       "result": result, "elapsed": elapsed}

                self._append_tool_result(convo, call, name, result, from_text)

            # Следующий естественный ход модели получит результаты этих
            # инструментов и переведёт UI к фазе проверки в начале цикла.

        if self._cancelled():
            return

        if not final_text:
            if plan and not plan_announced:
                for plan_event in announce_plan():
                    yield plan_event
                for work_event in deferred_work_events:
                    yield work_event
                deferred_work_events.clear()
            if self.plan_at and self.plan_at < self.plan_len:
                for progress in self._advance_plan(self.plan_len):
                    yield progress
            yield {"type": "status", "text": "Формулирую ответ"}
            try:
                closing = llm.chat(convo + [{
                    "role": "user",
                    "content": "Подведи итог выполненной работы для пользователя: что сделано и результат. "
                               "Кратко, markdown, по-русски. Не печатай вызовы инструментов.",
                }], tier=tier, max_tokens=1400,
                   operation="auto_final" if self.task_id else "foreground_final")
                final_text = closing.get("content", "")
            except Exception as exc:
                yield {"type": "error", "error": str(exc)}
                return
            # итог тоже может прийти с напечатанным вызовом — вычищаем
            if final_text:
                final_text = tools.parse_text_calls(final_text)[0]
            if final_text:
                yield {"type": "delta", "text": final_text}

        # последняя страховка: пустой ответ пользователь видит как поломку
        if not final_text.strip():
            final_text = self._fallback_summary()
            yield {"type": "delta", "text": final_text}

        # Режим управления компьютером: модель могла «отчитаться» о кликах, не
        # тронув мышь. Не выдаём выдумку за правду — честно предупреждаем.
        # Предупреждаем, если в режиме управления компьютером не выполнено ни
        # одного действия И модель уже проигнорировала прямое требование их
        # выполнить (retried_claim). Опираться на список глаголов нельзя —
        # «нажму» / «нажал» / «щёлкну» не перечислить полностью.
        if self.computer_use and not any(t in COMPUTER_TOOLS for t in self.used_tools):
            if retried_claim or _claims_action(final_text):
                final_text += (
                    "\n\n---\n⚠️ **Я на самом деле ничего не нажал.** Управление "
                    "компьютером не сработало: инструменты мыши и клавиатуры не "
                    "выполнились.\n\nНа macOS это почти всегда права доступа. Открой "
                    "**Системные настройки → Конфиденциальность и безопасность** и "
                    "разреши Терминалу (или приложению, из которого запущен JARVIS) "
                    "два пункта: **Универсальный доступ** и **Запись экрана**. "
                    "После этого перезапусти JARVIS и повтори просьбу."
                )
                yield {"type": "delta", "text": final_text[final_text.index("\n\n---\n"):]}

        # Writer-boundary для live interactive: какой бы веткой ни завершился
        # ответ (обычный turn, fallback или closing после tools), reply_ui обязан
        # попасть в этот же SSE ДО done. Это не зависит от повторной загрузки
        # истории и не заставляет фронтенд угадывать fence из финального текста.
        if not reply_ui_sent:
            panel_spec = combined_reply_ui_spec(final_text)
            if panel_spec:
                reply_ui_sent = True
                yield {"type": "reply_ui", "spec": panel_spec}

        # Варианты продолжения СЮДА НЕ ВХОДЯТ. Раньше они считались прямо здесь,
        # и пользователь ждал ещё один запрос к модели уже после готового ответа:
        # ответ дописан, а поток не закрыт и кнопка «стоп» продолжает гореть.
        # Теперь ответ завершается немедленно, а подсказки браузер запрашивает
        # отдельно (/api/replies) — они не могут задержать или сорвать ответ.
        yield {"type": "done", "content": final_text, "files": self.created_files,
               "tools": self.used_tools, "model": self.model_used, "tier": tier}

    def _fallback_summary(self) -> str:
        """Что показать, если модель не выдала ни слова."""
        parts = []
        if self.used_tools:
            names = ", ".join(dict.fromkeys(self.used_tools))
            parts.append("Готово. Что я сделал: %s." % names)
        if self.created_files:
            files = ", ".join(f.get("name", "") for f in self.created_files if f.get("name"))
            if files:
                parts.append("Файлы: %s — их можно скачать выше." % files)
        if not parts:
            parts.append("Я обработал запрос, но модель вернула пустой ответ. "
                         "Повтори вопрос — попробую другой моделью.")
        return "\n\n".join(parts)


def run_headless(prompt: str, task_id: str = "", agent_mode: bool = True,
                 chat_id: str = "",
                 cancel_check: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Запуск без UI (для фоновых задач AUTO). Возвращает итог и лог событий."""
    agent = Agent(chat_id=chat_id, task_id=task_id, agent_mode=agent_mode,
                  approvals_auto=False, visible_plan=False, cancel_check=cancel_check)
    # Фоновая задача исполняется «сейчас»: время ожидания уже прошло, поэтому
    # никаких «напомню позже» — нужен готовый текст, который увидит пользователь.
    extra = (
        "\n\nСЕЙЧАС ТЫ ВЫПОЛНЯЕШЬ ОТЛОЖЕННУЮ ЗАДАЧУ.\n"
        "Назначенный момент наступил — выполняй прямо сейчас.\n"
        "Не планируй задачу заново и не пиши, что напомнишь позже.\n"
        "Если просили что-то написать или напомнить — просто напиши это "
        "готовым текстом, обращаясь к пользователю.\n"
        "Ответ попадёт в диалог и в уведомление, поэтому он должен быть "
        "самодостаточным и по делу."
    )
    messages = [
        {"role": "system", "content": build_system_prompt(agent_mode=agent_mode) + extra},
        {"role": "user", "content": prompt},
    ]
    events: List[Dict[str, Any]] = []
    final = ""
    files: List[Dict[str, Any]] = []
    for event in agent.run(messages, user_text=prompt):
        etype = event.get("type")
        if etype in ("plan", "tool_start", "tool_result", "status", "file", "error", "model"):
            slim = {k: v for k, v in event.items() if k != "args"}
            if etype == "tool_result":
                res = slim.get("result")
                slim["result"] = (json.dumps(res, ensure_ascii=False)[:600] if isinstance(res, dict) else str(res)[:600])
            events.append(slim)
            if task_id:
                db.append_task_event(task_id, slim)
        if etype == "done":
            final = event.get("content", "")
            files = event.get("files", [])
    return {"content": final, "events": events, "files": files}

"""Оркестратор: незаметно выбирает самую дешёвую подходящую модель.

Логика: быстрая эвристика по тексту запроса → уровень (nano/base/smart/coder/vision).
Если эвристика не уверена — спрашиваем самую дешёвую модель-классификатор.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .config import CONFIG

TIER_ORDER = ["nano", "base", "smart", "coder", "vision"]

_SIMPLE_PATTERNS = [
    r"^\s*(привет|здравствуй|хай|йо|как дела|спасибо|пока|ок|окей|да|нет|ага)\b",
    r"^\s*(сколько времени|который час|какое сегодня число|какой сегодня день)",
    r"^\s*(переведи|переведи на|как будет)\s",
]
_HARD_KEYWORDS = [
    "проанализируй", "сравни", "стратегия", "план", "исследуй", "разбери подробно",
    "почему", "докажи", "оптимизируй", "архитектур", "спроектируй", "реши задачу",
    "рассчитай", "финанс", "юридич", "диссерт", "научн", "алгоритм", "многошаг",
]
_CODE_KEYWORDS = [
    "код", "напиши скрипт", "python", "javascript", "html", "css", "sql", "баг",
    "ошибка в коде", "функци", "программ", "регулярк", "bash", "терминал", "git",
]
_AGENT_KEYWORDS = [
    "найди", "собери", "сделай", "оформи", "скачай", "открой сайт", "зайди на",
    "проверь цены", "закажи", "купи", "отправь", "напиши другу", "мониторь",
    "каждый день", "напомни", "следи за",
]

# Запросы, которые НЕВОЗМОЖНО выполнить без похода в интернет. Даже если
# фраза короткая («новости за сегодня»), слабая модель тут бесполезна:
# инструменты она не вызовет и просто скажет «у меня нет доступа».
_NEEDS_TOOLS = [
    "новост", "погод", "курс ", "курс валют", "доллар", "евро", "биткоин",
    "цена", "цены", "сколько стоит", "расписание", "афиш", "premiere",
    "что нового", "за сегодня", "сегодня в мире", "последние событ",
    "актуальн", "свеж", "сейчас происходит", "результат матч", "счёт матча",
    "пробки", "рейс", "билет", "акци", "котировк", "прогноз",
]


def needs_live_data(text: str) -> bool:
    """Нужен ли живой интернет: такие запросы нельзя отдавать слабой модели."""
    t = (text or "").lower()
    return any(kw in t for kw in _NEEDS_TOOLS)


def _score_complexity(text: str) -> float:
    t = (text or "").lower().strip()
    words = len(t.split())
    score = 0.0
    # длина: 40 слов ≈ 0.2, 120 слов ≈ 0.45, дальше насыщение
    score += min(words / 250.0, 1.0) * 0.5
    if any(re.search(p, t) for p in _SIMPLE_PATTERNS) and len(t) < 90:
        score -= 0.6
    hard = sum(1 for kw in _HARD_KEYWORDS if kw in t)
    score += min(hard, 4) * 0.16
    code = sum(1 for kw in _CODE_KEYWORDS if kw in t)
    score += min(code, 3) * 0.1
    agentic = sum(1 for kw in _AGENT_KEYWORDS if kw in t)
    score += min(agentic, 3) * 0.07
    if t.count("?") > 1 or t.count("\n") > 3:
        score += 0.12
    # перечисления и уточнения = многосоставная задача
    score += min(t.count(",") / 12.0, 0.15)
    if words > 120:
        score += 0.15
    return max(0.0, min(score, 1.5))


def is_code_task(text: str) -> bool:
    t = (text or "").lower()
    return any(kw in t for kw in _CODE_KEYWORDS)


def looks_agentic(text: str) -> bool:
    t = (text or "").lower()
    return any(kw in t for kw in _AGENT_KEYWORDS)


def choose_tier(text: str, has_image: bool = False, agent_mode: bool = False,
                has_tools: bool = False, computer_use: bool = False) -> Dict[str, Any]:
    """Возвращает {tier, reason, score}."""
    forced = CONFIG.get("orchestrator.force_tier") or ""
    if forced:
        return {"tier": forced, "reason": "принудительно в настройках", "score": 1.0}
    if not CONFIG.get("orchestrator.auto_route", True):
        return {"tier": "base", "reason": "авто-маршрутизация выключена", "score": 0.5}
    if computer_use:
        # НЕ переключаемся на vision-модель: она не умеет вызывать инструменты,
        # и агент превращается в болтуна. Экран ей покажет отдельный вызов
        # (см. agent._describe_screen), а рулит процессом tool-capable модель.
        return {"tier": "smart", "reason": "управление компьютером — нужен точный вызов действий",
                "score": 1.0}
    if has_image:
        return {"tier": "vision", "reason": "во вложении изображение — нужна vision-модель", "score": 1.0}

    score = _score_complexity(text)
    if is_code_task(text) and score > 0.35:
        return {"tier": "coder", "reason": "задача про код", "score": score}
    if agent_mode:
        # агентский цикл требует надёжного tool-calling
        tier = "smart" if score > 0.75 else "base"
        return {"tier": tier, "reason": "агентский режим с инструментами", "score": score}
    if needs_live_data(text):
        # нужен реальный поиск в сети → только модель, которая уверенно
        # вызывает инструменты. Иначе получаем «у меня нет доступа к интернету».
        return {"tier": "base", "reason": "нужны свежие данные из интернета", "score": max(score, 0.4)}
    if has_tools:
        # болтовню не тащим в дорогую модель, даже если инструменты подключены
        if score < 0.12 and not looks_agentic(text):
            return {"tier": "nano", "reason": "простая реплика — экономим", "score": score}
        tier = "smart" if score > 0.75 else "base"
        return {"tier": tier, "reason": "нужны инструменты", "score": score}
    if score < 0.22:
        return {"tier": "nano", "reason": "простой короткий запрос — экономим", "score": score}
    if score < 0.75:
        return {"tier": "base", "reason": "обычная задача", "score": score}
    return {"tier": "smart", "reason": "сложная задача — берём сильную модель", "score": score}


def escalate(tier: str) -> Optional[str]:
    """Повышение уровня при неудаче/плохом ответе."""
    ladder = {"nano": "base", "base": "smart", "coder": "smart", "vision": "smart", "smart": None}
    return ladder.get(tier)


def summarize_history(messages: List[Dict[str, Any]], keep_last: int = 12) -> List[Dict[str, Any]]:
    """Экономия токенов: старые сообщения сжимаются дешёвой моделью."""
    if len(messages) <= keep_last + 4:
        return messages
    from . import llm  # локальный импорт, чтобы избежать циклов

    head = messages[:-keep_last]
    tail = messages[-keep_last:]
    text = "\n".join("%s: %s" % (m.get("role"), str(m.get("content"))[:600]) for m in head)
    try:
        summary = llm.chat(
            [
                {"role": "system", "content": "Сожми диалог в 10 фактов-тезисов на русском. Только факты, кратко."},
                {"role": "user", "content": text[:12000]},
            ],
            tier="nano", max_tokens=500, temperature=0.2,
        ).get("content", "")
    except Exception:
        summary = ""
    if not summary:
        return tail
    return [{"role": "system", "content": "Краткая память о предыдущей части диалога:\n" + summary}] + tail


# ------------------------------------------------------------- имя диалога
_TITLE_STOP = re.compile(r"^[\s\"'«»`*#>\-–—.:]+|[\s\"'«»`*#>\-–—.:]+$")

# такие «названия» бессмысленны — их не принимаем ни от модели, ни как фолбэк
_BAD_TITLES = {
    "новый диалог", "новый чат", "диалог", "чат", "беседа", "разговор", "без названия",
    "название", "заголовок", "тема", "новая тема", "запрос", "вопрос", "сообщение",
    "new chat", "new dialog", "untitled", "conversation", "chat", "title",
    "привет", "здравствуйте", "ответ", "текст", "задача", "разное", "общение",
}
_STOPWORDS = {
    "а", "бы", "в", "во", "все", "вот", "да", "для", "до", "его", "ее", "её", "если",
    "есть", "ещё", "еще", "же", "за", "и", "из", "или", "как", "мне", "мной", "мы",
    "на", "над", "не", "нет", "но", "о", "об", "она", "они", "от", "по", "под",
    "пожалуйста", "при", "про", "с", "со", "так", "также", "те", "то", "ты", "у",
    "уже", "что", "чтобы", "это", "я", "мой", "моя", "меня", "нам", "вы", "ваш",
    "такое", "такой", "такая", "мне", "нужно", "надо", "хочу", "давай", "можешь",
}
_VERB_HINTS = ("напиши", "сделай", "создай", "найди", "посчитай", "собери", "объясни",
               "переведи", "проверь", "покажи", "расскажи", "составь", "подбери",
               "сравни", "проанализируй", "помоги", "придумай", "оформи", "скачай")


def _is_bad_title(title: str) -> bool:
    t = re.sub(r"[^\wа-яё ]+", "", (title or "").lower()).strip()
    if not t or len(t) < 3:
        return True
    if t in _BAD_TITLES:
        return True
    # «Новый диалог 2», «Чат №3»
    if re.fullmatch(r"(новый диалог|новый чат|чат|диалог|беседа)\s*[№#]?\s*\d*", t):
        return True
    return False


def _keyword_title(text: str) -> str:
    """Осмысленный заголовок из ключевых слов, если модель не помогла."""
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return ""
    greet = ("джарвис", "jarvis", "привет", "здравствуй", "здравствуйте", "слушай",
             "эй", "пожалуйста", "окей", "ок", "плиз", "будь", "добр", "доброе", "утро",
             "добрый", "день", "вечер")
    # первая фраза; если она — только приветствие, берём следующую
    sentences = [s.strip() for s in re.split(r"[.!?\n]", clean) if s.strip()]
    first = clean
    for sent in sentences:
        rest = [w for w in re.findall(r"[\wА-Яа-яЁё\-\+#\.]+", sent)
                if w.lower().strip(",.!") not in greet]
        if rest:
            first = sent
            break
    words = re.findall(r"[\wА-Яа-яЁё\-\+#\.]+", first)
    while words and words[0].lower().strip(",.!") in greet:
        words.pop(0)
    if not words:
        words = re.findall(r"[\wА-Яа-яЁё\-\+#\.]+", first)
    keep = [w for w in words if w.lower() not in _STOPWORDS]
    if not keep:
        keep = words
    picked = keep[:6]
    title = " ".join(picked)
    if len(title) > 38:
        out: List[str] = []
        for w in picked:
            if len(" ".join(out + [w])) > 38:
                break
            out.append(w)
        title = " ".join(out) or title[:38]
    title = title.strip(" ,;:—-")
    if not title:
        return ""
    return title[0].upper() + title[1:]


def _fallback_title(text: str) -> str:
    """Если модель недоступна — собираем заголовок из ключевых слов."""
    title = _keyword_title(text)
    if title and not _is_bad_title(title):
        return title[:40]
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return "Диалог " + __import__("time").strftime("%d.%m %H:%M")
    return clean[:38].rstrip(" ,;:-") or "Диалог"


def _clean_model_title(raw: str) -> str:
    title = (raw or "").split("\n")[0]
    title = re.sub(r"^\s*(название|заголовок|title)\s*[:\-—]\s*", "", title, flags=re.I)
    title = _TITLE_STOP.sub("", title).strip()
    title = re.sub(r"\s+", " ", title)
    return title


def _make_title(text: str, kind: str = "chat") -> str:
    """Название придумывает сама модель — коротко и по смыслу."""
    from . import llm  # локальный импорт, чтобы избежать циклов

    snippet = re.sub(r"\s+", " ", (text or "").strip())[:900]
    if not snippet:
        return _fallback_title(text)

    what = ("названия диалогов" if kind == "chat" else "названия фоновых задач")
    system = (
        "Ты придумываешь %s. По сообщению пользователя дай короткое название на русском.\n"
        "ПРАВИЛА:\n"
        "1. 2-5 слов, до 34 символов, по сути темы — чтобы через неделю было понятно, о чём речь.\n"
        "2. Без кавычек, без точки в конце, без эмодзи, без пояснений.\n"
        "3. ЗАПРЕЩЕНО отвечать общими словами: «Новый диалог», «Чат», «Диалог», «Беседа», "
        "«Вопрос», «Запрос», «Без названия», «Разное», «Общение», «Тема».\n"
        "4. Если тема непонятна — назови её по ключевым словам сообщения.\n"
        "5. Ответь ТОЛЬКО названием, одной строкой." % what
    )
    for attempt in range(2):
        try:
            raw = llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": snippet},
                ],
                tier="nano", max_tokens=24, temperature=0.2 if attempt == 0 else 0.7,
            ).get("content", "")
        except Exception:
            break
        title = _clean_model_title(raw)
        if title and len(title) <= 48 and not _is_bad_title(title):
            return title[:40]
    return _fallback_title(text)


def make_chat_title(text: str) -> str:
    return _make_title(text, "chat")


def make_task_title(text: str) -> str:
    title = _make_title(text, "task")
    return title or (text[:48] or "Фоновая задача")

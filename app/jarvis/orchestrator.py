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
                has_tools: bool = False) -> Dict[str, Any]:
    """Возвращает {tier, reason, score}."""
    forced = CONFIG.get("orchestrator.force_tier") or ""
    if forced:
        return {"tier": forced, "reason": "принудительно в настройках", "score": 1.0}
    if not CONFIG.get("orchestrator.auto_route", True):
        return {"tier": "base", "reason": "авто-маршрутизация выключена", "score": 0.5}
    if has_image:
        return {"tier": "vision", "reason": "во вложении изображение — нужна vision-модель", "score": 1.0}

    score = _score_complexity(text)
    if is_code_task(text) and score > 0.35:
        return {"tier": "coder", "reason": "задача про код", "score": score}
    if agent_mode:
        # агентский цикл требует надёжного tool-calling
        tier = "smart" if score > 0.75 else "base"
        return {"tier": tier, "reason": "агентский режим с инструментами", "score": score}
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
_TITLE_STOP = re.compile(r"^[\s\"'«»`*#>\-–—.]+|[\s\"'«»`*#>\-–—.]+$")


def _fallback_title(text: str) -> str:
    """Если модель недоступна — аккуратно подрезаем первую фразу."""
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return "Новый диалог"
    first = re.split(r"[.!?\n]", clean)[0].strip() or clean
    if len(first) > 38:
        cut = first[:38].rsplit(" ", 1)[0]
        first = (cut or first[:38]).rstrip(",;:-") + "…"
    return first[:40]


def make_chat_title(text: str) -> str:
    """Название диалога придумывает сама модель — коротко и по смыслу."""
    from . import llm  # локальный импорт, чтобы избежать циклов

    snippet = re.sub(r"\s+", " ", (text or "").strip())[:900]
    if not snippet:
        return "Новый диалог"
    try:
        raw = llm.chat(
            [
                {"role": "system", "content":
                 "Ты придумываешь названия диалогов. По первому сообщению пользователя дай короткое "
                 "название на русском: 2-4 слова, до 32 символов, суть темы, без кавычек, без точки "
                 "в конце, без слов «запрос», «вопрос», «диалог». Ответь ТОЛЬКО названием."},
                {"role": "user", "content": snippet},
            ],
            tier="nano", max_tokens=24, temperature=0.3,
        ).get("content", "")
    except Exception:
        return _fallback_title(text)
    title = _TITLE_STOP.sub("", (raw or "").split("\n")[0]).strip()
    if not title or len(title) > 48:
        return _fallback_title(text)
    return title[:40]

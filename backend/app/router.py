"""Оркестратор: сам выбирает модель под сложность задачи и переключается при сбое.

Логика:
  1. Классифицируем задачу: light / medium / heavy (эвристика + длина контекста).
  2. Берём первого доступного провайдера в порядке приоритета (GigaChat → Ollama → OpenRouter → custom).
  3. Если провайдер упал — молча падаем на следующий, пользователь ничего не замечает.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from . import config, events, store
from .providers.base import LLMResult, ProviderError
from .providers.gigachat import MODELS as GIGA_MODELS, GigaChatProvider
from .providers import openai_compat

_giga = GigaChatProvider()
_ollama = openai_compat.ollama()
_openrouter = openai_compat.openrouter()
_custom = openai_compat.custom()

HEAVY_HINTS = re.compile(
    r"(проанализируй|стратег|исследуй|сравни|разработай|спланируй|напиши код|"
    r"алгоритм|доказательств|оптимизируй|architect|research|подробн\w+ отчёт)", re.I)
LIGHT_HINTS = re.compile(
    r"^(привет|спасибо|ок|да|нет|как дела|который час|время|погода|hi|hello)\b", re.I)


def classify(prompt: str, context_len: int = 0, has_tools: bool = False) -> str:
    """Определяем сложность запроса."""
    p = (prompt or "").strip()
    if len(p) < 40 and LIGHT_HINTS.search(p):
        return "light"
    if HEAVY_HINTS.search(p) or len(p) > 900 or context_len > 12000:
        return "heavy"
    if has_tools or len(p) > 200:
        return "medium"
    return "light"


async def _providers_in_order() -> List[Tuple[str, Any]]:
    order: List[Tuple[str, Any]] = []
    if await _giga.available():
        order.append(("gigachat", _giga))
    if await _ollama.available():
        order.append(("ollama", _ollama))
    if await _openrouter.available():
        order.append(("openrouter", _openrouter))
    if await _custom.available():
        order.append(("custom", _custom))
    return order


def _model_for(pname: str, provider: Any, tier: str) -> str:
    override = config.get(f"route_{tier}") or "auto"
    if override and override != "auto" and ":" in override:
        p, m = override.split(":", 1)
        if p == pname:
            return m
    if pname == "gigachat":
        return GIGA_MODELS.get(tier, GIGA_MODELS["light"])
    if hasattr(provider, "model"):
        return provider.model(tier)
    return ""


async def complete(messages: List[Dict[str, Any]], *, tools=None, tier: str = "",
                   temperature: float = 0.4, max_tokens: int = 1500,
                   task_id: str = "") -> LLMResult:
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = str(m.get("content", ""))
            break
    ctx_len = sum(len(str(m.get("content", ""))) for m in messages)
    tier = tier or (classify(last_user, ctx_len, bool(tools))
                    if config.get("auto_route") else "medium")

    chain = await _providers_in_order()
    if not chain:
        raise ProviderError(
            "Не подключена ни одна модель. Откройте «Настройки» и вставьте ключ GigaChat "
            "(бесплатно, работает в РФ) — или запустите Ollama локально.")

    errors = []
    for pname, provider in chain:
        model = _model_for(pname, provider, tier)
        events.publish("router", provider=pname, model=model, tier=tier, task_id=task_id)
        try:
            res = await provider.chat(messages, tools=tools, temperature=temperature,
                                      max_tokens=max_tokens, model=model)
            store.log_usage(pname, res.model or model, res.tokens_in, res.tokens_out, tier)
            return res
        except Exception as e:  # падаем на следующего провайдера
            errors.append(f"{pname}: {e}")
            events.publish("router_fallback", provider=pname, error=str(e)[:200])
            continue
    raise ProviderError("Все модели недоступны. " + " | ".join(errors)[:500])


async def status() -> Dict[str, Any]:
    chain = await _providers_in_order()
    return {
        "active": [p for p, _ in chain],
        "primary": chain[0][0] if chain else None,
        "gigachat": await _giga.available(),
        "ollama": await _ollama.available(),
        "openrouter": await _openrouter.available(),
        "custom": await _custom.available(),
        "usage": store.usage_summary(),
    }


def gigachat() -> GigaChatProvider:
    return _giga

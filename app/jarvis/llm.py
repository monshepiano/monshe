"""OpenAI-совместимый клиент без внешних зависимостей (urllib).

Поддерживает: Cloud.ru Foundation Models (основной) и DeepSeek (резерв).
Стриминг SSE, function calling, vision (image_url), список моделей.
"""
from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Generator, List, Optional, Tuple

from .config import CONFIG
from . import db

_SSL_CTX = ssl.create_default_context()
_MODELS_CACHE: Dict[str, Tuple[float, List[str]]] = {}
_META_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_CACHE_LOCK = threading.RLock()

# Ориентировочные цены (₽ за 1 млн токенов) — для счётчика расходов в UI.
PRICES_RUB = {
    "ai-sage/GigaChat3-10B-A1.8B": (12.2, 12.2),
    "openai/gpt-oss-120b": (15.86, 61.0),
    "openai/gpt-oss-20b": (7.0, 25.0),
    "Qwen/Qwen3-30B-A3B": (13.9, 55.6),
    "Qwen/Qwen3.6-35B-A3B": (219.6, 329.4),
    "zai-org/GLM-4.7": (549.0, 793.0),
    "MiniMaxAI/MiniMax-M2.5": (353.8, 475.8),
    "Qwen/Qwen3-Coder-Next": (122.0, 244.0),
    "Qwen/Qwen3-VL-8B-Instruct": (30.7, 119.6),
    "deepseek-chat": (25.0, 100.0),
}
DEFAULT_PRICE = (30.0, 90.0)


class LLMError(Exception):
    pass


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "JARVIS/1.0",
    }


def _request(url: str, api_key: str, payload: Optional[Dict] = None, method: str = "POST", timeout: int = 180):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(api_key), method=method)
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)


def provider_conf(name: str) -> Dict[str, Any]:
    return CONFIG.get("providers." + name, {}) or {}


def active_providers() -> List[str]:
    out = []
    for name in ("cloudru", "deepseek"):
        conf = provider_conf(name)
        if conf.get("enabled") and conf.get("api_key"):
            out.append(name)
    return out


def list_models_meta(provider: str, force: bool = False) -> List[Dict[str, Any]]:
    """Каталог моделей ЦЕЛИКОМ, вместе с метаданными провайдера.

    Раньше мы сохраняли только идентификаторы и потом угадывали умения модели
    по её названию. Но провайдер сам сообщает тип модели в metadata.type
    (text-to-text, audio-to-text, image-text-to-text...). Это факт, а не догадка,
    поэтому храним каталог как есть.
    """
    with _CACHE_LOCK:
        cached = _META_CACHE.get(provider)
        if cached and not force and time.time() - cached[0] < 600:
            return cached[1]
    conf = provider_conf(provider)
    if not conf.get("api_key"):
        return []
    try:
        with _request(conf["base_url"].rstrip("/") + "/models", conf["api_key"], None, "GET", timeout=25) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        items = [m for m in body.get("data", []) if m.get("id")]
    except Exception:
        items = []
    with _CACHE_LOCK:
        _META_CACHE[provider] = (time.time(), items)
        _MODELS_CACHE[provider] = (time.time(), [m["id"] for m in items])
    return items


def model_type(item: Dict[str, Any]) -> str:
    """Тип модели так, как его называет сам провайдер (пустая строка — не сказал)."""
    meta = item.get("metadata") or {}
    return str(meta.get("type") or item.get("type") or "").lower()


def models_of_type(provider: str, *needles: str) -> List[str]:
    """Модели, ТИП которых (по данным провайдера) содержит одну из подстрок."""
    out = []
    for item in list_models_meta(provider):
        kind = model_type(item)
        if kind and any(n in kind for n in needles):
            out.append(item["id"])
    return out


def list_models(provider: str, force: bool = False) -> List[str]:
    """Список моделей провайдера с кэшем на 10 минут."""
    with _CACHE_LOCK:
        cached = _MODELS_CACHE.get(provider)
        if cached and not force and time.time() - cached[0] < 600:
            return cached[1]
    models = [m["id"] for m in list_models_meta(provider, force=force)]
    with _CACHE_LOCK:
        _MODELS_CACHE[provider] = (time.time(), models)
    return models


def pick_model(tier: str, provider: str = "cloudru") -> str:
    """Выбирает конкретное имя модели под «уровень» из доступных у провайдера."""
    prefs = CONFIG.get("model_tiers." + tier, []) or []
    available = list_models(provider)
    if not available:
        # провайдер не ответил — берём первое предпочтение как есть
        return prefs[0] if prefs else ("deepseek-chat" if provider == "deepseek" else "openai/gpt-oss-120b")
    lowered = {m.lower(): m for m in available}
    for pref in prefs:
        if pref in available:
            return pref
        p = pref.lower()
        if p in lowered:
            return lowered[p]
        for low, orig in lowered.items():
            if p in low or low in p:
                return orig
    # Ничего не совпало. Дальше решает НЕ название, а тип модели из каталога
    # провайдера: «image-text-to-text» — это зрение, и это факт, а не догадка.
    if tier == "vision":
        seeing = models_of_type(provider, "image-text-to-text", "image-to-text", "multimodal")
        if seeing:
            return seeing[0]
        for m in available:
            if "-vl" in m.lower() or "vision" in m.lower():
                return m
    return available[0]


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price_in, price_out = PRICES_RUB.get(model, DEFAULT_PRICE)
    return prompt_tokens / 1e6 * price_in + completion_tokens / 1e6 * price_out


def _build_payload(model: str, messages: List[Dict], tools: Optional[List[Dict]], stream: bool,
                   temperature: Optional[float], max_tokens: Optional[int]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "temperature": CONFIG.get("orchestrator.temperature", 0.6) if temperature is None else temperature,
        "max_tokens": max_tokens or CONFIG.get("orchestrator.max_output_tokens", 2400),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return payload


def chat(messages: List[Dict], tier: str = "base", tools: Optional[List[Dict]] = None,
         temperature: Optional[float] = None, max_tokens: Optional[int] = None,
         provider: Optional[str] = None) -> Dict[str, Any]:
    """Не-стриминговый вызов с автоматическим фолбэком на резервного провайдера."""
    providers = [provider] if provider else (active_providers() or ["cloudru"])
    last_error: Optional[Exception] = None
    for prov in providers:
        conf = provider_conf(prov)
        if not conf.get("api_key"):
            continue
        model = pick_model(tier, prov)
        payload = _build_payload(model, messages, tools, False, temperature, max_tokens)
        for attempt in range(2):
            try:
                with _request(conf["base_url"].rstrip("/") + "/chat/completions", conf["api_key"], payload) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                usage = body.get("usage") or {}
                pt = int(usage.get("prompt_tokens") or 0)
                ct = int(usage.get("completion_tokens") or 0)
                db.log_usage(prov, model, tier, pt, ct, estimate_cost(model, pt, ct))
                choice = (body.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                return {
                    "content": message.get("content") or "",
                    "tool_calls": message.get("tool_calls") or [],
                    "reasoning": message.get("reasoning_content") or "",
                    "model": model,
                    "provider": prov,
                    "usage": usage,
                }
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")[:400]
                except Exception:
                    pass
                last_error = LLMError("HTTP %s %s: %s" % (exc.code, model, detail))
                if exc.code in (400, 404, 422) and tools:
                    # модель не умеет tools — пробуем без них
                    payload.pop("tools", None)
                    payload.pop("tool_choice", None)
                    continue
                break
            except Exception as exc:  # сеть/таймаут
                last_error = exc
                time.sleep(1.2)
    raise LLMError("Не удалось получить ответ от моделей: %s" % last_error)


def chat_stream(messages: List[Dict], tier: str = "base", tools: Optional[List[Dict]] = None,
                temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                provider: Optional[str] = None) -> Generator[Dict[str, Any], None, None]:
    """Стриминг. Отдаёт словари: {type: delta|reasoning|tool_calls|done|error}."""
    providers = [provider] if provider else (active_providers() or ["cloudru"])
    last_error: Optional[Exception] = None
    for prov in providers:
        conf = provider_conf(prov)
        if not conf.get("api_key"):
            continue
        model = pick_model(tier, prov)
        payload = _build_payload(model, messages, tools, True, temperature, max_tokens)
        # попытка 1 — с инструментами; попытка 2 — без них (если модель их не умеет)
        for attempt in range(2):
            started_output = False
            try:
                with _request(conf["base_url"].rstrip("/") + "/chat/completions", conf["api_key"], payload) as resp:
                    acc_content: List[str] = []
                    acc_reasoning: List[str] = []
                    tool_acc: Dict[int, Dict[str, Any]] = {}
                    usage: Dict[str, Any] = {}
                    started_output = True
                    yield {"type": "model", "model": model, "provider": prov, "tier": tier}
                    for raw in resp:
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            obj = json.loads(chunk)
                        except Exception:
                            continue
                        if obj.get("usage"):
                            usage = obj["usage"]
                        for choice in obj.get("choices") or []:
                            delta = choice.get("delta") or {}
                            piece = delta.get("content")
                            if piece:
                                acc_content.append(piece)
                                yield {"type": "delta", "text": piece}
                            think = delta.get("reasoning_content") or delta.get("reasoning")
                            if think:
                                acc_reasoning.append(think)
                                yield {"type": "reasoning", "text": think}
                            for tc in delta.get("tool_calls") or []:
                                idx = tc.get("index", 0)
                                slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] = fn["name"]
                                if fn.get("arguments"):
                                    slot["arguments"] += fn["arguments"]
                                    yield {"type": "tool_partial", "name": slot["name"], "args": slot["arguments"]}
                pt = int((usage or {}).get("prompt_tokens") or 0)
                ct = int((usage or {}).get("completion_tokens") or 0)
                if pt or ct:
                    db.log_usage(prov, model, tier, pt, ct, estimate_cost(model, pt, ct))
                calls = []
                for idx in sorted(tool_acc):
                    slot = tool_acc[idx]
                    if slot.get("name"):
                        calls.append({
                            "id": slot.get("id") or ("call_%d" % idx),
                            "type": "function",
                            "function": {"name": slot["name"], "arguments": slot.get("arguments") or "{}"},
                        })
                yield {
                    "type": "done",
                    "content": "".join(acc_content),
                    "reasoning": "".join(acc_reasoning),
                    "tool_calls": calls,
                    "model": model,
                    "provider": prov,
                    "usage": usage,
                }
                return
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")[:300]
                except Exception:
                    pass
                last_error = LLMError("HTTP %s: %s" % (exc.code, detail))
                if exc.code in (400, 404, 422) and payload.get("tools") and not started_output:
                    # модель не переваривает function calling — повторяем без инструментов
                    payload.pop("tools", None)
                    payload.pop("tool_choice", None)
                    continue
                break
            except Exception as exc:
                last_error = exc
                break
    yield {"type": "error", "error": "Модели недоступны: %s" % last_error}


def vision(prompt: str, image_data_url: str, tier: str = "vision") -> str:
    """Анализ изображения (кадр камеры, скриншот, фото)."""
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ],
    }]
    result = chat(messages, tier=tier, max_tokens=1200)
    return result.get("content", "")


def embed(texts: List[str]) -> List[List[float]]:
    conf = provider_conf("cloudru")
    if not conf.get("api_key"):
        return []
    model = pick_model("embed", "cloudru")
    try:
        with _request(conf["base_url"].rstrip("/") + "/embeddings", conf["api_key"],
                      {"model": model, "input": texts}, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return [item.get("embedding", []) for item in body.get("data", [])]
    except Exception:
        return []


def health() -> Dict[str, Any]:
    out = {}
    for name in ("cloudru", "deepseek"):
        conf = provider_conf(name)
        if not conf.get("api_key"):
            out[name] = {"ok": False, "reason": "нет ключа", "models": 0}
            continue
        models = list_models(name, force=True)
        out[name] = {"ok": bool(models), "models": len(models),
                     "reason": "" if models else "нет ответа от API"}
    return out

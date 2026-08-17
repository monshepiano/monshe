"""Универсальный OpenAI-совместимый провайдер: OpenRouter, Ollama, любой прокси."""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

import httpx

from .. import config
from .base import LLMResult, Provider, ProviderError, ToolCall


class OpenAICompatProvider(Provider):
    def __init__(self, name: str, base_url_key: str, api_key_key: str,
                 model_key: str, default_model: str = "", free: bool = True,
                 tiers: Dict[str, str] | None = None) -> None:
        self.name = name
        self._base_url_key = base_url_key
        self._api_key_key = api_key_key
        self._model_key = model_key
        self._default_model = default_model
        self.free = free
        self.tiers = tiers or {}

    def base_url(self) -> str:
        return (config.get(self._base_url_key) or "").rstrip("/")

    def api_key(self) -> str:
        return (config.get(self._api_key_key) or "").strip() if self._api_key_key else ""

    def model(self, tier: str = "light") -> str:
        if self.tiers.get(tier):
            return self.tiers[tier]
        return (config.get(self._model_key) or self._default_model) if self._model_key else self._default_model

    async def available(self) -> bool:
        if not self.base_url():
            return False
        if self._api_key_key and not self.api_key():
            return False
        if self.name == "ollama":
            try:
                async with httpx.AsyncClient(timeout=2) as cl:
                    r = await cl.get(self.base_url().replace("/v1", "") + "/api/tags")
                return r.status_code == 200
            except Exception:
                return False
        return True

    async def chat(self, messages: List[Dict[str, Any]], *, tools=None,
                   temperature: float = 0.4, max_tokens: int = 1500,
                   model: str = "") -> LLMResult:
        base = self.base_url()
        if not base:
            raise ProviderError(f"{self.name}: не задан адрес")
        url = base if base.endswith("/v1") or "/v1" in base else base + "/v1"
        payload: Dict[str, Any] = {
            "model": model or self.model(),
            "messages": _clean(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self.api_key():
            headers["Authorization"] = f"Bearer {self.api_key()}"
        if self.name == "openrouter":
            headers["HTTP-Referer"] = "https://localhost/jarvis"
            headers["X-Title"] = "Jarvis"
        async with httpx.AsyncClient(timeout=240) as cl:
            r = await cl.post(f"{url}/chat/completions", headers=headers, json=payload)
        if r.status_code != 200:
            raise ProviderError(f"{self.name} {r.status_code}: {r.text[:300]}")
        data = r.json()
        msg = data["choices"][0]["message"]
        usage = data.get("usage", {})
        calls: List[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except Exception:
                    args = {"_raw": args}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args or {},
                                  id=tc.get("id") or uuid.uuid4().hex[:8]))
        return LLMResult(text=msg.get("content") or "", tool_calls=calls,
                         provider=self.name, model=payload["model"],
                         tokens_in=usage.get("prompt_tokens", 0),
                         tokens_out=usage.get("completion_tokens", 0), raw=data)


def _clean(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Приводим историю к простому виду.

    Результаты инструментов отдаём как обычные сообщения пользователя: не все
    OpenAI-совместимые сервисы принимают role="tool" без предшествующего
    assistant-сообщения с tool_calls, а нам важна совместимость со всеми.
    """
    out = []
    for m in messages:
        role = m.get("role", "user")
        content = str(m.get("content", ""))
        if role == "tool":
            out.append({"role": "user",
                        "content": f"[результат инструмента {m.get('name', 'tool')}]\n{content}"})
        elif role in ("system", "user", "assistant"):
            out.append({"role": role, "content": content})
        else:
            out.append({"role": "user", "content": content})
    return out


def openrouter() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        "openrouter", "openrouter_base_url", "openrouter_api_key", "",
        default_model="deepseek/deepseek-chat-v3.1:free",
        tiers={
            "light": "meta-llama/llama-3.3-70b-instruct:free",
            "medium": "deepseek/deepseek-chat-v3.1:free",
            "heavy": "deepseek/deepseek-r1:free",
        },
    )


def custom() -> OpenAICompatProvider:
    return OpenAICompatProvider("custom", "custom_base_url", "custom_api_key", "custom_model")


def ollama() -> OpenAICompatProvider:
    return OpenAICompatProvider("ollama", "ollama_base_url", "", "ollama_model",
                                default_model="qwen2.5:7b")

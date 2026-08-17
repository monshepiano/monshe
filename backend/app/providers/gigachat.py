"""GigaChat (Сбер) — основной провайдер: работает из РФ без VPN, есть бесплатный лимит."""
from __future__ import annotations

import base64
import re
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

from .. import config
from .base import LLMResult, Provider, ProviderError, ToolCall

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://gigachat.devices.sberbank.ru/api/v1"

# Иерархия моделей: лёгкая → тяжёлая
MODELS = {
    "light": "GigaChat",
    "medium": "GigaChat-Pro",
    "heavy": "GigaChat-Max",
}


class GigaChatProvider(Provider):
    name = "gigachat"
    supports_tools = True
    supports_vision = True
    free = True

    def __init__(self) -> None:
        self._token: str = ""
        self._exp: float = 0.0

    # ------------------------------------------------------------ служебное
    def _creds(self) -> str:
        return (config.get("gigachat_credentials") or "").strip()

    def _verify(self):
        return bool(config.get("gigachat_verify_ssl"))

    async def available(self) -> bool:
        return bool(self._creds())

    async def _access_token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        creds = self._creds()
        if not creds:
            raise ProviderError("Не указан ключ GigaChat")
        async with httpx.AsyncClient(verify=self._verify(), timeout=30) as cl:
            r = await cl.post(
                OAUTH_URL,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "RqUID": str(uuid.uuid4()),
                    "Authorization": f"Basic {creds}",
                },
                data={"scope": config.get("gigachat_scope") or "GIGACHAT_API_PERS"},
            )
        if r.status_code != 200:
            raise ProviderError(f"GigaChat OAuth {r.status_code}: {r.text[:200]}")
        data = r.json()
        self._token = data["access_token"]
        self._exp = data.get("expires_at", 0) / 1000 or (time.time() + 1500)
        return self._token

    # ------------------------------------------------------------ чат
    async def chat(self, messages: List[Dict[str, Any]], *, tools=None,
                   temperature: float = 0.4, max_tokens: int = 1500,
                   model: str = "") -> LLMResult:
        token = await self._access_token()
        payload: Dict[str, Any] = {
            "model": model or MODELS["light"],
            "messages": _to_gigachat_messages(messages),
            "temperature": max(0.01, temperature),
            "max_tokens": max_tokens,
        }
        if tools:
            payload["functions"] = [_tool_to_function(t) for t in tools]
            payload["function_call"] = "auto"
        async with httpx.AsyncClient(verify=self._verify(), timeout=180) as cl:
            r = await cl.post(
                f"{API_URL}/chat/completions",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json=payload,
            )
        if r.status_code == 401:
            self._token = ""
            raise ProviderError("GigaChat: неверный ключ или истёк доступ")
        if r.status_code != 200:
            raise ProviderError(f"GigaChat {r.status_code}: {r.text[:300]}")
        data = r.json()
        choice = data["choices"][0]["message"]
        usage = data.get("usage", {})
        calls: List[ToolCall] = []
        fc = choice.get("function_call")
        if fc:
            calls.append(ToolCall(name=fc.get("name", ""),
                                  arguments=fc.get("arguments") or {},
                                  id=uuid.uuid4().hex[:8]))
        return LLMResult(
            text=choice.get("content") or "",
            tool_calls=calls,
            provider=self.name,
            model=payload["model"],
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            raw=data,
        )

    # ------------------------------------------------------------ картинки
    async def generate_image(self, prompt: str) -> bytes:
        """Генерация картинки встроенной функцией text2image."""
        token = await self._access_token()
        body = {
            "model": MODELS["light"],
            "messages": [
                {"role": "system", "content": "Ты — талантливый художник-иллюстратор."},
                {"role": "user", "content": prompt if "арису" in prompt.lower() else f"Нарисуй {prompt}"},
            ],
            "function_call": "auto",
        }
        async with httpx.AsyncClient(verify=self._verify(), timeout=240) as cl:
            r = await cl.post(f"{API_URL}/chat/completions",
                              headers={"Authorization": f"Bearer {token}",
                                       "Content-Type": "application/json"},
                              json=body)
            if r.status_code != 200:
                raise ProviderError(f"GigaChat image {r.status_code}: {r.text[:200]}")
            content = r.json()["choices"][0]["message"].get("content", "")
            m = re.search(r'src="([^"]+)"', content)
            if not m:
                raise ProviderError("Модель не вернула изображение")
            file_id = m.group(1)
            img = await cl.get(f"{API_URL}/files/{file_id}/content",
                               headers={"Authorization": f"Bearer {token}",
                                        "Accept": "application/jpg"})
            if img.status_code != 200:
                raise ProviderError(f"Не удалось скачать изображение: {img.status_code}")
            return img.content

    # ------------------------------------------------------------ файлы/зрение
    async def upload_file(self, path: str, purpose: str = "general") -> str:
        token = await self._access_token()
        with open(path, "rb") as f:
            files = {"file": (path.split("/")[-1], f.read())}
        async with httpx.AsyncClient(verify=self._verify(), timeout=180) as cl:
            r = await cl.post(f"{API_URL}/files",
                              headers={"Authorization": f"Bearer {token}"},
                              files=files, data={"purpose": purpose})
        if r.status_code not in (200, 201):
            raise ProviderError(f"Загрузка файла: {r.status_code} {r.text[:200]}")
        return r.json().get("id", "")

    async def vision(self, prompt: str, image_path: str, model: str = "") -> str:
        """Распознавание изображения (камера / скриншот / фото)."""
        file_id = await self.upload_file(image_path, purpose="general")
        token = await self._access_token()
        payload = {
            "model": model or MODELS["medium"],
            "messages": [{"role": "user", "content": prompt, "attachments": [file_id]}],
            "temperature": 0.3,
        }
        async with httpx.AsyncClient(verify=self._verify(), timeout=240) as cl:
            r = await cl.post(f"{API_URL}/chat/completions",
                              headers={"Authorization": f"Bearer {token}",
                                       "Content-Type": "application/json"},
                              json=payload)
        if r.status_code != 200:
            raise ProviderError(f"Vision {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"].get("content", "")

    async def transcribe(self, audio_path: str) -> str:
        """У GigaChat нет публичного STT — распознаём на устройстве (Web Speech API)."""
        raise ProviderError("Голос распознаётся в браузере, серверный STT не требуется")


def _to_gigachat_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for m in messages:
        role = m.get("role", "user")
        if role == "tool":
            out.append({"role": "function", "name": m.get("name", "tool"),
                        "content": str(m.get("content", ""))})
        else:
            item = {"role": role, "content": str(m.get("content", ""))}
            if m.get("attachments"):
                item["attachments"] = m["attachments"]
            out.append(item)
    return out


def _tool_to_function(tool: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
    }

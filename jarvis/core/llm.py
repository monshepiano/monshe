"""OpenAI-compatible client for Cloud.ru Foundation Models + DeepSeek backup."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .config import DS_BASE, FM_BASE, MODELS, load_settings
from .events import BUS

# Cloud.ru uses a private CA sometimes; on user machines we try verify first.
_CTX_STRICT = ssl.create_default_context()
_CTX_INSECURE = ssl._create_unverified_context()


class LLMError(RuntimeError):
    pass


def _headers(provider: str) -> dict[str, str]:
    s = load_settings()
    key = (s.get("fm_key") if provider == "fm" else s.get("ds_key")) or ""
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _url(provider: str, path: str) -> str:
    base = FM_BASE if provider == "fm" else DS_BASE
    return base.rstrip("/") + path


def _request(
    url: str,
    headers: dict,
    data: bytes | None = None,
    method: str = "GET",
    timeout: int = 90,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    last_err: Exception | None = None
    for ctx in (_CTX_STRICT, _CTX_INSECURE):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.getcode(), resp.read()
        except Exception as e:
            last_err = e
            continue
    raise LLMError(str(last_err) if last_err else "network error")


def ping_provider(provider: str) -> bool:
    try:
        code, raw = _request(_url(provider, "/models"), _headers(provider), timeout=12)
        return code == 200 and bool(raw)
    except Exception:
        return False


def complete(
    model_key: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.5,
    max_tokens: int = 1800,
    on_token: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    spec = MODELS[model_key]
    provider = spec["provider"]
    payload: dict[str, Any] = {
        "model": spec["id"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    BUS.emit("thought", text=f"Модель {spec['label']} · {provider.upper()}")
    t0 = time.time()
    try:
        code, raw = _request(
            _url(provider, "/chat/completions"),
            _headers(provider),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            timeout=120,
        )
    except Exception as e:
        raise LLMError(f"{spec['label']}: {e}") from e

    if code >= 400:
        raise LLMError(f"{spec['label']} HTTP {code}: {raw[:400]!r}")

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        raise LLMError(f"bad json from {spec['label']}: {e}") from e

    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    if on_token and text:
        on_token(text)
    usage = data.get("usage") or {}
    BUS.emit(
        "usage",
        model=spec["label"],
        ms=int((time.time() - t0) * 1000),
        tokens=usage.get("total_tokens") or 0,
    )
    return {
        "text": text,
        "tool_calls": msg.get("tool_calls") or [],
        "raw": data,
        "model": spec,
        "finish": choice.get("finish_reason"),
    }


def transcribe_wav(path: str) -> str:
    """Best-effort OpenAI-compatible transcription (whisper on Cloud.ru)."""
    # Multipart without extra deps
    import mimetypes
    import uuid as _uuid

    s = load_settings()
    boundary = "----Jarvis" + _uuid.uuid4().hex
    filename = path.split("/")[-1]
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        blob = f.read()
    chunks = []

    def field(name, value):
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode() + b"\r\n")

    field("model", "openai/whisper-large-v3")
    field("language", "ru")
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    )
    chunks.append(f"Content-Type: {ctype}\r\n\r\n".encode())
    chunks.append(blob + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    headers = {
        "Authorization": f"Bearer {s.get('fm_key')}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    try:
        code, raw = _request(
            _url("fm", "/audio/transcriptions"),
            headers,
            data=body,
            method="POST",
            timeout=120,
        )
        if code >= 400:
            raise LLMError(raw[:300].decode("utf-8", "replace"))
        data = json.loads(raw.decode("utf-8", "replace"))
        return data.get("text") or data.get("result") or ""
    except Exception as e:
        raise LLMError(f"whisper: {e}") from e

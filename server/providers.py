"""Единый интерфейс к разным LLM-провайдерам.

Без тяжёлых SDK — чистый HTTP (httpx), поэтому зависимости минимальны,
а смена движка сводится к одной строчке конфига.
"""
import httpx

from . import config


class ProviderError(Exception):
    pass


def _post_json(url, headers, payload, timeout=180):
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.post(url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise ProviderError(f"нет связи с провайдером ({e.__class__.__name__})")
    if r.status_code >= 400:
        raise ProviderError(f"API {r.status_code}: {r.text[:500]}")
    return r.json()


def _b64(image):
    """Возвращает (base64 без префикса, mime) из строки base64 или data-URI."""
    if not image:
        return None, None
    s, mime = image, "image/jpeg"
    if isinstance(s, str) and s.startswith("data:"):
        head, _, s = s.partition(",")
        if ";" in head:
            mime = head.split(";", 1)[0][len("data:"):] or mime
    return s, mime


def _openai_messages(system, messages):
    out = [{"role": "system", "content": system}]
    for m in messages:
        c = m.get("content")
        if isinstance(c, dict):
            text = c.get("text") or ""
            img, mime = _b64(c.get("image"))
            if img:
                c = [{"type": "text", "text": text},
                     {"type": "image_url",
                      "image_url": {"url": f"data:{mime};base64,{img}"}}]
            else:
                c = text
        out.append({"role": m.get("role", "user"), "content": c})
    return out


def _deepseek(model, system, messages, temperature, max_tokens, key):
    if any(isinstance(m.get("content"), dict) and m["content"].get("image")
           for m in messages):
        raise ProviderError("DeepSeek не поддерживает изображения — "
                            "задайте vision-модель (Gemini/OpenAI) в настройках.")
    payload = {"model": model,
               "messages": _openai_messages(system, messages),
               "stream": False, "max_tokens": max_tokens}
    if "reasoner" not in model:  # reasoner не принимает temperature
        payload["temperature"] = temperature
    data = _post_json("https://api.deepseek.com/chat/completions",
                      {"Authorization": f"Bearer {key}"}, payload)
    return data["choices"][0]["message"]["content"]


def _openai(model, system, messages, temperature, max_tokens, key):
    payload = {"model": model,
               "messages": _openai_messages(system, messages),
               "stream": False, "max_tokens": max_tokens,
               "temperature": temperature}
    data = _post_json("https://api.openai.com/v1/chat/completions",
                      {"Authorization": f"Bearer {key}"}, payload)
    return data["choices"][0]["message"]["content"]


def _anthropic(model, system, messages, temperature, max_tokens, key):
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, dict):
            parts = [{"type": "text", "text": c.get("text") or "Опиши изображение."}]
            img, mime = _b64(c.get("image"))
            if img:
                parts.append({"type": "image",
                              "source": {"type": "base64",
                                         "media_type": mime, "data": img}})
            c = parts
        out.append({"role": m.get("role", "user"), "content": c})
    payload = {"model": model, "max_tokens": max_tokens,
               "system": system, "messages": out, "temperature": temperature}
    data = _post_json("https://api.anthropic.com/v1/messages",
                      {"x-api-key": key,
                       "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}, payload)
    return "".join(b.get("text", "") for b in data.get("content", []))


def _gemini(model, system, messages, temperature, max_tokens, key):
    contents = []
    for m in messages:
        role = "model" if m.get("role") == "assistant" else "user"
        c = m.get("content")
        if isinstance(c, dict):
            parts = [{"text": c.get("text") or "Опиши изображение."}]
            img, mime = _b64(c.get("image"))
            if img:
                parts.append({"inline_data": {"mime_type": mime, "data": img}})
        else:
            parts = [{"text": c or ""}]
        contents.append({"role": role, "parts": parts})
    payload = {"contents": contents,
               "generationConfig": {"temperature": temperature,
                                    "maxOutputTokens": max_tokens}}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    data = _post_json(url, {}, payload)
    cand = (data.get("candidates") or [{}])[0]
    parts = cand.get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts) or "Пустой ответ Gemini"


def _ollama(model, system, messages, temperature, max_tokens, key=None):
    out = [{"role": "system", "content": system}]
    for m in messages:
        c = m.get("content")
        if isinstance(c, dict):
            img, _ = _b64(c.get("image"))
            item = {"role": m.get("role", "user"), "content": c.get("text") or ""}
            if img:
                item["images"] = [img]
            out.append(item)
        else:
            out.append({"role": m.get("role", "user"), "content": c or ""})
    payload = {"model": model, "messages": out, "stream": False}
    if temperature is not None:
        payload["options"] = {"temperature": temperature}
    data = _post_json(f"{config.ollama_host()}/api/chat", {}, payload)
    return data.get("message", {}).get("content", "")


def complete(provider, model, system, messages, temperature=0.6, max_tokens=1400):
    """Вызвать провайдера. messages — список {role, content}; content может
    быть строкой или dict {text, image(base64), mime} для мультимодальных."""
    key = ""
    env = config.API_KEYS.get(provider)
    if env:
        key = config.get(env, "")
    if provider == "deepseek":
        return _deepseek(model, system, messages, temperature, max_tokens, key)
    if provider == "openai":
        return _openai(model, system, messages, temperature, max_tokens, key)
    if provider == "anthropic":
        return _anthropic(model, system, messages, temperature, max_tokens, key)
    if provider == "gemini":
        return _gemini(model, system, messages, temperature, max_tokens, key)
    if provider == "ollama":
        return _ollama(model, system, messages, temperature, max_tokens)
    raise ProviderError(f"Неизвестный провайдер: {provider}")

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
        if cached and not force:
            age = time.time() - cached[0]
            if age < 600:
                return cached[1]
            # СТАРЫЙ КАТАЛОГ ЛУЧШЕ, ЧЕМ ОЖИДАНИЕ. Каталог моделей меняется раз
            # в недели, а протухал раз в 10 минут — и тогда первый же вопрос
            # пользователя вставал в очередь за походом в облако за списком
            # моделей (до 25 с таймаута). Именно поэтому Джарвис «иногда»
            # отвечал медленно: скорость зависела от того, попал ли вопрос в
            # окно обновления кэша. Отдаём что есть, обновляем в фоне.
            if cached[1] and not _META_BUSY.get(provider):
                _META_BUSY[provider] = True
                threading.Thread(target=_refresh_meta, args=(provider,),
                                 name="jarvis-models", daemon=True).start()
            return cached[1]
    conf = provider_conf(provider)
    if not conf.get("api_key"):
        return []
    try:
        # таймаут короткий: каталог — вспомогательные данные, а не ответ
        # пользователю. Не дождались — уйдём на предпочтения из настроек.
        with _request(conf["base_url"].rstrip("/") + "/models", conf["api_key"], None, "GET", timeout=6) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        items = [m for m in body.get("data", []) if m.get("id")]
    except Exception:
        items = []
    with _CACHE_LOCK:
        _META_CACHE[provider] = (time.time(), items)
        _MODELS_CACHE[provider] = (time.time(), [m["id"] for m in items])
    return items


_META_BUSY: Dict[str, bool] = {}


def _refresh_meta(provider: str) -> None:
    """Обновить каталог моделей в фоне, никого не задерживая."""
    try:
        list_models_meta(provider, force=True)
    except Exception:
        pass
    finally:
        _META_BUSY.pop(provider, None)


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


def vision_models(provider: str) -> List[str]:
    """Модели, которые ПО СЛОВАМ ПРОВАЙДЕРА принимают изображение.

    Если каталог не пришёл (нет сети), падаем на грубую догадку по имени —
    иначе оффлайн-сбой каталога выглядел бы как «зрения не существует».
    """
    seeing = models_of_type(provider, "image-text-to-text", "image-to-text", "multimodal")
    if seeing:
        return seeing
    return [m for m in list_models(provider) if "-vl" in m.lower() or "vision" in m.lower()]


def model_can_see(provider: str, model: str) -> bool:
    return bool(model) and model in vision_models(provider)


def has_image(messages: List[Dict]) -> bool:
    """Есть ли в диалоге хоть одна картинка (мультимодальный content)."""
    for m in messages or []:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


def flatten_images(messages: List[Dict]) -> List[Dict]:
    """Сплющить мультимодальный content в обычный текст.

    ПРИЧИНА существования этой функции: модель, не умеющая смотреть, на
    список частей отвечает HTTP 400 «unknown variant 'image_url'» и весь
    ответ превращается в «Модели недоступны». Такое случалось на второй
    попытке (эскалация vision → smart) и при фолбэке на резервного
    провайдера. Теперь картинка уходит ТОЛЬКО тому, кто умеет её принять,
    а остальным достаётся честная пометка вместо неперевариваемых данных.
    """
    out: List[Dict] = []
    for m in messages or []:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        parts: List[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                parts.append("[изображение приложено, но эта модель не умеет смотреть]")
        flat = dict(m)
        flat["content"] = "\n".join(p for p in parts if p)
        out.append(flat)
    return out


def pick_model(tier: str, provider: str = "cloudru") -> str:
    """Выбирает конкретное имя модели под «уровень» из доступных у провайдера."""
    prefs = CONFIG.get("model_tiers." + tier, []) or []

    # БЫСТРЫЙ ПУТЬ. Каталог нужен только чтобы проверить, существует ли
    # модель. Но пока каталог не пришёл, ждать его нельзя: это ожидание
    # стоит перед первым словом ответа. Если каталог уже лежит в кэше —
    # сверяемся с ним; если нет — берём предпочтение из настроек и идём
    # спрашивать модель, а каталог подтянется в фоне к следующему разу.
    # Ошибиться тут почти невозможно: имена в model_tiers мы задаём сами,
    # а если модель вдруг исчезла, провайдер ответит ошибкой и сработает
    # обычный запасной путь.
    with _CACHE_LOCK:
        have_cache = bool(_MODELS_CACHE.get(provider) or _META_CACHE.get(provider))
    if not have_cache and prefs and tier != "vision":
        if not _META_BUSY.get(provider):
            _META_BUSY[provider] = True
            threading.Thread(target=_refresh_meta, args=(provider,),
                             name="jarvis-models", daemon=True).start()
        return prefs[0]

    available = list_models(provider)
    if tier == "vision" and available:
        # Для зрения выбираем ТОЛЬКО среди зрячих моделей. Иначе предпочтение
        # вроде «VL» могло подстрокой поймать текстовую модель, и картинка
        # уходила тому, кто её не переваривает.
        seeing = vision_models(provider)
        if seeing:
            available = seeing
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
        seeing = vision_models(provider)
        if seeing:
            return seeing[0]
    return available[0]


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price_in, price_out = PRICES_RUB.get(model, DEFAULT_PRICE)
    return prompt_tokens / 1e6 * price_in + completion_tokens / 1e6 * price_out


# Глубина размышления по уровню. Ключ — тот же tier, что выбрал оркестратор:
# один источник истины, никаких вторых правил «когда думать дольше».
_REASONING_EFFORT = {
    "nano": "low",      # болтовня — думать не о чем
    "base": "low",      # обычные вопросы: ответ важнее внутреннего монолога
    "coder": "medium",  # код требует аккуратности
    "vision": "low",    # описать картинку — не задача на рассуждение
    "smart": "high",    # сюда попадают только те, кому рассуждение и нужно
}


def _drop_unsupported(payload: Dict[str, Any], detail: str) -> bool:
    """Убрать из запроса параметр, который не понял этот сервер.

    Возвращает True, если что-то выбросили и повтор имеет смысл. Порядок
    важен: сначала расстаёмся с необязательной «глубиной размышления», и
    только потом — с инструментами, без которых Джарвис теряет руки.
    """
    low = (detail or "").lower()
    if "reasoning_effort" in payload and ("reasoning" in low or "unknown" in low or "unsupported" in low):
        payload.pop("reasoning_effort", None)
        return True
    if "tools" in payload:
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
        return True
    if "reasoning_effort" in payload:
        payload.pop("reasoning_effort", None)
        return True
    return False


def _build_payload(model: str, messages: List[Dict], tools: Optional[List[Dict]], stream: bool,
                   temperature: Optional[float], max_tokens: Optional[int],
                   provider: str = "cloudru", tier: str = "base") -> Dict[str, Any]:
    # ЕДИНСТВЕННОЕ место, где рождается запрос к модели, — здесь же и
    # единственная проверка «а этот собеседник вообще умеет смотреть».
    # Раньше картинку клали в сообщение выше по коду и надеялись, что
    # маршрутизация не подведёт; любая эскалация или смена провайдера
    # ломала эту надежду и приносила HTTP 400.
    if has_image(messages) and not model_can_see(provider, model):
        messages = flatten_images(messages)
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "temperature": CONFIG.get("orchestrator.temperature", 0.6) if temperature is None else temperature,
        "max_tokens": max_tokens or CONFIG.get("orchestrator.max_output_tokens", 2400),
    }
    # ГЛУБИНА РАЗМЫШЛЕНИЯ. Вот настоящая причина «иногда думает бесконечно»:
    # и gpt-oss-120b (base), и GLM-4.7 (smart) — рассуждающие модели, и по
    # умолчанию они работают на medium. Размышление идёт ДО первого слова
    # ответа, пользователь всё это время смотрит в пустоту, а токены капают.
    # Замеры сообщества: low ≈ 880 токенов рассуждения, high ≈ 8000 — почти
    # десятикратная разница во времени ожидания на ровном месте.
    # Мы не угадываем сложность по тексту (эти списки слов уже выкинуты) —
    # глубина следует за УРОВНЕМ, который выбрал оркестратор: болтовня и
    # обычные вопросы отвечаются быстро, а smart зовётся только там, где
    # рассуждение действительно нужно.
    effort = _REASONING_EFFORT.get(tier or "base")
    if effort:
        payload["reasoning_effort"] = effort
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return payload


def chat(messages: List[Dict], tier: str = "base", tools: Optional[List[Dict]] = None,
         temperature: Optional[float] = None, max_tokens: Optional[int] = None,
         provider: Optional[str] = None, timeout: int = 180) -> Dict[str, Any]:
    """Не-стриминговый вызов с автоматическим фолбэком на резервного провайдера.

    timeout — для служебных мелочей вроде подсказок ответа: ждать их 3 минуты
    бессмысленно, пользователь к тому времени уже пишет следующий вопрос.
    """
    providers = [provider] if provider else (active_providers() or ["cloudru"])
    last_error: Optional[Exception] = None
    for prov in providers:
        conf = provider_conf(prov)
        if not conf.get("api_key"):
            continue
        model = pick_model(tier, prov)
        payload = _build_payload(model, messages, tools, False, temperature, max_tokens, prov, tier)
        for attempt in range(2):
            try:
                with _request(conf["base_url"].rstrip("/") + "/chat/completions", conf["api_key"],
                              payload, timeout=timeout) as resp:
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
                if exc.code in (400, 404, 422) and _drop_unsupported(payload, detail):
                    # модель не поняла какой-то параметр (tools или
                    # reasoning_effort) — выбрасываем именно его и повторяем,
                    # а не заваливаем весь запрос
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
        payload = _build_payload(model, messages, tools, True, temperature, max_tokens, prov, tier)
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
                if (exc.code in (400, 404, 422) and not started_output
                        and _drop_unsupported(payload, detail)):
                    # сервер не понял какой-то параметр (reasoning_effort или
                    # tools) — выбрасываем именно его и пробуем ещё раз
                    continue
                break
            except Exception as exc:
                last_error = exc
                break
    yield {"type": "error", "error": "Модели недоступны: %s" % last_error}


def vision(prompt: str, image_data_url: str, tier: str = "vision") -> str:
    """Анализ изображения (кадр камеры, скриншот, фото).

    Смотреть зовём ТОЛЬКО того провайдера, у которого есть зрячая модель.
    Раньше сюда приходил общий список провайдеров, и при любой заминке
    Cloud.ru картинка уезжала в DeepSeek — тот отвечал HTTP 400, а
    пользователь читал «Модели недоступны», хотя недоступно было зрение.
    """
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ],
    }]
    seeing = [p for p in (active_providers() or ["cloudru"]) if vision_models(p)]
    if not seeing:
        raise LLMError("ни у одного подключённого провайдера нет модели со зрением")
    last: Optional[Exception] = None
    for prov in seeing:
        try:
            return chat(messages, tier=tier, max_tokens=1200,
                        provider=prov).get("content", "")
        except Exception as exc:
            last = exc
    raise LLMError("зрение не ответило: %s" % last)


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

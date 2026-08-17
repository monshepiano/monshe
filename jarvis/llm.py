"""
Слой работы с моделями + оркестратор (роутер).

Идея: у Джарвиса один вход `complete()`, а какая именно нейросеть ответит —
решает роутер по сложности задачи. Дёшево там, где можно; умно там, где нужно.
Все провайдеры OpenAI-совместимы, так что переключение бесшовное.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable

import httpx

from .config import config


class LLMError(RuntimeError):
    pass


# Какая схема заголовков сработала для провайдера — запоминаем на время
# работы процесса, чтобы не перебирать варианты на каждом запросе.
_WORKING_AUTH: dict[str, str] = {}

# Признаки того, что сервер отверг именно авторизацию, а не запрос.
_AUTH_FAIL = re.compile(
    r"(401|403|unauthorized|access ?denied|forbidden|api key not found|"
    r"record not found|project not found|invalid api key|no api key)",
    re.IGNORECASE,
)


def is_auth_error(err: str) -> bool:
    """Похоже ли, что сервер не принял ключ/проект (а не сам запрос)."""
    return bool(_AUTH_FAIL.search(str(err or "")))


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    project_id: str = ""
    auth_mode: str = "auto"
    # Пара «ключ доступа»: Key ID (логин) + Key Secret (пароль).
    # Если заполнена — меняем её на временный токен через IAM.
    key_id: str = ""
    key_secret: str = ""
    _token: str = ""
    _token_exp: float = 0.0

    @property
    def ok(self) -> bool:
        has_creds = bool(self.api_key) or bool(self.key_id and self.key_secret)
        return bool(has_creds and self.base_url)

    def auth_modes(self) -> list[str]:
        """Схемы авторизации, которые имеет смысл попробовать, по порядку.

        У Cloud.ru в ходу сразу несколько несовместимых форматов:

        * ``apikey_hdr`` — ``Authorization: Api-Key <ключ>``. Официальный
          формат для статических API-ключей (документация "Аутентификация
          в API Cloud.ru").
        * ``bearer``     — ``Authorization: Bearer <ключ>``. Так написано
          в примере кода из быстрого старта Foundation Models.
        * ``both``       — Bearer плюс ``x-api-key`` и ``x-project-id``. Так
          делает официальная интеграция Cloud.ru для Home Assistant; без
          ``x-project-id`` сервис иногда отвечает "403: Project not found".
        * ``apikey``     — только ``x-api-key`` и ``x-project-id``.
        * ``iam_token``  — пара Key ID + Key Secret меняется на временный
          токен через iam.api.cloud.ru, дальше обычный Bearer.

        Какой из них ждёт конкретный аккаунт — заранее не известно, поэтому
        по умолчанию (``auto``) перебираем их сами и запоминаем удачный.
        """
        if self.auth_mode and self.auth_mode != "auto":
            return [self.auth_mode]
        order = ["bearer", "apikey_hdr"]
        if self.project_id:
            order = ["both", "bearer", "apikey_hdr", "apikey"]
        if self.key_secret:
            # Задана пара логин+пароль — это точно ключ доступа, а не
            # статический API-ключ: сразу идём за токеном.
            order = ["iam_token"] + order
        remembered = _WORKING_AUTH.get(self.name)
        if remembered in order:
            order = [remembered] + [m for m in order if m != remembered]
        return order

    def headers(self, *, json_body: bool = True, mode: str | None = None) -> dict:
        """Заголовки авторизации для выбранной схемы."""
        mode = mode or self.auth_modes()[0]
        h: dict[str, str] = {}
        if json_body:
            h["Content-Type"] = "application/json"
        if mode == "iam_token":
            h["Authorization"] = f"Bearer {self._token or self.api_key}"
        elif mode == "apikey_hdr":
            h["Authorization"] = f"Api-Key {self.api_key}"
        elif mode != "apikey":
            h["Authorization"] = f"Bearer {self.api_key}"
        if mode in ("both", "apikey") and self.project_id:
            h["x-api-key"] = self.api_key
            h["x-project-id"] = self.project_id
        if mode in ("iam_token", "apikey_hdr") and self.project_id:
            h["x-project-id"] = self.project_id
        return h

    async def prepare(self, mode: str) -> bool:
        """Догрузить то, что нужно схеме. Для iam_token — получить токен."""
        if mode != "iam_token":
            return True
        if self._token and time.time() < self._token_exp:
            return True
        key_id = (self.key_id or self.api_key).strip()
        secret = (self.key_secret or "").strip()
        if not (key_id and secret):
            return False
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(
                    "https://iam.api.cloud.ru/api/v1/auth/token",
                    headers={"Content-Type": "application/json"},
                    json={"keyId": key_id, "secret": secret},
                )
                if r.status_code >= 400:
                    return False
                data = r.json()
                tok = data.get("access_token") or data.get("accessToken")
                if not tok:
                    return False
                self._token = tok
                # Токен живёт час, обновим чуть раньше.
                self._token_exp = time.time() + float(
                    data.get("expires_in") or 3600
                ) - 120
                return True
        except Exception:
            return False

    def remember_auth(self, mode: str) -> None:
        _WORKING_AUTH[self.name] = mode


def providers() -> list[Provider]:
    out: list[Provider] = []
    for key in ("cloudru", "fallback"):
        node = config.get("providers", key, default={}) or {}
        if not node.get("enabled", False):
            continue
        p = Provider(
            key,
            (node.get("base_url") or "").strip().rstrip("/"),
            (node.get("api_key") or "").strip(),
            (node.get("project_id") or "").strip(),
            key_id=(node.get("key_id") or "").strip(),
            key_secret=(node.get("key_secret") or "").strip(),
        )
        if p.ok:
            out.append(p)
    return out


# ----------------------------------------------------------------------------
# Роутер: выбор модели под задачу
# ----------------------------------------------------------------------------

HARD_HINTS = re.compile(
    r"(спланируй|план\b|стратег|проанализируй|исследуй|сравни|разбер|почему|"
    r"докажи|оптимизир|архитектур|алгоритм|посчитай|рассчита|многошаг|"
    r"пошагов|report|research|analyz)",
    re.IGNORECASE,
)
CODE_HINTS = re.compile(
    r"(код|напиши скрипт|программ|python|javascript|функци|баг|ошибк[аи] в коде|"
    r"регуляр|sql|html|css|рефактор|debug|traceback)",
    re.IGNORECASE,
)
SIMPLE_HINTS = re.compile(
    r"^(привет|здравствуй|хай|спасибо|пока|как дела|ок|окей|да|нет|ага|"
    r"который час|сколько времени)\b",
    re.IGNORECASE,
)


@dataclass
class RouteDecision:
    model: str
    tier: str
    reason: str


def route(task: str, *, has_images: bool = False, mode: str = "chat",
          force: str | None = None) -> RouteDecision:
    """Выбирает модель. `mode`: chat | agent | plan | tool."""
    models = config.get("models", default={}) or {}

    if force:
        return RouteDecision(force, "forced", "модель задана вручную")

    if has_images:
        return RouteDecision(models.get("vision", ""), "vision",
                             "во вложении изображение — нужна зрячая модель")

    if not config.get("router", "enabled", default=True):
        return RouteDecision(models.get("smart", ""), "smart", "роутер выключен")

    text = (task or "").strip()
    length = len(text)

    if mode == "plan":
        return RouteDecision(models.get("smart", ""), "smart",
                             "планирование задачи — нужна сильная модель")
    if mode == "agent":
        return RouteDecision(models.get("agent", ""), "agent",
                             "агентский цикл с инструментами")
    if CODE_HINTS.search(text):
        return RouteDecision(models.get("code", ""), "code",
                             "задача про код — специализированная модель")
    if SIMPLE_HINTS.match(text) or length < 40:
        return RouteDecision(models.get("chat", ""), "chat",
                             "короткий простой запрос — быстрая модель")
    if HARD_HINTS.search(text) or length > 600:
        return RouteDecision(models.get("smart", ""), "smart",
                             "сложный запрос — включаю тяжёлую артиллерию")
    return RouteDecision(models.get("chat", ""), "chat", "обычный запрос")


# ----------------------------------------------------------------------------
# Вызовы моделей
# ----------------------------------------------------------------------------

@dataclass
class Completion:
    text: str
    model: str
    provider: str
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    reason: str = ""


def explain_error(err: str) -> str:
    """Переводит ошибку API на человеческий язык с готовым решением."""
    e = str(err or "")
    low = e.lower()
    if "api key not found" in low or "record not found" in low:
        return (
            "Cloud.ru не нашёл такой ключ. Проект определился правильно, "
            "а вот сам ключ сервис не узнаёт.\n\n"
            "Что проверить по порядку:\n"
            "1. Вставлен именно Key Secret, а не Key ID. Key Secret "
            "показывают ровно один раз — в окне сразу после создания ключа. "
            "Если вы его не сохранили, ключ надо создать заново.\n"
            "2. Ключ создан в том же проекте, ID которого указан выше.\n"
            "3. При создании ключа в поле «Сервисы» выбран FoundationModels.\n"
            "4. Ключ в статусе «Активен» и срок действия не истёк.\n\n"
            "Проще всего: cloud.ru → Пользователи → Сервисные аккаунты → "
            "ваш аккаунт → вкладка «API-ключи» → Создать API-ключ → "
            "Сервисы: FoundationModels → Создать → скопировать Key Secret "
            "целиком и вставить сюда.\n\n"
            "Если это не помогает — воспользуйтесь запасным входом в "
            "настройках: поля «Key ID» и «Key Secret» из раздела "
            "«Учётные данные доступа» → «Ключи доступа». Это другой тип "
            "ключа (логин + пароль), Джарвис сам обменяет его на токен."
        )
    if "project not found" in low or ("403" in e and "project" in low):
        return (
            "Cloud.ru не видит проект. Обычно это значит, что в настройках "
            "не указан Project ID (идентификатор проекта).\n"
            "Как исправить: личный кабинет Cloud.ru -> раздел Проекты -> "
            "откройте свой проект -> скопируйте его ID "
            "(длинная строка вида 50000000-4000-3000-2000-100000000001) "
            "и вставьте в настройках Джарвиса в поле «ID проекта»."
        )
    if "401" in e or "unauthorized" in low or "invalid api key" in low:
        return (
            "Ключ не принят. Проверьте, что вы вставили Key Secret целиком, "
            "без пробелов, и что у ключа выбран сервис Foundation Models "
            "и не истёк срок действия."
        )
    if "429" in e or "rate limit" in low or "quota" in low:
        return ("Слишком много запросов или закончилась квота. "
                "Подождите минуту и попробуйте снова.")
    if "404" in e and "model" in low:
        return ("Такой модели нет в вашем проекте. Откройте настройки "
                "Джарвиса и выберите модель из списка доступных.")
    if "timeout" in low or "timed out" in low:
        return "Cloud.ru не ответил вовремя. Попробуйте ещё раз."
    if "ssl" in low or "certificate" in low or "connect" in low or "dns" in low:
        return ("Нет связи с сервером Cloud.ru. Проверьте интернет "
                "(VPN для Cloud.ru не нужен и может мешать).")
    return ""


async def _post(provider: Provider, path: str, payload: dict,
                timeout: float = 180.0) -> dict:
    """POST с автоподбором схемы авторизации.

    Если сервер отвечает 401/403, пробуем следующий формат заголовков —
    у Cloud.ru их два, и какой ждёт конкретный аккаунт, заранее не ясно.
    """
    url = provider.base_url.rstrip("/") + path
    last_err = ""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for mode in provider.auth_modes():
            if not await provider.prepare(mode):
                continue
            r = await client.post(url, headers=provider.headers(mode=mode),
                                  json=payload)
            if r.status_code < 400:
                provider.remember_auth(mode)
                return r.json()
            last_err = f"{provider.name} {r.status_code}: {r.text[:400]}"
            if not is_auth_error(last_err):
                break
    raise LLMError(last_err)


async def complete(
    messages: list[dict],
    *,
    task_hint: str = "",
    mode: str = "chat",
    model: str | None = None,
    tools: list[dict] | None = None,
    temperature: float = 0.6,
    max_tokens: int = 4000,
    has_images: bool = False,
) -> Completion:
    """Единая точка входа. Сама выбирает модель и провайдера, с ретраями."""
    provs = providers()
    if not provs:
        raise LLMError(
            "Не задан API-ключ. Открой Настройки в интерфейсе Джарвиса "
            "и вставь ключ Cloud.ru Foundation Models."
        )

    hint = task_hint or (messages[-1].get("content", "") if messages else "")
    if isinstance(hint, list):  # мультимодальный контент
        hint = " ".join(p.get("text", "") for p in hint if isinstance(p, dict))
    decision = route(hint, has_images=has_images, mode=mode, force=model)

    payload: dict[str, Any] = {
        "model": decision.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    last_err: Exception | None = None
    for provider in provs:
        for attempt in range(2):
            try:
                data = await _post(provider, "/chat/completions", payload)
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message", {}) or {}
                content = msg.get("content") or ""
                if isinstance(content, list):
                    content = "".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                return Completion(
                    text=content.strip(),
                    model=data.get("model", decision.model),
                    provider=provider.name,
                    tool_calls=msg.get("tool_calls") or [],
                    usage=data.get("usage") or {},
                    reason=decision.reason,
                )
            except LLMError as e:
                last_err = e
                text = str(e)
                # Модель недоступна у этого провайдера — пробуем запасную
                if "404" in text or "model" in text.lower():
                    fb = config.get("models", "chat", default="")
                    if payload["model"] != fb and fb:
                        payload["model"] = fb
                        continue
                if "429" in text or "503" in text or "502" in text:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                break
            except Exception as e:  # сеть
                last_err = e
                await asyncio.sleep(1.0)
    hint = explain_error(str(last_err))
    if hint:
        raise LLMError(f"{hint}\n\nТехническая деталь: {last_err}")
    raise LLMError(f"Все провайдеры недоступны. Последняя ошибка: {last_err}")


async def stream(
    messages: list[dict],
    *,
    task_hint: str = "",
    mode: str = "chat",
    model: str | None = None,
    temperature: float = 0.6,
    max_tokens: int = 4000,
    has_images: bool = False,
) -> AsyncIterator[dict]:
    """Потоковая генерация. Отдаёт словари: {'type': 'meta'|'delta'|'done'}."""
    provs = providers()
    if not provs:
        yield {"type": "error", "text": "Не задан API-ключ Cloud.ru. Открой Настройки."}
        return

    hint = task_hint or (messages[-1].get("content", "") if messages else "")
    if isinstance(hint, list):
        hint = " ".join(p.get("text", "") for p in hint if isinstance(p, dict))
    decision = route(hint, has_images=has_images, mode=mode, force=model)

    payload = {
        "model": decision.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }

    last_err = None
    for provider in provs:
        url = provider.base_url.rstrip("/") + "/chat/completions"
        for mode in provider.auth_modes():
            try:
                if not await provider.prepare(mode):
                    continue
                async with httpx.AsyncClient(timeout=300.0) as client:
                    async with client.stream("POST", url,
                                             headers=provider.headers(mode=mode),
                                             json=payload) as r:
                        if r.status_code >= 400:
                            body = (await r.aread()).decode("utf-8", "ignore")
                            last_err = f"{r.status_code}: {body[:300]}"
                            if is_auth_error(last_err):
                                continue  # пробуем следующую схему заголовков
                            break
                        provider.remember_auth(mode)
                        yield {"type": "meta", "model": decision.model,
                               "provider": provider.name,
                               "reason": decision.reason}
                        async for line in r.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            chunk = line[5:].strip()
                            if chunk == "[DONE]":
                                yield {"type": "done"}
                                return
                            try:
                                obj = json.loads(chunk)
                            except json.JSONDecodeError:
                                continue
                            delta = (obj.get("choices") or [{}])[0].get("delta", {})
                            piece = delta.get("content")
                            if piece:
                                yield {"type": "delta", "text": piece}
                        yield {"type": "done"}
                        return
            except Exception as e:
                last_err = str(e)
                break
    hint = explain_error(str(last_err))
    text = f"Модели недоступны: {last_err}"
    if hint:
        text = f"{hint}\n\nТехническая деталь: {last_err}"
    yield {"type": "error", "text": text}


async def transcribe(audio_bytes: bytes, filename: str = "audio.webm") -> str:
    """Голос -> текст через whisper-large-v3 (Cloud.ru)."""
    provs = providers()
    if not provs:
        raise LLMError("Нет ключа для распознавания речи.")
    model = config.get("models", "stt", default="openai/whisper-large-v3")
    last_err = None
    for provider in provs:
        url = provider.base_url.rstrip("/") + "/audio/transcriptions"
        headers = provider.headers(json_body=False)
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                r = await client.post(
                    url,
                    headers=headers,
                    files={"file": (filename, audio_bytes, "application/octet-stream")},
                    data={"model": model, "language": "ru"},
                )
                if r.status_code >= 400:
                    last_err = f"{r.status_code}: {r.text[:200]}"
                    continue
                data = r.json()
                return (data.get("text") or "").strip()
        except Exception as e:
            last_err = str(e)
    raise LLMError(f"Не удалось распознать речь: {last_err}")


async def list_models() -> list[str]:
    out: list[str] = []
    for provider in providers():
        url = provider.base_url.rstrip("/") + "/models"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                for mode in provider.auth_modes():
                    if not await provider.prepare(mode):
                        continue
                    r = await client.get(
                        url, headers=provider.headers(json_body=False, mode=mode)
                    )
                    if r.status_code < 400:
                        provider.remember_auth(mode)
                        for m in r.json().get("data", []):
                            mid = m.get("id")
                            if mid and mid not in out:
                                out.append(mid)
                        break
                    if not is_auth_error(f"{r.status_code}: {r.text[:200]}"):
                        break
        except Exception:
            continue
    return out


def extract_json(text: str) -> Any:
    """Достаёт JSON из ответа модели, даже если он обёрнут в ```json ... ```."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None

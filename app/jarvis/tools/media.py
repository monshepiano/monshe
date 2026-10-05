"""Медиа-инструменты JARVIS: изображения через GigaChat и резервный ASR."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .. import db, llm, sandbox
from ..config import CONFIG

_GIGACHAT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
_GIGACHAT_API = "https://api.giga.chat/v1"
_RUSSIAN_CA = Path(__file__).resolve().parent.parent / "certs" / "russian_trusted_root_ca.pem"
_MAX_IMAGE_BYTES = 25 * 1024 * 1024
_UUID_RE = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b")
_TOKEN_LOCK = threading.RLock()
_TOKEN_CACHE: Dict[Tuple[str, str], Tuple[str, float]] = {}
_UA = "Mozilla/5.0 JARVIS/1.0"
_CTX = ssl.create_default_context()


def _ws() -> Path:
    return sandbox.root()


def _dl(name: str) -> str:
    return sandbox.dl(name)


class GigaChatError(RuntimeError):
    """Ошибка transport/API без утечки Authorization Key или access token."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _ssl_context() -> ssl.SSLContext:
    """Обычная TLS-проверка плюс официальный Russian Trusted Root CA.

    Никаких ``CERT_NONE``/unverified contexts: встроенный CA только дополняет
    системное хранилище macOS/Python и проверяет hostname как обычно.
    """
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=str(_RUSSIAN_CA))
    return context


def _http(url: str, *, method: str = "GET", headers: Optional[Dict[str, str]] = None,
          data: Optional[bytes] = None, timeout: int = 120,
          max_bytes: int = 2 * 1024 * 1024) -> Tuple[bytes, str]:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > max_bytes:
                raise GigaChatError("ответ GigaChat слишком большой")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise GigaChatError("ответ GigaChat слишком большой")
            return body, str(response.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(4096).decode("utf-8", "replace")
            parsed = json.loads(detail)
            detail = str(parsed.get("message") or parsed.get("error_description") or
                         parsed.get("error") or detail)
        except Exception:
            detail = ""
        message = "GigaChat вернул HTTP %s" % exc.code
        if detail:
            message += ": " + detail[:300]
        raise GigaChatError(message, int(exc.code or 0)) from None
    except GigaChatError:
        raise
    except Exception as exc:
        raise GigaChatError("не удалось связаться с GigaChat: %s" % exc) from None


def _access_token(force: bool = False) -> str:
    auth_key = str(CONFIG.get("media.gigachat_auth_key", "") or "").strip()
    scope = str(CONFIG.get("media.gigachat_scope", "GIGACHAT_API_PERS") or
                "GIGACHAT_API_PERS").strip()
    if not auth_key:
        raise GigaChatError(
            "не задан Authorization Key GigaChat — открой Настройки → Генерация изображений")
    cache_key = (auth_key, scope)
    now = time.time()
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and not force and cached[1] > now + 45:
            return cached[0]

        payload = urllib.parse.urlencode({"scope": scope}).encode("ascii")
        body, _ = _http(
            _GIGACHAT_OAUTH_URL,
            method="POST",
            headers={
                "Authorization": "Basic " + auth_key,
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data=payload,
            timeout=45,
        )
        try:
            answer = json.loads(body.decode("utf-8"))
            token = str(answer["access_token"])
            expires_raw = float(answer.get("expires_at") or 0)
        except Exception:
            raise GigaChatError("GigaChat не вернул корректный access token") from None
        # expires_at документирован в миллисекундах Unix. На случай ответа без
        # срока используем консервативные 29 минут из официальных 30.
        expires = expires_raw / 1000 if expires_raw > 10_000_000_000 else expires_raw
        if expires <= now:
            expires = now + 29 * 60
        _TOKEN_CACHE.clear()  # секрет сменился — старые токены больше не нужны
        _TOKEN_CACHE[cache_key] = (token, expires)
        return token


def _json_request(path: str, payload: Dict[str, Any], token: str) -> Dict[str, Any]:
    body, _ = _http(
        _GIGACHAT_API + path,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=180,
        max_bytes=4 * 1024 * 1024,
    )
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        raise GigaChatError("GigaChat вернул некорректный JSON") from None


def _image_id(answer: Dict[str, Any]) -> str:
    """Найти UUID только в message content/attachments, не в request id."""
    try:
        message = answer["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise GigaChatError("GigaChat не вернул результат генерации") from None

    content = str(message.get("content") or "")
    # Официальный ответ: <img src="uuid" fuse="true"/>. Regex UUID не зависит
    # от порядка HTML-атрибутов и не требует стороннего HTML-парсера.
    img = re.search(r"(?is)<img\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"]", content)
    if img:
        found = _UUID_RE.search(img.group(1))
        if found:
            return found.group(0).lower()

    for attachment in message.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        for key in ("file_id", "id", "src"):
            found = _UUID_RE.search(str(attachment.get(key) or ""))
            if found:
                return found.group(0).lower()

    raise GigaChatError(
        "GigaChat ответил без изображения. Попробуй точнее написать «нарисуй изображение…»")


def _image_bytes(file_id: str, token: str) -> Tuple[bytes, str]:
    body, content_type = _http(
        "%s/files/%s/content" % (_GIGACHAT_API, urllib.parse.quote(file_id)),
        headers={"Authorization": "Bearer " + token, "Accept": "application/jpg"},
        timeout=180,
        max_bytes=_MAX_IMAGE_BYTES,
    )
    if len(body) < 1000:
        raise GigaChatError("GigaChat вернул пустое или повреждённое изображение")
    if body.startswith(b"\xff\xd8\xff"):
        return body, ".jpg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return body, ".png"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return body, ".webp"
    raise GigaChatError("GigaChat вернул не изображение (%s)" % (content_type or "unknown type"))


# AA: СЛОВА ПРОМПТА ГЕНЕРАТОР РИСУЕТ БУКВАЛЬНО. «Привет, Джарвис, нарисуй
# котика» превращалось в картинку с мем-подписью «Hello Jarvis»: приветствие
# и обращение ехали в промпт, а модель изображений честно рисовала эти слова.
# Убираем ОБРАЩЕНИЯ И ПРИВЕТСТВИЯ до улучшения и после него — nano-модель
# может вернуть их обратно.
_ADDRESS_NOISE = re.compile(
    r"(?:^|\s)[прz3z]?р?(?:привет|приветствую|здравствуй|здравствуйте|"
    r"добры(?:й|е)\s+(?:день|вечер|утро)|хай|hello|hi|hey)[!,.:\s]*"
    r"|\b(?:джарвис|джарвиса|jarvis|jаrvis)\b[!,.:\s]*",
    re.IGNORECASE,
)


def strip_address(text: str) -> str:
    """Убрать из промпта картинки приветствия и обращения к ассистенту."""
    cleaned = _ADDRESS_NOISE.sub(" ", str(text or ""))
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.!-—")
    return cleaned.strip()


def _enhance_prompt(prompt: str, width: int, height: int) -> str:
    """Дешёвая модель уточняет сцену; при сбое исходная просьба не теряется."""
    if not CONFIG.get("media.enhance_prompt", True):
        return strip_address(prompt)
    ratio = "квадратный кадр"
    if width > height:
        ratio = "горизонтальный кадр"
    elif height > width:
        ratio = "вертикальный кадр"
    instruction = (
        "Ты арт-директор. Перепиши запрос как один точный промпт для современной "
        "фотографичной генерации изображения. Сохрани сюжет; опиши ОДНУ цельную "
        "сцену: главный объект и его действие, окружение, время суток, направление "
        "и характер света, фактуру материалов, ракурс камеры и настроение. Тела, "
        "лица и пропорции — естественные и анатомически верные. Это описание "
        "ТОЛЬКО видимой сцены: никаких обращений, приветствий, имён («привет», "
        "«Джарвис», «пожалуйста») и просьб — генератор рисует буквально любое "
        "слово из промпта как надпись на картинке. Не добавляй надписи, логотипы "
        "и watermark. Формат: %s. Ответь только готовым промптом на русском, "
        "до 900 знаков.\n\nЗапрос: %s"
    ) % (ratio, prompt)
    try:
        # BM8: потолок 12 секунд — улучшение промпта не имеет права
        # задерживать картинку; не успели — рисуем с исходным текстом
        response = llm.chat([{"role": "user", "content": instruction}], tier="nano",
                            max_tokens=450, temperature=0.45, timeout=12)
        text = str(response.get("content") or "").strip()
        # nano-модель могла вписать обращение — вычищаем и её ответ
        cleaned = strip_address(text)
        return cleaned[:1400] if cleaned else strip_address(prompt)
    except Exception:
        return strip_address(prompt)


def _image_format(image: bytes, content_type: str = "") -> str:
    if len(image) < 1000:
        raise GigaChatError("сервис вернул пустое или повреждённое изображение")
    if image.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if image.startswith(b"RIFF") and image[8:12] == b"WEBP":
        return ".webp"
    raise GigaChatError("сервис вернул не изображение (%s)" % (content_type or "unknown type"))


def _save_gateway_image(image: bytes, content_type: str, prompt: str) -> Dict[str, Any]:
    suffix = _image_format(image, content_type)
    image_id = hashlib.sha256(image).hexdigest()[:16]
    name = "jarvis_image_%s%s" % (image_id, suffix)
    target = sandbox.safe_path(name)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(image)
    tmp.replace(target)
    return {
        "ok": True,
        "path": name,
        "name": name,
        "size": len(image),
        "download_url": sandbox.dl(name),
        "preview_url": sandbox.dl(name),
        "prompt": prompt,
        "model": "cloud image gateway",
        "provider": "gateway",
        "watermark": False,
        "image_id": image_id,
    }


def _gateway_image(prompt: str, width: int, height: int) -> Dict[str, Any]:
    """Generate through the release gateway without exposing its provider key."""
    base_url = str(CONFIG.get("media.image_gateway_url", "") or "").strip().rstrip("/")
    client_token = str(CONFIG.get("media.image_gateway_token", "") or "").strip()
    if not base_url or not client_token:
        raise GigaChatError("облачная генерация не подключена в этой сборке")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost"):
        raise GigaChatError("image gateway должен использовать HTTPS")

    body = json.dumps({
        "prompt": prompt,
        "width": max(256, min(int(width or 1024), 2048)),
        "height": max(256, min(int(height or 1024), 2048)),
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base_url + "/v1/images/generations",
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + client_token,
            "Content-Type": "application/json",
            "Accept": "image/jpeg,image/png,image/webp",
            "User-Agent": _UA,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=240, context=_ssl_context()) as response:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > _MAX_IMAGE_BYTES:
                raise GigaChatError("облачный сервис вернул слишком большой файл")
            image = response.read(_MAX_IMAGE_BYTES + 1)
            if len(image) > _MAX_IMAGE_BYTES:
                raise GigaChatError("облачный сервис вернул слишком большой файл")
            return _save_gateway_image(
                image, str(response.headers.get("Content-Type") or ""), prompt)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read(4096).decode("utf-8", "replace"))
            message = str(detail.get("error") or "")[:300]
        except Exception:
            message = ""
        if exc.code == 401:
            message = "доступ этой сборки к облачной генерации истёк"
        elif exc.code in (400, 422):
            message = "сервис отклонил этот сюжет — попробуй описать его иначе"
        elif exc.code == 429:
            message = "лимит изображений временно исчерпан — попробуй чуть позже"
        raise GigaChatError(message or "image gateway вернул HTTP %s" % exc.code,
                            int(exc.code or 0)) from None
    except GigaChatError:
        raise
    except Exception as exc:
        raise GigaChatError("не удалось связаться с облачной генерацией: %s" % exc) from None


# BM6: ГЕНЕРАЦИЯ КАРТИНОК ЧЕРЕЗ YANDEX AI STUDIO — тот же ключ и folder_id,
# что у текстового провайдера yandex. YandexART работает асинхронно:
# ставим операцию в очередь, затем опрашиваем до готовности. Долгая
# генерация — не ошибка (BM5), ждём до трёх минут.
_YANDEX_ASYNC_URL = ("https://ai.api.cloud.yandex.net/foundationModels"
                     "/v1/imageGenerationAsync")
_YANDEX_IMAGE_PRICE_RUB = 2.23      # фикс. цена запроса YandexART
_YANDEX_OPERATIONS_URL = "https://operation.api.cloud.yandex.net/operations/"


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a or 1


def _yandex_image(prompt: str, width: int, height: int) -> Dict[str, Any]:
    """Картинка через YandexART: ключ берём у провайдера yandex из конфига."""
    conf = CONFIG.get("providers.yandex", {}) or {}
    api_key = str(conf.get("api_key") or "").strip()
    folder = str(conf.get("folder_id") or "").strip()
    if not api_key or not folder:
        raise GigaChatError("для Яндекса нужен ключ и folder_id "
                            "(Настройки → Yandex AI Studio)")
    model = str(CONFIG.get("media.yandex_image_model", "yandex-art")
                or "yandex-art").strip().rstrip("/")
    w = max(256, min(int(width or 1024), 2048))
    h = max(256, min(int(height or 1024), 2048))
    g = _gcd(w, h)
    payload = {
        "modelUri": "art://%s/%s/latest" % (folder, model),
        "generationOptions": {
            "aspectRatio": {"widthRatio": str(w // g), "heightRatio": str(h // g)},
        },
        "messages": [{"text": str(prompt or "")[:500]}],
    }

    def _post(auth_scheme: str) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json", "User-Agent": _UA}
        headers["Authorization"] = ((auth_scheme + " ") + api_key)
        req = urllib.request.Request(
            _YANDEX_ASYNC_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        op = _post("Api-Key")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:      # редкая конфигурация шлюза — пробуем Bearer
            op = _post("Bearer")
        else:
            raise
    op_id = str(op.get("id") or "")
    if not op_id:
        raise GigaChatError("Яндекс не принял запрос на генерацию")

    deadline = time.monotonic() + 170.0
    poll_errors = 0
    while time.monotonic() < deadline:
        time.sleep(2.0)
        req = urllib.request.Request(
            _YANDEX_OPERATIONS_URL + op_id,
            headers={"Authorization": "Api-Key " + api_key, "User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=20,
                                        context=_ssl_context()) as resp:
                st = json.loads(resp.read().decode("utf-8"))
        except Exception:
            # короткий сбой сети при опросе — не приговор: операция уже
            # стоит в очереди Яндекса, пробуем ещё (до трёх подряд)
            poll_errors += 1
            if poll_errors >= 3:
                raise GigaChatError("Яндекс: не удаётся дождаться результата")
            continue
        poll_errors = 0
        if st.get("error"):
            raise GigaChatError("Яндекс: %s" % (st.get("error") or "ошибка генерации"))
        if not st.get("done"):
            continue
        b64 = str(((st.get("response") or {}).get("image")) or "")
        image = base64.b64decode(b64) if b64 else b""
        if not image:
            raise GigaChatError("Яндекс вернул пустую картинку")
        if len(image) > _MAX_IMAGE_BYTES:
            raise GigaChatError("картинка Яндекса слишком большая")
        name = "yandex_image_%s.jpeg" % op_id[:8]
        target = sandbox.safe_path(name)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(image)
        tmp.replace(target)
        try:   # BM7: картинка стоит денег — попадает в счётчик расходов
            db.log_usage("yandex", model, "image", 0, 0, _YANDEX_IMAGE_PRICE_RUB)
        except Exception:
            pass
        return {
            "ok": True,
            "path": name,
            "name": name,
            "size": len(image),
            "download_url": sandbox.dl(name),
            "model": model,
            "provider": "yandex",
            "cost_rub": _YANDEX_IMAGE_PRICE_RUB,
        }
    raise GigaChatError("Яндекс не успел нарисовать за 3 минуты — попробуйте ещё раз")


# Y: ЦЕПОЧКА БЕСПЛАТНЫХ МОДЕЛЕЙ — от свежих к старым. Прежний безымянный
# дефолт рисованием напоминал «первые ИИ-модели»: Z-Image Turbo делает
# картинку с 2x-апскейлом, FLUX.2 Klein — новое поколение, классический
# flux остаётся последним запасным. Рабочая модель запоминается.
_FREE_IMAGE_MODELS = ("zimage", "klein", "flux")
_FREE_IMAGE_STATE: Dict[str, Any] = {"model": ""}
_FREE_IMAGE_BUDGET_S = 90.0     # BM8: общий потолок бесплатной цепочки


def _free_image(prompt: str, width: int, height: int) -> Dict[str, Any]:
    """Открытая бесплатная генерация — без ключей, работает из России.

    Cloud.ru Foundation Models картинки не генерирует вообще (в каталоге
    только LLM/embedding/rerank/audio/OCR), а GigaChat требует отдельного
    ключа. Этот маршрут — последний в цепочке: генерация работает из коробки.
    AB: КОРЕНЬ «ПОСЛЕ ОДНОЙ ГЕНЕРАЦИИ КАПУТ» — анонимный лимит генератора:
    второй запрос следом ловит 429/медленный ответ, а цепочка ждала по 180с
    на модель без единого повтора — до 9 минут тишины, и «навсегда капут».
    Теперь: короткий таймаут, referrer для щадящего лимита, и на 429/5xx —
    пауза и ПОВТОР той же модели, прежде чем ехать к следующей.
    """
    seed = int(time.time() * 1000) % 10 ** 8
    remembered = _FREE_IMAGE_STATE.get("model") or ""
    order = ((remembered,) + tuple(m for m in _FREE_IMAGE_MODELS if m != remembered)
             if remembered in _FREE_IMAGE_MODELS else _FREE_IMAGE_MODELS)
    negative = ("text, watermark, logo, signature, blurry, "
                "low quality, deformed, extra fingers, bad anatomy, "
                "mutated hands, extra limbs, disfigured face")
    # AC: детерминированный качество-хвост — понятен любой модели генератора,
    # не зависит от того, сработал ли nano-улучшатель
    full_prompt = (str(prompt or "").strip() +
                   ", high quality, highly detailed, sharp focus, natural proportions, "
                   "no text")[:1100]
    image = b""
    used = ""
    last_error = ""
    budget_deadline = time.monotonic() + _FREE_IMAGE_BUDGET_S
    for model in order:
        for attempt in (1, 2):
            left = budget_deadline - time.monotonic()
            if left <= 1:
                last_error = "бесплатный генератор: время вышло (%s)" % model
                break
            # Z: НЕГАТИВНЫЙ ПРОМПТ — убирает типичный мусор бесплатных генераторов
            # (текст на картинке, водяные знаки, мыло, кривые руки)
            url = ("https://image.pollinations.ai/prompt/%s?width=%d&height=%d"
                   "&nologo=true&seed=%d&model=%s&negative_prompt=%s&referrer=jarvis"
                   % (urllib.parse.quote(full_prompt)[:1400], width, height, seed, model,
                      urllib.parse.quote(negative)))
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            try:
                with urllib.request.urlopen(req, timeout=min(75, max(2, left)),
                                            context=_CTX) as resp:
                    image = resp.read()
            except urllib.error.HTTPError as exc:
                last_error = "HTTP %s от %s" % (exc.code, model)
                # лимит/поломка сервиса — одна повторная попытка с паузой
                if exc.code in (429, 500, 502, 503, 504) and attempt == 1:
                    time.sleep(4)
                    continue
                break
            except Exception as exc:
                last_error = "%s: %s" % (model, str(exc)[:120])
                if attempt == 1:
                    time.sleep(2)
                    continue
                break
            if image and len(image) >= 1200:
                used = model
                _FREE_IMAGE_STATE["model"] = model
                break
            last_error = "пустой ответ от " + model
            image = b""
            break
        if image:
            break
    if not image:
        # AA: текст ошибки — для модели. Прежняя формулировка позволяла LLM
        # выдумать «генератор требует платного доступа»: бесплатная цепочка
        # на минуту занята — это не тариф, скажи пользователю честно.
        return {"ok": False,
                "error": "бесплатный генератор временно недоступен (%s). "
                         "Платный доступ НЕ нужен: генерация бесплатна и "
                         "работает без ключей — лимит на минуту исчерпан, "
                         "подождите 30–60 секунд и вызовите инструмент снова."
                         % (last_error or "пустой ответ")}
    name = "image_%d.jpg" % (int(time.time() * 1000) % 10 ** 8)
    target = sandbox.safe_path(name)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(image)
    tmp.replace(target)
    return {
        "ok": True,
        "path": name,
        "name": name,
        "size": len(image),
        "download_url": sandbox.dl(name),
        "preview_url": sandbox.dl(name),
        "prompt": prompt,
        "model": (used or "flux") + " · free",
        "provider": "free",
        "watermark": False,
    }


def generate_image(prompt: str, width: int = 1024, height: int = 1024, style: str = "") -> Dict[str, Any]:
    """Создать ровно одно watermark-free изображение: gateway → GigaChat → free."""
    provider = str(CONFIG.get("media.image_provider", "auto") or "auto")
    if provider == "off":
        return {"ok": False, "error": "генерация изображений выключена в настройках"}
    if provider not in ("auto", "gateway", "yandex", "gigachat", "free"):
        return {"ok": False, "error": "неподдерживаемый провайдер изображений: " + provider}
    clean = (str(prompt or "") + ((", " + str(style).strip()) if str(style or "").strip() else "")).strip()
    if not clean:
        return {"ok": False, "error": "нужен промпт для изображения"}

    try:
        refined = _enhance_prompt(clean, int(width or 1024), int(height or 1024))
        has_gateway = bool(CONFIG.get("media.image_gateway_url", "") and
                           CONFIG.get("media.image_gateway_token", ""))
        # BM7: настроенный Яндекс рисует ПЕРВЫМ — у него нет водяных знаков
        # и это выбор человека; release gateway (бесплатный pollinations)
        # теперь ЗАПАСНОЙ, а не основной маршрут
        yandex_ready = bool(
            str((CONFIG.get("providers.yandex") or {}).get("api_key") or "").strip()
            and str((CONFIG.get("providers.yandex") or {}).get("folder_id") or "").strip())
        yandex_error = ""
        if provider == "yandex" or (provider == "auto" and yandex_ready):
            try:
                return _yandex_image(refined, int(width or 1024), int(height or 1024))
            except GigaChatError as exc:
                if provider == "yandex":
                    raise
                # Яндекс не ответил — едем дальше, но причину сохраним для
                # финальной ошибки: человек должен знать, ЧТО именно сломалось
                yandex_error = str(exc)
        if provider == "gateway" or (provider == "auto" and has_gateway):
            return _gateway_image(refined, int(width or 1024), int(height or 1024))
        if provider == "free":
            return _free_image(refined, int(width or 1024), int(height or 1024))
        if not str(CONFIG.get("media.gigachat_auth_key", "") or "").strip():
            # X: БЕЗ КЛЮЧА GIGACHAT ГЕНЕРАЦИЯ ВСЁ РАВНО РАБОТАЕТ — в любом
            # положении провайдера. Кнопка настроек однажды ставила
            # provider='gigachat' и оставляла его без ключа навсегда: каждый
            # запрос падал «вставь ключ». Форс без ключа — это незавершённая
            # настройка, а не приказ отказать: открытый flux рисует из коробки.
            res = _free_image(refined, int(width or 1024), int(height or 1024))
            # BM8: всё упало — человек видит ПРИЧИНУ, начиная с Яндекса
            if not res.get("ok") and yandex_error:
                return {"ok": False,
                        "error": "Яндекс не нарисовал (%s); бесплатный "
                                 "генератор тоже не ответил. Проверьте "
                                 "folder_id и роль ai.imageGeneration.user "
                                 "у ключа Яндекса." % yandex_error[:160]}
            return res

        model = str(CONFIG.get("media.gigachat_model", "GigaChat") or "GigaChat").strip()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": (
                    "Ты арт-директор. Для запроса пользователя обязательно вызови text2image. "
                    "Создай качественное изображение без текста, логотипов, рамок и водяных знаков.")},
                {"role": "user", "content": "Нарисуй изображение: " + refined},
            ],
            "function_call": "auto",
        }

        token = _access_token()
        try:
            answer = _json_request("/chat/completions", payload, token)
        except GigaChatError as exc:
            if exc.status != 401:
                raise
            token = _access_token(force=True)
            answer = _json_request("/chat/completions", payload, token)

        file_id = _image_id(answer)
        try:
            image, suffix = _image_bytes(file_id, token)
        except GigaChatError as exc:
            if exc.status != 401:
                raise
            token = _access_token(force=True)
            image, suffix = _image_bytes(file_id, token)

        name = "gigachat_image_%s%s" % (file_id[:8], suffix)
        target = sandbox.safe_path(name)
        # UUID делает имя idempotent для одного результата. replace не оставляет
        # частичный JPG при аварии питания или полном диске.
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(image)
        tmp.replace(target)
        return {
            "ok": True,
            "path": name,
            "name": name,
            "size": len(image),
            "download_url": sandbox.dl(name),
            "preview_url": sandbox.dl(name),
            "prompt": refined,
            "model": model + " · text2image",
            "provider": "gigachat",
            "watermark": False,
            "image_id": file_id,
        }
    except GigaChatError as exc:
        if provider in ("auto", "gigachat"):
            # X: платный маршрут споткнулся — бесплатный flux доедет до конца
            # (в том числе когда форсированный gigachat упал по своей вине:
            # пользователь просил картинку, а не разбор поломки)
            try:
                return _free_image(refined, int(width or 1024), int(height or 1024))
            except Exception:
                pass
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        if provider == "auto":
            try:
                return _free_image(refined, int(width or 1024), int(height or 1024))
            except Exception:
                pass
        return {"ok": False, "error": "не удалось сохранить изображение: %s" % exc}


def _ffmpeg() -> str | None:
    for candidate in ("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        try:
            subprocess.run([candidate, "-version"], capture_output=True, timeout=10, check=True)
            return candidate
        except Exception:
            continue
    return None


# ============================ РАСПОЗНАВАНИЕ РЕЧИ ============================
# Пять «почему» по жалобе на микрофон:
#   1. Почему лавина уведомлений? Браузерный путь и серверный перекидывали
#      работу друг другу, и каждый перескок показывал свой тост.
#   2. Почему они перекидывали? Серверный отвечал browser_asr:true, а
#      браузерный при ошибке сети уходил обратно на сервер.
#   3. Почему сервер отвечал browser_asr? Он не находил ASR-модель у Cloud.ru.
#   4. Почему не находил? Он опрашивал /v1/audio/transcriptions.
#   5. Почему это не работает? В официальной спецификации Foundation Models
#      РОВНО два метода: /v1/models и /v1/chat/completions. Эндпоинта
#      распознавания речи у провайдера нет вообще — ни у одной модели.
# Значит, искать его бессмысленно. Речь распознаётся там, где это реально
# возможно: моделью-«ушами» через обычный chat/completions (input_audio) —
# сначала у Cloud.ru, если в каталоге есть модель типа audio-to-text, затем
# у бесплатного открытого сервиса. Ответ один и окончательный: либо текст,
# либо честное «серверное распознавание недоступно» — без пинг-понга.

_PROBE_WAV = None
_ASR_ROUTE: Any = None          # кэш: каким путём речь реально распозналась
_ASR_ROUTE_AT = 0.0


def _probe_wav() -> bytes:
    """Крошечный корректный wav (0.1 с тишины) — им проверяем, ASR ли модель."""
    global _PROBE_WAV
    if _PROBE_WAV is None:
        frames = b"\x00\x00" * 1600            # 0.1 c при 16 кГц, 16 бит моно
        hdr = (b"RIFF" + (36 + len(frames)).to_bytes(4, "little") + b"WAVEfmt "
               + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
               + (1).to_bytes(2, "little") + (16000).to_bytes(4, "little")
               + (32000).to_bytes(4, "little") + (2).to_bytes(2, "little")
               + (16).to_bytes(2, "little") + b"data"
               + len(frames).to_bytes(4, "little"))
        _PROBE_WAV = hdr + frames
    return _PROBE_WAV


def _to_wav(src: Path) -> Path:
    """Привести запись к wav 16 кГц моно, если рядом есть ffmpeg."""
    ff = _ffmpeg()
    if not ff or src.suffix.lower() in (".wav", ".mp3"):
        return src
    wav = src.with_suffix(".wav")
    try:
        subprocess.run([ff, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(wav)],
                       capture_output=True, timeout=120)
        if wav.exists() and wav.stat().st_size > 200:
            return wav
    except Exception:
        pass
    return src


def _audio_mime(path: Path) -> tuple:
    """(формат для API, mime) по расширению файла."""
    ext = path.suffix.lower().lstrip(".") or "wav"
    if ext == "m4a":
        ext = "mp4"
    mimes = {"wav": "audio/wav", "mp3": "audio/mpeg", "webm": "audio/webm",
             "ogg": "audio/ogg", "mp4": "audio/mp4", "flac": "audio/flac"}
    return ext, mimes.get(ext, "application/octet-stream")


def _asr_via_transcriptions(conf: Dict[str, Any], model: str, path: Path,
                            language: str, timeout: int = 180) -> Dict[str, Any]:
    """Стандартный OpenAI-совместимый /audio/transcriptions (multipart).

    Аудиомодели провайдера (whisper и родня) живут именно на этом эндпоинте:
    раньше аудио отправлялось в chat/completions — провайдер отвечал 404,
    и микрофон не работал вовсе."""
    fmt, mime = _audio_mime(path)
    boundary = "----jarvisasr%d" % int(time.time() * 1000)
    fields = [("model", model), ("language", language or "ru"),
              ("response_format", "json")]
    parts = [("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
              % (boundary, k, v)).encode() for k, v in fields]
    parts += [
        ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
         "Content-Type: %s\r\n\r\n" % (boundary, path.name, mime)).encode(),
        path.read_bytes(),
        ("\r\n--%s--\r\n" % boundary).encode(),
    ]
    url = conf["base_url"].rstrip("/") + "/audio/transcriptions"
    try:
        req = urllib.request.Request(url, data=b"".join(parts), method="POST", headers={
            "Authorization": "Bearer " + conf["api_key"],
            "Content-Type": "multipart/form-data; boundary=" + boundary})
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        return {"http_ok": True, "text": (body.get("text") or "").strip()}
    except Exception as exc:
        return {"http_ok": False, "error": str(exc)[:200]}


def _asr_via_chat(conf: Dict[str, Any], model: str, path: Path,
                  language: str, timeout: int = 180) -> Dict[str, Any]:
    """Распознать речь моделью-«ушами» через обычный chat/completions.

    Это единственный метод, который у Cloud.ru вообще существует, поэтому и
    речь идёт через него: аудио передаётся частью сообщения (input_audio),
    ровно как картинка в vision.
    """
    fmt, _mime = _audio_mime(path)
    b64 = base64.b64encode(path.read_bytes()).decode()
    url = conf["base_url"].rstrip("/") + "/chat/completions"
    # X: whisper-large-v3 в каталоге Cloud.ru имеет контекст 448 токенов —
    # прежний max_tokens:1200 отправлял запрос в 400, и «ушки» не отвечали
    # никогда. Теперь лимит внутри контекста, и на всякий случай пробуем
    # ДВЕ формы: с текстовой инструкцией и чистое аудио (у аудиомоделей
    # свои капризы к лишнему тексту).
    shapes = (
        [
            {"type": "text", "text":
                "Запиши текст этой аудиозаписи дословно на языке оригинала (%s). "
                "Верни ТОЛЬКО расшифровку." % language},
            {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}},
        ],
        [
            {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}},
        ],
    )
    last_error = ""
    for content in shapes:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 200,
            "temperature": 0,
        }
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"), method="POST",
                headers={"Authorization": "Bearer " + conf["api_key"],
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            msg = ((body.get("choices") or [{}])[0].get("message") or {})
            text = (msg.get("content") or "").strip().strip('"«»')
            if text:
                return {"http_ok": True, "text": text}
            last_error = "пустая расшифровка"
        except Exception as exc:
            last_error = str(exc)[:200]
    return {"http_ok": False, "error": last_error}


def _asr_free(path: Path, language: str, timeout: int = 180) -> Dict[str, Any]:
    """Открытый бесплатный Whisper: работает без ключа и без VPN."""
    fmt, mime = _audio_mime(path)
    boundary = "----jarvisasr%d" % int(time.time() * 1000)
    fields = [("model", "whisper-large-v3"), ("language", language or "ru"),
              ("response_format", "json")]
    parts = [("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
              % (boundary, k, v)).encode() for k, v in fields]
    parts += [
        ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
         "Content-Type: %s\r\n\r\n" % (boundary, path.name, mime)).encode(),
        path.read_bytes(),
        ("\r\n--%s--\r\n" % boundary).encode(),
    ]
    url = CONFIG.get("media.asr_base", "https://gen.pollinations.ai/v1/audio/transcriptions")
    try:
        req = urllib.request.Request(url, data=b"".join(parts), method="POST", headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary, "User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        return {"http_ok": True, "text": (body.get("text") or "").strip()}
    except Exception as exc:
        return {"http_ok": False, "error": str(exc)[:200]}


def _asr_routes() -> list:
    """Все способы распознать речь, от лучшего к запасному.

    Модели Cloud.ru отбираются по ТИПУ из каталога («audio-to-text»), который
    сообщает сам провайдер, — а не по словам в названии. Если таких моделей
    нет, остаётся открытый Whisper: он бесплатный и доступен из России.
    """
    from .. import llm

    routes = []
    conf = llm.provider_conf("cloudru")
    if conf.get("api_key"):
        prefs = [p for p in (CONFIG.get("model_tiers.audio", []) or []) if p]
        catalog = llm.models_of_type("cloudru", "audio")
        for name in prefs + [m for m in catalog if m not in prefs]:
            # X: chat/completions — ЕДИНСТВЕННЫЙ путь, который у Cloud.ru
            # существует (OpenAPI-спецификация: только /models и
            # /chat/completions; /audio/transcriptions отвечает 404 и лишь
            # сжигал попытку). Потому chat идёт первым, transcriptions —
            # запасным на случай, когда провайдер его добавит.
            routes.append(("cloudru-chat:" + name,
                           lambda p, l, m=name, c=conf: _asr_via_chat(c, m, p, l)))
            routes.append(("cloudru-ts:" + name,
                           lambda p, l, m=name, c=conf: _asr_via_transcriptions(c, m, p, l)))
    routes.append(("free:whisper-large-v3", _asr_free))
    return routes


def transcribe_audio(path_or_data_url: str, language: str = "ru") -> Dict[str, Any]:
    """Распознать речь из аудиофайла. Ответ окончательный: текст либо отказ."""
    global _ASR_ROUTE, _ASR_ROUTE_AT
    _asr_tmp = None

    if path_or_data_url.startswith("data:"):
        header, _, b64 = path_or_data_url.partition(",")
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return {"ok": False, "error": "не удалось прочитать запись"}
        # Safari пишет audio/mp4, Chrome — audio/webm; раньше mp4-байты
        # сохранялись как .wav, и распознавание падало на первом же шаге
        low = header.lower()
        if "webm" in low:
            ext = "webm"
        elif "mp4" in low or "m4a" in low:
            ext = "mp4"
        elif "mpeg" in low or "mp3" in low:
            ext = "mp3"
        elif "ogg" in low or "opus" in low:
            ext = "ogg"
        elif "wav" in low:
            ext = "wav"
        else:
            ext = "webm"
        # AG: КОРЕНЬ «ГОЛОСОВЫЕ ОСТАЮТСЯ В ФАЙЛАХ» — запись сохранялась в
        # песочницу как voice_*.webm и жила там вечно. Голос — служебный груз
        # одного запроса: живёт во временном каталоге системы и исчезает
        # сразу после распознавания.
        _asr_tmp = tempfile.TemporaryDirectory(prefix="jarvis-asr-")
        src = Path(_asr_tmp.name) / ("voice.%s" % ext)
        src.write_bytes(raw)
    else:
        src = Path(path_or_data_url)
        if not src.is_absolute():
            src = _ws() / path_or_data_url
        if not src.exists():
            return {"ok": False, "error": "аудиофайл не найден"}

    try:
        return _transcribe_with(src, language)
    finally:
        # временная запись исчезает — каким бы ни был результат
        try:
            if _asr_tmp:
                _asr_tmp.cleanup()
        except Exception:
            pass


def _transcribe_with(src: Path, language: str) -> Dict[str, Any]:
    global _ASR_ROUTE, _ASR_ROUTE_AT
    if src.stat().st_size < 1200:
        return {"ok": False, "error": "Запись слишком короткая — я ничего не услышал."}

    upload = _to_wav(src)
    routes = _asr_routes()
    # путь, который сработал в прошлый раз, пробуем первым — экономим время
    if _ASR_ROUTE and time.time() - _ASR_ROUTE_AT < 3600:
        routes.sort(key=lambda r: 0 if r[0] == _ASR_ROUTE else 1)

    # ГЛУБОКИЙ КОРЕНЬ «микрофон не работает»: первый маршрут, ответивший 200
    # с ПУСТЫМ текстом (модель-«ушки» без слуха, кривой формат), останавливал
    # всю цепочку словами «Тишина» — хотя запасные маршруты (включая открытый
    # Whisper) услышали бы всё. Пустой ответ = тишина ТОЛЬКО этого маршрута:
    # пробуем следующий. «Тишина» честная лишь когда ВСЕ замолчали.
    errors = []
    silent = []
    for label, fn in routes:
        res = fn(upload, language)
        if res.get("http_ok"):
            text = (res.get("text") or "").strip()
            if text:
                _ASR_ROUTE, _ASR_ROUTE_AT = label, time.time()
                return {"ok": True, "text": text, "model": label}
            silent.append(label)
            continue
        errors.append("%s: %s" % (label, res.get("error", "")))
    _ASR_ROUTE = None
    if silent and not errors:
        return {"ok": False, "error": "Тишина — слов не разобрал. Скажи ещё раз."}
    # Человеческое сообщение вместо стека технических ошибок: пользователь
    # не должен читать про 404 и чужие unauthorized.
    return {"ok": False,
            "error": "Не получилось распознать речь — сервисы распознавания "
                     "сейчас недоступны. Попробуй ещё раз или набери текст."}


# BM10: МЕДИА ИЗ ИНТЕРНЕТА. Джарвис приносит человеку картинку, аудио или
# видео по прямой ссылке и открывает их НАТИВНО в ответе (видео — сразу
# воспроизводится, по умолчанию без звука). YouTube в РФ без VPN недоступен
# на уровне серверов Google — честно говорим об этом и предлагаем прямые
# ссылки либо RuTube/VK Видео.
_MEDIA_KINDS = {
    "image": ((".png", ".jpg", ".jpeg", ".gif", ".webp"), 25 * 1024 * 1024),
    "audio": ((".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".flac"), 60 * 1024 * 1024),
    "video": (".mp4 .webm .mov .m4v".split(), 250 * 1024 * 1024),
}
_YOUTUBE_RE = re.compile(
    r"(?i)(youtube\.com|youtu\.be|youtube-nocookie\.com)")


def _ext_of(url: str, content_type: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp3", ".m4a",
                ".wav", ".ogg", ".opus", ".aac", ".flac", ".mp4", ".webm",
                ".mov", ".m4v"):
        if path.endswith(ext):
            return ext
    ct = str(content_type or "").split(";")[0].strip().lower()
    return {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif",
            ".webp": "image/webp", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
            ".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/ogg",
            ".flac": "audio/flac", ".mp4": "video/mp4", ".webm": "video/webm",
            ".mov": "video/quicktime"}.get(ct) or (".jpg" if ct.startswith("image/")
                                                  else ".mp4" if ct.startswith("video/")
                                                  else ".mp3" if ct.startswith("audio/") else "")


# BM13: ЧТО МОЖЕТ БЫТЬ МЕДИА ВНУТРИ СТРАНИЦЫ. Модель даёт обычную ссылку
# на страницу (новость, плеер, страницу минусовки) — инструмент сам находит
# в HTML прямой файл: og:video/og:audio, <video src>, <source src>,
# <audio src> и голые ссылки на медиа-файлы
_PAGE_MEDIA_RE = re.compile(
    r"""(?:property|name)=["'](?:og:video(?::secure_url|:url)?|og:audio(?::secure_url|:url)?|"""
    r"""twitter:player:stream)["']\s+content=["']([^"']+)["']|"""
    r"""content=["']([^"']+)["']\s+(?:property|name)=["'](?:og:video(?::secure_url|:url)?|og:audio(?::secure_url|:url)?|twitter:player:stream)["']|"""
    r"""<(?:video|audio|source)[^>]+src=["']([^"']+)["']|"""
    r"""["'](https?://[^"'\s]+\.(?:mp4|webm|mov|m4v|mp3|wav|m4a|ogg|opus|aac|flac)(?:\?[^"'\s]*)?)["']""",
    re.IGNORECASE)
_PAGE_HTML_LIMIT = 3 * 1024 * 1024   # читаем до 3 МБ html — глубже медиа не лежит


def _media_from_page(html: str, base: str) -> str:
    """Первая прямая медиа-ссылка, найденная в HTML страницы."""
    for m in _PAGE_MEDIA_RE.finditer(html or ""):
        for group in m.groups():
            cand = (group or "").strip()
            if not cand:
                continue
            cand = urllib.parse.urljoin(base, cand)
            if _YOUTUBE_RE.search(cand):
                continue
            low = urllib.parse.urlparse(cand).path.lower()
            if any(low.endswith("." + e) for e in
                   ("mp4", "webm", "mov", "m4v", "mp3", "wav", "m4a",
                    "ogg", "opus", "aac", "flac")):
                return cand
            # og:video без расширения — всё равно файл, верим разметке
            return cand
    return ""


_STREAMING_RE = re.compile(
    r"(?:music\.yandex|spotify\.com|zvuk\.com|apple\.com/music|deezer)", re.IGNORECASE)


_QUERY_EMBED_HINT = (
    "ссылка не нужна: покажи запрос текстом или воспользуйся web_search и "
    "передай show_media найденную страницу/файл")

# BM18: стоки и платные медиатеки — глухой край: плеера у них нет, файл
# прячут за оплатой, а роботов встречают 403. Иметь их в результате —
# значит гарантированно НЕ найти файл. Скипаем сразу
_STOCK_RE = re.compile(
    r"(?:dreamstime|pikbest|shutterstock|gettyimages|istockphoto|"
    r"stock\.adobe|depositphotos|123rf|alamy|storyblocks|pond5|artlist|"
    r"epidemicsound|freepik|vecteezy|mixkit|motionarray|motionarray\.com|"
    r"magnific\.ai|freepik\.com|vecteezy\.com)", re.IGNORECASE)

# прямое расширение файла в ссылке — самый желанный результат
_DIRECT_FILE_RE = re.compile(
    r"\.mp[34]|\.m4a|\.webm|\.ogg|\.mov|\.m3u8?$|\.aac", re.IGNORECASE)


def _looks_like_domain(s: str) -> bool:
    return bool(re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", s, re.IGNORECASE))


def _media_from_query(query: str) -> Dict[str, Any]:
    """BM17: show_media умеет и ПОИСК. Модель передаёт текстовый запрос —
    сами ищем прямые файлы (mp3/mp4) и страницы с медиа, пробуем по
    очереди, первый успех уходит в чат. Прежде модель «не могла найти
    ни одного mp3/mp4 в интернете»: она не умеет искать прямые ссылки
    сама — теперь это дело инструмента."""
    from . import web as web_tools
    q = re.sub(r"\s+", " ", str(query or "")).strip()
    if not q:
        return {"ok": False, "error": "пустой запрос"}
    # BM18: интент запроса задаёт порядок вариантов — видео-просьба не
    # начинает жизнь с mp3-поиска, и наоборот
    low = q.lower()
    want_video = any(w in low for w in ("видео", "фильм", "клип", "ролик",
                                        "мультфильм", "трейлер"))
    want_audio = any(w in low for w in ("музык", "песн", "трек", "mp3",
                                        "аудиокниг", "звук", "минус"))
    video_variants = ["%s filetype:mp4" % q, "%s mp4 скачать" % q,
                      "%s видео mp4 прямая ссылка" % q, "%s смотреть mp4" % q]
    audio_variants = ["%s filetype:mp3" % q, "%s скачать mp3" % q,
                      "%s mp3 слушать" % q, "%s аудио" % q]
    if want_audio:
        variants = audio_variants + video_variants + [q]
    elif want_video:
        variants = video_variants + audio_variants + [q]
    else:
        variants = [q, "%s filetype:mp3" % q, "%s filetype:mp4" % q,
                    "%s скачать mp3" % q, "%s mp4" % q]

    tried = 0
    tried_links = []
    # проход 1 — ссылки на ПРЯМЫЕ ФАЙЛЫ (самый надёжный результат),
    # проход 2 — обычные страницы (в них ищем плеер/og:video)
    for pass_no in (1, 2):
        for v in variants:
            try:
                res = web_tools.web_search(v, count=6)
            except Exception as exc:
                continue
            for item in (res or {}).get("results") or []:
                link = str(item.get("url") or "").strip()
                if not link or not link.lower().startswith("http"):
                    continue
                if link in tried_links:
                    continue
                if (_YOUTUBE_RE.search(link) or _STREAMING_RE.search(link)
                        or _STOCK_RE.search(link)):
                    continue
                if pass_no == 1 and not _DIRECT_FILE_RE.search(link):
                    continue
                tried_links.append(link)
                tried += 1
                if tried > 14:
                    break
                out = show_media(link, _depth=1)
                if out.get("ok"):
                    return out
            if tried > 14:
                break
        if tried > 14:
            break
    return {"ok": False,
            "error": "по запросу «%s» не нашлось медиа, которое ложится "
                     "прямо в чат (прямой файл или страница с плеером). "
                     "Попробуй другой запрос или дай конкретную ссылку. %s"
                     % (q[:60], _QUERY_EMBED_HINT)}


def show_media(url: str, _depth: int = 0) -> Dict[str, Any]:
    """Показать человеку медиа из интернета: картинку, аудио или видео.

    Видео открывается нативным плеером и сразу воспроизводится (без звука).
    Принимает прямую ссылку на файл, обычную страницу (сам находит в ней
    медиа: og:video, плеер, ссылку на файл) и ТЕКСТОВЫЙ ЗАПРОС — тогда
    сам ищет прямые файлы и страницы с медиа (BM17). YouTube в России
    заблокирован — честно отказываем и предлагаем RuTube/VK Видео."""
    src = str(url or "").strip()
    if not src:
        return {"ok": False, "error": "нужна ссылка на медиа"}
    # BM17: не ссылка, а текстовый запрос — работаем поиском
    if (_depth == 0 and not src.lower().startswith(("http://", "https://"))
            and not _looks_like_domain(src)):
        return _media_from_query(src)
    if _depth == 0 and _looks_like_domain(src):
        src = "https://" + src
    if _YOUTUBE_RE.search(src):
        return {"ok": False,
                "error": "YouTube в России без VPN недоступен. НЕ давай ссылку "
                         "на YouTube в ответе. Поищи RuTube/VK Видео или прямой "
                         "файл (mp4) и вызови show_media с новой ссылкой."}
    # BM15: стриминги с DRM — честный отказ + инструкция модели искать
    # ТО, что ложится в чат (прямой файл / RuTube / VK), а не скидывать
    # ссылку на стриминг как «результат»
    if _STREAMING_RE.search(src):
        return {"ok": False,
                "error": "Это стриминг-сервис с защитой (DRM): прямого файла "
                         "здесь нет. НЕ давай ссылку на него в ответе. Поищи "
                         "прямой файл (запросы «filetype:mp3», «… скачать mp3») "
                         "или RuTube/VK Видео — и вызови show_media с новой "
                         "ссылкой."}
    parsed = urllib.parse.urlparse(src)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return {"ok": False, "error": "ссылка должна начинаться с http:// или https://"}

    req = urllib.request.Request(src, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_CTX) as resp:
            ctype = str(resp.headers.get("Content-Type") or "")
            declared = int(resp.headers.get("Content-Length") or 0)
            low = ctype.split(";")[0].lower()
            # BM13: ОБЫЧНАЯ СТРАНИЦА — не отказ, а поиск. Читаем html и
            # достаём прямую ссылку на медиа (og:video, плеер, файл)
            if ("html" in low or "xml" in low or "json" in low):
                html = b""
                while len(html) < _PAGE_HTML_LIMIT:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    html += chunk
                text = html.decode("utf-8", "replace")
                deep = _media_from_page(text, src)
                if not deep:
                    return {"ok": False,
                            "error": "на этой странице не нашлось медиа-файла "
                                     "(плеер внешний или защищён). Дай прямую "
                                     "ссылку на файл (mp4/mp3) или другую "
                                     "страницу — например, RuTube/VK Видео."}
                # нашли — идём за файлом (страницы-ссылки-на-страницы
                # ограничены глубиной, цикл A→B→A невозможен)
                if _depth >= 2:
                    return {"ok": False,
                            "error": "слишком много переходов страниц — дай "
                                     "прямую ссылку на медиа-файл"}
                return show_media(deep, _depth + 1)
            if low.startswith("image/"):
                kind = "image"
            elif low.startswith("audio/"):
                kind = "audio"
            elif low.startswith("video/"):
                kind = "video"
            else:
                # тип не сказали — пробуем угадать по расширению пути
                ext0 = _ext_of(src, "")
                kind = next((k for k, (exts, _) in _MEDIA_KINDS.items()
                             if ext0 in exts), "")
                if not kind:
                    return {"ok": False,
                            "error": "это не медиа-файл (тип: %s). Нужна "
                                     "прямая ссылка на картинку, аудио или "
                                     "видео" % (ctype or "неизвестен")}
            exts, cap = _MEDIA_KINDS[kind]
            if declared and declared > cap:
                return {"ok": False,
                        "error": "файл слишком большой (%s) — максимум %d МБ"
                                 % (fmt_mb(declared), cap // 1024 // 1024)}
            ext = _ext_of(src, ctype) or exts[0]
            data = b""
            while True:
                chunk = resp.read(1024 * 512)
                if not chunk:
                    break
                data += chunk
                if len(data) > cap:
                    return {"ok": False,
                            "error": "файл больше %d МБ — обрываю загрузку"
                                     % (cap // 1024 // 1024)}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "сервер ответил HTTP %s по этой ссылке" % exc.code}
    except Exception as exc:
        return {"ok": False, "error": "не удалось скачать: %s" % str(exc)[:160]}
    if not data:
        return {"ok": False, "error": "по ссылке пустой файл"}

    name = "media_%s%s" % (hashlib.sha256(data).hexdigest()[:12], ext)
    target = sandbox.safe_path(name)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    return {"ok": True, "path": name, "name": name, "kind": kind,
            "size": len(data), "download_url": sandbox.dl(name),
            "source_url": src[:300]}


def fmt_mb(n: int) -> str:
    return "%.1f МБ" % (n / 1024 / 1024)


def analyze_image(image_ref: str, question: str = "Что на изображении? Опиши подробно.") -> Dict[str, Any]:
    """Понять, что на картинке/кадре камеры/скриншоте."""
    from .. import llm

    data_url = image_ref
    if not image_ref.startswith("data:"):
        path = Path(image_ref)
        if not path.is_absolute():
            path = _ws() / image_ref
        if not path.exists():
            return {"ok": False, "error": "изображение не найдено: " + image_ref}
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        data_url = "data:%s;base64,%s" % (mime, base64.b64encode(path.read_bytes()).decode())
    try:
        answer = llm.vision(question, data_url)
        return {"ok": True, "answer": answer, "question": question}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def analyze_video(path: str, question: str = "Что происходит на видео?", frames: int = 4) -> Dict[str, Any]:
    """Разбор видео: вытаскиваем кадры и анализируем vision-моделью."""
    ff = _ffmpeg()
    src = Path(path)
    if not src.is_absolute():
        src = _ws() / path
    if not src.exists():
        return {"ok": False, "error": "видео не найдено"}
    if not ff:
        return {"ok": False, "error": "нужен ffmpeg: brew install ffmpeg"}
    outdir = _ws() / ("frames_%d" % int(time.time()))
    outdir.mkdir(exist_ok=True)
    try:
        subprocess.run([ff, "-y", "-i", str(src), "-vf", "fps=1/5,scale=768:-1",
                        "-frames:v", str(frames), str(outdir / "f_%02d.jpg")],
                       capture_output=True, timeout=240)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    shots = sorted(outdir.glob("*.jpg"))[:frames]
    if not shots:
        return {"ok": False, "error": "не удалось извлечь кадры"}
    notes = []
    for shot in shots:
        res = analyze_image(str(shot), "Опиши кратко, что на кадре.")
        if res.get("ok"):
            notes.append("• " + res["answer"][:400])
    audio_text = ""
    wav = outdir / "audio.wav"
    try:
        subprocess.run([ff, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(wav)],
                       capture_output=True, timeout=180)
        if wav.exists() and wav.stat().st_size > 2000:
            tr = transcribe_audio(str(wav))
            audio_text = tr.get("text", "") if tr.get("ok") else ""
    except Exception:
        pass
    return {"ok": True, "frames": len(shots), "observations": notes,
            "speech": audio_text[:4000], "question": question}


def send_telegram(text: str, silent: bool = False) -> Dict[str, Any]:
    """Отправить уведомление себе в Telegram."""
    conf = CONFIG.get("telegram", {}) or {}
    if not conf.get("enabled") or not conf.get("bot_token") or not conf.get("chat_id"):
        return {"ok": False, "error": "Telegram не настроен (Настройки → Telegram)"}
    url = "https://api.telegram.org/bot%s/sendMessage" % conf["bot_token"]
    payload = json.dumps({
        "chat_id": conf["chat_id"], "text": text[:4000],
        "parse_mode": "HTML", "disable_notification": bool(silent),
    }).encode()
    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25, context=_CTX) as resp:
            body = json.loads(resp.read().decode())
        return {"ok": bool(body.get("ok")), "result": "отправлено"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def telegram_send_file(path: str, caption: str = "") -> Dict[str, Any]:
    conf = CONFIG.get("telegram", {}) or {}
    if not conf.get("enabled") or not conf.get("bot_token") or not conf.get("chat_id"):
        return {"ok": False, "error": "Telegram не настроен"}
    src = _ws() / path
    if not src.exists():
        return {"ok": False, "error": "файл не найден"}
    boundary = "----jarvisfile%d" % int(time.time())
    parts = [
        ("--%s\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n%s\r\n" % (boundary, conf["chat_id"])).encode(),
        ("--%s\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n%s\r\n" % (boundary, caption[:900])).encode(),
        ("--%s\r\nContent-Disposition: form-data; name=\"document\"; filename=\"%s\"\r\n"
         "Content-Type: application/octet-stream\r\n\r\n" % (boundary, src.name)).encode(),
        src.read_bytes(),
        ("\r\n--%s--\r\n" % boundary).encode(),
    ]
    try:
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendDocument" % conf["bot_token"],
            data=b"".join(parts),
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        with urllib.request.urlopen(req, timeout=120, context=_CTX) as resp:
            body = json.loads(resp.read().decode())
        return {"ok": bool(body.get("ok"))}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

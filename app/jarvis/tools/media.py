"""Медиа-инструменты JARVIS: изображения через GigaChat и резервный ASR."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .. import llm, sandbox
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


def _enhance_prompt(prompt: str, width: int, height: int) -> str:
    """Дешёвая модель уточняет сцену; при сбое исходная просьба не теряется."""
    if not CONFIG.get("media.enhance_prompt", True):
        return prompt
    ratio = "квадратный кадр"
    if width > height:
        ratio = "горизонтальный кадр"
    elif height > width:
        ratio = "вертикальный кадр"
    instruction = (
        "Ты арт-директор. Перепиши запрос как один точный промпт для современной "
        "генерации изображения. Сохрани сюжет, добавь композицию, свет, фактуру и "
        "стиль. Не добавляй надписи, логотипы и watermark. Формат: %s. Ответь "
        "только готовым промптом на русском, до 900 знаков.\n\nЗапрос: %s"
    ) % (ratio, prompt)
    try:
        response = llm.chat([{"role": "user", "content": instruction}], tier="nano",
                            max_tokens=450, temperature=0.45)
        text = str(response.get("content") or "").strip()
        return text[:1400] if text else prompt
    except Exception:
        return prompt


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


def generate_image(prompt: str, width: int = 1024, height: int = 1024, style: str = "") -> Dict[str, Any]:
    """Создать ровно одно watermark-free изображение через gateway или GigaChat."""
    provider = str(CONFIG.get("media.image_provider", "auto") or "auto")
    if provider == "off":
        return {"ok": False, "error": "генерация изображений выключена в настройках"}
    if provider not in ("auto", "gateway", "gigachat"):
        return {"ok": False, "error": "неподдерживаемый провайдер изображений: " + provider}
    clean = (str(prompt or "") + ((", " + str(style).strip()) if str(style or "").strip() else "")).strip()
    if not clean:
        return {"ok": False, "error": "нужен промпт для изображения"}

    try:
        refined = _enhance_prompt(clean, int(width or 1024), int(height or 1024))
        has_gateway = bool(CONFIG.get("media.image_gateway_url", "") and
                           CONFIG.get("media.image_gateway_token", ""))
        if provider == "gateway" or (provider == "auto" and has_gateway):
            return _gateway_image(refined, int(width or 1024), int(height or 1024))
        if provider == "auto" and not CONFIG.get("media.gigachat_auth_key", ""):
            raise GigaChatError("облачная генерация не подключена в этой сборке")

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
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
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
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text":
                    "Запиши текст этой аудиозаписи дословно на языке оригинала (%s). "
                    "Верни ТОЛЬКО расшифровку, без комментариев, кавычек и пояснений. "
                    "Если речи нет — верни пустую строку." % language},
                {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}},
            ],
        }],
        "max_tokens": 1200,
        "temperature": 0,
    }
    url = conf["base_url"].rstrip("/") + "/chat/completions"
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + conf["api_key"],
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        msg = ((body.get("choices") or [{}])[0].get("message") or {})
        text = (msg.get("content") or "").strip().strip('"«»')
        return {"http_ok": True, "text": text}
    except Exception as exc:
        return {"http_ok": False, "error": str(exc)[:200]}


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
            # стандартный transcriptions-эндпоинт — основной путь для аудиомоделей
            routes.append(("cloudru-ts:" + name,
                           lambda p, l, m=name, c=conf: _asr_via_transcriptions(c, m, p, l)))
            # запасной: некоторые «ушки» принимают аудио прямо в chat
            routes.append(("cloudru-chat:" + name,
                           lambda p, l, m=name, c=conf: _asr_via_chat(c, m, p, l)))
    routes.append(("free:whisper-large-v3", _asr_free))
    return routes


def transcribe_audio(path_or_data_url: str, language: str = "ru") -> Dict[str, Any]:
    """Распознать речь из аудиофайла. Ответ окончательный: текст либо отказ."""
    global _ASR_ROUTE, _ASR_ROUTE_AT

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
        src = _ws() / ("voice_%d.%s" % (int(time.time()), ext))
        src.write_bytes(raw)
    else:
        src = Path(path_or_data_url)
        if not src.is_absolute():
            src = _ws() / path_or_data_url
        if not src.exists():
            return {"ok": False, "error": "аудиофайл не найден"}

    if src.stat().st_size < 1200:
        return {"ok": False, "error": "Запись слишком короткая — я ничего не услышал."}

    upload = _to_wav(src)
    routes = _asr_routes()
    # путь, который сработал в прошлый раз, пробуем первым — экономим время
    if _ASR_ROUTE and time.time() - _ASR_ROUTE_AT < 3600:
        routes.sort(key=lambda r: 0 if r[0] == _ASR_ROUTE else 1)

    errors = []
    for label, fn in routes:
        res = fn(upload, language)
        if res.get("http_ok"):
            text = (res.get("text") or "").strip()
            _ASR_ROUTE, _ASR_ROUTE_AT = label, time.time()
            if text:
                return {"ok": True, "text": text, "model": label}
            return {"ok": False, "error": "Тишина — слов не разобрал. Скажи ещё раз."}
        errors.append("%s: %s" % (label, res.get("error", "")))
    _ASR_ROUTE = None
    # Человеческое сообщение вместо стека технических ошибок: пользователь
    # не должен читать про 404 и чужие unauthorized.
    return {"ok": False,
            "error": "Не получилось распознать речь — сервисы распознавания "
                     "сейчас недоступны. Попробуй ещё раз или набери текст."}


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

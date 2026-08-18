"""Медиа: генерация изображений, распознавание речи/видео/камеры, TTS."""
from __future__ import annotations

import base64
import json
import re
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict

from ..config import CONFIG, WORKSPACE
from .. import sandbox

_CTX = ssl.create_default_context()
_UA = "Mozilla/5.0 JARVIS/1.0"


def _ws() -> Path:
    return sandbox.root()


def _dl(name: str) -> str:
    return sandbox.dl(name)


# ============================ ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ ============================
# Качество картинки определяют три вещи, и раньше мы не управляли ни одной:
#   1) какая модель рисует — мы молча брали умолчание сервиса (самое слабое);
#   2) насколько подробен промпт — мы слали короткую русскую фразу как есть,
#      а генераторы обучены на английских описаниях со светом, оптикой, стилем;
#   3) что делать, если модель не ответила — мы просто сдавались.
# Сервис к тому же выкинул параметр enhance, который раньше дорисовывал промпт
# за нас. Поэтому промпт теперь пишет наша собственная модель, а рисует лучшая
# из реально доступных — список берём у сервиса, а не из своей памяти.

# Порядок предпочтения: от сильных к простым. Имена, которых сегодня нет в
# каталоге, просто пропускаются — список не может «протухнуть» в худшую сторону.
_IMAGE_PREFS = [
    "nanobanana-pro", "nanobanana-2", "nanobanana",
    "seedream5-pro", "seedream5",
    "gptimage-large", "gpt-image-2", "gptimage",
    "ideogram-v4-quality", "ideogram-v4-balanced",
    "flux-2", "flux-pro", "flux", "krea", "zimage", "klein", "dreamshaper", "sana",
]
_IMAGE_MODELS: Any = None          # кэш каталога моделей рисования
_IMAGE_MODELS_AT = 0.0


def image_models() -> list:
    """Что сервис реально умеет рисовать прямо сейчас (кэш на час)."""
    global _IMAGE_MODELS, _IMAGE_MODELS_AT
    if _IMAGE_MODELS is not None and time.time() - _IMAGE_MODELS_AT < 3600:
        return _IMAGE_MODELS
    models = []
    try:
        req = urllib.request.Request("https://image.pollinations.ai/models",
                                     headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=25, context=_CTX) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        if isinstance(body, list):
            models = [m if isinstance(m, str) else str(m.get("name") or m.get("id") or "")
                      for m in body]
            models = [m for m in models if m]
    except Exception:
        models = []
    _IMAGE_MODELS, _IMAGE_MODELS_AT = models, time.time()
    return models


def _image_order() -> list:
    """Модели в порядке «сначала лучшая из доступных»."""
    live = image_models()
    forced = (CONFIG.get("media.image_model", "") or "").strip()
    order = [forced] if forced else []
    order += [m for m in _IMAGE_PREFS if not live or m in live]
    order += [m for m in live if m not in order]
    seen, out = set(), []
    for m in order:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out[:4] or [""]


def _art_prompt(prompt: str, style: str) -> str:
    """Превратить просьбу в подробное английское описание сцены.

    Это делает наша дешёвая модель — та же, что отвечает на короткие реплики.
    Стоит доли копейки, а разница в результате принципиальная: генераторы
    изображений понимают английский и «видят» свет, оптику, материалы.
    """
    raw = (prompt + (", " + style if style else "")).strip()
    if not CONFIG.get("media.enhance_prompt", True):
        return raw
    try:
        from .. import llm

        out = llm.chat([
            {"role": "system", "content":
                "You turn a short user request into ONE English prompt for a text-to-image model. "
                "Describe subject, composition, lighting, lens, materials, mood and style in 35-60 words. "
                "Keep every explicit detail the user asked for. No preamble, no quotes, no lists — "
                "output only the prompt itself."},
            {"role": "user", "content": raw[:600]},
        ], tier="nano", max_tokens=220, temperature=0.7).get("content", "")
        out = " ".join(out.split())
        if 20 < len(out) < 1200:
            return out
    except Exception:
        pass
    return raw


def generate_image(prompt: str, width: int = 1024, height: int = 1024, style: str = "") -> Dict[str, Any]:
    """Сгенерировать изображение по описанию (бесплатно, работает без VPN)."""
    provider = CONFIG.get("media.image_provider", "pollinations")
    if provider == "off":
        return {"ok": False, "error": "генерация изображений выключена в настройках"}
    width = max(256, min(int(width or 1024), 1536))
    height = max(256, min(int(height or 1024), 1536))
    full_prompt = _art_prompt(prompt, style)
    base = CONFIG.get("media.image_base", "https://image.pollinations.ai/prompt/")
    name = "img_%d.jpg" % int(time.time())
    dest = _ws() / name
    seed = int(time.time()) % 100000
    last = ""
    for model in _image_order():
        url = "%s%s?width=%d&height=%d&seed=%d&private=true%s" % (
            base, urllib.parse.quote(full_prompt[:1400]), width, height, seed,
            ("&model=" + urllib.parse.quote(model)) if model else "")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=180, context=_CTX) as resp:
                data = resp.read(20_000_000)
            if len(data) < 1000:
                last = "сервис вернул пустое изображение"
                continue
            dest.write_bytes(data)
            return {"ok": True, "path": name, "prompt": full_prompt, "model": model or "auto",
                    "download_url": _dl(name), "preview_url": _dl(name), "size": len(data)}
        except Exception as exc:
            last = str(exc)[:160]
            continue
    return {"ok": False, "error": "не удалось сгенерировать: %s" % (last or "сервис недоступен")}


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
            routes.append(("cloudru:" + name,
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
        ext = "webm" if "webm" in header else (
            "mp3" if "mpeg" in header else ("ogg" if "ogg" in header else "wav"))
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
    return {"ok": False,
            "error": "Распознавание речи сейчас недоступно: %s" % ("; ".join(errors)[:200])}


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

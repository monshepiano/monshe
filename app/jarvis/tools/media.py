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


def generate_image(prompt: str, width: int = 1024, height: int = 1024, style: str = "") -> Dict[str, Any]:
    """Сгенерировать изображение по описанию (бесплатно, работает без VPN)."""
    provider = CONFIG.get("media.image_provider", "pollinations")
    if provider == "off":
        return {"ok": False, "error": "генерация изображений выключена в настройках"}
    full_prompt = (prompt + (", " + style if style else "")).strip()
    base = CONFIG.get("media.image_base", "https://image.pollinations.ai/prompt/")
    url = "%s%s?width=%d&height=%d&nologo=true&seed=%d" % (
        base, urllib.parse.quote(full_prompt[:900]), width, height, int(time.time()) % 100000)
    name = "img_%d.jpg" % int(time.time())
    dest = _ws() / name
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=180, context=_CTX) as resp:
            data = resp.read(20_000_000)
        if len(data) < 1000:
            return {"ok": False, "error": "сервис вернул пустое изображение"}
        dest.write_bytes(data)
        return {"ok": True, "path": name, "prompt": full_prompt, "download_url": _dl(name),
                "preview_url": _dl(name), "size": len(data)}
    except Exception as exc:
        return {"ok": False, "error": "не удалось сгенерировать: %s" % exc}


def _ffmpeg() -> str | None:
    for candidate in ("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        try:
            subprocess.run([candidate, "-version"], capture_output=True, timeout=10, check=True)
            return candidate
        except Exception:
            continue
    return None


def _pick_audio_model() -> str:
    """Найти в каталоге провайдера реальную модель распознавания речи.

    Раньше модель угадывалась по подстрокам в имени («whisper», «audio»,
    «transcri»…). Это ловило посторонние модели: любая модель, у которой в
    названии оказалось слово «transcription», выглядела как ASR, уходила в
    /audio/transcriptions и микрофон молчал. Имя — не признак умения.

    Признак умения — сама возможность принять аудио. Проверяем её один раз
    честным запросом к /audio/transcriptions и запоминаем результат: модель,
    которая приняла аудиофайл, и есть ASR. Никаких списков слов.
    """
    from .. import llm

    global _AUDIO_MODEL
    if _AUDIO_MODEL is not None:
        return _AUDIO_MODEL

    prefs = [p for p in (llm.CONFIG.get("model_tiers.audio", []) or []) if p]
    available = llm.list_models("cloudru")

    # Имя задаёт только ОЧЕРЁДНОСТЬ проверки, а не сам выбор: подсказка
    # экономит запросы, но решает всегда ответ сервера. Поэтому модель с
    # «transcription» в названии больше не может быть выбрана по имени —
    # она просто проверяется раньше и отсеивается.
    def rank(name: str) -> int:
        low = name.lower()
        if name in prefs:
            return 0
        if any(w in low for w in ("whisper", "voxtral", "gigaam", "asr", "speech")):
            return 1
        return 2

    ordered = sorted(available, key=rank)[:12]      # дальше искать бессмысленно
    for name in ordered:
        if _accepts_audio(name):
            _AUDIO_MODEL = name
            return name
    _AUDIO_MODEL = ""
    return ""


_AUDIO_MODEL = None          # кэш: что реально приняло аудио
_PROBE_WAV = None


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


def _accepts_audio(model: str) -> bool:
    """Умеет ли модель принимать аудио: спрашиваем провайдера, а не имя."""
    from .. import llm

    conf = llm.provider_conf("cloudru")
    if not conf.get("api_key"):
        return False
    res = _post_audio(conf, model, "probe.wav", _probe_wav(), "ru", timeout=45)
    # модель-не-ASR отвечает 404/400 «model not found / not supported»,
    # настоящая ASR принимает файл (пустой текст на тишине — это успех)
    return res.get("http_ok", False)


def transcribe_audio(path_or_data_url: str, language: str = "ru") -> Dict[str, Any]:
    """Распознать речь из аудиофайла через Foundation Models (audio-to-text)."""
    from .. import llm

    conf = llm.provider_conf("cloudru")
    if not conf.get("api_key"):
        return {"ok": False, "error": "нет ключа Cloud.ru"}

    if path_or_data_url.startswith("data:"):
        header, _, b64 = path_or_data_url.partition(",")
        raw = base64.b64decode(b64)
        ext = "webm" if "webm" in header else ("mp3" if "mpeg" in header else "wav")
        src = _ws() / ("voice_%d.%s" % (int(time.time()), ext))
        src.write_bytes(raw)
    else:
        src = Path(path_or_data_url)
        if not src.is_absolute():
            src = _ws() / path_or_data_url
        if not src.exists():
            return {"ok": False, "error": "аудиофайл не найден"}

    # приводим к wav 16k при наличии ffmpeg — так принимают почти все ASR
    ff = _ffmpeg()
    upload = src
    if ff and src.suffix.lower() not in (".wav", ".mp3"):
        wav = src.with_suffix(".wav")
        try:
            subprocess.run([ff, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(wav)],
                           capture_output=True, timeout=120)
            if wav.exists() and wav.stat().st_size > 200:
                upload = wav
        except Exception:
            pass

    # Модель, которая реально принимает аудио (проверено запросом, не именем).
    model = _pick_audio_model()
    if not model:
        return {"ok": False, "browser_asr": True,
                "error": "В каталоге Cloud.ru нет доступной модели распознавания речи. "
                         "Переключаюсь на распознавание прямо в браузере."}
    res = _post_audio(conf, model, upload.name, upload.read_bytes(), language, timeout=180)
    if res.get("http_ok"):
        text = res.get("text") or ""
        return {"ok": bool(text), "text": text, "model": model}
    # модель перестала отвечать — забываем выбор, в следующий раз ищем заново
    global _AUDIO_MODEL
    _AUDIO_MODEL = None
    return {"ok": False, "browser_asr": True,
            "error": "Сервер распознавания не ответил (%s). Слушаю через браузер." % res.get("error", "")}


def _post_audio(conf: Dict[str, Any], model: str, filename: str, blob: bytes,
                language: str = "ru", timeout: int = 180) -> Dict[str, Any]:
    """Один-единственный способ отправить аудио в /audio/transcriptions.

    Им же проверяется, ASR ли модель, — поэтому проба и рабочий вызов не могут
    разойтись: то, что прошло проверку, гарантированно работает и в бою.
    """
    boundary = "----jarvis%d" % int(time.time() * 1000)
    parts = [
        ("--%s\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n%s\r\n" % (boundary, model)).encode(),
        ("--%s\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n%s\r\n" % (boundary, language)).encode(),
        ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
         "Content-Type: application/octet-stream\r\n\r\n" % (boundary, filename)).encode(),
        blob,
        ("\r\n--%s--\r\n" % boundary).encode(),
    ]
    url = conf["base_url"].rstrip("/") + "/audio/transcriptions"
    try:
        req = urllib.request.Request(url, data=b"".join(parts), method="POST", headers={
            "Authorization": "Bearer " + conf["api_key"],
            "Content-Type": "multipart/form-data; boundary=" + boundary,
        })
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        return {"http_ok": True, "text": payload.get("text") or payload.get("result") or ""}
    except Exception as exc:
        return {"http_ok": False, "error": str(exc)[:200]}


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

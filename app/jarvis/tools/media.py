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
    """Найти в каталоге провайдера реальную модель распознавания речи."""
    from .. import llm

    prefs = [p.lower() for p in (llm.CONFIG.get("model_tiers.audio", []) or [])]
    marks = prefs + ["whisper", "audio", "voxtral", "gigaam", "speech", "asr",
                     "stt", "transcri", "wav2vec", "seamless", "parakeet",
                     "canary", "vosk", "salute", "sense"]
    for name in llm.list_models("cloudru"):
        low = name.lower()
        if any(m and m in low for m in marks):
            return name
    return ""


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

    # Ищем настоящую audio-модель в каталоге. Раньше pick_model мог вернуть
    # обычную чат-модель — и /audio/transcriptions отвечал 404.
    model = _pick_audio_model()
    if not model:
        return {"ok": False, "browser_asr": True,
                "error": "В каталоге Cloud.ru нет доступной модели распознавания речи. "
                         "Переключаюсь на распознавание прямо в браузере."}
    boundary = "----jarvis%d" % int(time.time())
    parts = []
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n%s\r\n" % (boundary, model)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n%s\r\n" % (boundary, language)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n" % (boundary, upload.name)).encode())
    parts.append(upload.read_bytes())
    parts.append(("\r\n--%s--\r\n" % boundary).encode())
    body = b"".join(parts)
    url = conf["base_url"].rstrip("/") + "/audio/transcriptions"
    try:
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": "Bearer " + conf["api_key"],
            "Content-Type": "multipart/form-data; boundary=" + boundary,
        })
        with urllib.request.urlopen(req, timeout=180, context=_CTX) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        text = payload.get("text") or payload.get("result") or ""
        return {"ok": bool(text), "text": text, "model": model}
    except Exception as exc:
        return {"ok": False, "browser_asr": True,
                "error": "Сервер распознавания не ответил (%s). Слушаю через браузер." % exc}


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

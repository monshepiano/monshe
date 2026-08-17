"""
Сервер Джарвиса: HTTP API + WebSocket для живой трансляции мыслей.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from fastapi import (FastAPI, File, Form, HTTPException, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

from . import agent, llm, memory, scheduler, telegram_bot, tools
from .config import SANDBOX, UPLOADS, config

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"], allow_credentials=False,
)


# ---------------------------------------------------------------------------
# Шина событий -> WebSocket
# ---------------------------------------------------------------------------

class Bus:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def publish(self, payload: dict) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


bus = Bus()


@app.on_event("startup")
async def _startup() -> None:
    memory.init()
    scheduler.BUS_HOOK = bus.publish
    scheduler.start()
    telegram_bot.start()


# ---------------------------------------------------------------------------
# Статика и главная страница
# ---------------------------------------------------------------------------

if (WEB_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    f = WEB_DIR / "index.html"
    if not f.exists():
        return HTMLResponse("<h1>Интерфейс не найден</h1>", status_code=500)
    return HTMLResponse(f.read_text("utf-8"))


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "has_llm": config.has_llm, "time": time.time()}


# ---------------------------------------------------------------------------
# WebSocket — живой поток мыслей и действий
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    bus.clients.add(ws)
    try:
        await ws.send_json({"type": "hello", "has_llm": config.has_llm,
                            "persona": config.get("persona", default={})})
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await _handle_ws(ws, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        bus.clients.discard(ws)


async def _handle_ws(ws: WebSocket, msg: dict) -> None:
    kind = msg.get("type")

    if kind == "ping":
        await ws.send_json({"type": "pong"})

    elif kind == "chat":
        asyncio.create_task(_do_chat(msg))

    elif kind == "approval":
        ok = agent.resolve_approval(msg.get("approval_id", ""),
                                    msg.get("decision", "reject"))
        await ws.send_json({"type": "approval_ack", "ok": ok})

    elif kind == "cancel":
        tid = msg.get("task_id", "")
        if tid:
            memory.update_task(tid, status="cancelled")
            await bus.publish({"type": "task_cancelled", "task_id": tid})


async def _do_chat(msg: dict) -> None:
    text = (msg.get("text") or "").strip()
    session = msg.get("session") or "default"
    force_agent = bool(msg.get("agent"))
    attachment_ids = msg.get("attachments") or []

    if not text and not attachment_ids:
        return

    attachments = _load_attachments(attachment_ids)
    memory.add_message(session, "user", text,
                       {"attachments": [a["name"] for a in attachments]})

    use_agent = force_agent or await agent.needs_agent(text, bool(attachments))

    if use_agent:
        await bus.publish({"type": "mode", "mode": "agent"})
        try:
            hist = [{"role": m["role"], "content": m["content"]}
                    for m in memory.history(session, 8)
                    if m["role"] in ("user", "assistant")]
            res = await agent.run_agent(
                text, emit=bus.publish, session=session,
                attachments=attachments, history=hist,
            )
            if res.get("ok"):
                memory.add_message(session, "assistant", res.get("text", ""),
                                   {"task_id": res.get("task_id")})
        except Exception as e:
            await bus.publish({"type": "error", "text": str(e)})
        return

    await bus.publish({"type": "mode", "mode": "chat"})
    await bus.publish({"type": "answer_start"})
    try:
        async for chunk in agent.chat_stream(text, session=session,
                                             attachments=attachments):
            await bus.publish(chunk)
    except Exception as e:
        await bus.publish({"type": "error", "text": str(e)})
    await bus.publish({"type": "answer_end"})


def _load_attachments(ids: list[str]) -> list[dict]:
    out = []
    for name in ids:
        p = UPLOADS / name
        if not p.exists():
            continue
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
            out.append({"kind": "image", "name": p.name,
                        "data_url": tools.image_to_data_url(p)})
        else:
            out.append({"kind": "text", "name": p.name,
                        "text": tools.extract_text(p)})
    return out


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    safe = f"{uuid.uuid4().hex[:8]}_{Path(file.filename or 'file').name}"
    dest = UPLOADS / safe
    dest.write_bytes(await file.read())
    kind = "image" if dest.suffix.lower() in (
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp") else "file"
    return {"ok": True, "id": safe, "name": file.filename, "kind": kind,
            "size": dest.stat().st_size}


@app.post("/api/voice")
async def voice(file: UploadFile = File(...)) -> dict:
    """Голос -> текст (Whisper)."""
    data = await file.read()
    try:
        text = await llm.transcribe(data, file.filename or "audio.webm")
        return {"ok": True, "text": text}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/vision")
async def vision(file: UploadFile = File(...),
                 prompt: str = Form("Что на этом изображении?")) -> dict:
    """Кадр с камеры -> описание/ответ зрячей моделью."""
    UPLOADS.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS / f"cam_{uuid.uuid4().hex[:8]}.jpg"
    dest.write_bytes(await file.read())
    try:
        res = await llm.complete(
            [{"role": "system", "content": agent.build_system_prompt()},
             {"role": "user", "content": [
                 {"type": "text", "text": prompt},
                 {"type": "image_url",
                  "image_url": {"url": tools.image_to_data_url(dest)}},
             ]}],
            has_images=True, max_tokens=1500,
        )
        return {"ok": True, "text": res.text, "model": res.model,
                "upload_id": dest.name}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/history")
async def get_history(session: str = "default") -> dict:
    return {"ok": True, "messages": memory.history(session, 60)}


@app.get("/api/sessions")
async def get_sessions() -> dict:
    return {"ok": True, "sessions": memory.sessions()}


@app.post("/api/session/clear")
async def clear_session(payload: dict) -> dict:
    memory.clear_session(payload.get("session", "default"))
    return {"ok": True}


@app.get("/api/tasks")
async def get_tasks() -> dict:
    return {"ok": True, "tasks": memory.list_tasks()}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    task = memory.get_task(task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    return {"ok": True, "task": task, "steps": memory.task_steps(task_id)}


@app.post("/api/tasks/run")
async def run_task(payload: dict) -> dict:
    goal = (payload.get("goal") or "").strip()
    if not goal:
        raise HTTPException(400, "Пустая задача")
    tid = memory.create_task(goal[:80], goal, "background", True)
    asyncio.create_task(
        agent.run_agent(goal, emit=bus.publish, session="background",
                        task_id=tid, background=True)
    )
    return {"ok": True, "task_id": tid}


@app.get("/api/files")
async def api_files() -> dict:
    return await tools.list_files()


@app.get("/api/files/download/{path:path}")
async def download(path: str):
    p = (SANDBOX / urllib.parse.unquote(path)).resolve()
    if not str(p).startswith(str(SANDBOX.resolve())) or not p.exists():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(p, filename=p.name)


@app.get("/api/events")
async def api_events(unread: bool = False) -> dict:
    return {"ok": True, "events": memory.events(unread)}


@app.post("/api/events/read")
async def api_events_read() -> dict:
    memory.mark_events_read()
    return {"ok": True}


@app.get("/api/memory")
async def api_memory() -> dict:
    return {"ok": True, "facts": memory.recall(200)}


@app.post("/api/memory")
async def api_memory_add(payload: dict) -> dict:
    key, value = payload.get("key", ""), payload.get("value", "")
    if not key:
        raise HTTPException(400, "Нужен ключ")
    memory.remember(key, value, "manual")
    return {"ok": True}


@app.delete("/api/memory/{key}")
async def api_memory_del(key: str) -> dict:
    memory.forget(urllib.parse.unquote(key))
    return {"ok": True}


@app.get("/api/schedules")
async def api_schedules() -> dict:
    return {"ok": True, "schedules": memory.list_schedules()}


@app.post("/api/schedules")
async def api_schedule_add(payload: dict) -> dict:
    sid = memory.add_schedule(
        payload.get("title", "Задача"), payload.get("prompt", ""),
        int(payload.get("every_minutes") or 0),
    )
    return {"ok": True, "id": sid}


@app.delete("/api/schedules/{sid}")
async def api_schedule_del(sid: str) -> dict:
    memory.delete_schedule(sid)
    return {"ok": True}


@app.get("/api/config")
async def api_config() -> dict:
    data = json.loads(json.dumps(config.data))
    # Маскируем ключи в выдаче
    for name, node in (data.get("providers") or {}).items():
        if node.get("api_key"):
            node["api_key_set"] = True
            node["api_key"] = "••••" + node["api_key"][-4:]
        if node.get("key_secret"):
            node["key_secret_set"] = True
            node["key_secret"] = "••••" + node["key_secret"][-4:]
    if data.get("telegram", {}).get("bot_token"):
        data["telegram"]["bot_token"] = "••••" + data["telegram"]["bot_token"][-4:]
    return {"ok": True, "config": data}


@app.post("/api/config")
async def api_config_set(payload: dict) -> dict:
    """Принимает частичный конфиг и сливает его с текущим."""
    def merge(base: dict, patch: dict) -> dict:
        for k, v in patch.items():
            if isinstance(v, str):
                if v.startswith("••••"):
                    continue  # не затираем ключ маской
                # Пользователи часто копируют ключ с пробелом или переносом
                # строки на конце — молча убираем.
                v = v.strip()
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                merge(base[k], v)
            else:
                base[k] = v
        return base

    merge(config.data, payload or {})
    # Ключи чистим от кавычек, слов "Bearer"/"Api-Key" и невидимых пробелов,
    # иначе Cloud.ru отвечает "Invalid authorization header format".
    for _node in (config.data.get("providers") or {}).values():
        if not isinstance(_node, dict):
            continue
        for _f in ("api_key", "key_id", "key_secret"):
            if _node.get(_f):
                _node[_f] = llm.clean_key(_node[_f])
    config.save()
    # Ключ или проект могли смениться — заново подберём схему авторизации.
    llm._WORKING_AUTH.clear()
    telegram_bot.start()
    scheduler.start()
    return {"ok": True}


@app.get("/api/models")
async def api_models() -> dict:
    try:
        return {"ok": True, "models": await llm.list_models()}
    except Exception as e:
        return {"ok": False, "error": str(e),
                "hint": llm.explain_error(str(e)), "models": []}


@app.post("/api/test-key")
async def api_test_key(payload: dict) -> dict:
    """Проверка ключа прямо из интерфейса настроек."""
    base = payload.get("base_url") or config.get("providers", "cloudru", "base_url")
    key = payload.get("api_key") or config.get("providers", "cloudru", "api_key")
    project = payload.get("project_id")
    if project is None:
        project = config.get("providers", "cloudru", "project_id", default="")
    key_id = payload.get("key_id")
    if key_id is None:
        key_id = config.get("providers", "cloudru", "key_id", default="")
    key_secret = payload.get("key_secret")
    if key_secret is None:
        key_secret = config.get("providers", "cloudru", "key_secret", default="")
    from .llm import clean_key as _clean
    base = (base or "").strip().rstrip("/")
    raw_key = (key or "").strip()
    key = _clean(raw_key)
    project = (project or "").strip()
    key_id = _clean(key_id or "")
    key_secret = _clean(key_secret or "")
    if not key and not (key_id and key_secret):
        return {"ok": False, "error": "Ключ пустой", "hint": "Вставьте Key Secret."}

    import re as _re

    uuid_re = _re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", _re.I
    )
    if not project:
        return {
            "ok": False,
            "error": "Не указан ID проекта",
            "hint": (
                "Cloud.ru не примет запрос без ID проекта.\n"
                "Где взять: cloud.ru → вверху раскрыть список проектов → "
                "в строке нужного проекта нажать «⋮» → «Скопировать ID проекта»."
            ),
        }
    if not uuid_re.match(project):
        return {
            "ok": False,
            "error": f"ID проекта выглядит неправильно: {project[:40]}",
            "hint": (
                "ID проекта — это длинный код из букв и цифр с дефисами, например\n"
                "50000000-4000-3000-2000-100000000001\n\n"
                "Похоже, вы вставили название проекта или что-то другое. "
                "Откройте cloud.ru → вверху раскрыть список проектов → «⋮» у нужного "
                "проекта → «Скопировать ID проекта»."
            ),
        }

    import httpx
    from .llm import (Provider, best_error, clean_key, explain_error,
                      is_auth_error)

    prov = Provider("cloudru", base, key, project,
                    key_id=key_id, key_secret=key_secret)
    labels = {
        "both": "ключ + ID проекта в заголовках",
        "bearer": "только ключ (Bearer)",
        "apikey_hdr": "Authorization: Api-Key",
        "apikey": "только x-api-key",
        "iam_token": "обмен Key ID + Key Secret на токен",
    }
    errs: list[str] = []
    tried: list[str] = []
    report: list[str] = []

    def _fp(label: str, val: str) -> str:
        """Отпечаток секрета: длина и края, без раскрытия середины."""
        if not val:
            return f"{label}: (пусто)"
        shown = val if len(val) <= 12 else f"{val[:4]}…{val[-4:]}"
        kinds = []
        if any(c.isupper() for c in val):
            kinds.append("A-Z")
        if any(c.islower() for c in val):
            kinds.append("a-z")
        if any(c.isdigit() for c in val):
            kinds.append("0-9")
        odd = sorted({c for c in val if not c.isalnum() and c not in "-_."})
        extra = f", подозрительные символы: {odd}" if odd else ""
        return (f"{label}: {shown} (длина {len(val)}, "
                f"{'+'.join(kinds) or 'нет букв/цифр'}{extra})")

    report.append(_fp("Ключ", key))
    if raw_key != key:
        report.append(f"  ⚠ из ключа убран мусор, было {len(raw_key)} символов")
    report.append(_fp("Key ID", key_id))
    report.append(_fp("Key Secret", key_secret))
    report.append(f"ID проекта: {project}")
    report.append(f"Адрес: {base}")
    report.append("")

    try:
        async with httpx.AsyncClient(timeout=25) as c:
            for mode in prov.auth_modes():
                if not await prov.prepare(mode):
                    errs.append("Не удалось обменять Key ID + Key Secret на "
                                "токен: iam.api.cloud.ru отклонил эту пару. "
                                "Проверьте Key ID и Key Secret.")
                    report.append(
                        f"[{labels.get(mode, mode)}] — обмен на токен не удался")
                    continue
                r = await c.get(base.rstrip("/") + "/models",
                                headers=prov.headers(json_body=False, mode=mode))
                tried.append(labels.get(mode, mode))
                if r.status_code < 400:
                    prov.remember_auth(mode)
                    models = [m.get("id") for m in r.json().get("data", [])]
                    report.append(f"[{labels.get(mode, mode)}] → OK")
                    return {
                        "ok": True,
                        "count": len(models),
                        "models": models[:60],
                        "auth": labels.get(mode, mode),
                        "report": "\n".join(report),
                    }
                err = f"{r.status_code}: {r.text[:200]}"
                errs.append(err)
                sent = prov.headers(json_body=False, mode=mode)
                report.append(
                    f"[{labels.get(mode, mode)}]\n"
                    f"  отправлено: {', '.join(sorted(sent))}\n"
                    f"  ответ {r.status_code}: {r.text[:300]}")
                if not is_auth_error(err):
                    break
    except Exception as e:
        report.append(f"Соединение оборвалось: {type(e).__name__}: {e}")
        return {"ok": False, "error": str(e), "hint": explain_error(str(e)),
                "report": "\n".join(report)}

    shown = best_error(errs) or (errs[0] if errs else "неизвестная ошибка")
    hint = explain_error(shown)
    if raw_key and raw_key != key:
        hint = ("Из ключа пришлось убрать лишнее (пробелы, кавычки или "
                "слово Bearer/Api-Key) — проверьте, что скопировали "
                "только сам ключ.\n\n" + hint).strip()
    if len(tried) > 1:
        hint = (hint + "\n\nДжарвис попробовал все способы передать ключ ("
                + ", ".join(tried) + ") — сервис не принял ни один, "
                "значит дело в самом ключе, а не в способе подключения.").strip()
    return {"ok": False, "error": shown, "hint": hint,
            "report": "\n".join(report)}


def main() -> None:
    import uvicorn
    port = int(config.get("server", "port", default=8765))
    host = config.get("server", "host", default="0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()

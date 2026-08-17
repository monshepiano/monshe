"""FastAPI-сервер Джарвиса: API + SSE + раздача интерфейса."""
from __future__ import annotations

import asyncio
import json
import mimetypes
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, config, events, router, scheduler, store
from .tools import browser, sandbox, telegram

app = FastAPI(title="Jarvis", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])

UI_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


# ------------------------------------------------------------------ авторизация
@app.middleware("http")
async def auth_gate(request: Request, call_next):
    token = (config.get("auth_token") or "").strip()
    path = request.url.path
    open_paths = ("/api/health", "/api/setup-state")
    if token and path.startswith("/api") and path not in open_paths:
        got = (request.headers.get("x-jarvis-token")
               or request.query_params.get("token") or "")
        if got != token:
            return JSONResponse({"error": "Нужен токен доступа"}, status_code=401)
    return await call_next(request)


@app.on_event("startup")
async def _startup() -> None:
    config.ensure_dirs()
    store.init()
    scheduler.start()
    events.publish("system", message="Джарвис в сети")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await scheduler.stop()
    await browser.close()


# ------------------------------------------------------------------ модели запросов
class ChatIn(BaseModel):
    chat_id: Optional[str] = None
    text: str
    attachments: Optional[List[str]] = None


class TaskIn(BaseModel):
    title: Optional[str] = None
    goal: str
    schedule: Optional[str] = "once"
    delay_minutes: Optional[int] = 0
    chat_id: Optional[str] = ""


class ApprovalIn(BaseModel):
    approval_id: str
    approved: bool


class ConfigIn(BaseModel):
    values: Dict[str, Any]


class MemoryIn(BaseModel):
    kind: str = "fact"
    key: str
    value: str


# ------------------------------------------------------------------ базовое
@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "time": time.time(), "version": "1.0"}


@app.get("/api/setup-state")
async def setup_state() -> Dict[str, Any]:
    cfg = config.load()
    return {"configured": bool(cfg.get("gigachat_credentials") or cfg.get("openrouter_api_key")
                               or cfg.get("custom_base_url")),
            "auth_required": bool(cfg.get("auth_token"))}


@app.get("/api/status")
async def status() -> Dict[str, Any]:
    st = await router.status()
    st["telegram"] = telegram.enabled()
    st["browser"] = browser.available()
    st["pending_approvals"] = store.pending_approvals()
    return st


@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    return config.public_config()


@app.post("/api/config")
async def set_config(body: ConfigIn) -> Dict[str, Any]:
    clean = {k: v for k, v in body.values.items() if not (isinstance(v, str) and v.startswith("•"))}
    config.save(clean)
    events.publish("config_updated")
    return config.public_config()


@app.post("/api/telegram/test")
async def telegram_test() -> Dict[str, Any]:
    me = await telegram.whoami()
    sent = await telegram.send("🤖 Джарвис на связи. Уведомления подключены.")
    return {"bot": me.get("result", {}).get("username"), "sent": bool(sent.get("ok")),
            "error": sent.get("description") or sent.get("error")}


# ------------------------------------------------------------------ чат
@app.get("/api/chats")
async def chats() -> Dict[str, Any]:
    return {"chats": store.list_chats()}


@app.post("/api/chats")
async def new_chat() -> Dict[str, Any]:
    return {"chat_id": store.create_chat()}


@app.delete("/api/chats/{chat_id}")
async def del_chat(chat_id: str) -> Dict[str, Any]:
    store.delete_chat(chat_id)
    return {"ok": True}


@app.get("/api/chats/{chat_id}/messages")
async def messages(chat_id: str) -> Dict[str, Any]:
    return {"messages": store.get_messages(chat_id)}


@app.post("/api/chat")
async def chat(body: ChatIn) -> Dict[str, Any]:
    chat_id = body.chat_id or store.create_chat()
    store.add_message(chat_id, "user", body.text,
                      {"attachments": body.attachments or []})
    try:
        answer = await agent.chat(chat_id, body.text, body.attachments)
    except Exception as e:
        answer = f"⚠️ {type(e).__name__}: {e}"
        store.add_message(chat_id, "assistant", answer)
        events.publish("chat_end", chat_id=chat_id, text=answer)
    return {"chat_id": chat_id, "answer": answer}


# ------------------------------------------------------------------ задачи
@app.get("/api/tasks")
async def tasks() -> Dict[str, Any]:
    return {"tasks": store.list_tasks()}


@app.post("/api/tasks")
async def create_task(body: TaskIn) -> Dict[str, Any]:
    tid = scheduler.schedule_new(body.title or body.goal[:48], body.goal,
                                 body.schedule or "once", body.delay_minutes or 0,
                                 body.chat_id or "", source="user")
    return {"task_id": tid}


@app.post("/api/tasks/{task_id}/run")
async def rerun_task(task_id: str) -> Dict[str, Any]:
    scheduler.enqueue(task_id)
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def cancel_task(task_id: str) -> Dict[str, Any]:
    store.update_task(task_id, status="cancelled", next_run=None)
    return {"ok": True}


# ------------------------------------------------------------------ подтверждения
@app.get("/api/approvals")
async def approvals() -> Dict[str, Any]:
    return {"approvals": store.pending_approvals()}


@app.post("/api/approvals")
async def decide(body: ApprovalIn) -> Dict[str, Any]:
    ok = agent.resolve_approval(body.approval_id, body.approved)
    return {"ok": ok}


# ------------------------------------------------------------------ уведомления
@app.get("/api/notifications")
async def notifications() -> Dict[str, Any]:
    return {"notifications": store.list_notifications()}


@app.post("/api/notifications/read")
async def read_notifications(body: Dict[str, Any]) -> Dict[str, Any]:
    store.mark_notifications_read(body.get("ids"))
    return {"ok": True}


# ------------------------------------------------------------------ память
@app.get("/api/memory")
async def memory() -> Dict[str, Any]:
    return {"memory": store.recall()}


@app.post("/api/memory")
async def add_memory(body: MemoryIn) -> Dict[str, Any]:
    store.remember(body.kind, body.key, body.value)
    return {"ok": True}


@app.delete("/api/memory/{mem_id}")
async def del_memory(mem_id: str) -> Dict[str, Any]:
    store.forget(mem_id)
    return {"ok": True}


# ------------------------------------------------------------------ файлы / песочница
@app.get("/api/files")
async def files() -> Dict[str, Any]:
    return sandbox.list_files(".")


@app.get("/api/files/download")
async def download(path: str):
    p = (sandbox.root() / path.lstrip("/")).resolve()
    if not str(p).startswith(str(sandbox.root().resolve())) or not p.exists():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(p, filename=p.name)


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), category: str = Form("file")) -> Dict[str, Any]:
    config.ensure_dirs()
    safe_name = Path(file.filename or "upload.bin").name
    dest = sandbox.root() / safe_name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    events.publish("upload", path=safe_name, category=category)
    return {"ok": True, "path": safe_name,
            "mime": mimetypes.guess_type(safe_name)[0] or "application/octet-stream"}


@app.post("/api/vision")
async def vision(file: UploadFile = File(...),
                 question: str = Form("Что на изображении? Опиши подробно.")) -> Dict[str, Any]:
    """Кадр с камеры → описание."""
    dest = sandbox.root() / "camera_frame.jpg"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        text = await router.gigachat().vision(question, str(dest))
        return {"ok": True, "description": text, "path": "camera_frame.jpg"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------------ события (SSE)
@app.get("/api/stream")
async def stream(request: Request):
    q = events.subscribe()

    async def gen():
        try:
            for ev in events.recent(30):
                yield events.sse(ev)
            yield events.sse({"kind": "connected", "ts": time.time()})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=20)
                    yield events.sse(ev)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


# ------------------------------------------------------------------ интерфейс
if UI_DIR.exists():
    app.mount("/assets", StaticFiles(directory=UI_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        candidate = UI_DIR / full_path
        if full_path and candidate.exists() and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(UI_DIR / "index.html")
else:
    @app.get("/")
    async def no_ui():
        return JSONResponse({"error": "Интерфейс не собран. Запустите install.command."},
                            status_code=503)

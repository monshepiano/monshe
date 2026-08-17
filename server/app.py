"""HTTP-сервер JARVIS (FastAPI): API + статика интерфейса."""
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import brain, config, tools

BASE = Path(__file__).resolve().parent.parent
CLIENT = BASE / "client"

app = FastAPI(title="JARVIS")


class ChatBody(BaseModel):
    message: str
    image: Optional[str] = None
    mode: Optional[str] = None
    history: Optional[List[Dict[str, Any]]] = None


class KeyBody(BaseModel):
    provider: str
    key: str


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/config")
def get_config():
    tiers = {}
    for t in ("fast", "smart", "vision"):
        p, m = config.tier_cfg(t)
        tiers[t] = {
            "provider": p or None,
            "model": m or None,
            "ready": bool(p) and (p == "ollama" or config.has_key(p)),
        }
    return {
        "providers": config.configured_providers(),
        "tiers": tiers,
        "pc_enabled": config.enable_pc(),
        "mock": config.mock_enabled(),
    }


@app.post("/api/config/key")
def set_provider_key(body: KeyBody):
    env = config.API_KEYS.get(body.provider)
    if env is None:
        return {"ok": False, "error": "Неизвестный провайдер"}
    config.set_key(env, body.key.strip())
    return {"ok": True, "provider": body.provider,
            "ready": config.has_key(body.provider)}


@app.post("/api/chat")
def chat(body: ChatBody):
    t0 = time.time()
    res = brain.run_turn(body.message, body.image, body.mode, body.history)
    res["latency_ms"] = int((time.time() - t0) * 1000)
    return res


@app.get("/api/actions")
def list_actions():
    return {"actions": tools.list_actions()}


@app.post("/api/actions/{aid}/approve")
def approve(aid: str):
    return tools.approve(aid)


@app.post("/api/actions/{aid}/deny")
def deny(aid: str):
    return tools.deny(aid)


app.mount("/", StaticFiles(directory=str(CLIENT), html=True), name="client")

"""JARVIS configuration. Keys live in data/settings.json after first launch."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("JARVIS_DATA", ROOT / "data"))
STATIC = ROOT / "static"
SANDBOX = DATA / "sandbox"
UPLOADS = DATA / "uploads"
SCREEN = DATA / "screen"
MEMORY_PATH = DATA / "memory.json"
SETTINGS_PATH = DATA / "settings.json"
EVENTS_PATH = DATA / "events.jsonl"
HISTORY_PATH = DATA / "history.json"
PERSONA_PATH = DATA / "persona.md"

for _p in (DATA, SANDBOX, UPLOADS, SCREEN):
    _p.mkdir(parents=True, exist_ok=True)

# Built-in keys — used only if settings.json has empty values.
# User can change them in the UI later.
DEFAULT_FM_KEY = os.environ.get(
    "JARVIS_FM_KEY",
    "NzE0ZGY3MDAtMjM1MS00MWNiLTlkZmUtYWI5ZDJlZmU3OWUz.6fce77de064c16cf6789b4be595a18e3",
)
DEFAULT_DS_KEY = os.environ.get(
    "JARVIS_DS_KEY",
    "sk-fc57ab1c23a646cab7317e3523ba2d60",
)

FM_BASE = "https://foundation-models.api.cloud.ru/v1"
DS_BASE = "https://api.deepseek.com"

# Cheap → expensive. Orchestrator picks the first healthy match.
MODELS = {
    "cheap": {
        "id": "ai-sage/GigaChat3-10B-A1.8B",
        "provider": "fm",
        "label": "GigaChat 3 Lite",
        "rub_in": 12.2,
        "rub_out": 12.2,
        "vision": False,
        "tools": True,
    },
    "cheap_alt": {
        "id": "openai/gpt-oss-120b",
        "provider": "fm",
        "label": "GPT-OSS 120B",
        "rub_in": 15.86,
        "rub_out": 61.0,
        "vision": False,
        "tools": True,
    },
    "vision": {
        "id": "Qwen/Qwen3.6-35B-A3B",
        "provider": "fm",
        "label": "Qwen 3.6 Vision",
        "rub_in": 219.6,
        "rub_out": 329.4,
        "vision": True,
        "tools": True,
    },
    "strong": {
        "id": "ai-sage/GigaChat3.5-432B-A28B",
        "provider": "fm",
        "label": "GigaChat 3.5",
        "rub_in": 80.0,
        "rub_out": 80.0,
        "vision": False,
        "tools": True,
    },
    "reason": {
        "id": "deepseek-ai/DeepSeek-V4-Pro",
        "provider": "fm",
        "label": "DeepSeek V4 Pro",
        "rub_in": 183.0,
        "rub_out": 732.0,
        "vision": False,
        "tools": True,
    },
    "backup_cheap": {
        "id": "deepseek-chat",
        "provider": "ds",
        "label": "DeepSeek Chat",
        "rub_in": 2.0,
        "rub_out": 8.0,
        "vision": False,
        "tools": True,
    },
    "backup_reason": {
        "id": "deepseek-reasoner",
        "provider": "ds",
        "label": "DeepSeek Reasoner",
        "rub_in": 4.0,
        "rub_out": 16.0,
        "vision": False,
        "tools": True,
    },
}

DEFAULT_SETTINGS = {
    "owner_name": "",
    "assistant_name": "Джарвис",
    "language": "ru",
    "fm_key": DEFAULT_FM_KEY,
    "ds_key": DEFAULT_DS_KEY,
    "telegram_token": "",
    "telegram_chat_id": "",
    "voice_out": True,
    "proactive": True,
    "confirm_dangerous": True,
    "access_pin": "",
    "host": "0.0.0.0",
    "port": 8787,
    "theme": "arc",
}


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return data


def save_settings(patch: dict) -> dict:
    data = load_settings()
    data.update(patch or {})
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def new_id(prefix: str = "id") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"

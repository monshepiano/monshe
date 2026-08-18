"""Конфигурация JARVIS.

Хранится в ~/JARVIS/config.json. Ключи можно менять через UI (Настройки).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

_LOCK = threading.RLock()

HOME = Path(os.environ.get("JARVIS_HOME", str(Path.home() / "JARVIS")))
CONFIG_PATH = HOME / "config.json"
WORKSPACE = HOME / "workspace"
DATA_DIR = HOME / "data"
LOG_DIR = HOME / "logs"
SKILLS_DIR = HOME / "skills"

DEFAULTS: Dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 8765, "open_browser": True},
    "user": {"name": "", "city": "", "lang": "ru", "about": ""},
    "providers": {
        "cloudru": {
            "enabled": True,
            "base_url": "https://foundation-models.api.cloud.ru/v1",
            "api_key": "",
            "label": "Cloud.ru Foundation Models",
        },
        "deepseek": {
            "enabled": True,
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "label": "DeepSeek (резерв)",
        },
    },
    # Предпочтения по моделям для каждого «уровня». Поиск идёт по подстроке
    # среди реально доступных моделей провайдера (GET /v1/models).
    "model_tiers": {
        "nano": [
            "ai-sage/GigaChat3-10B-A1.8B",
            "GigaChat3-10B",
            "Qwen3-30B-A3B",
            "gpt-oss-20b",
            "Qwen2.5-7B",
        ],
        "base": [
            "openai/gpt-oss-120b",
            "gpt-oss-120b",
            "Qwen3.6-35B-A3B",
            "Qwen3-30B-A3B",
            "GigaChat3-10B",
        ],
        "smart": [
            "zai-org/GLM-4.7",
            "GLM-4.7",
            "MiniMax-M2.5",
            "Qwen3-235B",
            "DeepSeek-V3",
            "openai/gpt-oss-120b",
        ],
        "coder": [
            "Qwen3-Coder-Next",
            "Qwen3-Coder",
            "Devstral",
            "openai/gpt-oss-120b",
        ],
        "vision": [
            "Qwen3-VL-8B-Instruct",
            "Qwen3 VL 8B Instruct",
            "Qwen3-VL",
            "Qwen2.5-VL",
            "VL",
        ],
        "audio": ["whisper", "audio", "Voxtral"],
        "embed": ["Qwen3-Embedding-0.6B", "Embedding", "bge-m3"],
    },
    # Что УМЕЕТ модель каждого уровня. Раньше этого знания в системе не было
    # вообще: оркестратор считал «сложность» текста и мог отдать запрос с
    # инструментами модели, которая вызывать их не умеет. Она честно отвечала
    # «у меня нет доступа к интернету» — и это выглядело как поломка интернета,
    # хотя поломкой была маршрутизация.
    "model_caps": {
        "nano": {"tools": False, "vision": False},
        "base": {"tools": True, "vision": False},
        "smart": {"tools": True, "vision": False},
        "coder": {"tools": True, "vision": False},
        "vision": {"tools": False, "vision": True},
    },
    "orchestrator": {
        "auto_route": True,
        "force_tier": "",          # "" = авто; иначе nano/base/smart/coder
        "max_output_tokens": 2400,
        "temperature": 0.6,
        "budget_rub_per_day": 50,  # мягкий лимит, предупреждение в UI
    },
    "safety": {
        "confirm_payments": True,
        "confirm_delete": True,
        "confirm_shell": True,
        "confirm_computer_use": True,
        "confirm_send_message": True,
        "auto_approve_readonly": True,
    },
    "auto": {
        "enabled": True,
        "tick_seconds": 5,
        "proactive": True,
        "quiet_hours": [1, 8],
    },
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    # Баланс/расходы личного кабинета Cloud.ru (необязательно).
    # key_id/key_secret — ключ доступа сервисного аккаунта или персональный ключ,
    # agreement_id — из адресной строки личного кабинета.
    "billing": {
        "enabled": False,
        "key_id": "",
        "key_secret": "",
        "agreement_id": "",
        "customer_id": "",
        "refresh_minutes": 30,
    },
    "media": {
        "image_provider": "pollinations",   # pollinations | fm | off
        "image_base": "https://image.pollinations.ai/prompt/",
    },
    "computer_use": {"enabled": True, "screenshot_scale": 0.4},
    "agent": {"max_steps_chat": 6, "max_steps_agent": 18},
    "ui": {"theme": "arc", "sound": True, "wake_word": "джарвис", "voice_reply": True},
}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _migrate(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Подтянуть уже сохранённые конфиги под новые умолчания.

    Пользователь ничего не настраивал руками, а старое значение из файла
    перекрывает исправление: например, редкий тик AUTO заставлял напоминания
    срабатывать сильно позже назначенного времени.
    """
    auto = raw.get("auto")
    if isinstance(auto, dict):
        try:
            if int(auto.get("tick_seconds", 0)) > 10:
                auto["tick_seconds"] = DEFAULTS["auto"]["tick_seconds"]
        except Exception:
            auto["tick_seconds"] = DEFAULTS["auto"]["tick_seconds"]
    # computer_use.max_steps никогда не читался кодом: реальный лимит жил
    # константой в agent.py. Ключ переехал в agent.max_steps_* — убираем
    # обманку из сохранённых конфигов, чтобы настройка не врала.
    cu = raw.get("computer_use")
    if isinstance(cu, dict):
        cu.pop("max_steps", None)
    return raw


class Config:
    def __init__(self) -> None:
        self._data: Dict[str, Any] = dict(DEFAULTS)
        self.load()

    # ------------------------------------------------------------------ IO
    def load(self) -> None:
        with _LOCK:
            for directory in (HOME, WORKSPACE, DATA_DIR, LOG_DIR, SKILLS_DIR):
                directory.mkdir(parents=True, exist_ok=True)
            if CONFIG_PATH.exists():
                try:
                    raw = json.loads(CONFIG_PATH.read_text("utf-8"))
                    raw = _migrate(raw)
                    self._data = _merge(DEFAULTS, raw)
                except Exception:
                    self._data = dict(DEFAULTS)
            else:
                self._data = dict(DEFAULTS)
            # переменные окружения имеют приоритет (удобно для сервера)
            env_cloud = os.environ.get("CLOUDRU_API_KEY")
            env_ds = os.environ.get("DEEPSEEK_API_KEY")
            if env_cloud:
                self._data["providers"]["cloudru"]["api_key"] = env_cloud
            if env_ds:
                self._data["providers"]["deepseek"]["api_key"] = env_ds
            if os.environ.get("JARVIS_PORT"):
                try:
                    self._data["server"]["port"] = int(os.environ["JARVIS_PORT"])
                except ValueError:
                    pass
            if os.environ.get("JARVIS_HOST"):
                self._data["server"]["host"] = os.environ["JARVIS_HOST"]
            self.save()

    def save(self) -> None:
        with _LOCK:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(CONFIG_PATH)

    # --------------------------------------------------------------- access
    @property
    def data(self) -> Dict[str, Any]:
        return self._data

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value: Any) -> None:
        with _LOCK:
            node = self._data
            parts = path.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
            self.save()

    def update(self, patch: Dict[str, Any]) -> None:
        with _LOCK:
            self._data = _merge(self._data, patch)
            self.save()

    def public(self) -> Dict[str, Any]:
        """Конфиг для UI — ключи маскируются."""
        data = json.loads(json.dumps(self._data))
        for name, provider in data.get("providers", {}).items():
            key = provider.get("api_key") or ""
            provider["api_key"] = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("" if not key else "…")
            provider["has_key"] = bool(self._data["providers"][name].get("api_key"))
        token = data.get("telegram", {}).get("bot_token") or ""
        if token:
            data["telegram"]["bot_token"] = token[:8] + "…"
        billing = data.get("billing") or {}
        if billing:
            secret = billing.get("key_secret") or ""
            billing["key_secret"] = ("…" if secret else "")
            billing["has_secret"] = bool(self._data.get("billing", {}).get("key_secret"))
        return data


CONFIG = Config()

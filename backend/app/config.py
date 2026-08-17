"""Конфигурация Джарвиса.

Все настройки живут в ~/.jarvis/config.json — файл создаётся автоматически.
Ключи можно вписать через веб-интерфейс (панель «Настройки»), руками лезть не нужно.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Dict

HOME = Path(os.environ.get("JARVIS_HOME", Path.home() / ".jarvis"))
CONFIG_PATH = HOME / "config.json"
WORKSPACE = HOME / "workspace"      # песочница: сюда агент пишет файлы
UPLOADS = HOME / "uploads"          # прикреплённые пользователем файлы
DB_PATH = HOME / "jarvis.db"

DEFAULTS: Dict[str, Any] = {
    # --- Провайдеры моделей -------------------------------------------------
    # GigaChat (Сбер) — работает в РФ без VPN, есть бесплатный лимит.
    "gigachat_credentials": "",           # Authorization key (base64 client_id:secret)
    "gigachat_scope": "GIGACHAT_API_PERS",
    "gigachat_verify_ssl": False,         # у Сбера цепочка Минцифры, по умолчанию не проверяем
    # OpenRouter — много бесплатных моделей (нужен VPN не всегда, но часто; опционально)
    "openrouter_api_key": "",
    "openrouter_base_url": "https://openrouter.ai/api/v1",
    # Любой OpenAI-совместимый эндпоинт (например, локальная Ollama или прокси)
    "custom_base_url": "",
    "custom_api_key": "",
    "custom_model": "",
    # Ollama на этом же компьютере (полностью бесплатно, офлайн)
    "ollama_base_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen2.5:7b",

    # --- Маршрутизация ------------------------------------------------------
    "route_light": "auto",   # модель для простых задач
    "route_medium": "auto",
    "route_heavy": "auto",
    "auto_route": True,

    # --- Телеграм -----------------------------------------------------------
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_enabled": True,

    # --- Поведение агента ---------------------------------------------------
    "confirm_dangerous": True,      # спрашивать подтверждение перед опасными действиями
    "allow_shell": True,            # разрешить команды в песочнице (всё равно с подтверждением)
    "max_agent_steps": 24,
    "proactive": True,              # фоновые проактивные проверки
    "language": "ru",
    "user_name": "",
    "persona": "Ты — Джарвис: спокойный, точный, с лёгкой иронией. Отвечаешь по-русски, кратко и по делу.",

    # --- Сервер -------------------------------------------------------------
    "host": "0.0.0.0",
    "port": 8765,
    "auth_token": "",     # если не пусто — нужен токен для доступа (для сервера в интернете)
}

_lock = threading.Lock()
_cache: Dict[str, Any] | None = None


def ensure_dirs() -> None:
    for p in (HOME, WORKSPACE, UPLOADS):
        p.mkdir(parents=True, exist_ok=True)


def load() -> Dict[str, Any]:
    global _cache
    with _lock:
        if _cache is not None:
            return dict(_cache)
        ensure_dirs()
        data = dict(DEFAULTS)
        if CONFIG_PATH.exists():
            try:
                data.update(json.loads(CONFIG_PATH.read_text("utf-8")))
            except Exception:
                pass
        # переменные окружения имеют приоритет (удобно на сервере)
        for key in list(DEFAULTS):
            env = os.environ.get("JARVIS_" + key.upper())
            if env:
                if isinstance(DEFAULTS[key], bool):
                    data[key] = env.lower() in ("1", "true", "yes", "on")
                elif isinstance(DEFAULTS[key], int):
                    try:
                        data[key] = int(env)
                    except ValueError:
                        pass
                else:
                    data[key] = env
        _cache = data
        return dict(data)


def save(patch: Dict[str, Any]) -> Dict[str, Any]:
    global _cache
    data = load()
    for k, v in patch.items():
        if k in DEFAULTS:
            data[k] = v
    with _lock:
        ensure_dirs()
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        _cache = data
    return dict(data)


def get(key: str, default: Any = None) -> Any:
    return load().get(key, DEFAULTS.get(key, default))


def new_token() -> str:
    return secrets.token_urlsafe(24)


def public_config() -> Dict[str, Any]:
    """Конфиг для интерфейса: секреты маскируем."""
    data = load()
    out = dict(data)
    for secret_key in ("gigachat_credentials", "openrouter_api_key", "custom_api_key",
                       "telegram_bot_token", "auth_token"):
        val = data.get(secret_key) or ""
        out[secret_key] = ("•" * 8 + val[-4:]) if val else ""
        out[secret_key + "_set"] = bool(val)
    return out

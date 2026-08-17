"""
Конфигурация Джарвиса.

Все настройки живут в ~/.jarvis/config.json — обычном текстовом файле,
который пользователь может открыть и отредактировать (или через UI «Настройки»).
Файл создаётся автоматически при первом запуске.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("JARVIS_HOME", Path.home() / ".jarvis"))
CONFIG_PATH = HOME / "config.json"
SANDBOX = HOME / "sandbox"
UPLOADS = HOME / "uploads"
LOGS = HOME / "logs"
DB_PATH = HOME / "jarvis.db"
SKILLS = HOME / "skills"

DEFAULTS: dict[str, Any] = {
    # ---------- Провайдеры моделей ----------
    # Cloud.ru Evolution Foundation Models — работает из РФ без VPN,
    # OpenAI-совместимый API, много бесплатных/дешёвых моделей.
    "providers": {
        "cloudru": {
            "enabled": True,
            "base_url": "https://foundation-models.api.cloud.ru/v1",
            "api_key": "",
            # Идентификатор проекта Cloud.ru. Без него сервис отвечает
            # "403: Project not found". Личный кабинет -> Проекты ->
            # нужный проект -> скопировать ID (вид: 50000000-4000-...).
            "project_id": "",
            # Запасной способ входа: пара «ключ доступа» сервисного аккаунта.
            # Key ID — логин, Key Secret — пароль. Джарвис сам меняет их на
            # временный токен через iam.api.cloud.ru, если статический
            # API-ключ по какой-то причине не принимается.
            "key_id": "",
            "key_secret": "",
            "note": "Ключ: cloud.ru -> Пользователи -> Сервисные аккаунты -> API-ключ",
        },
        # Резервный/дополнительный OpenAI-совместимый провайдер
        # (VseGPT, AITunnel, ProxyAPI, OpenRouter — что угодно).
        "fallback": {
            "enabled": False,
            "base_url": "https://api.vsegpt.ru/v1",
            "api_key": "",
        },
    },
    # ---------- Маршрутизация моделей (оркестратор) ----------
    # Джарвис сам выбирает модель под сложность задачи.
    "models": {
        "chat": "openai/gpt-oss-120b",                      # быстрые ответы, болтовня
        "smart": "zai-org/GLM-4.6",                         # сложные рассуждения, планирование
        "agent": "openai/gpt-oss-120b",                     # агентский цикл с инструментами
        "code": "Qwen/Qwen3-Coder-480B-A35B-Instruct",      # код
        "vision": "Qwen/Qwen2.5-VL-72B-Instruct",           # картинки/камера
        "stt": "openai/whisper-large-v3",                   # распознавание речи
        "cheap": "Qwen/Qwen3-Next-80B-A3B-Instruct",        # классификация, мелочь
    },
    "router": {
        "enabled": True,
        "explain": True,  # показывать в интерфейсе, какая модель выбрана и почему
    },
    # ---------- Личность / персонализация ----------
    "persona": {
        "name": "Джарвис",
        "user_name": "Сэр",
        "style": (
            "Ты — Джарвис, персональный ИИ-ассистент в духе Джарвиса из «Железного человека». "
            "Говоришь по-русски, вежливо, с лёгкой иронией и абсолютной собранностью. "
            "Обращаешься к пользователю уважительно. Отвечаешь по делу, без воды."
        ),
        "voice_enabled": True,
    },
    # ---------- Безопасность ----------
    "safety": {
        # Инструменты, требующие подтверждения. Джарвис остановится и спросит.
        "confirm_tools": [
            "run_shell",
            "browser_act",
            "send_telegram",
            "delete_file",
            "http_request",
            "self_edit",
        ],
        "auto_approve_in_background": False,
        "max_steps": 24,          # максимум шагов агента на одну задачу
        "max_seconds": 900,       # тайм-аут задачи
    },
    # ---------- Интеграции ----------
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "notify_on_task_done": True,
        "allow_commands": True,   # управлять Джарвисом из Telegram
    },
    "search": {
        # Поисковики, доступные из РФ без VPN. Пробуются по порядку.
        "order": ["ddg", "yandex", "tavily"],
        "tavily_api_key": "",
        "yandex_api_key": "",     # Яндекс Поиск API (folder_id:key)
        "yandex_folder_id": "",
    },
    "image_gen": {
        # FusionBrain (Kandinsky) — бесплатно и доступно из РФ.
        "provider": "fusionbrain",
        "api_key": "",
        "secret_key": "",
    },
    # ---------- Фоновая работа ----------
    "background": {
        "enabled": True,
        "proactive": True,          # Джарвис сам предлагает и делает полезное
        "morning_briefing": "",     # напр. "09:00" — утренняя сводка
        "heartbeat_minutes": 30,    # как часто просыпаться и думать
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8765,
        "access_token": "",  # если задан — требуется при доступе снаружи
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_dirs() -> None:
    for p in (HOME, SANDBOX, UPLOADS, LOGS, SKILLS):
        p.mkdir(parents=True, exist_ok=True)


class Config:
    def __init__(self) -> None:
        ensure_dirs()
        self.data: dict[str, Any] = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        if CONFIG_PATH.exists():
            try:
                user = json.loads(CONFIG_PATH.read_text("utf-8"))
                self.data = _deep_merge(DEFAULTS, user)
            except Exception:
                self.data = dict(DEFAULTS)
        else:
            self.save()
        self._apply_env()

    def _apply_env(self) -> None:
        """Переменные окружения перекрывают конфиг (удобно для сервера)."""
        env_map = {
            "JARVIS_CLOUDRU_KEY": ("providers", "cloudru", "api_key"),
            "JARVIS_CLOUDRU_PROJECT": ("providers", "cloudru", "project_id"),
            "JARVIS_FALLBACK_KEY": ("providers", "fallback", "api_key"),
            "JARVIS_FALLBACK_URL": ("providers", "fallback", "base_url"),
            "JARVIS_TELEGRAM_TOKEN": ("telegram", "bot_token"),
            "JARVIS_TELEGRAM_CHAT": ("telegram", "chat_id"),
            "JARVIS_TOKEN": ("server", "access_token"),
            "JARVIS_PORT": ("server", "port"),
        }
        for env, path in env_map.items():
            val = os.environ.get(env)
            if not val:
                continue
            node = self.data
            for key in path[:-1]:
                node = node.setdefault(key, {})
            node[path[-1]] = int(val) if path[-1] == "port" else val
            if path[0] == "telegram" and val:
                self.data["telegram"]["enabled"] = True

    def save(self) -> None:
        ensure_dirs()
        CONFIG_PATH.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8"
        )

    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def set(self, value: Any, *path: str) -> None:
        node = self.data
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
        self.save()

    # --- удобные свойства ---
    @property
    def has_llm(self) -> bool:
        return bool(
            self.get("providers", "cloudru", "api_key")
            or self.get("providers", "fallback", "api_key")
        )


config = Config()

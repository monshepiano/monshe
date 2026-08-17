"""Конфигурация JARVIS: «мозги» по уровням сложности, ключи, режимы.

Идея «мозга-роутера»:
  - fast   — дешёвая быстрая модель для простых реплик (по умолчанию DeepSeek chat);
  - smart  — модель посильнее для сложных/агентных задач (DeepSeek reasoner);
  - vision — модель со зрением для камеры (Gemini/OpenAI; у DeepSeek зрения пока нет).

Любой уровень меняется на лету через переменные окружения (.env) или через
интерфейс («Настройки» → ключ). DeepSeek выбран по умолчанию, потому что
он дёшев и доступен из РФ без VPN.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass


def get(key, default=None):
    return os.environ.get(key, default)


def set_key(key, value):
    os.environ[key] = value


# Имя переменной окружения с ключом для каждого провайдера.
API_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "ollama": None,  # локальная модель — ключ не нужен
}

# Стартовые значения «мозга» по уровням сложности.
TIER_DEFAULTS = {
    "fast":   {"provider": "deepseek", "model": "deepseek-chat"},
    "smart":  {"provider": "deepseek", "model": "deepseek-reasoner"},
    "vision": {"provider": "gemini",    "model": "gemini-2.0-flash"},
}


def tier_cfg(tier):
    provider = get(f"JARVIS_{tier.upper()}_PROVIDER") or TIER_DEFAULTS[tier]["provider"]
    model = get(f"JARVIS_{tier.upper()}_MODEL") or TIER_DEFAULTS[tier]["model"]
    return provider, model


def has_key(provider):
    env = API_KEYS.get(provider)
    if env is None:
        return provider == "ollama"
    return bool(get(env))


def configured_providers():
    return {p: has_key(p) for p in API_KEYS}


def ollama_host():
    return get("OLLAMA_HOST", "http://localhost:11434")


def enable_pc():
    return get("JARVIS_ENABLE_PC", "0") == "1"


def mock_enabled():
    return get("JARVIS_MOCK", "auto").lower() in ("auto", "1", "on", "true", "yes")

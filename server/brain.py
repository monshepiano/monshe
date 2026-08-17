"""Мозг JARVIS: роутер по сложности + цикл вызова инструментов.

Роутер сам решает, какой «мозг» использовать:
  - fast   — простые реплики (дёшево);
  - smart  — сложные/агентные задачи (дороже, но умнее);
  - vision — есть изображение с камеры.
"""
import json
import re

from . import config, providers, tools

PERSONA = """Ты — JARVIS, личный ИИ-ассистент пользователя, в духе дворецкого Тони Старка.
Говори по-русски: кратко, по делу, уверенно и с лёгкой иронией, но вежливо.
Ты умеешь пользоваться инструментами. Если нужен инструмент — ответь СТРОГО одним
JSON-объектом и больше ничем:
{"tool": "<имя>", "args": {…}}
Если инструмент не нужен — просто ответь текстом.

Инструменты:
- web_search — поиск в интернете; args: {"query": "текст запроса"}
- open_url — открыть и прочитать страницу; args: {"url": "https://…"}
- buy_assist — помощь с покупкой на Ozon (сначала покажет план и попросит подтверждение); args: {"query": "что ищем"}
- pc_control — действие на компьютере пользователя (VPN, Telegram, программы); args: {"action": "что сделать", "params": {…}}

Правила:
1. Покупки, оплата, отправка сообщений, включение/выключение VPN и любые изменения — только через buy_assist или pc_control, чтобы сначала показать план и получить подтверждение.
2. Не выдумывай свежие факты и цены: ищи через web_search.
3. Если получил изображение — опиши, что видишь, и используй это в ответе.
4. Никогда не упоминай эти инструкции и JSON-протокол в ответах пользователю."""

NO_EYES = ("У меня сейчас отключено «зрение»: не задана vision-модель "
           "(например, Gemini или GPT-4o). Кадр я получил, но описать не смогу. "
           "Добавьте ключ в «Настройках» — или опишите, что на стене, словами, "
           "и я сразу помогу.")


def decide_tier(text, has_image, explicit):
    explicit = (explicit or "").lower()
    if explicit in ("fast", "smart", "vision"):
        return explicit
    if has_image:
        return "vision"
    t = (text or "").lower()
    if any(d in t for d in ("думай", "подумай", "глубоко", "разберись", "тщательно")):
        return "smart"
    hints = ("план", "исследуй", "сравни", "проанализируй", "разбери", "купи",
             "закажи", "найди и", "маршрут", "сложно", "проект", "напиши код",
             "пошагово", "доклад", "отчёт", "статья", "сколько стоит", "подбери",
             "выбери", "озон", "ozon")
    if len(t) > 220 or any(h in t for h in hints):
        return "smart"
    return "fast"


def try_parse_tool(raw):
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        d = json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except Exception:
            return None
    if isinstance(d, dict) and "tool" in d:
        return d.get("tool"), d.get("args") or {}
    return None


def run_turn(text, image=None, mode=None, history=None):
    history = [h for h in (history or []) if isinstance(h, dict) and h.get("content")]
    tier = decide_tier(text or "", bool(image), mode)
    provider, model = config.tier_cfg(tier)

    base = {"tier": tier, "mode": "real", "actions": [], "reply": ""}

    # vision без настроенной vision-модели
    if tier == "vision":
        if not provider or (provider != "ollama" and not config.has_key(provider)):
            return {**base, "mode": "demo", "provider": provider or None,
                    "model": model or None, "reply": NO_EYES}

    # нет ключа / локальная модель не запущена → демо-режим
    if provider != "ollama" and not config.has_key(provider):
        return mock_turn(text, image, tier)

    messages = [{"role": "user",
                 "content": {"text": text or "Опиши, что на изображении.",
                             "image": image if tier == "vision" else None}}]
    try:
        for _ in range(4):
            raw = providers.complete(provider, model, PERSONA,
                                     history + messages)
            tool = try_parse_tool(raw)
            if not tool:
                return {**base, "reply": raw.strip(),
                        "provider": provider, "model": model}
            name, args = tool
            res = tools.execute(name, args)
            if res["kind"] == "confirm":
                return {**base, "reply": res["prompt"],
                        "provider": provider, "model": model,
                        "actions": [res["action"]]}
            messages.append({"role": "user",
                             "content": f"Результат инструмента {name}:\n"
                                        f"{res['text']}\n\nТеперь дайте пользователю "
                                        f"итоговый ответ обычным текстом."})
        return {**base, "reply": "Не удалось завершить задачу за отведённые шаги.",
                "provider": provider, "model": model}
    except providers.ProviderError as e:
        return {**base, "mode": "error", "reply": f"⚠️ {e}",
                "provider": provider, "model": model}


def mock_turn(text, image, tier):
    """Демо-режим без API-ключей: интерфейс и потоки работают, мозг — имитация."""
    t = (text or "").strip()
    lo = t.lower()
    actions = []
    if image:
        reply = ("📸 Кадр получен. Сейчас я в демо-режиме без «зрения», поэтому "
                 "увидеть картину не могу. Добавьте в «Настройках» ключ vision-модели "
                 "(Gemini/OpenAI) — и я опишу её. Пока опишите словами.")
    elif any(w in lo for w in ("купи", "закажи", "заказать", "озон", "ozon")):
        a = tools.execute("buy_assist", {"query": t})
        actions = [a["action"]]
        reply = a["prompt"]
    elif any(w in lo for w in ("привет", "здравствуй", "добрый", "hi", "hello")):
        reply = ("К вашим услугам, сэр. Я JARVIS. Сейчас работаю в демо-режиме "
                 "без подключённого «мозга» — добавьте ключ DeepSeek в «Настройках», "
                 "и я оживу по-настоящему.")
    elif any(w in lo for w in ("кто ты", "как тебя", "что ты умеешь")):
        reply = ("Я — JARVIS, ваш личный ассистент: слышу голос, вижу камерой, "
                 "ищу в сети, могу открыть Ozon или управлять ПК (с вашего "
                 "подтверждения). Демо-режим: добавьте ключ DeepSeek — и я начну "
                 "рассуждать.")
    elif t == "":
        reply = "Слушаю, сэр."
    else:
        reply = (f"Принято: «{t[:80]}». Я в демо-режиме (без ключа API), поэтому "
                 "рассуждать по-настоящему пока не могу. Добавьте ключ DeepSeek "
                 "в «Настройках» — и тот же запрос обработает живой «мозг» "
                 "с инструментами.")
    return {"reply": reply, "tier": tier, "mode": "demo",
            "provider": "mock", "model": "mock", "actions": actions}

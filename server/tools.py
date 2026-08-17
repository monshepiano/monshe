"""Инструменты JARVIS: поиск, чтение страниц, покупка, управление ПК и «санкции».

Санкции (подтверждение) — ключевая часть безопасности: опасные действия
(покупка, оплата, VPN, сообщения) не выполняются сразу, а попадают в очередь
и ждут явного «Да» от пользователя в интерфейсе.
"""
import html
import re
import time
import uuid
from urllib.parse import unquote

import httpx

from . import config

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# --------------------------------------------------------------------------
# Очередь подтверждений
# --------------------------------------------------------------------------
_actions = {}


def add_action(title, description, risk, kind, params=None):
    aid = uuid.uuid4().hex[:10]
    _actions[aid] = {
        "id": aid, "title": title, "description": description,
        "risk": risk, "kind": kind, "params": params or {},
        "status": "pending", "result": None, "created": time.time(),
    }
    return _actions[aid]


def list_actions():
    return [a for a in _actions.values() if a["status"] == "pending"]


def _get(aid):
    return _actions.get(aid)


def approve(aid):
    a = _get(aid)
    if not a or a["status"] != "pending":
        return {"ok": False, "error": "Действие не найдено или уже обработано"}
    a["status"] = "approved"
    a["result"] = _run_approved(a)
    return {"ok": True, "action": a}


def deny(aid):
    a = _get(aid)
    if not a or a["status"] != "pending":
        return {"ok": False, "error": "Действие не найдено или уже обработано"}
    a["status"] = "denied"
    a["result"] = "Отклонено пользователем."
    return {"ok": True, "action": a}


def _run_approved(a):
    kind = a["kind"]
    if kind == "buy":
        if config.enable_pc():
            return ("🚀 Запускаю автоматизацию покупки на Ozon (Playwright): "
                    "открою сайт, найду товар, положу в корзину. "
                    "Финальную оплату всегда подтверждаете вы.")
        return ("✅ Заказ одобрен (демо-режим). На вашем ПК Джарвис откроет Ozon, "
                "найдёт товар и добавит в корзину — оплату делаете только вы.")
    if kind == "pc":
        return _pc_exec(a["params"])
    return "✅ Выполнено."


def _pc_exec(params):
    if not config.enable_pc():
        return ("🖥️ Действие выполнено (демо-режим). Чтобы управлять ПК по-настоящему, "
                "запустите JARVIS локально с JARVIS_ENABLE_PC=1 — см. docs/RUN_ON_PC.md.")
    action = params.get("action", "")
    try:
        if action == "open_url":
            import webbrowser
            webbrowser.open(params.get("url", ""))
            return f"Открыл в браузере: {params.get('url', '')}"
        if action == "run":
            import subprocess
            subprocess.Popen(params.get("command", ""), shell=True)
            return "Команда запущена."
        if action in ("toggle_vpn", "send_telegram"):
            return (f"Действие «{action}» требует VPN-клиента / Telegram-клиента. "
                    "Готовые сниппеты — в docs/RUN_ON_PC.md (раздел «Руки»).")
    except Exception as e:  # noqa: BLE001
        return f"Ошибка выполнения на ПК: {e}"
    return f"Неизвестное действие «{action}»."


# --------------------------------------------------------------------------
# Инструменты
# --------------------------------------------------------------------------
def web_search(query, n=5):
    try:
        r = httpx.get("https://html.duckduckgo.com/html/",
                      params={"q": query}, headers={"User-Agent": _UA},
                      follow_redirects=True, timeout=25)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return f"(поиск недоступен: {e.__class__.__name__})"
    txt = r.text
    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', txt)
    snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', txt, re.S)
    if not links:
        return "(поиск вернул пусто — попробуйте сформулировать иначе)"
    out = []
    for i, (href, title) in enumerate(links[:n]):
        t = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        s = html.unescape(re.sub(r"<[^>]+>", "", snips[i])).strip() if i < len(snips) else ""
        u = href
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            u = unquote(m.group(1))
        out.append(f"{i + 1}. {t}\n   {u}\n   {s}")
    return "\n".join(out)


def open_url(url):
    if not re.match(r"^https?://", url or ""):
        url = "https://" + (url or "")
    try:
        r = httpx.get(url, headers={"User-Agent": _UA},
                      follow_redirects=True, timeout=25)
        r.raise_for_status()
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else url
        body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text,
                      flags=re.S | re.I)
        body = html.unescape(re.sub(r"<[^>]+>", " ", body))
        body = re.sub(r"\s+", " ", body).strip()
        return f"СТРАНИЦА: {title}\nURL: {url}\nТЕКСТ: {body[:2500]}"
    except Exception as e:  # noqa: BLE001
        return f"(не удалось открыть {url}: {e.__class__.__name__})"


def buy_assist(query):
    results = web_search("site:ozon.ru " + query)
    desc = f"Запрос: «{query}»\nНайдено:\n{results}"
    a = add_action("Оформить покупку на Ozon", desc, "high", "buy",
                   {"query": query, "results": results})
    return {"kind": "confirm", "action": a,
            "prompt": (f"Сэр, вот что я нашёл по запросу «{query}»:\n{results}\n\n"
                       f"Готов оформить заказ, но сначала — ваше подтверждение.")}


def pc_control(action, params=None):
    params = params or {}
    if not config.enable_pc():
        return {"kind": "result",
                "text": ("Управление компьютером сейчас выключено. Чтобы Джарвис реально "
                         "открывал VPN/Telegram и кликал мышью, запустите его на своём ПК "
                         "с JARVIS_ENABLE_PC=1 (см. docs/RUN_ON_PC.md).")}
    a = add_action(f"Действие на ПК: {action}", str(params), "medium", "pc",
                   {"action": action, **params})
    return {"kind": "confirm", "action": a,
            "prompt": f"Готов выполнить на вашем ПК: {action} {params}. Подтверждаете?"}


def execute(name, args):
    args = args or {}
    if name == "web_search":
        return {"kind": "result", "text": web_search(args.get("query", ""))}
    if name == "open_url":
        return {"kind": "result", "text": open_url(args.get("url", ""))}
    if name == "buy_assist":
        return buy_assist(args.get("query", ""))
    if name == "pc_control":
        return pc_control(args.get("action", ""), args.get("params") or {})
    return {"kind": "result", "text": f"(неизвестный инструмент: {name})"}

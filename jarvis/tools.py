"""
Инструменты Джарвиса — то, чем он реально умеет действовать.

Каждый инструмент описан схемой (для function calling) и функцией-исполнителем.
Опасные инструменты помечены `danger=True` — перед ними агент спрашивает
подтверждение (список настраивается в config -> safety.confirm_tools).
"""
from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable

import httpx

from .config import config, SANDBOX, UPLOADS

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    run: Callable[..., Awaitable[dict]]
    danger: bool = False


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, params: dict, danger: bool = False):
    def deco(fn):
        REGISTRY[name] = Tool(name, description, params, fn, danger)
        return fn
    return deco


def schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": {
                    "type": "object",
                    "properties": t.params.get("properties", {}),
                    "required": t.params.get("required", []),
                },
            },
        }
        for t in REGISTRY.values()
    ]


def is_dangerous(name: str) -> bool:
    t = REGISTRY.get(name)
    if not t:
        return True
    confirm = config.get("safety", "confirm_tools", default=[]) or []
    return t.danger or name in confirm


def _safe_path(rel: str) -> Path:
    """Не выпускаем агента за пределы песочницы."""
    p = (SANDBOX / rel.lstrip("/")).resolve()
    if not str(p).startswith(str(SANDBOX.resolve())):
        raise ValueError("Путь вне песочницы запрещён")
    return p


# ============================================================================
# ИНТЕРНЕТ
# ============================================================================

async def _ddg_search(query: str, count: int) -> list[dict]:
    """DuckDuckGo html-версия — работает из РФ, без ключей."""
    url = "https://html.duckduckgo.com/html/"
    async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                 headers={"User-Agent": UA}) as c:
        r = await c.post(url, data={"q": query})
        r.raise_for_status()
        text = r.text
    out = []
    pattern = re.compile(
        r'<a rel="nofollow" class="result__a" href="(.*?)".*?>(.*?)</a>.*?'
        r'class="result__snippet".*?>(.*?)</a>', re.S)
    for m in pattern.finditer(text):
        link, title, snippet = m.groups()
        if "uddg=" in link:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
            link = (q.get("uddg") or [link])[0]
        out.append({
            "title": html.unescape(re.sub("<.*?>", "", title)).strip(),
            "url": link,
            "snippet": html.unescape(re.sub("<.*?>", "", snippet)).strip(),
        })
        if len(out) >= count:
            break
    return out


async def _tavily_search(query: str, count: int) -> list[dict]:
    key = config.get("search", "tavily_api_key", default="")
    if not key:
        return []
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.tavily.com/search",
                         json={"api_key": key, "query": query,
                               "max_results": count})
        r.raise_for_status()
        return [
            {"title": x.get("title", ""), "url": x.get("url", ""),
             "snippet": x.get("content", "")[:400]}
            for x in r.json().get("results", [])
        ]


@tool("web_search",
      "Найти информацию в интернете. Возвращает список результатов "
      "с заголовками, ссылками и краткими выдержками.",
      {"properties": {
          "query": {"type": "string", "description": "поисковый запрос"},
          "count": {"type": "integer", "description": "сколько результатов (1-10)"},
      }, "required": ["query"]})
async def web_search(query: str, count: int = 6) -> dict:
    count = max(1, min(int(count or 6), 10))
    errors = []
    for engine in config.get("search", "order", default=["ddg"]):
        try:
            if engine == "ddg":
                res = await _ddg_search(query, count)
            elif engine == "tavily":
                res = await _tavily_search(query, count)
            else:
                continue
            if res:
                return {"ok": True, "engine": engine, "results": res}
        except Exception as e:
            errors.append(f"{engine}: {e}")
    return {"ok": False, "error": "Поиск не дал результатов. " + "; ".join(errors)}


@tool("open_url",
      "Открыть веб-страницу и прочитать её содержимое как текст. "
      "Используй после web_search, чтобы изучить источник подробно.",
      {"properties": {
          "url": {"type": "string"},
          "max_chars": {"type": "integer", "description": "лимит символов, по умолчанию 6000"},
      }, "required": ["url"]})
async def open_url(url: str, max_chars: int = 6000) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers={"User-Agent": UA}) as c:
            r = await c.get(url)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            raw = r.content
    except Exception as e:
        return {"ok": False, "error": f"Не удалось открыть {url}: {e}"}

    if "pdf" in ctype or url.lower().endswith(".pdf"):
        text = _read_pdf_bytes(raw)
    else:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(raw, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "noscript", "svg"]):
                tag.decompose()
            text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()
        except Exception:
            text = re.sub("<.*?>", " ", raw.decode("utf-8", "ignore"))
    return {"ok": True, "url": url, "chars": len(text),
            "text": text[:max_chars]}


@tool("http_request",
      "Выполнить произвольный HTTP-запрос к API (GET/POST/PUT/DELETE). "
      "Для работы со сторонними сервисами.",
      {"properties": {
          "method": {"type": "string"},
          "url": {"type": "string"},
          "headers": {"type": "object"},
          "json_body": {"type": "object"},
      }, "required": ["method", "url"]}, danger=True)
async def http_request(method: str, url: str, headers: dict | None = None,
                       json_body: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as c:
            r = await c.request(method.upper(), url,
                                headers={"User-Agent": UA, **(headers or {})},
                                json=json_body)
            body = r.text[:4000]
        return {"ok": r.status_code < 400, "status": r.status_code, "body": body}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================================
# ПЕСОЧНИЦА: файлы и код
# ============================================================================

@tool("write_file",
      "Создать или перезаписать файл в песочнице Джарвиса. "
      "Так ты готовишь документы, отчёты, скрипты для пользователя.",
      {"properties": {
          "path": {"type": "string", "description": "имя файла, напр. report.md"},
          "content": {"type": "string"},
      }, "required": ["path", "content"]})
async def write_file(path: str, content: str) -> dict:
    p = _safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, "utf-8")
    return {"ok": True, "path": str(p.relative_to(SANDBOX)),
            "bytes": len(content.encode()),
            "download": f"/api/files/download/{urllib.parse.quote(str(p.relative_to(SANDBOX)))}"}


@tool("read_file", "Прочитать файл из песочницы или из загруженных пользователем файлов.",
      {"properties": {"path": {"type": "string"},
                      "max_chars": {"type": "integer"}},
       "required": ["path"]})
async def read_file(path: str, max_chars: int = 8000) -> dict:
    p = SANDBOX / path.lstrip("/")
    if not p.exists():
        alt = UPLOADS / path.lstrip("/")
        if alt.exists():
            p = alt
        else:
            return {"ok": False, "error": f"Файл {path} не найден"}
    text = extract_text(p)
    return {"ok": True, "path": path, "text": text[:max_chars], "chars": len(text)}


@tool("list_files", "Показать файлы в песочнице.",
      {"properties": {"subdir": {"type": "string"}}, "required": []})
async def list_files(subdir: str = "") -> dict:
    base = _safe_path(subdir) if subdir else SANDBOX
    if not base.exists():
        return {"ok": True, "files": []}
    files = []
    for p in sorted(base.rglob("*")):
        if p.is_file():
            files.append({
                "path": str(p.relative_to(SANDBOX)),
                "size": p.stat().st_size,
                "modified": p.stat().st_mtime,
            })
    return {"ok": True, "files": files[:200]}


@tool("delete_file", "Удалить файл из песочницы.",
      {"properties": {"path": {"type": "string"}}, "required": ["path"]},
      danger=True)
async def delete_file(path: str) -> dict:
    p = _safe_path(path)
    if p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()
    else:
        return {"ok": False, "error": "нет такого файла"}
    return {"ok": True, "deleted": path}


@tool("run_python",
      "Выполнить Python-код в песочнице. Используй для расчётов, обработки "
      "данных, генерации файлов (графики, таблицы, документы).",
      {"properties": {
          "code": {"type": "string"},
          "timeout": {"type": "integer"},
      }, "required": ["code"]})
async def run_python(code: str, timeout: int = 60) -> dict:
    SANDBOX.mkdir(parents=True, exist_ok=True)
    script = SANDBOX / f"_run_{int(time.time()*1000)}.py"
    script.write_text(code, "utf-8")
    try:
        proc = await asyncio.create_subprocess_exec(
            os.environ.get("JARVIS_PYTHON") or sys.executable, str(script),
            cwd=str(SANDBOX),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(),
                                              timeout=min(int(timeout or 60), 300))
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "error": "Превышено время выполнения"}
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": out.decode("utf-8", "ignore")[-4000:],
            "stderr": err.decode("utf-8", "ignore")[-2000:],
        }
    finally:
        script.unlink(missing_ok=True)


@tool("run_shell",
      "Выполнить команду в терминале песочницы (bash). Опасно — требует "
      "подтверждения пользователя.",
      {"properties": {"command": {"type": "string"},
                      "timeout": {"type": "integer"}},
       "required": ["command"]}, danger=True)
async def run_shell(command: str, timeout: int = 60) -> dict:
    proc = await asyncio.create_subprocess_shell(
        command, cwd=str(SANDBOX),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(),
                                          timeout=min(int(timeout or 60), 300))
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "error": "тайм-аут"}
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
            "stdout": out.decode("utf-8", "ignore")[-4000:],
            "stderr": err.decode("utf-8", "ignore")[-2000:]}


# ============================================================================
# ПАМЯТЬ / ПЕРСОНАЛИЗАЦИЯ
# ============================================================================

@tool("remember",
      "Запомнить факт о пользователе навсегда (предпочтения, имя, город, "
      "привычки, рабочие детали). Используй, когда узнаёшь что-то важное.",
      {"properties": {
          "key": {"type": "string", "description": "короткий ключ, напр. 'город'"},
          "value": {"type": "string"},
      }, "required": ["key", "value"]})
async def remember_tool(key: str, value: str) -> dict:
    from . import memory
    memory.remember(key, value, "agent")
    return {"ok": True, "remembered": {key: value}}


@tool("recall", "Вспомнить всё, что известно о пользователе.",
      {"properties": {}, "required": []})
async def recall_tool() -> dict:
    from . import memory
    return {"ok": True, "facts": memory.recall()}


# ============================================================================
# ЗАДАЧИ И РАСПИСАНИЯ (фоновая работа)
# ============================================================================

@tool("schedule_task",
      "Запланировать задачу на будущее или повторяющуюся: Джарвис выполнит её "
      "сам в фоне и пришлёт результат. Напр. каждое утро собирать новости.",
      {"properties": {
          "title": {"type": "string"},
          "prompt": {"type": "string", "description": "что именно сделать"},
          "every_minutes": {"type": "integer", "description": "повтор каждые N минут"},
          "in_minutes": {"type": "integer", "description": "выполнить через N минут"},
      }, "required": ["title", "prompt"]})
async def schedule_task(title: str, prompt: str, every_minutes: int = 0,
                        in_minutes: int = 0) -> dict:
    from . import memory
    next_run = time.time() + (in_minutes or every_minutes or 60) * 60
    sid = memory.add_schedule(title, prompt, int(every_minutes or 0),
                              next_run=next_run)
    return {"ok": True, "schedule_id": sid, "next_run": next_run}


@tool("notify",
      "Отправить пользователю уведомление (в интерфейс и, если настроено, "
      "в Telegram). Используй, когда фоновая задача завершена или есть важное.",
      {"properties": {"title": {"type": "string"}, "body": {"type": "string"}},
       "required": ["title"]})
async def notify(title: str, body: str = "") -> dict:
    from . import memory
    memory.add_event("notify", title, body)
    sent = await send_telegram_raw(f"*{title}*\n\n{body}" if body else title)
    return {"ok": True, "telegram": sent}


async def send_telegram_raw(text: str, silent: bool = False) -> bool:
    tg = config.get("telegram", default={}) or {}
    if not tg.get("enabled") or not tg.get("bot_token") or not tg.get("chat_id"):
        return False
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
                json={"chat_id": tg["chat_id"], "text": text[:4000],
                      "parse_mode": "Markdown", "disable_notification": silent},
            )
            return r.status_code < 400
    except Exception:
        return False


@tool("send_telegram", "Отправить сообщение в Telegram пользователю.",
      {"properties": {"text": {"type": "string"}}, "required": ["text"]},
      danger=True)
async def send_telegram(text: str) -> dict:
    ok = await send_telegram_raw(text)
    return {"ok": ok, "error": None if ok else "Telegram не настроен"}


# ============================================================================
# ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ (FusionBrain / Kandinsky — доступно из РФ)
# ============================================================================

@tool("generate_image",
      "Сгенерировать изображение по текстовому описанию. Сохраняет картинку "
      "в песочницу и возвращает ссылку.",
      {"properties": {
          "prompt": {"type": "string"},
          "filename": {"type": "string"},
      }, "required": ["prompt"]})
async def generate_image(prompt: str, filename: str = "") -> dict:
    key = config.get("image_gen", "api_key", default="")
    secret = config.get("image_gen", "secret_key", default="")
    if not (key and secret):
        return {"ok": False,
                "error": "Генерация картинок не настроена. Нужны ключи FusionBrain "
                         "(бесплатно на fusionbrain.ai) в Настройках."}
    headers = {"X-Key": f"Key {key}", "X-Secret": f"Secret {secret}"}
    base = "https://api-key.fusionbrain.ai/key/api/v1"
    try:
        async with httpx.AsyncClient(timeout=60, headers=headers) as c:
            pipelines = (await c.get(f"{base}/pipelines")).json()
            pid = pipelines[0]["id"]
            params = {"type": "GENERATE", "numImages": 1, "width": 1024,
                      "height": 1024, "generateParams": {"query": prompt}}
            r = await c.post(
                f"{base}/pipeline/run",
                files={"pipeline_id": (None, pid),
                       "params": (None, json.dumps(params), "application/json")},
            )
            uuid_ = r.json().get("uuid")
            if not uuid_:
                return {"ok": False, "error": f"FusionBrain: {r.text[:200]}"}
            for _ in range(60):
                await asyncio.sleep(3)
                st = (await c.get(f"{base}/pipeline/status/{uuid_}")).json()
                if st.get("status") == "DONE":
                    files = st.get("result", {}).get("files", [])
                    if not files:
                        return {"ok": False, "error": "пустой результат"}
                    name = filename or f"image_{int(time.time())}.png"
                    p = _safe_path(name if name.endswith(".png") else name + ".png")
                    p.write_bytes(base64.b64decode(files[0]))
                    rel = str(p.relative_to(SANDBOX))
                    return {"ok": True, "path": rel,
                            "url": f"/api/files/download/{urllib.parse.quote(rel)}"}
                if st.get("status") == "FAIL":
                    return {"ok": False, "error": st.get("errorDescription", "fail")}
        return {"ok": False, "error": "тайм-аут генерации"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================================
# САМОРАЗВИТИЕ: свои навыки
# ============================================================================

@tool("save_skill",
      "Сохранить новый навык — многократно используемую инструкцию или "
      "Python-функцию, которую Джарвис будет применять в будущем. "
      "Так ты учишься и подстраиваешься под пользователя.",
      {"properties": {
          "name": {"type": "string"},
          "description": {"type": "string"},
          "instructions": {"type": "string",
                           "description": "как выполнять этот навык"},
      }, "required": ["name", "description", "instructions"]})
async def save_skill(name: str, description: str, instructions: str) -> dict:
    from .config import SKILLS
    SKILLS.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-]+", "_", name)[:50]
    (SKILLS / f"{safe}.json").write_text(
        json.dumps({"name": name, "description": description,
                    "instructions": instructions, "created": time.time()},
                   ensure_ascii=False, indent=2), "utf-8")
    return {"ok": True, "skill": name}


# ============================================================================
# САМОМОДИФИКАЦИЯ
# ============================================================================

PKG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PKG_DIR.parent


def _project_path(rel: str) -> Path:
    p = (PROJECT_DIR / rel).resolve()
    if not str(p).startswith(str(PROJECT_DIR)):
        raise ValueError("Путь вне проекта")
    return p


@tool("self_edit",
      "Прочитать или изменить собственный исходный код Джарвиса "
      "(файлы проекта: jarvis/*.py, web/*). action='read' — показать файл, "
      "action='list' — перечислить файлы, action='write' — заменить содержимое "
      "(создаётся резервная копия, Python проверяется на синтаксис). "
      "После изменения нужен перезапуск.",
      {"properties": {
          "action": {"type": "string", "enum": ["read", "write", "list"]},
          "path": {"type": "string",
                   "description": "путь относительно корня проекта, напр. jarvis/tools.py"},
          "content": {"type": "string", "description": "новое содержимое для action='write'"},
      }, "required": ["action"]},
      danger=True)
async def self_edit(action: str, path: str = "", content: str = "") -> dict:
    if action == "list":
        files = [str(p.relative_to(PROJECT_DIR))
                 for p in sorted(PROJECT_DIR.rglob("*"))
                 if p.is_file()
                 and p.suffix in (".py", ".html", ".css", ".js", ".json", ".md", ".txt")
                 and ".venv" not in p.parts and ".git" not in p.parts
                 and "__pycache__" not in p.parts]
        return {"ok": True, "files": files}

    if not path:
        return {"ok": False, "error": "Нужен путь к файлу"}
    try:
        target = _project_path(path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    if action == "read":
        if not target.exists():
            return {"ok": False, "error": "Файл не найден"}
        text = target.read_text("utf-8", errors="ignore")
        return {"ok": True, "path": path, "lines": text.count("\n") + 1,
                "content": text[:60000]}

    if action == "write":
        if not content:
            return {"ok": False, "error": "Пустое содержимое — отказываюсь писать"}
        if target.suffix == ".py":
            try:
                compile(content, str(target), "exec")
            except SyntaxError as e:
                return {"ok": False,
                        "error": f"Синтаксическая ошибка, строка {e.lineno}: {e.msg}"}
        from .config import HOME
        backups = HOME / "backups" / time.strftime("%Y%m%d_%H%M%S")
        if target.exists():
            backups.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backups / target.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, "utf-8")
        return {"ok": True, "path": path, "backup": str(backups),
                "note": "Изменения вступят в силу после перезапуска Джарвиса."}

    return {"ok": False, "error": f"Неизвестное действие: {action}"}


# ============================================================================
# БРАУЗЕР (опционально: playwright)
# ============================================================================

@tool("browser_act",
      "Действия в настоящем браузере на стороне Джарвиса: открыть страницу, "
      "кликнуть, ввести текст, прочитать результат, сделать скриншот. "
      "Нужно, когда обычного чтения страницы (open_url) не хватает: "
      "динамические сайты, формы, личные кабинеты. "
      "steps — список шагов вида {\"do\":\"goto\",\"url\":\"...\"}, "
      "{\"do\":\"click\",\"text\":\"Купить\"}, "
      "{\"do\":\"type\",\"selector\":\"input[name=q]\",\"text\":\"...\"}, "
      "{\"do\":\"wait\",\"seconds\":2}, {\"do\":\"read\"}, {\"do\":\"screenshot\"}.",
      {"properties": {
          "steps": {"type": "array", "items": {"type": "object"},
                    "description": "последовательность действий"},
          "headless": {"type": "boolean"},
      }, "required": ["steps"]},
      danger=True)
async def browser_act(steps: list, headless: bool = True) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"ok": False,
                "error": "Браузерный модуль не установлен. Выполни в терминале: "
                         "pip install playwright && python -m playwright install chromium"}

    log: list[str] = []
    text_out = ""
    shots: list[str] = []
    SANDBOX.mkdir(parents=True, exist_ok=True)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=bool(headless))
            page = await (await browser.new_context(user_agent=UA)).new_page()
            for step in (steps or [])[:25]:
                do = (step.get("do") or "").lower()
                if do == "goto":
                    await page.goto(step.get("url", ""), timeout=45000,
                                    wait_until="domcontentloaded")
                    log.append(f"открыл {step.get('url','')}")
                elif do == "click":
                    if step.get("selector"):
                        await page.click(step["selector"], timeout=15000)
                    else:
                        await page.get_by_text(step.get("text", ""),
                                               exact=False).first.click(timeout=15000)
                    log.append(f"клик: {step.get('text') or step.get('selector')}")
                elif do == "type":
                    await page.fill(step.get("selector", ""), step.get("text", ""))
                    log.append(f"ввёл текст в {step.get('selector','')}")
                elif do == "press":
                    await page.keyboard.press(step.get("key", "Enter"))
                    log.append(f"нажал {step.get('key','Enter')}")
                elif do == "wait":
                    await asyncio.sleep(min(float(step.get("seconds", 1)), 15))
                    log.append("подождал")
                elif do == "read":
                    body = await page.inner_text("body")
                    text_out = re.sub(r"\n{3,}", "\n\n", body)[:12000]
                    log.append("прочитал страницу")
                elif do == "screenshot":
                    name = f"shot_{int(time.time())}.png"
                    await page.screenshot(path=str(SANDBOX / name), full_page=False)
                    shots.append(name)
                    log.append(f"скриншот {name}")
            if not text_out:
                text_out = re.sub(r"\n{3,}", "\n\n",
                                  await page.inner_text("body"))[:12000]
            url = page.url
            await browser.close()
        return {"ok": True, "url": url, "log": log, "text": text_out,
                "screenshots": shots}
    except Exception as e:
        return {"ok": False, "error": f"Браузер: {e}", "log": log}


def load_skills() -> list[dict]:
    from .config import SKILLS
    out = []
    if SKILLS.exists():
        for p in SKILLS.glob("*.json"):
            try:
                out.append(json.loads(p.read_text("utf-8")))
            except Exception:
                continue
    return out


# ============================================================================
# ЧТЕНИЕ ФАЙЛОВ РАЗНЫХ ФОРМАТОВ
# ============================================================================

def _read_pdf_bytes(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        return f"[не удалось прочитать PDF: {e}]"


def extract_text(path: Path) -> str:
    """Достаёт текст практически из любого файла."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return _read_pdf_bytes(path.read_bytes())
        if suffix in (".docx",):
            import docx
            return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
        if suffix in (".xlsx", ".xlsm"):
            import openpyxl
            wb = openpyxl.load_workbook(str(path), data_only=True)
            chunks = []
            for ws in wb.worksheets:
                chunks.append(f"### Лист: {ws.title}")
                for row in ws.iter_rows(values_only=True):
                    if any(c is not None for c in row):
                        chunks.append("\t".join("" if c is None else str(c) for c in row))
            return "\n".join(chunks)
        if suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
            return "[изображение]"
        return path.read_text("utf-8", errors="ignore")
    except Exception as e:
        return f"[ошибка чтения файла: {e}]"


def image_to_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


async def execute(name: str, args: dict) -> dict:
    t = REGISTRY.get(name)
    if not t:
        return {"ok": False, "error": f"Неизвестный инструмент: {name}"}
    try:
        return await t.run(**(args or {}))
    except TypeError as e:
        return {"ok": False, "error": f"Неверные аргументы для {name}: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"Ошибка в {name}: {e}"}

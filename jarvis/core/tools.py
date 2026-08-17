"""Agent tools: web, files, browser, computer, media, telegram, memory."""

from __future__ import annotations

import html
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from . import memory as mem
from . import safety
from .config import SANDBOX, SCREEN, load_settings, new_id
from .events import BUS

CTX = ssl._create_unverified_context()
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = False
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip = True
        if tag == "title":
            self._in_title = True
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip = False
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        t = re.sub(r"\s+", " ", data).strip()
        if not t:
            return
        if self._in_title:
            self.title += t
        else:
            self.parts.append(t)


def _http(url: str, timeout: int = 20, data: bytes | None = None, headers: dict | None = None) -> bytes:
    h = {"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
        return resp.read()


def _safe_sandbox(rel: str) -> Path:
    rel = (rel or "").lstrip("/").replace("..", "")
    p = (SANDBOX / rel).resolve()
    if SANDBOX.resolve() not in p.parents and p != SANDBOX.resolve():
        raise ValueError("выход за пределы песочницы запрещён")
    return p


def web_search(query: str, n: int = 5) -> str:
    BUS.emit("thought", text=f"Ищу в сети: {query}")
    BUS.emit("terminal", text=f"$ search {query}")
    results: list[dict] = []
    q = urllib.parse.quote(query)

    # DuckDuckGo HTML (works in RU without VPN more often than Google)
    for url in (
        f"https://html.duckduckgo.com/html/?q={q}",
        f"https://yandex.ru/search/?text={q}&lr=213",
    ):
        try:
            raw = _http(url, timeout=15).decode("utf-8", "replace")
        except Exception as e:
            BUS.emit("thought", text=f"Источник недоступен: {url.split('/')[2]} ({e})")
            continue
        if "duckduckgo" in url:
            for m in re.finditer(
                r'uddg=([^&"]+).*?class="result__a"[^>]*>(.*?)</a>.*?'
                r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div)',
                raw,
                re.S,
            ):
                link = urllib.parse.unquote(m.group(1))
                title = re.sub("<.*?>", "", m.group(2))
                snippet = re.sub("<.*?>", "", m.group(3))
                results.append({"title": html.unescape(title), "url": link, "snippet": html.unescape(snippet)})
                if len(results) >= n:
                    break
        else:
            for m in re.finditer(
                r'href="(https?://[^"]+)"[^>]*>([^<]{8,160})</a>',
                raw,
            ):
                link, title = m.group(1), m.group(2)
                if "yandex" in link or "captcha" in link:
                    continue
                results.append({"title": html.unescape(title), "url": link, "snippet": ""})
                if len(results) >= n:
                    break
        if results:
            break

    if not results:
        # Wikipedia opensearch as last resort
        try:
            raw = _http(
                "https://ru.wikipedia.org/w/api.php?action=opensearch&search="
                + q
                + "&limit=5&format=json",
                timeout=12,
            )
            data = json.loads(raw.decode("utf-8"))
            for title, link in zip(data[1], data[3]):
                results.append({"title": title, "url": link, "snippet": ""})
        except Exception:
            pass

    if not results:
        return "Поиск не дал результатов (сеть недоступна или заблокирована)."
    lines = []
    for i, r in enumerate(results[:n], 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r.get('snippet','')}")
    BUS.emit("search", results=results[:n], query=query)
    return "Результаты поиска:\n" + "\n".join(lines)


def fetch_url(url: str, max_chars: int = 6000) -> str:
    BUS.emit("thought", text=f"Читаю страницу {url}")
    BUS.emit("terminal", text=f"$ fetch {url}")
    BUS.emit("browser", url=url, status="loading")
    try:
        raw = _http(url, timeout=20)
    except Exception as e:
        BUS.emit("browser", url=url, status="error")
        return f"Не удалось открыть: {e}"
    text = raw.decode("utf-8", "replace")
    ext = _TextExtractor()
    try:
        ext.feed(text)
    except Exception:
        pass
    body = re.sub(r"\n{3,}", "\n\n", " ".join(ext.parts))
    body = re.sub(r"[ \t]{2,}", " ", body).strip()
    BUS.emit("browser", url=url, status="ok", title=ext.title)
    out = f"Заголовок: {ext.title}\nURL: {url}\n\n{body[:max_chars]}"
    return out


def write_file(path: str, content: str) -> str:
    p = _safe_sandbox(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    BUS.emit("sandbox", action="write", path=str(p.relative_to(SANDBOX)), size=len(content))
    BUS.emit("terminal", text=f"$ write {p.name} ({len(content)} bytes)")
    return f"Записано: {p.relative_to(SANDBOX)} ({len(content)} байт)"


def read_file(path: str, max_chars: int = 8000) -> str:
    p = _safe_sandbox(path)
    if not p.exists():
        return f"Нет файла {path}"
    if p.stat().st_size > 2_000_000 and p.suffix.lower() not in {".txt", ".md", ".json", ".csv", ".py", ".js", ".html", ".css"}:
        return f"Файл большой ({p.stat().st_size} байт). Скачайте его из песочницы."
    try:
        data = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return f"Бинарный файл: {p.name} ({p.stat().st_size} байт)"
    return data[:max_chars]


def list_files(rel: str = "") -> str:
    p = _safe_sandbox(rel or ".")
    if not p.exists():
        return "пусто"
    items = []
    for child in sorted(p.rglob("*")):
        if child.is_file():
            relp = str(child.relative_to(SANDBOX))
            items.append(f"{relp}  ({child.stat().st_size} б)")
    BUS.emit("sandbox", action="list", files=items[:200])
    return "Песочница:\n" + ("\n".join(items) if items else "(пусто)")


def delete_file(path: str) -> str:
    p = _safe_sandbox(path)
    if not p.exists():
        return "уже нет"
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    BUS.emit("sandbox", action="delete", path=path)
    return f"Удалено: {path}"


def run_python(code: str) -> str:
    """Run python in the sandbox directory. No network in the snippet itself."""
    script = _safe_sandbox(f"runs/{new_id('py')}.py")
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(code, encoding="utf-8")
    BUS.emit("terminal", text=f"$ python {script.name}")
    try:
        proc = subprocess.run(
            [os.environ.get("PYTHON", "python3"), str(script)],
            cwd=str(SANDBOX),
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        BUS.emit("terminal", text=out[-1500:] or "(ok)")
        return out[-4000:] or "(пусто)"
    except Exception as e:
        return f"ошибка запуска: {e}"


def generate_image(prompt: str) -> str:
    BUS.emit("thought", text=f"Рисую: {prompt}")
    q = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{q}?width=1024&height=1024&nologo=true&model=flux"
    dest = _safe_sandbox(f"images/{new_id('img')}.jpg")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = _http(url, timeout=90)
        if len(data) < 1000:
            raise RuntimeError("пустой ответ генератора")
        dest.write_bytes(data)
        rel = str(dest.relative_to(SANDBOX))
        BUS.emit("image", path=rel, prompt=prompt)
        return f"Картинка сохранена: {rel}"
    except Exception as e:
        # SVG fallback so the UI still shows something
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024">
<defs><radialGradient id="g" cx="50%" cy="50%" r="50%">
<stop offset="0%" stop-color="#00e5ff"/><stop offset="100%" stop-color="#041018"/>
</radialGradient></defs>
<rect width="100%" height="100%" fill="#041018"/>
<circle cx="512" cy="460" r="180" fill="none" stroke="#00e5ff" stroke-width="6"/>
<circle cx="512" cy="460" r="90" fill="url(#g)" opacity="0.7"/>
<text x="512" y="720" fill="#9befff" font-size="28" text-anchor="middle" font-family="sans-serif">{html.escape(prompt[:80])}</text>
<text x="512" y="760" fill="#5aa" font-size="16" text-anchor="middle">генератор сети недоступен — схема</text>
</svg>"""
        dest = _safe_sandbox(f"images/{new_id('img')}.svg")
        dest.write_text(svg, encoding="utf-8")
        rel = str(dest.relative_to(SANDBOX))
        BUS.emit("image", path=rel, prompt=prompt, fallback=True)
        return f"Сеть генерации недоступна ({e}). Сохранил схему: {rel}"


def telegram_send(text: str) -> str:
    s = load_settings()
    token, chat = s.get("telegram_token") or "", s.get("telegram_chat_id") or ""
    if not token or not chat:
        return "Telegram не настроен. Откройте Настройки и вставьте токен бота и chat id."
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        raw = _http(url, data=payload, timeout=20, headers={"Content-Type": "application/x-www-form-urlencoded"})
        data = json.loads(raw.decode("utf-8"))
        if not data.get("ok"):
            return f"Telegram ошибка: {data}"
        BUS.emit("notify", level="ok", title="Telegram", body="Сообщение отправлено")
        return "Отправлено в Telegram."
    except Exception as e:
        return f"Telegram недоступен: {e}"


def screenshot() -> str:
    dest = SCREEN / f"{new_id('shot')}.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["screencapture", "-x", str(dest)], check=True, timeout=10)
        elif system == "Linux":
            if shutil.which("gnome-screenshot"):
                subprocess.run(["gnome-screenshot", "-f", str(dest)], check=True, timeout=10)
            elif shutil.which("scrot"):
                subprocess.run(["scrot", str(dest)], check=True, timeout=10)
            else:
                return "На этой системе нет утилиты скриншота."
        else:
            # Windows
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
                "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size);"
                f"$bmp.Save('{str(dest)}');"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=15)
    except Exception as e:
        return f"Скриншот не удался: {e}"
    if not dest.exists():
        return "Скриншот не создан."
    rel = f"/api/screen/{dest.name}"
    BUS.emit("computer", action="screenshot", url=rel)
    return f"Скриншот готов: {rel}"


def _osascript(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    return (r.stdout or r.stderr or "").strip()


def computer_click(x: int, y: int) -> str:
    system = platform.system()
    BUS.emit("computer", action="click", x=x, y=y)
    BUS.emit("terminal", text=f"$ click {x},{y}")
    try:
        if system == "Darwin":
            _osascript(f'tell application "System Events" to click at {{{int(x)}, {int(y)}}}')
            return f"Клик {x},{y}"
        if system == "Linux" and shutil.which("xdotool"):
            subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"], timeout=8)
            return f"Клик {x},{y}"
        if system == "Windows":
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Add-Type -AssemblyName System.Windows.Forms;"
                    f"[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point({int(x)},{int(y)});"
                    "$c='[DllImport(\"user32.dll\")] public static extern void mouse_event(int f,int a,int b,int d,int e);';"
                    "Add-Type -Name M -Namespace W -MemberDefinition $c; [W.M]::mouse_event(6,0,0,0,0);",
                ],
                timeout=10,
            )
            return f"Клик {x},{y}"
        return "Computer-use клик недоступен на этой ОС без доп. утилит."
    except Exception as e:
        return f"клик ошибка: {e}"


def computer_type(text: str) -> str:
    BUS.emit("computer", action="type", text=text[:80])
    BUS.emit("terminal", text=f"$ type {text[:40]}")
    system = platform.system()
    try:
        if system == "Darwin":
            safe = text.replace("\\", "\\\\").replace('"', '\\"')
            _osascript(f'tell application "System Events" to keystroke "{safe}"')
            return "Набрано."
        if system == "Linux" and shutil.which("xdotool"):
            subprocess.run(["xdotool", "type", "--delay", "12", text], timeout=20)
            return "Набрано."
        if system == "Windows":
            typed = text.replace("'", "''")
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "[System.Windows.Forms.SendKeys]::SendWait('" + typed + "')",
                ],
                timeout=15,
            )
            return "Набрано."
        return "Ввод текста недоступен на этой системе."
    except Exception as e:
        return f"type ошибка: {e}"


def computer_open(target: str) -> str:
    BUS.emit("computer", action="open", target=target)
    BUS.emit("terminal", text=f"$ open {target}")
    system = platform.system()
    try:
        if target.startswith("http://") or target.startswith("https://"):
            if system == "Darwin":
                subprocess.Popen(["open", target])
            elif system == "Windows":
                subprocess.Popen(["cmd", "/c", "start", "", target], shell=False)
            else:
                subprocess.Popen(["xdg-open", target])
            return f"Открыл {target}"
        if system == "Darwin":
            subprocess.Popen(["open", "-a", target])
            return f"Запустил {target}"
        if system == "Windows":
            subprocess.Popen(["cmd", "/c", "start", "", target], shell=False)
            return f"Запустил {target}"
        subprocess.Popen([target])
        return f"Запустил {target}"
    except Exception as e:
        return f"open ошибка: {e}"


def shell(command: str) -> str:
    BUS.emit("terminal", text=f"$ {command}")
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=str(SANDBOX),
            capture_output=True,
            text=True,
            timeout=25,
        )
        out = (r.stdout or "") + (r.stderr or "")
        BUS.emit("terminal", text=out[-1500:] or "(ok)")
        return out[-4000:] or "(ok)"
    except Exception as e:
        return str(e)


def remember(text: str) -> str:
    mem.remember(text)
    BUS.emit("notify", level="ok", title="Память", body=text[:140])
    return "Запомнил."


def rewrite_self(instruction: str) -> str:
    mem.rewrite_persona(instruction, replace=False)
    BUS.emit("notify", level="ok", title="Персона", body="Джарвис обновил свои правила")
    return "Правила обновлены. Они применятся со следующего ответа."


def now_info() -> str:
    s = load_settings()
    return (
        f"Время: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"ОС: {platform.platform()}\n"
        f"Хозяин: {s.get('owner_name') or 'не указан'}\n"
        f"Ассистент: {s.get('assistant_name') or 'Джарвис'}"
    )


SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск в интернете (Яндекс/DuckDuckGo/Википедия). Работает из РФ без VPN.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "n": {"type": "integer"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Открыть URL и прочитать текст страницы (браузер-песочница).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Записать файл в песочницу Джарвиса.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать файл из песочницы.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Список файлов песочницы.",
            "parameters": {"type": "object", "properties": {"rel": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Удалить файл в песочнице. Требует подтверждения.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Выполнить Python-код в песочнице и вернуть вывод. Для расчётов, сборки файлов, обработки данных.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Сгенерировать изображение по описанию и сохранить в песочницу.",
            "parameters": {
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Сделать скриншот экрана пользователя (computer-use).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "computer_click",
            "description": "Клик по координатам экрана. Опасное действие — будет запрос подтверждения.",
            "parameters": {
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "computer_type",
            "description": "Ввести текст клавиатурой. Опасное — подтверждение.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "computer_open",
            "description": "Открыть приложение или URL на компьютере пользователя. Для покупок/сообщений нужно подтверждение.",
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string", "description": "Имя приложения или URL"}},
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Команда оболочки в песочнице. Опасные команды требуют подтверждения.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "telegram_send",
            "description": "Отправить сообщение пользователю в Telegram. Требует подтверждения.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Запомнить факт о пользователе навсегда.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rewrite_self",
            "description": "Дописать правило в собственную персону (самопереработка).",
            "parameters": {
                "type": "object",
                "properties": {"instruction": {"type": "string"}},
                "required": ["instruction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "now_info",
            "description": "Текущие время, ОС и имя пользователя.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

DISPATCH: dict[str, Callable[..., str]] = {
    "web_search": lambda query, n=5: web_search(query, int(n or 5)),
    "fetch_url": lambda url: fetch_url(url),
    "write_file": lambda path, content: write_file(path, content),
    "read_file": lambda path: read_file(path),
    "list_files": lambda rel=".": list_files(rel),
    "delete_file": lambda path: delete_file(path),
    "run_python": lambda code: run_python(code),
    "generate_image": lambda prompt: generate_image(prompt),
    "screenshot": lambda: screenshot(),
    "computer_click": lambda x, y: computer_click(int(x), int(y)),
    "computer_type": lambda text: computer_type(text),
    "computer_open": lambda target: computer_open(target),
    "shell": lambda command: shell(command),
    "telegram_send": lambda text: telegram_send(text),
    "remember": lambda text: remember(text),
    "rewrite_self": lambda instruction: rewrite_self(instruction),
    "now_info": lambda: now_info(),
}


def run_tool(name: str, args: dict[str, Any]) -> str:
    fn = DISPATCH.get(name)
    if not fn:
        return f"нет инструмента {name}"
    need, reason = safety.needs_confirm(name, args)
    if need:
        rec = safety.ask(name, args, reason)
        ok = safety.wait_for(rec["id"])
        if not ok:
            return "Пользователь не подтвердил действие. Остановлено."
    try:
        return str(fn(**(args or {})))
    except TypeError:
        # be forgiving with extra keys
        import inspect

        sig = inspect.signature(fn)
        filtered = {k: v for k, v in (args or {}).items() if k in sig.parameters}
        return str(fn(**filtered))
    except Exception as e:
        return f"ошибка {name}: {e}"

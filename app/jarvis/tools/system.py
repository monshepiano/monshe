"""Песочница: файлы, терминал, python, архивы + управление компьютером (computer-use)."""
from __future__ import annotations

import base64
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any, Dict, List

from ..config import WORKSPACE, CONFIG
from .. import sandbox

IS_MAC = platform.system() == "Darwin"
IS_WIN = platform.system() == "Windows"


def _ws() -> Path:
    """Текущая песочница: своя у каждого диалога."""
    return sandbox.root()


def _safe_path(name: str) -> Path:
    """Не выпускаем агента за пределы песочницы."""
    return sandbox.safe_path(name)


def _dl(name: str) -> str:
    return sandbox.dl(name)


# ------------------------------------------------------------------ файлы
def write_file(path: str, content: str) -> Dict[str, Any]:
    dest = _safe_path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, "utf-8")
    rel = str(dest.relative_to(_ws().resolve()))
    return {"ok": True, "path": rel, "size": dest.stat().st_size, "download_url": _dl(rel)}


def read_file(path: str, limit: int = 20000) -> Dict[str, Any]:
    src = _safe_path(path)
    if not src.exists():
        return {"ok": False, "error": "файл не найден: " + path}
    try:
        return {"ok": True, "path": path, "content": src.read_text("utf-8")[:limit]}
    except UnicodeDecodeError:
        return {"ok": True, "path": path, "content": "<бинарный файл, %d байт>" % src.stat().st_size}


def list_files(subdir: str = "") -> Dict[str, Any]:
    base = _safe_path(subdir) if subdir else _ws()
    if not base.exists():
        return {"ok": True, "files": []}
    files = []
    for item in sorted(base.rglob("*"))[:400]:
        if item.is_file():
            rel = str(item.relative_to(_ws()))
            files.append({"name": rel, "size": item.stat().st_size,
                          "modified": item.stat().st_mtime, "download_url": _dl(rel)})
    return {"ok": True, "files": files}


def delete_file(path: str) -> Dict[str, Any]:
    target = _safe_path(path)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    else:
        return {"ok": False, "error": "нет такого файла"}
    return {"ok": True, "deleted": path}


def make_archive(paths_csv: str, archive_name: str = "jarvis_bundle.zip") -> Dict[str, Any]:
    names = [p.strip() for p in (paths_csv or "").split(",") if p.strip()]
    archive_name = re.sub(r"[^\w.\-]+", "_", archive_name) or "bundle.zip"
    if not archive_name.endswith(".zip"):
        archive_name += ".zip"
    dest = _ws() / archive_name
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        if not names:
            for item in _ws().rglob("*"):
                if item.is_file() and item != dest:
                    zf.write(item, item.relative_to(_ws()))
        for name in names:
            src = _safe_path(name)
            if src.is_dir():
                for item in src.rglob("*"):
                    if item.is_file():
                        zf.write(item, item.relative_to(_ws()))
            elif src.exists():
                zf.write(src, src.relative_to(_ws()))
    return {"ok": True, "path": archive_name, "size": dest.stat().st_size, "download_url": _dl(archive_name)}


# --------------------------------------------------------------- терминал
def run_shell(command: str, timeout: int = 90) -> Dict[str, Any]:
    """Выполнить команду в песочнице (рабочая папка ~/JARVIS/workspace)."""
    try:
        proc = subprocess.run(command, shell=True, cwd=str(_ws()), capture_output=True,
                              text=True, timeout=timeout)
        return {"ok": proc.returncode == 0, "code": proc.returncode,
                "stdout": (proc.stdout or "")[-8000:], "stderr": (proc.stderr or "")[-4000:],
                "command": command}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "таймаут %ds" % timeout, "command": command}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "command": command}


def run_python(code: str, timeout: int = 90) -> Dict[str, Any]:
    """Выполнить python-код в песочнице (анализ данных, расчёты, генерация файлов)."""
    script = _ws() / ("_run_%d.py" % int(time.time() * 1000))
    script.write_text(code, "utf-8")
    try:
        proc = subprocess.run([sys.executable, str(script)], cwd=str(_ws()),
                              capture_output=True, text=True, timeout=timeout)
        return {"ok": proc.returncode == 0, "stdout": (proc.stdout or "")[-8000:],
                "stderr": (proc.stderr or "")[-4000:], "code": proc.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "таймаут %ds" % timeout}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            script.unlink()
        except Exception:
            pass


# --------------------------------------------------------- computer-use
def _osa(script: str) -> Dict[str, Any]:
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
        return {"ok": proc.returncode == 0, "out": (proc.stdout or "").strip(),
                "err": (proc.stderr or "").strip()}
    except Exception as exc:
        return {"ok": False, "err": str(exc)}


def _jxa(script: str, timeout: int = 30) -> Dict[str, Any]:
    """JavaScript for Automation. Даёт доступ к CoreGraphics без установки пакетов."""
    try:
        proc = subprocess.run(["osascript", "-l", "JavaScript", "-e", script],
                              capture_output=True, text=True, timeout=timeout)
        return {"ok": proc.returncode == 0, "out": (proc.stdout or "").strip(),
                "err": (proc.stderr or "").strip()}
    except Exception as exc:
        return {"ok": False, "err": str(exc)}


_JXA_PRELUDE = (
    "ObjC.import('CoreGraphics');ObjC.import('Foundation');"
    "function pt(x,y){return {x:x,y:y};}"
)


def _png_size(data: bytes) -> tuple:
    """Ширина и высота PNG из заголовка IHDR — без сторонних библиотек."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
    except Exception:
        pass
    return (0, 0)


def screenshot(scale: float = 0.4) -> Dict[str, Any]:
    """Снимок экрана. Возвращает data-url (для vision-модели) и файл в песочнице."""
    out = _ws() / ("screen_%d.png" % int(time.time()))
    try:
        if IS_MAC:
            subprocess.run(["screencapture", "-x", "-C", str(out)], timeout=25, check=True)
        elif IS_WIN:
            ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                  "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
                  "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                  "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                  "$g.CopyFromScreen($b.X,$b.Y,0,0,$bmp.Size);"
                  "$bmp.Save('%s')" % str(out).replace("\\", "\\\\"))
            subprocess.run(["powershell", "-Command", ps], timeout=30, check=True)
        else:
            for cmd in (["import", "-window", "root", str(out)],
                        ["gnome-screenshot", "-f", str(out)],
                        ["scrot", str(out)]):
                try:
                    subprocess.run(cmd, timeout=25, check=True)
                    break
                except Exception:
                    continue
        if not out.exists():
            return {"ok": False, "error": "не удалось сделать снимок экрана"}
        data = out.read_bytes()
        full = _png_size(data)          # реальный размер экрана в пикселях снимка
        # уменьшаем размер данных для vision-модели, если доступен sips (macOS)
        if IS_MAC and scale and scale < 1:
            try:
                subprocess.run(["sips", "-Z", str(int(1400 * scale)), str(out)],
                               capture_output=True, timeout=20)
                data = out.read_bytes()
            except Exception:
                pass
        small = _png_size(data)
        b64 = base64.b64encode(data).decode()
        rel = out.name
        # во сколько раз уменьшили: нужно, чтобы пересчитать координаты клика
        factor = (full[0] / small[0]) if (full[0] and small[0]) else 1.0
        return {"ok": True, "path": rel, "download_url": _dl(rel),
                "data_url": "data:image/png;base64," + b64, "bytes": len(data),
                "width": small[0], "height": small[1],
                "screen_width": full[0], "screen_height": full[1],
                "scale": round(1.0 / factor, 6) if factor else 1.0}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def mouse_move(x: int, y: int) -> Dict[str, Any]:
    """Плавно переместить курсор в точку экрана (macOS, без сторонних пакетов)."""
    if not IS_MAC:
        return {"ok": False, "error": "перемещение курсора поддерживается на macOS"}
    script = _JXA_PRELUDE + (
        "var target=$.CGPointMake(%d,%d);"
        "var cur=$.CGEventGetLocation($.CGEventCreate($()));"
        "var steps=18;"
        "for(var i=1;i<=steps;i++){"
        "  var p=$.CGPointMake(cur.x+(target.x-cur.x)*i/steps, cur.y+(target.y-cur.y)*i/steps);"
        "  var e=$.CGEventCreateMouseEvent($(), 5, p, 0);"
        "  $.CGEventPost(0, e);"
        "  $.NSThread.sleepForTimeInterval(0.012);"
        "}'ok'"
    ) % (x, y)
    res = _jxa(script)
    if res.get("ok"):
        return {"ok": True, "x": x, "y": y}
    # запасной путь — Quartz из Python, если вдруг установлен pyobjc
    code = ("import Quartz\n"
            "e=Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (%d,%d), 0)\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)\n" % (x, y))
    alt = run_python_system(code)
    return {"ok": alt.get("ok", False), "x": x, "y": y,
            "error": (res.get("err") or alt.get("stderr", ""))[:300]}


def run_python_system(code: str, timeout: int = 30) -> Dict[str, Any]:
    """Python вне песочницы — только для computer-use (Quartz и т.п.)."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as fh:
        fh.write(code)
        path = fh.name
    try:
        proc = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=timeout)
        return {"ok": proc.returncode == 0, "stdout": proc.stdout[-3000:], "stderr": proc.stderr[-3000:]}
    except Exception as exc:
        return {"ok": False, "stderr": str(exc)}
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


def mouse_click(x: int = -1, y: int = -1, button: str = "left", double: bool = False) -> Dict[str, Any]:
    """Клик мышью по координатам экрана (macOS, без сторонних пакетов)."""
    if not IS_MAC:
        return {"ok": False, "error": "computer-use сейчас поддержан для macOS"}
    down, up = (1, 2) if button == "left" else (3, 4)
    btn = 0 if button == "left" else 1
    clicks = 2 if double else 1
    script = _JXA_PRELUDE + (
        "var x=%d, y=%d;"
        "var cur=$.CGEventGetLocation($.CGEventCreate($()));"
        "var p=(x<0||y<0)?cur:$.CGPointMake(x,y);"
        "var mv=$.CGEventCreateMouseEvent($(), 5, p, 0);"
        "$.CGEventPost(0, mv);"
        "$.NSThread.sleepForTimeInterval(0.08);"
        "for(var i=1;i<=%d;i++){"
        "  var d=$.CGEventCreateMouseEvent($(), %d, p, %d);"
        "  $.CGEventSetIntegerValueField(d, 1, i);"
        "  $.CGEventPost(0, d);"
        "  $.NSThread.sleepForTimeInterval(0.05);"
        "  var u=$.CGEventCreateMouseEvent($(), %d, p, %d);"
        "  $.CGEventSetIntegerValueField(u, 1, i);"
        "  $.CGEventPost(0, u);"
        "  $.NSThread.sleepForTimeInterval(0.08);"
        "}'ok'"
    ) % (x, y, clicks, down, btn, up, btn)
    res = _jxa(script)
    if res.get("ok"):
        return {"ok": True, "x": x, "y": y, "button": button, "double": double}
    # запасной путь — System Events (нужны права «Универсальный доступ»)
    if x >= 0 and y >= 0:
        fallback = _osa('tell application "System Events" to click at {%d, %d}' % (x, y))
        if fallback.get("ok"):
            return {"ok": True, "x": x, "y": y, "button": button, "via": "System Events"}
    return {"ok": False, "x": x, "y": y,
            "error": (res.get("err") or "не удалось выполнить клик")[:300]}


def mouse_scroll(amount: int = -3, horizontal: int = 0) -> Dict[str, Any]:
    """Прокрутка колесом мыши: отрицательное значение — вниз."""
    if not IS_MAC:
        return {"ok": False, "error": "computer-use сейчас поддержан для macOS"}
    script = _JXA_PRELUDE + (
        "var e=$.CGEventCreateScrollWheelEvent($(), 0, 2, %d, %d);"
        "$.CGEventPost(0, e);'ok'" % (int(amount), int(horizontal))
    )
    res = _jxa(script)
    return {"ok": res.get("ok", False), "amount": amount, "error": res.get("err", "")[:200]}


def mouse_drag(x1: int, y1: int, x2: int, y2: int) -> Dict[str, Any]:
    """Перетащить мышью из точки в точку."""
    if not IS_MAC:
        return {"ok": False, "error": "computer-use сейчас поддержан для macOS"}
    script = _JXA_PRELUDE + (
        "var a=$.CGPointMake(%d,%d), b=$.CGPointMake(%d,%d);"
        "$.CGEventPost(0, $.CGEventCreateMouseEvent($(), 5, a, 0));"
        "$.NSThread.sleepForTimeInterval(0.08);"
        "$.CGEventPost(0, $.CGEventCreateMouseEvent($(), 1, a, 0));"
        "$.NSThread.sleepForTimeInterval(0.1);"
        "for(var i=1;i<=20;i++){"
        "  var p=$.CGPointMake(a.x+(b.x-a.x)*i/20, a.y+(b.y-a.y)*i/20);"
        "  $.CGEventPost(0, $.CGEventCreateMouseEvent($(), 6, p, 0));"
        "  $.NSThread.sleepForTimeInterval(0.015);"
        "}"
        "$.CGEventPost(0, $.CGEventCreateMouseEvent($(), 2, b, 0));'ok'"
    ) % (x1, y1, x2, y2)
    res = _jxa(script)
    return {"ok": res.get("ok", False), "from": [x1, y1], "to": [x2, y2],
            "error": res.get("err", "")[:200]}


def type_text(text: str) -> Dict[str, Any]:
    """Напечатать текст в активном окне."""
    if IS_MAC:
        safe = text.replace("\\", "\\\\").replace('"', '\\"')
        res = _osa('tell application "System Events" to keystroke "%s"' % safe)
        return {"ok": res.get("ok", False), "typed": text[:120], "error": res.get("err", "")}
    return {"ok": False, "error": "поддержано для macOS"}


_KEY_CODES = {
    "enter": 36, "return": 36, "tab": 48, "space": 49, "delete": 51, "escape": 53,
    "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126, "backspace": 51,
}


def press_key(key: str, modifiers: str = "") -> Dict[str, Any]:
    """Нажать клавишу. modifiers: command,shift,option,control через запятую."""
    if not IS_MAC:
        return {"ok": False, "error": "поддержано для macOS"}
    mods = [m.strip() for m in modifiers.split(",") if m.strip()]
    mod_str = (" using {" + ", ".join("%s down" % m for m in mods) + "}") if mods else ""
    key_l = key.lower().strip()
    if key_l in _KEY_CODES:
        script = 'tell application "System Events" to key code %d%s' % (_KEY_CODES[key_l], mod_str)
    else:
        script = 'tell application "System Events" to keystroke "%s"%s' % (key_l[:1] if len(key_l) == 1 else key_l, mod_str)
    res = _osa(script)
    return {"ok": res.get("ok", False), "key": key, "modifiers": modifiers, "error": res.get("err", "")}


def open_app(name: str) -> Dict[str, Any]:
    """Открыть приложение или URL на компьютере."""
    try:
        if IS_MAC:
            if name.startswith("http"):
                subprocess.run(["open", name], timeout=20, check=True)
            else:
                subprocess.run(["open", "-a", name], timeout=20, check=True)
        elif IS_WIN:
            os.startfile(name)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", name], timeout=20, check=True)
        return {"ok": True, "opened": name}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def screen_info() -> Dict[str, Any]:
    """Размер экрана и текущая позиция курсора."""
    if IS_MAC:
        res = _jxa(_JXA_PRELUDE +
                   "var d=$.CGDisplayBounds($.CGMainDisplayID());"
                   "var c=$.CGEventGetLocation($.CGEventCreate($()));"
                   "[Math.round(d.size.width),Math.round(d.size.height),"
                   "Math.round(c.x),Math.round(c.y)].join(' ')")
        if res.get("ok") and res.get("out"):
            try:
                w, h, cx, cy = res["out"].split()
                return {"ok": True, "width": int(w), "height": int(h),
                        "cursor": [int(cx), int(cy)], "os": "macOS"}
            except Exception:
                pass
        alt = _osa('tell application "Finder" to get bounds of window of desktop')
        if alt.get("ok") and alt.get("out"):
            try:
                parts = [int(v.strip()) for v in alt["out"].split(",")]
                return {"ok": True, "width": parts[2], "height": parts[3], "os": "macOS"}
            except Exception:
                pass
    return {"ok": True, "width": 0, "height": 0, "os": platform.system()}


def system_info() -> Dict[str, Any]:
    return {
        "ok": True,
        "os": platform.system(),
        "release": platform.release(),
        "python": sys.version.split()[0],
        "workspace": str(_ws()),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# --------------------------------------------------- управление песочницей
def sandbox_info() -> Dict[str, Any]:
    """Что сейчас лежит в песочнице этого диалога."""
    data = sandbox.info()
    data["files_list"] = [f["name"] for f in sandbox.listing()][:80]
    return data


def sandbox_clear(confirm: str = "") -> Dict[str, Any]:
    """Полностью стереть песочницу текущего диалога."""
    before = len(sandbox.listing())
    res = sandbox.wipe()
    res["before"] = before
    res["message"] = "Песочница «%s» очищена: удалено объектов — %d." % (res.get("name", ""), res.get("removed", 0))
    return res


def sandbox_rename(name: str) -> Dict[str, Any]:
    """Переименовать песочницу этого диалога."""
    return sandbox.rename(name)

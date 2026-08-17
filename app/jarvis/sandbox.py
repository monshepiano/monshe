"""Песочницы JARVIS — по одной на диалог.

У каждого диалога свой каталог внутри ~/JARVIS/workspace/chats/<chat_id>.
Пока диалога нет (фоновая задача, вызов инструмента напрямую) используется
общая песочница ~/JARVIS/workspace/common.

Текущая песочница передаётся через contextvars: сервер выставляет её на время
обработки запроса, а инструменты просто спрашивают root().
"""
from __future__ import annotations

import contextvars
import json
import re
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import WORKSPACE

_CURRENT: contextvars.ContextVar[str] = contextvars.ContextVar("jarvis_chat_id", default="")

COMMON = "common"
_NAMES = WORKSPACE / "chats" / "_names.json"


def set_chat(chat_id: str) -> None:
    """Привязать текущий поток обработки к песочнице диалога."""
    _CURRENT.set(_clean_id(chat_id or ""))


def current_chat() -> str:
    return _CURRENT.get() or ""


def _clean_id(chat_id: str) -> str:
    return re.sub(r"[^\w\-]+", "", str(chat_id or ""))[:64]


def root(chat_id: Optional[str] = None) -> Path:
    """Каталог песочницы: у диалога — свой, иначе общий."""
    cid = _clean_id(chat_id if chat_id is not None else current_chat())
    path = (WORKSPACE / "chats" / cid) if cid else (WORKSPACE / COMMON)
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_path(name: str, chat_id: Optional[str] = None) -> Path:
    """Не выпускаем агента за пределы песочницы."""
    base = root(chat_id)
    clean = (name or "").strip().lstrip("/")
    path = (base / clean).resolve()
    if not str(path).startswith(str(base.resolve())):
        raise ValueError("Путь вне песочницы: " + str(name))
    return path


def dl(name: str, chat_id: Optional[str] = None) -> str:
    """Ссылка на скачивание файла именно из этой песочницы."""
    cid = _clean_id(chat_id if chat_id is not None else current_chat())
    url = "/api/files/download?name=" + urllib.parse.quote(name)
    if cid:
        url += "&chat=" + urllib.parse.quote(cid)
    return url


# ------------------------------------------------------------------ имена
def _load_names() -> Dict[str, str]:
    try:
        return json.loads(_NAMES.read_text("utf-8"))
    except Exception:
        return {}


def _save_names(data: Dict[str, str]) -> None:
    try:
        _NAMES.parent.mkdir(parents=True, exist_ok=True)
        _NAMES.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    except Exception:
        pass


def name_of(chat_id: Optional[str] = None) -> str:
    cid = _clean_id(chat_id if chat_id is not None else current_chat())
    if not cid:
        return "Общая песочница"
    return _load_names().get(cid) or "Песочница диалога"


def rename(new_name: str, chat_id: Optional[str] = None) -> Dict[str, Any]:
    cid = _clean_id(chat_id if chat_id is not None else current_chat())
    title = (new_name or "").strip()[:60]
    if not cid:
        return {"ok": False, "error": "общую песочницу переименовать нельзя"}
    if not title:
        return {"ok": False, "error": "пустое имя"}
    data = _load_names()
    data[cid] = title
    _save_names(data)
    return {"ok": True, "name": title}


# ------------------------------------------------------------------ файлы
def listing(chat_id: Optional[str] = None, limit: int = 400) -> List[Dict[str, Any]]:
    base = root(chat_id)
    out: List[Dict[str, Any]] = []
    for item in sorted(base.rglob("*"))[:limit]:
        if item.is_file():
            rel = str(item.relative_to(base))
            out.append({
                "name": rel,
                "size": item.stat().st_size,
                "modified": item.stat().st_mtime,
                "download_url": dl(rel, chat_id),
            })
    return out


def size_of(chat_id: Optional[str] = None) -> int:
    total = 0
    for item in root(chat_id).rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except Exception:
                pass
    return total


def info(chat_id: Optional[str] = None) -> Dict[str, Any]:
    cid = _clean_id(chat_id if chat_id is not None else current_chat())
    files = listing(chat_id)
    return {
        "ok": True,
        "chat_id": cid,
        "name": name_of(chat_id),
        "path": str(root(chat_id)),
        "files": len(files),
        "size": size_of(chat_id),
        "shared": not cid,
    }


def wipe(chat_id: Optional[str] = None) -> Dict[str, Any]:
    """Полностью очистить песочницу (сам каталог остаётся)."""
    base = root(chat_id)
    removed = 0
    for item in list(base.iterdir()):
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            removed += 1
        except Exception:
            pass
    return {"ok": True, "removed": removed, "name": name_of(chat_id)}


def drop(chat_id: str) -> None:
    """Удалить песочницу вместе с каталогом (при удалении диалога)."""
    cid = _clean_id(chat_id)
    if not cid:
        return
    try:
        shutil.rmtree(WORKSPACE / "chats" / cid, ignore_errors=True)
    except Exception:
        pass
    data = _load_names()
    if cid in data:
        data.pop(cid, None)
        _save_names(data)


TEXT_EXT = (".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".css",
            ".xml", ".yml", ".yaml", ".log", ".sh", ".ini", ".conf", ".sql", ".toml")


def view(name: str, chat_id: Optional[str] = None, limit: int = 40000) -> Dict[str, Any]:
    """Прочитать файл для просмотра в терминале."""
    try:
        src = safe_path(name, chat_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not src.exists() or not src.is_file():
        return {"ok": False, "error": "файл не найден"}
    stat = src.stat()
    head = {"ok": True, "name": name, "size": stat.st_size,
            "modified": stat.st_mtime, "download_url": dl(name, chat_id)}
    low = name.lower()
    if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
        head.update({"kind": "image", "content": ""})
        return head
    if not low.endswith(TEXT_EXT) and stat.st_size > 2_000_000:
        head.update({"kind": "binary", "content": ""})
        return head
    try:
        head.update({"kind": "text", "content": src.read_text("utf-8")[:limit]})
    except Exception:
        head.update({"kind": "binary", "content": ""})
    return head


def stamp() -> str:
    return time.strftime("%H:%M:%S")

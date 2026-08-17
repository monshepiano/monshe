"""Песочница: файлы и команды. Всё строго внутри ~/.jarvis/workspace."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Dict, List

from .. import config

MAX_READ = 60000


def root() -> Path:
    config.ensure_dirs()
    return config.WORKSPACE


def _safe(rel: str) -> Path:
    p = (root() / rel.lstrip("/")).resolve()
    if not str(p).startswith(str(root().resolve())):
        raise ValueError("Выход за пределы песочницы запрещён")
    return p


def write_file(path: str, content: str) -> Dict[str, Any]:
    p = _safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, "utf-8")
    return {"ok": True, "path": str(p.relative_to(root())), "bytes": len(content.encode())}


def read_file(path: str) -> Dict[str, Any]:
    p = _safe(path)
    if not p.exists():
        return {"ok": False, "error": "Файл не найден"}
    try:
        return {"ok": True, "path": path, "content": p.read_text("utf-8")[:MAX_READ]}
    except UnicodeDecodeError:
        return {"ok": True, "path": path, "content": f"<бинарный файл, {p.stat().st_size} байт>"}


def list_files(path: str = ".") -> Dict[str, Any]:
    p = _safe(path)
    if not p.exists():
        return {"ok": True, "files": []}
    items = []
    for f in sorted(p.rglob("*"))[:400]:
        if f.is_file():
            items.append({"path": str(f.relative_to(root())), "size": f.stat().st_size,
                          "mtime": f.stat().st_mtime})
    return {"ok": True, "files": items}


def delete_file(path: str) -> Dict[str, Any]:
    p = _safe(path)
    if p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()
    return {"ok": True, "deleted": path}


def make_zip(paths: List[str], name: str = "bundle.zip") -> Dict[str, Any]:
    out = _safe(name)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in paths:
            p = _safe(rel)
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        z.write(f, str(f.relative_to(root())))
            elif p.exists():
                z.write(p, str(p.relative_to(root())))
    return {"ok": True, "path": name, "size": out.stat().st_size}


async def run(command: str, timeout: int = 90) -> Dict[str, Any]:
    """Команда в песочнице. Опасные вызовы отсекает слой подтверждений."""
    if not config.get("allow_shell"):
        return {"ok": False, "error": "Команды отключены в настройках"}
    env = dict(os.environ, HOME=str(root()), PYTHONUNBUFFERED="1")
    try:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=str(root()), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "error": f"Превышено время ({timeout} c)"}
        text = (out or b"").decode("utf-8", "replace")
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
                "output": text[-8000:]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def run_python(code: str, timeout: int = 90) -> Dict[str, Any]:
    p = _safe("_snippet.py")
    p.write_text(code, "utf-8")
    res = await run(f"python3 _snippet.py", timeout=timeout)
    return res

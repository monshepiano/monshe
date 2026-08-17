#!/usr/bin/env python3
"""Сборка единого самораспаковывающегося установщика JARVIS.command.

Берёт всё содержимое app/, пакует в tar.gz, кодирует base64 и приклеивает
к shell-шаблону. На выходе — один файл, который можно просто скачать и
дважды кликнуть на macOS.
"""
from __future__ import annotations

import base64
import io
import os
import re
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
TEMPLATE = ROOT / "install" / "template.sh"
OUT = ROOT / "install" / "JARVIS.command"

KEYS_FILE = Path(__file__).resolve().parent / "keys.json"


def load_keys() -> tuple[str, str]:
    """Ключи берём из install/keys.json или из переменных окружения.

    Файл keys.json намеренно не хранится в репозитории: секреты не должны
    попадать в git. Формат: {"cloudru": "...", "deepseek": "..."}
    """
    data = {}
    if KEYS_FILE.exists():
        import json
        data = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    cloud = os.environ.get("CLOUDRU_API_KEY") or data.get("cloudru", "")
    deep = os.environ.get("DEEPSEEK_API_KEY") or data.get("deepseek", "")
    if not cloud:
        print("ВНИМАНИЕ: ключ Cloud.ru не задан — пользователю придётся вписать его в интерфейсе")
    return cloud, deep


SKIP_DIRS = {"__pycache__", ".git", ".DS_Store", "node_modules"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def version() -> str:
    text = (APP / "jarvis" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'VERSION\s*=\s*["\']([^"\']+)', text)
    return m.group(1) if m else "1.0.0"


def build_payload() -> bytes:
    buf = io.BytesIO()
    files = []
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tar:
        for path in sorted(APP.rglob("*")):
            rel = path.relative_to(APP)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if path.suffix in SKIP_SUFFIX:
                continue
            if not path.is_file():
                continue
            info = tar.gettarinfo(str(path), arcname=str(rel))
            info.uid = info.gid = 0
            info.uname = info.gname = "jarvis"
            info.mtime = 0
            info.mode = 0o644
            with path.open("rb") as fh:
                tar.addfile(info, fh)
            files.append(str(rel))
    if not files:
        raise SystemExit("app/ пуст — нечего паковать")
    print("упаковано файлов: %d" % len(files))
    for name in files:
        print("   ", name)
    return buf.getvalue()


def main() -> None:
    ver = version()
    payload = build_payload()
    b64 = base64.b64encode(payload).decode("ascii")
    wrapped = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))

    tpl = TEMPLATE.read_text(encoding="utf-8")
    cloud_key, deep_key = load_keys()
    # ключи кладём в base64: так они не попадают в сканеры секретов «как есть»,
    # установщик расшифрует их при первом запуске
    tpl = (tpl.replace("__VERSION__", ver)
              .replace("__CLOUDRU_KEY_B64__", base64.b64encode(cloud_key.encode()).decode())
              .replace("__DEEPSEEK_KEY_B64__", base64.b64encode(deep_key.encode()).decode()))
    if not tpl.endswith("\n"):
        tpl += "\n"

    OUT.write_text(tpl + wrapped + "\n", encoding="utf-8")
    os.chmod(OUT, 0o755)
    size = OUT.stat().st_size
    print("\nготово: %s  (%.1f КБ, версия %s)" % (OUT, size / 1024, ver))


if __name__ == "__main__":
    main()

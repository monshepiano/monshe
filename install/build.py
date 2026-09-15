#!/usr/bin/env python3
"""Сборка единого самораспаковывающегося установщика JARVIS.command.

Берёт всё содержимое app/, пакует в tar.gz, кодирует base64 и приклеивает
к shell-шаблону. На выходе — один файл, который можно просто скачать и
дважды кликнуть на macOS.
"""
from __future__ import annotations

import base64
import gzip
import io
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
TEMPLATE = ROOT / "install" / "template.sh"
OUT = ROOT / "install" / "JARVIS.command"
BUNDLE = ROOT / "JARVIS.zip"
QUICKSTART = ROOT / "install" / "ЧИТАЙ-МЕНЯ.txt"

KEYS_FILE = Path(__file__).resolve().parent / "keys.json"


def _keys_from_previous_installer() -> tuple[str, str]:
    """Сохранить уже встроенные ключи при пересборке, не печатая их в лог.

    ``JARVIS.command`` одновременно является доставляемым updater-файлом. До
    этой проверки локальная пересборка без ``keys.json`` молча заменяла рабочий
    updater версией без доступа к моделям. Берём значения только из точного
    аргумента setup-блока ранее собранного файла и строго проверяем base64.
    """
    if not OUT.exists():
        return "", ""
    try:
        old = OUT.read_text(encoding="utf-8")
        found = re.search(
            r'"\$PY"\s+-\s+"\$HOME_DIR"\s+"([A-Za-z0-9+/=]*)"\s+'
            r'"([A-Za-z0-9+/=]*)"\s+<<\'PYSETUP\'', old)
        if not found:
            return "", ""
        values = []
        for encoded in found.groups():
            raw = base64.b64decode(encoded, validate=True).decode("utf-8") if encoded else ""
            values.append(raw)
        return values[0], values[1]
    except (OSError, UnicodeError, ValueError):
        return "", ""


def load_keys() -> tuple[str, str]:
    """Ключи: environment/ignored keys.json, затем рабочий старый updater.

    Файл keys.json намеренно не хранится в репозитории. Формат:
    {"cloudru": "...", "deepseek": "..."}.
    """
    data = {}
    if KEYS_FILE.exists():
        import json
        data = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    previous_cloud, previous_deep = _keys_from_previous_installer()
    cloud = (os.environ.get("CLOUDRU_API_KEY") or data.get("cloudru", "")
             or previous_cloud)
    deep = (os.environ.get("DEEPSEEK_API_KEY") or data.get("deepseek", "")
            or previous_deep)
    if previous_cloud and not os.environ.get("CLOUDRU_API_KEY") and not data.get("cloudru"):
        print("ключи: сохранены из предыдущего updater-файла")
    if not cloud:
        print("ВНИМАНИЕ: ключ Cloud.ru не задан — пользователю придётся вписать его в интерфейсе")
    return cloud, deep


SKIP_DIRS = {"__pycache__", ".git", ".DS_Store", "node_modules"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def version() -> str:
    text = (APP / "jarvis" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'(?:__version__|VERSION)\s*=\s*["\']([^"\']+)', text)
    return m.group(1) if m else "1.0.0"


def build_payload() -> bytes:
    # tarfile ``w:gz`` записывает текущее время в gzip-header, из-за чего два
    # билда одного source tree имели разные hashes. Сначала строим
    # нормализованный tar, затем gzip с mtime=0 — release становится
    # воспроизводимым байт-в-байт.
    buf = io.BytesIO()
    files = []
    with tarfile.open(fileobj=buf, mode="w") as tar:
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
    return gzip.compress(buf.getvalue(), compresslevel=9, mtime=0)


def build_bundle() -> None:
    """Собрать полный ZIP: кликабельный updater + русская инструкция.

    ZIP metadata тоже фиксирована: GitHub artifact и его hash меняются только
    когда меняется реальное содержимое, а не время локальной пересборки.
    """
    entries = ((OUT, "JARVIS.command", 0o755),
               (QUICKSTART, "ЧИТАЙ-МЕНЯ.txt", 0o644))
    with zipfile.ZipFile(BUNDLE, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as archive:
        for source, name, mode in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (mode & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED,
                             compresslevel=9)


def main() -> None:
    ver = version()
    payload = build_payload()
    b64 = base64.b64encode(payload).decode("ascii")
    wrapped = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))

    tpl = TEMPLATE.read_text(encoding="utf-8")
    cloud_key, deep_key = load_keys()
    # base64 здесь лишь формат передачи через shell-аргумент, а не защита:
    # установщик расшифрует значения при первом запуске.
    tpl = (tpl.replace("__VERSION__", ver)
              .replace("__CLOUDRU_KEY_B64__", base64.b64encode(cloud_key.encode()).decode())
              .replace("__DEEPSEEK_KEY_B64__", base64.b64encode(deep_key.encode()).decode()))
    if not tpl.endswith("\n"):
        tpl += "\n"

    OUT.write_text(tpl + wrapped + "\n", encoding="utf-8")
    os.chmod(OUT, 0o755)
    size = OUT.stat().st_size
    build_bundle()
    bundle_size = BUNDLE.stat().st_size
    print("\nготово: %s  (%.1f КБ, версия %s)" % (OUT, size / 1024, ver))
    print("полный ZIP: %s  (%.1f КБ)" % (BUNDLE, bundle_size / 1024))


if __name__ == "__main__":
    main()

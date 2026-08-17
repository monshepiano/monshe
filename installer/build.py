#!/usr/bin/env python3
"""Pack the whole app into a single JARVIS.command and zip it alone."""

from __future__ import annotations

import base64
import io
import os
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "jarvis"
DIST = ROOT / "dist"
OUT_CMD = DIST / "JARVIS.command"
OUT_ZIP = DIST / "JARVIS.zip"

SKIP_DIR = {".git", "__pycache__", "data", ".venv", "venv"}
SKIP_FILE = {".DS_Store"}


def pack_app() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in APP.rglob("*"):
            if any(p in SKIP_DIR for p in path.parts):
                continue
            if path.name in SKIP_FILE:
                continue
            if path.is_file():
                tar.add(path, arcname=str(path.relative_to(APP)))
    return buf.getvalue()


HEADER = r'''#!/bin/bash
# JARVIS — установщик / открыватор / обновлятор
# Двойной клик. Больше ничего нажимать не нужно.
set -e
DEST="$HOME/Jarvis"
APP="$DEST/app"
VER_NEW="__VERSION__"
mkdir -p "$DEST"

say() { printf "\n  \033[36mJARVIS\033[0m  %s\n" "$1"; }

find_python() {
  for c in python3 python3.12 python3.11 python3.10 python; do
    if command -v "$c" >/dev/null 2>&1; then
      v=$("$c" -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)
      if [ "$v" -ge 309 ]; then echo "$c"; return 0; fi
    fi
  done
  return 1
}

install_python_mac() {
  say "Python не найден — ставлю официальный пакет…"
  PKG="/tmp/jarvis-python.pkg"
  if command -v brew >/dev/null 2>&1; then
    brew install python@3.12 || brew install python
    return 0
  fi
  curl -fsSL -o "$PKG" "https://www.python.org/ftp/python/3.12.8/python-3.12.8-macos11.pkg" || true
  if [ -f "$PKG" ]; then
    osascript -e 'do shell script "installer -pkg /tmp/jarvis-python.pkg -target /" with administrator privileges' || sudo installer -pkg "$PKG" -target /
  fi
}

extract_payload() {
  say "Распаковываю контур $VER_NEW → $APP"
  rm -rf "$APP"
  mkdir -p "$APP"
  # payload is base64 after the marker
  LINE=$(awk '/^__JARVIS_PAYLOAD__$/{print NR+1; exit}' "$0")
  tail -n +"$LINE" "$0" | base64 -d | tar -xzf - -C "$APP"
  echo "$VER_NEW" > "$DEST/VERSION"
}

need_update() {
  [ ! -f "$APP/start.py" ] && return 0
  [ ! -f "$DEST/VERSION" ] && return 0
  OLD=$(cat "$DEST/VERSION" 2>/dev/null || echo 0)
  [ "$OLD" != "$VER_NEW" ] && return 0
  return 1
}

open_ui() {
  PY=$(find_python || true)
  if [ -z "$PY" ]; then
    if [ "$(uname)" = "Darwin" ]; then install_python_mac; fi
    PY=$(find_python || true)
  fi
  if [ -z "$PY" ]; then
    say "Не нашёл Python 3.9+. Установите python.org и снова откройте этот файл."
    if [ -t 0 ]; then read -r _; fi
    exit 1
  fi
  say "Запускаю на http://127.0.0.1:8787  ($PY)"
  cd "$APP"
  export JARVIS_NO_BROWSER=0
  exec "$PY" "$APP/start.py"
}

# already running? just open
if curl -fsS -m 1 http://127.0.0.1:8787/ >/dev/null 2>&1; then
  say "Уже онлайн — открываю окно"
  if command -v open >/dev/null 2>&1; then open "http://127.0.0.1:8787"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "http://127.0.0.1:8787"
  fi
  exit 0
fi

if need_update; then
  extract_payload
  # helper alias for next times
  cp "$0" "$DEST/JARVIS.command" 2>/dev/null || true
  chmod +x "$DEST/JARVIS.command" "$0" || true
  if [ "$(uname)" = "Darwin" ]; then
    osascript <<OSA 2>/dev/null || true
tell application "Finder"
  try
    make alias file to POSIX file "$DEST/JARVIS.command" at desktop
  end try
end tell
OSA
  fi
  say "Готово. Дальше просто открывайте этот же файл."
fi

open_ui
exit 0
__JARVIS_PAYLOAD__
'''


def main():
    DIST.mkdir(exist_ok=True)
    version = (APP / "VERSION").read_text().strip()
    blob = pack_app()
    b64 = base64.b64encode(blob).decode("ascii")
    # wrap
    wrapped = "\n".join(b64[i : i + 76] for i in range(0, len(b64), 76))
    text = HEADER.replace("__VERSION__", version) + wrapped + "\n"
    OUT_CMD.write_text(text, encoding="utf-8")
    os.chmod(OUT_CMD, 0o755)

    if OUT_ZIP.exists():
        OUT_ZIP.unlink()
    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(OUT_CMD, arcname="JARVIS.command")
    print(f"wrote {OUT_CMD} ({OUT_CMD.stat().st_size} bytes)")
    print(f"wrote {OUT_ZIP} ({OUT_ZIP.stat().st_size} bytes)  files={z.namelist() if False else ['JARVIS.command']}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Запуск Джарвиса.

Читает порт из ~/.jarvis/config.json (если файла нет — создаётся с настройками
по умолчанию), поднимает сервер и открывает интерфейс в браузере.

Запускать этот файл руками не обязательно: двойного клика по «Джарвис.command»
(macOS) или «Джарвис.bat» (Windows) достаточно.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

from app import config  # noqa: E402


def _open_browser(url: str) -> None:
    time.sleep(1.6)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> None:
    config.ensure_dirs()
    host = os.environ.get("HOST") or config.get("host", "0.0.0.0")
    port = int(os.environ.get("PORT") or config.get("port", 8765))
    ui = ROOT / "frontend" / "dist" / "index.html"

    print("\n\033[96m╔══════════════════════════════════════════════╗")
    print("║            J . A . R . V . I . S .           ║")
    print("╚══════════════════════════════════════════════╝\033[0m")
    if not ui.exists():
        print("\033[93m⚠ Интерфейс не собран. Запустите установщик «Джарвис.command».\033[0m")
    print(f"  Интерфейс:  \033[96mhttp://localhost:{port}\033[0m")
    print(f"  С телефона: http://<ip-этого-компьютера>:{port}")
    print("  Данные:     " + str(config.HOME))
    print("  Остановить: Ctrl+C\n")

    if os.environ.get("JARVIS_NO_BROWSER") != "1":
        threading.Thread(target=_open_browser, args=(f"http://localhost:{port}",),
                         daemon=True).start()

    import uvicorn
    uvicorn.run("app.main:app", host=host, port=port, log_level="info",
                app_dir=str(ROOT / "backend"))


if __name__ == "__main__":
    main()

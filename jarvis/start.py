#!/usr/bin/env python3
"""Launch JARVIS. Double-clicked by the installer."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.chdir(ROOT)
os.environ.setdefault("JARVIS_DATA", os.path.join(ROOT, "data"))


def _port_open(host: str, port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def main():
    from core.config import load_settings
    from core.server import serve

    s = load_settings()
    port = int(os.environ.get("JARVIS_PORT") or s.get("port") or 8787)
    host = os.environ.get("JARVIS_HOST") or s.get("host") or "0.0.0.0"

    if _port_open("127.0.0.1", port):
        print(f"JARVIS already running on :{port}", flush=True)
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:
            pass
        return

    def _open():
        time.sleep(0.6)
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:
            pass

    if os.environ.get("JARVIS_NO_BROWSER") != "1":
        threading.Thread(target=_open, daemon=True).start()
    serve(host, port)


if __name__ == "__main__":
    main()

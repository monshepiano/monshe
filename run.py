"""Точка входа: python run.py

Сам находит свободный порт (8000, 8001, …), чтобы не падать, если порт занят
другим приложением. Можно задать явно: PORT=8000 python run.py
"""
import os
import socket

import uvicorn


def pick_port():
    env = os.environ.get("PORT")
    if env:
        return int(env)
    for p in range(8000, 8020):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", p))
                return p
            except OSError:
                continue
    return 8000


if __name__ == "__main__":
    port = pick_port()
    print(f"JARVIS запускается: http://localhost:{port}", flush=True)
    uvicorn.run("server.app:app", host="0.0.0.0", port=port, reload=False)

#!/usr/bin/env bash
# JARVIS — автозапуск (Mac / Linux)
set -e
cd "$(dirname "$0")"

echo "== JARVIS · автозапуск =="

# --- Python ---
PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
command -v "$PY" >/dev/null 2>&1 || {
  echo "[!] Python не найден. Скачайте установщик: https://www.python.org/downloads/"
  echo "    (галочка 'Add Python to PATH') и запустите скрипт ещё раз."
  exit 1
}
"$PY" --version

# --- Скачать/обновить проект ---
NEED=0
if [ ! -d monshe/server ]; then NEED=1; fi
if [ "$NEED" = "0" ] && ! grep -qF 'pick_port' monshe/run.py 2>/dev/null; then NEED=1; fi

if [ "$NEED" = "1" ]; then
  echo "[*] Скачиваю/обновляю проект..."
  [ -f monshe/.env ] && cp monshe/.env .jarvis.env.backup || true
  curl -sL -o jarvis.zip "https://github.com/monshepiano/monshe/archive/refs/heads/arena/01a00f32-monshe.zip"
  rm -rf monshe
  unzip -q jarvis.zip
  mv monshe-arena-01a00f32-monshe monshe
  rm jarvis.zip
  [ -f .jarvis.env.backup ] && mv .jarvis.env.backup monshe/.env || true
fi
cd monshe

# --- Ключ DeepSeek ---
if [ ! -f .env ]; then
  read -r -p "Ключ DeepSeek (sk-...): " k
  echo "DEEPSEEK_API_KEY=$k" > .env
fi

# --- Зависимости ---
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
. .venv/bin/activate
pip install -q -r requirements.txt

# --- Свободный порт ---
PORT=$(for p in 8000 8001 8002 8003 8004; do
  "$PY" -c "import socket; socket.socket().bind(('0.0.0.0',$p))" 2>/dev/null && { echo $p; break; }
done)
PORT=${PORT:-8000}

# --- Запуск: браузер открываем только когда JARVIS реально готов ---
echo "[*] Запускаю JARVIS на порту $PORT ..."
PORT="$PORT" "$PY" run.py > jarvis.log 2>&1 &
SERVER_PID=$!

READY=0
for i in $(seq 1 60); do
  if curl -sf "http://localhost:$PORT/api/health" 2>/dev/null | grep -q '"ok":true'; then
    READY=1
    break
  fi
  sleep 0.5
done

if [ "$READY" = "1" ]; then
  open "http://localhost:$PORT" 2>/dev/null || xdg-open "http://localhost:$PORT" 2>/dev/null || true
  echo
  echo "  [ok] JARVIS запущен → http://localhost:$PORT"
  echo "  Сервер работает, пока открыто это окно (закрыть — Ctrl+C)."
  wait "$SERVER_PID"
else
  echo "[!] JARVIS не поднялся за 30 сек. Лог:"
  tail -20 jarvis.log
  echo "[!] Если выше 'address already in use' — порты 8000-8004 заняты."
  echo "    Закройте приложения на них и запустите ещё раз."
fi

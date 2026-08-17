#!/usr/bin/env bash
# JARVIS — автозапуск (Mac / Linux)
set -e
cd "$(dirname "$0")"

echo "== JARVIS · автозапуск =="

# найти Python (python3 или python)
PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
command -v "$PY" >/dev/null 2>&1 || {
  echo "[!] Python не найден."
  echo "    Скачайте установщик с https://www.python.org/downloads/"
  echo "    (поставьте галочку 'Add Python to PATH') и запустите скрипт ещё раз."
  exit 1
}
"$PY" --version

if [ ! -d monshe/server ]; then
  echo "[*] Скачиваю проект..."
  curl -L -o jarvis.zip "https://github.com/monshepiano/monshe/archive/refs/heads/arena/01a00f32-monshe.zip"
  unzip -q jarvis.zip
  mv monshe-arena-01a00f32-monshe monshe
  rm jarvis.zip
fi
cd monshe

if [ ! -f .env ]; then
  read -r -p "Ключ DeepSeek (sk-...): " k
  echo "DEEPSEEK_API_KEY=$k" > .env
fi

"$PY" -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
(open "http://localhost:8000" 2>/dev/null || xdg-open "http://localhost:8000" 2>/dev/null || true) &
"$PY" run.py


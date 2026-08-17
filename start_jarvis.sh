#!/usr/bin/env bash
# JARVIS — автозапуск (Mac / Linux)
set -e
cd "$(dirname "$0")"

echo "== JARVIS · автозапуск =="

command -v python3 >/dev/null 2>&1 || { echo "[!] Установите Python 3.10+"; exit 1; }

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

python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
(open "http://localhost:8000" 2>/dev/null || xdg-open "http://localhost:8000" 2>/dev/null || true) &
python3 run.py

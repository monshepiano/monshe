#!/bin/bash
# ============================================================
#  J.A.R.V.I.S. — запуск одним кликом
#  Просто дважды щёлкните по этому файлу.
# ============================================================

cd "$(dirname "$0")" || exit 1
clear

CYAN=$'\033[38;5;51m'; GOLD=$'\033[38;5;214m'; DIM=$'\033[2m'; RED=$'\033[31m'; OFF=$'\033[0m'

echo "${CYAN}"
echo "     ╦  ╔═╗ ╦═╗ ╦  ╦ ╦ ╔═╗"
echo "     ║  ╠═╣ ╠╦╝ ╚╗╔╝ ║ ╚═╗"
echo "    ╚╝  ╩ ╩ ╩╚═  ╚╝  ╩ ╚═╝"
echo "${OFF}${DIM}    персональный ИИ-ассистент${OFF}"
echo

# ---------- 1. Ищем Python ----------
PY=""
for c in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "${RED}Не найден Python 3.${OFF}"
  echo "Сейчас macOS предложит установить инструменты разработчика —"
  echo "нажмите «Установить» и подождите. Потом снова запустите этот файл."
  xcode-select --install 2>/dev/null
  echo; read -r -p "Нажмите Enter, чтобы закрыть окно..."
  exit 1
fi

# ---------- 2. Готовим окружение ----------
if [ ! -d ".venv" ]; then
  echo "${GOLD}Первый запуск: устанавливаю компоненты (2–4 минуты)…${OFF}"
  "$PY" -m venv .venv || { echo "${RED}Не удалось создать окружение.${OFF}"; read -r -p "Enter..."; exit 1; }
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt || {
    echo "${RED}Не удалось скачать компоненты. Проверьте интернет.${OFF}"; read -r -p "Enter..."; exit 1; }
  echo "${GOLD}Готово.${OFF}"
elif [ requirements.txt -nt .venv ]; then
  echo "${DIM}Обновляю компоненты…${OFF}"
  .venv/bin/python -m pip install --quiet -r requirements.txt
  touch .venv
fi

# ---------- 3. Порт ----------
PORT="${JARVIS_PORT:-8765}"
while lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; do
  PORT=$((PORT + 1))
done
export JARVIS_PORT="$PORT"

echo
echo "${CYAN}Джарвис запускается…${OFF}"
echo "${DIM}Интерфейс: http://localhost:$PORT${OFF}"
echo "${DIM}Чтобы выключить — закройте это окно или нажмите Ctrl+C.${OFF}"
echo

# ---------- 4. Открываем браузер, когда сервер поднимется ----------
(
  for _ in $(seq 1 60); do
    if curl -s "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
      open "http://localhost:$PORT"
      break
    fi
    sleep 0.5
  done
) &

# ---------- 5. Поехали ----------
exec .venv/bin/python -m jarvis.server

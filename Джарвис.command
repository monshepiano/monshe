#!/bin/bash
# ============================================================================
#  J.A.R.V.I.S. — установщик, обновлятор и запускатор в одном файле (macOS/Linux)
#  Просто дважды кликните по этому файлу. Всё остальное произойдёт само.
# ============================================================================

cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
VENV="$ROOT/.venv"
MARK="$ROOT/.jarvis-installed"

CYAN='\033[96m'; GREEN='\033[92m'; YELLOW='\033[93m'; RED='\033[91m'; OFF='\033[0m'

echo ""
echo -e "${CYAN}  ╔══════════════════════════════════════════════╗"
echo -e "  ║            J . A . R . V . I . S .           ║"
echo -e "  ║        персональный ИИ-агент, версия 1.0     ║"
echo -e "  ╚══════════════════════════════════════════════╝${OFF}"
echo ""

# ------------------------------------------------------------------ 1. Python
PY=""
for c in python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    V=$("$c" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null)
    if [ -n "$V" ] && [ "$V" -ge 309 ]; then PY="$c"; break; fi
  fi
done

if [ -z "$PY" ]; then
  echo -e "${YELLOW}  Не нашёл Python 3.9+. Сейчас установлю его сам…${OFF}"
  if [ "$(uname)" = "Darwin" ]; then
    if ! command -v brew >/dev/null 2>&1; then
      echo -e "${YELLOW}  Ставлю Homebrew (может попросить пароль от компьютера)…${OFF}"
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || true
      eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null)" || true
    fi
    brew install python@3.12 && PY=python3.12
  else
    sudo apt-get update -qq && sudo apt-get install -y python3 python3-venv python3-pip && PY=python3
  fi
fi

if [ -z "$PY" ]; then
  echo -e "${RED}  Не удалось установить Python. Скачайте его с python.org и запустите файл заново.${OFF}"
  read -r -p "  Нажмите Enter, чтобы закрыть…"
  exit 1
fi
echo -e "${GREEN}  ✓ Python найден:${OFF} $($PY --version)"

# ------------------------------------------------------------------ 2. Окружение
if [ ! -d "$VENV" ]; then
  echo -e "${CYAN}  Создаю рабочее окружение…${OFF}"
  "$PY" -m venv "$VENV" || { echo -e "${RED}  Ошибка создания окружения${OFF}"; read -r; exit 1; }
fi

echo -e "${CYAN}  Проверяю библиотеки (первый раз это 1–2 минуты)…${OFF}"
"$VENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1
"$VENV/bin/pip" install -q -r "$ROOT/backend/requirements.txt" || {
  echo -e "${RED}  Не удалось поставить библиотеки. Проверьте интернет и запустите файл заново.${OFF}"
  read -r -p "  Нажмите Enter…"; exit 1; }
echo -e "${GREEN}  ✓ Библиотеки на месте${OFF}"

# ------------------------------------------------------------------ 3. Интерфейс
if [ ! -f "$ROOT/frontend/dist/index.html" ]; then
  if command -v npm >/dev/null 2>&1; then
    echo -e "${CYAN}  Собираю интерфейс…${OFF}"
    (cd "$ROOT/frontend" && npm install --silent --no-audit --no-fund && npm run build >/dev/null)
  fi
fi
if [ -f "$ROOT/frontend/dist/index.html" ]; then
  echo -e "${GREEN}  ✓ Интерфейс готов${OFF}"
else
  echo -e "${YELLOW}  ⚠ Интерфейс не собран — но сервер всё равно запустится${OFF}"
fi

# ------------------------------------------------------------------ 4. Браузер (опция)
if [ ! -f "$MARK" ]; then
  echo ""
  echo -e "${CYAN}  Установить браузерный движок? Он нужен, чтобы Джарвис умел сам"
  echo -e "  открывать сайты, кликать и заполнять формы (~150 МБ, 2–3 минуты).${OFF}"
  read -r -p "  Установить? [y/N]: " ANS
  if [ "$ANS" = "y" ] || [ "$ANS" = "Y" ] || [ "$ANS" = "д" ]; then
    "$VENV/bin/pip" install -q playwright && "$VENV/bin/playwright" install chromium
    echo -e "${GREEN}  ✓ Браузер установлен${OFF}"
  else
    echo -e "${YELLOW}  Пропущено. Позже можно поставить: запустите этот файл и удалите .jarvis-installed${OFF}"
  fi
  date > "$MARK"
fi

# ------------------------------------------------------------------ 5. Запуск
echo ""
echo -e "${CYAN}  Запускаю Джарвиса…${OFF}"
echo ""
exec "$VENV/bin/python" "$ROOT/run.py"

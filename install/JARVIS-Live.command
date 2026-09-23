#!/bin/bash
# ============================================================================
#  J A R V I S  —  персональный AI-агент
#  ПОСТОЯННЫЙ установщик · скачивается ОДИН раз
#
#  Этот файл не содержит самой программы — он всегда скачивает и
#  запускает самую свежую версию с GitHub. Дважды кликните по нему:
#   1) определит самую свежую ветку репозитория и скачает её
#   2) сохранит ваши ключи и чаты (они лежат отдельно, в ~/JARVIS)
#   3) запустит сервер и откроет интерфейс в браузере
#  Повторный запуск = обновление (если вышло что-то новое) + открытие.
#  Нужен интернет. Офлайн-вариант — JARVIS.command из JARVIS.zip.
#
#  Зафиксировать ветку можно так:  JARVIS_BRANCH=main ./JARVIS-Live.command
# ============================================================================
set -u

REPO="monshepiano/monshe"
BRANCH="${JARVIS_BRANCH:-}"
VERSION=""

# путь к самому себе нужно вычислить ДО любых cd
SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"
HOME_DIR="$HOME/JARVIS"
APP_DIR="$HOME_DIR/app"
LOG_DIR="$HOME_DIR/logs"
LOG_FILE="$LOG_DIR/server.log"
PORT_FILE="$HOME_DIR/.port"
PID_FILE="$HOME_DIR/.pid"
SOURCE_FILE="$HOME_DIR/.source"
DEFAULT_PORT=8765

C_CYAN=$'\033[38;5;51m'; C_DIM=$'\033[2m'; C_OK=$'\033[38;5;46m'
C_WARN=$'\033[38;5;214m'; C_ERR=$'\033[38;5;203m'; C_OFF=$'\033[0m'; C_B=$'\033[1m'

say()  { printf "%s\n" "$1"; }
ok()   { printf "  ${C_OK}✔${C_OFF} %s\n" "$1"; }
info() { printf "  ${C_CYAN}›${C_OFF} %s\n" "$1"; }
warn() { printf "  ${C_WARN}!${C_OFF} %s\n" "$1"; }
err()  { printf "  ${C_ERR}✖${C_OFF} %s\n" "$1"; }

clear 2>/dev/null || true
printf "${C_CYAN}"
cat <<'BANNER'
      ██  █████  ██████  ██    ██ ██ ███████
      ██ ██   ██ ██   ██ ██    ██ ██ ██
      ██ ███████ ██████  ██    ██ ██ ███████
 ██   ██ ██   ██ ██   ██  ██  ██  ██      ██
  █████  ██   ██ ██   ██   ████   ██ ███████
BANNER
printf "${C_OFF}${C_DIM}      персональный AI-агент · всегда свежая версия с GitHub${C_OFF}\n\n"

# --------------------------------------------------------------- 1. Python
info "Проверяю Python…"
PY=""
for cand in python3 /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    v="$("$cand" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)"
    if [ "${v:-0}" -ge 308 ] 2>/dev/null; then PY="$cand"; break; fi
  fi
done

if [ -z "$PY" ]; then
  warn "Python 3 не найден. Сейчас macOS предложит установить инструменты разработчика."
  say  "     Нажмите «Установить», дождитесь окончания (5–10 минут) и снова"
  say  "     дважды кликните по этому файлу."
  xcode-select --install >/dev/null 2>&1 || true
  say ""
  read -r -p "  Нажмите Enter, чтобы закрыть окно… " _ || true
  exit 1
fi
ok "Python: $("$PY" -c 'import sys;print(sys.version.split()[0])') ($PY)"

# ------------------------------------------------------ 2. Останов старого
if [ -f "$PID_FILE" ]; then
  OLDPID="$(cat "$PID_FILE" 2>/dev/null || echo '')"
  if [ -n "$OLDPID" ] && kill -0 "$OLDPID" >/dev/null 2>&1; then
    info "Останавливаю запущенную копию Джарвиса…"
    kill "$OLDPID" >/dev/null 2>&1 || true
    sleep 1
    kill -9 "$OLDPID" >/dev/null 2>&1 || true
  fi
  rm -f "$PID_FILE"
fi

# ------------------------------------------ 3. Какую ветку брать с GitHub
FIRST_RUN=0
[ -d "$APP_DIR" ] || FIRST_RUN=1
if [ "$FIRST_RUN" = "1" ]; then info "Устанавливаю Джарвиса в $HOME_DIR…"; else info "Проверяю обновления с GitHub…"; fi

mkdir -p "$HOME_DIR" "$LOG_DIR" "$HOME_DIR/workspace" "$HOME_DIR/data" "$HOME_DIR/skills"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/jarvis.XXXXXX")"

COMMIT_SHA=""
COMMIT_DATE=""

# Самая свежая ветка = ветка с самым поздним коммитом. Так установщик
# следит за тем, что реально пушится в репозиторий, а не за застывшей
# main. JARVIS_BRANCH выше фиксирует выбор вручную.
CANDIDATES=""
if [ -n "$BRANCH" ]; then
  info "Ветка зафиксирована вручную: $BRANCH"
  CANDIDATES="$BRANCH"
else
  # ВАЖНО: скрипт ниже читается из heredoc, поэтому список веток он
  # скачивает сам (urllib), а не приходит по конвейеру в stdin.
  RESOLVED="$("$PY" - "$REPO" <<'PYBRANCH'
import json, sys, urllib.parse, urllib.request
repo = sys.argv[1]
def _get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-installer"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)
try:
    names = [b.get("name") for b in _get(
        "https://api.github.com/repos/%s/branches?per_page=100" % repo, 15)]
    names = [n for n in names if isinstance(n, str) and n][:30]
except Exception:
    sys.exit(0)
best = None
for name in names:
    try:
        commit = _get("https://api.github.com/repos/%s/commits/%s"
                      % (repo, urllib.parse.quote(name)), 10)
        date = (((commit.get("commit") or {}).get("committer")) or {}).get("date") or ""
        sha = commit.get("sha") or ""
    except Exception:
        continue
    if date and (best is None or date > best[0]):
        best = (date, name, sha)
if best:
    print("%s\t%s\t%s" % (best[1], best[0], best[2]))
PYBRANCH
)"
  if [ -n "${RESOLVED:-}" ]; then
    BRANCH="$(printf '%s' "$RESOLVED" | cut -f1)"
    COMMIT_DATE="$(printf '%s' "$RESOLVED" | cut -f2)"
    COMMIT_SHA="$(printf '%s' "$RESOLVED" | cut -f3)"
    info "Самая свежая ветка: $BRANCH (коммит от ${COMMIT_DATE:-?})"
    CANDIDATES="$BRANCH main"
  else
    warn "Не удалось узнать самую свежую ветку — пробую запасные."
    CANDIDATES="main arena/01a0c9e8-monshe"
    BRANCH="main"
  fi
fi

# Быстрый путь: тот же коммит уже стоит, сервер просто запускается заново
NEED_DOWNLOAD=1
if [ -n "$COMMIT_SHA" ] && [ -f "$SOURCE_FILE" ] && [ -f "$APP_DIR/jarvis/server.py" ]; then
  MARKED="$(cat "$SOURCE_FILE" 2>/dev/null || true)"
  if [ "$MARKED" = "$BRANCH $COMMIT_SHA" ]; then
    NEED_DOWNLOAD=0
    ok "Установлена уже самая свежая версия — скачивание не нужно"
  fi
fi

APP_SRC=""
if [ "$NEED_DOWNLOAD" = "1" ]; then
  for cand in $CANDIDATES; do
    info "Скачиваю программу с GitHub (ветка $cand)…"
    if ! curl -fsSL --retry 2 --max-time 180 \
         -o "$TMP_DIR/src.tar.gz" \
         "https://codeload.github.com/$REPO/tar.gz/refs/heads/$cand" 2>/dev/null; then
      continue
    fi
    tar -xzf "$TMP_DIR/src.tar.gz" -C "$TMP_DIR" 2>/dev/null || true
    APP_SRC="$(find "$TMP_DIR" -maxdepth 2 -type d -name app -print -quit 2>/dev/null || true)"
    if [ -n "$APP_SRC" ] && [ -f "$APP_SRC/jarvis/server.py" ]; then
      BRANCH="$cand"
      break
    fi
    APP_SRC=""
  done
  if [ -z "$APP_SRC" ]; then
    err "Не удалось скачать программу с GitHub."
    say "     Проверьте интернет и запустите файл ещё раз."
    rm -rf "$TMP_DIR"
    read -r -p "  Нажмите Enter, чтобы закрыть окно… " _ || true
    exit 1
  fi
  rm -rf "$APP_DIR"
  mkdir -p "$APP_DIR"
  cp -R "$APP_SRC/." "$APP_DIR/"
  find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  VERSION="$("$PY" - "$APP_DIR/jarvis/__init__.py" <<'PYVER'
import re, sys
try:
    text = open(sys.argv[1], encoding="utf-8").read()
except OSError:
    sys.exit(0)
m = re.search(r"(?:__version__|VERSION)\s*=\s*[\"']([^\"']+)", text)
print(m.group(1) if m else "")
PYVER
)"
  if [ -n "$COMMIT_SHA" ]; then
    printf '%s %s\n' "$BRANCH" "$COMMIT_SHA" > "$SOURCE_FILE"
  fi
  ok "Программа установлена: версия ${VERSION:-?} → $APP_DIR"
else
  VERSION="$("$PY" - "$APP_DIR/jarvis/__init__.py" <<'PYVER'
import re, sys
try:
    text = open(sys.argv[1], encoding="utf-8").read()
except OSError:
    sys.exit(0)
m = re.search(r"(?:__version__|VERSION)\s*=\s*[\"']([^\"']+)", text)
print(m.group(1) if m else "")
PYVER
)"
fi
rm -rf "$TMP_DIR"

# --------------------------------------------------- 4. Ключи и настройки
RAW_KEY_B64=""
if [ "$FIRST_RUN" = "1" ] && [ -t 0 ] && [ -z "${JARVIS_NO_INPUT:-}" ]; then
  NEED_KEY="$("$PY" - "$HOME_DIR" <<'PYCHECK'
import json, os, sys
try:
    with open(os.path.join(sys.argv[1], "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
except Exception:
    cfg = {}
key = (((cfg.get("providers") or {}).get("cloudru") or {}).get("api_key")) or ""
print("" if key else "1")
PYCHECK
)"
  if [ "$NEED_KEY" = "1" ]; then
    say ""
    say "  Для работы нужен API-ключ Cloud.ru (foundation-models.api.cloud.ru)."
    say "  Вставьте ключ и нажмите Enter — или просто Enter, чтобы ввести"
    say "  его позже в самом интерфейсе: Настройки → провайдеры."
    read -r -s -p "  Ключ Cloud.ru: " RAW_KEY || RAW_KEY=""
    say ""
    if [ -n "$RAW_KEY" ]; then
      RAW_KEY_B64="$(printf '%s' "$RAW_KEY" | base64 | tr -d '\n')"
    fi
  fi
fi

info "Настраиваю ключи нейросетей…"
"$PY" - "$HOME_DIR" "$RAW_KEY_B64" "" "" "" "" <<'PYSETUP'
import base64, json, os, sys
home = sys.argv[1]
def _dec(v):
    try:
        return base64.b64decode(v).decode("utf-8")
    except Exception:
        return ""
CLOUD_KEY = _dec(sys.argv[2] if len(sys.argv) > 2 else "")
DEEP_KEY = _dec(sys.argv[3] if len(sys.argv) > 3 else "")
GIGACHAT_KEY = _dec(sys.argv[4] if len(sys.argv) > 4 else "")
IMAGE_GATEWAY_URL = _dec(sys.argv[5] if len(sys.argv) > 5 else "")
IMAGE_GATEWAY_TOKEN = _dec(sys.argv[6] if len(sys.argv) > 6 else "")
path = os.path.join(home, "config.json")
cfg = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        cfg = {}
prov = cfg.setdefault("providers", {})
cloud = prov.setdefault("cloudru", {})
deep = prov.setdefault("deepseek", {})
cloud.setdefault("enabled", True)
cloud.setdefault("base_url", "https://foundation-models.api.cloud.ru/v1")
cloud.setdefault("label", "Cloud.ru Foundation Models")
deep.setdefault("enabled", True)
deep.setdefault("base_url", "https://api.deepseek.com/v1")
deep.setdefault("label", "DeepSeek (резерв)")
# уже однажды введённый ключ живёт в config.json и переживает обновления
if not cloud.get("api_key") and CLOUD_KEY:
    cloud["api_key"] = CLOUD_KEY
if not deep.get("api_key") and DEEP_KEY:
    deep["api_key"] = DEEP_KEY
media = cfg.setdefault("media", {})
media.setdefault("image_provider", "auto")
media.setdefault("gigachat_scope", "GIGACHAT_API_PERS")
media.setdefault("gigachat_model", "GigaChat")
if not media.get("gigachat_auth_key") and GIGACHAT_KEY:
    media["gigachat_auth_key"] = GIGACHAT_KEY
if IMAGE_GATEWAY_URL and IMAGE_GATEWAY_TOKEN:
    media["image_gateway_url"] = IMAGE_GATEWAY_URL
    media["image_gateway_token"] = IMAGE_GATEWAY_TOKEN
    if media.get("image_provider") != "off":
        media["image_provider"] = "gateway"
srv = cfg.setdefault("server", {})
srv.setdefault("host", "127.0.0.1")
srv.setdefault("port", 8765)
srv.setdefault("open_browser", False)
with open(path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)
print("config ok")
PYSETUP
ok "Ключи на месте (вводятся и меняются в интерфейсе → Настройки)"

# ------------------------------------------------------------- 5. Порт
PORT="$DEFAULT_PORT"
port_busy() { "$PY" - "$1" <<'PYPORT'
import socket, sys
s = socket.socket()
try:
  s.bind(("127.0.0.1", int(sys.argv[1]))); print("free")
except OSError:
  print("busy")
finally:
  s.close()
PYPORT
}
for try_port in 8765 8766 8767 8768 8790; do
  if [ "$(port_busy "$try_port")" = "free" ]; then PORT="$try_port"; break; fi
done
echo "$PORT" > "$PORT_FILE"
URL="http://127.0.0.1:$PORT"

# ------------------------------------------------------------ 6. Запуск
info "Запускаю Джарвиса…"
cd "$APP_DIR" || exit 1
JARVIS_NO_BROWSER=1 JARVIS_PORT="$PORT" nohup "$PY" -m jarvis >>"$LOG_FILE" 2>&1 &
SRV_PID=$!
echo "$SRV_PID" > "$PID_FILE"
disown "$SRV_PID" 2>/dev/null || true

READY=0
for _ in $(seq 1 40); do
  if [ "$(port_busy "$PORT")" = "busy" ]; then READY=1; break; fi
  if ! kill -0 "$SRV_PID" >/dev/null 2>&1; then break; fi
  sleep 0.4
done

if [ "$READY" != "1" ]; then
  err "Джарвис не смог запуститься. Последние строки журнала:"
  tail -n 20 "$LOG_FILE" 2>/dev/null | sed 's/^/     /'
  say ""
  read -r -p "  Нажмите Enter, чтобы закрыть окно… " _ || true
  exit 1
fi
ok "Сервер работает: $URL"

# --------------------------------------------------- 7. Ярлык на рабочий стол
# Ярлык — копия этого же постоянного установщика: клик по нему всегда
# ставит самую свежую версию, файл никогда не устаревает.
KEEP="$HOME_DIR/Джарвис.command"
if [ -f "$SELF" ] && [ "$SELF" != "$KEEP" ]; then
  cp -f "$SELF" "$KEEP" 2>/dev/null && chmod +x "$KEEP" 2>/dev/null
fi
DESKTOP="$HOME/Desktop"
if [ -d "$DESKTOP" ] && [ -f "$KEEP" ] && [ ! -e "$DESKTOP/Джарвис.command" ]; then
  if cp -f "$KEEP" "$DESKTOP/Джарвис.command" 2>/dev/null; then
    chmod +x "$DESKTOP/Джарвис.command" 2>/dev/null
    xattr -d com.apple.quarantine "$DESKTOP/Джарвис.command" >/dev/null 2>&1 || true
    ok "Ярлык «Джарвис» добавлен на рабочий стол"
  fi
fi

# ---------------------------------------------------------- 8. Браузер
# Автотест и серверная установка могут явно попросить не трогать графическую
# сессию. Раньше JARVIS_NO_BROWSER передавался только Python-процессу, а сам
# shell всё равно запускал open/xdg-open — флаг не выполнял обещание.
NO_BROWSER="${JARVIS_NO_BROWSER:-0}"
case " $* " in *" --install-only "*) NO_BROWSER=1 ;; esac
if [ "$NO_BROWSER" != "1" ]; then
  if command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
  fi
fi

say ""
printf "${C_OK}${C_B}  Джарвис запущен.${C_OFF}\n"
say ""
say "  Версия:         ${VERSION:-?}  (ветка $BRANCH)"
say "  Интерфейс:      $URL"
say "  Папка данных:   $HOME_DIR"
say "  Журнал:         $LOG_FILE"
say ""
say "  Этот установщик не устаревает: при каждом запуске он сам"
say "  скачивает самую свежую версию с GitHub. Скачивать что-то"
say "  вручную больше не нужно."
say ""
say "  Чтобы остановить — закройте это окно и выполните:  kill $SRV_PID"
say ""
printf "${C_DIM}  Это окно можно закрыть — Джарвис продолжит работать.${C_OFF}\n"
say ""
read -r -p "  Нажмите Enter, чтобы закрыть окно… " _ || true
exit 0

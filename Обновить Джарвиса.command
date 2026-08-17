#!/bin/bash
# ============================================================
#  Обновление Джарвиса до последней версии
#  Дважды щёлкните по этому файлу.
# ============================================================

cd "$(dirname "$0")" || exit 1
clear
CYAN=$'\033[38;5;51m'; GOLD=$'\033[38;5;214m'; RED=$'\033[31m'; DIM=$'\033[2m'; OFF=$'\033[0m'

echo "${CYAN}Обновление Джарвиса${OFF}"
echo

if [ ! -d ".git" ]; then
  echo "${GOLD}Эта копия установлена из архива.${OFF}"
  echo
  echo "Чтобы обновиться — просто попросите у меня новый архив"
  echo "и распакуйте его поверх этой папки (заменив файлы)."
  echo
  echo "${DIM}Ваши настройки, память и файлы лежат отдельно (~/.jarvis)"
  echo "и при замене папки не пострадают.${OFF}"
  echo
  echo "${DIM}Обновлю только компоненты, если они устарели…${OFF}"
  if [ -d ".venv" ]; then
    .venv/bin/python -m pip install --quiet --upgrade -r requirements.txt \
      && echo "${GOLD}Компоненты в порядке.${OFF}"
  fi
  echo
  read -r -p "Нажмите Enter, чтобы закрыть окно..."
  exit 0
fi

echo "${DIM}Скачиваю изменения…${OFF}"
git stash --include-untracked --quiet 2>/dev/null
if git pull --ff-only; then
  echo "${GOLD}Код обновлён.${OFF}"
else
  echo "${RED}Не удалось обновить. Проверьте интернет.${OFF}"
fi
git stash pop --quiet 2>/dev/null

if [ -d ".venv" ]; then
  echo "${DIM}Обновляю компоненты…${OFF}"
  .venv/bin/python -m pip install --quiet --upgrade -r requirements.txt
fi

echo
echo "${GOLD}Готово. Ваши настройки и память (~/.jarvis) не тронуты.${OFF}"
echo "Запустите «Джарвис.command»."
echo
read -r -p "Нажмите Enter, чтобы закрыть окно..."

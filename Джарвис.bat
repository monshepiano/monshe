@echo off
chcp 65001 >nul
title J.A.R.V.I.S.
cd /d "%~dp0"

echo.
echo   ==============================================
echo             J . A . R . V . I . S .
echo          персональный ИИ-агент, версия 1.0
echo   ==============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo   Не найден Python. Открываю страницу загрузки...
  echo   Установите Python, обязательно поставив галочку "Add python.exe to PATH",
  echo   затем запустите этот файл снова.
  start https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv" (
  echo   Создаю рабочее окружение...
  python -m venv .venv
)

echo   Проверяю библиотеки (первый раз это 1-2 минуты)...
call .venv\Scripts\python.exe -m pip install -q --upgrade pip
call .venv\Scripts\python.exe -m pip install -q -r backend\requirements.txt
if errorlevel 1 (
  echo   Не удалось поставить библиотеки. Проверьте интернет и запустите снова.
  pause
  exit /b 1
)

if not exist "frontend\dist\index.html" (
  where npm >nul 2>&1
  if not errorlevel 1 (
    echo   Собираю интерфейс...
    pushd frontend
    call npm install --silent --no-audit --no-fund
    call npm run build
    popd
  )
)

echo.
echo   Запускаю Джарвиса...
echo.
.venv\Scripts\python.exe run.py
pause

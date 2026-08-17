@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title JARVIS
cd /d "%~dp0"

echo.
echo  ==============================================
echo    JARVIS  ·  автозапуск (сделает всё сам)
echo  ==============================================
echo.

REM ---------- 1) Python ----------
where python >nul 2>nul
if %errorlevel%==0 goto :py_ok

echo  [x] Python не найден. Пробую установить автоматически...
where winget >nul 2>nul
if not %errorlevel%==0 (
    echo  [!] Не вышло. Установите Python вручную: https://www.python.org/downloads/
    pause
    exit /b 1
)
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
echo.
echo  Python установлен. Закройте это окно и запустите файл ещё раз.
pause
exit /b 0

:py_ok
echo  [ok] Python найден.
python --version

REM ---------- 2) Скачать проект ----------
if exist "monshe\server" goto :have_proj
echo.
echo  [*] Скачиваю проект JARVIS с GitHub (один раз)...
powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -Uri 'https://github.com/monshepiano/monshe/archive/refs/heads/arena/01a00f32-monshe.zip' -OutFile 'jarvis.zip'"
if not exist "jarvis.zip" (
    echo  [!] Не удалось скачать. Проверьте интернет и запустите ещё раз.
    pause
    exit /b 1
)
powershell -NoProfile -Command "Expand-Archive -Force -Path 'jarvis.zip' -DestinationPath '.'"
for /d %%d in (monshe-*) do ren "%%d" monshe
del jarvis.zip 2>nul
echo  [ok] Проект на месте.

:have_proj
cd monshe

REM ---------- 3) Ключ DeepSeek ----------
if exist ".env" goto :have_env
echo.
set /p "DSKEY=Вставьте ключ DeepSeek (sk-...): "
> ".env" echo DEEPSEEK_API_KEY=!DSKEY!
echo  [ok] Ключ записан.

:have_env

REM ---------- 4) Зависимости ----------
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  [*] Создаю окружение и ставлю зависимости (пару минут)...
    python -m venv .venv
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
if not %errorlevel%==0 (
    echo  [!] Ошибка установки зависимостей.
    pause
    exit /b 1
)

REM ---------- 5) Запуск ----------
echo.
echo  [*] Запускаю JARVIS: http://localhost:8000
start "" http://localhost:8000
python run.py

pause

@echo off
chcp 65001 >nul
rem Первый запуск: создаёт окружение, ставит зависимости и копирует .env.example в .env.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Создаю виртуальное окружение...
  python -m venv .venv || goto :error
  .venv\Scripts\python.exe -m pip install -r requirements.txt || goto :error
)
if not exist .env copy .env.example .env >nul
start "" http://127.0.0.1:8765
.venv\Scripts\python.exe run.py
goto :eof
:error
echo Не удалось подготовить окружение. Нужен Python 3.11+ (python.org).
pause

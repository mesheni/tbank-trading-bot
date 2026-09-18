@echo off
rem Короткий вызов бота: tbank status, tbank run, tbank report ...
rem Ищет venv рядом с собой, падает с подсказкой, если его нет.
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" cli.py %*
) else (
    echo Не найден .venv\Scripts\python.exe — создайте:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do taskkill /PID %%a /F >nul 2>nul
set PYTHONDONTWRITEBYTECODE=1
start "crawler-ui" /min python ".\app\ui.py" --open
timeout /t 1 /nobreak >nul


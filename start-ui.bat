@echo off
cd /d "%~dp0"
echo Starting Web UI...

REM Detect Python: py launcher 3.14/3.13/default -> python
where py >nul 2>&1
if %errorlevel%==0 (
    py -3.14 -m app.ui --open 2>nul
    if errorlevel 1 py -3.13 -m app.ui --open 2>nul
    if errorlevel 1 py -3 -m app.ui --open
    goto :done
)
where python >nul 2>&1
if %errorlevel%==0 (
    python -m app.ui --open
    goto :done
)

echo [ERROR] Python not found. Please install Python 3.10+ and add it to PATH.
echo         Download: https://www.python.org/downloads/

:done
echo.
pause

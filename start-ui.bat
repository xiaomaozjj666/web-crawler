@echo off
cd /d "%~dp0"
echo Starting Web UI...

REM --- 探测可用 Python 解释器：逐个验证 --version，第一个成功者胜出 ---
REM 探测失败不吞错误；主命令只运行一次，出错时保留 stderr 输出
set "EXIT_CODE=1"
py -3.14 --version >nul 2>&1 && goto :run314
py -3.13 --version >nul 2>&1 && goto :run313
py -3 --version >nul 2>&1 && goto :run3
python --version >nul 2>&1 && goto :runpy

echo [ERROR] Python not found. Please install Python 3.10+ and add it to PATH.
echo         Download: https://www.python.org/downloads/
goto :done

:run314
set "PY_CMD=py -3.14"
goto :execute

:run313
set "PY_CMD=py -3.13"
goto :execute

:run3
set "PY_CMD=py -3"
goto :execute

:runpy
set "PY_CMD=python"

:execute
echo Using Python: %PY_CMD%
%PY_CMD% -m app.ui --open
set "EXIT_CODE=%errorlevel%"

:done
echo.
pause
exit /b %EXIT_CODE%

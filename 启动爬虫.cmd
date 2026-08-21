@echo off
chcp 65001 >nul
cd /d "%~dp0"
title web-crawler 爬虫工作台（安全版）
echo ================================================
echo   web-crawler 爬虫工作台 - 安全版（默认）
echo   公开默认安全：拒绝私网 / 环回 / 云元数据目标
echo   Web UI: http://127.0.0.1:8765
echo ================================================
echo.

REM --- 探测可用 Python 解释器 ---
set "EXIT_CODE=1"
py -3.14 --version >nul 2>&1 && goto :run314
py -3.13 --version >nul 2>&1 && goto :run313
py -3 --version >nul 2>&1 && goto :run3
python --version >nul 2>&1 && goto :runpy

echo [ERROR] 未找到 Python，请安装 Python 3.10+ 并加入 PATH。
echo         下载: https://www.python.org/downloads/
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
echo 使用 Python: %PY_CMD%
REM 免安装运行：把项目 src/ 加入模块搜索路径（与 demo.py 一致）
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
%PY_CMD% app/ui.py --open
set "EXIT_CODE=%errorlevel%"

:done
echo.
pause
exit /b %EXIT_CODE%
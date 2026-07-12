@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
echo 正在启动 web_crawler 示范...

REM 按优先级探测可用 Python：py launcher 的 3.14/3.13/默认 -> python
where py >nul 2>&1
if %errorlevel%==0 (
    py -3.14 demo.py 2>nul
    if errorlevel 1 py -3.13 demo.py 2>nul
    if errorlevel 1 py -3 demo.py
    goto :done
)
where python >nul 2>&1
if %errorlevel%==0 (
    python demo.py
    goto :done
)

echo [错误] 未检测到 Python，请先安装 Python 3.10+ 并加入 PATH。
echo        下载地址：https://www.python.org/downloads/

:done
echo.
pause

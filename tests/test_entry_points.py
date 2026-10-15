"""模块入口冒烟测试:防止拆分再引入循环导入。

``python -m`` 启动时入口模块以 ``__main__`` 身份执行,若阶段子模块反向
import 入口模块(如 ``from web_crawler.app import crawler``),会形成双身份
循环导入——单元测试用普通 import 方式发现不了,只有子进程真实启动才暴露
(回归于 v0.5.1 拆分)。本文件用子进程守住各模块入口。
"""

from __future__ import annotations

import os
import subprocess
import sys


def test_crawler_module_entry_runs() -> None:
    """``python -m web_crawler.app.crawler --help`` 正常退出(无循环导入)。"""
    r = subprocess.run(
        [sys.executable, "-m", "web_crawler.app.crawler", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    assert "--url" in r.stdout


def test_ui_module_importable() -> None:
    """``import web_crawler.app.ui`` 无循环导入(ui 薄壳 re-export 链完整)。"""
    r = subprocess.run(
        [sys.executable, "-c", "import web_crawler.app.ui as m; print(m.__name__)"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    assert "web_crawler.app.ui" in r.stdout


def test_mcp_server_module_entry_runs() -> None:
    """``python -m web_crawler.mcp.server --help`` 正常退出(无循环导入)。

    子进程显式钉 ``PYTHONIOENCODING=cp1252``:入口若不自行切到 UTF-8,打印中文
    帮助会 ``UnicodeEncodeError`` 退出 1(2026-10-16 CI 的 windows-latest 实测,
    本机 ACP=65001 看不见这类缺陷)。
    """
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    r = subprocess.run(
        [sys.executable, "-m", "web_crawler.mcp.server", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=True,
        env=env,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    assert "--version" in r.stdout

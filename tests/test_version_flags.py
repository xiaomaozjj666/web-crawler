"""全 CLI 家族 ``-V/--version`` 标志与 ``python -m web_crawler`` 直跑入口的测试。

背景：历史上只有 ``web-crawler-mcp`` 支持 ``--version``（mcp/server.py），
其余 5 个入口缺失，违反 POSIX 惯例且排障（确认安装版本）不便。本文件统一
回归：每个 CLI 的 ``--version`` 必须以退出码 0 退出、输出包含包版本号，
``python -m web_crawler`` 必须可用且输出风格一致。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from web_crawler import __version__

# (模块路径, prog 名) —— 6 个 CLI 入口
_CLI_ENTRIES = [
    ("web_crawler.app.crawler", "web-crawler"),
    ("web_crawler.app.startproject", "web-crawler-startproject"),
    ("web_crawler.app.shell", "web-crawler-shell"),
    ("web_crawler.mcp.cli", "web-crawler-reverse"),
    ("web_crawler.mcp.server", "web-crawler-mcp"),
]


@pytest.mark.parametrize(("module", "prog"), _CLI_ENTRIES)
def test_cli_version_flag(module: str, prog: str, capsys: pytest.CaptureFixture[str]) -> None:
    """``-V/--version`` 以 SystemExit(0) 退出且输出含 prog 与版本号。"""
    for flag in ("-V", "--version"):
        with pytest.raises(SystemExit) as exc:
            if module == "web_crawler.app.crawler":
                # crawler 的 parser 在 _crawler_cli.build_parser
                from web_crawler.app._crawler_cli import build_parser

                build_parser().parse_args([flag])
            elif module == "web_crawler.mcp.cli":
                from web_crawler.mcp.cli import build_parser as build_reverse_parser

                build_reverse_parser().parse_args([flag])
            elif module == "web_crawler.mcp.server":
                from web_crawler.mcp.server import _build_parser as build_mcp_parser

                build_mcp_parser().parse_args([flag])
            else:
                # main(argv) 路径
                mod = __import__(module, fromlist=["main"])
                mod.main([flag])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert prog in out
        assert __version__ in out


def test_crawler_ui_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """``crawler-ui --version``：ui.main() 无 argv 参数，patch sys.argv 触发。"""
    from web_crawler.app import ui

    old_argv = sys.argv
    try:
        sys.argv = ["crawler-ui", "--version"]
        with pytest.raises(SystemExit) as exc:
            ui.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "crawler-ui" in out
        assert __version__ in out
    finally:
        sys.argv = old_argv


def test_python_m_web_crawler_version() -> None:
    """``python -m web_crawler --version`` 输出与 CLI 家族一致。"""
    proc = subprocess.run(
        [sys.executable, "-m", "web_crawler", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    assert f"web-crawler {__version__}" in proc.stdout


def test_python_m_web_crawler_help() -> None:
    """``python -m web_crawler --help`` 列出全部入口并指向文档。"""
    proc = subprocess.run(
        [sys.executable, "-m", "web_crawler", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    for prog in ("web-crawler-shell", "web-crawler-startproject", "crawler-ui", "web-crawler-mcp"):
        assert prog in proc.stdout


def test_python_m_web_crawler_rejects_subcommand() -> None:
    """本入口不接受子命令：未知参数报错且退出码 2。"""
    proc = subprocess.run(
        [sys.executable, "-m", "web_crawler", "crawl", "https://example.com"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 2
    assert "未知参数" in proc.stderr

"""其余五个 CLI 入口的统一输出模式回归测试（全部离线）。"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from web_crawler._cli import output_mode
from web_crawler.app import shell, startproject, ui
from web_crawler.mcp import cli as reverse_cli
from web_crawler.mcp import server as mcp_server
from web_crawler.response import Response


@pytest.fixture(autouse=True)
def _restore_logging_levels() -> Any:
    """入口会配置进程级日志；避免测试间泄漏级别。"""
    root = logging.getLogger()
    crawler = logging.getLogger("crawler")
    levels = (root.level, crawler.level)
    yield
    root.setLevel(levels[0])
    crawler.setLevel(levels[1])


def _entry_runner(
    entry: str, argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> Callable[[], Any]:
    """统一五种入口的 argv 风格（其中三个历史入口只读 sys.argv）。"""
    if entry == "startproject":
        return lambda: startproject.main(argv)
    if entry == "shell":
        return lambda: shell.main(argv)
    module = {"ui": ui, "mcp": mcp_server, "reverse": reverse_cli}[entry]
    monkeypatch.setattr(sys, "argv", [entry, *argv])
    return module.main


@pytest.mark.parametrize("entry", ["ui", "mcp", "reverse", "startproject", "shell"])
def test_output_modes_accepted_and_mutually_exclusive(
    entry: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """五入口均接受三种互斥模式，且保持 --version 快速路径。"""
    for flag, expected in (("--quiet", "quiet"), ("--verbose", "verbose"), ("--json", "json")):
        if entry == "mcp":
            args = mcp_server._build_parser().parse_args([flag])
        elif entry == "reverse":
            args = reverse_cli.build_parser().parse_args([flag])
        else:
            # UI/startproject/shell 通过真实入口解析；version 在任何副作用前退出。
            run = _entry_runner(entry, [flag, "--version"], monkeypatch)
            with pytest.raises(SystemExit) as exc_info:
                run()
            assert exc_info.value.code == 0
            capsys.readouterr()
            continue
        assert output_mode(args) == expected

    run = _entry_runner(entry, ["--quiet", "--json", "--version"], monkeypatch)
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 2
    capsys.readouterr()


def test_ui_json_emits_one_status_before_serving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[str] = []
    fake_server = MagicMock()
    fake_server.serve_forever.side_effect = lambda: events.append("serve")
    original_emit = ui.emit_json

    def recording_emit(payload: object) -> None:
        events.append("json")
        original_emit(payload)

    monkeypatch.setattr(ui, "emit_json", recording_emit)
    monkeypatch.setattr(ui, "ThreadingHTTPServer", lambda *args: fake_server)
    monkeypatch.setattr(ui.database, "init_db", lambda: None)
    monkeypatch.setattr(ui.database, "fail_stale_running_tasks", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["crawler-ui", "--json", "--port", "8765"])

    ui.main()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert events == ["json", "serve"]
    assert captured.out.count("\n") == 1
    assert payload["status"] == "starting"
    assert payload["url"] == "http://127.0.0.1:8765"


@pytest.mark.parametrize(("flag", "warns"), [("--quiet", False), ("--json", True)])
def test_mcp_quiet_and_json_never_pollute_protocol_stdout(
    flag: str,
    warns: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    fake_server = MagicMock()
    fake_server.run.side_effect = lambda: print('{"jsonrpc":"2.0","id":1,"result":{}}')
    monkeypatch.setattr(mcp_server, "ReverseMCPServer", lambda: fake_server)
    monkeypatch.setattr(sys, "argv", ["web-crawler-mcp", flag])

    mcp_server.main()

    captured = capsys.readouterr()
    assert json.loads(captured.out)["jsonrpc"] == "2.0"
    assert captured.out.count("\n") == 1
    assert ("DEEPSEEK_API_KEY" in captured.err) is warns
    fake_server.close.assert_called_once_with()


def test_mcp_verbose_enables_debug_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake_server = MagicMock()
    monkeypatch.setattr(mcp_server, "ReverseMCPServer", lambda: fake_server)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(sys, "argv", ["web-crawler-mcp", "--verbose"])
    caplog.set_level(logging.DEBUG)

    mcp_server.main()

    assert logging.getLogger().level == logging.DEBUG
    assert "MCP stdio 服务启动" in caplog.text


@pytest.mark.parametrize("flag", ["--quiet", "--json"])
def test_reverse_result_survives_output_modes(
    flag: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_server = MagicMock()
    fake_server.handle_tool.return_value = '{"ok":true,"value":"结果"}'
    monkeypatch.setattr(reverse_cli, "_make_server", lambda model: fake_server)
    monkeypatch.setattr(
        sys, "argv", ["web-crawler-reverse", flag, "scripts", "https://example.com"]
    )

    with pytest.raises(SystemExit) as exc_info:
        reverse_cli.main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True, "value": "结果"}
    if flag == "--json":
        assert captured.out.count("\n") == 1


def test_reverse_runtime_error_uses_shared_json_classification(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(reverse_cli, "_make_server", MagicMock(side_effect=OSError("offline")))
    monkeypatch.setattr(
        sys, "argv", ["web-crawler-reverse", "--json", "scripts", "https://example.com"]
    )
    with pytest.raises(SystemExit) as exc_info:
        reverse_cli.main()
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "network"
    assert payload["error"]["hint"]
    assert captured.err == ""


def test_startproject_json_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert startproject.main(["--json", "demo"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.out.count("\n") == 1
    assert captured.err == ""
    assert payload["status"] == "success"
    assert payload["project"] == str(tmp_path / "demo")
    assert set(payload["files"]) == {
        "README.md",
        "main.py",
        "requirements.txt",
        "spiders/example_spider.py",
    }
    assert payload["next_steps"] == [
        "cd demo",
        'pip install "stealth-web-crawler[all]"',
        "python main.py",
    ]


def test_startproject_quiet_silences_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert startproject.main(["--quiet", "demo"]) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert (tmp_path / "demo" / "main.py").is_file()


def test_startproject_verbose_adds_file_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert startproject.main(["--verbose", "demo"]) == 0
    out = capsys.readouterr().out
    assert "生成文件" in out
    assert "spiders/example_spider.py" in out
    assert "cd demo" in out


def test_startproject_json_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert startproject.main(["--json", "1bad"]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "configuration"
    assert captured.out.count("\n") == 1
    assert captured.err == ""


def _response() -> Response:
    return Response(
        url="https://example.com/final",
        status=200,
        content=b"<html><head><title>Example</title></head><body><h1>Hello</h1></body></html>",
    )


def test_shell_json_fetches_once_without_repl(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fetcher = MagicMock()
    fetcher.get.return_value = _response()
    monkeypatch.setattr(shell, "Fetcher", lambda **kwargs: fetcher)
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": (_ for _ in ()).throw(AssertionError("不应进入 REPL"))
    )

    assert shell.main(["--json", "https://example.com/"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.out.count("\n") == 1
    assert payload == {
        "status": "success",
        "url": "https://example.com/",
        "final_url": "https://example.com/final",
        "status_code": 200,
        "ok": True,
        "content_length": 75,
        "title": "Example",
    }
    fetcher.get.assert_called_once_with("https://example.com/")


def test_shell_json_requires_url_before_fetch_or_repl(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fetcher_factory = MagicMock()
    monkeypatch.setattr(shell, "Fetcher", fetcher_factory)
    assert shell.main(["--json"]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"]["type"] == "configuration"
    assert captured.err == ""
    fetcher_factory.assert_not_called()


def test_shell_json_fetch_error_is_classified(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fetcher = MagicMock()
    fetcher.get.side_effect = OSError("connection refused")
    monkeypatch.setattr(shell, "Fetcher", lambda **kwargs: fetcher)
    assert shell.main(["--json", "https://example.com/"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "network"


def test_shell_quiet_preserves_fetch_and_selector_results(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fetcher = MagicMock()
    fetcher.get.return_value = _response()
    monkeypatch.setattr(shell, "Fetcher", lambda **kwargs: fetcher)
    answers = iter(["css h1", "quit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert shell.main(["--quiet", "https://example.com/"]) == 0

    out = capsys.readouterr().out
    assert "响应摘要" in out
    assert "匹配 1 个元素" in out
    assert "Hello" in out

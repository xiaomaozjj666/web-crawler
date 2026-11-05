"""五个辅助 CLI 入口的统一输出模式集成测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from web_crawler.app import shell, startproject, ui
from web_crawler.mcp import cli as reverse_cli
from web_crawler.mcp import server as mcp_server
from web_crawler.response import Response


def test_startproject_json_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert startproject.main(["--json", "demo"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.out.count("\n") == 1
    assert payload["status"] == "success"
    assert payload["project"].endswith("demo")
    assert len(payload["files"]) == 4
    assert payload["next_steps"][-1] == "python main.py"


def test_startproject_quiet_and_verbose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert startproject.main(["--quiet", "silent"]) == 0
    assert capsys.readouterr().out == ""
    assert startproject.main(["--verbose", "loud"]) == 0
    output = capsys.readouterr().out
    assert "生成文件" in output
    assert "spiders/example_spider.py" in output


def test_startproject_json_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert startproject.main(["--json", "not-valid"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "configuration"
    assert not (tmp_path / "not-valid").exists()


def test_shell_json_is_one_shot_machine_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    response = Response(
        "https://example.com/final",
        200,
        b"<html><title>Example</title><p>ok</p></html>",
        {"content-type": "text/html"},
    )
    fetcher = MagicMock()
    fetcher.get.return_value = response
    monkeypatch.setattr(shell, "Fetcher", lambda **kwargs: fetcher)
    assert shell.main(["https://example.com", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.out.count("\n") == 1
    assert payload["status"] == "success"
    assert payload["status_code"] == 200
    assert payload["final_url"] == "https://example.com/final"
    assert payload["title"] == "Example"
    assert payload["content_length"] == len(response.content)


def test_shell_json_requires_url(capsys: pytest.CaptureFixture[str]) -> None:
    assert shell.main(["--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "configuration"


def test_reverse_json_result_stays_single_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = MagicMock()
    server.handle_tool.return_value = '{"algorithm":"AES","ok":true}'
    monkeypatch.setattr(reverse_cli, "_make_server", lambda model: server)
    args = reverse_cli.build_parser().parse_args(["--json", "analyze", "var x = 1;"])
    assert reverse_cli.cmd_analyze_js(args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["algorithm"] == "AES"
    assert captured.out.count("\n") == 1


def test_ui_json_start_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = MagicMock()
    server.serve_forever.side_effect = KeyboardInterrupt
    monkeypatch.setattr(ui, "ThreadingHTTPServer", lambda address, handler: server)
    monkeypatch.setattr(ui.database, "init_db", lambda: None)
    monkeypatch.setattr(ui.database, "fail_stale_running_tasks", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["crawler-ui", "--json"])
    ui.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "starting"
    assert payload["command"] == "crawler-ui"
    assert captured.out.count("\n") == 1
    server.server_close.assert_called_once_with()


def test_mcp_quiet_does_not_pollute_json_rpc_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = MagicMock()
    monkeypatch.setattr(mcp_server, "ReverseMCPServer", lambda: fake)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["web-crawler-mcp", "--quiet"])
    mcp_server.main()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    fake.run.assert_called_once_with()
    fake.close.assert_called_once_with()

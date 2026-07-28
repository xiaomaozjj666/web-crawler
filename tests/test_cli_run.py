"""Tests for the CLI run subcommand: argument parsing and cmd_run execution."""

from __future__ import annotations

from typing import Any

import pytest


def test_cli_run_subcommand_parses_args() -> None:
    """run 子命令能正确解析所有参数，包括 --enable-screenshot。"""
    from web_crawler.mcp.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--url",
            "http://example.com",
            "--task",
            "提取 Anti-Content",
            "--target-params",
            "anti_content,sign",
            "--max-steps",
            "10",
            "--headless",
            "--enable-checkpoint",
            "--min-confidence",
            "0.5",
            "--no-enable-guard",
            "--allowed-domains",
            "example.com,cdn.example.com",
            "--no-enable-screenshot",
            "--output",
            "-",
        ]
    )
    assert args.command == "run"
    assert args.url == "http://example.com"
    assert args.task == "提取 Anti-Content"
    assert args.target_params == "anti_content,sign"
    assert args.max_steps == 10
    assert args.headless is True
    assert args.enable_checkpoint is True
    assert args.min_confidence == 0.5
    assert args.enable_guard is False
    assert args.allowed_domains == "example.com,cdn.example.com"
    assert args.enable_screenshot is False
    assert args.output == "-"


def test_cli_run_subcommand_defaults() -> None:
    """run 子命令的默认值正确。"""
    from web_crawler.mcp.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["run", "--url", "http://x"])
    assert args.max_steps == 20
    assert args.headless is False
    assert args.enable_checkpoint is False
    assert args.min_confidence == 0.4
    assert args.enable_guard is True
    assert args.enable_screenshot is True
    assert args.output == "-"


def test_cli_run_executes_with_mocked_agent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_run 用 mock 的 ReverseAgent.run 验证完整流程。"""
    import json

    from web_crawler.mcp import cli as cli_module

    # 模拟 ReverseAgent 类
    class _MockAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.config = kwargs.get("config")
            self.closed = False

        def run(self, url: str, task: str = "") -> dict:
            return {
                "success": True,
                "target_params_found": {"anti_content": "abc123"},
                "steps": 5,
                "compiled_script": "print('hello')",
            }

        def close(self) -> None:
            self.closed = True

    # 模拟 DeepSeekProvider
    class _MockProvider:
        def __init__(self, **kwargs: Any) -> None:
            pass

    # 注入 mock
    import web_crawler.ai.llm as llm_module

    monkeypatch.setattr(llm_module, "DeepSeekProvider", _MockProvider)
    monkeypatch.setattr("web_crawler.ai.reverse_agent.ReverseAgent", _MockAgent)

    # 构造 args
    from web_crawler.mcp.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--url",
            "http://test.example.com",
            "--task",
            "test task",
            "--target-params",
            "anti_content",
            "--max-steps",
            "5",
        ]
    )

    exit_code = cli_module.cmd_run(args)
    assert exit_code == 0

    # 验证 stdout 输出是合法 JSON
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["success"] is True
    assert result["target_params_found"]["anti_content"] == "abc123"
    assert result["steps"] == 5


def test_cli_run_save_script_to_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_run 的 --save-script 参数正确保存脚本到文件。"""
    from web_crawler.mcp import cli as cli_module

    class _MockAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, url: str, task: str = "") -> dict:
            return {
                "success": True,
                "compiled_script": "print('mock script')",
                "steps": 1,
            }

        def close(self) -> None:
            pass

    class _MockProvider:
        def __init__(self, **kwargs: Any) -> None:
            pass

    import web_crawler.ai.llm as llm_module

    monkeypatch.setattr(llm_module, "DeepSeekProvider", _MockProvider)
    monkeypatch.setattr("web_crawler.ai.reverse_agent.ReverseAgent", _MockAgent)

    script_path = tmp_path / "output_script.py"
    from web_crawler.mcp.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--url",
            "http://x",
            "--save-script",
            str(script_path),
            "--output",
            str(tmp_path / "result.json"),
        ]
    )
    exit_code = cli_module.cmd_run(args)
    assert exit_code == 0

    # 脚本文件应存在且内容正确
    assert script_path.exists()
    assert script_path.read_text(encoding="utf-8") == "print('mock script')"

    # JSON 结果也应写入文件
    result_file = tmp_path / "result.json"
    assert result_file.exists()
    import json

    result = json.loads(result_file.read_text(encoding="utf-8"))
    assert result["success"] is True

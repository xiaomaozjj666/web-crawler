"""CLI 模块单元测试：覆盖 ``src/web_crawler/mcp/cli.py`` 的所有子命令、
参数解析、输出格式与交互式 REPL 模式。

所有 MCP server 依赖均通过 mock 注入，不启动真实浏览器或 LLM 调用。
``cmd_run`` 的成功路径已在 ``test_cli_run.py`` 覆盖，本文件补充错误路径与
其余子命令。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from web_crawler.mcp import cli as cli_module

# ---------------------------------------------------------------------------
# 辅助：构造 mock server 与 args
# ---------------------------------------------------------------------------


def _mock_server(handle_tool_result: str = '{"ok": true}') -> Any:
    """返回一个 mock server，handle_tool 返回给定 JSON 字符串。"""
    server = MagicMock()
    server.handle_tool.return_value = handle_tool_result
    server.get_tools.return_value = [
        {"name": "reverse_engineer_url", "description": "test tool"}
    ]
    return server


def _parse_args(argv: list[str]) -> Any:
    """用 build_parser 解析参数列表。"""
    return cli_module.build_parser().parse_args(argv)


# -- _print_json / _print_result --------------------------------------------


def test_print_json_outputs_formatted_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_print_json 输出格式化的 JSON（中文不转义）。"""
    cli_module._print_json({"msg": "你好"}, indent=4)
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["msg"] == "你好"


def test_print_result_success_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_print_result 对成功响应返回退出码 0。"""
    result = json.dumps({"data": "ok"})
    code = cli_module._print_result(result)
    assert code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["data"] == "ok"


def test_print_result_error_returns_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_print_result 对包含 error 的响应返回退出码 1。"""
    result = json.dumps({"error": "boom"})
    code = cli_module._print_result(result)
    assert code == 1


def test_print_result_uses_str_default_for_non_serializable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_print_json 通过 default=str 处理不可序列化对象。"""
    cli_module._print_json({"obj": object()})
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert "obj" in parsed


# -- _make_server -----------------------------------------------------------


def test_make_server_creates_reverse_mcp_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_make_server 调用 ReverseMCPServer 构造函数。"""
    fake_instance = MagicMock()
    monkeypatch.setattr(
        "web_crawler.mcp.server.ReverseMCPServer",
        lambda *args, **kwargs: fake_instance,
    )
    result = cli_module._make_server(model="test-model")
    assert result is fake_instance


# -- cmd_reverse_url --------------------------------------------------------


def test_cmd_reverse_url_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_reverse_url 成功时输出 JSON 并返回 0。"""
    server = _mock_server(json.dumps({"success": True}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["reverse", "http://x", "--target-params", "sign"])
    code = cli_module.cmd_reverse_url(args)
    assert code == 0
    server.handle_tool.assert_called_once()
    server.close.assert_called_once()


def test_cmd_reverse_url_error_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_reverse_url 工具返回错误时退出码 1。"""
    server = _mock_server(json.dumps({"error": "boom"}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["reverse", "http://x"])
    code = cli_module.cmd_reverse_url(args)
    assert code == 1


# -- cmd_inject_hooks -------------------------------------------------------


def test_cmd_inject_hooks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_inject_hooks 正确调用 handle_tool。"""
    server = _mock_server(json.dumps({"injected": ["fetch_hook"]}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["hooks", "http://x", "--hooks", "fetch_hook"])
    code = cli_module.cmd_inject_hooks(args)
    assert code == 0
    server.handle_tool.assert_called_once_with(
        "inject_hooks", {"url": "http://x", "hooks": ["fetch_hook"]}
    )


# -- cmd_analyze_js ---------------------------------------------------------


def test_cmd_analyze_js_with_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_analyze_js 通过 --file 读取 JS 文件。"""
    js_file = tmp_path / "code.js"
    js_file.write_text("var x = 1;", encoding="utf-8")
    server = _mock_server(json.dumps({"algorithm": "MD5"}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["analyze", "--file", str(js_file)])
    code = cli_module.cmd_analyze_js(args)
    assert code == 0
    call_args, _ = server.handle_tool.call_args
    assert call_args[1]["code"] == "var x = 1;"


def test_cmd_analyze_js_with_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_analyze_js 通过位置参数传代码。"""
    server = _mock_server(json.dumps({"algorithm": "AES"}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["analyze", "var y = 2;"])
    code = cli_module.cmd_analyze_js(args)
    assert code == 0


def test_cmd_analyze_js_empty_code_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_analyze_js 无代码输入时返回 1 并写 stderr。"""
    monkeypatch.setattr(cli_module, "_make_server", lambda model: _mock_server())
    args = _parse_args(["analyze"])
    code = cli_module.cmd_analyze_js(args)
    assert code == 1
    captured = capsys.readouterr()
    assert "错误" in captured.err


# -- cmd_webpack ------------------------------------------------------------


def test_cmd_webpack_with_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_webpack 通过 --file 读取源码。"""
    js_file = tmp_path / "bundle.js"
    js_file.write_text("__webpack_modules__={}", encoding="utf-8")
    server = _mock_server(json.dumps({"count": 0}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["webpack", "--file", str(js_file)])
    code = cli_module.cmd_webpack(args)
    assert code == 0


def test_cmd_webpack_empty_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_webpack 无源码输入时返回 1。"""
    monkeypatch.setattr(cli_module, "_make_server", lambda model: _mock_server())
    args = _parse_args(["webpack"])
    code = cli_module.cmd_webpack(args)
    assert code == 1


# -- cmd_deobfuscate --------------------------------------------------------


def test_cmd_deobfuscate_success_prints_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_deobfuscate 成功时直接打印反混淆后的代码。"""
    server = _mock_server(json.dumps({"deobfuscated": "var x = 1;"}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["deobfuscate", "var x=1"])
    code = cli_module.cmd_deobfuscate(args)
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "var x = 1;"


def test_cmd_deobfuscate_error_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_deobfuscate 工具返回错误时退出码 1。"""
    server = _mock_server(json.dumps({"error": "LLM call failed"}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["deobfuscate", "x"])
    code = cli_module.cmd_deobfuscate(args)
    assert code == 1


def test_cmd_deobfuscate_no_deobfuscated_field(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """结果无 deobfuscated 字段时打印完整 JSON。"""
    server = _mock_server(json.dumps({"other": "data"}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["deobfuscate", "x"])
    code = cli_module.cmd_deobfuscate(args)
    assert code == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["other"] == "data"


def test_cmd_deobfuscate_empty_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_deobfuscate 无代码输入时返回 1。"""
    monkeypatch.setattr(cli_module, "_make_server", lambda model: _mock_server())
    args = _parse_args(["deobfuscate"])
    code = cli_module.cmd_deobfuscate(args)
    assert code == 1


# -- cmd_reimplement --------------------------------------------------------


def test_cmd_reimplement_success_prints_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_reimplement 成功时直接打印重写后的代码。"""
    server = _mock_server(json.dumps({"code": "print('hi')"}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["reimplement", "fn()", "--language", "go"])
    code = cli_module.cmd_reimplement(args)
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "print('hi')"


def test_cmd_reimplement_error_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_reimplement 工具返回错误时退出码 1。"""
    server = _mock_server(json.dumps({"error": "boom"}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["reimplement", "x"])
    code = cli_module.cmd_reimplement(args)
    assert code == 1


def test_cmd_reimplement_no_code_field(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """结果无 code 字段时打印完整 JSON。"""
    server = _mock_server(json.dumps({"other": "data"}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["reimplement", "x"])
    code = cli_module.cmd_reimplement(args)
    assert code == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["other"] == "data"


def test_cmd_reimplement_empty_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_reimplement 无代码输入时返回 1。"""
    monkeypatch.setattr(cli_module, "_make_server", lambda model: _mock_server())
    args = _parse_args(["reimplement"])
    code = cli_module.cmd_reimplement(args)
    assert code == 1


# -- cmd_captcha ------------------------------------------------------------


def test_cmd_captcha(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_captcha 调用 solve_captcha 工具。"""
    server = _mock_server(json.dumps({"type": "none", "solved": True}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["captcha", "http://x"])
    code = cli_module.cmd_captcha(args)
    assert code == 0
    server.handle_tool.assert_called_once_with("solve_captcha", {"url": "http://x"})


# -- cmd_captcha_image ------------------------------------------------------


def test_cmd_captcha_image_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """text 模式读取图片并 base64 编码。"""
    img = tmp_path / "captcha.png"
    img.write_bytes(b"fake-png")
    server = _mock_server(json.dumps({"mode": "text", "text": "abc", "ok": True}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(
        ["captcha-image", "--mode", "text", "--image", str(img), "--mime", "image/jpeg"]
    )
    code = cli_module.cmd_captcha_image(args)
    assert code == 0
    call_args, _ = server.handle_tool.call_args
    payload = call_args[1]
    assert payload["mode"] == "text"
    assert payload["mime"] == "image/jpeg"
    import base64

    assert payload["image"] == base64.b64encode(b"fake-png").decode("ascii")


def test_cmd_captcha_image_text_no_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """text 模式缺 --image 时返回 1。"""
    monkeypatch.setattr(cli_module, "_make_server", lambda model: _mock_server())
    args = _parse_args(["captcha-image", "--mode", "text"])
    code = cli_module.cmd_captcha_image(args)
    assert code == 1
    captured = capsys.readouterr()
    assert "--image" in captured.err


def test_cmd_captcha_image_slider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """slider 模式读取 bg 和 slider 图片。"""
    bg = tmp_path / "bg.png"
    bg.write_bytes(b"bg-data")
    slider = tmp_path / "slider.png"
    slider.write_bytes(b"slider-data")
    server = _mock_server(json.dumps({"mode": "slider", "ok": True, "x": 100}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(
        ["captcha-image", "--mode", "slider", "--bg", str(bg), "--slider", str(slider)]
    )
    code = cli_module.cmd_captcha_image(args)
    assert code == 0
    call_args, _ = server.handle_tool.call_args
    payload = call_args[1]
    assert "bg" in payload
    assert "slider" in payload


def test_cmd_captcha_image_slider_no_bg(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """slider 模式缺 --bg 时返回 1。"""
    monkeypatch.setattr(cli_module, "_make_server", lambda model: _mock_server())
    args = _parse_args(["captcha-image", "--mode", "slider"])
    code = cli_module.cmd_captcha_image(args)
    assert code == 1
    captured = capsys.readouterr()
    assert "--bg" in captured.err


def test_cmd_captcha_image_click(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """click 模式读取图片并附带 prompt。"""
    img = tmp_path / "click.png"
    img.write_bytes(b"img-data")
    server = _mock_server(json.dumps({"mode": "click", "ok": True}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(
        ["captcha-image", "--mode", "click", "--image", str(img), "--prompt", "点红灯"]
    )
    code = cli_module.cmd_captcha_image(args)
    assert code == 0
    call_args, _ = server.handle_tool.call_args
    assert call_args[1]["prompt"] == "点红灯"


def test_cmd_captcha_image_click_no_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """click 模式缺 --image 时返回 1。"""
    monkeypatch.setattr(cli_module, "_make_server", lambda model: _mock_server())
    args = _parse_args(["captcha-image", "--mode", "click"])
    code = cli_module.cmd_captcha_image(args)
    assert code == 1
    captured = capsys.readouterr()
    assert "--image" in captured.err


# -- cmd_pentest ------------------------------------------------------------


def test_cmd_pentest_with_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_pentest 正确解析 --checks 参数并携带授权确认。"""
    server = _mock_server(json.dumps({"target": "x"}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(
        ["pentest", "example.com", "--checks", "ports,dirs", "--authorized"]
    )
    code = cli_module.cmd_pentest(args)
    assert code == 0
    call_args, _ = server.handle_tool.call_args
    payload = call_args[1]
    assert payload["checks"] == ["ports", "dirs"]
    assert payload["timeout"] == 30.0
    assert payload["authorization_confirmed"] is True


def test_cmd_pentest_with_ports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_pentest 正确解析 --ports 参数。"""
    server = _mock_server(json.dumps({"target": "x"}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["pentest", "x", "--ports", "22,80,443", "--authorized"])
    code = cli_module.cmd_pentest(args)
    assert code == 0
    call_args, _ = server.handle_tool.call_args
    assert call_args[1]["ports"] == [22, 80, 443]


def test_cmd_pentest_invalid_ports_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_pentest 端口列表格式无效时返回 1。"""
    monkeypatch.setattr(cli_module, "_make_server", lambda model: _mock_server())
    args = _parse_args(["pentest", "x", "--ports", "abc", "--authorized"])
    code = cli_module.cmd_pentest(args)
    assert code == 1
    captured = capsys.readouterr()
    assert "端口列表格式无效" in captured.err


def test_cmd_pentest_default_no_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_pentest 无 --checks 时不传 checks 字段。"""
    server = _mock_server(json.dumps({"target": "x"}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["pentest", "x", "--authorized"])
    code = cli_module.cmd_pentest(args)
    assert code == 0
    call_args, _ = server.handle_tool.call_args
    assert "checks" not in call_args[1]


def test_cmd_pentest_without_authorized_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_pentest 未传 --authorized 时拒绝执行并返回 1。"""
    monkeypatch.setattr(cli_module, "_make_server", lambda model: _mock_server())
    args = _parse_args(["pentest", "x"])
    code = cli_module.cmd_pentest(args)
    assert code == 1
    captured = capsys.readouterr()
    assert "--authorized" in captured.err


# -- cmd_capture ------------------------------------------------------------


def test_cmd_capture(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_capture 调用 capture_network_requests 工具。"""
    server = _mock_server(json.dumps({"count": 0}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["capture", "http://x", "--wait", "3.5"])
    code = cli_module.cmd_capture(args)
    assert code == 0
    server.handle_tool.assert_called_once_with(
        "capture_network_requests", {"url": "http://x", "wait_time": 3.5}
    )


# -- cmd_scripts ------------------------------------------------------------


def test_cmd_scripts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_scripts 调用 get_page_scripts 工具。"""
    server = _mock_server(json.dumps({"count": 0}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    args = _parse_args(["scripts", "http://x"])
    code = cli_module.cmd_scripts(args)
    assert code == 0
    server.handle_tool.assert_called_once_with("get_page_scripts", {"url": "http://x"})


# -- cmd_run 错误路径（成功路径已在 test_cli_run.py 覆盖） ---------------------


def test_cmd_run_agent_exception_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_run 的 agent.run 抛异常时返回 1 并写 stderr。"""

    class _CrashAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, url: str, task: str = "") -> dict:
            raise RuntimeError("agent crashed")

        def close(self) -> None:
            pass

    class _MockProvider:
        def __init__(self, **kwargs: Any) -> None:
            pass

    monkeypatch.setattr("web_crawler.ai.llm.DeepSeekProvider", _MockProvider)
    monkeypatch.setattr("web_crawler.ai.reverse_agent.ReverseAgent", _CrashAgent)
    args = _parse_args(["run", "--url", "http://x"])
    code = cli_module.cmd_run(args)
    assert code == 1
    captured = capsys.readouterr()
    assert "Agent 执行失败" in captured.err


def test_cmd_run_save_script_os_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_run 保存脚本文件失败时写 stderr 但不阻断（仍返回 0）。"""

    class _MockAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, url: str, task: str = "") -> dict:
            return {"success": True, "compiled_script": "print('x')", "steps": 1}

        def close(self) -> None:
            pass

    class _MockProvider:
        def __init__(self, **kwargs: Any) -> None:
            pass

    monkeypatch.setattr("web_crawler.ai.llm.DeepSeekProvider", _MockProvider)
    monkeypatch.setattr("web_crawler.ai.reverse_agent.ReverseAgent", _MockAgent)
    args = _parse_args(
        ["run", "--url", "http://x", "--save-script", "Z:/no_such_dir/x.py"]
    )
    code = cli_module.cmd_run(args)
    assert code == 0
    captured = capsys.readouterr()
    assert "保存脚本失败" in captured.err


def test_cmd_run_with_allowed_domains(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_run 传入 --allowed-domains 时正确解析为列表。"""

    class _MockAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, url: str, task: str = "") -> dict:
            return {"success": True, "steps": 1}

        def close(self) -> None:
            pass

    class _MockProvider:
        def __init__(self, **kwargs: Any) -> None:
            pass

    monkeypatch.setattr("web_crawler.ai.llm.DeepSeekProvider", _MockProvider)
    monkeypatch.setattr("web_crawler.ai.reverse_agent.ReverseAgent", _MockAgent)
    args = _parse_args(
        ["run", "--url", "http://x", "--allowed-domains", "a.com, b.com"]
    )
    code = cli_module.cmd_run(args)
    assert code == 0


# -- cmd_interactive REPL ---------------------------------------------------


def _setup_interactive(
    monkeypatch: pytest.MonkeyPatch, inputs: list[str], result: str = '{"ok": true}'
) -> Any:
    """配置 interactive 模式的 mock：server + input 序列。"""
    server = _mock_server(result)
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    # 模拟 input() 依次返回 inputs 中的值
    input_iter = iter(inputs)

    def _fake_input(prompt: str = "") -> str:
        return next(input_iter)

    monkeypatch.setattr("builtins.input", _fake_input)
    return server


def test_cmd_interactive_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式输入 exit 正常退出。"""
    _setup_interactive(monkeypatch, ["exit"])
    args = _parse_args(["interactive"])
    code = cli_module.cmd_interactive(args)
    assert code == 0


def test_cmd_interactive_quit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式输入 quit 正常退出。"""
    _setup_interactive(monkeypatch, ["quit"])
    args = _parse_args(["interactive"])
    code = cli_module.cmd_interactive(args)
    assert code == 0


def test_cmd_interactive_eof(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 EOF (Ctrl+D) 正常退出。"""
    server = _mock_server()
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)

    def _raise_eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)
    args = _parse_args(["interactive"])
    code = cli_module.cmd_interactive(args)
    assert code == 0


def test_cmd_interactive_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 KeyboardInterrupt 正常退出。"""
    server = _mock_server()
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)

    def _raise_kbd(prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _raise_kbd)
    args = _parse_args(["interactive"])
    code = cli_module.cmd_interactive(args)
    assert code == 0
    captured = capsys.readouterr()
    assert "退出" in captured.out


def test_cmd_interactive_empty_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式空行被跳过。"""
    _setup_interactive(monkeypatch, ["", "exit"])
    args = _parse_args(["interactive"])
    code = cli_module.cmd_interactive(args)
    assert code == 0


def test_cmd_interactive_tools_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 tools 命令列出所有工具。"""
    server = _setup_interactive(monkeypatch, ["tools", "exit"])
    server.get_tools.return_value = [
        {"name": "reverse_engineer_url", "description": "逆向分析"},
        {"name": "deobfuscate_js", "description": "反混淆"},
    ]
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    captured = capsys.readouterr()
    assert "reverse_engineer_url" in captured.out
    assert "deobfuscate_js" in captured.out


def test_cmd_interactive_reverse_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 reverse 命令调用 reverse_engineer_url。"""
    server = _setup_interactive(
        monkeypatch,
        ["reverse http://x --target-params sign anti", "exit"],
        json.dumps({"agent": True}),
    )
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    server.handle_tool.assert_called_once()
    name, _ = server.handle_tool.call_args[0]
    assert name == "reverse_engineer_url"


def test_cmd_interactive_hooks_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 hooks 命令调用 inject_hooks。"""
    server = _setup_interactive(
        monkeypatch, ["hooks http://x", "exit"], json.dumps({"injected": []})
    )
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    server.handle_tool.assert_called_once_with("inject_hooks", {"url": "http://x", "hooks": []})


def test_cmd_interactive_analyze_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 analyze 命令调用 analyze_js_code。"""
    server = _setup_interactive(
        monkeypatch, ["analyze var_x=1", "exit"], json.dumps({"algorithm": "MD5"})
    )
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    server.handle_tool.assert_called_once_with(
        "analyze_js_code", {"code": "var_x=1"}
    )


def test_cmd_interactive_webpack_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 webpack 命令调用 extract_webpack_modules。"""
    server = _setup_interactive(
        monkeypatch, ["webpack var_x=1", "exit"], json.dumps({"count": 0})
    )
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    server.handle_tool.assert_called_once_with(
        "extract_webpack_modules", {"code": "var_x=1"}
    )


def test_cmd_interactive_deobfuscate_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 deobfuscate 命令调用 deobfuscate_js。"""
    server = _setup_interactive(
        monkeypatch, ["deobfuscate var_x=1", "exit"], json.dumps({"deobfuscated": "x"})
    )
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    server.handle_tool.assert_called_once_with("deobfuscate_js", {"code": "var_x=1"})


def test_cmd_interactive_reimplement_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 reimplement 命令调用 reimplement_algorithm。"""
    server = _setup_interactive(
        monkeypatch, ["reimplement var_x=1", "exit"], json.dumps({"code": "print()"})
    )
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    call_args, _ = server.handle_tool.call_args
    assert call_args[1]["code"] == "var_x=1"
    assert call_args[1]["language"] == "python"


def test_cmd_interactive_captcha_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 captcha 命令调用 solve_captcha。"""
    server = _setup_interactive(
        monkeypatch, ["captcha http://x", "exit"], json.dumps({"type": "none"})
    )
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    server.handle_tool.assert_called_once_with("solve_captcha", {"url": "http://x"})


def test_cmd_interactive_captcha_image_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 captcha-image text 命令读取图片。"""
    img = tmp_path / "cap.png"
    img.write_bytes(b"img")
    server = _mock_server(json.dumps({"text": "abc"}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    inputs = [f"captcha-image text {img}", "exit"]
    input_iter = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda p="": next(input_iter))
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    server.handle_tool.assert_called_once()
    call_args, _ = server.handle_tool.call_args
    assert call_args[1]["mode"] == "text"


def test_cmd_interactive_captcha_image_slider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 captcha-image slider 命令读取两张图片。"""
    bg = tmp_path / "bg.png"
    bg.write_bytes(b"bg")
    sl = tmp_path / "sl.png"
    sl.write_bytes(b"sl")
    server = _mock_server(json.dumps({"ok": True}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    inputs = [f"captcha-image slider {bg} {sl}", "exit"]
    input_iter = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda p="": next(input_iter))
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    call_args, _ = server.handle_tool.call_args
    assert call_args[1]["mode"] == "slider"


def test_cmd_interactive_captcha_image_click(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 captcha-image click 命令读取图片与 prompt。"""
    img = tmp_path / "click.png"
    img.write_bytes(b"img")
    server = _mock_server(json.dumps({"ok": True}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    inputs = [f"captcha-image click {img} 点红灯", "exit"]
    input_iter = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda p="": next(input_iter))
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    call_args, _ = server.handle_tool.call_args
    payload = call_args[1]
    assert payload["mode"] == "click"
    assert payload["prompt"] == "点红灯"


def test_cmd_interactive_captcha_image_invalid_usage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 captcha-image 参数不足时输出用法提示。"""
    _setup_interactive(monkeypatch, ["captcha-image text", "exit"])
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    captured = capsys.readouterr()
    assert "用法" in captured.err


def test_cmd_interactive_captcha_image_read_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 captcha-image 读取图片失败时输出错误。"""
    _setup_interactive(monkeypatch, ["captcha-image text /nonexistent.png", "exit"])
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    captured = capsys.readouterr()
    assert "读取图片失败" in captured.err


def test_cmd_interactive_pentest_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 pentest 命令需 --authorized 确认并调用 pentest_recon。"""
    server = _setup_interactive(
        monkeypatch,
        ["pentest example.com --checks ports,dirs --authorized", "exit"],
        json.dumps({"target": "example.com"}),
    )
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    call_args, _ = server.handle_tool.call_args
    payload = call_args[1]
    assert payload["target"] == "example.com"
    assert payload["checks"] == ["ports", "dirs"]
    assert payload["authorization_confirmed"] is True


def test_cmd_interactive_pentest_without_authorized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 pentest 未带 --authorized 时提示并跳过（不调用工具）。"""
    server = _setup_interactive(
        monkeypatch,
        ["pentest example.com", "exit"],
        json.dumps({"target": "example.com"}),
    )
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    server.handle_tool.assert_not_called()
    captured = capsys.readouterr()
    assert "--authorized" in captured.err


def test_cmd_interactive_capture_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 capture 命令调用 capture_network_requests。"""
    server = _setup_interactive(
        monkeypatch, ["capture http://x", "exit"], json.dumps({"count": 0})
    )
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    server.handle_tool.assert_called_once_with(
        "capture_network_requests", {"url": "http://x", "wait_time": 5.0}
    )


def test_cmd_interactive_scripts_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 scripts 命令调用 get_page_scripts。"""
    server = _setup_interactive(
        monkeypatch, ["scripts http://x", "exit"], json.dumps({"count": 0})
    )
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    server.handle_tool.assert_called_once_with("get_page_scripts", {"url": "http://x"})


def test_cmd_interactive_unknown_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式未知命令输出提示。"""
    _setup_interactive(monkeypatch, ["bogus_cmd", "exit"])
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    captured = capsys.readouterr()
    assert "未知命令" in captured.out


def test_cmd_interactive_analyze_file_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 analyze 命令读取文件内容。"""
    js = tmp_path / "code.js"
    js.write_text("var x = 1;", encoding="utf-8")
    server = _setup_interactive(
        monkeypatch, [f"analyze {js}", "exit"], json.dumps({"algorithm": "MD5"})
    )
    args = _parse_args(["interactive"])
    cli_module.cmd_interactive(args)
    call_args, _ = server.handle_tool.call_args
    assert call_args[1]["code"] == "var x = 1;"


def test_cmd_interactive_analyze_file_os_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """interactive 模式 analyze 文件读取失败时输出错误并继续。"""
    # tmp_path 是目录，Path.exists() 返回 True 但 read_text 抛 OSError
    _setup_interactive(monkeypatch, [f"analyze {tmp_path}", "exit"])
    args = _parse_args(["interactive"])
    code = cli_module.cmd_interactive(args)
    assert code == 0
    captured = capsys.readouterr()
    assert "读取文件失败" in captured.err


def test_cmd_interactive_repl_alias(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """repl 是 interactive 的别名。"""
    _setup_interactive(monkeypatch, ["exit"])
    args = _parse_args(["repl"])
    code = cli_module.cmd_interactive(args)
    assert code == 0


# -- main 入口 --------------------------------------------------------------


def test_main_no_command_prints_help_and_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """无子命令时 main 打印帮助并以 2 退出（缺必需命令属用法错误）。"""
    monkeypatch.setattr("sys.argv", ["web-crawler-reverse"])
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main()
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "子命令" in captured.out or "usage" in captured.out.lower()


def test_main_with_command_executes_func(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """main 有子命令时调用对应 func 并以 func 返回值退出。"""
    server = _mock_server(json.dumps({"ok": True}))
    monkeypatch.setattr(cli_module, "_make_server", lambda model: server)
    monkeypatch.setattr("sys.argv", ["web-crawler-reverse", "scripts", "http://x"])
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main()
    assert exc_info.value.code == 0


# -- build_parser 参数解析 --------------------------------------------------


def test_build_parser_global_model_arg() -> None:
    """build_parser 支持 --model 全局参数。"""
    args = _parse_args(["--model", "custom-model", "scripts", "http://x"])
    assert args.model == "custom-model"


def test_build_parser_captcha_image_choices() -> None:
    """captcha-image 的 --mode 限定 choices。"""
    # 合法 mode
    args = _parse_args(["captcha-image", "--mode", "text"])
    assert args.mode == "text"
    # 非法 mode 应在 argparse 层报错
    with pytest.raises(SystemExit):
        _parse_args(["captcha-image", "--mode", "bogus"])


def test_build_parser_interactive_alias() -> None:
    """interactive 子命令有 repl 别名。"""
    args = _parse_args(["repl"])
    assert args.command in ("interactive", "repl")

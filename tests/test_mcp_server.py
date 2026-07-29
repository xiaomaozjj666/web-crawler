"""MCP server 单元测试：覆盖 ReverseMCPServer 的工具分发、JSON-RPC 协议、
序列化辅助与 stdio 主循环。所有外部依赖（LLM provider、浏览器、ReverseAgent、
pentest 工具链）均被 mock，不发起真实网络请求或浏览器启动。

覆盖目标：``src/web_crawler/mcp/server.py``。
"""

from __future__ import annotations

import io
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from web_crawler.mcp import server as server_module
from web_crawler.mcp.server import (
    ReverseMCPServer,
    _error,
    _json_default,
    _to_json,
)

# ---------------------------------------------------------------------------
# 测试辅助：构造一个绕过 __init__ 的 ReverseMCPServer（避免真实 provider/agent）
# ---------------------------------------------------------------------------


def _make_server(
    *,
    provider: Any = None,
    analyzer: Any = None,
    captcha_manager: Any = None,
    agent: Any = None,
) -> ReverseMCPServer:
    """绕过 __init__ 构造 server，手动注入 mock 依赖。"""
    srv: ReverseMCPServer = object.__new__(ReverseMCPServer)
    srv.provider_name = "deepseek"
    srv.model = "deepseek-v4-pro"
    srv.provider = provider or MagicMock(name="provider", model="m", name_="deepseek")
    srv.analyzer = analyzer or MagicMock(name="analyzer")
    srv.captcha_manager = captcha_manager or MagicMock(name="captcha_manager")
    srv.agent = agent
    srv._fetcher = None
    srv._closed = False
    return srv


# -- 序列化辅助函数 ----------------------------------------------------------


class _Color(Enum):
    """用于测试 _json_default 枚举序列化。"""

    RED = "red"


@dataclass
class _Point:
    """用于测试 _json_default dataclass 序列化。"""

    x: int
    y: int


def test_json_default_serializes_dataclass() -> None:
    """dataclass 实例应被 asdict 序列化为 dict。"""
    assert _json_default(_Point(1, 2)) == {"x": 1, "y": 2}


def test_json_default_serializes_dataclass_type_returns_str() -> None:
    """dataclass 类本身（非实例）应回退到 str。"""
    # _Point 是类型对象，is_dataclass 返回 True 但 isinstance(obj, type) 也为 True，
    # 因此不进 asdict 分支，走 str 兜底。
    result = _json_default(_Point)
    assert isinstance(result, str)


def test_json_default_serializes_enum() -> None:
    """枚举成员应序列化为 .value。"""
    assert _json_default(_Color.RED) == "red"


def test_json_default_fallback_to_str() -> None:
    """无法识别的对象回退到 str。"""
    obj = object()
    assert _json_default(obj) == str(obj)


def test_to_json_preserves_chinese() -> None:
    """JSON 序列化不转义中文字符。"""
    result = _to_json({"msg": "你好"})
    assert "你好" in result
    parsed = json.loads(result)
    assert parsed["msg"] == "你好"


def test_to_json_supports_dataclass() -> None:
    """_to_json 通过 default 钩子序列化 dataclass。"""
    result = _to_json({"point": _Point(3, 4)})
    parsed = json.loads(result)
    assert parsed["point"] == {"x": 3, "y": 4}


def test_error_includes_extra_fields() -> None:
    """_error 构造的错误响应包含 error 与额外字段。"""
    result = _error("boom", details="x", code=42)
    parsed = json.loads(result)
    assert parsed["error"] == "boom"
    assert parsed["details"] == "x"
    assert parsed["code"] == 42


# -- 资源管理 ----------------------------------------------------------------


def test_create_agent_returns_none_when_module_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_HAS_REVERSE_AGENT 为 False 时 _create_agent 返回 None。"""
    monkeypatch.setattr(server_module, "_HAS_REVERSE_AGENT", False)
    monkeypatch.setattr(server_module, "ReverseAgent", None)
    monkeypatch.setattr(server_module, "ReverseAgentConfig", None)
    srv = _make_server()
    assert srv._create_agent() is None


def test_create_agent_returns_none_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ReverseAgent 构造抛异常时 _create_agent 返回 None（容错降级）。"""
    monkeypatch.setattr(server_module, "_HAS_REVERSE_AGENT", True)

    def _boom(**kwargs: Any) -> None:
        raise RuntimeError("init failed")

    monkeypatch.setattr(server_module, "ReverseAgent", _boom)
    monkeypatch.setattr(server_module, "ReverseAgentConfig", lambda: MagicMock())
    srv = _make_server()
    assert srv._create_agent() is None


def test_get_fetcher_lazy_creation() -> None:
    """_get_fetcher 首次调用创建 CamoufoxFetcher，后续复用同一实例。"""
    srv = _make_server()
    fake_fetcher = MagicMock()
    with patch.object(server_module, "CamoufoxFetcher", return_value=fake_fetcher):
        first = srv._get_fetcher()
        second = srv._get_fetcher()
    assert first is fake_fetcher
    assert second is fake_fetcher


def test_close_clears_fetcher_and_marks_closed() -> None:
    """close 关闭 fetcher 并标记 _closed。"""
    srv = _make_server()
    fake_fetcher = MagicMock()
    srv._fetcher = fake_fetcher
    srv.close()
    fake_fetcher.close.assert_called_once()
    assert srv._fetcher is None
    assert srv._closed is True


def test_close_swallows_fetcher_exception() -> None:
    """fetcher.close 抛异常时 close 静默吞掉（best-effort 清理）。"""
    srv = _make_server()
    fake_fetcher = MagicMock()
    fake_fetcher.close.side_effect = RuntimeError("boom")
    srv._fetcher = fake_fetcher
    srv.close()  # 不应抛
    assert srv._fetcher is None
    assert srv._closed is True


def test_close_without_fetcher() -> None:
    """无 fetcher 时 close 仅标记 _closed。"""
    srv = _make_server()
    srv.close()
    assert srv._closed is True
    assert srv._fetcher is None


# -- 工具列表 / prompts / resources 元信息 ----------------------------------


def test_get_tools_have_required_schema_fields() -> None:
    """每个工具必须包含 name / description / inputSchema。"""
    srv = _make_server()
    tools = srv.get_tools()
    assert len(tools) >= 10
    for tool in tools:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
        assert tool["inputSchema"]["type"] == "object"


def test_get_tools_includes_all_handler_names() -> None:
    """get_tools 返回的工具名应与 handle_tool 的分发表一致。"""
    srv = _make_server()
    names = {t["name"] for t in srv.get_tools()}
    expected = {
        "reverse_engineer_url",
        "inject_hooks",
        "analyze_js_code",
        "extract_webpack_modules",
        "deobfuscate_js",
        "reimplement_algorithm",
        "solve_captcha",
        "solve_captcha_image",
        "pentest_recon",
        "capture_network_requests",
        "get_page_scripts",
    }
    assert expected <= names


def test_get_prompts_structure() -> None:
    """prompts 列表结构正确，每个 prompt 含 name/description/arguments。"""
    srv = _make_server()
    prompts = srv.get_prompts()
    assert len(prompts) == 3
    for p in prompts:
        assert "name" in p
        assert "description" in p
        assert "arguments" in p
        for a in p["arguments"]:
            assert "name" in a
            assert "required" in a


def test_get_resources_structure() -> None:
    """resources 列表结构正确。"""
    srv = _make_server()
    resources = srv.get_resources()
    assert len(resources) == 4
    uris = {r["uri"] for r in resources}
    assert "agent://state" in uris
    assert "hooks://library" in uris


# -- render_prompt ----------------------------------------------------------


def test_render_prompt_reverse_engineer_url_no_params() -> None:
    """reverse_engineer_url prompt 无目标参数时显示自动识别。"""
    srv = _make_server()
    rendered = srv.render_prompt("reverse_engineer_url", {"url": "http://x"})
    assert "http://x" in rendered
    assert "(自动识别)" in rendered
    assert "20" in rendered  # 默认 max_steps


def test_render_prompt_reverse_engineer_url_with_params() -> None:
    """reverse_engineer_url prompt 带目标参数时正确拼接。"""
    srv = _make_server()
    rendered = srv.render_prompt(
        "reverse_engineer_url",
        {"url": "http://x", "target_params": "Anti-Content,X-Bogus", "max_steps": "5"},
    )
    assert "Anti-Content、X-Bogus" in rendered
    assert "- 最大步数: 5" in rendered


def test_render_prompt_deobfuscate_js_without_focus() -> None:
    """deobfuscate_js prompt 无 focus_param 时不输出重点关注行。"""
    srv = _make_server()
    rendered = srv.render_prompt("deobfuscate_js", {"code": "var a=1"})
    assert "var a=1" in rendered
    assert "重点关注参数" not in rendered


def test_render_prompt_deobfuscate_js_with_focus() -> None:
    """deobfuscate_js prompt 带 focus_param 时输出重点关注行。"""
    srv = _make_server()
    rendered = srv.render_prompt(
        "deobfuscate_js", {"code": "var a=1", "focus_param": "sign"}
    )
    assert "重点关注参数：sign" in rendered


def test_render_prompt_reimplement_algorithm_default_language() -> None:
    """reimplement_algorithm prompt 默认语言为 python。"""
    srv = _make_server()
    rendered = srv.render_prompt("reimplement_algorithm", {"code": "fn()"})
    assert "python" in rendered
    assert "fn()" in rendered


def test_render_prompt_reimplement_algorithm_custom_language() -> None:
    """reimplement_algorithm prompt 支持自定义语言。"""
    srv = _make_server()
    rendered = srv.render_prompt(
        "reimplement_algorithm", {"code": "fn()", "language": "go"}
    )
    assert "go" in rendered


def test_render_prompt_unknown_returns_message() -> None:
    """未知 prompt 名返回 unknown prompt 提示。"""
    srv = _make_server()
    rendered = srv.render_prompt("nope", {})
    assert "unknown prompt" in rendered
    assert "nope" in rendered


# -- read_resource ----------------------------------------------------------


def test_read_resource_agent_state() -> None:
    """agent://state 返回 provider/model/状态信息。"""
    provider = MagicMock()
    provider.name = "deepseek"
    provider.model = "m1"
    srv = _make_server(provider=provider, agent=MagicMock())
    parsed = json.loads(srv.read_resource("agent://state"))
    assert parsed["has_reverse_agent"] is True
    assert parsed["provider"] == "deepseek"
    assert parsed["model"] == "m1"
    assert parsed["browser_reused"] is False
    assert parsed["closed"] is False


def test_read_resource_agent_state_no_provider_attrs() -> None:
    """provider 无 name/model 属性时回退到 str/provider。"""
    provider = "plain-string"
    srv = _make_server(provider=provider)
    parsed = json.loads(srv.read_resource("agent://state"))
    assert parsed["has_reverse_agent"] is False
    assert parsed["provider"] == "plain-string"


def test_read_resource_agent_history() -> None:
    """agent://history 返回空历史占位。"""
    srv = _make_server()
    parsed = json.loads(srv.read_resource("agent://history"))
    assert parsed["history"] == []
    assert "note" in parsed


def test_read_resource_hooks_library() -> None:
    """hooks://library 返回 HookLibrary.names()。"""
    srv = _make_server()
    parsed = json.loads(srv.read_resource("hooks://library"))
    assert "hooks" in parsed
    assert isinstance(parsed["hooks"], list)


def test_read_resource_extracted_params_schema_available() -> None:
    """schema://extracted_params 在 pydantic 可用时返回 JSON Schema。"""
    srv = _make_server()
    result = srv.read_resource("schema://extracted_params")
    parsed = json.loads(result)
    # pydantic 已安装时返回 JSON Schema（含 properties/title）；否则返回 note
    assert "properties" in parsed or "note" in parsed


def test_read_resource_extracted_params_schema_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """schema 调用异常时返回错误 JSON。"""
    srv = _make_server()
    import web_crawler.ai.schema as schema_mod

    # 让 ExtractedParams.model_json_schema() 抛异常，触发 except 分支
    fake = MagicMock()
    fake.model_json_schema.side_effect = RuntimeError("schema boom")
    monkeypatch.setattr(schema_mod, "ExtractedParams", fake)
    parsed = json.loads(srv.read_resource("schema://extracted_params"))
    assert "error" in parsed
    assert "schema unavailable" in parsed["error"]


def test_read_resource_unknown_uri() -> None:
    """未知 uri 返回错误响应。"""
    srv = _make_server()
    parsed = json.loads(srv.read_resource("bogus://nowhere"))
    assert "error" in parsed
    assert "bogus://nowhere" in parsed["error"]


# -- progress ---------------------------------------------------------------


def test_make_progress_token_format() -> None:
    """progress token 包含 progressToken 与 total。"""
    srv = _make_server()
    token = srv.make_progress_token("reverse", total=5)
    assert token["progressToken"].startswith("reverse-")
    assert token["total"] == 5


def test_report_progress_writes_to_stderr_without_mcp(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_HAS_MCP 为 False 时 report_progress 写 stderr。"""
    monkeypatch.setattr(server_module, "_HAS_MCP", False)
    srv = _make_server()
    srv.report_progress("tok-1", 2, 5, message="halfway")
    captured = capsys.readouterr()
    assert "[progress] tok-1: 2/5" in captured.err
    assert "halfway" in captured.err


def test_report_progress_writes_to_stderr_with_mcp(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_HAS_MCP 为 True 时 report_progress 仍写 stderr（mcp 上下文外无真正推送）。"""
    monkeypatch.setattr(server_module, "_HAS_MCP", True)
    srv = _make_server()
    srv.report_progress("tok-2", 1, 3)
    captured = capsys.readouterr()
    assert "[progress] tok-2: 1/3" in captured.err


# -- handle_tool 分发与错误处理 ----------------------------------------------


def test_handle_tool_unknown_tool() -> None:
    """未知工具名返回 unknown tool 错误。"""
    srv = _make_server()
    parsed = json.loads(srv.handle_tool("nope", {}))
    assert "unknown tool" in parsed["error"]


def test_handle_tool_passes_empty_args() -> None:
    """handle_tool 在 args 为 None 时传空 dict 给 handler。"""
    srv = _make_server()
    # reverse_engineer_url 缺 url 会 KeyError，被兜底捕获
    parsed = json.loads(srv.handle_tool("reverse_engineer_url", None))  # type: ignore[arg-type]
    assert "error" in parsed


def test_handle_tool_catches_timeout_error() -> None:
    """handler 抛 TimeoutError 时返回 timeout 错误。"""
    srv = _make_server()

    def _boom(args: dict) -> str:
        raise TimeoutError("too slow")

    with patch.object(srv, "_tool_deobfuscate_js", _boom):
        parsed = json.loads(srv.handle_tool("deobfuscate_js", {"code": "x"}))
    assert parsed["error"] == "timeout"


def test_handle_tool_catches_import_error() -> None:
    """handler 抛 ImportError 时返回 dependency missing 错误。"""
    srv = _make_server()

    def _boom(args: dict) -> str:
        raise ImportError("no module")

    with patch.object(srv, "_tool_deobfuscate_js", _boom):
        parsed = json.loads(srv.handle_tool("deobfuscate_js", {"code": "x"}))
    assert parsed["error"] == "dependency missing"


def test_handle_tool_catches_runtime_error() -> None:
    """handler 抛 RuntimeError 时返回 runtime error 错误。"""
    srv = _make_server()

    def _boom(args: dict) -> str:
        raise RuntimeError("llm down")

    with patch.object(srv, "_tool_deobfuscate_js", _boom):
        parsed = json.loads(srv.handle_tool("deobfuscate_js", {"code": "x"}))
    assert parsed["error"] == "runtime error"


def test_handle_tool_catches_generic_exception() -> None:
    """handler 抛其他异常时返回带 traceback 的错误。"""
    srv = _make_server()

    def _boom(args: dict) -> str:
        raise ValueError("bad value")

    with patch.object(srv, "_tool_deobfuscate_js", _boom):
        parsed = json.loads(srv.handle_tool("deobfuscate_js", {"code": "x"}))
    assert parsed["error"] == "bad value"
    assert "traceback" in parsed


# -- _tool_reverse_engineer_url ---------------------------------------------


class _FakeAgentConfig:
    """模拟 ReverseAgentConfig 的配置对象。"""

    hooks = ["fetch_hook"]
    headless = True
    wait_after_navigate = 2.0
    proxy = None
    os_name = "windows"


class _FakeAgent:
    """模拟 ReverseAgent：type() 返回 _FakeAgent 类本身，便于 type(self.agent)(...) 构造。"""

    def __init__(
        self,
        *,
        config: Any = None,
        provider: Any = None,
        analyzer: Any = None,
        event_bus: Any = None,
    ) -> None:
        self.config = config or _FakeAgentConfig()
        self.provider = provider
        self.analyzer = analyzer
        self.run_called = False
        self.closed = False

    def run(self, url: str, task: str = "") -> dict:
        self.run_called = True
        return {"success": True, "steps": 3}

    def close(self) -> None:
        self.closed = True


class _FakeAgentCrash(_FakeAgent):
    """run() 抛异常的 FakeAgent 变体。"""

    def run(self, url: str, task: str = "") -> dict:
        raise RuntimeError("agent crashed")


def test_tool_reverse_engineer_url_with_agent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent 可用时走完整 ReverseAgent 流程，返回 agent=True。"""
    monkeypatch.setattr(server_module, "_HAS_REVERSE_AGENT", True)

    base_agent = _FakeAgent(
        config=_FakeAgentConfig(), provider=MagicMock(), analyzer=MagicMock()
    )
    srv = _make_server(agent=base_agent)

    monkeypatch.setattr(
        "web_crawler.ai.reverse_agent.ReverseAgentConfig",
        lambda **kw: MagicMock(**kw),
    )

    class _FakeEventBus:
        def __init__(self) -> None:
            self.subscribers: list[Any] = []

        def subscribe(self, fn: Any) -> None:
            self.subscribers.append(fn)

    monkeypatch.setattr("web_crawler.ai.watchdog.EventBus", _FakeEventBus)

    parsed = json.loads(
        srv._tool_reverse_engineer_url({"url": "http://x", "max_steps": 5})
    )
    assert parsed["agent"] is True
    assert parsed["result"]["success"] is True


def test_tool_reverse_engineer_url_agent_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent 执行抛异常时降级为基本采集。"""
    monkeypatch.setattr(server_module, "_HAS_REVERSE_AGENT", True)

    base_agent = _FakeAgentCrash(
        config=_FakeAgentConfig(), provider=MagicMock(), analyzer=MagicMock()
    )
    srv = _make_server(agent=base_agent)
    monkeypatch.setattr(
        "web_crawler.ai.reverse_agent.ReverseAgentConfig",
        lambda **kw: MagicMock(**kw),
    )

    class _FakeEventBus:
        def __init__(self) -> None:
            self.subscribers: list[Any] = []

        def subscribe(self, fn: Any) -> None:
            self.subscribers.append(fn)

    monkeypatch.setattr("web_crawler.ai.watchdog.EventBus", _FakeEventBus)

    # mock _run_browser_task 让降级路径成功
    collected = {"hook_records": [], "hook_count": 0, "scripts": []}
    with patch.object(srv, "_run_browser_task", return_value=collected):
        parsed = json.loads(srv._tool_reverse_engineer_url({"url": "http://x"}))
    assert parsed["agent"] is False
    assert "降级" in parsed["note"]


def test_tool_reverse_engineer_url_no_agent_basic_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent 为 None 时走基本采集路径。"""
    srv = _make_server(agent=None)
    collected = {
        "hook_records": [{"type": "fetch", "url": "http://x/api"}],
        "hook_count": 1,
        "scripts": ["http://x/a.js"],
    }
    with patch.object(srv, "_run_browser_task", return_value=collected) as mock_run:
        parsed = json.loads(
            srv._tool_reverse_engineer_url(
                {"url": "http://x", "target_params": ["Anti-Content"]}
            )
        )
    assert parsed["agent"] is False
    assert "不可用" in parsed["note"]
    assert parsed["url"] == "http://x"
    assert parsed["target_params"] == ["Anti-Content"]
    assert parsed["hook_count"] == 1
    mock_run.assert_called_once()


def test_tool_reverse_engineer_url_browser_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 agent 且浏览器任务失败时返回错误。"""
    srv = _make_server(agent=None)
    with patch.object(srv, "_run_browser_task", side_effect=RuntimeError("browser dead")):
        parsed = json.loads(srv._tool_reverse_engineer_url({"url": "http://x"}))
    assert "error" in parsed
    assert "浏览器操作失败" in parsed["error"]


# -- _tool_inject_hooks -----------------------------------------------------


def test_tool_inject_hooks_success() -> None:
    """成功注入 Hook 时返回 injected/preview 信息。"""
    srv = _make_server(agent=None)
    preview = [{"type": "fetch", "url": "http://x/api"}]
    with patch.object(srv, "_run_browser_task", return_value=preview):
        parsed = json.loads(
            srv._tool_inject_hooks({"url": "http://x", "hooks": ["fetch_hook"]})
        )
    assert parsed["injected"] == ["fetch_hook"]
    assert parsed["invalid_hooks"] == []
    assert parsed["preview_count"] == 1


def test_tool_inject_hooks_filters_invalid_names() -> None:
    """未知 hook 名称被剔除并记录到 invalid_hooks。"""
    srv = _make_server(agent=None)
    with patch.object(srv, "_run_browser_task", return_value=[]):
        parsed = json.loads(
            srv._tool_inject_hooks({"url": "http://x", "hooks": ["fetch_hook", "bogus"]})
        )
    assert "fetch_hook" in parsed["injected"]
    assert "bogus" in parsed["invalid_hooks"]


def test_tool_inject_hooks_no_valid_hooks() -> None:
    """全部 hook 名称无效时返回错误。"""
    srv = _make_server(agent=None)
    parsed = json.loads(
        srv._tool_inject_hooks({"url": "http://x", "hooks": ["bogus1", "bogus2"]})
    )
    assert "error" in parsed
    assert "no valid hooks" in parsed["error"]


def test_tool_inject_hooks_browser_error() -> None:
    """浏览器任务失败时返回错误。"""
    srv = _make_server(agent=None)
    with patch.object(srv, "_run_browser_task", side_effect=RuntimeError("boom")):
        parsed = json.loads(srv._tool_inject_hooks({"url": "http://x"}))
    assert "error" in parsed


# -- _tool_analyze_js_code --------------------------------------------------


def test_tool_analyze_js_code_success() -> None:
    """成功分析 JS 代码时返回 algorithm/inputs 等字段。"""
    analyzer = MagicMock()
    analyzer.analyze_fragment.return_value = MagicMock(
        algorithm="MD5",
        inputs=["timestamp"],
        output="hash",
        code_flow="step1",
        confidence=0.9,
        deobfuscated="clean",
    )
    srv = _make_server(analyzer=analyzer)
    parsed = json.loads(srv._tool_analyze_js_code({"code": "function() {}"}))
    assert parsed["algorithm"] == "MD5"
    assert parsed["inputs"] == ["timestamp"]
    assert parsed["confidence"] == 0.9
    assert "target_param" not in parsed


def test_tool_analyze_js_code_with_target_param() -> None:
    """带 target_param 时结果包含 target_param 字段。"""
    analyzer = MagicMock()
    analyzer.analyze_fragment.return_value = MagicMock(
        algorithm="AES", inputs=[], output="", code_flow="", confidence=0.5, deobfuscated=""
    )
    srv = _make_server(analyzer=analyzer)
    parsed = json.loads(
        srv._tool_analyze_js_code({"code": "x", "target_param": "sign"})
    )
    assert parsed["target_param"] == "sign"


def test_tool_analyze_js_code_llm_error() -> None:
    """analyzer.analyze_fragment 抛异常时返回 LLM call failed。"""
    analyzer = MagicMock()
    analyzer.analyze_fragment.side_effect = RuntimeError("llm down")
    srv = _make_server(analyzer=analyzer)
    parsed = json.loads(srv._tool_analyze_js_code({"code": "x"}))
    assert parsed["error"] == "LLM call failed"


# -- _tool_extract_webpack_modules ------------------------------------------


def test_tool_extract_webpack_modules() -> None:
    """提取 webpack 模块返回 modules/count/entry_point。"""
    analyzer = MagicMock()
    module = MagicMock(id=100, dependencies=[200], exports="sign", source="abc" * 200)
    analyzer.extract_webpack_modules.return_value = [module]
    analyzer.identify_entry_point.return_value = 100
    srv = _make_server(analyzer=analyzer)
    parsed = json.loads(srv._tool_extract_webpack_modules({"source": "bundle"}))
    assert parsed["count"] == 1
    assert parsed["entry_point"] == 100
    assert parsed["modules"][0]["id"] == 100
    assert parsed["modules"][0]["source_length"] == 600


# -- _tool_deobfuscate_js ---------------------------------------------------


def test_tool_deobfuscate_js_success() -> None:
    """成功反混淆返回 deobfuscated 与 length。"""
    analyzer = MagicMock()
    analyzer.deobfuscate.return_value = "var x = 1;"
    srv = _make_server(analyzer=analyzer)
    parsed = json.loads(srv._tool_deobfuscate_js({"code": "var x=1"}))
    assert parsed["deobfuscated"] == "var x = 1;"
    assert parsed["length"] == 10


def test_tool_deobfuscate_js_error() -> None:
    """反混淆抛异常时返回 LLM call failed。"""
    analyzer = MagicMock()
    analyzer.deobfuscate.side_effect = RuntimeError("boom")
    srv = _make_server(analyzer=analyzer)
    parsed = json.loads(srv._tool_deobfuscate_js({"code": "x"}))
    assert parsed["error"] == "LLM call failed"


# -- _tool_reimplement_algorithm --------------------------------------------


def test_tool_reimplement_algorithm_success() -> None:
    """成功重写返回 language/code/length。"""
    analyzer = MagicMock()
    analyzer.suggest_reimplementation.return_value = "print('hi')"
    srv = _make_server(analyzer=analyzer)
    parsed = json.loads(
        srv._tool_reimplement_algorithm({"code": "fn()", "language": "go"})
    )
    assert parsed["language"] == "go"
    assert parsed["code"] == "print('hi')"
    assert parsed["length"] == len("print('hi')")


def test_tool_reimplement_algorithm_default_language() -> None:
    """未指定 language 时默认 python。"""
    analyzer = MagicMock()
    analyzer.suggest_reimplementation.return_value = "print('hi')"
    srv = _make_server(analyzer=analyzer)
    parsed = json.loads(srv._tool_reimplement_algorithm({"code": "fn()"}))
    assert parsed["language"] == "python"


def test_tool_reimplement_algorithm_error() -> None:
    """重写抛异常时返回 LLM call failed。"""
    analyzer = MagicMock()
    analyzer.suggest_reimplementation.side_effect = RuntimeError("boom")
    srv = _make_server(analyzer=analyzer)
    parsed = json.loads(srv._tool_reimplement_algorithm({"code": "x"}))
    assert parsed["error"] == "LLM call failed"


# -- _tool_solve_captcha ----------------------------------------------------


def test_tool_solve_captcha_none_detected() -> None:
    """未检测到验证码时返回 type=none/solved=True。"""
    captcha_manager = MagicMock()
    captcha_manager.detector.detect.return_value = None
    srv = _make_server(captcha_manager=captcha_manager, agent=None)
    with patch.object(srv, "_run_browser_task", return_value=None) as mock_run:
        # _run_browser_task 把 task_fn 的返回值透传；task_fn 内部调用 detect
        # 由于 task_fn 在 _run_browser_task 内执行，需让 mock 真正调用 task_fn
        def _execute(url: str, task_fn: Any, **kw: Any) -> Any:
            return task_fn(MagicMock())

        mock_run.side_effect = _execute
        parsed = json.loads(srv._tool_solve_captcha({"url": "http://x"}))
    assert parsed["type"] == "none"
    assert parsed["solved"] is True


def test_tool_solve_captcha_detected_and_solved() -> None:
    """检测到验证码并处理成功时返回 type/solved=True。"""
    from web_crawler.ai.captcha import CaptchaInfo, CaptchaType

    captcha_manager = MagicMock()
    info = CaptchaInfo(type=CaptchaType.TURNSTILE, iframe_url="http://x", site_key="k")
    captcha_manager.detector.detect.return_value = info
    captcha_manager.solver.solve.return_value = True
    srv = _make_server(captcha_manager=captcha_manager, agent=None)

    def _execute(url: str, task_fn: Any, **kw: Any) -> Any:
        return task_fn(MagicMock())

    with patch.object(srv, "_run_browser_task", side_effect=_execute):
        parsed = json.loads(srv._tool_solve_captcha({"url": "http://x"}))
    assert parsed["type"] == "turnstile"
    assert parsed["solved"] is True
    assert parsed["message"] == "已处理"


def test_tool_solve_captcha_detected_not_solved() -> None:
    """检测到验证码但处理失败时返回 solved=False/需要人工介入。"""
    from web_crawler.ai.captcha import CaptchaInfo, CaptchaType

    captcha_manager = MagicMock()
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA)
    captcha_manager.detector.detect.return_value = info
    captcha_manager.solver.solve.return_value = False
    srv = _make_server(captcha_manager=captcha_manager, agent=None)

    def _execute(url: str, task_fn: Any, **kw: Any) -> Any:
        return task_fn(MagicMock())

    with patch.object(srv, "_run_browser_task", side_effect=_execute):
        parsed = json.loads(srv._tool_solve_captcha({"url": "http://x"}))
    assert parsed["solved"] is False
    assert parsed["message"] == "需要人工介入"


def test_tool_solve_captcha_browser_error() -> None:
    """浏览器任务失败时返回错误。"""
    srv = _make_server(agent=None)
    with patch.object(srv, "_run_browser_task", side_effect=RuntimeError("boom")):
        parsed = json.loads(srv._tool_solve_captcha({"url": "http://x"}))
    assert "error" in parsed


# -- _tool_solve_captcha_image ----------------------------------------------


def _patch_image_captcha(monkeypatch: pytest.MonkeyPatch, solver: Any) -> None:
    """patch image_captcha 模块的 ImageCaptchaSolver 与 ImageSolverConfig。"""
    monkeypatch.setattr(
        "web_crawler.ai.image_captcha.ImageCaptchaSolver", lambda **kw: solver
    )
    monkeypatch.setattr("web_crawler.ai.image_captcha.ImageSolverConfig", lambda: MagicMock())


def test_tool_solve_captcha_image_invalid_mode() -> None:
    """非法 mode 返回错误。"""
    srv = _make_server()
    parsed = json.loads(srv._tool_solve_captcha_image({"mode": "bogus"}))
    assert "error" in parsed
    assert "text / slider / click" in parsed["error"]


def test_tool_solve_captcha_image_text_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """text 模式成功识别返回 text 字段。"""
    solver = MagicMock()
    solver.solve_text.return_value = "abc123"
    _patch_image_captcha(monkeypatch, solver)
    srv = _make_server()
    parsed = json.loads(
        srv._tool_solve_captcha_image({"mode": "text", "image": "base64data"})
    )
    assert parsed["mode"] == "text"
    assert parsed["text"] == "abc123"
    assert parsed["ok"] is True


def test_tool_solve_captcha_image_text_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """text 模式识别为空时 ok=False。"""
    solver = MagicMock()
    solver.solve_text.return_value = ""
    _patch_image_captcha(monkeypatch, solver)
    srv = _make_server()
    parsed = json.loads(
        srv._tool_solve_captcha_image({"mode": "text", "image": "base64data"})
    )
    assert parsed["ok"] is False


def test_tool_solve_captcha_image_text_no_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """text 模式缺 image 参数返回错误。"""
    _patch_image_captcha(monkeypatch, MagicMock())
    srv = _make_server()
    parsed = json.loads(srv._tool_solve_captcha_image({"mode": "text"}))
    assert "error" in parsed
    assert "image is required" in parsed["error"]


def test_tool_solve_captcha_image_slider_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """slider 模式成功识别返回 x/y 坐标。"""
    solver = MagicMock()
    solver.solve_slider.return_value = MagicMock(x=120, y=0, method="llm", confidence=0.9)
    _patch_image_captcha(monkeypatch, solver)
    srv = _make_server()
    parsed = json.loads(
        srv._tool_solve_captcha_image(
            {"mode": "slider", "bg": "b64", "slider": "s64"}
        )
    )
    assert parsed["mode"] == "slider"
    assert parsed["ok"] is True
    assert parsed["x"] == 120
    assert parsed["method"] == "llm"


def test_tool_solve_captcha_image_slider_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """slider 模式识别失败返回 ok=False。"""
    solver = MagicMock()
    solver.solve_slider.return_value = None
    _patch_image_captcha(monkeypatch, solver)
    srv = _make_server()
    parsed = json.loads(
        srv._tool_solve_captcha_image(
            {"mode": "slider", "bg": "b64", "slider": "s64"}
        )
    )
    assert parsed["ok"] is False
    assert parsed["message"] == "识别失败"


def test_tool_solve_captcha_image_slider_missing_bg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """slider 模式缺 bg/slider 参数返回错误。"""
    _patch_image_captcha(monkeypatch, MagicMock())
    srv = _make_server()
    parsed = json.loads(srv._tool_solve_captcha_image({"mode": "slider", "bg": ""}))
    assert "error" in parsed
    assert "bg and slider" in parsed["error"]


def test_tool_solve_captcha_image_click_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """click 模式成功识别返回 points/labels。"""
    solver = MagicMock()
    solver.solve_click.return_value = MagicMock(
        points=[(10, 20), (30, 40)], labels=["A", "B"], method="llm"
    )
    _patch_image_captcha(monkeypatch, solver)
    srv = _make_server()
    parsed = json.loads(
        srv._tool_solve_captcha_image(
            {"mode": "click", "image": "b64", "prompt": "点击红绿灯"}
        )
    )
    assert parsed["mode"] == "click"
    assert parsed["ok"] is True
    assert parsed["points"] == [{"x": 10, "y": 20}, {"x": 30, "y": 40}]
    assert parsed["labels"] == ["A", "B"]


def test_tool_solve_captcha_image_click_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """click 模式识别失败返回 ok=False。"""
    solver = MagicMock()
    solver.solve_click.return_value = None
    _patch_image_captcha(monkeypatch, solver)
    srv = _make_server()
    parsed = json.loads(
        srv._tool_solve_captcha_image({"mode": "click", "image": "b64", "prompt": "x"})
    )
    assert parsed["ok"] is False


def test_tool_solve_captcha_image_click_no_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """click 模式缺 image 参数返回错误。"""
    _patch_image_captcha(monkeypatch, MagicMock())
    srv = _make_server()
    parsed = json.loads(srv._tool_solve_captcha_image({"mode": "click"}))
    assert "error" in parsed
    assert "image is required" in parsed["error"]


def test_tool_solve_captcha_image_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """solver 内部抛异常时返回 captcha image solve failed。"""
    solver = MagicMock()
    solver.solve_text.side_effect = RuntimeError("vision down")
    _patch_image_captcha(monkeypatch, solver)
    srv = _make_server()
    parsed = json.loads(
        srv._tool_solve_captcha_image({"mode": "text", "image": "b64"})
    )
    assert parsed["error"] == "captcha image solve failed"


# -- _tool_pentest_recon ----------------------------------------------------


def _patch_pentest_modules(
    monkeypatch: pytest.MonkeyPatch,
    *,
    port_scan: list | None = None,
    dir_brute: list | None = None,
    subdomains: list | None = None,
    vulns: list | None = None,
    headers: Any = None,
) -> None:
    """patch pentest 子模块，返回 mock 结果。headers 需为真实 dataclass 实例。"""
    from web_crawler.pentest import HeaderCheckResult

    fake_port_scanner = MagicMock()
    fake_port_scanner.scan.return_value = port_scan or []
    fake_dir_bruter = MagicMock()
    fake_dir_bruter.__enter__ = MagicMock(return_value=fake_dir_bruter)
    fake_dir_bruter.__exit__ = MagicMock(return_value=False)
    fake_dir_bruter.brute.return_value = dir_brute or []
    fake_subdomain = MagicMock()
    fake_subdomain.enumerate.return_value = subdomains or []
    fake_vuln = MagicMock()
    fake_vuln.__enter__ = MagicMock(return_value=fake_vuln)
    fake_vuln.__exit__ = MagicMock(return_value=False)
    fake_vuln.scan_url.return_value = vulns or []
    fake_header = MagicMock()
    fake_header.__enter__ = MagicMock(return_value=fake_header)
    fake_header.__exit__ = MagicMock(return_value=False)
    fake_header.check.return_value = headers or HeaderCheckResult(
        url="http://x", score=0, grade="F"
    )

    monkeypatch.setattr("web_crawler.pentest.PortScanner", fake_port_scanner)
    monkeypatch.setattr("web_crawler.pentest.DirBruter", lambda: fake_dir_bruter)
    monkeypatch.setattr(
        "web_crawler.pentest.SubdomainEnumerator", lambda: fake_subdomain
    )
    monkeypatch.setattr("web_crawler.pentest.VulnScanner", lambda: fake_vuln)
    monkeypatch.setattr("web_crawler.pentest.HeaderChecker", lambda: fake_header)


def test_tool_pentest_recon_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功执行渗透侦察返回 to_dict 结果。"""
    _patch_pentest_modules(monkeypatch)
    srv = _make_server()
    parsed = json.loads(
        srv._tool_pentest_recon({"target": "example.com", "checks": ["ports"]})
    )
    assert "target" in parsed
    assert parsed["target"] == "example.com"


def test_tool_pentest_recon_url_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """target 为 URL 时正确提取 host 与 base_url。"""
    _patch_pentest_modules(monkeypatch)
    srv = _make_server()
    parsed = json.loads(
        srv._tool_pentest_recon(
            {"target": "https://example.com/", "checks": ["headers"]}
        )
    )
    assert "target" in parsed


def test_tool_pentest_recon_no_target() -> None:
    """target 为空时返回错误。"""
    srv = _make_server()
    parsed = json.loads(srv._tool_pentest_recon({"target": ""}))
    assert "error" in parsed
    assert "target is required" in parsed["error"]


def test_tool_pentest_recon_unknown_checks() -> None:
    """未知 check 名称返回错误。"""
    srv = _make_server()
    parsed = json.loads(
        srv._tool_pentest_recon({"target": "x", "checks": ["bogus"]})
    )
    assert "error" in parsed
    assert "unknown check names" in parsed["error"]


def test_tool_pentest_recon_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """pentest 内部抛异常时返回 pentest recon failed。"""
    fake_port_scanner = MagicMock()
    fake_port_scanner.return_value.scan.side_effect = RuntimeError("network down")
    monkeypatch.setattr("web_crawler.pentest.PortScanner", fake_port_scanner)
    srv = _make_server()
    parsed = json.loads(
        srv._tool_pentest_recon({"target": "x", "checks": ["ports"]})
    )
    assert parsed["error"] == "pentest recon failed"


def test_tool_pentest_recon_custom_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """自定义端口列表透传给 PortScanner.scan。"""
    fake_port_scanner = MagicMock()
    fake_port_scanner.return_value.scan.return_value = []
    monkeypatch.setattr("web_crawler.pentest.PortScanner", fake_port_scanner)
    srv = _make_server()
    srv._tool_pentest_recon(
        {"target": "x", "checks": ["ports"], "ports": [22, 80]}
    )
    fake_port_scanner.return_value.scan.assert_called_once_with("x", [22, 80])


def test_tool_pentest_recon_timeout_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """timeout 超出范围时被 clamp 到 [1, 300]。"""
    _patch_pentest_modules(monkeypatch)
    srv = _make_server()
    # 超大 timeout 不应报错（被 clamp 到 300）
    parsed = json.loads(
        srv._tool_pentest_recon({"target": "x", "checks": ["ports"], "timeout": 999})
    )
    assert "error" not in parsed


# -- _tool_capture_network_requests ------------------------------------------


def test_tool_capture_network_requests_success() -> None:
    """成功捕获网络请求返回 requests/count。"""
    srv = _make_server(agent=None)
    records = [{"type": "fetch", "url": "http://x/api"}]
    with patch.object(srv, "_run_browser_task", return_value=records):
        parsed = json.loads(
            srv._tool_capture_network_requests({"url": "http://x", "wait_time": 2.0})
        )
    assert parsed["count"] == 1
    assert parsed["requests"] == records


def test_tool_capture_network_requests_browser_error() -> None:
    """浏览器任务失败时返回错误。"""
    srv = _make_server(agent=None)
    with patch.object(srv, "_run_browser_task", side_effect=RuntimeError("boom")):
        parsed = json.loads(srv._tool_capture_network_requests({"url": "http://x"}))
    assert "error" in parsed


# -- _tool_get_page_scripts -------------------------------------------------


def test_tool_get_page_scripts_success() -> None:
    """成功获取脚本列表返回 scripts/count。"""
    srv = _make_server(agent=None)
    scripts = [{"src": "http://x/a.js", "type": "", "async": False, "defer": False}]
    with patch.object(srv, "_run_browser_task", return_value=scripts):
        parsed = json.loads(srv._tool_get_page_scripts({"url": "http://x"}))
    assert parsed["count"] == 1
    assert parsed["scripts"] == scripts


def test_tool_get_page_scripts_browser_error() -> None:
    """浏览器任务失败时返回错误。"""
    srv = _make_server(agent=None)
    with patch.object(srv, "_run_browser_task", side_effect=RuntimeError("boom")):
        parsed = json.loads(srv._tool_get_page_scripts({"url": "http://x"}))
    assert "error" in parsed


# -- _handle_jsonrpc 协议 ---------------------------------------------------


def test_handle_jsonrpc_notification_returns_none() -> None:
    """无 id 的通知返回 None（不回复）。"""
    srv = _make_server()
    assert srv._handle_jsonrpc("initialize", {}, None) is None


def test_handle_jsonrpc_initialize() -> None:
    """initialize 返回协议版本与 serverInfo。"""
    srv = _make_server()
    resp = srv._handle_jsonrpc("initialize", {}, 1)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == server_module._PROTOCOL_VERSION
    assert resp["result"]["serverInfo"]["name"] == server_module._SERVER_NAME


def test_handle_jsonrpc_tools_list() -> None:
    """tools/list 返回工具列表。"""
    srv = _make_server()
    resp = srv._handle_jsonrpc("tools/list", {}, 2)
    assert "tools" in resp["result"]
    assert len(resp["result"]["tools"]) >= 10


def test_handle_jsonrpc_tools_call_success() -> None:
    """tools/call 成功时返回 content 与 isError=False。"""
    srv = _make_server()
    analyzer = MagicMock()
    analyzer.deobfuscate.return_value = "clean"
    srv.analyzer = analyzer
    resp = srv._handle_jsonrpc(
        "tools/call",
        {"name": "deobfuscate_js", "arguments": {"code": "x"}},
        3,
    )
    assert resp["result"]["isError"] is False
    assert resp["result"]["content"][0]["type"] == "text"


def test_handle_jsonrpc_tools_call_error_result() -> None:
    """tools/call 返回错误 JSON 时 isError=True。"""
    srv = _make_server()
    resp = srv._handle_jsonrpc(
        "tools/call",
        {"name": "deobfuscate_js", "arguments": {}},  # 缺 code，handler 抛异常
        4,
    )
    assert resp["result"]["isError"] is True


def test_handle_jsonrpc_tools_call_invalid_name_param() -> None:
    """tools/call 缺 name 参数返回 -32602 错误。"""
    srv = _make_server()
    resp = srv._handle_jsonrpc("tools/call", {}, 5)
    assert resp["error"]["code"] == -32602


def test_handle_jsonrpc_tools_call_non_string_name() -> None:
    """tools/call 的 name 非字符串返回 -32602 错误。"""
    srv = _make_server()
    resp = srv._handle_jsonrpc("tools/call", {"name": 123}, 6)
    assert resp["error"]["code"] == -32602


def test_handle_jsonrpc_prompts_list() -> None:
    """prompts/list 返回 prompt 列表。"""
    srv = _make_server()
    resp = srv._handle_jsonrpc("prompts/list", {}, 7)
    assert "prompts" in resp["result"]
    assert len(resp["result"]["prompts"]) == 3


def test_handle_jsonrpc_prompts_get_success() -> None:
    """prompts/get 成功渲染返回 messages。"""
    srv = _make_server()
    resp = srv._handle_jsonrpc(
        "prompts/get", {"name": "deobfuscate_js", "arguments": {"code": "x"}}, 8
    )
    assert "messages" in resp["result"]
    assert resp["result"]["messages"][0]["role"] == "user"


def test_handle_jsonrpc_prompts_get_invalid_name() -> None:
    """prompts/get 缺 name 返回 -32602 错误。"""
    srv = _make_server()
    resp = srv._handle_jsonrpc("prompts/get", {}, 9)
    assert resp["error"]["code"] == -32602


def test_handle_jsonrpc_resources_list() -> None:
    """resources/list 返回资源列表。"""
    srv = _make_server()
    resp = srv._handle_jsonrpc("resources/list", {}, 10)
    assert "resources" in resp["result"]
    assert len(resp["result"]["resources"]) == 4


def test_handle_jsonrpc_resources_read_success() -> None:
    """resources/read 成功返回 contents。"""
    srv = _make_server()
    resp = srv._handle_jsonrpc("resources/read", {"uri": "agent://state"}, 11)
    assert "contents" in resp["result"]
    assert resp["result"]["contents"][0]["uri"] == "agent://state"


def test_handle_jsonrpc_resources_read_invalid_uri() -> None:
    """resources/read 缺 uri 返回 -32602 错误。"""
    srv = _make_server()
    resp = srv._handle_jsonrpc("resources/read", {}, 12)
    assert resp["error"]["code"] == -32602


def test_handle_jsonrpc_method_not_found() -> None:
    """未知 method 返回 -32601 错误。"""
    srv = _make_server()
    resp = srv._handle_jsonrpc("bogus/method", {}, 13)
    assert resp["error"]["code"] == -32601
    assert "bogus/method" in resp["error"]["message"]


# -- _run_stdio_manual ------------------------------------------------------


def test_run_stdio_manual_handles_initialize(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """stdio 手动模式正确处理 initialize 请求并输出 JSON-RPC 响应。"""
    monkeypatch.setattr(server_module, "_HAS_MCP", False)
    srv = _make_server()
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    monkeypatch.setattr("sys.stdin", io.StringIO(request + "\n"))
    srv._run_stdio_manual()
    captured = capsys.readouterr()
    resp = json.loads(captured.out.strip())
    assert resp["id"] == 1
    assert resp["result"]["serverInfo"]["name"] == server_module._SERVER_NAME


def test_run_stdio_manual_parse_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """非法 JSON 返回 Parse error（id=null）。"""
    monkeypatch.setattr(server_module, "_HAS_MCP", False)
    srv = _make_server()
    monkeypatch.setattr("sys.stdin", io.StringIO("not json\n"))
    srv._run_stdio_manual()
    captured = capsys.readouterr()
    resp = json.loads(captured.out.strip())
    assert resp["error"]["code"] == -32700
    assert resp["id"] is None


def test_run_stdio_manual_skips_empty_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """空行被跳过，不产生输出。"""
    monkeypatch.setattr(server_module, "_HAS_MCP", False)
    srv = _make_server()
    monkeypatch.setattr("sys.stdin", io.StringIO("\n   \n"))
    srv._run_stdio_manual()
    captured = capsys.readouterr()
    assert captured.out == ""


def test_run_stdio_manual_skips_non_dict_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON 解析为非 dict（如数组）时静默跳过。"""
    monkeypatch.setattr(server_module, "_HAS_MCP", False)
    srv = _make_server()
    monkeypatch.setattr("sys.stdin", io.StringIO("[1, 2, 3]\n"))
    srv._run_stdio_manual()
    captured = capsys.readouterr()
    assert captured.out == ""


def test_run_stdio_manual_notification_no_reply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """无 id 的通知不产生回复。"""
    monkeypatch.setattr(server_module, "_HAS_MCP", False)
    srv = _make_server()
    request = json.dumps({"jsonrpc": "2.0", "method": "initialize"})
    monkeypatch.setattr("sys.stdin", io.StringIO(request + "\n"))
    srv._run_stdio_manual()
    captured = capsys.readouterr()
    assert captured.out == ""


def test_run_stdio_manual_writes_warning_without_mcp(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_HAS_MCP 为 False 时写 stderr 警告。"""
    monkeypatch.setattr(server_module, "_HAS_MCP", False)
    srv = _make_server()
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    srv._run_stdio_manual()
    captured = capsys.readouterr()
    assert "mcp SDK 未安装" in captured.err


# -- run / serve / main -----------------------------------------------------


def test_run_uses_mcp_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """_HAS_MCP 为 True 时 run 走 asyncio _run_mcp 路径。"""
    monkeypatch.setattr(server_module, "_HAS_MCP", True)
    srv = _make_server()
    called = {"async_run": False}

    async def _fake_run_mcp() -> None:
        called["async_run"] = True

    with patch.object(srv, "_run_mcp", _fake_run_mcp):
        srv.run()
    assert called["async_run"] is True


def test_run_uses_manual_when_no_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    """_HAS_MCP 为 False 时 run 走 _run_stdio_manual 路径。"""
    monkeypatch.setattr(server_module, "_HAS_MCP", False)
    srv = _make_server()
    called = {"manual": False}

    def _fake_manual() -> None:
        called["manual"] = True

    with patch.object(srv, "_run_stdio_manual", _fake_manual):
        srv.run()
    assert called["manual"] is True


def test_serve_is_alias_for_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """serve 方法是 run 的别名。"""
    monkeypatch.setattr(server_module, "_HAS_MCP", False)
    srv = _make_server()
    called = {"run": False}

    def _fake_run() -> None:
        called["run"] = True

    with patch.object(srv, "run", _fake_run):
        srv.serve()
    assert called["run"] is True


def test_main_warns_without_api_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """无 DEEPSEEK_API_KEY 时 main 写 stderr 警告并正常退出。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    fake_server = MagicMock()
    fake_server.run = MagicMock()  # 不抛异常
    with patch.object(server_module, "ReverseMCPServer", return_value=fake_server):
        server_module.main()
    captured = capsys.readouterr()
    assert "DEEPSEEK_API_KEY" in captured.err
    fake_server.close.assert_called_once()


def test_main_handles_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run 抛 KeyboardInterrupt 时 main 静默退出并清理。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    fake_server = MagicMock()
    fake_server.run.side_effect = KeyboardInterrupt
    with patch.object(server_module, "ReverseMCPServer", return_value=fake_server):
        server_module.main()  # 不应抛
    fake_server.close.assert_called_once()


# -- _run_mcp（仅验证装饰器注册逻辑不报错） -----------------------------------


def test_run_mcp_registers_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """_run_mcp 在 mcp SDK 可用时注册 tools/prompts/resources handler。

    通过 mock stdio_server 与 Server，验证 _run_mcp 能完整执行而不报错。
    """
    monkeypatch.setattr(server_module, "_HAS_MCP", True)

    # 构造 fake mcp SDK
    fake_types = MagicMock()
    fake_types.Tool = MagicMock()
    fake_types.TextContent = MagicMock(return_value={"type": "text"})
    fake_types.Prompt = MagicMock()
    fake_types.PromptArgument = MagicMock()
    fake_types.GetPromptResult = MagicMock()
    fake_types.PromptMessage = MagicMock()
    fake_types.Resource = MagicMock()
    fake_types.CallToolResult = MagicMock()
    monkeypatch.setattr(server_module, "types", fake_types)

    fake_server_instance = MagicMock()

    class _FakeServer:
        def __init__(self, name: str) -> None:
            self.name = name

        def list_tools(self) -> Any:
            def decorator(fn: Any) -> Any:
                return fn

            return decorator

        def call_tool(self) -> Any:
            def decorator(fn: Any) -> Any:
                return fn

            return decorator

        def list_prompts(self) -> Any:
            def decorator(fn: Any) -> Any:
                return fn

            return decorator

        def get_prompt(self) -> Any:
            def decorator(fn: Any) -> Any:
                return fn

            return decorator

        def list_resources(self) -> Any:
            def decorator(fn: Any) -> Any:
                return fn

            return decorator

        def read_resource(self) -> Any:
            def decorator(fn: Any) -> Any:
                return fn

            return decorator

        def create_initialization_options(self) -> Any:
            return MagicMock()

        async def run(self, *args: Any, **kwargs: Any) -> None:
            fake_server_instance.run_called = True

    monkeypatch.setattr(server_module, "Server", _FakeServer)

    @asynccontextmanager
    async def _fake_stdio_server() -> Any:
        yield (MagicMock(), MagicMock())

    monkeypatch.setattr(server_module, "stdio_server", _fake_stdio_server)

    srv = _make_server()
    import asyncio

    asyncio.run(srv._run_mcp())
    assert fake_server_instance.run_called is True


# -- __init__ 构造（通过 mock 依赖验证） --------------------------------------


def test_server_init_with_mocked_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """通过 mock get_provider / JSAnalyzer / CaptchaManager 验证 __init__ 链路。"""
    fake_provider = MagicMock(name="provider", model="m")
    fake_analyzer = MagicMock(name="analyzer")
    fake_captcha = MagicMock(name="captcha")

    monkeypatch.setattr(
        server_module, "get_provider", lambda name, **kw: fake_provider
    )
    monkeypatch.setattr(
        server_module, "JSAnalyzer", lambda provider, model: fake_analyzer
    )
    monkeypatch.setattr(server_module, "CaptchaManager", lambda: fake_captcha)
    monkeypatch.setattr(server_module, "_HAS_REVERSE_AGENT", False)
    monkeypatch.setattr(server_module, "ReverseAgent", None)
    monkeypatch.setattr(server_module, "ReverseAgentConfig", None)

    srv = ReverseMCPServer(provider_name="deepseek", model="m")
    assert srv.provider is fake_provider
    assert srv.analyzer is fake_analyzer
    assert srv.captcha_manager is fake_captcha
    assert srv.agent is None
    assert srv._fetcher is None
    assert srv._closed is False
    srv.close()

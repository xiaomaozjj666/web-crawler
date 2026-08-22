"""ReverseAgent 单元测试：覆盖 observe/think/act/extract/prompt/cleanup 等分支。

本文件与 test_reverse_agent_browser.py（浏览器交互动作）和
test_reverse_agent_integration.py（截图、Config smoke）互补，重点覆盖：
- 模块级辅助函数：_js_str / _extract_json；
- Action / Observation dataclass 行为；
- _observe / _observe_async：hook_data/scripts/captcha/dom pruner/截图；
- _think / _think_async / _parse_action / _fallback_action；
- _act / _act_async 剩余分支：navigate / inject_hook / analyze_js / wait /
  extract / solve_captcha / done / 未知 action_type；
- _inject_hooks(_async)、_collect_scripts(_async)、_analyze_captured_js；
- _try_extract_param(_async) / _search_param_in_records（headers/url/body/form 各分支）；
- _create_page / _create_page_async / _setup_page_listeners / _try_recover_page(_async)；
- _read_hook_data(_async) / _read_hook_records(_async)；
- _build_think_prompt + _format_* summary 系列；
- _safe_page_url / _screenshot_dir / _screenshot_task_id / checkpoints_snapshot；
- _emit / _on_stall；
- _cleanup_* / close / aclose / __enter__ / __exit__ / __aenter__ / __aexit__。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from web_crawler.ai.captcha import CaptchaType
from web_crawler.ai.guardrails import GuardrailAction, GuardrailResult
from web_crawler.ai.llm import LLMResponse, ProviderCapabilities
from web_crawler.ai.reverse_agent import (
    Action,
    Observation,
    ReverseAgent,
    ReverseAgentConfig,
    _extract_json,
    _js_str,
)

# ---------------------------------------------------------------------------
# 辅助桩对象
# ---------------------------------------------------------------------------


class StubProvider:
    """返回预设回复序列的桩 provider，用于 _think 测试。"""

    model = "stub-model"
    capabilities = ProviderCapabilities()

    def __init__(self, replies: list[str] | None = None) -> None:
        self._replies = list(replies or [])
        self.calls: int = 0
        self.last_messages: list[Any] | None = None

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        self.last_messages = messages
        content = self._replies.pop(0) if self._replies else '{"action_type": "wait"}'
        return LLMResponse(content=content, model=self.model, usage={"tokens": 1})

    async def achat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        return self.chat(messages, **kwargs)


class _SyncPage:
    """模拟 Playwright 同步 Page，覆盖 observe/act 各分支需要的方法。"""

    def __init__(
        self,
        *,
        url: str = "https://example.com",
        title: str = "Example",
        content_html: str = "<html><body>x</body></html>",
        hook_records: list[dict] | None = None,
        scripts: list[str] | None = None,
        screenshot_fail: bool = False,
    ) -> None:
        self._url = url
        self._title = title
        self._content = content_html
        self._hook_records = hook_records if hook_records is not None else []
        self._scripts = scripts if scripts is not None else []
        self._screenshot_fail = screenshot_fail
        self.screenshot_calls: list[dict[str, Any]] = []
        self.goto_calls: list[dict[str, Any]] = []
        self.evaluate_calls: list[str] = []
        self.on_calls: list[tuple[str, Any]] = []

    @property
    def url(self) -> str:
        return self._url

    def title(self) -> str:
        return self._title

    def content(self) -> str:
        return self._content

    def screenshot(self, *, path: str = "", **kwargs: Any) -> bytes:
        self.screenshot_calls.append({"path": path, **kwargs})
        if self._screenshot_fail:
            raise RuntimeError("screenshot failed")
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return b"\x89PNG\r\n\x1a\n"

    def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append({"url": url, **kwargs})
        self._url = url

    def evaluate(self, script: str) -> Any:
        self.evaluate_calls.append(script)
        # 根据 script 内容返回不同结果
        if "__hook_data__" in script:
            return list(self._hook_records)
        if "querySelectorAll('script[src]')" in script:
            return list(self._scripts)
        return None

    def query_selector(self, selector: str) -> Any:
        """模拟 query_selector：始终返回 None（无验证码/元素命中）。"""
        return None

    def on(self, event: str, handler: Any) -> None:
        self.on_calls.append((event, handler))


class _AsyncPage:
    """模拟 Playwright 异步 Page，覆盖 observe_async/act_async 分支。"""

    def __init__(
        self,
        *,
        url: str = "https://example.com",
        title: str = "Async Page",
        content_html: str = "<html><body>y</body></html>",
        hook_records: list[dict] | None = None,
        scripts: list[str] | None = None,
        screenshot_fail: bool = False,
    ) -> None:
        self._url = url
        self._title = title
        self._content = content_html
        self._hook_records = hook_records if hook_records is not None else []
        self._scripts = scripts if scripts is not None else []
        self._screenshot_fail = screenshot_fail
        self.screenshot_calls: list[dict[str, Any]] = []
        self.goto_calls: list[dict[str, Any]] = []
        self.evaluate_calls: list[str] = []

    @property
    def url(self) -> str:
        return self._url

    async def title(self) -> str:
        return self._title

    async def content(self) -> str:
        return self._content

    async def screenshot(self, *, path: str = "", **kwargs: Any) -> bytes:
        self.screenshot_calls.append({"path": path, **kwargs})
        if self._screenshot_fail:
            raise RuntimeError("screenshot failed")
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return b"\x89PNG\r\n\x1a\n"

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append({"url": url, **kwargs})
        self._url = url

    async def evaluate(self, script: str) -> Any:
        self.evaluate_calls.append(script)
        if "__hook_data__" in script:
            return list(self._hook_records)
        if "querySelectorAll('script[src]')" in script:
            return list(self._scripts)
        return None

    def on(self, event: str, handler: Any) -> None:
        pass

    def query_selector(self, selector: str) -> Any:
        """模拟 query_selector：始终返回 None（无验证码/元素命中）。"""
        return None


def _make_agent(**config_kwargs: Any) -> ReverseAgent:
    """构造一个禁用所有重依赖的 ReverseAgent 实例。"""
    defaults: dict[str, Any] = {
        "enable_screenshot": False,
        "enable_guard": False,
        "enable_judge": False,
        "enable_recorder": False,
        "planner_interval": None,
        "humanize_input": False,
        "wait_after_navigate": 0.0,
    }
    defaults.update(config_kwargs)
    cfg = ReverseAgentConfig(**defaults)
    return ReverseAgent(config=cfg, provider=StubProvider())


# ---------------------------------------------------------------------------
# 模块级辅助函数：_js_str / _extract_json
# ---------------------------------------------------------------------------


class TestJsStr:
    def test_escapes_backslash_and_quotes(self) -> None:
        assert _js_str('a"b\\c') == '"a\\"b\\\\c"'

    def test_escapes_newlines_and_backticks(self) -> None:
        result = _js_str("line1\nline2\r\n`code`")
        assert "\\n" in result
        assert "\\r" in result
        assert "\\`" in result

    def test_wraps_in_double_quotes(self) -> None:
        assert _js_str("hello").startswith('"')
        assert _js_str("hello").endswith('"')

    def test_handles_non_string_input(self) -> None:
        """非字符串应先转 str 再转义。"""
        result = _js_str(123)  # type: ignore[arg-type]
        assert result == '"123"'


class TestExtractJson:
    def test_parses_plain_json(self) -> None:
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_parses_code_fence_json(self) -> None:
        text = "```json\n" + '{"a": 1}' + "\n```"
        assert _extract_json(text) == {"a": 1}

    def test_parses_code_fence_without_lang(self) -> None:
        text = "```\n" + '{"a": 1}' + "\n```"
        assert _extract_json(text) == {"a": 1}

    def test_extracts_embedded_json_from_text(self) -> None:
        text = '好的，结果是 {"a": 1, "b": 2} 请查阅'
        assert _extract_json(text) == {"a": 1, "b": 2}

    def test_returns_empty_dict_on_invalid_json(self) -> None:
        assert _extract_json("not json at all") == {}

    def test_returns_empty_dict_on_invalid_embedded_json(self) -> None:
        """JSON 块存在但解析失败时返回空 dict。"""
        assert _extract_json("{not valid json}") == {}

    def test_strips_whitespace(self) -> None:
        assert _extract_json('  {"a": 1}  ') == {"a": 1}


# ---------------------------------------------------------------------------
# Action / Observation dataclass
# ---------------------------------------------------------------------------


class TestActionDataclass:
    def test_default_action_is_wait(self) -> None:
        a = Action(action_type="wait")
        assert a.action_type == "wait"
        assert a.params == {}
        assert a.reasoning == ""

    def test_from_dict_with_full_data(self) -> None:
        a = Action.from_dict(
            {"action_type": "click", "params": {"selector": "x"}, "reasoning": "r"}
        )
        assert a.action_type == "click"
        assert a.params == {"selector": "x"}
        assert a.reasoning == "r"

    def test_from_dict_with_missing_fields_uses_defaults(self) -> None:
        a = Action.from_dict({})
        assert a.action_type == "wait"  # 默认
        assert a.params == {}
        assert a.reasoning == ""

    def test_from_dict_with_none_params(self) -> None:
        a = Action.from_dict({"action_type": "wait", "params": None})
        assert a.params == {}

    def test_from_dict_with_none_reasoning(self) -> None:
        a = Action.from_dict({"action_type": "wait", "reasoning": None})
        assert a.reasoning == ""


class TestObservationDataclass:
    def test_default_screenshot_path_is_empty(self) -> None:
        obs = Observation(
            url="u",
            hook_data={},
            network_requests=[],
            scripts=[],
            captcha_type=CaptchaType.NONE,
            page_title="t",
            dom_summary="d",
        )
        assert obs.screenshot_path == ""


# ---------------------------------------------------------------------------
# ReverseAgentConfig 默认值
# ---------------------------------------------------------------------------


class TestReverseAgentConfig:
    def test_all_defaults_are_reasonable(self) -> None:
        cfg = ReverseAgentConfig()
        assert cfg.max_steps == 20
        assert cfg.headless is False
        assert cfg.wait_after_navigate == 3.0
        assert cfg.os_name == "windows"
        assert cfg.planner_interval == 5
        assert cfg.loop_threshold == 3
        assert cfg.max_history == 25
        assert cfg.enable_judge is True
        assert cfg.judge_strict is True
        assert cfg.enable_recorder is True
        assert cfg.heartbeat_timeout == 120.0
        assert cfg.max_retries == 2
        assert cfg.dom_prune_max_chars == 0
        assert cfg.dom_prune_llm_rank is False
        assert cfg.enable_checkpoint is False
        assert cfg.checkpoint_interval == 1
        assert cfg.checkpoint_keep == 5
        assert cfg.min_confidence == 0.4
        assert cfg.confidence_llm_score is False
        assert cfg.enable_guard is True
        assert cfg.allowed_domains is None
        assert cfg.enable_screenshot is True
        assert cfg.humanize_input is True
        assert cfg.enable_image_captcha is True
        assert cfg.should_stop is None  # 默认不启用停止回调


# ---------------------------------------------------------------------------
# ReverseAgent.__init__ 组件装配
# ---------------------------------------------------------------------------


class TestReverseAgentInit:
    def test_default_init_creates_all_components(self) -> None:
        agent = ReverseAgent()
        try:
            # 关键组件都应被实例化
            assert agent.captcha_manager is not None
            assert agent.planner is not None  # planner_interval=5 默认启用
            assert agent.loop_detector is not None
            assert agent.context_compressor is not None
            assert agent.judge is not None  # enable_judge 默认 True
            assert agent.recorder is not None  # enable_recorder 默认 True
            assert agent.event_bus is not None
            assert agent.heartbeat is not None
            assert agent.crash_recovery is not None
            assert agent.checkpoint_manager is not None
            assert agent.confidence_scorer is not None
            assert agent.guard is not None  # enable_guard 默认 True
            assert agent.dom_pruner is None  # dom_prune_max_chars=0 默认禁用
            # 默认初始化字段
            assert agent._tabs == {}
            assert agent._network_log == []
            assert agent._screenshots == []
            assert agent._current_plan is None
            assert agent._last_judge_result is None
            assert agent._compiled_script == ""
            assert agent._last_pruned_dom is None
            assert agent._last_confidence is None
            assert agent._last_guard_result is None
            assert agent._last_think_prompt == ""
            assert agent._last_think_completion == ""
            assert agent._last_llm_usage is None
            assert agent._last_error_screenshot == ""
        finally:
            agent.close()

    def test_init_with_planner_disabled_when_interval_none(self) -> None:
        cfg = ReverseAgentConfig(planner_interval=None)
        agent = ReverseAgent(config=cfg)
        try:
            assert agent.planner is None
        finally:
            agent.close()

    def test_init_with_judge_disabled(self) -> None:
        cfg = ReverseAgentConfig(enable_judge=False)
        agent = ReverseAgent(config=cfg)
        try:
            assert agent.judge is None
        finally:
            agent.close()

    def test_init_with_recorder_disabled(self) -> None:
        cfg = ReverseAgentConfig(enable_recorder=False)
        agent = ReverseAgent(config=cfg)
        try:
            assert agent.recorder is None
        finally:
            agent.close()

    def test_init_with_guard_disabled(self) -> None:
        cfg = ReverseAgentConfig(enable_guard=False)
        agent = ReverseAgent(config=cfg)
        try:
            assert agent.guard is None
        finally:
            agent.close()

    def test_init_with_dom_pruner_enabled(self) -> None:
        cfg = ReverseAgentConfig(dom_prune_max_chars=5000)
        agent = ReverseAgent(config=cfg)
        try:
            assert agent.dom_pruner is not None
            assert agent.dom_pruner.max_chars == 5000
        finally:
            agent.close()

    def test_init_with_image_captcha_disabled(self) -> None:
        cfg = ReverseAgentConfig(enable_image_captcha=False)
        agent = ReverseAgent(config=cfg)
        try:
            # image_solver 应为 None（通过 captcha_manager 内部状态判断）
            # 直接验证不抛异常即可
            assert agent.captcha_manager is not None
        finally:
            agent.close()

    def test_init_with_custom_event_bus(self) -> None:
        from web_crawler.ai.watchdog import EventBus

        bus = EventBus()
        agent = ReverseAgent(event_bus=bus)
        try:
            assert agent.event_bus is bus
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# _emit / _on_stall
# ---------------------------------------------------------------------------


class TestEmitAndStall:
    def test_emit_publishes_event_to_bus(self) -> None:
        agent = _make_agent()
        try:
            events: list[Any] = []
            agent.event_bus.subscribe(lambda e: events.append(e))
            agent._emit("test.event", step=3, foo="bar")
            assert len(events) == 1
            assert events[0].type == "test.event"
            assert events[0].step == 3
            assert events[0].payload["foo"] == "bar"
        finally:
            agent.close()

    def test_on_stall_emits_stall_event(self) -> None:
        agent = _make_agent()
        try:
            events: list[Any] = []
            agent.event_bus.subscribe(lambda e: events.append(e))
            agent._on_stall(step=5, elapsed=200.0)
            assert len(events) == 1
            assert events[0].type == "stall"
            assert events[0].step == 5
            assert events[0].payload["elapsed"] == 200.0
            assert "200.0s" in events[0].payload["message"]
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# _safe_page_url
# ---------------------------------------------------------------------------


class TestSafePageUrl:
    def test_returns_url_attribute(self) -> None:
        page = MagicMock()
        page.url = "https://x.example"
        assert ReverseAgent._safe_page_url(page) == "https://x.example"

    def test_returns_empty_string_on_exception(self) -> None:
        page = MagicMock()
        type(page).url = property(lambda self: (_ for _ in ()).throw(RuntimeError("no url")))
        assert ReverseAgent._safe_page_url(page) == ""


# ---------------------------------------------------------------------------
# _observe / _observe_async
# ---------------------------------------------------------------------------


class TestObserve:
    def test_observe_collects_all_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_observe 应聚合 url/hook_data/network/scripts/captcha/dom/截图。"""
        monkeypatch.chdir(Path(__file__).parent)  # 截图写入 tests 目录
        cfg = ReverseAgentConfig(
            enable_screenshot=True,
            enable_guard=False,
            enable_judge=False,
            enable_recorder=False,
            planner_interval=None,
            humanize_input=False,
        )
        agent = ReverseAgent(config=cfg, provider=StubProvider())
        try:
            page = _SyncPage(
                url="https://x.example/p",
                title="Demo",
                content_html="<html><body>hello</body></html>",
                hook_records=[{"type": "fetch", "url": "https://api.example"}],
                scripts=["https://x.example/a.js", "https://x.example/b.js"],
            )
            # 预填 _network_log
            agent._network_log.append({"url": "https://n.example", "method": "GET"})
            obs = agent._observe(page, step=1)
            assert obs.url == "https://x.example/p"
            assert obs.page_title == "Demo"
            assert obs.hook_data["count"] == 1
            assert len(obs.network_requests) == 1
            assert len(obs.scripts) == 2
            assert obs.captcha_type == CaptchaType.NONE
            assert "hello" in obs.dom_summary
            # 截图路径非空
            assert obs.screenshot_path != ""
            # _hook_data_cache 应被更新
            assert agent._hook_data_cache["count"] == 1
        finally:
            agent.close()

    def test_observe_handles_page_methods_throwing(self) -> None:
        """page.title() / content() 抛异常时不应崩溃。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            page.url = "https://x.example"
            page.title.side_effect = RuntimeError("no title")
            page.content.side_effect = RuntimeError("no content")
            # evaluate 需返回空列表（collect_hook_data 无 try/except），不能抛异常
            page.evaluate.return_value = []
            page.screenshot.return_value = b""
            # captcha_manager.detector.detect 返回 None
            agent.captcha_manager.detector.detect = MagicMock(return_value=None)  # type: ignore[assignment]
            obs = agent._observe(page, step=1)
            assert obs.url == "https://x.example"
            assert obs.page_title == ""
            assert obs.dom_summary == ""
            assert obs.captcha_type == CaptchaType.NONE
            assert obs.scripts == []
        finally:
            agent.close()

    def test_observe_clears_network_log_after_read(self) -> None:
        """_observe 每步只取增量请求并清空 _network_log，防止跨步累积。"""
        agent = _make_agent()
        try:
            page = _SyncPage(url="https://x.example", content_html="<html></html>")
            agent._network_log.append({"url": "https://n.example", "method": "GET"})
            obs = agent._observe(page, step=1)
            assert len(obs.network_requests) == 1
            assert agent._network_log == []
        finally:
            agent.close()

    def test_observe_uses_dom_pruner_when_enabled(self) -> None:
        """dom_prune_max_chars > 0 时 _observe 应调用 DomPruner.prune。"""
        cfg = ReverseAgentConfig(
            enable_screenshot=False,
            enable_guard=False,
            enable_judge=False,
            enable_recorder=False,
            planner_interval=None,
            humanize_input=False,
            dom_prune_max_chars=1000,
        )
        agent = ReverseAgent(config=cfg, provider=StubProvider())
        try:
            page = MagicMock()
            page.url = "https://x.example"
            page.title.return_value = "T"
            page.content.return_value = "x" * 5000
            page.evaluate.return_value = []
            agent.captcha_manager.detector.detect = MagicMock(return_value=None)  # type: ignore[assignment]
            # Mock DomPruner.prune 返回带 text 的 PrunedDom
            pruned = MagicMock()
            pruned.text = "PRUNED_TEXT"
            agent.dom_pruner.prune = MagicMock(return_value=pruned)  # type: ignore[assignment]
            obs = agent._observe(page, step=1)
            assert obs.dom_summary == "PRUNED_TEXT"
            assert agent._last_pruned_dom is pruned
        finally:
            agent.close()

    def test_observe_dom_pruner_falls_back_when_text_empty(self) -> None:
        """DomPruner 返回空 text 时应回退到原始 dom 截断。"""
        cfg = ReverseAgentConfig(
            enable_screenshot=False,
            enable_guard=False,
            enable_judge=False,
            enable_recorder=False,
            planner_interval=None,
            humanize_input=False,
            dom_prune_max_chars=1000,
        )
        agent = ReverseAgent(config=cfg, provider=StubProvider())
        try:
            page = MagicMock()
            page.url = "https://x.example"
            page.title.return_value = "T"
            page.content.return_value = "abcdefghij" * 500
            page.evaluate.return_value = []
            agent.captcha_manager.detector.detect = MagicMock(return_value=None)  # type: ignore[assignment]
            pruned = MagicMock()
            pruned.text = ""  # 空 text
            agent.dom_pruner.prune = MagicMock(return_value=pruned)  # type: ignore[assignment]
            obs = agent._observe(page, step=1)
            # 应回退到 dom_raw[:2000]
            assert len(obs.dom_summary) == 2000
        finally:
            agent.close()


class TestObserveAsync:
    @pytest.mark.asyncio
    async def test_observe_async_collects_all_fields(self) -> None:
        """异步 _observe_async 应聚合所有字段。"""
        cfg = ReverseAgentConfig(
            enable_screenshot=False,
            enable_guard=False,
            enable_judge=False,
            enable_recorder=False,
            planner_interval=None,
            humanize_input=False,
        )
        agent = ReverseAgent(config=cfg, provider=StubProvider())
        try:
            page = _AsyncPage(
                url="https://x.example/async",
                title="AsyncDemo",
                hook_records=[{"type": "xhr"}],
                scripts=["a.js"],
            )
            agent._network_log.append({"url": "n"})
            obs = await agent._observe_async(page, step=1)
            assert obs.url == "https://x.example/async"
            assert obs.page_title == "AsyncDemo"
            assert obs.hook_data["count"] == 1
            assert len(obs.network_requests) == 1
            assert len(obs.scripts) == 1
            assert agent._hook_data_cache["count"] == 1
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_observe_async_handles_throwing_page(self) -> None:
        """异步路径 page 方法抛异常时不崩溃。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            page.url = "https://x.example"
            page.title = AsyncMock(side_effect=RuntimeError("title fail"))
            page.content = AsyncMock(side_effect=RuntimeError("content fail"))
            # evaluate 不能抛异常：_observe_async 内联 await page.evaluate 无 try/except
            page.evaluate = AsyncMock(return_value=[])
            page.screenshot = AsyncMock(return_value=b"")
            agent.captcha_manager.detector.detect = MagicMock(return_value=None)  # type: ignore[assignment]
            obs = await agent._observe_async(page, step=1)
            assert obs.page_title == ""
            assert obs.dom_summary == ""
            assert obs.scripts == []
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_observe_async_uses_dom_pruner(self) -> None:
        """异步路径启用 DomPruner 时调用 prune_async。"""
        cfg = ReverseAgentConfig(
            enable_screenshot=False,
            enable_guard=False,
            enable_judge=False,
            enable_recorder=False,
            planner_interval=None,
            humanize_input=False,
            dom_prune_max_chars=500,
        )
        agent = ReverseAgent(config=cfg, provider=StubProvider())
        try:
            page = MagicMock()
            page.url = "https://x.example"
            page.title = AsyncMock(return_value="T")
            page.content = AsyncMock(return_value="x" * 5000)
            page.evaluate = AsyncMock(return_value=[])
            agent.captcha_manager.detector.detect = MagicMock(return_value=None)  # type: ignore[assignment]
            pruned = MagicMock()
            pruned.text = "ASYNC_PRUNED"
            agent.dom_pruner.prune_async = AsyncMock(return_value=pruned)  # type: ignore[assignment]
            obs = await agent._observe_async(page, step=1)
            assert obs.dom_summary == "ASYNC_PRUNED"
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# _think / _think_async / _parse_action / _fallback_action
# ---------------------------------------------------------------------------


class TestThink:
    def test_think_calls_provider_and_returns_action(self) -> None:
        agent = _make_agent()
        try:
            provider = StubProvider(
                ['{"action_type": "click", "params": {"selector": "x"}, "reasoning": "r"}']
            )
            agent.provider = provider
            obs = Observation(
                url="u",
                hook_data={},
                network_requests=[],
                scripts=[],
                captcha_type=CaptchaType.NONE,
                page_title="t",
                dom_summary="d",
            )
            action = agent._think(obs, "task", [])
            assert action.action_type == "click"
            assert action.params == {"selector": "x"}
            assert action.reasoning == "r"
            # _last_think_prompt / _last_think_completion 应被更新
            assert agent._last_think_prompt != ""
            assert agent._last_think_completion != ""
            # usage 应被暂存
            assert agent._last_llm_usage == {"tokens": 1}
            assert provider.calls == 1
        finally:
            agent.close()

    def test_think_with_plan_injects_subgoal_into_prompt(self) -> None:
        agent = _make_agent()
        try:
            agent.provider = StubProvider(['{"action_type": "wait"}'])
            from web_crawler.ai.planner import Plan, SubGoal

            plan = Plan(subgoals=[SubGoal(description="my-subgoal", success_criteria="ok")])
            obs = Observation(
                url="u",
                hook_data={},
                network_requests=[],
                scripts=[],
                captcha_type=CaptchaType.NONE,
                page_title="t",
                dom_summary="d",
            )
            agent._think(obs, "task", [], plan=plan)
            # prompt 应包含子目标描述
            assert "my-subgoal" in agent._last_think_prompt
            assert "当前子目标" in agent._last_think_prompt
        finally:
            agent.close()

    def test_think_with_cumulative_summary_in_prompt(self) -> None:
        agent = _make_agent()
        try:
            agent.provider = StubProvider(['{"action_type": "wait"}'])
            agent.context_compressor._cumulative_summary = "PAST_SUMMARY_CONTENT"
            obs = Observation(
                url="u",
                hook_data={},
                network_requests=[],
                scripts=[],
                captcha_type=CaptchaType.NONE,
                page_title="t",
                dom_summary="d",
            )
            agent._think(obs, "task", [])
            assert "PAST_SUMMARY_CONTENT" in agent._last_think_prompt
            assert "历史摘要" in agent._last_think_prompt
        finally:
            agent.close()


class TestThinkAsync:
    @pytest.mark.asyncio
    async def test_think_async_uses_achat(self) -> None:
        agent = _make_agent()
        try:
            provider = MagicMock()
            provider.achat = AsyncMock(
                return_value=LLMResponse(
                    content='{"action_type": "done", "params": {"success": true}}',
                    model="fake",
                )
            )
            agent.provider = provider
            obs = Observation(
                url="u",
                hook_data={},
                network_requests=[],
                scripts=[],
                captcha_type=CaptchaType.NONE,
                page_title="t",
                dom_summary="d",
            )
            action = await agent._think_async(obs, "task", [])
            provider.achat.assert_awaited_once()
            assert action.action_type == "done"
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_think_async_falls_back_to_sync_chat(self) -> None:
        """provider 无 achat 时回退到同步 chat。"""
        agent = _make_agent()
        try:
            provider = MagicMock(spec=["chat"])  # 只暴露 chat
            provider.chat.return_value = LLMResponse(
                content='{"action_type": "wait"}', model="fake"
            )
            agent.provider = provider
            obs = Observation(
                url="u",
                hook_data={},
                network_requests=[],
                scripts=[],
                captcha_type=CaptchaType.NONE,
                page_title="t",
                dom_summary="d",
            )
            action = await agent._think_async(obs, "task", [])
            provider.chat.assert_called_once()
            assert action.action_type == "wait"
        finally:
            agent.close()


class TestParseAction:
    def test_parses_valid_json(self) -> None:
        agent = _make_agent()
        try:
            action = agent._parse_action('{"action_type": "done", "params": {"success": true}}')
            assert action.action_type == "done"
            assert action.params == {"success": True}
        finally:
            agent.close()

    def test_returns_wait_on_invalid_json(self) -> None:
        agent = _make_agent()
        try:
            action = agent._parse_action("not json")
            assert action.action_type == "wait"
            assert action.params == {"seconds": 1.0}
            assert "无法解析" in action.reasoning
        finally:
            agent.close()

    def test_returns_wait_on_empty_string(self) -> None:
        agent = _make_agent()
        try:
            action = agent._parse_action("")
            assert action.action_type == "wait"
        finally:
            agent.close()


class TestFallbackAction:
    def test_fallback_returns_extract_action_with_first_target(self) -> None:
        cfg = ReverseAgentConfig(
            enable_screenshot=False,
            enable_guard=False,
            enable_judge=False,
            enable_recorder=False,
            planner_interval=None,
            humanize_input=False,
            target_params=["Anti-Content", "X-Bogus"],
        )
        agent = ReverseAgent(config=cfg, provider=StubProvider())
        try:
            obs = Observation(
                url="u",
                hook_data={},
                network_requests=[],
                scripts=[],
                captcha_type=CaptchaType.NONE,
                page_title="t",
                dom_summary="d",
            )
            action = agent._fallback_action(obs)
            assert action.action_type == "extract"
            assert action.params == {"param_name": "Anti-Content"}
        finally:
            agent.close()

    def test_fallback_with_no_target_params(self) -> None:
        """无目标参数时 fallback 降级为 wait，避免空操作 extract 空转。"""
        agent = _make_agent()
        try:
            obs = Observation(
                url="u",
                hook_data={},
                network_requests=[],
                scripts=[],
                captcha_type=CaptchaType.NONE,
                page_title="t",
                dom_summary="d",
            )
            action = agent._fallback_action(obs)
            assert action.action_type == "wait"
            assert action.params == {"seconds": 2.0}
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# _act / _act_async 各分支
# ---------------------------------------------------------------------------


class TestActBranches:
    def test_act_navigate_with_url(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="navigate", params={"url": "https://x.example"})
            result = agent._act(page, action, step=1)
            assert result is None
            page.goto.assert_called_once_with(
                "https://x.example", wait_until="domcontentloaded", timeout=30000
            )
        finally:
            agent.close()

    def test_act_navigate_without_url_does_nothing(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="navigate", params={})
            result = agent._act(page, action, step=1)
            assert result is None
            page.goto.assert_not_called()
        finally:
            agent.close()

    def test_act_inject_hook_returns_true_on_success(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.return_value = None
            action = Action(action_type="inject_hook", params={"hooks": ["fetch_hook"]})
            result = agent._act(page, action, step=1)
            assert result is True
        finally:
            agent.close()

    def test_act_inject_hook_returns_false_on_failure(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.side_effect = RuntimeError("injection failed")
            action = Action(action_type="inject_hook", params={"hooks": ["unknown"]})
            result = agent._act(page, action, step=1)
            assert result is False
        finally:
            agent.close()

    def test_act_wait_sleeps_seconds(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="wait", params={"seconds": 0.1})
            with patch("web_crawler.ai.reverse_agent.time.sleep") as mock_sleep:
                agent._act(page, action, step=1)
                mock_sleep.assert_called_once()
                # 应在 [0.1, 30] 范围
                called_arg = mock_sleep.call_args[0][0]
                assert 0.1 <= called_arg <= 30.0
        finally:
            agent.close()

    def test_act_wait_clamps_seconds_to_range(self) -> None:
        """wait 的 seconds 超出范围应被 clamp 到 [0.1, 30]。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="wait", params={"seconds": 100.0})
            with patch("web_crawler.ai.reverse_agent.time.sleep") as mock_sleep:
                agent._act(page, action, step=1)
                called_arg = mock_sleep.call_args[0][0]
                assert called_arg == 30.0  # 被 clamp 到上限
        finally:
            agent.close()

    def test_act_wait_default_seconds_when_missing(self) -> None:
        """wait 未传 seconds 时默认 1.0。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="wait", params={})
            with patch("web_crawler.ai.reverse_agent.time.sleep") as mock_sleep:
                agent._act(page, action, step=1)
                called_arg = mock_sleep.call_args[0][0]
                assert called_arg == 1.0
        finally:
            agent.close()

    def test_act_extract_no_param_name_returns_none(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="extract", params={})  # 无 param_name
            result = agent._act(page, action, step=1)
            assert result is None
        finally:
            agent.close()

    def test_act_extract_finds_param_in_records(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.return_value = [
                {"headers": {"Anti-Content": "abc123"}, "url": "", "body": ""}
            ]
            action = Action(action_type="extract", params={"param_name": "Anti-Content"})
            result = agent._act(page, action, step=1)
            assert result == "abc123"
        finally:
            agent.close()

    def test_act_solve_captcha_delegates_to_manager(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            agent.captcha_manager.handle = MagicMock(return_value=True)  # type: ignore[assignment]
            action = Action(action_type="solve_captcha", params={})
            result = agent._act(page, action, step=1)
            assert result is True
            agent.captcha_manager.handle.assert_called_once_with(page)
        finally:
            agent.close()

    def test_act_unknown_action_raises(self) -> None:
        """未知动作类型应抛 ValueError（进入 act_error 路径写 history）。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="totally_unknown", params={})
            with pytest.raises(ValueError, match="未知动作类型"):
                agent._act(page, action, step=1)
        finally:
            agent.close()

    def test_act_done_returns_none(self) -> None:
        """done 在 _act 中无专门执行分支，应返回 None（主循环在外层处理 done）。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="done", params={"success": True})
            result = agent._act(page, action, step=1)
            assert result is None
        finally:
            agent.close()

    def test_act_analyze_js_returns_none_when_no_fragments(self) -> None:
        """analyze_js 在 _analyze_captured_js 返回 None 时应返回 None。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="analyze_js", params={"script_urls": []})
            result = agent._act(page, action, step=1)
            assert result is None
        finally:
            agent.close()


class TestActAsyncBranches:
    @pytest.mark.asyncio
    async def test_act_async_navigate(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.goto = AsyncMock()
            action = Action(action_type="navigate", params={"url": "https://y.example"})
            result = await agent._act_async(page, action, step=1)
            assert result is None
            page.goto.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_navigate_without_url(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.goto = AsyncMock()
            action = Action(action_type="navigate", params={})
            result = await agent._act_async(page, action, step=1)
            assert result is None
            page.goto.assert_not_awaited()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_inject_hook_success(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock()
            action = Action(action_type="inject_hook", params={"hooks": ["fetch_hook"]})
            result = await agent._act_async(page, action, step=1)
            assert result is True
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_inject_hook_failure(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock(side_effect=RuntimeError("inj fail"))
            action = Action(action_type="inject_hook", params={"hooks": ["bad"]})
            result = await agent._act_async(page, action, step=1)
            assert result is False
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_wait(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="wait", params={"seconds": 0.1})
            with patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()) as m:
                await agent._act_async(page, action, step=1)
                m.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_extract_no_param_name(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="extract", params={})
            result = await agent._act_async(page, action, step=1)
            assert result is None
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_extract_with_param(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock(
                return_value=[{"headers": {"sign": "value"}, "url": "", "body": ""}]
            )
            action = Action(action_type="extract", params={"param_name": "sign"})
            result = await agent._act_async(page, action, step=1)
            assert result == "value"
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_solve_captcha(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            agent.captcha_manager.handle = MagicMock(return_value=True)  # type: ignore[assignment]
            action = Action(action_type="solve_captcha", params={})
            result = await agent._act_async(page, action, step=1)
            assert result is True
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_unknown_action(self) -> None:
        """未知动作类型应抛 ValueError（进入 act_error 路径写 history）。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="totally_unknown", params={})
            with pytest.raises(ValueError, match="未知动作类型"):
                await agent._act_async(page, action, step=1)
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_done_returns_none(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="done", params={"success": True})
            result = await agent._act_async(page, action, step=1)
            assert result is None
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_analyze_js_returns_none(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="analyze_js", params={"script_urls": []})
            result = await agent._act_async(page, action, step=1)
            assert result is None
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# 浏览器交互动作（click / type / scroll / press / hover / select_option）
# ---------------------------------------------------------------------------


class TestDoClick:
    def test_click_without_humanize_calls_page_click(self) -> None:
        """humanize_input=False 时直接调用 page.click。"""
        agent = _make_agent()  # humanize_input=False
        try:
            page = MagicMock()
            action = Action(action_type="click", params={"selector": "#btn", "button": "right"})
            agent._do_click(page, action, step=1)
            page.click.assert_called_once_with(
                "#btn", button="right", timeout=ReverseAgent._INTERACTION_TIMEOUT
            )
        finally:
            agent.close()

    def test_click_default_button_is_left(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="click", params={"selector": "#btn"})
            agent._do_click(page, action, step=1)
            assert page.click.call_args[1]["button"] == "left"
        finally:
            agent.close()

    def test_click_missing_selector_raises(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="click", params={})
            with pytest.raises(ValueError, match="selector"):
                agent._do_click(page, action, step=1)
        finally:
            agent.close()

    def test_click_with_humanize_uses_humanize_click(self) -> None:
        """humanize_input=True 时走 _humanize_click 路径。"""
        agent = _make_agent(humanize_input=True)
        try:
            page = MagicMock()
            action = Action(action_type="click", params={"selector": "#btn"})
            with patch("web_crawler.ai.reverse_agent.time.sleep"):
                agent._do_click(page, action, step=1)
            # humanize 路径会先 hover 再 click
            page.hover.assert_called_once()
            page.click.assert_called_once()
        finally:
            agent.close()

    def test_humanize_click_swallows_hover_failure(self) -> None:
        """_humanize_click 中 hover 失败不阻断 click。"""
        agent = _make_agent(humanize_input=True)
        try:
            page = MagicMock()
            page.hover.side_effect = RuntimeError("hover fail")
            with patch("web_crawler.ai.reverse_agent.time.sleep"):
                agent._humanize_click(page, "#sel")
            page.click.assert_called_once()
        finally:
            agent.close()


class TestDoClickAsync:
    @pytest.mark.asyncio
    async def test_click_async_without_humanize(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.click = AsyncMock()
            action = Action(action_type="click", params={"selector": "#btn"})
            await agent._do_click_async(page, action, step=1)
            page.click.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_click_async_missing_selector_raises(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="click", params={})
            with pytest.raises(ValueError, match="selector"):
                await agent._do_click_async(page, action, step=1)
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_click_async_with_humanize(self) -> None:
        agent = _make_agent(humanize_input=True)
        try:
            page = MagicMock()
            page.hover = AsyncMock()
            page.click = AsyncMock()
            action = Action(action_type="click", params={"selector": "#btn"})
            with patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()):
                await agent._do_click_async(page, action, step=1)
            page.hover.assert_awaited_once()
            page.click.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_humanize_click_async_swallows_hover_failure(self) -> None:
        agent = _make_agent(humanize_input=True)
        try:
            page = MagicMock()
            page.hover = AsyncMock(side_effect=RuntimeError("hover fail"))
            page.click = AsyncMock()
            with patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()):
                await agent._humanize_click_async(page, "#sel")
            page.click.assert_awaited_once()
        finally:
            agent.close()


class TestDoType:
    def test_type_without_humanize_calls_page_type(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="type", params={"selector": "#inp", "text": "hello"})
            agent._do_type(page, action, step=1)
            page.fill.assert_called_once()  # clear 默认 True
            page.type.assert_called_once()
        finally:
            agent.close()

    def test_type_clear_false_skips_fill(self) -> None:
        """clear=False 时不调用 page.fill。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(
                action_type="type", params={"selector": "#inp", "text": "x", "clear": False}
            )
            agent._do_type(page, action, step=1)
            page.fill.assert_not_called()
            page.type.assert_called_once()
        finally:
            agent.close()

    def test_type_missing_selector_raises(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="type", params={"text": "x"})
            with pytest.raises(ValueError, match="selector"):
                agent._do_type(page, action, step=1)
        finally:
            agent.close()

    def test_type_with_humanize_uses_humanize_type(self) -> None:
        agent = _make_agent(humanize_input=True)
        try:
            page = MagicMock()
            action = Action(action_type="type", params={"selector": "#inp", "text": "hi"})
            with patch("web_crawler.ai.reverse_agent.time.sleep"):
                agent._do_type(page, action, step=1)
            page.focus.assert_called_once()
            page.type.assert_called_once()
        finally:
            agent.close()

    def test_humanize_type_falls_back_when_delay_unsupported(self) -> None:
        """page.type 不支持 delay 参数时应退化为不带 delay 的调用。"""
        agent = _make_agent(humanize_input=True)
        try:
            page = MagicMock()
            page.focus = MagicMock()
            # 第一次带 delay 抛 TypeError，第二次不带 delay 成功
            page.type.side_effect = [TypeError("no delay"), None]
            with patch("web_crawler.ai.reverse_agent.time.sleep"):
                agent._humanize_type(page, "#sel", "txt")
            assert page.type.call_count == 2
        finally:
            agent.close()

    def test_humanize_type_swallows_focus_failure(self) -> None:
        agent = _make_agent(humanize_input=True)
        try:
            page = MagicMock()
            page.focus.side_effect = RuntimeError("focus fail")
            with patch("web_crawler.ai.reverse_agent.time.sleep"):
                agent._humanize_type(page, "#sel", "txt")
            page.type.assert_called_once()
        finally:
            agent.close()


class TestDoTypeAsync:
    @pytest.mark.asyncio
    async def test_type_async_without_humanize(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.fill = AsyncMock()
            page.type = AsyncMock()
            action = Action(action_type="type", params={"selector": "#inp", "text": "hi"})
            await agent._do_type_async(page, action, step=1)
            page.fill.assert_awaited_once()
            page.type.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_type_async_missing_selector_raises(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="type", params={})
            with pytest.raises(ValueError, match="selector"):
                await agent._do_type_async(page, action, step=1)
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_type_async_with_humanize(self) -> None:
        agent = _make_agent(humanize_input=True)
        try:
            page = MagicMock()
            page.fill = AsyncMock()
            page.focus = AsyncMock()
            page.type = AsyncMock()
            action = Action(action_type="type", params={"selector": "#inp", "text": "hi"})
            with patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()):
                await agent._do_type_async(page, action, step=1)
            page.focus.assert_awaited_once()
            page.type.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_humanize_type_async_falls_back_when_delay_unsupported(self) -> None:
        agent = _make_agent(humanize_input=True)
        try:
            page = MagicMock()
            page.focus = AsyncMock()
            page.type = AsyncMock(side_effect=[TypeError("no delay"), None])
            with patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()):
                await agent._humanize_type_async(page, "#sel", "txt")
            assert page.type.await_count == 2
        finally:
            agent.close()


class TestDoScroll:
    def test_scroll_window_when_no_selector(self) -> None:
        """无 selector 时滚动整个窗口。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="scroll", params={"x": 0, "y": 500})
            agent._do_scroll(page, action, step=1)
            page.evaluate.assert_called_once()
            assert "window.scrollBy" in page.evaluate.call_args[0][0]
        finally:
            agent.close()

    def test_scroll_element_with_selector(self) -> None:
        """有 selector 时滚动到元素内。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="scroll", params={"selector": ".list", "y": 300})
            agent._do_scroll(page, action, step=1)
            page.evaluate.assert_called_once()
            assert "querySelector" in page.evaluate.call_args[0][0]
        finally:
            agent.close()

    def test_scroll_defaults_x_zero_y_800(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="scroll", params={})
            agent._do_scroll(page, action, step=1)
            script = page.evaluate.call_args[0][0]
            assert "800" in script
        finally:
            agent.close()


class TestDoScrollAsync:
    @pytest.mark.asyncio
    async def test_scroll_async_window(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock()
            action = Action(action_type="scroll", params={"y": 200})
            await agent._do_scroll_async(page, action, step=1)
            page.evaluate.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_scroll_async_element(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock()
            action = Action(action_type="scroll", params={"selector": "#box"})
            await agent._do_scroll_async(page, action, step=1)
            script = page.evaluate.call_args[0][0]
            assert "querySelector" in script
        finally:
            agent.close()


class TestDoPress:
    def test_press_without_selector(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="press", params={"key": "Enter"})
            agent._do_press(page, action, step=1)
            page.press.assert_called_once_with("Enter")
        finally:
            agent.close()

    def test_press_with_selector_focuses_first(self) -> None:
        """有 selector 时先 focus 再 press。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="press", params={"selector": "#inp", "key": "Tab"})
            agent._do_press(page, action, step=1)
            page.focus.assert_called_once()
            page.press.assert_called_once_with("Tab")
        finally:
            agent.close()

    def test_press_default_key_is_enter(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="press", params={})
            agent._do_press(page, action, step=1)
            page.press.assert_called_once_with("Enter")
        finally:
            agent.close()


class TestDoPressAsync:
    @pytest.mark.asyncio
    async def test_press_async_without_selector(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.press = AsyncMock()
            action = Action(action_type="press", params={"key": "Escape"})
            await agent._do_press_async(page, action, step=1)
            page.press.assert_awaited_once_with("Escape")
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_press_async_with_selector(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.focus = AsyncMock()
            page.press = AsyncMock()
            action = Action(action_type="press", params={"selector": "#i", "key": "Enter"})
            await agent._do_press_async(page, action, step=1)
            page.focus.assert_awaited_once()
            page.press.assert_awaited_once()
        finally:
            agent.close()


class TestDoHover:
    def test_hover_calls_page_hover(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="hover", params={"selector": ".menu"})
            agent._do_hover(page, action, step=1)
            page.hover.assert_called_once()
        finally:
            agent.close()

    def test_hover_missing_selector_raises(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="hover", params={})
            with pytest.raises(ValueError, match="selector"):
                agent._do_hover(page, action, step=1)
        finally:
            agent.close()


class TestDoHoverAsync:
    @pytest.mark.asyncio
    async def test_hover_async_calls_page_hover(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.hover = AsyncMock()
            action = Action(action_type="hover", params={"selector": ".m"})
            await agent._do_hover_async(page, action, step=1)
            page.hover.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_hover_async_missing_selector_raises(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="hover", params={})
            with pytest.raises(ValueError, match="selector"):
                await agent._do_hover_async(page, action, step=1)
        finally:
            agent.close()


class TestDoSelectOption:
    def test_select_option_calls_page_select(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(
                action_type="select_option", params={"selector": "#country", "value": "CN"}
            )
            agent._do_select_option(page, action, step=1)
            page.select_option.assert_called_once()
        finally:
            agent.close()

    def test_select_option_missing_selector_raises(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="select_option", params={"value": "CN"})
            with pytest.raises(ValueError, match="selector"):
                agent._do_select_option(page, action, step=1)
        finally:
            agent.close()


class TestDoSelectOptionAsync:
    @pytest.mark.asyncio
    async def test_select_option_async(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.select_option = AsyncMock()
            action = Action(action_type="select_option", params={"selector": "#c", "value": "US"})
            await agent._do_select_option_async(page, action, step=1)
            page.select_option.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_select_option_async_missing_selector_raises(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="select_option", params={})
            with pytest.raises(ValueError, match="selector"):
                await agent._do_select_option_async(page, action, step=1)
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# 多标签页管理（new_tab / switch_tab / close_tab）
# ---------------------------------------------------------------------------


class TestDoNewTab:
    def test_new_tab_creates_and_navigates(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            new_page = MagicMock()
            agent._context = MagicMock()
            agent._context.new_page.return_value = new_page
            agent.fetcher = MagicMock()
            action = Action(
                action_type="new_tab", params={"url": "https://t.example", "name": "tab1"}
            )
            with patch("web_crawler.ai.reverse_agent.time.sleep"):
                agent._do_new_tab(page, action, step=1)
            agent._context.new_page.assert_called_once()
            new_page.goto.assert_called_once()
            assert agent._page is new_page
            assert agent._tabs["tab1"] is new_page
            # 主页面也应登记
            assert agent._tabs["main"] is page
        finally:
            agent.close()

    def test_new_tab_without_url_skips_goto(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            new_page = MagicMock()
            agent._context = MagicMock()
            agent._context.new_page.return_value = new_page
            agent.fetcher = MagicMock()
            action = Action(action_type="new_tab", params={})
            agent._do_new_tab(page, action, step=1)
            new_page.goto.assert_not_called()
        finally:
            agent.close()

    def test_new_tab_setup_page_failure_swallowed(self) -> None:
        """fetcher._setup_page 抛异常时不阻断 new_tab 流程。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            new_page = MagicMock()
            agent._context = MagicMock()
            agent._context.new_page.return_value = new_page
            agent.fetcher = MagicMock()
            agent.fetcher._setup_page.side_effect = RuntimeError("setup fail")
            action = Action(action_type="new_tab", params={})
            agent._do_new_tab(page, action, step=1)
            # 仍应完成标签创建
            assert agent._page is new_page
        finally:
            agent.close()


class TestDoNewTabAsync:
    @pytest.mark.asyncio
    async def test_new_tab_async_creates_and_navigates(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            new_page = MagicMock()
            new_page.goto = AsyncMock()
            agent._context = MagicMock()
            agent._context.new_page = AsyncMock(return_value=new_page)
            agent.fetcher = MagicMock()
            agent.fetcher._setup_page_async = AsyncMock()
            action = Action(
                action_type="new_tab", params={"url": "https://a.example", "name": "t1"}
            )
            with patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()):
                await agent._do_new_tab_async(page, action, step=1)
            agent._context.new_page.assert_awaited_once()
            new_page.goto.assert_awaited_once()
            assert agent._page is new_page
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_new_tab_async_without_url(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            new_page = MagicMock()
            agent._context = MagicMock()
            agent._context.new_page = AsyncMock(return_value=new_page)
            agent.fetcher = MagicMock()
            agent.fetcher._setup_page_async = AsyncMock()
            action = Action(action_type="new_tab", params={})
            await agent._do_new_tab_async(page, action, step=1)
            new_page.goto.assert_not_called()
        finally:
            agent.close()


class TestDoSwitchTab:
    def test_switch_tab_by_name(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            target = MagicMock()
            agent._tabs["extra"] = target
            action = Action(action_type="switch_tab", params={"name": "extra"})
            agent._do_switch_tab(page, action, step=1)
            assert agent._page is target
            target.bring_to_front.assert_called_once()
        finally:
            agent.close()

    def test_switch_tab_not_found_raises(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="switch_tab", params={"name": "nope"})
            with pytest.raises(ValueError, match="找不到标签页"):
                agent._do_switch_tab(page, action, step=1)
        finally:
            agent.close()

    def test_switch_tab_bring_to_front_failure_swallowed(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            target = MagicMock()
            target.bring_to_front.side_effect = RuntimeError("fail")
            agent._tabs["t"] = target
            action = Action(action_type="switch_tab", params={"name": "t"})
            agent._do_switch_tab(page, action, step=1)
            assert agent._page is target
        finally:
            agent.close()


class TestDoSwitchTabAsync:
    @pytest.mark.asyncio
    async def test_switch_tab_async_by_name(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            target = MagicMock()
            target.bring_to_front = AsyncMock()
            agent._tabs["x"] = target
            action = Action(action_type="switch_tab", params={"name": "x"})
            await agent._do_switch_tab_async(page, action, step=1)
            assert agent._page is target
            target.bring_to_front.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_switch_tab_async_not_found_raises(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="switch_tab", params={"index": 99})
            with pytest.raises(ValueError, match="找不到标签页"):
                await agent._do_switch_tab_async(page, action, step=1)
        finally:
            agent.close()


class TestDoCloseTab:
    def test_close_tab_removes_and_closes(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            target = MagicMock()
            agent._tabs["t"] = target
            action = Action(action_type="close_tab", params={"name": "t"})
            agent._do_close_tab(page, action, step=1)
            target.close.assert_called_once()
            assert "t" not in agent._tabs
        finally:
            agent.close()

    def test_close_tab_not_found_raises(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="close_tab", params={"name": "nope"})
            with pytest.raises(ValueError, match="找不到标签页"):
                agent._do_close_tab(page, action, step=1)
        finally:
            agent.close()

    def test_close_current_tab_falls_back_to_main(self) -> None:
        """关闭当前活跃标签时 self._page 回退到 main。"""
        agent = _make_agent()
        try:
            main_page = MagicMock()
            current = MagicMock()
            agent._tabs["main"] = main_page
            agent._tabs["current"] = current
            agent._page = current
            action = Action(action_type="close_tab", params={"name": "current"})
            agent._do_close_tab(current, action, step=1)
            assert agent._page is main_page
        finally:
            agent.close()

    def test_close_tab_close_failure_swallowed(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            target = MagicMock()
            target.close.side_effect = RuntimeError("close fail")
            agent._tabs["t"] = target
            action = Action(action_type="close_tab", params={"name": "t"})
            agent._do_close_tab(page, action, step=1)
            # 即使 close 抛异常也不传播
            assert "t" not in agent._tabs
        finally:
            agent.close()


class TestDoCloseTabAsync:
    @pytest.mark.asyncio
    async def test_close_tab_async_removes_and_closes(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            target = MagicMock()
            target.close = AsyncMock()
            agent._tabs["t"] = target
            action = Action(action_type="close_tab", params={"name": "t"})
            await agent._do_close_tab_async(page, action, step=1)
            target.close.assert_awaited_once()
            assert "t" not in agent._tabs
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_close_tab_async_not_found_raises(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="close_tab", params={"name": "nope"})
            with pytest.raises(ValueError, match="找不到标签页"):
                await agent._do_close_tab_async(page, action, step=1)
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_close_tab_async_current_falls_back_to_main(self) -> None:
        agent = _make_agent()
        try:
            main_page = MagicMock()
            current = MagicMock()
            current.close = AsyncMock()
            agent._tabs["main"] = main_page
            agent._tabs["cur"] = current
            agent._page = current
            action = Action(action_type="close_tab", params={"name": "cur"})
            await agent._do_close_tab_async(current, action, step=1)
            assert agent._page is main_page
        finally:
            agent.close()


class TestActBrowserActionsDispatch:
    """验证 _act / _act_async 能正确分发到各 _do_* 方法。"""

    def test_act_click_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="click", params={"selector": "#b"})
            assert agent._act(page, action, step=1) is None
            page.click.assert_called_once()
        finally:
            agent.close()

    def test_act_type_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="type", params={"selector": "#i", "text": "x"})
            assert agent._act(page, action, step=1) is None
            page.type.assert_called_once()
        finally:
            agent.close()

    def test_act_scroll_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="scroll", params={"y": 100})
            assert agent._act(page, action, step=1) is None
            page.evaluate.assert_called_once()
        finally:
            agent.close()

    def test_act_press_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="press", params={"key": "Enter"})
            assert agent._act(page, action, step=1) is None
            page.press.assert_called_once()
        finally:
            agent.close()

    def test_act_hover_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="hover", params={"selector": ".m"})
            assert agent._act(page, action, step=1) is None
            page.hover.assert_called_once()
        finally:
            agent.close()

    def test_act_select_option_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            action = Action(action_type="select_option", params={"selector": "#s", "value": "v"})
            assert agent._act(page, action, step=1) is None
            page.select_option.assert_called_once()
        finally:
            agent.close()

    def test_act_new_tab_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            new_page = MagicMock()
            agent._context = MagicMock()
            agent._context.new_page.return_value = new_page
            agent.fetcher = MagicMock()
            action = Action(action_type="new_tab", params={})
            assert agent._act(page, action, step=1) is None
            agent._context.new_page.assert_called_once()
        finally:
            agent.close()

    def test_act_switch_tab_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            target = MagicMock()
            agent._tabs["t"] = target
            action = Action(action_type="switch_tab", params={"name": "t"})
            assert agent._act(page, action, step=1) is None
            assert agent._page is target
        finally:
            agent.close()

    def test_act_close_tab_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            target = MagicMock()
            agent._tabs["t"] = target
            action = Action(action_type="close_tab", params={"name": "t"})
            assert agent._act(page, action, step=1) is None
            target.close.assert_called_once()
        finally:
            agent.close()


class TestActAsyncBrowserActionsDispatch:
    """验证 _act_async 能正确分发到各 _do_*_async 方法。"""

    @pytest.mark.asyncio
    async def test_act_async_click_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.click = AsyncMock()
            action = Action(action_type="click", params={"selector": "#b"})
            assert await agent._act_async(page, action, step=1) is None
            page.click.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_type_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.fill = AsyncMock()
            page.type = AsyncMock()
            action = Action(action_type="type", params={"selector": "#i", "text": "x"})
            assert await agent._act_async(page, action, step=1) is None
            page.type.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_scroll_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock()
            action = Action(action_type="scroll", params={"y": 100})
            assert await agent._act_async(page, action, step=1) is None
            page.evaluate.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_press_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.press = AsyncMock()
            action = Action(action_type="press", params={"key": "Tab"})
            assert await agent._act_async(page, action, step=1) is None
            page.press.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_hover_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.hover = AsyncMock()
            action = Action(action_type="hover", params={"selector": ".m"})
            assert await agent._act_async(page, action, step=1) is None
            page.hover.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_select_option_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.select_option = AsyncMock()
            action = Action(action_type="select_option", params={"selector": "#s", "value": "v"})
            assert await agent._act_async(page, action, step=1) is None
            page.select_option.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_new_tab_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            new_page = MagicMock()
            agent._context = MagicMock()
            agent._context.new_page = AsyncMock(return_value=new_page)
            agent.fetcher = MagicMock()
            agent.fetcher._setup_page_async = AsyncMock()
            action = Action(action_type="new_tab", params={})
            assert await agent._act_async(page, action, step=1) is None
            agent._context.new_page.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_switch_tab_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            target = MagicMock()
            target.bring_to_front = AsyncMock()
            agent._tabs["t"] = target
            action = Action(action_type="switch_tab", params={"name": "t"})
            assert await agent._act_async(page, action, step=1) is None
            assert agent._page is target
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_act_async_close_tab_dispatches(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            target = MagicMock()
            target.close = AsyncMock()
            agent._tabs["t"] = target
            action = Action(action_type="close_tab", params={"name": "t"})
            assert await agent._act_async(page, action, step=1) is None
            target.close.assert_awaited_once()
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# _inject_hooks / _inject_hooks_async
# ---------------------------------------------------------------------------


class TestInjectHooks:
    def test_inject_hooks_success(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.return_value = None
            assert agent._inject_hooks(page, ["fetch_hook"]) is True
            page.evaluate.assert_called_once()
        finally:
            agent.close()

    def test_inject_hooks_failure(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.side_effect = RuntimeError("eval fail")
            assert agent._inject_hooks(page, None) is False
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_inject_hooks_async_success(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock()
            assert await agent._inject_hooks_async(page, ["fetch_hook"]) is True
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_inject_hooks_async_failure(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock(side_effect=RuntimeError("eval fail"))
            assert await agent._inject_hooks_async(page, None) is False
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# _collect_scripts / _collect_scripts_async
# ---------------------------------------------------------------------------


class TestCollectScripts:
    def test_collect_scripts_returns_list(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.return_value = ["a.js", "b.js"]
            result = agent._collect_scripts(page)
            assert result == ["a.js", "b.js"]
        finally:
            agent.close()

    def test_collect_scripts_handles_exception(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.side_effect = RuntimeError("fail")
            result = agent._collect_scripts(page)
            assert result == []
        finally:
            agent.close()

    def test_collect_scripts_handles_none_return(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.return_value = None
            result = agent._collect_scripts(page)
            assert result == []
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_collect_scripts_async_returns_list(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock(return_value=["x.js"])
            result = await agent._collect_scripts_async(page)
            assert result == ["x.js"]
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_collect_scripts_async_handles_exception(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock(side_effect=RuntimeError("fail"))
            result = await agent._collect_scripts_async(page)
            assert result == []
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_collect_scripts_async_handles_none_return(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock(return_value=None)
            result = await agent._collect_scripts_async(page)
            assert result == []
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# _search_param_in_records
# ---------------------------------------------------------------------------


class TestSearchParamInRecords:
    def test_empty_records_returns_none(self) -> None:
        assert ReverseAgent._search_param_in_records([], "x") is None

    def test_finds_param_in_headers(self) -> None:
        records = [{"headers": {"Anti-Content": "abc"}, "url": "", "body": ""}]
        assert ReverseAgent._search_param_in_records(records, "Anti-Content") == "abc"

    def test_finds_param_in_headers_case_insensitive(self) -> None:
        records = [{"headers": {"anti-content": "v1"}, "url": "", "body": ""}]
        assert ReverseAgent._search_param_in_records(records, "Anti-Content") == "v1"

    def test_finds_param_in_url_query(self) -> None:
        records = [
            {"headers": {}, "url": "https://x.example/api?sign=hello&other=world", "body": ""}
        ]
        assert ReverseAgent._search_param_in_records(records, "sign") == "hello"

    def test_finds_param_in_url_query_case_insensitive(self) -> None:
        records = [{"headers": {}, "url": "https://x.example/api?SIGN=hello", "body": ""}]
        assert ReverseAgent._search_param_in_records(records, "sign") == "hello"

    def test_finds_param_in_json_body(self) -> None:
        records = [{"headers": {}, "url": "", "body": json.dumps({"Anti-Content": "json-value"})}]
        assert ReverseAgent._search_param_in_records(records, "Anti-Content") == "json-value"

    def test_finds_param_in_form_body(self) -> None:
        records = [{"headers": {}, "url": "", "body": "sign=form-value&other=x"}]
        assert ReverseAgent._search_param_in_records(records, "sign") == "form-value"

    def test_returns_none_when_param_not_found(self) -> None:
        records = [{"headers": {"X-Other": "v"}, "url": "https://x.example", "body": ""}]
        assert ReverseAgent._search_param_in_records(records, "sign") is None

    def test_handles_records_with_none_fields(self) -> None:
        records = [{"headers": None, "url": None, "body": None}]
        # 不应崩溃
        assert ReverseAgent._search_param_in_records(records, "x") is None

    def test_handles_url_without_query_string(self) -> None:
        """URL 中无 query 时 url 分支虽然命中 lower 比较，但 parse_qs 返回空。"""
        records = [{"headers": {}, "url": "https://x.example/no-query", "body": ""}]
        # "no-query" 不含目标 "sign"，所以不会进入 query 解析
        assert ReverseAgent._search_param_in_records(records, "sign") is None

    def test_handles_body_invalid_json_then_form(self) -> None:
        """body 包含目标参数但 JSON 解析失败时回退到 form 解析。"""
        records = [{"headers": {}, "url": "", "body": "Anti-Content=form-val"}]
        # 不是合法 JSON 但包含目标参数，应回退到 form 解析
        assert ReverseAgent._search_param_in_records(records, "Anti-Content") == "form-val"


# ---------------------------------------------------------------------------
# _try_extract_param / _try_extract_param_async / _read_hook_records
# ---------------------------------------------------------------------------


class TestTryExtractParam:
    def test_try_extract_param_merges_fresh_and_cached(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.return_value = [{"headers": {"sign": "fresh"}}]
            agent._hook_data_cache = {"records": [{"headers": {"sign": "cached"}}]}
            result = agent._try_extract_param(page, "sign")
            # 应优先返回 fresh 中的值
            assert result == "fresh"
        finally:
            agent.close()

    def test_try_extract_param_falls_back_to_cache(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.side_effect = RuntimeError("eval fail")
            agent._hook_data_cache = {"records": [{"headers": {"sign": "cached"}}]}
            result = agent._try_extract_param(page, "sign")
            assert result == "cached"
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_try_extract_param_async_merges(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock(return_value=[{"headers": {"sign": "fresh-async"}}])
            agent._hook_data_cache = {"records": []}
            result = await agent._try_extract_param_async(page, "sign")
            assert result == "fresh-async"
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_try_extract_param_async_falls_back_to_cache(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock(side_effect=RuntimeError("fail"))
            agent._hook_data_cache = {"records": [{"headers": {"sign": "cached-async"}}]}
            result = await agent._try_extract_param_async(page, "sign")
            assert result == "cached-async"
        finally:
            agent.close()


class TestReadHookData:
    def test_read_hook_data_returns_records_and_count(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.return_value = [{"a": 1}, {"b": 2}]
            result = agent._read_hook_data(page)
            assert result["count"] == 2
            assert len(result["records"]) == 2
        finally:
            agent.close()

    def test_read_hook_data_handles_exception(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.side_effect = RuntimeError("fail")
            result = agent._read_hook_data(page)
            assert result == {"records": [], "count": 0}
        finally:
            agent.close()

    def test_read_hook_data_handles_none(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.return_value = None
            result = agent._read_hook_data(page)
            assert result["count"] == 0
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_read_hook_data_async_success(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock(return_value=[{"x": 1}])
            result = await agent._read_hook_data_async(page)
            assert result["count"] == 1
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_read_hook_data_async_handles_exception(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock(side_effect=RuntimeError("fail"))
            result = await agent._read_hook_data_async(page)
            assert result == {"records": [], "count": 0}
        finally:
            agent.close()

    def test_read_hook_records_merges_fresh_and_cached(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.return_value = [{"fresh": 1}]
            agent._hook_data_cache = {"records": [{"cached": 2}]}
            records = agent._read_hook_records(page)
            assert {"fresh": 1} in records
            assert {"cached": 2} in records
            assert len(records) == 2
        finally:
            agent.close()

    def test_read_hook_records_handles_exception(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate.side_effect = RuntimeError("fail")
            agent._hook_data_cache = {"records": [{"cached": 1}]}
            records = agent._read_hook_records(page)
            assert records == [{"cached": 1}]
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_read_hook_records_async_merges(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock(return_value=[{"fresh": 1}])
            agent._hook_data_cache = {"records": [{"cached": 2}]}
            records = await agent._read_hook_records_async(page)
            assert len(records) == 2
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_read_hook_records_async_handles_exception(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.evaluate = AsyncMock(side_effect=RuntimeError("fail"))
            agent._hook_data_cache = {"records": []}
            records = await agent._read_hook_records_async(page)
            assert records == []
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# _build_think_prompt + _format_* summary 系列
# ---------------------------------------------------------------------------


class TestBuildThinkPrompt:
    def test_prompt_contains_all_sections(self) -> None:
        agent = _make_agent(target_params=["sign"])
        try:
            obs = Observation(
                url="https://x.example",
                hook_data={"count": 5, "records": [{"type": "fetch", "url": "u"}]},
                network_requests=[{"method": "GET", "url": "n", "resource_type": "xhr"}],
                scripts=["a.js"],
                captcha_type=CaptchaType.NONE,
                page_title="T",
                dom_summary="dom content",
            )
            history = [{"step": 1, "action": "click", "reasoning": "test"}]
            prompt = agent._build_think_prompt(obs, "task", history)
            assert "task" in prompt
            assert "https://x.example" in prompt
            assert "T" in prompt
            assert "5" in prompt  # hook_count
            assert "1" in prompt  # network/script count
            assert "dom content" in prompt
            assert "sign" in prompt
            assert "step 1: click" in prompt
        finally:
            agent.close()

    def test_prompt_uses_unspecified_when_no_target_params(self) -> None:
        agent = _make_agent()
        try:
            obs = Observation(
                url="u",
                hook_data={},
                network_requests=[],
                scripts=[],
                captcha_type=CaptchaType.NONE,
                page_title="t",
                dom_summary="d",
            )
            prompt = agent._build_think_prompt(obs, "", [])
            assert "(未指定)" in prompt  # task 与 target_params 都未指定
            assert "(无)" in prompt  # history 为空
        finally:
            agent.close()


class TestFormatHookSummary:
    def test_empty_records_returns_no(self) -> None:
        assert ReverseAgent._format_hook_summary({"records": []}) == "(无)"

    def test_empty_dict_returns_no(self) -> None:
        assert ReverseAgent._format_hook_summary({}) == "(无)"

    def test_formats_records_with_type_method_url(self) -> None:
        result = ReverseAgent._format_hook_summary(
            {
                "records": [
                    {"type": "fetch", "method": "GET", "url": "https://x.example"},
                    {"type": "xhr", "method": "POST", "url": "https://y.example"},
                ]
            }
        )
        assert "[fetch] GET https://x.example" in result
        assert "[xhr] POST https://y.example" in result

    def test_includes_headers_and_body_when_present(self) -> None:
        result = ReverseAgent._format_hook_summary(
            {
                "records": [
                    {
                        "type": "fetch",
                        "method": "GET",
                        "url": "u",
                        "headers": {"k1": "v1", "k2": "v2"},
                        "body": "body content",
                    }
                ]
            }
        )
        assert "headers:" in result
        assert "k1=v1" in result
        assert "body:" in result

    def test_truncates_long_header_values(self) -> None:
        """header 值超过 200 字符应被截断，防止 token 膨胀与提示注入。"""
        long_value = "x" * 500
        result = ReverseAgent._format_hook_summary(
            {
                "records": [
                    {
                        "type": "fetch",
                        "method": "GET",
                        "url": "u",
                        "headers": {"X-Long": long_value},
                    }
                ]
            }
        )
        assert "x" * 200 in result
        assert "x" * 201 not in result

    def test_limits_to_last_20_records(self) -> None:
        records = [{"type": "fetch", "method": "GET", "url": f"u-{i}"} for i in range(30)]
        result = ReverseAgent._format_hook_summary({"records": records})
        # 只保留最后 20 条（u-10 ~ u-29）
        assert "u-9" not in result  # 第 9 条已被丢弃
        assert "u-10" in result  # 倒数第 20 条保留
        assert "u-29" in result  # 最后一条应在

    def test_handles_non_dict_headers(self) -> None:
        """headers 非 dict 时不应崩溃。"""
        result = ReverseAgent._format_hook_summary(
            {"records": [{"type": "fetch", "headers": "not-a-dict", "url": "u", "method": "GET"}]}
        )
        assert "[fetch] GET u" in result


class TestFormatNetworkSummary:
    def test_empty_returns_no(self) -> None:
        assert ReverseAgent._format_network_summary([]) == "(无)"

    def test_formats_requests(self) -> None:
        result = ReverseAgent._format_network_summary(
            [
                {"method": "GET", "url": "https://a.example", "resource_type": "xhr"},
                {"method": "POST", "url": "https://b.example", "resource_type": "fetch"},
            ]
        )
        assert "[xhr] GET https://a.example" in result
        assert "[fetch] POST https://b.example" in result

    def test_handles_missing_fields(self) -> None:
        result = ReverseAgent._format_network_summary([{}])
        # 缺字段时用 "?" 占位
        assert "[?]" in result


class TestFormatScriptSummary:
    def test_empty_returns_no(self) -> None:
        assert ReverseAgent._format_script_summary([]) == "(无)"

    def test_formats_scripts(self) -> None:
        result = ReverseAgent._format_script_summary(["a.js", "b.js", "c.js"])
        assert "a.js" in result
        assert "b.js" in result
        assert "c.js" in result

    def test_limits_to_20_scripts(self) -> None:
        scripts = [f"s{i}.js" for i in range(30)]
        result = ReverseAgent._format_script_summary(scripts)
        assert "s0.js" in result  # 前 20 条应保留
        assert "s29.js" not in result  # 第 21 条以后被丢弃


class TestFormatHistorySummary:
    def test_empty_returns_no(self) -> None:
        assert ReverseAgent._format_history_summary([]) == "(无)"

    def test_formats_history_entries(self) -> None:
        result = ReverseAgent._format_history_summary(
            [
                {"step": 1, "action": "click", "reasoning": "clicked"},
                {"step": 2, "event": "observe_error", "error": "boom"},
            ]
        )
        assert "step 1: click" in result
        assert "clicked" in result
        assert "step 2: observe_error" in result
        assert "boom" in result

    def test_limits_to_last_10_entries(self) -> None:
        history = [{"step": i, "action": "wait"} for i in range(20)]
        result = ReverseAgent._format_history_summary(history)
        assert "step 10" in result  # 最后 10 条包含 step 10-19
        assert "step 0" not in result  # 早期条目被丢弃

    def test_truncates_reasoning_to_150_chars(self) -> None:
        long_reasoning = "x" * 200
        result = ReverseAgent._format_history_summary(
            [{"step": 1, "action": "wait", "reasoning": long_reasoning}]
        )
        # reasoning 应被截断到 150 字符
        assert "x" * 150 in result
        assert "x" * 200 not in result


# ---------------------------------------------------------------------------
# _setup_page_listeners / _create_page / _try_recover_page
# ---------------------------------------------------------------------------


class TestSetupPageListeners:
    def test_registers_request_listener(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            page.on = MagicMock()
            agent._setup_page_listeners(page)
            page.on.assert_called_once()
            args = page.on.call_args[0]
            assert args[0] == "request"
            assert callable(args[1])
        finally:
            agent.close()

    def test_request_listener_appends_to_network_log(self) -> None:
        agent = _make_agent()
        try:
            page = MagicMock()
            agent._setup_page_listeners(page)
            # 取出注册的 handler 调用
            handler = page.on.call_args[0][1]
            req = MagicMock()
            req.url = "https://x.example/api"
            req.method = "GET"
            req.resource_type = "xhr"
            req.headers = {"X-H": "v"}
            req.post_data = "body"
            handler(req)
            assert len(agent._network_log) == 1
            entry = agent._network_log[0]
            assert entry["url"] == "https://x.example/api"
            assert entry["method"] == "GET"
            assert entry["resource_type"] == "xhr"
            assert entry["post_data"] == "body"
        finally:
            agent.close()

    def test_request_listener_handles_exception(self) -> None:
        """req 属性访问抛异常时不崩溃。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            agent._setup_page_listeners(page)
            handler = page.on.call_args[0][1]

            class _BadReq:
                @property
                def url(self) -> str:
                    raise RuntimeError("bad")

            handler(_BadReq())
            # 不应崩溃，network_log 应保持空
            assert agent._network_log == []
        finally:
            agent.close()

    def test_setup_page_listeners_handles_page_on_failure(self) -> None:
        """page.on 抛异常时不崩溃。"""
        agent = _make_agent()
        try:
            page = MagicMock()
            page.on.side_effect = RuntimeError("on fail")
            # 不应抛异常
            agent._setup_page_listeners(page)
        finally:
            agent.close()

    def test_setup_page_listeners_clears_existing_network_log(self) -> None:
        agent = _make_agent()
        try:
            agent._network_log.append({"url": "old"})
            page = MagicMock()
            agent._setup_page_listeners(page)
            assert agent._network_log == []
        finally:
            agent.close()


class TestCreatePage:
    def test_create_page_returns_context_and_page(self) -> None:
        """_create_page 应调用 fetcher._ensure_browser 并注入 Hook。"""
        agent = _make_agent(hooks=["fetch_hook"])
        try:
            # Mock fetcher
            fetcher = MagicMock()
            browser = MagicMock()
            context = MagicMock()
            page = MagicMock()
            browser.new_context.return_value = context
            context.new_page.return_value = page
            fetcher._ensure_browser.return_value = browser
            fetcher.extra_headers = {}
            fetcher.verify = True
            agent.fetcher = fetcher

            ctx, p = agent._create_page(["fetch_hook"])
            assert ctx is context
            assert p is page
            browser.new_context.assert_called_once()
            context.add_init_script.assert_called_once()
            context.new_page.assert_called_once()
            fetcher._setup_page.assert_called_once_with(page)
        finally:
            agent.close()

    def test_create_page_handles_init_script_exception(self) -> None:
        """add_init_script 抛异常时不崩溃。"""
        agent = _make_agent()
        try:
            fetcher = MagicMock()
            browser = MagicMock()
            context = MagicMock()
            page = MagicMock()
            browser.new_context.return_value = context
            context.new_page.return_value = page
            context.add_init_script.side_effect = RuntimeError("init fail")
            fetcher._ensure_browser.return_value = browser
            fetcher.extra_headers = None
            fetcher.verify = True
            agent.fetcher = fetcher

            ctx, p = agent._create_page(None)
            assert ctx is context
            assert p is page
        finally:
            agent.close()

    def test_create_page_handles_setup_page_exception(self) -> None:
        """fetcher._setup_page 抛异常时不崩溃。"""
        agent = _make_agent()
        try:
            fetcher = MagicMock()
            browser = MagicMock()
            context = MagicMock()
            page = MagicMock()
            browser.new_context.return_value = context
            context.new_page.return_value = page
            fetcher._ensure_browser.return_value = browser
            fetcher._setup_page.side_effect = RuntimeError("setup fail")
            fetcher.extra_headers = None
            fetcher.verify = True
            agent.fetcher = fetcher

            _ctx, p = agent._create_page(None)
            assert p is page
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_create_page_async_returns_context_and_page(self) -> None:
        agent = _make_agent(hooks=["fetch_hook"])
        try:
            fetcher = MagicMock()
            browser = MagicMock()
            context = MagicMock()
            page = MagicMock()
            browser.new_context = AsyncMock(return_value=context)
            context.new_page = AsyncMock(return_value=page)
            context.add_init_script = AsyncMock()
            fetcher._ensure_async_browser = AsyncMock(return_value=browser)
            fetcher._setup_page_async = AsyncMock()
            fetcher.extra_headers = None
            fetcher.verify = True
            agent.fetcher = fetcher

            ctx, p = await agent._create_page_async(["fetch_hook"])
            assert ctx is context
            assert p is page
            browser.new_context.assert_awaited_once()
            context.add_init_script.assert_awaited_once()
            context.new_page.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_create_page_async_handles_init_script_exception(self) -> None:
        agent = _make_agent()
        try:
            fetcher = MagicMock()
            browser = MagicMock()
            context = MagicMock()
            page = MagicMock()
            browser.new_context = AsyncMock(return_value=context)
            context.new_page = AsyncMock(return_value=page)
            context.add_init_script = AsyncMock(side_effect=RuntimeError("init fail"))
            fetcher._ensure_async_browser = AsyncMock(return_value=browser)
            fetcher._setup_page_async = AsyncMock()
            fetcher.extra_headers = None
            fetcher.verify = True
            agent.fetcher = fetcher

            _ctx, p = await agent._create_page_async(None)
            assert p is page
        finally:
            agent.close()


class TestTryRecoverPage:
    def test_try_recover_page_success(self) -> None:
        agent = _make_agent()
        try:
            # 让 _create_page 返回 mock 对象
            new_page = MagicMock()
            agent._create_page = MagicMock(return_value=(MagicMock(), new_page))  # type: ignore[assignment]
            ok, page = agent._try_recover_page("https://x.example")
            assert ok is True
            assert page is new_page
            assert agent._page is new_page
            agent._create_page.assert_called_once()
        finally:
            agent.close()

    def test_try_recover_page_failure(self) -> None:
        agent = _make_agent()
        try:
            agent._create_page = MagicMock(side_effect=RuntimeError("create fail"))  # type: ignore[assignment]
            ok, page = agent._try_recover_page("https://x.example")
            assert ok is False
            assert page is None
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_try_recover_page_async_success(self) -> None:
        agent = _make_agent()
        try:
            ctx = MagicMock()
            page = MagicMock()
            page.goto = AsyncMock()
            agent._create_page_async = AsyncMock(return_value=(ctx, page))  # type: ignore[assignment]
            ok, new_page = await agent._try_recover_page_async("https://x.example")
            assert ok is True
            assert new_page is page
            assert agent._page is page
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_try_recover_page_async_failure(self) -> None:
        agent = _make_agent()
        try:
            agent._create_page_async = AsyncMock(side_effect=RuntimeError("create fail"))  # type: ignore[assignment]
            ok, page = await agent._try_recover_page_async("https://x.example")
            assert ok is False
            assert page is None
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# _analyze_captured_js
# ---------------------------------------------------------------------------


class TestAnalyzeCapturedJs:
    @staticmethod
    def _fake_httpx_client(responses: list[Any]) -> MagicMock:
        """构造带 __enter__ 自返的 httpx.Client mock，get 依次返回给定响应。"""
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.side_effect = responses
        return client

    @staticmethod
    def _fake_resp(url: str, status: int = 200, text: str = "") -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.text = text
        resp.url = url
        resp.content = text.encode("utf-8")
        return resp

    def test_returns_none_when_no_scripts(self) -> None:
        agent = _make_agent()
        try:
            result = agent._analyze_captured_js([], ["sign"])
            assert result is None
        finally:
            agent.close()

    def test_returns_none_when_all_scripts_fail_to_fetch(self) -> None:
        """httpx.Client.get 抛异常时所有 fragment 失败，返回 None。"""
        agent = _make_agent()
        try:
            client = self._fake_httpx_client([])
            client.get.side_effect = RuntimeError("network fail")
            with patch("httpx.Client", return_value=client):
                result = agent._analyze_captured_js(
                    ["https://x.example/a.js", "https://x.example/b.js"], ["sign"]
                )
                assert result is None
        finally:
            agent.close()

    def test_returns_none_when_status_not_200(self) -> None:
        agent = _make_agent()
        try:
            client = self._fake_httpx_client(
                [self._fake_resp("https://x.example/a.js", status=404)]
            )
            with patch("httpx.Client", return_value=client):
                result = agent._analyze_captured_js(["https://x.example/a.js"], ["sign"])
                assert result is None
        finally:
            agent.close()

    def test_returns_none_when_text_empty(self) -> None:
        agent = _make_agent()
        try:
            client = self._fake_httpx_client([self._fake_resp("https://x.example/a.js")])
            with patch("httpx.Client", return_value=client):
                result = agent._analyze_captured_js(["https://x.example/a.js"], ["sign"])
                assert result is None
        finally:
            agent.close()

    def test_skips_unsafe_script_url(self) -> None:
        """内网/localhost 或非白名单域的脚本 URL 不应被拉取。"""
        agent = _make_agent()
        try:
            client = self._fake_httpx_client(
                [self._fake_resp("http://127.0.0.1/a.js", status=200, text="code")]
            )
            with patch("httpx.Client", return_value=client):
                result = agent._analyze_captured_js(["http://127.0.0.1/a.js"], ["sign"])
                assert result is None
            client.get.assert_not_called()
        finally:
            agent.close()

    def test_skips_script_fetch_above_size_cap(self) -> None:
        """超过内容大小上限的脚本应被跳过。"""
        agent = _make_agent()
        try:
            big = self._fake_resp(
                "https://x.example/big.js", text="x" * (agent._MAX_JS_FETCH_BYTES + 1)
            )
            client = self._fake_httpx_client([big])
            with patch("httpx.Client", return_value=client):
                result = agent._analyze_captured_js(["https://x.example/big.js"], ["sign"])
                assert result is None
        finally:
            agent.close()

    def test_skips_script_outside_allowed_domains(self) -> None:
        """配置 allowed_domains 白名单时，白名单外脚本不应被拉取。"""
        cfg = ReverseAgentConfig(
            enable_screenshot=False,
            enable_guard=False,
            enable_judge=False,
            enable_recorder=False,
            planner_interval=None,
            humanize_input=False,
            allowed_domains=["example.com"],
        )
        agent = ReverseAgent(config=cfg, provider=StubProvider())
        try:
            client = self._fake_httpx_client(
                [self._fake_resp("https://evil.com/a.js", text="code")]
            )
            with patch("httpx.Client", return_value=client):
                result = agent._analyze_captured_js(["https://evil.com/a.js"], ["sign"])
                assert result is None
            client.get.assert_not_called()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_analyze_captured_js_async_skips_unsafe_url(self) -> None:
        """异步路径同样拒绝内网脚本 URL（httpx.AsyncClient）。"""
        agent = _make_agent()
        try:
            client = self._fake_httpx_client(
                [self._fake_resp("http://127.0.0.1/a.js", text="code")]
            )
            client.__aenter__.return_value = client
            with patch("httpx.AsyncClient", return_value=client):
                result = await agent._analyze_captured_js_async(["http://127.0.0.1/a.js"], ["sign"])
                assert result is None
            client.get.assert_not_called()
        finally:
            agent.close()

    def test_returns_best_result_by_confidence(self) -> None:
        """应按 confidence + target 命中选择最优 fragment。"""
        agent = _make_agent()
        try:
            # Mock analyzer
            good_result = MagicMock()
            good_result.confidence = 0.9
            good_result.inputs = ["sign-input"]
            bad_result = MagicMock()
            bad_result.confidence = 0.2
            bad_result.inputs = []
            agent.analyzer.analyze_fragment = MagicMock(side_effect=[bad_result, good_result])  # type: ignore[assignment]

            client = self._fake_httpx_client(
                [
                    self._fake_resp("https://x.example/a.js", text="var x = 1;"),
                    self._fake_resp("https://x.example/b.js", text="var sign = function() {};"),
                ]
            )
            with patch("httpx.Client", return_value=client):
                result = agent._analyze_captured_js(
                    ["https://x.example/a.js", "https://x.example/b.js"], ["sign"]
                )
                # 应选择 good_result（confidence=0.9 + target 命中加 0.5 = 1.4）
                assert result is good_result
        finally:
            agent.close()

    def test_skips_analyzer_exception(self) -> None:
        """analyzer.analyze_fragment 抛异常时跳过该 fragment。"""
        agent = _make_agent()
        try:
            good_result = MagicMock()
            good_result.confidence = 0.8
            good_result.inputs = []
            agent.analyzer.analyze_fragment = MagicMock(
                side_effect=[RuntimeError("analyze fail"), good_result]
            )  # type: ignore[assignment]

            client = self._fake_httpx_client(
                [
                    self._fake_resp("https://x.example/a.js", text="code"),
                    self._fake_resp("https://x.example/b.js", text="code"),
                ]
            )
            with patch("httpx.Client", return_value=client):
                result = agent._analyze_captured_js(
                    ["https://x.example/a.js", "https://x.example/b.js"], []
                )
                # 第一个 fragment 抛异常被跳过，第二个返回 good_result
                assert result is good_result
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# _screenshot_dir / _screenshot_task_id / checkpoints_snapshot
# ---------------------------------------------------------------------------


class TestScreenshotHelpers:
    def test_screenshot_dir_is_cwd_reverse_screenshots(self, tmp_path: Path) -> None:
        agent = _make_agent()
        try:
            import os

            old_cwd = os.getcwd()
            os.chdir(tmp_path)
            try:
                d = agent._screenshot_dir()
                assert d == tmp_path / "reverse_screenshots"
            finally:
                os.chdir(old_cwd)
        finally:
            agent.close()

    def test_screenshot_task_id_defaults_to_default(self) -> None:
        agent = _make_agent()
        try:
            assert agent._screenshot_task_id() == "default"
        finally:
            agent.close()

    def test_screenshot_task_id_uses_checkpoint_manager_task_id(self) -> None:
        agent = _make_agent()
        try:
            agent.checkpoint_manager.task_id = "task-abc"
            assert agent._screenshot_task_id() == "task-abc"
        finally:
            agent.close()

    def test_screenshot_task_id_handles_exception(self) -> None:
        agent = _make_agent()
        try:
            # checkpoint_manager 抛异常时回退为 default
            agent.checkpoint_manager = MagicMock()  # type: ignore[assignment]
            type(agent.checkpoint_manager).task_id = property(  # type: ignore[misc]
                lambda self: (_ for _ in ()).throw(RuntimeError("no tid"))
            )
            assert agent._screenshot_task_id() == "default"
        finally:
            agent.close()

    def test_screenshot_filename_sanitized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """task_id 含路径分隔符时应被清理，防止路径穿越。"""
        cfg = ReverseAgentConfig(
            enable_screenshot=True,
            enable_guard=False,
            enable_judge=False,
            enable_recorder=False,
            planner_interval=None,
            humanize_input=False,
        )
        agent = ReverseAgent(config=cfg, provider=StubProvider())
        try:
            monkeypatch.chdir(tmp_path)
            agent.checkpoint_manager.task_id = "../../evil"
            page = MagicMock()
            page.screenshot.return_value = b""
            path = agent._take_screenshot(page, 1)
            # 文件名中不应出现 ".." 或路径分隔符（Windows 为 \，POSIX 为 /），
            # 分隔符被替换为 "_"；用 basename 断言以保证平台无关
            assert ".." not in path
            basename = Path(path).name
            assert "/" not in basename and "\\" not in basename
            assert basename.endswith("_step1.png")
            assert path.endswith("_step1.png")
        finally:
            agent.close()

    def test_screenshot_rotation_keeps_latest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """超出保留上限时按任务清理最旧截图。"""
        cfg = ReverseAgentConfig(
            enable_screenshot=True,
            enable_guard=False,
            enable_judge=False,
            enable_recorder=False,
            planner_interval=None,
            humanize_input=False,
        )
        agent = ReverseAgent(config=cfg, provider=StubProvider())
        try:
            monkeypatch.chdir(tmp_path)
            agent.checkpoint_manager.task_id = "task-rot"
            page = MagicMock()

            def _fake_screenshot(*, path: str = "", **kwargs: Any) -> bytes:
                Path(path).write_bytes(b"x")
                return b"x"

            page.screenshot = _fake_screenshot
            # 预填超过上限的旧截图
            out_dir = agent._screenshot_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            for i in range(agent._MAX_SCREENSHOTS_PER_TASK + 5):
                (out_dir / f"task-rot_step{i}.png").write_bytes(b"x")
            agent._take_screenshot(page, 999)
            remaining = list(out_dir.glob("task-rot_step*.png"))
            assert len(remaining) <= agent._MAX_SCREENSHOTS_PER_TASK
            # 最新的 step999 应保留
            assert (out_dir / "task-rot_step999.png").exists()
        finally:
            agent.close()


class TestCheckpointsSnapshot:
    def test_returns_empty_when_checkpoint_disabled(self) -> None:
        agent = _make_agent()  # enable_checkpoint=False
        try:
            assert agent.checkpoints_snapshot() == []
        finally:
            agent.close()

    def test_returns_empty_when_no_task_id(self) -> None:
        """enable_checkpoint=True 但 task_id 为空时返回空。"""
        cfg = ReverseAgentConfig(
            enable_screenshot=False,
            enable_guard=False,
            enable_judge=False,
            enable_recorder=False,
            planner_interval=None,
            humanize_input=False,
            enable_checkpoint=True,
        )
        agent = ReverseAgent(config=cfg, provider=StubProvider())
        try:
            assert agent.checkpoints_snapshot() == []
        finally:
            agent.close()

    def test_returns_list_when_task_id_and_paths_exist(self) -> None:
        cfg = ReverseAgentConfig(
            enable_screenshot=False,
            enable_guard=False,
            enable_judge=False,
            enable_recorder=False,
            planner_interval=None,
            humanize_input=False,
            enable_checkpoint=True,
        )
        agent = ReverseAgent(config=cfg, provider=StubProvider())
        try:
            agent.checkpoint_manager.task_id = "task-xyz"
            # Mock store.list_checkpoints 返回带 step-N 命名的路径
            paths = [Path("/tmp/task-xyz/step-0007"), Path("/tmp/task-xyz/step-0014")]
            agent.checkpoint_manager.store.list_checkpoints = MagicMock(return_value=paths)  # type: ignore[assignment]
            result = agent.checkpoints_snapshot()
            assert len(result) == 2
            assert {"step": 7, "path": str(paths[0])} in result
            assert {"step": 14, "path": str(paths[1])} in result
        finally:
            agent.close()

    def test_handles_invalid_step_filename(self) -> None:
        cfg = ReverseAgentConfig(
            enable_screenshot=False,
            enable_guard=False,
            enable_judge=False,
            enable_recorder=False,
            planner_interval=None,
            humanize_input=False,
            enable_checkpoint=True,
        )
        agent = ReverseAgent(config=cfg, provider=StubProvider())
        try:
            agent.checkpoint_manager.task_id = "task-xyz"
            paths = [Path("/tmp/task-xyz/not-a-step")]
            agent.checkpoint_manager.store.list_checkpoints = MagicMock(return_value=paths)  # type: ignore[assignment]
            result = agent.checkpoints_snapshot()
            assert result == [{"step": 0, "path": str(paths[0])}]
        finally:
            agent.close()

    def test_handles_store_exception(self) -> None:
        cfg = ReverseAgentConfig(
            enable_screenshot=False,
            enable_guard=False,
            enable_judge=False,
            enable_recorder=False,
            planner_interval=None,
            humanize_input=False,
            enable_checkpoint=True,
        )
        agent = ReverseAgent(config=cfg, provider=StubProvider())
        try:
            agent.checkpoint_manager.task_id = "task-xyz"
            agent.checkpoint_manager.store.list_checkpoints = MagicMock(
                side_effect=RuntimeError("store fail")
            )  # type: ignore[assignment]
            assert agent.checkpoints_snapshot() == []
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# 资源清理：close / aclose / __enter__ / __exit__ / __aenter__ / __aexit__
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_close_closes_page_and_context_and_fetcher(self) -> None:
        agent = _make_agent()
        page = MagicMock()
        context = MagicMock()
        fetcher = MagicMock()
        agent._page = page
        agent._context = context
        agent.fetcher = fetcher
        agent.close()
        page.close.assert_called_once()
        context.close.assert_called_once()
        fetcher.close.assert_called_once()
        assert agent._page is None
        assert agent._context is None
        assert agent.fetcher is None

    def test_close_handles_exceptions_silently(self) -> None:
        """close 时各组件抛异常不应传播。"""
        agent = _make_agent()
        page = MagicMock()
        page.close.side_effect = RuntimeError("page close fail")
        context = MagicMock()
        context.close.side_effect = RuntimeError("context close fail")
        fetcher = MagicMock()
        fetcher.close.side_effect = RuntimeError("fetcher close fail")
        agent._page = page
        agent._context = context
        agent.fetcher = fetcher
        # 不应抛异常
        agent.close()
        assert agent._page is None
        assert agent._context is None
        assert agent.fetcher is None

    def test_close_when_no_page_context_fetcher(self) -> None:
        """未启动浏览器时 close 不应崩溃。"""
        agent = _make_agent()
        agent.close()  # 不抛异常即通过

    @pytest.mark.asyncio
    async def test_aclose_closes_async(self) -> None:
        agent = _make_agent()
        page = MagicMock()
        page.close = AsyncMock()
        context = MagicMock()
        context.close = AsyncMock()
        fetcher = MagicMock()
        fetcher.aclose = AsyncMock()
        agent._page = page
        agent._context = context
        agent.fetcher = fetcher
        await agent.aclose()
        page.close.assert_awaited_once()
        context.close.assert_awaited_once()
        fetcher.aclose.assert_awaited_once()
        assert agent._page is None
        assert agent.fetcher is None

    @pytest.mark.asyncio
    async def test_aclose_handles_exceptions(self) -> None:
        """aclose 时各组件抛异常不应传播。"""
        agent = _make_agent()
        page = MagicMock()
        page.close = AsyncMock(side_effect=RuntimeError("close fail"))
        context = MagicMock()
        context.close = AsyncMock(side_effect=RuntimeError("close fail"))
        fetcher = MagicMock()
        fetcher.aclose = AsyncMock(side_effect=RuntimeError("close fail"))
        agent._page = page
        agent._context = context
        agent.fetcher = fetcher
        await agent.aclose()  # 不抛异常即通过
        assert agent._page is None

    def test_context_manager_protocol(self) -> None:
        """__enter__ / __exit__ 应正确工作。"""
        agent = _make_agent()
        with agent as a:
            assert a is agent
        # 退出后应已 close
        assert agent.fetcher is None

    @pytest.mark.asyncio
    async def test_async_context_manager_protocol(self) -> None:
        """__aenter__ / __aexit__ 应正确工作。"""
        agent = _make_agent()
        async with agent as a:
            assert a is agent
        assert agent.fetcher is None

    def test_cleanup_page_sync_closes_page_then_context(self) -> None:
        agent = _make_agent()
        page = MagicMock()
        context = MagicMock()
        agent._page = page
        agent._context = context
        agent._cleanup_page_sync()
        page.close.assert_called_once()
        context.close.assert_called_once()
        assert agent._page is None
        assert agent._context is None

    def test_cleanup_page_sync_handles_exceptions(self) -> None:
        agent = _make_agent()
        page = MagicMock()
        page.close.side_effect = RuntimeError("fail")
        context = MagicMock()
        context.close.side_effect = RuntimeError("fail")
        agent._page = page
        agent._context = context
        agent._cleanup_page_sync()  # 不抛异常
        assert agent._page is None
        assert agent._context is None

    @pytest.mark.asyncio
    async def test_cleanup_page_async(self) -> None:
        agent = _make_agent()
        page = MagicMock()
        page.close = AsyncMock()
        context = MagicMock()
        context.close = AsyncMock()
        agent._page = page
        agent._context = context
        await agent._cleanup_page_async()
        page.close.assert_awaited_once()
        context.close.assert_awaited_once()
        assert agent._page is None

    @pytest.mark.asyncio
    async def test_cleanup_page_async_handles_exceptions(self) -> None:
        agent = _make_agent()
        page = MagicMock()
        page.close = AsyncMock(side_effect=RuntimeError("fail"))
        context = MagicMock()
        context.close = AsyncMock(side_effect=RuntimeError("fail"))
        agent._page = page
        agent._context = context
        await agent._cleanup_page_async()  # 不抛异常
        assert agent._page is None


# ---------------------------------------------------------------------------
# _resolve_tab 边界（补充 multi-tab 测试）
# ---------------------------------------------------------------------------


class TestResolveTab:
    def test_resolve_tab_returns_none_when_no_name_no_index(self) -> None:
        agent = _make_agent()
        try:
            assert agent._resolve_tab(name=None, index=None) is None
        finally:
            agent.close()

    def test_resolve_tab_returns_none_for_invalid_index(self) -> None:
        agent = _make_agent()
        try:
            agent._tabs = {"a": "page-a"}
            assert agent._resolve_tab(name=None, index=99) is None
        finally:
            agent.close()

    def test_resolve_tab_returns_none_for_non_int_index(self) -> None:
        agent = _make_agent()
        try:
            agent._tabs = {"a": "page-a"}
            # index 是无法转 int 的字符串
            assert agent._resolve_tab(name=None, index="not-int") is None  # type: ignore[arg-type]
        finally:
            agent.close()

    def test_resolve_tab_by_index_returns_correct_tab(self) -> None:
        agent = _make_agent()
        try:
            agent._tabs = {"first": "p1", "second": "p2"}
            assert agent._resolve_tab(name=None, index=0) == "p1"
            assert agent._resolve_tab(name=None, index=1) == "p2"
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# _do_new_tab / _do_new_tab_async：导航 URL 被护栏拒绝
# ---------------------------------------------------------------------------


class TestDoNewTabGuardDenied:
    def _denied_result(self) -> GuardrailResult:
        return GuardrailResult(
            action=GuardrailAction.DENY,
            matched_rules=["blocked_domain"],
            details=["denied"],
        )

    def test_new_tab_guard_denied_skips_goto(self) -> None:
        """护栏拒绝 new_tab 的导航 URL → 不 goto，仅发 guard.deny 事件。"""
        agent = _make_agent(enable_guard=True)
        try:
            page = MagicMock()
            new_page = MagicMock()
            agent._context = MagicMock()
            agent._context.new_page.return_value = new_page
            agent.fetcher = MagicMock()
            agent.guard.check_navigation_url = MagicMock(  # type: ignore[assignment,union-attr]
                return_value=self._denied_result()
            )
            events: list[Any] = []
            agent.event_bus.subscribe(events.append)
            action = Action(
                action_type="new_tab", params={"url": "https://evil.example", "name": "t1"}
            )
            agent._do_new_tab(page, action, step=1)
            new_page.goto.assert_not_called()
            assert any(getattr(e, "type", None) == "guard.deny" for e in events)
            # 页面仍应切换到新标签
            assert agent._page is new_page
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_new_tab_async_guard_denied_skips_goto(self) -> None:
        """异步路径护栏拒绝 new_tab 的导航 URL → 不 goto，仅发 guard.deny 事件。"""
        agent = _make_agent(enable_guard=True)
        try:
            page = MagicMock()
            new_page = MagicMock()
            new_page.goto = AsyncMock()
            agent._context = MagicMock()
            agent._context.new_page = AsyncMock(return_value=new_page)
            agent.fetcher = MagicMock()
            agent.fetcher._setup_page_async = AsyncMock()
            agent.guard.check_navigation_url = MagicMock(  # type: ignore[assignment,union-attr]
                return_value=self._denied_result()
            )
            events: list[Any] = []
            agent.event_bus.subscribe(events.append)
            action = Action(
                action_type="new_tab", params={"url": "https://evil.example", "name": "t1"}
            )
            with patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()):
                await agent._do_new_tab_async(page, action, step=1)
            new_page.goto.assert_not_called()
            assert any(getattr(e, "type", None) == "guard.deny" for e in events)
            assert agent._page is new_page
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# _is_safe_script_url：SSRF 防护各分支
# ---------------------------------------------------------------------------


class TestIsSafeScriptUrl:
    def test_invalid_url_returns_false(self) -> None:
        """urlparse 解析失败（非法 IPv6 括号）→ 不安全。"""
        agent = _make_agent()
        try:
            assert agent._is_safe_script_url("http://[::1") is False
        finally:
            agent.close()

    def test_non_http_scheme_returns_false(self) -> None:
        """非 http/https 方案（ftp/file/data）→ 不安全。"""
        agent = _make_agent()
        try:
            assert agent._is_safe_script_url("ftp://example.com/a.js") is False
            assert agent._is_safe_script_url("file:///etc/passwd") is False
            assert agent._is_safe_script_url("data:text/javascript,alert(1)") is False
        finally:
            agent.close()

    def test_empty_host_returns_false(self) -> None:
        """host 为空（http:///path）→ 不安全。"""
        agent = _make_agent()
        try:
            assert agent._is_safe_script_url("http:///a.js") is False
        finally:
            agent.close()

    def test_private_link_local_ip_returns_false(self) -> None:
        """内网/回环/链路本地 IP → 不安全。"""
        agent = _make_agent()
        try:
            assert agent._is_safe_script_url("http://10.0.0.1/a.js") is False
            assert agent._is_safe_script_url("http://127.0.0.1/a.js") is False
            assert agent._is_safe_script_url("http://169.254.1.1/a.js") is False
        finally:
            agent.close()

    def test_allowed_domains_wildcard_entry(self) -> None:
        """白名单含 "*" 条目 → 任意域名放行。"""
        agent = _make_agent(allowed_domains=["*", "example.com"])
        try:
            assert agent._is_safe_script_url("https://any.example/a.js") is True
        finally:
            agent.close()

    def test_allowed_domains_subdomain_wildcard(self) -> None:
        """白名单 "*.example.com" → 根域与子域命中。"""
        agent = _make_agent(allowed_domains=["*.example.com"])
        try:
            assert agent._is_safe_script_url("https://example.com/a.js") is True
            assert agent._is_safe_script_url("https://cdn.example.com/a.js") is True
            assert agent._is_safe_script_url("https://evil.net/a.js") is False
        finally:
            agent.close()

    def test_allowed_domains_exact_match(self) -> None:
        """白名单精确域名命中。"""
        agent = _make_agent(allowed_domains=["example.com"])
        try:
            assert agent._is_safe_script_url("https://example.com/a.js") is True
            assert agent._is_safe_script_url("https://other.com/a.js") is False
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# _analyze_captured_js 补充：重定向后目标不安全
# ---------------------------------------------------------------------------


class TestAnalyzeCapturedJsRedirect:
    def test_skips_redirect_to_unsafe_final_url(self) -> None:
        """重定向后的最终 URL 不安全（内网 IP）→ 跳过该脚本。"""
        agent = _make_agent()
        try:
            client = MagicMock()
            client.__enter__.return_value = client
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "var x = 1;"
            resp.url = "http://169.254.169.254/latest"  # 重定向到链路本地
            resp.content = b"var x = 1;"
            client.get.side_effect = [resp]
            with patch("httpx.Client", return_value=client):
                result = agent._analyze_captured_js(["https://x.example/a.js"], ["sign"])
                assert result is None
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_analyze_captured_js_async_all_branches(self) -> None:
        """异步路径覆盖重定向不安全/非200/超限/异常/成功各分支。"""
        agent = _make_agent()
        try:
            good_result = MagicMock()
            good_result.confidence = 0.9
            good_result.inputs = ["sign-input"]
            agent.analyzer.analyze_fragment = MagicMock(  # type: ignore[assignment]
                return_value=good_result
            )

            def _resp(url: str, status: int = 200, text: str = "") -> MagicMock:
                r = MagicMock()
                r.status_code = status
                r.text = text
                r.url = url
                r.content = text.encode("utf-8")
                return r

            redirect_bad = _resp("http://169.254.169.254/latest", text="x")
            not_200 = _resp("https://x.example/b.js", status=404, text="nope")
            too_big = _resp(
                "https://x.example/c.js",
                text="x" * (agent._MAX_JS_FETCH_BYTES + 1),
            )
            ok = _resp("https://x.example/d.js", text="var sign = function() {};")

            client = AsyncMock()
            client.__aenter__.return_value = client
            client.__aexit__ = AsyncMock(return_value=False)
            client.get.side_effect = [redirect_bad, not_200, too_big, ok, RuntimeError("boom")]
            with patch("httpx.AsyncClient", return_value=client):
                result = await agent._analyze_captured_js_async(
                    [
                        "https://x.example/a.js",
                        "https://x.example/b.js",
                        "https://x.example/c.js",
                        "https://x.example/d.js",
                        "https://x.example/e.js",
                    ],
                    ["sign"],
                )
                # 只有 ok 响应进入 fragments → 返回该分析结果
                assert result is good_result
        finally:
            agent.close()


# ---------------------------------------------------------------------------
# _rotate_screenshots：清理异常容错
# ---------------------------------------------------------------------------


class TestRotateScreenshots:
    def test_unlink_failure_swallowed(self) -> None:
        """删除旧截图抛 OSError → 吞掉，不中断。"""

        class _FakePath:
            def __init__(self, name: str) -> None:
                self.name = name

            def __lt__(self, other: Any) -> bool:
                return self.name < other.name

            def unlink(self) -> None:
                raise OSError("file locked")

        class _FakeDir:
            def glob(self, pattern: str) -> list[_FakePath]:
                return [_FakePath(f"t_step{i}.png") for i in range(51)]

        agent = _make_agent()
        try:
            agent._rotate_screenshots(_FakeDir(), "t")  # 不抛异常
        finally:
            agent.close()

    def test_glob_failure_swallowed(self) -> None:
        """glob 抛 OSError（目录不存在）→ 吞掉。"""

        class _FakeDir:
            def glob(self, pattern: str) -> list[Any]:
                raise OSError("no such directory")

        agent = _make_agent()
        try:
            agent._rotate_screenshots(_FakeDir(), "t")  # 不抛异常
        finally:
            agent.close()

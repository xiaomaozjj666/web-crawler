"""Tests for ReverseAgent integration: config, component init, and screenshot."""

from __future__ import annotations

from typing import Any

import pytest


class _FakePage:
    """模拟 Playwright Page 对象，仅实现 screenshot 方法。"""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.screenshot_calls: list[dict[str, Any]] = []

    def screenshot(self, *, path: str = "", **kwargs: Any) -> bytes:
        self.screenshot_calls.append({"path": path, **kwargs})
        if self._fail:
            raise RuntimeError("screenshot not available")
        # 写一个占位文件，模拟真实截图行为
        from pathlib import Path

        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return b"\x89PNG\r\n\x1a\n"


class _FakeAsyncPage:
    """模拟异步 Playwright Page 对象，screenshot 为协程。"""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.screenshot_calls: list[dict[str, Any]] = []

    async def screenshot(self, *, path: str = "", **kwargs: Any) -> bytes:
        self.screenshot_calls.append({"path": path, **kwargs})
        if self._fail:
            raise RuntimeError("screenshot not available")
        from pathlib import Path

        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return b"\x89PNG\r\n\x1a\n"


# -- ReverseAgent 集成新组件的 smoke 测试 -----------------------------------
def test_reverse_agent_config_has_new_fields() -> None:
    """ReverseAgentConfig 新增字段应有合理默认值。"""
    from web_crawler.ai.reverse_agent import ReverseAgentConfig

    cfg = ReverseAgentConfig()
    assert cfg.dom_prune_max_chars == 0  # 默认禁用 DomPruner
    assert cfg.enable_checkpoint is False
    assert cfg.min_confidence == 0.4
    assert cfg.enable_guard is True
    assert cfg.allowed_domains is None


def test_reverse_agent_init_new_components() -> None:
    """ReverseAgent 实例化后应有所有新组件实例。"""
    from web_crawler.ai.reverse_agent import ReverseAgent

    agent = ReverseAgent()
    # 4 个新组件都应存在
    assert agent.confidence_scorer is not None
    assert agent.guard is not None  # 默认启用
    assert agent.checkpoint_manager is not None
    # DomPruner 默认禁用
    assert agent.dom_pruner is None
    # 启用 DomPruner 的配置
    from web_crawler.ai.reverse_agent import ReverseAgentConfig

    cfg = ReverseAgentConfig(dom_prune_max_chars=4000)
    agent2 = ReverseAgent(config=cfg)
    assert agent2.dom_pruner is not None
    assert agent2.dom_pruner.max_chars == 4000


def test_reverse_agent_arun_signature_unchanged() -> None:
    """arun 仍是 async 协程且签名不变。"""
    import inspect

    from web_crawler.ai.reverse_agent import ReverseAgent

    assert inspect.iscoroutinefunction(ReverseAgent.arun)
    sig = inspect.signature(ReverseAgent.arun)
    assert "url" in sig.parameters
    assert "task" in sig.parameters


# -- 截图功能 ---------------------------------------------------------------


def test_screenshot_disabled_returns_empty() -> None:
    """enable_screenshot=False 时截图返回空字符串。"""
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    agent = ReverseAgent(config=ReverseAgentConfig(enable_screenshot=False))
    page = _FakePage()
    result = agent._take_screenshot(page, step=1)
    assert result == ""
    assert agent._screenshots == []
    agent.close()


def test_screenshot_success_returns_path_and_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """启用截图时成功保存文件，返回路径并记录到 _screenshots。"""
    import os

    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    monkeypatch.chdir(tmp_path)
    agent = ReverseAgent(config=ReverseAgentConfig(enable_screenshot=True))
    try:
        page = _FakePage()
        result = agent._take_screenshot(page, step=1)
        assert result != ""
        assert os.path.exists(result)
        assert result.endswith("_step1.png")
        assert len(agent._screenshots) == 1
        assert agent._screenshots[0]["step"] == 1
        assert agent._screenshots[0]["error"] is False
        assert agent._last_error_screenshot == ""
    finally:
        agent.close()


def test_screenshot_error_marked_and_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """error=True 时截图路径带 _error 后缀并标记 error_screenshot。"""
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    monkeypatch.chdir(tmp_path)
    agent = ReverseAgent(config=ReverseAgentConfig(enable_screenshot=True))
    try:
        page = _FakePage()
        result = agent._take_screenshot(page, step=3, error=True)
        assert result != ""
        assert "_error" in result
        assert agent._screenshots[0]["error"] is True
        assert agent._last_error_screenshot == result
    finally:
        agent.close()


def test_screenshot_failure_returns_empty_and_no_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """page.screenshot 抛异常时返回空字符串，不崩溃，不记录。"""
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    monkeypatch.chdir(tmp_path)
    agent = ReverseAgent(config=ReverseAgentConfig(enable_screenshot=True))
    try:
        page = _FakePage(fail=True)
        result = agent._take_screenshot(page, step=1)
        assert result == ""
        assert agent._screenshots == []
    finally:
        agent.close()


def test_screenshot_none_page_returns_empty() -> None:
    """page=None 时截图返回空字符串。"""
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    agent = ReverseAgent(config=ReverseAgentConfig(enable_screenshot=True))
    result = agent._take_screenshot(None, step=1)
    assert result == ""
    agent.close()


def test_async_screenshot_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """异步截图版本也正确保存文件。"""
    import asyncio
    import os

    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    monkeypatch.chdir(tmp_path)
    agent = ReverseAgent(config=ReverseAgentConfig(enable_screenshot=True))
    try:
        page = _FakeAsyncPage()

        async def _run() -> str:
            return await agent._take_screenshot_async(page, step=1)

        result = asyncio.run(_run())
        assert result != ""
        assert os.path.exists(result)
        assert len(agent._screenshots) == 1
    finally:
        agent.close()

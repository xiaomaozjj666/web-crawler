"""ReverseAgent run()/arun() 主循环测试 + 剩余小分支覆盖。

覆盖 reverse_agent.py 中 run() (418-778) 和 arun() (786-1147) 的全部主循环
分支，以及若干未被既有测试文件覆盖的小段代码（1554/1725/1768/1795/2023/
2264/2312/2314）。

测试策略：mock CamoufoxFetcher 构造、_create_page/_create_page_async，
用 StubProvider 控制 LLM 返回的 Action，mock confidence_scorer/guard/judge
的可控行为，覆盖正常完成 / max_steps / 循环检测 / 异常处理 / 验证码 /
截图 / 断点 / Planner 重规划 / 多标签页 等全部退出与分支路径。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from web_crawler.ai.captcha import CaptchaType
from web_crawler.ai.confidence import ConfidenceResult
from web_crawler.ai.guardrails import GuardrailAction, GuardrailResult
from web_crawler.ai.judge import JudgeResult
from web_crawler.ai.llm import LLMResponse, ProviderCapabilities
from web_crawler.ai.reverse_agent import (
    Action,
    Observation,
    ReverseAgent,
    ReverseAgentConfig,
)

# ---------------------------------------------------------------------------
# 桩对象
# ---------------------------------------------------------------------------


class StubProvider:
    """返回预设回复序列的桩 provider。"""

    model = "stub-model"
    capabilities = ProviderCapabilities()

    def __init__(self, replies: list[str] | None = None) -> None:
        self._replies = list(replies or [])
        self.calls: int = 0

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        content = self._replies.pop(0) if self._replies else '{"action_type": "wait"}'
        return LLMResponse(content=content, model=self.model, usage={"tokens": 1})

    async def achat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        return self.chat(messages, **kwargs)


class _LoopPage:
    """同步 Page 桩：支持 run() 主循环所需的全部方法。"""

    def __init__(
        self,
        *,
        url: str = "https://example.com",
        title: str = "Example",
        content_html: str = "<html><body>x</body></html>",
        hook_records: list[dict] | None = None,
        scripts: list[str] | None = None,
        goto_fail: bool = False,
        evaluate_fail_on_unknown: bool = False,
    ) -> None:
        self._url = url
        self._title = title
        self._content = content_html
        self._hook_records = hook_records if hook_records is not None else []
        self._scripts = scripts if scripts is not None else []
        self._goto_fail = goto_fail
        self._evaluate_fail_on_unknown = evaluate_fail_on_unknown
        self.goto_calls: list[dict[str, Any]] = []
        self.screenshot_calls: list[dict[str, Any]] = []

    @property
    def url(self) -> str:
        return self._url

    def title(self) -> str:
        return self._title

    def content(self) -> str:
        return self._content

    def screenshot(self, *, path: str = "", **kwargs: Any) -> bytes:
        self.screenshot_calls.append({"path": path, **kwargs})
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return b"\x89PNG\r\n\x1a\n"

    def goto(self, url: str, **kwargs: Any) -> None:
        if self._goto_fail:
            raise RuntimeError("goto failed")
        self.goto_calls.append({"url": url, **kwargs})
        self._url = url

    def evaluate(self, script: str) -> Any:
        if "__hook_data__" in script:
            return list(self._hook_records)
        if "querySelectorAll('script[src]')" in script:
            return list(self._scripts)
        if self._evaluate_fail_on_unknown:
            raise RuntimeError("evaluate failed")
        return None

    def query_selector(self, selector: str) -> Any:
        return None

    def on(self, event: str, handler: Any) -> None:
        pass

    def close(self) -> None:
        pass


class _AsyncLoopPage:
    """异步 Page 桩：支持 arun() 主循环所需的全部方法。"""

    def __init__(
        self,
        *,
        url: str = "https://example.com",
        title: str = "Example",
        content_html: str = "<html><body>x</body></html>",
        hook_records: list[dict] | None = None,
        scripts: list[str] | None = None,
        goto_fail: bool = False,
        evaluate_fail_on_unknown: bool = False,
    ) -> None:
        self._url = url
        self._title = title
        self._content = content_html
        self._hook_records = hook_records if hook_records is not None else []
        self._scripts = scripts if scripts is not None else []
        self._goto_fail = goto_fail
        self._evaluate_fail_on_unknown = evaluate_fail_on_unknown
        self.goto_calls: list[dict[str, Any]] = []
        self.screenshot_calls: list[dict[str, Any]] = []

    @property
    def url(self) -> str:
        return self._url

    async def title(self) -> str:
        return self._title

    async def content(self) -> str:
        return self._content

    async def screenshot(self, *, path: str = "", **kwargs: Any) -> bytes:
        self.screenshot_calls.append({"path": path, **kwargs})
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return b"\x89PNG\r\n\x1a\n"

    async def goto(self, url: str, **kwargs: Any) -> None:
        if self._goto_fail:
            raise RuntimeError("goto failed")
        self.goto_calls.append({"url": url, **kwargs})
        self._url = url

    async def evaluate(self, script: str) -> Any:
        if "__hook_data__" in script:
            return list(self._hook_records)
        if "querySelectorAll('script[src]')" in script:
            return list(self._scripts)
        if self._evaluate_fail_on_unknown:
            raise RuntimeError("evaluate failed")
        return None

    def query_selector(self, selector: str) -> Any:
        return None

    def on(self, event: str, handler: Any) -> None:
        pass

    async def close(self) -> None:
        pass


def _make_observation(url: str = "https://x.example") -> Observation:
    """构造一个最小可用的 Observation。"""
    return Observation(
        url=url,
        hook_data={"records": [], "count": 0},
        network_requests=[],
        scripts=[],
        captcha_type=CaptchaType.NONE,
        page_title="T",
        dom_summary="dom",
    )


def _make_loop_agent(
    *,
    replies: list[str] | None = None,
    async_mode: bool = False,
    confidence: float = 1.0,
    guard_denied: bool = False,
    judge_verified: bool | None = None,
    enable_judge: bool = False,
    enable_guard: bool = False,
    enable_recorder: bool = False,
    enable_checkpoint: bool = False,
    enable_screenshot: bool = False,
    planner_interval: int | None = None,
    target_params: list[str] | None = None,
    max_steps: int = 20,
    max_history: int = 25,
    loop_threshold: int = 3,
    page: Any = None,
    hook_records: list[dict] | None = None,
    **extra_config: Any,
) -> tuple[ReverseAgent, Any, StubProvider]:
    """创建一个用于主循环测试的 agent，所有重依赖已 mock。

    返回 (agent, page, provider)。page 是桩 Page 对象，
    agent._create_page / _create_page_async 已 mock 为返回 (MagicMock ctx, page)。
    """
    cfg = ReverseAgentConfig(
        max_steps=max_steps,
        enable_screenshot=enable_screenshot,
        enable_guard=enable_guard,
        enable_judge=enable_judge,
        enable_recorder=enable_recorder,
        planner_interval=planner_interval,
        humanize_input=False,
        wait_after_navigate=0.0,
        enable_checkpoint=enable_checkpoint,
        target_params=target_params,
        max_history=max_history,
        loop_threshold=loop_threshold,
        **extra_config,
    )
    provider = StubProvider(replies)
    agent = ReverseAgent(config=cfg, provider=provider)

    # 创建桩 page（若调用方未提供，则按 hook_records 创建）
    if page is None:
        kwargs: dict[str, Any] = {}
        if hook_records is not None:
            kwargs["hook_records"] = hook_records
        page = _AsyncLoopPage(**kwargs) if async_mode else _LoopPage(**kwargs)
    ctx = MagicMock()

    # Mock _create_page / _create_page_async
    if async_mode:
        agent._create_page_async = AsyncMock(return_value=(ctx, page))  # type: ignore[assignment]
    else:
        agent._create_page = MagicMock(return_value=(ctx, page))  # type: ignore[assignment]

    # Mock confidence_scorer
    conf = ConfidenceResult(score=confidence, reasons=[], action_type="done")
    agent.confidence_scorer.score = MagicMock(return_value=conf)  # type: ignore[assignment]
    agent.confidence_scorer.score_async = AsyncMock(return_value=conf)  # type: ignore[assignment]

    # Mock guard（若启用）
    if enable_guard:
        guard_result = GuardrailResult(
            action=GuardrailAction.DENY if guard_denied else GuardrailAction.ALLOW,
            matched_rules=["test_rule"] if guard_denied else [],
            details=["denied"] if guard_denied else [],
        )
        agent.guard.check = MagicMock(return_value=guard_result)  # type: ignore[assignment,union-attr]
        agent.guard.check_async = AsyncMock(return_value=guard_result)  # type: ignore[assignment,union-attr]

    # Mock judge（若启用）
    if enable_judge and judge_verified is not None:
        judge_result = JudgeResult(verified=judge_verified, missing=[], reasoning="ok")
        agent.judge.validate = MagicMock(return_value=judge_result)  # type: ignore[assignment,union-attr]
        agent.judge.validate_async = AsyncMock(return_value=judge_result)  # type: ignore[assignment,union-attr]

    return agent, page, provider


# ---------------------------------------------------------------------------
# 同步 run() 主循环测试
# ---------------------------------------------------------------------------


class TestRunLoop:
    """覆盖 run() (418-778) 的全部分支。"""

    def test_basic_done_no_judge_no_target(self) -> None:
        """done 动作，无 judge、无 target_params → 正常退出。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(replies=['{"action_type": "done"}'])
            try:
                result = agent.run("https://x.example", "task")
                assert result["steps"] == 1
                assert result["success"] is False  # target_params_found 为空
                assert result["plan"] is None
                assert result["judge_result"] is None
                assert result["compiled_script"] is None
                assert result["last_confidence"] is not None
                assert result["last_confidence"]["score"] == 1.0
            finally:
                agent.close()

    def test_done_with_judge_verified_and_target(self) -> None:
        """done + judge verified + target_params 全找到 → success=True。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "extract", "params": {"param_name": "sign"}}',
                    '{"action_type": "done"}',
                ],
                enable_judge=True,
                judge_verified=True,
                target_params=["sign"],
                enable_recorder=True,
                hook_records=[{"headers": {"sign": "val"}, "url": "", "body": ""}],
            )
            try:
                result = agent.run("https://x.example", "task")
                assert result["success"] is True
                assert result["target_params_found"] == {"sign": "val"}
                assert result["judge_result"] is not None
                assert result["judge_result"]["verified"] is True
                assert result["compiled_script"] is not None
            finally:
                agent.close()

    def test_done_with_judge_not_verified_continues(self) -> None:
        """done + judge not verified → 覆盖为 fallback，继续循环。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "done"}',  # judge 不通过 → fallback
                    '{"action_type": "done"}',  # 再次 done
                ],
                enable_judge=True,
                judge_verified=False,
            )
            try:
                result = agent.run("https://x.example", "task")
                # judge 未通过时覆盖 done 为 fallback（extract），继续循环
                assert result["steps"] >= 1
            finally:
                agent.close()

    def test_max_steps_reached(self) -> None:
        """循环到 max_steps 后退出。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "wait", "params": {"seconds": 0.1}}'],
                max_steps=2,
            )
            try:
                result = agent.run("https://x.example", "task")
                assert result["steps"] == 2
            finally:
                agent.close()

    def test_observe_error_recover_then_done(self) -> None:
        """_observe 第一步抛异常，crash_recovery 恢复后继续，第二步 done。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(replies=['{"action_type": "done"}'])
            agent._observe = MagicMock(  # type: ignore[assignment]
                side_effect=[RuntimeError("observe boom"), _make_observation()]
            )
            # 恢复成功：返回 (True, 新 page)，循环应重新绑定到新页
            new_page = MagicMock()
            agent._try_recover_page = MagicMock(return_value=(True, new_page))  # type: ignore[assignment]
            try:
                result = agent.run("https://x.example", "task")
                # 第一步 observe_error 入 history，第二步 done 入 history → 2 条
                assert result["steps"] == 2
                assert any(e.get("event") == "observe_error" for e in result["history"])
                # 恢复后第二步 observe 应使用新 page（循环已重新绑定）
                second_call = agent._observe.call_args_list[1]
                assert second_call.args[0] is new_page
            finally:
                agent.close()

    def test_observe_error_break(self) -> None:
        """_observe 持续抛异常，crash_recovery 耗尽后 break。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                max_steps=5,
            )
            agent._observe = MagicMock(  # type: ignore[assignment]
                side_effect=RuntimeError("observe boom")
            )
            agent._try_recover_page = MagicMock(return_value=(False, None))  # type: ignore[assignment]
            try:
                result = agent.run("https://x.example", "task")
                # 恢复失败后 break，history 包含 observe_error 条目
                assert any(e.get("event") == "observe_error" for e in result["history"])
            finally:
                agent.close()

    def test_think_error_fallback(self) -> None:
        """provider.chat 抛异常 → think_error + fallback action。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, provider = _make_loop_agent(replies=None)
            provider.chat = MagicMock(side_effect=RuntimeError("think boom"))  # type: ignore[assignment]
            agent._fallback_action = MagicMock(  # type: ignore[assignment]
                return_value=Action(action_type="done")
            )
            try:
                result = agent.run("https://x.example", "task")
                assert any(e.get("event") == "think_error" for e in result["history"])
            finally:
                agent.close()

    def test_confidence_low_fallback(self) -> None:
        """confidence 低于阈值 → fallback action。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                confidence=0.1,  # 低于 min_confidence=0.4
            )
            agent._fallback_action = MagicMock(  # type: ignore[assignment]
                return_value=Action(action_type="done")
            )
            try:
                result = agent.run("https://x.example", "task")
                assert result["last_confidence"]["score"] == 0.1
            finally:
                agent.close()

    def test_guard_deny(self) -> None:
        """guard DENY → 跳过执行，直接进入下一步。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "navigate", "params": {"url": "https://evil.example"}}',
                    '{"action_type": "done"}',
                ],
                enable_guard=True,
                guard_denied=True,
                max_steps=5,
            )
            try:
                result = agent.run("https://x.example", "task")
                # 第一步被 guard 拒绝，第二步 done
                assert any(e.get("event") == "guard_denied" for e in result["history"])
            finally:
                agent.close()

    def test_act_error(self) -> None:
        """_act 抛异常 → act_error + 截图。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "wait"}',
                    '{"action_type": "click", "params": {}}',  # 无 selector → ValueError
                    '{"action_type": "done"}',
                ],
                enable_recorder=True,
                max_steps=5,
            )
            try:
                result = agent.run("https://x.example", "task")
                assert any(e.get("event") == "act_error" for e in result["history"])
            finally:
                agent.close()

    def test_inject_hook_failed(self) -> None:
        """inject_hook 返回 False → history 记录 inject_hook_failed。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "inject_hook", "params": {"hooks": ["bad"]}}',
                    '{"action_type": "done"}',
                ],
                enable_recorder=True,
                max_steps=5,
            )
            agent._inject_hooks = MagicMock(return_value=False)  # type: ignore[assignment]
            try:
                result = agent.run("https://x.example", "task")
                assert any(e.get("event") == "inject_hook_failed" for e in result["history"])
            finally:
                agent.close()

    def test_extract_updates_target_params(self) -> None:
        """extract 动作返回值 → target_params_found 更新。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "extract", "params": {"param_name": "sign"}}',
                    '{"action_type": "done"}',
                ],
                target_params=["sign"],
                max_steps=5,
                hook_records=[{"headers": {"sign": "found_val"}, "url": "", "body": ""}],
            )
            try:
                result = agent.run("https://x.example", "task")
                assert result["target_params_found"] == {"sign": "found_val"}
                assert result["success"] is True
            finally:
                agent.close()

    def test_analyze_js_updates_analysis(self) -> None:
        """analyze_js 返回 AnalysisResult → analysis 字段更新。"""
        from web_crawler.ai.analyzer import AnalysisResult

        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "analyze_js", "params": {}}',
                    '{"action_type": "done"}',
                ],
                max_steps=5,
            )
            fake_analysis = MagicMock(spec=AnalysisResult)
            agent._act = MagicMock(  # type: ignore[assignment]
                side_effect=[fake_analysis, None]
            )
            try:
                result = agent.run("https://x.example", "task")
                assert result["analysis"] is fake_analysis
            finally:
                agent.close()

    def test_loop_detected_with_planner(self) -> None:
        """3 步相同观察 → LoopDetector 触发 → Planner 重规划。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    # step 1: make_plan (周期，_current_plan is None)
                    '{"subgoals": [{"description": "p1", "success_criteria": "ok"}]}',
                    '{"action_type": "wait"}',
                    # step 2: think
                    '{"action_type": "wait"}',
                    # step 3: loop detected → make_plan
                    '{"subgoals": [{"description": "p2", "success_criteria": "ok"}]}',
                    '{"action_type": "done"}',
                ],
                planner_interval=100,  # 避免周期触发
                max_steps=5,
                loop_threshold=3,
            )
            try:
                result = agent.run("https://x.example", "task")
                # 应正常退出
                assert result["steps"] >= 1
                # loop_detector 触发后 planner 重规划
                assert result["plan"] is not None
            finally:
                agent.close()

    def test_loop_detected_without_planner(self) -> None:
        """无 planner 时 LoopDetector 触发 → 跳过重规划。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "wait"}', '{"action_type": "done"}'],
                planner_interval=None,
                max_steps=5,
                loop_threshold=3,
            )
            try:
                result = agent.run("https://x.example", "task")
                # 无 planner 时也能正常退出
                assert result["plan"] is None
            finally:
                agent.close()

    def test_planner_interval_triggers(self) -> None:
        """planner_interval 到期 → 周期重规划。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    # step 1: make_plan (None → interval)
                    '{"subgoals": [{"description": "p1", "success_criteria": "ok"}]}',
                    '{"action_type": "wait"}',
                    # step 2: interval=1 → make_plan again
                    '{"subgoals": [{"description": "p2", "success_criteria": "ok"}]}',
                    '{"action_type": "done"}',
                ],
                planner_interval=1,
                max_steps=5,
            )
            try:
                result = agent.run("https://x.example", "task")
                assert result["plan"] is not None
            finally:
                agent.close()

    def test_context_compressed(self) -> None:
        """context_compressor.maybe_compress 返回 compressed=True → 发事件。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(replies=['{"action_type": "done"}'])
            agent.context_compressor.maybe_compress = MagicMock(  # type: ignore[assignment]
                return_value=([{"step": -1, "action": "_history_compressed"}], True)
            )
            try:
                result = agent.run("https://x.example", "task")
                # 压缩后 history 仍非空
                assert len(result["history"]) >= 1
            finally:
                agent.close()

    def test_checkpoint_enabled(self) -> None:
        """enable_checkpoint=True → 每步保存 checkpoint。

        done 动作会 break 在 checkpoint save 之前，所以用 wait + done 让
        step 1 触发一次 checkpoint save。
        """
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "wait", "params": {"seconds": 0.1}}',
                    '{"action_type": "done"}',
                ],
                enable_checkpoint=True,
                max_steps=5,
            )
            agent.checkpoint_manager.task_id = "test-task"
            agent.checkpoint_manager.save = MagicMock()  # type: ignore[assignment]
            agent.checkpoint_manager.build_checkpoint = MagicMock(  # type: ignore[assignment]
                return_value=MagicMock()
            )
            try:
                result = agent.run("https://x.example", "task")
                # step 1 (wait) 触发 checkpoint save；step 2 (done) break 前不保存
                agent.checkpoint_manager.save.assert_called_once()
                assert result["checkpoints"] == []  # task_id 未持久化文件
            finally:
                agent.close()

    def test_resume_from_checkpoint(self) -> None:
        """从 checkpoint 恢复 → 导航到 resume URL + 注入 hooks + start_step 跳过。"""
        from web_crawler.ai.checkpoint import Checkpoint

        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                enable_checkpoint=True,
                max_steps=10,
            )
            cp = Checkpoint(
                task_id="resume-task",
                step=2,
                url="https://resumed.example",
                task="task",
                target_params_found={"sign": "val"},
                target_params=["sign"],
                hooks=["fetch_hook"],
                history=[{"step": 1, "action": "wait"}],
                cumulative_summary="past summary",
            )
            agent.checkpoint_manager.load_latest = MagicMock(return_value=cp)  # type: ignore[assignment]
            agent._inject_hooks = MagicMock(return_value=True)  # type: ignore[assignment]
            try:
                result = agent.run("https://init.example", "task")
                # 应导航到 resume URL
                assert page.goto_calls[0]["url"] == "https://resumed.example"
                # 应重新注入 hooks
                agent._inject_hooks.assert_called_once()
                # resume 后 history 应包含 checkpoint 中的历史
                assert result["target_params_found"] == {"sign": "val"}
            finally:
                agent.close()

    def test_resume_completed_all_steps(self) -> None:
        """resume 的 step >= max_steps → 直接退出，不进入循环。"""
        from web_crawler.ai.checkpoint import Checkpoint

        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[],
                enable_checkpoint=True,
                max_steps=3,
            )
            cp = Checkpoint(task_id="done-task", step=3, url="https://done.example")
            agent.checkpoint_manager.load_latest = MagicMock(return_value=cp)  # type: ignore[assignment]
            try:
                result = agent.run("https://init.example", "task")
                # start_step = 4 > max_steps = 3 → 不进入循环
                assert result["steps"] == 0  # 无新 history
            finally:
                agent.close()

    def test_navigate_error(self) -> None:
        """page.goto 抛异常 → history 记录 navigate_error。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                page=_LoopPage(goto_fail=True),
            )
            try:
                result = agent.run("https://x.example", "task")
                assert any(e.get("event") == "navigate_error" for e in result["history"])
            finally:
                agent.close()

    def test_recorder_compile_error(self) -> None:
        """recorder.compile_script 抛异常 → _compiled_script=""。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "extract", "params": {"param_name": "sign"}}',
                    '{"action_type": "done"}',
                ],
                enable_recorder=True,
                target_params=["sign"],
                hook_records=[{"headers": {"sign": "val"}, "url": "", "body": ""}],
            )
            agent.recorder.compile_script = MagicMock(  # type: ignore[assignment,union-attr]
                side_effect=RuntimeError("compile boom")
            )
            try:
                result = agent.run("https://x.example", "task")
                assert result["compiled_script"] is None  # _compiled_script="" → None
            finally:
                agent.close()

    def test_screenshot_enabled(self, tmp_path: Path) -> None:
        """enable_screenshot=True → _observe 中截图，result 含 screenshots。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
            patch.object(Path, "mkdir"),
            patch(
                "web_crawler.ai.reverse_agent.ReverseAgent._screenshot_dir",
                return_value=tmp_path,
            ),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                enable_screenshot=True,
            )
            try:
                result = agent.run("https://x.example", "task")
                assert len(result["screenshots"]) >= 1
                assert result["screenshots"][0]["step"] == 1
            finally:
                agent.close()

    def test_done_without_judge(self) -> None:
        """done 动作 + 无 judge → 走 else 分支直接 break。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                enable_judge=False,
            )
            try:
                result = agent.run("https://x.example", "task")
                assert result["judge_result"] is None
            finally:
                agent.close()

    def test_judge_verified_affects_success(self) -> None:
        """judge verified=True → success = success and verified。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "extract", "params": {"param_name": "sign"}}',
                    '{"action_type": "done"}',
                ],
                enable_judge=True,
                judge_verified=True,
                target_params=["sign"],
                hook_records=[{"headers": {"sign": "val"}, "url": "", "body": ""}],
            )
            try:
                result = agent.run("https://x.example", "task")
                # target_params_found 非空 + judge verified → success=True
                assert result["success"] is True
                assert result["judge_result"]["verified"] is True
            finally:
                agent.close()

    def test_new_tab_loop_observes_new_page(self) -> None:
        """new_tab 后循环后续 observe 应使用新标签页（修复循环持有旧 page）。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "new_tab", "params": {"url": "https://tab.example", "name": "t2"}}',
                    '{"action_type": "done"}',
                ],
                max_steps=5,
            )
            new_page = _LoopPage(url="https://tab.example", title="Tab")
            ctx = MagicMock()
            ctx.new_page.return_value = new_page
            # 覆盖 _create_page，让 run() 使用能创建新标签页的 ctx
            agent._create_page = MagicMock(return_value=(ctx, page))  # type: ignore[assignment]
            # spy：记录 _observe 收到的 page，验证循环页切换
            agent._observe = MagicMock(wraps=agent._observe)  # type: ignore[assignment]
            try:
                agent.run("https://x.example", "task")
                # 第 1 步 observe 在主页面，第 2 步 observe 在新标签页
                calls = agent._observe.call_args_list
                assert calls[0].args[0] is page
                assert calls[1].args[0] is new_page
            finally:
                agent.close()

    def test_switch_tab_loop_observes_switched_page(self) -> None:
        """switch_tab 后循环后续 observe 应使用切换后的标签页。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "new_tab", "params": {"url": "", "name": "t2"}}',
                    '{"action_type": "switch_tab", "params": {"name": "t2"}}',
                    '{"action_type": "done"}',
                ],
                max_steps=5,
            )
            new_page = _LoopPage(url="https://tab.example", title="Tab")
            ctx = MagicMock()
            ctx.new_page.return_value = new_page
            agent._create_page = MagicMock(return_value=(ctx, page))  # type: ignore[assignment]
            # spy：记录 _observe 收到的 page
            agent._observe = MagicMock(wraps=agent._observe)  # type: ignore[assignment]
            try:
                agent.run("https://x.example", "task")
                calls = agent._observe.call_args_list
                # 第 1 步在主页面；new_tab 后第 2 步 observe 新标签页；
                # switch_tab 后第 3 步 observe 仍是新标签页
                assert calls[0].args[0] is page
                assert calls[1].args[0] is new_page
                assert calls[2].args[0] is new_page
            finally:
                agent.close()

    def test_resume_from_real_checkpoint_file(self, tmp_path: Path) -> None:
        """真实存储层 resume：save → 新实例 load → 从正确 step 继续。"""
        from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

        store = CheckpointStore(base_dir=tmp_path)
        store.save(
            Checkpoint(
                task_id="resume-task",
                step=2,
                url="https://resumed.example",
                task="task",
                target_params_found={"sign": "val"},
                target_params=["sign"],
                hooks=["fetch_hook"],
                history=[{"step": 1, "action": "wait"}],
                cumulative_summary="past summary",
            )
        )
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                enable_checkpoint=True,
                max_steps=10,
            )
            agent.checkpoint_manager.store = store
            agent.checkpoint_manager.task_id = "resume-task"
            agent._inject_hooks = MagicMock(return_value=True)  # type: ignore[assignment]
            try:
                result = agent.run("https://init.example", "task")
                # 应导航回 resume URL 并续跑
                assert page.goto_calls[0]["url"] == "https://resumed.example"
                agent._inject_hooks.assert_called_once()
                assert result["target_params_found"] == {"sign": "val"}
                # 续跑从 step 3 开始（step 2 已完成）
                steps = [h.get("step") for h in result["history"] if h.get("step")]
                assert 3 in steps
            finally:
                agent.close()

    def test_checkpoint_task_id_auto_set_from_url(self, tmp_path: Path) -> None:
        """enable_checkpoint 时 run() 自动用 url 设置稳定 task_id。"""
        from web_crawler.ai.checkpoint import CheckpointStore

        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                enable_checkpoint=True,
            )
            agent.checkpoint_manager.store = CheckpointStore(base_dir=tmp_path)
            agent.checkpoint_manager.save = MagicMock()  # type: ignore[assignment]
            try:
                agent.run("https://init.example", "task")
                tid = agent.checkpoint_manager.task_id
                assert tid != ""
                # 同一 url+task 在"新实例"上应得到相同 id（可跨进程续跑）
                from web_crawler.ai.checkpoint import CheckpointManager

                assert CheckpointManager().ensure_task_id("https://init.example", "task") == tid
            finally:
                agent.close()

    def test_plan_advances_on_extract_success(self) -> None:
        """extract 成功 → Planner 当前子目标推进到下一个。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    (
                        '{"subgoals": [{"description": "p1", "success_criteria": "ok"}, '
                        '{"description": "p2", "success_criteria": "ok"}]}'
                    ),
                    '{"action_type": "extract", "params": {"param_name": "sign"}}',
                    '{"action_type": "done"}',
                ],
                planner_interval=100,
                target_params=["sign"],
                max_steps=10,
                hook_records=[{"headers": {"sign": "val"}, "url": "", "body": ""}],
            )
            try:
                result = agent.run("https://x.example", "task")
                plan = result["plan"]
                assert plan is not None
                assert plan["subgoals"][0]["completed"] is True
                assert plan["current_index"] == 1
            finally:
                agent.close()

    def test_plan_advances_on_judge_verified(self) -> None:
        """done 通过 judge 验证 → Planner 当前子目标推进。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"subgoals": [{"description": "p1", "success_criteria": "ok"}]}',
                    '{"action_type": "done"}',
                ],
                planner_interval=100,
                enable_judge=True,
                judge_verified=True,
                max_steps=10,
            )
            try:
                result = agent.run("https://x.example", "task")
                plan = result["plan"]
                assert plan is not None
                assert plan["subgoals"][0]["completed"] is True
            finally:
                agent.close()

    async def test_plan_advances_on_extract_success_async(self) -> None:
        """异步循环：extract 成功 → Planner 当前子目标推进到下一个。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    (
                        '{"subgoals": [{"description": "p1", "success_criteria": "ok"}, '
                        '{"description": "p2", "success_criteria": "ok"}]}'
                    ),
                    '{"action_type": "extract", "params": {"param_name": "sign"}}',
                    '{"action_type": "done"}',
                ],
                async_mode=True,
                planner_interval=100,
                target_params=["sign"],
                max_steps=10,
                hook_records=[{"headers": {"sign": "val"}, "url": "", "body": ""}],
            )
            try:
                result = await agent.arun("https://x.example", "task")
                plan = result["plan"]
                assert plan is not None
                assert plan["subgoals"][0]["completed"] is True
                assert plan["current_index"] == 1
            finally:
                agent.close()

    async def test_plan_advances_on_judge_verified_async(self) -> None:
        """异步循环：done 通过 judge 验证 → Planner 当前子目标推进。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"subgoals": [{"description": "p1", "success_criteria": "ok"}]}',
                    '{"action_type": "done"}',
                ],
                async_mode=True,
                planner_interval=100,
                enable_judge=True,
                judge_verified=True,
                max_steps=10,
            )
            try:
                result = await agent.arun("https://x.example", "task")
                plan = result["plan"]
                assert plan is not None
                assert plan["subgoals"][0]["completed"] is True
            finally:
                agent.close()

    def test_heartbeat_stall_detected_during_loop(self) -> None:
        """think 前 check_stall 检测到卡死 → 发布 stall 事件。"""
        import time as _time

        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(replies=['{"action_type": "done"}'])
            agent.heartbeat.max_interval = 1.0
            events: list[Any] = []
            agent.event_bus.subscribe(events.append)
            orig_reset = agent.heartbeat.reset

            def _reset_and_age() -> None:
                orig_reset()
                agent.heartbeat._last_tick_time = _time.time() - 100  # 制造超时

            agent.heartbeat.reset = _reset_and_age  # type: ignore[method-assign]
            try:
                agent.run("https://x.example", "task")
                assert any(getattr(e, "type", None) == "stall" for e in events)
            finally:
                agent.close()

    def test_unknown_action_recorded_in_history(self) -> None:
        """未知 action_type → act_error 写入 history（不再静默空转）。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "totally_unknown", "params": {}}',
                    '{"action_type": "done"}',
                ],
                max_steps=5,
            )
            try:
                result = agent.run("https://x.example", "task")
                acts = [h for h in result["history"] if h.get("event") == "act_error"]
                assert acts and "未知动作类型" in acts[0]["error"]
            finally:
                agent.close()

    def test_should_stop_breaks_loop_early(self) -> None:
        """should_stop 返回 True → 循环提前中断，结果状态标为 stopped。"""
        calls = {"n": 0}

        def should_stop() -> bool:
            calls["n"] += 1
            return calls["n"] >= 2  # 第 2 步循环顶部返回 True

        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "wait", "params": {"seconds": 0.1}}'],
                max_steps=10,
                should_stop=should_stop,
            )
            events: list[Any] = []
            agent.event_bus.subscribe(events.append)
            try:
                result = agent.run("https://x.example", "task")
                assert result["status"] == "stopped"
                assert result["steps"] == 1  # 只执行了第 1 步
                assert any(getattr(e, "type", None) == "agent.stopped" for e in events)
            finally:
                agent.close()

    def test_should_stop_false_runs_to_completion(self) -> None:
        """should_stop 恒返回 False → 正常完成，状态为 completed。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                max_steps=10,
                should_stop=lambda: False,
            )
            try:
                result = agent.run("https://x.example", "task")
                assert result["status"] == "completed"
            finally:
                agent.close()

    def test_act_error_marks_current_record_only(self) -> None:
        """act 异常只把当前步记录标失败，不误标上一步（修复 recorder 记录错位）。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "wait", "params": {"seconds": 0.1}}',
                    '{"action_type": "click", "params": {}}',  # 无 selector → ValueError
                    '{"action_type": "done"}',
                ],
                enable_recorder=True,
                max_steps=5,
            )
            try:
                result = agent.run("https://x.example", "task")
                assert any(h.get("event") == "act_error" for h in result["history"])
                assert agent.recorder is not None
                recs = agent.recorder.records
                assert recs[0].action_type == "wait" and recs[0].success is True
                assert recs[1].action_type == "click" and recs[1].success is False
            finally:
                agent.close()

    def test_result_hook_data_merged_with_cache(self) -> None:
        """结果 dict 的 hook_data 应合并最后一次观察缓存，而非近空。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.time.sleep"),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                hook_records=[{"headers": {"sign": "val"}, "url": "", "body": ""}],
            )
            try:
                result = agent.run("https://x.example", "task")
                assert result["hook_data"]["count"] >= 1
                assert any("sign" in str(r) for r in result["hook_data"]["records"])
            finally:
                agent.close()


# ---------------------------------------------------------------------------
# 异步 arun() 主循环测试
# ---------------------------------------------------------------------------


class TestArunLoop:
    """覆盖 arun() (786-1147) 的全部分支。"""

    async def test_basic_done_no_judge(self) -> None:
        """done 动作，无 judge、无 target_params → 正常退出。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                async_mode=True,
            )
            try:
                result = await agent.arun("https://x.example", "task")
                assert result["steps"] == 1
                assert result["success"] is False
                assert result["last_confidence"] is not None
            finally:
                agent.close()

    async def test_done_with_judge_verified(self) -> None:
        """done + judge verified + target_params 全找到 → success=True。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "extract", "params": {"param_name": "sign"}}',
                    '{"action_type": "done"}',
                ],
                async_mode=True,
                enable_judge=True,
                judge_verified=True,
                target_params=["sign"],
                enable_recorder=True,
                hook_records=[{"headers": {"sign": "val"}, "url": "", "body": ""}],
            )
            try:
                result = await agent.arun("https://x.example", "task")
                assert result["success"] is True
                assert result["compiled_script"] is not None
            finally:
                agent.close()

    async def test_done_with_judge_not_verified(self) -> None:
        """done + judge not verified → fallback，继续循环。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}', '{"action_type": "done"}'],
                async_mode=True,
                enable_judge=True,
                judge_verified=False,
                max_steps=5,
            )
            try:
                result = await agent.arun("https://x.example", "task")
                assert result["steps"] >= 1
            finally:
                agent.close()

    async def test_max_steps_reached(self) -> None:
        """循环到 max_steps 后退出。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "wait", "params": {"seconds": 0.1}}'],
                async_mode=True,
                max_steps=2,
            )
            try:
                result = await agent.arun("https://x.example", "task")
                assert result["steps"] == 2
            finally:
                agent.close()

    async def test_observe_error_recover_then_done(self) -> None:
        """_observe_async 第一步抛异常，恢复后继续，第二步 done。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                async_mode=True,
            )
            agent._observe_async = AsyncMock(  # type: ignore[assignment]
                side_effect=[RuntimeError("observe boom"), _make_observation()]
            )
            # 恢复成功：返回 (True, 新 page)，循环应重新绑定到新页
            new_page = MagicMock()
            agent._try_recover_page_async = AsyncMock(  # type: ignore[assignment]
                return_value=(True, new_page)
            )
            try:
                result = await agent.arun("https://x.example", "task")
                # 第一步 observe_error 入 history，第二步 done 入 history → 2 条
                assert result["steps"] == 2
                assert any(e.get("event") == "observe_error" for e in result["history"])
                # 恢复后第二步 observe 应使用新 page（循环已重新绑定）
                second_call = agent._observe_async.call_args_list[1]
                assert second_call.args[0] is new_page
            finally:
                agent.close()

    async def test_observe_error_break(self) -> None:
        """_observe_async 持续抛异常，crash_recovery 耗尽后 break。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                async_mode=True,
                max_steps=5,
            )
            agent._observe_async = AsyncMock(  # type: ignore[assignment]
                side_effect=RuntimeError("observe boom")
            )
            agent._try_recover_page_async = AsyncMock(  # type: ignore[assignment]
                return_value=(False, None)
            )
            try:
                result = await agent.arun("https://x.example", "task")
                assert any(e.get("event") == "observe_error" for e in result["history"])
            finally:
                agent.close()

    async def test_think_error_fallback(self) -> None:
        """provider.achat 抛异常 → think_error + fallback action。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, provider = _make_loop_agent(replies=None, async_mode=True)
            provider.achat = AsyncMock(side_effect=RuntimeError("think boom"))  # type: ignore[assignment]
            agent._fallback_action = MagicMock(  # type: ignore[assignment]
                return_value=Action(action_type="done")
            )
            try:
                result = await agent.arun("https://x.example", "task")
                assert any(e.get("event") == "think_error" for e in result["history"])
            finally:
                agent.close()

    async def test_confidence_low_fallback(self) -> None:
        """confidence 低于阈值 → fallback action。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                async_mode=True,
                confidence=0.1,
            )
            agent._fallback_action = MagicMock(  # type: ignore[assignment]
                return_value=Action(action_type="done")
            )
            try:
                result = await agent.arun("https://x.example", "task")
                assert result["last_confidence"]["score"] == 0.1
            finally:
                agent.close()

    async def test_guard_deny(self) -> None:
        """guard DENY → 跳过执行，直接进入下一步。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "navigate", "params": {"url": "https://evil.example"}}',
                    '{"action_type": "done"}',
                ],
                async_mode=True,
                enable_guard=True,
                guard_denied=True,
                max_steps=5,
            )
            try:
                result = await agent.arun("https://x.example", "task")
                assert any(e.get("event") == "guard_denied" for e in result["history"])
            finally:
                agent.close()

    async def test_act_error(self) -> None:
        """_act_async 抛异常 → act_error。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "wait"}',
                    '{"action_type": "click", "params": {}}',
                    '{"action_type": "done"}',
                ],
                async_mode=True,
                enable_recorder=True,
                max_steps=5,
            )
            try:
                result = await agent.arun("https://x.example", "task")
                assert any(e.get("event") == "act_error" for e in result["history"])
            finally:
                agent.close()

    async def test_inject_hook_extract_and_analyze(self) -> None:
        """一步测试 inject_hook 失败 + extract 成功 + analyze_js 返回。"""
        from web_crawler.ai.analyzer import AnalysisResult

        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "inject_hook", "params": {"hooks": ["bad"]}}',
                    '{"action_type": "extract", "params": {"param_name": "sign"}}',
                    '{"action_type": "analyze_js", "params": {}}',
                    '{"action_type": "done"}',
                ],
                async_mode=True,
                enable_recorder=True,
                target_params=["sign"],
                max_steps=5,
                hook_records=[{"headers": {"sign": "v"}, "url": "", "body": ""}],
            )
            agent._inject_hooks_async = AsyncMock(return_value=False)  # type: ignore[assignment]
            fake_analysis = MagicMock(spec=AnalysisResult)
            # 让 _act_async 对 analyze_js 返回 fake_analysis
            original_act_async = agent._act_async

            call_count = {"n": 0}

            async def _mock_act_async(page: Any, action: Action, *, step: int = 0) -> Any:
                call_count["n"] += 1
                if action.action_type == "inject_hook":
                    return False
                if action.action_type == "analyze_js":
                    return fake_analysis
                return await original_act_async(page, action, step=step)

            agent._act_async = _mock_act_async  # type: ignore[assignment]
            try:
                result = await agent.arun("https://x.example", "task")
                assert any(e.get("event") == "inject_hook_failed" for e in result["history"])
                assert result["target_params_found"] == {"sign": "v"}
                assert result["analysis"] is fake_analysis
            finally:
                agent.close()

    async def test_loop_detected_with_planner(self) -> None:
        """3 步相同观察 → LoopDetector 触发 → Planner 重规划。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"subgoals": [{"description": "p1", "success_criteria": "ok"}]}',
                    '{"action_type": "wait"}',
                    '{"action_type": "wait"}',
                    '{"subgoals": [{"description": "p2", "success_criteria": "ok"}]}',
                    '{"action_type": "done"}',
                ],
                async_mode=True,
                planner_interval=100,
                max_steps=5,
                loop_threshold=3,
            )
            try:
                result = await agent.arun("https://x.example", "task")
                assert result["plan"] is not None
            finally:
                agent.close()

    async def test_checkpoint_enabled(self) -> None:
        """enable_checkpoint=True → 每步保存 checkpoint。

        done 动作会 break 在 checkpoint save 之前，所以用 wait + done 让
        step 1 触发一次 checkpoint save。
        """
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "wait", "params": {"seconds": 0.1}}',
                    '{"action_type": "done"}',
                ],
                async_mode=True,
                enable_checkpoint=True,
                max_steps=5,
            )
            agent.checkpoint_manager.task_id = "async-task"
            agent.checkpoint_manager.save = MagicMock()  # type: ignore[assignment]
            agent.checkpoint_manager.build_checkpoint = MagicMock(  # type: ignore[assignment]
                return_value=MagicMock()
            )
            try:
                await agent.arun("https://x.example", "task")
                agent.checkpoint_manager.save.assert_called_once()
            finally:
                agent.close()

    async def test_resume_from_checkpoint(self) -> None:
        """从 checkpoint 恢复 → 导航到 resume URL + 注入 hooks。"""
        from web_crawler.ai.checkpoint import Checkpoint

        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                async_mode=True,
                enable_checkpoint=True,
                max_steps=10,
            )
            cp = Checkpoint(
                task_id="resume-task",
                step=2,
                url="https://resumed.example",
                hooks=["fetch_hook"],
                target_params_found={"sign": "val"},
                cumulative_summary="summary",
            )
            agent.checkpoint_manager.load_latest = MagicMock(return_value=cp)  # type: ignore[assignment]
            agent._inject_hooks_async = AsyncMock(return_value=True)  # type: ignore[assignment]
            try:
                result = await agent.arun("https://init.example", "task")
                assert page.goto_calls[0]["url"] == "https://resumed.example"
                agent._inject_hooks_async.assert_called_once()
                assert result["target_params_found"] == {"sign": "val"}
            finally:
                agent.close()

    async def test_resume_completed_all_steps(self) -> None:
        """resume 的 step >= max_steps → 直接退出，不进入循环（覆盖 arun 行 856）。"""
        from web_crawler.ai.checkpoint import Checkpoint

        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[],
                async_mode=True,
                enable_checkpoint=True,
                max_steps=3,
            )
            cp = Checkpoint(task_id="done-task", step=3, url="https://done.example")
            agent.checkpoint_manager.load_latest = MagicMock(return_value=cp)  # type: ignore[assignment]
            try:
                result = await agent.arun("https://init.example", "task")
                # start_step = 4 > max_steps = 3 → 不进入循环
                assert result["steps"] == 0  # 无新 history
            finally:
                agent.close()

    async def test_navigate_error(self) -> None:
        """page.goto 抛异常 → history 记录 navigate_error。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                async_mode=True,
                page=_AsyncLoopPage(goto_fail=True),
            )
            try:
                result = await agent.arun("https://x.example", "task")
                assert any(e.get("event") == "navigate_error" for e in result["history"])
            finally:
                agent.close()

    async def test_recorder_compile_error(self) -> None:
        """recorder.compile_script 抛异常 → _compiled_script=""。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "extract", "params": {"param_name": "sign"}}',
                    '{"action_type": "done"}',
                ],
                async_mode=True,
                enable_recorder=True,
                target_params=["sign"],
                hook_records=[{"headers": {"sign": "val"}, "url": "", "body": ""}],
            )
            agent.recorder.compile_script = MagicMock(  # type: ignore[assignment,union-attr]
                side_effect=RuntimeError("compile boom")
            )
            try:
                result = await agent.arun("https://x.example", "task")
                assert result["compiled_script"] is None
            finally:
                agent.close()

    async def test_context_compressed(self) -> None:
        """context_compressor.maybe_compress_async 返回 compressed=True。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "done"}'],
                async_mode=True,
            )
            agent.context_compressor.maybe_compress_async = AsyncMock(  # type: ignore[assignment]
                return_value=([{"step": -1, "action": "_history_compressed"}], True)
            )
            try:
                result = await agent.arun("https://x.example", "task")
                assert len(result["history"]) >= 1
            finally:
                agent.close()

    async def test_new_tab_async_loop_observes_new_page(self) -> None:
        """异步：new_tab 后循环后续 observe 应使用新标签页。"""
        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, page, _ = _make_loop_agent(
                replies=[
                    '{"action_type": "new_tab", "params": {"url": "https://tab.example", "name": "t2"}}',
                    '{"action_type": "done"}',
                ],
                async_mode=True,
                max_steps=5,
            )
            new_page = _AsyncLoopPage(url="https://tab.example", title="Tab")
            ctx = MagicMock()
            ctx.new_page = AsyncMock(return_value=new_page)
            # 覆盖 _create_page_async，让 arun() 使用能创建新标签页的 ctx
            agent._create_page_async = AsyncMock(return_value=(ctx, page))  # type: ignore[assignment]
            # spy：记录 _observe_async 收到的 page
            agent._observe_async = AsyncMock(wraps=agent._observe_async)  # type: ignore[assignment]
            try:
                await agent.arun("https://x.example", "task")
                calls = agent._observe_async.call_args_list
                assert calls[0].args[0] is page
                assert calls[1].args[0] is new_page
            finally:
                agent.close()

    async def test_should_stop_breaks_loop_early_async(self) -> None:
        """异步：should_stop 返回 True → arun 提前中断，状态标为 stopped。"""
        calls = {"n": 0}

        def should_stop() -> bool:
            calls["n"] += 1
            return calls["n"] >= 2

        with (
            patch("web_crawler.ai.reverse_agent.CamoufoxFetcher"),
            patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()),
        ):
            agent, _page, _ = _make_loop_agent(
                replies=['{"action_type": "wait", "params": {"seconds": 0.1}}'],
                async_mode=True,
                max_steps=10,
                should_stop=should_stop,
            )
            try:
                result = await agent.arun("https://x.example", "task")
                assert result["status"] == "stopped"
                assert result["steps"] == 1
            finally:
                agent.close()


# ---------------------------------------------------------------------------
# 小段未覆盖分支（1554/1725/1768/1795/2023/2264/2312/2314）
# ---------------------------------------------------------------------------


class TestSmallUncoveredBranches:
    """覆盖分散在 reverse_agent.py 中的小段未覆盖代码。"""

    @pytest.mark.asyncio
    async def test_humanize_type_async_focus_failure(self) -> None:
        """行 1554-1555：_humanize_type_async 中 page.focus 抛异常时静默。"""
        from web_crawler.ai.reverse_agent import ReverseAgentConfig

        cfg = ReverseAgentConfig(humanize_input=True)
        agent = ReverseAgent(config=cfg, provider=StubProvider())
        try:
            page = MagicMock()
            page.focus = AsyncMock(side_effect=RuntimeError("focus fail"))
            page.type = AsyncMock()
            with patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()):
                await agent._humanize_type_async(page, "#sel", "txt")
            page.type.assert_awaited_once()
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_new_tab_async_setup_page_failure(self) -> None:
        """行 1725-1726：_do_new_tab_async 中 _setup_page_async 抛异常时静默。"""
        agent = ReverseAgent(config=ReverseAgentConfig(humanize_input=False))
        try:
            page = MagicMock()
            new_page = MagicMock()
            new_page.goto = AsyncMock()
            ctx = MagicMock()
            ctx.new_page = AsyncMock(return_value=new_page)
            agent._context = ctx
            agent.fetcher = MagicMock()
            agent.fetcher._setup_page_async = AsyncMock(side_effect=RuntimeError("setup fail"))
            action = Action(action_type="new_tab", params={"url": "https://t.example"})
            with patch("web_crawler.ai.reverse_agent.asyncio.sleep", new=AsyncMock()):
                await agent._do_new_tab_async(page, action, step=1)
            # 即使 setup 失败仍应完成标签创建
            assert agent._page is new_page
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_switch_tab_async_bring_to_front_failure(self) -> None:
        """行 1768-1769：_do_switch_tab_async 中 bring_to_front 抛异常时静默。"""
        agent = ReverseAgent(config=ReverseAgentConfig(humanize_input=False))
        try:
            page = MagicMock()
            target = MagicMock()
            target.bring_to_front = AsyncMock(side_effect=RuntimeError("front fail"))
            agent._tabs["t"] = target
            action = Action(action_type="switch_tab", params={"name": "t"})
            await agent._do_switch_tab_async(page, action, step=1)
            # 即使 bring_to_front 失败仍应切换
            assert agent._page is target
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_close_tab_async_close_failure(self) -> None:
        """行 1795-1796：_do_close_tab_async 中 target.close 抛异常时静默。"""
        agent = ReverseAgent(config=ReverseAgentConfig(humanize_input=False))
        try:
            page = MagicMock()
            target = MagicMock()
            target.close = AsyncMock(side_effect=RuntimeError("close fail"))
            agent._tabs["t"] = target
            action = Action(action_type="close_tab", params={"name": "t"})
            await agent._do_close_tab_async(page, action, step=1)
            # 即使 close 抛异常也不传播，且标签已从 _tabs 移除
            assert "t" not in agent._tabs
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_create_page_async_setup_page_failure(self) -> None:
        """行 2023-2024：_create_page_async 中 _setup_page_async 抛异常时静默。"""
        agent = ReverseAgent(config=ReverseAgentConfig(humanize_input=False))
        try:
            fetcher = MagicMock()
            browser = MagicMock()
            context = MagicMock()
            page = MagicMock()
            browser.new_context = AsyncMock(return_value=context)
            context.new_page = AsyncMock(return_value=page)
            context.add_init_script = AsyncMock()
            fetcher._ensure_async_browser = AsyncMock(return_value=browser)
            fetcher._setup_page_async = AsyncMock(side_effect=RuntimeError("setup fail"))
            fetcher.extra_headers = None
            fetcher.verify = True
            agent.fetcher = fetcher

            _ctx, p = await agent._create_page_async(None)
            assert p is page
        finally:
            agent.close()

    def test_checkpoints_snapshot_invalid_step_name(self) -> None:
        """行 2264-2265：checkpoints_snapshot 中 step-XXX 非数字时 ValueError 被捕获。"""
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
            agent.checkpoint_manager.task_id = "task-bad"
            # name = "step-abc" → int("abc") raises ValueError
            paths = [Path("/tmp/task-bad/step-abc")]
            agent.checkpoint_manager.store.list_checkpoints = MagicMock(return_value=paths)  # type: ignore[assignment]
            result = agent.checkpoints_snapshot()
            # ValueError 被捕获，step 保持默认 0
            assert result == [{"step": 0, "path": str(paths[0])}]
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_screenshot_async_error_path(self) -> None:
        """行 2312：_take_screenshot_async error=True 时设置 _last_error_screenshot。"""
        from web_crawler.ai.reverse_agent import ReverseAgentConfig

        agent = ReverseAgent(config=ReverseAgentConfig(enable_screenshot=True))
        try:
            page = MagicMock()
            page.screenshot = AsyncMock()
            with tempfile.TemporaryDirectory() as tmpdir:
                old_cwd = os.getcwd()
                os.chdir(tmpdir)
                try:
                    result = await agent._take_screenshot_async(page, step=2, error=True)
                    assert result != ""
                    assert "_error" in result
                    assert agent._last_error_screenshot == result
                finally:
                    os.chdir(old_cwd)
        finally:
            agent.close()

    @pytest.mark.asyncio
    async def test_screenshot_async_exception_returns_empty(self) -> None:
        """行 2314-2315：_take_screenshot_async page.screenshot 抛异常 → 返回 ""。"""
        from web_crawler.ai.reverse_agent import ReverseAgentConfig

        agent = ReverseAgent(config=ReverseAgentConfig(enable_screenshot=True))
        try:
            page = MagicMock()
            page.screenshot = AsyncMock(side_effect=RuntimeError("screenshot fail"))
            result = await agent._take_screenshot_async(page, step=1)
            assert result == ""
            assert agent._screenshots == []
        finally:
            agent.close()

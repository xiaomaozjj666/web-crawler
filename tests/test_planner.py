"""Tests for Planner / Plan / SubGoal: 双脑分离的高层规划器。

覆盖目标：
- :class:`SubGoal` / :class:`Plan` — dataclass 行为、advance、is_complete、to_dict；
- :class:`Planner.make_plan` / :meth:`make_plan_async` — LLM 返回解析、
  provider 异常降级、JSON 容错（代码块/嵌套）、非 dict / 非 list 子目标过滤；
- :meth:`Planner._build_prompt` — 各种字段写入 prompt；
- :class:`Planner.__init__` — interval 最小值校验。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from web_crawler.ai.llm import LLMResponse
from web_crawler.ai.planner import Plan, Planner, SubGoal

# ---------------------------------------------------------------------------
# 辅助桩对象
# ---------------------------------------------------------------------------


class _FakeProvider:
    """记录调用并按预设回复返回的桩 provider。"""

    model = "fake-model"

    def __init__(self, replies: list[str] | None = None) -> None:
        self._replies = list(replies or [])
        self.calls: list[Any] = []

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append(messages)
        content = self._replies.pop(0) if self._replies else "{}"
        return LLMResponse(content=content, model=self.model)

    async def achat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        return self.chat(messages, **kwargs)


class _Observation:
    """最小化 Observation 桩对象，满足 Planner._build_prompt 取值。"""

    def __init__(
        self,
        *,
        url: str = "https://example.com",
        page_title: str = "Page",
        captcha_type: Any = None,
        hook_data: dict[str, Any] | None = None,
        network_requests: list[dict] | None = None,
        scripts: list[str] | None = None,
    ) -> None:
        self.url = url
        self.page_title = page_title
        # captcha_type 可能是 enum（带 .value）或字符串
        self.captcha_type = captcha_type if captcha_type is not None else _CaptchaStub("none")
        self.hook_data = hook_data if hook_data is not None else {"count": 0}
        self.network_requests = network_requests or []
        self.scripts = scripts or []


class _CaptchaStub:
    """模拟 CaptchaType enum 的最小桩。"""

    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


# ---------------------------------------------------------------------------
# SubGoal / Plan dataclass
# ---------------------------------------------------------------------------


class TestSubGoal:
    def test_default_fields(self) -> None:
        sg = SubGoal(description="d")
        assert sg.description == "d"
        assert sg.success_criteria == ""
        assert sg.completed is False

    def test_to_dict(self) -> None:
        sg = SubGoal(description="d", success_criteria="c", completed=True)
        d = sg.to_dict()
        assert d == {
            "description": "d",
            "success_criteria": "c",
            "completed": True,
        }


class TestPlan:
    def test_empty_plan_current_subgoal_is_none(self) -> None:
        p = Plan()
        assert p.current_subgoal is None
        assert p.is_complete is True  # all([]) == True

    def test_current_subgoal_returns_first_when_index_zero(self) -> None:
        sg1 = SubGoal(description="first")
        sg2 = SubGoal(description="second")
        p = Plan(subgoals=[sg1, sg2])
        assert p.current_subgoal is sg1
        assert p.is_complete is False

    def test_advance_marks_current_complete_and_moves_to_next(self) -> None:
        sg1 = SubGoal(description="first")
        sg2 = SubGoal(description="second")
        p = Plan(subgoals=[sg1, sg2])
        next_sg = p.advance()
        assert sg1.completed is True
        assert next_sg is sg2
        assert p.current_index == 1

    def test_advance_past_end_returns_none(self) -> None:
        sg = SubGoal(description="only")
        p = Plan(subgoals=[sg])
        p.advance()
        # 再 advance 应返回 None，且不越界
        assert p.advance() is None
        assert p.current_index == 1
        assert p.is_complete is True

    def test_is_complete_when_all_subgoals_completed(self) -> None:
        sg1 = SubGoal(description="a", completed=True)
        sg2 = SubGoal(description="b", completed=True)
        p = Plan(subgoals=[sg1, sg2])
        assert p.is_complete is True

    def test_is_complete_when_current_index_exceeds_length(self) -> None:
        p = Plan(subgoals=[SubGoal(description="a")], current_index=5)
        assert p.is_complete is True

    def test_to_dict_includes_subgoals_index_and_step(self) -> None:
        sg = SubGoal(description="d", success_criteria="c")
        p = Plan(subgoals=[sg], current_index=0, created_at_step=3)
        d = p.to_dict()
        assert d["current_index"] == 0
        assert d["created_at_step"] == 3
        assert len(d["subgoals"]) == 1
        assert d["subgoals"][0]["description"] == "d"


# ---------------------------------------------------------------------------
# Planner 构造与 prompt
# ---------------------------------------------------------------------------


class TestPlannerInit:
    def test_default_interval_is_five(self) -> None:
        p = Planner(_FakeProvider())
        assert p.planner_interval == 5

    def test_interval_below_one_clamped_to_one(self) -> None:
        p = Planner(_FakeProvider(), planner_interval=0)
        assert p.planner_interval == 1

    def test_negative_interval_clamped_to_one(self) -> None:
        p = Planner(_FakeProvider(), planner_interval=-10)
        assert p.planner_interval == 1

    def test_custom_interval_preserved(self) -> None:
        p = Planner(_FakeProvider(), planner_interval=10)
        assert p.planner_interval == 10


class TestPlannerBuildPrompt:
    def test_prompt_includes_task_target_observation_fields(self) -> None:
        prompt = Planner._build_prompt(
            "locate sign param",
            _Observation(
                url="https://x.example/p",
                page_title="Demo",
                hook_data={"count": 5},
                network_requests=[{"url": "a"}],
                scripts=["s.js"],
            ),
            "previous summary",
            ["sign"],
        )
        assert "locate sign param" in prompt
        assert "sign" in prompt
        assert "https://x.example/p" in prompt
        assert "Demo" in prompt
        assert "5" in prompt  # hook_count
        assert "1" in prompt  # network/script count
        assert "previous summary" in prompt

    def test_prompt_uses_unspecified_when_fields_missing(self) -> None:
        prompt = Planner._build_prompt("", _Observation(), "", None)
        assert "(未指定)" in prompt  # task 与 target_params 都未指定
        assert "(无)" in prompt  # history_summary 为空

    def test_prompt_handles_captcha_type_without_value_attr(self) -> None:
        """captcha_type 无 .value 属性时应使用 str() 回退。"""
        prompt = Planner._build_prompt(
            "task",
            _Observation(captcha_type="plain-string-captcha"),
            "",
            None,
        )
        assert "plain-string-captcha" in prompt


# ---------------------------------------------------------------------------
# Planner.make_plan
# ---------------------------------------------------------------------------


class TestPlannerMakePlan:
    def test_make_plan_parses_valid_subgoals(self) -> None:
        """LLM 返回合法 JSON 时应解析为 Plan。"""
        reply = json.dumps(
            {
                "subgoals": [
                    {"description": "navigate to login", "success_criteria": "page loaded"},
                    {"description": "extract sign", "success_criteria": "sign found"},
                ]
            }
        )
        provider = _FakeProvider([reply])
        planner = Planner(provider, planner_interval=3)
        plan = planner.make_plan("task", _Observation(), step=2)
        assert len(plan.subgoals) == 2
        assert plan.subgoals[0].description == "navigate to login"
        assert plan.subgoals[0].success_criteria == "page loaded"
        assert plan.subgoals[0].completed is False
        assert plan.created_at_step == 2

    def test_make_plan_handles_code_fence_wrapped_json(self) -> None:
        """LLM 返回带 ```json ... ``` 代码块时应能解析。"""
        reply = "```json\n" + json.dumps(
            {"subgoals": [{"description": "step1", "success_criteria": "ok"}]}
        ) + "\n```"
        provider = _FakeProvider([reply])
        planner = Planner(provider)
        plan = planner.make_plan("task", _Observation())
        assert len(plan.subgoals) == 1
        assert plan.subgoals[0].description == "step1"

    def test_make_plan_handles_embedded_json(self) -> None:
        """LLM 在 JSON 前后有多余文字时应能提取内嵌 JSON。"""
        reply = (
            "好的，下面是规划：\n"
            + json.dumps({"subgoals": [{"description": "s1"}]})
            + "\n请按此执行。"
        )
        provider = _FakeProvider([reply])
        planner = Planner(provider)
        plan = planner.make_plan("task", _Observation())
        assert len(plan.subgoals) == 1

    def test_make_plan_returns_empty_when_subgoals_not_list(self) -> None:
        """subgoals 字段不是 list 时返回空 Plan。"""
        provider = _FakeProvider(['{"subgoals": "not-a-list"}'])
        planner = Planner(provider)
        plan = planner.make_plan("task", _Observation())
        assert plan.subgoals == []

    def test_make_plan_returns_empty_when_invalid_json(self) -> None:
        """LLM 返回非 JSON 文本时返回空 Plan。"""
        provider = _FakeProvider(["not a json at all"])
        planner = Planner(provider)
        plan = planner.make_plan("task", _Observation())
        assert plan.subgoals == []

    def test_make_plan_returns_empty_when_json_block_invalid(self) -> None:
        """LLM 返回含 {...} 但 JSON 非法时返回空 Plan。"""
        provider = _FakeProvider(["Result: { not valid json }"])
        planner = Planner(provider)
        plan = planner.make_plan("task", _Observation())
        assert plan.subgoals == []

    def test_make_plan_skips_non_dict_subgoal_entries(self) -> None:
        """subgoals 列表中非 dict 项应被跳过。"""
        reply = json.dumps(
            {
                "subgoals": [
                    {"description": "valid"},
                    "invalid-string",
                    None,
                    42,
                    {"description": "also-valid"},
                ]
            }
        )
        provider = _FakeProvider([reply])
        planner = Planner(provider)
        plan = planner.make_plan("task", _Observation())
        assert len(plan.subgoals) == 2
        assert plan.subgoals[0].description == "valid"
        assert plan.subgoals[1].description == "also-valid"

    def test_make_plan_handles_missing_description_field(self) -> None:
        """子目标 dict 缺 description 时使用空字符串。"""
        reply = json.dumps(
            {"subgoals": [{"success_criteria": "ok"}]}  # 缺 description
        )
        provider = _FakeProvider([reply])
        planner = Planner(provider)
        plan = planner.make_plan("task", _Observation())
        assert len(plan.subgoals) == 1
        assert plan.subgoals[0].description == ""
        assert plan.subgoals[0].success_criteria == "ok"

    def test_make_plan_returns_empty_plan_on_provider_exception(self) -> None:
        """provider.chat 抛异常时应返回空 Plan 而不抛出。"""

        class _FailProvider:
            model = "fail"

            def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
                raise RuntimeError("llm down")

        planner = Planner(_FailProvider(), planner_interval=2)  # type: ignore[arg-type]
        plan = planner.make_plan("task", _Observation(), step=4)
        assert plan.subgoals == []
        assert plan.created_at_step == 4

    def test_make_plan_passes_target_params_and_history_to_prompt(self) -> None:
        """prompt 应包含 target_params 与 history_summary。"""
        provider = _FakeProvider(['{"subgoals": []}'])
        planner = Planner(provider)
        planner.make_plan(
            "task",
            _Observation(),
            history_summary="done-A",
            target_params=["Anti-Content"],
        )
        assert provider.calls  # 调用过 chat
        sent_messages = provider.calls[0]
        # 第二条消息（user）应包含 history 与 target
        user_msg = sent_messages[1]
        assert "Anti-Content" in user_msg.content
        assert "done-A" in user_msg.content


# ---------------------------------------------------------------------------
# Planner.make_plan_async
# ---------------------------------------------------------------------------


class TestPlannerMakePlanAsync:
    @pytest.mark.asyncio
    async def test_make_plan_async_uses_achat_when_available(self) -> None:
        """provider 有 achat 方法时应走异步路径。"""
        reply = json.dumps({"subgoals": [{"description": "async-sg"}]})
        provider = MagicMock()
        provider.achat = AsyncMock(return_value=LLMResponse(content=reply, model="fake"))
        planner = Planner(provider)
        plan = await planner.make_plan_async("task", _Observation(), step=1)
        provider.achat.assert_awaited_once()
        assert len(plan.subgoals) == 1
        assert plan.subgoals[0].description == "async-sg"

    @pytest.mark.asyncio
    async def test_make_plan_async_falls_back_to_sync_chat(self) -> None:
        """provider 无 achat 时应回退到同步 chat。"""
        reply = json.dumps({"subgoals": [{"description": "sync-fallback"}]})
        provider = MagicMock(spec=["chat"])  # 只暴露 chat
        provider.chat.return_value = LLMResponse(content=reply, model="fake")
        planner = Planner(provider)
        plan = await planner.make_plan_async("task", _Observation())
        provider.chat.assert_called_once()
        assert plan.subgoals[0].description == "sync-fallback"

    @pytest.mark.asyncio
    async def test_make_plan_async_handles_provider_exception(self) -> None:
        """异步路径 provider 抛异常时应返回空 Plan。"""
        provider = MagicMock()
        provider.achat = AsyncMock(side_effect=RuntimeError("async llm down"))
        planner = Planner(provider)
        plan = await planner.make_plan_async("task", _Observation(), step=3)
        assert plan.subgoals == []
        assert plan.created_at_step == 3

    @pytest.mark.asyncio
    async def test_make_plan_async_handles_invalid_json(self) -> None:
        """异步路径 LLM 返回非 JSON 时返回空 Plan。"""
        provider = MagicMock()
        provider.achat = AsyncMock(return_value=LLMResponse(content="garbage", model="fake"))
        planner = Planner(provider)
        plan = await planner.make_plan_async("task", _Observation())
        assert plan.subgoals == []

    @pytest.mark.asyncio
    async def test_make_plan_async_skips_non_dict_subgoals(self) -> None:
        """异步路径同样跳过非 dict 子目标项。"""
        reply = json.dumps(
            {"subgoals": [{"description": "ok"}, "bad", 1, None, {"description": "ok2"}]}
        )
        provider = MagicMock()
        provider.achat = AsyncMock(return_value=LLMResponse(content=reply, model="fake"))
        planner = Planner(provider)
        plan = await planner.make_plan_async("task", _Observation())
        assert len(plan.subgoals) == 2

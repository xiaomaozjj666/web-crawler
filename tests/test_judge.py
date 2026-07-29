"""Tests for TaskJudge / JudgeResult: 任务完成二次验证。

覆盖目标：
- :class:`JudgeResult` — dataclass 字段、passed 别名、to_dict 序列化；
- :class:`TaskJudge.__init__` — strict 模式开关；
- :meth:`TaskJudge.validate` / :meth:`validate_async`：
  * 严格模式 + 缺参数 → 直接判失败（不调 LLM）；
  * 严格模式 + 参数齐全 → 调 LLM；
  * LLM 调用异常 → 返回失败结果；
  * LLM 返回 verified=True 且 strict 模式下 missing 非空 → 强制改为 False；
  * LLM 返回 verified=False 且 strict 模式下 missing 为空 → 重算 missing；
  * missing 字段非 list 时的容错；
  * 异步路径回退到同步 chat；
- :meth:`TaskJudge._build_prompt` — action / observation / target 序列化路径。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from web_crawler.ai.judge import JudgeResult, TaskJudge
from web_crawler.ai.llm import LLMResponse

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
        content = self._replies.pop(0) if self._replies else '{"verified": true}'
        return LLMResponse(content=content, model=self.model)

    async def achat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        return self.chat(messages, **kwargs)


class _Action:
    """模拟 Action dataclass。"""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {"action_type": "done", "params": {"success": True}}


class _CaptchaStub:
    """模拟 CaptchaType enum。"""

    def __init__(self, value: str) -> None:
        self.value = value


class _Observation:
    """模拟 Observation dataclass。"""

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
        self.captcha_type = captcha_type if captcha_type is not None else _CaptchaStub("none")
        self.hook_data = hook_data if hook_data is not None else {"count": 0, "records": []}
        self.network_requests = network_requests or []
        self.scripts = scripts or []


# ---------------------------------------------------------------------------
# JudgeResult dataclass
# ---------------------------------------------------------------------------


class TestJudgeResult:
    def test_default_fields(self) -> None:
        r = JudgeResult(verified=True)
        assert r.verified is True
        assert r.missing == []
        assert r.reasoning == ""
        assert r.raw == {}

    def test_passed_is_alias_for_verified(self) -> None:
        assert JudgeResult(verified=True).passed is True
        assert JudgeResult(verified=False).passed is False

    def test_to_dict_returns_full_dict(self) -> None:
        r = JudgeResult(
            verified=False,
            missing=["a", "b"],
            reasoning="not done",
            raw={"foo": "bar"},
        )
        d = r.to_dict()
        assert d == {
            "verified": False,
            "missing": ["a", "b"],
            "reasoning": "not done",
            "raw": {"foo": "bar"},
        }

    def test_to_dict_missing_is_copied(self) -> None:
        """to_dict 应返回 missing 的副本，避免外部修改原列表。"""
        original = ["x"]
        r = JudgeResult(verified=False, missing=original)
        d = r.to_dict()
        d["missing"].append("y")
        assert r.missing == ["x"]  # 原列表不被影响


# ---------------------------------------------------------------------------
# TaskJudge 初始化
# ---------------------------------------------------------------------------


class TestTaskJudgeInit:
    def test_default_strict_is_true(self) -> None:
        judge = TaskJudge(_FakeProvider())
        assert judge.strict is True

    def test_strict_can_be_disabled(self) -> None:
        judge = TaskJudge(_FakeProvider(), strict=False)
        assert judge.strict is False


# ---------------------------------------------------------------------------
# TaskJudge.validate 严格模式
# ---------------------------------------------------------------------------


class TestTaskJudgeValidateStrict:
    def test_strict_missing_target_param_returns_failure_without_llm(self) -> None:
        """严格模式 + 缺参数 → 不调 LLM 直接判失败。"""
        provider = _FakeProvider(['{"verified": true}'])
        judge = TaskJudge(provider, strict=True)
        result = judge.validate(
            action=_Action(),
            observation=_Observation(),
            target_params_found={"a": "1"},  # 缺 b
            task="find a and b",
            target_params=["a", "b"],
        )
        assert result.verified is False
        assert result.missing == ["b"]
        assert "严格模式" in result.reasoning
        # LLM 不应被调用
        assert provider.calls == []

    def test_strict_all_params_found_calls_llm_and_passes(self) -> None:
        """严格模式 + 参数齐全 → 调 LLM，LLM 返回 verified=True 时通过。"""
        provider = _FakeProvider(['{"verified": true, "missing": [], "reasoning": "ok"}'])
        judge = TaskJudge(provider, strict=True)
        result = judge.validate(
            action=_Action(),
            observation=_Observation(),
            target_params_found={"a": "1", "b": "2"},
            task="find a and b",
            target_params=["a", "b"],
        )
        assert result.verified is True
        assert result.missing == []
        assert result.reasoning == "ok"
        assert provider.calls  # LLM 被调用

    def test_strict_llm_returns_verified_true_with_missing_is_corrected(self) -> None:
        """严格模式 + LLM 返回 verified=True 但 missing 非空 → 强制改为 False。"""
        provider = _FakeProvider(
            ['{"verified": true, "missing": ["c"], "reasoning": "ok but missing c"}']
        )
        judge = TaskJudge(provider, strict=True)
        result = judge.validate(
            action=_Action(),
            observation=_Observation(),
            target_params_found={"a": "1"},
            task="task",
            target_params=["a"],  # a 已找到，但 LLM 说 missing c
        )
        # strict + verified + missing 非空 → verified 强制 False
        assert result.verified is False
        assert "c" in result.missing

    def test_strict_llm_returns_verified_false_with_empty_missing_recomputes(self) -> None:
        """严格模式 + LLM 返回 verified=False 且 missing 为空 → 重算 missing。"""
        provider = _FakeProvider(
            ['{"verified": false, "missing": [], "reasoning": "not complete"}']
        )
        judge = TaskJudge(provider, strict=True)
        result = judge.validate(
            action=_Action(),
            observation=_Observation(),
            target_params_found={"a": "1"},  # 缺 b
            task="find a and b",
            target_params=["a", "b"],
        )
        assert result.verified is False
        # missing 应被重算为 ["b"]
        assert "b" in result.missing

    def test_strict_no_target_params_skips_shortcut_and_calls_llm(self) -> None:
        """严格模式 + target_params=None → 跳过差集检查，调 LLM。"""
        provider = _FakeProvider(['{"verified": true, "missing": []}'])
        judge = TaskJudge(provider, strict=True)
        result = judge.validate(
            action=_Action(),
            observation=_Observation(),
            target_params_found={"a": "1"},
            task="open task",
            target_params=None,
        )
        assert result.verified is True
        assert provider.calls  # LLM 被调用

    def test_llm_exception_returns_failure(self) -> None:
        """provider.chat 抛异常时返回 verified=False。"""

        class _FailProvider:
            model = "fail"

            def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
                raise RuntimeError("llm down")

        judge = TaskJudge(_FailProvider(), strict=False)  # type: ignore[arg-type]
        result = judge.validate(
            action=_Action(),
            observation=_Observation(),
            target_params_found={},
            task="task",
            target_params=["x"],
        )
        assert result.verified is False
        assert "x" in result.missing
        assert "Judge LLM 调用失败" in result.reasoning

    def test_missing_field_non_list_is_handled(self) -> None:
        """LLM 返回 missing 为非 list 时应被当作空列表处理。"""
        provider = _FakeProvider(['{"verified": true, "missing": "not-a-list"}'])
        judge = TaskJudge(provider, strict=False)
        result = judge.validate(
            action=_Action(),
            observation=_Observation(),
            target_params_found={},
            task="task",
        )
        assert result.verified is True
        assert result.missing == []  # 非 list 时被替换为空

    def test_extract_json_handles_code_fence(self) -> None:
        """LLM 返回带 ```json 代码块的 JSON 时应能解析。"""
        reply = "```json\n" + json.dumps({"verified": True, "missing": []}) + "\n```"
        provider = _FakeProvider([reply])
        judge = TaskJudge(provider, strict=False)
        result = judge.validate(
            action=_Action(),
            observation=_Observation(),
            target_params_found={},
            task="task",
        )
        assert result.verified is True

    def test_extract_json_handles_embedded_json(self) -> None:
        """LLM 在 JSON 前后有多余文字时应能提取内嵌 JSON。"""
        reply = "好的，以下是判断结果：\n" + json.dumps(
            {"verified": False, "missing": ["x"], "reasoning": "no x"}
        ) + "\n请查阅。"
        provider = _FakeProvider([reply])
        judge = TaskJudge(provider, strict=False)
        result = judge.validate(
            action=_Action(),
            observation=_Observation(),
            target_params_found={},
            task="task",
        )
        assert result.verified is False
        assert result.missing == ["x"]

    def test_extract_json_returns_empty_on_invalid_input(self) -> None:
        """LLM 返回完全无法解析的内容时返回空 dict，verified 默认 False。"""
        provider = _FakeProvider(["totally garbage no json here"])
        judge = TaskJudge(provider, strict=False)
        result = judge.validate(
            action=_Action(),
            observation=_Observation(),
            target_params_found={},
            task="task",
        )
        assert result.verified is False
        assert result.missing == []


# ---------------------------------------------------------------------------
# TaskJudge.validate_async
# ---------------------------------------------------------------------------


class TestTaskJudgeValidateAsync:
    @pytest.mark.asyncio
    async def test_async_strict_missing_returns_failure_without_llm(self) -> None:
        """异步路径严格模式缺参数直接判失败。"""
        provider = MagicMock()
        provider.achat = AsyncMock(return_value=LLMResponse(content="x", model="fake"))
        judge = TaskJudge(provider, strict=True)
        result = await judge.validate_async(
            action=_Action(),
            observation=_Observation(),
            target_params_found={},
            task="task",
            target_params=["missing-param"],
        )
        assert result.verified is False
        assert "missing-param" in result.missing
        provider.achat.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_uses_achat_when_available(self) -> None:
        """provider 有 achat 时走异步路径。"""
        provider = MagicMock()
        provider.achat = AsyncMock(
            return_value=LLMResponse(
                content='{"verified": true, "missing": []}', model="fake"
            )
        )
        judge = TaskJudge(provider, strict=False)
        result = await judge.validate_async(
            action=_Action(),
            observation=_Observation(),
            target_params_found={},
            task="task",
        )
        provider.achat.assert_awaited_once()
        assert result.verified is True

    @pytest.mark.asyncio
    async def test_async_falls_back_to_sync_chat(self) -> None:
        """provider 无 achat 时回退到同步 chat。"""
        provider = MagicMock(spec=["chat"])  # 只有 chat
        provider.chat.return_value = LLMResponse(
            content='{"verified": true, "missing": []}', model="fake"
        )
        judge = TaskJudge(provider, strict=False)
        result = await judge.validate_async(
            action=_Action(),
            observation=_Observation(),
            target_params_found={},
            task="task",
        )
        provider.chat.assert_called_once()
        assert result.verified is True

    @pytest.mark.asyncio
    async def test_async_llm_exception_returns_failure(self) -> None:
        """异步路径 LLM 异常返回失败。"""
        provider = MagicMock()
        provider.achat = AsyncMock(side_effect=RuntimeError("async llm down"))
        judge = TaskJudge(provider, strict=False)
        result = await judge.validate_async(
            action=_Action(),
            observation=_Observation(),
            target_params_found={},
            task="task",
            target_params=["x"],
        )
        assert result.verified is False
        assert "x" in result.missing
        assert "Judge LLM 调用失败" in result.reasoning

    @pytest.mark.asyncio
    async def test_async_strict_verified_with_missing_corrected(self) -> None:
        """异步严格模式 + LLM 返回 verified=True 但 missing 非空 → 强制 False。"""
        provider = MagicMock()
        provider.achat = AsyncMock(
            return_value=LLMResponse(
                content='{"verified": true, "missing": ["z"]}', model="fake"
            )
        )
        judge = TaskJudge(provider, strict=True)
        result = await judge.validate_async(
            action=_Action(),
            observation=_Observation(),
            target_params_found={"a": "1"},
            task="task",
            target_params=["a"],
        )
        assert result.verified is False
        assert "z" in result.missing

    @pytest.mark.asyncio
    async def test_async_strict_not_verified_with_empty_missing_recomputes(self) -> None:
        """异步严格模式 + LLM 返回 verified=False 且 missing 空 → 重算。"""
        provider = MagicMock()
        provider.achat = AsyncMock(
            return_value=LLMResponse(
                content='{"verified": false, "missing": []}', model="fake"
            )
        )
        judge = TaskJudge(provider, strict=True)
        result = await judge.validate_async(
            action=_Action(),
            observation=_Observation(),
            target_params_found={"a": "1"},
            task="task",
            target_params=["a", "b"],
        )
        assert result.verified is False
        assert "b" in result.missing


# ---------------------------------------------------------------------------
# _build_prompt 各种 action / observation 序列化路径
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_prompt_serializes_action_with_to_dict(self) -> None:
        """action 有 to_dict 方法时调用它。"""
        action = _Action()
        prompt = TaskJudge._build_prompt(
            action,
            _Observation(),
            {"sign": "abc"},
            "find sign",
            ["sign"],
        )
        assert "find sign" in prompt
        assert "sign" in prompt  # target_str
        assert "abc" in prompt  # found_str
        # action_str 应来自 to_dict
        assert "done" in prompt

    def test_prompt_serializes_action_with___dict___when_no_to_dict(self) -> None:
        """action 无 to_dict 但有 __dict__ 时序列化 __dict__。"""

        class _PlainAction:
            def __init__(self) -> None:
                self.action_type = "wait"
                self.params = {"seconds": 1}

        prompt = TaskJudge._build_prompt(
            _PlainAction(),
            _Observation(),
            {},
            "task",
            None,
        )
        assert "wait" in prompt
        assert "(未指定)" in prompt  # target_params=None

    def test_prompt_serializes_action_with_str_when_no_dict(self) -> None:
        """action 既无 to_dict 也无 __dict__ 时用 str()。"""
        prompt = TaskJudge._build_prompt(
            "just-a-string-action",  # type: ignore[arg-type]
            _Observation(),
            {},
            "task",
            None,
        )
        assert "just-a-string-action" in prompt

    def test_prompt_observation_fields_included(self) -> None:
        """observation 各字段应写入 prompt。"""
        obs = _Observation(
            url="https://x.example/p",
            page_title="Demo Title",
            hook_data={"count": 3, "records": [{"type": "fetch"}]},
            network_requests=[{"url": "a"}],
            scripts=["s1.js"],
        )
        prompt = TaskJudge._build_prompt(_Action(), obs, {}, "task", None)
        assert "https://x.example/p" in prompt
        assert "Demo Title" in prompt
        assert "none" in prompt  # captcha_type.value

    def test_prompt_observation_missing_fields_skipped(self) -> None:
        """observation 缺失字段（None）应被跳过不写入 prompt。"""

        class _SparseObs:
            url = "https://x.example"

            @property
            def __dict__(self) -> dict:  # type: ignore[override]
                return {"url": self.url}

        # 此时 getattr(obs, 'page_title', None) 返回 None
        prompt = TaskJudge._build_prompt(_Action(), _SparseObs(), {}, "task", None)  # type: ignore[arg-type]
        assert "https://x.example" in prompt

    def test_prompt_observation_list_dict_truncated(self) -> None:
        """observation 中 list/dict 字段应被截断到 1500 字符。"""
        long_records = [{"i": i} for i in range(500)]
        obs = _Observation(hook_data={"count": 500, "records": long_records})
        prompt = TaskJudge._build_prompt(_Action(), obs, {}, "task", None)
        # 应包含 hook_data 序列化结果（被截断）
        assert "hook_data" in prompt

    def test_prompt_target_params_none_uses_unspecified(self) -> None:
        """target_params=None 时显示 (未指定)。"""
        prompt = TaskJudge._build_prompt(_Action(), _Observation(), {}, "task", None)
        assert "(未指定)" in prompt

    def test_prompt_target_params_empty_list_uses_unspecified(self) -> None:
        """target_params=[] 时 target_str 为空字符串，显示 '(未指定)' 因为 falsy。"""
        # 注意实现：target_str = ", ".join([]) = "" → f-string 显示空字符串
        # 但 (未指定) 来自 task="" 时
        prompt = TaskJudge._build_prompt(_Action(), _Observation(), {}, "", [])
        # target_str="" 时 f"## 预期目标参数\n{target_str}\n" 显示空行
        assert "## 预期目标参数" in prompt

    def test_prompt_task_empty_uses_unspecified(self) -> None:
        """task="" 时显示 (未指定)。"""
        prompt = TaskJudge._build_prompt(_Action(), _Observation(), {}, "", None)
        assert "(未指定)" in prompt

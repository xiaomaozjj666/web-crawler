"""Tests for the ConfidenceScorer: action confidence scoring and browser action scoring."""

from __future__ import annotations

from typing import Any

import pytest

from web_crawler import LLMResponse
from web_crawler.ai.confidence import _VALID_ACTIONS, ConfidenceResult, ConfidenceScorer
from web_crawler.ai.llm import _normalize_messages


class FakeProvider:
    """Deterministic provider that replays canned JSON replies (no HTTP)."""

    model = "fake-model"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append(_normalize_messages(messages))
        content = self._replies.pop(0) if self._replies else "{}"
        return LLMResponse(content=content, model=self.model)


class AsyncFakeProvider(FakeProvider):
    """带 achat 方法的异步 provider。"""

    async def achat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append(_normalize_messages(messages))
        content = self._replies.pop(0) if self._replies else "{}"
        return LLMResponse(content=content, model=self.model)


class BrokenProvider:
    """chat 抛异常的 provider。"""

    model = "broken"

    def chat(self, messages: Any, **kwargs: Any) -> Any:
        raise RuntimeError("llm broken")


class BrokenAsyncProvider:
    """achat 抛异常的异步 provider。"""

    model = "broken-async"

    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        raise RuntimeError("async llm broken")


# ---------------------------------------------------------------------------
# ConfidenceResult dataclass
# ---------------------------------------------------------------------------


def test_confidence_result_passed_property() -> None:
    """passed 属性：score >= 0.5 为 True。"""
    assert ConfidenceResult(score=0.6).passed is True
    assert ConfidenceResult(score=0.5).passed is True
    assert ConfidenceResult(score=0.49).passed is False
    assert ConfidenceResult(score=0.0).passed is False


def test_confidence_result_to_dict() -> None:
    """to_dict 应返回完整字段。"""
    r = ConfidenceResult(
        score=0.72,
        reasons=["format: -0.1", "novelty: -0.2"],
        action_type="navigate",
        raw={"score": 0.9},
    )
    d = r.to_dict()
    assert d == {
        "score": 0.72,
        "reasons": ["format: -0.1", "novelty: -0.2"],
        "action_type": "navigate",
        "raw": {"score": 0.9},
    }
    # reasons 应是副本
    d["reasons"].append("x")
    assert len(r.reasons) == 2


def test_confidence_result_defaults() -> None:
    """ConfidenceResult 默认值。"""
    r = ConfidenceResult(score=1.0)
    assert r.reasons == []
    assert r.action_type == ""
    assert r.raw == {}


# ---------------------------------------------------------------------------
# ConfidenceScorer 初始化
# ---------------------------------------------------------------------------


def test_scorer_init_clamps_min_confidence() -> None:
    """min_confidence 应被钳制到 [0, 1]。"""
    s = ConfidenceScorer(min_confidence=-0.5)
    assert s.min_confidence == 0.0
    s = ConfidenceScorer(min_confidence=1.5)
    assert s.min_confidence == 1.0


def test_scorer_init_enable_llm_requires_provider() -> None:
    """无 provider 时 enable_llm_score 应被强制为 False。"""
    s = ConfidenceScorer(enable_llm_score=True, provider=None)
    assert s.enable_llm_score is False


def test_scorer_init_novelty_window_clamped() -> None:
    """novelty_window 应被钳制到 >= 1。"""
    s = ConfidenceScorer(novelty_window=0)
    assert s.novelty_window == 1
    s = ConfidenceScorer(novelty_window=-5)
    assert s.novelty_window == 1


# ---------------------------------------------------------------------------
# 基础 score 路径
# ---------------------------------------------------------------------------


def test_confidence_high_score_for_well_formed_action() -> None:
    scorer = ConfidenceScorer(min_confidence=0.5)
    action = {
        "action_type": "extract",
        "params": {"param_name": "Anti-Content"},
        "reasoning": "通过 hook 捕获到 Anti-Content 头，提取该参数",
    }
    result = scorer.score(action, task="提取 Anti-Content", target_params=["Anti-Content"])
    assert result.score >= 0.5
    assert result.action_type == "extract"


def test_confidence_low_score_for_invalid_action() -> None:
    scorer = ConfidenceScorer(min_confidence=0.5)
    action = {"action_type": "unknown_action", "params": {}, "reasoning": ""}
    result = scorer.score(action)
    assert result.score < 0.5
    assert scorer.should_reject(result)


def test_confidence_dedup_history_penalizes_repeat() -> None:
    scorer = ConfidenceScorer(min_confidence=0.5)
    action = {
        "action_type": "navigate",
        "params": {"url": "https://example.com/page"},
        "reasoning": "navigate to page",
    }
    history = [
        {"action": "navigate", "params": {"url": "https://example.com/page"}},
        {"action": "navigate", "params": {"url": "https://example.com/page"}},
    ]
    result = scorer.score(action, history=history)
    # 应被 novelty 规则扣分
    assert any("novelty" in r for r in result.reasons)


def test_confidence_llm_score_with_fake_provider() -> None:
    """LLM 评分路径：FakeProvider 返回固定 score。"""
    provider = FakeProvider(['{"score": 0.9, "reason": "valid action"}'])
    scorer = ConfidenceScorer(min_confidence=0.5, enable_llm_score=True, provider=provider)
    action = {
        "action_type": "extract",
        "params": {"param_name": "sign"},
        "reasoning": "从 hook 数据中提取 sign 参数",
    }
    result = scorer.score(action, task="extract sign", target_params=["sign"])
    # 综合 score = 规则分*0.6 + 0.9*0.4
    assert 0 < result.score <= 1.0
    assert result.raw.get("score") == 0.9


def test_confidence_scores_browser_actions() -> None:
    """click / type / scroll 等浏览器动作应得到合理置信度。"""
    scorer = ConfidenceScorer(min_confidence=0.4)
    # 完整 click 动作应得高分
    click_action = {
        "action_type": "click",
        "params": {"selector": "button#submit"},
        "reasoning": "点击提交按钮以触发加密参数生成",
    }
    result = scorer.score(click_action)
    assert result.action_type == "click"
    assert result.score >= 0.4
    # type 缺 text 应被扣分
    type_action = {
        "action_type": "type",
        "params": {"selector": "input#q"},
        "reasoning": "输入查询关键词",
    }
    type_result = scorer.score(type_action)
    assert type_result.score < 1.0
    assert any("text" in r for r in type_result.reasons)
    # scroll 无必填参数，应得高分
    scroll_action = {
        "action_type": "scroll",
        "params": {"x": 0, "y": 800},
        "reasoning": "向下滚动加载更多内容",
    }
    scroll_result = scorer.score(scroll_action)
    assert scroll_result.action_type == "scroll"
    assert scroll_result.score >= 0.5
    # 未知动作仍低分
    unknown = {
        "action_type": "frob",
        "params": {},
        "reasoning": "",
    }
    unknown_result = scorer.score(unknown)
    assert unknown_result.score < 0.5


def test_confidence_valid_actions_includes_browser_types() -> None:
    """_VALID_ACTIONS 应包含 6 类浏览器交互动作。"""
    for at in ("click", "type", "scroll", "press", "hover", "select_option"):
        assert at in _VALID_ACTIONS


# ---------------------------------------------------------------------------
# _score_format 分支
# ---------------------------------------------------------------------------


def test_score_format_missing_action_type() -> None:
    """缺 action_type 时 format 评分 0 并记录原因。"""
    scorer = ConfidenceScorer()
    result = scorer.score({"params": {}, "reasoning": "x" * 20})
    assert any("missing action_type" in r for r in result.reasons)


def test_score_format_unknown_action_type() -> None:
    """未知 action_type 时 format 评分 0.5。"""
    scorer = ConfidenceScorer()
    result = scorer.score({"action_type": "frob", "params": {}, "reasoning": "x" * 20})
    assert any("unknown action_type" in r for r in result.reasons)


def test_score_format_missing_params_field() -> None:
    """合法 action_type 但缺 params 字段时 format 评分 0.7。"""
    scorer = ConfidenceScorer()
    result = scorer.score({"action_type": "navigate", "reasoning": "x" * 20})
    assert any("missing params field" in r for r in result.reasons)


def test_score_format_complete() -> None:
    """完整动作 format 评分 1.0（无扣分原因）。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {"action_type": "navigate", "params": {"url": "https://x.com"}, "reasoning": "x" * 20}
    )
    assert not any("format:" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# _score_params 分支
# ---------------------------------------------------------------------------


def test_score_params_not_dict() -> None:
    """params 非 dict 时应扣分。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {"action_type": "navigate", "params": "not a dict", "reasoning": "x" * 20}
    )
    assert any("params is not dict" in r for r in result.reasons)


def test_score_params_wrong_type() -> None:
    """必填参数类型错误时应扣分。"""
    scorer = ConfidenceScorer()
    # url 应为 str，传 int
    result = scorer.score(
        {"action_type": "navigate", "params": {"url": 12345}, "reasoning": "x" * 20}
    )
    assert any("wrong type for" in r for r in result.reasons)


def test_score_params_missing_required() -> None:
    """缺必填参数时应扣分。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {"action_type": "navigate", "params": {}, "reasoning": "x" * 20}
    )
    assert any("missing required" in r for r in result.reasons)


def test_score_params_unknown_action_type() -> None:
    """未知 action_type 的 params 评分应返回 0.3。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {"action_type": "frob", "params": {}, "reasoning": "x" * 20}
    )
    assert any("can't validate unknown" in r for r in result.reasons)


def test_score_params_no_required_fields() -> None:
    """无必填参数的动作（如 solve_captcha/done/scroll）params 评分 1.0。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {"action_type": "done", "params": {}, "reasoning": "x" * 20}
    )
    assert not any("params:" in r for r in result.reasons)


def test_score_params_multiple_wrong_types() -> None:
    """多个必填参数类型错误时应累计扣分。"""
    scorer = ConfidenceScorer()
    # type 需要 selector:str + text:str，都传 int
    result = scorer.score(
        {"action_type": "type", "params": {"selector": 1, "text": 2}, "reasoning": "x" * 20}
    )
    wrong_type_reasons = [r for r in result.reasons if "wrong type" in r]
    assert len(wrong_type_reasons) == 2


# ---------------------------------------------------------------------------
# _score_novelty 分支
# ---------------------------------------------------------------------------


def test_score_novelty_no_history() -> None:
    """无历史时 novelty 评分 1.0。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {"action_type": "navigate", "params": {"url": "https://x.com"}, "reasoning": "x" * 20}
    )
    assert not any("novelty:" in r for r in result.reasons)


def test_score_novelty_single_duplicate() -> None:
    """1 次重复应扣 0.5。"""
    scorer = ConfidenceScorer()
    action = {
        "action_type": "navigate",
        "params": {"url": "https://x.com"},
        "reasoning": "x" * 20,
    }
    history = [{"action": "navigate", "params": {"url": "https://x.com"}}]
    result = scorer.score(action, history=history)
    assert any("novelty:" in r and "duplicated 1" in r for r in result.reasons)


def test_score_novelty_multiple_duplicates() -> None:
    """3+ 次重复应扣到 0。"""
    scorer = ConfidenceScorer()
    action = {
        "action_type": "navigate",
        "params": {"url": "https://x.com"},
        "reasoning": "x" * 20,
    }
    history = [
        {"action": "navigate", "params": {"url": "https://x.com"}},
        {"action": "navigate", "params": {"url": "https://x.com"}},
        {"action": "navigate", "params": {"url": "https://x.com"}},
    ]
    result = scorer.score(action, history=history)
    assert any("novelty:" in r for r in result.reasons)


def test_score_novelty_history_with_action_type_key() -> None:
    """history 用 action_type key 也应识别重复。"""
    scorer = ConfidenceScorer()
    action = {
        "action_type": "navigate",
        "params": {"url": "https://x.com"},
        "reasoning": "x" * 20,
    }
    history = [{"action_type": "navigate", "params": {"url": "https://x.com"}}]
    result = scorer.score(action, history=history)
    assert any("novelty:" in r for r in result.reasons)


def test_score_novelty_history_no_duplicate() -> None:
    """有历史但无重复时 novelty 评分 1.0（覆盖 duplicates==0 分支）。"""
    scorer = ConfidenceScorer()
    action = {
        "action_type": "navigate",
        "params": {"url": "https://new-url.com"},
        "reasoning": "x" * 20,
    }
    history = [
        {"action": "click", "params": {"selector": "#btn"}},
        {"action": "extract", "params": {"param_name": "sign"}},
    ]
    result = scorer.score(action, history=history)
    assert not any("novelty:" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# _score_relevance 分支
# ---------------------------------------------------------------------------


def test_score_relevance_unknown_action_type() -> None:
    """未知 action_type 时 relevance 评分 0.3。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {"action_type": "frob", "params": {}, "reasoning": "x" * 20},
        target_params=["sign"],
    )
    assert any("relevance:" in r and "unknown" in r for r in result.reasons)


def test_score_relevance_no_target_info() -> None:
    """无 target_params 和 task 时 relevance 评分 0.8。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {"action_type": "navigate", "params": {"url": "https://x.com"}, "reasoning": "x" * 20}
    )
    assert not any("relevance:" in r for r in result.reasons)


def test_score_relevance_done_all_mentioned() -> None:
    """done 动作提及所有目标参数时 relevance 评分 1.0。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {
            "action_type": "done",
            "params": {},
            "reasoning": "完成提取 sign 和 token 参数",
        },
        target_params=["sign", "token"],
    )
    assert not any("relevance:" in r for r in result.reasons)


def test_score_relevance_done_partial_mentioned() -> None:
    """done 动作只提及部分目标参数时应扣分。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {
            "action_type": "done",
            "params": {},
            "reasoning": "完成提取 sign 参数",
        },
        target_params=["sign", "token"],
    )
    assert any("done but only mentioned" in r for r in result.reasons)


def test_score_relevance_done_no_target_params() -> None:
    """done 动作无 target_params 时 relevance 评分 0.9。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {"action_type": "done", "params": {}, "reasoning": "x" * 20},
        task="完成任务",
    )
    assert not any("relevance:" in r for r in result.reasons)


def test_score_relevance_extract_no_target_params() -> None:
    """extract 动作无 target_params 时 relevance 评分 0.8。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {"action_type": "extract", "params": {"param_name": "x"}, "reasoning": "x" * 20},
        task="extract something",
    )
    assert not any("relevance:" in r for r in result.reasons)


def test_score_relevance_extract_in_target_params() -> None:
    """extract 动作的 param_name 在 target_params 中时 relevance 评分 1.0。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {"action_type": "extract", "params": {"param_name": "sign"}, "reasoning": "x" * 20},
        target_params=["sign", "token"],
    )
    assert not any("relevance:" in r for r in result.reasons)


def test_score_relevance_extract_not_in_target_params() -> None:
    """extract 动作的 param_name 不在 target_params 中时应扣分。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {"action_type": "extract", "params": {"param_name": "unknown"}, "reasoning": "x" * 20},
        target_params=["sign", "token"],
    )
    assert any("extract" in r and "not in target_params" in r for r in result.reasons)


def test_score_relevance_other_action_mentions_target() -> None:
    """其他动作 reasoning 提及目标参数时应加分。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {
            "action_type": "inject_hook",
            "params": {"hooks": ["fetch"]},
            "reasoning": "注入 hook 捕获 sign 参数的生成过程",
        },
        target_params=["sign"],
    )
    assert not any("relevance:" in r for r in result.reasons)


def test_score_relevance_other_action_no_mention() -> None:
    """其他动作 reasoning 未提及目标参数时 relevance 评分 0.7。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {
            "action_type": "inject_hook",
            "params": {"hooks": ["fetch"]},
            "reasoning": "注入 hook 捕获网络请求",
        },
        target_params=["sign"],
    )
    assert not any("relevance:" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# _score_reasoning 分支
# ---------------------------------------------------------------------------


def test_score_reasoning_empty() -> None:
    """空 reasoning 应扣分。"""
    scorer = ConfidenceScorer()
    result = scorer.score({"action_type": "done", "params": {}, "reasoning": ""})
    assert any("reasoning:" in r and "empty" in r for r in result.reasons)


def test_score_reasoning_too_short() -> None:
    """过短 reasoning（<10 字符）应扣分。"""
    scorer = ConfidenceScorer()
    result = scorer.score({"action_type": "done", "params": {}, "reasoning": "short"})
    assert any("reasoning:" in r and "too short" in r for r in result.reasons)


def test_score_reasoning_too_verbose() -> None:
    """过长 reasoning（>500 字符）应扣分。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {"action_type": "done", "params": {}, "reasoning": "x" * 501}
    )
    assert any("reasoning:" in r and "too verbose" in r for r in result.reasons)


def test_score_reasoning_proper_length() -> None:
    """合理长度 reasoning 评分 1.0。"""
    scorer = ConfidenceScorer()
    result = scorer.score(
        {"action_type": "done", "params": {}, "reasoning": "这是一个合理的推理过程"}
    )
    assert not any("reasoning:" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# should_reject
# ---------------------------------------------------------------------------


def test_should_reject_below_threshold() -> None:
    """分数低于阈值时应 reject。"""
    scorer = ConfidenceScorer(min_confidence=0.8)
    result = ConfidenceResult(score=0.5)
    assert scorer.should_reject(result) is True


def test_should_reject_at_threshold() -> None:
    """分数等于阈值时不应 reject。"""
    scorer = ConfidenceScorer(min_confidence=0.5)
    result = ConfidenceResult(score=0.5)
    assert scorer.should_reject(result) is False


# ---------------------------------------------------------------------------
# 异步 score_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_async_basic() -> None:
    """score_async 应与 score 行为一致。"""
    scorer = ConfidenceScorer(min_confidence=0.5)
    action = {
        "action_type": "extract",
        "params": {"param_name": "Anti-Content"},
        "reasoning": "通过 hook 捕获到 Anti-Content 头，提取该参数",
    }
    result = await scorer.score_async(
        action, task="提取 Anti-Content", target_params=["Anti-Content"]
    )
    assert result.score >= 0.5
    assert result.action_type == "extract"


@pytest.mark.asyncio
async def test_score_async_no_llm() -> None:
    """score_async 无 LLM 时 raw 应为空 dict。"""
    scorer = ConfidenceScorer()
    result = await scorer.score_async(
        {"action_type": "done", "params": {}, "reasoning": "x" * 20}
    )
    assert result.raw == {}


@pytest.mark.asyncio
async def test_score_async_with_llm_achat() -> None:
    """score_async 优先使用 provider.achat。"""
    provider = AsyncFakeProvider(['{"score": 0.85, "reason": "good"}'])
    scorer = ConfidenceScorer(enable_llm_score=True, provider=provider)
    result = await scorer.score_async(
        {"action_type": "done", "params": {}, "reasoning": "x" * 20}
    )
    assert result.raw.get("score") == 0.85
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_score_async_with_llm_no_achat_falls_back_to_chat() -> None:
    """provider 无 achat 时 score_async 回退到同步 chat。"""
    provider = FakeProvider(['{"score": 0.7, "reason": "ok"}'])
    scorer = ConfidenceScorer(enable_llm_score=True, provider=provider)
    result = await scorer.score_async(
        {"action_type": "done", "params": {}, "reasoning": "x" * 20}
    )
    assert result.raw.get("score") == 0.7
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_score_async_llm_error() -> None:
    """score_async LLM 异常时应降级返回 0.5。"""
    scorer = ConfidenceScorer(enable_llm_score=True, provider=BrokenAsyncProvider())
    result = await scorer.score_async(
        {"action_type": "done", "params": {}, "reasoning": "x" * 20}
    )
    # LLM 异常时 raw 为空，但仍返回评分
    assert result.raw == {}
    assert any("llm" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# LLM 评分路径
# ---------------------------------------------------------------------------


def test_llm_score_error_returns_default() -> None:
    """LLM 异常时应返回 0.5 默认分。"""
    scorer = ConfidenceScorer(enable_llm_score=True, provider=BrokenProvider())
    result = scorer.score({"action_type": "done", "params": {}, "reasoning": "x" * 20})
    assert result.raw == {}
    assert any("llm_error" in r for r in result.reasons)


def test_llm_score_clamps_to_01() -> None:
    """LLM 返回超出 [0,1] 的分数应被钳制。"""
    provider = FakeProvider(['{"score": 1.5, "reason": "too high"}'])
    scorer = ConfidenceScorer(enable_llm_score=True, provider=provider)
    result = scorer.score({"action_type": "done", "params": {}, "reasoning": "x" * 20})
    # raw 保留原始值，但 final_score 应被钳制
    assert result.raw.get("score") == 1.5
    assert result.score <= 1.0


def test_llm_score_negative_clamped() -> None:
    """LLM 返回负分应被钳制到 0。"""
    provider = FakeProvider(['{"score": -0.5, "reason": "too low"}'])
    scorer = ConfidenceScorer(enable_llm_score=True, provider=provider)
    result = scorer.score({"action_type": "done", "params": {}, "reasoning": "x" * 20})
    assert result.raw.get("score") == -0.5
    assert result.score >= 0.0


def test_llm_score_default_on_missing_score_field() -> None:
    """LLM 返回无 score 字段时应使用默认 0.5。"""
    provider = FakeProvider(['{"reason": "no score"}'])
    scorer = ConfidenceScorer(enable_llm_score=True, provider=provider)
    result = scorer.score({"action_type": "done", "params": {}, "reasoning": "x" * 20})
    assert result.raw.get("reason") == "no score"
    assert "score" not in result.raw or result.raw.get("score") == 0.5


# ---------------------------------------------------------------------------
# _parse_score_response 静态方法
# ---------------------------------------------------------------------------


def test_parse_score_response_valid_json() -> None:
    """合法 JSON 应正确解析。"""
    out = ConfidenceScorer._parse_score_response('{"score": 0.8, "reason": "good"}')
    assert out["score"] == 0.8
    assert out["reason"] == "good"


def test_parse_score_response_with_markdown() -> None:
    """带 markdown 代码块应提取 JSON。"""
    text = '```json\n{"score": 0.9}\n```'
    out = ConfidenceScorer._parse_score_response(text)
    assert out["score"] == 0.9


def test_parse_score_response_with_surrounding_text() -> None:
    """带前后文本应提取 JSON。"""
    text = 'The result is: {"score": 0.7, "reason": "ok"}\nDone.'
    out = ConfidenceScorer._parse_score_response(text)
    assert out["score"] == 0.7


def test_parse_score_response_no_json_object() -> None:
    """无 JSON 对象时应返回 parse_failed。"""
    out = ConfidenceScorer._parse_score_response("no json here")
    assert out == {"score": 0.5, "reason": "parse_failed"}


def test_parse_score_response_invalid_json() -> None:
    """非法 JSON 应返回 parse_failed。"""
    out = ConfidenceScorer._parse_score_response("{not valid json}")
    assert out == {"score": 0.5, "reason": "parse_failed"}


# ---------------------------------------------------------------------------
# _build_score_prompt 静态方法
# ---------------------------------------------------------------------------


def test_build_score_prompt_includes_all_sections() -> None:
    """_build_score_prompt 应包含任务、目标参数、动作、历史。"""
    prompt = ConfidenceScorer._build_score_prompt(
        action={"action_type": "navigate", "params": {"url": "https://x.com"}},
        task="extract sign",
        target_params=["sign"],
        history=[{"action": "click", "params": {"selector": "#btn"}}],
    )
    assert "extract sign" in prompt
    assert "sign" in prompt
    assert "navigate" in prompt
    assert "click" in prompt


def test_build_score_prompt_empty_inputs() -> None:
    """空输入应使用占位符。"""
    prompt = ConfidenceScorer._build_score_prompt(
        action={}, task="", target_params=[], history=[]
    )
    assert "未指定" in prompt


def test_build_score_prompt_truncates_long_history() -> None:
    """历史超过 5 条时应只保留最近 5 条。"""
    history = [{"action": f"a{i}"} for i in range(10)]
    prompt = ConfidenceScorer._build_score_prompt(
        action={"action_type": "done"}, task="t", target_params=[], history=history
    )
    # 应包含最后 5 条
    assert "a9" in prompt
    assert "a5" in prompt
    # 不应包含前 5 条
    assert "a0" not in prompt
    assert "a4" not in prompt


# ---------------------------------------------------------------------------
# _action_to_dict 静态方法
# ---------------------------------------------------------------------------


def test_action_to_dict_from_dict() -> None:
    """dict 直接返回。"""
    d = {"action_type": "navigate", "params": {"url": "x"}}
    assert ConfidenceScorer._action_to_dict(d) is d


def test_action_to_dict_from_to_dict_method() -> None:
    """有 to_dict 方法的对象调用其 to_dict。"""

    class _Obj:
        def to_dict(self) -> dict[str, Any]:
            return {"action_type": "done", "params": {}}

    obj = _Obj()
    assert ConfidenceScorer._action_to_dict(obj) == {"action_type": "done", "params": {}}


def test_action_to_dict_from_object_attrs() -> None:
    """有 __dict__ 的对象转 dict。"""

    class _Obj:
        def __init__(self) -> None:
            self.action_type = "click"
            self.params = {"selector": "#x"}

    obj = _Obj()
    out = ConfidenceScorer._action_to_dict(obj)
    assert out["action_type"] == "click"
    assert out["params"] == {"selector": "#x"}


def test_action_to_dict_from_primitive() -> None:
    """原始值转为 {action_type: str(value)}。"""
    out = ConfidenceScorer._action_to_dict("just a string")
    assert out == {"action_type": "just a string"}
    out = ConfidenceScorer._action_to_dict(42)
    assert out == {"action_type": "42"}


# ---------------------------------------------------------------------------
# 集成场景
# ---------------------------------------------------------------------------


def test_score_with_action_object_via_to_dict() -> None:
    """传带 to_dict 方法的 Action 对象应正常评分。"""

    class _Action:
        action_type = "navigate"
        params = {"url": "https://example.com"}
        reasoning = "导航到目标页面进行抓取"

        def to_dict(self) -> dict[str, Any]:
            return {
                "action_type": self.action_type,
                "params": self.params,
                "reasoning": self.reasoning,
            }

    scorer = ConfidenceScorer()
    result = scorer.score(_Action())
    assert result.action_type == "navigate"


def test_score_with_action_object_via_dict() -> None:
    """传有 __dict__ 的 Action 对象应正常评分。"""

    class _Action:
        def __init__(self) -> None:
            self.action_type = "click"
            self.params = {"selector": "#btn"}
            self.reasoning = "点击按钮"

    scorer = ConfidenceScorer()
    result = scorer.score(_Action())
    assert result.action_type == "click"


def test_score_reasoning_with_target_params_mentioned() -> None:
    """reasoning 提及多个目标参数时相关性应递增。"""
    scorer = ConfidenceScorer()
    action = {
        "action_type": "inject_hook",
        "params": {"hooks": ["fetch"]},
        "reasoning": "注入 hook 捕获 sign 和 token 的生成过程",
    }
    result = scorer.score(action, target_params=["sign", "token"])
    # reasoning 提及 sign 和 token，相关性应高
    assert result.score > 0.5

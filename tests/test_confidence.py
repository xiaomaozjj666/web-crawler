"""Tests for the ConfidenceScorer: action confidence scoring and browser action scoring."""

from __future__ import annotations

from typing import Any

from web_crawler import LLMResponse
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


def test_confidence_high_score_for_well_formed_action() -> None:
    from web_crawler.ai.confidence import ConfidenceScorer

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
    from web_crawler.ai.confidence import ConfidenceScorer

    scorer = ConfidenceScorer(min_confidence=0.5)
    action = {"action_type": "unknown_action", "params": {}, "reasoning": ""}
    result = scorer.score(action)
    assert result.score < 0.5
    assert scorer.should_reject(result)


def test_confidence_dedup_history_penalizes_repeat() -> None:
    from web_crawler.ai.confidence import ConfidenceScorer

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
    from web_crawler.ai.confidence import ConfidenceScorer

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
    from web_crawler.ai.confidence import ConfidenceScorer

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
    from web_crawler.ai.confidence import _VALID_ACTIONS

    for at in ("click", "type", "scroll", "press", "hover", "select_option"):
        assert at in _VALID_ACTIONS

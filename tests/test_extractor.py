"""Tests for the AIExtractor: selector generation, self-healing, and validation."""

from __future__ import annotations

from typing import Any

import pytest

from web_crawler import AIExtractor, LLMResponse, Response, Selector
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


_HTML = (
    '<html><body><h1 class="title">Hello World</h1>'
    '<a class="more" href="/next">next</a></body></html>'
)


def _response() -> Response:
    return Response("https://example.com", 200, _HTML.encode("utf-8"))


def test_extractor_generates_and_validates_selectors() -> None:
    provider = FakeProvider(['{"title": "h1.title", "link": "a.more::attr(href)"}'])
    extractor = AIExtractor(provider=provider)
    result = extractor.extract(_response(), {"title": "heading", "link": "the link"})

    assert result.ok
    assert result.data["title"] == "Hello World"
    assert result.data["link"] == "/next"
    assert result.selectors["link"] == "a.more::attr(href)"


def test_extractor_self_heals_failing_field() -> None:
    # First reply has a wrong selector for `title`; heal round fixes it.
    provider = FakeProvider(
        [
            '{"title": "h1.wrong", "link": "a.more::attr(href)"}',
            '{"title": "h1.title"}',
        ]
    )
    extractor = AIExtractor(provider=provider)
    result = extractor.extract(_response(), {"title": "heading", "link": "the link"})

    assert result.data["title"] == "Hello World"
    assert result.rounds == 2
    assert result.ok


def test_extractor_reports_missing_when_unhealable() -> None:
    provider = FakeProvider(['{"title": "h1.nope"}'])
    extractor = AIExtractor(provider=provider, max_heal_rounds=0)
    result = extractor.extract(_response(), {"title": "heading"})

    assert not result.ok
    assert "title" in result.missing


def test_extractor_accepts_selector_directly() -> None:
    provider = FakeProvider(['{"title": "h1.title"}'])
    extractor = AIExtractor(provider=provider)
    sel = Selector(_HTML, url="https://example.com")
    result = extractor.extract(sel, {"title": "heading"})
    assert result.data["title"] == "Hello World"


# ===========================================================================
# 扩展：未覆盖分支补齐
# ===========================================================================


def test_extract_json_parses_plain_json() -> None:
    """_extract_json 应直接解析纯 JSON 文本。"""
    from web_crawler.ai.extractor import _extract_json

    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_falls_back_to_regex_search() -> None:
    """_extract_json 在纯 JSON 解析失败时应使用正则提取嵌入的 JSON 对象。"""
    from web_crawler.ai.extractor import _extract_json

    # 纯 JSON 解析失败（有前后噪音），正则提取 {"a": 1}
    result = _extract_json('noise {"a": 1} trailing')
    assert result == {"a": 1}


def test_extract_json_regex_match_but_invalid_json_returns_empty() -> None:
    """正则匹配到 {...} 但内部 JSON 无效时返回空 dict。"""
    from web_crawler.ai.extractor import _extract_json

    result = _extract_json("{invalid json content}")
    assert result == {}


def test_extract_json_no_match_returns_empty() -> None:
    """文本中无可匹配的 JSON 对象时返回空 dict。"""
    from web_crawler.ai.extractor import _extract_json

    assert _extract_json("no json here at all") == {}


def test_extractor_init_with_model_param(monkeypatch: pytest.MonkeyPatch) -> None:
    """构造 AIExtractor 时传入 model 参数应调用 get_provider(model=...)。"""
    from web_crawler.ai import extractor as extractor_module

    captured: dict[str, Any] = {}

    def fake_get_provider(model: str | None = None) -> Any:
        captured["model"] = model
        return FakeProvider(["{}"])

    monkeypatch.setattr(extractor_module, "get_provider", fake_get_provider)
    AIExtractor(model="custom-model")
    assert captured["model"] == "custom-model"


def test_extractor_html_sample_with_non_str_html() -> None:
    """_html_sample 在 html 属性非 str 时应转为字符串。"""
    provider = FakeProvider(["{}"])
    extractor = AIExtractor(provider=provider)

    class FakeSelector:
        html = 12345  # 非 str

    result = extractor._html_sample(FakeSelector())  # type: ignore[arg-type]
    assert isinstance(result, str)
    assert "12345" in result


def test_extractor_self_heal_breaks_when_no_fixes_returned() -> None:
    """自愈阶段 provider 返回空 fixes 时应立即 break（不再重试）。"""
    # 第一轮返回错误 selector，自愈轮返回空 dict → break
    provider = FakeProvider(['{"title": "h1.wrong"}', "{}"])
    extractor = AIExtractor(provider=provider, max_heal_rounds=3)
    result = extractor.extract(_response(), {"title": "heading"})
    assert not result.ok
    assert "title" in result.missing
    # 只调用了 2 次（初始 + 1 次自愈），因为空 fixes 触发 break
    assert len(provider.calls) == 2

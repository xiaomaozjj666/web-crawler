"""Tests for AIScrapeAgent block detection and human handoff."""

from __future__ import annotations

from typing import Any

from web_crawler import AIExtractor, AIScrapeAgent, LLMResponse, Response
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


class FakeFetcher:
    """Minimal fetcher returning canned responses; records fetched URLs."""

    def __init__(self, resp: Response) -> None:
        self._resp = resp
        self.fetched: list[str] = []

    def get(self, url: str) -> Response:
        self.fetched.append(url)
        return self._resp


class ExplodingProvider:
    """Provider that fails if used — proves extraction was skipped."""

    model = "boom"

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        raise AssertionError("extractor must not be called on a blocked page")


_HTML = (
    '<html><body><h1 class="title">Hello World</h1>'
    '<a class="more" href="/next">next</a></body></html>'
)


def _response() -> Response:
    return Response("https://example.com", 200, _HTML.encode("utf-8"))


def test_detect_block_on_captcha_body() -> None:
    from web_crawler.ai.agent import detect_block

    resp = Response("https://x.example", 200, b"<html>Please complete the captcha</html>")
    assert detect_block(resp) is not None


def test_detect_block_on_forbidden_status() -> None:
    from web_crawler.ai.agent import detect_block

    resp = Response("https://x.example", 403, b"<html>nope</html>")
    assert detect_block(resp) == "http 403"


def test_detect_block_none_on_normal_page() -> None:
    from web_crawler.ai.agent import detect_block

    assert detect_block(_response()) is None


def test_agent_hands_off_to_human_on_block() -> None:

    captcha = Response("https://x.example", 200, b"<html>hcaptcha challenge</html>")
    fetcher = FakeFetcher(captcha)
    seen: list[Any] = []
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=AIExtractor(provider=ExplodingProvider()),
        respect_robots=False,
        min_delay=0.0,
        on_block=seen.append,
    )
    result = agent.scrape("https://x.example", {"title": "heading"})

    assert result.needs_human is True
    assert result.block_reason is not None
    assert not result.ok
    assert result.data == {}
    assert len(seen) == 1  # on_block callback fired once


def test_agent_extracts_normally_when_not_blocked() -> None:

    fetcher = FakeFetcher(_response())
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=AIExtractor(provider=FakeProvider(['{"title": "h1.title"}'])),
        respect_robots=False,
        min_delay=0.0,
    )
    result = agent.scrape("https://example.com", {"title": "heading"})

    assert not result.needs_human
    assert result.data["title"] == "Hello World"

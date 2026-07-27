"""Tests for the additive AI layer (no network; a fake LLM provider is used)."""

from __future__ import annotations

from typing import Any

import pytest

from web_crawler import (
    AIExtractor,
    DeepSeekProvider,
    LLMMessage,
    LLMResponse,
    Response,
    Selector,
    available_providers,
    get_provider,
    register_provider,
)
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


# -- llm layer --------------------------------------------------------------
def test_normalize_messages_accepts_str_dict_and_message() -> None:
    out = _normalize_messages([LLMMessage("system", "s"), {"role": "user", "content": "u"}, "bare"])
    assert out == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "user", "content": "bare"},
    ]
    assert _normalize_messages("hello") == [{"role": "user", "content": "hello"}]


def test_get_provider_defaults_to_deepseek_v4_pro() -> None:
    provider = get_provider(api_key="dummy")
    assert isinstance(provider, DeepSeekProvider)
    assert provider.model == "deepseek-v4-pro"
    assert "deepseek" in available_providers()


def test_deepseek_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the test hermetic: ignore any real key from the environment / .env file.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("web_crawler.ai.llm._DOTENV_LOADED", True)
    provider = DeepSeekProvider(api_key="")
    with pytest.raises(RuntimeError, match="no API key"):
        provider.chat("hi")


def test_register_custom_provider() -> None:
    register_provider("fakereg", lambda **kw: FakeProvider(["{}"]))
    assert "fakereg" in available_providers()
    assert isinstance(get_provider("fakereg"), FakeProvider)


# -- extractor --------------------------------------------------------------
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


# -- agent: block detection & human handoff (BrowserAct-inspired) -----------
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
    from web_crawler import AIScrapeAgent

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
    from web_crawler import AIScrapeAgent

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


# -- camoufox fetcher (graceful degrade when camoufox is absent) ------------
def test_camoufox_fetcher_requires_camoufox() -> None:
    from web_crawler import CamoufoxFetcher
    from web_crawler.compat import HAS_CAMOUFOX

    if HAS_CAMOUFOX:
        # Only build launch kwargs; do not actually launch a browser in CI.
        f = CamoufoxFetcher(os="windows", humanize=True, geoip=True, block_webrtc=True)
        kwargs = f._launch_kwargs()
        assert kwargs["os"] == "windows"
        assert kwargs["humanize"] is True
        assert kwargs["geoip"] is True
        assert kwargs["block_webrtc"] is True
    else:
        with pytest.raises(ImportError, match="camoufox"):
            CamoufoxFetcher()

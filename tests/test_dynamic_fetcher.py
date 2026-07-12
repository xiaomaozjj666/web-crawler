"""Tests for DynamicFetcher / StealthyFetcher construction and helpers.

These exercise the non-browser code paths (proxy parsing, resource blocking,
stealth JS injection constant, default values). Launching real browsers is too
slow/flaky for the unit test suite; the full render path is covered by the
smoke test in the fetchers package itself.
"""

from __future__ import annotations

import pytest

from web_crawler import DynamicFetcher, StealthyFetcher, compat


def test_dynamic_fetcher_constructs_when_playwright_available() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(headless=True, timeout=15.0, block_images=True)
    assert f.headless is True
    assert f.timeout == 15.0
    assert f.block_images is True
    f.close()


def test_parse_proxy_simple() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    settings = f._parse_proxy("http://1.2.3.4:8080")
    assert settings == {"server": "http://1.2.3.4:8080"}
    f.close()


def test_parse_proxy_with_auth() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    settings = f._parse_proxy("http://user:pass@1.2.3.4:8080")
    assert settings["server"] == "http://1.2.3.4:8080"
    assert settings["username"] == "user"
    assert settings["password"] == "pass"
    f.close()


def test_parse_proxy_none_returns_none() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    assert f._parse_proxy(None) is None
    assert f._parse_proxy("") is None
    f.close()


def test_blocked_types_empty_by_default() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(block_images=False, disable_resources=False)
    assert f._blocked_types() == set()
    f.close()


def test_blocked_types_images_only() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(block_images=True, disable_resources=False)
    assert f._blocked_types() == {"image"}
    f.close()


def test_blocked_types_disable_resources() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(block_images=True, disable_resources=True)
    blocked = f._blocked_types()
    assert "image" in blocked
    assert "media" in blocked
    assert "font" in blocked
    f.close()


def test_route_handler_aborts_blocked_and_continues_others() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(block_images=True)
    handler = f._make_route_handler({"image"})

    class FakeRoute:
        def __init__(self, rtype: str) -> None:
            self.request = type("Req", (), {"resource_type": rtype})()
            self.aborted = False
            self.continued = False

        def abort(self) -> None:
            self.aborted = True

        def continue_(self) -> None:
            self.continued = True

    blocked_route = FakeRoute("image")
    handler(blocked_route)
    assert blocked_route.aborted is True
    assert blocked_route.continued is False

    ok_route = FakeRoute("document")
    handler(ok_route)
    assert ok_route.aborted is False
    assert ok_route.continued is True
    f.close()


def test_stealthy_fetcher_inherits_dynamic() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    assert issubclass(StealthyFetcher, DynamicFetcher)


def test_stealthy_fetcher_defaults_are_more_aggressive() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = StealthyFetcher()
    # Stealthy defaults block images, fake google referer, humanize + cf solve.
    assert f.block_images is True
    assert f.google_search is True
    assert f.humanize is True
    assert f.solve_cloudflare is True
    assert f.wait_timeout == 15.0
    f.close()


def test_stealthy_js_constant_is_substantial() -> None:
    from web_crawler.fetchers.stealthy import _STEALTH_JS

    # The stealth script must patch the key browser-fingerprint surfaces.
    assert "webdriver" in _STEALTH_JS
    assert "plugins" in _STEALTH_JS
    assert "languages" in _STEALTH_JS
    assert "userAgent" in _STEALTH_JS
    assert len(_STEALTH_JS) > 200


def test_stealthy_fetcher_overrides_hooks() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = StealthyFetcher()
    # StealthyFetcher overrides the rendering hooks to inject stealth JS.
    assert "stealthy" in type(f)._setup_page.__qualname__.lower() or (
        type(f)._setup_page is not DynamicFetcher._setup_page
    )
    f.close()

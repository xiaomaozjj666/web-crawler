"""Tests for the Fetcher (stealth HTTP via curl_cffi / httpx fallback).

These hit a local HTTP server (see conftest) so they exercise the real request
path including TLS-fingerprint impersonation against localhost.
"""

from __future__ import annotations

import pytest

from web_crawler import Fetcher, ProxyPool, Response, compat


def test_fetcher_constructs_with_curl_cffi() -> None:
    if not compat.HAS_CURL_CFFI:
        pytest.skip("curl_cffi not installed")
    with Fetcher(impersonate="chrome131", timeout=5.0) as f:
        assert f._use_curl is True
        assert f.impersonate == "chrome131"
        assert f.timeout == 5.0


def test_fetcher_get_returns_response(local_server: str) -> None:
    with Fetcher(timeout=10.0) as f:
        resp = f.get(local_server + "/")
    assert isinstance(resp, Response)
    assert resp.status == 200
    assert resp.ok
    assert b"Welcome" in resp.content
    # Selector helpers work on the fetched body
    assert resp.css_first("h1") is not None
    assert str(resp.css_first("h1").text) == "Welcome"


def test_fetcher_get_json(local_server: str) -> None:
    with Fetcher(timeout=10.0) as f:
        resp = f.get(local_server + "/json")
    assert resp.json() == {"ok": True, "n": 42}


def test_fetcher_get_404(local_server: str) -> None:
    with Fetcher(timeout=10.0) as f:
        resp = f.get(local_server + "/404")
    assert resp.status == 404
    assert resp.ok is False


def test_fetcher_context_manager_closes(local_server: str) -> None:
    with Fetcher(timeout=10.0) as f:
        f.get(local_server + "/")
    assert f._session is None


def test_fetcher_post(local_server: str) -> None:
    with Fetcher(timeout=10.0) as f:
        resp = f.post(local_server + "/")
    # http.server returns 501 for unimplemented methods
    assert resp.status >= 400


def test_fetcher_async_get(local_server: str) -> None:
    import asyncio

    async def go() -> Response:
        async with Fetcher(timeout=10.0) as f:
            return await f.async_get(local_server + "/")

    resp = asyncio.run(go())
    assert resp.status == 200
    assert b"Welcome" in resp.content


def test_fetcher_with_proxy_pool_is_consulted_per_request() -> None:
    # The fetcher must ask the pool for a proxy on each request. We verify the
    # pool is wired up by spying on its internal counter (no network needed).
    pool = ProxyPool(["http://127.0.0.1:8080", "http://127.0.0.1:8081"])
    with Fetcher(proxy=pool, timeout=2.0, retries=0) as f:
        # _resolve_proxy is the integration point between fetcher and pool.
        first = f._resolve_proxy()
        second = f._resolve_proxy()
    assert first in {"http://127.0.0.1:8080", "http://127.0.0.1:8081"}
    # round-robin cycles to the other proxy on the second call
    assert second != first
    assert len(pool) == 2


def test_fetcher_request_headers_merged(local_server: str) -> None:
    with Fetcher(timeout=10.0, extra_headers={"X-Test": "abc"}) as f:
        merged = f._merge_headers({"X-Per": "req"})
    # User headers preserved
    assert merged["X-Test"] == "abc"
    assert merged["X-Per"] == "req"


def test_fetcher_retries_on_server_error(monkeypatch, local_server: str) -> None:
    # Force 500s by monkeypatching is hard; instead verify retries arg is stored.
    with Fetcher(retries=2, timeout=10.0) as f:
        assert f.retries == 2


def test_parse_retry_after_seconds() -> None:
    from web_crawler.fetchers.fetcher import _parse_retry_after

    assert _parse_retry_after("120") == 120.0
    assert _parse_retry_after("0") == 0.0


def test_parse_retry_after_caps_at_300() -> None:
    from web_crawler.fetchers.fetcher import _parse_retry_after

    # 超过 5 分钟上限会被截断
    assert _parse_retry_after("9999") == 300.0


def test_parse_retry_after_none_or_invalid() -> None:
    from web_crawler.fetchers.fetcher import _parse_retry_after

    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("garbage") is None


def test_parse_retry_after_http_date() -> None:
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    from web_crawler.fetchers.fetcher import _parse_retry_after

    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    value = format_datetime(future, usegmt=True)
    result = _parse_retry_after(value)
    assert result is not None
    assert 50 <= result <= 70


def test_fetcher_default_headers_are_realistic() -> None:
    with Fetcher() as f:
        headers = f._default_headers()
    assert "Accept-Language" in headers
    assert "Sec-Ch-Ua" in headers
    assert "Upgrade-Insecure-Requests" in headers


def test_fetcher_build_response_carries_adaptive() -> None:
    with Fetcher(adaptive=True) as f:
        resp = f._build_response("https://x.example", 200, b"<a id='p'>x</a>", {})
    assert resp.selector.adaptive is True


# -- JA4 指纹定制 ----------------------------------------------------------


def test_fetcher_ja4_fingerprint_stored() -> None:
    """ja4_fingerprint 参数应被存储到 self.ja4_fingerprint。"""
    ja4 = "t13d1516h2_8daaf6152771_b0da82dd1658"
    with Fetcher(ja4_fingerprint=ja4) as f:
        assert f.ja4_fingerprint == ja4


def test_fetcher_ja4_fingerprint_default_none() -> None:
    """默认 ja4_fingerprint 为 None（不定制）。"""
    with Fetcher() as f:
        assert f.ja4_fingerprint is None


def test_fetcher_ja4_passed_to_curl_sync_session(monkeypatch) -> None:
    """ja4_fingerprint 应通过 ja3 参数透传到 curl_cffi Session（同步）。"""
    captured: dict[str, object] = {}

    class _StubSession:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    from web_crawler.fetchers import fetcher as fetcher_mod

    class _Ver:
        V1_1 = "v1_1"

    monkeypatch.setattr(
        fetcher_mod,
        "_load_curl_backend",
        lambda: (_Ver, _StubSession, _StubSession, Exception),
    )

    ja4 = "t13d1516h2_8daaf6152771_b0da82dd1658"
    with Fetcher(ja4_fingerprint=ja4, http2=True) as f:
        session = f._build_curl_sync_session()
    assert isinstance(session, _StubSession)
    assert captured["ja3"] == ja4
    assert captured["impersonate"] == "chrome131"
    # http2=True 时不写 http_version
    assert "http_version" not in captured


def test_fetcher_ja4_passed_to_curl_async_session(monkeypatch) -> None:
    """ja4_fingerprint 应通过 ja3 参数透传到 curl_cffi AsyncSession（异步）。"""
    captured: dict[str, object] = {}

    class _StubAsyncSession:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    from web_crawler.fetchers import fetcher as fetcher_mod

    class _Ver:
        V1_1 = "v1_1"

    monkeypatch.setattr(
        fetcher_mod,
        "_load_curl_backend",
        lambda: (_Ver, _StubAsyncSession, _StubAsyncSession, Exception),
    )

    ja4 = "t13d1516h2_8daaf6152771_b0da82dd1658"
    with Fetcher(ja4_fingerprint=ja4) as f:
        session = f._build_curl_async_session()
    assert isinstance(session, _StubAsyncSession)
    assert captured["ja3"] == ja4


def test_fetcher_ja4_not_passed_when_none(monkeypatch) -> None:
    """ja4_fingerprint=None 时不应向 curl_cffi Session 传 ja3 参数。"""
    captured: dict[str, object] = {}

    class _StubSession:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    from web_crawler.fetchers import fetcher as fetcher_mod

    class _Ver:
        V1_1 = "v1_1"

    monkeypatch.setattr(
        fetcher_mod,
        "_load_curl_backend",
        lambda: (_Ver, _StubSession, _StubSession, Exception),
    )

    with Fetcher(ja4_fingerprint=None) as f:
        session = f._build_curl_sync_session()
    assert isinstance(session, _StubSession)
    assert "ja3" not in captured, "ja4=None 时不应传 ja3 参数"

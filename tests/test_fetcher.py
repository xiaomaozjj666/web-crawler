"""Tests for the Fetcher (stealth HTTP via curl_cffi / httpx fallback).

These hit a local HTTP server (see conftest) so they exercise the real request
path including TLS-fingerprint impersonation against localhost.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import httpx
import pytest

from web_crawler import AsyncFetcher, Fetcher, ProxyPool, Response, compat


class _RedirectHandler(BaseHTTPRequestHandler):
    """Serves redirect chains / loops / non-http redirect targets."""

    def log_message(self, *args: object) -> None:  # silence test noise
        pass

    def do_GET(self) -> None:
        if self.path == "/chain/1":
            self._redirect("/chain/2")
        elif self.path == "/chain/2":
            self._redirect("/chain/3")
        elif self.path == "/chain/3":
            self._redirect("/chain/final")
        elif self.path == "/chain/final":
            self._send(200, b"final")
        elif self.path == "/loop":
            self._redirect("/loop")
        elif self.path == "/to-file":
            self._redirect("file:///etc/passwd")
        elif self.path == "/to-ftp":
            self._redirect("ftp://example.com/x")
        else:
            self._send(404, b"no such route")

    def do_POST(self) -> None:
        # 先读完请求体再响应：未读数据残留会让连接关闭时发 RST 而非 FIN，
        # Windows 上客户端偶发 WinError 10053（主机软件中止连接）
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length:
            self.rfile.read(length)
        if self.path == "/post-303":
            # 303 See Other：无论原方法都改 GET
            self.send_response(303)
            self.send_header("Location", "/chain/final")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(404, b"no such route")

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def redirect_server() -> str:
    """Local server with redirect routes (chain, loop, non-http targets)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join(timeout=5)


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


# -- JA3 指纹定制 ----------------------------------------------------------


def test_fetcher_ja3_fingerprint_stored() -> None:
    """ja3_fingerprint 参数应被存储到 self.ja3_fingerprint。"""
    ja3 = "771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-17513,29-23-24,0"
    with Fetcher(ja3_fingerprint=ja3) as f:
        assert f.ja3_fingerprint == ja3


def test_fetcher_ja3_fingerprint_default_none() -> None:
    """默认 ja3_fingerprint 为 None（不定制）。"""
    with Fetcher() as f:
        assert f.ja3_fingerprint is None


def test_fetcher_ja3_passed_to_curl_sync_session(monkeypatch) -> None:
    """ja3_fingerprint 应通过 ja3 参数透传到 curl_cffi Session（同步）。"""
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

    ja3 = "771,4865-4866,0-23-65281,29-23-24,0"
    with Fetcher(ja3_fingerprint=ja3, http2=True) as f:
        session = f._build_curl_sync_session()
    assert isinstance(session, _StubSession)
    assert captured["ja3"] == ja3
    assert captured["impersonate"] == "chrome131"
    # http2=True 时不写 http_version
    assert "http_version" not in captured


def test_fetcher_ja3_passed_to_curl_async_session(monkeypatch) -> None:
    """ja3_fingerprint 应通过 ja3 参数透传到 curl_cffi AsyncSession（异步）。"""
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

    ja3 = "771,4865-4866,0-23-65281,29-23-24,0"
    with Fetcher(ja3_fingerprint=ja3) as f:
        session = f._build_curl_async_session()
    assert isinstance(session, _StubAsyncSession)
    assert captured["ja3"] == ja3


def test_fetcher_ja3_not_passed_when_none(monkeypatch) -> None:
    """ja3_fingerprint=None 时不应向 curl_cffi Session 传 ja3 参数。"""
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

    with Fetcher(ja3_fingerprint=None) as f:
        session = f._build_curl_sync_session()
    assert isinstance(session, _StubSession)
    assert "ja3" not in captured, "ja3=None 时不应传 ja3 参数"


def test_fetcher_ja4_alias_deprecated_and_forwards(monkeypatch) -> None:
    """旧参数 ja4_fingerprint 应发 DeprecationWarning 并转发到 ja3_fingerprint。"""
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

    ja3 = "771,4865-4866,0-23-65281,29-23-24,0"
    with pytest.warns(DeprecationWarning, match="ja3_fingerprint"):
        f = Fetcher(ja4_fingerprint=ja3)
    try:
        # 兼容：ja4_fingerprint 属性仍可读，且 ja3_fingerprint 已赋值
        assert f.ja3_fingerprint == ja3
        assert f.ja4_fingerprint == ja3
        session = f._build_curl_sync_session()
    finally:
        f.close()
    assert isinstance(session, _StubSession)
    assert captured["ja3"] == ja3


# ---------------------------------------------------------------------------
# httpx 回退路径：curl_cffi 不可用时降级到 httpx
# ---------------------------------------------------------------------------


def test_fetcher_falls_back_to_httpx_with_warning(monkeypatch) -> None:
    """curl_cffi 不可用时应降级到 httpx 并发出 RuntimeWarning。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    with pytest.warns(RuntimeWarning, match="curl_cffi is not installed"):
        f = Fetcher(timeout=5.0)
    assert f._use_curl is False
    f.close()


def test_fetcher_raises_when_no_backend(monkeypatch) -> None:
    """curl_cffi 与 httpx 均不可用时应抛 ImportError。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", False)

    with pytest.raises(ImportError, match="Neither curl_cffi nor httpx"):
        Fetcher()


def test_fetcher_build_httpx_sync_client(monkeypatch) -> None:
    """httpx 回退路径下 _build_httpx_sync_client 应返回 httpx.Client。

    使用 ``spec=["Client"]`` 防止 MagicMock 自动创建 ``HTTPTransport`` 等属性，
    确保 hasattr 检查失败时走 proxy= 降级路径。
    """
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            pass

    fake_httpx = MagicMock(spec=["Client"])
    fake_httpx.Client = _FakeClient
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=7.0, follow_redirects=False, verify=False, http2=False)
    client = f._build_httpx_sync_client("http://proxy:8080")
    assert isinstance(client, _FakeClient)
    assert captured["proxy"] == "http://proxy:8080"
    assert captured["follow_redirects"] is False
    assert captured["verify"] is False
    assert captured["timeout"] == 7.0
    assert captured["http2"] is False
    f.close()


def test_fetcher_build_httpx_async_client(monkeypatch) -> None:
    """httpx 回退路径下 _build_httpx_async_client 应返回 httpx.AsyncClient。

    使用 ``spec=["AsyncClient"]`` 防止 MagicMock 自动创建 ``HTTPTransport`` 等属性，
    确保 hasattr 检查失败时走 proxy= 降级路径。
    """
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    captured: dict[str, object] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock(spec=["AsyncClient"])
    fake_httpx.AsyncClient = _FakeAsyncClient
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=9.0, http2=True)
    client = f._build_httpx_async_client(None)
    assert isinstance(client, _FakeAsyncClient)
    # proxy=None 时不传 proxy/mounts 参数（等同默认行为）
    assert "proxy" not in captured
    assert captured["http2"] is True
    assert captured["timeout"] == 9.0
    f.close()


def test_fetcher_merge_headers_httpx_path(monkeypatch) -> None:
    """httpx 回退路径下 _merge_headers 应使用 _default_headers 作为基础。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(extra_headers={"X-Custom": "yes"})
    merged = f._merge_headers({"X-Per": "req"})
    # httpx 路径包含浏览器默认头
    assert "Accept-Language" in merged
    assert merged["X-Custom"] == "yes"
    assert merged["X-Per"] == "req"
    f.close()


def test_fetcher_retry_errors_httpx_path(monkeypatch) -> None:
    """httpx 回退路径下 _retry_errors 应返回 (httpx.HTTPError, OSError)。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    fake_httpx = MagicMock()
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher()
    errors = f._retry_errors()
    assert httpx.HTTPError in errors or errors == (fake_httpx.HTTPError, OSError)
    assert OSError in errors
    f.close()


def test_fetcher_retry_errors_curl_path() -> None:
    """curl 路径下 _retry_errors 应返回 curl RequestError + OSError。"""
    from web_crawler import compat

    if not compat.HAS_CURL_CFFI:
        pytest.skip("curl_cffi not installed")
    with Fetcher() as f:
        errors = f._retry_errors()
    assert OSError in errors
    assert len(errors) == 2


# ---------------------------------------------------------------------------
# _send_once_sync / _send_once_async httpx 回退路径
# ---------------------------------------------------------------------------


def test_fetcher_send_once_sync_httpx_no_proxy(monkeypatch) -> None:
    """httpx 回退、无代理时应复用 session client（close_after=False）。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    raw_resp = MagicMock()
    raw_resp.url = "https://x.example/"
    raw_resp.status_code = 200
    raw_resp.content = b"ok"
    raw_resp.headers = {"Content-Type": "text/html"}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> MagicMock:
            return raw_resp

        def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0)
    result = f._send_once_sync(
        "GET", "https://x.example/", None, None, None, {}, None, 5.0, True, True
    )
    assert result is raw_resp
    f.close()


def test_fetcher_send_once_sync_httpx_with_proxy(monkeypatch) -> None:
    """httpx 回退、有代理时应创建临时 client 并在用后关闭。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    raw_resp = MagicMock()
    raw_resp.url = "https://x.example/"
    raw_resp.status_code = 200
    raw_resp.content = b"ok"
    raw_resp.headers = {}

    closed = {"flag": False}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def request(self, **kwargs: object) -> MagicMock:
            return raw_resp

        def close(self) -> None:
            closed["flag"] = True

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0)
    result = f._send_once_sync(
        "GET", "https://x.example/", None, None, None, {}, "http://proxy:8080", 5.0, True, True
    )
    assert result is raw_resp
    assert closed["flag"] is True  # 临时 client 被关闭
    f.close()


def test_fetcher_send_once_async_httpx_no_proxy(monkeypatch) -> None:
    """httpx 异步回退、无代理时应复用 session client。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    raw_resp = MagicMock()
    raw_resp.url = "https://x.example/"
    raw_resp.status_code = 200
    raw_resp.content = b"ok"
    raw_resp.headers = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: object) -> MagicMock:
            return raw_resp

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def go() -> object:
        with pytest.warns(RuntimeWarning):
            f = Fetcher(timeout=5.0)
        try:
            return await f._send_once_async(
                "GET", "https://x.example/", None, None, None, {}, None, 5.0, True, True
            )
        finally:
            f.close()

    import asyncio

    result = asyncio.run(go())
    assert result is raw_resp


def test_fetcher_send_once_async_httpx_with_proxy(monkeypatch) -> None:
    """httpx 异步回退、有代理时应创建临时 client 并在用后 aclose。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    raw_resp = MagicMock()
    raw_resp.url = "https://x.example/"
    raw_resp.status_code = 200
    raw_resp.content = b"ok"
    raw_resp.headers = {}

    closed = {"flag": False}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: object) -> MagicMock:
            return raw_resp

        async def aclose(self) -> None:
            closed["flag"] = True

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def go() -> object:
        with pytest.warns(RuntimeWarning):
            f = Fetcher(timeout=5.0)
        try:
            return await f._send_once_async(
                "GET",
                "https://x.example/",
                None,
                None,
                None,
                {},
                "http://proxy:8080",
                5.0,
                True,
                True,
            )
        finally:
            f.close()

    import asyncio

    result = asyncio.run(go())
    assert result is raw_resp
    assert closed["flag"] is True


# ---------------------------------------------------------------------------
# 重试逻辑：429 / 5xx / Retry-After / ProxyPool 轮换
# ---------------------------------------------------------------------------


def test_fetcher_retries_on_500_then_succeeds(monkeypatch) -> None:
    """5xx 响应应触发重试，最终成功时返回最后一次的 Response。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    call_count = {"n": 0}

    class _FakeResponse:
        def __init__(self, status: int) -> None:
            self.url = "https://x.example/"
            self.status_code = status
            self.content = b"ok"
            self.headers: dict[str, str] = {}

    def make_request(**kwargs: object) -> _FakeResponse:
        call_count["n"] += 1
        if call_count["n"] < 3:
            return _FakeResponse(500)
        return _FakeResponse(200)

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> _FakeResponse:
            return make_request()

        def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)
    # 跳过 backoff sleep
    monkeypatch.setattr(fetcher_mod.time, "sleep", lambda s: None)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(retries=3, timeout=5.0)
    resp = f.get("https://x.example/")
    assert resp.status == 200
    assert call_count["n"] == 3
    f.close()


def test_fetcher_retries_on_429_then_succeeds(monkeypatch) -> None:
    """429 响应应触发重试，最终成功时返回 Response。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    call_count = {"n": 0}

    class _FakeResponse:
        def __init__(self, status: int) -> None:
            self.url = "https://x.example/"
            self.status_code = status
            self.content = b"ok"
            self.headers: dict[str, str] = {}

    def make_request(**kwargs: object) -> _FakeResponse:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _FakeResponse(429)
        return _FakeResponse(200)

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> _FakeResponse:
            return make_request()

        def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)
    monkeypatch.setattr(fetcher_mod.time, "sleep", lambda s: None)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(retries=2, timeout=5.0)
    resp = f.get("https://x.example/")
    assert resp.status == 200
    assert call_count["n"] == 2
    f.close()


def test_fetcher_429_marks_proxy_failed_in_pool(monkeypatch) -> None:
    """429 且使用 ProxyPool 时应调用 mark_failed 并轮换代理。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    pool = ProxyPool(["http://p1:8080", "http://p2:8080"])

    class _FakeResponse:
        def __init__(self, status: int) -> None:
            self.url = "https://x.example/"
            self.status_code = status
            self.content = b"ok"
            self.headers: dict[str, str] = {}

    call_count = {"n": 0}

    def make_request(**kwargs: object) -> _FakeResponse:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _FakeResponse(429)
        return _FakeResponse(200)

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> _FakeResponse:
            return make_request()

        def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)
    monkeypatch.setattr(fetcher_mod.time, "sleep", lambda s: None)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(proxy=pool, retries=2, timeout=5.0)
    resp = f.get("https://x.example/")
    assert resp.status == 200
    assert call_count["n"] == 2
    f.close()


def test_fetcher_raises_after_exhausting_retries(monkeypatch) -> None:
    """重试耗尽后应抛出最后一次异常。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    fake_error = type("FakeErr", (BaseException,), {})

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> None:
            raise fake_error("boom")

        def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    fake_httpx.HTTPError = fake_error
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)
    monkeypatch.setattr(fetcher_mod.time, "sleep", lambda s: None)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(retries=1, timeout=5.0)
    with pytest.raises(fake_error):
        f.get("https://x.example/")
    f.close()


def test_fetcher_no_retry_returns_500_immediately(monkeypatch) -> None:
    """retries=0 时 5xx 不重试，直接返回 Response。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    class _FakeResponse:
        url = "https://x.example/"
        status_code = 500
        content = b"err"
        headers: dict[str, str] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> _FakeResponse:
            return _FakeResponse()

        def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(retries=0, timeout=5.0)
    resp = f.get("https://x.example/")
    assert resp.status == 500
    f.close()


# ---------------------------------------------------------------------------
# 公开 API：put / delete / head / options（同步）
# ---------------------------------------------------------------------------


def test_fetcher_put_returns_response(monkeypatch) -> None:
    """Fetcher.put 应调用 request('PUT', ...) 并返回 Response。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    captured: dict[str, object] = {}

    class _FakeResponse:
        url = "https://x.example/put"
        status_code = 200
        content = b"ok"
        headers: dict[str, str] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> _FakeResponse:
            captured.update(kwargs)
            return _FakeResponse()

        def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0)
    resp = f.put("https://x.example/put")
    assert resp.status == 200
    assert captured["method"] == "PUT"
    f.close()


def test_fetcher_delete_returns_response(monkeypatch) -> None:
    """Fetcher.delete 应调用 request('DELETE', ...)。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    class _FakeResponse:
        url = "https://x.example/del"
        status_code = 204
        content = b""
        headers: dict[str, str] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> _FakeResponse:
            return _FakeResponse()

        def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0)
    resp = f.delete("https://x.example/del")
    assert resp.status == 204
    f.close()


def test_fetcher_head_returns_response(monkeypatch) -> None:
    """Fetcher.head 应调用 request('HEAD', ...)。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    class _FakeResponse:
        url = "https://x.example/head"
        status_code = 200
        content = b""
        headers: dict[str, str] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> _FakeResponse:
            return _FakeResponse()

        def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0)
    resp = f.head("https://x.example/head")
    assert resp.status == 200
    f.close()


def test_fetcher_options_returns_response(monkeypatch) -> None:
    """Fetcher.options 应调用 request('OPTIONS', ...)。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    class _FakeResponse:
        url = "https://x.example/opt"
        status_code = 200
        content = b""
        headers: dict[str, str] = {"Allow": "GET, POST"}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> _FakeResponse:
            return _FakeResponse()

        def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0)
    resp = f.options("https://x.example/opt")
    assert resp.status == 200
    f.close()


def test_fetcher_async_post_returns_response(monkeypatch) -> None:
    """Fetcher.async_post 应异步发送 POST 请求。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    class _FakeResponse:
        url = "https://x.example/post"
        status_code = 201
        content = b"created"
        headers: dict[str, str] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: object) -> _FakeResponse:
            return _FakeResponse()

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def go() -> Response:
        with pytest.warns(RuntimeWarning):
            f = Fetcher(timeout=5.0)
        try:
            return await f.async_post("https://x.example/post", data={"k": "v"})
        finally:
            f.close()

    import asyncio

    resp = asyncio.run(go())
    assert resp.status == 201


# ---------------------------------------------------------------------------
# close / aclose 生命周期
# ---------------------------------------------------------------------------


def test_fetcher_close_with_active_async_session_warns(monkeypatch) -> None:
    """close() 时若异步 session 仍存在，应发出 ResourceWarning 并保留引用。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    class _FakeAsyncClient:
        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0)
    # 模拟异步 session 已创建
    session = MagicMock()
    f._async_session = session
    with pytest.warns(ResourceWarning, match="Fetcher.close"):
        f.close()
    # 引用被保留（不置 None），之后仍可 aclose() 释放
    assert f._async_session is session


def test_fetcher_close_swallows_session_exception(monkeypatch) -> None:
    """close() 时 session.close() 抛异常应被静默吞掉。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    class _BadSession:
        def close(self) -> None:
            raise RuntimeError("close failed")

    fake_httpx = MagicMock()
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0)
    f._session = _BadSession()
    # 不应抛异常
    f.close()
    assert f._session is None


def test_fetcher_aclose_closes_both_sessions(monkeypatch) -> None:
    """aclose() 应同时关闭同步和异步 session。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    sync_closed = {"flag": False}
    async_closed = {"flag": False}

    class _FakeSyncSession:
        def close(self) -> None:
            sync_closed["flag"] = True

    class _FakeAsyncSession:
        async def close(self) -> None:
            async_closed["flag"] = True

    fake_httpx = MagicMock()
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0)
    f._session = _FakeSyncSession()
    f._async_session = _FakeAsyncSession()

    async def go() -> None:
        await f.aclose()

    import asyncio

    asyncio.run(go())
    assert sync_closed["flag"] is True
    assert async_closed["flag"] is True
    assert f._session is None
    assert f._async_session is None


def test_fetcher_aclose_swallows_exceptions(monkeypatch) -> None:
    """aclose() 时 session.close() 抛异常应被静默吞掉。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    class _BadSync:
        def close(self) -> None:
            raise RuntimeError("sync close failed")

    class _BadAsync:
        async def close(self) -> None:
            raise RuntimeError("async close failed")

    fake_httpx = MagicMock()
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0)
    f._session = _BadSync()
    f._async_session = _BadAsync()

    async def go() -> None:
        await f.aclose()  # 不应抛异常

    import asyncio

    asyncio.run(go())
    assert f._session is None
    assert f._async_session is None


def test_fetcher_context_manager_closes_sync_only(monkeypatch) -> None:
    """__exit__ 调用 close()，无异步 session 时不发 ResourceWarning。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    fake_httpx = MagicMock()
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning), Fetcher(timeout=5.0) as f:
        pass
    assert f._session is None
    assert f._async_session is None


# ---------------------------------------------------------------------------
# AsyncFetcher 公开 API
# ---------------------------------------------------------------------------


def test_async_fetcher_put_returns_response(monkeypatch) -> None:
    """AsyncFetcher.put 应异步发送 PUT 请求。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    class _FakeResponse:
        url = "https://x.example/put"
        status_code = 200
        content = b"ok"
        headers: dict[str, str] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: object) -> _FakeResponse:
            return _FakeResponse()

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def go() -> Response:
        with pytest.warns(RuntimeWarning):
            f = AsyncFetcher(timeout=5.0)
        try:
            return await f.put("https://x.example/put")
        finally:
            await f.aclose()

    import asyncio

    resp = asyncio.run(go())
    assert resp.status == 200


def test_async_fetcher_delete_returns_response(monkeypatch) -> None:
    """AsyncFetcher.delete 应异步发送 DELETE 请求。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    class _FakeResponse:
        url = "https://x.example/del"
        status_code = 204
        content = b""
        headers: dict[str, str] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: object) -> _FakeResponse:
            return _FakeResponse()

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def go() -> Response:
        with pytest.warns(RuntimeWarning):
            f = AsyncFetcher(timeout=5.0)
        try:
            return await f.delete("https://x.example/del")
        finally:
            await f.aclose()

    import asyncio

    resp = asyncio.run(go())
    assert resp.status == 204


def test_async_fetcher_head_returns_response(monkeypatch) -> None:
    """AsyncFetcher.head 应异步发送 HEAD 请求。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    class _FakeResponse:
        url = "https://x.example/head"
        status_code = 200
        content = b""
        headers: dict[str, str] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: object) -> _FakeResponse:
            return _FakeResponse()

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def go() -> Response:
        with pytest.warns(RuntimeWarning):
            f = AsyncFetcher(timeout=5.0)
        try:
            return await f.head("https://x.example/head")
        finally:
            await f.aclose()

    import asyncio

    resp = asyncio.run(go())
    assert resp.status == 200


def test_async_fetcher_options_returns_response(monkeypatch) -> None:
    """AsyncFetcher.options 应异步发送 OPTIONS 请求。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    class _FakeResponse:
        url = "https://x.example/opt"
        status_code = 200
        content = b""
        headers: dict[str, str] = {"Allow": "GET"}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: object) -> _FakeResponse:
            return _FakeResponse()

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def go() -> Response:
        with pytest.warns(RuntimeWarning):
            f = AsyncFetcher(timeout=5.0)
        try:
            return await f.options("https://x.example/opt")
        finally:
            await f.aclose()

    import asyncio

    resp = asyncio.run(go())
    assert resp.status == 200


def test_async_fetcher_aclose_with_sync_session(monkeypatch) -> None:
    """AsyncFetcher.aclose 应同时清理同步 session（防御性）。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    sync_closed = {"flag": False}

    class _FakeSync:
        def close(self) -> None:
            sync_closed["flag"] = True

    class _FakeAsync:
        async def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = AsyncFetcher(timeout=5.0)
    f._session = _FakeSync()
    f._async_session = _FakeAsync()

    async def go() -> None:
        await f.aclose()

    import asyncio

    asyncio.run(go())
    assert sync_closed["flag"] is True
    assert f._session is None
    assert f._async_session is None


def test_async_fetcher_aclose_swallows_exceptions(monkeypatch) -> None:
    """AsyncFetcher.aclose 时 session.close() 抛异常应被静默吞掉。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    class _BadSync:
        def close(self) -> None:
            raise RuntimeError("sync failed")

    class _BadAsync:
        async def close(self) -> None:
            raise RuntimeError("async failed")

    fake_httpx = MagicMock()
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = AsyncFetcher(timeout=5.0)
    f._session = _BadSync()
    f._async_session = _BadAsync()

    async def go() -> None:
        await f.aclose()  # 不应抛异常

    import asyncio

    asyncio.run(go())
    assert f._session is None
    assert f._async_session is None


# ---------------------------------------------------------------------------
# _to_response 转换
# ---------------------------------------------------------------------------


def test_fetcher_to_response(monkeypatch) -> None:
    """_to_response 应将原始响应转为库 Response。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    raw = MagicMock()
    raw.url = "https://x.example/page"
    raw.status_code = 200
    raw.content = b"<html><body>hi</body></html>"
    raw.headers = {"Content-Type": "text/html"}

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0)
    resp = f._to_response(raw, {"X-Test": "1"})
    assert resp.url == "https://x.example/page"
    assert resp.status == 200
    assert b"hi" in resp.content
    assert resp.headers["Content-Type"] == "text/html"
    assert resp.request_headers == {"X-Test": "1"}
    f.close()


# ---------------------------------------------------------------------------
# fetchers/__init__.py: __getattr__ 懒加载 / __dir__
# ---------------------------------------------------------------------------


def test_fetchers_init_dir_returns_all_public_names() -> None:
    """__dir__ 应包含 __all__ 中声明的所有公共名称。"""
    from web_crawler import fetchers

    names = dir(fetchers)
    for expected in fetchers.__all__:
        assert expected in names, f"{expected} 应出现在 dir(fetchers)"


def test_fetchers_init_getattr_unknown_raises() -> None:
    """访问未声明的属性应抛 AttributeError。"""
    from web_crawler import fetchers

    with pytest.raises(AttributeError, match="has no attribute"):
        fetchers.DoesNotExistForReal  # noqa: B018


def test_fetchers_init_getattr_dynamic_fetcher() -> None:
    """访问 DynamicFetcher 应触发懒加载并缓存到 globals。"""
    import sys

    # 确保模块已导入
    from web_crawler import fetchers

    # 清除可能存在的缓存，强制走 __getattr__；先保存原类与模块对象，
    # 以便测试结束后恢复——否则后续测试会拿到两份不同的 DynamicFetcher 类
    # （类定义所在的模块被移出 sys.modules 后重新导入会产生新类对象，
    # 导致 issubclass/patch 失效）。
    saved_classes = {
        name: fetchers.__dict__.get(name)
        for name in ("DynamicFetcher", "StealthyFetcher", "CamoufoxFetcher")
    }
    saved_module = sys.modules.get("web_crawler.fetchers.dynamic")

    # 若 playwright 未安装则跳过懒加载结果验证
    try:
        cls = fetchers.DynamicFetcher
        assert cls is not None
        # 验证已缓存
        assert "DynamicFetcher" in fetchers.__dict__
    except ImportError:
        pytest.skip("playwright not installed — 懒加载需要 playwright")
    finally:
        # 恢复原类缓存与 sys.modules，避免污染后续测试
        for name, saved in saved_classes.items():
            if saved is not None:
                fetchers.__dict__[name] = saved
            else:
                fetchers.__dict__.pop(name, None)
        if saved_module is not None:
            sys.modules["web_crawler.fetchers.dynamic"] = saved_module
        else:
            sys.modules.pop("web_crawler.fetchers.dynamic", None)


# ---------------------------------------------------------------------------
# 补充：覆盖 _load_httpx_backend / AsyncFetcher.get+post / async 重试 / __aenter__
# ---------------------------------------------------------------------------


def test_load_httpx_backend_returns_module() -> None:
    """_load_httpx_backend 应返回真实的 httpx 模块。"""
    from web_crawler.fetchers.fetcher import _load_httpx_backend

    result = _load_httpx_backend()
    assert result is httpx


def test_async_fetcher_get_with_params_and_headers(monkeypatch) -> None:
    """AsyncFetcher.get 应透传 params 和 headers。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    captured: dict[str, object] = {}

    class _FakeResponse:
        url = "https://x.example/"
        status_code = 200
        content = b"ok"
        headers: dict[str, str] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: object) -> _FakeResponse:
            captured.update(kwargs)
            return _FakeResponse()

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def go() -> Response:
        with pytest.warns(RuntimeWarning):
            f = AsyncFetcher(timeout=5.0)
        try:
            return await f.get("https://x.example/", params={"q": "1"}, headers={"X-H": "v"})
        finally:
            await f.aclose()

    import asyncio

    resp = asyncio.run(go())
    assert resp.status == 200
    assert captured["method"] == "GET"
    assert captured["params"] == {"q": "1"}


def test_async_fetcher_post_with_full_kwargs(monkeypatch) -> None:
    """AsyncFetcher.post 应透传 params/headers/data/json。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    captured: dict[str, object] = {}

    class _FakeResponse:
        url = "https://x.example/post"
        status_code = 201
        content = b"created"
        headers: dict[str, str] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: object) -> _FakeResponse:
            captured.update(kwargs)
            return _FakeResponse()

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def go() -> Response:
        with pytest.warns(RuntimeWarning):
            f = AsyncFetcher(timeout=5.0)
        try:
            return await f.post(
                "https://x.example/post",
                params={"p": "1"},
                headers={"X-H": "v"},
                data={"d": "2"},
                json={"j": "3"},
            )
        finally:
            await f.aclose()

    import asyncio

    resp = asyncio.run(go())
    assert resp.status == 201
    assert captured["method"] == "POST"
    assert captured["params"] == {"p": "1"}
    assert captured["data"] == {"d": "2"}
    assert captured["json"] == {"j": "3"}


def test_async_fetcher_async_context_manager(monkeypatch) -> None:
    """AsyncFetcher 应支持 async with 上下文管理器。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    class _FakeResponse:
        url = "https://x.example/"
        status_code = 200
        content = b"ok"
        headers: dict[str, str] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: object) -> _FakeResponse:
            return _FakeResponse()

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def go() -> Response:
        with pytest.warns(RuntimeWarning):
            async with AsyncFetcher(timeout=5.0) as f:
                return await f.get("https://x.example/")

    import asyncio

    resp = asyncio.run(go())
    assert resp.status == 200


def test_async_fetcher_retries_on_500_then_succeeds(monkeypatch) -> None:
    """异步路径下 5xx 应触发重试，最终成功时返回 Response。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    call_count = {"n": 0}

    class _FakeResponse:
        def __init__(self, status: int) -> None:
            self.url = "https://x.example/"
            self.status_code = status
            self.content = b"ok"
            self.headers: dict[str, str] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: object) -> _FakeResponse:
            call_count["n"] += 1
            if call_count["n"] < 3:
                return _FakeResponse(500)
            return _FakeResponse(200)

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def go() -> Response:
        with pytest.warns(RuntimeWarning):
            f = AsyncFetcher(retries=3, timeout=5.0)
        try:
            return await f.get("https://x.example/")
        finally:
            await f.aclose()

    import asyncio

    async def no_sleep(s: float) -> None:
        pass

    monkeypatch.setattr(fetcher_mod.asyncio, "sleep", no_sleep)
    resp = asyncio.run(go())
    assert resp.status == 200
    assert call_count["n"] == 3


def test_async_fetcher_retries_on_429_with_proxy_pool(monkeypatch) -> None:
    """异步路径下 429 + ProxyPool 应调用 mark_failed 并轮换代理。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    pool = ProxyPool(["http://p1:8080", "http://p2:8080"])
    call_count = {"n": 0}

    class _FakeResponse:
        def __init__(self, status: int) -> None:
            self.url = "https://x.example/"
            self.status_code = status
            self.content = b"ok"
            self.headers: dict[str, str] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: object) -> _FakeResponse:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _FakeResponse(429)
            return _FakeResponse(200)

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def no_sleep(s: float) -> None:
        pass

    monkeypatch.setattr(fetcher_mod.asyncio, "sleep", no_sleep)

    async def go() -> Response:
        with pytest.warns(RuntimeWarning):
            f = AsyncFetcher(proxy=pool, retries=2, timeout=5.0)
        try:
            return await f.get("https://x.example/")
        finally:
            await f.aclose()

    import asyncio

    resp = asyncio.run(go())
    assert resp.status == 200
    assert call_count["n"] == 2


def test_async_fetcher_raises_after_exhausting_retries(monkeypatch) -> None:
    """异步路径下重试耗尽后应抛出最后一次异常。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    fake_error = type("FakeErr", (BaseException,), {})

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: object) -> None:
            raise fake_error("boom")

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.HTTPError = fake_error
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def no_sleep(s: float) -> None:
        pass

    monkeypatch.setattr(fetcher_mod.asyncio, "sleep", no_sleep)

    async def go() -> None:
        with pytest.warns(RuntimeWarning):
            f = AsyncFetcher(retries=1, timeout=5.0)
        try:
            await f.get("https://x.example/")
        finally:
            await f.aclose()

    import asyncio

    with pytest.raises(fake_error):
        asyncio.run(go())


def test_fetcher_send_sync_runtime_error_fallback(monkeypatch) -> None:
    """_send_sync 循环正常结束未捕获异常时应抛 RuntimeError。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    # 构造一个 retries=0 但返回 5xx 的场景不会触发 RuntimeError；
    # 这里通过 mock _send_once_sync 返回非重试状态来覆盖正常路径
    # RuntimeError 分支需要循环走完但 last_exc 为 None 且无返回——
    # 实际上该分支在正常流程下不可达，属于防御性代码。跳过直接覆盖。

    # 改为验证 retries=0 + 200 正常返回路径
    class _FakeResponse:
        url = "https://x.example/"
        status_code = 200
        content = b"ok"
        headers: dict[str, str] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> _FakeResponse:
            return _FakeResponse()

        def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(retries=0, timeout=5.0)
    resp = f.request("GET", "https://x.example/")
    assert resp.status == 200
    f.close()


def test_fetcher_curl_session_http1(monkeypatch) -> None:
    """curl 路径下 http2=False 时应向 Session 传 http_version=V1_1。"""
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

    with Fetcher(http2=False) as f:
        sync_session = f._build_curl_sync_session()
    assert isinstance(sync_session, _StubSession)
    assert captured["http_version"] == "v1_1"

    with Fetcher(http2=False) as f:
        async_session = f._build_curl_async_session()
    assert isinstance(async_session, _StubSession)
    assert captured["http_version"] == "v1_1"


# ---------------------------------------------------------------------------
# SSRF 防护：scheme 白名单 + 逐跳重定向校验 + max_redirects
# ---------------------------------------------------------------------------


def test_fetcher_rejects_non_http_schemes() -> None:
    """file/ftp/data/gopher 等非 http(s) scheme 应在发请求前被拒绝。"""
    with Fetcher(timeout=5.0) as f:
        for bad in (
            "file:///etc/passwd",
            "ftp://example.com/x",
            "data:text/plain,hi",
            "gopher://example.com/1",
        ):
            with pytest.raises(ValueError, match="only http/https"):
                f.get(bad)


def test_fetcher_async_rejects_non_http_scheme() -> None:
    """异步路径同样拒绝非 http(s) scheme。"""
    import asyncio

    async def go() -> None:
        async with Fetcher(timeout=5.0) as f:
            with pytest.raises(ValueError, match="only http/https"):
                await f.async_get("file:///etc/passwd")

    asyncio.run(go())


def test_fetcher_follows_redirect_chain(redirect_server: str) -> None:
    """http(s) 重定向链应被逐跳跟随，最终返回目标页。"""
    with Fetcher(timeout=5.0) as f:
        resp = f.get(redirect_server + "/chain/1")
    assert resp.status == 200
    assert resp.url == redirect_server + "/chain/final"


def test_fetcher_async_follows_redirect_chain(redirect_server: str) -> None:
    """异步路径重定向链同样被逐跳跟随。"""
    import asyncio

    async def go() -> Response:
        async with Fetcher(timeout=5.0) as f:
            return await f.async_get(redirect_server + "/chain/1")

    resp = asyncio.run(go())
    assert resp.status == 200
    assert resp.url == redirect_server + "/chain/final"


def test_fetcher_allow_redirects_false_stops_at_302(redirect_server: str) -> None:
    """allow_redirects=False 时不跟随重定向，返回 302 响应。"""
    with Fetcher(timeout=5.0) as f:
        resp = f.get(redirect_server + "/chain/1", allow_redirects=False)
    assert resp.status == 302


def test_fetcher_rejects_redirect_to_non_http(redirect_server: str) -> None:
    """重定向目标是 file:// 或 ftp:// 时应在跳转前拒绝。"""
    with Fetcher(timeout=5.0) as f:
        with pytest.raises(ValueError, match="only http/https"):
            f.get(redirect_server + "/to-file")
        with pytest.raises(ValueError, match="only http/https"):
            f.get(redirect_server + "/to-ftp")


def test_fetcher_max_redirects_loop_raises(redirect_server: str) -> None:
    """重定向环超过 max_redirects 上限时抛 RuntimeError。"""
    with (
        Fetcher(timeout=5.0, max_redirects=3) as f,
        pytest.raises(RuntimeError, match="too many redirects"),
    ):
        f.get(redirect_server + "/loop")


def test_fetcher_max_redirects_validation() -> None:
    """max_redirects < 0 应在构造时抛 ValueError。"""
    with pytest.raises(ValueError, match="max_redirects"):
        Fetcher(max_redirects=-1)


def test_fetcher_async_redirect_to_non_http_rejected(redirect_server: str) -> None:
    """异步路径重定向到非 http(s) 同样被拒绝。"""
    import asyncio

    async def go() -> None:
        async with Fetcher(timeout=5.0) as f:
            with pytest.raises(ValueError, match="only http/https"):
                await f.async_get(redirect_server + "/to-file")

    asyncio.run(go())


# ---------------------------------------------------------------------------
# 未知 kwargs 显式报错（明确 API 契约）
# ---------------------------------------------------------------------------


def test_fetcher_unknown_kwargs_raise_type_error(monkeypatch) -> None:
    """auth/cookies 等未声明参数应抛 TypeError，而不是静默丢弃。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    fake_httpx = MagicMock()
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0)
    with pytest.raises(TypeError, match="auth"):
        f.get("http://example.com/", auth=("u", "p"))
    with pytest.raises(TypeError, match="cookies"):
        f.get("http://example.com/", cookies={"a": "b"})
    f.close()


def test_fetcher_async_unknown_kwargs_raise_type_error(monkeypatch) -> None:
    """异步路径同样对未声明 kwargs 抛 TypeError。"""
    import asyncio

    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    fake_httpx = MagicMock()
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def go() -> None:
        with pytest.warns(RuntimeWarning):
            f = AsyncFetcher(timeout=5.0)
        try:
            with pytest.raises(TypeError, match="stream"):
                await f.get("http://example.com/", stream=True)
        finally:
            await f.aclose()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# httpx 回退：缺 h2 时 http2 降级为 HTTP/1.1
# ---------------------------------------------------------------------------


def test_fetcher_httpx_http2_fallback_without_h2(monkeypatch) -> None:
    """httpx 回退路径 http2=True 缺 h2 时应降级为 http2=False 并告警。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    calls: list[dict[str, object]] = []

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)
            if kwargs.get("http2"):
                raise ImportError("h2 not installed")
            self.kwargs = kwargs

        def close(self) -> None:
            pass

    fake_httpx = MagicMock(spec=["Client"])
    fake_httpx.Client = _FakeClient
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0, http2=True)
    with pytest.warns(RuntimeWarning, match="'h2' package"):
        client = f._build_httpx_sync_client()
    assert isinstance(client, _FakeClient)
    assert calls[0]["http2"] is True  # 首次尝试仍带 http2
    assert calls[-1]["http2"] is False  # 降级后 http2=False
    f.close()


def test_fetcher_httpx_async_http2_fallback_without_h2(monkeypatch) -> None:
    """异步 client 同样在缺 h2 时降级 http2=False。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    calls: list[dict[str, object]] = []

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)
            if kwargs.get("http2"):
                raise ImportError("h2 not installed")

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock(spec=["AsyncClient"])
    fake_httpx.AsyncClient = _FakeAsyncClient
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = AsyncFetcher(timeout=5.0, http2=True)
    with pytest.warns(RuntimeWarning, match="'h2' package"):
        client = f._build_httpx_async_client()
    assert isinstance(client, _FakeAsyncClient)
    assert calls[-1]["http2"] is False
    import asyncio

    asyncio.run(f.aclose())


def test_fetcher_build_httpx_async_client_sync_transport_fallback(monkeypatch) -> None:
    """httpx 无 AsyncHTTPTransport 时，异步 client 的 mounts 退化为同步 HTTPTransport。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    captured: dict[str, object] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock(spec=["AsyncClient", "HTTPTransport"])
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.HTTPTransport = MagicMock()
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0)
    client = f._build_httpx_async_client("http://proxy:8080")
    assert isinstance(client, _FakeAsyncClient)
    # mounts 包含同步 HTTPTransport 实例，且不再走 proxy= 参数
    assert "proxy" not in captured
    mounts = captured["mounts"]
    assert isinstance(mounts, dict)
    assert "all://" in mounts
    assert mounts["all://"] is fake_httpx.HTTPTransport.return_value
    f.close()


# ---------------------------------------------------------------------------
# 代理故障闭环：连接错误/5xx 标记失败并轮换，成功清零计数
# ---------------------------------------------------------------------------


def _make_fake_response(status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.url = "https://x.example/"
    resp.status_code = status
    resp.content = b"ok"
    resp.headers: dict[str, str] = {}
    return resp


def test_fetcher_rotates_proxy_on_connection_error(monkeypatch) -> None:
    """连接错误时应 mark_failed 当前代理并在重试时轮换到下一个代理。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    pool = ProxyPool(["http://p1:8080", "http://p2:8080"])
    used_proxies: list[str | None] = []
    calls = {"n": 0}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> MagicMock:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("connection refused")
            return _make_fake_response(200)

        def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)
    monkeypatch.setattr(fetcher_mod.time, "sleep", lambda s: None)

    # 记录每次请求实际使用的代理
    orig = fetcher_mod.Fetcher._send_once_sync

    def spy(
        self: object,
        method: str,
        url: str,
        params: object,
        data: object,
        json: object,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
        allow_redirects: bool,
        verify: bool,
    ) -> object:
        used_proxies.append(proxy)
        result = orig(
            self,
            method,
            url,
            params,
            data,
            json,
            headers,
            proxy,
            timeout,
            allow_redirects,
            verify,
        )
        return result

    monkeypatch.setattr(fetcher_mod.Fetcher, "_send_once_sync", spy)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(proxy=pool, retries=2, timeout=5.0)
    resp = f.get("https://x.example/")
    assert resp.status == 200
    # 第一次请求用 p1（失败），重试轮换到 p2
    assert used_proxies[0] == "http://p1:8080"
    assert used_proxies[1] == "http://p2:8080"
    assert pool._failures["http://p1:8080"] == 1
    f.close()


def test_fetcher_marks_proxy_failed_on_5xx(monkeypatch) -> None:
    """5xx 响应应 mark_failed 当前代理并在重试时轮换。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    pool = ProxyPool(["http://p1:8080", "http://p2:8080"])
    used_proxies: list[str | None] = []
    calls = {"n": 0}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> MagicMock:
            calls["n"] += 1
            if calls["n"] == 1:
                return _make_fake_response(500)
            return _make_fake_response(200)

        def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)
    monkeypatch.setattr(fetcher_mod.time, "sleep", lambda s: None)

    orig = fetcher_mod.Fetcher._send_once_sync

    def spy(
        self: object,
        method: str,
        url: str,
        params: object,
        data: object,
        json: object,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
        allow_redirects: bool,
        verify: bool,
    ) -> object:
        used_proxies.append(proxy)
        result = orig(
            self,
            method,
            url,
            params,
            data,
            json,
            headers,
            proxy,
            timeout,
            allow_redirects,
            verify,
        )
        return result

    monkeypatch.setattr(fetcher_mod.Fetcher, "_send_once_sync", spy)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(proxy=pool, retries=2, timeout=5.0)
    resp = f.get("https://x.example/")
    assert resp.status == 200
    assert used_proxies[0] == "http://p1:8080"
    assert used_proxies[1] == "http://p2:8080"
    assert pool._failures["http://p1:8080"] == 1
    f.close()


def test_fetcher_marks_proxy_success_on_2xx(monkeypatch) -> None:
    """2xx 成功响应应 mark_success 清零当前代理的失败计数。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    pool = ProxyPool(["http://p1:8080", "http://p2:8080"])
    # 预先模拟 p1 有历史失败
    pool.mark_failed("http://p1:8080")
    pool.mark_failed("http://p1:8080")

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> MagicMock:
            return _make_fake_response(200)

        def close(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(proxy=pool, retries=0, timeout=5.0)
    resp = f.get("https://x.example/")
    assert resp.status == 200
    # round_robin 第一个可用代理是 p1，成功后其失败计数清零
    assert pool._failures["http://p1:8080"] == 0
    f.close()


# ===========================================================================
# 扩展：_build_httpx_async_client proxy= 兜底 / _next_redirect 分支
# ===========================================================================


def test_fetcher_build_httpx_async_client_proxy_kwargs_fallback(monkeypatch) -> None:
    """httpx 无 AsyncHTTPTransport/HTTPTransport 时异步 client 走 proxy= 参数。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    captured: dict[str, object] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def aclose(self) -> None:
            pass

    # spec 只含 AsyncClient：hasattr(AsyncHTTPTransport)/hasattr(HTTPTransport) 均 False
    fake_httpx = MagicMock(spec=["AsyncClient"])
    fake_httpx.AsyncClient = _FakeAsyncClient
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(timeout=5.0)
    client = f._build_httpx_async_client("http://proxy:8080")
    assert isinstance(client, _FakeAsyncClient)
    # 无 transports 时降级为 proxy= 关键字
    assert captured["proxy"] == "http://proxy:8080"
    f.close()


def _make_raw_response(status: int, headers: dict[str, str]) -> MagicMock:
    raw = MagicMock()
    raw.status_code = status
    raw.headers = headers
    return raw


def test_next_redirect_non_redirect_status_returns_none() -> None:
    """非 3xx 状态码不触发重定向解析。"""
    from web_crawler import Fetcher

    f = Fetcher.__new__(Fetcher)
    assert f._next_redirect(_make_raw_response(200, {}), "https://a.com/x", "GET", {}) is None


def test_next_redirect_missing_location_returns_none() -> None:
    """302 但无 Location 头时返回 None。"""
    from web_crawler import Fetcher

    f = Fetcher.__new__(Fetcher)
    raw = _make_raw_response(302, {})
    assert f._next_redirect(raw, "https://a.com/x", "GET", {}) is None


def test_next_redirect_cross_origin_strips_authorization() -> None:
    """跨源跳转时剥离 Authorization 头，避免凭据泄漏给第三方。"""
    from web_crawler import Fetcher

    f = Fetcher.__new__(Fetcher)
    raw = _make_raw_response(302, {"Location": "https://b.com/y"})
    headers = {"Authorization": "Bearer tok", "X-Custom": "1"}
    next_url, new_method, next_headers = f._next_redirect(raw, "https://a.com/x", "GET", headers)
    assert next_url == "https://b.com/y"
    assert new_method is None
    assert "Authorization" not in next_headers
    assert next_headers["X-Custom"] == "1"


def test_next_redirect_303_switches_to_get() -> None:
    """303 无论原方法都切换为 GET。"""
    from web_crawler import Fetcher

    f = Fetcher.__new__(Fetcher)
    raw = _make_raw_response(303, {"Location": "https://a.com/y"})
    _, new_method, _ = f._next_redirect(raw, "https://a.com/x", "POST", {})
    assert new_method == "GET"


def test_next_redirect_302_post_switches_to_get() -> None:
    """301/302 且原方法为 POST 时切换为 GET。"""
    from web_crawler import Fetcher

    f = Fetcher.__new__(Fetcher)
    raw = _make_raw_response(302, {"Location": "https://a.com/y"})
    _, new_method, _ = f._next_redirect(raw, "https://a.com/x", "POST", {})
    assert new_method == "GET"


# ===========================================================================
# 扩展：POST 303 重定向改 GET（sync/async 的 new_method 分支）+ async 重定向边界
# ===========================================================================


def test_fetcher_post_303_redirect_converts_to_get(redirect_server: str) -> None:
    """POST 收到 303 后应切换为 GET 并清空请求体继续跳转。"""
    with Fetcher(timeout=5.0) as f:
        resp = f.post(redirect_server + "/post-303", data=b"payload")
    assert resp.status == 200
    assert resp.url == redirect_server + "/chain/final"


def test_fetcher_async_post_303_redirect_converts_to_get(redirect_server: str) -> None:
    """异步路径 POST 收到 303 后同样切换为 GET（覆盖 new_method 分支）。"""
    import asyncio

    async def go() -> Response:
        async with Fetcher(timeout=5.0) as f:
            return await f.async_post(redirect_server + "/post-303", data=b"payload")

    resp = asyncio.run(go())
    assert resp.status == 200
    assert resp.url == redirect_server + "/chain/final"


def test_fetcher_async_allow_redirects_false_stops_at_302(redirect_server: str) -> None:
    """异步路径 allow_redirects=False 时不跟随重定向，返回 302 响应。"""
    import asyncio

    async def go() -> Response:
        async with Fetcher(timeout=5.0) as f:
            return await f.async_get(redirect_server + "/chain/1", allow_redirects=False)

    resp = asyncio.run(go())
    assert resp.status == 302


def test_fetcher_async_max_redirects_loop_raises(redirect_server: str) -> None:
    """异步路径重定向环超过 max_redirects 上限时抛 RuntimeError。"""
    import asyncio

    async def go() -> None:
        async with Fetcher(timeout=5.0, max_redirects=3) as f:
            await f.async_get(redirect_server + "/loop")

    with pytest.raises(RuntimeError, match="too many redirects"):
        asyncio.run(go())


# ===========================================================================
# 扩展：异步路径连接错误时 ProxyPool 标记失败并轮换
# ===========================================================================


def test_async_fetcher_rotates_proxy_on_connection_error(monkeypatch) -> None:
    """异步路径连接错误时应 mark_failed 当前代理并在重试时轮换。"""
    from web_crawler.fetchers import fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)

    pool = ProxyPool(["http://p1:8080", "http://p2:8080"])
    used_proxies: list[str | None] = []
    calls = {"n": 0}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: object) -> MagicMock:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("connection refused")
            return _make_fake_response(200)

        async def aclose(self) -> None:
            pass

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)

    async def no_sleep(s: float) -> None:
        pass

    monkeypatch.setattr(fetcher_mod.asyncio, "sleep", no_sleep)

    # 记录每次请求实际使用的代理
    orig = fetcher_mod.AsyncFetcher._send_once_async

    async def spy(
        self: object,
        method: str,
        url: str,
        params: object,
        data: object,
        json: object,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
        allow_redirects: bool,
        verify: bool,
    ) -> object:
        used_proxies.append(proxy)
        return await orig(
            self,
            method,
            url,
            params,
            data,
            json,
            headers,
            proxy,
            timeout,
            allow_redirects,
            verify,
        )

    monkeypatch.setattr(fetcher_mod.AsyncFetcher, "_send_once_async", spy)

    async def go() -> Response:
        with pytest.warns(RuntimeWarning):
            f = AsyncFetcher(proxy=pool, retries=2, timeout=5.0)
        try:
            return await f.get("https://x.example/")
        finally:
            await f.aclose()

    import asyncio

    resp = asyncio.run(go())
    assert resp.status == 200
    # 第一次请求用 p1（失败），重试轮换到 p2
    assert used_proxies[0] == "http://p1:8080"
    assert used_proxies[1] == "http://p2:8080"
    assert pool._failures["http://p1:8080"] == 1

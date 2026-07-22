"""Stealth HTTP fetcher backed by ``curl_cffi`` TLS-fingerprint impersonation.

Aligns with Scrapling's ``Fetcher`` / ``AsyncFetcher``: the primary workhorse
for invisible HTTP fetching. ``curl_cffi`` replays a real browser's TLS/JA3
fingerprint and HTTP/2 frame ordering, so requests look indistinguishable from
Chrome at the network layer. When ``curl_cffi`` is unavailable the fetcher
transparently degrades to ``httpx`` (with a warning) so the same API keeps
working, albeit without fingerprint stealth.

Backend imports (``curl_cffi`` / ``httpx``) are deferred to ``__init__`` time
rather than module import time, so merely importing the module does not force
the optional dependencies to load.

All methods return the library-wide :class:`~web_crawler.response.Response`.
"""

from __future__ import annotations

import asyncio
import random
import time
import warnings
from typing import TYPE_CHECKING, Any

from ..compat import HAS_CURL_CFFI, HAS_HTTPX
from ._base import BaseFetcher
from .proxy import ProxyPool

if TYPE_CHECKING:
    from ..parser.adaptive import AdaptiveStorage
    from ..response import Response


def _load_curl_backend() -> tuple[Any, Any, Any, Any]:
    """Import curl_cffi symbols lazily (only when curl backend is selected)."""
    from curl_cffi import CurlHttpVersion
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    from curl_cffi.requests import Session as CurlSession
    from curl_cffi.requests.exceptions import RequestException as CurlRequestError

    return CurlHttpVersion, CurlSession, CurlAsyncSession, CurlRequestError


def _load_httpx_backend() -> Any:
    """Import httpx lazily (only when the fallback path is taken)."""
    import httpx

    return httpx


def _parse_retry_after(value: str | None) -> float | None:
    """解析 HTTP ``Retry-After`` 响应头，返回等待秒数。

    支持两种形式（RFC 7231）：
    - 整数秒：``Retry-After: 120``
    - HTTP 日期：``Retry-After: Wed, 21 Oct 2026 07:28:00 GMT``
    无法解析时返回 ``None``。
    """
    if not value:
        return None
    value = value.strip()
    # 尝试整数秒
    try:
        seconds = float(value)
        if seconds >= 0:
            return min(seconds, 300.0)  # 上限 5 分钟，避免过长阻塞
    except ValueError:
        pass
    # 尝试 HTTP 日期
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(value)
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        delta = (dt - now).total_seconds()
        return max(0.0, min(delta, 300.0))
    except (TypeError, ValueError, OverflowError):
        return None


class _FetcherCore(BaseFetcher):
    """Shared session/retry/header logic for :class:`Fetcher` and :class:`AsyncFetcher`.

    Both the sync and async fetchers share the same configuration, header
    merging, retry loop, and curl-vs-httpx backend selection. This base holds
    that shared state and the helper methods; the two concrete subclasses
    expose only their respective sync/async public APIs.
    """

    def __init__(
        self,
        *,
        impersonate: str = "chrome131",
        timeout: float = 30.0,
        proxy: str | ProxyPool | None = None,
        retries: int = 0,
        adaptive: bool = False,
        storage: AdaptiveStorage | None = None,
        extra_headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
        verify: bool = True,
        http2: bool = True,
    ) -> None:
        super().__init__(
            timeout=timeout,
            proxy=proxy,
            retries=retries,
            adaptive=adaptive,
            storage=storage,
            extra_headers=extra_headers,
            follow_redirects=follow_redirects,
            verify=verify,
        )
        self.impersonate = impersonate
        self.http2 = http2

        # Backend is selected once at construction; sessions are built lazily.
        self._use_curl: bool = HAS_CURL_CFFI
        if not self._use_curl and not HAS_HTTPX:  # pragma: no cover - defensive
            raise ImportError(
                "Neither curl_cffi nor httpx is installed; Fetcher cannot operate. "
                "Install one with: pip install curl_cffi"
            )
        if not self._use_curl:
            warnings.warn(
                "curl_cffi is not installed; falling back to httpx. "
                "TLS-fingerprint impersonation is disabled — requests may be "
                "more easily detected. Install curl_cffi for full stealth.",
                RuntimeWarning,
                stacklevel=2,
            )

        # Lazily-created sessions (sync / async). Each is None until first use.
        self._session: Any = None
        self._async_session: Any = None

    # -- session construction (deferred backend imports) -------------------
    def _build_curl_sync_session(self) -> Any:
        CurlHttpVersion, CurlSession, _, _ = _load_curl_backend()
        kwargs: dict[str, Any] = {
            "impersonate": self.impersonate,
            "verify": self.verify,
            "timeout": self.timeout,
            "allow_redirects": self.follow_redirects,
        }
        if not self.http2:
            kwargs["http_version"] = CurlHttpVersion.V1_1
        return CurlSession(**kwargs)

    def _build_curl_async_session(self) -> Any:
        CurlHttpVersion, _, CurlAsyncSession, _ = _load_curl_backend()
        kwargs: dict[str, Any] = {
            "impersonate": self.impersonate,
            "verify": self.verify,
            "timeout": self.timeout,
            "allow_redirects": self.follow_redirects,
        }
        if not self.http2:
            kwargs["http_version"] = CurlHttpVersion.V1_1
        return CurlAsyncSession(**kwargs)

    def _build_httpx_sync_client(self, proxy: str | None = None) -> Any:
        httpx = _load_httpx_backend()
        return httpx.Client(
            http2=self.http2,
            proxy=proxy,
            follow_redirects=self.follow_redirects,
            verify=self.verify,
            timeout=self.timeout,
        )

    def _build_httpx_async_client(self, proxy: str | None = None) -> Any:
        httpx = _load_httpx_backend()
        return httpx.AsyncClient(
            http2=self.http2,
            proxy=proxy,
            follow_redirects=self.follow_redirects,
            verify=self.verify,
            timeout=self.timeout,
        )

    def _ensure_sync_session(self) -> Any:
        if self._session is None:
            self._session = (
                self._build_curl_sync_session()
                if self._use_curl
                else self._build_httpx_sync_client()
            )
        return self._session

    def _ensure_async_session(self) -> Any:
        if self._async_session is None:
            self._async_session = (
                self._build_curl_async_session()
                if self._use_curl
                else self._build_httpx_async_client()
            )
        return self._async_session

    # -- header merging -----------------------------------------------------
    def _merge_headers(self, per_request: dict[str, str] | None) -> dict[str, str]:
        """Merge headers without clobbering the curl_cffi impersonation fingerprint.

        curl_cffi's ``impersonate`` already injects a full browser header set
        matching the chosen TLS fingerprint, so for the curl path we only layer
        the user's explicit headers on top. For the httpx fallback there is no
        fingerprint, so the realistic default header set is used as the base.
        """
        if self._use_curl:
            merged: dict[str, str] = dict(self.extra_headers)
        else:
            merged = self._default_headers()
            merged.update(self.extra_headers)
        if per_request:
            merged.update(per_request)
        return merged

    def _retry_errors(self) -> tuple[type[BaseException], ...]:
        if self._use_curl:
            _, _, _, CurlRequestError = _load_curl_backend()
            return (CurlRequestError, OSError)
        if HAS_HTTPX:
            httpx = _load_httpx_backend()
            return (httpx.HTTPError, OSError)
        return ()  # pragma: no cover - defensive

    # -- response conversion -------------------------------------------------
    def _to_response(self, raw: Any, request_headers: dict[str, str]) -> Response:
        # 运行时导入 Response 以避免模块加载期的循环导入
        from ..response import Response

        return Response(
            url=str(raw.url),
            status=int(raw.status_code),
            content=raw.content,
            headers=dict(raw.headers),
            request_headers=request_headers,
            storage=self.storage,
            adaptive=self.adaptive,
        )

    # -- shared async transport (used by both Fetcher and AsyncFetcher) -----
    async def _send_once_async(
        self,
        method: str,
        url: str,
        params: Any,
        data: Any,
        json: Any,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
        allow_redirects: bool,
        verify: bool,
    ) -> Any:
        if self._use_curl:
            session = self._ensure_async_session()
            return await session.request(
                method=method,
                url=url,
                params=params,
                data=data,
                json=json,
                headers=headers,
                proxy=proxy,
                timeout=timeout,
                allow_redirects=allow_redirects,
                verify=verify,
            )
        # httpx async fallback
        if proxy is None:
            client = self._ensure_async_session()
            close_after = False
        else:
            client = self._build_httpx_async_client(proxy)
            close_after = True
        try:
            return await client.request(
                method=method,
                url=url,
                params=params,
                data=data,
                json=json,
                headers=headers,
                timeout=timeout,
                follow_redirects=allow_redirects,
            )
        finally:
            if close_after:
                await client.aclose()

    async def _send_async(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json: Any = None,
        **kwargs: Any,
    ) -> Response:
        merged_headers = self._merge_headers(headers)
        timeout = kwargs.pop("timeout", self.timeout)
        verify = kwargs.pop("verify", self.verify)
        allow_redirects = kwargs.pop("allow_redirects", self.follow_redirects)
        proxy = self._resolve_proxy()
        retry_errors = self._retry_errors()
        last_exc: BaseException | None = None
        for attempt in range(self.retries + 1):
            backoff = min(2.0**attempt, 10.0) + random.random() * 0.25
            try:
                raw = await self._send_once_async(
                    method,
                    url,
                    params,
                    data,
                    json,
                    merged_headers,
                    proxy,
                    timeout,
                    allow_redirects,
                    verify,
                )
            except retry_errors as exc:
                last_exc = exc
                if attempt == self.retries:
                    raise
                await asyncio.sleep(backoff)
                continue
            # 5xx 与 429（被限流）均重试；其余直接返回
            should_retry = raw.status_code >= 500 or raw.status_code == 429
            if not should_retry or attempt == self.retries:
                return self._to_response(raw, merged_headers)
            # 429 视为封锁信号：若有代理池则标记当前代理失败并换下一个
            if raw.status_code == 429 and isinstance(self.proxy, ProxyPool) and proxy:
                self.proxy.mark_failed(proxy)
                proxy = self._resolve_proxy()
            # 429 时尊重 Retry-After；否则指数退避
            delay = _parse_retry_after(raw.headers.get("Retry-After")) or backoff
            await asyncio.sleep(delay)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"request to {url} failed without a captured exception")


class Fetcher(_FetcherCore):
    """Stealth HTTP fetcher using ``curl_cffi`` TLS impersonation (synchronous).

    When ``curl_cffi`` is installed (the default expectation) a
    :class:`curl_cffi.requests.Session` is held and impersonated as a real
    browser. If ``curl_cffi`` is missing the fetcher falls back to ``httpx``
    and emits a warning so callers know fingerprint stealth is disabled.

    This class also exposes async methods (``async_get`` / ``async_request``)
    so a single instance can serve both sync and async callers. For a pure
    async-only API surface, use :class:`AsyncFetcher`.

    Parameters
    ----------
    impersonate:
        ``curl_cffi`` browser fingerprint to impersonate (default ``"chrome131"``).
    http2:
        Enable HTTP/2 (default ``True``).
    """

    # -- synchronous transport ----------------------------------------------
    def _send_once_sync(
        self,
        method: str,
        url: str,
        params: Any,
        data: Any,
        json: Any,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
        allow_redirects: bool,
        verify: bool,
    ) -> Any:
        if self._use_curl:
            session = self._ensure_sync_session()
            return session.request(
                method=method,
                url=url,
                params=params,
                data=data,
                json=json,
                headers=headers,
                proxy=proxy,
                timeout=timeout,
                allow_redirects=allow_redirects,
                verify=verify,
            )
        # httpx fallback: proxy needs a dedicated client; no-proxy reuses the pool
        if proxy is None:
            client = self._ensure_sync_session()
            close_after = False
        else:
            client = self._build_httpx_sync_client(proxy)
            close_after = True
        try:
            return client.request(
                method=method,
                url=url,
                params=params,
                data=data,
                json=json,
                headers=headers,
                timeout=timeout,
                follow_redirects=allow_redirects,
            )
        finally:
            if close_after:
                client.close()

    def _send_sync(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json: Any = None,
        **kwargs: Any,
    ) -> Response:
        merged_headers = self._merge_headers(headers)
        timeout = kwargs.pop("timeout", self.timeout)
        verify = kwargs.pop("verify", self.verify)
        allow_redirects = kwargs.pop("allow_redirects", self.follow_redirects)
        proxy = self._resolve_proxy()
        retry_errors = self._retry_errors()
        last_exc: BaseException | None = None
        for attempt in range(self.retries + 1):
            backoff = min(2.0**attempt, 10.0) + random.random() * 0.25
            try:
                raw = self._send_once_sync(
                    method,
                    url,
                    params,
                    data,
                    json,
                    merged_headers,
                    proxy,
                    timeout,
                    allow_redirects,
                    verify,
                )
            except retry_errors as exc:
                last_exc = exc
                if attempt == self.retries:
                    raise
                time.sleep(backoff)
                continue
            # 5xx 与 429（被限流）均重试；其余直接返回
            should_retry = raw.status_code >= 500 or raw.status_code == 429
            if not should_retry or attempt == self.retries:
                return self._to_response(raw, merged_headers)
            # 429 视为封锁信号：若有代理池则标记当前代理失败并换下一个
            if raw.status_code == 429 and isinstance(self.proxy, ProxyPool) and proxy:
                self.proxy.mark_failed(proxy)
                proxy = self._resolve_proxy()
            # 429 时尊重 Retry-After；否则指数退避
            delay = _parse_retry_after(raw.headers.get("Retry-After")) or backoff
            time.sleep(delay)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"request to {url} failed without a captured exception")

    # -- public synchronous API ---------------------------------------------
    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        """Send a request with ``method`` and return a :class:`Response`."""
        return self._send_sync(method, url, **kwargs)

    def get(
        self, url: str, *, params: Any = None, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Response:
        kwargs.setdefault("params", params)
        kwargs.setdefault("headers", headers)
        return self.request("GET", url, **kwargs)

    def post(
        self,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json: Any = None,
        **kwargs: Any,
    ) -> Response:
        kwargs.setdefault("params", params)
        kwargs.setdefault("headers", headers)
        kwargs.setdefault("data", data)
        kwargs.setdefault("json", json)
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Response:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Response:
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> Response:
        return self.request("HEAD", url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> Response:
        return self.request("OPTIONS", url, **kwargs)

    # -- public asynchronous API --------------------------------------------
    async def async_request(self, method: str, url: str, **kwargs: Any) -> Response:
        """Asynchronously send a request with ``method`` and return a :class:`Response`."""
        return await self._send_async(method, url, **kwargs)

    async def async_get(self, url: str, **kwargs: Any) -> Response:
        return await self.async_request("GET", url, **kwargs)

    async def async_post(self, url: str, **kwargs: Any) -> Response:
        return await self.async_request("POST", url, **kwargs)

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        """Close the underlying synchronous session.

        异步会话无法在同步上下文中安全关闭（需要事件循环），请使用 ``aclose()``
        或 ``async with`` 上下文管理器来清理异步会话资源。
        """
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
            self._session = None
        # 异步 session 不在这里强行关闭，避免在无事件循环时抛 RuntimeError
        if self._async_session is not None:
            warnings.warn(
                "Fetcher.close() 跳过了异步会话的关闭；请使用 await fetcher.aclose() "
                "或 ``async with Fetcher(...)`` 来正确释放异步资源。",
                ResourceWarning,
                stacklevel=2,
            )
            self._async_session = None

    async def aclose(self) -> None:
        """Asynchronously close both sync and async sessions."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None
        if self._async_session is not None:
            try:
                await self._async_session.close()
            except Exception:  # noqa: BLE001
                pass
            self._async_session = None

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


class AsyncFetcher(_FetcherCore):
    """Async-only stealth HTTP fetcher (Scrapling ``AsyncFetcher`` parity).

    Shares all configuration, backend selection and retry logic with
    :class:`Fetcher` but exposes **only** the asynchronous API. There are no
    synchronous ``get``/``post`` methods, so the intent is unambiguous and the
    sync session is never created.

    Use this in async applications to avoid accidentally blocking the event
    loop with a synchronous call.
    """

    # -- public asynchronous API --------------------------------------------
    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        """Asynchronously send a request with ``method`` and return a :class:`Response`."""
        return await self._send_async(method, url, **kwargs)

    async def get(
        self, url: str, *, params: Any = None, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Response:
        kwargs.setdefault("params", params)
        kwargs.setdefault("headers", headers)
        return await self.request("GET", url, **kwargs)

    async def post(
        self,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json: Any = None,
        **kwargs: Any,
    ) -> Response:
        kwargs.setdefault("params", params)
        kwargs.setdefault("headers", headers)
        kwargs.setdefault("data", data)
        kwargs.setdefault("json", json)
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> Response:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> Response:
        return await self.request("DELETE", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> Response:
        return await self.request("HEAD", url, **kwargs)

    async def options(self, url: str, **kwargs: Any) -> Response:
        return await self.request("OPTIONS", url, **kwargs)

    # -- lifecycle -----------------------------------------------------------
    async def aclose(self) -> None:
        """Asynchronously close the async session (sync session is never created)."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None
        if self._async_session is not None:
            try:
                await self._async_session.close()
            except Exception:  # noqa: BLE001
                pass
            self._async_session = None

    async def __aenter__(self) -> AsyncFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


__all__ = ["Fetcher", "AsyncFetcher"]

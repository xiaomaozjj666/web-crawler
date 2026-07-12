"""Tests for the async-only AsyncFetcher (Scrapling AsyncFetcher parity)."""

from __future__ import annotations

import asyncio
import inspect

from web_crawler import AsyncFetcher, Response


def test_async_fetcher_is_distinct_class() -> None:
    """AsyncFetcher must be its own class, not an alias of Fetcher."""
    from web_crawler import Fetcher

    assert AsyncFetcher is not Fetcher
    assert issubclass(AsyncFetcher, object)


def test_async_fetcher_has_no_sync_methods() -> None:
    """AsyncFetcher must NOT expose synchronous get/post/request (async-only API)."""
    f = AsyncFetcher(timeout=5.0)
    try:
        # All public HTTP methods must be coroutines (async-only API).
        assert inspect.iscoroutinefunction(f.get)
        assert inspect.iscoroutinefunction(f.post)
        assert inspect.iscoroutinefunction(f.request)
    finally:
        asyncio.run(f.aclose())


def test_async_fetcher_get_returns_response(local_server: str) -> None:
    async def go() -> Response:
        async with AsyncFetcher(timeout=10.0) as f:
            return await f.get(local_server + "/")

    resp = asyncio.run(go())
    assert isinstance(resp, Response)
    assert resp.status == 200
    assert b"Welcome" in resp.content


def test_async_fetcher_post(local_server: str) -> None:
    async def go() -> Response:
        async with AsyncFetcher(timeout=10.0) as f:
            return await f.post(local_server + "/")

    resp = asyncio.run(go())
    assert resp.status >= 400  # http.server returns 501 for POST


def test_async_fetcher_context_manager() -> None:
    async def go() -> None:
        async with AsyncFetcher(timeout=5.0) as f:
            assert f._async_session is None  # not yet created
        # After exit, sessions cleaned up
        assert f._async_session is None

    asyncio.run(go())


def test_async_fetcher_aclose_idempotent() -> None:
    async def go() -> None:
        f = AsyncFetcher(timeout=5.0)
        await f.aclose()
        await f.aclose()  # second close is a no-op

    asyncio.run(go())

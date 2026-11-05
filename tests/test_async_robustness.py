"""异步 robots、普通响应上限与异步用户钩子的回归测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from web_crawler import AsyncFetcher, Fetcher, Response, Spider
from web_crawler.exceptions import ConfigError, FetchError
from web_crawler.spider import ItemPipeline


class _PageSpider(Spider):
    start_urls = ["https://public.example/"]

    def parse(self, response: Response) -> Any:
        return None


@pytest.mark.asyncio
async def test_async_robots_uses_fetcher_ssrf_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robots URL 必须先过统一 SSRF 入口，被拒绝时不得进入传输层。"""
    validated: list[str] = []
    transport_calls: list[str] = []
    original_validate = Fetcher._validate_target

    def spy_validate(self: Fetcher, url: str) -> None:
        validated.append(url)
        original_validate(self, url)

    async def forbidden_transport(self: Fetcher, *args: Any) -> httpx.Response:
        transport_calls.append(str(args[1]))
        raise AssertionError("unsafe robots URL reached the transport")

    monkeypatch.setattr(Fetcher, "_validate_target", spy_validate)
    monkeypatch.setattr(Fetcher, "_send_with_redirects_async", forbidden_transport)
    async with Fetcher(resolve_hosts=False, allow_private_hosts=False) as fetcher:
        spider = _PageSpider(fetcher=fetcher)
        spider.respect_robots = True
        # 拉取失败沿用 robots 的保守放行语义，但 SSRF 校验本身必须发生。
        assert await spider._robots_allowed_async("http://127.0.0.1/private") is True

    assert validated == ["http://127.0.0.1/robots.txt"]
    assert transport_calls == []


@pytest.mark.asyncio
async def test_async_robots_yields_loop_and_preserves_single_flight_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robots 等待期间其他任务可运行；同 host 并发首查也只拉取一次。"""

    class SlowRobotsFetcher:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.release_robots = asyncio.Event()

        async def async_get(self, url: str, **kwargs: Any) -> Response:
            self.calls.append(url)
            if url.endswith("/robots.txt"):
                # 只有另一个事件循环任务能解除该等待；若 worker 同步阻塞便超时。
                await self.release_robots.wait()
                return Response(url, 200, b"User-agent: *\nCrawl-delay: 0\n")
            return Response(url, 200, b"<html></html>")

    class TwoPages(_PageSpider):
        start_urls = ["https://public.example/a", "https://public.example/b"]
        max_concurrency = 2
        respect_robots = True

    fetcher = SlowRobotsFetcher()
    legacy_calls: list[str] = []

    def forbidden_legacy_fetch(url: str, timeout: float = 10.0) -> str:
        legacy_calls.append(url)
        raise AssertionError("async worker used synchronous urllib robots fetch")

    monkeypatch.setattr("web_crawler.spider.spider.fetch_robots_text", forbidden_legacy_fetch)
    spider = TwoPages(fetcher=fetcher)

    async def independent_heartbeat() -> None:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        fetcher.release_robots.set()

    heartbeat = asyncio.create_task(independent_heartbeat())
    await asyncio.wait_for(spider.async_run(), timeout=1.0)
    await heartbeat

    assert legacy_calls == []
    assert fetcher.calls.count("https://public.example/robots.txt") == 1
    assert spider.stats.pages_crawled == 2


def _raw_response(url: str, content: bytes, *, content_length: str | None = None) -> httpx.Response:
    headers = {} if content_length is None else {"Content-Length": content_length}
    return httpx.Response(
        200,
        headers=headers,
        content=content,
        request=httpx.Request("GET", url),
    )


def test_fetcher_get_default_limit_and_per_request_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通 get 默认带 64 MiB 上限，调用方仍可显式传 None 关闭。"""
    url = "https://public.example/large"
    with Fetcher(allow_private_hosts=True) as fetcher:
        assert fetcher.max_bytes == 64 * 1024 * 1024
        monkeypatch.setattr(
            fetcher,
            "_send_with_redirects_sync",
            lambda *args: _raw_response(url, b"12345"),
        )
        assert fetcher.get(url, max_bytes=None).content == b"12345"


def test_fetcher_get_max_bytes_rejects_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步普通 get 超限抛带 max_bytes 与实际长度的明确 FetchError。"""
    url = "https://public.example/large"
    with Fetcher(allow_private_hosts=True, max_bytes=4) as fetcher:
        monkeypatch.setattr(
            fetcher,
            "_send_with_redirects_sync",
            lambda *args: _raw_response(url, b"12345"),
        )
        with pytest.raises(FetchError, match=r"response exceeds max_bytes \(4 bytes\)"):
            fetcher.get(url)


@pytest.mark.asyncio
async def test_async_fetcher_get_max_bytes_checks_decoded_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异步 get 即使 Content-Length 未暴露真实大小，也按最终 body 复核。"""
    url = "https://public.example/large"

    async def fake_send(*args: Any) -> httpx.Response:
        return _raw_response(url, b"12345", content_length="3")

    async with AsyncFetcher(allow_private_hosts=True) as fetcher:
        monkeypatch.setattr(fetcher, "_send_with_redirects_async", fake_send)
        with pytest.raises(FetchError, match=r"received 5 bytes"):
            await fetcher.get(url, max_bytes=4)


def test_fetcher_get_rejects_negative_max_bytes_before_transport() -> None:
    """负数上限快失败，不进入 SSRF DNS 或网络传输。"""
    with Fetcher(max_bytes=None) as fetcher, pytest.raises(ConfigError, match="max_bytes"):
        fetcher.get("https://public.example/", max_bytes=-1)
    with pytest.raises(ConfigError, match="max_bytes"):
        Fetcher(max_bytes=-1)


@pytest.mark.asyncio
async def test_async_callback_and_mixed_sync_async_item_pipelines() -> None:
    """async 回调被 await，同步/异步管道按声明顺序处理同一 item。"""

    class SyncPipeline(ItemPipeline):
        def process_item(self, item: dict[str, Any], spider: Spider) -> dict[str, Any]:
            item["steps"] = ["sync"]
            return item

    class AsyncPipeline(ItemPipeline):
        async def process_item(self, item: dict[str, Any], spider: Spider) -> dict[str, Any]:
            await asyncio.sleep(0)
            item["steps"].append("async")
            return item

    class AsyncCallbackSpider(_PageSpider):
        item_pipelines = [SyncPipeline, AsyncPipeline]

        async def parse(self, response: Response) -> list[dict[str, Any]]:
            await asyncio.sleep(0)
            return [{"url": response.url}]

    class ImmediateFetcher:
        async def async_get(self, url: str, **kwargs: Any) -> Response:
            return Response(url, 200, b"<html></html>")

    items = await AsyncCallbackSpider(fetcher=ImmediateFetcher()).async_run()
    assert items == [{"url": "https://public.example/", "steps": ["sync", "async"]}]

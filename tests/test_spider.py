"""Tests for the Spider framework using a fake in-memory fetcher."""

from __future__ import annotations

import asyncio
import base64
import json
import urllib.robotparser
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from web_crawler import Request, Response, Spider, SpiderError
from web_crawler.spider import DownloaderMiddleware, DropItem, IgnoreRequest, ItemPipeline


class FakeFetcher:
    """Returns canned HTML per URL — no network."""

    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> Response:
        self.calls.append(url)
        content = self.pages.get(url, b"<html><body>not found</body></html>")
        status = 200 if url in self.pages else 404
        return Response(url, status, content)

    async def async_get(self, url: str, **kwargs: Any) -> Response:
        return self.get(url, **kwargs)

    async def async_post(self, url: str, **kwargs: Any) -> Response:
        return self.get(url, **kwargs)

    async def async_request(self, method: str, url: str, **kwargs: Any) -> Response:
        return self.get(url, **kwargs)


PAGES = {
    "https://shop.example.com/": b"""
        <html><body>
          <a class="next" href="/page2">next</a>
          <div class="item"><span class="name">A</span></div>
          <div class="item"><span class="name">B</span></div>
        </body></html>""",
    "https://shop.example.com/page2": b"""
        <html><body>
          <div class="item"><span class="name">C</span></div>
        </body></html>""",
}


class ItemSpider(Spider):
    name = "item"
    start_urls = ["https://shop.example.com/"]
    allowed_domains = ["shop.example.com"]

    def parse(self, response: Response) -> Any:
        for item in response.css(".item"):
            name = item.css_first(".name")
            if name:
                yield {"name": str(name.text)}
        next_link = response.css_first("a.next")
        if next_link:
            yield Request(response.urljoin(next_link.attr("href")))


def test_request_validates_url() -> None:
    with pytest.raises(ValueError):
        Request(url="")


def test_request_defaults() -> None:
    r = Request("https://x")
    assert r.method == "GET"
    assert r.callback == "parse"
    assert r.priority == 0
    assert r.meta == {}


def test_spider_collects_items_and_follows_links() -> None:
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    items = spider.run()
    names = [it["name"] for it in items]
    assert names == ["A", "B", "C"]
    assert spider.stats.pages_crawled == 2
    assert spider.stats.items_scraped == 3
    assert spider.stats.requests_failed == 0


def test_spider_respects_allowed_domains() -> None:
    pages = {
        "https://shop.example.com/": b'<a href="https://other.example/x">ext</a>',
        "https://other.example/x": b"<p>should not be fetched</p>",
    }

    class S(Spider):
        start_urls = ["https://shop.example.com/"]
        allowed_domains = ["shop.example.com"]

        def parse(self, response: Response) -> Any:
            for a in response.css("a"):
                yield Request(response.urljoin(a.attr("href")))

    fetcher = FakeFetcher(pages)
    spider = S(fetcher=fetcher)
    spider.run()
    assert "https://other.example/x" not in fetcher.calls


def test_spider_deduplicates_requests() -> None:
    pages = {
        "https://shop.example.com/": b'<a href="/">loop</a><a href="/page2">p2</a>',
        "https://shop.example.com/page2": b'<a href="/">home</a>',
    }

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            for a in response.css("a"):
                yield Request(response.urljoin(a.attr("href")))

    fetcher = FakeFetcher(pages)
    spider = S(fetcher=fetcher)
    spider.run()
    # Each URL fetched at most once (dedup via seen-set)
    assert fetcher.calls.count("https://shop.example.com/") == 1
    assert fetcher.calls.count("https://shop.example.com/page2") == 1


def test_spider_max_requests_cap() -> None:
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    spider.run(max_requests=1)
    assert spider.stats.pages_crawled == 1


def test_spider_requires_fetcher() -> None:
    spider = ItemSpider()
    with pytest.raises(SpiderError, match="fetcher"):
        spider.run()


def test_spider_failed_request_does_not_abort_run() -> None:
    fetcher = FakeFetcher({})  # all URLs 404 but still returns a Response
    spider = ItemSpider(fetcher=fetcher)
    items = spider.run()
    # 404 page has no .item -> no items, but run completes without raising
    assert items == []
    assert spider.stats.pages_crawled == 1


def test_spider_callback_error_raises_spider_error() -> None:
    class Bad(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            raise RuntimeError("boom")

    spider = Bad(fetcher=FakeFetcher(PAGES))
    with pytest.raises(SpiderError, match="boom"):
        spider.run()


def test_spider_missing_callback_raises() -> None:
    class S(Spider):
        start_urls = ["https://shop.example.com/"]

    spider = S(fetcher=FakeFetcher(PAGES))
    req = Request("https://shop.example.com/", callback="nope")
    spider.dupefilter.seen.add(req.url)  # bypass filter
    with pytest.raises(SpiderError, match="not found"):
        spider._dispatch(Response(req.url, 200, b""), req)


def test_spider_pause_and_resume(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"

    class PausingSpider(Spider):
        name = "pausing"
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield {"x": 1}
            # Trigger pause after the start page only
            self.pause()
            for a in response.css("a"):
                yield Request(response.urljoin(a.attr("href")))

    fetcher = FakeFetcher(PAGES)
    spider = PausingSpider(fetcher=fetcher)
    items = spider.run(state_file=state_file)
    # First page only (paused before following links)
    assert len(items) == 1
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert len(state["queue"]) == 1  # /page2 queued but not yet fetched
    # seen 现在存指纹（method+url+body 的 SHA1），恢复时按原样装回
    from web_crawler.spider import DupeFilter

    assert DupeFilter.fingerprint(Request("https://shop.example.com/page2")) in state["seen"]

    # Resume with a different (non-pausing) spider that just collects items.
    class ResumingSpider(Spider):
        name = "pausing"  # same name so the default state path matches

        def parse(self, response: Response) -> Any:
            for item in response.css(".item"):
                name = item.css_first(".name")
                if name:
                    yield {"name": str(name.text)}

    resumed = ResumingSpider(fetcher=FakeFetcher(PAGES))
    items2 = resumed.run(state_file=state_file, resume=True)
    # page2 yields one item ("C")
    assert len(items2) == 1
    assert items2[0]["name"] == "C"
    # State file cleared on completion
    assert state_file.exists() is False


def test_spider_priority_ordering() -> None:
    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield Request("https://shop.example.com/low", priority=1)
            yield Request("https://shop.example.com/high", priority=10)
            yield Request("https://shop.example.com/mid", priority=5)

    pages = {
        "https://shop.example.com/": b"",
        "https://shop.example.com/high": b"",
        "https://shop.example.com/mid": b"",
        "https://shop.example.com/low": b"",
    }
    fetcher = FakeFetcher(pages)
    spider = S(fetcher=fetcher)
    spider.run()
    # high (10) before mid (5) before low (1)
    idx = {u: i for i, u in enumerate(fetcher.calls)}
    assert idx["https://shop.example.com/high"] < idx["https://shop.example.com/mid"]
    assert idx["https://shop.example.com/mid"] < idx["https://shop.example.com/low"]


def test_spider_async_run() -> None:
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    items = asyncio.run(spider.async_run())
    names = [it["name"] for it in items]
    assert names == ["A", "B", "C"]
    assert spider.stats.pages_crawled == 2


def test_spider_async_run_with_download_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """async_run() 在 download_delay>0 时应 await asyncio.sleep。"""

    class S(Spider):
        start_urls = ["https://shop.example.com/"]
        download_delay = 0.5

        def parse(self, response: Response) -> Any:
            return None

    spider = S(fetcher=FakeFetcher(PAGES))
    sleep_calls: list[float] = []

    async def mock_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr("asyncio.sleep", mock_sleep)
    asyncio.run(spider.async_run())
    assert sleep_calls == [0.5]


def test_spider_stream_with_download_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """stream() 在 download_delay>0 时应 await asyncio.sleep。"""

    class S(Spider):
        start_urls = ["https://shop.example.com/"]
        download_delay = 0.5

        def parse(self, response: Response) -> Any:
            return None

    spider = S(fetcher=FakeFetcher(PAGES))
    sleep_calls: list[float] = []

    async def mock_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    async def collect() -> list[Any]:
        async for _ in spider.stream():
            pass
        return []

    asyncio.run(collect())
    assert sleep_calls == [0.5]


def test_spider_meta_passes_between_requests() -> None:
    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield Request(
                "https://shop.example.com/page2",
                meta={"origin": response.url},
                callback="parse2",
            )

        def parse2(self, response: Response) -> Any:
            yield {"origin": response.meta.get("origin")}

    spider = S(fetcher=FakeFetcher(PAGES))
    items = spider.run()
    assert items == [{"origin": "https://shop.example.com/"}]


def test_spider_stats_as_dict() -> None:
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    spider.run()
    d = spider.stats.as_dict()
    assert d["pages_crawled"] == 2
    assert d["items_scraped"] == 3
    assert d["elapsed_seconds"] >= 0


def test_spider_stream_yields_items() -> None:
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))

    async def collect() -> list[Any]:
        items = []
        async for item in spider.stream():
            items.append(item)
        return items

    items = asyncio.run(collect())
    names = [it["name"] for it in items]
    assert names == ["A", "B", "C"]
    assert spider.stats.pages_crawled == 2
    assert spider.stats.items_scraped == 3


def test_spider_stream_requires_fetcher() -> None:
    spider = ItemSpider()

    async def go() -> None:
        async for _ in spider.stream():
            pass

    with pytest.raises(SpiderError, match="fetcher"):
        asyncio.run(go())


def test_spider_stream_respects_max_requests() -> None:
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))

    async def collect() -> list[Any]:
        items = []
        async for item in spider.stream(max_requests=1):
            items.append(item)
        return items

    items = asyncio.run(collect())
    assert spider.stats.pages_crawled == 1
    # 第一页有 2 个 item
    assert len(items) == 2


# ---------------------------------------------------------------------------
# 以下为补充测试：覆盖 urljoin / _filter / _dispatch / 状态持久化 / async / stream 边界
# ---------------------------------------------------------------------------


def test_spider_urljoin_resolves_relative() -> None:
    """urljoin 应解析相对 URL。"""
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    assert spider.urljoin("https://x.example/a/", "b") == "https://x.example/a/b"
    assert spider.urljoin("https://x.example/", "/p") == "https://x.example/p"


def test_spider_allowed_with_no_domains_allows_all() -> None:
    """allowed_domains 为空时所有域名都被允许。"""
    spider = Spider()
    assert spider.allowed("https://anything.example/") is True


def test_spider_allowed_with_subdomain_match() -> None:
    """子域名匹配应通过。"""

    class S(Spider):
        allowed_domains = ["example.com"]

    spider = S()
    assert spider.allowed("https://example.com/") is True
    assert spider.allowed("https://sub.example.com/") is True
    assert spider.allowed("https://other.example.org/") is False


def test_spider_filter_dont_filter_bypasses_seen() -> None:
    """dont_filter=True 时即使请求已见过也返回 True。"""
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    spider.dupefilter.request_seen(Request("https://x.example/"))
    req = Request("https://x.example/", dont_filter=True)
    assert spider._filter(req) is True


def test_spider_filter_skips_seen_url() -> None:
    """重复请求（同指纹）被 _filter 过滤。"""
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    spider.dupefilter.request_seen(Request("https://x.example/"))
    req = Request("https://x.example/")
    assert spider._filter(req) is False


def test_spider_filter_rejects_off_domain() -> None:
    """非允许域名 URL 被 _filter 拒绝。"""

    class S(Spider):
        allowed_domains = ["shop.example.com"]

    spider = S()
    req = Request("https://other.example.com/")
    assert spider._filter(req) is False


def test_spider_dispatch_returns_empty_when_callback_returns_none() -> None:
    """callback 返回 None 时 _dispatch 返回空列表。"""

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            return None

    spider = S(fetcher=FakeFetcher(PAGES))
    req = Request("https://shop.example.com/")
    outputs = spider._dispatch(Response(req.url, 200, b""), req)
    assert outputs == []


def test_spider_dispatch_copies_meta_to_response() -> None:
    """_dispatch 应把 request.meta 拷贝到 response.meta。"""

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield {"meta": response.meta.get("k")}

    spider = S(fetcher=FakeFetcher(PAGES))
    req = Request("https://shop.example.com/", meta={"k": "v"})
    outputs = spider._dispatch(Response(req.url, 200, b""), req)
    assert outputs == [{"meta": "v"}]


def test_spider_fetch_sync_post_method() -> None:
    """_fetch_sync POST 方法走 fetcher.post 路径。"""
    fetcher = MagicMock()
    fetcher.post.return_value = Response("https://x/", 200, b"")
    spider = Spider(fetcher=fetcher)
    req = Request("https://x/", method="POST", body=b"data")
    spider._fetch_sync(req)
    fetcher.post.assert_called_once()
    args, kwargs = fetcher.post.call_args
    assert args[0] == "https://x/"
    assert kwargs.get("data") == b"data"


def test_spider_fetch_sync_other_method() -> None:
    """_fetch_sync 非 GET/POST 方法走 fetcher.request 路径。"""
    fetcher = MagicMock()
    fetcher.request.return_value = Response("https://x/", 200, b"")
    spider = Spider(fetcher=fetcher)
    req = Request("https://x/", method="PUT", body=b"data")
    spider._fetch_sync(req)
    fetcher.request.assert_called_once()
    args, _kwargs = fetcher.request.call_args
    assert args[0] == "PUT"
    assert args[1] == "https://x/"


def test_spider_fetch_async_post_method() -> None:
    """_fetch_async POST 方法走 async_post 路径。"""
    fetcher = MagicMock()

    async def _async_post_return(*args: Any, **kwargs: Any) -> Response:
        return Response("https://x/", 200, b"")

    fetcher.async_post = _async_post_return
    spider = Spider(fetcher=fetcher)
    req = Request("https://x/", method="POST", body=b"data")

    async def go() -> Response:
        return await spider._fetch_async(req)

    resp = asyncio.run(go())
    assert resp.url == "https://x/"


def test_spider_fetch_async_other_method() -> None:
    """_fetch_async 非 GET/POST 方法走 async_request 路径。"""

    async def _async_request(method: str, url: str, **kwargs: Any) -> Response:
        return Response(url, 200, b"", headers={"method": method})

    fetcher = MagicMock()
    fetcher.async_request = _async_request
    spider = Spider(fetcher=fetcher)
    req = Request("https://x/", method="DELETE", body=b"")

    async def go() -> Response:
        return await spider._fetch_async(req)

    resp = asyncio.run(go())
    assert resp.url == "https://x/"


def test_spider_state_path_default() -> None:
    """_state_path 在不传 path 时返回默认 .{name}_state.json。"""
    spider = ItemSpider()
    p = spider._state_path(None)
    assert p.name == ".item_state.json"


def test_spider_state_path_explicit(tmp_path: Path) -> None:
    """_state_path 传入路径时返回该路径。"""
    spider = ItemSpider()
    p = spider._state_path(tmp_path / "x.json")
    assert p == tmp_path / "x.json"


def test_spider_load_state_returns_empty_when_file_missing(tmp_path: Path) -> None:
    """_load_state 文件不存在时返回 ([], False)。"""
    spider = ItemSpider()
    queue, restored = spider._load_state(tmp_path / "no-such.json")
    assert queue == []
    assert restored is False


def test_spider_load_state_raises_on_corrupt_file(tmp_path: Path) -> None:
    """_load_state 在 JSON 损坏时抛 SpiderError。"""
    spider = ItemSpider()
    bad = tmp_path / "bad.json"
    bad.write_text("{not legal json", encoding="utf-8")
    with pytest.raises(SpiderError, match="corrupt spider state"):
        spider._load_state(bad)


def test_spider_load_state_restores_seen_and_queue(tmp_path: Path) -> None:
    """_load_state 应恢复 seen 集合与 queue。"""
    spider = ItemSpider()
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "seen": ["https://a/", "https://b/"],
                "queue": [
                    {
                        "url": "https://c/",
                        "method": "GET",
                        "callback": "parse",
                        "priority": 5,
                        "dont_filter": False,
                    }
                ],
                "stats": {
                    "pages_crawled": 7,
                    "items_scraped": 3,
                    "requests_scheduled": 8,
                    "requests_failed": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    queue, restored = spider._load_state(state_file)
    assert restored is True
    assert len(queue) == 1
    assert queue[0].url == "https://c/"
    assert queue[0].priority == 5
    assert "https://a/" in spider.dupefilter.seen
    assert spider.stats.pages_crawled == 7
    assert spider.stats.requests_failed == 1


def test_spider_run_with_download_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """run() 在 download_delay>0 时应 sleep。"""

    class S(Spider):
        start_urls = ["https://shop.example.com/"]
        download_delay = 0.5

        def parse(self, response: Response) -> Any:
            return None

    spider = S(fetcher=FakeFetcher(PAGES))
    sleep_calls: list[float] = []
    monkeypatch.setattr("web_crawler.spider.spider.time.sleep", lambda s: sleep_calls.append(s))
    spider.run()
    assert sleep_calls == [0.5]


def test_spider_run_records_failed_request() -> None:
    """run() 在 fetcher.get 抛异常时计入 requests_failed 并继续。"""

    class _RaisingFetcher:
        def get(self, url: str, **kwargs: Any) -> Response:
            raise ConnectionError("network down")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield {"x": 1}

    spider = S(fetcher=_RaisingFetcher())
    items = spider.run()
    assert items == []
    assert spider.stats.requests_failed == 1
    assert spider.stats.pages_crawled == 0


def test_spider_async_run_requires_fetcher() -> None:
    """async_run 缺 fetcher 抛 SpiderError。"""
    spider = ItemSpider()
    with pytest.raises(SpiderError, match="fetcher"):
        asyncio.run(spider.async_run())


def test_spider_async_run_callback_error_raises() -> None:
    """async_run 在 callback 抛错时转为 SpiderError。"""

    class Bad(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            raise RuntimeError("async boom")

    spider = Bad(fetcher=FakeFetcher(PAGES))
    with pytest.raises(SpiderError, match="async boom"):
        asyncio.run(spider.async_run())


def test_spider_async_run_resume(tmp_path: Path) -> None:
    """async_run 从 state file resume。"""
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "seen": [],
                "queue": [
                    {
                        "url": "https://shop.example.com/page2",
                        "method": "GET",
                        "callback": "parse",
                    }
                ],
                "stats": {},
            }
        ),
        encoding="utf-8",
    )
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    items = asyncio.run(spider.async_run(state_file=state_file, resume=True))
    # /page2 有 1 个 item ("C")
    assert any(it["name"] == "C" for it in items)
    # 完成后 state file 应被清理
    assert not state_file.exists()


def test_spider_async_run_saves_state_when_paused(tmp_path: Path) -> None:
    """async_run 在 pause 后保存剩余 queue 到 state。"""
    state_file = tmp_path / "async_state.json"

    class PausingSpider(Spider):
        name = "pausing_async"
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield {"x": 1}
            self.pause()
            for a in response.css("a"):
                yield Request(response.urljoin(a.attr("href")))

    spider = PausingSpider(fetcher=FakeFetcher(PAGES))
    items = asyncio.run(spider.async_run(state_file=state_file))
    assert len(items) == 1
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(state["queue"]) == 1


def test_spider_async_run_handles_failed_request() -> None:
    """async_run 在 fetcher.async_get 抛异常时计入 requests_failed。"""

    class _RaisingAsyncFetcher:
        async def async_get(self, url: str, **kwargs: Any) -> Response:
            raise ConnectionError("async network down")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield {"x": 1}

    spider = S(fetcher=_RaisingAsyncFetcher())
    items = asyncio.run(spider.async_run())
    assert items == []
    assert spider.stats.requests_failed == 1


def test_spider_stream_resume(tmp_path: Path) -> None:
    """stream() 从 state file resume。"""
    state_file = tmp_path / "stream_state.json"
    state_file.write_text(
        json.dumps(
            {
                "seen": [],
                "queue": [
                    {
                        "url": "https://shop.example.com/page2",
                        "method": "GET",
                        "callback": "parse",
                    }
                ],
                "stats": {},
            }
        ),
        encoding="utf-8",
    )
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))

    async def collect() -> list[Any]:
        items = []
        async for item in spider.stream(state_file=state_file, resume=True):
            items.append(item)
        return items

    items = asyncio.run(collect())
    assert any(it["name"] == "C" for it in items)
    assert not state_file.exists()


def test_spider_stream_callback_error_raises() -> None:
    """stream() 在 callback 抛错时转为 SpiderError。"""

    class Bad(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            raise RuntimeError("stream boom")

    spider = Bad(fetcher=FakeFetcher(PAGES))

    async def go() -> None:
        async for _ in spider.stream():
            pass

    with pytest.raises(SpiderError, match="stream boom"):
        asyncio.run(go())


def test_spider_stream_handles_failed_request() -> None:
    """stream() 在 fetcher.async_get 抛异常时计入 requests_failed 并继续。"""

    class _RaisingAsyncFetcher:
        async def async_get(self, url: str, **kwargs: Any) -> Response:
            raise ConnectionError("stream network down")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield {"x": 1}

    spider = S(fetcher=_RaisingAsyncFetcher())

    async def collect() -> list[Any]:
        items = []
        async for item in spider.stream():
            items.append(item)
        return items

    items = asyncio.run(collect())
    assert items == []
    assert spider.stats.requests_failed == 1


def test_spider_stream_saves_state_when_paused(tmp_path: Path) -> None:
    """stream() 在 pause 后保存剩余 queue。"""
    state_file = tmp_path / "stream_pause.json"

    class PausingSpider(Spider):
        name = "pausing_stream"
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield {"x": 1}
            self.pause()
            for a in response.css("a"):
                yield Request(response.urljoin(a.attr("href")))

    spider = PausingSpider(fetcher=FakeFetcher(PAGES))

    async def collect() -> list[Any]:
        items = []
        async for item in spider.stream(state_file=state_file):
            items.append(item)
        return items

    items = asyncio.run(collect())
    assert len(items) == 1
    assert state_file.exists()


def test_spider_stream_clears_state_file_on_completion(tmp_path: Path) -> None:
    """stream() 完成且无剩余 queue 时删除 state file。"""
    state_file = tmp_path / "stream_complete.json"
    state_file.write_text(
        json.dumps({"seen": [], "queue": [], "stats": {}}),
        encoding="utf-8",
    )
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))

    async def collect() -> list[Any]:
        items = []
        async for item in spider.stream(state_file=state_file, resume=True):
            items.append(item)
        return items

    # resume=True 但 queue 为空，循环不执行；state file 在结束时被清理
    # 注意：因为 queue 空，body 不进入循环，但 start_requests 也不会跑
    # 实际上 resume=True 时跳过 start_requests，所以无任何请求
    asyncio.run(collect())
    assert state_file.exists() is False


def test_spider_run_clears_state_file_on_completion(tmp_path: Path) -> None:
    """run() 完成且无剩余 queue 时删除 state file。"""
    state_file = tmp_path / "run_complete.json"
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    spider.run(state_file=state_file)
    # 完成且 queue 空时 state file 应被删除
    assert not state_file.exists()


def test_spider_async_run_clears_state_file_on_completion(tmp_path: Path) -> None:
    """async_run() 完成时删除 state file。"""
    state_file = tmp_path / "async_complete.json"
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    asyncio.run(spider.async_run(state_file=state_file))
    assert not state_file.exists()


def test_spider_run_dedup_with_dont_filter_allows_duplicate() -> None:
    """dont_filter=True 的 Request 即使 URL 重复也会被调度。"""

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._reyielded = False

        def parse(self, response: Response) -> Any:
            # 仅在首次回调时再次 yield 同一 URL（dont_filter=True），避免无限循环
            if not self._reyielded:
                self._reyielded = True
                yield Request("https://shop.example.com/", dont_filter=True)

    fetcher = FakeFetcher(PAGES)
    spider = S(fetcher=fetcher)
    spider.run()
    # 应被重复抓取
    assert fetcher.calls.count("https://shop.example.com/") == 2


def test_spider_stats_elapsed_zero_when_not_started() -> None:
    """stats.elapsed 在未启动时为 0。"""
    spider = ItemSpider()
    assert spider.stats.elapsed == 0.0


def test_spider_stats_as_dict_after_run() -> None:
    """stats.as_dict 在 run 后应含合理字段。"""
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    spider.run()
    d = spider.stats.as_dict()
    assert d["pages_crawled"] == 2
    assert d["items_scraped"] == 3
    assert d["requests_scheduled"] >= 1
    assert d["elapsed_seconds"] >= 0


def test_spider_request_post_init_validates_empty_url() -> None:
    """Request 构造时空 URL 抛 ValueError。"""
    with pytest.raises(ValueError, match="non-empty"):
        Request(url="")


def test_spider_run_max_requests_zero() -> None:
    """max_requests=0 时 run 不抓取任何页面。"""
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    items = spider.run(max_requests=0)
    assert items == []
    assert spider.stats.pages_crawled == 0


def test_spider_async_run_max_requests_zero() -> None:
    """async_run max_requests=0 时不抓取。"""
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    items = asyncio.run(spider.async_run(max_requests=0))
    assert items == []
    assert spider.stats.pages_crawled == 0


def test_spider_dump_state_writes_json(tmp_path: Path) -> None:
    """_dump_state 写入包含 seen/queue/stats 的 JSON。"""
    spider = ItemSpider()
    spider.dupefilter.seen.add("https://a/")
    queue = [Request("https://b/", priority=3, callback="parse2")]
    state_file = tmp_path / "dump.json"
    spider._dump_state(queue, state_file)
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert "https://a/" in payload["seen"]
    assert payload["queue"][0]["url"] == "https://b/"
    assert payload["queue"][0]["priority"] == 3
    assert payload["queue"][0]["callback"] == "parse2"


def test_spider_name_auto_set_from_class_name() -> None:
    """无 name 时 __init__ 用类名填充。"""

    class MyCustomSpider(Spider):
        pass

    spider = MyCustomSpider()
    assert spider.name == "MyCustomSpider"


def test_spider_stream_yields_request_does_not_collect() -> None:
    """stream() 中 Request 不作为 item yield，而是调度。"""
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))

    async def collect() -> list[Any]:
        items = []
        async for item in spider.stream():
            items.append(item)
        return items

    items = asyncio.run(collect())
    # items 应为 dict（scraped items），不含 Request
    for it in items:
        assert isinstance(it, dict)


def test_spider_async_run_post_request() -> None:
    """async_run 中 Request method=POST 走 async_post 路径。"""

    async def _async_post(*args: Any, **kwargs: Any) -> Response:
        return Response("https://shop.example.com/submit", 200, b"<p>ok</p>")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield Request("https://shop.example.com/submit", method="POST", body=b"k=v")
            yield {"done": True}

    fetcher = FakeFetcher(PAGES)
    fetcher.async_post = _async_post  # type: ignore[attr-defined, method-assign]
    spider = S(fetcher=fetcher)
    items = asyncio.run(spider.async_run())
    assert any(it.get("done") for it in items)


def test_spider_stream_post_request() -> None:
    """stream() 中 Request method=POST 走 async_post。"""

    async def _async_post(*args: Any, **kwargs: Any) -> Response:
        return Response("https://shop.example.com/submit", 200, b"<p>ok</p>")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield Request("https://shop.example.com/submit", method="POST", body=b"k=v")
            yield {"submitted": True}

    fetcher = FakeFetcher(PAGES)
    fetcher.async_post = _async_post  # type: ignore[attr-defined, method-assign]
    spider = S(fetcher=fetcher)

    async def collect() -> list[Any]:
        items = []
        async for item in spider.stream():
            items.append(item)
        return items

    items = asyncio.run(collect())
    assert any(it.get("submitted") for it in items)


# ===========================================================================
# 回归：暂停/恢复状态序列化与状态文件生命周期（Fix1/2/3）
# ===========================================================================


def test_spider_pause_resume_preserves_post_body(tmp_path: Path) -> None:
    """带 body 的 POST 请求暂停后恢复仍为 POST 且 body 完整（base64 往返）。"""
    state_file = tmp_path / "post_state.json"
    recorded: list[tuple[str, str, bytes | None]] = []

    class RecFetcher:
        def get(self, url: str, **kwargs: Any) -> Response:
            recorded.append((url, "GET", kwargs.get("data")))
            return Response(url, 200, b"<p>ok</p>")

        def post(self, url: str, **kwargs: Any) -> Response:
            recorded.append((url, "POST", kwargs.get("data")))
            return Response(url, 200, b"<p>posted</p>")

    class S(Spider):
        name = "poster"
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            if response.url == "https://shop.example.com/":
                # 仅在 start 页调度 POST 并暂停；恢复后不再 pause
                yield Request(
                    "https://shop.example.com/submit",
                    method="POST",
                    body=b"k=v",
                    retries=2,
                )
                self.pause()
            else:
                yield {"posted": True}

    spider = S(fetcher=RecFetcher())
    spider.run(state_file=state_file)
    # 暂停前只抓了 start 页，POST 请求留在队列里
    assert recorded == [("https://shop.example.com/", "GET", None)]
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["queue"][0]["method"] == "POST"
    assert state["queue"][0]["retries"] == 2
    assert base64.b64decode(state["queue"][0]["body"]) == b"k=v"

    spider2 = S(fetcher=RecFetcher())
    items2 = spider2.run(state_file=state_file, resume=True)
    # 恢复后仍按 POST + 原始 body 发送
    assert recorded[-1] == ("https://shop.example.com/submit", "POST", b"k=v")
    assert items2 == [{"posted": True}]
    # 队列消费完毕，状态文件被清理
    assert not state_file.exists()


def test_spider_fresh_run_does_not_delete_existing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全新运行（未传 state_file/resume）完成时不得删除既有的暂停状态文件。"""
    monkeypatch.chdir(tmp_path)
    state_path = tmp_path / ".item_state.json"
    state_path.write_text(json.dumps({"seen": [], "queue": [], "stats": {}}), encoding="utf-8")

    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    spider.run()
    # 既有状态文件保留，未被全新运行误删
    assert state_path.exists()


def test_spider_max_requests_does_not_write_default_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """max_requests 提前结束（未暂停、未传 state_file）不得向 CWD 写状态文件。"""
    monkeypatch.chdir(tmp_path)
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    spider.run(max_requests=1)
    assert spider.stats.pages_crawled == 1
    assert not (tmp_path / ".item_state.json").exists()


def test_spider_pause_with_bytes_meta_serializes(tmp_path: Path) -> None:
    """meta 含 bytes 时暂停落盘不崩溃（default=str 兜底序列化）。"""
    state_file = tmp_path / "meta_state.json"

    class S(Spider):
        name = "meta_bytes"
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield {"x": 1}
            self.pause()
            yield Request("https://shop.example.com/page2", meta={"raw": b"\x00\x01payload"})

    spider = S(fetcher=FakeFetcher(PAGES))
    items = spider.run(state_file=state_file)
    assert len(items) == 1
    state = json.loads(state_file.read_text(encoding="utf-8"))
    # bytes 经 default=str 序列化为 "b'...'" 字符串，不再抛 TypeError
    assert "payload" in state["queue"][0]["meta"]["raw"]


def test_spider_allowed_ignores_port_and_userinfo() -> None:
    """allowed() 用 hostname 比较，忽略端口与 userinfo。"""

    class S(Spider):
        allowed_domains = ["example.com"]

    spider = S()
    assert spider.allowed("http://example.com:8080/") is True
    assert spider.allowed("https://user:pass@example.com/x") is True
    assert spider.allowed("https://sub.example.com:8443/") is True
    assert spider.allowed("https://other.org/") is False


def test_spider_allowed_missing_host_returns_false() -> None:
    """URL 无 host（如 about:blank）时 allowed() 返回 False。"""

    class S(Spider):
        allowed_domains = ["example.com"]

    spider = S()
    assert spider.allowed("about:blank") is False


# ===========================================================================
# DupeFilter 指纹去重 / 重试 / robots / 状态保存健壮性
# ===========================================================================


def test_dupefilter_fingerprint_distinguishes_method_and_body() -> None:
    """同 URL 不同 method / body 的指纹互不相同。"""
    from web_crawler.spider import DupeFilter

    base = Request("https://x.example/api")
    assert DupeFilter.fingerprint(base) != DupeFilter.fingerprint(
        Request("https://x.example/api", method="POST")
    )
    assert DupeFilter.fingerprint(base) != DupeFilter.fingerprint(
        Request("https://x.example/api", method="POST", body=b"a=1")
    )
    assert DupeFilter.fingerprint(
        Request("https://x.example/api", method="POST", body=b"a=1")
    ) != DupeFilter.fingerprint(Request("https://x.example/api", method="POST", body=b"a=2"))
    # method 大小写不敏感
    assert DupeFilter.fingerprint(base) == DupeFilter.fingerprint(
        Request("https://x.example/api", method="get")
    )


def test_dupefilter_request_seen_registers_once() -> None:
    """request_seen 首次返回 False 并登记，再次返回 True。"""
    from web_crawler.spider import DupeFilter

    df = DupeFilter()
    assert df.request_seen(Request("https://x.example/")) is False
    assert df.request_seen(Request("https://x.example/")) is True
    # 不同 body 视为不同请求
    assert df.request_seen(Request("https://x.example/", method="POST", body=b"p=1")) is False


def test_spider_same_url_different_body_not_deduped() -> None:
    """同 URL 不同 body（分页参数在 body 中）不再被裸 URL 去重误杀。"""
    calls: list[tuple[str, bytes | None]] = []

    class PostFetcher:
        def get(self, url: str, **kwargs: Any) -> Response:
            calls.append((url, None))
            return Response(url, 200, b"")

        def post(self, url: str, **kwargs: Any) -> Response:
            calls.append((url, kwargs.get("data")))
            return Response(url, 200, b"")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield Request("https://shop.example.com/api", method="POST", body=b"page=1")
            yield Request("https://shop.example.com/api", method="POST", body=b"page=2")

    spider = S(fetcher=PostFetcher())
    spider.run()
    assert calls.count(("https://shop.example.com/api", b"page=1")) == 1
    assert calls.count(("https://shop.example.com/api", b"page=2")) == 1


def test_spider_custom_dupefilter_injected() -> None:
    """构造参数 dupefilter 可替换为自定义实现。"""
    from web_crawler.spider import DupeFilter

    class RecordingDupeFilter(DupeFilter):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def request_seen(self, request: Request) -> bool:
            self.calls += 1
            return super().request_seen(request)

    df = RecordingDupeFilter()
    spider = ItemSpider(fetcher=FakeFetcher(PAGES), dupefilter=df)
    spider.run()
    # 种子直接登记指纹（不经 request_seen），回调产出的 /page2 经 _filter 调用一次
    assert df.calls == 1


def test_spider_run_retries_failed_request_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run() 失败请求按 max_retries 重试，第二次成功后正常产出。"""
    sleeps: list[float] = []
    monkeypatch.setattr("web_crawler.spider.spider.time.sleep", lambda s: sleeps.append(s))

    class FlakyFetcher:
        def __init__(self) -> None:
            self.attempts = 0

        def get(self, url: str, **kwargs: Any) -> Response:
            self.attempts += 1
            if self.attempts == 1:
                raise ConnectionError("transient")
            return Response(url, 200, b"<p>ok</p>")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]
        max_retries = 2

        def parse(self, response: Response) -> Any:
            yield {"ok": True}

    fetcher = FlakyFetcher()
    spider = S(fetcher=fetcher)
    items = spider.run()
    assert items == [{"ok": True}]
    assert fetcher.attempts == 2
    assert spider.stats.requests_failed == 0
    assert spider.stats.pages_crawled == 1
    # 第一次重试的退避为 0.5s（0.5 * 2^0）
    assert sleeps == [0.5]


def test_spider_run_retry_exhaustion_counts_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重试耗尽后计入 requests_failed 且不中断整体运行。"""
    sleeps: list[float] = []
    monkeypatch.setattr("web_crawler.spider.spider.time.sleep", lambda s: sleeps.append(s))

    class AlwaysFail:
        def get(self, url: str, **kwargs: Any) -> Response:
            raise ConnectionError("down")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]
        max_retries = 2

        def parse(self, response: Response) -> Any:
            yield {"never": True}

    spider = S(fetcher=AlwaysFail())
    items = spider.run()
    assert items == []
    assert spider.stats.requests_failed == 1
    assert spider.stats.pages_crawled == 0
    # 指数退避：0.5, 1.0
    assert sleeps == [0.5, 1.0]


def test_spider_stream_retries_failed_request_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream() 失败请求重试后成功。"""
    sleep_calls: list[float] = []

    async def mock_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    class FlakyAsyncFetcher:
        def __init__(self) -> None:
            self.attempts = 0

        async def async_get(self, url: str, **kwargs: Any) -> Response:
            self.attempts += 1
            if self.attempts == 1:
                raise ConnectionError("transient")
            return Response(url, 200, b"<p>ok</p>")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]
        max_retries = 1

        def parse(self, response: Response) -> Any:
            yield {"ok": True}

    fetcher = FlakyAsyncFetcher()
    spider = S(fetcher=fetcher)

    async def collect() -> list[Any]:
        items = []
        async for item in spider.stream():
            items.append(item)
        return items

    items = asyncio.run(collect())
    assert items == [{"ok": True}]
    assert fetcher.attempts == 2
    assert spider.stats.requests_failed == 0


def test_spider_robots_denied_request_is_filtered() -> None:
    """robots.txt 禁止的路径在 _filter 阶段被拒绝（不发请求）。"""
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    spider.respect_robots = True
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /private"])
    spider._robots_cache["https://shop.example.com"] = parser
    assert spider._filter(Request("https://shop.example.com/private")) is False
    assert spider._filter(Request("https://shop.example.com/public")) is True


def test_spider_robots_fetch_failure_treated_as_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robots.txt 拉取失败时保守视为允许，且同 host 不再重复外呼。"""
    read_calls: list[str] = []

    def fake_read(self: urllib.robotparser.RobotFileParser) -> None:
        read_calls.append(self.url)  # type: ignore[attr-defined]
        raise OSError("network unreachable")

    monkeypatch.setattr(urllib.robotparser.RobotFileParser, "read", fake_read)

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield {"x": 1}

    spider = S(fetcher=FakeFetcher(PAGES))
    spider.respect_robots = True
    assert spider._robots_allowed("https://shop.example.com/a") is True
    assert spider._robots_allowed("https://shop.example.com/b") is True
    # 失败判定被缓存：只外呼一次
    assert len(read_calls) == 1


def test_spider_robots_fetch_success_caches_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robots.txt 拉取成功后解析器被缓存，同 host 只外呼一次。"""
    read_calls: list[str] = []

    def fake_read(self: urllib.robotparser.RobotFileParser) -> None:
        read_calls.append(self.url)  # type: ignore[attr-defined]
        self.parse(["User-agent: *", "Disallow: /private", "Crawl-delay: 1"])

    monkeypatch.setattr(urllib.robotparser.RobotFileParser, "read", fake_read)

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

    spider = S(fetcher=FakeFetcher(PAGES))
    spider.respect_robots = True
    assert spider._robots_allowed("https://shop.example.com/private") is False
    assert spider._robots_allowed("https://shop.example.com/public") is True
    # 命中缓存：两个 URL 只触发一次 robots.txt 拉取
    assert len(read_calls) == 1


def test_spider_robots_disabled_by_default() -> None:
    """默认 respect_robots=False 时不做任何 robots 检查。"""
    spider = ItemSpider()
    assert spider._robots_allowed("https://shop.example.com/private") is True
    assert spider._robots_cache == {}


def test_spider_callback_error_still_saves_state(tmp_path: Path) -> None:
    """run() 回调抛 SpiderError 时，状态文件仍被保存（不丢已排队请求）。

    注：generator 回调中途 raise 会丢弃本次已 yield 的产出，因此待保存的
    请求须在前一个成功页的回调里产出。
    """
    state_file = tmp_path / "err_state.json"

    class Bad(Spider):
        name = "bad"
        start_urls = ["https://shop.example.com/", "https://shop.example.com/page2"]

        def parse(self, response: Response) -> Any:
            if response.url.endswith("page2"):
                raise RuntimeError("boom")
            yield Request("https://shop.example.com/pending1")
            yield {"x": 1}

    spider = Bad(fetcher=FakeFetcher(PAGES))
    with pytest.raises(SpiderError, match="boom"):
        spider.run(state_file=state_file)
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert [q["url"] for q in state["queue"]] == ["https://shop.example.com/pending1"]


def test_spider_stream_callback_error_still_saves_state(tmp_path: Path) -> None:
    """stream() 回调抛 SpiderError 时，状态文件仍被保存。"""
    state_file = tmp_path / "err_stream_state.json"

    class Bad(Spider):
        name = "bad_stream"
        start_urls = ["https://shop.example.com/", "https://shop.example.com/page2"]

        def parse(self, response: Response) -> Any:
            if response.url.endswith("page2"):
                raise RuntimeError("stream boom")
            yield Request("https://shop.example.com/pending2")

    spider = Bad(fetcher=FakeFetcher(PAGES))

    async def go() -> None:
        async for _ in spider.stream(state_file=state_file):
            pass

    with pytest.raises(SpiderError, match="stream boom"):
        asyncio.run(go())
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert [q["url"] for q in state["queue"]] == ["https://shop.example.com/pending2"]


# ===========================================================================
# 下载中间件 / item 管道 / 持续流式调度
# ===========================================================================


class _RecordingMiddleware(DownloaderMiddleware):
    """记录请求并支持短路/变换的可复用中间件。"""

    def __init__(
        self,
        short_circuit: Response | None = None,
        ignore_urls: set[str] | None = None,
        new_status: int | None = None,
    ) -> None:
        self.seen: list[str] = []
        self.short_circuit = short_circuit
        self.ignore_urls = ignore_urls or set()
        self.new_status = new_status

    def process_request(self, request: Request, spider: Spider) -> Response | None:
        self.seen.append(request.url)
        if request.url in self.ignore_urls:
            raise IgnoreRequest("denied")
        return self.short_circuit

    def process_response(self, response: Response, request: Request, spider: Spider) -> Response:
        if self.new_status is not None:
            response.status = self.new_status
        return response


def test_middleware_process_request_short_circuits_download() -> None:
    """process_request 返回 Response 时直接短路，不再发请求。"""
    fetched: list[str] = []

    class SpyFetcher(FakeFetcher):
        def get(self, url: str, **kwargs: Any) -> Response:
            fetched.append(url)
            return super().get(url, **kwargs)

    canned = Response("https://shop.example.com/", 200, b"<p>cached</p>")
    mw = _RecordingMiddleware(short_circuit=canned)

    class S(ItemSpider):
        start_urls = ["https://shop.example.com/"]

    spider = S(fetcher=SpyFetcher(PAGES))
    spider._middlewares = [mw]
    items = spider.run()
    # 短路响应 <p>cached</p> 无 .item，且真实 fetcher 从未被调用
    assert items == []
    assert fetched == []
    assert mw.seen == ["https://shop.example.com/"]


def test_middleware_short_circuit_content_reaches_callback() -> None:
    """短路响应的内容可被回调读到（走完整 dispatch 链）。"""
    canned = Response(
        "https://shop.example.com/", 200, b'<div class="item"><span class="name">Z</span></div>'
    )
    mw = _RecordingMiddleware(short_circuit=canned)

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            for item in response.css(".item"):
                name = item.css_first(".name")
                if name:
                    yield {"name": str(name.text)}

    spider = S(fetcher=FakeFetcher(PAGES))
    spider._middlewares = [mw]
    items = spider.run()
    assert items == [{"name": "Z"}]


def test_middleware_ignore_request_counts_ignored() -> None:
    """process_request 抛 IgnoreRequest 丢弃请求并计入 requests_ignored。"""
    mw = _RecordingMiddleware(ignore_urls={"https://shop.example.com/"})

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield {"x": 1}

    spider = S(fetcher=FakeFetcher(PAGES))
    spider._middlewares = [mw]
    items = spider.run()
    assert items == []
    assert spider.stats.requests_ignored == 1
    assert spider.stats.pages_crawled == 0


def test_middleware_process_response_transforms_response() -> None:
    """process_response 的变换对回调可见。"""
    mw = _RecordingMiddleware(new_status=999)

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield {"status": response.status}

    spider = S(fetcher=FakeFetcher(PAGES))
    spider._middlewares = [mw]
    items = spider.run()
    assert items == [{"status": 999}]


def test_middleware_runs_in_stream_too() -> None:
    """stream() 同样经过中间件链。"""
    mw = _RecordingMiddleware(ignore_urls={"https://shop.example.com/page2"})

    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    spider._middlewares = [mw]

    async def collect() -> list[Any]:
        items = []
        async for item in spider.stream():
            items.append(item)
        return items

    items = asyncio.run(collect())
    # 首页正常（A、B），page2 被中间件丢弃（C 缺失）
    names = [it["name"] for it in items]
    assert names == ["A", "B"]
    assert spider.stats.requests_ignored == 1


class _UpperPipeline(ItemPipeline):
    def process_item(self, item: Any, spider: Spider) -> Any:
        if isinstance(item, dict) and "name" in item:
            return {**item, "name": item["name"].upper()}
        return item


class _DropAPipeline(ItemPipeline):
    def process_item(self, item: Any, spider: Spider) -> Any:
        if isinstance(item, dict) and item.get("name") == "A":
            raise DropItem("drop A")
        return item


def test_item_pipeline_transforms_items() -> None:
    """item 管道按顺序变换 item。"""
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    spider._item_pipelines = [_UpperPipeline()]
    items = spider.run()
    assert [it["name"] for it in items] == ["A", "B", "C"]


def test_item_pipeline_drop_item_excludes_from_output() -> None:
    """DropItem 丢弃的 item 不进入结果、不计入 items_scraped。"""
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    spider._item_pipelines = [_DropAPipeline()]
    items = spider.run()
    assert [it["name"] for it in items] == ["B", "C"]
    assert spider.stats.items_scraped == 2


def test_item_pipeline_class_attribute_instantiated() -> None:
    """middlewares/item_pipelines 类属性传类时会被自动实例化。"""
    assert issubclass(_UpperPipeline, ItemPipeline)

    class S(ItemSpider):
        item_pipelines = [_UpperPipeline]

    spider = S(fetcher=FakeFetcher(PAGES))
    assert len(spider._item_pipelines) == 1
    assert isinstance(spider._item_pipelines[0], _UpperPipeline)
    items = spider.run()
    assert [it["name"] for it in items] == ["A", "B", "C"]


def test_stream_schedules_new_requests_while_slow_one_in_flight() -> None:
    """持续流式调度：慢请求在途时不阻塞新请求的派发（无整批 barrier）。

    构造：/slow 延迟 0.4s；/fast 立即返回且产出 /child。
    批式 barrier 下 /child 必须等 /slow 完成后才能调度，
    流式下 /child 在 /slow 完成前即被抓取。
    """
    done_order: list[str] = []

    class TimingFetcher:
        async def async_get(self, url: str, **kwargs: Any) -> Response:
            if url.endswith("/slow"):
                await asyncio.sleep(0.4)
                done_order.append("slow")
            elif url.endswith("/child"):
                done_order.append("child")
            else:
                done_order.append("fast")
            return Response(url, 200, b"")

    class S(Spider):
        start_urls = ["https://x.example/fast", "https://x.example/slow"]
        max_concurrency = 8

        def parse(self, response: Response) -> Any:
            if response.url.endswith("/fast"):
                yield Request("https://x.example/child")

    spider = S(fetcher=TimingFetcher())

    async def collect() -> list[Any]:
        items = []
        async for item in spider.stream():
            items.append(item)
        return items

    asyncio.run(collect())
    # child 在 slow 之前完成 = 慢请求未阻塞后续调度
    assert done_order.index("child") < done_order.index("slow")

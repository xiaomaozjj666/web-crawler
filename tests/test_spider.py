"""Tests for the Spider framework using a fake in-memory fetcher."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from web_crawler import Request, Response, Spider, SpiderError


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
    spider._seen.add(req.url)  # bypass filter
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
    assert "https://shop.example.com/page2" in state["seen"]

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
    """dont_filter=True 时即使 URL 已 seen 也返回 True。"""
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    spider._seen.add("https://x.example/")
    req = Request("https://x.example/", dont_filter=True)
    assert spider._filter(req) is True


def test_spider_filter_skips_seen_url() -> None:
    """重复 URL 被 _filter 过滤。"""
    spider = ItemSpider(fetcher=FakeFetcher(PAGES))
    spider._seen.add("https://x.example/")
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
    assert "https://a/" in spider._seen
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
    monkeypatch.setattr(
        "web_crawler.spider.spider.time.sleep", lambda s: sleep_calls.append(s)
    )
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
    spider._seen.add("https://a/")
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

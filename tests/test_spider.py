"""Tests for the Spider framework using a fake in-memory fetcher."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

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

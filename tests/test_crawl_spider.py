"""CrawlSpider / Rule 的测试（内存 FakeFetcher，无网络）。

覆盖：start_requests 指向 _parse_response、最小规则组合（产 item +
纯跟随）、A→B→C 递归深度、A→B→A 环路 DupeFilter 不死循环、
allowed_domains 过滤跟随请求、callback=None 纯跟随不产 item、
follow=False 只跟一层、回调方法名缺失警告（每规则一次）+ 跳过、
实例级 rules 赋值、CrawlSpider + feed_output 落盘端到端。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from web_crawler import CrawlSpider, LinkExtractor, Response, Rule

HOST = "shop.example.com"
BASE = f"https://{HOST}"

PAGES = {
    f"{BASE}/": (b'<html><body><a href="/list">list</a><a href="/detail/1">d1</a></body></html>'),
    f"{BASE}/list": b'<html><body><a href="/detail/1">d1</a></body></html>',
    f"{BASE}/detail/1": b"<html><body></body></html>",
}


class FakeFetcher:
    """返回按 URL 预置的 HTML，无网络；记录调用顺序。"""

    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> Response:
        self.calls.append(url)
        content = self.pages.get(url, b"<html><body></body></html>")
        status = 200 if url in self.pages else 404
        return Response(url, status, content)

    async def async_get(self, url: str, **kwargs: Any) -> Response:
        return self.get(url, **kwargs)


def test_start_requests_target_parse_response() -> None:
    """start_requests 产出的请求 callback 指向 _parse_response。"""
    spider = CrawlSpider(fetcher=FakeFetcher({}))
    spider.start_urls = [f"{BASE}/"]
    requests = list(spider.start_requests())
    assert [r.url for r in requests] == [f"{BASE}/"]
    assert all(r.callback == "_parse_response" for r in requests)


def test_minimal_spider_collects_items_and_follows() -> None:
    """两条规则（一条产 item 一条纯 follow）：item 集合与跟随请求都发生。"""

    class S(CrawlSpider):
        name = "minimal"
        start_urls = [f"{BASE}/"]
        rules = [
            Rule(LinkExtractor(allow=(r"/detail/\d+",)), callback="parse_detail"),
            Rule(LinkExtractor(allow=(r"/list",)), follow=True),
        ]

        def parse_detail(self, response: Response) -> Any:
            yield {"page": response.url}

    fetcher = FakeFetcher(PAGES)
    spider = S(fetcher=fetcher)
    items = spider.run()
    # 每个被 _parse_response 处理的页面都产出一条 item（简化语义）；
    # 跟随请求按规则声明顺序入队（同优先级 FIFO）：detail 规则先于 list
    assert [it["page"] for it in items] == [
        f"{BASE}/",
        f"{BASE}/detail/1",
        f"{BASE}/list",
    ]
    # 跟随请求确实发生：detail/1 与 list 都被抓取
    assert fetcher.calls == [f"{BASE}/", f"{BASE}/detail/1", f"{BASE}/list"]
    assert spider.stats.pages_crawled == 3


def test_recursive_traversal_depth() -> None:
    """A→B→C 页面链：follow=True 递归遍历到链尾。"""
    chain = {
        f"{BASE}/a": b'<html><body><a href="/b">B</a></body></html>',
        f"{BASE}/b": b'<html><body><a href="/c">C</a></body></html>',
        f"{BASE}/c": b"<html><body></body></html>",
    }

    class S(CrawlSpider):
        name = "chain"
        start_urls = [f"{BASE}/a"]
        rules = [Rule(LinkExtractor(allow=(r"/[bc]",)))]

    fetcher = FakeFetcher(chain)
    items = S(fetcher=fetcher).run()
    assert fetcher.calls == [f"{BASE}/a", f"{BASE}/b", f"{BASE}/c"]
    assert items == []


def test_cycle_terminates_via_dupefilter() -> None:
    """A→B→A 环路：DupeFilter 去重让遍历有限，不死循环。"""
    cycle = {
        f"{BASE}/a": b'<html><body><a href="/b">B</a></body></html>',
        f"{BASE}/b": b'<html><body><a href="/a">A</a></body></html>',
    }

    class S(CrawlSpider):
        name = "cycle"
        start_urls = [f"{BASE}/a"]
        rules = [Rule(LinkExtractor())]

    fetcher = FakeFetcher(cycle)
    S(fetcher=fetcher).run()
    assert fetcher.calls.count(f"{BASE}/a") == 1
    assert fetcher.calls.count(f"{BASE}/b") == 1


def test_allowed_domains_filters_follow_requests() -> None:
    """allowed_domains 过滤跟随请求：外域链接不会被请求。"""
    pages = {
        f"{BASE}/": (
            b'<html><body><a href="/inner">in</a>'
            b'<a href="https://other.example/x">out</a></body></html>'
        ),
        f"{BASE}/inner": b"<html><body></body></html>",
    }

    class S(CrawlSpider):
        name = "domain"
        start_urls = [f"{BASE}/"]
        allowed_domains = [HOST]
        rules = [Rule(LinkExtractor())]

    fetcher = FakeFetcher(pages)
    S(fetcher=fetcher).run()
    assert f"{BASE}/inner" in fetcher.calls
    assert "https://other.example/x" not in fetcher.calls


def test_pure_follow_rule_yields_no_items() -> None:
    """callback=None 纯跟随：不产 item，但跟随请求照常发生。"""

    class S(CrawlSpider):
        name = "pure_follow"
        start_urls = [f"{BASE}/"]
        rules = [Rule(LinkExtractor(allow=(r"/list",)), follow=True)]

    fetcher = FakeFetcher(PAGES)
    items = S(fetcher=fetcher).run()
    assert items == []
    assert f"{BASE}/list" in fetcher.calls


def test_follow_false_only_single_level() -> None:
    """follow=False 只跟一层：命中页面被请求并交给回调，不再递归。"""
    pages = {
        f"{BASE}/a": b'<html><body><a href="/b">B</a></body></html>',
        f"{BASE}/b": b'<html><body><a href="/c">C</a></body></html>',
        f"{BASE}/c": b"<html><body></body></html>",
    }

    class S(CrawlSpider):
        name = "single_level"
        start_urls = [f"{BASE}/a"]
        rules = [Rule(LinkExtractor(allow=(r"/[bc]",)), callback="parse_hit", follow=False)]

        def parse_hit(self, response: Response) -> Any:
            yield {"page": response.url}

    fetcher = FakeFetcher(pages)
    items = S(fetcher=fetcher).run()
    # 只跟一层：/a（种子）与 /b（第一层）被处理，/c 不再被请求
    assert fetcher.calls == [f"{BASE}/a", f"{BASE}/b"]
    # 规则回调在种子页与命中页各调用一次
    assert [it["page"] for it in items] == [f"{BASE}/a", f"{BASE}/b"]


def test_missing_callback_method_is_skipped() -> None:
    """callback 指向不存在的方法名时静默跳过该规则，跟随照常。"""

    class S(CrawlSpider):
        name = "missing_cb"
        start_urls = [f"{BASE}/"]
        rules = [Rule(LinkExtractor(allow=(r"/list",)), callback="no_such_method")]

    fetcher = FakeFetcher(PAGES)
    items = S(fetcher=fetcher).run()
    assert items == []
    assert fetcher.calls == [f"{BASE}/", f"{BASE}/list"]


def test_missing_callback_method_warns_once_across_responses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M-6 回归：回调缺失记 warning，两条响应只警告一次（不逐响应刷屏）。"""

    class S(CrawlSpider):
        name = "missing_cb_warn"
        start_urls = [f"{BASE}/"]
        rules = [Rule(LinkExtractor(allow=(r"/list",)), callback="no_such_method")]

    fetcher = FakeFetcher(PAGES)
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.crawl"):
        items = S(fetcher=fetcher).run()
    assert items == []
    # / 与 /list 两个响应都走 _parse_response 评估该规则：warning 只出现一次
    assert fetcher.calls == [f"{BASE}/", f"{BASE}/list"]
    warnings = [rec.getMessage() for rec in caplog.records if "no_such_method" in rec.getMessage()]
    assert len(warnings) == 1
    assert "跳过" in warnings[0]


def test_rules_assigned_on_instance() -> None:
    """__init__ 后给 self.rules 赋值同样生效（运行时统一读实例属性）。"""

    class S(CrawlSpider):
        name = "instance_rules"
        start_urls = [f"{BASE}/"]

        def parse_page(self, response: Response) -> Any:
            yield {"page": response.url}

    spider = S(fetcher=FakeFetcher(PAGES))
    spider.rules = [Rule(LinkExtractor(allow=(r"/list",)), callback="parse_page")]
    items = spider.run()
    assert [it["page"] for it in items] == [f"{BASE}/", f"{BASE}/list"]


def test_crawl_spider_with_feed_output_end_to_end(tmp_path: Path) -> None:
    """CrawlSpider + feed_output 落盘端到端：JSONL 内容与 run 返回一致。"""

    class S(CrawlSpider):
        name = "feed_crawl"
        start_urls = [f"{BASE}/"]
        rules = [Rule(LinkExtractor(allow=(r"/list",)), callback="parse_page")]

        def parse_page(self, response: Response) -> Any:
            yield {"url": response.url, "status": response.status}

    path = tmp_path / "feed.jsonl"
    spider = S(fetcher=FakeFetcher(PAGES), feed_output=path)
    items = spider.run()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    expected = [
        {"url": f"{BASE}/", "status": 200},
        {"url": f"{BASE}/list", "status": 200},
    ]
    assert items == expected
    assert lines == expected
    assert spider.stats.items_scraped == 2

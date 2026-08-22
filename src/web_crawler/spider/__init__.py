"""Scrapling/Scrapy 风格的 spider 框架。

轻量、低依赖的 spider 引擎，以基于回调的请求/响应管道驱动隐身 fetcher，
提供广度优先调度、域名过滤与落盘的暂停/恢复状态。

Example
-------
>>> from web_crawler import Spider, Request, Fetcher
>>> class QuotesSpider(Spider):
...     start_urls = ["https://quotes.toscrape.com/"]
...     def parse(self, response):
...         for quote in response.css("div.quote"):
...             yield {"text": quote.css_first(".text").text, "author": quote.css_first(".author").text}
...         for href in response.css("a.next::attr(href)"):
...             yield Request(response.urljoin(href))
>>> QuotesSpider(fetcher=Fetcher()).run()
"""

from __future__ import annotations

from .spider import (
    DownloaderMiddleware,
    DropItem,
    DupeFilter,
    IgnoreRequest,
    ItemPipeline,
    Request,
    Spider,
    SpiderError,
    SpiderStats,
)

__all__ = [
    "DownloaderMiddleware",
    "DropItem",
    "DupeFilter",
    "IgnoreRequest",
    "ItemPipeline",
    "Request",
    "Spider",
    "SpiderError",
    "SpiderStats",
]

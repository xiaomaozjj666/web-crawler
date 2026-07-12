"""Scrapling/Scrapy-style spider framework.

A lightweight, dependency-light spider engine that drives the stealth fetchers
with a callback-based request/response pipeline, breadth-first scheduling,
domain filtering, and on-disk pause/resume state.

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

from .spider import Request, Spider, SpiderError, SpiderStats

__all__ = ["Request", "Spider", "SpiderError", "SpiderStats"]

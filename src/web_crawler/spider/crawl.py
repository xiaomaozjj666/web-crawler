r"""Scrapy 风格的 :class:`CrawlSpider` —— 规则驱动的跟随爬虫。

用 :class:`Rule` 列表声明「跟随策略」而非在每个回调里手写链接翻页：
每个响应统一进入 :meth:`CrawlSpider._parse_response`，按规则提取链接、
生成跟随请求（递归遍历）并调用规则指定的回调产出 item。

跟随请求沿用 :class:`~web_crawler.spider.Spider` 基类的全部既有机制：
``allowed_domains`` 域过滤、:class:`~web_crawler.spider.DupeFilter` 去重
（环路不死循环）、robots 检查、重试 / 节流 / 缓存 / feed 导出等——
引擎在调度回调产出的 Request 时自动过 :meth:`Spider._filter`。

示例
----
>>> from web_crawler import CrawlSpider, Rule, LinkExtractor
>>> class QuotesCrawler(CrawlSpider):
...     start_urls = ["https://quotes.toscrape.com/"]
...     rules = [
...         Rule(LinkExtractor(allow=(r"/page/\d+",)), callback="parse_page"),
...     ]
...     def parse_page(self, response):
...         for q in response.css("div.quote"):
...             yield {"text": q.css_first(".text").text}
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from ..parser.links import LinkExtractor
from ..response import Response
from .spider import Request, Spider

logger = logging.getLogger(__name__)

# CrawlSpider 全部响应（种子与跟随）共用的回调方法名。
# 本库 Request.callback 是字符串方法名（Spider._dispatch 以 getattr 解析），
# 与 Scrapy 的字符串约定一致。
_PARSE_RESPONSE = "_parse_response"


@dataclass
class Rule:
    """CrawlSpider 的跟随规则。

    Attributes
    ----------
    link_extractor:
        命中链接的提取器（含 allow/deny/域过滤等全部语义）。
    callback:
        回调方法名（``None`` = 不产 item，仅跟随）。方法不存在时跳过
        该规则并记 warning（每规则只警告一次；对齐 Scrapy 的宽容处理，
        不中断爬取）。
    follow:
        是否继续从命中的链接（页面）提取（递归遍历）。``False`` 时
        「只跟一层」：命中链接仍会被请求，但直接交给 ``callback``
        解析、不再递归；``callback`` 为 ``None`` 时不生成请求。
    """

    link_extractor: LinkExtractor
    callback: str | None = None
    follow: bool = True


class CrawlSpider(Spider):
    """规则驱动的跟随爬虫（对标 Scrapy CrawlSpider）。

    子类声明 :attr:`rules`（类属性或 ``__init__`` 里给 ``self.rules``
    赋值均可，运行时统一读 ``self.rules``），并实现各规则的回调方法。
    继承 :class:`~web_crawler.spider.Spider` 全部能力（feed_output /
    retry / autothrottle / cache / 暂停恢复等自动可用）。
    """

    rules: list[Rule] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 已警告过「回调方法缺失」的规则名：每规则只警告一次，防止逐
        # 响应重复刷屏。实例属性而非类属性——可变集合不能跨实例共享
        self._warned_missing_callbacks: set[str] = set()

    def start_requests(self) -> Iterator[Request]:
        """产出初始请求：callback 指向 :meth:`_parse_response`。"""
        for url in self.start_urls:
            yield Request(url=url, callback=_PARSE_RESPONSE)

    def _parse_response(self, response: Response) -> Iterator[Any]:
        """核心分发：逐规则提取链接、跟随并调用回调。

        对每个 rule：

        1. 用 ``rule.link_extractor.extract(response)`` 提取命中链接；
        2. ``rule.follow=True`` 时为每条命中链接产出跟随 Request
           （callback 仍是 :meth:`_parse_response`，实现递归遍历）；
           域过滤 / 去重 / robots 由引擎调度层自动执行；
        3. ``rule.follow=False`` 时「只跟一层」：命中链接仍生成请求，
           但 callback 直接指向 ``rule.callback``（不再从命中页面继续
           提取）；``rule.callback`` 为 ``None`` 时不生成请求；
        4. ``rule.callback`` 指向的方法存在时，在当前 response 上调用
           并透传其全部产出（item 或 Request——Request 同样进入引擎调度）。

        ``rule.callback`` 指向不存在的方法名时宽容跳过该规则（对齐
        Scrapy，不中断爬取），但会记 warning 且每规则只警告一次——
        静默丢弃会掩盖回调名拼写错误等配置问题。
        """
        for rule in self.rules:
            links = rule.link_extractor.extract(response)
            name = rule.callback
            callback = getattr(self, name, None) if name is not None else None
            if name is not None and callback is None:
                if name not in self._warned_missing_callbacks:
                    self._warned_missing_callbacks.add(name)
                    logger.warning(
                        "CrawlSpider 规则回调 %r 不存在于 %s，跳过该规则",
                        name,
                        type(self).__name__,
                    )
            if rule.follow:
                for link in links:
                    yield Request(url=link.url, callback=_PARSE_RESPONSE)
            elif name is not None and callback is not None:
                # 只跟一层：命中页面交给规则回调解析，不再递归提取
                for link in links:
                    yield Request(url=link.url, callback=name)
            if callback is not None:
                result = callback(response)
                if result is not None:
                    yield from result


__all__ = ["CrawlSpider", "Rule"]

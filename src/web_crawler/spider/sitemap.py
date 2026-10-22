r"""SitemapSpider —— sitemap.xml 驱动的爬虫（对标 Scrapy SitemapSpider）。

以站点地图取代链接跟随作为发现机制：sitemapindex 递归展开、
``sitemap_rules`` 正则分发回调、robots.txt 的 ``Sitemap:`` 行自动发现。

种子来源（两种，可并存于不同配置）：

1. ``sitemap_urls`` 显式给出的 sitemap 地址（对标 Scrapy）；
2. ``sitemap_urls`` 为空且 ``respect_robots=True`` 时，从 ``start_urls``
   各域 robots.txt 的 ``Sitemap:`` 行自动发现（本库对 Scrapy 的扩展，
   Scrapy 要求用户自行在 sitemap_urls 里写好或读 robots）。

跟 :class:`~web_crawler.spider.crawl.CrawlSpider` 同款「自动生效」语义：
``<loc>`` 生成的 Request 沿用基类引擎调度，自动过 ``allowed_domains``
域过滤、:class:`~web_crawler.spider.DupeFilter` 去重（sitemapindex 环路
不死循环）与 robots 检查；限速 / AutoThrottle / 重试 / feed 导出等全部
继承可用。sitemap 自身的抓取也走 spider 的 fetcher（享受 SSRF 校验）。

示例
----
>>> from web_crawler import SitemapSpider
>>> class BlogSitemap(SitemapSpider):
...     sitemap_urls = ["https://example.com/sitemap.xml"]
...     sitemap_rules = [(r"/blog/", "parse_blog")]
...     def parse_blog(self, response):
...         yield {"url": response.url, "title": response.css_first("title").text}
"""

from __future__ import annotations

import gzip
import logging
import re
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lxml import etree

from ..exceptions import ConfigError
from ..response import Response
from .autothrottle import AutoThrottle
from .spider import DupeFilter, Request, Spider

logger = logging.getLogger(__name__)

# SitemapSpider 全部 sitemap 响应（显式种子与 index 递归）共用的回调方法名。
# 与 CrawlSpider 的 _PARSE_RESPONSE 同款手法：Request.callback 是字符串方法名，
# Spider._dispatch 以 getattr 解析。
_PARSE_SITEMAP = "_parse_sitemap"


def _sitemap_body(content: bytes) -> bytes:
    """按 gzip magic 解包 sitemap 内容（.gz sitemap 常见，站点可能整站压缩）。"""
    if content[:2] == b"\x1f\x8b":
        return gzip.decompress(content)
    return content


def _iter_child_locs(root: etree._Element, child: str) -> Iterator[str]:
    """按文档序产出 ``root`` 下 ``child`` 元素的非空 ``<loc>`` 文本。

    只比对 localname：标准 sitemap 用 ``http://www.sitemaps.org/schemas/
    sitemap/0.9`` 命名空间，但现实中存在无命名空间或自造 URI 的站点，
    localname 匹配对两者都健壮；XML 实体（``&amp;`` 等）由 lxml 解析时
    自动还原。注释 / PI 等非元素节点（tag 为可调用对象）跳过。
    """
    for entry in root:
        if not isinstance(entry.tag, str) or etree.QName(entry).localname != child:
            continue
        for node in entry:
            if isinstance(node.tag, str) and etree.QName(node).localname == "loc":
                text = (node.text or "").strip()
                if text:
                    yield text


class SitemapSpider(Spider):
    """sitemap 驱动的爬虫（对标 Scrapy SitemapSpider）。

    Attributes
    ----------
    sitemap_urls:
        显式 sitemap 种子地址（sitemap 或 sitemapindex 均可，index 会递归
        展开）。为空且 ``respect_robots=True`` 时改由 ``start_urls`` 各域
        robots.txt 的 ``Sitemap:`` 行自动发现种子。
    sitemap_rules:
        ``(正则, 回调方法名)`` 有序列表。每个 ``<loc>`` 按声明顺序做
        ``re.search`` 匹配，**首个**命中规则的回调生效；回调名为 ``None``
        表示显式排除（命中即丢弃，可用 ``(r"", None)`` 收尾实现白名单）；
        全不命中同样丢弃。列表为空时全部丢弃并在构造期发 warning（对齐
        「无规则即无产出」的保守语义，Scrapy 的默认 ``[('', 'parse')]``
        需用户显式声明才有等价行为）。
    """

    sitemap_urls: list[str] = []
    sitemap_rules: list[tuple[str, str | None]] = []

    def __init__(
        self,
        fetcher: Any | None = None,
        *,
        adaptive: bool = False,
        dupefilter: DupeFilter | None = None,
        autothrottle: AutoThrottle | None = None,
        stats_output: str | Path | None = None,
        feed_output: str | Path | None = None,
    ) -> None:
        super().__init__(
            fetcher,
            adaptive=adaptive,
            dupefilter=dupefilter,
            autothrottle=autothrottle,
            stats_output=stats_output,
            feed_output=feed_output,
        )
        # 种子校验（构造期快失败）：显式种子与 robots 自动发现（需要
        # start_urls 提供域 + respect_robots 开启）至少占其一，否则运行
        # 期必然空转
        if not self.sitemap_urls and not (self.respect_robots and self.start_urls):
            raise ConfigError(
                "SitemapSpider 需要 sitemap_urls 种子；或提供 start_urls 且 "
                "respect_robots=True 以从各域 robots.txt 的 Sitemap: 行自动发现"
            )
        # 规则编译（构造期快失败）：非法正则在此暴露而非首个响应到达时
        self._sitemap_cbs: list[tuple[re.Pattern[str], str | None]] = []
        for idx, (pattern, callback) in enumerate(self.sitemap_rules):
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise ConfigError(f"sitemap_rules[{idx}] 含非法正则 {pattern!r}: {exc}") from exc
            self._sitemap_cbs.append((compiled, callback))
        if not self._sitemap_cbs:
            logger.warning(
                "sitemap_rules 为空：%s 将丢弃全部 <loc> URL（请声明 (正则, 回调) 规则）",
                self.name,
            )

    # -- user hooks --------------------------------------------------------
    def start_requests(self) -> Iterator[Request]:
        """产出 sitemap 种子请求：显式 sitemap_urls 优先，否则 robots 自动发现。"""
        urls = list(self.sitemap_urls)
        if not urls:
            urls = self._sitemaps_from_robots()
            if not urls:
                logger.warning("robots.txt 未提供任何 Sitemap: 行，%s 无种子可抓", self.name)
        for url in urls:
            yield Request(url=url, callback=_PARSE_SITEMAP)

    # -- sitemap 链路 ------------------------------------------------------
    def _sitemaps_from_robots(self) -> list[str]:
        """从 ``start_urls`` 各域 robots.txt 的 ``Sitemap:`` 行提取 sitemap 种子。

        走基类 :class:`~web_crawler.robots.RobotsPolicy` 的按主机缓存：先经
        :meth:`Spider._robots_allowed` 触发拉取（与后续每个请求的 robots
        检查共享缓存，同一主机至多一次 robots.txt 外呼），再从缓存的原始
        行提取 ``Sitemap:`` 值。``RobotsPolicy`` 尚未把原始行暴露为公共
        API（本次改动范围不含 robots.py），故直接读其 ``_lines``——同一
        发布版内私有结构稳定，升级时随 RobotsPolicy 一起评估。
        """
        found: list[str] = []
        seen_hosts: set[str] = set()
        for start in self.start_urls:
            # 触发按主机缓存拉取（respect_robots=False 时本方法不会被调到）
            self._robots_allowed(start)
            parsed = urlparse(start)
            host_key = f"{parsed.scheme}://{parsed.netloc}"
            if host_key in seen_hosts:
                continue
            seen_hosts.add(host_key)
            for raw in self._robots_policy._lines.get(host_key, []):
                # Sitemap: 行不区分大小写；行内注释按 robots 惯例剥除；
                # split(":", 1) 只切键后的第一个冒号，URL 内的冒号无损
                line = raw.split("#", 1)[0].strip()
                if line.lower().startswith("sitemap:"):
                    value = line.split(":", 1)[1].strip()
                    if value:
                        found.append(value)
        return found

    def _callback_for(self, url: str) -> str | None:
        """按声明顺序 ``re.search`` 匹配 sitemap_rules，返回首个命中的回调名。

        命中规则的 ``callback`` 为 ``None`` 表示显式排除（返回 None）；
        全不命中同样返回 None。两种丢弃共用返回值——差异只在用户意图，
        引擎处理完全一致（不生成请求）。
        """
        for pattern, callback in self._sitemap_cbs:
            if pattern.search(url):
                return callback
        return None

    def _parse_sitemap(self, response: Response) -> Iterator[Any]:
        """解析 sitemap 响应：index 递归入队子 sitemap，urlset 规则分发。

        根元素 localname 判型：``sitemapindex`` 的每个 ``<sitemap>/<loc>``
        以本回调再次入队（递归展开由 DupeFilter 兜底防环）；``urlset`` 的
        每个 ``<url>/<loc>`` 经 :meth:`_callback_for` 分发为命中回调的
        Request，未命中 / 显式排除的丢弃。解析失败（非 XML / 损坏 gzip）
        只记 warning 不中断——单个坏 sitemap 不该连累其余种子的抓取。
        """
        try:
            root = etree.fromstring(_sitemap_body(response.content))
        except (etree.XMLSyntaxError, gzip.BadGzipFile, EOFError, zlib.error) as exc:
            # XMLSyntaxError 覆盖非 XML / 语法错误；其余三个覆盖损坏 gzip
            # 流的三种典型抛出（magic 异常 / 截断 / CRC 校验失败）
            logger.warning("sitemap 解析失败 %s: %s", response.url, exc)
            return
        root_tag = etree.QName(root).localname
        if root_tag == "sitemapindex":
            for loc in _iter_child_locs(root, "sitemap"):
                yield Request(url=loc, callback=_PARSE_SITEMAP)
        elif root_tag == "urlset":
            for loc in _iter_child_locs(root, "url"):
                callback = self._callback_for(loc)
                if callback is not None:
                    yield Request(url=loc, callback=callback)
        else:
            logger.warning("未知 sitemap 根元素 %r（%s），跳过该种子", root_tag, response.url)


__all__ = ["SitemapSpider"]

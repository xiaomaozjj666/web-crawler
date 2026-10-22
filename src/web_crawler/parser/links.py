r"""Scrapy 风格的 :class:`LinkExtractor` —— 超链接提取与过滤。

从 :class:`~web_crawler.response.Response`（复用其已缓存的解析树）或
裸 HTML 字符串提取 ``<a href>`` 超链接，产出
:class:`Link`（url + 文本 + 附带属性），并按构造参数过滤：

- ``allow`` / ``deny`` — URL 正则白/黑名单（``re.search`` 语义，deny 优先；
  空的 allow 表示全放行）；
- ``allow_domains`` / ``deny_domains`` — 主机名后缀匹配（``example.com``
  命中 ``sub.example.com``，与 :meth:`Spider.allowed
  <web_crawler.spider.Spider.allowed>` 同一语义）；
- ``canonicalize`` — URL 规范化（去 fragment、查询参数按段排序）；
- ``unique`` — 按规范化 URL 去重并保留首次出现顺序。

示例
----
>>> from web_crawler import LinkExtractor
>>> extractor = LinkExtractor(allow=(r"/page/\d+",))
>>> links = extractor.extract('<a href="/page/1">1</a>', base_url="https://x.com")
>>> links[0].url
'https://x.com/page/1'
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

from lxml import etree
from lxml import html as lxml_html

from ..exceptions import ConfigError

if TYPE_CHECKING:
    from ..response import Response


@dataclass
class Link:
    """超链接（url + 文本 + 附带属性）。

    ``url`` 是经 ``urljoin`` 绝对化（且 ``canonicalize=True`` 时规范化）
    后的地址；``attrs`` 携带 ``href`` 之外的原生属性（如 ``title``）。
    """

    url: str
    text: str = ""
    attrs: dict[str, str] = field(default_factory=dict)


def _canonicalize_url(url: str) -> str:
    """简化版 URL 规范化：去 fragment、查询参数按段排序。

    对齐 Scrapy w3lib ``canonicalize_url`` 的核心语义但刻意保守：查询串
    按 ``&`` 分段原样排序（不做百分号解码/重编码往返，避免 ``%20`` 被
    改写为 ``+`` 一类的编码漂移），也不做 host 大小写 / 默认端口等
    过激规范化——只做去重语义上必须的两件事。
    """
    parts = urlsplit(url)
    query = parts.query
    if query:
        query = "&".join(sorted(query.split("&")))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _compile_patterns(
    patterns: tuple[str, ...] | list[str], label: str
) -> tuple[re.Pattern[str], ...]:
    """编译正则串元组；非法模式抛 :class:`ConfigError`（一次性校验）。"""
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise ConfigError(
                f"LinkExtractor {label} contains invalid regex {pattern!r}: {exc}"
            ) from exc
    return tuple(compiled)


def _hostname(url: str) -> str:
    """提取 URL 的 host（小写；缺失或畸形 URL 返回空串）。"""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _domain_matches(host: str, domain: str) -> bool:
    """主机名后缀匹配：``example.com`` 命中 ``sub.example.com``。

    与 :meth:`Spider.allowed <web_crawler.spider.Spider.allowed>` 的判定
    完全同源（相等或以 ``.`` 前缀结尾），保证 LinkExtractor 的域过滤
    与 spider 引擎的 allowed_domains 过滤语义一致。
    """
    d = domain.lower()
    return host == d or host.endswith("." + d)


class LinkExtractor:
    """从 Response/HTML 提取超链接并过滤（对标 Scrapy LinkExtractor）。

    Parameters
    ----------
    allow:
        URL 正则白名单（``re.search`` 语义，空 = 全放行）。
    deny:
        URL 正则黑名单（优先于 ``allow``）。
    allow_domains:
        域白名单（空 = 全放行；后缀匹配命中子域）。
    deny_domains:
        域黑名单（优先于 ``allow_domains``）。
    unique:
        按规范化后的 url 去重，保留首次出现顺序。
    canonicalize:
        URL 规范化（去 fragment、查询参数排序）；去重键与
        :class:`Link.url` 均使用规范化后的值。
    strip:
        链接文本做 strip + 空白折叠（``"  a  b "`` → ``"a b"``）。
    """

    def __init__(
        self,
        *,
        allow: tuple[str, ...] | list[str] = (),
        deny: tuple[str, ...] | list[str] = (),
        allow_domains: tuple[str, ...] | list[str] = (),
        deny_domains: tuple[str, ...] | list[str] = (),
        unique: bool = True,
        canonicalize: bool = True,
        strip: bool = True,
    ) -> None:
        # 正则在构造期编译一次并缓存（提取热路径只做 search），
        # 非法模式在此刻即抛 ConfigError 而非首次提取时。
        self.allow = _compile_patterns(allow, "allow")
        self.deny = _compile_patterns(deny, "deny")
        self.allow_domains = tuple(allow_domains)
        self.deny_domains = tuple(deny_domains)
        self.unique = unique
        self.canonicalize = canonicalize
        self.strip = strip

    def extract(self, response_or_html: Response | str, base_url: str = "") -> list[Link]:
        """提取并过滤超链接。

        ``response_or_html`` 支持两种输入：

        - :class:`~web_crawler.response.Response` — 复用其惰性缓存的选择器
          树（``response.selector.element``），基准 URL 取 ``response.url``；
        - ``str`` — 直接按 HTML 解析，基准 URL 取 ``base_url`` 参数
          （缺省空串时相对链接无法绝对化，按原样保留）。
        """
        if isinstance(response_or_html, str):
            base = base_url
            html = response_or_html
            # 空白文档直接短路：lxml_html.fromstring("") 会抛 ParserError
            if not html.strip():
                return []
            root: etree._Element = lxml_html.fromstring(html)
        else:
            base = response_or_html.url
            root = response_or_html.selector.element

        links: list[Link] = []
        seen: set[str] = set()
        for anchor in root.iter("a"):
            href = anchor.get("href")
            if href is None:
                continue
            url = urljoin(base, href) if base else href
            if self.canonicalize:
                url = _canonicalize_url(url)
            if not self._url_allowed(url):
                continue
            if self.unique and url in seen:
                continue
            seen.add(url)
            text = "".join(anchor.itertext())
            if self.strip:
                text = " ".join(text.split())
            attrs = {k: v for k, v in anchor.attrib.items() if k != "href"}
            links.append(Link(url=url, text=text, attrs=attrs))
        return links

    def _url_allowed(self, url: str) -> bool:
        """单条 URL 的四层过滤：deny > allow > deny_domains > allow_domains。"""
        if any(p.search(url) for p in self.deny):
            return False
        if self.allow and not any(p.search(url) for p in self.allow):
            return False
        if self.deny_domains or self.allow_domains:
            host = _hostname(url)
            if not host:
                return False
            if any(_domain_matches(host, d) for d in self.deny_domains):
                return False
            if self.allow_domains and not any(_domain_matches(host, d) for d in self.allow_domains):
                return False
        return True


__all__ = ["Link", "LinkExtractor"]

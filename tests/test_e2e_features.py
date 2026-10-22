"""0.6.0/0.7.0 新功能的真实网络冒烟测试（e2e，全部 ``@pytest.mark.slow``）。

download / sitemap / feed / validation / CrawlSpider 等新功能已有大量 mock
单元测试，本文件补齐**真实网络**端到端冒烟：验证「库层 → DNS → TLS →
HTTP → 解析」完整链路在真实公网目标上走得通。

稳定性策略（nightly e2e job 无 continue-on-error，稳定压倒一切）：
- 目标只选极稳官方域：``https://example.com``（IANA 官方页，文案固定）、
  ``https://www.iana.org/``（真实链接结构）、``https://docs.python.org/
  sitemap.xml``（PSF 托管的合法 sitemap）；
- 刻意回避 httpbin.org / httpbingo.org（历史高频抖动）；
- 每个测试开头做 TCP 探测（socket 连 443，5s 超时，结果模块级缓存共享），
  不可达即 ``pytest.skip``——无网是环境限制而非缺陷（skip 而非 xfail），
  本地离线跑不炸，nightly（有网）真实执行；
- 单测试控制在 30s 内：``max_requests`` 限流、CrawlSpider 浅遍历
  （``follow=False`` 只跟一层）；
- 产物一律落 ``tmp_path``，不污染仓库；
- 2026-10 实测 example.com 已改版为极简页（无 ``<h1>`` / ``<a>`` 标签），
  结构断言改用固定的 ``<title>Example Domain</title>``；
- IANA robots.txt 响应头无 ``Accept-Ranges: bytes``（实测），断点续传
  断言按服务器能力条件执行，不支持时静默跳过该子断言。
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from web_crawler import (
    AsyncFetcher,
    CacheMiddleware,
    CrawlSpider,
    Fetcher,
    LinkExtractor,
    Response,
    RobotsPolicy,
    Rule,
    SitemapSpider,
    Spider,
    ValidationPipeline,
)
from web_crawler.robots import fetch_robots_text

# -- 网络目标常量 ------------------------------------------------------------

EXAMPLE_URL = "https://example.com"
IANA_HOME_URL = "https://www.iana.org/"
IANA_ROBOTS_URL = "https://www.iana.org/robots.txt"
# PSF 托管的稳定 sitemap（2026-10 实测 200 + 合法 urlset；python.org 根域
# 的 /sitemap.xml 已 404，docs 子域可用）
PY_SITEMAP_URL = "https://docs.python.org/sitemap.xml"

# -- 网络不可达守卫（模块级缓存：同一 host 全模块只探测一次） -----------------

_REACHABLE: dict[str, bool] = {}


def _skip_if_unreachable(host: str, port: int = 443, timeout: float = 5.0) -> None:
    """探测 ``host:port`` 的 TCP 连通性，不可达则 skip（环境限制非缺陷）。"""
    key = f"{host}:{port}"
    if key not in _REACHABLE:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                _REACHABLE[key] = True
        except OSError:
            _REACHABLE[key] = False
    if not _REACHABLE[key]:
        pytest.skip(f"网络不可达: {host}:{port}（环境限制，非缺陷）")


# -- 1. Fetcher 真实抓取冒烟 ---------------------------------------------------


@pytest.mark.slow
def test_e2e_fetcher_get_smoke() -> None:
    """Fetcher 真实抓取 example.com：status 200 + 固定文案 + CSS 选择器可用。

    库层端到端走通：curl_cffi/httpx 会话 → DNS → TLS → HTTP → Response
    归一化 → lxml 选择器解析。
    """
    _skip_if_unreachable("example.com")
    fetcher = Fetcher()
    try:
        resp = fetcher.get(EXAMPLE_URL)
    finally:
        fetcher.close()
    assert resp.status == 200
    assert "Example Domain" in resp.text, "正文应包含 IANA 固定文案"
    # 2026-10 实测页面已改版为极简页（无 h1/a），title 仍几十年固定
    titles = resp.css("title")
    assert titles, "CSS 选择器应能命中 <title>"
    assert titles[0].text == "Example Domain"


# -- 2. download() 真实流式下载 -------------------------------------------------


@pytest.mark.slow
def test_e2e_download_streaming_and_idempotent(tmp_path: Path) -> None:
    """download() 真实流式下载 iana robots.txt：落盘 + 幂等重下 + 条件续传。

    验证点：
    - 文件存在且内容非空（含 robots 标准头）；
    - 返回值为绝对 Path；
    - 幂等性：下载完成后再次 download 同 URL 同 dest（``.part`` 已被
      replace 不存在）→ 覆盖成功且内容一致；
    - 服务器支持 ``Accept-Ranges: bytes`` 时额外验证 ``resume=True``
      二次下载内容不变（IANA 实测不支持，该子断言按能力条件执行）。
    """
    _skip_if_unreachable("www.iana.org")
    dest = tmp_path / "iana-robots.txt"
    captured_headers: dict[str, str] = {}

    def on_headers(status: int, headers: Mapping[str, str]) -> None:
        for key, value in headers.items():
            captured_headers[key.lower()] = value

    fetcher = Fetcher()
    try:
        path = fetcher.download(IANA_ROBOTS_URL, dest, on_headers=on_headers)
        first_content = dest.read_bytes()
        # 幂等性：同 URL 同 dest 二次下载（.part 已被原子替换不存在）
        fetcher.download(IANA_ROBOTS_URL, dest)
        assert dest.read_bytes() == first_content, "幂等重下后内容应一致"
        # 条件续传：仅当服务器声明支持 Range 时验证 resume 路径
        if "bytes" in captured_headers.get("accept-ranges", "").lower():
            resumed = fetcher.download(IANA_ROBOTS_URL, dest, resume=True)
            assert resumed.read_bytes() == first_content, "resume 二次下载内容应不变"
    finally:
        fetcher.close()

    assert path.is_absolute(), "download 应返回绝对路径"
    assert path.exists()
    assert first_content, "下载内容应非空"
    assert b"User-agent" in first_content, "robots.txt 应含标准 User-agent 头"


# -- 3. Spider + feed_output 端到端 ---------------------------------------------


@pytest.mark.slow
def test_e2e_spider_feed_output(tmp_path: Path) -> None:
    """单页 Spider + feed_output JSONL 端到端：run() 落盘且行数与 item 数一致。"""
    _skip_if_unreachable("example.com")

    class TitleSpider(Spider):
        name = "e2e_title"
        start_urls = [EXAMPLE_URL]

        def parse(self, response: Response) -> Iterator[dict[str, Any]]:
            title = response.css_first("title")
            yield {"url": response.url, "title": title.text if title else ""}

    feed = tmp_path / "items.jsonl"
    fetcher = Fetcher()
    try:
        spider = TitleSpider(fetcher, feed_output=feed)
        items = spider.run()
    finally:
        fetcher.close()

    assert len(items) == 1
    lines = feed.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(items) == 1, "feed 行数应等于 item 数"
    record = json.loads(lines[0])
    # example.com 会 301 到带尾斜杠的规范化 URL
    assert record["url"].rstrip("/") == EXAMPLE_URL
    assert record["title"] == "Example Domain"


# -- 4. ValidationPipeline 真实数据 ---------------------------------------------


@pytest.mark.slow
def test_e2e_validation_pipeline() -> None:
    """ValidationPipeline 真实数据校验：好 item 通过、坏 item 被丢弃计数。"""
    _skip_if_unreachable("example.com")

    class ValidatingSpider(Spider):
        name = "e2e_validation"
        start_urls = [EXAMPLE_URL]
        item_pipelines = [
            ValidationPipeline(
                {"url": {"type": str}, "title": {"type": str}},
            )
        ]

        def parse(self, response: Response) -> Iterator[dict[str, Any]]:
            title = response.css_first("title")
            # 好 item：字段齐全类型正确
            yield {"url": response.url, "title": title.text if title else ""}
            # 坏 item：缺失必填 url 且 title 类型错误 → 应被 DropItem 丢弃
            yield {"title": 123}

    fetcher = Fetcher()
    try:
        spider = ValidatingSpider(fetcher)
        items = spider.run()
    finally:
        fetcher.close()

    assert spider.stats.items_scraped == 1
    assert spider.stats.items_dropped == 1, "坏 item 应被校验管道丢弃"
    assert len(items) == 1
    assert items[0]["title"] == "Example Domain"


# -- 5. LinkExtractor 真实页面 ---------------------------------------------------


@pytest.mark.slow
def test_e2e_link_extractor_real_page() -> None:
    """LinkExtractor 提取 iana.org 首页真实链接：非空 + 绝对化 + 去重生效。"""
    _skip_if_unreachable("www.iana.org")
    fetcher = Fetcher()
    try:
        resp = fetcher.get(IANA_HOME_URL)
    finally:
        fetcher.close()

    links = LinkExtractor().extract(resp)
    assert links, "iana 首页应能提取出真实链接"
    urls = [link.url for link in links]
    # urljoin 绝对化生效：全部为绝对 URL
    assert all(url.startswith(("http://", "https://")) for url in urls), (
        "提取的链接应全部为绝对 URL"
    )
    # unique 去重生效：无重复
    assert len(urls) == len(set(urls)), "unique 去重后不应有重复链接"


# -- 6. CrawlSpider 浅遍历 ------------------------------------------------------


@pytest.mark.slow
def test_e2e_crawl_spider_shallow() -> None:
    """CrawlSpider 规则驱动浅遍历 iana.org：限域 + 只跟一层 + 限流不超时。"""
    _skip_if_unreachable("www.iana.org")

    class IanaShallowSpider(CrawlSpider):
        name = "e2e_iana_shallow"
        start_urls = [IANA_HOME_URL]
        allowed_domains = ["iana.org"]
        rules = [
            Rule(
                LinkExtractor(allow_domains=("iana.org",)),
                callback="parse_page",
                follow=False,  # 只跟一层，防止深爬超时
            ),
        ]

        def parse_page(self, response: Response) -> Iterator[dict[str, Any]]:
            yield {"url": response.url, "status": response.status}

    fetcher = Fetcher()
    try:
        spider = IanaShallowSpider(fetcher)
        items = spider.run(max_requests=5)
    finally:
        fetcher.close()

    assert spider.stats.pages_crawled > 1, "浅遍历应至少抓取种子页 + 1 个跟随页"
    assert spider.stats.pages_crawled <= 5, "max_requests 限流应生效"
    assert items, "跟随页应产出 item"


# -- 7. SitemapSpider 真实 sitemap 驱动 ------------------------------------------


class _SitemapDocMixin:
    """SitemapSpider 回调混入：从 loc 页面提取标题。"""

    def parse_doc(self, response: Response) -> Iterator[dict[str, Any]]:
        title = response.css_first("title")
        yield {"url": response.url, "title": title.text if title else ""}


class _ExplicitSitemapSpider(_SitemapDocMixin, SitemapSpider):
    """显式 sitemap 种子模式。"""

    name = "e2e_sitemap_explicit"
    sitemap_urls = [PY_SITEMAP_URL]
    sitemap_rules = [(r"", "parse_doc")]


class _DiscoverySitemapSpider(_SitemapDocMixin, SitemapSpider):
    """robots.txt ``Sitemap:`` 行自动发现种子模式。"""

    name = "e2e_sitemap_discovery"
    start_urls = [IANA_HOME_URL]
    respect_robots = True
    sitemap_rules = [(r"", "parse_doc")]


@pytest.mark.slow
def test_e2e_sitemap_spider() -> None:
    """SitemapSpider 真实 sitemap.xml 驱动爬取：种子请求发出 + loc 页面抓取。

    种子选择策略（不能因目标站点无 sitemap 让 nightly 红）：
    - 先看 iana robots.txt 有无 ``Sitemap:`` 行：有则优先验证自动发现路径；
    - 无（2026-10 实测如此）→ 回退知名稳定 sitemap（docs.python.org）；
      现场探测不可用（非 200 / 非 sitemap XML）则 skip。
    """
    _skip_if_unreachable("www.iana.org")
    _skip_if_unreachable("docs.python.org")

    fetcher = Fetcher()
    try:
        robots_resp = fetcher.get(IANA_ROBOTS_URL)
        sitemap_rows = [
            line.split(":", 1)[1].strip()
            for line in robots_resp.text.splitlines()
            if line.lower().startswith("sitemap:")
        ]
        if not sitemap_rows:
            probe = fetcher.get(PY_SITEMAP_URL)
            if probe.status != 200 or not (
                b"<urlset" in probe.content[:2000] or b"<sitemapindex" in probe.content[:2000]
            ):
                pytest.skip(f"sitemap 目标不可用（status={probe.status}），非缺陷")
        spider_cls = _DiscoverySitemapSpider if sitemap_rows else _ExplicitSitemapSpider
        spider = spider_cls(fetcher)
        items = spider.run(max_requests=5)
    finally:
        fetcher.close()

    assert spider.stats.pages_crawled > 0, "sitemap 种子请求应被真实抓取"
    assert spider.stats.pages_crawled <= 5, "max_requests 限流应生效"
    assert items, "sitemap <loc> 页面应产出 item"


# -- 8. AsyncFetcher 真实异步 ----------------------------------------------------


@pytest.mark.slow
async def test_e2e_async_fetcher_real() -> None:
    """AsyncFetcher 真实异步抓取：``async with`` + ``await get`` 端到端 200。"""
    _skip_if_unreachable("example.com")
    async with AsyncFetcher() as fetcher:
        resp = await fetcher.get(EXAMPLE_URL)
    assert resp.status == 200
    assert "Example Domain" in resp.text


# -- 9. CacheMiddleware 真实缓存 --------------------------------------------------


@pytest.mark.slow
def test_e2e_cache_middleware_real(tmp_path: Path) -> None:
    """CacheMiddleware 真实缓存：第一遍落盘 miss，第二遍命中不发网络请求。"""
    _skip_if_unreachable("example.com")
    cache = CacheMiddleware(cache_dir=tmp_path / "cache", ttl=3600)

    class CachedTitleSpider(Spider):
        name = "e2e_cached_title"
        start_urls = [EXAMPLE_URL]
        middlewares = [cache]

        def parse(self, response: Response) -> Iterator[dict[str, Any]]:
            title = response.css_first("title")
            yield {"title": title.text if title else ""}

    fetcher = Fetcher()
    try:
        first = CachedTitleSpider(fetcher).run()
        second = CachedTitleSpider(fetcher).run()
    finally:
        fetcher.close()

    stats = cache.stats()
    assert stats["misses"] == 1, "第一遍应未命中缓存"
    assert stats["hits"] == 1, "第二遍应命中缓存"
    assert stats["entries"] == 1, "缓存目录应恰好有一个条目"
    assert first == second == [{"title": "Example Domain"}], "两遍产出应一致"


# -- 10. robots 真实解析 ----------------------------------------------------------


@pytest.mark.slow
def test_e2e_robots_policy_real() -> None:
    """RobotsPolicy 真实拉取解析 iana robots.txt + Spider respect_robots 真实路径。"""
    _skip_if_unreachable("www.iana.org")
    policy = RobotsPolicy(user_agent="*")
    # iana robots.txt 为「User-agent: * / Disallow:」（全允许），标准库默认
    # 拉取函数（urllib）真实外呼后解析不炸、语义正确
    assert policy.allowed(IANA_HOME_URL, fetch_robots_text) is True
    # 按主机缓存生效：同主机第二次查询不再外呼 robots.txt
    assert policy.allowed("https://www.iana.org/about", fetch_robots_text) is True
    delay = policy.crawl_delay(IANA_HOME_URL, fetch_robots_text)
    assert delay is None or delay >= 0, "Crawl-delay 解析不应返回负值"

    # Spider respect_robots=True 的真实路径：robots 全允许 → 正常抓取一页
    class RobotsRespectSpider(Spider):
        name = "e2e_robots_respect"
        start_urls = [IANA_HOME_URL]
        respect_robots = True

        def parse(self, response: Response) -> Iterator[dict[str, Any]]:
            yield {"url": response.url}

    fetcher = Fetcher()
    try:
        spider = RobotsRespectSpider(fetcher)
        items = spider.run(max_requests=1)
    finally:
        fetcher.close()

    assert spider.stats.pages_crawled == 1
    assert items == [{"url": IANA_HOME_URL}]

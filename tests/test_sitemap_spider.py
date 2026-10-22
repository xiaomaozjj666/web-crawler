"""SitemapSpider 的测试（内存 FakeFetcher，无网络）。

覆盖：urlset 解析与 sitemap_rules 分发、sitemapindex 递归（含嵌套
index 与无命名空间变体）、sitemap_max_depth 递归上限（链式 index
超限 warning 停止）、内部实体不展开（billion laughs 防护）、robots.txt
的 Sitemap: 行自动发现播种（含无 Sitemap 行 warning 与同域只拉一次）、
全不命中丢弃 + 空规则 warning、callback=None 显式排除、gzip sitemap
解压（含损坏 gzip 容错）、非 XML / 未知根元素容错、feed_output 落盘
端到端、种子 / 正则两处构造期 ConfigError。
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from web_crawler import ConfigError, Response, SitemapSpider

HOST = "shop.example.com"
BASE = f"https://{HOST}"

# 标准 urlset：一条博客页、一条商店页、一条不命中任何规则的杂项页
URLSET = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{BASE}/blog/1</loc></url>
  <url><loc>{BASE}/blog/2</loc></url>
  <url><loc>{BASE}/shop/1</loc></url>
  <url><loc>{BASE}/misc/1</loc></url>
</urlset>""".encode()

# 两层 sitemapindex：顶层 index → 子 index（blog）→ urlset；另一分支
# 子 index（shop）刻意不带命名空间（现实中存在的畸形站点）
INDEX = f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{BASE}/sitemap-blog.xml</loc></sitemap>
  <sitemap><loc>{BASE}/sitemap-shop.xml</loc></sitemap>
</sitemapindex>""".encode()

NESTED_INDEX_BLOG = f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{BASE}/sitemap-blog-posts.xml</loc></sitemap>
</sitemapindex>""".encode()

URLSET_BLOG_POSTS = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{BASE}/blog/1</loc></url>
  <url><loc>{BASE}/blog/2</loc></url>
</urlset>""".encode()

# 无命名空间的 urlset（localname 匹配照样工作）
URLSET_SHOP_NO_NS = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset>
  <url><loc>{BASE}/shop/1</loc></url>
</urlset>""".encode()


class FakeFetcher:
    """返回按 URL 预置的内容，无网络；记录调用顺序。"""

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


def test_urlset_rules_dispatch() -> None:
    """urlset 的 <loc> 按声明顺序匹配规则，命中 URL 走对应回调、未命中丢弃。"""
    pages = {
        f"{BASE}/sitemap.xml": URLSET,
        f"{BASE}/blog/1": b"<html></html>",
        f"{BASE}/blog/2": b"<html></html>",
        f"{BASE}/shop/1": b"<html></html>",
    }

    class S(SitemapSpider):
        name = "rules"
        sitemap_urls = [f"{BASE}/sitemap.xml"]
        sitemap_rules = [(r"/blog/", "parse_blog"), (r"/shop/", "parse_shop")]

        def parse_blog(self, response: Response) -> Any:
            yield {"cb": "blog", "url": response.url}

        def parse_shop(self, response: Response) -> Any:
            yield {"cb": "shop", "url": response.url}

    fetcher = FakeFetcher(pages)
    items = S(fetcher=fetcher).run()
    # 文档序入队（同优先级 FIFO）：两条 blog 在前、一条 shop 在后；
    # /misc/1 不命中任何规则被丢弃，不会发出请求
    assert items == [
        {"cb": "blog", "url": f"{BASE}/blog/1"},
        {"cb": "blog", "url": f"{BASE}/blog/2"},
        {"cb": "shop", "url": f"{BASE}/shop/1"},
    ]
    assert f"{BASE}/misc/1" not in fetcher.calls
    assert fetcher.calls[0] == f"{BASE}/sitemap.xml"


def test_sitemapindex_recurses_into_child_sitemaps() -> None:
    """sitemapindex 递归展开：index → 子 index → urlset，逐层入队抓取。"""
    pages = {
        f"{BASE}/sitemap.xml": INDEX,
        f"{BASE}/sitemap-blog.xml": NESTED_INDEX_BLOG,
        f"{BASE}/sitemap-blog-posts.xml": URLSET_BLOG_POSTS,
        f"{BASE}/sitemap-shop.xml": URLSET_SHOP_NO_NS,
        f"{BASE}/blog/1": b"<html></html>",
        f"{BASE}/blog/2": b"<html></html>",
        f"{BASE}/shop/1": b"<html></html>",
    }

    class S(SitemapSpider):
        name = "nested"
        sitemap_urls = [f"{BASE}/sitemap.xml"]
        sitemap_rules = [(r"/blog/", "parse_blog"), (r"/shop/", "parse_shop")]

        def parse_blog(self, response: Response) -> Any:
            yield {"cb": "blog", "url": response.url}

        def parse_shop(self, response: Response) -> Any:
            yield {"cb": "shop", "url": response.url}

    fetcher = FakeFetcher(pages)
    items = S(fetcher=fetcher).run()
    # sitemap 抓取链按入队序（heap counter）：顶层 index → blog 子 index →
    # shop 子 index（无命名空间）→ blog-posts urlset（嵌套 index 晚一步入队）
    sitemap_calls = [u for u in fetcher.calls if "sitemap" in u]
    assert sitemap_calls == [
        f"{BASE}/sitemap.xml",
        f"{BASE}/sitemap-blog.xml",
        f"{BASE}/sitemap-shop.xml",
        f"{BASE}/sitemap-blog-posts.xml",
    ]
    # loc 请求同样按入队序：shop/1（shop 分支先完成展开）先于 blog/1、blog/2
    assert items == [
        {"cb": "shop", "url": f"{BASE}/shop/1"},
        {"cb": "blog", "url": f"{BASE}/blog/1"},
        {"cb": "blog", "url": f"{BASE}/blog/2"},
    ]


def test_robots_sitemap_line_autodiscovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """sitemap_urls 为空且 respect_robots=True 时从 robots.txt 的 Sitemap: 行播种。"""
    robots_text = (
        "User-agent: *\n"
        "Disallow: /private\n"
        f"Sitemap: {BASE}/sitemap.xml\n"
        "# Sitemap: https://commented-out.example/s.xml\n"
    )
    monkeypatch.setattr(
        "web_crawler.spider.spider.fetch_robots_text",
        lambda url, timeout=10.0: robots_text,
    )
    pages = {
        f"{BASE}/sitemap.xml": URLSET,
        f"{BASE}/blog/1": b"<html></html>",
        f"{BASE}/blog/2": b"<html></html>",
    }

    class S(SitemapSpider):
        name = "robots_seed"
        start_urls = [f"{BASE}/"]
        respect_robots = True
        sitemap_rules = [(r"/blog/", "parse_blog")]

        def parse_blog(self, response: Response) -> Any:
            yield {"url": response.url}

    fetcher = FakeFetcher(pages)
    items = S(fetcher=fetcher).run()
    # robots 发现的 sitemap 是第一个被抓取的 URL；注释掉的 Sitemap 行
    # 不播种；robots 的 Disallow /private 不影响 sitemap 与 blog 页
    assert fetcher.calls[0] == f"{BASE}/sitemap.xml"
    assert items == [{"url": f"{BASE}/blog/1"}, {"url": f"{BASE}/blog/2"}]


def test_unmatched_urls_dropped_and_empty_rules_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """sitemap_rules 为空时构造期 warning，且全部 <loc> 被丢弃（无 loc 请求）。"""
    pages = {f"{BASE}/sitemap.xml": URLSET}

    class S(SitemapSpider):
        name = "no_rules"
        sitemap_urls = [f"{BASE}/sitemap.xml"]
        sitemap_rules = []

        def parse(self, response: Response) -> Any:
            yield {"url": response.url}

    fetcher = FakeFetcher(pages)
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.sitemap"):
        spider = S(fetcher=fetcher)
        items = spider.run()
    # 只抓了 sitemap 本身；没有任何 <loc> 页面请求、没有任何 item
    assert items == []
    assert fetcher.calls == [f"{BASE}/sitemap.xml"]
    assert any("sitemap_rules" in rec.getMessage() for rec in caplog.records)


def test_none_callback_rule_drops_matching_urls() -> None:
    """callback=None 的规则显式排除命中 URL（白名单末尾兜底手法）。"""
    pages = {
        f"{BASE}/sitemap.xml": URLSET,
        f"{BASE}/blog/1": b"<html></html>",
    }

    class S(SitemapSpider):
        name = "whitelist"
        sitemap_urls = [f"{BASE}/sitemap.xml"]
        # 空正则匹配一切：未被前面规则命中的 URL 到此被显式排除
        sitemap_rules = [(r"/blog/1$", "parse_blog"), (r"", None)]

        def parse_blog(self, response: Response) -> Any:
            yield {"url": response.url}

    fetcher = FakeFetcher(pages)
    items = S(fetcher=fetcher).run()
    # 仅 blog/1 命中第一条规则；blog/2 / shop/1 / misc/1 命中第二条
    # 但 callback=None 被丢弃
    assert items == [{"url": f"{BASE}/blog/1"}]
    assert fetcher.calls == [f"{BASE}/sitemap.xml", f"{BASE}/blog/1"]


def test_gzip_sitemap_content_is_decompressed() -> None:
    """gzip 压缩的 sitemap 内容被自动解压后正常解析分发。"""
    pages = {
        f"{BASE}/sitemap.xml.gz": gzip.compress(URLSET),
        f"{BASE}/blog/1": b"<html></html>",
    }

    class S(SitemapSpider):
        name = "gz"
        sitemap_urls = [f"{BASE}/sitemap.xml.gz"]
        sitemap_rules = [(r"/blog/1$", "parse_blog"), (r"", None)]

        def parse_blog(self, response: Response) -> Any:
            yield {"url": response.url}

    fetcher = FakeFetcher(pages)
    items = S(fetcher=fetcher).run()
    assert items == [{"url": f"{BASE}/blog/1"}]
    assert fetcher.calls == [f"{BASE}/sitemap.xml.gz", f"{BASE}/blog/1"]


def test_sitemap_spider_with_feed_output_end_to_end(tmp_path: Path) -> None:
    """SitemapSpider + feed_output：loc 页面 item 落盘 JSONL 与 run 返回一致。"""
    pages = {
        f"{BASE}/sitemap.xml": URLSET,
        f"{BASE}/blog/1": b"<html></html>",
        f"{BASE}/blog/2": b"<html></html>",
        f"{BASE}/shop/1": b"<html></html>",
    }

    class S(SitemapSpider):
        name = "feed_sitemap"
        sitemap_urls = [f"{BASE}/sitemap.xml"]
        sitemap_rules = [(r"/blog/", "parse_blog"), (r"/shop/", "parse_shop")]

        def parse_blog(self, response: Response) -> Any:
            yield {"kind": "blog", "url": response.url, "status": response.status}

        def parse_shop(self, response: Response) -> Any:
            yield {"kind": "shop", "url": response.url, "status": response.status}

    path = tmp_path / "feed.jsonl"
    spider = S(fetcher=FakeFetcher(pages), feed_output=path)
    items = spider.run()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    expected = [
        {"kind": "blog", "url": f"{BASE}/blog/1", "status": 200},
        {"kind": "blog", "url": f"{BASE}/blog/2", "status": 200},
        {"kind": "shop", "url": f"{BASE}/shop/1", "status": 200},
    ]
    assert items == expected
    assert lines == expected
    assert spider.stats.items_scraped == 3


def test_config_error_when_no_seeds() -> None:
    """无任何种子来源（显式与自动发现均不可用）时构造期抛 ConfigError。"""

    class Empty(SitemapSpider):
        name = "empty"
        sitemap_urls = []
        start_urls = []

        def parse(self, response: Response) -> Any:
            return None

    with pytest.raises(ConfigError, match="sitemap_urls"):
        Empty(fetcher=FakeFetcher({}))

    # sitemap_urls 为空且未开 respect_robots：start_urls 无法转化为种子
    class NoRobots(SitemapSpider):
        name = "no_robots"
        sitemap_urls = []
        start_urls = [f"{BASE}/"]
        respect_robots = False

        def parse(self, response: Response) -> Any:
            return None

    with pytest.raises(ConfigError, match="respect_robots"):
        NoRobots(fetcher=FakeFetcher({}))


def test_config_error_invalid_regex() -> None:
    """sitemap_rules 含非法正则时构造期抛 ConfigError（快失败）。"""

    class S(SitemapSpider):
        name = "bad_regex"
        sitemap_urls = [f"{BASE}/sitemap.xml"]
        sitemap_rules = [("[", "parse")]

        def parse(self, response: Response) -> Any:
            return None

    with pytest.raises(ConfigError, match="sitemap_rules"):
        S(fetcher=FakeFetcher({}))


# ===========================================================================
# sitemapindex 递归上限（M-2）与实体不展开（M-3）
# ===========================================================================


def _index_xml(child: str) -> bytes:
    """构造指向 child 的单条目 sitemapindex 文档。"""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <sitemap><loc>{child}</loc></sitemap>\n"
        "</sitemapindex>"
    ).encode()


def test_sitemapindex_depth_limit_stops_recursion_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M-2：链式 index 递归受 sitemap_max_depth 约束，超限 warning 停止展开。"""
    # 3 层链式 index：L1（种子）→ L2 → L3 → 叶子 urlset
    pages = {
        f"{BASE}/sitemap-l1.xml": _index_xml(f"{BASE}/sitemap-l2.xml"),
        f"{BASE}/sitemap-l2.xml": _index_xml(f"{BASE}/sitemap-l3.xml"),
        f"{BASE}/sitemap-l3.xml": _index_xml(f"{BASE}/sitemap-leaf.xml"),
        f"{BASE}/sitemap-leaf.xml": URLSET_BLOG_POSTS,
        f"{BASE}/blog/1": b"<html></html>",
    }

    class S(SitemapSpider):
        name = "depth_limit"
        sitemap_urls = [f"{BASE}/sitemap-l1.xml"]
        sitemap_rules = [(r"/blog/", "parse_blog")]
        sitemap_max_depth = 2

        def parse_blog(self, response: Response) -> Any:
            yield {"url": response.url}

    fetcher = FakeFetcher(pages)
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.sitemap"):
        items = S(fetcher=fetcher).run()
    # 只抓到第 2 层（L1、L2）：第 3 层 L3 及叶子不被请求
    assert f"{BASE}/sitemap-l3.xml" not in fetcher.calls
    assert f"{BASE}/sitemap-leaf.xml" not in fetcher.calls
    assert fetcher.calls == [f"{BASE}/sitemap-l1.xml", f"{BASE}/sitemap-l2.xml"]
    assert items == []
    assert any("上限" in rec.getMessage() for rec in caplog.records)


def test_sitemapindex_depth_counts_from_seed_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M-2 补充：默认上限 10 下正常嵌套（种子=第 1 层）不受影响。"""
    # 嵌套 index → urlset 两层结构是现实主流形态，默认上限不拦截
    pages = {
        f"{BASE}/sitemap.xml": INDEX,
        f"{BASE}/sitemap-blog.xml": NESTED_INDEX_BLOG,
        f"{BASE}/sitemap-blog-posts.xml": URLSET_BLOG_POSTS,
        f"{BASE}/blog/1": b"<html></html>",
        f"{BASE}/blog/2": b"<html></html>",
    }

    class S(SitemapSpider):
        name = "depth_default"
        sitemap_urls = [f"{BASE}/sitemap.xml"]
        sitemap_rules = [(r"/blog/", "parse_blog")]

        def parse_blog(self, response: Response) -> Any:
            yield {"url": response.url}

    fetcher = FakeFetcher(pages)
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.sitemap"):
        items = S(fetcher=fetcher).run()
    assert [it["url"] for it in items] == [f"{BASE}/blog/1", f"{BASE}/blog/2"]
    # 三层嵌套（index → 子 index → urlset）未触发上限 warning
    assert not any("上限" in rec.getMessage() for rec in caplog.records)


def test_internal_entities_not_expanded(caplog: pytest.LogCaptureFixture) -> None:
    """M-3：内部实体不展开（billion laughs 防护），文本 loc 正常提取。"""
    # 嵌套内部实体：若解析器展开会指数放大内存；不展开时实体 loc 的
    # text 为 None，判空跳过即可
    entity_sitemap = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b"<!DOCTYPE urlset [\n"
        b'  <!ENTITY lol "lol">\n'
        b'  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
        b"]>\n"
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        b"  <url><loc>&lol2;</loc></url>\n"
        b"  <url><loc>https://shop.example.com/blog/1</loc></url>\n"
        b"</urlset>"
    )
    pages = {
        f"{BASE}/sitemap.xml": entity_sitemap,
        f"{BASE}/blog/1": b"<html></html>",
    }

    class S(SitemapSpider):
        name = "entities"
        sitemap_urls = [f"{BASE}/sitemap.xml"]
        sitemap_rules = [(r"/blog/", "parse_blog")]

        def parse_blog(self, response: Response) -> Any:
            yield {"url": response.url}

    fetcher = FakeFetcher(pages)
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.sitemap"):
        items = S(fetcher=fetcher).run()
    # 实体 loc 跳过、文本 loc 正常分发；不炸、不产生解析失败 warning
    assert items == [{"url": f"{BASE}/blog/1"}]
    assert not any("sitemap 解析失败" in rec.getMessage() for rec in caplog.records)


# ===========================================================================
# 解析容错：非 XML / 损坏 gzip / 未知根元素
# ===========================================================================


def test_non_xml_sitemap_warns_and_other_seeds_proceed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """非 XML 响应（截断的 HTML 404 页）：warning 跳过该种子，其余种子照常。"""
    pages = {
        # 未闭合标签的 HTML 错误页：XML 解析器直接抛语法错误
        f"{BASE}/sitemap-bad.xml": b"<html><body><h1>404 Not Found",
        f"{BASE}/sitemap-good.xml": URLSET,
        f"{BASE}/blog/1": b"<html></html>",
        f"{BASE}/blog/2": b"<html></html>",
    }

    class S(SitemapSpider):
        name = "non_xml"
        sitemap_urls = [f"{BASE}/sitemap-bad.xml", f"{BASE}/sitemap-good.xml"]
        sitemap_rules = [(r"/blog/", "parse_blog")]

        def parse_blog(self, response: Response) -> Any:
            yield {"url": response.url}

    fetcher = FakeFetcher(pages)
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.sitemap"):
        items = S(fetcher=fetcher).run()
    # 坏种子只 warning 不中断：好种子的 loc 照常分发
    assert items == [{"url": f"{BASE}/blog/1"}, {"url": f"{BASE}/blog/2"}]
    warnings = [
        rec.getMessage() for rec in caplog.records if "sitemap 解析失败" in rec.getMessage()
    ]
    assert len(warnings) == 1
    assert f"{BASE}/sitemap-bad.xml" in warnings[0]


def test_corrupt_gzip_sitemap_warns_without_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """损坏 gzip（magic 头对但内容坏）：warning 跳过，运行不中断。"""
    pages = {
        # gzip magic（\x1f\x8b）之后是垃圾数据：解压必败
        f"{BASE}/sitemap.xml.gz": b"\x1f\x8b" + b"\x00" * 16,
        f"{BASE}/blog/1": b"<html></html>",
    }

    class S(SitemapSpider):
        name = "corrupt_gz"
        sitemap_urls = [f"{BASE}/sitemap.xml.gz"]
        sitemap_rules = [(r"/blog/", "parse_blog")]

        def parse_blog(self, response: Response) -> Any:
            yield {"url": response.url}

    fetcher = FakeFetcher(pages)
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.sitemap"):
        items = S(fetcher=fetcher).run()
    assert items == []
    assert fetcher.calls == [f"{BASE}/sitemap.xml.gz"]
    assert any("sitemap 解析失败" in rec.getMessage() for rec in caplog.records)


def test_rss_root_element_warns_and_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """RSS 根元素（非 sitemap/sitemapindex）：warning 跳过该种子。"""
    rss = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<rss version="2.0"><channel><title>feed</title></channel></rss>'
    )
    pages = {f"{BASE}/sitemap.xml": rss}

    class S(SitemapSpider):
        name = "rss_root"
        sitemap_urls = [f"{BASE}/sitemap.xml"]
        sitemap_rules = [(r"", "parse")]

        def parse(self, response: Response) -> Any:
            yield {"url": response.url}

    fetcher = FakeFetcher(pages)
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.sitemap"):
        items = S(fetcher=fetcher).run()
    assert items == []
    assert any("未知 sitemap 根元素" in rec.getMessage() for rec in caplog.records)
    assert any("'rss'" in rec.getMessage() for rec in caplog.records)


# ===========================================================================
# robots 自动发现：无 Sitemap 行 warning 与同域只拉一次
# ===========================================================================


def test_robots_without_sitemap_line_warns_no_seeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """robots.txt 无任何 Sitemap: 行：warning 且无种子（无 sitemap 请求）。"""
    monkeypatch.setattr(
        "web_crawler.spider.spider.fetch_robots_text",
        lambda url, timeout=10.0: "User-agent: *\nDisallow: /private\n",
    )

    class S(SitemapSpider):
        name = "no_sitemap_line"
        start_urls = [f"{BASE}/"]
        respect_robots = True
        sitemap_rules = [(r"", "parse")]

        def parse(self, response: Response) -> Any:
            yield {"url": response.url}

    fetcher = FakeFetcher({})
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.sitemap"):
        items = S(fetcher=fetcher).run()
    assert items == []
    # 无种子：唯一的请求是 robots.txt 本身（经 RobotsPolicy），无 sitemap 抓取
    assert fetcher.calls == []
    assert any("无种子" in rec.getMessage() for rec in caplog.records)


def test_robots_fetched_once_for_same_host_start_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个同域 start_urls：robots.txt 只拉取一次（按主机缓存共享）。"""
    robots_calls: list[str] = []

    def fake_robots(url: str, timeout: float = 10.0) -> str:
        robots_calls.append(url)
        return f"Sitemap: {BASE}/sitemap.xml\n"

    monkeypatch.setattr("web_crawler.spider.spider.fetch_robots_text", fake_robots)
    pages = {
        f"{BASE}/sitemap.xml": URLSET_BLOG_POSTS,
        f"{BASE}/blog/1": b"<html></html>",
        f"{BASE}/blog/2": b"<html></html>",
    }

    class S(SitemapSpider):
        name = "same_host"
        start_urls = [f"{BASE}/", f"{BASE}/page2"]
        respect_robots = True
        sitemap_rules = [(r"/blog/", "parse_blog")]

        def parse_blog(self, response: Response) -> Any:
            yield {"url": response.url}

    items = S(fetcher=FakeFetcher(pages)).run()
    # 同域两条 start_urls 只触发一次 robots.txt 外呼
    assert robots_calls == [f"{BASE}/robots.txt"]
    assert items == [{"url": f"{BASE}/blog/1"}, {"url": f"{BASE}/blog/2"}]

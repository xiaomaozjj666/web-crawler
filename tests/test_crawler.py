"""Tests for the crawler."""

from urllib.robotparser import RobotFileParser

from web_crawler.crawler import Crawler


def test_extract_links_keeps_same_domain_only() -> None:
    html = (
        '<a href="/about">about</a>'
        '<a href="https://other.com/x">external</a>'
        '<a href="https://example.com/contact">contact</a>'
    )
    links = Crawler._extract_links("https://example.com/", html)
    assert "https://example.com/about" in links
    assert "https://example.com/contact" in links
    assert all("other.com" not in link for link in links)


def test_extract_links_handles_no_anchors() -> None:
    assert Crawler._extract_links("https://example.com/", "<p>hi</p>") == []


def test_extract_links_deduplicates() -> None:
    """同一 href 重复出现时只保留一次。"""
    html = '<a href="/a">a</a><a href="/a">a again</a>'
    links = Crawler._extract_links("https://example.com/", html)
    assert links == ["https://example.com/a"]


def test_extract_links_deduplicates_absolute_and_relative() -> None:
    """相对路径与绝对路径指向同一 URL 时去重。"""
    html = '<a href="/page">relative</a><a href="https://example.com/page">absolute</a>'
    links = Crawler._extract_links("https://example.com/", html)
    assert links == ["https://example.com/page"]


def test_can_fetch_no_robots_returns_true() -> None:
    crawler = Crawler(respect_robots=False)
    assert crawler.respect_robots is False


def test_robot_parser_default_allow() -> None:
    rp = RobotFileParser()
    rp.parse([])
    assert rp.can_fetch("*", "https://example.com/") is True


def test_robot_parser_block_all() -> None:
    rp = RobotFileParser()
    rp.parse(["User-agent: *", "Disallow: /"])
    assert rp.can_fetch("*", "https://example.com/") is False


# ===========================================================================
# _extract_links URL 规范化（Fix4）：fragment/大小写/默认端口/非 http scheme
# ===========================================================================


def test_extract_links_strips_fragments_and_dedups() -> None:
    """fragment 不同的链接归一后去重，页面内锚点不再产生重复 URL。"""
    html = '<a href="#section">s</a><a href="/page">p</a><a href="/page#x">px</a>'
    links = Crawler._extract_links("https://example.com/start", html)
    # 页面内锚点解析为去掉 fragment 的当前页；/page 与 /page#x 归一后去重
    assert links == ["https://example.com/start", "https://example.com/page"]


def test_extract_links_normalizes_case_and_default_port() -> None:
    """域名大小写与默认端口归一后去重；同 netloc 的 http/https 均保留。"""
    html = (
        '<a href="/Page">rel</a>'
        '<a href="HTTPS://EXAMPLE.com/Page">abs</a>'
        '<a href="https://example.com:443/Page">port</a>'
        '<a href="http://example.com/Page">http</a>'
    )
    links = Crawler._extract_links("https://example.com/", html)
    assert links == ["https://example.com/Page", "http://example.com/Page"]


def test_extract_links_keeps_non_default_port() -> None:
    """非默认端口（8080）在 base 中保留，链接不误归一。"""
    html = '<a href="/a">a</a><a href="http://example.com:8080/a">p8080</a>'
    links = Crawler._extract_links("http://example.com:8080/", html)
    assert links == ["http://example.com:8080/a"]


def test_extract_links_drops_non_http_schemes() -> None:
    """ftp/mailto/javascript 链接被丢弃；scheme 相对链接（//host）被收录。"""
    html = (
        '<a href="ftp://example.com/file">ftp</a>'
        '<a href="mailto:x@example.com">mail</a>'
        '<a href="javascript:void(0)">js</a>'
        '<a href="//example.com/ok">proto-rel</a>'
    )
    links = Crawler._extract_links("https://example.com/", html)
    assert links == ["https://example.com/ok"]


def test_extract_links_skips_invalid_port() -> None:
    """端口非法的链接被丢弃，其余链接不受影响。"""
    html = '<a href="https://example.com:bad/x">bad</a><a href="/ok">ok</a>'
    links = Crawler._extract_links("https://example.com/", html)
    assert links == ["https://example.com/ok"]

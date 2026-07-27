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

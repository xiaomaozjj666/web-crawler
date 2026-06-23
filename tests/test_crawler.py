"""Tests for the crawler link extraction."""

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

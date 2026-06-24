"""Tests for the crawler."""

from web_crawler.crawler import CrawlResult, Crawler


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


def test_crawl_visits_pages_and_dedupes(monkeypatch) -> None:
    pages = {
        "https://example.com/": '<a href="/a">a</a><a href="/b">b</a>',
        "https://example.com/a": '<a href="/">home</a>',
        "https://example.com/b": "<p>leaf</p>",
    }

    def fake_fetch(self: Crawler, url: str) -> CrawlResult:
        return CrawlResult(url=url, status_code=200, links=Crawler._extract_links(url, pages[url]))

    monkeypatch.setattr(Crawler, "fetch", fake_fetch)
    results = Crawler().crawl("https://example.com/")
    visited = {r.url for r in results}
    assert visited == set(pages)

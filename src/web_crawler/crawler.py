"""Core crawler logic."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


@dataclass
class CrawlResult:
    """Holds the outcome of crawling a single page."""

    url: str
    status_code: int
    links: list[str] = field(default_factory=list)


class Crawler:
    """A minimal, same-domain breadth-first web crawler."""

    def __init__(self, timeout: float = 10.0, user_agent: str = "web-crawler/0.1") -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    def fetch(self, url: str) -> CrawlResult:
        """Fetch a single URL and extract same-domain links."""
        response = self.session.get(url, timeout=self.timeout)
        links = self._extract_links(url, response.text)
        return CrawlResult(url=url, status_code=response.status_code, links=links)

    def crawl(self, start_url: str, max_pages: int = 50) -> list[CrawlResult]:
        """Crawl same-domain pages breadth-first up to ``max_pages``."""
        seen: set[str] = {start_url}
        queue: deque[str] = deque([start_url])
        results: list[CrawlResult] = []
        while queue and len(results) < max_pages:
            current = queue.popleft()
            try:
                result = self.fetch(current)
            except requests.RequestException:
                continue
            results.append(result)
            for link in result.links:
                if link not in seen:
                    seen.add(link)
                    queue.append(link)
        return results

    @staticmethod
    def _extract_links(base_url: str, html: str) -> list[str]:
        base_domain = urlparse(base_url).netloc
        soup = BeautifulSoup(html, "lxml")
        links: list[str] = []
        for anchor in soup.find_all("a", href=True):
            absolute = urljoin(base_url, str(anchor["href"]))
            if urlparse(absolute).netloc == base_domain:
                links.append(absolute)
        return links

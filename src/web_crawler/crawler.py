"""Core crawler logic with async concurrency and robots.txt support."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx


@dataclass
class CrawlResult:
    """Holds the outcome of crawling a single page."""

    url: str
    status_code: int
    links: list[str] = field(default_factory=list)
    error: str | None = None


class Crawler:
    """An async, same-domain breadth-first web crawler with robots.txt support."""

    def __init__(
        self,
        max_concurrency: int = 10,
        delay: float = 0.2,
        timeout: float = 15.0,
        user_agent: str = "web-crawler/1.0",
        respect_robots: bool = True,
    ) -> None:
        self.max_concurrency = max_concurrency
        self.delay = delay
        self.timeout = timeout
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        self._robot_parsers: dict[str, RobotFileParser] = {}
        self._last_fetch: float = 0.0

    async def _get_robot_parser(self, domain: str) -> RobotFileParser | None:
        rp = RobotFileParser()
        rp.set_url(f"https://{domain}/robots.txt")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"https://{domain}/robots.txt")
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                    return rp
        except Exception:
            pass
        return None

    async def _can_fetch(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        domain = parsed.netloc
        if domain not in self._robot_parsers:
            self._robot_parsers[domain] = await self._get_robot_parser(domain)
        rp = self._robot_parsers[domain]
        if rp is None:
            return True
        return rp.can_fetch(self.user_agent, url)

    async def fetch(self, client: httpx.AsyncClient, url: str) -> CrawlResult:
        """Fetch a single URL and extract same-domain links."""
        now = time.monotonic()
        since_last = now - self._last_fetch
        if since_last < self.delay:
            await asyncio.sleep(self.delay - since_last)
        self._last_fetch = time.monotonic()

        try:
            if not await self._can_fetch(url):
                return CrawlResult(url=url, status_code=403, error="Blocked by robots.txt")
            response = await client.get(url, timeout=self.timeout)
            links = self._extract_links(url, response.text)
            return CrawlResult(url=url, status_code=response.status_code, links=links)
        except httpx.TimeoutException:
            return CrawlResult(url=url, status_code=0, error="timeout")
        except httpx.HTTPStatusError as e:
            return CrawlResult(url=url, status_code=e.response.status_code, error=str(e))
        except Exception as e:
            return CrawlResult(url=url, status_code=0, error=str(e))

    async def crawl(
        self, start_url: str, max_pages: int = 50
    ) -> list[CrawlResult]:
        """Crawl same-domain pages breadth-first with async concurrency."""
        seen: set[str] = {start_url}
        queue: list[str] = [start_url]
        results: list[CrawlResult] = []
        sem = asyncio.Semaphore(self.max_concurrency)

        async def worker(url: str) -> None:
            async with sem:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    headers={"User-Agent": self.user_agent},
                    follow_redirects=True,
                ) as client:
                    result = await self.fetch(client, url)
            results.append(result)
            if result.error is None:
                for link in result.links:
                    if link not in seen:
                        seen.add(link)
                        queue.append(link)

        while queue and len(results) < max_pages:
            batch = queue[:max_pages - len(results)]
            del queue[:len(batch)]
            await asyncio.gather(*[worker(url) for url in batch])

        return results

    @staticmethod
    def _extract_links(base_url: str, html: str) -> list[str]:
        from bs4 import BeautifulSoup
        base_domain = urlparse(base_url).netloc
        soup = BeautifulSoup(html, "lxml")
        links: list[str] = []
        for anchor in soup.find_all("a", href=True):
            absolute = urljoin(base_url, str(anchor["href"]))
            if urlparse(absolute).netloc == base_domain:
                links.append(absolute)
        return links

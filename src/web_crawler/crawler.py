"""Core crawler logic with async concurrency and robots.txt support."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit
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
        # 值可能是已完成的 parser、进行中的 robots 拉取任务（single-flight）或 None
        self._robot_parsers: dict[
            str, RobotFileParser | asyncio.Task[RobotFileParser | None] | None
        ] = {}
        # 按域名记录上次请求时间，避免全局延迟误伤不同域名
        self._last_fetch: dict[str, float] = {}

    async def _get_robot_parser(self, scheme: str, domain: str) -> RobotFileParser | None:
        """Fetch and parse robots.txt for ``domain`` using the correct ``scheme``."""
        rp = RobotFileParser()
        robots_url = f"{scheme}://{domain}/robots.txt"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(robots_url)
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
            # single-flight：同一域名并发首访共享同一次 robots.txt 拉取，
            # 避免每个 worker 各自请求一次
            self._robot_parsers[domain] = asyncio.create_task(
                self._get_robot_parser(parsed.scheme or "https", domain)
            )
        rp = self._robot_parsers[domain]
        if isinstance(rp, asyncio.Task):
            rp = await rp
            self._robot_parsers[domain] = rp
        if rp is None:
            return True
        return rp.can_fetch(self.user_agent, url)

    async def _throttle(self, url: str) -> None:
        """按域名限速：同一域名两次请求间至少间隔 ``self.delay`` 秒。"""
        if self.delay <= 0:
            return
        domain = urlparse(url).netloc
        now = time.monotonic()
        last = self._last_fetch.get(domain, 0.0)
        wait_for = self.delay - (now - last)
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        self._last_fetch[domain] = time.monotonic()

    async def fetch(self, client: httpx.AsyncClient, url: str) -> CrawlResult:
        """Fetch a single URL and extract same-domain links."""
        await self._throttle(url)

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

    async def crawl(self, start_url: str, max_pages: int = 50) -> list[CrawlResult]:
        """Crawl same-domain pages breadth-first with async concurrency."""
        seen: set[str] = {start_url}
        queue: list[str] = [start_url]
        results: list[CrawlResult] = []
        sem = asyncio.Semaphore(self.max_concurrency)
        lock = asyncio.Lock()

        async def worker(client: httpx.AsyncClient, url: str) -> None:
            async with sem:
                result = await self.fetch(client, url)
            async with lock:
                results.append(result)
                if result.error is None:
                    for link in result.links:
                        if link not in seen:
                            seen.add(link)
                            queue.append(link)

        # 复用单个 AsyncClient 以利用 HTTP keep-alive 连接池
        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            while queue and len(results) < max_pages:
                batch = queue[: max_pages - len(results)]
                del queue[: len(batch)]
                await asyncio.gather(*[worker(client, url) for url in batch])

        return results

    @staticmethod
    def _normalize_url(url: str) -> str | None:
        """规范化 URL：仅保留 http/https，域名小写、剥默认端口、去 fragment。

        返回规范化后的绝对 URL；scheme 非 http/https、host 缺失或端口非法时
        返回 ``None``，调用方直接丢弃该链接。
        """
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return None
        host = parts.hostname
        if not host:
            return None
        try:
            port = parts.port
        except ValueError:
            return None
        if port is not None and (
            (parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)
        ):
            port = None
        netloc = host.lower() if port is None else f"{host.lower()}:{port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))

    @staticmethod
    def _extract_links(base_url: str, html: str) -> list[str]:
        """Extract deduplicated same-domain links from ``html``."""
        from bs4 import BeautifulSoup

        base = Crawler._normalize_url(base_url)
        if base is None:
            return []
        base_domain = urlsplit(base).netloc
        soup = BeautifulSoup(html, "lxml")
        links: list[str] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            normalized = Crawler._normalize_url(urljoin(base_url, str(anchor["href"])))
            if normalized is None:
                continue
            # 同域比较基于规范化后的 netloc（大小写/默认端口已归一）
            if urlsplit(normalized).netloc != base_domain:
                continue
            if normalized not in seen:
                seen.add(normalized)
                links.append(normalized)
        return links

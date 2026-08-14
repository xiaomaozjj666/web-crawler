"""Spider engine: request scheduling, callback dispatch, pause/resume.

The engine is intentionally small and synchronous-friendly while still
supporting concurrent fetching via :class:`asyncio.Semaphore`. It treats the
fetcher as a pluggable backend (any object exposing ``get``/``async_get`` or
``fetch``/``async_fetch`` returning a :class:`~web_crawler.response.Response`),
so the same spider can run against :class:`~web_crawler.fetchers.Fetcher`,
:class:`~web_crawler.fetchers.DynamicFetcher`, or
:class:`~web_crawler.fetchers.StealthyFetcher`.
"""

from __future__ import annotations

import asyncio
import base64
import heapq
import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from ..response import Response

logger = logging.getLogger(__name__)


class _FetcherLike(Protocol):
    """Structural type for any fetcher the spider can drive."""

    def get(self, url: str, **kwargs: Any) -> Response: ...  # pragma: no cover

    async def async_get(self, url: str, **kwargs: Any) -> Response: ...  # pragma: no cover


class SpiderError(Exception):
    """Raised for fatal spider engine errors (bad callbacks, state corruption)."""


@dataclass(order=True)
class Request:
    """A scheduled request.

    ``callback`` is the name of a method on the :class:`Spider` subclass
    (default ``"parse"``). ``priority`` higher values are processed first.
    ``meta`` is propagated to ``response.meta`` so callbacks can pass state.
    """

    url: str
    method: str = "GET"
    callback: str = "parse"
    headers: dict[str, str] | None = None
    body: bytes | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    retries: int = 0
    priority: int = 0
    dont_filter: bool = False

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("Request.url must be a non-empty string")


@dataclass
class SpiderStats:
    """Lightweight run statistics."""

    pages_crawled: int = 0
    items_scraped: int = 0
    requests_scheduled: int = 0
    requests_failed: int = 0
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def elapsed(self) -> float:
        if not self.start_time:
            return 0.0
        end = self.end_time or time.monotonic()
        return end - self.start_time

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages_crawled": self.pages_crawled,
            "items_scraped": self.items_scraped,
            "requests_scheduled": self.requests_scheduled,
            "requests_failed": self.requests_failed,
            "elapsed_seconds": round(self.elapsed, 3),
        }


class Spider:
    """Base class for user spiders.

    Subclasses define :attr:`start_urls` (or override :meth:`start_requests`)
    and a ``parse`` callback. Callbacks may ``yield`` additional
    :class:`Request` objects (which are scheduled) or any other object
    (treated as a scraped item and collected).
    """

    name: str = ""
    start_urls: list[str] = []
    allowed_domains: list[str] = []
    custom_settings: dict[str, Any] = {}
    max_concurrency: int = 8
    download_delay: float = 0.0

    def __init__(self, fetcher: Any | None = None, *, adaptive: bool = False) -> None:
        # ``fetcher`` is deferred so a spider can be defined without one.
        self.fetcher = fetcher
        self.adaptive = adaptive
        self.stats = SpiderStats()
        self._seen: set[str] = set()
        self._paused = False
        self._heap_counter = 0
        if not self.name:
            self.name = self.__class__.__name__

    # -- user hooks --------------------------------------------------------
    def start_requests(self) -> Iterator[Request]:
        """Yield the initial requests. Override for custom seeding."""
        for url in self.start_urls:
            yield Request(url=url)

    def parse(self, response: Response) -> Iterator[Any]:  # pragma: no cover - abstract
        """Default callback. Override in subclasses."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement parse() or specify a callback"
        )

    # -- helpers -----------------------------------------------------------
    def allowed(self, url: str) -> bool:
        """Return True if ``url``'s host is permitted (忽略端口与 userinfo)."""
        if not self.allowed_domains:
            return True
        host = urlparse(url).hostname
        if not host:
            return False
        host = host.lower()
        return any(host == d.lower() or host.endswith("." + d.lower()) for d in self.allowed_domains)

    def urljoin(self, base: str, url: str) -> str:
        from urllib.parse import urljoin

        return urljoin(base, url)

    # -- scheduling --------------------------------------------------------
    def _filter(self, request: Request) -> bool:
        if request.dont_filter:
            return True
        if request.url in self._seen:
            return False
        if not self.allowed(request.url):
            logger.debug("filtered off-domain: %s", request.url)
            return False
        self._seen.add(request.url)
        return True

    def _dispatch(self, response: Response, request: Request) -> list[Any]:
        """Run the named callback and collect its yielded outputs."""
        # 拷贝 meta 而非共享引用，避免多个回调间意外互相修改
        response.meta = dict(request.meta)
        callback = getattr(self, request.callback, None)
        if callback is None:
            raise SpiderError(f"callback {request.callback!r} not found on {type(self).__name__}")
        result = callback(response)
        if result is None:
            return []
        return list(result)

    # -- fetcher adapters -------------------------------------------------
    def _fetch_sync(self, request: Request) -> Response:
        assert self.fetcher is not None, "a fetcher must be provided to run a spider"
        headers = request.headers
        if request.method == "GET":
            return self.fetcher.get(request.url, headers=headers)
        if request.method == "POST":
            return self.fetcher.post(request.url, headers=headers, data=request.body)
        return self.fetcher.request(request.method, request.url, headers=headers, data=request.body)

    async def _fetch_async(self, request: Request) -> Response:
        assert self.fetcher is not None, "a fetcher must be provided to run a spider"
        headers = request.headers
        if request.method == "GET":
            return await self.fetcher.async_get(request.url, headers=headers)
        if request.method == "POST":
            return await self.fetcher.async_post(request.url, headers=headers, data=request.body)
        return await self.fetcher.async_request(
            request.method, request.url, headers=headers, data=request.body
        )

    # -- state persistence -------------------------------------------------
    def _state_path(self, path: str | Path | None) -> Path:
        return Path(path) if path else Path(f".{self.name}_state.json")

    def _dump_state(self, queue: list[Request], path: Path) -> None:
        payload = {
            "seen": sorted(self._seen),
            "queue": [
                {
                    "url": r.url,
                    "method": r.method,
                    "callback": r.callback,
                    "headers": r.headers,
                    "meta": r.meta,
                    "priority": r.priority,
                    "dont_filter": r.dont_filter,
                    "retries": r.retries,
                    # body 是 bytes，base64 编码以便 JSON 序列化（恢复时原样还原）
                    "body": base64.b64encode(r.body).decode("ascii") if r.body is not None else None,
                }
                for r in queue
            ],
            "stats": {
                "pages_crawled": self.stats.pages_crawled,
                "items_scraped": self.stats.items_scraped,
                "requests_scheduled": self.stats.requests_scheduled,
                "requests_failed": self.stats.requests_failed,
            },
        }
        # default=str：meta 等自由字段即使含不可序列化对象（如 bytes）也不让暂停崩溃
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    def _load_state(self, path: Path) -> tuple[list[Request], bool]:
        if not path.exists():
            return [], False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SpiderError(f"corrupt spider state file {path}: {exc}") from exc
        self._seen = set(payload.get("seen", []))
        queue = [
            Request(
                url=item["url"],
                method=item.get("method", "GET"),
                callback=item.get("callback", "parse"),
                headers=item.get("headers"),
                meta=item.get("meta", {}),
                priority=item.get("priority", 0),
                dont_filter=item.get("dont_filter", False),
                retries=item.get("retries", 0),
                # 兼容旧状态文件：body 字段缺失时视为无 body
                body=base64.b64decode(item["body"]) if item.get("body") else None,
            )
            for item in payload.get("queue", [])
        ]
        stats = payload.get("stats", {})
        self.stats.pages_crawled = stats.get("pages_crawled", 0)
        self.stats.items_scraped = stats.get("items_scraped", 0)
        self.stats.requests_scheduled = stats.get("requests_scheduled", 0)
        self.stats.requests_failed = stats.get("requests_failed", 0)
        return queue, True

    # -- public API --------------------------------------------------------
    def pause(self) -> None:
        """Signal the running loop to persist state and stop after current batch."""
        self._paused = True

    def run(
        self,
        *,
        max_requests: int | None = None,
        state_file: str | Path | None = None,
        resume: bool = False,
    ) -> list[Any]:
        """Run the spider synchronously and return collected items.

        Parameters
        ----------
        max_requests:
            Hard cap on the number of requests to issue this run.
        state_file:
            Path to a JSON file used for pause/resume. If ``resume`` is True
            and the file exists, the queue and seen-set are restored.
        resume:
            Resume from ``state_file`` if it exists.
        """
        if self.fetcher is None:
            raise SpiderError("Spider.run requires a fetcher; pass fetcher= to the constructor")

        path = self._state_path(state_file)
        # 状态文件仅由"暂停"或显式管理（state_file/resume）触发读写：
        # 全新运行不得覆盖/删除既有的暂停状态文件，max_requests 提前结束
        # 也不得在未显式管理时向 CWD 落盘。
        manage_state = state_file is not None or resume
        owns_state = resume  # resume 从该文件恢复，视为本次运行消费该文件
        # queue is a min-heap of ``(-priority, counter, Request)`` — heapq
        # yields the smallest tuple first, so negating priority gives
        # highest-priority-first ordering.
        queue: list[tuple[int, int, Request]] = []
        if resume:
            loaded, restored = self._load_state(path)
            if restored:
                logger.info("resumed spider %s with %d queued requests", self.name, len(loaded))
                for r in loaded:
                    self._heap_counter += 1
                    heapq.heappush(queue, (-r.priority, self._heap_counter, r))
        else:
            for r in self.start_requests():
                self._heap_counter += 1
                heapq.heappush(queue, (-r.priority, self._heap_counter, r))
            for _, _, r in queue:
                self._seen.add(r.url)

        items: list[Any] = []
        self.stats.start_time = time.monotonic()
        self._paused = False

        while queue and not self._paused:
            if max_requests is not None and self.stats.pages_crawled >= max_requests:
                break
            _, _, request = heapq.heappop(queue)
            self.stats.requests_scheduled += 1
            try:
                response = self._fetch_sync(request)
            except Exception as exc:
                self.stats.requests_failed += 1
                logger.warning("request failed: %s (%s)", request.url, exc)
                continue

            self.stats.pages_crawled += 1
            if self.download_delay:
                time.sleep(self.download_delay)
            try:
                outputs = self._dispatch(response, request)
            except Exception as exc:
                raise SpiderError(
                    f"callback {request.callback!r} raised on {request.url}: {exc}"
                ) from exc

            for out in outputs:
                if isinstance(out, Request):
                    if self._filter(out):
                        self._heap_counter += 1
                        heapq.heappush(queue, (-out.priority, self._heap_counter, out))
                else:
                    items.append(out)
                    self.stats.items_scraped += 1

        self.stats.end_time = time.monotonic()
        if self._paused or (manage_state and queue):
            self._dump_state([r for _, _, r in queue], path)
            owns_state = True
            logger.info("state saved to %s (%d requests remaining)", path, len(queue))
        elif manage_state and owns_state and path.exists():
            path.unlink()
        return items

    async def async_run(
        self,
        *,
        max_requests: int | None = None,
        state_file: str | Path | None = None,
        resume: bool = False,
    ) -> list[Any]:
        """Async variant: fetches concurrently up to :attr:`max_concurrency`.

        Delegates to :meth:`stream` so the core worker loop is defined once.
        """
        if self.fetcher is None:
            raise SpiderError("Spider.async_run requires a fetcher")
        return [item async for item in self.stream(
            max_requests=max_requests,
            state_file=state_file,
            resume=resume,
        )]

    async def stream(
        self,
        *,
        max_requests: int | None = None,
        state_file: str | Path | None = None,
        resume: bool = False,
    ) -> AsyncIterator[Any]:
        """异步流式产出抓取到的 item，适合长爬取与实时管道。

        用法::

            async for item in spider.stream():
                process(item)

        与 :meth:`async_run` 不同，不把所有 item 缓存在内存里，而是
        每抓到一条就 ``yield`` 出去。
        """
        if self.fetcher is None:
            raise SpiderError("Spider.stream requires a fetcher")

        path = self._state_path(state_file)
        # 与 run() 相同的状态文件生命周期：仅暂停或显式管理时读写
        manage_state = state_file is not None or resume
        owns_state = resume
        queue: list[tuple[int, int, Request]] = []
        if resume:
            loaded, restored = self._load_state(path)
            if restored:
                logger.info("resumed spider %s with %d queued requests", self.name, len(loaded))
                for r in loaded:
                    self._heap_counter += 1
                    heapq.heappush(queue, (-r.priority, self._heap_counter, r))
        else:
            for r in self.start_requests():
                self._heap_counter += 1
                heapq.heappush(queue, (-r.priority, self._heap_counter, r))
            for _, _, r in queue:
                self._seen.add(r.url)

        sem = asyncio.Semaphore(self.max_concurrency)
        self.stats.start_time = time.monotonic()
        self._paused = False

        async def worker(item: tuple[int, int, Request]) -> list[Any]:
            _, _, request = item
            async with sem:
                self.stats.requests_scheduled += 1
                try:
                    response = await self._fetch_async(request)
                except Exception as exc:
                    self.stats.requests_failed += 1
                    logger.warning("request failed: %s (%s)", request.url, exc)
                    return []
                self.stats.pages_crawled += 1
                if self.download_delay:
                    await asyncio.sleep(self.download_delay)
                try:
                    return self._dispatch(response, request)
                except Exception as exc:
                    raise SpiderError(
                        f"callback {request.callback!r} raised on {request.url}: {exc}"
                    ) from exc

        while queue and not self._paused:
            if max_requests is not None and self.stats.pages_crawled >= max_requests:
                break
            remaining = (
                max_requests - self.stats.pages_crawled if max_requests is not None else len(queue)
            )
            batch_size = min(self.max_concurrency, len(queue), max(0, remaining))
            if batch_size <= 0:  # pragma: no cover - 防御性：上层已保证 remaining>0
                break
            batch = [heapq.heappop(queue) for _ in range(batch_size)]
            results = await asyncio.gather(*[worker(r) for r in batch])
            for outputs in results:
                for out in outputs:
                    if isinstance(out, Request):
                        if self._filter(out):
                            self._heap_counter += 1
                            heapq.heappush(queue, (-out.priority, self._heap_counter, out))
                        continue
                    self.stats.items_scraped += 1
                    yield out

        self.stats.end_time = time.monotonic()
        if self._paused or (manage_state and queue):
            self._dump_state([r for _, _, r in queue], path)
            owns_state = True
            logger.info("state saved to %s (%d requests remaining)", path, len(queue))
        elif manage_state and owns_state and path.exists():
            path.unlink()


__all__ = ["Request", "Spider", "SpiderError", "SpiderStats"]

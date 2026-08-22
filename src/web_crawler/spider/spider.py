"""Spider 引擎：请求调度、回调分发、暂停/恢复。

引擎刻意保持小巧且对同步友好，同时支持并发抓取。fetcher 被视为
可插拔后端（任何暴露 ``get``/``async_get`` 并返回
:class:`~web_crawler.response.Response` 的对象），因此同一个 spider
可运行在 :class:`~web_crawler.fetchers.Fetcher`、
:class:`~web_crawler.fetchers.DynamicFetcher` 或
:class:`~web_crawler.fetchers.StealthyFetcher` 之上。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
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
from ..robots import RobotsPolicy, fetch_robots_text

logger = logging.getLogger(__name__)


class _FetcherLike(Protocol):
    """spider 可驱动的任意 fetcher 的结构化类型。"""

    def get(self, url: str, **kwargs: Any) -> Response: ...  # pragma: no cover

    async def async_get(self, url: str, **kwargs: Any) -> Response: ...  # pragma: no cover


class SpiderError(Exception):
    """致命 spider 引擎错误（回调错误、状态损坏）时抛出。"""


class IgnoreRequest(Exception):
    """中间件抛出以丢弃某个请求（计入 ``requests_ignored``，不打断运行）。"""


class DropItem(Exception):
    """item 管道抛出以丢弃某条 item（不计入 ``items_scraped``）。"""


class DownloaderMiddleware:
    """下载中间件基类：在请求发出前/响应返回后介入下载流程。

    - :meth:`process_request` 返回 ``None`` 放行下载；返回
      :class:`~web_crawler.response.Response` 直接短路（不再发请求）；
      抛 :class:`IgnoreRequest` 丢弃该请求。
    - :meth:`process_response` 收到下载结果，返回（可替换的）Response。

    中间件按声明顺序依次执行；默认实现全部直通。
    """

    def process_request(self, request: Request, spider: Spider) -> Response | None:
        return None

    def process_response(self, response: Response, request: Request, spider: Spider) -> Response:
        return response


class ItemPipeline:
    """item 管道基类：回调产出的每条 item 依次经过各管道。

    :meth:`process_item` 返回变换后的 item；返回 ``None`` 或抛
    :class:`DropItem` 丢弃该条。
    """

    def process_item(self, item: Any, spider: Spider) -> Any:
        return item


@dataclass(order=True)
class Request:
    """一个已调度的请求。

    ``callback`` 是 :class:`Spider` 子类上的方法名（默认 ``"parse"``）。
    ``priority`` 值越大越先处理。``meta`` 会透传到 ``response.meta``，
    供回调传递状态。
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
    """轻量运行统计。"""

    pages_crawled: int = 0
    items_scraped: int = 0
    requests_scheduled: int = 0
    requests_failed: int = 0
    requests_ignored: int = 0
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
            "requests_ignored": self.requests_ignored,
            "elapsed_seconds": round(self.elapsed, 3),
        }


class DupeFilter:
    """请求去重器：以 method + url + body 的 SHA1 指纹判定重复。

    相比裸 URL 集合，同一 URL 的不同 method / body（如同一接口的
    不同分页参数）不再被互相误杀；``Spider`` 可通过构造参数
    ``dupefilter`` 替换为自定义实现（如磁盘持久化版本）。
    """

    def __init__(self) -> None:
        self.seen: set[str] = set()

    @staticmethod
    def fingerprint(request: Request) -> str:
        h = hashlib.sha1()
        h.update(request.method.upper().encode("utf-8"))
        h.update(b"\x00")
        h.update(request.url.encode("utf-8"))
        if request.body is not None:
            h.update(b"\x00")
            h.update(request.body)
        return h.hexdigest()

    def request_seen(self, request: Request) -> bool:
        """若 ``request`` 曾出现过则返回 True，否则登记并返回 False。"""
        fp = self.fingerprint(request)
        if fp in self.seen:
            return True
        self.seen.add(fp)
        return False


class Spider:
    """用户 spider 的基类。

    子类定义 :attr:`start_urls`（或重写 :meth:`start_requests`）与一个
    ``parse`` 回调。回调可以 ``yield`` 更多 :class:`Request` 对象
    （会被调度）或任意其他对象（视为抓取到的 item 并收集）。
    """

    name: str = ""
    start_urls: list[str] = []
    allowed_domains: list[str] = []
    custom_settings: dict[str, Any] = {}
    max_concurrency: int = 8
    download_delay: float = 0.0
    # 下载失败后的最大重试次数（指数退避）；0 表示不重试，保持旧行为
    max_retries: int = 0
    # 是否遵守目标站点 robots.txt（对回调产出的请求生效；拉取失败视为允许）
    respect_robots: bool = False
    # robots.txt 检查使用的 User-Agent（"*" 表示对所有 UA 的规则取并集的保守判定）
    user_agent: str = "*"
    # 下载中间件（类或实例均可，按声明顺序执行）
    middlewares: list[type[DownloaderMiddleware] | DownloaderMiddleware] = []
    # item 管道（类或实例均可，按声明顺序执行）
    item_pipelines: list[type[ItemPipeline] | ItemPipeline] = []

    def __init__(
        self,
        fetcher: Any | None = None,
        *,
        adaptive: bool = False,
        dupefilter: DupeFilter | None = None,
    ) -> None:
        # ``fetcher`` 允许延迟提供，spider 可先定义后绑定。
        self.fetcher = fetcher
        self.adaptive = adaptive
        self.stats = SpiderStats()
        self.dupefilter = dupefilter if dupefilter is not None else DupeFilter()
        self._middlewares: list[DownloaderMiddleware] = [
            mw() if isinstance(mw, type) else mw for mw in self.middlewares
        ]
        self._item_pipelines: list[ItemPipeline] = [
            pipe() if isinstance(pipe, type) else pipe for pipe in self.item_pipelines
        ]
        self._robots_policy = RobotsPolicy(self.user_agent)
        self._paused = False
        self._heap_counter = 0
        if not self.name:
            self.name = self.__class__.__name__

    # -- user hooks --------------------------------------------------------
    def start_requests(self) -> Iterator[Request]:
        """产出初始请求。重写以自定义种子。"""
        for url in self.start_urls:
            yield Request(url=url)

    def parse(self, response: Response) -> Iterator[Any]:  # pragma: no cover - abstract
        """默认回调。在子类中重写。"""
        raise NotImplementedError(
            f"{type(self).__name__} must implement parse() or specify a callback"
        )

    # -- helpers -----------------------------------------------------------
    def allowed(self, url: str) -> bool:
        """``url`` 的 host 在允许范围内时返回 True（忽略端口与 userinfo）。"""
        if not self.allowed_domains:
            return True
        host = urlparse(url).hostname
        if not host:
            return False
        host = host.lower()
        return any(
            host == d.lower() or host.endswith("." + d.lower()) for d in self.allowed_domains
        )

    def urljoin(self, base: str, url: str) -> str:
        from urllib.parse import urljoin

        return urljoin(base, url)

    # -- scheduling --------------------------------------------------------
    def _robots_allowed(self, url: str) -> bool:
        """检查 ``url`` 是否被目标站点 robots.txt 允许（解析结果按 host 缓存）。

        委托公共 :class:`~web_crawler.robots.RobotsPolicy`（与
        AIScrapeAgent 共用同一实现）：robots.txt 拉取失败时保守视为
        允许，不让一次瞬时故障拦截整个爬取；404 视为全允许。
        """
        if not self.respect_robots:
            return True
        return self._robots_policy.allowed(url, fetch_robots_text)

    def _filter(self, request: Request) -> bool:
        if request.dont_filter:
            return True
        if not self.allowed(request.url):
            logger.debug("filtered off-domain: %s", request.url)
            return False
        if self.dupefilter.request_seen(request):
            return False
        if not self._robots_allowed(request.url):
            logger.info("filtered by robots.txt: %s", request.url)
            return False
        return True

    def _dispatch(self, response: Response, request: Request) -> list[Any]:
        """执行按名取得的回调并收集其 yield 的产出。"""
        # 拷贝 meta 而非共享引用，避免多个回调间意外互相修改
        response.meta = dict(request.meta)
        callback = getattr(self, request.callback, None)
        if callback is None:
            raise SpiderError(f"callback {request.callback!r} not found on {type(self).__name__}")
        result = callback(response)
        if result is None:
            return []
        return list(result)

    # -- middleware / pipeline ----------------------------------------------
    def _apply_request_middlewares(self, request: Request) -> Response | None:
        """依次执行 process_request；返回 Response 表示短路下载。"""
        for mw in self._middlewares:
            result = mw.process_request(request, self)
            if isinstance(result, Response):
                return result
        return None

    def _apply_response_middlewares(self, response: Response, request: Request) -> Response:
        """依次执行 process_response（前一个的产出是后一个的输入）。"""
        for mw in self._middlewares:
            response = mw.process_response(response, request, self)
        return response

    def _apply_item_pipelines(self, item: Any) -> Any:
        """依次执行 process_item；返回 None 表示该条被丢弃。"""
        for pipe in self._item_pipelines:
            try:
                item = pipe.process_item(item, self)
            except DropItem:
                return None
            if item is None:
                return None
        return item

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
            # 指纹集合（旧版状态文件为 URL 字符串，恢复时按原样装回亦可，
            # 只是判定粒度退化，不会误杀新请求）
            "seen": sorted(self.dupefilter.seen),
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
                    "body": base64.b64encode(r.body).decode("ascii")
                    if r.body is not None
                    else None,
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
        self.dupefilter.seen = set(payload.get("seen", []))
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
        """通知运行中的循环持久化状态并在当前批次后停止。"""
        self._paused = True

    def run(
        self,
        *,
        max_requests: int | None = None,
        state_file: str | Path | None = None,
        resume: bool = False,
    ) -> list[Any]:
        """同步运行 spider 并返回收集到的 item。

        Parameters
        ----------
        max_requests:
            本次运行发出请求数的硬上限。
        state_file:
            用于暂停/恢复的 JSON 文件路径。``resume`` 为 True 且文件存在时，
            队列与已见集合会被恢复。
        resume:
            ``state_file`` 存在时从其恢复。
        """
        if self.fetcher is None:
            raise SpiderError("Spider.run requires a fetcher; pass fetcher= to the constructor")

        path = self._state_path(state_file)
        # 状态文件仅由"暂停"或显式管理（state_file/resume）触发读写：
        # 全新运行不得覆盖/删除既有的暂停状态文件，max_requests 提前结束
        # 也不得在未显式管理时向 CWD 落盘。
        manage_state = state_file is not None or resume
        owns_state = resume  # resume 从该文件恢复，视为本次运行消费该文件
        # queue 是 ``(-priority, counter, Request)`` 的最小堆 —— heapq
        # 先弹出最小元组，priority 取负即得到高优先级先出的顺序。
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
                self.dupefilter.seen.add(self.dupefilter.fingerprint(r))

        items: list[Any] = []
        self.stats.start_time = time.monotonic()
        self._paused = False

        # try/finally 保证回调异常或循环中断时也能完成状态持久化，
        # 而不是让已排队的请求凭空丢失
        try:
            while queue and not self._paused:
                if max_requests is not None and self.stats.pages_crawled >= max_requests:
                    break
                _, _, request = heapq.heappop(queue)
                self.stats.requests_scheduled += 1
                # process_request 可短路下载（返回 Response）或丢弃请求
                try:
                    response = self._apply_request_middlewares(request)
                except IgnoreRequest:
                    self.stats.requests_ignored += 1
                    logger.info("request ignored by middleware: %s", request.url)
                    continue
                if response is None:
                    try:
                        response = self._fetch_sync(request)
                    except IgnoreRequest:
                        self.stats.requests_ignored += 1
                        logger.info("request ignored by middleware: %s", request.url)
                        continue
                    except Exception as exc:
                        if request.retries < self.max_retries:
                            request.retries += 1
                            delay = min(0.5 * 2 ** (request.retries - 1), 8.0)
                            if delay:
                                time.sleep(delay)
                            self._heap_counter += 1
                            heapq.heappush(queue, (-request.priority, self._heap_counter, request))
                            logger.info(
                                "retrying %s (attempt %d/%d)",
                                request.url,
                                request.retries,
                                self.max_retries,
                            )
                        else:
                            self.stats.requests_failed += 1
                            logger.warning("request failed: %s (%s)", request.url, exc)
                        continue
                response = self._apply_response_middlewares(response, request)

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
                        continue
                    processed = self._apply_item_pipelines(out)
                    if processed is None:
                        continue
                    items.append(processed)
                    self.stats.items_scraped += 1
        finally:
            self.stats.end_time = time.monotonic()
            if self._paused or (manage_state and queue):
                self._dump_state([r for _, _, r in queue], path)
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
        """异步版本：并发抓取，上限为 :attr:`max_concurrency`。

        委托给 :meth:`stream`，核心 worker 循环只实现一份。
        """
        if self.fetcher is None:
            raise SpiderError("Spider.async_run requires a fetcher")
        return [
            item
            async for item in self.stream(
                max_requests=max_requests,
                state_file=state_file,
                resume=resume,
            )
        ]

    async def stream(
        self,
        *,
        max_requests: int | None = None,
        state_file: str | Path | None = None,
        resume: bool = False,
    ) -> AsyncIterator[Any]:
        """异步流式产出抓取到的 item，适合长爬取与实时管道。

        调度为持续流式：并发槽位空出即取队首请求派发，慢请求不会
        阻塞后续请求的调度（无整批 barrier）。

        用法::

            async for item in spider.stream():
                process(item)

        与 :meth:`async_run` 不同，不把所有 item 缓存在内存里，而是
        每抓到一条就 ``yield`` 出去（按完成顺序）。
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
                self.dupefilter.seen.add(self.dupefilter.fingerprint(r))

        self.stats.start_time = time.monotonic()
        self._paused = False

        async def worker(request: Request, buf: list[Any]) -> None:
            """下载单个请求并处理产出：新 Request 入队，item 写入 buf。"""
            # process_request 可短路下载或丢弃请求
            try:
                response = self._apply_request_middlewares(request)
            except IgnoreRequest:
                self.stats.requests_ignored += 1
                logger.info("request ignored by middleware: %s", request.url)
                return
            if response is None:
                try:
                    response = await self._fetch_async(request)
                except IgnoreRequest:
                    self.stats.requests_ignored += 1
                    logger.info("request ignored by middleware: %s", request.url)
                    return
                except Exception as exc:
                    # 与 run() 一致的重试语义：push 回队列而非在 worker 内自旋，
                    # 让主循环统一控制调度与暂停检查
                    if request.retries < self.max_retries:
                        request.retries += 1
                        delay = min(0.5 * 2 ** (request.retries - 1), 8.0)
                        if delay:
                            await asyncio.sleep(delay)
                        self._heap_counter += 1
                        heapq.heappush(queue, (-request.priority, self._heap_counter, request))
                        logger.info(
                            "retrying %s (attempt %d/%d)",
                            request.url,
                            request.retries,
                            self.max_retries,
                        )
                    else:
                        self.stats.requests_failed += 1
                        logger.warning("request failed: %s (%s)", request.url, exc)
                    return
            response = self._apply_response_middlewares(response, request)
            self.stats.pages_crawled += 1
            if self.download_delay:
                await asyncio.sleep(self.download_delay)
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
                    continue
                processed = self._apply_item_pipelines(out)
                if processed is not None:
                    buf.append(processed)

        # 持续流式调度：只要有空闲并发槽位就立刻取队首请求派发，
        # 慢请求不再阻塞后续请求（区别于旧的"整批等待"模式）。
        # try/finally：消费方提前 break（aclose）、回调异常或暂停时
        # 都要完成状态持久化，不丢已排队的请求。
        pending: set[asyncio.Task[None]] = set()
        items_buf: list[Any] = []
        try:
            while True:
                # 补并发槽位：max_requests 以"已完成 + in-flight"为下限计数，
                # 保证精确不超发也不少发
                while (
                    queue
                    and len(pending) < self.max_concurrency
                    and not self._paused
                    and (
                        max_requests is None
                        or self.stats.pages_crawled + len(pending) < max_requests
                    )
                ):
                    _, _, request = heapq.heappop(queue)
                    self.stats.requests_scheduled += 1
                    pending.add(asyncio.create_task(worker(request, items_buf)))
                if not pending:
                    break  # 无 in-flight 且（队列空或不再取件：暂停/达上限）
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if (exc := task.exception()) is not None:
                        for leftover in pending:
                            leftover.cancel()
                        raise exc
                # drain 完成的 item（完成顺序，非调度顺序）
                while items_buf:
                    item = items_buf.pop(0)
                    self.stats.items_scraped += 1
                    yield item
        finally:
            for leftover in pending:
                leftover.cancel()
            self.stats.end_time = time.monotonic()
            if self._paused or (manage_state and queue):
                self._dump_state([r for _, _, r in queue], path)
                logger.info("state saved to %s (%d requests remaining)", path, len(queue))
            elif manage_state and owns_state and path.exists():
                path.unlink()


__all__ = [
    "DownloaderMiddleware",
    "DropItem",
    "DupeFilter",
    "IgnoreRequest",
    "ItemPipeline",
    "Request",
    "Spider",
    "SpiderError",
    "SpiderStats",
]

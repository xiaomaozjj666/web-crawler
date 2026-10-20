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
from collections import deque
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from ..exceptions import ConfigError
from ..response import Response
from ..robots import RobotsPolicy, fetch_robots_text
from .autothrottle import AutoThrottle

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1024)
def _throttle_host(url: str) -> str:
    """提取 URL 的 host 作为 autothrottle 域键（小写；无 host 返回空串）。

    热路径缓存（对齐 selector.py 的 CSS→XPath 翻译缓存手法）：调度器对
    每个请求至少调用两次（``_domain_delay_for`` 与三源延迟的 host 键），
    解析是纯函数、URL 字符串可哈希——``lru_cache`` 让重复 URL（重试 /
    ``dont_filter`` 重抓 / 同 URL 多次调度）只付一次 ``urlparse`` 成本。
    上限 1024 条防长爬取任务内存无限增长，被挤出 LRU 的条目下次调用
    重新解析即可（无正确性影响）。"""
    return (urlparse(url).hostname or "").lower()


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
            raise ConfigError("Request.url must be a non-empty string")


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
    # 运行结束时的 autothrottle 快照（未开启时保持 None，as_dict 不透出
    # 该键——默认路径的输出与旧版逐字一致）
    autothrottle: dict[str, dict[str, float | int | None]] | None = None

    @property
    def elapsed(self) -> float:
        if not self.start_time:
            return 0.0
        end = self.end_time or time.monotonic()
        return end - self.start_time

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "pages_crawled": self.pages_crawled,
            "items_scraped": self.items_scraped,
            "requests_scheduled": self.requests_scheduled,
            "requests_failed": self.requests_failed,
            "requests_ignored": self.requests_ignored,
            "elapsed_seconds": round(self.elapsed, 3),
        }
        if self.autothrottle is not None:
            d["autothrottle"] = self.autothrottle
        return d


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
    # 按域名限速：{域名: 同域相邻请求的最小间隔秒数}。空 dict 不影响行为；
    # 设置了延迟的域在 stream() 中会被串行化（同域并发降为 1）
    per_domain_delay: dict[str, float] = {}
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
        autothrottle: AutoThrottle | None = None,
        stats_output: str | Path | None = None,
    ) -> None:
        # ``fetcher`` 允许延迟提供，spider 可先定义后绑定。
        self.fetcher = fetcher
        self.adaptive = adaptive
        self.stats = SpiderStats()
        self.dupefilter = dupefilter if dupefilter is not None else DupeFilter()
        # AutoThrottle 自适应限速器：None = 完全关闭，节流行为与旧版
        # 逐字一致（零回归硬性要求）；开启后同域延迟按响应延迟 EMA 反馈
        self.autothrottle = autothrottle
        # stats 导出路径：None = 不导出（默认，行为与旧版逐字一致）；
        # ``.jsonl`` 后缀按行追加（多次运行可聚合分析），其他后缀写
        # 单次 JSON 快照（覆盖写）。运行结束（正常/异常/暂停均）落盘。
        self.stats_output = stats_output
        self._middlewares: list[DownloaderMiddleware] = [
            mw() if isinstance(mw, type) else mw for mw in self.middlewares
        ]
        self._item_pipelines: list[ItemPipeline] = [
            pipe() if isinstance(pipe, type) else pipe for pipe in self.item_pipelines
        ]
        self._robots_policy = RobotsPolicy(self.user_agent)
        self._domain_last_ts: dict[str, float] = {}
        self._domain_locks: dict[str, asyncio.Lock] = {}
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
        host = _throttle_host(url)
        if not host:
            return False
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

    def _robots_crawl_delay(self, url: str) -> float | None:
        """查询 ``url`` 所在站点 robots.txt 声明的 Crawl-delay（秒）。

        仅 ``respect_robots=True`` 时生效；与 :meth:`_robots_allowed`
        共享同一按主机缓存（谁先触发谁拉取，同主机至多一次外呼）。
        未声明 / 拉取失败返回 None，不改变节流行为。对齐 Scrapy 语义：
        Crawl-delay 是站点方显式声明的礼貌底线，只应抬高延迟、永不放宽。
        """
        if not self.respect_robots:
            return None
        return self._robots_policy.crawl_delay(url, fetch_robots_text)

    def _domain_delay_for(self, url: str) -> tuple[float, str]:
        """返回 ``(延迟, 匹配到的域名)``（精确或子域后缀匹配，取最长命中）。

        状态键用匹配域名而非请求 host：同域不同子域（a.x.com / b.x.com）
        共享同一限速账本；未配置返回 ``(0.0, "")``。
        """
        host = _throttle_host(url)
        if not host or not self.per_domain_delay:
            return 0.0, ""
        best_delay, best_domain = 0.0, ""
        for domain, delay in self.per_domain_delay.items():
            d = domain.lower()
            if (host == d or host.endswith("." + d)) and delay > best_delay:
                best_delay, best_domain = delay, d
        return best_delay, best_domain

    def _throttle_delay_for(self, url: str) -> tuple[float, str]:
        """统一节流入口：返回 ``(延迟秒数, 节流账本键)``。

        三个延迟来源取最保守者（最大值）：

        1. ``per_domain_delay`` 静态下限——用户显式承诺的礼貌底线；
        2. autothrottle 自适应值——只允许比静态下限更保守；
        3. robots ``Crawl-delay``（仅 ``respect_robots=True``）——站点方
           显式声明的礼貌底线（对齐 Scrapy 语义），同样只允许抬高。

        三者全部未开启时与 :meth:`_domain_delay_for` 逐字等价（零行为
        变化）。账本键随胜者：静态下限胜出取最长命中域（同域子域共享
        账本的旧行为），autothrottle / robots 胜出取请求 host（两者都按
        host 生效，记账粒度一致）。
        """
        delay, key = self._domain_delay_for(url)
        if self.autothrottle is not None or self.respect_robots:
            host = _throttle_host(url)
            if self.autothrottle is not None:
                at_delay = self.autothrottle.get_delay(host)
                if at_delay > delay:
                    delay, key = at_delay, host
            robots_delay = self._robots_crawl_delay(url)
            if robots_delay is not None and robots_delay > delay:
                delay, key = robots_delay, host
        return delay, key

    def _note_throttle(self, url: str, elapsed: float, success: bool) -> None:
        """把一次真实下载的结果反馈给 autothrottle（未开启时为空操作）。

        只在真实下载路径调用：中间件短路（如缓存命中）没有网络耗时，
        不该反馈；``elapsed`` 只含下载本身，不含引擎自己的节流等待。
        """
        if self.autothrottle is None:
            return
        self.autothrottle.note_response(_throttle_host(url), elapsed, success)

    def _throttle_domain_sync(self, url: str) -> None:
        """同步路径：补足与同域上一请求的最小间隔（run() 顺序执行，无需锁）。"""
        delay, key = self._throttle_delay_for(url)
        if delay <= 0:
            return
        now = time.monotonic()
        wait = self._domain_last_ts.get(key, 0.0) + delay - now
        if wait > 0:
            time.sleep(wait)
        self._domain_last_ts[key] = time.monotonic()

    async def _throttle_domain_async(
        self,
        url: str,
        lock: asyncio.Lock,
        *,
        precomputed: tuple[float, str] | None = None,
    ) -> None:
        """异步路径：在 per-domain 锁内补足同域最小间隔（同域串行）。

        ``precomputed`` 是调用方已算好的 ``(delay, key)``：stream() 的
        worker 为取域锁先调用过一次 :meth:`_throttle_delay_for`，且两次
        调用之间无 ``await`` 点（事件循环内原子、autothrottle/robots 状态
        不可能变化），传入即严格等价于内部重算——省掉每请求一次完整的三
        源延迟计算。缺省 ``None`` 时内部自算，独立调用行为不变。
        """
        delay, key = precomputed if precomputed is not None else self._throttle_delay_for(url)
        if delay <= 0:
            return
        async with lock:
            now = time.monotonic()
            wait = self._domain_last_ts.get(key, 0.0) + delay - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._domain_last_ts[key] = time.monotonic()

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

    # -- run()/stream() 共享的调度基元 ----------------------------------------
    # 两条执行路径（sync 循环 / async 流式 worker）的队列引导、重试与状态
    # 收尾必须逐字同源——此前各自维护，退避公式与日志格式双份，改一边漏
    # 一边即行为分叉。

    def _bootstrap_queue(self, path: Path, resume: bool) -> list[tuple[int, int, Request]]:
        """构建初始优先队列：resume 从状态恢复，否则消费 start_requests 并播种去重指纹。"""
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
        return queue

    def _retry_backoff(self, retries: int) -> float:
        """第 ``retries`` 次重试前的指数退避秒数（0.5s 起步，封顶 8s）。"""
        return min(0.5 * 2 ** (retries - 1), 8.0)

    def _schedule_retry(
        self, request: Request, queue: list[tuple[int, int, Request]], exc: Exception
    ) -> bool:
        """决定是否重试：是则递增计数、重新入队并记日志，返回 True。

        退避睡眠由调用方按 sync/async 各自的 sleep 实现时长取自
        :meth:`_retry_backoff`——入队与计数必须先于睡眠（与历史顺序一致）。
        """
        if request.retries >= self.max_retries:
            self.stats.requests_failed += 1
            logger.warning("request failed: %s (%s)", request.url, exc)
            return False
        request.retries += 1
        self._heap_counter += 1
        heapq.heappush(queue, (-request.priority, self._heap_counter, request))
        logger.info(
            "retrying %s (attempt %d/%d)",
            request.url,
            request.retries,
            self.max_retries,
        )
        return True

    def _finalize_state(
        self,
        queue: list[tuple[int, int, Request]],
        path: Path,
        manage_state: bool,
        owns_state: bool,
    ) -> None:
        """运行结束的状态文件收尾：暂停/显式管理时持久化，resume 消费后清除。"""
        if self._paused or (manage_state and queue):
            self._dump_state([r for _, _, r in queue], path)
            logger.info("state saved to %s (%d requests remaining)", path, len(queue))
        elif manage_state and owns_state and path.exists():
            path.unlink()

    def _export_stats(self) -> None:
        """运行结束时把 stats 快照写入 ``stats_output``（未设置则不导出）。

        ``.jsonl`` 后缀按 JSON Lines 追加一行：``as_dict()`` 全部字段加
        ISO 格式结束时间戳 ``asctime``（UTC），多次运行天然可聚合分析；
        其他后缀写单次 JSON 快照（覆盖写）。父目录不存在时自动创建；
        写入失败（权限等）只记 warning 日志不抛出（走 logging 而非
        warnings）——可观测性导出绝不能让已抓到的数据因收尾异常而丢失。
        ``as_dict()`` 的输出格式保持逐字不变，导出字段在副本上追加。
        """
        if self.stats_output is None:
            return
        path = Path(self.stats_output)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix.lower() == ".jsonl":
                payload = self.stats.as_dict()
                # stats.start/end_time 是 monotonic 时钟（无墙钟语义），
                # 结束时间戳独立取 UTC 墙钟保证跨运行可比较
                payload["asctime"] = datetime.now(timezone.utc).isoformat()
                with path.open("a", encoding="utf-8") as fh:
                    # default=str 对齐 JSON 快照分支（_dump_state 同款防御）：
                    # 未来字段不可序列化时降级为 str 表示——TypeError 不在
                    # except OSError 覆盖范围内，穿透后会中断 run() 返回
                    # 已抓取的 items，违背导出"绝不连累数据"的设计目标
                    fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            else:
                # default=str 对齐 _dump_state：防御未来扩展出不可序列化字段
                path.write_text(
                    json.dumps(self.stats.as_dict(), ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
        except OSError as exc:
            logger.warning("stats export to %s failed: %s", path, exc)

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
        queue = self._bootstrap_queue(path, resume)

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
                    self._throttle_domain_sync(request.url)
                    # elapsed 只度量下载本身（不含上面的节流等待），
                    # 供 autothrottle 估计站点真实服务能力
                    fetch_started = time.monotonic()
                    try:
                        response = self._fetch_sync(request)
                    except IgnoreRequest:
                        self.stats.requests_ignored += 1
                        logger.info("request ignored by middleware: %s", request.url)
                        continue
                    except Exception as exc:
                        # 失败也反馈真实耗时：请求失败本身就该放大该域惩罚
                        self._note_throttle(request.url, time.monotonic() - fetch_started, False)
                        if self._schedule_retry(request, queue, exc):
                            delay = self._retry_backoff(request.retries)
                            if delay:
                                time.sleep(delay)
                        continue
                    self._note_throttle(request.url, time.monotonic() - fetch_started, response.ok)
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
            if self.autothrottle is not None:
                self.stats.autothrottle = self.autothrottle.stats()
            self._finalize_state(queue, path, manage_state, owns_state)
            # 状态持久化优先于可观测性导出（前者防丢请求），导出自身
            # 吞掉 OSError（见 _export_stats），不会让 finally 链断裂
            self._export_stats()
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
        queue = self._bootstrap_queue(path, resume)

        self.stats.start_time = time.monotonic()
        self._paused = False

        async def worker(request: Request, buf: deque[Any]) -> None:
            """下载单个请求并处理产出：新 Request 入队，item 写入 buf。"""
            # process_request 可短路下载或丢弃请求
            try:
                response = self._apply_request_middlewares(request)
            except IgnoreRequest:
                self.stats.requests_ignored += 1
                logger.info("request ignored by middleware: %s", request.url)
                return
            if response is None:
                delay, domain_key = self._throttle_delay_for(request.url)
                if delay > 0:
                    lock = self._domain_locks.get(domain_key)
                    if lock is None:
                        lock = asyncio.Lock()
                        self._domain_locks[domain_key] = lock
                    # precomputed：两次三源计算之间无 await（严格等价），
                    # 省掉每请求一次的重复三源延迟计算
                    await self._throttle_domain_async(
                        request.url, lock, precomputed=(delay, domain_key)
                    )
                # elapsed 只度量下载本身（不含上面的节流等待），
                # 供 autothrottle 估计站点真实服务能力
                fetch_started = time.monotonic()
                try:
                    response = await self._fetch_async(request)
                except IgnoreRequest:
                    self.stats.requests_ignored += 1
                    logger.info("request ignored by middleware: %s", request.url)
                    return
                except Exception as exc:
                    # 失败也反馈真实耗时：请求失败本身就该放大该域惩罚
                    self._note_throttle(request.url, time.monotonic() - fetch_started, False)
                    # 与 run() 一致的重试语义：push 回队列而非在 worker 内自旋，
                    # 让主循环统一控制调度与暂停检查
                    if self._schedule_retry(request, queue, exc):
                        delay = self._retry_backoff(request.retries)
                        if delay:
                            await asyncio.sleep(delay)
                    return
                self._note_throttle(request.url, time.monotonic() - fetch_started, response.ok)
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
        items_buf: deque[Any] = deque()
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
                    item = items_buf.popleft()
                    self.stats.items_scraped += 1
                    yield item
        finally:
            for leftover in pending:
                leftover.cancel()
            self.stats.end_time = time.monotonic()
            if self.autothrottle is not None:
                self.stats.autothrottle = self.autothrottle.stats()
            self._finalize_state(queue, path, manage_state, owns_state)
            # 与 run() 同源：消费方提前 break（aclose）、回调异常或暂停
            # 走到这里的每条路径都要完成 stats 导出
            self._export_stats()


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

"""网络层性能压测（任务 17）：调度热路径操作计数、连接复用、内存峰值。

设计原则（CI 抖动下不 flake）：**不做 wall-time 断言**——

* 热路径用 monkeypatch 的**操作计数**证明缓存/单次计算生效（如「1000
  次调度调用的 urlparse 次数 ≤ N」）；
* 内存用 ``tracemalloc`` 峰值断言大响应（10MB）不被复制；
* 语义等价用三源延迟矩阵（与 tests/test_spider_stats.py 的 14 例互补、
  取值不重复）、钉扎/池化/单会话复用的集成断言守住优化零行为变化。
"""

from __future__ import annotations

import asyncio
import gc
import socket
import tracemalloc
import urllib.robotparser
from typing import Any

import pytest

from web_crawler import Response, Spider
from web_crawler._ssrf import _dns_verdict_cache
from web_crawler.fetchers import _pin as _pin_mod
from web_crawler.fetchers import fetcher as fetcher_mod
from web_crawler.fetchers.fetcher import Fetcher
from web_crawler.fetchers.session import SessionPool
from web_crawler.spider import AutoThrottle
from web_crawler.spider import spider as spider_mod

HOST = "shop.example.com"
BASE = f"https://{HOST}"
URL = f"{BASE}/page"


# ===========================================================================
# 共用设施
# ===========================================================================


class FakeFetcher:
    """返回 200 空页的无网络 fetcher。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> Response:
        self.calls.append(url)
        return Response(url, 200, b"<html><body>ok</body></html>")

    async def async_get(self, url: str, **kwargs: Any) -> Response:
        return self.get(url, **kwargs)


def _at_with_delay(delay: float) -> AutoThrottle:
    """构造 delay 恰为指定值的 AutoThrottle（alpha=1 首样本即真值）。"""
    at = AutoThrottle(alpha=1.0, target_concurrency=1.0)
    at.note_response(HOST, delay, True)
    return at


def _make_spider(
    *,
    fetcher: Any | None = None,
    per_domain: dict[str, float] | None = None,
    autothrottle: AutoThrottle | None = None,
    robots_lines: list[str] | None = None,
    respect_robots: bool = True,
) -> Spider:
    """构造三源延迟测试 spider（robots 解析器直接注入缓存，零网络）。"""

    class S(Spider):
        name = "net_perf"

        def parse(self, response: Response) -> None:
            return None

    # Spider.__init__ 原生接受 autothrottle 关键字（None = 关闭），
    # 必须经构造函数注入——事后赋值之外的本地变量会被静默丢弃。
    spider = S(
        fetcher=fetcher if fetcher is not None else FakeFetcher(),
        autothrottle=autothrottle,
    )
    spider.respect_robots = respect_robots
    if per_domain:
        spider.per_domain_delay = per_domain
    if robots_lines is not None:
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(robots_lines)
        spider._robots_policy._cache[f"https://{HOST}"] = parser
    return spider


@pytest.fixture(autouse=True)
def _clear_throttle_cache() -> None:
    """每用例清空 _throttle_host 的 lru_cache，计数断言不被跨用例串扰。"""
    spider_mod._throttle_host.cache_clear()


class _FakeClock:
    """受控假时钟：monotonic() 读数仅由 advance() 推进。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, dt: float) -> None:
        self._now += dt


# ===========================================================================
# A. 调度热路径：操作计数证明缓存/单次计算生效
# ===========================================================================


def test_throttle_host_lru_cache_hits_on_repeated_urls() -> None:
    """同一 URL 重复调度：lru_cache 仅首次 miss，其余全部命中。"""
    assert spider_mod._throttle_host(URL) == HOST  # 1 次 miss
    for _ in range(1000):
        spider_mod._throttle_host(URL)
    info = spider_mod._throttle_host.cache_info()
    assert info.misses == 1
    assert info.hits >= 1000
    assert info.maxsize == 1024


def test_throttle_host_value_equivalence_across_url_forms() -> None:
    """缓存后的 host 提取与直接 urlparse 逐形态等价（含边界 URL）。"""
    from urllib.parse import urlparse

    cases: list[str] = [
        "https://EXAMPLE.com/path?q=1",
        f"{BASE}:8443/deep/path",
        "https://user:secret@host.test/x",
        "https://[2001:db8::1]/x",
        "https://example.com./x",
        "not a url",
        "https:///pathonly",
        "//host-relative",
    ]
    for url in cases:
        expected = (urlparse(url).hostname or "").lower()
        assert spider_mod._throttle_host(url) == expected


def test_scheduler_urlparse_count_bounded_over_1000_calls(monkeypatch) -> None:
    """1000 次三源调度调用的 urlparse 次数 ≤ 10（优化前为 2000）。

    任务点名的操作计数断言：monkeypatch spider 命名空间的 urlparse 计数，
    证明 _throttle_host 的 lru_cache 让重复 URL 只付一次解析成本。
    """
    spider = _make_spider(
        per_domain={"example.com": 0.5},
        autothrottle=AutoThrottle(alpha=1.0),
        robots_lines=["User-agent: *", "Crawl-delay: 2"],
    )
    counter = {"n": 0}
    real_urlparse = spider_mod.urlparse

    def counting_urlparse(url: str) -> Any:
        counter["n"] += 1
        return real_urlparse(url)

    monkeypatch.setattr(spider_mod, "urlparse", counting_urlparse)
    spider._throttle_delay_for(URL)  # 首次 miss（1 次 urlparse）
    for _ in range(1000):
        spider._throttle_delay_for(URL)
    assert counter["n"] <= 10


def test_stream_computes_three_source_delay_once_per_request(monkeypatch) -> None:
    """stream() 每请求只计算一次三源延迟（优化前为 2 次）。

    三源全开但延迟全 0（不产生真实 sleep）；monkeypatch 实例方法计数。
    """
    spider = _make_spider(
        per_domain={"example.com": 0.0},
        autothrottle=AutoThrottle(),  # 无样本 → start_delay=0
        robots_lines=["User-agent: *", "Crawl-delay: 0"],
    )
    spider.start_urls = [f"{BASE}/p1", f"{BASE}/p2", f"{BASE}/p3"]
    counter = {"n": 0}
    real_tdf = spider._throttle_delay_for

    def counting_tdf(url: str) -> tuple[float, str]:
        counter["n"] += 1
        return real_tdf(url)

    monkeypatch.setattr(spider, "_throttle_delay_for", counting_tdf)
    asyncio.run(spider.async_run())
    assert spider.stats.pages_crawled == 3
    assert counter["n"] == 3  # 优化前为 6（每请求 2 次）


def test_stream_autothrottle_queried_once_per_request() -> None:
    """stream() 每请求只查询一次 autothrottle.get_delay（优化前为 2 次）。"""
    at = AutoThrottle()
    counter = {"n": 0}
    real_get = at.get_delay

    def counting_get(domain: str) -> float:
        counter["n"] += 1
        return real_get(domain)

    at.get_delay = counting_get  # type: ignore[method-assign]
    spider = _make_spider(
        autothrottle=at,
        robots_lines=["User-agent: *", "Crawl-delay: 0"],
    )
    spider.start_urls = [f"{BASE}/p1", f"{BASE}/p2", f"{BASE}/p3"]
    asyncio.run(spider.async_run())
    assert counter["n"] == 3  # 优化前为 6


def test_throttle_domain_async_precomputed_matches_recomputed(monkeypatch) -> None:
    """precomputed 传值与内部自算产生完全相同的节流时序。

    同一配置的两个 spider 各自用独立假时钟驱动 ``_throttle_domain_async``
    的两种调用形态（robots Crawl-delay: 1，两跳、每跳后 0.1s 下载），
    断言 sleep 序列一致——证明 stream worker 传 ``precomputed`` 与旧的
    锁内重算严格等价（两次计算之间无 await 点，输入必然相同）。
    """

    async def drive(clock: _FakeClock, sleeps: list[float], *, precomputed: bool) -> None:
        async def fake_async_sleep(delay: float) -> None:
            sleeps.append(delay)
            clock.advance(delay)

        monkeypatch.setattr("asyncio.sleep", fake_async_sleep)
        monkeypatch.setattr(
            "web_crawler.spider.spider.time",
            type("FakeTime", (), {"monotonic": clock.monotonic, "sleep": lambda s: None})(),
        )
        spider = _make_spider(robots_lines=["User-agent: *", "Crawl-delay: 1"])
        lock = asyncio.Lock()
        for _ in range(2):
            if precomputed:
                delay, key = spider._throttle_delay_for(URL)
                await spider._throttle_domain_async(URL, lock, precomputed=(delay, key))
            else:
                await spider._throttle_domain_async(URL, lock)
            clock.advance(0.1)  # 模拟下载耗时

    sleeps_precomputed: list[float] = []
    sleeps_recomputed: list[float] = []
    asyncio.run(drive(_FakeClock(), sleeps_precomputed, precomputed=True))
    asyncio.run(drive(_FakeClock(), sleeps_recomputed, precomputed=False))
    assert sleeps_precomputed == sleeps_recomputed == pytest.approx([1.0, 0.9])


def test_stream_robots_crawl_delay_timing_after_optimization(monkeypatch) -> None:
    """stream() 全链路 robots Crawl-delay 时序保持（假时钟，3 请求）。

    与 test_spider_stats.py 的 2 请求用例互补：3 请求验证节流账本在多次
    「计算一次 → precomputed 传值」循环后仍然正确推进。推演（Crawl-delay
    1，下载各 0.1）：请求1 sleep(1.0) → t=1.1；请求2 sleep(0.9) → t=2.1；
    请求3 wait=2.0+1.0-2.1=0.9 → sleep(0.9)。
    """
    clock = _FakeClock()
    sleeps: list[float] = []

    async def fake_async_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock.advance(delay)

    monkeypatch.setattr("asyncio.sleep", fake_async_sleep)
    monkeypatch.setattr(
        "web_crawler.spider.spider.time",
        type("FakeTime", (), {"monotonic": clock.monotonic, "sleep": lambda s: None})(),
    )

    class TimedFetcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def _respond(self, url: str) -> Response:
            self.calls.append(url)
            clock.advance(0.1)
            return Response(url, 200, b"<html><body>ok</body></html>")

        async def async_get(self, url: str, **kwargs: Any) -> Response:
            return self._respond(url)

    spider = _make_spider(
        fetcher=TimedFetcher(),
        robots_lines=["User-agent: *", "Crawl-delay: 1"],
    )
    spider.max_concurrency = 1
    spider.start_urls = [f"{BASE}/p1", f"{BASE}/p2", f"{BASE}/p3"]
    asyncio.run(spider.async_run())
    assert sleeps == pytest.approx([1.0, 0.9, 0.9])
    assert spider.stats.pages_crawled == 3


# ===========================================================================
# B. 三源延迟语义矩阵（与 test_spider_stats.py 的 14 例互补，取值不重复）
# ===========================================================================


@pytest.mark.parametrize(
    ("per_domain", "at_delay", "robots_delay", "expected_delay", "expected_key"),
    [
        # robots 最大（3 > 1.5 > 0.8）：键取 host
        ({"example.com": 1.5}, 0.8, 3, 3.0, HOST),
        # 静态下限最大（6.0 > 3.0 > 2）：保留静态域键
        ({"example.com": 6.0}, 3.0, 2, 6.0, "example.com"),
        # autothrottle 最大（4.0 > 1 > 0.0）：键取 host
        ({"example.com": 0.0}, 4.0, 1, 4.0, HOST),
        # 静态与 robots 相等（2.0 == 2.0 > 0.5）：静态先比对胜出，键为域
        ({"example.com": 2.0}, 0.5, 2, 2.0, "example.com"),
    ],
)
def test_three_source_semantics_after_optimization(
    per_domain: dict[str, float],
    at_delay: float,
    robots_delay: int,
    expected_delay: float,
    expected_key: str,
) -> None:
    """优化后三源取大者语义逐例保持（含 host 缓存路径上的等值边界）。

    ``robots_delay`` 用整数：标准库 robotparser 只认整数字符串的
    Crawl-delay（"Crawl-delay: 2.5" 解析得 None），小数会静默失效。
    """
    spider = _make_spider(
        per_domain=per_domain,
        autothrottle=_at_with_delay(at_delay),
        robots_lines=["User-agent: *", f"Crawl-delay: {robots_delay}"],
    )
    assert spider._throttle_delay_for(f"{BASE}/x") == (expected_delay, expected_key)
    # 重复调用（缓存命中路径）返回完全一致——缓存不改变语义
    assert spider._throttle_delay_for(f"{BASE}/x") == (expected_delay, expected_key)


def test_throttle_host_cache_case_insensitive_domain_keys() -> None:
    """大小写不同的 URL 共享 autothrottle 账本键（缓存后语义保持）。"""
    at = AutoThrottle(alpha=1.0, target_concurrency=1.0)
    at.note_response(HOST.upper(), 2.0, True)  # 账本键内部小写化
    spider = _make_spider(autothrottle=at, robots_lines=["User-agent: *"])
    # 大写 host 的 URL 经缓存提取后与已记账的小写 host 同键 → 取到 2.0
    assert spider._throttle_delay_for(f"https://{HOST.upper()}/x") == (2.0, HOST)


def test_per_domain_delay_longest_suffix_match() -> None:
    """_domain_delay_for 最长后缀命中与未命中语义（走缓存 host 路径）。"""
    spider = _make_spider(
        per_domain={"example.com": 0.5, "shop.example.com": 1.5, "other.org": 2.0}
    )
    assert spider._domain_delay_for(f"{BASE}/x") == (1.5, "shop.example.com")
    assert spider._domain_delay_for("https://example.com/") == (0.5, "example.com")
    assert spider._domain_delay_for("https://sub.shop.example.com/") == (
        1.5,
        "shop.example.com",
    )
    assert spider._domain_delay_for("https://elsewhere.net/") == (0.0, "")


def test_allowed_domains_equivalence_with_cached_host() -> None:
    """allowed() 复用缓存 host 后：精确/子域/大写配置/无 host 语义不变。"""
    spider = _make_spider()
    spider.allowed_domains = ["Example.com"]
    assert spider.allowed(f"{BASE}/x") is True  # 子域命中
    assert spider.allowed("https://example.com/") is True  # 精确命中
    assert spider.allowed("https://badexample.com/") is False  # 后缀非子域
    assert spider.allowed("https://other.org/x") is False
    assert spider.allowed("https:///nolhost") is False  # 无 host
    spider.allowed_domains = []
    assert spider.allowed("https://anything.test/") is True  # 未配置全放行


# ===========================================================================
# C. fetcher 热路径：探测缓存与请求头合并语义
# ===========================================================================


def _importable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def test_decodable_encodings_probe_cached_and_accurate() -> None:
    """解码能力探测进程内只做一次（缓存生效）且与运行时 import 一致。"""
    first = fetcher_mod._decodable_encodings()
    second = fetcher_mod._decodable_encodings()
    assert first is second  # 同一 frozenset 对象 → 缓存生效
    assert {"gzip", "deflate", "identity"} <= first
    assert ("br" in first) == _importable("brotli")
    assert ("zstd" in first) == _importable("zstandard")


def test_merge_headers_httpx_path_semantics_unchanged(monkeypatch) -> None:
    """httpx 兜底路径头合并语义：默认头 + extra 覆盖 + per-request 覆盖。"""
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    f = Fetcher(extra_headers={"X-Custom": "yes", "Accept-Language": "zh-CN"})
    try:
        merged = f._merge_headers({"X-Per": "req"})
        assert "Accept-Language" in merged
        assert merged["X-Custom"] == "yes"
        assert merged["X-Per"] == "req"
        # extra 覆盖默认头的值
        assert merged["Accept-Language"] == "zh-CN"
        # Accept-Encoding 已按可解压编码收缩（不含未安装解码器的编码）
        encodings = {e.strip() for e in merged["Accept-Encoding"].split(",")}
        assert ("br" in encodings) == _importable("brotli")
        # 默认头本不含 zstd：收缩是纯减法，不会因 zstandard 已安装而新增
        assert "zstd" not in encodings
    finally:
        f.close()


def test_merge_headers_httpx_path_repeated_calls_consistent(monkeypatch) -> None:
    """连续 1000 次合并结果逐字一致：探测缓存不引入状态污染。"""
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    f = Fetcher(extra_headers={"X-Custom": "yes"})
    try:
        first = f._merge_headers({"X-Per": "req"})
        for _ in range(1000):
            if f._merge_headers({"X-Per": "req"}) != first:
                pytest.fail("merge result drifted across repeated calls")
    finally:
        f.close()


def test_merge_headers_curl_path_semantics_unchanged(monkeypatch) -> None:
    """curl 路径只叠加用户显式头（不掺浏览器默认头）——指纹不被稀释。"""
    _use_fake_curl(monkeypatch)
    f = Fetcher(extra_headers={"X-Custom": "yes"})
    try:
        merged = f._merge_headers({"X-Per": "req"})
        assert merged == {"X-Custom": "yes", "X-Per": "req"}
        # 无 per-request 头时同样只含 extra
        assert f._merge_headers(None) == {"X-Custom": "yes"}
    finally:
        f.close()


def test_retry_errors_cached_and_identical() -> None:
    """_retry_errors 首次计算后实例级缓存：同一元组对象、值等价。"""
    f = Fetcher()
    try:
        errors = f._retry_errors()
        assert f._retry_errors() is errors  # 缓存生效
        assert OSError in errors
        assert len(errors) == 2
    finally:
        f.close()


# ===========================================================================
# D. 内存：大响应零拷贝（tracemalloc 峰值断言）
# ===========================================================================


class _FakeRawResponse:
    """传输层原始响应替身：大 content 直接挂在属性上。"""

    def __init__(self, url: str, content: bytes) -> None:
        self.url = url
        self.status_code = 200
        self.content = content
        self.headers = {"Content-Type": "text/html", "Server": "bench"}


def test_to_response_zero_copy_large_content() -> None:
    """10MB 响应转换：Response.content 与 raw.content 是同一对象（零拷贝）。"""
    f = Fetcher()
    try:
        big = b"a" * (10 * 1024 * 1024)
        resp = f._to_response(_FakeRawResponse(URL, big), {})
        assert resp.content is big
    finally:
        f.close()


def test_to_response_peak_memory_large_content_bounded() -> None:
    """10MB 响应转换的 tracemalloc 峰值增量 < 1MB（内容不复制、不翻倍）。"""
    f = Fetcher()
    try:
        big = b"a" * (10 * 1024 * 1024)
        raw = _FakeRawResponse(URL, big)
        f._to_response(raw, {})  # warm up（消除首次分配噪声）
        gc.collect()
        tracemalloc.start()
        tracemalloc.reset_peak()
        resp = f._to_response(raw, {})
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert resp.content is big
        assert peak < 1024 * 1024  # 峰值仅 dict(headers) 等小对象
    finally:
        f.close()


def test_spider_run_large_response_memory_bounded() -> None:
    """run() 处理两页 10MB 响应：tracemalloc 峰值增量 < 5MB（不翻倍）。

    内容（20MB）在测量前已分配（基线内）；断言的是引擎路径（Response
    构造 / meta 拷贝 / 调度）不复制大 content。
    """
    pages = {f"{BASE}/p{i}": b"x" * (10 * 1024 * 1024) for i in (1, 2)}

    class BigFetcher(FakeFetcher):
        def __init__(self, big_pages: dict[str, bytes]) -> None:
            super().__init__()
            self.big_pages = big_pages

        def get(self, url: str, **kwargs: Any) -> Response:
            self.calls.append(url)
            return Response(url, 200, self.big_pages[url])

    spider = _make_spider(fetcher=BigFetcher(pages), respect_robots=False)
    spider.start_urls = list(pages)
    gc.collect()
    tracemalloc.start()
    tracemalloc.reset_peak()
    items = spider.run()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert items == []
    assert spider.stats.pages_crawled == 2
    assert peak < 5 * 1024 * 1024


# ===========================================================================
# E. 连接复用与钉扎语义保持（优化不得破坏前序任务的能力）
# ===========================================================================


class _FakeCurlVersion:
    """curl_cffi.CurlHttpVersion 最小替身。"""

    V1_1 = "v1_1"


class _FakeCurlSession:
    """curl_cffi 同步 Session 替身：计数 request 并记录 curl_options。"""

    def __init__(self, **kwargs: object) -> None:
        self.curl_options: dict[Any, Any] = {}
        self.request_count = 0
        self.closed = False

    def request(self, **kwargs: Any) -> _FakeRawResponse:
        self.request_count += 1
        return _FakeRawResponse("https://example.com/", b"ok")

    def close(self) -> None:
        self.closed = True


class _FakeAsyncCurlSession:
    """curl_cffi AsyncSession 替身：request/close 均为协程（stream() 路径）。

    缺了它，异步会话被同步替身顶替时 ``await session.request(...)`` 直接
    抛 ``object can't be used in 'await' expression``——3 请求全部失败。
    """

    def __init__(self, **kwargs: object) -> None:
        self.curl_options: dict[Any, Any] = {}
        self.request_count = 0
        self.closed = False

    async def request(self, **kwargs: Any) -> _FakeRawResponse:
        self.request_count += 1
        return _FakeRawResponse("https://example.com/", b"ok")

    async def close(self) -> None:
        self.closed = True


def _use_fake_curl(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 Fetcher 走 curl 分支但绑定 fake session（无需真实网络）。

    第三个元素是**异步**会话类：stream() 的并发下载路径会
    ``await session.request(...)``，必须由协程替身承接。
    """
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", True)
    monkeypatch.setattr(
        fetcher_mod,
        "_load_curl_backend",
        lambda: (_FakeCurlVersion, _FakeCurlSession, _FakeAsyncCurlSession, Exception),
    )


def test_sync_session_reused_across_requests(monkeypatch) -> None:
    """无池路径：3 次请求仅构建 1 次会话、全程同一对象（连接复用保持）。"""
    _use_fake_curl(monkeypatch)
    builds = {"n": 0}
    real_build = Fetcher._build_curl_sync_session

    def counting_build(self: Fetcher, impersonate: str | None = None) -> Any:
        builds["n"] += 1
        return real_build(self, impersonate)

    monkeypatch.setattr(Fetcher, "_build_curl_sync_session", counting_build)
    f = Fetcher(allow_private_hosts=True)
    try:
        statuses = [f.get("https://example.com/").status for _ in range(3)]
        assert statuses == [200, 200, 200]
        assert builds["n"] == 1  # 单会话惰性构建一次
        assert f._session is not None
        assert f._session.request_count == 3  # 同一会话上复用连接
    finally:
        f.close()


def test_session_pool_reuses_sessions_across_requests(monkeypatch) -> None:
    """SessionPool 路径：3 次请求池内只创建 1 个会话（借还复用保持）。"""
    _use_fake_curl(monkeypatch)
    pool = SessionPool(max_size=2)
    f = Fetcher(allow_private_hosts=True, session_pool=pool)
    try:
        for _ in range(3):
            assert f.get("https://example.com/").status == 200
        stats = pool.stats()
        assert stats["created"] == 1  # 借 → 还 → 借：同一会话复用
        assert stats["active"] == 0
        assert stats["idle"] == 1
    finally:
        f.close()
        pool.close()


def test_pin_curl_session_applied_on_every_request(monkeypatch) -> None:
    """DNS 钉扎（CURLOPT_RESOLVE）在优化后每请求仍然生效（语义保持）。"""
    _use_fake_curl(monkeypatch)
    # 哨兵 RESOLVE 键（对齐 test_ssrf_pin 的做法，不依赖真实 curl_cffi 枚举）
    resolve_opt = object()
    monkeypatch.setattr(_pin_mod, "_curl_opt_resolve", lambda: resolve_opt)
    # 开发机 shell 常驻代理变量时钉扎被跳过——强制视为未配置
    monkeypatch.setattr(fetcher_mod, "env_proxy_configured", lambda: False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )
    _dns_verdict_cache._data.clear()

    f = Fetcher(allow_private_hosts=False, resolve_hosts=True)
    try:
        for _ in range(3):
            assert f.get("https://example.com/").status == 200
        session = f._session
        assert isinstance(session, _FakeCurlSession)
        # 每次请求后钉扎条目仍在（即时解析校验 + CURLOPT_RESOLVE 写入）
        assert session.curl_options[resolve_opt] == ["example.com:443:93.184.216.34"]
        assert session.request_count == 3
    finally:
        f.close()
        _dns_verdict_cache._data.clear()


def test_request_throttle_and_pin_coexist_in_stream(monkeypatch) -> None:
    """三源节流与单会话复用在 stream() 并发路径上共存（集成冒烟）。

    零延迟三源全开 + fake curl 单会话：并发 2 槽跑 3 请求，节流计数 3、
    会话构建 1 次——两类优化（调度热路径 + 连接复用）同时生效。
    """
    _use_fake_curl(monkeypatch)
    spider = _make_spider(
        per_domain={"example.com": 0.0},
        autothrottle=AutoThrottle(),
        robots_lines=["User-agent: *", "Crawl-delay: 0"],
    )
    spider.start_urls = [f"{BASE}/p1", f"{BASE}/p2", f"{BASE}/p3"]
    spider.max_concurrency = 2

    builds = {"n": 0}
    real_build = Fetcher._build_curl_async_session

    def counting_build(self: Fetcher, impersonate: str | None = None) -> Any:
        builds["n"] += 1
        return real_build(self, impersonate)

    counter = {"n": 0}
    real_tdf = spider._throttle_delay_for

    def counting_tdf(url: str) -> tuple[float, str]:
        counter["n"] += 1
        return real_tdf(url)

    f = Fetcher(allow_private_hosts=True)
    monkeypatch.setattr(Fetcher, "_build_curl_async_session", counting_build)
    monkeypatch.setattr(spider, "_throttle_delay_for", counting_tdf)
    try:
        spider.fetcher = f
        asyncio.run(spider.async_run())
        assert spider.stats.pages_crawled == 3
        assert counter["n"] == 3  # 每请求恰好一次三源计算
        assert builds["n"] == 1  # 异步单会话只构建一次
        assert f._async_session is not None
        assert f._async_session.request_count == 3
    finally:
        asyncio.run(f.aclose())

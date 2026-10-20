"""AutoThrottle 自适应限速测试：单元（EMA/钳制/惩罚/stats）+ Spider 集成。

集成测试用受控假时钟（替换 spider 模块命名空间内的 ``time``）与假
``asyncio.sleep`` / ``time.sleep``（记录时长并推进假时钟、不真实等待），
让节流时序完全确定，可做精确数值断言。
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from web_crawler import Response, Spider
from web_crawler.spider import AutoThrottle

# ===========================================================================
# 单元测试：纯 AutoThrottle（数值语义）
# ===========================================================================


def test_ema_first_sample_assigned_directly() -> None:
    """首样本直接赋值（非 alpha 加权），避免冷启动低估。"""
    at = AutoThrottle(alpha=0.3)
    at.note_response("x.com", 0.8, True)
    stats = at.stats()["x.com"]
    assert stats["ema_latency"] == pytest.approx(0.8)
    assert stats["samples"] == 1
    # target_concurrency 默认 1.0 → delay == ema
    assert at.get_delay("x.com") == pytest.approx(0.8)


def test_ema_converges_to_slow_regime() -> None:
    """延迟从 0.2s 抬升到 1.0s 后，EMA 应逼近新真值（0.7^50 残留可忽略）。"""
    at = AutoThrottle(alpha=0.3, target_concurrency=1.0)
    for _ in range(50):
        at.note_response("x.com", 0.2, True)
    # 尚未变慢：EMA 停留在 0.2
    assert at.stats()["x.com"]["ema_latency"] == pytest.approx(0.2, abs=1e-9)
    for _ in range(50):
        at.note_response("x.com", 1.0, True)
    stats = at.stats()["x.com"]
    assert stats["ema_latency"] == pytest.approx(1.0, abs=1e-6)
    assert stats["samples"] == 100


def test_target_concurrency_divides_delay() -> None:
    """target_delay = EMA / target_concurrency（Scrapy 语义）。"""
    at = AutoThrottle(alpha=1.0, target_concurrency=2.0)
    at.note_response("x.com", 1.0, True)
    assert at.get_delay("x.com") == pytest.approx(0.5)


def test_delay_clamped_to_min_and_max() -> None:
    """延迟钳制到 [min_delay, max_delay] 边界。"""
    fast = AutoThrottle(alpha=1.0, min_delay=0.5, max_delay=60.0)
    fast.note_response("fast.com", 0.01, True)
    assert fast.get_delay("fast.com") == pytest.approx(0.5)

    slow = AutoThrottle(alpha=1.0, min_delay=0.0, max_delay=1.0)
    slow.note_response("slow.com", 100.0, True)
    assert slow.get_delay("slow.com") == pytest.approx(1.0)


def test_unknown_domain_returns_start_delay() -> None:
    """无样本域返回 start_delay（对齐 Scrapy AUTOTHROTTLE_START_DELAY）。"""
    at = AutoThrottle(start_delay=0.25)
    assert at.get_delay("never-seen.com") == pytest.approx(0.25)
    # 默认 0.0：不拖慢冷启动
    assert AutoThrottle().get_delay("other.com") == pytest.approx(0.0)


def test_failure_penalty_escalates_and_recovers() -> None:
    """连续失败逐次放大惩罚（1.5x），成功后对称衰减回落到 1.0。"""
    at = AutoThrottle(alpha=1.0, penalty_factor=1.5)
    at.note_response("x.com", 1.0, True)
    assert at.get_delay("x.com") == pytest.approx(1.0)

    # 连续失败 3 次：penalty = 1.5^3 = 3.375
    for _ in range(3):
        at.note_response("x.com", 1.0, False)
    assert at.get_delay("x.com") == pytest.approx(3.375)
    # 失败样本不进 EMA（健康延迟基准不被污染）
    stats = at.stats()["x.com"]
    assert stats["ema_latency"] == pytest.approx(1.0)
    assert stats["consecutive_failures"] == 3

    # 成功后对称回落：3.375 / 1.5 = 2.25
    at.note_response("x.com", 1.0, True)
    assert at.get_delay("x.com") == pytest.approx(2.25)
    assert at.stats()["x.com"]["consecutive_failures"] == 0
    # 2.25 → 1.5 → 1.0（下限），再成功不再下降
    at.note_response("x.com", 1.0, True)
    at.note_response("x.com", 1.0, True)
    assert at.get_delay("x.com") == pytest.approx(1.0)
    at.note_response("x.com", 1.0, True)
    assert at.stats()["x.com"]["penalty"] == pytest.approx(1.0)


def test_penalty_capped_at_max_penalty() -> None:
    """惩罚倍率封顶：1.5^10 被钳到 max_penalty=4.0。"""
    at = AutoThrottle(alpha=1.0, penalty_factor=1.5, max_penalty=4.0)
    at.note_response("x.com", 1.0, True)
    for _ in range(10):
        at.note_response("x.com", 1.0, False)
    assert at.stats()["x.com"]["penalty"] == pytest.approx(4.0)
    assert at.get_delay("x.com") == pytest.approx(4.0)


def test_stats_structure() -> None:
    """stats() 返回 {domain: {ema_latency/delay/penalty/连续失败/样本数}}。"""
    at = AutoThrottle(alpha=1.0)
    at.note_response("a.com", 0.5, True)
    at.note_response("b.com", 1.0, False)
    stats = at.stats()
    assert set(stats) == {"a.com", "b.com"}
    expected_fields = {"ema_latency", "delay", "penalty", "consecutive_failures", "samples"}
    for fields in stats.values():
        assert set(fields) == expected_fields
    assert stats["a.com"]["ema_latency"] == pytest.approx(0.5)
    assert stats["a.com"]["delay"] == pytest.approx(0.5)
    assert stats["a.com"]["samples"] == 1
    # b.com 只有失败样本：ema 为 None，惩罚 1.5，delay 走 start_delay*penalty
    assert stats["b.com"]["ema_latency"] is None
    assert stats["b.com"]["penalty"] == pytest.approx(1.5)
    assert stats["b.com"]["delay"] == pytest.approx(0.0)
    assert stats["b.com"]["consecutive_failures"] == 1


def test_domains_are_isolated() -> None:
    """各域独立记账，互不影响。"""
    at = AutoThrottle(alpha=1.0)
    at.note_response("a.com", 1.0, True)
    assert at.get_delay("a.com") == pytest.approx(1.0)
    assert at.get_delay("b.com") == pytest.approx(0.0)
    assert set(at.stats()) == {"a.com"}


def test_domain_matching_case_insensitive() -> None:
    """域键大小写不敏感。"""
    at = AutoThrottle(alpha=1.0)
    at.note_response("X.COM", 1.0, True)
    assert at.get_delay("x.com") == pytest.approx(1.0)
    assert at.stats()["x.com"]["samples"] == 1


def test_negative_elapsed_clamped_to_zero() -> None:
    """负 elapsed（时钟回拨等）按 0 处理，不污染 EMA。"""
    at = AutoThrottle(alpha=1.0)
    at.note_response("x.com", -5.0, True)
    assert at.stats()["x.com"]["ema_latency"] == pytest.approx(0.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"alpha": 0.0},
        {"alpha": 1.5},
        {"target_concurrency": 0.0},
        {"target_concurrency": -1.0},
        {"min_delay": -1.0},
        {"min_delay": 0.5, "max_delay": 0.1},
        {"penalty_factor": 1.0},
        {"penalty_factor": 0.5},
        {"max_penalty": 0.9},
        {"max_domains": 0},
        {"max_domains": -1},
    ],
)
def test_invalid_params_raise(kwargs: dict[str, float]) -> None:
    """非法参数组合在构造期即抛 ValueError。"""
    with pytest.raises(ValueError):
        AutoThrottle(**kwargs)  # type: ignore[arg-type]


def test_thread_safety_smoke() -> None:
    """多线程并发反馈不丢样本（锁保护的字典读写）。"""
    at = AutoThrottle(alpha=0.3)

    def hammer(domain: str) -> None:
        for _ in range(200):
            at.note_response(domain, 0.1, True)

    threads = [threading.Thread(target=hammer, args=(f"d{i}.com",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stats = at.stats()
    assert sum(s["samples"] for s in stats.values()) == 8 * 200
    for s in stats.values():
        assert s["ema_latency"] == pytest.approx(0.1, abs=1e-9)


def test_domain_lru_evicts_least_recently_noted() -> None:
    """max_domains LRU 上限：淘汰最久未反馈的域，被淘汰域重新出现时冷启动。"""
    at = AutoThrottle(alpha=1.0, max_domains=4)
    # 依次反馈 4 个域：LRU 顺序（旧 → 新）为 a, b, c, d
    for d in ("a.com", "b.com", "c.com", "d.com"):
        at.note_response(d, 1.0, True)
    assert set(at.stats()) == {"a.com", "b.com", "c.com", "d.com"}

    # 触碰 b（move_to_end）：顺序变为 a, c, d, b；新域 e 进入 → 淘汰最旧的 a
    at.note_response("b.com", 2.0, True)
    at.note_response("e.com", 1.0, True)
    stats = at.stats()
    assert set(stats) == {"b.com", "c.com", "d.com", "e.com"}
    assert stats["b.com"]["ema_latency"] == pytest.approx(2.0)  # 触碰即更新 EMA
    assert stats["e.com"]["samples"] == 1  # 新域冷启动

    # 被淘汰的 a 重新出现：冷启动（EMA 从首样本起步、samples 归零）
    at.note_response("a.com", 0.5, True)
    stats = at.stats()
    assert set(stats) == {"b.com", "d.com", "e.com", "a.com"}  # c 成为最旧被淘汰
    assert stats["a.com"]["ema_latency"] == pytest.approx(0.5)
    assert stats["a.com"]["samples"] == 1


def test_read_paths_do_not_touch_lru_order() -> None:
    """读路径（get_delay / stats）不触碰 LRU 顺序：不会把最旧域"救活"。"""
    at = AutoThrottle(alpha=1.0, max_domains=2)
    at.note_response("a.com", 1.0, True)
    at.note_response("b.com", 1.0, True)
    for _ in range(5):
        at.get_delay("a.com")  # 反复读最旧的 a
    at.stats()
    at.note_response("c.com", 1.0, True)  # 超限 → 仍淘汰 a（读不重排）
    assert set(at.stats()) == {"b.com", "c.com"}


# ===========================================================================
# Spider 集成：假时钟 + 假 sleep 的确定性时序断言
# ===========================================================================

HOST = "shop.example.com"
URLS = [f"https://{HOST}/p{i}" for i in range(1, 5)]


class FakeClock:
    """受控假时钟：monotonic() 读数仅由 advance() 推进。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, dt: float) -> None:
        self._now += dt


class FakeTimedFetcher:
    """按脚本注入下载耗时与状态码的 fetcher（同步/异步双路径）。

    耗时通过推进假时钟模拟，不真实等待；durations/statuses 逐次消耗，
    耗尽后沿用各自的最后一个值。
    """

    def __init__(
        self,
        clock: FakeClock,
        durations: list[float],
        statuses: list[int] | None = None,
    ) -> None:
        self._clock = clock
        self._durations = durations
        self._statuses = statuses or []
        self.calls: list[str] = []

    def _respond(self, url: str) -> Response:
        i = len(self.calls)
        self.calls.append(url)
        dt = self._durations[min(i, len(self._durations) - 1)]
        status = self._statuses[i] if i < len(self._statuses) else 200
        self._clock.advance(dt)
        return Response(url, status, b"<html><body>ok</body></html>")

    def get(self, url: str, **kwargs: Any) -> Response:
        return self._respond(url)

    async def async_get(self, url: str, **kwargs: Any) -> Response:
        return self._respond(url)


def _install_fake_time(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> list[float]:
    """把 spider 模块命名空间内的 time 换成假时钟；返回同步 sleep 记录表。

    只替换 ``web_crawler.spider.spider`` 内的 ``time`` 绑定，不碰全局
    time 模块；假 sleep 记录时长并推进假时钟（与真实时间流逝一致）。
    """
    sync_sleeps: list[float] = []

    def fake_sync_sleep(seconds: float) -> None:
        sync_sleeps.append(seconds)
        clock.advance(seconds)

    fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=fake_sync_sleep)
    monkeypatch.setattr("web_crawler.spider.spider.time", fake_time)
    return sync_sleeps


def _install_fake_async_sleep(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> list[float]:
    """把 asyncio.sleep 换成记录 + 推进假时钟的假实现；返回记录表。"""
    async_sleeps: list[float] = []

    async def fake_async_sleep(delay: float) -> None:
        async_sleeps.append(delay)
        clock.advance(delay)

    monkeypatch.setattr("asyncio.sleep", fake_async_sleep)
    return async_sleeps


class ThrottleSpySpider(Spider):
    """记录每个响应到达时该域当前 autothrottle 延迟的测试 spider。

    max_concurrency=1 保证串行调度，节流时序完全确定。
    """

    name = "throttle_spy"
    max_concurrency = 1
    download_delay = 0.0

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.delay_log: list[float] = []

    def parse(self, response: Response) -> Any:
        assert self.autothrottle is not None
        self.delay_log.append(self.autothrottle.get_delay(HOST))
        return None


def _make_spider(fetcher: FakeTimedFetcher, autothrottle: AutoThrottle | None) -> ThrottleSpySpider:
    spider = ThrottleSpySpider(fetcher=fetcher, autothrottle=autothrottle)
    spider.start_urls = list(URLS)  # type: ignore[misc]
    return spider


def test_stream_delay_escalates_on_slow_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """响应变慢后延迟随之上调：[0.2, 0.2, 1.0, 1.0]（alpha=1 时 EMA=最新样本）。

    下载耗时恰好填满间隔，无需额外补足 sleep——这正是 AutoThrottle 的
    设计目标（让下载间隔 ≈ 响应延迟）。
    """
    clock = FakeClock()
    async_sleeps = _install_fake_async_sleep(monkeypatch, clock)
    _install_fake_time(monkeypatch, clock)
    at = AutoThrottle(alpha=1.0, target_concurrency=1.0)
    fetcher = FakeTimedFetcher(clock, durations=[0.2, 0.2, 1.0, 1.0])
    spider = _make_spider(fetcher, at)

    async def collect() -> list[Any]:
        return [item async for item in spider.stream()]

    asyncio.run(collect())
    assert fetcher.calls == URLS
    assert spider.delay_log == pytest.approx([0.2, 0.2, 1.0, 1.0])
    assert async_sleeps == pytest.approx([])
    # 运行结束后的快照：EMA 收敛到 1.0
    assert spider.stats.autothrottle is not None
    assert spider.stats.autothrottle[HOST]["ema_latency"] == pytest.approx(1.0)


def test_stream_throttles_when_faster_than_ema(monkeypatch: pytest.MonkeyPatch) -> None:
    """下载比 EMA 快时补足间隔：先慢(1.0)后快(0.1)，sleep 序列确定。

    推演（alpha=0.3, tc=1, start=0；fake sleep 与 fetch 都推进假时钟）：
    - p1: 无样本 delay=0，不节流。t 0→1.0，ema=1.0
    - p2: delay=1.0，wait=0+1.0-1.0=0（不睡）。t→1.1，ema=0.73
    - p3: delay=0.73，wait=1.0+0.73-1.1=0.63。t→1.83，ema=0.541
    - p4: delay=0.541，wait=1.73+0.541-1.83=0.441
    """
    clock = FakeClock()
    async_sleeps = _install_fake_async_sleep(monkeypatch, clock)
    _install_fake_time(monkeypatch, clock)
    at = AutoThrottle(alpha=0.3, target_concurrency=1.0)
    fetcher = FakeTimedFetcher(clock, durations=[1.0, 0.1, 0.1, 0.1])
    spider = _make_spider(fetcher, at)

    async def collect() -> list[Any]:
        return [item async for item in spider.stream()]

    asyncio.run(collect())
    assert async_sleeps == pytest.approx([0.63, 0.441])
    assert spider.delay_log == pytest.approx([1.0, 0.73, 0.541, 0.4087])


def test_run_sync_matches_async_throttling(monkeypatch: pytest.MonkeyPatch) -> None:
    """同步 run() 路径与异步 stream() 节流语义一致：sleep 序列相同。"""
    clock = FakeClock()
    sync_sleeps = _install_fake_time(monkeypatch, clock)
    at = AutoThrottle(alpha=0.3, target_concurrency=1.0)
    fetcher = FakeTimedFetcher(clock, durations=[1.0, 0.1, 0.1, 0.1])
    spider = _make_spider(fetcher, at)

    spider.run()
    assert sync_sleeps == pytest.approx([0.63, 0.441])
    assert spider.stats.pages_crawled == 4
    assert spider.delay_log == pytest.approx([1.0, 0.73, 0.541, 0.4087])


def test_stream_failure_penalty_then_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """5xx 触发惩罚放大（0.2→0.3），成功后衰减回落（0.3→0.2）。"""
    clock = FakeClock()
    _install_fake_async_sleep(monkeypatch, clock)
    _install_fake_time(monkeypatch, clock)
    at = AutoThrottle(alpha=1.0, penalty_factor=1.5)
    fetcher = FakeTimedFetcher(clock, durations=[0.2, 0.2, 0.2, 0.2], statuses=[200, 200, 500, 200])
    spider = _make_spider(fetcher, at)

    async def collect() -> list[Any]:
        return [item async for item in spider.stream()]

    asyncio.run(collect())
    # 第 3 个响应是 500：success=False → penalty=1.5 → delay=0.2*1.5=0.3；
    # 第 4 个成功 → penalty 回落 1.0 → delay=0.2
    assert spider.delay_log == pytest.approx([0.2, 0.2, 0.3, 0.2])


def test_stream_request_failure_noted_as_penalty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetcher 抛异常（超时类失败）也反馈：惩罚放大，计入样本。"""

    class FlakyFetcher:
        def __init__(self, clock: FakeClock) -> None:
            self._clock = clock

        async def async_get(self, url: str, **kwargs: Any) -> Response:
            self._clock.advance(0.2)
            raise ConnectionError("timeout")

    clock = FakeClock()
    _install_fake_async_sleep(monkeypatch, clock)
    _install_fake_time(monkeypatch, clock)
    at = AutoThrottle(alpha=1.0, penalty_factor=1.5)
    spider = _make_spider(FlakyFetcher(clock), at)

    async def collect() -> list[Any]:
        return [item async for item in spider.stream()]

    items = asyncio.run(collect())
    assert items == []
    stats = at.stats()[HOST]
    # 4 次请求全部失败：ema 保持 None（失败不进 EMA），penalty 封顶放大
    assert stats["ema_latency"] is None
    assert stats["samples"] == 4
    assert stats["consecutive_failures"] == 4
    assert stats["penalty"] == pytest.approx(1.5**4)
    # 无成功样本 → delay 走 start_delay * penalty = 0.0（min_delay 默认 0）
    assert stats["delay"] == pytest.approx(0.0)


def test_default_off_zero_behavior_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """不传 autothrottle：不节流、不 sleep、stats 无 autothrottle 键。"""
    clock = FakeClock()
    sync_sleeps = _install_fake_time(monkeypatch, clock)
    async_sleeps = _install_fake_async_sleep(monkeypatch, clock)
    fetcher = FakeTimedFetcher(clock, durations=[0.5, 0.5, 0.5, 0.5])

    class S(Spider):
        name = "plain"
        download_delay = 0.0

        def parse(self, response: Response) -> Any:
            yield {"url": response.url}

    spider = S(fetcher=fetcher)
    spider.start_urls = list(URLS)  # type: ignore[misc]
    items = spider.run()
    assert len(items) == 4
    assert spider.stats.pages_crawled == 4
    assert sync_sleeps == []  # 无 per_domain_delay / download_delay：零等待
    assert async_sleeps == []
    assert spider.stats.autothrottle is None
    assert "autothrottle" not in spider.stats.as_dict()
    assert spider.autothrottle is None


def test_stats_snapshot_exposed_after_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """运行结束后 stats.autothrottle 有快照，as_dict() 透出该键。"""
    clock = FakeClock()
    _install_fake_time(monkeypatch, clock)
    at = AutoThrottle(alpha=1.0)
    fetcher = FakeTimedFetcher(clock, durations=[0.3, 0.3, 0.3, 0.3])
    spider = _make_spider(fetcher, at)
    spider.run()

    d = spider.stats.as_dict()
    assert "autothrottle" in d
    snapshot = d["autothrottle"][HOST]
    assert snapshot["ema_latency"] == pytest.approx(0.3)
    assert snapshot["delay"] == pytest.approx(0.3)
    assert snapshot["samples"] == 4
    assert snapshot["consecutive_failures"] == 0


def test_per_domain_delay_stacks_with_autothrottle() -> None:
    """叠加语义：生效延迟取 max(per_domain_delay, autothrottle 延迟)。"""
    clock = FakeClock()
    fetcher = FakeTimedFetcher(clock, durations=[0.2])
    at = AutoThrottle(alpha=1.0)
    spider = _make_spider(fetcher, at)
    spider.per_domain_delay = {"example.com": 0.5}

    # autothrottle 延迟 0.2 < 静态下限 0.5 → 取 0.5，账本键为静态匹配域
    at.note_response(HOST, 0.2, True)
    assert spider._throttle_delay_for(f"https://{HOST}/x") == (0.5, "example.com")

    # 响应变慢：autothrottle 延迟 2.0 > 静态下限 → 取 2.0，账本键为 host
    at.note_response(HOST, 2.0, True)
    assert spider._throttle_delay_for(f"https://{HOST}/x") == (2.0, HOST)


def test_autothrottle_off_matches_legacy_lookup() -> None:
    """autothrottle=None 时 _throttle_delay_for 与 _domain_delay_for 逐字等价。"""
    clock = FakeClock()
    fetcher = FakeTimedFetcher(clock, durations=[0.2])
    spider = _make_spider(fetcher, None)
    spider.per_domain_delay = {"example.com": 0.5}
    url = f"https://{HOST}/x"
    assert spider._throttle_delay_for(url) == spider._domain_delay_for(url)
    spider.per_domain_delay = {}
    assert spider._throttle_delay_for(url) == spider._domain_delay_for(url)


def test_request_without_host_uses_empty_domain_key() -> None:
    """无 host 的 URL 退化为空域键，get_delay 走无样本路径不崩溃。"""
    at = AutoThrottle(alpha=1.0)
    at.note_response("", 1.0, True)
    assert at.get_delay("") == pytest.approx(1.0)


# ===========================================================================
# 多线程并发压测：混合读写不变量（钉死「单锁足够、无需分片锁」结论）
# ===========================================================================


def test_concurrent_mixed_workload_stress() -> None:
    """8 线程 × N 次混合 note_response / get_delay / stats 并发压测。

    单把 ``threading.Lock`` + 纯内存纳秒级临界区（见 AutoThrottle 类
    docstring「并发边界」）在高频混合读写下的不变量：

    * 无异常（任何竞态崩溃 / KeyError / 数值炸开都会被抓到）；
    * 样本零丢失：无淘汰场景（8 域 << 65536 上限）下每域 samples
      精确等于该域收到的 note_response 次数；
    * EMA 数值自洽：alpha=1.0 时 EMA 恒等于该域最后一次成功样本。
    """
    at = AutoThrottle(alpha=1.0)
    n_threads, n_iters = 8, 400
    errors: list[BaseException] = []
    last_success: dict[str, float] = {}
    collect = threading.Lock()

    def hammer(idx: int) -> None:
        domain = f"s{idx}.example.com"
        local_last = -1.0
        try:
            for i in range(n_iters):
                elapsed = 0.05 + (i % 7) * 0.01
                at.note_response(domain, elapsed, success=(i % 3 != 0))
                # 混合读：邻居域的 get_delay + 全局 stats 快照
                at.get_delay(f"s{(idx + 1) % n_threads}.example.com")
                at.stats()
                if i % 3 != 0:
                    local_last = elapsed
            with collect:
                last_success[domain] = local_last
        except BaseException as exc:
            with collect:
                errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发压测出现异常: {errors!r}"
    stats = at.stats()
    # 域数守恒：无淘汰（域数远小于 max_domains）
    assert set(stats) == {f"s{i}.example.com" for i in range(n_threads)}
    # 样本零丢失 + EMA 自洽（alpha=1.0 → EMA == 最后一次成功样本）
    for domain, snap in stats.items():
        assert snap["samples"] == n_iters, domain
        assert snap["ema_latency"] == pytest.approx(last_success[domain]), domain
    assert sum(snap["samples"] for snap in stats.values()) == n_threads * n_iters


def test_concurrent_lru_cap_holds_under_eviction_stress() -> None:
    """海量唯一域并发反馈下的 LRU 上限守恒（淘汰重置 samples，断言上界）。

    8 线程 × 150 个互不相同的域（共 1200 域）打满 ``max_domains=4`` 的
    小上限：并发淘汰最久未反馈域时，域数恒不超过上限；被淘汰域冷启动
    重置 samples，总量只能断言上界（<= 总反馈次数）。
    """
    at = AutoThrottle(alpha=0.3, max_domains=4)
    n_threads, n_iters = 8, 150
    errors: list[BaseException] = []
    collect = threading.Lock()

    def hammer(idx: int) -> None:
        try:
            for i in range(n_iters):
                at.note_response(f"t{idx}-{i}.example.com", 0.1, True)
                at.get_delay(f"t{idx}-{i}.example.com")
        except BaseException as exc:
            with collect:
                errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发压测出现异常: {errors!r}"
    stats = at.stats()
    # LRU 上限守恒：任意并发交错下域数恒 <= max_domains
    assert len(stats) <= 4
    # 淘汰重置 samples（冷启动）：总量按上界断言
    assert sum(snap["samples"] for snap in stats.values()) <= n_threads * n_iters

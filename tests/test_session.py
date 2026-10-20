"""SessionPool（会话池 + 指纹随会话轮换）的单元与集成测试。

单元部分用可注入的假会话工厂 / 假时钟验证池的生命周期语义（借还、上限、
指纹轮换分布、空闲回收、失败淘汰、stats 结构、无 curl_cffi 降级）；集成
部分复用 test_ssrf_pin 的 fake curl 后端模式验证 Fetcher / AsyncFetcher 的
acquire → 请求（含 DNS 钉扎）→ release 全链路，另配一组真实 curl_cffi
对本地 HTTP 服务器的冒烟用例；末尾配同步侧多线程并发借还压测与
stats() 并发快照竞态回归（对应内置规则审查 M1/L7）。
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from web_crawler._ssrf import _dns_verdict_cache
from web_crawler.compat import HAS_CURL_CFFI
from web_crawler.fetchers import _pin as _pin_mod
from web_crawler.fetchers import fetcher as fetcher_mod
from web_crawler.fetchers import session as session_mod
from web_crawler.fetchers.fetcher import AsyncFetcher, Fetcher
from web_crawler.fetchers.session import DEFAULT_FINGERPRINTS, SessionHandle, SessionPool

# 代替 CurlOpt.RESOLVE 的哨兵键：钉扎写入 curl_options 的键与真实枚举等价
_RESOLVE_OPT = object()


# ── fake 设施 ────────────────────────────────────────────────────────────────


class _FakeRawResponse:
    """传输层原始响应的最小替身（200 / 无重定向 / 空 body）。"""

    url = "https://example.com/"
    status_code = 200
    content = b"ok"
    headers: dict[str, str] = {}


class _FakeSyncSession:
    """同步会话替身：记录构造参数、request 调用、curl_options 写入与关闭。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = dict(kwargs)
        self.curl_options: dict[Any, Any] = {}
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    def request(self, **kwargs: Any) -> _FakeRawResponse:
        self.requests.append(kwargs)
        return _FakeRawResponse()

    def close(self) -> None:
        self.closed = True


class _FailingSyncSession(_FakeSyncSession):
    """request 恒抛错的同步替身（失败淘汰路径）。"""

    def request(self, **kwargs: Any) -> _FakeRawResponse:
        self.requests.append(kwargs)
        raise RuntimeError("transport failure")


class _FakeAsyncSession(_FakeSyncSession):
    """异步会话替身：只有 async close（覆盖 aclose 优先分支的兼容路径）。"""

    async def request(self, **kwargs: Any) -> _FakeRawResponse:
        self.requests.append(kwargs)
        return _FakeRawResponse()

    async def close(self) -> None:
        self.closed = True


class _FailingAsyncSession(_FailingSyncSession):
    """request 恒抛错的异步替身。"""

    async def request(self, **kwargs: Any) -> _FakeRawResponse:
        self.requests.append(kwargs)
        raise RuntimeError("transport failure")

    async def close(self) -> None:
        self.closed = True


class _FakeAsyncSessionWithAclose(_FakeAsyncSession):
    """带 aclose 的异步替身（aclose 优先路径）。"""

    async def aclose(self) -> None:
        self.closed = True


class _RecordingFactory:
    """会话工厂替身：记录指纹分配序列与创建的会话实例。"""

    def __init__(self, cls: type = _FakeSyncSession) -> None:
        self.cls = cls
        self.fingerprints: list[str | None] = []
        self.sessions: list[Any] = []

    def __call__(self, impersonate: str | None) -> Any:
        self.fingerprints.append(impersonate)
        session = self.cls()
        self.sessions.append(session)
        return session


class _FakeClock:
    """可控单调时钟（空闲回收测试的 mock 时间源）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeCurlVersion:
    """curl_cffi.CurlHttpVersion 的最小替身（仅需 V1_1 属性存在）。"""

    V1_1 = "v1_1"


def _public_resolver(ips: list[str]) -> Any:
    """返回恒定公网解析结果的 fake ``getaddrinfo``（钉扎测试用）。"""

    def getaddrinfo(host: str, port: object, *args: object, **kwargs: object) -> list[tuple]:
        infos: list[tuple] = []
        for ip in ips:
            if ":" in ip:
                infos.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0)))
            else:
                infos.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)))
        return infos

    return getaddrinfo


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例独立环境：清代理环境变量（钉扎跳过条件）与 DNS 判定缓存。"""
    for var in _pin_mod._ENV_PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    _dns_verdict_cache._data.clear()


def _use_fake_curl(
    monkeypatch: pytest.MonkeyPatch,
    sync_cls: type = _FakeSyncSession,
    async_cls: type = _FakeAsyncSession,
    error: type = RuntimeError,
) -> None:
    """让 Fetcher 走 curl 分支并绑定 fake session（无需安装 curl_cffi）。"""
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", True)
    # 池侧也要打桩：SessionPool 读的是 session 模块内的同名常量，只改 fetcher
    # 那份会让指纹维度整体降级为 None，与「假装装了 curl_cffi」的自相矛盾。
    monkeypatch.setattr(session_mod, "HAS_CURL_CFFI", True)
    monkeypatch.setattr(
        fetcher_mod,
        "_load_curl_backend",
        lambda: (_FakeCurlVersion, sync_cls, async_cls, error),
    )
    monkeypatch.setattr(_pin_mod, "_curl_opt_resolve", lambda: _RESOLVE_OPT)


# ── 一、构造校验与基础语义 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"strategy": "bogus"},
        {"max_size": 0},
        {"max_size": -1},
        {"max_idle": -1.0},
        {"max_failures": 0},
        {"prewarm": -1},
        {"prewarm": 9},  # 超过默认 max_size=8
    ],
)
def test_constructor_rejects_bad_arguments(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        SessionPool(**kwargs)


def test_constructor_rejects_empty_fingerprints() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        SessionPool([])


def test_acquire_release_roundtrip() -> None:
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory)
    handle = pool.acquire()
    assert pool.stats()["active"] == 1
    assert pool.stats()["idle"] == 0
    pool.release(handle)
    stats = pool.stats()
    assert stats["active"] == 0
    assert stats["idle"] == 1
    assert len(factory.sessions) == 1
    assert factory.sessions[0].closed is False
    pool.close()


def test_acquire_reuses_released_session() -> None:
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory)
    first = pool.acquire()
    pool.release(first)
    second = pool.acquire()
    assert second.session is first.session  # 复用而非重建
    assert len(factory.sessions) == 1
    pool.release(second)
    pool.close()


def test_idle_reuse_is_lifo() -> None:
    """空闲复用按 LIFO：最近归还的会话先被取用（热连接优先）。"""
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory, max_size=4)
    h1, h2 = pool.acquire(), pool.acquire()
    pool.release(h1)
    pool.release(h2)
    again = pool.acquire()
    assert again.session is h2.session
    pool.release(again)
    pool.close()


def test_release_unknown_handle_is_noop() -> None:
    """重复归还 / 未知句柄宽容 no-op（对齐 ProxyPool 的宽容语义）。"""
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory)
    handle = pool.acquire()
    pool.release(handle)
    pool.release(handle)  # 重复归还
    pool.release(SessionHandle(session=object(), fingerprint=None, last_used=0.0))
    assert pool.stats()["idle"] == 1
    pool.close()


def test_sync_release_ignores_async_handle_and_vice_versa() -> None:
    """同步/异步 API 必须配对：错误一侧的归还按 no-op 处理。"""
    factory = _RecordingFactory()
    async_factory = _RecordingFactory(_FakeAsyncSession)
    pool = SessionPool(session_factory=factory, async_session_factory=async_factory)

    async def go() -> None:
        handle = await pool.acquire_async()
        pool.release(handle)  # 错误一侧：no-op，条目仍留在异步借出集合
        assert pool.stats()["async_active"] == 1
        await pool.release_async(handle)  # 正确一侧
        assert pool.stats()["idle"] == 1

    asyncio.run(go())
    pool.close()


# ── 二、池上限与临时会话 ─────────────────────────────────────────────────────


def test_overflow_acquires_ephemeral_sessions() -> None:
    """池满时临时会话应急（永不阻塞），归还即关闭、不占池缓存。"""
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory, max_size=2)
    h1, h2 = pool.acquire(), pool.acquire()
    h3 = pool.acquire()  # 池满：临时会话
    assert h3.ephemeral is True
    assert h1.ephemeral is False
    assert pool.stats()["active"] == 3  # 临时会话也计入借出
    pool.release(h3)
    assert factory.sessions[2].closed is True
    assert pool.stats()["discarded_overflow"] == 1
    pool.release(h1)
    pool.release(h2)
    stats = pool.stats()
    assert stats["idle"] == 2  # 池内缓存回落到上限内
    assert stats["active"] == 0
    pool.close()


# ── 三、指纹轮换 ─────────────────────────────────────────────────────────────


def test_fingerprint_rotation_round_robin(monkeypatch: pytest.MonkeyPatch) -> None:
    # 轮换算法与是否安装 curl_cffi 无关：显式假定可用，否则结果随装机环境漂移
    monkeypatch.setattr(session_mod, "HAS_CURL_CFFI", True)
    factory = _RecordingFactory()
    pool = SessionPool(["fp-a", "fp-b", "fp-c"], max_size=8, session_factory=factory)
    handles = [pool.acquire() for _ in range(5)]
    assert [h.fingerprint for h in handles] == ["fp-a", "fp-b", "fp-c", "fp-a", "fp-b"]
    assert factory.fingerprints == ["fp-a", "fp-b", "fp-c", "fp-a", "fp-b"]
    pool.close()


def test_fingerprint_rotation_random(monkeypatch: pytest.MonkeyPatch) -> None:
    # 同 test_fingerprint_rotation_round_robin：假定 curl_cffi 可用
    monkeypatch.setattr(session_mod, "HAS_CURL_CFFI", True)
    factory = _RecordingFactory()
    pool = SessionPool(["fp-a", "fp-b"], strategy="random", max_size=8, session_factory=factory)
    handles = [pool.acquire() for _ in range(10)]
    assert all(h.fingerprint in {"fp-a", "fp-b"} for h in handles)
    pool.close()


def test_stats_fingerprint_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    # 同 test_fingerprint_rotation_round_robin：假定 curl_cffi 可用
    monkeypatch.setattr(session_mod, "HAS_CURL_CFFI", True)
    factory = _RecordingFactory()
    pool = SessionPool(["fp-a", "fp-a", "fp-b"], max_size=8, session_factory=factory)
    for _ in range(3):
        pool.acquire()
    assert pool.stats()["fingerprints"] == {"fp-a": 2, "fp-b": 1}
    pool.close()


def test_missing_curl_cffi_skips_fingerprint_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 curl_cffi 环境：指纹维度跳过（工厂收到 None），httpx 池化仍可用。"""
    monkeypatch.setattr(session_mod, "HAS_CURL_CFFI", False)
    factory = _RecordingFactory()
    pool = SessionPool(["fp-a", "fp-b"], session_factory=factory)
    handle = pool.acquire()
    assert handle.fingerprint is None
    assert factory.fingerprints == [None]
    assert pool.stats()["fingerprints"] == {None: 1}
    pool.release(handle)
    pool.close()


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi not installed")
def test_default_fingerprints_accepted_by_curl_cffi() -> None:
    """默认指纹列表必须全部被当前 curl_cffi 接受（版本升级守护）。"""
    from curl_cffi.requests.impersonate import BrowserType

    supported = {b.value for b in BrowserType}
    assert set(DEFAULT_FINGERPRINTS) <= supported


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi not installed")
def test_default_factory_builds_curl_session() -> None:
    pool = SessionPool(["chrome131"])
    handle = pool.acquire()
    try:
        from curl_cffi.requests import Session as CurlSession

        assert isinstance(handle.session, CurlSession)
        assert handle.fingerprint == "chrome131"
    finally:
        pool.release(handle)
    pool.close()


def test_default_factory_falls_back_to_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "HAS_CURL_CFFI", False)
    pool = SessionPool()
    handle = pool.acquire()
    try:
        import httpx

        assert isinstance(handle.session, httpx.Client)
        assert handle.fingerprint is None
    finally:
        pool.release(handle)
    pool.close()


# ── 四、空闲回收（mock 时间）与失败淘汰 ───────────────────────────────────────


def test_idle_reaping_after_max_idle() -> None:
    clock = _FakeClock()
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory, max_idle=60.0, clock=clock)
    handle = pool.acquire()
    pool.release(handle)
    clock.advance(60.1)
    fresh = pool.acquire()  # 惰性回收：取用时逐出超时会话
    assert fresh.session is not handle.session
    assert handle.session.closed is True
    stats = pool.stats()
    assert stats["evicted_idle"] == 1
    assert stats["active"] == 1
    assert stats["idle"] == 0
    pool.release(fresh)
    pool.close()


def test_idle_session_kept_at_exact_boundary() -> None:
    """空闲时长恰好等于 max_idle 不回收（严格大于判定）。"""
    clock = _FakeClock()
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory, max_idle=60.0, clock=clock)
    handle = pool.acquire()
    pool.release(handle)
    clock.advance(60.0)
    again = pool.acquire()
    assert again.session is handle.session
    assert pool.stats()["evicted_idle"] == 0
    pool.release(again)
    pool.close()


def test_release_failure_evicts_after_threshold() -> None:
    """连续失败达 max_failures：淘汰并重建（指纹轮换序继续推进）。"""
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory, max_failures=2)
    handle = pool.acquire()
    pool.release(handle, success=False)
    again = pool.acquire()
    assert again.session is handle.session  # 未达阈值：仍在池中
    pool.release(again, success=False)  # 连续第 2 次：淘汰
    assert handle.session.closed is True
    stats = pool.stats()
    assert stats["evicted_failed"] == 1
    assert stats["idle"] == 0
    third = pool.acquire()
    assert third.session is not handle.session
    pool.release(third)
    pool.close()


def test_release_success_resets_failure_streak() -> None:
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory, max_failures=2)
    handle = pool.acquire()
    pool.release(handle, success=False)
    handle = pool.acquire()
    pool.release(handle, success=True)  # 成功清零
    handle = pool.acquire()
    pool.release(handle, success=False)  # 重新累计的第 1 次（< 阈值）
    assert handle.session.closed is False
    assert pool.stats()["idle"] == 1
    pool.close()


def test_mark_failed_marks_stale_then_evicts_on_release() -> None:
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory, max_failures=2)
    handle = pool.acquire()
    pool.mark_failed(handle)
    pool.mark_failed(handle)  # 借出中累计达阈值
    assert handle.stale is True
    pool.release(handle)  # 归还即淘汰
    assert handle.session.closed is True
    assert pool.stats()["evicted_failed"] == 1
    pool.close()


def test_mark_failed_ignores_released_handle() -> None:
    """已归还/未知句柄的 mark_failed 是 no-op（仅作用于借出中）。"""
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory)
    handle = pool.acquire()
    pool.release(handle)
    pool.mark_failed(handle)
    assert handle.failures == 0
    assert pool.stats()["idle"] == 1
    pool.close()


def test_mark_success_rescues_marked_session() -> None:
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory, max_failures=2)
    handle = pool.acquire()
    pool.mark_failed(handle)
    pool.mark_failed(handle)
    assert handle.stale is True
    pool.mark_success(handle)  # 解除 stale
    pool.release(handle)
    assert handle.session.closed is False
    assert pool.stats()["idle"] == 1
    pool.close()


# ── 五、预热与统计 ───────────────────────────────────────────────────────────


def test_prewarm_builds_sync_sessions_upfront(monkeypatch: pytest.MonkeyPatch) -> None:
    # 预热同样按指纹分配会话，故假定 curl_cffi 可用
    monkeypatch.setattr(session_mod, "HAS_CURL_CFFI", True)
    factory = _RecordingFactory()
    pool = SessionPool(["fp-a", "fp-b", "fp-c"], prewarm=2, session_factory=factory)
    stats = pool.stats()
    assert stats["idle"] == 2
    assert stats["created"] == 2
    assert stats["fingerprints"] == {"fp-a": 1, "fp-b": 1}
    handle = pool.acquire()
    assert handle.fingerprint == "fp-b"  # LIFO：最近预热的先被取用
    pool.release(handle)
    pool.close()


def test_stats_structure() -> None:
    pool = SessionPool(["fp-a"], max_size=3, max_idle=10.0)
    stats = pool.stats()
    assert set(stats) == {
        "strategy",
        "max_size",
        "max_idle",
        "active",
        "idle",
        "total",
        "sync_active",
        "async_active",
        "created",
        "evicted",
        "evicted_failed",
        "evicted_idle",
        "discarded_overflow",
        "fingerprints",
    }
    assert stats["strategy"] == "round_robin"
    assert stats["max_size"] == 3
    assert stats["max_idle"] == 10.0
    assert stats["fingerprints"] == {}
    pool.close()


def test_len_counts_pooled_sessions() -> None:
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory, max_size=4)
    assert len(pool) == 0
    h1, _ = pool.acquire(), pool.acquire()
    assert len(pool) == 2
    pool.release(h1)
    assert len(pool) == 2  # 借出 + 空闲都计入
    pool.close()
    assert len(pool) == 0


def test_repr_contains_strategy() -> None:
    pool = SessionPool(strategy="round_robin")
    assert "round_robin" in repr(pool)
    pool.close()


def test_bind_factories_is_first_wins() -> None:
    factory = _RecordingFactory()
    pool = SessionPool()
    pool.bind_factories(session_factory=factory, async_session_factory=factory)
    other = _RecordingFactory()
    pool.bind_factories(session_factory=other)  # 已绑定：不覆盖
    handle = pool.acquire()
    assert len(factory.sessions) == 1
    assert len(other.sessions) == 0
    pool.release(handle)
    pool.close()


# ── 六、生命周期 ─────────────────────────────────────────────────────────────


def test_close_closes_all_sessions_and_is_idempotent() -> None:
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory, max_size=4)
    h1, _ = pool.acquire(), pool.acquire()
    pool.release(h1)
    pool.close()  # 借出中的会话也一并关闭
    assert all(s.closed for s in factory.sessions)
    assert pool.stats()["total"] == 0
    pool.close()  # 幂等
    h3 = pool.acquire()  # 关闭后池仍可复用（按需新建）
    assert h3.session.closed is False
    pool.release(h3)
    pool.close()


def test_sync_context_manager_closes() -> None:
    factory = _RecordingFactory()
    with SessionPool(session_factory=factory, prewarm=1) as pool:
        assert pool.stats()["idle"] == 1
    assert factory.sessions[0].closed is True


async def test_async_context_manager_closes() -> None:
    factory = _RecordingFactory(_FakeAsyncSession)
    async with SessionPool(async_session_factory=factory) as pool:
        await pool.prewarm_async(1)
        assert pool.stats()["idle"] == 1
    assert factory.sessions[0].closed is True


# ── 七、异步池 ───────────────────────────────────────────────────────────────


async def test_async_acquire_release_roundtrip() -> None:
    factory = _RecordingFactory(_FakeAsyncSession)
    pool = SessionPool(async_session_factory=factory)
    handle = await pool.acquire_async()
    assert pool.stats()["active"] == 1
    await pool.release_async(handle)
    stats = pool.stats()
    assert stats["active"] == 0
    assert stats["idle"] == 1
    again = await pool.acquire_async()
    assert again.session is handle.session  # 复用
    await pool.release_async(again)
    await pool.aclose()
    assert handle.session.closed is True


async def test_async_overflow_ephemeral() -> None:
    factory = _RecordingFactory(_FakeAsyncSession)
    pool = SessionPool(async_session_factory=factory, max_size=1)
    h1 = await pool.acquire_async()
    h2 = await pool.acquire_async()  # 池满：临时会话
    assert h2.ephemeral is True
    await pool.release_async(h2)
    assert factory.sessions[1].closed is True
    assert pool.stats()["discarded_overflow"] == 1
    await pool.release_async(h1)
    await pool.aclose()


async def test_async_failure_eviction() -> None:
    factory = _RecordingFactory(_FakeAsyncSession)
    pool = SessionPool(async_session_factory=factory, max_failures=2)
    handle = await pool.acquire_async()
    await pool.release_async(handle, success=False)
    handle = await pool.acquire_async()  # 同一会话再失败
    await pool.release_async(handle, success=False)
    assert handle.session.closed is True
    assert pool.stats()["evicted_failed"] == 1
    await pool.aclose()


async def test_async_idle_reaping_with_fake_clock() -> None:
    clock = _FakeClock()
    factory = _RecordingFactory(_FakeAsyncSession)
    pool = SessionPool(async_session_factory=factory, max_idle=60.0, clock=clock)
    handle = await pool.acquire_async()
    await pool.release_async(handle)
    clock.advance(61.0)
    fresh = await pool.acquire_async()  # 取用时逐出（关闭延后到 await 点）
    assert fresh.session is not handle.session
    assert handle.session.closed is True
    assert pool.stats()["evicted_idle"] == 1
    await pool.release_async(fresh)
    await pool.aclose()


async def test_async_fingerprint_rotation_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步侧指纹轮换游标独立：与同步侧互不干扰。"""
    # 同 test_fingerprint_rotation_round_robin：假定 curl_cffi 可用
    monkeypatch.setattr(session_mod, "HAS_CURL_CFFI", True)
    factory = _RecordingFactory(_FakeAsyncSession)
    pool = SessionPool(["fp-a", "fp-b"], async_session_factory=factory)
    handles = [await pool.acquire_async() for _ in range(3)]
    assert [h.fingerprint for h in handles] == ["fp-a", "fp-b", "fp-a"]
    for handle in handles:
        await pool.release_async(handle)
    await pool.aclose()


async def test_async_aclose_prefers_aclose_method() -> None:
    factory = _RecordingFactory(_FakeAsyncSessionWithAclose)
    pool = SessionPool(async_session_factory=factory)
    handle = await pool.acquire_async()
    await pool.release_async(handle)
    await pool.aclose()
    assert factory.sessions[0].closed is True


def test_async_pool_survives_multiple_event_loops() -> None:
    """跨事件循环复用同一池：旧条目弃置重建，不抛错、不误关旧会话。"""
    factory = _RecordingFactory(_FakeAsyncSession)
    pool = SessionPool(async_session_factory=factory)

    async def cycle() -> None:
        handle = await pool.acquire_async()
        await pool.release_async(handle)

    asyncio.run(cycle())  # loop 1
    asyncio.run(cycle())  # loop 2：旧循环条目弃置、重建
    assert len(factory.sessions) == 2
    assert pool.stats()["idle"] == 1  # loop 2 的会话在池中
    asyncio.run(pool.aclose())  # loop 3：池绑定的是 loop 2 → 弃置而非关闭
    # 两个会话都未被关闭（各自绑定的 loop 已停，无法安全关闭——弃置是设计行为）
    assert factory.sessions[0].closed is False
    assert factory.sessions[1].closed is False
    assert pool.stats()["total"] == 0


async def test_prewarm_async_in_event_loop() -> None:
    factory = _RecordingFactory(_FakeAsyncSession)
    pool = SessionPool(["fp-a", "fp-b"], async_session_factory=factory)
    await pool.prewarm_async(2)
    assert len(factory.sessions) == 2
    handle = await pool.acquire_async()
    assert handle.session is factory.sessions[-1]  # LIFO
    await pool.release_async(handle)
    await pool.aclose()
    assert all(s.closed for s in factory.sessions)


# ── 八、Fetcher 集成（fake 后端：acquire → 请求（含钉扎）→ release） ──────────


def test_fetcher_binds_pool_factory_and_pipelines_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """零配置池传入 Fetcher：自动绑定工厂，请求走池化会话并复用。"""
    _use_fake_curl(monkeypatch)
    monkeypatch.setattr(socket, "getaddrinfo", _public_resolver(["93.184.216.34"]))
    pool = SessionPool(["chrome131", "safari184"])
    with Fetcher(session_pool=pool, timeout=5.0) as f:
        resp = f.get("https://example.com/")
        assert resp.status == 200
        assert f._session is None  # 单会话路径不触发
        stats = pool.stats()
        assert stats["sync_active"] == 0  # 已归还
        assert stats["idle"] == 1
        handle = pool.acquire()
        session = handle.session
        assert handle.fingerprint in {"chrome131", "safari184"}
        assert session.kwargs["impersonate"] == handle.fingerprint  # 工厂按池指纹构建
        assert len(session.requests) == 1
        pool.release(handle)
        f.get("https://example.com/")  # 第二次请求复用同一池化会话
        assert len(session.requests) == 2
        assert pool.stats()["created"] == 1
    pool.close()


def test_fetcher_pool_applies_dns_pinning(monkeypatch: pytest.MonkeyPatch) -> None:
    """池化请求仍走连接级 DNS 钉扎（CURLOPT_RESOLVE 写入借出的会话）。"""
    _use_fake_curl(monkeypatch)
    monkeypatch.setattr(socket, "getaddrinfo", _public_resolver(["93.184.216.34"]))
    pool = SessionPool(["chrome131"])
    with Fetcher(session_pool=pool, timeout=5.0, allow_private_hosts=False) as f:
        f.get("https://example.com/")
    handle = pool.acquire()
    assert handle.session.curl_options[_RESOLVE_OPT] == ["example.com:443:93.184.216.34"]
    pool.release(handle)
    pool.close()


def test_fetcher_pool_marks_failures_and_evicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """请求抛错按 success=False 归还：连续失败达阈值淘汰。"""
    _use_fake_curl(monkeypatch, sync_cls=_FailingSyncSession)
    monkeypatch.setattr(socket, "getaddrinfo", _public_resolver(["93.184.216.34"]))
    pool = SessionPool(max_failures=2)
    with Fetcher(session_pool=pool, timeout=5.0) as f:
        for _ in range(2):
            with pytest.raises(RuntimeError):
                f.get("https://example.com/")
    assert pool.stats()["evicted_failed"] == 1
    pool.close()


def test_fetcher_close_leaves_pool_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetcher 不拥有池：close() 只关自己的会话，池由持有者管理。"""
    _use_fake_curl(monkeypatch)
    monkeypatch.setattr(socket, "getaddrinfo", _public_resolver(["93.184.216.34"]))
    pool = SessionPool(["chrome131"])
    f = Fetcher(session_pool=pool, timeout=5.0)
    f.get("https://example.com/")
    f.close()
    assert pool.stats()["idle"] == 1  # 池会话未被 fetcher.close 关闭
    handle = pool.acquire()
    pool.release(handle)
    pool.close()


def test_fetcher_without_pool_uses_single_session_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：不传池（默认 None）时行为与历史版本一致（单会话路径）。"""
    _use_fake_curl(monkeypatch)
    monkeypatch.setattr(socket, "getaddrinfo", _public_resolver(["93.184.216.34"]))
    f = Fetcher(timeout=5.0)
    assert f.session_pool is None
    f.get("https://example.com/")
    assert f._session is not None  # 惰性单会话已创建
    f.close()


def test_fetcher_httpx_fallback_with_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx 兜底 + 池：连接池化生效（指纹维度跳过），代理路径不借池。"""

    class _FakeHTTPTransport:
        def __init__(self, proxy: str | None = None) -> None:
            self.proxy = proxy

    fake_httpx = type("fake_httpx", (), {})()
    fake_httpx.HTTPTransport = _FakeHTTPTransport
    fake_httpx.AsyncHTTPTransport = _FakeHTTPTransport
    fake_httpx.HTTPError = type("HTTPError", (Exception,), {})
    fake_httpx.Client = _FakeSyncSession
    fake_httpx.AsyncClient = _FakeAsyncSession

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)
    # 池侧同步降级（与 fetcher 的 httpx 兜底环境一致）：指纹维度整体跳过
    monkeypatch.setattr(session_mod, "HAS_CURL_CFFI", False)

    pool = SessionPool()
    f = Fetcher(session_pool=pool, timeout=5.0, allow_private_hosts=True)
    assert f._use_curl is False
    # 无代理请求：走池化客户端
    f.get("http://example.com/")
    stats = pool.stats()
    assert stats["sync_active"] == 0
    assert stats["idle"] == 1
    handle = pool.acquire()
    assert handle.fingerprint is None  # httpx 无指纹维度
    assert len(handle.session.requests) == 1
    pool.release(handle)
    # 代理请求（构造级 proxy）：不借池（代理按请求轮换，池化客户端无法跟随）
    proxied = Fetcher(
        session_pool=pool, timeout=5.0, allow_private_hosts=True, proxy="http://127.0.0.1:9"
    )
    proxied.get("http://example.com/")
    assert pool.stats()["sync_active"] == 0
    assert "http://127.0.0.1:9" in proxied._proxied_sync_clients
    f.close()
    proxied.close()
    pool.close()


async def test_async_fetcher_with_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """AsyncFetcher + 池：异步借还全链路（含钉扎）正常工作。"""
    _use_fake_curl(monkeypatch, async_cls=_FakeAsyncSession)
    monkeypatch.setattr(socket, "getaddrinfo", _public_resolver(["93.184.216.34"]))
    pool = SessionPool(["chrome131", "safari184"])
    async with AsyncFetcher(session_pool=pool, timeout=5.0, allow_private_hosts=False) as f:
        resp = await f.get("https://example.com/")
        assert resp.status == 200
        assert f._async_session is None  # 单会话路径不触发
        stats = pool.stats()
        assert stats["async_active"] == 0
        assert stats["idle"] == 1
        handle = await pool.acquire_async()
        assert handle.fingerprint in {"chrome131", "safari184"}
        assert len(handle.session.requests) == 1
        assert handle.session.curl_options[_RESOLVE_OPT] == ["example.com:443:93.184.216.34"]
        await pool.release_async(handle)
    await pool.aclose()


# ── 九、真实后端冒烟（本地 HTTP 服务器） ─────────────────────────────────────


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi not installed")
def test_pool_smoke_real_curl_cffi(local_server: str) -> None:
    """真实 curl_cffi 后端冒烟：池化请求 + 会话复用 + stats 一致。"""
    pool = SessionPool(prewarm=2)
    with Fetcher(session_pool=pool, timeout=10.0) as f:
        resp = f.get(local_server + "/")
        assert resp.status == 200
        handle = pool.acquire()
        pool.release(handle)
        first = handle.session
        f.get(local_server + "/")  # 复用同一池化会话
        again = pool.acquire()
        try:
            assert again.session is first
        finally:
            pool.release(again)
        stats = pool.stats()
        assert stats["created"] == 2  # 预热的两个会话轮换使用
        assert stats["sync_active"] == 0
    pool.close()


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi not installed")
async def test_pool_smoke_real_async(local_server: str) -> None:
    """真实异步冒烟：预热 + 并发借还后池状态一致。"""
    pool = SessionPool(["chrome131", "safari184"])
    async with AsyncFetcher(session_pool=pool, timeout=10.0) as f:
        await pool.prewarm_async(2)
        results = await asyncio.gather(f.get(local_server + "/"), f.get(local_server + "/json"))
        assert all(r.status == 200 for r in results)
    stats = pool.stats()
    assert stats["async_active"] == 0
    assert stats["created"] >= 2
    await pool.aclose()


# ── 十、并发安全（内置规则审查 M1 / L7） ──────────────────────────────────────


def test_stats_concurrent_with_async_borrow_return_is_race_free() -> None:
    """M1 回归：监控线程 stats() 与事件循环并发借还不抛 set 迭代竞态。

    修复前 stats() 在锁外用 Python 层 for 循环迭代 ``_async_active``
    （set 类型）：事件循环线程并发 add/remove 时，set 迭代器检测到尺寸
    变化抛 ``RuntimeError: Set changed size during iteration``。此处按
    审查员的复现思路缩小 GIL 切换间隔提高线程交错概率（结束恢复原值），
    监控线程循环调用 stats() 直到借还结束，断言全程零异常。
    """
    factory = _RecordingFactory(_FakeAsyncSession)
    pool = SessionPool(async_session_factory=factory, max_size=16)
    stop = threading.Event()
    errors: list[Exception] = []

    async def churn() -> None:
        # 常驻 200 个句柄（对齐审查员的复现参数）反复借还，
        # 让 _async_active 的尺寸在 199/200 间持续变化
        handles = [await pool.acquire_async() for _ in range(200)]
        for _ in range(150):
            await pool.release_async(handles.pop())
            handles.append(await pool.acquire_async())
        while handles:
            await pool.release_async(handles.pop())

    def monitor() -> None:
        while not stop.is_set():
            try:
                pool.stats()
            except Exception as exc:  # 压测关心任何意外异常（含 RuntimeError）
                errors.append(exc)

    original = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # 提高交错概率；finally 恢复
    monitor_thread = threading.Thread(target=monitor)
    monitor_thread.start()
    try:
        asyncio.run(churn())
    finally:
        stop.set()
        monitor_thread.join()
        sys.setswitchinterval(original)
    assert errors == []
    pool.close()


def test_concurrent_sync_borrow_return_thread_safety() -> None:
    """L7：同步侧多线程并发借还压测——无异常、句柄守恒、stats 自洽。

    SessionPool 同步侧以 threading.Lock 宣称线程安全（对齐
    CacheMiddleware / AutoThrottle 的同类并发压测）。8 线程 × 各 400 轮
    acquire/release（池上限 4 制造瞬时溢出的 ephemeral 路径；混入
    ``success=False`` 归还与并发 stats() 读取），结束后断言全部归还、
    会话总数守恒（created == idle + evicted + discarded：每个创建的会话
    要么仍在池内、要么已被关闭）、工厂创建数与计数一致。
    """
    factory = _RecordingFactory()
    pool = SessionPool(session_factory=factory, max_size=4)
    threads, rounds = 8, 400
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for i in range(rounds):
                handle = pool.acquire()
                # 混入失败归还（failures 计数路径）；i%4==0 失败、其余成功，
                # failures 达到 max_failures 前必被成功归还清零
                pool.release(handle, success=(i % 4 != 0))
                if i % 64 == 0:
                    pool.stats()  # 并发诊断读取
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(worker) for _ in range(threads)]
        for future in futures:
            future.result()
    assert errors == []
    stats = pool.stats()
    assert stats["sync_active"] == 0
    assert stats["async_active"] == 0
    assert stats["active"] == 0
    assert stats["idle"] <= 4  # 池缓存不超上限
    assert stats["total"] == stats["active"] + stats["idle"]
    # 守恒：创建的每个会话要么仍在池内（idle）、要么已被关闭（淘汰/溢出）
    assert stats["created"] == stats["idle"] + stats["evicted"] + stats["discarded_overflow"]
    assert len(factory.sessions) == stats["created"]
    pool.close()

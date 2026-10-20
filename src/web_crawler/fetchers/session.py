"""带指纹轮换与健康管理的会话池。

对齐 :class:`~web_crawler.fetchers.proxy.ProxyPool` 的池化模式：fetcher 按
请求借还的可插拔会话池。每个池化会话持有独立的 ``curl_cffi``
impersonate 指纹（chrome / safari / edge / firefox 系列轮换），取用会话时
指纹已就绪——既避免每次请求重建 TLS 栈，又让不同会话在网络层呈现不同
浏览器画像，降低单指纹被风控聚类的风险。

与 ProxyPool 共享的治理模式（API 风格对齐）：

* **轮换**：指纹按 ``round_robin``（默认）或 ``random`` 策略从
  ``fingerprints`` 列表分配，仅在新建会话时消耗一个指纹。
* **淘汰**：会话连续失败 ``max_failures`` 次即标记 stale 并关闭淘汰，
  下次取用时重建（对应 ProxyPool 的 ``mark_failed`` / 冷却语义）。
* **惰性清理**：空闲超过 ``max_idle`` 秒的会话在下次取用时逐出关闭
  （对应 ProxyPool ``get()`` 里对冷却代理的惰性清理）。

与 ProxyPool 的差异（由资源语义决定）：

* **借还而非查询**：HTTP 会话是有状态资源（连接池、cookie jar），用
  ``acquire`` / ``release`` 显式生命周期而非 ``get()`` 无状态轮询。
* **上限语义**：``max_size`` 是池缓存的会话数上限；瞬时并发超过上限时
  新建临时会话应急，归还时直接关闭——绝不共享同一会话并发请求，也绝不
  阻塞等待（与 ProxyPool ``get()`` 永不阻塞的风格一致）。
* **同步/异步双池**：同一实例同时管理同步与异步两套会话缓存（各自的
  上限、轮换游标与计数）。同步侧用 ``threading.Lock``（ProxyPool 同款）；
  异步侧为单事件循环内的无锁设计——方法体内无 ``await`` 的区段在协作式
  调度下天然原子，需要 ``await`` 的会话关闭一律延后到状态更新完成之后。

``curl_cffi`` 缺失时优雅降级：指纹维度跳过（会话指纹为 ``None``、工厂
收到 ``None``），httpx 会话池化（连接复用）仍然有效——与
:class:`~web_crawler.fetchers.fetcher.Fetcher` 的 httpx 兜底行为对齐。

注意：异步侧的会话绑定创建它的事件循环（curl_cffi ``AsyncSession`` /
httpx ``AsyncClient`` 的连接池都挂在 loop 上）。跨循环复用同一池时旧
条目会被弃置重建；不支持多个事件循环并发使用同一池。
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..compat import HAS_CURL_CFFI
from ..exceptions import ConfigError

__all__ = [
    "DEFAULT_FINGERPRINTS",
    "SessionHandle",
    "SessionPool",
]

# 默认指纹列表：覆盖 curl_cffi 支持的四大主流浏览器家族的近期稳定版本。
# 轮换分配让池中会话呈现多样化的 TLS/JA3 画像。
DEFAULT_FINGERPRINTS: tuple[str, ...] = (
    "chrome131",
    "chrome136",
    "safari18_0",
    "safari184",
    "edge101",
    "firefox135",
)


@dataclass(slots=True, eq=False)
class SessionHandle:
    """池化会话的借出凭证（:meth:`SessionPool.acquire` 返回、``release`` 传回）。

    ``eq=False`` 强制身份语义：池用 set / list 按"同一个会话对象"追踪，
    两个碰巧同指纹的会话不相等。

    借出期间直接用 ``handle.session.request(...)`` 发请求，用完必须归还
    （同步 :meth:`SessionPool.release` / 异步 ``release_async``，两者配对
    使用——归还给"错误的一侧"会被宽容忽略，会话将滞留在借出集合）。

    Attributes:
        session: 后端会话对象（curl_cffi Session/AsyncSession 或 httpx
            Client/AsyncClient）。
        fingerprint: 该会话持有的 impersonate 指纹；降级模式（无
            curl_cffi）下为 ``None``。
        last_used: 最近一次归还/创建时刻（monotonic；空闲回收计时基准）。
        failures: 当前连续失败次数（``release(success=False)`` 累计，
            成功归还清零）。
        stale: 已被判定淘汰；归还时关闭、不再入池。
        ephemeral: 池满时应急新建的临时会话；归还即关闭、不占池缓存。
    """

    session: Any
    fingerprint: str | None
    last_used: float
    failures: int = 0
    stale: bool = False
    ephemeral: bool = False


def _close_session_quiet(session: Any) -> None:
    """尽力关闭同步会话（淘汰/清理是 best-effort 路径，关闭失败不外抛）。"""
    try:
        session.close()
    except Exception:
        pass


async def _aclose_session_quiet(session: Any) -> None:
    """尽力关闭异步会话（优先 ``aclose``，兼容只有 async ``close`` 的后端）。"""
    try:
        aclose = getattr(session, "aclose", None)
        if callable(aclose):
            await aclose()
        else:
            await session.close()
    except Exception:
        pass


def _default_sync_session_factory(impersonate: str | None) -> Any:
    """内置默认同步会话工厂：有指纹走 curl_cffi，降级走 httpx。"""
    if impersonate is not None:
        from curl_cffi.requests import Session as CurlSession

        return CurlSession(impersonate=impersonate)
    import httpx

    return httpx.Client()


def _default_async_session_factory(impersonate: str | None) -> Any:
    """内置默认异步会话工厂（构造本身无需事件循环，loop 在请求时才绑定）。"""
    if impersonate is not None:
        from curl_cffi.requests import AsyncSession as CurlAsyncSession

        return CurlAsyncSession(impersonate=impersonate)
    import httpx

    return httpx.AsyncClient()


class SessionPool:
    """带指纹轮换与健康管理的会话池（同步 + 异步双池，线程安全）。

    Parameters
    ----------
    fingerprints:
        impersonate 目标列表（按序轮换分配给新建会话）；``None``（默认）
        使用 :data:`DEFAULT_FINGERPRINTS`。``curl_cffi`` 不可用时整个指纹
        维度被跳过（降级为纯 httpx 会话池化）。
    strategy:
        ``"round_robin"``（默认）按序轮换指纹；``"random"`` 随机挑选。
    max_size:
        池缓存的会话数上限（同步/异步两套缓存各自适用）。瞬时并发超过
        上限时新建临时会话应急，归还时直接关闭。
    max_idle:
        空闲会话的回收秒数；空闲超过该值的会话在下次取用时逐出关闭。
    max_failures:
        连续失败多少次后会话被淘汰（对应 ProxyPool 的 ``max_failures``）。
    prewarm:
        构造时预先建好的同步会话数；异步预热在事件循环内进行，用
        ``await prewarm_async()``。
    session_factory:
        同步会话工厂 ``Callable[[str | None], Any]``——入参为该会话的
        指纹（降级时 ``None``），返回后端会话。``None`` 用内置默认工厂。
        :class:`~web_crawler.fetchers.fetcher.Fetcher` 构造时会自动绑定
        自己的工厂（见 :meth:`bind_factories`）。
    async_session_factory:
        异步会话工厂（签名同上）。``None`` 用内置默认工厂。
    clock:
        单调时钟（默认 ``time.monotonic``）；可注入假时钟测试空闲回收。
    """

    def __init__(
        self,
        fingerprints: Sequence[str] | None = None,
        *,
        strategy: str = "round_robin",
        max_size: int = 8,
        max_idle: float = 120.0,
        max_failures: int = 3,
        prewarm: int = 0,
        session_factory: Callable[[str | None], Any] | None = None,
        async_session_factory: Callable[[str | None], Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if strategy not in ("round_robin", "random"):
            raise ConfigError(f"unknown strategy: {strategy!r} (use 'round_robin' or 'random')")
        if max_size < 1:
            raise ConfigError("max_size must be >= 1")
        if max_idle < 0:
            raise ConfigError("max_idle must be >= 0")
        if max_failures < 1:
            raise ConfigError("max_failures must be >= 1")
        if prewarm < 0 or prewarm > max_size:
            raise ConfigError("prewarm must be within [0, max_size]")
        fps = DEFAULT_FINGERPRINTS if fingerprints is None else tuple(fingerprints)
        if not fps:
            raise ConfigError("fingerprints must be non-empty")

        self._fingerprints: tuple[str, ...] = fps
        self._strategy = strategy
        self._max_size = max_size
        self._max_idle = max_idle
        self._max_failures = max_failures
        self._prewarm = prewarm
        self._now: Callable[[], float] = clock or time.monotonic
        self._session_factory = session_factory
        self._async_session_factory = async_session_factory

        # 同步侧（threading.Lock 保护；与 ProxyPool 同款锁模式）
        self._lock = threading.Lock()
        self._sync_idle: list[SessionHandle] = []  # LIFO：优先复用最近归还的热连接
        self._sync_active: set[SessionHandle] = set()
        self._sync_fp_index = -1
        # 异步侧（单事件循环内原子、无锁；跨循环复用见 acquire_async）
        self._async_idle: list[SessionHandle] = []
        self._async_active: set[SessionHandle] = set()
        self._async_pending_close: list[SessionHandle] = []  # 待 await 关闭的淘汰会话
        self._async_fp_index = -1
        self._async_loop: asyncio.AbstractEventLoop | None = None
        # 统计计数（sync/async 分开累计，stats() 合并；异步计数只在事件循环线程写）
        self._sync_created = 0
        self._async_created = 0
        self._sync_evicted_failed = 0
        self._async_evicted_failed = 0
        self._sync_evicted_idle = 0
        self._async_evicted_idle = 0
        self._sync_discarded_overflow = 0
        self._async_discarded_overflow = 0

        if prewarm:
            self._prewarm_sync_locked(prewarm)

    # -- 指纹轮换 -------------------------------------------------------------
    def _pick_fingerprint(self, *, sync: bool) -> str | None:
        """为下一个新建会话挑选指纹；``curl_cffi`` 不可用时返回 ``None``（降级）。

        每次读取模块级 ``HAS_CURL_CFFI`` 的当前值而非构造时快照，保持与
        Fetcher 后端探测一致的可测试性（monkeypatch 即时生效）。轮换游标
        同步/异步各自独立：两侧的游标自增都发生在各自锁保护的原子区内，
        完全无锁且无竞态，分布仍然均匀。
        """
        if not HAS_CURL_CFFI:
            return None
        if self._strategy == "random":
            return random.choice(self._fingerprints)
        if sync:
            self._sync_fp_index += 1
            return self._fingerprints[self._sync_fp_index % len(self._fingerprints)]
        self._async_fp_index += 1
        return self._fingerprints[self._async_fp_index % len(self._fingerprints)]

    # -- 同步池 -----------------------------------------------------------------
    def _prewarm_sync_locked(self, count: int) -> None:
        """构造期同步预热（调用方必须持锁）。"""
        now = self._now()
        factory = self._session_factory or _default_sync_session_factory
        for _ in range(count):
            fingerprint = self._pick_fingerprint(sync=True)
            self._sync_idle.append(
                SessionHandle(session=factory(fingerprint), fingerprint=fingerprint, last_used=now)
            )
            self._sync_created += 1

    def _create_sync_locked(self, now: float, *, ephemeral: bool) -> SessionHandle:
        """新建同步会话并计入借出集合（调用方必须持锁）。"""
        fingerprint = self._pick_fingerprint(sync=True)
        factory = self._session_factory or _default_sync_session_factory
        handle = SessionHandle(
            session=factory(fingerprint),
            fingerprint=fingerprint,
            last_used=now,
            ephemeral=ephemeral,
        )
        self._sync_active.add(handle)
        self._sync_created += 1
        return handle

    def _reap_sync_locked(self, now: float) -> list[SessionHandle]:
        """逐出空闲超时的同步会话（调用方必须持锁；惰性触发，对齐 ProxyPool）。

        只做状态更新与句柄收集，不执行关闭——会话关闭涉及 socket 清理
        （毫秒级阻塞），持锁执行会放大高并发借还的锁竞争（对齐 :meth:`close`
        的"锁内收集、锁外关闭"模式）。返回待关闭的句柄列表，由调用方在
        锁外统一执行 :func:`_close_session_quiet`。
        """
        if not self._sync_idle:
            return []
        kept: list[SessionHandle] = []
        expired: list[SessionHandle] = []
        for handle in self._sync_idle:
            if now - handle.last_used > self._max_idle:
                expired.append(handle)
            else:
                kept.append(handle)
        if expired:
            self._sync_idle = kept
            self._sync_evicted_idle += len(expired)
        return expired

    def acquire(self) -> SessionHandle:
        """借出一个同步会话：优先复用空闲会话，其次新建（池满则临时新建）。

        永不阻塞（对齐 ProxyPool ``get()``）。借出的会话用完必须
        :meth:`release` 归还；取用时指纹已就绪（会话创建即绑定指纹）。
        """
        now = self._now()
        with self._lock:
            expired = self._reap_sync_locked(now)
            if self._sync_idle:
                handle = self._sync_idle.pop()  # LIFO：复用最近归还的会话
                self._sync_active.add(handle)
                handle.last_used = now
            else:
                # 空闲耗尽：池缓存未满则正常新建，已满则临时会话应急（归还即关闭）
                ephemeral = len(self._sync_active) >= self._max_size
                handle = self._create_sync_locked(now, ephemeral=ephemeral)
        # 锁外关闭空闲回收的会话（socket 关闭可能毫秒级阻塞，持锁执行会放大
        # 高并发借还的锁竞争——对齐 close() 的"锁内收集、锁外关闭"模式）；
        # 句柄状态更新已在锁内完成，此处不影响借出结果
        for stale_handle in expired:
            _close_session_quiet(stale_handle.session)
        return handle

    def release(self, handle: SessionHandle, *, success: bool = True) -> None:
        """归还借出的同步会话；请求失败时传 ``success=False`` 累计健康计数。

        连续失败达到 ``max_failures`` 次（或已被 :meth:`mark_failed` 标记
        stale）的会话直接关闭淘汰，下次 :meth:`acquire` 新建重建（指纹轮换
        序继续推进）；临时会话（池满应急新建）用完即关。未知/已归还/异步
        句柄宽容 no-op（对齐 ProxyPool 对未知代理的宽容语义）。

        会话关闭在锁外执行：socket 关闭可能毫秒级阻塞，持锁关闭会放大高
        并发借还下的锁竞争（对齐 :meth:`close` 的既有模式——锁内只更新
        状态并收集待关句柄，锁外统一执行关闭）。
        """
        now = self._now()
        to_close: list[SessionHandle] = []
        with self._lock:
            if handle not in self._sync_active:
                return
            self._sync_active.remove(handle)
            if success:
                handle.failures = 0
            else:
                handle.failures += 1
            if handle.stale or handle.failures >= self._max_failures:
                self._sync_evicted_failed += 1
                to_close.append(handle)
            elif handle.ephemeral:
                self._sync_discarded_overflow += 1
                to_close.append(handle)
            else:
                handle.last_used = now
                self._sync_idle.append(handle)
        for closed in to_close:
            _close_session_quiet(closed.session)

    # -- 异步池（单事件循环内无锁；状态更新区段无 await，原子） -----------------
    def _create_async(self, *, ephemeral: bool) -> SessionHandle:
        """新建异步会话（调用方处于事件循环原子区；是否入集合由调用方决定）。"""
        fingerprint = self._pick_fingerprint(sync=False)
        factory = self._async_session_factory or _default_async_session_factory
        handle = SessionHandle(
            session=factory(fingerprint),
            fingerprint=fingerprint,
            last_used=self._now(),
            ephemeral=ephemeral,
        )
        self._async_created += 1
        return handle

    def _reap_async(self, now: float) -> None:
        """逐出空闲超时的异步会话；关闭需要 ``await``，移入待关队列延后处理。"""
        if not self._async_idle:
            return
        kept: list[SessionHandle] = []
        expired: list[SessionHandle] = []
        for handle in self._async_idle:
            if now - handle.last_used > self._max_idle:
                expired.append(handle)
            else:
                kept.append(handle)
        if expired:
            self._async_idle = kept
            self._async_evicted_idle += len(expired)
            self._async_pending_close.extend(expired)

    async def _drain_async_pending_close(self) -> None:
        """关闭全部待关的异步会话（淘汰/回收路径统一在此 await）。"""
        pending = self._async_pending_close
        self._async_pending_close = []
        for handle in pending:
            await _aclose_session_quiet(handle.session)

    def _rebind_async_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """把异步池绑定到 ``loop``，弃置绑定其他循环的旧条目。

        旧条目（连接池挂在旧 loop 上）既不可用也无法安全关闭（旧 loop 多半
        已停），直接弃置交由 GC 兜底释放。调用方处于事件循环原子区。
        """
        self._async_loop = loop
        self._async_idle.clear()
        self._async_active.clear()
        self._async_pending_close.clear()

    async def acquire_async(self) -> SessionHandle:
        """借出一个异步会话（事件循环内无锁原子；语义同 :meth:`acquire`）。

        必须在事件循环内调用；跨循环复用同一池时旧条目自动弃置重建（不支
        持多个事件循环并发使用同一池）。
        """
        loop = asyncio.get_running_loop()
        if self._async_loop is not loop:
            self._rebind_async_loop(loop)
        # —— 到返回为止的区段无 await：协作式调度下原子 ——
        now = self._now()
        self._reap_async(now)
        if self._async_idle:
            handle = self._async_idle.pop()  # LIFO：复用最近归还的会话
            self._async_active.add(handle)
            handle.last_used = now
        else:
            ephemeral = len(self._async_active) >= self._max_size
            handle = self._create_async(ephemeral=ephemeral)
            self._async_active.add(handle)
        # 淘汰关闭需要 await，延后到借出状态已落定之后
        await self._drain_async_pending_close()
        return handle

    async def release_async(self, handle: SessionHandle, *, success: bool = True) -> None:
        """归还借出的异步会话（语义同 :meth:`release`）。"""
        to_close: SessionHandle | None = None
        # —— 状态更新区段无 await：原子 ——
        if handle in self._async_active:
            self._async_active.remove(handle)
            if success:
                handle.failures = 0
            else:
                handle.failures += 1
            if handle.stale or handle.failures >= self._max_failures:
                to_close = handle
                self._async_evicted_failed += 1
            elif handle.ephemeral:
                to_close = handle
                self._async_discarded_overflow += 1
            else:
                handle.last_used = self._now()
                self._async_idle.append(handle)
        if to_close is not None:
            await _aclose_session_quiet(to_close.session)
        await self._drain_async_pending_close()

    async def prewarm_async(self, count: int | None = None) -> None:
        """在当前事件循环内预先建好 ``count`` 个异步会话（默认构造参数 ``prewarm``）。

        异步会话绑定创建它的事件循环，预热必须在目标循环内进行（构造参数
        ``prewarm`` 只预热同步会话）。
        """
        n = self._prewarm if count is None else count
        if n < 0 or n > self._max_size:
            raise ConfigError("prewarm count must be within [0, max_size]")
        loop = asyncio.get_running_loop()
        if self._async_loop is not loop:
            self._rebind_async_loop(loop)
        now = self._now()
        # 区段无 await：原子
        while len(self._async_idle) < min(n, self._max_size):
            handle = self._create_async(ephemeral=False)
            handle.last_used = now
            self._async_idle.append(handle)
        await self._drain_async_pending_close()

    # -- 健康标记（对齐 ProxyPool 的 mark_failed / mark_success） ----------------
    def mark_failed(self, handle: SessionHandle) -> None:
        """记录借出中会话的一次失败；累计达阈值标记 stale（归还时淘汰）。

        仅作用于借出中的句柄——空闲会话的淘汰由归还路径
        （``release(success=False)`` 的连续失败累计）完成。未知/已归还的
        句柄 no-op（对齐 ProxyPool 对未知代理的宽容语义）。

        线程安全语义：同步与异步句柄的 ``failures``/``stale`` 读-改-写都
        在 ``threading.Lock`` 内完成——GIL 下 ``+=`` 非原子，锁外更新在
        多线程并发调用（如线程池）时会丢失计数；锁内为纯内存纳秒级操作，
        事件循环线程调用无感知。注意与 ``release_async``（事件循环内无锁
        更新）并发标记同一异步句柄属于异步侧单循环设计之外的用法，调用方
        需避免（见模块 docstring 的单事件循环约束）。
        """
        with self._lock:
            if handle in self._sync_active:
                handle.failures += 1
                if handle.failures >= self._max_failures:
                    handle.stale = True
            # 异步句柄的计数更新同样纳入锁内：消除多线程并发调用
            # mark_failed 之间的读-改-写竞态（丢失计数）
            if handle in self._async_active:
                handle.failures += 1
                if handle.failures >= self._max_failures:
                    handle.stale = True

    def mark_success(self, handle: SessionHandle) -> None:
        """清零会话的连续失败计数并解除 stale 标记（对齐 ``ProxyPool.mark_success``）。

        仅对本池管理的句柄（同步/异步任一活跃或空闲集合中）生效，未知/
        已出池句柄 no-op——与 :meth:`mark_failed` 的宽容语义对称。
        """
        with self._lock:
            if (
                handle in self._sync_active
                or handle in self._sync_idle
                or handle in self._async_active
                or handle in self._async_idle
            ):
                handle.failures = 0
                handle.stale = False

    # -- 工厂绑定（Fetcher 构造时自动调用） --------------------------------------
    def bind_factories(
        self,
        *,
        session_factory: Callable[[str | None], Any] | None = None,
        async_session_factory: Callable[[str | None], Any] | None = None,
    ) -> None:
        """为尚未配置的工厂槽位绑定会话工厂（first-wins，不覆盖已有绑定）。

        :class:`~web_crawler.fetchers.fetcher.Fetcher` /
        :class:`~web_crawler.fetchers.fetcher.AsyncFetcher` 构造时会自动调
        用：把自己按指纹构建会话的方法绑进池，让池化会话继承 fetcher 的
        超时/校验/HTTP2 等配置。多个 fetcher 共享同一池时以第一个绑定者
        为准——建议共享者配置一致，或构造池时显式传入工厂。
        """
        with self._lock:
            if session_factory is not None and self._session_factory is None:
                self._session_factory = session_factory
            if async_session_factory is not None and self._async_session_factory is None:
                self._async_session_factory = async_session_factory

    # -- 诊断与生命周期 -----------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """返回池状态快照（活跃/空闲/指纹分布/淘汰计数）。

        同步侧在锁内做一次惰性空闲回收（对齐 ProxyPool
        ``available_count`` 的惰性冷却清理）。异步侧集合可能被事件循环
        线程并发修改（``acquire_async``/``release_async`` 的无锁设计）：
        必须先用 ``list()`` 做一次原子快照（纯 C 层拷贝、不释放 GIL）再
        迭代——若在锁外用 Python 层 for 循环逐句柄迭代 set，尺寸被并发
        修改会抛 ``RuntimeError: Set changed size during iteration``。
        快照后数值可能瞬时略滞后（诊断用途可接受）。异步句柄的锁外关闭
        （空闲回收）与同步侧统一：锁内收集、锁外执行。
        """
        now = self._now()
        fingerprints: dict[str | None, int] = {}
        with self._lock:
            expired = self._reap_sync_locked(now)
            sync_active = len(self._sync_active)
            sync_idle = len(self._sync_idle)
            for handle in self._sync_active:
                fingerprints[handle.fingerprint] = fingerprints.get(handle.fingerprint, 0) + 1
            for handle in self._sync_idle:
                fingerprints[handle.fingerprint] = fingerprints.get(handle.fingerprint, 0) + 1
            sync_created = self._sync_created
            sync_evicted_failed = self._sync_evicted_failed
            sync_evicted_idle = self._sync_evicted_idle
            sync_discarded = self._sync_discarded_overflow
            # 异步侧集合快照放在锁内：list() 对 set/list 是纯 C 层原子拷贝
            # （执行期间不释放 GIL），与事件循环线程的 add/remove 并发也
            # 不会触发 set 迭代器失效；与同步侧快照同处一个临界区、口径
            # 一致。锁内为纯内存 O(n) 操作（500 句柄微秒级），无 IO 无
            # 嵌套锁，无死锁风险。注意：事件循环线程本身不持有本锁，锁的
            # 作用是与同步侧借用方互斥并统一快照时机，快照的线程安全由
            # list() 的原子性保证。
            async_active_handles = list(self._async_active)
            async_idle_handles = list(self._async_idle)
            async_created = self._async_created
            async_evicted_failed = self._async_evicted_failed
            async_evicted_idle = self._async_evicted_idle
            async_discarded = self._async_discarded_overflow
        # 锁外基于不可变快照做指纹统计与汇总（快照是普通 list，迭代安全）
        for handle in async_active_handles:
            fingerprints[handle.fingerprint] = fingerprints.get(handle.fingerprint, 0) + 1
        for handle in async_idle_handles:
            fingerprints[handle.fingerprint] = fingerprints.get(handle.fingerprint, 0) + 1
        # 空闲回收的会话锁外关闭（socket 关闭可能毫秒级阻塞，不持锁）
        for stale_handle in expired:
            _close_session_quiet(stale_handle.session)
        evicted_failed = sync_evicted_failed + async_evicted_failed
        evicted_idle = sync_evicted_idle + async_evicted_idle
        discarded = sync_discarded + async_discarded
        active = sync_active + len(async_active_handles)
        idle = sync_idle + len(async_idle_handles)
        return {
            "strategy": self._strategy,
            "max_size": self._max_size,
            "max_idle": self._max_idle,
            "active": active,
            "idle": idle,
            "total": active + idle,
            "sync_active": sync_active,
            "async_active": len(async_active_handles),
            "created": sync_created + async_created,
            "evicted": evicted_failed + evicted_idle,
            "evicted_failed": evicted_failed,
            "evicted_idle": evicted_idle,
            "discarded_overflow": discarded,
            "fingerprints": fingerprints,
        }

    def close(self) -> None:
        """关闭全部同步会话（异步会话请用 ``await aclose()``）。

        调用前应确保没有在途借出（借出中的会话会被一并关闭，后续归还按
        no-op 处理）。关闭后池仍可继续使用（缓存清空后按需新建）。
        """
        with self._lock:
            handles = self._sync_idle + list(self._sync_active)
            self._sync_idle.clear()
            self._sync_active.clear()
        for handle in handles:
            _close_session_quiet(handle.session)

    async def aclose(self) -> None:
        """关闭全部异步会话（同步会话请用 ``close()``；语义同上）。"""
        loop = asyncio.get_running_loop()
        if self._async_loop is not loop:
            # 池未绑定当前循环：旧条目无法安全关闭，弃置后直接清空
            self._rebind_async_loop(loop)
        handles = self._async_idle + list(self._async_active)
        handles.extend(self._async_pending_close)
        self._async_idle.clear()
        self._async_active.clear()
        self._async_pending_close.clear()
        for handle in handles:
            await _aclose_session_quiet(handle.session)

    def __len__(self) -> int:
        with self._lock:
            total = len(self._sync_idle) + len(self._sync_active)
        return total + len(self._async_idle) + len(self._async_active)

    def __repr__(self) -> str:
        snapshot = self.stats()
        return (
            f"<SessionPool strategy={self._strategy!r} size={snapshot['total']} "
            f"active={snapshot['active']} idle={snapshot['idle']} "
            f"max_size={self._max_size}>"
        )

    def __enter__(self) -> SessionPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def __aenter__(self) -> SessionPool:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

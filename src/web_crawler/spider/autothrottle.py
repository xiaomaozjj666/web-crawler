"""AutoThrottle 自适应限速（对标 Scrapy 的 ``AutoThrottle`` 扩展）。

按域跟踪响应延迟的指数移动平均（EMA），把"目标并发"换算成相邻请求的
最小间隔：站点变慢则自动放慢、变快则自动提速；超时 / 5xx / 异常等失败
样本不进 EMA，而是触发惩罚倍率——延迟临时放大、连续失败逐次放大、
成功后对称衰减回落，给受压站点喘息空间。

算法原理
--------
设第 ``n`` 个成功样本的下载耗时为 ``L_n`` 秒、平滑系数 ``alpha``::

    EMA_n = alpha * L_n + (1 - alpha) * EMA_{n-1}    (n > 1)
    EMA_1 = L_1                                       (首样本直接赋值)

首样本直接赋值而非从 0 起步：从 0 起步约需 ``1/alpha`` 个样本才能追上
真值，冷启动阶段 EMA 会长期低估真实延迟，限速形同虚设。

目标延迟对齐 Scrapy ``AUTOTHROTTLE_TARGET_CONCURRENCY`` 的公认语义
（"希望同域保持的并发请求数"，delay = latency / concurrency，即
Little 定律的单域投影）::

    target_delay = EMA / target_concurrency

失败样本不进 EMA（EMA 只刻画健康响应的延迟分布，混入超时会永久拉高
基准），改为放大惩罚倍率 ``penalty``：连续失败逐次乘 ``penalty_factor``
（封顶 ``max_penalty``），成功一次则对称地除回去（下限 1.0）::

    delay = clamp(target_delay * penalty, min_delay, max_delay)

线程模型：``threading.Lock`` 仅保护 per-domain 字典的读写，临界区是纯
内存操作（纳秒级）且不含任何 ``await``——单事件循环内的异步调用不会
因此阻塞循环；同步路径被多线程共享（如多个 spider 线程共用同一个
AutoThrottle 实例）时同样安全。``stats()`` 在锁内只做快照拷贝、锁外
组装返回值，避免与 ``get_delay`` 的加锁路径互相重入（Lock 不可重入）。

内存有界：``_domains`` 按域维护 LRU（上限 ``max_domains``，默认 65536），
超限时淘汰最久未 ``note_response`` 的域（利用 dict 保序特性，读路径
``get_delay`` / ``stats`` 不触碰顺序）；被淘汰域重新反馈时等价于冷启动
（EMA 从首样本起步、惩罚归 1），语义无损——长时开放爬取海量唯一域名时
内存不会线性增长。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..exceptions import ConfigError

__all__ = ["AutoThrottle"]


@dataclass
class _DomainState:
    """单域限速账本（AutoThrottle 内部状态，不对外暴露）。"""

    # 响应延迟 EMA（秒）；None 表示该域尚无成功样本
    ema_latency: float | None = None
    # 失败惩罚倍率（>= 1.0）：连续失败放大、成功对称回落
    penalty: float = 1.0
    # 连续失败计数（成功即清零；仅观测用，不参与公式）
    consecutive_failures: int = 0
    # 已反馈的样本总数（成功 + 失败）
    samples: int = 0


class AutoThrottle:
    """按域自适应限速器：EMA 延迟反馈 + 失败惩罚 + 目标并发换算。

    供 :class:`~web_crawler.spider.Spider` 的 ``autothrottle`` 构造参数
    注入；也可独立使用（任何能按域反馈 ``(elapsed, success)`` 的调度器
    都能驱动它）。

    Parameters
    ----------
    alpha:
        EMA 平滑系数， ``(0, 1]``；越大越贴近最新样本、越小越平滑。
        默认 0.3。
    target_concurrency:
        目标并发（对齐 Scrapy ``AUTOTHROTTLE_TARGET_CONCURRENCY``）：
        希望同域同时保持的请求数。``target_delay = EMA / 该值``。
        默认 1.0。
    min_delay:
        延迟下限（秒）。默认 0.0。
    max_delay:
        延迟上限（秒），同时兜底惩罚放大后的极端值。默认 60.0。
    start_delay:
        无样本时的起步延迟（对齐 Scrapy ``AUTOTHROTTLE_START_DELAY``：
        首个响应到来前先用固定值）。默认 0.0（不拖慢冷启动）。
    penalty_factor:
        失败惩罚倍率的放大系数（> 1.0）：每次失败乘它、每次成功除它。
        默认 1.5。
    max_penalty:
        惩罚倍率封顶（>= 1.0）。默认 10.0。
    max_domains:
        域账本 LRU 上限（>= 1），默认 65536：超出时淘汰最久未反馈的域，
        被淘汰域重新出现时等价于冷启动（EMA 从首样本起步、惩罚归 1）。
        防止长时开放爬取海量唯一域名时 ``_domains`` 无上限增长。

    并发边界：全部状态由单把 ``threading.Lock`` 保护，临界区为纯内存
    纳秒级操作（字典读写 + 浮点运算，无 IO、无 ``await``），且调用频率
    受网络请求速率约束（每次下载才反馈一次）——锁竞争上界远低于分片锁
    带来的复杂度收益，无需分片锁。

    Example
    -------
    >>> from web_crawler.spider import AutoThrottle
    >>> at = AutoThrottle(target_concurrency=2.0)
    >>> at.note_response("example.com", 0.5, success=True)  # ema=0.5
    >>> at.get_delay("example.com")  # 0.5 / 2.0
    0.25
    """

    def __init__(
        self,
        *,
        alpha: float = 0.3,
        target_concurrency: float = 1.0,
        min_delay: float = 0.0,
        max_delay: float = 60.0,
        start_delay: float = 0.0,
        penalty_factor: float = 1.5,
        max_penalty: float = 10.0,
        max_domains: int = 65536,
    ) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ConfigError(f"alpha must be in (0, 1], got {alpha!r}")
        if target_concurrency <= 0.0:
            raise ConfigError(f"target_concurrency must be > 0, got {target_concurrency!r}")
        if min_delay < 0.0:
            raise ConfigError(f"min_delay must be >= 0, got {min_delay!r}")
        if max_delay < min_delay:
            raise ConfigError(f"max_delay ({max_delay!r}) must be >= min_delay ({min_delay!r})")
        if penalty_factor <= 1.0:
            raise ConfigError(f"penalty_factor must be > 1.0, got {penalty_factor!r}")
        if max_penalty < 1.0:
            raise ConfigError(f"max_penalty must be >= 1.0, got {max_penalty!r}")
        if max_domains < 1:
            raise ConfigError(f"max_domains must be >= 1, got {max_domains!r}")

        self.alpha = alpha
        self.target_concurrency = target_concurrency
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.start_delay = start_delay
        self.penalty_factor = penalty_factor
        self.max_penalty = max_penalty
        self.max_domains = max_domains
        self._domains: dict[str, _DomainState] = {}
        # dict 保序（Python 3.7+）：尾部恒为最近 note_response 的域，超限
        # 时 pop 首部即淘汰"最久未反馈"的域（读路径不触碰顺序，见模块
        # docstring「内存有界」）；见「线程模型」：临界区为纳秒级纯内存
        # 操作，异步路径持锁不含 await，不会阻塞事件循环
        self._lock = threading.Lock()

    # -- 反馈 -------------------------------------------------------------
    def note_response(self, domain: str, elapsed: float, success: bool) -> None:
        """向限速器反馈一次下载结果，并触碰该域的 LRU 位置。

        成功样本纳入 EMA 并对称回落惩罚；失败样本不进 EMA（保持健康
        延迟基准），只逐次放大惩罚。Spider 侧在每次真实下载（非中间件
        短路）完成后调用，``success`` 传 ``response.ok``。

        反馈使该域移到 LRU 末尾；``max_domains`` 超限时最久未反馈的域
        被淘汰，其重新出现时等价于冷启动（EMA 从首样本起步、惩罚归 1）。

        Parameters
        ----------
        domain:
            域名键（大小写不敏感；调用方保证同域同键）。
        elapsed:
            本次下载耗时（秒）。负值按 0 处理（防御时钟回拨）。
        success:
            是否成功（2xx/3xx 视为成功）。
        """
        elapsed = max(elapsed, 0.0)
        key = domain.lower()
        with self._lock:
            # LRU 触碰：先 pop 再写回（move_to_end 语义），使 dict 尾部
            # 恒为最近反馈的域；新域同样落尾（冷启动，见类 docstring）
            state = self._domains.pop(key, None)
            if state is None:
                state = _DomainState()
            self._domains[key] = state
            # 超限淘汰最久未反馈的域（dict 首部）：长时开放爬取海量唯一
            # 域名时内存有界；被淘汰域重新反馈时由上面的冷启动路径接住
            while len(self._domains) > self.max_domains:
                self._domains.pop(next(iter(self._domains)))
            state.samples += 1
            if success:
                state.consecutive_failures = 0
                # 首样本直接赋值（见模块 docstring），后续按 EMA 递推
                if state.ema_latency is None:
                    state.ema_latency = elapsed
                else:
                    state.ema_latency = (
                        self.alpha * elapsed + (1.0 - self.alpha) * state.ema_latency
                    )
                # 成功：惩罚对称回落，1.0 为下限（无惩罚）
                state.penalty = max(state.penalty / self.penalty_factor, 1.0)
            else:
                state.consecutive_failures += 1
                # 失败：惩罚逐次放大，max_penalty 封顶
                state.penalty = min(state.penalty * self.penalty_factor, self.max_penalty)

    # -- 查询 ---------------------------------------------------------------
    def get_delay(self, domain: str) -> float:
        """返回该域相邻请求的最小间隔（秒）。

        无样本（或尚无成功样本）时返回 ``clamp(start_delay)``，否则返回
        ``clamp(EMA / target_concurrency * penalty)``。
        """
        with self._lock:
            state = self._domains.get(domain.lower())
            if state is None:
                return self._clamp(self.start_delay)
            return self._delay_of(state)

    def stats(self) -> dict[str, dict[str, float | int | None]]:
        """返回各域限速快照。

        结构：``{domain: {"ema_latency", "delay", "penalty",
        "consecutive_failures", "samples"}}``。``ema_latency`` 在该域尚无
        成功样本时为 ``None``。
        """
        # 锁内只做快照拷贝（锁不可重入，不能在持锁时调 get_delay）
        with self._lock:
            snapshot: dict[str, _DomainState] = dict(self._domains)
        return {
            domain: {
                "ema_latency": state.ema_latency,
                "delay": self._delay_of(state),
                "penalty": state.penalty,
                "consecutive_failures": state.consecutive_failures,
                "samples": state.samples,
            }
            for domain, state in snapshot.items()
        }

    # -- 内部 ---------------------------------------------------------------
    def _delay_of(self, state: _DomainState) -> float:
        """由账本状态计算延迟（无锁纯函数，调用方须已保证状态一致）。"""
        if state.ema_latency is None:
            base = self.start_delay
        else:
            base = state.ema_latency / self.target_concurrency
        return self._clamp(base * state.penalty)

    def _clamp(self, delay: float) -> float:
        """把延迟钳制到 ``[min_delay, max_delay]``。"""
        return max(self.min_delay, min(self.max_delay, delay))

"""Watchdog 事件总线 + 崩溃自愈模块。

借鉴 OpenTelemetry Agent 规范与 PentAGI 的 watchdog 思路，给
:class:`~web_crawler.ai.reverse_agent.ReverseAgent` 加一个统一的事件总线，
让外部订阅者能观察 Agent 内部状态、收集指标、记录审计日志；并在异常时
触发"崩溃自愈"回调，把恢复策略从 Agent 主循环中解耦。

能力清单
--------
- :class:`EventBus` — 进程内同步事件总线，多订阅者广播；
- :class:`AgentEvent` — 标准化事件结构（type / step / payload / timestamp）；
- :class:`Heartbeat` — 步进心跳：超过 ``max_interval`` 没有新步进即触发
  ``on_stall`` 回调，用于检测 AI 卡死（LLM 长时间不返回）；
- :class:`CrashRecovery` — 崩溃恢复策略：按 ``max_retries`` 次数回调
  ``recovery_fn``，超过次数即放弃并把异常记录到事件总线。

设计要点
--------
- 全部同步实现，避免引入 ``asyncio.Lock`` 复杂度；EventBus.publish 是
  O(订阅者数) 的同步广播，订阅者执行慢会拖慢主循环，订阅者应保持轻量；
- 异常隔离：单个订阅者抛错不影响其他订阅者，错误写入 stderr；
- 零依赖：只用标准库，与项目整体风格一致。
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

# 事件类型常量（字符串，便于 JSON 序列化与跨语言对接）
EVENT_STEP_START = "step.start"
EVENT_STEP_END = "step.end"
EVENT_ACTION = "action"
EVENT_OBSERVATION = "observation"
EVENT_THINK_ERROR = "think.error"
EVENT_ACT_ERROR = "act.error"
EVENT_OBSERVE_ERROR = "observe.error"
EVENT_RECOVER = "recover"
EVENT_RECOVER_FAILED = "recover.failed"
EVENT_DONE = "done"
EVENT_STALL = "stall"
EVENT_PLAN = "plan"
EVENT_LOOP_DETECTED = "loop.detected"
EVENT_CONTEXT_COMPRESSED = "context.compressed"


@dataclass
class AgentEvent:
    """标准化事件结构。"""

    type: str
    step: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "step": self.step,
            "payload": self.payload,
            "timestamp": self.timestamp,
        }


# 订阅者签名：接收 AgentEvent，无返回值（异常隔离由总线负责）
Subscriber = Callable[[AgentEvent], None]


class EventBus:
    """进程内同步事件总线。

    多订阅者广播；订阅者异常不影响其他订阅者。线程安全（细粒度锁）。

    用法
    ----
    >>> bus = EventBus()
    >>> bus.subscribe(lambda e: print(e.type))
    >>> bus.publish(AgentEvent(type=EVENT_STEP_START, step=1))
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()
        # 已发布事件计数（仅用于指标观察，不保留事件本体）
        self._published_count = 0

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        """注册订阅者，返回取消订阅的回调。"""
        with self._lock:
            self._subscribers.append(subscriber)

        def _unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return _unsubscribe

    def publish(self, event: AgentEvent) -> None:
        """同步广播事件给所有订阅者。单个订阅者异常不影响其他。"""
        with self._lock:
            subs = list(self._subscribers)
            self._published_count += 1
        for sub in subs:
            try:
                sub(event)
            except Exception as exc:
                sys.stderr.write(
                    f"[event-bus] subscriber {getattr(sub, '__name__', sub)!r} raised: {exc!r}\n"
                )
                sys.stderr.flush()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def published_count(self) -> int:
        with self._lock:
            return self._published_count

    # -- 便捷构造 ----------------------------------------------------------

    def emit(self, type_: str, *, step: int = 0, **payload: Any) -> None:
        """便捷构造并发布事件。"""
        self.publish(AgentEvent(type=type_, step=step, payload=payload))


class Heartbeat:
    """步进心跳：检测 Agent 是否卡死。

    每完成一步调用 :meth:`tick`；超过 ``max_interval`` 秒未 tick，
    :meth:`check_stall` 即返回 True，由调用方决定是否触发恢复。

    设计用于同步循环：Agent 主循环在 ``for step in range(...)`` 末尾
    调用 ``heartbeat.tick(step)``，并在循环顶部调用
    ``heartbeat.check_stall()`` 判断是否需要恢复。
    """

    def __init__(
        self,
        *,
        max_interval: float = 120.0,
        on_stall: Callable[[int, float], None] | None = None,
    ) -> None:
        self.max_interval = max(1.0, max_interval)
        self.on_stall = on_stall
        self._last_tick_step = 0
        self._last_tick_time = time.time()
        self._lock = threading.Lock()
        self._stall_reported = False

    def tick(self, step: int) -> None:
        """更新最近一次步进时间。每步调用一次。"""
        with self._lock:
            self._last_tick_step = step
            self._last_tick_time = time.time()
            self._stall_reported = False

    def check_stall(self, *, step: int) -> bool:
        """检查是否卡死。卡死时触发一次 ``on_stall`` 回调（不重复触发）。"""
        with self._lock:
            elapsed = time.time() - self._last_tick_time
            stalled = elapsed > self.max_interval
            if stalled and not self._stall_reported:
                self._stall_reported = True
                if self.on_stall is not None:
                    try:
                        self.on_stall(step, elapsed)
                    except Exception as exc:
                        sys.stderr.write(f"[heartbeat] on_stall raised: {exc!r}\n")
                        sys.stderr.flush()
            return stalled

    def reset(self) -> None:
        """重置心跳（新任务开始时调用）。"""
        with self._lock:
            self._last_tick_step = 0
            self._last_tick_time = time.time()
            self._stall_reported = False


class CrashRecovery:
    """崩溃恢复策略协调器。

    把"重试 N 次"的策略从 Agent 主循环解耦。Agent 在 ``try/except`` 中
    调用 :meth:`attempt`，``recovery_fn`` 由 Agent 提供（如重启浏览器、
    重建 page）。超过 ``max_retries`` 即放弃，把异常写进事件总线。

    Parameters
    ----------
    max_retries:
        最大重试次数（不含首次尝试）。默认 2。
    bus:
        可选的事件总线，用于发布 ``recover`` / ``recover.failed`` 事件。
    """

    def __init__(
        self,
        *,
        max_retries: int = 2,
        bus: EventBus | None = None,
    ) -> None:
        self.max_retries = max(0, max_retries)
        self.bus = bus
        self._attempts = 0

    @property
    def attempts(self) -> int:
        """当前已重试次数。"""
        return self._attempts

    def reset(self) -> None:
        """新任务开始时重置计数。"""
        self._attempts = 0

    def attempt(
        self,
        recovery_fn: Callable[[], bool],
        *,
        step: int = 0,
        error: Exception | None = None,
    ) -> bool:
        """尝试一次恢复。返回是否应继续主循环。

        - ``recovery_fn`` 返回 True → 恢复成功，重置计数，返回 True；
        - ``recovery_fn`` 返回 False → 计数 +1，未超限返回 True（继续重试），
          超限返回 False（放弃）。
        """
        if self._attempts >= self.max_retries:
            if self.bus is not None:
                self.bus.emit(
                    EVENT_RECOVER_FAILED,
                    step=step,
                    attempts=self._attempts,
                    error=str(error) if error else "",
                )
            return False
        self._attempts += 1
        try:
            ok = recovery_fn()
        except Exception as exc:
            if self.bus is not None:
                self.bus.emit(
                    EVENT_RECOVER_FAILED,
                    step=step,
                    attempts=self._attempts,
                    error=str(exc),
                )
            return False
        if self.bus is not None:
            self.bus.emit(
                EVENT_RECOVER,
                step=step,
                attempts=self._attempts,
                success=ok,
                error=str(error) if error else "",
            )
        if ok:
            self._attempts = 0
            return True
        return self._attempts < self.max_retries


class EventBusLogger:
    """便捷订阅者：把所有事件以 JSON 行格式写入文件/流。

    用于调试与审计：``bus.subscribe(EventBusLogger(sys.stderr).log)``。
    """

    def __init__(self, stream: Any = None) -> None:
        self.stream = stream or sys.stderr

    def log(self, event: AgentEvent) -> None:
        import json

        try:
            line = json.dumps(event.to_dict(), ensure_ascii=False, default=str)
            self.stream.write(line + "\n")
            self.stream.flush()
        except Exception:
            pass


def collect_metrics(events: Iterable[AgentEvent]) -> dict[str, Any]:
    """对一组事件做基础聚合，返回指标 dict。

    用于 :class:`EventBus` 之外的事件流分析（如订阅者缓存最近 N 条事件
    后调用本函数做摘要）。
    """
    events_list = list(events)
    if not events_list:
        return {"count": 0}
    counts: dict[str, int] = {}
    errors = 0
    last_step = 0
    first_ts = events_list[0].timestamp
    last_ts = events_list[-1].timestamp
    for e in events_list:
        counts[e.type] = counts.get(e.type, 0) + 1
        if e.type.endswith(".error") or e.type.endswith(".failed"):
            errors += 1
        last_step = max(last_step, e.step)
    return {
        "count": len(events_list),
        "by_type": counts,
        "errors": errors,
        "last_step": last_step,
        "duration_seconds": last_ts - first_ts,
    }


__all__ = [
    "EVENT_ACTION",
    "EVENT_ACT_ERROR",
    "EVENT_CONTEXT_COMPRESSED",
    "EVENT_DONE",
    "EVENT_LOOP_DETECTED",
    "EVENT_OBSERVATION",
    "EVENT_OBSERVE_ERROR",
    "EVENT_PLAN",
    "EVENT_RECOVER",
    "EVENT_RECOVER_FAILED",
    "EVENT_STALL",
    "EVENT_STEP_END",
    "EVENT_STEP_START",
    "EVENT_THINK_ERROR",
    "AgentEvent",
    "CrashRecovery",
    "EventBus",
    "EventBusLogger",
    "Heartbeat",
    "collect_metrics",
]

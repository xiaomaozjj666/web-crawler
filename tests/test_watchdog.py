"""Watchdog 模块单元测试。

覆盖 EventBus / Heartbeat / CrashRecovery / EventBusLogger / collect_metrics，
用 mock time 避免真实等待。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from web_crawler.ai.watchdog import (
    EVENT_ACT_ERROR,
    EVENT_ACTION,
    EVENT_DONE,
    EVENT_OBSERVATION,
    EVENT_RECOVER,
    EVENT_RECOVER_FAILED,
    EVENT_STEP_END,
    EVENT_STEP_START,
    EVENT_THINK_ERROR,
    AgentEvent,
    CrashRecovery,
    EventBus,
    EventBusLogger,
    Heartbeat,
    collect_metrics,
)

# ---------------------------------------------------------------------------
# AgentEvent
# ---------------------------------------------------------------------------


def test_agent_event_to_dict_roundtrip() -> None:
    event = AgentEvent(type=EVENT_STEP_START, step=3, payload={"k": "v"})
    d = event.to_dict()
    assert d["type"] == EVENT_STEP_START
    assert d["step"] == 3
    assert d["payload"] == {"k": "v"}
    assert isinstance(d["timestamp"], float)


def test_agent_event_defaults() -> None:
    event = AgentEvent(type=EVENT_DONE)
    assert event.step == 0
    assert event.payload == {}
    assert event.timestamp > 0


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


def test_event_bus_subscribe_and_publish_broadcasts_to_all() -> None:
    bus = EventBus()
    received_a: list[AgentEvent] = []
    received_b: list[AgentEvent] = []
    bus.subscribe(received_a.append)
    bus.subscribe(received_b.append)

    event = AgentEvent(type=EVENT_ACTION, step=1)
    bus.publish(event)

    assert received_a == [event]
    assert received_b == [event]
    assert bus.published_count == 1
    assert bus.subscriber_count == 2


def test_event_bus_unsubscribe_removes_subscriber() -> None:
    bus = EventBus()
    received: list[AgentEvent] = []
    unsub = bus.subscribe(received.append)
    assert bus.subscriber_count == 1

    unsub()
    assert bus.subscriber_count == 0

    bus.publish(AgentEvent(type=EVENT_DONE))
    assert received == []
    # 即使取消订阅后 published_count 仍累加
    assert bus.published_count == 1


def test_event_bus_unsubscribe_idempotent() -> None:
    """重复取消订阅不应报错。"""
    bus = EventBus()
    unsub = bus.subscribe(lambda e: None)
    unsub()
    unsub()  # 再次调用不应抛错
    assert bus.subscriber_count == 0


def test_event_bus_subscriber_exception_does_not_affect_others(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """单个订阅者抛错不影响其他订阅者，错误写入 stderr。"""
    bus = EventBus()
    ok_received: list[AgentEvent] = []

    def bad_sub(_event: AgentEvent) -> None:
        raise ValueError("boom")

    bus.subscribe(bad_sub)
    bus.subscribe(ok_received.append)

    event = AgentEvent(type=EVENT_OBSERVATION)
    bus.publish(event)

    assert ok_received == [event]
    captured = capsys.readouterr()
    assert "boom" in captured.err


def test_event_bus_emit_convenience_method() -> None:
    """emit 便捷构造并发布事件。"""
    bus = EventBus()
    received: list[AgentEvent] = []
    bus.subscribe(received.append)

    bus.emit(EVENT_STEP_END, step=5, action="click", result="ok")

    assert len(received) == 1
    event = received[0]
    assert event.type == EVENT_STEP_END
    assert event.step == 5
    assert event.payload == {"action": "click", "result": "ok"}
    assert bus.published_count == 1


def test_event_bus_published_count_increments() -> None:
    bus = EventBus()
    assert bus.published_count == 0
    bus.publish(AgentEvent(type=EVENT_DONE))
    bus.publish(AgentEvent(type=EVENT_DONE))
    assert bus.published_count == 2


def test_event_bus_no_subscribers_publish_is_noop() -> None:
    bus = EventBus()
    bus.publish(AgentEvent(type=EVENT_DONE))
    assert bus.published_count == 1


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_max_interval_clamped_to_minimum_one() -> None:
    hb = Heartbeat(max_interval=0.5)
    assert hb.max_interval == 1.0


def test_heartbeat_max_interval_zero_clamped() -> None:
    hb = Heartbeat(max_interval=0)
    assert hb.max_interval == 1.0


def test_heartbeat_tick_updates_step_and_resets_stall_flag() -> None:
    hb = Heartbeat(max_interval=10.0)
    hb.tick(5)
    assert hb._last_tick_step == 5
    assert hb._stall_reported is False


def test_heartbeat_check_stall_false_when_recent() -> None:
    hb = Heartbeat(max_interval=100.0)
    hb.tick(1)
    assert hb.check_stall(step=2) is False


def test_heartbeat_check_stall_true_when_overdue() -> None:
    """模拟时间流逝超过 max_interval，check_stall 返回 True。"""
    hb = Heartbeat(max_interval=1.0)
    # 固定 tick 时间
    base_time = 1000.0
    with patch("web_crawler.ai.watchdog.time.time", return_value=base_time):
        hb.tick(1)
    # 时间推进到超过 max_interval
    with patch("web_crawler.ai.watchdog.time.time", return_value=base_time + 2.0):
        assert hb.check_stall(step=2) is True


def test_heartbeat_on_stall_callback_called_once_not_repeated() -> None:
    """on_stall 回调只在首次卡死时触发一次，不重复。"""
    calls: list[tuple[int, float]] = []
    hb = Heartbeat(max_interval=1.0, on_stall=lambda step, elapsed: calls.append((step, elapsed)))
    base_time = 1000.0
    with patch("web_crawler.ai.watchdog.time.time", return_value=base_time):
        hb.tick(1)
    with patch("web_crawler.ai.watchdog.time.time", return_value=base_time + 5.0):
        assert hb.check_stall(step=2) is True
        # 再次检查，不应重复触发回调
        assert hb.check_stall(step=3) is True
    assert len(calls) == 1
    assert calls[0][0] == 2


def test_heartbeat_on_stall_exception_handled(capsys: pytest.CaptureFixture[str]) -> None:
    """on_stall 抛错时写入 stderr，不影响 check_stall 返回值。"""

    def bad_callback(step: int, elapsed: float) -> None:
        raise RuntimeError("callback failed")

    hb = Heartbeat(max_interval=1.0, on_stall=bad_callback)
    base_time = 1000.0
    with patch("web_crawler.ai.watchdog.time.time", return_value=base_time):
        hb.tick(1)
    with patch("web_crawler.ai.watchdog.time.time", return_value=base_time + 2.0):
        result = hb.check_stall(step=2)

    assert result is True
    captured = capsys.readouterr()
    assert "callback failed" in captured.err


def test_heartbeat_reset_clears_state() -> None:
    hb = Heartbeat(max_interval=1.0)
    hb.tick(5)
    hb._stall_reported = True
    hb.reset()
    assert hb._last_tick_step == 0
    assert hb._stall_reported is False
    # reset 后立即 check_stall 不应卡死
    assert hb.check_stall(step=1) is False


def test_heartbeat_tick_resets_stall_reported_flag() -> None:
    """卡死后再次 tick 应清除 stall_reported，允许下次卡死重新报告。"""
    hb = Heartbeat(max_interval=1.0, on_stall=lambda s, e: None)
    base_time = 1000.0
    with patch("web_crawler.ai.watchdog.time.time", return_value=base_time):
        hb.tick(1)
    with patch("web_crawler.ai.watchdog.time.time", return_value=base_time + 2.0):
        hb.check_stall(step=2)
    assert hb._stall_reported is True
    # 新的 tick 重置标志
    with patch("web_crawler.ai.watchdog.time.time", return_value=base_time + 3.0):
        hb.tick(3)
    assert hb._stall_reported is False


def test_heartbeat_no_callback_still_reports_stall() -> None:
    """未设置 on_stall 时 check_stall 仍正确返回卡死状态。"""
    hb = Heartbeat(max_interval=1.0)
    base_time = 1000.0
    with patch("web_crawler.ai.watchdog.time.time", return_value=base_time):
        hb.tick(1)
    with patch("web_crawler.ai.watchdog.time.time", return_value=base_time + 2.0):
        assert hb.check_stall(step=2) is True


# ---------------------------------------------------------------------------
# CrashRecovery
# ---------------------------------------------------------------------------


def test_crash_recovery_max_retries_clamped_to_zero() -> None:
    cr = CrashRecovery(max_retries=-5)
    assert cr.max_retries == 0


def test_crash_recovery_attempts_starts_at_zero() -> None:
    cr = CrashRecovery(max_retries=3)
    assert cr.attempts == 0


def test_crash_recovery_reset() -> None:
    cr = CrashRecovery(max_retries=3)
    cr._attempts = 2
    cr.reset()
    assert cr.attempts == 0


def test_crash_recovery_success_returns_true_and_resets() -> None:
    cr = CrashRecovery(max_retries=3)
    result = cr.attempt(lambda: True, step=1)
    assert result is True
    assert cr.attempts == 0  # 成功后重置


def test_crash_recovery_success_emits_recover_event() -> None:
    bus = EventBus()
    received: list[AgentEvent] = []
    bus.subscribe(received.append)
    cr = CrashRecovery(max_retries=3, bus=bus)

    cr.attempt(lambda: True, step=2, error=ValueError("boom"))

    events = [e for e in received if e.type == EVENT_RECOVER]
    assert len(events) == 1
    assert events[0].step == 2
    assert events[0].payload["success"] is True
    assert "boom" in events[0].payload["error"]


def test_crash_recovery_failure_under_limit_returns_true_to_retry() -> None:
    cr = CrashRecovery(max_retries=3)
    result = cr.attempt(lambda: False, step=1)
    # 失败但未超限 → 返回 True 继续
    assert result is True
    assert cr.attempts == 1


def test_crash_recovery_failure_emits_recover_event_with_success_false() -> None:
    bus = EventBus()
    received: list[AgentEvent] = []
    bus.subscribe(received.append)
    cr = CrashRecovery(max_retries=3, bus=bus)

    cr.attempt(lambda: False, step=1)

    events = [e for e in received if e.type == EVENT_RECOVER]
    assert len(events) == 1
    assert events[0].payload["success"] is False


def test_crash_recovery_failure_at_limit_returns_false() -> None:
    """达到 max_retries 后失败返回 False（放弃）。"""
    cr = CrashRecovery(max_retries=2)
    # 第一次失败：attempts=1, 1<2 → True
    assert cr.attempt(lambda: False) is True
    # 第二次失败：attempts=2, 2<2 False → 返回 False
    assert cr.attempt(lambda: False) is False
    assert cr.attempts == 2


def test_crash_recovery_already_at_limit_returns_false_immediately() -> None:
    """已超 max_retries 时直接返回 False，不调用 recovery_fn。"""
    cr = CrashRecovery(max_retries=0)
    called: list[bool] = []

    def recovery_fn() -> bool:
        called.append(True)
        return True

    result = cr.attempt(recovery_fn)
    assert result is False
    assert called == []  # 未调用


def test_crash_recovery_at_limit_emits_recover_failed() -> None:
    bus = EventBus()
    received: list[AgentEvent] = []
    bus.subscribe(received.append)
    cr = CrashRecovery(max_retries=0, bus=bus)

    result = cr.attempt(lambda: True, step=5, error=RuntimeError("e"))
    assert result is False
    failed = [e for e in received if e.type == EVENT_RECOVER_FAILED]
    assert len(failed) == 1
    assert failed[0].step == 5
    assert failed[0].payload["attempts"] == 0


def test_crash_recovery_recovery_fn_exception_returns_false() -> None:
    bus = EventBus()
    received: list[AgentEvent] = []
    bus.subscribe(received.append)
    cr = CrashRecovery(max_retries=3, bus=bus)

    def boom() -> bool:
        raise RuntimeError("recovery crashed")

    result = cr.attempt(boom, step=1)
    assert result is False
    assert cr.attempts == 1
    failed = [e for e in received if e.type == EVENT_RECOVER_FAILED]
    assert len(failed) == 1
    assert "recovery crashed" in failed[0].payload["error"]


def test_crash_recovery_no_bus_does_not_emit() -> None:
    """无 bus 时不应抛错。"""
    cr = CrashRecovery(max_retries=2)
    # 成功路径
    assert cr.attempt(lambda: True) is True
    # 失败路径
    assert cr.attempt(lambda: False) is True

    # 异常路径
    def boom() -> bool:
        raise ValueError("recovery error")

    assert cr.attempt(boom) is False


def test_crash_recovery_success_after_failures_resets_counter() -> None:
    cr = CrashRecovery(max_retries=3)
    cr.attempt(lambda: False)  # attempts=1
    cr.attempt(lambda: False)  # attempts=2
    assert cr.attempts == 2
    # 成功 → 重置
    assert cr.attempt(lambda: True) is True
    assert cr.attempts == 0


# ---------------------------------------------------------------------------
# EventBusLogger
# ---------------------------------------------------------------------------


def test_event_bus_logger_writes_json_line() -> None:
    stream = MagicMock()
    logger = EventBusLogger(stream)
    event = AgentEvent(type=EVENT_STEP_START, step=1, payload={"a": 1})
    logger.log(event)
    assert stream.write.called
    line = stream.write.call_args_list[0].args[0]
    assert EVENT_STEP_START in line
    assert stream.flush.called


def test_event_bus_logger_default_stream_is_stderr() -> None:
    import sys

    logger = EventBusLogger()
    assert logger.stream is sys.stderr


def test_event_bus_logger_exception_swallowed() -> None:
    """log 内部异常应被静默吞掉，不抛出。"""
    stream = MagicMock()
    stream.write.side_effect = ValueError("write failed")
    logger = EventBusLogger(stream)
    # 不应抛错
    logger.log(AgentEvent(type=EVENT_DONE))


# ---------------------------------------------------------------------------
# collect_metrics
# ---------------------------------------------------------------------------


def test_collect_metrics_empty_returns_count_zero() -> None:
    result = collect_metrics([])
    assert result == {"count": 0}


def test_collect_metrics_aggregates_counts() -> None:
    events = [
        AgentEvent(type=EVENT_STEP_START, step=1, timestamp=100.0),
        AgentEvent(type=EVENT_STEP_END, step=1, timestamp=101.0),
        AgentEvent(type=EVENT_ACTION, step=2, timestamp=102.0),
        AgentEvent(type=EVENT_ACTION, step=3, timestamp=103.0),
    ]
    result = collect_metrics(events)
    assert result["count"] == 4
    assert result["by_type"] == {EVENT_STEP_START: 1, EVENT_STEP_END: 1, EVENT_ACTION: 2}
    assert result["errors"] == 0
    assert result["last_step"] == 3
    assert result["duration_seconds"] == 3.0


def test_collect_metrics_counts_errors() -> None:
    """以 .error 或 .failed 结尾的事件计入 errors。"""
    events = [
        AgentEvent(
            type=EVENT_THINK_ERROR, step=1
        ),  # think.error -> 不以 .error 结尾? 实际是 think.error
        AgentEvent(type=EVENT_RECOVER_FAILED, step=2),
        AgentEvent(type=EVENT_ACT_ERROR, step=3),
    ]
    result = collect_metrics(events)
    # EVENT_THINK_ERROR = "think.error" 以 ".error" 结尾
    # EVENT_ACT_ERROR = "act.error" 以 ".error" 结尾
    # EVENT_RECOVER_FAILED = "recover.failed" 以 ".failed" 结尾
    assert result["errors"] == 3


def test_collect_metrics_accepts_generator() -> None:
    events = (AgentEvent(type=EVENT_DONE, step=i) for i in range(3))
    result = collect_metrics(events)
    assert result["count"] == 3
    assert result["last_step"] == 2


# ---------------------------------------------------------------------------
# 集成：EventBus + CrashRecovery
# ---------------------------------------------------------------------------


def test_integration_crash_recovery_publishes_to_event_bus() -> None:
    """CrashRecovery 把恢复事件发布到 EventBus，订阅者能收到。"""
    bus = EventBus()
    events: list[AgentEvent] = []
    bus.subscribe(events.append)

    cr = CrashRecovery(max_retries=2, bus=bus)
    # 成功恢复
    cr.attempt(lambda: True, step=1, error=ValueError("initial"))
    # 失败到放弃
    cr.attempt(lambda: False, step=2)
    cr.attempt(lambda: False, step=3)
    cr.attempt(lambda: False, step=4)  # 超限

    types = [e.type for e in events]
    assert EVENT_RECOVER in types
    assert EVENT_RECOVER_FAILED in types

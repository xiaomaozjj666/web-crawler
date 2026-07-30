"""Tests for ProxyPool rotation, failure tracking and cooldown."""

from __future__ import annotations

import time

from web_crawler.fetchers.proxy import ProxyPool


def test_empty_pool_returns_none() -> None:
    assert ProxyPool().get() is None
    assert len(ProxyPool()) == 0


def test_round_robin_cycles_in_order() -> None:
    pool = ProxyPool(["a", "b", "c"], strategy="round_robin")
    assert [pool.get() for _ in range(5)] == ["a", "b", "c", "a", "b"]


def test_random_strategy_returns_member() -> None:
    pool = ProxyPool(["a", "b", "c"], strategy="random")
    for _ in range(20):
        assert pool.get() in {"a", "b", "c"}


def test_invalid_strategy_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="unknown strategy"):
        ProxyPool(["a"], strategy="bogus")


def test_mark_failed_cools_down_after_threshold() -> None:
    pool = ProxyPool(["a", "b"], max_failures=3, cooldown=10.0)
    pool.mark_failed("a")
    pool.mark_failed("a")
    # Still available before reaching max_failures
    assert "a" in {pool.get(), pool.get()}
    pool.mark_failed("a")  # 3rd failure -> cooldown
    # Now only "b" should be served
    for _ in range(5):
        assert pool.get() == "b"


def test_mark_success_resets_failures() -> None:
    pool = ProxyPool(["a", "b"], max_failures=2)
    pool.mark_failed("a")
    pool.mark_failed("a")  # cooled down
    pool.mark_success("a")  # reset
    served = {pool.get() for _ in range(6)}
    assert "a" in served


def test_add_and_remove() -> None:
    pool = ProxyPool(["a"])
    assert len(pool) == 1
    pool.add("b")
    assert len(pool) == 2
    pool.add("a")  # duplicate no-op
    assert len(pool) == 2
    pool.remove("a")
    assert len(pool) == 1
    pool.remove("zzz")  # absent no-op
    assert len(pool) == 1


def test_cooldown_expires() -> None:
    pool = ProxyPool(["a", "b"], max_failures=1, cooldown=0.05)
    pool.mark_failed("a")  # immediately cooled down for 0.05s
    assert pool.get() == "b"
    time.sleep(0.06)
    # After cooldown, "a" is available again (round_robin will reach it).
    served = {pool.get() for _ in range(6)}
    assert "a" in served


def test_repr_contains_strategy() -> None:
    pool = ProxyPool(["a"], strategy="round_robin")
    assert "round_robin" in repr(pool)


def test_available_count() -> None:
    """available_count 返回未冷却的代理数。"""
    pool = ProxyPool(["a", "b", "c"], strategy="round_robin", max_failures=1, cooldown=60.0)
    assert pool.available_count() == 3
    pool.mark_failed("a")
    assert pool.available_count() == 2
    pool.mark_success("a")
    assert pool.available_count() == 3

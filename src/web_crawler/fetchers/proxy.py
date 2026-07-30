"""Rotating proxy pool with failure tracking and cooldown.

Aligns with Scrapling's approach to proxy management: a pluggable pool that the
fetchers query per-request. Proxies are rotated (round-robin or random) and any
proxy that fails too often is temporarily cooled down so the fetcher keeps
operating without manual intervention. The pool is thread-safe so it can be
shared across a crawler's worker threads.
"""

from __future__ import annotations

import random
import threading
import time


class ProxyPool:
    """Thread-safe rotating proxy pool with per-proxy failure tracking.

    Parameters
    ----------
    proxies:
        Initial list of proxy URLs (e.g. ``"http://user:pass@host:port"``).
    strategy:
        ``"round_robin"`` (default) cycles through proxies in order;
        ``"random"`` picks a uniformly random available proxy.
    max_failures:
        Number of consecutive failures after which a proxy enters cooldown.
    cooldown:
        Seconds a cooled-down proxy is skipped before being retried.
    """

    def __init__(
        self,
        proxies: list[str] | None = None,
        *,
        strategy: str = "round_robin",
        max_failures: int = 3,
        cooldown: float = 60.0,
    ) -> None:
        if strategy not in ("round_robin", "random"):
            raise ValueError(f"unknown strategy: {strategy!r} (use 'round_robin' or 'random')")
        self._proxies: list[str] = list(proxies) if proxies else []
        self._strategy = strategy
        self._max_failures = max_failures
        self._cooldown = cooldown
        self._lock = threading.Lock()
        # round_robin 游标
        self._index = 0
        # 每个代理的累计失败次数与冷却到期时间（monotonic）
        self._failures: dict[str, int] = {p: 0 for p in self._proxies}
        self._cooldowns: dict[str, float] = {p: 0.0 for p in self._proxies}

    def _available(self) -> list[str]:
        """Return proxies not currently in cooldown (caller holds the lock)."""
        now = time.monotonic()
        return [p for p in self._proxies if self._cooldowns.get(p, 0.0) <= now]

    def get(self) -> str | None:
        """Return the next available proxy URL, or ``None`` if the pool is empty."""
        with self._lock:
            available = self._available()
            if not available:
                return None
            if self._strategy == "random":
                return random.choice(available)
            # round_robin: 沿用游标遍历整个列表，跳过冷却中的代理
            n = len(self._proxies)
            for _ in range(n):
                idx = self._index % n
                self._index += 1
                proxy = self._proxies[idx]
                if proxy in available:
                    return proxy
            return None  # pragma: no cover - available 非空时循环必定命中

    def mark_failed(self, proxy: str) -> None:
        """Record a failure for ``proxy``; cool it down once ``max_failures`` is reached."""
        with self._lock:
            count = self._failures.get(proxy, 0) + 1
            self._failures[proxy] = count
            if count >= self._max_failures:
                # 累计失败达到阈值，进入冷却期
                self._cooldowns[proxy] = time.monotonic() + self._cooldown

    def mark_success(self, proxy: str) -> None:
        """Reset the failure counter and clear any cooldown for ``proxy``."""
        with self._lock:
            self._failures[proxy] = 0
            self._cooldowns[proxy] = 0.0

    def add(self, proxy: str) -> None:
        """Append a proxy to the pool (no-op if already present)."""
        with self._lock:
            if proxy not in self._proxies:
                self._proxies.append(proxy)
                self._failures.setdefault(proxy, 0)
                self._cooldowns.setdefault(proxy, 0.0)

    def remove(self, proxy: str) -> None:
        """Remove a proxy from the pool (no-op if absent)."""
        with self._lock:
            if proxy in self._proxies:
                self._proxies.remove(proxy)
                self._failures.pop(proxy, None)
                self._cooldowns.pop(proxy, None)

    def available_count(self) -> int:
        """Return the number of proxies not currently in cooldown."""
        with self._lock:
            return len(self._available())

    def __len__(self) -> int:
        with self._lock:
            return len(self._proxies)

    def __repr__(self) -> str:
        with self._lock:
            available = len(self._available())
        return (
            f"<ProxyPool strategy={self._strategy!r} "
            f"size={len(self)} available={available} max_failures={self._max_failures}>"
        )


__all__ = ["ProxyPool"]

"""Scrapling-style fetchers subpackage.

Provides a uniform interface to three complementary fetching strategies, all
returning the library-wide :class:`~web_crawler.response.Response`:

* :class:`Fetcher` — stealth HTTP via ``curl_cffi`` TLS-fingerprint
  impersonation (with an ``httpx`` fallback).
* :class:`AsyncFetcher` — async-only variant of :class:`Fetcher`.
* :class:`DynamicFetcher` — JavaScript-rendered fetching via Playwright.
* :class:`StealthyFetcher` — a :class:`DynamicFetcher` hardened with stealth
  scripts, humanized input and Cloudflare-challenge handling.

A :class:`ProxyPool` is provided for rotating proxies with cooldown.

To avoid forcing ``playwright`` to load when only the HTTP fetcher is needed,
:class:`DynamicFetcher` and :class:`StealthyFetcher` are imported lazily via
``__getattr__``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._base import BaseFetcher
from .fetcher import AsyncFetcher, Fetcher
from .proxy import ProxyPool

# DynamicFetcher / StealthyFetcher pull in playwright; import them lazily so
# that ``from web_crawler.fetchers import Fetcher`` stays cheap.
_LAZY: dict[str, tuple[str, str]] = {
    "DynamicFetcher": ("web_crawler.fetchers.dynamic", "DynamicFetcher"),
    "StealthyFetcher": ("web_crawler.fetchers.stealthy", "StealthyFetcher"),
}

__all__ = [
    "BaseFetcher",
    "Fetcher",
    "AsyncFetcher",
    "DynamicFetcher",
    "StealthyFetcher",
    "ProxyPool",
]


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        import importlib

        module_path, attr_name = _LAZY[name]
        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


if TYPE_CHECKING:
    from .dynamic import DynamicFetcher
    from .stealthy import StealthyFetcher

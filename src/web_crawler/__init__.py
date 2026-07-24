"""web_crawler — a Scrapling-aligned stealth scraping library.

Public surface mirrors Scrapling's high-level API:

- :class:`Selector` / :class:`Adaptors` — adaptive lxml selectors with element
  fingerprinting and structural-similarity relocation.
- :class:`Response` — normalized fetch result with selector helpers.
- :class:`Fetcher` — stealth HTTP via ``curl_cffi`` TLS fingerprinting
  (httpx fallback).
- :class:`AsyncFetcher` — async-only fetcher (same stealth, async-only API).
- :class:`DynamicFetcher` — Playwright-rendered fetching.
- :class:`StealthyFetcher` — anti-bot / Cloudflare-aware Playwright fetcher.
- :class:`ProxyPool` — round-robin / random proxy rotation with health tracking.
- :class:`Spider` / :class:`Request` — callback-driven spider framework with
  pause/resume.

All public symbols are lazily imported: ``import web_crawler`` does NOT pull in
``playwright`` or ``curl_cffi``. Heavy submodules are only loaded when the
corresponding class is first accessed (Scrapling uses the same pattern).

Quick start
-----------
>>> from web_crawler import Fetcher, Selector
>>> fetcher = Fetcher(impersonate="chrome131")
>>> resp = fetcher.get("https://example.com")
>>> for link in resp.css("a"):
...     print(link.attr("href"))
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.2.0"
__author__ = "web-crawler contributors"

# Mapping of public symbol name -> (module path, attribute name).
# Modules are imported on first access via __getattr__, so importing
# ``web_crawler`` never triggers playwright/curl_cffi loads.
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # parser / adaptive
    "Selector": ("web_crawler.parser.selector", "Selector"),
    "Adaptors": ("web_crawler.parser.selector", "Adaptors"),
    "Adaptor": ("web_crawler.parser.selector", "Selector"),
    "AdaptiveStorage": ("web_crawler.parser.adaptive", "AdaptiveStorage"),
    "compute_fingerprint": ("web_crawler.parser.adaptive", "compute_fingerprint"),
    "similarity_score": ("web_crawler.parser.adaptive", "similarity_score"),
    # fetchers
    "BaseFetcher": ("web_crawler.fetchers._base", "BaseFetcher"),
    "Fetcher": ("web_crawler.fetchers.fetcher", "Fetcher"),
    "AsyncFetcher": ("web_crawler.fetchers.fetcher", "AsyncFetcher"),
    "DynamicFetcher": ("web_crawler.fetchers.dynamic", "DynamicFetcher"),
    "StealthyFetcher": ("web_crawler.fetchers.stealthy", "StealthyFetcher"),
    "ProxyPool": ("web_crawler.fetchers.proxy", "ProxyPool"),
    # response
    "Response": ("web_crawler.response", "Response"),
    # visual extraction (PixelRAG-style)
    "VisualExtractor": ("web_crawler.parser.visual", "VisualExtractor"),
    # spider
    "Request": ("web_crawler.spider.spider", "Request"),
    "Spider": ("web_crawler.spider.spider", "Spider"),
    "SpiderError": ("web_crawler.spider.spider", "SpiderError"),
    "SpiderStats": ("web_crawler.spider.spider", "SpiderStats"),
}

__all__ = [
    # parser / adaptive
    "Selector",
    "Adaptors",
    "Adaptor",
    "AdaptiveStorage",
    "compute_fingerprint",
    "similarity_score",
    # fetchers
    "BaseFetcher",
    "Fetcher",
    "AsyncFetcher",
    "DynamicFetcher",
    "StealthyFetcher",
    "ProxyPool",
    # response
    "Response",
    # visual extraction (PixelRAG-style)
    "VisualExtractor",
    # spider
    "Request",
    "Spider",
    "SpiderError",
    "SpiderStats",
    "__version__",
]


def __getattr__(name: str) -> Any:
    """Lazily import public symbols on first access (Scrapling-style).

    This keeps ``import web_crawler`` cheap and avoids forcing users who only
    need the parser to also install playwright/curl_cffi.
    """
    if name in _LAZY_IMPORTS:
        import importlib

        module_path, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        # Cache on this module so subsequent lookups skip the import.
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


# For type checkers, resolve the real symbols so IDE/mypy see full signatures.
if TYPE_CHECKING:
    from .fetchers._base import BaseFetcher
    from .fetchers.dynamic import DynamicFetcher
    from .fetchers.fetcher import AsyncFetcher, Fetcher
    from .fetchers.proxy import ProxyPool
    from .fetchers.stealthy import StealthyFetcher
    from .parser.adaptive import AdaptiveStorage, compute_fingerprint, similarity_score
    from .parser.selector import Adaptors, Selector
    from .parser.visual import VisualExtractor
    from .response import Response
    from .spider.spider import Request, Spider, SpiderError, SpiderStats

    Adaptor = Selector

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
    "CamoufoxFetcher": ("web_crawler.fetchers.camoufox", "CamoufoxFetcher"),
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
    # ai (pluggable LLM layer + AI-assisted extraction; default DeepSeek-V4-Pro)
    "get_provider": ("web_crawler.ai.llm", "get_provider"),
    "register_provider": ("web_crawler.ai.llm", "register_provider"),
    "available_providers": ("web_crawler.ai.llm", "available_providers"),
    "LLMMessage": ("web_crawler.ai.llm", "LLMMessage"),
    "LLMResponse": ("web_crawler.ai.llm", "LLMResponse"),
    "LLMProvider": ("web_crawler.ai.llm", "LLMProvider"),
    "OpenAICompatibleProvider": ("web_crawler.ai.llm", "OpenAICompatibleProvider"),
    "DeepSeekProvider": ("web_crawler.ai.llm", "DeepSeekProvider"),
    "AIExtractor": ("web_crawler.ai.extractor", "AIExtractor"),
    "ExtractionResult": ("web_crawler.ai.extractor", "ExtractionResult"),
    "AIScrapeAgent": ("web_crawler.ai.agent", "AIScrapeAgent"),
    "ScrapeResult": ("web_crawler.ai.agent", "ScrapeResult"),
    "RobotsPolicy": ("web_crawler.ai.agent", "RobotsPolicy"),
    "detect_block": ("web_crawler.ai.agent", "detect_block"),
    # ai — JS 逆向 Agent 套件
    "HookLibrary": ("web_crawler.ai.hooks", "HookLibrary"),
    "generate_combined_script": ("web_crawler.ai.hooks", "generate_combined_script"),
    "JSAnalyzer": ("web_crawler.ai.analyzer", "JSAnalyzer"),
    "JSFragment": ("web_crawler.ai.analyzer", "JSFragment"),
    "CaptchaType": ("web_crawler.ai.captcha", "CaptchaType"),
    "CaptchaManager": ("web_crawler.ai.captcha", "CaptchaManager"),
    "ReverseAgent": ("web_crawler.ai.reverse_agent", "ReverseAgent"),
    "ReverseAgentConfig": ("web_crawler.ai.reverse_agent", "ReverseAgentConfig"),
    # mcp server
    "ReverseMCPServer": ("web_crawler.mcp.server", "ReverseMCPServer"),
}

__all__ = [
    "AIExtractor",
    "AIScrapeAgent",
    "AdaptiveStorage",
    "Adaptor",
    "Adaptors",
    "AsyncFetcher",
    # fetchers
    "BaseFetcher",
    "CamoufoxFetcher",
    "CaptchaManager",
    "CaptchaType",
    "DeepSeekProvider",
    "DynamicFetcher",
    "ExtractionResult",
    "Fetcher",
    # ai — JS 逆向 Agent 套件
    "HookLibrary",
    "JSAnalyzer",
    "JSFragment",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "OpenAICompatibleProvider",
    "ProxyPool",
    # spider
    "Request",
    # response
    "Response",
    "ReverseAgent",
    "ReverseAgentConfig",
    # mcp server
    "ReverseMCPServer",
    "RobotsPolicy",
    "ScrapeResult",
    # parser / adaptive
    "Selector",
    "Spider",
    "SpiderError",
    "SpiderStats",
    "StealthyFetcher",
    # visual extraction (PixelRAG-style)
    "VisualExtractor",
    "__version__",
    "available_providers",
    "compute_fingerprint",
    "detect_block",
    "generate_combined_script",
    # ai
    "get_provider",
    "register_provider",
    "similarity_score",
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
    from .ai.agent import AIScrapeAgent, RobotsPolicy, ScrapeResult
    from .ai.extractor import AIExtractor, ExtractionResult
    from .ai.llm import (
        DeepSeekProvider,
        LLMMessage,
        LLMProvider,
        LLMResponse,
        OpenAICompatibleProvider,
        available_providers,
        get_provider,
        register_provider,
    )
    from .fetchers._base import BaseFetcher
    from .fetchers.camoufox import CamoufoxFetcher
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

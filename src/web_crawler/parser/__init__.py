"""Scrapling 风格的自适应解析子包。

子包导入保持轻量：公开符号首次访问时按映射加载对应模块，避免只用到
``Selector`` 时连带载入链接提取、字段规则及存储实现。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AdaptiveStorage": ("web_crawler.parser.adaptive", "AdaptiveStorage"),
    "Adaptor": ("web_crawler.parser.selector", "Adaptor"),
    "Adaptors": ("web_crawler.parser.selector", "Adaptors"),
    "Attrs": ("web_crawler._types", "Attrs"),
    "CoverageReport": ("web_crawler.parser.rules", "CoverageReport"),
    "ExtractionRules": ("web_crawler.parser.rules", "ExtractionRules"),
    "Link": ("web_crawler.parser.links", "Link"),
    "LinkExtractor": ("web_crawler.parser.links", "LinkExtractor"),
    "ResultList": ("web_crawler._types", "ResultList"),
    "RuleCoverage": ("web_crawler.parser.rules", "RuleCoverage"),
    "Selector": ("web_crawler.parser.selector", "Selector"),
    "Selectors": ("web_crawler._types", "Selectors"),
    "TextHandler": ("web_crawler._types", "TextHandler"),
    "TextHandlers": ("web_crawler._types", "TextHandlers"),
    "best_match": ("web_crawler.parser.adaptive", "best_match"),
    "compute_fingerprint": ("web_crawler.parser.adaptive", "compute_fingerprint"),
    "similarity_score": ("web_crawler.parser.adaptive", "similarity_score"),
}

__all__ = [
    "AdaptiveStorage",
    "Adaptor",
    "Adaptors",
    "Attrs",
    "CoverageReport",
    "ExtractionRules",
    "Link",
    "LinkExtractor",
    "ResultList",
    "RuleCoverage",
    "Selector",
    "Selectors",
    "TextHandler",
    "TextHandlers",
    "best_match",
    "compute_fingerprint",
    "similarity_score",
]


def __getattr__(name: str) -> Any:
    if name not in _LAZY_IMPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module_path, attr_name = _LAZY_IMPORTS[name]
    value = getattr(importlib.import_module(module_path), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


if TYPE_CHECKING:
    from .._types import Attrs, ResultList, Selectors, TextHandler, TextHandlers
    from .adaptive import AdaptiveStorage, best_match, compute_fingerprint, similarity_score
    from .links import Link, LinkExtractor
    from .rules import CoverageReport, ExtractionRules, RuleCoverage
    from .selector import Adaptor, Adaptors, Selector

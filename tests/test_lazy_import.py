"""Tests for lazy imports, public API surface, and backward-compat aliases."""

from __future__ import annotations

import importlib
import sys

import pytest


def test_import_does_not_load_playwright() -> None:
    """``import web_crawler`` must not load the playwright package."""
    # Save & restore sys.modules so later tests still see the original
    # module objects (patch targets must match the class's module globals).
    saved = dict(sys.modules)
    for mod in list(sys.modules):
        if mod.startswith("web_crawler") or mod == "playwright":
            del sys.modules[mod]
    try:
        importlib.import_module("web_crawler")
        assert "playwright" not in sys.modules, "import web_crawler loaded playwright"
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


def test_import_does_not_load_curl_cffi() -> None:
    """``import web_crawler`` must not load curl_cffi at import time."""
    saved = dict(sys.modules)
    for mod in list(sys.modules):
        if mod.startswith("web_crawler") or mod == "curl_cffi":
            del sys.modules[mod]
    try:
        importlib.import_module("web_crawler")
        assert "curl_cffi" not in sys.modules, "import web_crawler loaded curl_cffi"
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


def test_selector_is_lazily_resolved() -> None:
    """Accessing Selector triggers the lazy import and caches it."""
    import web_crawler

    # Force re-resolve by deleting the cached attribute.
    if "Selector" in web_crawler.__dict__:
        del web_crawler.__dict__["Selector"]
    sel_cls = web_crawler.Selector
    from web_crawler.parser.selector import Selector as Direct

    assert sel_cls is Direct
    # Now cached
    assert web_crawler.__dict__["Selector"] is Direct


def test_unknown_attribute_raises() -> None:
    import web_crawler

    with pytest.raises(AttributeError, match="DoesNotExist"):
        web_crawler.DoesNotExist  # noqa: B018


def test_dir_includes_all_public_names() -> None:
    import web_crawler

    names = dir(web_crawler)
    for expected in ["Selector", "Fetcher", "AsyncFetcher", "Response", "Spider", "__version__"]:
        assert expected in names


def test_adaptor_alias_equals_selector() -> None:
    import web_crawler

    assert web_crawler.Adaptor is web_crawler.Selector
    from web_crawler.parser.selector import Adaptor, Selector

    assert Adaptor is Selector


def test_adaptors_exported() -> None:
    import web_crawler

    assert hasattr(web_crawler, "Adaptors")
    from web_crawler.parser.selector import Adaptors

    assert web_crawler.Adaptors is Adaptors


def test_async_fetcher_exported_and_distinct() -> None:
    import web_crawler

    assert hasattr(web_crawler, "AsyncFetcher")
    assert web_crawler.AsyncFetcher is not web_crawler.Fetcher


def test_version_is_string() -> None:
    import web_crawler

    assert isinstance(web_crawler.__version__, str)
    assert len(web_crawler.__version__) > 0

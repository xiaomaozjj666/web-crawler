"""compat 模块可选依赖检测的单元测试。

覆盖 require_curl_cffi / require_playwright / require_camoufox 在依赖
缺失时抛 ImportError 的分支，通过 mock HAS_* 标志实现（测试环境已安装
全部可选依赖，这些分支在真实环境下不会触发）。
"""

from __future__ import annotations

import pytest

from web_crawler import compat


def test_require_curl_cffi_passes_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """HAS_CURL_CFFI=True 时不抛异常。"""
    monkeypatch.setattr(compat, "HAS_CURL_CFFI", True)
    compat.require_curl_cffi()  # 不应抛


def test_require_curl_cffi_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """HAS_CURL_CFFI=False 时抛 ImportError。"""
    monkeypatch.setattr(compat, "HAS_CURL_CFFI", False)
    with pytest.raises(ImportError, match="curl_cffi"):
        compat.require_curl_cffi()


def test_require_playwright_passes_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compat, "HAS_PLAYWRIGHT", True)
    compat.require_playwright()


def test_require_playwright_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compat, "HAS_PLAYWRIGHT", False)
    with pytest.raises(ImportError, match="playwright"):
        compat.require_playwright()


def test_require_camoufox_passes_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compat, "HAS_CAMOUFOX", True)
    compat.require_camoufox()


def test_require_camoufox_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compat, "HAS_CAMOUFOX", False)
    with pytest.raises(ImportError, match="camoufox"):
        compat.require_camoufox()

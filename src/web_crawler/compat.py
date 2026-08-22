"""可选依赖探测。

Scrapling 风格架构：核心解析器与自适应引擎只依赖 Python 标准库加
``lxml``/``cssselect``（必装项）。重型能力（TLS 指纹隐身抓取、JavaScript
渲染）由可选的第三方包支撑，缺失时优雅降级。
"""

from __future__ import annotations

try:
    import curl_cffi  # noqa: F401
    from curl_cffi import requests as _curl_requests  # noqa: F401

    HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover - 仅在未安装 curl_cffi 时执行
    HAS_CURL_CFFI = False

try:
    import playwright  # noqa: F401
    from playwright.async_api import async_playwright  # noqa: F401

    HAS_PLAYWRIGHT = True
except ImportError:  # pragma: no cover - 仅在未安装 playwright 时执行
    HAS_PLAYWRIGHT = False

try:
    import httpx  # noqa: F401

    HAS_HTTPX = True
except ImportError:  # pragma: no cover
    HAS_HTTPX = False

try:
    import camoufox  # noqa: F401
    from camoufox.sync_api import Camoufox  # noqa: F401

    HAS_CAMOUFOX = True
except ImportError:  # pragma: no cover - 仅在未安装 camoufox 时执行
    HAS_CAMOUFOX = False


def require_curl_cffi() -> None:
    """``curl_cffi`` 未安装时抛出带安装指引的异常。"""
    if not HAS_CURL_CFFI:
        raise ImportError(
            "curl_cffi is required for stealth HTTP fetching with TLS "
            "fingerprint impersonation. Install it with: pip install curl_cffi"
        )


def require_playwright() -> None:
    """``playwright`` 未安装时抛出带安装指引的异常。"""
    if not HAS_PLAYWRIGHT:
        raise ImportError(
            "playwright is required for JavaScript-rendered fetching. "
            "Install it with: pip install playwright && playwright install chromium"
        )


def require_camoufox() -> None:
    """``camoufox`` 未安装时抛出带安装指引的异常。"""
    if not HAS_CAMOUFOX:
        raise ImportError(
            "camoufox is required for the anti-fingerprint Firefox fetcher. "
            "Install it with: pip install camoufox[geoip] && camoufox fetch"
        )


__all__ = [
    "HAS_CAMOUFOX",
    "HAS_CURL_CFFI",
    "HAS_HTTPX",
    "HAS_PLAYWRIGHT",
    "require_camoufox",
    "require_curl_cffi",
    "require_playwright",
]

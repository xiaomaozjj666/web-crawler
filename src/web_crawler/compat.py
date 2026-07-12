"""Optional dependency detection.

Scrapling-style architecture: the core parser and adaptive engine depend only
on the Python standard library plus ``lxml``/``cssselect`` (already required).
Heavy capabilities (TLS-fingerprint stealth fetching, JavaScript rendering) are
backed by optional third-party packages and degrade gracefully when absent.
"""

from __future__ import annotations

try:
    import curl_cffi  # noqa: F401
    from curl_cffi import requests as _curl_requests  # noqa: F401

    HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover - exercised only without curl_cffi
    HAS_CURL_CFFI = False

try:
    import playwright  # noqa: F401
    from playwright.async_api import async_playwright  # noqa: F401

    HAS_PLAYWRIGHT = True
except ImportError:  # pragma: no cover - exercised only without playwright
    HAS_PLAYWRIGHT = False

try:
    import httpx  # noqa: F401

    HAS_HTTPX = True
except ImportError:  # pragma: no cover
    HAS_HTTPX = False


def require_curl_cffi() -> None:
    """Raise an informative error if ``curl_cffi`` is not installed."""
    if not HAS_CURL_CFFI:
        raise ImportError(
            "curl_cffi is required for stealth HTTP fetching with TLS "
            "fingerprint impersonation. Install it with: pip install curl_cffi"
        )


def require_playwright() -> None:
    """Raise an informative error if ``playwright`` is not installed."""
    if not HAS_PLAYWRIGHT:
        raise ImportError(
            "playwright is required for JavaScript-rendered fetching. "
            "Install it with: pip install playwright && playwright install chromium"
        )


__all__ = [
    "HAS_CURL_CFFI",
    "HAS_PLAYWRIGHT",
    "HAS_HTTPX",
    "require_curl_cffi",
    "require_playwright",
]

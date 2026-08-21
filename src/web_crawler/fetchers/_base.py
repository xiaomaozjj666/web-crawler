"""Shared configuration base for all fetchers.

Mirrors Scrapling's ``BaseFetcher``: a single place that holds the options
common to the HTTP, dynamic and stealthy fetchers (timeouts, proxies, retries,
adaptive storage, default headers) plus the helpers that turn a raw transport
response into the library-wide :class:`~web_crawler.response.Response`.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .._ssrf import host_is_unsafe, is_private_ip, is_unsafe_hostname, validate_url_host
from ..response import Response
from .proxy import ProxyPool

if TYPE_CHECKING:
    from ..parser.adaptive import AdaptiveStorage

# SSRF 防护：仅允许 http/https 协议
_ALLOWED_URL_SCHEMES = ("http", "https")

# 测试套件用本地 HTTP 服务器（127.0.0.1）验证真实抓取行为；设置该环境变量
# 后 fetcher 入口跳过 host 层校验（scheme 校验仍保留）。生产环境不设置。
_ALLOW_PRIVATE_HOSTS_ENV = "WEB_CRAWLER_ALLOW_PRIVATE_HOSTS"


def _default_allow_private_hosts() -> bool:
    return os.environ.get(_ALLOW_PRIVATE_HOSTS_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def validate_url_scheme(url: str) -> None:
    """拒绝 scheme 非 http/https 的 URL（SSRF 防护）。

    在每次抓取入口与每一个重定向跳转前调用，确保 ``file://``/``ftp://``/
    ``data:``/``gopher://`` 等 URL 在到达传输层之前被拒绝——curl_cffi 的
    libcurl 与 Playwright 都支持 ``file://``，会直接读取本地文件。
    """
    scheme = urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(
            f"URL scheme {scheme or '<none>'!r} is not allowed; only http/https are supported"
        )


def validate_url(url: str, *, resolve: bool = False) -> None:
    """拒绝 scheme 或 host 不安全的 URL（SSRF 防护，抓取入口统一调用）。

    在 :func:`validate_url_scheme` 之外补上 host 层校验：私网/环回/链路本地
    （含云元数据 169.254.169.254）与 localhost/*.localhost/*.local 等目标
    在到达传输层之前被拒绝。``resolve=True`` 时额外对主机名做一次 DNS 解析
    检查（解析失败按保守策略拒绝）。
    """
    validate_url_scheme(url)
    validate_url_host(url, resolve=resolve)


class BaseFetcher:
    """Shared configuration base class for all fetchers.

    Subclasses (``Fetcher``, ``DynamicFetcher``, ``StealthyFetcher``) inherit
    these options and the response-building helpers so that every fetcher
    returns a uniform :class:`Response`.
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        proxy: str | ProxyPool | None = None,
        retries: int = 0,
        adaptive: bool = False,
        storage: AdaptiveStorage | None = None,
        extra_headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
        verify: bool = True,
        allow_private_hosts: bool | None = None,
        resolve_hosts: bool = False,
    ) -> None:
        self.timeout = timeout
        self.proxy = proxy
        self.retries = retries
        self.adaptive = adaptive
        self.storage = storage
        self.extra_headers: dict[str, str] = dict(extra_headers) if extra_headers else {}
        self.follow_redirects = follow_redirects
        self.verify = verify
        # SSRF host 层校验开关：默认拒绝私网/环回/链路本地 host。
        # allow_private_hosts=True 时仅校验 scheme（本地测试服务器/内网目标
        # 显式放行）；resolve_hosts=True 时对主机名额外做 DNS 解析检查。
        self.allow_private_hosts = (
            _default_allow_private_hosts() if allow_private_hosts is None else allow_private_hosts
        )
        self.resolve_hosts = resolve_hosts

    def _validate_target(self, url: str) -> None:
        """抓取前的 scheme + host 双重校验（SSRF 防护统一入口）。"""
        if self.allow_private_hosts:
            validate_url_scheme(url)
        else:
            validate_url(url, resolve=self.resolve_hosts)

    def _resolve_proxy(self) -> str | None:
        """Resolve the proxy to use for the next request.

        A :class:`ProxyPool` is queried (and thus rotated) per-request; a plain
        string is returned as-is; ``None`` means no proxy.
        """
        if self.proxy is None:
            return None
        if isinstance(self.proxy, ProxyPool):
            return self.proxy.get()
        return self.proxy

    def _default_headers(self) -> dict[str, str]:
        """Realistic browser-style headers to lower bot-detection probability."""
        return {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "max-age=0",
            "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def _build_response(
        self,
        url: str,
        status: int,
        content: bytes,
        headers: dict[str, str] | None,
        *,
        request_headers: dict[str, str] | None = None,
    ) -> Response:
        """Wrap a raw transport response in the library-wide :class:`Response`."""
        return Response(
            url=url,
            status=status,
            content=content,
            headers=headers,
            request_headers=request_headers,
            storage=self.storage,
            adaptive=self.adaptive,
        )


__all__ = [
    "BaseFetcher",
    "host_is_unsafe",
    "is_private_ip",
    "is_unsafe_hostname",
    "validate_url",
    "validate_url_host",
    "validate_url_scheme",
]

"""Shared configuration base for all fetchers.

Mirrors Scrapling's ``BaseFetcher``: a single place that holds the options
common to the HTTP, dynamic and stealthy fetchers (timeouts, proxies, retries,
adaptive storage, default headers) plus the helpers that turn a raw transport
response into the library-wide :class:`~web_crawler.response.Response`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..response import Response
from .proxy import ProxyPool

if TYPE_CHECKING:
    from ..parser.adaptive import AdaptiveStorage


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
    ) -> None:
        self.timeout = timeout
        self.proxy = proxy
        self.retries = retries
        self.adaptive = adaptive
        self.storage = storage
        self.extra_headers: dict[str, str] = dict(extra_headers) if extra_headers else {}
        self.follow_redirects = follow_redirects
        self.verify = verify

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


__all__ = ["BaseFetcher"]

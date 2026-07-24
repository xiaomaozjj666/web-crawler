"""Response object returned by all fetchers.

A single :class:`Response` normalizes the output of the HTTP, dynamic and
stealthy fetchers so downstream code can treat them uniformly — including
Scrapling-style selector helpers (``response.css``, ``response.xpath``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from ._types import ResultList
from .parser.selector import Selector

if TYPE_CHECKING:
    from .parser.adaptive import AdaptiveStorage


class Response:
    """A normalized fetch response with selector helpers."""

    def __init__(
        self,
        url: str,
        status: int,
        content: bytes,
        headers: dict[str, str] | None = None,
        *,
        encoding: str = "utf-8",
        request_headers: dict[str, str] | None = None,
        storage: AdaptiveStorage | None = None,
        adaptive: bool = False,
        screenshots: list[dict[str, Any]] | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self.content = content
        self.headers = headers or {}
        self.encoding = encoding
        self.request_headers = request_headers or {}
        self._storage = storage
        self._adaptive = adaptive
        self._selector: Selector | None = None
        # Free-form bag for spider callbacks to pass state across requests.
        self.meta: dict[str, Any] = {}
        # PixelRAG-style screenshot tiles (populated by DynamicFetcher).
        self.screenshots: list[dict[str, Any]] | None = screenshots

    # -- text / parsing ----------------------------------------------------
    @property
    def text(self) -> str:
        return self.content.decode(self.encoding, errors="replace")

    @property
    def selector(self) -> Selector:
        """Lazily parsed :class:`Selector` over the response body."""
        if self._selector is None:
            self._selector = Selector(
                self.content,
                url=self.url,
                adaptive=self._adaptive,
                storage=self._storage,
            )
        return self._selector

    def css(self, selector: str, **kwargs: Any) -> ResultList[Selector]:
        return self.selector.css(selector, **kwargs)

    def css_first(
        self, selector: str, default: Selector | None = None, **kwargs: Any
    ) -> Selector | None:
        return self.selector.css_first(selector, default, **kwargs)

    def xpath(self, selector: str, **kwargs: Any) -> ResultList[Selector]:
        return self.selector.xpath(selector, **kwargs)

    def xpath_first(
        self, selector: str, default: Selector | None = None, **kwargs: Any
    ) -> Selector | None:
        return self.selector.xpath_first(selector, default, **kwargs)

    # -- conveniences ------------------------------------------------------
    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    def json(self, **kwargs: Any) -> Any:
        import json

        return json.loads(self.text, **kwargs)

    def urljoin(self, href: str) -> str:
        """Resolve ``href`` against this response's URL (Scrapling/Scrapy-style)."""
        return urljoin(self.url, href)

    def __repr__(self) -> str:
        return f"<Response {self.status} {self.url}>"


__all__ = ["Response"]

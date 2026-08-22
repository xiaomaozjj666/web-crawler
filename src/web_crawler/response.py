"""所有 fetcher 统一返回的 Response 对象。

用同一个 :class:`Response` 归一化 HTTP、动态与隐身 fetcher 的输出，让
下游代码以一致方式处理——包括 Scrapling 风格的选择器助手
（``response.css``、``response.xpath``）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from ._types import ResultList
from .parser.selector import Selector

if TYPE_CHECKING:
    from .parser.adaptive import AdaptiveStorage


class Response:
    """带选择器助手的归一化抓取响应。"""

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
        # 供 spider 回调跨请求传递状态的自由容器。
        self.meta: dict[str, Any] = {}
        # PixelRAG 风格的截图分块（由 DynamicFetcher 填充）。
        self.screenshots: list[dict[str, Any]] | None = screenshots

    # -- 文本 / 解析 --------------------------------------------------------
    @property
    def text(self) -> str:
        return self.content.decode(self.encoding, errors="replace")

    @property
    def selector(self) -> Selector:
        """对响应体惰性解析得到的 :class:`Selector`。"""
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

    # -- 便捷方法 -----------------------------------------------------------
    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    def json(self, **kwargs: Any) -> Any:
        import json

        return json.loads(self.text, **kwargs)

    def urljoin(self, href: str) -> str:
        """以本响应的 URL 为基准解析 ``href``（Scrapling/Scrapy 风格）。"""
        return urljoin(self.url, href)

    def __repr__(self) -> str:
        return f"<Response {self.status} {self.url}>"


__all__ = ["Response"]

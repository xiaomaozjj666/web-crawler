"""Scrapling 风格的 fetcher 子包。

为三种互补的抓取策略提供统一接口，全部返回库级统一的
:class:`~web_crawler.response.Response`：

* :class:`Fetcher` — 基于 ``curl_cffi`` TLS 指纹伪装的隐身 HTTP
  （带 ``httpx`` 兜底）。
* :class:`AsyncFetcher` — :class:`Fetcher` 的纯异步版本。
* :class:`DynamicFetcher` — 基于 Playwright 的 JavaScript 渲染抓取。
* :class:`StealthyFetcher` — 用隐身脚本、拟人输入与 Cloudflare 质询处理
  加固过的 :class:`DynamicFetcher`。

另提供 :class:`ProxyPool` 用于带冷却的代理轮换。

为了避免只需 HTTP fetcher 时也强制加载 ``playwright``，
:class:`DynamicFetcher` 与 :class:`StealthyFetcher` 通过 ``__getattr__``
惰性导入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._base import BaseFetcher
from .fetcher import AsyncFetcher, Fetcher
from .proxy import ProxyPool

# DynamicFetcher / StealthyFetcher 会引入 playwright；惰性导入，让
# ``from web_crawler.fetchers import Fetcher`` 保持轻量。
_LAZY: dict[str, tuple[str, str]] = {
    "DynamicFetcher": ("web_crawler.fetchers.dynamic", "DynamicFetcher"),
    "StealthyFetcher": ("web_crawler.fetchers.stealthy", "StealthyFetcher"),
    "CamoufoxFetcher": ("web_crawler.fetchers.camoufox", "CamoufoxFetcher"),
}

__all__ = [
    "AsyncFetcher",
    "BaseFetcher",
    "CamoufoxFetcher",
    "DynamicFetcher",
    "Fetcher",
    "ProxyPool",
    "StealthyFetcher",
]


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        import importlib

        module_path, attr_name = _LAZY[name]
        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


if TYPE_CHECKING:
    from .camoufox import CamoufoxFetcher
    from .dynamic import DynamicFetcher
    from .stealthy import StealthyFetcher

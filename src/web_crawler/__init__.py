"""web_crawler — 对齐 Scrapling 的隐身抓取库。

公开接口对齐 Scrapling 的高层 API：

- :class:`Selector` / :class:`Adaptors` — 自适应 lxml 选择器，支持元素指纹
  与结构相似度重定位。
- :class:`Response` — 带选择器助手的归一化抓取结果。
- :class:`Fetcher` — 基于 ``curl_cffi`` TLS 指纹的隐身 HTTP（httpx 兜底）。
- :class:`AsyncFetcher` — 纯异步 fetcher（同样的隐身能力，纯异步 API）。
- :class:`DynamicFetcher` — Playwright 渲染抓取。
- :class:`StealthyFetcher` — 反反爬 / 感知 Cloudflare 的 Playwright fetcher。
- :class:`ProxyPool` — 轮询 / 随机代理轮换，带健康跟踪。
- :class:`SessionPool` — curl_cffi 会话池，指纹随会话轮换（round_robin /
  random），空闲回收与失败淘汰。
- :class:`Spider` / :class:`Request` — 回调驱动的 spider 框架，支持
  暂停/恢复、:class:`CacheMiddleware` 磁盘缓存（TTL / 离线回放）、
  :class:`AutoThrottle` 自适应限速、robots ``Crawl-delay`` 遵守、
  stats JSON/JSONL 导出、:class:`FeedExportPipeline` item 落盘导出
  （JSON 快照 / JSONL 追加 / CSV，``feed_output=`` 便捷参数直达）与
  :class:`ValidationPipeline` item 校验管道（pydantic 模型或零依赖
  dict schema，失败丢弃并计数 ``items_dropped``）。
- :class:`LinkExtractor` / :class:`Link` — Scrapy 风格的链接提取与过滤
  （allow/deny 正则、域白/黑名单、规范化去重）。
- :class:`CrawlSpider` / :class:`Rule` — 规则驱动的跟随爬虫（对标
  Scrapy CrawlSpider），配合 ``LinkExtractor`` 声明式全站遍历。
- :class:`SitemapSpider` — sitemap.xml 驱动的爬虫（对标 Scrapy
  SitemapSpider）：sitemapindex 递归展开、``sitemap_rules`` 正则分发
  回调、robots.txt 的 ``Sitemap:`` 行自动发现。
- :class:`WebCrawlerError` — 统一异常体系根（子类兼容
  ``ValueError`` / ``RuntimeError``），可用 ``except WebCrawlerError``
  一次捕获库自身的错误。

所有公开符号均为惰性导入：``import web_crawler`` 不会引入 ``playwright``
或 ``curl_cffi``。重型子模块仅在对应类首次访问时加载（Scrapling 采用同样
的模式）。

快速上手
--------
>>> from web_crawler import Fetcher, Selector
>>> fetcher = Fetcher(impersonate="chrome131")
>>> resp = fetcher.get("https://example.com")
>>> for link in resp.css("a"):
...     print(link.attr("href"))
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.6.0"
__author__ = "web-crawler contributors"

# 公开符号名 -> (模块路径, 属性名) 的映射。
# 模块在首次经 __getattr__ 访问时才导入，因此 ``import web_crawler``
# 绝不会触发 playwright/curl_cffi 加载。
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # 统一异常体系（兼容式多继承：子类同时是 ValueError/RuntimeError）
    "WebCrawlerError": ("web_crawler.exceptions", "WebCrawlerError"),
    "UnsafeTargetError": ("web_crawler.exceptions", "UnsafeTargetError"),
    "ConfigError": ("web_crawler.exceptions", "ConfigError"),
    "ParseError": ("web_crawler.exceptions", "ParseError"),
    "FetchError": ("web_crawler.exceptions", "FetchError"),
    "InvalidStateError": ("web_crawler.exceptions", "InvalidStateError"),
    "AIProviderError": ("web_crawler.exceptions", "AIProviderError"),
    # parser / 自适应
    "Selector": ("web_crawler.parser.selector", "Selector"),
    "Adaptors": ("web_crawler.parser.selector", "Adaptors"),
    "Adaptor": ("web_crawler.parser.selector", "Selector"),
    "AdaptiveStorage": ("web_crawler.parser.adaptive", "AdaptiveStorage"),
    "compute_fingerprint": ("web_crawler.parser.adaptive", "compute_fingerprint"),
    "similarity_score": ("web_crawler.parser.adaptive", "similarity_score"),
    # parser / 链接提取
    "Link": ("web_crawler.parser.links", "Link"),
    "LinkExtractor": ("web_crawler.parser.links", "LinkExtractor"),
    # fetcher 系列
    "BaseFetcher": ("web_crawler.fetchers._base", "BaseFetcher"),
    "Fetcher": ("web_crawler.fetchers.fetcher", "Fetcher"),
    "AsyncFetcher": ("web_crawler.fetchers.fetcher", "AsyncFetcher"),
    "DynamicFetcher": ("web_crawler.fetchers.dynamic", "DynamicFetcher"),
    "StealthyFetcher": ("web_crawler.fetchers.stealthy", "StealthyFetcher"),
    "CamoufoxFetcher": ("web_crawler.fetchers.camoufox", "CamoufoxFetcher"),
    "ProxyPool": ("web_crawler.fetchers.proxy", "ProxyPool"),
    "SessionPool": ("web_crawler.fetchers.session", "SessionPool"),
    # 响应对象
    "Response": ("web_crawler.response", "Response"),
    # 可视化提取（PixelRAG 风格）
    "VisualExtractor": ("web_crawler.parser.visual", "VisualExtractor"),
    # spider 框架
    "AutoThrottle": ("web_crawler.spider.autothrottle", "AutoThrottle"),
    "CacheMiddleware": ("web_crawler.spider.cache", "CacheMiddleware"),
    "DupeFilter": ("web_crawler.spider.spider", "DupeFilter"),
    "DownloaderMiddleware": ("web_crawler.spider.spider", "DownloaderMiddleware"),
    "DropItem": ("web_crawler.spider.spider", "DropItem"),
    "FeedExportPipeline": ("web_crawler.spider.feed", "FeedExportPipeline"),
    "IgnoreRequest": ("web_crawler.spider.spider", "IgnoreRequest"),
    "ItemPipeline": ("web_crawler.spider.spider", "ItemPipeline"),
    "Request": ("web_crawler.spider.spider", "Request"),
    "Spider": ("web_crawler.spider.spider", "Spider"),
    "SpiderError": ("web_crawler.spider.spider", "SpiderError"),
    "SpiderStats": ("web_crawler.spider.spider", "SpiderStats"),
    # spider / item 校验管道
    "ValidationPipeline": ("web_crawler.spider.validation", "ValidationPipeline"),
    # spider / 规则驱动跟随爬虫
    "CrawlSpider": ("web_crawler.spider.crawl", "CrawlSpider"),
    "Rule": ("web_crawler.spider.crawl", "Rule"),
    # spider / sitemap 驱动爬虫
    "SitemapSpider": ("web_crawler.spider.sitemap", "SitemapSpider"),
    # ai（可插拔 LLM 层 + AI 辅助提取；默认 DeepSeek-V4.1-Flash）
    "get_provider": ("web_crawler.ai.llm", "get_provider"),
    "register_provider": ("web_crawler.ai.llm", "register_provider"),
    "available_providers": ("web_crawler.ai.llm", "available_providers"),
    "LLMMessage": ("web_crawler.ai.llm", "LLMMessage"),
    "LLMResponse": ("web_crawler.ai.llm", "LLMResponse"),
    "LLMProvider": ("web_crawler.ai.llm", "LLMProvider"),
    "OpenAICompatibleProvider": ("web_crawler.ai.llm", "OpenAICompatibleProvider"),
    "DeepSeekProvider": ("web_crawler.ai.llm", "DeepSeekProvider"),
    "AIExtractor": ("web_crawler.ai.extractor", "AIExtractor"),
    "ExtractionResult": ("web_crawler.ai.extractor", "ExtractionResult"),
    "AIScrapeAgent": ("web_crawler.ai.agent", "AIScrapeAgent"),
    "ScrapeResult": ("web_crawler.ai.agent", "ScrapeResult"),
    "RobotsPolicy": ("web_crawler.ai.agent", "RobotsPolicy"),
    "detect_block": ("web_crawler.ai.agent", "detect_block"),
    # ai — JS 逆向 Agent 套件
    "HookLibrary": ("web_crawler.ai.hooks", "HookLibrary"),
    "generate_combined_script": ("web_crawler.ai.hooks", "generate_combined_script"),
    "JSAnalyzer": ("web_crawler.ai.analyzer", "JSAnalyzer"),
    "JSFragment": ("web_crawler.ai.analyzer", "JSFragment"),
    "CaptchaType": ("web_crawler.ai.captcha", "CaptchaType"),
    "CaptchaManager": ("web_crawler.ai.captcha", "CaptchaManager"),
    "ImageCaptchaSolver": ("web_crawler.ai.image_captcha", "ImageCaptchaSolver"),
    "ReverseAgent": ("web_crawler.ai.reverse_agent", "ReverseAgent"),
    "ReverseAgentConfig": ("web_crawler.ai.reverse_agent", "ReverseAgentConfig"),
    # MCP 服务器
    "ReverseMCPServer": ("web_crawler.mcp.server", "ReverseMCPServer"),
}

__all__ = [
    "AIExtractor",
    "AIProviderError",
    "AIScrapeAgent",
    "AdaptiveStorage",
    "Adaptor",
    "Adaptors",
    "AsyncFetcher",
    "AutoThrottle",
    # fetcher 系列
    "BaseFetcher",
    "CacheMiddleware",
    "CamoufoxFetcher",
    "CaptchaManager",
    "CaptchaType",
    "ConfigError",
    "CrawlSpider",
    "DeepSeekProvider",
    "DownloaderMiddleware",
    "DropItem",
    "DupeFilter",
    "DynamicFetcher",
    "ExtractionResult",
    "FeedExportPipeline",
    "FetchError",
    "Fetcher",
    # ai — JS 逆向 Agent 套件
    "HookLibrary",
    "IgnoreRequest",
    "ImageCaptchaSolver",
    "InvalidStateError",
    "ItemPipeline",
    "JSAnalyzer",
    "JSFragment",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "Link",
    "LinkExtractor",
    "OpenAICompatibleProvider",
    "ParseError",
    "ProxyPool",
    # spider 框架
    "Request",
    # 响应对象
    "Response",
    "ReverseAgent",
    "ReverseAgentConfig",
    # MCP 服务器
    "ReverseMCPServer",
    "RobotsPolicy",
    "Rule",
    "ScrapeResult",
    # parser / 自适应
    "Selector",
    "SessionPool",
    "SitemapSpider",
    "Spider",
    "SpiderError",
    "SpiderStats",
    "StealthyFetcher",
    "UnsafeTargetError",
    # spider / item 校验管道
    "ValidationPipeline",
    # 可视化提取（PixelRAG 风格）
    "VisualExtractor",
    "WebCrawlerError",
    "__version__",
    "available_providers",
    "compute_fingerprint",
    "detect_block",
    "generate_combined_script",
    # ai
    "get_provider",
    "register_provider",
    "similarity_score",
]


def __getattr__(name: str) -> Any:
    """首次访问时惰性导入公开符号（Scrapling 风格）。

    这样 ``import web_crawler`` 保持轻量，只用到解析器的用户不必安装
    playwright/curl_cffi。
    """
    if name in _LAZY_IMPORTS:
        import importlib

        module_path, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        # 缓存到本模块，后续查找可跳过导入。
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


# 供类型检查器解析真实符号，让 IDE/mypy 看到完整签名。
if TYPE_CHECKING:
    from .ai.agent import AIScrapeAgent, RobotsPolicy, ScrapeResult
    from .ai.extractor import AIExtractor, ExtractionResult
    from .ai.image_captcha import ImageCaptchaSolver
    from .ai.llm import (
        DeepSeekProvider,
        LLMMessage,
        LLMProvider,
        LLMResponse,
        OpenAICompatibleProvider,
        available_providers,
        get_provider,
        register_provider,
    )
    from .exceptions import (
        AIProviderError,
        ConfigError,
        FetchError,
        InvalidStateError,
        ParseError,
        UnsafeTargetError,
        WebCrawlerError,
    )
    from .fetchers._base import BaseFetcher
    from .fetchers.camoufox import CamoufoxFetcher
    from .fetchers.dynamic import DynamicFetcher
    from .fetchers.fetcher import AsyncFetcher, Fetcher
    from .fetchers.proxy import ProxyPool
    from .fetchers.session import SessionPool
    from .fetchers.stealthy import StealthyFetcher
    from .parser.adaptive import AdaptiveStorage, compute_fingerprint, similarity_score
    from .parser.links import Link, LinkExtractor
    from .parser.selector import Adaptors, Selector
    from .parser.visual import VisualExtractor
    from .response import Response
    from .spider.autothrottle import AutoThrottle
    from .spider.cache import CacheMiddleware
    from .spider.crawl import CrawlSpider, Rule
    from .spider.feed import FeedExportPipeline
    from .spider.sitemap import SitemapSpider
    from .spider.spider import (
        DownloaderMiddleware,
        DropItem,
        DupeFilter,
        IgnoreRequest,
        ItemPipeline,
        Request,
        Spider,
        SpiderError,
        SpiderStats,
    )
    from .spider.validation import ValidationPipeline

    Adaptor = Selector

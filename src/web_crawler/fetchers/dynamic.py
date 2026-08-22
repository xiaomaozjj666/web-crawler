"""基于 Playwright 的 JavaScript 渲染 fetcher。

对齐 Scrapling 的 ``DynamicFetcher``：用真实 headless Chromium 渲染页面，
从而能够抓取重 JavaScript 的站点。单个浏览器实例跨多次抓取复用以提升
性能，而每次抓取创建全新的浏览器 context，避免请求间 cookie/状态泄漏。

类暴露了少量受保护的钩子方法（``_setup_page``、``_post_load``），隐身子类
可以借此注入隐身脚本与拟人行为，而无需复制渲染流程。

受 PixelRAG 启发：``screenshot_tiles()`` 渲染页面并切成固定高度的截图分块，
可直接用于视觉嵌入或 VLM 抽取。
"""

from __future__ import annotations

import asyncio
import base64
import math
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from typing_extensions import Self

from ..compat import require_playwright
from ._base import BaseFetcher
from .proxy import ProxyPool

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from ..parser.adaptive import AdaptiveStorage

# 真实 Chrome UA，降低被识别为自动化浏览器的概率
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# disable_resources 时拦截的资源类型，加速渲染
_BLOCKED_RESOURCE_TYPES: tuple[str, ...] = ("image", "media", "font", "stylesheet")


class DynamicFetcher(BaseFetcher):
    """基于 Playwright 的 fetcher，先渲染 JavaScript 再返回 HTML。

    Parameters
    ----------
    headless:
        以 headless 模式运行 Chromium。
    block_images:
        中止图片请求以加速渲染。
    disable_resources:
        中止图片/媒体/字体/样式表请求，追求最快速度。
    wait_selector:
        可选的 CSS 选择器，导航完成后等待其出现。
    wait_timeout:
        ``wait_selector`` 与 ``networkidle`` 等待的超时时间（秒）。
    network_idle:
        导航后等待 ``networkidle`` 加载状态。
    page_action:
        页面加载后以 Playwright ``Page`` 为参数调用的回调，可在截取 HTML
        前执行自定义交互（滚动、点击等）。
    google_search:
        将 referer 设为 ``https://www.google.com/``，伪装流量来源。
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        proxy: str | ProxyPool | None = None,
        timeout: float = 30.0,
        adaptive: bool = False,
        storage: AdaptiveStorage | None = None,
        extra_headers: dict[str, str] | None = None,
        block_images: bool = False,
        disable_resources: bool = False,
        wait_selector: str | None = None,
        wait_timeout: float = 10.0,
        network_idle: bool = True,
        page_action: Callable[[Page], None] | None = None,
        google_search: bool = False,
        verify: bool = True,
        allow_private_hosts: bool | None = None,
        resolve_hosts: bool = False,
    ) -> None:
        super().__init__(
            timeout=timeout,
            proxy=proxy,
            retries=0,
            adaptive=adaptive,
            storage=storage,
            extra_headers=extra_headers,
            follow_redirects=True,
            verify=verify,
            allow_private_hosts=allow_private_hosts,
            resolve_hosts=resolve_hosts,
        )
        require_playwright()
        self.headless = headless
        self.block_images = block_images
        self.disable_resources = disable_resources
        self.wait_selector = wait_selector
        self.wait_timeout = wait_timeout
        self.network_idle = network_idle
        self.page_action = page_action
        self.google_search = google_search
        self.user_agent = _DEFAULT_UA
        # browser 实例在 fetcher 生命周期内复用，每次 fetch 创建新 context
        self._pw: Any = None
        self._browser: Any = None
        self._async_pw: Any = None
        self._async_browser: Any = None

    # -- 代理 / 资源辅助 ------------------------------------------------------
    def _parse_proxy(self, proxy: str | None) -> dict[str, str] | None:
        """把代理 URL 转换为 Playwright 的 ``ProxySettings`` 字典。"""
        if not proxy:
            return None
        # 无 scheme 的代理 URL 默认按 http 处理；IPv6 地址需补方括号
        if "://" not in proxy:
            proxy = "http://" + proxy
        parsed = urlparse(proxy)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        server = f"{parsed.scheme}://{host}"
        if parsed.port:
            server += f":{parsed.port}"
        settings: dict[str, str] = {"server": server}
        # userinfo 可能是百分号编码（如 user%40x），解码后交给 Playwright
        if parsed.username:
            settings["username"] = unquote(parsed.username)
        if parsed.password:
            settings["password"] = unquote(parsed.password)
        return settings

    def _context_kwargs(
        self,
        *,
        viewport: dict[str, int] | None = None,
        proxy: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """构造 ``browser.new_context`` 的参数。

        子类（如 :class:`CamoufoxFetcher`）可覆写此方法以调整指纹策略——
        例如不传 user_agent/locale/viewport，避免覆盖 Camoufox 生成的指纹。
        """
        kwargs: dict[str, Any] = {
            "user_agent": self.user_agent,
            "locale": "en-US",
            "extra_http_headers": self.extra_headers or None,
            "proxy": proxy,
            "ignore_https_errors": not self.verify,
        }
        if viewport is not None:
            kwargs["viewport"] = viewport
        return kwargs

    def _blocked_types(self) -> set[str]:
        blocked: set[str] = set()
        if self.block_images:
            blocked.add("image")
        if self.disable_resources:
            blocked.update(_BLOCKED_RESOURCE_TYPES)
        return blocked

    def _make_route_handler(self, blocked: set[str]) -> Callable[..., Any]:
        def handler(route: Any) -> None:
            if route.request.resource_type in blocked:
                route.abort()
            else:
                route.continue_()

        return handler

    # -- 渲染钩子（由 StealthyFetcher 覆写） ----------------------------------
    def _setup_page(self, page: Any) -> None:
        """钩子：导航前配置页面（路由拦截）。"""
        blocked = self._blocked_types()
        if blocked:
            page.route("**/*", self._make_route_handler(blocked))

    def _post_load(self, page: Any) -> None:
        """钩子：导航后与页面交互（默认空实现）。"""

    # -- 同步浏览器生命周期 ---------------------------------------------------
    def _ensure_browser(self) -> Any:
        from playwright.sync_api import sync_playwright

        if self._browser is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self.headless)
        return self._browser

    def _render_page(self, browser: Any, url: str, proxy_settings: Any) -> Any:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        context = browser.new_context(
            **self._context_kwargs(viewport={"width": 1366, "height": 768}, proxy=proxy_settings)
        )
        try:
            page = context.new_page()
            self._setup_page(page)
            referer = "https://www.google.com/" if self.google_search else None
            resp = page.goto(
                url,
                wait_until="domcontentloaded",
                referer=referer,
                timeout=self.timeout * 1000,
            )
            self._post_load(page)
            if self.wait_selector:
                page.wait_for_selector(self.wait_selector, timeout=self.wait_timeout * 1000)
            if self.page_action is not None:
                self.page_action(page)
            if self.network_idle:
                # networkidle 可能永远无法达到，超时后继续（best-effort）
                try:
                    page.wait_for_load_state("networkidle", timeout=self.wait_timeout * 1000)
                except PlaywrightTimeoutError:
                    pass
            content = page.content().encode("utf-8", errors="replace")
            status = resp.status if resp is not None else 200
            headers = dict(resp.headers) if resp is not None else {}
            return self._build_response(
                page.url,
                status,
                content,
                headers,
                request_headers=self.extra_headers,
            )
        finally:
            context.close()

    def fetch(self, url: str, **kwargs: Any) -> Any:
        """用 headless 浏览器渲染 ``url`` 并返回 :class:`Response`。"""
        self._validate_target(url)
        browser = self._ensure_browser()
        proxy = self._resolve_proxy()
        proxy_settings = self._parse_proxy(proxy)
        try:
            return self._render_page(browser, url, proxy_settings)
        except Exception as exc:
            raise RuntimeError(f"dynamic fetch of {url} failed: {exc}") from exc

    def get(self, url: str, **kwargs: Any) -> Any:
        """``fetch`` 的动词统一别名：与 :class:`~web_crawler.fetchers.Fetcher.get`
        对齐，使 Spider 等上层组件无需感知 fetcher 具体类型。

        仅支持 GET 语义；带 ``data`` 等参数时退回 :meth:`fetch` 的默认行为。
        """
        return self.fetch(url, **kwargs)

    # -- 异步浏览器生命周期 ---------------------------------------------------
    async def _ensure_async_browser(self) -> Any:
        from playwright.async_api import async_playwright

        if self._async_browser is None:
            self._async_pw = await async_playwright().start()
            self._async_browser = await self._async_pw.chromium.launch(headless=self.headless)
        return self._async_browser

    async def _setup_page_async(self, page: Any) -> None:
        """异步钩子：导航前配置页面（路由拦截）。"""
        blocked = self._blocked_types()
        if blocked:
            await page.route("**/*", self._make_route_handler(blocked))

    async def _post_load_async(self, page: Any) -> None:
        """异步钩子：导航后与页面交互（默认空实现）。"""

    async def _render_page_async(self, browser: Any, url: str, proxy_settings: Any) -> Any:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        context = await browser.new_context(
            **self._context_kwargs(viewport={"width": 1366, "height": 768}, proxy=proxy_settings)
        )
        try:
            page = await context.new_page()
            await self._setup_page_async(page)
            referer = "https://www.google.com/" if self.google_search else None
            resp = await page.goto(
                url,
                wait_until="domcontentloaded",
                referer=referer,
                timeout=self.timeout * 1000,
            )
            await self._post_load_async(page)
            if self.wait_selector:
                await page.wait_for_selector(self.wait_selector, timeout=self.wait_timeout * 1000)
            if self.page_action is not None:
                self.page_action(page)
            if self.network_idle:
                try:
                    await page.wait_for_load_state("networkidle", timeout=self.wait_timeout * 1000)
                except PlaywrightTimeoutError:
                    pass
            content = (await page.content()).encode("utf-8", errors="replace")
            status = resp.status if resp is not None else 200
            headers = dict(resp.headers) if resp is not None else {}
            return self._build_response(
                page.url,
                status,
                content,
                headers,
                request_headers=self.extra_headers,
            )
        finally:
            await context.close()

    async def async_fetch(self, url: str, **kwargs: Any) -> Any:
        """异步渲染 ``url`` 并返回 :class:`Response`。"""
        self._validate_target(url)
        browser = await self._ensure_async_browser()
        proxy = self._resolve_proxy()
        proxy_settings = self._parse_proxy(proxy)
        try:
            return await self._render_page_async(browser, url, proxy_settings)
        except Exception as exc:
            raise RuntimeError(f"dynamic async fetch of {url} failed: {exc}") from exc

    async def async_get(self, url: str, **kwargs: Any) -> Any:
        """``async_fetch`` 的动词统一别名（见 :meth:`get`）。"""
        return await self.async_fetch(url, **kwargs)

    # -- 截图分块（PixelRAG 风格） --------------------------------------------

    def screenshot_tiles(
        self,
        url: str,
        *,
        tile_height: int = 1024,
        viewport_width: int = 875,
        format: str = "png",
        quality: int = 80,
        max_tiles: int = 50,
    ) -> list[dict[str, Any]]:
        """渲染 ``url`` 并把整页切成固定高度的截图分块。

        PixelRAG 风格的视觉分块：不从 HTML 提取文本，而是把渲染后的页面
        截取为分块，可直接送入视觉语言模型做内容抽取或视觉嵌入。

        Parameters
        ----------
        url:
            要渲染并截图的页面 URL。
        tile_height:
            每个分块的高度（CSS 像素，默认 1024，与 PixelRAG 一致）。
        viewport_width:
            浏览器视口宽度（CSS 像素，默认 875，与 PixelRAG 一致）。
        format:
            截图图片格式：``"png"`` 或 ``"jpeg"``。
        quality:
            JPEG 质量（1-100），PNG 时忽略。
        max_tiles:
            单次调用最多生成的截图片数上限（防御超长页面/无限滚动导致的
            内存与耗时失控）；超过上限时截断并发出 RuntimeWarning。

        Returns
        -------
        list[dict]
            每个分块形如 ``{index, total, b64: str, width, height}``，
            其中 ``b64`` 是适合 VLM 输入的 base64 图片字符串。
        """
        self._validate_target(url)
        browser = self._ensure_browser()
        proxy = self._resolve_proxy()
        proxy_settings = self._parse_proxy(proxy)

        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        context = browser.new_context(
            **self._context_kwargs(
                viewport={"width": viewport_width, "height": 768}, proxy=proxy_settings
            )
        )
        try:
            page = context.new_page()
            self._setup_page(page)

            referer = "https://www.google.com/" if self.google_search else None
            page.goto(
                url, wait_until="domcontentloaded", referer=referer, timeout=self.timeout * 1000
            )
            self._post_load(page)

            if self.wait_selector:
                page.wait_for_selector(self.wait_selector, timeout=self.wait_timeout * 1000)
            if self.page_action is not None:
                self.page_action(page)
            if self.network_idle:
                try:
                    page.wait_for_load_state("networkidle", timeout=self.wait_timeout * 1000)
                except PlaywrightTimeoutError:
                    pass

            # 获取整页尺寸
            dims: dict[str, float] = page.evaluate("""() => ({
                width: Math.max(
                    document.documentElement.scrollWidth,
                    document.documentElement.clientWidth,
                    document.body?.scrollWidth || 0
                ),
                height: Math.max(
                    document.documentElement.scrollHeight,
                    document.documentElement.clientHeight,
                    document.body?.scrollHeight || 0
                ),
            })""")
            page_width = int(dims["width"])
            page_height = int(dims["height"])
            num_tiles = max(1, math.ceil(page_height / tile_height))
            if num_tiles > max_tiles:
                warnings.warn(
                    f"page height {page_height}px exceeds max_tiles={max_tiles}; "
                    "truncating screenshot tiles",
                    RuntimeWarning,
                    stacklevel=2,
                )
                num_tiles = max_tiles

            clip_format = "jpeg" if format == "jpeg" else "png"

            tiles: list[dict[str, Any]] = []
            for i in range(num_tiles):
                y_start = i * tile_height
                y_end = min(y_start + tile_height, page_height)
                clip_height = y_end - y_start
                if clip_height <= 0:
                    break

                screenshot_bytes = page.screenshot(
                    clip={"x": 0, "y": y_start, "width": page_width, "height": clip_height},
                    type=clip_format,
                    quality=quality if format == "jpeg" else None,
                    full_page=False,  # type: ignore[arg-type]
                )
                tiles.append(
                    {
                        "index": i,
                        "total": num_tiles,
                        "b64": base64.b64encode(screenshot_bytes).decode("ascii"),
                        "width": page_width,
                        "height": clip_height,
                    }
                )

            return tiles
        finally:
            context.close()

    async def async_screenshot_tiles(
        self,
        url: str,
        *,
        tile_height: int = 1024,
        viewport_width: int = 875,
        format: str = "png",
        quality: int = 80,
        max_tiles: int = 50,
    ) -> list[dict[str, Any]]:
        """异步版 :meth:`screenshot_tiles`。"""
        self._validate_target(url)
        browser = await self._ensure_async_browser()
        proxy = self._resolve_proxy()
        proxy_settings = self._parse_proxy(proxy)

        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        context = await browser.new_context(
            **self._context_kwargs(
                viewport={"width": viewport_width, "height": 768}, proxy=proxy_settings
            )
        )
        try:
            page = await context.new_page()
            await self._setup_page_async(page)

            referer = "https://www.google.com/" if self.google_search else None
            await page.goto(
                url, wait_until="domcontentloaded", referer=referer, timeout=self.timeout * 1000
            )
            await self._post_load_async(page)

            if self.wait_selector:
                await page.wait_for_selector(self.wait_selector, timeout=self.wait_timeout * 1000)
            if self.page_action is not None:
                self.page_action(page)
            if self.network_idle:
                try:
                    await page.wait_for_load_state("networkidle", timeout=self.wait_timeout * 1000)
                except PlaywrightTimeoutError:
                    pass

            dims: dict[str, float] = await page.evaluate("""() => ({
                width: Math.max(
                    document.documentElement.scrollWidth,
                    document.documentElement.clientWidth,
                    document.body?.scrollWidth || 0
                ),
                height: Math.max(
                    document.documentElement.scrollHeight,
                    document.documentElement.clientHeight,
                    document.body?.scrollHeight || 0
                ),
            })""")
            page_width = int(dims["width"])
            page_height = int(dims["height"])
            num_tiles = max(1, math.ceil(page_height / tile_height))
            if num_tiles > max_tiles:
                warnings.warn(
                    f"page height {page_height}px exceeds max_tiles={max_tiles}; "
                    "truncating screenshot tiles",
                    RuntimeWarning,
                    stacklevel=2,
                )
                num_tiles = max_tiles

            clip_format = "jpeg" if format == "jpeg" else "png"

            tiles: list[dict[str, Any]] = []
            for i in range(num_tiles):
                y_start = i * tile_height
                y_end = min(y_start + tile_height, page_height)
                clip_height = y_end - y_start
                if clip_height <= 0:
                    break

                screenshot_bytes = await page.screenshot(
                    clip={"x": 0, "y": y_start, "width": page_width, "height": clip_height},
                    type=clip_format,
                    quality=quality if format == "jpeg" else None,
                    full_page=False,  # type: ignore[arg-type]
                )
                tiles.append(
                    {
                        "index": i,
                        "total": num_tiles,
                        "b64": base64.b64encode(screenshot_bytes).decode("ascii"),
                        "width": page_width,
                        "height": clip_height,
                    }
                )

            return tiles
        finally:
            await context.close()

    # -- 生命周期 ------------------------------------------------------------
    def close(self) -> None:
        """关闭复用的同步浏览器并停止 Playwright driver（同步）。

        对 async 句柄做 best-effort 清理：启动临时事件循环执行 aclose。
        避免混用 async/sync 接口后 async browser 进程残留。
        """
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        # 异步句柄 best-effort 清理：仅当当前线程没有运行中的事件循环时才用
        # 临时事件循环尝试；失败或存在运行中 loop 时保留引用并告警，之后仍可
        # aclose()（跨事件循环关闭 Playwright 对象必然失败，不能静默丢弃引用）。
        if self._async_browser is not None or self._async_pw is not None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                try:
                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(self._cleanup_async_handles())
                    finally:
                        loop.close()
                except Exception:
                    pass
            if self._async_browser is not None or self._async_pw is not None:
                warnings.warn(
                    "DynamicFetcher.close() 未能关闭异步浏览器句柄（可能绑定在"
                    "其他事件循环）；请使用 await fetcher.aclose() 释放异步资源。",
                    ResourceWarning,
                    stacklevel=2,
                )

    async def _cleanup_async_handles(self) -> None:
        """关闭 async browser/pw 句柄（供 aclose() 调用）。

        单个句柄关闭失败时保留引用（不置 None），以便后续 aclose() 重试，
        避免"清理失败却丢失引用导致进程泄漏"。
        """
        if self._async_browser is not None:
            try:
                await self._async_browser.close()
                self._async_browser = None
            except Exception:
                pass
        if self._async_pw is not None:
            try:
                await self._async_pw.stop()
                self._async_pw = None
            except Exception:
                pass

    async def aclose(self) -> None:
        """异步关闭同步与异步浏览器 / Playwright driver。"""
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        await self._cleanup_async_handles()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


__all__ = ["DynamicFetcher"]

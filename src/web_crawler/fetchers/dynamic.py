"""JavaScript-rendering fetcher backed by Playwright.

Aligns with Scrapling's ``DynamicFetcher``: renders pages with a real headless
Chromium so JavaScript-heavy sites can be scraped. A single browser instance is
reused across fetches for performance, while a fresh browser context is created
per fetch to avoid cookie/state leakage between requests.

The class exposes small protected hook methods (``_setup_page``,
``_post_load``) so the stealthy subclass can inject stealth scripts and
humanized behavior without duplicating the rendering flow.

PixelRAG-inspired: ``screenshot_tiles()`` renders a page and slices it into
fixed-height screenshot tiles, ready for visual embedding or VLM extraction.
"""

from __future__ import annotations

import base64
import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

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
    """Playwright-backed fetcher that renders JavaScript before returning HTML.

    Parameters
    ----------
    headless:
        Run Chromium in headless mode.
    block_images:
        Abort image requests to speed up rendering.
    disable_resources:
        Abort image/media/font/stylesheet requests for maximum speed.
    wait_selector:
        Optional CSS selector to wait for after navigation.
    wait_timeout:
        Timeout (seconds) for ``wait_selector`` and ``networkidle`` waits.
    network_idle:
        Wait for the ``networkidle`` load state after navigation.
    page_action:
        Callback invoked with the Playwright ``Page`` after load, allowing
        custom interactions (scroll, click, …) before the HTML is captured.
    google_search:
        Set the referer to ``https://www.google.com/`` to disguise traffic source.
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

    # -- proxy / resource helpers -------------------------------------------
    def _parse_proxy(self, proxy: str | None) -> dict[str, str] | None:
        """Convert a proxy URL into Playwright's ``ProxySettings`` dict."""
        if not proxy:
            return None
        parsed = urlparse(proxy)
        server = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            server += f":{parsed.port}"
        settings: dict[str, str] = {"server": server}
        if parsed.username:
            settings["username"] = parsed.username
        if parsed.password:
            settings["password"] = parsed.password
        return settings

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

    # -- rendering hooks (overridden by StealthyFetcher) --------------------
    def _setup_page(self, page: Any) -> None:
        """Hook: configure the page before navigation (route blocking)."""
        blocked = self._blocked_types()
        if blocked:
            page.route("**/*", self._make_route_handler(blocked))

    def _post_load(self, page: Any) -> None:
        """Hook: interact with the page after navigation (no-op by default)."""

    # -- synchronous browser lifecycle --------------------------------------
    def _ensure_browser(self) -> Any:
        from playwright.sync_api import sync_playwright

        if self._browser is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self.headless)
        return self._browser

    def _render_page(self, browser: Any, url: str, proxy_settings: Any) -> Any:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        context = browser.new_context(
            user_agent=self.user_agent,
            locale="en-US",
            viewport={"width": 1366, "height": 768},
            extra_http_headers=self.extra_headers or None,
            proxy=proxy_settings,
            ignore_https_errors=not self.verify,
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
        """Render ``url`` with a headless browser and return a :class:`Response`."""
        browser = self._ensure_browser()
        proxy = self._resolve_proxy()
        proxy_settings = self._parse_proxy(proxy)
        try:
            return self._render_page(browser, url, proxy_settings)
        except Exception as exc:
            raise RuntimeError(f"dynamic fetch of {url} failed: {exc}") from exc

    # -- asynchronous browser lifecycle -------------------------------------
    async def _ensure_async_browser(self) -> Any:
        from playwright.async_api import async_playwright

        if self._async_browser is None:
            self._async_pw = await async_playwright().start()
            self._async_browser = await self._async_pw.chromium.launch(headless=self.headless)
        return self._async_browser

    async def _setup_page_async(self, page: Any) -> None:
        """Async hook: configure the page before navigation (route blocking)."""
        blocked = self._blocked_types()
        if blocked:
            await page.route("**/*", self._make_route_handler(blocked))

    async def _post_load_async(self, page: Any) -> None:
        """Async hook: interact with the page after navigation (no-op by default)."""

    async def _render_page_async(self, browser: Any, url: str, proxy_settings: Any) -> Any:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        context = await browser.new_context(
            user_agent=self.user_agent,
            locale="en-US",
            viewport={"width": 1366, "height": 768},
            extra_http_headers=self.extra_headers or None,
            proxy=proxy_settings,
            ignore_https_errors=not self.verify,
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
        """Asynchronously render ``url`` and return a :class:`Response`."""
        browser = await self._ensure_async_browser()
        proxy = self._resolve_proxy()
        proxy_settings = self._parse_proxy(proxy)
        try:
            return await self._render_page_async(browser, url, proxy_settings)
        except Exception as exc:
            raise RuntimeError(f"dynamic async fetch of {url} failed: {exc}") from exc

    # -- screenshot tiling (PixelRAG-style) ----------------------------------

    def screenshot_tiles(
        self,
        url: str,
        *,
        tile_height: int = 1024,
        viewport_width: int = 875,
        format: str = "png",
        quality: int = 80,
    ) -> list[dict[str, Any]]:
        """Render ``url`` and slice the full page into fixed-height screenshot tiles.

        PixelRAG-style visual chunking: instead of extracting text from HTML,
        capture the rendered page as screenshot tiles that can be fed directly
        to a vision-language model for content extraction or visual embedding.

        Parameters
        ----------
        url:
            The page URL to render and screenshot.
        tile_height:
            Height of each tile in CSS pixels (default 1024, matching PixelRAG).
        viewport_width:
            Browser viewport width in CSS pixels (default 875, matching PixelRAG).
        format:
            Screenshot image format: ``"png"`` or ``"jpeg"``.
        quality:
            JPEG quality (1-100), ignored for PNG.

        Returns
        -------
        list[dict]
            Each tile as ``{index, total, b64: str, width, height}`` where
            ``b64`` is a base64-encoded image string suitable for VLM input.
        """
        browser = self._ensure_browser()
        proxy = self._resolve_proxy()
        proxy_settings = self._parse_proxy(proxy)

        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        context = browser.new_context(
            user_agent=self.user_agent,
            locale="en-US",
            viewport={"width": viewport_width, "height": 768},
            extra_http_headers=self.extra_headers or None,
            proxy=proxy_settings,
            ignore_https_errors=not self.verify,
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

            # Get full page dimensions
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
    ) -> list[dict[str, Any]]:
        """Async version of :meth:`screenshot_tiles`."""
        browser = await self._ensure_async_browser()
        proxy = self._resolve_proxy()
        proxy_settings = self._parse_proxy(proxy)

        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        context = await browser.new_context(
            user_agent=self.user_agent,
            locale="en-US",
            viewport={"width": viewport_width, "height": 768},
            extra_http_headers=self.extra_headers or None,
            proxy=proxy_settings,
            ignore_https_errors=not self.verify,
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

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        """Close the reused sync browser and stop the Playwright driver (sync).

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
        # 异步句柄 best-effort 清理
        if self._async_browser is not None or self._async_pw is not None:
            import asyncio

            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(self._cleanup_async_handles())
                loop.close()
            except Exception:
                pass

    async def _cleanup_async_handles(self) -> None:
        """关闭 async browser/pw 句柄（供 close() 同步路径调用）。"""
        if self._async_browser is not None:
            try:
                await self._async_browser.close()
            except Exception:
                pass
            self._async_browser = None
        if self._async_pw is not None:
            try:
                await self._async_pw.stop()
            except Exception:
                pass
            self._async_pw = None

    async def aclose(self) -> None:
        """Asynchronously close both sync and async browsers / Playwright drivers."""
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

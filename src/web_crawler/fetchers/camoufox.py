"""Anti-fingerprint Firefox fetcher backed by Camoufox.

Borrows from the open-source `Camoufox <https://github.com/daijro/camoufox>`_
project (MIT) — the same anti-detect Firefox that Scrapling's ``StealthyFetcher``
builds on. Camoufox is a patched Firefox that spoofs its fingerprint at the C++
level (navigator, screen, WebGL, fonts, timezone/geolocation, …) and exposes a
Playwright-compatible ``Browser`` object, so it slots into the existing
:class:`~web_crawler.fetchers.dynamic.DynamicFetcher` rendering flow.

Only the browser *launch* and *context creation* differ from
:class:`DynamicFetcher`: Camoufox generates a coherent fingerprint at launch, so
the per-fetch context must **not** override user-agent / locale / viewport
(doing so would clash with the generated fingerprint and leak automation). All
waiting / ``page_action`` / ``network_idle`` behaviour is inherited unchanged.

This is a general anti-fingerprint rendering option; it does not hook, reverse
engineer, or forge any site-specific request signatures.

Example
-------
>>> from web_crawler import CamoufoxFetcher
>>> with CamoufoxFetcher(os="windows", humanize=True, geoip=True) as f:
...     resp = f.fetch("https://example.com")
...     print(resp.status)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..compat import require_camoufox
from .dynamic import DynamicFetcher
from .proxy import ProxyPool

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from ..parser.adaptive import AdaptiveStorage


class CamoufoxFetcher(DynamicFetcher):
    """A :class:`DynamicFetcher` that renders with the Camoufox anti-detect Firefox.

    Parameters (in addition to :class:`DynamicFetcher`'s)
    -----------------------------------------------------
    os:
        Fingerprint OS: ``"windows"``/``"macos"``/``"linux"`` or a list to pick
        from randomly. ``None`` lets Camoufox choose.
    humanize:
        Humanize cursor movement — ``True`` or the max duration in seconds.
    locale:
        Locale(s) for the Intl API, e.g. ``"en-US"`` or ``["en-US", "fr-FR"]``.
    geoip:
        ``True`` to auto-derive geolocation/timezone from the (proxy) IP, or a
        specific IP string. Recommended when using a proxy.
    block_webrtc:
        Block WebRTC entirely (prevents local-IP leaks).
    window:
        Fixed ``(width, height)``; leave ``None`` so Camoufox picks one (a fixed
        size is itself fingerprintable).
    camoufox_options:
        Escape hatch: extra keyword arguments passed straight to ``Camoufox(...)``
        (e.g. ``screen``, ``fonts``, ``fingerprint_preset``, ``disable_coop``).
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
        # -- Camoufox-specific ------------------------------------------------
        os: str | list[str] | None = None,
        humanize: bool | float = True,
        locale: str | list[str] | None = None,
        geoip: bool | str | None = None,
        block_webrtc: bool = False,
        window: tuple[int, int] | None = None,
        camoufox_options: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            headless=headless,
            proxy=proxy,
            timeout=timeout,
            adaptive=adaptive,
            storage=storage,
            extra_headers=extra_headers,
            block_images=block_images,
            disable_resources=disable_resources,
            wait_selector=wait_selector,
            wait_timeout=wait_timeout,
            network_idle=network_idle,
            page_action=page_action,
            google_search=google_search,
            verify=verify,
        )
        require_camoufox()
        self.os = os
        self.humanize = humanize
        self.locale = locale
        self.geoip = geoip
        self.block_webrtc = block_webrtc
        self.window = window
        self.camoufox_options = dict(camoufox_options) if camoufox_options else {}
        # Camoufox 上下文管理器句柄（跨 fetch 复用同一浏览器）
        self._camoufox_cm: Any = None
        self._async_camoufox_cm: Any = None

    # -- launch kwargs ------------------------------------------------------
    def _launch_kwargs(self) -> dict[str, Any]:
        """Assemble the ``Camoufox(...)`` launch options for this fetcher."""
        kwargs: dict[str, Any] = {"headless": self.headless, "humanize": self.humanize}
        if self.os is not None:
            kwargs["os"] = self.os
        if self.locale is not None:
            kwargs["locale"] = self.locale
        if self.geoip is not None:
            kwargs["geoip"] = self.geoip
        if self.block_webrtc:
            kwargs["block_webrtc"] = True
        if self.window is not None:
            kwargs["window"] = self.window
        proxy = self._resolve_proxy()
        proxy_settings = self._parse_proxy(proxy)
        if proxy_settings is not None:
            kwargs["proxy"] = proxy_settings
        # 用户显式传入的高级选项覆盖默认值
        kwargs.update(self.camoufox_options)
        return kwargs

    # -- context kwargs -----------------------------------------------------
    def _context_kwargs(
        self,
        *,
        viewport: dict[str, int] | None = None,
        proxy: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        # 关键区别：不传 user_agent/locale/viewport（也不传 proxy——代理在启动时
        # 交给 Camoufox），保留 Camoufox 生成的指纹，避免指纹冲突泄漏自动化痕迹。
        return {
            "extra_http_headers": self.extra_headers or None,
            "ignore_https_errors": not self.verify,
        }

    # -- sync browser lifecycle --------------------------------------------
    def _ensure_browser(self) -> Any:
        if self._browser is None:
            from camoufox.sync_api import Camoufox

            # Camoufox 自行管理内部 Playwright driver；用 __enter__ 保活浏览器
            self._camoufox_cm = Camoufox(**self._launch_kwargs())
            self._browser = self._camoufox_cm.__enter__()
        return self._browser

    def _render_page(self, browser: Any, url: str, proxy_settings: Any) -> Any:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        context = browser.new_context(**self._context_kwargs())
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
                try:
                    page.wait_for_load_state("networkidle", timeout=self.wait_timeout * 1000)
                except PlaywrightTimeoutError:
                    pass
            content = page.content().encode("utf-8", errors="replace")
            status = resp.status if resp is not None else 200
            headers = dict(resp.headers) if resp is not None else {}
            return self._build_response(
                page.url, status, content, headers, request_headers=self.extra_headers
            )
        finally:
            context.close()

    # -- async browser lifecycle -------------------------------------------
    async def _ensure_async_browser(self) -> Any:
        if self._async_browser is None:
            from camoufox.async_api import AsyncCamoufox

            self._async_camoufox_cm = AsyncCamoufox(**self._launch_kwargs())
            self._async_browser = await self._async_camoufox_cm.__aenter__()
        return self._async_browser

    async def _render_page_async(self, browser: Any, url: str, proxy_settings: Any) -> Any:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        context = await browser.new_context(**self._context_kwargs())
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
                page.url, status, content, headers, request_headers=self.extra_headers
            )
        finally:
            await context.close()

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        """Close the Camoufox browser and its managed Playwright driver (sync)."""
        if self._camoufox_cm is not None:
            try:
                self._camoufox_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._camoufox_cm = None
            self._browser = None

    async def aclose(self) -> None:
        """Close both sync and async Camoufox browsers."""
        if self._camoufox_cm is not None:
            try:
                self._camoufox_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._camoufox_cm = None
            self._browser = None
        if self._async_camoufox_cm is not None:
            try:
                await self._async_camoufox_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._async_camoufox_cm = None
            self._async_browser = None


__all__ = ["CamoufoxFetcher"]

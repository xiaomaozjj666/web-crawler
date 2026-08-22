"""基于 Camoufox 的反指纹 Firefox fetcher。

借鉴开源项目 `Camoufox <https://github.com/daijuro/camoufox>`_（MIT）——
与 Scrapling ``StealthyFetcher`` 所用相同的反检测 Firefox。Camoufox 是在
C++ 层伪装指纹（navigator、screen、WebGL、字体、时区/地理位置等）的魔改
Firefox，并暴露 Playwright 兼容的 ``Browser`` 对象，因此可以直接接入现有
:class:`~web_crawler.fetchers.dynamic.DynamicFetcher` 的渲染流程。

与 :class:`DynamicFetcher` 的区别仅在浏览器*启动*与*上下文创建*：Camoufox
在启动时生成一套自洽的指纹，因此每次抓取的 context **不得**覆盖
user-agent / locale / viewport（否则会与生成的指纹冲突，泄漏自动化痕迹）。
所有等待 / ``page_action`` / ``network_idle`` 行为原样继承。

这是一个通用的反指纹渲染选项；不 hook、不逆向、不伪造任何特定站点的
请求签名。

示例
----
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
    """用 Camoufox 反检测 Firefox 渲染的 :class:`DynamicFetcher`。

    Parameters（在 :class:`DynamicFetcher` 之外新增的）
    -----------------------------------------------------
    os:
        指纹操作系统：``"windows"``/``"macos"``/``"linux"``，或传入列表
        随机挑选。``None`` 时交给 Camoufox 决定。
    humanize:
        拟人化光标移动——``True`` 或最大时长（秒）。
    locale:
        Intl API 的 locale，如 ``"en-US"`` 或 ``["en-US", "fr-FR"]``。
    geoip:
        ``True`` 时根据（代理）IP 自动推导地理位置/时区，也可传具体 IP
        字符串。使用代理时建议开启。
    block_webrtc:
        彻底屏蔽 WebRTC（防止本机 IP 泄漏）。
    window:
        固定的 ``(宽, 高)``；保持 ``None`` 让 Camoufox 自选（固定尺寸本身
        就是指纹特征）。
    camoufox_options:
        逃生口：直接透传给 ``Camoufox(...)`` 的额外关键字参数
        （如 ``screen``、``fonts``、``fingerprint_preset``、``disable_coop``）。
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
        allow_private_hosts: bool | None = None,
        resolve_hosts: bool = False,
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
            allow_private_hosts=allow_private_hosts,
            resolve_hosts=resolve_hosts,
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

    # -- 启动参数 -----------------------------------------------------------
    def _launch_kwargs(self) -> dict[str, Any]:
        """组装本次抓取所需的 ``Camoufox(...)`` 启动选项。"""
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

    # -- context 参数 --------------------------------------------------------
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

    # -- 同步浏览器生命周期 ---------------------------------------------------
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

    # -- 异步浏览器生命周期 ---------------------------------------------------
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

    # -- 生命周期 ------------------------------------------------------------
    def close(self) -> None:
        """关闭 Camoufox 浏览器及其托管的 Playwright driver（同步）。"""
        if self._camoufox_cm is not None:
            try:
                self._camoufox_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._camoufox_cm = None
            self._browser = None

    async def aclose(self) -> None:
        """关闭同步与异步两个 Camoufox 浏览器。"""
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

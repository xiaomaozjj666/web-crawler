"""针对 bot 检测与 Cloudflare 质询加固的隐身 fetcher。

对齐 Scrapling 的 ``StealthyFetcher``：:class:`DynamicFetcher` 的子类，
注入隐身脚本掩盖自动化指纹，可选地拟人化输入（随机鼠标移动与延时），并
尽力解决 Cloudflare "Just a moment" 过渡页。

隐身逻辑通过覆写 :class:`DynamicFetcher` 定义的渲染钩子
（``_setup_page`` / ``_post_load`` 及其异步版本）实现，渲染流程本身复用
而非复制。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ._base import BaseFetcher  # noqa: F401  (re-exported via package for typing)
from .dynamic import DynamicFetcher
from .proxy import ProxyPool

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from ..parser.adaptive import AdaptiveStorage

# 隐身脚本：覆盖常见自动化检测指纹，在每次导航前注入执行。
# 覆盖 navigator.webdriver / plugins / languages / platform / vendor /
# appVersion / userAgent，并补全 window.chrome 与 permissions.query。
_STEALTH_JS = r"""
(() => {
    const ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        + '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
    try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
    try { Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]}); } catch (e) {}
    try { Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']}); } catch (e) {}
    try { Object.defineProperty(navigator, 'platform', {get: () => 'Win32'}); } catch (e) {}
    try { Object.defineProperty(navigator, 'vendor', {get: () => 'Google Inc.'}); } catch (e) {}
    try { Object.defineProperty(navigator, 'appVersion', {get: () => '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'}); } catch (e) {}
    try { Object.defineProperty(navigator, 'userAgent', {get: () => ua}); } catch (e) {}
    try { window.chrome = window.chrome || {runtime: {}}; } catch (e) {}
    try {
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications'
                ? Promise.resolve({state: Notification.permission})
                : originalQuery(parameters)
        );
    } catch (e) {}
})();
"""


class StealthyFetcher(DynamicFetcher):
    """用隐身、拟人化与 Cloudflare 处理加固的 :class:`DynamicFetcher`。

    默认值面向隐身调优：为提速屏蔽图片，并把 referer 伪装为 Google。
    设置 ``humanize=True`` 可加入随机鼠标移动与延时；设置
    ``solve_cloudflare=True`` 可尽力等待并点击 Cloudflare 质询过渡页。
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
        block_images: bool = True,
        wait_selector: str | None = None,
        wait_timeout: float = 15.0,
        network_idle: bool = True,
        page_action: Callable[[Page], None] | None = None,
        google_search: bool = True,
        humanize: bool = True,
        solve_cloudflare: bool = True,
        verify: bool = True,
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
            wait_selector=wait_selector,
            wait_timeout=wait_timeout,
            network_idle=network_idle,
            page_action=page_action,
            google_search=google_search,
            verify=verify,
            allow_private_hosts=allow_private_hosts,
            resolve_hosts=resolve_hosts,
        )
        self.humanize = humanize
        self.solve_cloudflare = solve_cloudflare

    # -- 同步钩子 -------------------------------------------------------------
    def _setup_page(self, page: Any) -> None:
        # 注入隐身脚本，必须在任何导航之前执行以覆盖指纹
        page.add_init_script(_STEALTH_JS)
        super()._setup_page(page)

    def _post_load(self, page: Any) -> None:
        if self.solve_cloudflare:
            self._solve_cloudflare_sync(page)
        if self.humanize:
            self._humanize_sync(page)

    def _humanize_sync(self, page: Any) -> None:
        # 模拟人类行为：鼠标移动到随机坐标 + 随机延时
        try:
            page.mouse.move(random.uniform(100, 800), random.uniform(100, 600))
            time.sleep(random.uniform(0.5, 2.0))
        except Exception:
            pass

    def _solve_cloudflare_sync(self, page: Any) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        try:
            title = page.title()
            is_challenge = "just a moment" in title.lower()
            if not is_challenge:
                is_challenge = (
                    page.query_selector(
                        "#challenge-running, #challenge-form, "
                        "iframe[src*='challenges.cloudflare.com']"
                    )
                    is not None
                )
            if not is_challenge:
                return
            # 等待 Cloudflare turnstile 复选框并尝试点击
            deadline_ms = self.wait_timeout * 1000
            try:
                page.wait_for_selector(
                    "iframe[src*='challenges.cloudflare.com']", timeout=deadline_ms
                )
            except PlaywrightTimeoutError:
                pass
            for frame in page.frames:
                try:
                    checkbox = frame.query_selector("input[type='checkbox']")
                    if checkbox is not None:
                        checkbox.click()
                        break
                except Exception:
                    continue
            try:
                page.wait_for_load_state("networkidle", timeout=deadline_ms)
            except PlaywrightTimeoutError:
                pass
        except Exception:
            pass

    # -- 异步钩子 -------------------------------------------------------------
    async def _setup_page_async(self, page: Any) -> None:
        await page.add_init_script(_STEALTH_JS)
        await super()._setup_page_async(page)

    async def _post_load_async(self, page: Any) -> None:
        if self.solve_cloudflare:
            await self._solve_cloudflare_async(page)
        if self.humanize:
            await self._humanize_async(page)

    async def _humanize_async(self, page: Any) -> None:
        try:
            await page.mouse.move(random.uniform(100, 800), random.uniform(100, 600))
            await asyncio.sleep(random.uniform(0.5, 2.0))
        except Exception:
            pass

    async def _solve_cloudflare_async(self, page: Any) -> None:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        try:
            title = await page.title()
            is_challenge = "just a moment" in title.lower()
            if not is_challenge:
                is_challenge = (
                    await page.query_selector(
                        "#challenge-running, #challenge-form, "
                        "iframe[src*='challenges.cloudflare.com']"
                    )
                    is not None
                )
            if not is_challenge:
                return
            deadline_ms = self.wait_timeout * 1000
            try:
                await page.wait_for_selector(
                    "iframe[src*='challenges.cloudflare.com']", timeout=deadline_ms
                )
            except PlaywrightTimeoutError:
                pass
            for frame in page.frames:
                try:
                    checkbox = await frame.query_selector("input[type='checkbox']")
                    if checkbox is not None:
                        await checkbox.click()
                        break
                except Exception:
                    continue
            try:
                await page.wait_for_load_state("networkidle", timeout=deadline_ms)
            except PlaywrightTimeoutError:
                pass
        except Exception:
            pass

    # -- 公开 API（委托给父类，父类会调用隐身钩子） ---------------------------
    def fetch(self, url: str, **kwargs: Any) -> Any:
        """以完整隐身模式渲染 ``url`` 并返回 :class:`Response`。"""
        return super().fetch(url, **kwargs)

    async def async_fetch(self, url: str, **kwargs: Any) -> Any:
        """异步以完整隐身模式渲染 ``url`` 并返回 :class:`Response`。"""
        return await super().async_fetch(url, **kwargs)


__all__ = ["StealthyFetcher"]

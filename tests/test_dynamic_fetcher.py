"""Tests for DynamicFetcher / StealthyFetcher construction and helpers.

These exercise the non-browser code paths (proxy parsing, resource blocking,
stealth JS injection constant, default values). Launching real browsers is too
slow/flaky for the unit test suite; the full render path is covered by the
smoke test in the fetchers package itself.
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from web_crawler import DynamicFetcher, StealthyFetcher, compat


# ---------------------------------------------------------------------------
# 共享 mock 工厂：构造伪造的 Playwright sync/async 对象图
# ---------------------------------------------------------------------------
def _make_sync_response(status: int = 200, headers: dict[str, str] | None = None) -> MagicMock:
    """构造伪造的 Playwright 同步 Response 对象。"""
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {"Content-Type": "text/html"}
    return resp


def _make_sync_page(
    *,
    url: str = "https://example.com/page",
    content: str = "<html><body>hi</body></html>",
    response: MagicMock | None = None,
    title: str = "Example",
) -> MagicMock:
    """构造伪造的 Playwright 同步 Page 对象。"""
    page = MagicMock()
    page.url = url
    page.content.return_value = content
    page.title.return_value = title
    page.goto.return_value = response if response is not None else _make_sync_response()
    page.frames = []
    page.mouse = MagicMock()
    # 默认 query_selector 返回 None（非挑战页）
    page.query_selector.return_value = None
    return page


def _make_sync_context(page: MagicMock | None = None) -> MagicMock:
    """构造伪造的 Playwright 同步 BrowserContext。"""
    ctx = MagicMock()
    ctx.new_page.return_value = page if page is not None else _make_sync_page()
    return ctx


def _make_sync_browser(context: MagicMock | None = None) -> MagicMock:
    """构造伪造的 Playwright 同步 Browser。"""
    browser = MagicMock()
    browser.new_context.return_value = context if context is not None else _make_sync_context()
    return browser


def _make_async_response(status: int = 200, headers: dict[str, str] | None = None) -> AsyncMock:
    resp = AsyncMock()
    resp.status = status
    resp.headers = headers or {"Content-Type": "text/html"}
    return resp


def _make_async_page(
    *,
    url: str = "https://example.com/page",
    content: str = "<html><body>hi</body></html>",
    response: AsyncMock | None = None,
    title: str = "Example",
) -> AsyncMock:
    """构造伪造的 Playwright 异步 Page 对象。所有方法返回 coroutine。"""
    page = AsyncMock()
    page.url = url
    page.content.return_value = content
    page.title.return_value = title
    page.goto.return_value = response if response is not None else _make_async_response()
    page.frames = []
    page.mouse = AsyncMock()
    page.query_selector.return_value = None
    return page


def _make_async_context(page: AsyncMock | None = None) -> AsyncMock:
    ctx = AsyncMock()
    ctx.new_page.return_value = page if page is not None else _make_async_page()
    return ctx


def _make_async_browser(context: AsyncMock | None = None) -> AsyncMock:
    browser = AsyncMock()
    browser.new_context.return_value = context if context is not None else _make_async_context()
    return browser


# ---------------------------------------------------------------------------
# 原有构造与纯函数测试
# ---------------------------------------------------------------------------
def test_dynamic_fetcher_constructs_when_playwright_available() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(headless=True, timeout=15.0, block_images=True)
    assert f.headless is True
    assert f.timeout == 15.0
    assert f.block_images is True
    f.close()


def test_parse_proxy_simple() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    settings = f._parse_proxy("http://1.2.3.4:8080")
    assert settings == {"server": "http://1.2.3.4:8080"}
    f.close()


def test_parse_proxy_with_auth() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    settings = f._parse_proxy("http://user:pass@1.2.3.4:8080")
    assert settings["server"] == "http://1.2.3.4:8080"
    assert settings["username"] == "user"
    assert settings["password"] == "pass"
    f.close()


def test_parse_proxy_none_returns_none() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    assert f._parse_proxy(None) is None
    assert f._parse_proxy("") is None
    f.close()


def test_blocked_types_empty_by_default() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(block_images=False, disable_resources=False)
    assert f._blocked_types() == set()
    f.close()


def test_blocked_types_images_only() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(block_images=True, disable_resources=False)
    assert f._blocked_types() == {"image"}
    f.close()


def test_blocked_types_disable_resources() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(block_images=True, disable_resources=True)
    blocked = f._blocked_types()
    assert "image" in blocked
    assert "media" in blocked
    assert "font" in blocked
    f.close()


def test_route_handler_aborts_blocked_and_continues_others() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(block_images=True)
    handler = f._make_route_handler({"image"})

    class FakeRoute:
        def __init__(self, rtype: str) -> None:
            self.request = type("Req", (), {"resource_type": rtype})()
            self.aborted = False
            self.continued = False

        def abort(self) -> None:
            self.aborted = True

        def continue_(self) -> None:
            self.continued = True

    blocked_route = FakeRoute("image")
    handler(blocked_route)
    assert blocked_route.aborted is True
    assert blocked_route.continued is False

    ok_route = FakeRoute("document")
    handler(ok_route)
    assert ok_route.aborted is False
    assert ok_route.continued is True
    f.close()


def test_stealthy_fetcher_inherits_dynamic() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    assert issubclass(StealthyFetcher, DynamicFetcher)


def test_stealthy_fetcher_defaults_are_more_aggressive() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = StealthyFetcher()
    # Stealthy defaults block images, fake google referer, humanize + cf solve.
    assert f.block_images is True
    assert f.google_search is True
    assert f.humanize is True
    assert f.solve_cloudflare is True
    assert f.wait_timeout == 15.0
    f.close()


def test_stealthy_js_constant_is_substantial() -> None:
    from web_crawler.fetchers.stealthy import _STEALTH_JS

    # The stealth script must patch the key browser-fingerprint surfaces.
    assert "webdriver" in _STEALTH_JS
    assert "plugins" in _STEALTH_JS
    assert "languages" in _STEALTH_JS
    assert "userAgent" in _STEALTH_JS
    assert len(_STEALTH_JS) > 200


def test_stealthy_fetcher_overrides_hooks() -> None:
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = StealthyFetcher()
    # StealthyFetcher overrides the rendering hooks to inject stealth JS.
    assert "stealthy" in type(f)._setup_page.__qualname__.lower() or (
        type(f)._setup_page is not DynamicFetcher._setup_page
    )
    f.close()


# ---------------------------------------------------------------------------
# _parse_proxy 边界分支（无端口 / 仅用户名）
# ---------------------------------------------------------------------------
def test_parse_proxy_no_port() -> None:
    """无端口的代理 URL：server 不带 :port 后缀。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    settings = f._parse_proxy("http://1.2.3.4")
    assert settings == {"server": "http://1.2.3.4"}
    f.close()


def test_parse_proxy_only_username() -> None:
    """仅有 username（无 password）的代理 URL。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    settings = f._parse_proxy("http://onlyuser@1.2.3.4:8080")
    assert settings["server"] == "http://1.2.3.4:8080"
    assert settings["username"] == "onlyuser"
    assert "password" not in settings
    f.close()


# ---------------------------------------------------------------------------
# _setup_page / _post_load 钩子
# ---------------------------------------------------------------------------
def test_setup_page_routes_when_blocked_types_exist() -> None:
    """有拦截类型时调用 page.route('**/*', handler)。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(block_images=True, disable_resources=True)
    page = MagicMock()
    f._setup_page(page)
    page.route.assert_called_once()
    args, _ = page.route.call_args
    assert args[0] == "**/*"
    f.close()


def test_setup_page_no_route_when_no_blocked_types() -> None:
    """无拦截类型时不调用 page.route。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(block_images=False, disable_resources=False)
    page = MagicMock()
    f._setup_page(page)
    page.route.assert_not_called()
    f.close()


def test_post_load_is_noop_by_default() -> None:
    """基类 _post_load 默认不做任何事，不应触碰 page。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    page = MagicMock()
    # 应无异常且无方法调用
    f._post_load(page)
    assert page.method_calls == []
    f.close()


def test_setup_page_async_routes_when_blocked() -> None:
    """异步 _setup_page_async 在有拦截类型时 await page.route。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher(block_images=True)
        page = AsyncMock()
        await f._setup_page_async(page)
        # handler 是闭包，无法重新创建比较，仅校验调用签名
        page.route.assert_awaited_once()
        args, _ = page.route.call_args
        assert args[0] == "**/*"
        assert callable(args[1])
        f.close()

    import asyncio

    asyncio.run(go())


def test_setup_page_async_no_route_when_empty() -> None:
    """异步 _setup_page_async 在无拦截类型时不调用 page.route。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher()
        page = AsyncMock()
        await f._setup_page_async(page)
        page.route.assert_not_called()
        f.close()

    import asyncio

    asyncio.run(go())


def test_post_load_async_is_noop_by_default() -> None:
    """基类 _post_load_async 默认不做任何事。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher()
        page = AsyncMock()
        # 调用应无异常且无副作用（基类 _post_load_async 是空实现）
        await f._post_load_async(page)
        f.close()

    import asyncio

    asyncio.run(go())


# ---------------------------------------------------------------------------
# _ensure_browser / _render_page / fetch 同步路径
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not compat.HAS_PLAYWRIGHT, reason="playwright not installed")
@patch("playwright.sync_api.sync_playwright")
def test_ensure_browser_starts_playwright_and_launches_chromium(mock_sync_pw: MagicMock) -> None:
    """首次调用时启动 Playwright driver 并启动 chromium，二次调用复用。"""
    f = DynamicFetcher(headless=False)
    mock_driver = MagicMock()
    mock_browser = MagicMock()
    mock_driver.chromium.launch.return_value = mock_browser
    mock_sync_pw.return_value.start.return_value = mock_driver

    b1 = f._ensure_browser()
    assert b1 is mock_browser
    mock_driver.chromium.launch.assert_called_once_with(headless=False)
    # 二次调用复用，不重新启动
    b2 = f._ensure_browser()
    assert b2 is mock_browser
    assert mock_sync_pw.return_value.start.call_count == 1
    f.close()


def test_render_page_full_sync_flow() -> None:
    """_render_page 完整同步渲染流程：导航 → 等待 → 截取 HTML → 构建 Response。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(
        google_search=True,
        wait_selector="#main",
        network_idle=True,
        extra_headers={"X-Test": "1"},
    )
    resp_mock = _make_sync_response(status=200, headers={"X-Resp": "yes"})
    page = _make_sync_page(content="<html>rendered</html>", response=resp_mock)
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    out = f._render_page(browser, "https://example.com/x", None)
    # new_context 收到 UA / locale / viewport / extra_headers / proxy / ignore_https_errors
    browser.new_context.assert_called_once()
    call_kwargs = browser.new_context.call_args.kwargs
    assert call_kwargs["user_agent"] == f.user_agent
    assert call_kwargs["locale"] == "en-US"
    assert call_kwargs["viewport"] == {"width": 1366, "height": 768}
    assert call_kwargs["extra_http_headers"] == {"X-Test": "1"}
    assert call_kwargs["proxy"] is None
    assert call_kwargs["ignore_https_errors"] is False
    # goto 使用 google referer
    page.goto.assert_called_once()
    goto_kwargs = page.goto.call_args.kwargs
    assert goto_kwargs["wait_until"] == "domcontentloaded"
    assert goto_kwargs["referer"] == "https://www.google.com/"
    # wait_selector 触发 wait_for_selector
    page.wait_for_selector.assert_called_once_with("#main", timeout=f.wait_timeout * 1000)
    # network_idle 触发 wait_for_load_state
    page.wait_for_load_state.assert_called_once_with("networkidle", timeout=f.wait_timeout * 1000)
    # context.close 在 finally 中调用
    ctx.close.assert_called_once()
    # 返回 Response 对象，content 是 bytes
    assert out.status == 200
    assert out.content == b"<html>rendered</html>"
    assert out.headers == {"X-Resp": "yes"}
    f.close()


def test_render_page_no_wait_selector_no_network_idle() -> None:
    """wait_selector=None + network_idle=False 时不调用对应等待。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(wait_selector=None, network_idle=False)
    page = _make_sync_page()
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    f._render_page(browser, "https://example.com/x", None)
    page.wait_for_selector.assert_not_called()
    page.wait_for_load_state.assert_not_called()
    f.close()


def test_render_page_invokes_page_action() -> None:
    """page_action 回调被调用并传入 page。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    called_with: list[Any] = []

    def action(page: Any) -> None:
        called_with.append(page)

    f = DynamicFetcher(page_action=action, network_idle=False)
    page = _make_sync_page()
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    f._render_page(browser, "https://example.com/x", None)
    assert called_with == [page]
    f.close()


def test_render_page_handles_none_response() -> None:
    """page.goto 返回 None 时使用默认 status=200 / headers={}。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(network_idle=False)
    page = _make_sync_page()
    page.goto.return_value = None  # 响应为 None
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    out = f._render_page(browser, "https://example.com/x", None)
    assert out.status == 200
    assert out.headers == {}
    f.close()


def test_render_page_networkidle_timeout_swallowed() -> None:
    """networkidle 超时（PlaywrightTimeoutError）被吞掉，不传播。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    from playwright.sync_api import TimeoutError as PWTimeoutError

    f = DynamicFetcher(network_idle=True)
    page = _make_sync_page()
    page.wait_for_load_state.side_effect = PWTimeoutError("networkidle timed out")
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    # 不应抛出
    out = f._render_page(browser, "https://example.com/x", None)
    assert out.status == 200
    f.close()


def test_render_page_context_closed_on_exception() -> None:
    """渲染过程中抛异常时，context.close 仍在 finally 中被调用。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(network_idle=False)
    page = _make_sync_page()
    page.goto.side_effect = RuntimeError("navigation failed")
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    with pytest.raises(RuntimeError, match="navigation failed"):
        f._render_page(browser, "https://example.com/x", None)
    ctx.close.assert_called_once()
    f.close()


def test_fetch_wraps_render_failure_in_runtime_error() -> None:
    """fetch 把 _render_page 抛出的异常包装为 RuntimeError。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    f._browser = MagicMock()
    f._browser.new_context.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="dynamic fetch of https://x/ failed"):
        f.fetch("https://x/")
    f.close()


def test_fetch_success_returns_response() -> None:
    """fetch 成功路径：返回 _render_page 构建的 Response。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(network_idle=False)
    page = _make_sync_page()
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    f._browser = browser  # 跳过 _ensure_browser 的真实启动

    out = f.fetch("https://example.com/x")
    assert out.status == 200
    assert b"hi" in out.content
    f.close()


def test_fetch_passes_proxy_settings_to_render() -> None:
    """fetch 把 _resolve_proxy + _parse_proxy 的结果传给 _render_page。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(proxy="http://1.2.3.4:8080", network_idle=False)
    page = _make_sync_page()
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    f._browser = browser

    f.fetch("https://example.com/x")
    # new_context 的 proxy 参数应是解析后的 settings dict
    assert browser.new_context.call_args.kwargs["proxy"] == {"server": "http://1.2.3.4:8080"}
    f.close()


# ---------------------------------------------------------------------------
# _ensure_async_browser / _render_page_async / async_fetch 异步路径
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not compat.HAS_PLAYWRIGHT, reason="playwright not installed")
@patch("playwright.async_api.async_playwright")
def test_ensure_async_browser_starts_driver_and_launches(mock_async_pw: Any) -> None:
    """首次异步调用启动 async_playwright driver + chromium；二次复用。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher(headless=True)
        mock_driver = AsyncMock()
        mock_browser = AsyncMock()
        # async_playwright().start() 必须 await，故 start 是 AsyncMock
        mock_async_pw.return_value.start = AsyncMock(return_value=mock_driver)
        mock_driver.chromium.launch = AsyncMock(return_value=mock_browser)

        b1 = await f._ensure_async_browser()
        assert b1 is mock_browser
        mock_driver.chromium.launch.assert_awaited_once_with(headless=True)
        b2 = await f._ensure_async_browser()
        assert b2 is mock_browser
        assert mock_async_pw.return_value.start.await_count == 1
        f.close()

    import asyncio

    asyncio.run(go())


def test_render_page_async_full_flow() -> None:
    """异步渲染流程：导航 → 等待 → 截取 HTML → 构建 Response。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher(
            google_search=True,
            wait_selector="#main",
            network_idle=True,
            extra_headers={"X-Test": "1"},
        )
        resp_mock = _make_async_response(status=200, headers={"X-Resp": "yes"})
        page = _make_async_page(content="<html>async rendered</html>", response=resp_mock)
        ctx = _make_async_context(page)
        browser = _make_async_browser(ctx)

        out = await f._render_page_async(browser, "https://example.com/x", None)
        browser.new_context.assert_awaited_once()
        call_kwargs = browser.new_context.call_args.kwargs
        assert call_kwargs["user_agent"] == f.user_agent
        assert call_kwargs["proxy"] is None
        page.goto.assert_awaited_once()
        assert page.goto.call_args.kwargs["referer"] == "https://www.google.com/"
        page.wait_for_selector.assert_awaited_once_with("#main", timeout=f.wait_timeout * 1000)
        page.wait_for_load_state.assert_awaited_once_with(
            "networkidle", timeout=f.wait_timeout * 1000
        )
        ctx.close.assert_awaited_once()
        assert out.status == 200
        assert out.content == b"<html>async rendered</html>"
        f.close()

    import asyncio

    asyncio.run(go())


def test_render_page_async_no_wait_no_idle() -> None:
    """异步路径 wait_selector=None + network_idle=False 不调用等待。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher(wait_selector=None, network_idle=False)
        page = _make_async_page()
        ctx = _make_async_context(page)
        browser = _make_async_browser(ctx)
        await f._render_page_async(browser, "https://example.com/x", None)
        page.wait_for_selector.assert_not_called()
        page.wait_for_load_state.assert_not_called()
        f.close()

    import asyncio

    asyncio.run(go())


def test_render_page_async_handles_none_response() -> None:
    """异步路径 page.goto 返回 None 时使用默认 status=200 / headers={}。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher(network_idle=False)
        page = _make_async_page()
        page.goto.return_value = None
        ctx = _make_async_context(page)
        browser = _make_async_browser(ctx)
        out = await f._render_page_async(browser, "https://example.com/x", None)
        assert out.status == 200
        assert out.headers == {}
        f.close()

    import asyncio

    asyncio.run(go())


def test_render_page_async_networkidle_timeout_swallowed() -> None:
    """异步路径 networkidle 超时被吞掉。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    from playwright.async_api import TimeoutError as PWAsyncTimeoutError

    async def go() -> None:
        f = DynamicFetcher(network_idle=True)
        page = _make_async_page()
        page.wait_for_load_state.side_effect = PWAsyncTimeoutError("timeout")
        ctx = _make_async_context(page)
        browser = _make_async_browser(ctx)
        out = await f._render_page_async(browser, "https://example.com/x", None)
        assert out.status == 200
        f.close()

    import asyncio

    asyncio.run(go())


def test_render_page_async_context_closed_on_exception() -> None:
    """异步路径异常时 context.close 仍在 finally 中被 await。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher(network_idle=False)
        page = _make_async_page()
        page.goto.side_effect = RuntimeError("nav failed")
        ctx = _make_async_context(page)
        browser = _make_async_browser(ctx)
        with pytest.raises(RuntimeError, match="nav failed"):
            await f._render_page_async(browser, "https://example.com/x", None)
        ctx.close.assert_awaited_once()
        f.close()

    import asyncio

    asyncio.run(go())


def test_async_fetch_wraps_failure_in_runtime_error() -> None:
    """async_fetch 把异常包装为 RuntimeError。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher()
        f._async_browser = AsyncMock()
        f._async_browser.new_context.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError, match="dynamic async fetch of https://x/ failed"):
            await f.async_fetch("https://x/")
        f.close()

    import asyncio

    asyncio.run(go())


def test_async_fetch_success() -> None:
    """async_fetch 成功路径返回 Response。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher(network_idle=False)
        page = _make_async_page()
        ctx = _make_async_context(page)
        browser = _make_async_browser(ctx)
        f._async_browser = browser  # 跳过真实启动
        out = await f.async_fetch("https://example.com/x")
        assert out.status == 200
        assert b"hi" in out.content
        f.close()

    import asyncio

    asyncio.run(go())


def test_render_page_async_invokes_page_action() -> None:
    """异步路径 page_action 回调被调用。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    called_with: list[Any] = []

    def action(page: Any) -> None:
        called_with.append(page)

    async def go() -> None:
        f = DynamicFetcher(page_action=action, network_idle=False)
        page = _make_async_page()
        ctx = _make_async_context(page)
        browser = _make_async_browser(ctx)
        await f._render_page_async(browser, "https://example.com/x", None)
        assert called_with == [page]
        f.close()

    import asyncio

    asyncio.run(go())


# ---------------------------------------------------------------------------
# screenshot_tiles / async_screenshot_tiles
# ---------------------------------------------------------------------------
def test_screenshot_tiles_slices_full_page() -> None:
    """screenshot_tiles 按给定 tile_height 切片，每片含 base64 编码图片。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(network_idle=False)
    page = _make_sync_page()
    # 总高 2500px，tile_height 1024 → 3 片（1024 / 1024 / 452）
    page.evaluate.return_value = {"width": 875.0, "height": 2500.0}
    page.screenshot.side_effect = [
        b"PNG-TILE-0",
        b"PNG-TILE-1",
        b"PNG-TILE-2",
    ]
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    f._browser = browser

    tiles = f.screenshot_tiles("https://example.com/long", tile_height=1024)
    assert len(tiles) == 3
    assert tiles[0]["index"] == 0
    assert tiles[0]["total"] == 3
    assert tiles[0]["width"] == 875
    assert tiles[0]["height"] == 1024
    assert tiles[0]["b64"] == base64.b64encode(b"PNG-TILE-0").decode("ascii")
    assert tiles[2]["height"] == 452  # 2500 - 2*1024
    # screenshot 调用参数（clip / type / quality / full_page）
    first_call_kwargs = page.screenshot.call_args_list[0].kwargs
    assert first_call_kwargs["clip"] == {"x": 0, "y": 0, "width": 875, "height": 1024}
    assert first_call_kwargs["type"] == "png"
    assert first_call_kwargs["full_page"] is False
    f.close()


def test_screenshot_tiles_jpeg_format_passes_quality() -> None:
    """format='jpeg' 时使用 jpeg type 并传入 quality。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(network_idle=False)
    page = _make_sync_page()
    page.evaluate.return_value = {"width": 875.0, "height": 100.0}
    page.screenshot.return_value = b"JPEG"
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    f._browser = browser

    tiles = f.screenshot_tiles("https://example.com/", tile_height=1024, format="jpeg", quality=70)
    assert len(tiles) == 1
    kwargs = page.screenshot.call_args.kwargs
    assert kwargs["type"] == "jpeg"
    assert kwargs["quality"] == 70
    f.close()


def test_screenshot_tiles_single_tile_when_short_page() -> None:
    """页面高度 < tile_height 时仅 1 片。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(network_idle=False)
    page = _make_sync_page()
    page.evaluate.return_value = {"width": 875.0, "height": 100.0}
    page.screenshot.return_value = b"PNG"
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    f._browser = browser

    tiles = f.screenshot_tiles("https://example.com/")
    assert len(tiles) == 1
    assert tiles[0]["height"] == 100
    f.close()


def test_screenshot_tiles_uses_proxy_when_configured() -> None:
    """screenshot_tiles 在有代理时把 proxy_settings 传给 new_context。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(proxy="http://1.2.3.4:8080", network_idle=False)
    page = _make_sync_page()
    page.evaluate.return_value = {"width": 875.0, "height": 100.0}
    page.screenshot.return_value = b"PNG"
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    f._browser = browser

    f.screenshot_tiles("https://example.com/")
    assert browser.new_context.call_args.kwargs["proxy"] == {"server": "http://1.2.3.4:8080"}
    f.close()


def test_async_screenshot_tiles_slices_full_page() -> None:
    """异步切片：与同步逻辑等价。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher(network_idle=False)
        page = _make_async_page()
        page.evaluate.return_value = {"width": 875.0, "height": 2048.0}
        page.screenshot.side_effect = [b"A", b"B"]
        ctx = _make_async_context(page)
        browser = _make_async_browser(ctx)
        f._async_browser = browser

        tiles = await f.async_screenshot_tiles("https://example.com/", tile_height=1024)
        assert len(tiles) == 2
        assert tiles[0]["b64"] == base64.b64encode(b"A").decode("ascii")
        assert tiles[1]["b64"] == base64.b64encode(b"B").decode("ascii")
        f.close()

    import asyncio

    asyncio.run(go())


def test_async_screenshot_tiles_jpeg() -> None:
    """异步切片支持 jpeg 格式。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher(network_idle=False)
        page = _make_async_page()
        page.evaluate.return_value = {"width": 875.0, "height": 100.0}
        page.screenshot.return_value = b"JPEG"
        ctx = _make_async_context(page)
        browser = _make_async_browser(ctx)
        f._async_browser = browser

        tiles = await f.async_screenshot_tiles("https://example.com/", format="jpeg", quality=50)
        assert len(tiles) == 1
        kwargs = page.screenshot.call_args.kwargs
        assert kwargs["type"] == "jpeg"
        assert kwargs["quality"] == 50
        f.close()

    import asyncio

    asyncio.run(go())


def test_screenshot_tiles_with_wait_selector_and_page_action() -> None:
    """screenshot_tiles 支持 wait_selector + page_action + network_idle。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    called_with: list[Any] = []

    def action(page: Any) -> None:
        called_with.append(page)

    f = DynamicFetcher(
        wait_selector="#main",
        page_action=action,
        network_idle=True,
    )
    page = _make_sync_page()
    page.evaluate.return_value = {"width": 875.0, "height": 100.0}
    page.screenshot.return_value = b"PNG"
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    f._browser = browser

    f.screenshot_tiles("https://example.com/")
    page.wait_for_selector.assert_called_once_with("#main", timeout=f.wait_timeout * 1000)
    page.wait_for_load_state.assert_called_once_with("networkidle", timeout=f.wait_timeout * 1000)
    assert called_with == [page]
    f.close()


def test_screenshot_tiles_networkidle_timeout_swallowed() -> None:
    """screenshot_tiles 中 networkidle 超时被吞掉。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    from playwright.sync_api import TimeoutError as PWTimeoutError

    f = DynamicFetcher(network_idle=True)
    page = _make_sync_page()
    page.evaluate.return_value = {"width": 875.0, "height": 100.0}
    page.screenshot.return_value = b"PNG"
    page.wait_for_load_state.side_effect = PWTimeoutError("timeout")
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    f._browser = browser

    tiles = f.screenshot_tiles("https://example.com/")
    assert len(tiles) == 1
    f.close()


def test_screenshot_tiles_zero_height_page_returns_no_tiles() -> None:
    """page_height=0 时 clip_height<=0 → break，返回空列表。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(network_idle=False)
    page = _make_sync_page()
    page.evaluate.return_value = {"width": 875.0, "height": 0.0}
    page.screenshot.return_value = b"PNG"
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    f._browser = browser

    tiles = f.screenshot_tiles("https://example.com/")
    # height=0 → num_tiles=max(1, ceil(0/1024))=1, 但 clip_height=0 → break
    assert tiles == []
    f.close()


def test_async_screenshot_tiles_with_wait_selector_and_page_action() -> None:
    """异步切片支持 wait_selector + page_action + network_idle。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    called_with: list[Any] = []

    def action(page: Any) -> None:
        called_with.append(page)

    async def go() -> None:
        f = DynamicFetcher(
            wait_selector="#main",
            page_action=action,
            network_idle=True,
        )
        page = _make_async_page()
        page.evaluate.return_value = {"width": 875.0, "height": 100.0}
        page.screenshot.return_value = b"PNG"
        ctx = _make_async_context(page)
        browser = _make_async_browser(ctx)
        f._async_browser = browser

        tiles = await f.async_screenshot_tiles("https://example.com/")
        assert len(tiles) == 1
        page.wait_for_selector.assert_awaited_once_with("#main", timeout=f.wait_timeout * 1000)
        page.wait_for_load_state.assert_awaited_once_with(
            "networkidle", timeout=f.wait_timeout * 1000
        )
        assert called_with == [page]
        f.close()

    import asyncio

    asyncio.run(go())


def test_async_screenshot_tiles_networkidle_timeout_swallowed() -> None:
    """异步切片 networkidle 超时被吞掉。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    from playwright.async_api import TimeoutError as PWAsyncTimeoutError

    async def go() -> None:
        f = DynamicFetcher(network_idle=True)
        page = _make_async_page()
        page.evaluate.return_value = {"width": 875.0, "height": 100.0}
        page.screenshot.return_value = b"PNG"
        page.wait_for_load_state.side_effect = PWAsyncTimeoutError("timeout")
        ctx = _make_async_context(page)
        browser = _make_async_browser(ctx)
        f._async_browser = browser

        tiles = await f.async_screenshot_tiles("https://example.com/")
        assert len(tiles) == 1
        f.close()

    import asyncio

    asyncio.run(go())


def test_async_screenshot_tiles_zero_height_page_returns_no_tiles() -> None:
    """异步切片 page_height=0 时返回空列表。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher(network_idle=False)
        page = _make_async_page()
        page.evaluate.return_value = {"width": 875.0, "height": 0.0}
        page.screenshot.return_value = b"PNG"
        ctx = _make_async_context(page)
        browser = _make_async_browser(ctx)
        f._async_browser = browser

        tiles = await f.async_screenshot_tiles("https://example.com/")
        assert tiles == []
        f.close()

    import asyncio

    asyncio.run(go())


# ---------------------------------------------------------------------------
# close / aclose / _cleanup_async_handles / context managers
# ---------------------------------------------------------------------------
def test_close_cleans_sync_browser_and_pw() -> None:
    """close() 关闭并清理 sync browser / pw 句柄。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    browser_mock = MagicMock()
    pw_mock = MagicMock()
    f._browser = browser_mock
    f._pw = pw_mock
    f.close()
    browser_mock.close.assert_called_once()
    pw_mock.stop.assert_called_once()
    assert f._browser is None
    assert f._pw is None
    # 二次 close 幂等
    f.close()


def test_close_swallows_sync_cleanup_exceptions() -> None:
    """close() 对 sync 句柄清理异常 best-effort 吞掉。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    browser_mock = MagicMock()
    browser_mock.close.side_effect = RuntimeError("close failed")
    pw_mock = MagicMock()
    pw_mock.stop.side_effect = RuntimeError("stop failed")
    f._browser = browser_mock
    f._pw = pw_mock
    # 不应抛
    f.close()
    assert f._browser is None
    assert f._pw is None


def test_close_cleans_async_handles_via_temp_loop() -> None:
    """close() 检测到 async 句柄时启动临时事件循环清理。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    async_browser_mock = AsyncMock()
    async_pw_mock = AsyncMock()
    f._async_browser = async_browser_mock
    f._async_pw = async_pw_mock
    # 用同步 close() 路径触发临时事件循环清理 async 句柄
    f.close()
    assert f._async_browser is None
    assert f._async_pw is None


def test_close_swallows_async_cleanup_failure() -> None:
    """close() 临时事件循环清理失败时保留引用并发出 ResourceWarning。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    async_browser_mock = AsyncMock()
    async_browser_mock.close.side_effect = RuntimeError("async close failed")
    f._async_browser = async_browser_mock
    with pytest.warns(ResourceWarning, match="异步浏览器句柄"):
        f.close()
    # 清理失败时引用被保留，之后仍可 aclose() 重试
    assert f._async_browser is async_browser_mock


def test_cleanup_async_handles_idempotent() -> None:
    """_cleanup_async_handles 二次调用无副作用。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher()
        await f._cleanup_async_handles()  # 全 None，应无异常
        f.close()

    import asyncio

    asyncio.run(go())


def test_cleanup_async_handles_closes_both() -> None:
    """_cleanup_async_handles 同时关闭 async_browser 和 async_pw。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher()
        async_browser_mock = AsyncMock()
        async_pw_mock = AsyncMock()
        f._async_browser = async_browser_mock
        f._async_pw = async_pw_mock
        await f._cleanup_async_handles()
        async_browser_mock.close.assert_awaited_once()
        async_pw_mock.stop.assert_awaited_once()
        assert f._async_browser is None
        assert f._async_pw is None
        f.close()

    import asyncio

    asyncio.run(go())


def test_cleanup_async_handles_swallows_exceptions() -> None:
    """_cleanup_async_handles 对异常 best-effort 吞掉，并保留引用以便重试。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher()
        async_browser_mock = AsyncMock()
        async_browser_mock.close.side_effect = RuntimeError("x")
        async_pw_mock = AsyncMock()
        async_pw_mock.stop.side_effect = RuntimeError("y")
        f._async_browser = async_browser_mock
        f._async_pw = async_pw_mock
        await f._cleanup_async_handles()
        # 关闭失败时引用保留（不置 None），避免"失败却丢失引用导致进程泄漏"
        assert f._async_browser is async_browser_mock
        assert f._async_pw is async_pw_mock
        f.close()

    import asyncio

    asyncio.run(go())


def test_aclose_closes_sync_and_async() -> None:
    """aclose() 同时清理 sync + async 句柄。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher()
        browser_mock = MagicMock()
        pw_mock = MagicMock()
        async_browser_mock = AsyncMock()
        async_pw_mock = AsyncMock()
        f._browser = browser_mock
        f._pw = pw_mock
        f._async_browser = async_browser_mock
        f._async_pw = async_pw_mock
        await f.aclose()
        browser_mock.close.assert_called_once()
        pw_mock.stop.assert_called_once()
        async_browser_mock.close.assert_awaited_once()
        async_pw_mock.stop.assert_awaited_once()
        assert f._browser is None
        assert f._pw is None
        assert f._async_browser is None
        assert f._async_pw is None

    import asyncio

    asyncio.run(go())


def test_aclose_idempotent() -> None:
    """aclose 二次调用无副作用。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher()
        await f.aclose()
        await f.aclose()

    import asyncio

    asyncio.run(go())


def test_aclose_swallows_sync_cleanup_exceptions() -> None:
    """aclose() 对 sync browser/pw close 异常 best-effort 吞掉。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher()
        browser_mock = MagicMock()
        browser_mock.close.side_effect = RuntimeError("sync close failed")
        pw_mock = MagicMock()
        pw_mock.stop.side_effect = RuntimeError("sync stop failed")
        f._browser = browser_mock
        f._pw = pw_mock
        # 不应抛
        await f.aclose()
        assert f._browser is None
        assert f._pw is None

    import asyncio

    asyncio.run(go())


def test_sync_context_manager_calls_close() -> None:
    """__enter__ 返回 self，__exit__ 触发 close()。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    with patch.object(f, "close") as mock_close:
        entered = f.__enter__()
        assert entered is f
        f.__exit__(None, None, None)
        mock_close.assert_called_once()


def test_async_context_manager_calls_aclose() -> None:
    """__aenter__ 返回 self，__aexit__ 触发 aclose()。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher()
        with patch.object(f, "aclose", new=AsyncMock()) as mock_aclose:
            entered = await f.__aenter__()
            assert entered is f
            await f.__aexit__(None, None, None)
            mock_aclose.assert_awaited_once()
        f.close()

    import asyncio

    asyncio.run(go())


# ---------------------------------------------------------------------------
# require_playwright 在 __init__ 时调用
# ---------------------------------------------------------------------------
def test_init_calls_require_playwright() -> None:
    """__init__ 调用 require_playwright；若抛错则构造失败。"""
    with patch("web_crawler.fetchers.dynamic.require_playwright") as mock_req:
        f = DynamicFetcher()
        mock_req.assert_called_once()
        f.close()


def test_init_raises_when_playwright_missing() -> None:
    """require_playwright 抛 ImportError 时构造失败。"""
    with (
        patch(
            "web_crawler.fetchers.dynamic.require_playwright",
            side_effect=ImportError("playwright is required"),
        ),
        pytest.raises(ImportError, match="playwright is required"),
    ):
        DynamicFetcher()


# ---------------------------------------------------------------------------
# 默认 UA / extra_headers / verify 配置
# ---------------------------------------------------------------------------
def test_default_user_agent_is_chrome_131() -> None:
    """默认 UA 包含 Chrome 131 标识。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    assert "Chrome/131" in f.user_agent
    f.close()


def test_verify_false_propagates_ignore_https_errors() -> None:
    """verify=False 时 new_context 的 ignore_https_errors=True。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(verify=False, network_idle=False)
    page = _make_sync_page()
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    f._browser = browser
    f.fetch("https://example.com/x")
    assert browser.new_context.call_args.kwargs["ignore_https_errors"] is True
    f.close()


def test_extra_headers_none_passes_none_to_context() -> None:
    """extra_headers 为空时 new_context 的 extra_http_headers=None。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(network_idle=False)
    page = _make_sync_page()
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    f._browser = browser
    f.fetch("https://example.com/x")
    assert browser.new_context.call_args.kwargs["extra_http_headers"] is None
    f.close()


# ---------------------------------------------------------------------------
# SSRF 防护：dynamic 入口拒绝非 http(s) scheme
# ---------------------------------------------------------------------------
def test_fetch_rejects_non_http_scheme() -> None:
    """fetch 入口应拒绝 file:// 等非 http(s) URL（无需启动浏览器）。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    with pytest.raises(ValueError, match="only http/https"):
        f.fetch("file:///etc/passwd")
    with pytest.raises(ValueError, match="only http/https"):
        f.fetch("ftp://example.com/x")
    f.close()


def test_async_fetch_rejects_non_http_scheme() -> None:
    """async_fetch 入口同样拒绝非 http(s) URL。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher()
        try:
            with pytest.raises(ValueError, match="only http/https"):
                await f.async_fetch("file:///etc/passwd")
        finally:
            f.close()

    import asyncio

    asyncio.run(go())


def test_screenshot_tiles_rejects_non_http_scheme() -> None:
    """screenshot_tiles 入口同样拒绝非 http(s) URL。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    with pytest.raises(ValueError, match="only http/https"):
        f.screenshot_tiles("data:text/plain,hi")
    f.close()


# ---------------------------------------------------------------------------
# _parse_proxy 边界：无 scheme / IPv6 / 百分号编码凭据
# ---------------------------------------------------------------------------
def test_parse_proxy_scheme_less() -> None:
    """无 scheme 的代理 URL 默认按 http 处理。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    assert f._parse_proxy("1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}
    assert f._parse_proxy("user:pass@1.2.3.4:8080") == {
        "server": "http://1.2.3.4:8080",
        "username": "user",
        "password": "pass",
    }
    f.close()


def test_parse_proxy_ipv6() -> None:
    """IPv6 代理地址需补回方括号。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    assert f._parse_proxy("http://[::1]:8080") == {"server": "http://[::1]:8080"}
    f.close()


def test_parse_proxy_percent_encoded_credentials() -> None:
    """百分号编码的 userinfo 应解码后交给 Playwright。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    settings = f._parse_proxy("http://user%40x:p%40ss@1.2.3.4:8080")
    assert settings["server"] == "http://1.2.3.4:8080"
    assert settings["username"] == "user@x"
    assert settings["password"] == "p@ss"
    f.close()


# ---------------------------------------------------------------------------
# screenshot_tiles max_tiles 上限
# ---------------------------------------------------------------------------
def test_screenshot_tiles_max_tiles_cap() -> None:
    """超过 max_tiles 上限时截断并发出 RuntimeWarning。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher(network_idle=False)
    page = _make_sync_page()
    page.evaluate.return_value = {"width": 875.0, "height": 5000.0}
    page.screenshot.side_effect = [b"T0", b"T1", b"T2"]
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    f._browser = browser

    with pytest.warns(RuntimeWarning, match="max_tiles"):
        tiles = f.screenshot_tiles("https://example.com/", tile_height=500, max_tiles=3)
    assert len(tiles) == 3
    assert tiles[0]["total"] == 3
    assert tiles[2]["height"] == 500
    f.close()


def test_async_screenshot_tiles_max_tiles_cap() -> None:
    """异步切片路径同样受 max_tiles 上限约束。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher(network_idle=False)
        page = _make_async_page()
        page.evaluate.return_value = {"width": 875.0, "height": 5000.0}
        page.screenshot.side_effect = [b"A", b"B"]
        ctx = _make_async_context(page)
        browser = _make_async_browser(ctx)
        f._async_browser = browser
        with pytest.warns(RuntimeWarning, match="max_tiles"):
            tiles = await f.async_screenshot_tiles(
                "https://example.com/", tile_height=500, max_tiles=2
            )
        assert len(tiles) == 2
        f.close()

    import asyncio

    asyncio.run(go())


# ---------------------------------------------------------------------------
# close() 事件循环检测：运行中 loop 不启动临时循环，失败保留引用
# ---------------------------------------------------------------------------
def test_close_inside_running_loop_warns_and_keeps_handles() -> None:
    """close() 在运行中的事件循环内被调用时不应启动临时循环，应告警并保留引用。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = DynamicFetcher()
        async_browser_mock = AsyncMock()
        async_pw_mock = AsyncMock()
        f._async_browser = async_browser_mock
        f._async_pw = async_pw_mock
        with pytest.warns(ResourceWarning, match="异步浏览器句柄"):
            f.close()
        # 运行中 loop 场景不强行清理：引用保留，随后 aclose() 仍可正常释放
        assert f._async_browser is async_browser_mock
        assert f._async_pw is async_pw_mock
        await f.aclose()
        assert f._async_browser is None
        assert f._async_pw is None

    import asyncio

    asyncio.run(go())


def test_close_temp_loop_failure_keeps_reference() -> None:
    """close() 临时事件循环清理失败时保留引用（不静默丢失句柄）。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    async_browser_mock = AsyncMock()
    async_browser_mock.close.side_effect = RuntimeError("attached to a different loop")
    f._async_browser = async_browser_mock
    with pytest.warns(ResourceWarning, match="异步浏览器句柄"):
        f.close()
    assert f._async_browser is async_browser_mock


def test_close_temp_loop_creation_failure_swallowed() -> None:
    """close() 临时事件循环创建失败时吞掉异常并告警保留引用。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = DynamicFetcher()
    async_browser_mock = AsyncMock()
    f._async_browser = async_browser_mock
    # new_event_loop 抛错 → 内层 try/except 吞掉，引用保留并告警
    with (
        patch(
            "web_crawler.fetchers.dynamic.asyncio.new_event_loop",
            side_effect=RuntimeError("no loop available"),
        ),
        pytest.warns(ResourceWarning, match="异步浏览器句柄"),
    ):
        f.close()
    assert f._async_browser is async_browser_mock


def test_dynamic_fetcher_get_alias_delegates_to_fetch() -> None:
    """get 是 fetch 的动词统一别名（与 Fetcher.get 对齐，供 Spider 等复用）。

    以未绑定方法直接调用，不构造实例，无 playwright 环境下也可验证。
    """
    fake_self = MagicMock()
    sentinel = MagicMock(name="response")
    mock_fetch = MagicMock(return_value=sentinel)
    fake_self.fetch = mock_fetch
    result = DynamicFetcher.get(fake_self, "https://example.com/")
    assert result is sentinel
    mock_fetch.assert_called_once_with("https://example.com/")


async def test_dynamic_fetcher_async_get_alias_delegates_to_async_fetch() -> None:
    """async_get 是 async_fetch 的动词统一别名。"""
    fake_self = MagicMock()
    sentinel = MagicMock(name="response")
    mock_af = AsyncMock(return_value=sentinel)
    fake_self.async_fetch = mock_af
    result = await DynamicFetcher.async_get(fake_self, "https://example.com/")
    assert result is sentinel
    mock_af.assert_awaited_once_with("https://example.com/")

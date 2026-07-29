"""Tests for the StealthyFetcher.

Covers stealth JS injection in _setup_page, humanized mouse/delay behavior in
_post_load, and the Cloudflare-challenge detection branches in
_solve_cloudflare_sync / _solve_cloudflare_async (title-based, selector-based,
checkbox-click, networkidle-wait, and all best-effort exception swallowing).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from web_crawler import DynamicFetcher, StealthyFetcher, compat


# ---------------------------------------------------------------------------
# 共享 mock 工厂
# ---------------------------------------------------------------------------
def _make_sync_response(status: int = 200, headers: dict[str, str] | None = None) -> MagicMock:
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
    page = MagicMock()
    page.url = url
    page.content.return_value = content
    page.title.return_value = title
    page.goto.return_value = response if response is not None else _make_sync_response()
    page.frames = []
    page.mouse = MagicMock()
    page.query_selector.return_value = None
    return page


def _make_sync_context(page: MagicMock | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.new_page.return_value = page if page is not None else _make_sync_page()
    return ctx


def _make_sync_browser(context: MagicMock | None = None) -> MagicMock:
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


@pytest.fixture
def stealthy() -> Any:
    """构造一个 StealthyFetcher 实例。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = StealthyFetcher()
    yield f
    f.close()


# ---------------------------------------------------------------------------
# 构造与默认值
# ---------------------------------------------------------------------------
def test_stealthy_inherits_dynamic() -> None:
    """StealthyFetcher 是 DynamicFetcher 的子类。"""
    assert issubclass(StealthyFetcher, DynamicFetcher)


def test_stealthy_defaults(stealthy: Any) -> None:
    """Stealthy 默认值：block_images / google_search / humanize / solve_cloudflare 开启。"""
    assert stealthy.block_images is True
    assert stealthy.google_search is True
    assert stealthy.humanize is True
    assert stealthy.solve_cloudflare is True
    assert stealthy.wait_timeout == 15.0


def test_stealthy_disables_block_images_when_asked() -> None:
    """可显式关闭 block_images。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = StealthyFetcher(block_images=False)
    assert f.block_images is False
    f.close()


def test_stealthy_disables_humanize_and_cloudflare() -> None:
    """可显式关闭 humanize / solve_cloudflare。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = StealthyFetcher(humanize=False, solve_cloudflare=False)
    assert f.humanize is False
    assert f.solve_cloudflare is False
    f.close()


def test_stealthy_overrides_setup_page_hook() -> None:
    """StealthyFetcher 覆盖 _setup_page 注入 stealth JS。"""
    assert StealthyFetcher._setup_page is not DynamicFetcher._setup_page


def test_stealthy_overrides_post_load_hook() -> None:
    """StealthyFetcher 覆盖 _post_load。"""
    assert StealthyFetcher._post_load is not DynamicFetcher._post_load


def test_stealthy_overrides_async_hooks() -> None:
    """StealthyFetcher 覆盖异步钩子。"""
    assert StealthyFetcher._setup_page_async is not DynamicFetcher._setup_page_async
    assert StealthyFetcher._post_load_async is not DynamicFetcher._post_load_async


# ---------------------------------------------------------------------------
# _STEALTH_JS 常量
# ---------------------------------------------------------------------------
def test_stealth_js_patches_all_fingerprint_surfaces() -> None:
    """stealth JS 覆盖所有关键指纹面。"""
    from web_crawler.fetchers.stealthy import _STEALTH_JS

    for key in ("webdriver", "plugins", "languages", "platform", "vendor",
                "appVersion", "userAgent", "chrome", "permissions"):
        assert key in _STEALTH_JS, f"_STEALTH_JS 缺少 {key} 补丁"


# ---------------------------------------------------------------------------
# _setup_page 同步钩子
# ---------------------------------------------------------------------------
def test_setup_page_injects_stealth_js_then_super(stealthy: Any) -> None:
    """_setup_page 先注入 stealth JS，再调用 super()._setup_page（资源拦截）。"""
    page = MagicMock()
    stealthy._setup_page(page)
    # add_init_script 被调用，参数是 _STEALTH_JS
    page.add_init_script.assert_called_once()
    args, _ = page.add_init_script.call_args
    from web_crawler.fetchers.stealthy import _STEALTH_JS

    assert args[0] == _STEALTH_JS


def test_setup_page_with_block_images_also_routes(stealthy: Any) -> None:
    """block_images=True 时 _setup_page 同时注入 JS 和 route 拦截。"""
    page = MagicMock()
    stealthy._setup_page(page)
    # add_init_script + page.route（因为 block_images=True 默认）
    page.add_init_script.assert_called_once()
    page.route.assert_called_once_with("**/*", page.route.call_args.args[1])


def test_setup_page_no_route_when_no_blocked(stealthy: Any) -> None:
    """无拦截类型时仅注入 stealth JS，不调用 page.route。"""
    stealthy.block_images = False
    stealthy.disable_resources = False
    page = MagicMock()
    stealthy._setup_page(page)
    page.add_init_script.assert_called_once()
    page.route.assert_not_called()


# ---------------------------------------------------------------------------
# _post_load 同步钩子
# ---------------------------------------------------------------------------
def test_post_load_calls_cloudflare_and_humanize(stealthy: Any) -> None:
    """_post_load 在两个开关都开时调用 _solve_cloudflare_sync + _humanize_sync。"""
    page = MagicMock()
    with patch.object(stealthy, "_solve_cloudflare_sync") as mock_cf, \
         patch.object(stealthy, "_humanize_sync") as mock_h:
        stealthy._post_load(page)
        mock_cf.assert_called_once_with(page)
        mock_h.assert_called_once_with(page)


def test_post_load_skips_cloudflare_when_disabled(stealthy: Any) -> None:
    """solve_cloudflare=False 时跳过 _solve_cloudflare_sync。"""
    stealthy.solve_cloudflare = False
    page = MagicMock()
    with patch.object(stealthy, "_solve_cloudflare_sync") as mock_cf, \
         patch.object(stealthy, "_humanize_sync") as mock_h:
        stealthy._post_load(page)
        mock_cf.assert_not_called()
        mock_h.assert_called_once_with(page)


def test_post_load_skips_humanize_when_disabled(stealthy: Any) -> None:
    """humanize=False 时跳过 _humanize_sync。"""
    stealthy.humanize = False
    page = MagicMock()
    with patch.object(stealthy, "_solve_cloudflare_sync") as mock_cf, \
         patch.object(stealthy, "_humanize_sync") as mock_h:
        stealthy._post_load(page)
        mock_cf.assert_called_once_with(page)
        mock_h.assert_not_called()


def test_post_load_skips_both_when_disabled(stealthy: Any) -> None:
    """两个开关都关时 _post_load 不做任何事。"""
    stealthy.solve_cloudflare = False
    stealthy.humanize = False
    page = MagicMock()
    with patch.object(stealthy, "_solve_cloudflare_sync") as mock_cf, \
         patch.object(stealthy, "_humanize_sync") as mock_h:
        stealthy._post_load(page)
        mock_cf.assert_not_called()
        mock_h.assert_not_called()


# ---------------------------------------------------------------------------
# _humanize_sync
# ---------------------------------------------------------------------------
@patch("web_crawler.fetchers.stealthy.time.sleep")
def test_humanize_sync_moves_mouse_and_sleeps(mock_sleep: MagicMock, stealthy: Any) -> None:
    """_humanize_sync 移动鼠标并 sleep 随机时长。"""
    page = MagicMock()
    stealthy._humanize_sync(page)
    page.mouse.move.assert_called_once()
    # move 接收两个 float 参数
    args, _ = page.mouse.move.call_args
    assert 100 <= args[0] <= 800
    assert 100 <= args[1] <= 600
    mock_sleep.assert_called_once()
    # sleep 时长在 [0.5, 2.0] 范围
    sleep_arg = mock_sleep.call_args.args[0]
    assert 0.5 <= sleep_arg <= 2.0


@patch("web_crawler.fetchers.stealthy.time.sleep")
def test_humanize_sync_swallows_exceptions(mock_sleep: MagicMock, stealthy: Any) -> None:
    """_humanize_sync 对 mouse.move 异常 best-effort 吞掉。"""
    page = MagicMock()
    page.mouse.move.side_effect = RuntimeError("mouse gone")
    # 不应抛
    stealthy._humanize_sync(page)
    mock_sleep.assert_not_called()  # move 失败后不 sleep


@patch("web_crawler.fetchers.stealthy.time.sleep")
def test_humanize_sync_swallows_sleep_exception(mock_sleep: MagicMock, stealthy: Any) -> None:
    """_humanize_sync 对 time.sleep 异常 best-effort 吞掉。"""
    page = MagicMock()
    mock_sleep.side_effect = RuntimeError("interrupted")
    # 不应抛
    stealthy._humanize_sync(page)
    page.mouse.move.assert_called_once()


# ---------------------------------------------------------------------------
# _solve_cloudflare_sync —— 各挑战检测分支
# ---------------------------------------------------------------------------
def test_solve_cloudflare_returns_early_when_not_challenge(stealthy: Any) -> None:
    """非挑战页（title 不含 just a moment，无挑战 selector）时立即返回。"""
    page = MagicMock()
    page.title.return_value = "Normal Page"
    page.query_selector.return_value = None
    stealthy._solve_cloudflare_sync(page)
    # 不应等待 selector / load_state
    page.wait_for_selector.assert_not_called()
    page.wait_for_load_state.assert_not_called()


def test_solve_cloudflare_detects_challenge_by_title(stealthy: Any) -> None:
    """title 含 'just a moment' 时识别为挑战页。"""
    page = MagicMock()
    page.title.return_value = "Just a moment..."
    page.query_selector.return_value = None
    page.frames = []
    stealthy._solve_cloudflare_sync(page)
    # 应等待 cloudflare iframe selector
    page.wait_for_selector.assert_called_once()
    args, _ = page.wait_for_selector.call_args
    assert "challenges.cloudflare.com" in args[0]
    # 应等待 networkidle
    page.wait_for_load_state.assert_called_once()


def test_solve_cloudflare_detects_challenge_by_selector(stealthy: Any) -> None:
    """query_selector 找到挑战元素时识别为挑战页。"""
    page = MagicMock()
    page.title.return_value = "Normal"
    page.query_selector.return_value = MagicMock()  # 找到挑战元素
    page.frames = []
    stealthy._solve_cloudflare_sync(page)
    page.wait_for_selector.assert_called_once()
    page.wait_for_load_state.assert_called_once()


def test_solve_cloudflare_clicks_checkbox_in_frame(stealthy: Any) -> None:
    """挑战页中找到 checkbox 时点击它。"""
    page = MagicMock()
    page.title.return_value = "Just a moment..."
    page.query_selector.return_value = None
    checkbox = MagicMock()
    frame = MagicMock()
    frame.query_selector.return_value = checkbox
    page.frames = [frame]
    stealthy._solve_cloudflare_sync(page)
    checkbox.click.assert_called_once()


def test_solve_cloudflare_skips_frames_without_checkbox(stealthy: Any) -> None:
    """frame 中无 checkbox 时跳过，不抛异常。"""
    page = MagicMock()
    page.title.return_value = "Just a moment..."
    page.query_selector.return_value = None
    frame1 = MagicMock()
    frame1.query_selector.return_value = None  # 无 checkbox
    frame2 = MagicMock()
    frame2.query_selector.side_effect = RuntimeError("frame detached")
    checkbox = MagicMock()
    frame3 = MagicMock()
    frame3.query_selector.return_value = checkbox
    page.frames = [frame1, frame2, frame3]
    stealthy._solve_cloudflare_sync(page)
    # 第三个 frame 的 checkbox 被点击
    checkbox.click.assert_called_once()


def test_solve_cloudflare_swallows_wait_for_selector_timeout(stealthy: Any) -> None:
    """wait_for_selector 抛 PlaywrightTimeoutError 时被吞掉。"""
    from playwright.sync_api import TimeoutError as PWTimeoutError

    page = MagicMock()
    page.title.return_value = "Just a moment..."
    page.query_selector.return_value = None
    page.wait_for_selector.side_effect = PWTimeoutError("timeout")
    page.frames = []
    # 不应抛
    stealthy._solve_cloudflare_sync(page)


def test_solve_cloudflare_swallows_wait_for_load_state_timeout(stealthy: Any) -> None:
    """wait_for_load_state 抛 PlaywrightTimeoutError 时被吞掉。"""
    from playwright.sync_api import TimeoutError as PWTimeoutError

    page = MagicMock()
    page.title.return_value = "Just a moment..."
    page.query_selector.return_value = None
    page.wait_for_load_state.side_effect = PWTimeoutError("timeout")
    page.frames = []
    # 不应抛
    stealthy._solve_cloudflare_sync(page)


def test_solve_cloudflare_swallows_title_exception(stealthy: Any) -> None:
    """page.title() 抛异常时整个函数 best-effort 吞掉。"""
    page = MagicMock()
    page.title.side_effect = RuntimeError("page crashed")
    # 不应抛
    stealthy._solve_cloudflare_sync(page)


def test_solve_cloudflare_uses_wait_timeout_for_deadline(stealthy: Any) -> None:
    """deadline_ms = wait_timeout * 1000 传给 wait_for_selector / wait_for_load_state。"""
    stealthy.wait_timeout = 7.5
    page = MagicMock()
    page.title.return_value = "Just a moment..."
    page.query_selector.return_value = None
    page.frames = []
    stealthy._solve_cloudflare_sync(page)
    expected_ms = 7500
    _, kwargs = page.wait_for_selector.call_args
    assert kwargs["timeout"] == expected_ms
    _, kwargs = page.wait_for_load_state.call_args
    assert kwargs["timeout"] == expected_ms


# ---------------------------------------------------------------------------
# _setup_page_async / _post_load_async 异步钩子
# ---------------------------------------------------------------------------
def test_setup_page_async_injects_stealth_js(stealthy: Any) -> None:
    """异步 _setup_page_async 注入 stealth JS 并调用 super。"""

    async def go() -> None:
        page = AsyncMock()
        await stealthy._setup_page_async(page)
        page.add_init_script.assert_awaited_once()
        args, _ = page.add_init_script.call_args
        from web_crawler.fetchers.stealthy import _STEALTH_JS

        assert args[0] == _STEALTH_JS
        # block_images 默认 True → 也调用 page.route
        page.route.assert_awaited_once()

    import asyncio

    asyncio.run(go())


def test_setup_page_async_no_route_when_no_blocked(stealthy: Any) -> None:
    """异步 _setup_page_async 在无拦截类型时仅注入 JS。"""
    stealthy.block_images = False
    stealthy.disable_resources = False

    async def go() -> None:
        page = AsyncMock()
        await stealthy._setup_page_async(page)
        page.add_init_script.assert_awaited_once()
        page.route.assert_not_called()

    import asyncio

    asyncio.run(go())


def test_post_load_async_calls_both_hooks(stealthy: Any) -> None:
    """异步 _post_load_async 调用 _solve_cloudflare_async + _humanize_async。"""

    async def go() -> None:
        page = AsyncMock()
        with patch.object(stealthy, "_solve_cloudflare_async", new=AsyncMock()) as mock_cf, \
             patch.object(stealthy, "_humanize_async", new=AsyncMock()) as mock_h:
            await stealthy._post_load_async(page)
            mock_cf.assert_awaited_once_with(page)
            mock_h.assert_awaited_once_with(page)

    import asyncio

    asyncio.run(go())


def test_post_load_async_skips_cloudflare_when_disabled(stealthy: Any) -> None:
    """异步路径 solve_cloudflare=False 时跳过 _solve_cloudflare_async。"""
    stealthy.solve_cloudflare = False

    async def go() -> None:
        page = AsyncMock()
        with patch.object(stealthy, "_solve_cloudflare_async", new=AsyncMock()) as mock_cf, \
             patch.object(stealthy, "_humanize_async", new=AsyncMock()) as mock_h:
            await stealthy._post_load_async(page)
            mock_cf.assert_not_called()
            mock_h.assert_awaited_once_with(page)

    import asyncio

    asyncio.run(go())


def test_post_load_async_skips_humanize_when_disabled(stealthy: Any) -> None:
    """异步路径 humanize=False 时跳过 _humanize_async。"""
    stealthy.humanize = False

    async def go() -> None:
        page = AsyncMock()
        with patch.object(stealthy, "_solve_cloudflare_async", new=AsyncMock()) as mock_cf, \
             patch.object(stealthy, "_humanize_async", new=AsyncMock()) as mock_h:
            await stealthy._post_load_async(page)
            mock_cf.assert_awaited_once_with(page)
            mock_h.assert_not_called()

    import asyncio

    asyncio.run(go())


# ---------------------------------------------------------------------------
# _humanize_async
# ---------------------------------------------------------------------------
@patch("web_crawler.fetchers.stealthy.asyncio.sleep", new_callable=AsyncMock)
def test_humanize_async_moves_mouse_and_sleeps(mock_sleep: AsyncMock, stealthy: Any) -> None:
    """异步 _humanize_async 移动鼠标并 await asyncio.sleep。"""

    async def go() -> None:
        page = AsyncMock()
        await stealthy._humanize_async(page)
        page.mouse.move.assert_awaited_once()
        args, _ = page.mouse.move.call_args
        assert 100 <= args[0] <= 800
        assert 100 <= args[1] <= 600
        mock_sleep.assert_awaited_once()

    import asyncio

    asyncio.run(go())


@patch("web_crawler.fetchers.stealthy.asyncio.sleep", new_callable=AsyncMock)
def test_humanize_async_swallows_exceptions(mock_sleep: AsyncMock, stealthy: Any) -> None:
    """异步 _humanize_async 对 mouse.move 异常 best-effort 吞掉。"""

    async def go() -> None:
        page = AsyncMock()
        page.mouse.move.side_effect = RuntimeError("mouse gone")
        # 不应抛
        await stealthy._humanize_async(page)
        mock_sleep.assert_not_called()

    import asyncio

    asyncio.run(go())


# ---------------------------------------------------------------------------
# _solve_cloudflare_async —— 各挑战检测分支
# ---------------------------------------------------------------------------
def test_solve_cloudflare_async_returns_early_when_not_challenge(stealthy: Any) -> None:
    """异步路径非挑战页立即返回。"""

    async def go() -> None:
        page = AsyncMock()
        page.title.return_value = "Normal Page"
        page.query_selector.return_value = None
        await stealthy._solve_cloudflare_async(page)
        page.wait_for_selector.assert_not_called()
        page.wait_for_load_state.assert_not_called()

    import asyncio

    asyncio.run(go())


def test_solve_cloudflare_async_detects_challenge_by_title(stealthy: Any) -> None:
    """异步路径 title 含 'just a moment' 时识别为挑战页。"""

    async def go() -> None:
        page = AsyncMock()
        page.title.return_value = "Just a moment..."
        page.query_selector.return_value = None
        page.frames = []
        await stealthy._solve_cloudflare_async(page)
        page.wait_for_selector.assert_awaited_once()
        page.wait_for_load_state.assert_awaited_once()

    import asyncio

    asyncio.run(go())


def test_solve_cloudflare_async_detects_challenge_by_selector(stealthy: Any) -> None:
    """异步路径 query_selector 找到挑战元素时识别为挑战页。"""

    async def go() -> None:
        page = AsyncMock()
        page.title.return_value = "Normal"
        page.query_selector.return_value = MagicMock()
        page.frames = []
        await stealthy._solve_cloudflare_async(page)
        page.wait_for_selector.assert_awaited_once()
        page.wait_for_load_state.assert_awaited_once()

    import asyncio

    asyncio.run(go())


def test_solve_cloudflare_async_clicks_checkbox_in_frame(stealthy: Any) -> None:
    """异步路径找到 checkbox 时 await click。"""

    async def go() -> None:
        page = AsyncMock()
        page.title.return_value = "Just a moment..."
        page.query_selector.return_value = None
        checkbox = AsyncMock()
        frame = AsyncMock()
        frame.query_selector.return_value = checkbox
        page.frames = [frame]
        await stealthy._solve_cloudflare_async(page)
        checkbox.click.assert_awaited_once()

    import asyncio

    asyncio.run(go())


def test_solve_cloudflare_async_skips_frames_without_checkbox(stealthy: Any) -> None:
    """异步路径 frame 中无 checkbox 或抛异常时跳过。"""

    async def go() -> None:
        page = AsyncMock()
        page.title.return_value = "Just a moment..."
        page.query_selector.return_value = None
        frame1 = AsyncMock()
        frame1.query_selector.return_value = None
        frame2 = AsyncMock()
        frame2.query_selector.side_effect = RuntimeError("detached")
        checkbox = AsyncMock()
        frame3 = AsyncMock()
        frame3.query_selector.return_value = checkbox
        page.frames = [frame1, frame2, frame3]
        await stealthy._solve_cloudflare_async(page)
        checkbox.click.assert_awaited_once()

    import asyncio

    asyncio.run(go())


def test_solve_cloudflare_async_swallows_wait_for_selector_timeout(stealthy: Any) -> None:
    """异步路径 wait_for_selector 抛 PlaywrightTimeoutError 时被吞掉。"""
    from playwright.async_api import TimeoutError as PWAsyncTimeoutError

    async def go() -> None:
        page = AsyncMock()
        page.title.return_value = "Just a moment..."
        page.query_selector.return_value = None
        page.wait_for_selector.side_effect = PWAsyncTimeoutError("timeout")
        page.frames = []
        await stealthy._solve_cloudflare_async(page)

    import asyncio

    asyncio.run(go())


def test_solve_cloudflare_async_swallows_wait_for_load_state_timeout(stealthy: Any) -> None:
    """异步路径 wait_for_load_state 抛 PlaywrightTimeoutError 时被吞掉。"""
    from playwright.async_api import TimeoutError as PWAsyncTimeoutError

    async def go() -> None:
        page = AsyncMock()
        page.title.return_value = "Just a moment..."
        page.query_selector.return_value = None
        page.wait_for_load_state.side_effect = PWAsyncTimeoutError("timeout")
        page.frames = []
        await stealthy._solve_cloudflare_async(page)

    import asyncio

    asyncio.run(go())


def test_solve_cloudflare_async_swallows_title_exception(stealthy: Any) -> None:
    """异步路径 page.title() 抛异常时整个函数 best-effort 吞掉。"""

    async def go() -> None:
        page = AsyncMock()
        page.title.side_effect = RuntimeError("page crashed")
        await stealthy._solve_cloudflare_async(page)

    import asyncio

    asyncio.run(go())


# ---------------------------------------------------------------------------
# fetch / async_fetch 委托父类
# ---------------------------------------------------------------------------
def test_fetch_delegates_to_parent_and_injects_stealth() -> None:
    """fetch 委托父类，但通过 _setup_page 注入 stealth JS。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = StealthyFetcher(network_idle=False)
    page = _make_sync_page(content="<html>stealth</html>")
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    f._browser = browser

    out = f.fetch("https://example.com/x")
    assert out.status == 200
    # stealth JS 被注入
    page.add_init_script.assert_called_once()
    f.close()


def test_fetch_wraps_failure_in_runtime_error() -> None:
    """fetch 把 _render_page 异常包装为 RuntimeError。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = StealthyFetcher()
    f._browser = MagicMock()
    f._browser.new_context.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="dynamic fetch of https://x/ failed"):
        f.fetch("https://x/")
    f.close()


def test_async_fetch_delegates_to_parent_and_injects_stealth() -> None:
    """async_fetch 委托父类，但通过 _setup_page_async 注入 stealth JS。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = StealthyFetcher(network_idle=False)
        page = _make_async_page(content="<html>async stealth</html>")
        ctx = _make_async_context(page)
        browser = _make_async_browser(ctx)
        f._async_browser = browser
        out = await f.async_fetch("https://example.com/x")
        assert out.status == 200
        page.add_init_script.assert_awaited_once()
        f.close()

    import asyncio

    asyncio.run(go())


def test_async_fetch_wraps_failure_in_runtime_error() -> None:
    """async_fetch 把异常包装为 RuntimeError。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    async def go() -> None:
        f = StealthyFetcher()
        f._async_browser = AsyncMock()
        f._async_browser.new_context.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError, match="dynamic async fetch of https://x/ failed"):
            await f.async_fetch("https://x/")
        f.close()

    import asyncio

    asyncio.run(go())


# ---------------------------------------------------------------------------
# 端到端 _render_page 集成（验证 stealth 钩子在渲染流程中被调用）
# ---------------------------------------------------------------------------
def test_render_page_invokes_stealth_hooks_in_order() -> None:
    """_render_page 调用 _setup_page（注入 JS）→ goto → _post_load（humanize+cf）。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = StealthyFetcher(network_idle=False, humanize=False, solve_cloudflare=False)
    page = _make_sync_page(content="<html>rendered</html>")
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    f._render_page(browser, "https://example.com/x", None)
    # stealth JS 注入
    page.add_init_script.assert_called_once()
    # _post_load 被调用（但两个开关都关，所以无 mouse/cf 调用）
    page.mouse.move.assert_not_called()
    f.close()


def test_render_page_with_humanize_moves_mouse() -> None:
    """_render_page 在 humanize=True 时通过 _post_load 移动鼠标。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = StealthyFetcher(network_idle=False, humanize=True, solve_cloudflare=False)
    page = _make_sync_page()
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    with patch("web_crawler.fetchers.stealthy.time.sleep"):
        f._render_page(browser, "https://example.com/x", None)
    page.mouse.move.assert_called_once()
    f.close()


def test_render_page_with_cloudflare_detection() -> None:
    """_render_page 在检测到 Cloudflare 挑战时尝试解决。"""
    if not compat.HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")
    f = StealthyFetcher(network_idle=False, humanize=False, solve_cloudflare=True)
    page = _make_sync_page(title="Just a moment...")
    page.frames = []
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    f._render_page(browser, "https://example.com/x", None)
    # 应等待 cloudflare iframe
    page.wait_for_selector.assert_called_once()
    f.close()

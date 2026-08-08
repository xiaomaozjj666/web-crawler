"""Tests for the CamoufoxFetcher.

Covers launch-kw assembly, sync/async browser lifecycle (mocked Camoufox),
rendering flow (which must NOT override UA/locale/viewport to preserve the
Camoufox-generated fingerprint), and close/aclose cleanup paths.

All tests mock the camoufox dependency via sys.modules injection so they run
regardless of whether camoufox/numpy is actually importable in the test env.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("playwright", reason="CamoufoxFetcher requires playwright (install with pip install playwright && playwright install chromium)")


# ---------------------------------------------------------------------------
# 共享 mock 工厂：构造伪造的 Playwright sync/async 对象图
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


# ---------------------------------------------------------------------------
# 共享 fixture：注入伪造的 camoufox 模块到 sys.modules，patch require_camoufox
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_camoufox(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """注入伪造的 camoufox.{sync_api,async_api} 模块并 patch require_camoufox。

    返回 {"sync": sync_module, "async": async_module} 供测试定制。
    """
    # require_camoufox 默认 no-op，让 __init__ 不因 HAS_CAMOUFOX=False 而失败
    monkeypatch.setattr(
        "web_crawler.fetchers.camoufox.require_camoufox", lambda: None, raising=False
    )

    fake_sync_module = MagicMock(name="camoufox.sync_api")
    fake_sync_module.Camoufox = MagicMock(name="Camoufox")
    fake_async_module = MagicMock(name="camoufox.async_api")
    fake_async_module.AsyncCamoufox = MagicMock(name="AsyncCamoufox")
    # camoufox 包本身（避免 import camoufox 触发 numpy）
    fake_pkg = MagicMock(name="camoufox")
    fake_pkg.sync_api = fake_sync_module
    fake_pkg.async_api = fake_async_module

    monkeypatch.setitem(sys.modules, "camoufox", fake_pkg)
    monkeypatch.setitem(sys.modules, "camoufox.sync_api", fake_sync_module)
    monkeypatch.setitem(sys.modules, "camoufox.async_api", fake_async_module)

    return {"sync": fake_sync_module, "async": fake_async_module}


@pytest.fixture
def fetcher(mock_camoufox: dict[str, MagicMock]) -> Any:
    """构造一个 CamoufoxFetcher 实例（已 patch 依赖）。"""
    from web_crawler import CamoufoxFetcher

    f = CamoufoxFetcher()
    yield f
    f.close()


# ---------------------------------------------------------------------------
# 构造与 require_camoufox
# ---------------------------------------------------------------------------
def test_camoufox_fetcher_requires_camoufox() -> None:
    """camoufox 未安装时 require_camoufox 抛 ImportError，构造失败。"""
    from web_crawler import CamoufoxFetcher

    with patch(
        "web_crawler.fetchers.camoufox.require_camoufox",
        side_effect=ImportError("camoufox is required for the anti-fingerprint Firefox fetcher"),
    ), pytest.raises(ImportError, match="camoufox"):
        CamoufoxFetcher()


def test_init_calls_require_camoufox(mock_camoufox: dict[str, MagicMock]) -> None:
    """__init__ 调用 require_camoufox。"""
    from web_crawler import CamoufoxFetcher

    with patch("web_crawler.fetchers.camoufox.require_camoufox") as mock_req:
        f = CamoufoxFetcher()
        mock_req.assert_called_once()
        f.close()


# ---------------------------------------------------------------------------
# _launch_kwargs 各参数组合
# ---------------------------------------------------------------------------
def test_launch_kwargs_minimal(fetcher: Any) -> None:
    """仅 headless + humanize 默认值；不添加 None 字段。"""
    kwargs = fetcher._launch_kwargs()
    assert kwargs == {"headless": True, "humanize": True}


def test_launch_kwargs_all_options(mock_camoufox: dict[str, MagicMock]) -> None:
    """所有可选项都被纳入 kwargs。"""
    from web_crawler import CamoufoxFetcher

    f = CamoufoxFetcher(
        headless=False,
        os="macos",
        humanize=2.5,
        locale="en-US",
        geoip=True,
        block_webrtc=True,
        window=(1280, 800),
    )
    kwargs = f._launch_kwargs()
    assert kwargs["headless"] is False
    assert kwargs["humanize"] == 2.5
    assert kwargs["os"] == "macos"
    assert kwargs["locale"] == "en-US"
    assert kwargs["geoip"] is True
    assert kwargs["block_webrtc"] is True
    assert kwargs["window"] == (1280, 800)
    f.close()


def test_launch_kwargs_humanize_as_float(fetcher: Any) -> None:
    """humanize 接受 float（最大持续秒数）。"""
    assert fetcher._launch_kwargs()["humanize"] is True  # 默认


def test_launch_kwargs_humanize_float(mock_camoufox: dict[str, MagicMock]) -> None:
    """humanize 接受 float 值。"""
    from web_crawler import CamoufoxFetcher

    f = CamoufoxFetcher(humanize=1.5)
    assert f._launch_kwargs()["humanize"] == 1.5
    f.close()


def test_launch_kwargs_with_proxy(mock_camoufox: dict[str, MagicMock]) -> None:
    """代理 URL 被解析为 ProxySettings 并加入 kwargs。"""
    from web_crawler import CamoufoxFetcher

    f = CamoufoxFetcher(proxy="http://user:pass@1.2.3.4:8080")
    kwargs = f._launch_kwargs()
    assert kwargs["proxy"] == {
        "server": "http://1.2.3.4:8080",
        "username": "user",
        "password": "pass",
    }
    f.close()


def test_launch_kwargs_no_proxy_when_none(fetcher: Any) -> None:
    """无代理时 kwargs 不含 proxy 键。"""
    assert "proxy" not in fetcher._launch_kwargs()


def test_launch_kwargs_camoufox_options_override_defaults(mock_camoufox: dict[str, MagicMock]) -> None:
    """camoufox_options 覆盖默认值并补充新键。"""
    from web_crawler import CamoufoxFetcher

    f = CamoufoxFetcher(camoufox_options={"headless": False, "fingerprint_preset": "stealth"})
    kwargs = f._launch_kwargs()
    # 用户传入的 headless 覆盖默认 True
    assert kwargs["headless"] is False
    assert kwargs["fingerprint_preset"] == "stealth"
    f.close()


def test_launch_kwargs_camoufox_options_copied_not_mutated(
    mock_camoufox: dict[str, MagicMock],
) -> None:
    """camoufox_options 被 dict() 拷贝，修改原 dict 不影响 fetcher。"""
    from web_crawler import CamoufoxFetcher

    original: dict[str, Any] = {"fingerprint_preset": "stealth"}
    f = CamoufoxFetcher(camoufox_options=original)
    original["headless"] = True  # 修改原 dict
    kwargs = f._launch_kwargs()
    # 不应被原 dict 的后续修改影响：headless 来自默认值
    assert kwargs["headless"] is True
    f.close()


def test_camoufox_attributes_stored(mock_camoufox: dict[str, MagicMock]) -> None:
    """构造参数被存为实例属性。"""
    from web_crawler import CamoufoxFetcher

    f = CamoufoxFetcher(
        os="linux",
        humanize=False,
        locale=["en-US", "fr-FR"],
        geoip="1.2.3.4",
        block_webrtc=True,
        window=(1024, 768),
    )
    assert f.os == "linux"
    assert f.humanize is False
    assert f.locale == ["en-US", "fr-FR"]
    assert f.geoip == "1.2.3.4"
    assert f.block_webrtc is True
    assert f.window == (1024, 768)
    f.close()


# ---------------------------------------------------------------------------
# _ensure_browser / _render_page / fetch 同步路径
# ---------------------------------------------------------------------------
def test_ensure_browser_uses_camoufox_sync(fetcher: Any) -> None:
    """_ensure_browser 通过 Camoufox(__enter__) 启动 Firefox；二次复用。"""
    mock_cm = MagicMock()
    mock_browser = MagicMock()
    mock_cm.__enter__.return_value = mock_browser
    # 直接 patch sys.modules 里的 Camoufox 类
    with patch.object(
        sys.modules["camoufox.sync_api"], "Camoufox", return_value=mock_cm
    ) as mock_cls:
        b1 = fetcher._ensure_browser()
        assert b1 is mock_browser
        mock_cls.assert_called_once()
        # 二次调用复用
        b2 = fetcher._ensure_browser()
        assert b2 is mock_browser
        assert mock_cls.call_count == 1


def test_ensure_browser_passes_launch_kwargs(fetcher: Any) -> None:
    """_ensure_browser 把 _launch_kwargs() 透传给 Camoufox。"""
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = MagicMock()
    with patch.object(
        sys.modules["camoufox.sync_api"], "Camoufox", return_value=mock_cm
    ) as mock_cls:
        fetcher._ensure_browser()
        expected_kwargs = fetcher._launch_kwargs()
        mock_cls.assert_called_once_with(**expected_kwargs)


def test_render_page_does_not_override_fingerprint(fetcher: Any) -> None:
    """Camoufox 的 _render_page 不传 UA/locale/viewport，保留生成指纹。"""
    fetcher.extra_headers = {"X-Test": "1"}
    fetcher.verify = False
    page = _make_sync_page()
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    fetcher._render_page(browser, "https://example.com/x", None)
    call_kwargs = browser.new_context.call_args.kwargs
    # 关键：不传 user_agent / locale / viewport
    assert "user_agent" not in call_kwargs
    assert "locale" not in call_kwargs
    assert "viewport" not in call_kwargs
    # 但传 extra_http_headers / ignore_https_errors
    assert call_kwargs["extra_http_headers"] == {"X-Test": "1"}
    assert call_kwargs["ignore_https_errors"] is True


def test_render_page_full_sync_flow(fetcher: Any) -> None:
    """Camoufox 同步渲染完整流程：导航 → 等待 → 截取 HTML → 构建 Response。"""
    fetcher.google_search = True
    fetcher.wait_selector = "#main"
    fetcher.network_idle = True
    resp_mock = _make_sync_response(status=200, headers={"X-Resp": "yes"})
    page = _make_sync_page(content="<html>camoufox</html>", response=resp_mock)
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    out = fetcher._render_page(browser, "https://example.com/x", None)
    page.goto.assert_called_once()
    assert page.goto.call_args.kwargs["referer"] == "https://www.google.com/"
    page.wait_for_selector.assert_called_once_with("#main", timeout=fetcher.wait_timeout * 1000)
    page.wait_for_load_state.assert_called_once_with(
        "networkidle", timeout=fetcher.wait_timeout * 1000
    )
    ctx.close.assert_called_once()
    assert out.status == 200
    assert out.content == b"<html>camoufox</html>"


def test_render_page_no_wait_no_idle(fetcher: Any) -> None:
    """wait_selector=None + network_idle=False 不调用对应等待。"""
    fetcher.wait_selector = None
    fetcher.network_idle = False
    page = _make_sync_page()
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    fetcher._render_page(browser, "https://example.com/x", None)
    page.wait_for_selector.assert_not_called()
    page.wait_for_load_state.assert_not_called()


def test_render_page_handles_none_response(fetcher: Any) -> None:
    """page.goto 返回 None 时使用默认 status=200 / headers={}。"""
    fetcher.network_idle = False
    page = _make_sync_page()
    page.goto.return_value = None
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    out = fetcher._render_page(browser, "https://example.com/x", None)
    assert out.status == 200
    assert out.headers == {}


def test_render_page_networkidle_timeout_swallowed(fetcher: Any) -> None:
    """networkidle 超时被吞掉。"""
    from playwright.sync_api import TimeoutError as PWTimeoutError

    fetcher.network_idle = True
    page = _make_sync_page()
    page.wait_for_load_state.side_effect = PWTimeoutError("timeout")
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    out = fetcher._render_page(browser, "https://example.com/x", None)
    assert out.status == 200


def test_render_page_context_closed_on_exception(fetcher: Any) -> None:
    """渲染异常时 context.close 在 finally 中被调用。"""
    fetcher.network_idle = False
    page = _make_sync_page()
    page.goto.side_effect = RuntimeError("nav failed")
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    with pytest.raises(RuntimeError, match="nav failed"):
        fetcher._render_page(browser, "https://example.com/x", None)
    ctx.close.assert_called_once()


def test_render_page_invokes_page_action(mock_camoufox: dict[str, MagicMock]) -> None:
    """page_action 回调被调用。"""
    from web_crawler import CamoufoxFetcher

    called_with: list[Any] = []

    def action(page: Any) -> None:
        called_with.append(page)

    f = CamoufoxFetcher(page_action=action, network_idle=False)
    page = _make_sync_page()
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    f._render_page(browser, "https://example.com/x", None)
    assert called_with == [page]
    f.close()


def test_fetch_success_returns_response(fetcher: Any) -> None:
    """fetch 成功路径返回 Response。"""
    fetcher.network_idle = False
    page = _make_sync_page()
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    fetcher._browser = browser  # 跳过 _ensure_browser

    out = fetcher.fetch("https://example.com/x")
    assert out.status == 200
    assert b"hi" in out.content


def test_fetch_wraps_failure_in_runtime_error(fetcher: Any) -> None:
    """fetch 把异常包装为 RuntimeError。"""
    fetcher._browser = MagicMock()
    fetcher._browser.new_context.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="dynamic fetch of https://x/ failed"):
        fetcher.fetch("https://x/")


def test_fetch_passes_proxy_to_render(mock_camoufox: dict[str, MagicMock]) -> None:
    """fetch 在有代理时仍能正常渲染（proxy_settings 传给 _render_page 但被忽略）。"""
    from web_crawler import CamoufoxFetcher

    f = CamoufoxFetcher(proxy="http://1.2.3.4:8080", network_idle=False)
    page = _make_sync_page()
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)
    f._browser = browser

    out = f.fetch("https://example.com/x")
    assert out.status == 200
    # Camoufox 的 _render_page 不传 proxy 给 new_context（指纹由 Camoufox 内部处理）
    assert "proxy" not in browser.new_context.call_args.kwargs
    f.close()


# ---------------------------------------------------------------------------
# _ensure_async_browser / _render_page_async / async_fetch 异步路径
# ---------------------------------------------------------------------------
def test_ensure_async_browser_uses_async_camoufox(fetcher: Any) -> None:
    """_ensure_async_browser 通过 AsyncCamoufox(__aenter__) 启动；二次复用。"""
    mock_cm = AsyncMock()
    mock_browser = AsyncMock()
    mock_cm.__aenter__.return_value = mock_browser

    async def go() -> None:
        with patch.object(
            sys.modules["camoufox.async_api"], "AsyncCamoufox", return_value=mock_cm
        ) as mock_cls:
            b1 = await fetcher._ensure_async_browser()
            assert b1 is mock_browser
            mock_cls.assert_called_once()
            b2 = await fetcher._ensure_async_browser()
            assert b2 is mock_browser
            assert mock_cls.call_count == 1

    import asyncio

    asyncio.run(go())


def test_render_page_async_full_flow(fetcher: Any) -> None:
    """Camoufox 异步渲染完整流程。"""
    fetcher.google_search = True
    fetcher.wait_selector = "#main"
    fetcher.network_idle = True
    resp_mock = _make_async_response(status=200, headers={"X-Resp": "yes"})
    page = _make_async_page(content="<html>async camoufox</html>", response=resp_mock)
    ctx = _make_async_context(page)
    browser = _make_async_browser(ctx)

    async def go() -> None:
        out = await fetcher._render_page_async(browser, "https://example.com/x", None)
        # 同样不传 UA / locale / viewport
        call_kwargs = browser.new_context.call_args.kwargs
        assert "user_agent" not in call_kwargs
        assert "locale" not in call_kwargs
        assert "viewport" not in call_kwargs
        page.goto.assert_awaited_once()
        assert page.goto.call_args.kwargs["referer"] == "https://www.google.com/"
        page.wait_for_selector.assert_awaited_once_with(
            "#main", timeout=fetcher.wait_timeout * 1000
        )
        page.wait_for_load_state.assert_awaited_once_with(
            "networkidle", timeout=fetcher.wait_timeout * 1000
        )
        ctx.close.assert_awaited_once()
        assert out.status == 200
        assert out.content == b"<html>async camoufox</html>"

    import asyncio

    asyncio.run(go())


def test_render_page_async_no_wait_no_idle(fetcher: Any) -> None:
    """异步路径 wait_selector=None + network_idle=False 不调用等待。"""
    fetcher.wait_selector = None
    fetcher.network_idle = False
    page = _make_async_page()
    ctx = _make_async_context(page)
    browser = _make_async_browser(ctx)

    async def go() -> None:
        await fetcher._render_page_async(browser, "https://example.com/x", None)
        page.wait_for_selector.assert_not_called()
        page.wait_for_load_state.assert_not_called()

    import asyncio

    asyncio.run(go())


def test_render_page_async_handles_none_response(fetcher: Any) -> None:
    """异步路径 page.goto 返回 None 时使用默认 status=200 / headers={}。"""
    fetcher.network_idle = False
    page = _make_async_page()
    page.goto.return_value = None
    ctx = _make_async_context(page)
    browser = _make_async_browser(ctx)

    async def go() -> None:
        out = await fetcher._render_page_async(browser, "https://example.com/x", None)
        assert out.status == 200
        assert out.headers == {}

    import asyncio

    asyncio.run(go())


def test_render_page_async_networkidle_timeout_swallowed(fetcher: Any) -> None:
    """异步路径 networkidle 超时被吞掉。"""
    from playwright.async_api import TimeoutError as PWAsyncTimeoutError

    fetcher.network_idle = True
    page = _make_async_page()
    page.wait_for_load_state.side_effect = PWAsyncTimeoutError("timeout")
    ctx = _make_async_context(page)
    browser = _make_async_browser(ctx)

    async def go() -> None:
        out = await fetcher._render_page_async(browser, "https://example.com/x", None)
        assert out.status == 200

    import asyncio

    asyncio.run(go())


def test_render_page_async_context_closed_on_exception(fetcher: Any) -> None:
    """异步路径异常时 context.close 仍在 finally 中被 await。"""
    fetcher.network_idle = False
    page = _make_async_page()
    page.goto.side_effect = RuntimeError("nav failed")
    ctx = _make_async_context(page)
    browser = _make_async_browser(ctx)

    async def go() -> None:
        with pytest.raises(RuntimeError, match="nav failed"):
            await fetcher._render_page_async(browser, "https://example.com/x", None)
        ctx.close.assert_awaited_once()

    import asyncio

    asyncio.run(go())


def test_render_page_async_invokes_page_action(mock_camoufox: dict[str, MagicMock]) -> None:
    """异步路径 page_action 回调被调用。"""
    from web_crawler import CamoufoxFetcher

    called_with: list[Any] = []

    def action(page: Any) -> None:
        called_with.append(page)

    f = CamoufoxFetcher(page_action=action, network_idle=False)
    page = _make_async_page()
    ctx = _make_async_context(page)
    browser = _make_async_browser(ctx)

    async def go() -> None:
        await f._render_page_async(browser, "https://example.com/x", None)
        assert called_with == [page]

    import asyncio

    asyncio.run(go())
    f.close()


def test_async_fetch_success(fetcher: Any) -> None:
    """async_fetch 成功路径返回 Response。"""
    fetcher.network_idle = False
    page = _make_async_page()
    ctx = _make_async_context(page)
    browser = _make_async_browser(ctx)
    fetcher._async_browser = browser

    async def go() -> None:
        out = await fetcher.async_fetch("https://example.com/x")
        assert out.status == 200
        assert b"hi" in out.content

    import asyncio

    asyncio.run(go())


def test_async_fetch_wraps_failure_in_runtime_error(fetcher: Any) -> None:
    """async_fetch 把异常包装为 RuntimeError。"""
    fetcher._async_browser = AsyncMock()
    fetcher._async_browser.new_context.side_effect = RuntimeError("boom")

    async def go() -> None:
        with pytest.raises(RuntimeError, match="dynamic async fetch of https://x/ failed"):
            await fetcher.async_fetch("https://x/")

    import asyncio

    asyncio.run(go())


# ---------------------------------------------------------------------------
# close / aclose
# ---------------------------------------------------------------------------
def test_close_exits_camoufox_cm(fetcher: Any) -> None:
    """close() 调用 camoufox_cm.__exit__ 并清理 browser 引用。"""
    mock_cm = MagicMock()
    fetcher._camoufox_cm = mock_cm
    fetcher._browser = MagicMock()
    fetcher.close()
    mock_cm.__exit__.assert_called_once_with(None, None, None)
    assert fetcher._camoufox_cm is None
    assert fetcher._browser is None


def test_close_swallows_cm_exit_exception(fetcher: Any) -> None:
    """close() 对 __exit__ 异常 best-effort 吞掉。"""
    mock_cm = MagicMock()
    mock_cm.__exit__.side_effect = RuntimeError("exit failed")
    fetcher._camoufox_cm = mock_cm
    fetcher._browser = MagicMock()
    # 不应抛
    fetcher.close()
    assert fetcher._camoufox_cm is None
    assert fetcher._browser is None


def test_close_idempotent_when_no_cm(fetcher: Any) -> None:
    """无 camoufox_cm 时 close 是无操作。"""
    fetcher.close()
    fetcher.close()  # 二次调用无副作用
    assert fetcher._camoufox_cm is None
    assert fetcher._browser is None


def test_aclose_closes_sync_and_async_cm(fetcher: Any) -> None:
    """aclose() 同时关闭 sync camoufox_cm 和 async camoufox_cm。"""
    sync_cm = MagicMock()
    async_cm = AsyncMock()
    fetcher._camoufox_cm = sync_cm
    fetcher._browser = MagicMock()
    fetcher._async_camoufox_cm = async_cm
    fetcher._async_browser = AsyncMock()

    async def go() -> None:
        await fetcher.aclose()
        sync_cm.__exit__.assert_called_once_with(None, None, None)
        async_cm.__aexit__.assert_awaited_once_with(None, None, None)
        assert fetcher._camoufox_cm is None
        assert fetcher._browser is None
        assert fetcher._async_camoufox_cm is None
        assert fetcher._async_browser is None

    import asyncio

    asyncio.run(go())


def test_aclose_swallows_exceptions(fetcher: Any) -> None:
    """aclose() 对 sync/async cm 异常 best-effort 吞掉。"""
    sync_cm = MagicMock()
    sync_cm.__exit__.side_effect = RuntimeError("sync exit failed")
    async_cm = AsyncMock()
    async_cm.__aexit__.side_effect = RuntimeError("async exit failed")
    fetcher._camoufox_cm = sync_cm
    fetcher._browser = MagicMock()
    fetcher._async_camoufox_cm = async_cm
    fetcher._async_browser = AsyncMock()

    async def go() -> None:
        # 不应抛
        await fetcher.aclose()
        assert fetcher._camoufox_cm is None
        assert fetcher._async_camoufox_cm is None

    import asyncio

    asyncio.run(go())


def test_aclose_idempotent(fetcher: Any) -> None:
    """aclose 二次调用无副作用。"""
    import asyncio

    async def go() -> None:
        await fetcher.aclose()
        await fetcher.aclose()

    asyncio.run(go())


def test_aclose_only_sync_cm(fetcher: Any) -> None:
    """aclose() 仅 sync cm 存在时只清理 sync（async_cm 为 None）。"""
    sync_cm = MagicMock()
    fetcher._camoufox_cm = sync_cm
    fetcher._browser = MagicMock()

    async def go() -> None:
        await fetcher.aclose()
        sync_cm.__exit__.assert_called_once_with(None, None, None)
        assert fetcher._camoufox_cm is None
        assert fetcher._browser is None

    import asyncio

    asyncio.run(go())


def test_aclose_only_async_cm(fetcher: Any) -> None:
    """aclose() 仅 async cm 存在时只清理 async。"""
    async_cm = AsyncMock()
    fetcher._async_camoufox_cm = async_cm
    fetcher._async_browser = AsyncMock()

    async def go() -> None:
        await fetcher.aclose()
        async_cm.__aexit__.assert_awaited_once_with(None, None, None)
        assert fetcher._async_camoufox_cm is None
        assert fetcher._async_browser is None

    import asyncio

    asyncio.run(go())


# ---------------------------------------------------------------------------
# 继承自 DynamicFetcher 的 disable_resources / block_images 行为
# ---------------------------------------------------------------------------
def test_camoufox_inherits_resource_blocking(fetcher: Any) -> None:
    """CamoufoxFetcher 继承 DynamicFetcher 的资源拦截逻辑。"""
    fetcher.block_images = True
    fetcher.disable_resources = True
    blocked = fetcher._blocked_types()
    assert "image" in blocked
    assert "media" in blocked
    assert "font" in blocked
    assert "stylesheet" in blocked


def test_camoufox_is_dynamic_fetcher_subclass() -> None:
    """CamoufoxFetcher 是 DynamicFetcher 的子类。"""
    from web_crawler import CamoufoxFetcher, DynamicFetcher

    assert issubclass(CamoufoxFetcher, DynamicFetcher)


def test_camoufox_setup_page_inherited(fetcher: Any) -> None:
    """Camoufox 不覆盖 _setup_page，继承 DynamicFetcher 的资源拦截逻辑。"""
    from web_crawler import CamoufoxFetcher
    from web_crawler.fetchers.dynamic import DynamicFetcher

    # CamoufoxFetcher 不应覆盖 _setup_page（用父类的）
    assert CamoufoxFetcher._setup_page is DynamicFetcher._setup_page


def test_camoufox_extra_headers_passed_to_context(fetcher: Any) -> None:
    """extra_headers 被传给 new_context。"""
    fetcher.extra_headers = {"X-Custom": "value"}
    fetcher.network_idle = False
    page = _make_sync_page()
    ctx = _make_sync_context(page)
    browser = _make_sync_browser(ctx)

    fetcher._render_page(browser, "https://example.com/x", None)
    assert browser.new_context.call_args.kwargs["extra_http_headers"] == {"X-Custom": "value"}

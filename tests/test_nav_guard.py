"""导航 SSRF 守卫：文档级请求逐跳校验（同步 / 异步 / fail-closed）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from web_crawler._nav_guard import (
    _make_guard,
    _redirect_target,
    frame_urls,
    install_context_navigation_guard,
    install_context_navigation_guard_async,
    install_navigation_guard,
    install_navigation_guard_async,
    power_aware_url_guard,
    resolve_redirect_chain_async,
    resolve_redirect_chain_sync,
    safe_goto_async,
    safe_goto_sync,
    settle_refusal,
    settle_refusal_async,
)
from web_crawler.exceptions import UnsafeTargetError


class _Request:
    def __init__(self, url: str, resource_type: str) -> None:
        self.url = url
        self.resource_type = resource_type


class _Route:
    def __init__(self, url: str, resource_type: str = "document") -> None:
        self.request = _Request(url, resource_type)
        self.continued = False
        self.fell_back = False
        self.aborted = False

    def continue_(self) -> None:
        self.continued = True

    def fallback(self) -> None:
        # 守卫放行 = 交还链上更早注册的处理器（本桩直接标记）
        self.fell_back = True

    def abort(self) -> None:
        self.aborted = True


class _Page:
    def __init__(self) -> None:
        self.pattern = ""
        self.handler: Any = None

    def route(self, pattern: str, handler: Any) -> None:
        self.pattern = pattern
        self.handler = handler


def test_sync_guard_allows_public_document() -> None:
    page = _Page()
    blocked = install_navigation_guard(page, lambda url: None)
    assert page.pattern == "**/*"

    route = _Route("https://example.com/a")
    page.handler(route)

    assert route.fell_back is True
    assert route.aborted is False
    assert blocked == []


def test_sync_guard_blocks_non_public_document_and_records_url() -> None:
    page = _Page()
    blocked = install_navigation_guard(page, lambda url: "non-public" if "169.254" in url else None)

    route = _Route("http://169.254.169.254/latest/meta-data/")
    page.handler(route)

    assert route.aborted is True
    assert route.continued is False
    assert blocked == ["http://169.254.169.254/latest/meta-data/"]


def test_sync_guard_passes_non_document_without_checking() -> None:
    page = _Page()
    calls: list[str] = []

    def check(url: str) -> str | None:
        calls.append(url)
        return "blocked"

    install_navigation_guard(page, check)
    route = _Route("http://127.0.0.1/app.js", resource_type="script")
    page.handler(route)

    assert route.fell_back is True
    assert calls == []  # 子资源不走逐跳判定（输出侧打码负责）


def test_sync_guard_fails_closed_when_check_raises() -> None:
    page = _Page()
    blocked = install_navigation_guard(page, lambda url: 1 / 0)  # type: ignore[arg-type,return-value]

    route = _Route("https://example.com/")
    page.handler(route)

    assert route.aborted is True
    assert blocked == ["https://example.com/"]


def test_sync_guard_shares_blocked_list_across_pages() -> None:
    shared: list[str] = []
    first, second = _Page(), _Page()
    install_navigation_guard(first, lambda url: "no", blocked=shared)
    install_navigation_guard(second, lambda url: "no", blocked=shared)

    first.handler(_Route("https://a.example/"))
    second.handler(_Route("https://b.example/"))

    assert shared == ["https://a.example/", "https://b.example/"]


class _AsyncRoute(_Route):
    async def continue_(self) -> None:  # type: ignore[override]
        self.continued = True

    async def fallback(self) -> None:  # type: ignore[override]
        self.fell_back = True

    async def abort(self) -> None:  # type: ignore[override]
        self.aborted = True


class _AsyncPage:
    def __init__(self) -> None:
        self.handler: Any = None

    async def route(self, pattern: str, handler: Any) -> None:
        self.handler = handler


@pytest.mark.asyncio
async def test_async_guard_allows_and_blocks() -> None:
    page = _AsyncPage()
    blocked = await install_navigation_guard_async(
        page, lambda url: None if url.startswith("https://public.") else "no"
    )

    allowed = _AsyncRoute("https://public.example/a")
    await page.handler(allowed)
    assert allowed.fell_back is True and allowed.aborted is False

    denied = _AsyncRoute("http://10.0.0.1/admin")
    await page.handler(denied)
    assert denied.aborted is True and denied.fell_back is False
    assert blocked == ["http://10.0.0.1/admin"]


@pytest.mark.asyncio
async def test_async_guard_skips_non_document() -> None:
    page = _AsyncPage()
    calls: list[str] = []

    # check_url 契约是同步函数；异步守卫内部同步调用它
    def check(url: str) -> str | None:
        calls.append(url)
        return "blocked"

    await install_navigation_guard_async(page, check)
    route = _AsyncRoute("https://example.com/api", resource_type="xhr")
    await page.handler(route)

    assert route.fell_back is True
    assert calls == []


# -- 重定向链预检 -------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class _SyncReq:
    def __init__(self, responses: list[_Resp]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _Resp:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_preflight_returns_final_url_and_disposes() -> None:
    responses = [_Resp(302, {"Location": "/final"}), _Resp(200)]
    req = _SyncReq(responses)
    probe = resolve_redirect_chain_sync(req, "https://public.example/start", lambda url: None)

    assert probe.url == "https://public.example/final"
    assert probe.chain == ("https://public.example/start", "https://public.example/final")
    assert probe.blocked == () and probe.complete is True
    assert all(item.disposed for item in responses)
    # 预检必须逐跳显式跟随，禁止底层客户端自动重定向
    assert all(kwargs["max_redirects"] == 0 for _url, kwargs in req.calls)


def test_preflight_blocks_redirect_to_non_public() -> None:
    responses = [
        _Resp(301, {"location": "/hop"}),
        _Resp(302, {"location": "http://169.254.169.254/latest/meta-data/"}),
    ]
    req = _SyncReq(responses)

    def gate(url: str) -> str | None:
        return "non-public" if "169.254" in url else None

    probe = resolve_redirect_chain_sync(req, "https://public.example/start", gate)

    assert probe.blocked == ("http://169.254.169.254/latest/meta-data/",)
    assert probe.url == "https://public.example/hop"
    assert probe.chain == ("https://public.example/start", "https://public.example/hop")
    assert probe.complete is True


def test_preflight_fails_closed_when_gate_raises() -> None:
    req = _SyncReq([_Resp(302, {"location": "/next"})])

    def gate(url: str) -> str | None:
        if url.endswith("/start"):
            return None
        raise RuntimeError("gate boom")

    probe = resolve_redirect_chain_sync(req, "https://public.example/start", gate)
    assert probe.blocked == ("https://public.example/next",)
    assert probe.url == "https://public.example/start"


def test_preflight_network_failure_reports_incomplete() -> None:
    class _BoomReq:
        def get(self, url: str, **kwargs: Any) -> _Resp:
            raise RuntimeError("connection reset")

    probe = resolve_redirect_chain_sync(
        _BoomReq(), "https://public.example/start", lambda url: None
    )
    assert probe.url == "https://public.example/start"
    assert probe.complete is False and probe.blocked == ()


def test_preflight_rejects_too_many_redirects() -> None:
    req = _SyncReq([_Resp(302, {"location": f"/hop{i}"}) for i in range(3)])
    probe = resolve_redirect_chain_sync(
        req, "https://public.example/start", lambda url: None, max_redirects=2
    )
    # 超过上限 = 链未走完：不记为 blocked，而是 complete=False，由 _preflight_refusal 拒绝
    assert probe.complete is False
    assert probe.blocked == ()


def test_preflight_blocks_non_public_entry_before_any_request() -> None:
    """入口 URL 未过门禁时不得发出预检请求（本函数可被单独调用）。"""
    entry = "http://169.254.169.254/latest/meta-data/"
    req = _SyncReq([])
    probe = resolve_redirect_chain_sync(req, entry, lambda url: "non-public")

    assert probe.blocked == (entry,)
    assert probe.url == entry
    assert probe.complete is True
    assert req.calls == []

    # 门禁自身异常同样 fail closed，且不发请求
    def raising_gate(url: str) -> str | None:
        raise RuntimeError("boom")

    raising = _SyncReq([])
    failed = resolve_redirect_chain_sync(raising, entry, raising_gate)
    assert failed.blocked == (entry,)
    assert raising.calls == []


class _AsyncResp(_Resp):
    async def dispose(self) -> None:  # type: ignore[override]
        self.disposed = True


class _AsyncReq:
    def __init__(self, responses: list[_AsyncResp]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> _AsyncResp:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_async_preflight_blocks_private_redirect() -> None:
    responses = [
        _AsyncResp(302, {"location": "/hop"}),
        _AsyncResp(307, {"location": "http://10.0.0.5/admin"}),
    ]
    req = _AsyncReq(responses)
    probe = await resolve_redirect_chain_async(
        req,
        "https://public.example/start",
        lambda url: "no" if "10.0.0.5" in url else None,
    )
    assert probe.blocked == ("http://10.0.0.5/admin",)
    assert all(item.disposed for item in responses)


@pytest.mark.asyncio
async def test_async_preflight_blocks_non_public_entry_before_any_request() -> None:
    entry = "http://169.254.169.254/latest/meta-data/"
    req = _AsyncReq([])
    probe = await resolve_redirect_chain_async(req, entry, lambda url: "non-public")

    assert probe.blocked == (entry,)
    assert probe.url == entry
    assert probe.complete is True
    assert req.calls == []

    # 门禁自身异常同样 fail closed，且不发请求
    def raising_gate(url: str) -> str | None:
        raise RuntimeError("boom")

    raising = _AsyncReq([])
    failed = await resolve_redirect_chain_async(raising, entry, raising_gate)
    assert failed.blocked == (entry,)
    assert raising.calls == []


@pytest.mark.asyncio
async def test_async_guard_fails_closed_when_check_raises() -> None:
    page = _AsyncPage()

    def gate(url: str) -> str | None:
        raise RuntimeError("gate boom")

    blocked = await install_navigation_guard_async(page, gate)
    route = _AsyncRoute("https://example.com/")
    await page.handler(route)

    assert route.aborted is True
    assert blocked == ["https://example.com/"]


@pytest.mark.asyncio
async def test_async_preflight_disposes_and_reports_network_failure() -> None:
    response = _AsyncResp(200)
    probe = await resolve_redirect_chain_async(
        _AsyncReq([response]), "https://public.example/start", lambda url: None
    )
    assert probe.url == "https://public.example/start"
    assert probe.complete is True and probe.blocked == ()
    assert response.disposed is True

    class _BoomReq:
        async def get(self, url: str, **kwargs: Any) -> _AsyncResp:
            raise RuntimeError("connection reset")

    failed = await resolve_redirect_chain_async(
        _BoomReq(), "https://public.example/start", lambda url: None
    )
    assert failed.complete is False
    assert failed.url == "https://public.example/start"


@pytest.mark.asyncio
async def test_async_preflight_gate_raises_and_redirect_limit() -> None:
    def gate(url: str) -> str | None:
        if url.endswith("/start"):
            return None
        raise RuntimeError("gate boom")

    probe = await resolve_redirect_chain_async(
        _AsyncReq([_AsyncResp(302, {"location": "/next"})]),
        "https://public.example/start",
        gate,
    )
    assert probe.blocked == ("https://public.example/next",)

    limited = _AsyncReq([_AsyncResp(302, {"location": f"/h{i}"}) for i in range(3)])
    too_many = await resolve_redirect_chain_async(
        limited, "https://public.example/start", lambda url: None, max_redirects=2
    )
    assert too_many.complete is False
    assert too_many.blocked == ()


@pytest.mark.asyncio
async def test_async_preflight_tolerates_missing_or_raising_dispose() -> None:
    class _NoDispose:
        status = 200
        headers: dict[str, str] = {}

    class _BoomDispose(_AsyncResp):
        async def dispose(self) -> None:  # type: ignore[override]
            raise RuntimeError("dispose boom")

    for response in (_NoDispose(), _BoomDispose(200)):
        probe = await resolve_redirect_chain_async(
            _AsyncReq([response]),  # type: ignore[list-item]
            "https://public.example/start",
            lambda url: None,
        )
        assert probe.complete is True


def test_preflight_tolerates_invalid_status_missing_or_raising_dispose() -> None:
    class _NoDispose:
        status = 200
        headers: dict[str, str] = {}

    class _BoomDispose(_Resp):
        def dispose(self) -> None:
            raise RuntimeError("dispose boom")

    for response in (_NoDispose(), _BoomDispose(200), _Resp("bad")):  # type: ignore[arg-type]
        probe = resolve_redirect_chain_sync(
            _SyncReq([response]),  # type: ignore[list-item]
            "https://public.example/start",
            lambda url: None,
        )
        assert probe.complete is True


# -- safe_goto：统一导航入口（失败关闭） ----------------------------------------

PUBLIC = "https://public.example/start"


class _SafeResp:
    def __init__(self, status: int, location: str | None = None) -> None:
        self.status = status
        self.headers = {"location": location} if location else {}

    def dispose(self) -> None:
        pass


class _SafeAsyncResp(_SafeResp):
    async def dispose(self) -> None:  # type: ignore[override]
        pass


class _SafeReq:
    """按调用顺序返回预置响应；步骤为异常实例时抛出。"""

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append((url, kwargs))
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class _SafeAsyncReq(_SafeReq):
    async def get(self, url: str, **kwargs: Any) -> Any:  # type: ignore[override]
        return super().get(url, **kwargs)


class _SafePage:
    """记录 goto；可在 goto 时模拟守卫拦截（on_goto）或抛错（error）。"""

    def __init__(
        self,
        *,
        url: str = "",
        on_goto: Any = None,
        error: BaseException | None = None,
    ) -> None:
        self.url = url
        self.on_goto = on_goto
        self.error = error
        self.goto_calls: list[tuple[str, dict[str, Any]]] = []

    def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append((url, kwargs))
        if self.on_goto is not None:
            self.on_goto(self)
        if self.error is not None:
            raise self.error


class _SafeAsyncPage(_SafePage):
    async def goto(self, url: str, **kwargs: Any) -> None:  # type: ignore[override]
        _SafePage.goto(self, url, **kwargs)


def _allow(url: str) -> None:
    return None


def _deny_metadata(url: str) -> str | None:
    return "non-public" if "169.254" in url else None


def test_safe_goto_sync_navigates_to_verified_final_url() -> None:
    req = _SafeReq(_SafeResp(302, "/final"), _SafeResp(200))
    page = _SafePage(url="https://public.example/final")
    blocked: list[str] = []

    safe_goto_sync(page, req, PUBLIC, _allow, blocked=blocked, timeout=5000)

    assert page.goto_calls == [
        (
            "https://public.example/final",
            {"wait_until": "domcontentloaded", "timeout": 5000, "referer": PUBLIC},
        )
    ]
    assert blocked == []


def test_safe_goto_sync_caps_preflight_timeout() -> None:
    req = _SafeReq(_SafeResp(200))
    safe_goto_sync(_SafePage(url=PUBLIC), req, PUBLIC, _allow, blocked=[], timeout=60000)
    assert req.calls[0][1]["timeout"] == 15000


def test_safe_goto_sync_refuses_when_preflight_incomplete() -> None:
    page = _SafePage(url=PUBLIC)
    with pytest.raises(UnsafeTargetError, match="未完成"):
        safe_goto_sync(page, _SafeReq(RuntimeError("reset")), PUBLIC, _allow, blocked=[])
    assert page.goto_calls == []


def test_safe_goto_sync_refuses_redirect_to_private_before_goto() -> None:
    page = _SafePage(url=PUBLIC)
    req = _SafeReq(_SafeResp(302, "http://169.254.169.254/"))
    with pytest.raises(UnsafeTargetError, match="重定向目标"):
        safe_goto_sync(page, req, PUBLIC, _deny_metadata, blocked=[])
    assert page.goto_calls == []


def test_safe_goto_sync_fails_closed_without_request_context() -> None:
    page = _SafePage(url=PUBLIC)
    with pytest.raises(UnsafeTargetError, match="无法获取浏览器请求上下文"):
        safe_goto_sync(page, None, PUBLIC, _allow, blocked=[])
    assert page.goto_calls == []


def test_safe_goto_sync_maps_route_block_that_aborted_goto() -> None:
    blocked: list[str] = []
    page = _SafePage(
        url=PUBLIC,
        on_goto=lambda _p: blocked.append("http://169.254.169.254/"),
        error=RuntimeError("net::ERR_FAILED"),
    )
    with pytest.raises(UnsafeTargetError, match="重定向目标") as info:
        safe_goto_sync(page, _SafeReq(_SafeResp(200)), PUBLIC, _allow, blocked=blocked)
    assert isinstance(info.value.__cause__, RuntimeError)


def test_safe_goto_sync_detects_route_block_after_successful_goto() -> None:
    blocked: list[str] = []
    page = _SafePage(url=PUBLIC, on_goto=lambda _p: blocked.append("http://10.0.0.1/"))
    with pytest.raises(UnsafeTargetError, match="重定向目标"):
        safe_goto_sync(page, _SafeReq(_SafeResp(200)), PUBLIC, _allow, blocked=blocked)


def test_safe_goto_sync_reraises_unrelated_goto_error() -> None:
    page = _SafePage(url=PUBLIC, error=RuntimeError("net::ERR_CONNECTION_RESET"))
    with pytest.raises(RuntimeError, match="ERR_CONNECTION_RESET"):
        safe_goto_sync(page, _SafeReq(_SafeResp(200)), PUBLIC, _allow, blocked=[])


def test_safe_goto_sync_ignores_blocks_recorded_before_this_navigation() -> None:
    """同一会话的历史拦截记录不应让本次合法导航误失败。"""
    page = _SafePage(url=PUBLIC)
    safe_goto_sync(page, _SafeReq(_SafeResp(200)), PUBLIC, _allow, blocked=["http://old/"])
    assert len(page.goto_calls) == 1


def test_safe_goto_sync_rejects_non_public_landing() -> None:
    page = _SafePage(url="http://169.254.169.254/")
    with pytest.raises(UnsafeTargetError, match="navigation landed"):
        safe_goto_sync(page, _SafeReq(_SafeResp(200)), PUBLIC, _deny_metadata, blocked=[])


def test_safe_goto_sync_fails_closed_when_landing_gate_raises() -> None:
    def check(url: str) -> str | None:
        if url == PUBLIC:
            return None
        raise RuntimeError("boom")

    page = _SafePage(url="https://public.example/other")
    with pytest.raises(UnsafeTargetError, match="navigation landed"):
        safe_goto_sync(page, _SafeReq(_SafeResp(200)), PUBLIC, check, blocked=[])


@pytest.mark.asyncio
async def test_safe_goto_async_navigates_to_verified_final_url() -> None:
    req = _SafeAsyncReq(_SafeAsyncResp(302, "/final"), _SafeAsyncResp(200))
    page = _SafeAsyncPage(url="https://public.example/final")
    blocked: list[str] = []

    await safe_goto_async(page, req, PUBLIC, _allow, blocked=blocked, timeout=5000)

    assert page.goto_calls[0][0] == "https://public.example/final"
    assert page.goto_calls[0][1]["referer"] == PUBLIC
    assert blocked == []


@pytest.mark.asyncio
async def test_safe_goto_async_refuses_when_preflight_incomplete() -> None:
    page = _SafeAsyncPage(url=PUBLIC)
    req = _SafeAsyncReq(RuntimeError("reset"))
    with pytest.raises(UnsafeTargetError, match="未完成"):
        await safe_goto_async(page, req, PUBLIC, _allow, blocked=[])
    assert page.goto_calls == []


@pytest.mark.asyncio
async def test_safe_goto_async_fails_closed_without_request_context() -> None:
    page = _SafeAsyncPage(url=PUBLIC)
    with pytest.raises(UnsafeTargetError, match="无法获取浏览器请求上下文"):
        await safe_goto_async(page, None, PUBLIC, _allow, blocked=[])
    assert page.goto_calls == []


@pytest.mark.asyncio
async def test_safe_goto_async_detects_route_block_after_successful_goto() -> None:
    blocked: list[str] = []
    page = _SafeAsyncPage(url=PUBLIC, on_goto=lambda _p: blocked.append("http://10.0.0.1/"))
    with pytest.raises(UnsafeTargetError, match="重定向目标"):
        await safe_goto_async(
            page, _SafeAsyncReq(_SafeAsyncResp(200)), PUBLIC, _allow, blocked=blocked
        )


@pytest.mark.asyncio
async def test_safe_goto_async_reraises_unrelated_goto_error() -> None:
    page = _SafeAsyncPage(url=PUBLIC, error=RuntimeError("net::ERR_CONNECTION_RESET"))
    with pytest.raises(RuntimeError, match="ERR_CONNECTION_RESET"):
        await safe_goto_async(page, _SafeAsyncReq(_SafeAsyncResp(200)), PUBLIC, _allow, blocked=[])


def test_settle_refusal_allows_public_landing_without_blocks() -> None:
    assert settle_refusal([], PUBLIC, _allow) is None


def test_settle_refusal_reports_late_route_block() -> None:
    reason = settle_refusal(["http://10.0.0.1/"], PUBLIC, _allow)
    assert reason is not None
    assert "重定向目标" in reason
    assert "10.0.0.1" in reason


def test_settle_refusal_reports_landing_on_non_public_url() -> None:
    reason = settle_refusal([], "http://169.254.169.254/", _deny_metadata)
    assert reason is not None
    assert "navigation landed" in reason


def test_settle_refusal_skips_landing_check_without_url() -> None:
    def check(url: str) -> str | None:
        return "should not be called"

    assert settle_refusal([], "", check) is None
    assert settle_refusal([], None, check) is None


# -- Refresh 头跳转与 iframe 落点复核 ----------------------------------------------


def _deny_private(url: str) -> str | None:
    return "non-public" if "10.0.0.1" in url else None


def test_preflight_refresh_header_to_internal_is_refused() -> None:
    """200 + Refresh 头跳内网：浏览器会据此导航，预检必须看见并拒绝。"""
    req = _SyncReq([_Resp(200, {"refresh": "0; url=http://10.0.0.1/"})])
    probe = resolve_redirect_chain_sync(req, PUBLIC, _deny_private)
    assert probe.blocked == ("http://10.0.0.1/",)
    assert probe.complete is True


def test_preflight_follows_refresh_header_to_public_target() -> None:
    req = _SyncReq([_Resp(200, {"Refresh": "0;URL='https://public.example/next'"}), _Resp(200)])
    probe = resolve_redirect_chain_sync(req, PUBLIC, _allow)
    assert probe.url == "https://public.example/next"
    assert probe.complete is True


def test_refresh_without_url_is_not_a_navigation() -> None:
    """``Refresh: 5`` 只是原地刷新，不产生新的跳转目标。"""
    assert _redirect_target(_Resp(200, {"refresh": "5"})) == ""


def test_location_takes_precedence_over_refresh_on_3xx() -> None:
    resp = _Resp(302, {"location": "/a", "refresh": "0; url=/b"})
    assert _redirect_target(resp) == "/a"


def test_sync_guard_subframe_block_is_not_recorded() -> None:
    """子框架被拒只 abort、不记入拦截列表（第三方广告框不应判定任务失败）。"""
    page = _Page()
    blocked = install_navigation_guard(page, lambda url: "non-public")
    route = _Route("http://169.254.169.254/")
    route.request.frame = SimpleNamespace(parent_frame=object())
    page.handler(route)
    assert route.aborted is True
    assert blocked == []


def test_frame_urls_reads_every_frame() -> None:
    page = SimpleNamespace(frames=[SimpleNamespace(url=PUBLIC), SimpleNamespace(url="")])
    assert frame_urls(page) == [PUBLIC, ""]


def test_frame_urls_propagates_failure() -> None:
    """取不到框架列表必须上抛（调用方失败关闭），不能当作"无 iframe"放行。"""
    with pytest.raises(AttributeError):
        frame_urls(object())


def test_settle_refusal_reports_landing_in_iframe() -> None:
    reason = settle_refusal([], PUBLIC, _deny_metadata, ["http://169.254.169.254/x"])
    assert reason is not None
    assert "frame" in reason


def test_settle_refusal_skips_browser_internal_and_public_frames() -> None:
    frames = ["about:blank", "chrome-error://chromewebdata/", PUBLIC, "https://ads.example/"]
    assert settle_refusal([], PUBLIC, _deny_metadata, frames) is None


@pytest.mark.parametrize(
    "refresh",
    [
        "0, url=http://10.0.0.1/",  # 逗号分隔符（WHATWG 合法）
        "0 url=http://10.0.0.1/",  # 仅空白分隔
        "0; url='http://10.0.0.1/'; x=1",  # 带引号并附加参数
        '0;URL="http://10.0.0.1/"',  # 双引号 + 大写键
        "  0 ;  url = http://10.0.0.1/  ",  # 任意空白
    ],
)
def test_refresh_header_variants_to_internal_are_refused(refresh: str) -> None:
    """WHATWG 允许的各种 Refresh 写法都必须被解析为同一跳转目标并被拒绝。"""
    req = _SyncReq([_Resp(200, {"refresh": refresh})])
    probe = resolve_redirect_chain_sync(req, PUBLIC, _deny_private)
    assert probe.blocked, refresh
    assert probe.blocked[0].startswith("http://10.0.0.1/"), refresh


@pytest.mark.parametrize(
    "refresh",
    [
        "0; http://10.0.0.1/",  # 无 url= 关键字：整个余部就是目标（WHATWG，双引擎实测导航）
        "0, //10.0.0.1/x",  # 协议相对形式同上
        "0; 'http://10.0.0.1/'",  # 余部带引号
        "0\n;http://10.0.0.1/",  # 换行空白后直接绝对 URL
    ],
)
def test_refresh_without_url_keyword_still_preflighted(refresh: str) -> None:
    """WHATWG 规定分隔符后无 ``url=`` 关键字时余部整体按 URL 解析：不能当作链尾放过。"""
    req = _SyncReq([_Resp(200, {"refresh": refresh})])
    probe = resolve_redirect_chain_sync(req, PUBLIC, _deny_private)
    assert probe.blocked, refresh
    assert probe.complete is True, refresh


def test_refresh_remainder_public_url_not_overblocked() -> None:
    """余部解析修复后不再用词检索兜底：公网目标（路径含 url= 字样）不被误拒。"""
    target = _redirect_target(_Resp(200, {"refresh": "0; http://public.example/x?url=1"}))
    assert target == "http://public.example/x?url=1"


@pytest.mark.parametrize(
    "refresh",
    ["0.5; url=http://10.0.0.1/", ".5; url=http://10.0.0.1/"],
)
def test_refresh_decimal_time_fails_closed(refresh: str) -> None:
    """十进制时长（``0.5``/前导点 ``.5``）不在 WHATWG 语法内、引擎解析有分歧：失败关闭。"""
    req = _SyncReq([_Resp(200, {"refresh": refresh})])
    probe = resolve_redirect_chain_sync(req, PUBLIC, _deny_private)
    assert probe.blocked, refresh
    assert probe.complete is True, refresh


@pytest.mark.parametrize("refresh", ["0; url=", "0 url=", "5"])
def test_refresh_empty_target_is_in_place_refresh(refresh: str) -> None:
    """空目标（``5; url=``）与纯秒数：浏览器原地刷新，不产生新跳转目标。"""
    assert _redirect_target(_Resp(200, {"refresh": refresh})) == ""


def test_refresh_relative_remainder_is_a_target() -> None:
    """``5, x=1`` 的余部按规范是相对 URL（同宿主跳转），交给门禁判定而非忽略。"""
    assert _redirect_target(_Resp(200, {"refresh": "5, x=1"})) == "x=1"
    assert _redirect_target(_Resp(200, {"refresh": "0; url=;"})) == ";"


def test_settle_refusal_ignores_data_and_blob_frames() -> None:
    """data:/blob: 子框架不发网络请求，不是 SSRF 落点，不得误判为拦截。"""
    frames = ["data:text/html,ad", "blob:https://example.com/uuid", PUBLIC]
    assert settle_refusal([], PUBLIC, _deny_metadata, frames) is None


def test_settle_refusal_still_refuses_http_frame_on_metadata() -> None:
    frames = ["data:text/html,ad", "http://169.254.169.254/latest/"]
    reason = settle_refusal([], PUBLIC, _deny_metadata, frames)
    assert reason is not None
    assert "169.254.169.254" in reason


# -- 上下文级守卫（弹窗覆盖）与 handler 异常路径 ---------------------------------


class _Context:
    def __init__(self) -> None:
        self.pattern = ""
        self.handler: Any = None

    def route(self, pattern: str, handler: Any) -> None:
        self.pattern = pattern
        self.handler = handler


class _AsyncContext(_Context):
    async def route(self, pattern: str, handler: Any) -> None:  # type: ignore[override]
        self.pattern = pattern
        self.handler = handler


class _PopupRequest:
    """弹窗的首个导航请求：访问 frame 会抛错（Playwright 对弹窗的实测行为）。"""

    resource_type = "document"

    def __init__(self, url: str) -> None:
        self.url = url

    @property
    def frame(self) -> Any:
        raise RuntimeError("Frame for this navigation request is not available")


class _PopupRoute(_Route):
    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.request = _PopupRequest(url)  # type: ignore[assignment]


class _AsyncPopupRoute(_AsyncRoute):
    def __init__(self, url: str) -> None:
        _AsyncRoute.__init__(self, url)
        self.request = _PopupRequest(url)  # type: ignore[assignment]


def test_context_guard_installs_on_context_and_shares_list() -> None:
    context = _Context()
    shared: list[str] = []
    result = install_context_navigation_guard(context, _allow, shared)

    assert result is shared
    assert context.pattern == "**/*"
    assert callable(context.handler)


def test_context_guard_records_popup_block_even_when_frame_unavailable() -> None:
    """弹窗首个文档请求取不到 frame：仍须 abort 并记录（失败关闭）。"""
    context = _Context()
    blocked = install_context_navigation_guard(context, _deny_metadata)
    route = _PopupRoute("http://169.254.169.254/latest/meta-data/")

    context.handler(route)

    assert route.aborted is True
    assert blocked == ["http://169.254.169.254/latest/meta-data/"]


def test_sync_guard_handler_exception_records_and_aborts() -> None:
    """handler 内部异常（continue 失败）：必须 abort 并记录，不能让请求挂起。"""

    class _BrokenRoute(_Route):
        def fallback(self) -> None:
            raise RuntimeError("fallback failed")

    page = _Page()
    blocked = install_navigation_guard(page, _allow)
    route = _BrokenRoute("https://example.com/x", resource_type="script")

    page.handler(route)

    assert route.aborted is True
    assert blocked == ["https://example.com/x"]


@pytest.mark.asyncio
async def test_async_guard_handler_exception_records_and_aborts() -> None:
    class _BrokenAsyncRoute(_AsyncRoute):
        async def fallback(self) -> None:  # type: ignore[override]
            raise RuntimeError("fallback failed")

    page = _AsyncPage()
    blocked = await install_navigation_guard_async(page, _allow)
    route = _BrokenAsyncRoute("https://example.com/x", resource_type="script")

    await page.handler(route)

    assert route.aborted is True
    assert blocked == ["https://example.com/x"]


@pytest.mark.asyncio
async def test_async_context_guard_installs_and_records_popup_block() -> None:
    context = _AsyncContext()
    blocked = await install_context_navigation_guard_async(context, _deny_metadata)

    assert context.pattern == "**/*"
    route = _AsyncPopupRoute("http://169.254.169.254/latest/meta-data/")
    await context.handler(route)

    assert route.aborted is True
    assert blocked == ["http://169.254.169.254/latest/meta-data/"]


@pytest.mark.asyncio
async def test_async_guard_subframe_block_is_not_recorded() -> None:
    """iframe 文档被拒：只 abort，不记入 blocked（不杀任务）。"""
    page = _AsyncPage()
    blocked = await install_navigation_guard_async(page, _deny_metadata)
    route = _AsyncRoute("http://169.254.169.254/latest/")
    route.request.frame = SimpleNamespace(parent_frame=object())  # type: ignore[assignment]

    await page.handler(route)

    assert route.aborted is True
    assert blocked == []


def test_landing_check_skips_browser_internal_page_urls() -> None:
    """about:blank / chrome-error: 是浏览器内部页，不做落点判定。

    门禁必须全拒：否则无法区分"门禁放行"与"前缀跳过"。
    """

    def deny_all(url: str) -> str | None:
        return "deny all"

    for url in ("about:blank", "chrome-error://chromewebdata/"):
        assert settle_refusal([], url, deny_all) is None
    # 对照：同一门禁下真实 URL 必须被拒
    assert settle_refusal([], "http://169.254.169.254/", deny_all) is not None


def test_landing_check_still_refuses_data_page_url() -> None:
    """data: 只对子框架豁免；作为页面落点仍要过门禁。"""
    reason = settle_refusal(
        [], "data:text/html,<h1>x</h1>", lambda url: "data url" if url.startswith("data:") else None
    )

    assert reason is not None
    assert "data:" in reason


class TestPowerAwareUrlGuard:
    """本地入口（app UI / CLI）门禁：host 校验认 Power Mode，scheme 白名单恒生效。"""

    def test_public_allowed_private_and_scheme_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("WEB_CRAWLER_POWER_MODE", raising=False)
        guard = power_aware_url_guard()
        # 公网 IP 字面量：不触发 DNS，纯本地判定
        assert guard("https://93.184.216.34/") is None
        assert guard("http://127.0.0.1/") is not None
        assert guard("http://169.254.169.254/") is not None
        assert guard("file:///etc/passwd") is not None

    def test_power_mode_allows_private_but_not_scheme(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WEB_CRAWLER_POWER_MODE", "1")
        guard = power_aware_url_guard()
        assert guard("http://127.0.0.1:8080/debug") is None
        assert guard("file:///etc/passwd") is not None


@pytest.mark.asyncio
async def test_async_gate_runs_off_event_loop_thread() -> None:
    """N-3 契约：异步路径的门禁（可能含同步 DNS）必须在线程池执行，不阻塞事件循环。"""
    import threading

    loop_thread = threading.get_ident()
    seen: list[int] = []

    def check(url: str) -> str | None:
        seen.append(threading.get_ident())
        return None

    req = _AsyncReq([_AsyncResp(200)])
    probe = await resolve_redirect_chain_async(req, PUBLIC, check)
    assert probe.complete is True
    assert seen, "门禁未被调用"
    assert all(t != loop_thread for t in seen), "门禁在事件循环线程内执行（阻塞 loop）"


@pytest.mark.asyncio
async def test_settle_refusal_async_offloads_gate() -> None:
    """settle_refusal_async 同样把门禁判定移出事件循环线程。"""
    import threading

    loop_thread = threading.get_ident()
    seen: list[int] = []

    def check(url: str) -> str | None:
        seen.append(threading.get_ident())
        return "non-public" if "169.254" in url else None

    reason = await settle_refusal_async([], PUBLIC, check, ["http://169.254.169.254/x"])
    assert reason is not None and "frame" in reason
    assert all(t != loop_thread for t in seen)


def test_safe_goto_sync_explicit_referer_wins() -> None:
    """调用方显式 referer（fetcher google_search）覆盖入口 URL 规则。"""
    req = _SafeReq(_SafeResp(302, "/final"), _SafeResp(200))
    page = _SafePage(url="https://public.example/final")
    safe_goto_sync(
        page,
        req,
        PUBLIC,
        _allow,
        blocked=[],
        timeout=5000,
        referer="https://www.google.com/",
    )
    assert page.goto_calls[0][1]["referer"] == "https://www.google.com/"


def test_safe_goto_sync_no_referer_no_redirect_keeps_default() -> None:
    """未重定向且未显式指定 referer 时不附带 referer（保持浏览器默认行为）。"""
    req = _SafeReq(_SafeResp(200))
    page = _SafePage(url=PUBLIC)
    safe_goto_sync(page, req, PUBLIC, _allow, blocked=[], timeout=5000)
    assert "referer" not in page.goto_calls[0][1]


class _ChainPage:
    """按 Playwright 语义派发的最小 route 链模型：后注册先执行。

    fallback() → 交还下一个（更早注册的）handler；continue_() → 终结整条链；
    abort() → 终结。用于验证"守卫 + 资源拦截器"组合下两者都生效。
    """

    def __init__(self) -> None:
        self._older: list[Any] = []  # 先注册
        self._newer: list[Any] = []  # 后注册（守卫）

    def register(self, handler: Any, *, newer: bool) -> None:
        (self._newer if newer else self._older).append(handler)

    def _dispatch(self, route: Any, handlers: list[Any]) -> None:
        # handlers 按注册顺序；Playwright 从最新开始
        idx = len(handlers) - 1
        while idx >= 0:
            handler = handlers[idx]
            handler(route)
            if route.aborted or route.continued:
                return
            if route.fell_back:
                idx -= 1
                continue
            # handler 既未终结也未交还：视为链耗尽放行
            return

    def dispatch(self, route: Any) -> None:
        self._dispatch(route, self._older + self._newer)


def test_guard_registered_last_does_not_bypass_resource_blocker() -> None:
    """Important-1 回归：守卫后注册（先执行），fallback 必须把非文档请求交还先注册的资源拦截器。"""
    page = _ChainPage()
    images: list[str] = []

    def blocker(route: Any) -> None:  # 先注册：拦截 image
        if route.request.resource_type == "image":
            images.append(route.request.url)
            route.abort()
            return
        route.continue_()

    blocked: list[str] = []
    page.register(blocker, newer=False)
    page.register(_make_guard(_deny_metadata, blocked), newer=True)  # 后注册：守卫先执行

    doc = _Route(PUBLIC)
    page.dispatch(doc)
    assert doc.fell_back or doc.continued
    assert doc.aborted is False

    img = _Route("https://public.example/pic.png", resource_type="image")
    page.dispatch(img)
    assert images == ["https://public.example/pic.png"], "资源拦截器被守卫旁路（Important-1 回归）"
    assert img.aborted is True

    bad = _Route("http://169.254.169.254/x")
    page.dispatch(bad)
    assert bad.aborted is True
    assert blocked == ["http://169.254.169.254/x"]


# ========== 预检瞬时失败重试（2026-11 调研实测：julius.ai 三次渲染一次被误拦）==========


class TestPreflightRetry:
    """预检链「没走完」时整体重试；blocked 是确定性结论，绝不重试。"""

    def test_transient_failure_retried_until_complete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import web_crawler._nav_guard as nav_guard

        monkeypatch.setattr(nav_guard, "PREFLIGHT_RETRY_BACKOFF_SECONDS", 0)

        class _FlakyReq:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, url: str, **kwargs: Any) -> _Resp:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("connection reset")
                return _Resp(200)

        req = _FlakyReq()
        probe = resolve_redirect_chain_sync(req, "https://public.example/start", lambda url: None)
        assert probe.complete is True
        assert probe.blocked == ()
        assert req.calls == 2

    def test_blocked_target_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import web_crawler._nav_guard as nav_guard

        monkeypatch.setattr(nav_guard, "PREFLIGHT_RETRY_BACKOFF_SECONDS", 0)

        def gate(url: str) -> str | None:
            return "non-public" if "169.254" in url else None

        req = _SyncReq([_Resp(302, {"location": "http://169.254.169.254/meta"})])
        probe = resolve_redirect_chain_sync(req, "https://public.example/start", gate)
        assert probe.blocked == ("http://169.254.169.254/meta",)
        assert probe.complete is True
        # 只消耗了一次链走查：被拦结论确定，不因重试放大请求
        assert len(req.calls) == 1

    def test_exhausted_attempts_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import web_crawler._nav_guard as nav_guard

        monkeypatch.setattr(nav_guard, "PREFLIGHT_RETRY_BACKOFF_SECONDS", 0)

        class _BoomReq:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, url: str, **kwargs: Any) -> _Resp:
                self.calls += 1
                raise RuntimeError("connection reset")

        req = _BoomReq()
        probe = resolve_redirect_chain_sync(
            req, "https://public.example/start", lambda url: None, attempts=2
        )
        assert probe.complete is False
        assert probe.blocked == ()
        assert req.calls == 2

    @pytest.mark.asyncio
    async def test_async_transient_failure_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import web_crawler._nav_guard as nav_guard

        monkeypatch.setattr(nav_guard, "PREFLIGHT_RETRY_BACKOFF_SECONDS", 0)

        class _FlakyAsyncReq:
            def __init__(self) -> None:
                self.calls = 0

            async def get(self, url: str, **kwargs: Any) -> _Resp:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("connection reset")
                return _Resp(200)

        req = _FlakyAsyncReq()
        probe = await resolve_redirect_chain_async(
            req, "https://public.example/start", lambda url: None
        )
        assert probe.complete is True
        assert req.calls == 2

    @pytest.mark.asyncio
    async def test_async_blocked_target_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import web_crawler._nav_guard as nav_guard

        monkeypatch.setattr(nav_guard, "PREFLIGHT_RETRY_BACKOFF_SECONDS", 0)

        def gate(url: str) -> str | None:
            return "non-public" if "169.254" in url else None

        class _OnceReq:
            def __init__(self) -> None:
                self.calls = 0

            async def get(self, url: str, **kwargs: Any) -> _Resp:
                self.calls += 1
                return _Resp(302, {"location": "http://169.254.169.254/meta"})

        req = _OnceReq()
        probe = await resolve_redirect_chain_async(req, "https://public.example/start", gate)
        assert probe.blocked == ("http://169.254.169.254/meta",)
        assert probe.complete is True
        assert req.calls == 1

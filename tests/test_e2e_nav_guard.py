"""导航守卫的真浏览器端到端测试（``@pytest.mark.slow``，nightly CI 运行）。

用本地回环 HTTP 服务器 + 真实 Playwright 浏览器验证整条守卫链路：

* 正路径：``allow_private_hosts=True`` 的 fetcher 抓取回环页面——预检、route
  守卫、落点复核全部执行且不阻断正常抓取（回归保护）。
* 拒绝路径：注入拒绝 ``/private-hit`` 的判定（子类覆写 ``_nav_check``）——
  302 重定向与 ``Refresh`` 头跳转都必须在请求发出前被预检拒绝，且目标端点
  **零命中**。

拒绝路径不能直接用默认策略测（回环入口本身会被私网校验拒绝），子类注入的
是"拒绝特定路径"的判定，测的是守卫机器（预检/route/复核），不是策略本身；
策略判定已由 test_nav_guard / test_ssrf_pin 的单元测试锁定。
"""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from web_crawler import compat
from web_crawler.exceptions import UnsafeTargetError
from web_crawler.fetchers.dynamic import DynamicFetcher

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not compat.HAS_PLAYWRIGHT, reason="playwright not installed"),
]


@pytest.fixture(scope="module", autouse=True)
def _proactor_policy_on_win32() -> Any:
    """Windows 上临时切回 Proactor 事件循环策略（测完恢复）。

    conftest 为 curl_cffi 的 async 后端全局设置 WindowsSelectorEventLoopPolicy，
    但 Selector 循环不支持 ``asyncio.create_subprocess_exec``——Playwright driver
    启动即 ``NotImplementedError``。本模块的真实浏览器测试需要 Proactor；
    模块结束恢复 conftest 的策略，后续 curl_cffi 测试不受影响。Linux 无此问题。
    """
    if sys.platform != "win32":
        yield
        return
    import asyncio
    import warnings

    prev = asyncio.get_event_loop_policy()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    yield
    asyncio.set_event_loop_policy(prev)


class _GuardServer:
    """回环测试服务器：记录对 /private-hit 的命中，提供正常页与跳转页。"""

    def __init__(self) -> None:
        self.private_hits: list[str] = []
        self.image_hits: list[str] = []
        self._lock = threading.Lock()
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = server.server_address[1]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _make_handler(self) -> type:
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = self.path.split("?")[0]
                if path == "/private-hit":
                    with outer._lock:
                        outer.private_hits.append(self.path)
                    self._respond(200, b"PRIVATE-HIT")
                elif path == "/hit-image":
                    with outer._lock:
                        outer.image_hits.append(self.path)
                    self._respond(200, b"IMG", headers={"Content-Type": "image/png"})
                elif path == "/imgpage":
                    self._respond(
                        200,
                        b'<html><body><img src="/hit-image"></body></html>',
                        headers={"Content-Type": "text/html"},
                    )
                elif path == "/ok":
                    self._respond(200, b"OK-PAGE-CONTENT")
                elif path == "/start302":
                    self._respond(302, b"", headers={"Location": "/private-hit"})
                elif path == "/refresh-private":
                    self._respond(200, b"REFRESH-PAGE", headers={"Refresh": "0; url=/private-hit"})
                elif path == "/meta2":
                    self._respond(
                        200,
                        b"<html><head><meta http-equiv='refresh' content='0;url=/hit-meta'>"
                        b"</head></html>",
                    )
                elif path == "/jsnav2":
                    self._respond(
                        200,
                        b"<html><body><script>location.href='/hit-js'</script></body></html>",
                    )
                elif path == "/meta-private":
                    body = (
                        b"<html><head><meta http-equiv='refresh' content='0;url=/private-hit'>"
                        b"</head><body>META</body></html>"
                    )
                    self._respond(200, body)
                else:
                    self._respond(404, b"not found")

            def _respond(
                self,
                status: int,
                body: bytes,
                headers: dict[str, str] | None = None,
            ) -> None:
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        return _Handler

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def guard_server() -> Any:
    srv = _GuardServer()
    yield srv
    srv.close()


class _PathRefusingFetcher(DynamicFetcher):
    """注入"拒绝 /private-hit 路径"的判定：测守卫机器，不测默认策略。"""

    def _nav_check(self) -> Any:  # type: ignore[override]

        def check(url: str) -> str | None:
            if "/private-hit" in url:
                return "simulated policy: private target refused"
            return None

        return check


def test_e2e_fetcher_positive_path_through_full_guard(guard_server: Any) -> None:
    """正路径：预检 + route 守卫 + 落点复核全链路执行，正常抓取不受影响。"""
    f = DynamicFetcher(
        allow_private_hosts=True,  # 回环入口：本机测试服务器场景（见 stealth.md Power Mode）
        headless=True,
        timeout=10,
    )
    try:
        resp = f.fetch(f"{guard_server.base}/ok")
        assert resp.status == 200
        assert b"OK-PAGE-CONTENT" in resp.content
        assert guard_server.private_hits == []
    finally:
        f.close()


def test_e2e_fetcher_preflight_refuses_302_to_private_target(guard_server: Any) -> None:
    """302 → 内网路径：预检在请求发出前拒绝，目标端点零命中。"""
    f = _PathRefusingFetcher(allow_private_hosts=True, headless=True, timeout=10)
    try:
        with pytest.raises(UnsafeTargetError, match="重定向目标"):
            f.fetch(f"{guard_server.base}/start302")
        assert guard_server.private_hits == []
    finally:
        f.close()


def test_e2e_fetcher_preflight_refuses_refresh_header_to_private(guard_server: Any) -> None:
    """``Refresh: 0; url=/private-hit``：预检逐跳校验覆盖，端点零命中。"""
    f = _PathRefusingFetcher(allow_private_hosts=True, headless=True, timeout=10)
    try:
        with pytest.raises(UnsafeTargetError):
            f.fetch(f"{guard_server.base}/refresh-private")
        assert guard_server.private_hits == []
    finally:
        f.close()


# ---------------------------------------------------------------------------
# meta-refresh / JS 导航的 route 可见性（双引擎实测后的固化：守卫必须拦截）
# 实测记录：无守卫时 Chromium 与 Camoufox 的后续文档请求均进 page.route 且
# 服务器收到命中（.arts/route_visibility.json，2026-11-13）。
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entry,marker", [("/meta2", "meta-refresh"), ("/jsnav2", "js-nav")])
def test_e2e_route_guard_intercepts_meta_and_js_navigation(
    guard_server: Any, entry: str, marker: str
) -> None:
    """真浏览器：meta-refresh 与脚本导航的后续文档请求被 route 守卫拦截（零命中）。"""
    from playwright.sync_api import sync_playwright

    def check(url: str) -> str | None:
        return "simulated policy: refused" if "/hit-" in url else None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            blocked: list[str] = []
            from web_crawler._nav_guard import install_navigation_guard

            install_navigation_guard(page, check, blocked=blocked)
            page.goto(f"{guard_server.base}{entry}", wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(1500)
            assert guard_server.private_hits == [], marker
            assert blocked, f"{marker}: 守卫未记录任何拦截"
            context.close()
        finally:
            browser.close()


def test_e2e_block_images_survives_guard_chain(guard_server: Any) -> None:
    """Important-1 回归：导航守卫与资源拦截共存——图片请求被拦截（服务器零命中）。

    Playwright 的 route 链按"后注册先执行"派发；守卫（后注册）放行时用
    fallback 而非 continue_，否则先注册的资源拦截器永远收不到请求。
    """
    f = DynamicFetcher(
        allow_private_hosts=True,
        headless=True,
        timeout=15,
        block_images=True,
    )
    try:
        resp = f.fetch(f"{guard_server.base}/imgpage")
        assert resp.status == 200
        assert b"<img" in resp.content
        assert guard_server.image_hits == [], "图片请求穿透了守卫链（block_images 失效）"
    finally:
        f.close()

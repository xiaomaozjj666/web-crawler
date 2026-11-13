"""浏览器导航 SSRF 守卫：逐跳校验文档级请求的目标地址（失败关闭）。

Playwright 的 ``page.goto`` 会自动跟随 HTTP 重定向，且跳转发生在浏览器内部，
调用方只能看到入口 URL 与最终响应；只校验入口 URL 时，"公网 URL → 302 →
内网 / 云元数据地址" 可以绕过 MCP 工具的 URL 门禁。

本模块提供三层互补的防护：

* :func:`install_navigation_guard` / :func:`install_navigation_guard_async`
  用 ``page.route`` 拦截**文档级请求**（主框架与 iframe），在请求发出前调用
  调用方提供的 ``check_url`` 判定；被拒绝的导航直接 ``abort``。这一层覆盖
  页面脚本主动发起的跳转。:func:`install_context_navigation_guard` 在上下文层
  安装同一判定，覆盖 ``window.open`` 等弹窗。子框架（iframe）被拒时只阻断
  请求、不判定任务失败。
* :func:`resolve_redirect_chain_sync` / :func:`resolve_redirect_chain_async`
  在导航前用 ``APIRequestContext`` 显式走一遍 HTTP 重定向链，逐跳校验并返回
  已确认的链尾 URL。实测 Camoufox/Juggler 的 ``page.route`` 看不到浏览器
  内部跟随的重定向跳转（只有首个请求进 handler），因此这一层是防 302
  绕过的关键。
* :func:`safe_goto_sync` / :func:`safe_goto_async` 把上面两层与落点复核编排
  成一个入口；MCP 浏览器工具、逆向 Agent 与三个浏览器 fetcher
  （DynamicFetcher / StealthyFetcher / CamoufoxFetcher）的导航统一经此。
  **失败关闭**：预检异常、预检
  未完成、缺少 request 上下文、链上存在非公网目标、route 守卫拦截、落点非公网，
  任一情形都抛 :class:`~web_crawler.exceptions.UnsafeTargetError`。

边界：

* 只拦截 ``resource_type == "document"`` 的请求。脚本、图片、XHR 等子资源不
  逐跳拦截（避免给每次请求增加判定开销），它们的目标地址仍由 MCP 输出侧的
  出口过滤打码，不回传内网内容。
* DNS 重绑定：门禁在校验时解析域名，浏览器随后自行解析。Playwright 不提供按
  请求的 IP 钉扎（fetcher 侧的 ``CURLOPT_RESOLVE`` 无等价物），因此这里只能
  缩小 check-then-connect 窗口，**不能彻底消除**。这是已知残余风险。
* 预检与浏览器不是同一客户端：服务端可以区分二者（预检返回公网地址、浏览器
  收到内网跳转）。此时浏览器请求已经发出，只能依靠落点复核在返回内容前拒绝，
  属于事后拦截。
* 子框架（iframe）的重定向链不经预检：浏览器在子框架内部跟随的 302 同样不经
  route。子框架的直接请求会被逐个判定，但跳转链的后段未被校验。
* meta-refresh 与脚本导航：双引擎实测（Chromium + Camoufox，2026-11-13）其
  后续文档请求均经 ``page.route`` 可见并被守卫拦截；跳转目标自身是 302 链时
  链后段仍不经预检（与主框架同类边界）。
* 异步上下文中的门禁判定（可能含同步 ``getaddrinfo``）经线程池执行
  （:func:`_agate`），不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from functools import partial
from typing import Any, NamedTuple
from urllib.parse import urljoin, urlsplit

from ._ssrf import validate_url_host
from .exceptions import UnsafeTargetError

# 与 ``mcp._ssrf_gate._check_url`` 相同契约：返回错误信息表示拒绝，None 表示放行。
UrlCheck = Callable[[str], str | None]


def power_aware_url_guard() -> UrlCheck:
    """本地入口（app UI / ``web-crawler-reverse run`` CLI）的导航门禁工厂。

    与 MCP 浏览器工具的严格门禁（``mcp._ssrf_gate._check_url``，不认 Power
    Mode）不同：本地工具面向可信环境，host 校验随 ``WEB_CRAWLER_POWER_MODE=1``
    放行（与 fetcher 入口同一信任模型），localhost 调试不受阻；http/https
    scheme 白名单始终生效，Power Mode 也不放行非 http(s) 目标。
    """

    def check(url: str) -> str | None:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return f"invalid url: {url!r}"
        if parsed.scheme not in ("http", "https"):
            return f"scheme not allowed: {parsed.scheme!r}（仅支持 http/https）"
        try:
            validate_url_host(url, resolve=True)
        except UnsafeTargetError as exc:
            return str(exc)
        return None

    return check


# 视为重定向的状态码（300/304/305 不改变导航语义，按终止处理）。
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

DEFAULT_MAX_REDIRECTS = 5

# 预检单跳超时上限（毫秒）：预检只为拿到 Location，不应拖慢导航。
PREFLIGHT_TIMEOUT_CAP_MS = 15000

_NO_REQUEST_CONTEXT = "无法获取浏览器请求上下文，无法完成重定向预检"


class RedirectProbe(NamedTuple):
    """HTTP 重定向预检结果。

    ``url`` 是可直接导航的目标（链尾或最后一个已校验 URL），``chain`` 是
    已确认的跳转链，``blocked`` 非空表示链中存在被门禁拒绝的目标——
    调用方必须在发起任何浏览器请求前失败。``complete`` 为 False 表示预检
    本身失败（网络/超时），跳转链可能未走完。
    """

    url: str
    chain: tuple[str, ...]
    blocked: tuple[str, ...]
    complete: bool


def _gate(check_url: UrlCheck, url: str) -> str | None:
    """调用门禁；门禁自身异常按拒绝处理（fail closed）。"""
    try:
        return check_url(url)
    except Exception:
        return "navigation guard failed"


async def _agate(check_url: UrlCheck, url: str) -> str | None:
    """异步上下文中的门禁调用（:func:`_gate` 的非阻塞版）。

    check_url 可能做同步 DNS 解析（``socket.getaddrinfo``，秒级阻塞）；事件循环
    内直接调用会卡住整个 loop，因此放到默认线程池执行。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _gate, check_url, url)


def _refuse(reason: str) -> UnsafeTargetError:
    """统一的导航拒绝异常，消息前缀固定便于日志与分类检索。"""
    return UnsafeTargetError(f"navigation blocked: {reason}")


def _request_url(route: Any) -> str:
    """取请求 URL；异常结构（测试替身/未来 API）返回空串。"""
    request = getattr(route, "request", None)
    url = getattr(request, "url", "") if request is not None else ""
    return url if isinstance(url, str) else ""


def _is_document(route: Any) -> bool:
    """判断请求是否为文档级导航（主框架 / iframe / 重定向跳转）。"""
    request = getattr(route, "request", None)
    return getattr(request, "resource_type", "") == "document"


def _is_subframe(route: Any) -> bool:
    """文档请求是否属于子框架（iframe）。

    弹窗的首个导航请求访问 ``request.frame`` 会抛错（"frame is not available"），
    此时与结构未知一样按主框架处理（失败关闭）。
    """
    try:
        return route.request.frame.parent_frame is not None
    except Exception:
        return False


def _make_guard(check_url: UrlCheck, blocked: list[str]) -> Callable[[Any], None]:
    """构造同步 route 处理函数。

    主框架文档被拒：记入 ``blocked`` 并 abort，调用方据此判定任务失败。
    子框架（iframe）文档被拒：只 abort、不记入——第三方广告框指向私网或解析
    失败的域名不应杀掉整次任务；请求本身已被阻断，不会向内网发出。
    """

    def _guard(route: Any) -> None:
        try:
            if not _is_document(route):
                route.continue_()
                return
            url = _request_url(route)
            if not _gate(check_url, url):
                route.continue_()
                return
            if not _is_subframe(route):
                blocked.append(url)
            route.abort()
        except Exception:
            # 守卫自身出错：请求必须被结束（否则导航挂起）并记为拒绝（失败关闭）
            blocked.append(_request_url(route) or "<unknown>")
            route.abort()

    return _guard


def _make_guard_async(check_url: UrlCheck, blocked: list[str]) -> Callable[[Any], Any]:
    """异步版 :func:`_make_guard`。"""

    async def _guard(route: Any) -> None:
        try:
            if not _is_document(route):
                await route.continue_()
                return
            url = _request_url(route)
            # 门禁可能做同步 DNS：经线程池执行，不阻塞事件循环（见 _agate）
            if not await _agate(check_url, url):
                await route.continue_()
                return
            if not _is_subframe(route):
                blocked.append(url)
            await route.abort()
        except Exception:
            blocked.append(_request_url(route) or "<unknown>")
            await route.abort()

    return _guard


def install_navigation_guard(
    page: Any, check_url: UrlCheck, blocked: list[str] | None = None
) -> list[str]:
    """在同步 Page 上安装导航守卫，返回被拦截 URL 列表（就地累积）。

    守卫必须在首次导航前安装。列表中每新增一项代表一次被 ``abort`` 的主框架
    文档请求；``page.goto`` 会因此抛错，调用方应据此返回 ``unsafe_target``。
    传入 ``blocked`` 可让同一会话的多个页面共享拦截记录。仅覆盖该页；弹窗等
    新页面请同时安装 :func:`install_context_navigation_guard`。

    注册顺序约束：Playwright 后注册者先执行，且本守卫放行时用 ``continue_()``
    短路整条 handler 链——因此本守卫必须是该 route 层**最后注册**的 handler；
    之后若在同一 ``page.route("**/*")`` 上再注册别的 handler（如资源拦截），
    会被本守卫的 ``continue_()`` 旁路而失效。
    """
    blocked = [] if blocked is None else blocked
    page.route("**/*", _make_guard(check_url, blocked))
    return blocked


def install_context_navigation_guard(
    context: Any, check_url: UrlCheck, blocked: list[str] | None = None
) -> list[str]:
    """在 BrowserContext 上安装同一守卫，覆盖上下文内所有页面（含弹窗）。

    ``page.route`` 只作用于该页，``window.open`` 产生的新页面不经过它；实测用户
    点击触发的弹窗可直接访问内网，因此必须在上下文层安装。
    """
    blocked = [] if blocked is None else blocked
    context.route("**/*", _make_guard(check_url, blocked))
    return blocked


async def install_navigation_guard_async(
    page: Any, check_url: UrlCheck, blocked: list[str] | None = None
) -> list[str]:
    """异步版 :func:`install_navigation_guard`（Playwright async API）。"""
    blocked = [] if blocked is None else blocked
    await page.route("**/*", _make_guard_async(check_url, blocked))
    return blocked


async def install_context_navigation_guard_async(
    context: Any, check_url: UrlCheck, blocked: list[str] | None = None
) -> list[str]:
    """异步版 :func:`install_context_navigation_guard`。"""
    blocked = [] if blocked is None else blocked
    await context.route("**/*", _make_guard_async(check_url, blocked))
    return blocked


# ``Refresh: <秒>; url=<目标>``：浏览器据此发起导航（任意状态码均生效）。
# 解析对齐 WHATWG shared declarative refresh steps（Chromium 与 Camoufox 实测
# 一致）：开头是一段 ASCII 数字（无时间 → 不刷新，前导点 ``.5`` 除外——见
# `_refresh_target` 内注释）；其后可有空白与一个
# ``;`` / ``,`` 分隔符；分隔符后若以 ``url =`` 开头（不区分大小写、允许空白）
# 则取其后为目标，否则**整个余部就是目标**——``Refresh: 0; http://x/`` 浏览器
# 会导航，不能当作"无跳转"放过。目标带引号时截断于闭合引号；目标为空
# （``5; url=``）时浏览器原地刷新，不是新跳转。
_REFRESH_URL_KEY_RE = re.compile(r"url\s*=\s*", re.IGNORECASE)
# 规范外形式（十进制时长 ``0.5``）的解析存在引擎分歧：无法确证目标时失败关闭，
# 返回带此前缀的占位串，由链预检直接记 blocked（不交给 check_url 判定）。
_REFRESH_UNPARSEABLE = "refresh-unparseable:"


def _refresh_target(headers: Any) -> str:
    """从 Refresh 头取跳转目标；无跳转意图时返回空串。

    无时间前缀、目标为空（原地刷新）返回空串；规范外形式（十进制时长）返回
    占位串，由链预检失败关闭。
    """
    raw = str(headers.get("refresh") or headers.get("Refresh") or "")
    s = raw
    i, n = 0, len(s)
    while i < n and s[i].isspace():
        i += 1
    j = i
    while j < n and s[j].isdigit():
        j += 1
    if j == i:
        if i < n and s[i] == ".":
            # 前导点十进制时长（``.5``）：规范 timeString 为空但字符为 ``.`` 时
            # 继续（time=0），Chromium 与 Camoufox 实测均导航——解析分歧 → 失败关闭
            return _REFRESH_UNPARSEABLE + raw
        return ""  # 规范：无时间前缀 → 不刷新
    i = j
    while i < n and s[i].isspace():
        i += 1
    if i < n and s[i] in ",;":
        i += 1
        while i < n and s[i].isspace():
            i += 1
    if i < n and s[i] == ".":
        return _REFRESH_UNPARSEABLE + raw
    key = _REFRESH_URL_KEY_RE.match(s, i)
    if key is not None:
        i = key.end()
    value = s[i:].strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        value = value[1:]
        end = value.find(quote)
        value = value[:end] if end >= 0 else value
    return value.strip()


def _redirect_target(response: Any) -> str:
    """取响应指向的下一跳：3xx 的 Location，或任意状态码下的 Refresh 跳转。

    两者都会触发浏览器导航，因此都必须进入逐跳校验；都不存在时返回空串（链尾）。
    """
    try:
        status = int(getattr(response, "status", 0) or 0)
    except (TypeError, ValueError):
        return ""
    headers = getattr(response, "headers", None) or {}
    if status in REDIRECT_STATUSES:
        location = headers.get("location") or headers.get("Location") or ""
        if location:
            return str(location).strip()
    return _refresh_target(headers)


def _dispose(response: Any) -> None:
    """尽力释放预检响应（浏览器不下载正文）；失败不影响安全判定。"""
    dispose = getattr(response, "dispose", None)
    if callable(dispose):
        try:
            dispose()
        except Exception:
            pass


async def _dispose_async(response: Any) -> None:
    """异步版 :func:`_dispose`。"""
    dispose = getattr(response, "dispose", None)
    if callable(dispose):
        try:
            await dispose()
        except Exception:
            pass


def resolve_redirect_chain_sync(
    request_context: Any,
    url: str,
    check_url: UrlCheck,
    *,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    timeout_ms: int = 15000,
) -> RedirectProbe:
    """用同步 APIRequestContext 预检 HTTP 重定向链，逐跳调用 ``check_url``。

    Camoufox/Juggler 的 ``page.route`` 看不到浏览器内部跟随的重定向跳转
    （实测仅首个请求进 handler），因此必须在导航前用独立请求走一遍链。
    ``max_redirects=0`` 保证每一跳都由本函数显式校验；预检与浏览器上下文
    共享 cookie，链上设置的 Set-Cookie 不会丢失。

    预检必须用 GET（与浏览器导航同方法），不得改用 HEAD 来省下响应体：HEAD
    与 GET 的重定向结果可以不同（服务端可对 HEAD 返回 200 / 405 或不同的
    Location），预检看到的链就不是浏览器将要走的链，等于绕过门禁。响应体由
    ``dispose`` 释放，代价是多读一次文档正文，这是为正确性付出的成本。
    """
    # 入口 URL 也必须在发起任何请求前校验：本函数可能被单独调用，
    # 不能假定调用方已经过门禁。
    entry_error = _gate(check_url, url)
    if entry_error:
        return RedirectProbe(url, (url,), (url,), True)
    current = url
    chain: list[str] = [url]
    blocked: list[str] = []
    for _ in range(max_redirects + 1):
        try:
            # GET（非 HEAD）：见本函数文档中的方法选择理由
            response = request_context.get(current, max_redirects=0, timeout=timeout_ms)
        except Exception:
            return RedirectProbe(current, tuple(chain), tuple(blocked), False)
        try:
            location = _redirect_target(response)
        finally:
            _dispose(response)
        if not location:
            return RedirectProbe(current, tuple(chain), tuple(blocked), True)
        if location.startswith(_REFRESH_UNPARSEABLE):
            # 无法解析的跳转意图不是 URL，不交给 check_url 判定（其放行与否不可靠）
            blocked.append(location)
            return RedirectProbe(current, tuple(chain), tuple(blocked), True)
        nxt = urljoin(current, location)
        if _gate(check_url, nxt):
            blocked.append(nxt)
            return RedirectProbe(current, tuple(chain), tuple(blocked), True)
        chain.append(nxt)
        current = nxt
    # 超过跳转上限：链未走完，无法确认安全（记为未完成，避免无限链）
    return RedirectProbe(current, tuple(chain), tuple(blocked), False)


async def resolve_redirect_chain_async(
    request_context: Any,
    url: str,
    check_url: UrlCheck,
    *,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    timeout_ms: int = 15000,
) -> RedirectProbe:
    """异步版 :func:`resolve_redirect_chain_sync`（Playwright async API）。"""
    # 入口 URL 也必须在发起任何请求前校验（见同步版说明）。
    entry_error = await _agate(check_url, url)
    if entry_error:
        return RedirectProbe(url, (url,), (url,), True)
    current = url
    chain: list[str] = [url]
    blocked: list[str] = []
    for _ in range(max_redirects + 1):
        try:
            response = await request_context.get(current, max_redirects=0, timeout=timeout_ms)
        except Exception:
            return RedirectProbe(current, tuple(chain), tuple(blocked), False)
        try:
            location = _redirect_target(response)
        finally:
            await _dispose_async(response)
        if not location:
            return RedirectProbe(current, tuple(chain), tuple(blocked), True)
        if location.startswith(_REFRESH_UNPARSEABLE):
            # 无法解析的跳转意图不是 URL，不交给 check_url 判定（其放行与否不可靠）
            blocked.append(location)
            return RedirectProbe(current, tuple(chain), tuple(blocked), True)
        nxt = urljoin(current, location)
        if await _agate(check_url, nxt):
            blocked.append(nxt)
            return RedirectProbe(current, tuple(chain), tuple(blocked), True)
        chain.append(nxt)
        current = nxt
    # 超过跳转上限：链未走完，无法确认安全（记为未完成，避免无限链）
    return RedirectProbe(current, tuple(chain), tuple(blocked), False)


# -- 导航判定（纯函数，同步 / 异步共用，避免两份判定逻辑漂移） -------------------


def _preflight_refusal(probe: RedirectProbe) -> str | None:
    """预检结论 → 拒绝原因；允许导航时返回 None。

    ``complete=False`` 必须拒绝：预检没走完就无法排除链上后段的内网跳转，
    而浏览器内部跟随的跳转不经 route 守卫，放行即等于绕过门禁（fail closed）。
    """
    if probe.blocked:
        return f"重定向目标 {probe.blocked[-1]!r} 不是公网地址"
    if not probe.complete:
        return f"重定向预检未完成，无法确认跳转链安全（入口 {probe.chain[0]!r}）"
    return None


def _goto_kwargs(url: str, probe: RedirectProbe, wait_until: str, timeout: float) -> dict[str, Any]:
    """导航参数：跳转过时以入口 URL 作为 referer，保持来源语义。"""
    kwargs: dict[str, Any] = {"wait_until": wait_until, "timeout": timeout}
    if probe.url != url:
        kwargs["referer"] = url
    return kwargs


_BROWSER_INTERNAL_PREFIXES = ("about:", "chrome-error:")
_NON_NETWORK_PREFIXES = ("data:", "blob:")


def _blocked_refusal(blocked: list[str], before: int) -> str | None:
    """route 守卫新增拦截记录 → 拒绝原因（纯内存判定，无 DNS）。"""
    if len(blocked) > before:
        return f"navigation blocked: 重定向目标 {blocked[-1]!r} 不是公网地址"
    return None


def _landing_refusal(page_url: Any, check_url: UrlCheck) -> str | None:
    """落点 URL 门禁判定（可能触发同步 DNS，事件循环内须用异步版）。"""
    if (
        isinstance(page_url, str)
        and page_url
        and not page_url.startswith(_BROWSER_INTERNAL_PREFIXES)
    ):
        error = _gate(check_url, page_url)
        if error:
            return f"navigation landed on disallowed url {page_url!r}: {error}"
    return None


async def _landing_refusal_async(page_url: Any, check_url: UrlCheck) -> str | None:
    """异步版 :func:`_landing_refusal`：门禁经线程池执行（见 :func:`_agate`）。"""
    if (
        isinstance(page_url, str)
        and page_url
        and not page_url.startswith(_BROWSER_INTERNAL_PREFIXES)
    ):
        error = await _agate(check_url, page_url)
        if error:
            return f"navigation landed on disallowed url {page_url!r}: {error}"
    return None


def _post_goto_refusal(
    blocked: list[str], before: int, page_url: Any, check_url: UrlCheck
) -> str | None:
    """导航完成后的复核：route 守卫是否拦过新请求、落点是否仍为公网。

    浏览器内部页（``about:blank``、``chrome-error:`` 错误页）不含网络内容，不做
    落点判定；导航被 abort 时真正的拦截已记录在 ``blocked`` 中。
    """
    refusal = _blocked_refusal(blocked, before)
    if refusal:
        return refusal
    return _landing_refusal(page_url, check_url)


def frame_urls(page: Any) -> list[str]:
    """页面全部框架（含 iframe）的当前 URL。

    取不到时异常直接上抛（不吞掉）：复核无法完成时调用方失败关闭，而不是跳过。
    """
    return [str(getattr(frame, "url", "") or "") for frame in page.frames]


def settle_refusal(
    blocked: list[str],
    page_url: Any,
    check_url: UrlCheck,
    frames: Iterable[str] = (),
) -> str | None:
    """导航之后任意时刻的复核：晚到的 route 拦截、当前落点与各 iframe 落点。

    JS 导航的 abort 可能发生在 ``goto`` 返回之后（实测：goto 返回时拦截数为 0，
    随后才出现），:func:`safe_goto_sync` 的即时复核看不到这类拦截。调用方应在
    等待页面稳定、执行任务之后再调用本函数。返回拒绝原因，允许时返回 None。

    ``frames`` 为 :func:`frame_urls` 的结果：iframe 的重定向链不经预检与主框架
    route，只能在落点上事后发现（见模块文档的残余风险）；被 abort 的 iframe 仍
    停在 ``about:blank`` 或旧地址，会被跳过。

    ``blocked`` 只传入"尚未复核的新增记录"，已处理过的记录不要重复传入。
    """
    refusal = _post_goto_refusal(blocked, 0, page_url, check_url)
    if refusal:
        return refusal
    for furl in frames:
        # data:/blob: 等非网络文档不发出网络请求，不是 SSRF 落点；其余 scheme 照常判定
        if (
            not furl
            or furl == page_url
            or furl.startswith(_BROWSER_INTERNAL_PREFIXES)
            or furl.startswith(_NON_NETWORK_PREFIXES)
        ):
            continue
        error = _gate(check_url, furl)
        if error:
            return f"navigation landed on disallowed frame url {furl!r}: {error}"
    return None


async def settle_refusal_async(
    blocked: list[str],
    page_url: Any,
    check_url: UrlCheck,
    frames: Iterable[str] = (),
) -> str | None:
    """异步版 :func:`settle_refusal`：门禁（可能含同步 DNS）放入线程池执行。

    供事件循环内的调用方（逆向 Agent 主循环、异步 fetcher）使用，避免逐帧
    DNS 解析阻塞整个 loop。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, partial(settle_refusal, blocked, page_url, check_url, frames)
    )


def _preflight_timeout(timeout: float) -> int:
    """预检超时（毫秒）：不超过导航超时与 :data:`PREFLIGHT_TIMEOUT_CAP_MS`。

    导航超时 ≤0 在 Playwright 中意为"不限时"，此时预检仍取上限。
    """
    if timeout <= 0:
        return PREFLIGHT_TIMEOUT_CAP_MS
    return max(1, min(int(timeout), PREFLIGHT_TIMEOUT_CAP_MS))


# -- 统一导航入口（失败关闭） ---------------------------------------------------


def safe_goto_sync(
    page: Any,
    request_context: Any,
    url: str,
    check_url: UrlCheck,
    *,
    blocked: list[str],
    wait_until: str = "domcontentloaded",
    timeout: float = 30000,
    referer: str | None = None,
) -> Any:
    """同步页面的受门禁导航：预检整条跳转链 → 导航到已校验链尾 → 复核落点。

    前置条件：守卫已由 :func:`install_navigation_guard` 安装在 ``page`` 上，且
    传入的 ``blocked`` 与安装时为同一列表。所有拒绝都发生在 ``goto`` 之前或之后
    的判定中，且拒绝路径一律抛 :class:`~web_crawler.exceptions.UnsafeTargetError`。
    ``referer`` 为调用方显式指定的来源（如 fetcher 的 google_search）；未指定时
    发生重定向才以入口 URL 作为 referer。返回 ``page.goto`` 的响应对象。
    """
    if request_context is None:
        raise _refuse(_NO_REQUEST_CONTEXT + "（按失败关闭拒绝导航）")
    probe = resolve_redirect_chain_sync(
        request_context,
        url,
        check_url,
        max_redirects=DEFAULT_MAX_REDIRECTS,
        timeout_ms=_preflight_timeout(timeout),
    )
    refusal = _preflight_refusal(probe)
    if refusal:
        raise _refuse(refusal)
    before = len(blocked)
    goto_kwargs = _goto_kwargs(url, probe, wait_until, timeout)
    if referer is not None:
        goto_kwargs["referer"] = referer
    try:
        resp = page.goto(probe.url, **goto_kwargs)
    except Exception as exc:
        if len(blocked) > before:
            raise _refuse(f"重定向目标 {blocked[-1]!r} 不是公网地址") from exc
        raise
    # goto 成功不等于安全：落点复核必须执行完毕才能把响应交出去
    # （曾写成 return page.goto(...)，成功路径直接返回、跳过落点复核——3 个测试当场抓住）
    refusal = _post_goto_refusal(blocked, before, getattr(page, "url", ""), check_url)
    if refusal:
        raise UnsafeTargetError(refusal)
    return resp


async def safe_goto_async(
    page: Any,
    request_context: Any,
    url: str,
    check_url: UrlCheck,
    *,
    blocked: list[str],
    wait_until: str = "domcontentloaded",
    timeout: float = 30000,
    referer: str | None = None,
) -> Any:
    """异步版 :func:`safe_goto_sync`（Playwright async API）。返回 ``page.goto`` 响应。"""
    if request_context is None:
        raise _refuse(_NO_REQUEST_CONTEXT + "（按失败关闭拒绝导航）")
    probe = await resolve_redirect_chain_async(
        request_context,
        url,
        check_url,
        max_redirects=DEFAULT_MAX_REDIRECTS,
        timeout_ms=_preflight_timeout(timeout),
    )
    refusal = _preflight_refusal(probe)
    if refusal:
        raise _refuse(refusal)
    before = len(blocked)
    goto_kwargs = _goto_kwargs(url, probe, wait_until, timeout)
    if referer is not None:
        goto_kwargs["referer"] = referer
    try:
        resp = await page.goto(probe.url, **goto_kwargs)
    except Exception as exc:
        if len(blocked) > before:
            raise _refuse(f"重定向目标 {blocked[-1]!r} 不是公网地址") from exc
        raise
    refusal = _blocked_refusal(blocked, before)
    if refusal:
        raise UnsafeTargetError(refusal)
    refusal = await _landing_refusal_async(getattr(page, "url", ""), check_url)
    if refusal:
        raise UnsafeTargetError(refusal)
    return resp

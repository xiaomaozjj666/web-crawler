"""基于 ``curl_cffi`` TLS 指纹伪装的隐身 HTTP fetcher。

对齐 Scrapling 的 ``Fetcher`` / ``AsyncFetcher``：隐形 HTTP 抓取的主力。
``curl_cffi`` 重放真实浏览器的 TLS/JA3 指纹与 HTTP/2 帧顺序，让请求在
网络层与 Chrome 难以区分。``curl_cffi`` 不可用时，fetcher 透明降级到
``httpx``（附告警），同一 API 继续可用，只是没有指纹隐身能力。

后端导入（``curl_cffi`` / ``httpx``）推迟到 ``__init__`` 时机而非模块
导入时机，因此仅导入本模块不会强制加载可选依赖。

所有方法返回库级统一的 :class:`~web_crawler.response.Response`。
"""

from __future__ import annotations

import asyncio
import random
import time
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from typing_extensions import Self

from ..compat import HAS_CURL_CFFI, HAS_HTTPX
from ._base import BaseFetcher
from ._pin import env_proxy_configured, host_header_for, pin_url
from .proxy import ProxyPool

if TYPE_CHECKING:
    from ..parser.adaptive import AdaptiveStorage
    from ..response import Response


def _load_curl_backend() -> tuple[Any, Any, Any, Any]:
    """惰性导入 curl_cffi 符号（仅在选用 curl 后端时）。"""
    from curl_cffi import CurlHttpVersion
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    from curl_cffi.requests import Session as CurlSession
    from curl_cffi.requests.exceptions import RequestException as CurlRequestError

    return CurlHttpVersion, CurlSession, CurlAsyncSession, CurlRequestError


def _load_httpx_backend() -> Any:
    """惰性导入 httpx（仅在走兜底路径时）。"""
    import httpx

    return httpx


def _httpx_body(data: Any) -> tuple[Any, Any]:
    """httpx 兜底路径的 body 分流：新版 httpx 对 ``data=bytes/str`` 已发
    DeprecationWarning（建议改 ``content=``），而表单 Mapping 仍必须走
    ``data=``。按类型二选一，保证互斥且语义不变。"""
    if data is not None and not isinstance(data, Mapping):
        return data, None
    return None, data


def _parse_retry_after(value: str | None) -> float | None:
    """解析 HTTP ``Retry-After`` 响应头，返回等待秒数。

    支持两种形式（RFC 7231）：
    - 整数秒：``Retry-After: 120``
    - HTTP 日期：``Retry-After: Wed, 21 Oct 2026 07:28:00 GMT``
    无法解析时返回 ``None``。
    """
    if not value:
        return None
    value = value.strip()
    # 尝试整数秒
    try:
        seconds = float(value)
        if seconds >= 0:
            return min(seconds, 300.0)  # 上限 5 分钟，避免过长阻塞
    except ValueError:
        pass
    # 尝试 HTTP 日期
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(value)
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        delta = (dt - now).total_seconds()
        return max(0.0, min(delta, 300.0))
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(slots=True)
class _RedirectState:
    """手动重定向跟随的可变状态（sync/async 两个循环共用同一推进逻辑）。"""

    url: str
    method: str
    params: Any
    data: Any
    json: Any
    headers: dict[str, str]


class _FetcherCore(BaseFetcher):
    """:class:`Fetcher` 与 :class:`AsyncFetcher` 共享的会话/重试/请求头逻辑。

    同步与异步 fetcher 共享同一套配置、请求头合并、重试循环以及
    curl-vs-httpx 后端选择。该基类持有这些共享状态与辅助方法；两个具体
    子类只暴露各自的同步/异步公开 API。
    """

    def __init__(
        self,
        *,
        impersonate: str = "chrome131",
        timeout: float = 30.0,
        proxy: str | ProxyPool | None = None,
        retries: int = 0,
        adaptive: bool = False,
        storage: AdaptiveStorage | None = None,
        extra_headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
        verify: bool = True,
        http2: bool = True,
        max_redirects: int = 5,
        ja3_fingerprint: str | None = None,
        ja4_fingerprint: str | None = None,  # 兼容旧参数名（已弃用）
        allow_private_hosts: bool | None = None,
        resolve_hosts: bool = True,
    ) -> None:
        super().__init__(
            timeout=timeout,
            proxy=proxy,
            retries=retries,
            adaptive=adaptive,
            storage=storage,
            extra_headers=extra_headers,
            follow_redirects=follow_redirects,
            verify=verify,
            allow_private_hosts=allow_private_hosts,
            resolve_hosts=resolve_hosts,
        )
        if max_redirects < 0:
            raise ValueError("max_redirects must be >= 0")
        self.max_redirects = max_redirects
        self.impersonate = impersonate
        self.http2 = http2
        # JA3 指纹定制：传入时通过 curl_cffi 的 ja3 参数覆盖默认 TLS 扩展顺序。
        # curl_cffi 0.7+ 支持在 Session 构造时传 ja3 字符串做细粒度 TLS 指纹定制；
        # 若 curl_cffi 不可用则此字段被忽略（httpx 后端无 TLS 指纹能力）。
        if ja4_fingerprint is not None:
            warnings.warn(
                "ja4_fingerprint is deprecated; use ja3_fingerprint instead",
                DeprecationWarning,
                stacklevel=2,
            )
            if ja3_fingerprint is None:
                ja3_fingerprint = ja4_fingerprint
        self.ja3_fingerprint = ja3_fingerprint
        # 兼容旧属性名（读取旧代码仍可用）
        self.ja4_fingerprint = ja3_fingerprint
        # httpx http2=True 缺少可选依赖 h2 时降级为 HTTP/1.1，仅告警一次
        self._http2_fallback_warned = False

        # 后端在构造时一次性选定；会话惰性创建。
        self._use_curl: bool = HAS_CURL_CFFI
        if not self._use_curl and not HAS_HTTPX:  # pragma: no cover - defensive
            raise ImportError(
                "Neither curl_cffi nor httpx is installed; Fetcher cannot operate. "
                "Install one with: pip install curl_cffi"
            )
        if not self._use_curl:
            warnings.warn(
                "curl_cffi is not installed; falling back to httpx. "
                "TLS-fingerprint impersonation is disabled — requests may be "
                "more easily detected. Install curl_cffi for full stealth.",
                RuntimeWarning,
                stacklevel=2,
            )

        # 惰性创建的会话（同步 / 异步）。首次使用前均为 None。
        self._session: Any = None
        self._async_session: Any = None
        # 代理路径的专用 httpx 客户端（按 proxy 字符串缓存）。原实现每次
        # 请求都重建客户端——重定向每一跳、每次重试都全新 TLS 握手，零
        # 连接复用；缓存后与无代理会话同样复用连接池，close()/aclose() 统一释放。
        self._proxied_sync_clients: dict[str, Any] = {}
        self._proxied_async_clients: dict[str, Any] = {}

    # -- 会话构建（后端导入延迟） ---------------------------------------------
    def _curl_session_kwargs(self) -> dict[str, Any]:
        """curl_cffi 会话公共参数（sync/async 构建器共用，防双路径分叉）。"""
        kwargs: dict[str, Any] = {
            "impersonate": self.impersonate,
            "verify": self.verify,
            "timeout": self.timeout,
            "allow_redirects": self.follow_redirects,
        }
        if self.ja3_fingerprint:
            # curl_cffi 的 ja3 参数接受 JA3 格式的 TLS 扩展字符串，
            # 覆盖 impersonate 预设的默认 TLS 指纹。
            kwargs["ja3"] = self.ja3_fingerprint
        return kwargs

    def _build_curl_sync_session(self) -> Any:
        CurlHttpVersion, CurlSession, _, _ = _load_curl_backend()
        kwargs = self._curl_session_kwargs()
        if not self.http2:
            kwargs["http_version"] = CurlHttpVersion.V1_1
        return CurlSession(**kwargs)

    def _build_curl_async_session(self) -> Any:
        CurlHttpVersion, _, CurlAsyncSession, _ = _load_curl_backend()
        kwargs = self._curl_session_kwargs()
        if not self.http2:
            kwargs["http_version"] = CurlHttpVersion.V1_1
        return CurlAsyncSession(**kwargs)

    def _httpx_transport_kwargs(
        self, proxy: str | None, *, is_async: bool
    ) -> tuple[Any, dict[str, Any]]:
        """返回 ``(httpx 模块, client kwargs)``，代理挂载策略 sync/async 共用。"""
        httpx = _load_httpx_backend()
        kwargs: dict[str, Any] = {
            "http2": self.http2,
            "follow_redirects": self.follow_redirects,
            "verify": self.verify,
            "timeout": self.timeout,
        }
        if proxy:
            # httpx >= 0.28 弃用 ``proxy=`` 改为 ``mounts=``；
            # 有对应 Transport 就用它，老版本 httpx 走旧参数。
            if is_async and hasattr(httpx, "AsyncHTTPTransport"):
                kwargs["mounts"] = {"all://": httpx.AsyncHTTPTransport(proxy=proxy)}
            elif hasattr(httpx, "HTTPTransport"):
                kwargs["mounts"] = {"all://": httpx.HTTPTransport(proxy=proxy)}
            else:
                kwargs["proxy"] = proxy
        return httpx, kwargs

    def _build_httpx_client(self, factory: Any, kwargs: dict[str, Any]) -> Any:
        """按 kwargs 构建客户端；http2=True 缺 h2 时降级 HTTP/1.1（仅告警一次）。"""
        try:
            return factory(**kwargs)
        except ImportError:
            if not self._http2_fallback_warned:
                warnings.warn(
                    "httpx HTTP/2 support requires the optional 'h2' package; "
                    "falling back to HTTP/1.1.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                self._http2_fallback_warned = True
            kwargs["http2"] = False
            return factory(**kwargs)

    def _build_httpx_sync_client(self, proxy: str | None = None) -> Any:
        httpx, kwargs = self._httpx_transport_kwargs(proxy, is_async=False)
        return self._build_httpx_client(httpx.Client, kwargs)

    def _build_httpx_async_client(self, proxy: str | None = None) -> Any:
        httpx, kwargs = self._httpx_transport_kwargs(proxy, is_async=True)
        return self._build_httpx_client(httpx.AsyncClient, kwargs)

    def _ensure_sync_session(self) -> Any:
        if self._session is None:
            self._session = (
                self._build_curl_sync_session()
                if self._use_curl
                else self._build_httpx_sync_client()
            )
        return self._session

    def _ensure_async_session(self) -> Any:
        if self._async_session is None:
            self._async_session = (
                self._build_curl_async_session()
                if self._use_curl
                else self._build_httpx_async_client()
            )
        return self._async_session

    def _ensure_proxied_sync_client(self, proxy: str) -> Any:
        """代理路径的 httpx 同步客户端（按 proxy 缓存复用）。"""
        client = self._proxied_sync_clients.get(proxy)
        if client is None:
            client = self._build_httpx_sync_client(proxy)
            self._proxied_sync_clients[proxy] = client
        return client

    def _ensure_proxied_async_client(self, proxy: str) -> Any:
        """代理路径的 httpx 异步客户端（按 proxy 缓存复用）。"""
        client = self._proxied_async_clients.get(proxy)
        if client is None:
            client = self._build_httpx_async_client(proxy)
            self._proxied_async_clients[proxy] = client
        return client

    # -- 请求头合并 -----------------------------------------------------------
    def _merge_headers(self, per_request: dict[str, str] | None) -> dict[str, str]:
        """合并请求头且不破坏 curl_cffi 伪装指纹。

        curl_cffi 的 ``impersonate`` 已注入与所选 TLS 指纹匹配的完整浏览器
        请求头，因此 curl 路径只在其上叠加用户显式传入的请求头。httpx 兜底
        路径没有指纹，改用仿真默认请求头作为基底。
        """
        if self._use_curl:
            merged: dict[str, str] = dict(self.extra_headers)
        else:
            merged = self._default_headers()
            merged.update(self.extra_headers)
            self._narrow_accept_encoding(merged)
        if per_request:
            merged.update(per_request)
        return merged

    @staticmethod
    def _narrow_accept_encoding(headers: dict[str, str]) -> None:
        """按 httpx 运行时实际可解压的编码收缩 ``Accept-Encoding``。

        仿真默认头广告了 ``br``/``zstd``；curl 原生支持这些编码，但 httpx 的
        brotli/zstd 解码依赖可选包——不装包却广告 ``br``，服务器就会返回
        httpx 解不开的 brotli body（实测复现）。广告能力必须以解码能力为准。
        """
        value = headers.get("Accept-Encoding")
        if not value:
            return
        decodable = {"gzip", "deflate", "identity"}
        try:
            import brotli  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            pass
        else:
            decodable.add("br")
        try:
            import zstandard  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            pass
        else:
            decodable.add("zstd")
        kept = [e.strip() for e in value.split(",") if e.strip().lower() in decodable]
        if kept:
            headers["Accept-Encoding"] = ", ".join(kept)
        else:
            headers.pop("Accept-Encoding", None)

    def _retry_errors(self) -> tuple[type[BaseException], ...]:
        if self._use_curl:
            _, _, _, CurlRequestError = _load_curl_backend()
            return (CurlRequestError, OSError)
        if HAS_HTTPX:
            httpx = _load_httpx_backend()
            return (httpx.HTTPError, OSError)
        return ()  # pragma: no cover - defensive

    # -- 响应转换 -------------------------------------------------------------
    def _to_response(self, raw: Any, request_headers: dict[str, str]) -> Response:
        # 运行时导入 Response 以避免模块加载期的循环导入
        from ..response import Response

        return Response(
            url=str(raw.url),
            status=int(raw.status_code),
            content=raw.content,
            headers=dict(raw.headers),
            request_headers=request_headers,
            storage=self.storage,
            adaptive=self.adaptive,
        )

    # -- TOCTOU 钉扎（httpx 路径；sync/async 发送共用） -----------------------
    def _pin_target(
        self, url: str, proxy: str | None
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """返回 ``(请求 URL, 附加头, 扩展)``；不可钉时原样返回。

        仅在"直连 + DNS 复查开启"时生效（显式/环境代理由代理解析、IP 字面量
        无解析步骤、allow_private_hosts 下钉扎无意义）。钉扎成功时用显式
        ``Host`` 头与 ``sni_hostname`` 扩展保持原始主机名的路由与证书校验
        语义（见 fetchers/_pin.py 模块文档）。
        """
        if (
            proxy is not None
            or env_proxy_configured()
            or not self.resolve_hosts
            or self.allow_private_hosts
        ):
            return url, {}, {}
        parsed = urlparse(url)
        pinned = pin_url(url)
        if pinned == url:
            return url, {}, {}
        extra_headers = {"Host": host_header_for(url)}
        extensions: dict[str, Any] = {}
        if parsed.hostname:
            extensions = {"sni_hostname": parsed.hostname}
        return pinned, extra_headers, extensions

    # -- 共享异步传输（Fetcher 与 AsyncFetcher 共用） -------------------------
    async def _send_once_async(
        self,
        method: str,
        url: str,
        params: Any,
        data: Any,
        json: Any,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
        allow_redirects: bool,
        verify: bool,
    ) -> Any:
        if self._use_curl:
            session = self._ensure_async_session()
            return await session.request(
                method=method,
                url=url,
                params=params,
                data=data,
                json=json,
                headers=headers,
                proxy=proxy,
                timeout=timeout,
                allow_redirects=allow_redirects,
                verify=verify,
            )
        # httpx 异步兜底：无代理复用共享会话；代理客户端按 proxy 缓存复用
        client = (
            self._ensure_async_session()
            if proxy is None
            else self._ensure_proxied_async_client(proxy)
        )
        request_url, extra_headers, extensions = self._pin_target(url, proxy)
        if extra_headers:
            headers = {**headers, **extra_headers}
        content, data_arg = _httpx_body(data)
        return await client.request(
            method=method,
            url=request_url,
            params=params,
            content=content,
            data=data_arg,
            json=json,
            headers=headers,
            timeout=timeout,
            follow_redirects=allow_redirects,
            extensions=extensions,
        )

    # -- 重定向跟随（逐跳 SSRF scheme 校验） ----------------------------------
    def _next_redirect(
        self, raw: Any, current_url: str, method: str, headers: dict[str, str]
    ) -> tuple[str, str | None, dict[str, str]] | None:
        """解析重定向跳转目标；返回 (next_url, new_method, new_headers)。

        逐跳校验目标 scheme（重定向到非 http(s) 即拒绝），并在跨源跳转时
        剥离 Authorization 头，避免凭据泄漏给第三方站点。非重定向状态或
        缺少 Location 时返回 ``None``。
        """
        status = int(raw.status_code)
        if status not in (301, 302, 303, 307, 308):
            return None
        location = raw.headers.get("Location") if raw.headers else None
        if not location:
            return None
        next_url = urljoin(current_url, location.strip())
        self._validate_target(next_url)
        next_headers = headers
        if urlparse(next_url).netloc != urlparse(current_url).netloc:
            next_headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
        # 303 恒转 GET；301/302 的 POST 也转 GET（与浏览器/curl 默认一致）
        new_method: str | None = None
        if status == 303 or (status in (301, 302) and method == "POST"):
            new_method = "GET"
        return next_url, new_method, next_headers

    def _advance_redirect(self, state: _RedirectState, raw: Any) -> bool:
        """按 ``raw`` 的重定向响应推进状态机；非重定向返回 False。

        与历史行为逐字一致：params 在任意跳转后清空；303 恒转 GET、
        301/302 的 POST 转 GET 时清空 body；跨源跳转由 :meth:`_next_redirect`
        剥离 Authorization（headers 以返回的新引用为准，不原位修改）。
        """
        next_hop = self._next_redirect(raw, state.url, state.method, state.headers)
        if next_hop is None:
            return False
        next_url, new_method, next_headers = next_hop
        state.url = next_url
        state.params = None
        state.headers = next_headers
        if new_method is not None:
            state.method = new_method
            state.data = None
            state.json = None
        return True

    async def _send_with_redirects_async(
        self,
        method: str,
        url: str,
        params: Any,
        data: Any,
        json: Any,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
        allow_redirects: bool,
        verify: bool,
    ) -> Any:
        """发送一次请求并手动跟随重定向（最多 max_redirects 跳，逐跳校验 scheme）。"""
        if not allow_redirects:
            return await self._send_once_async(
                method, url, params, data, json, headers, proxy, timeout, False, verify
            )
        state = _RedirectState(url, method, params, data, json, headers)
        for _ in range(self.max_redirects + 1):
            raw = await self._send_once_async(
                state.method,
                state.url,
                state.params,
                state.data,
                state.json,
                state.headers,
                proxy,
                timeout,
                False,
                verify,
            )
            if not self._advance_redirect(state, raw):
                return raw
        raise RuntimeError(f"too many redirects (max {self.max_redirects}) for {url}")

    async def _send_async(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json: Any = None,
        **kwargs: Any,
    ) -> Response:
        self._validate_target(url)
        merged_headers = self._merge_headers(headers)
        timeout = kwargs.pop("timeout", self.timeout)
        verify = kwargs.pop("verify", self.verify)
        allow_redirects = kwargs.pop("allow_redirects", self.follow_redirects)
        if kwargs:
            raise TypeError(f"unexpected keyword argument(s): {', '.join(sorted(kwargs))}")
        proxy = self._resolve_proxy()
        retry_errors = self._retry_errors()
        last_exc: BaseException | None = None
        for attempt in range(self.retries + 1):
            backoff = min(2.0**attempt, 10.0) + random.random() * 0.25
            try:
                raw = await self._send_with_redirects_async(
                    method,
                    url,
                    params,
                    data,
                    json,
                    merged_headers,
                    proxy,
                    timeout,
                    allow_redirects,
                    verify,
                )
            except retry_errors as exc:
                last_exc = exc
                if attempt == self.retries:
                    raise
                # 连接错误/超时：若有代理池则标记当前代理失败并轮换，避免死代理原地重试
                if isinstance(self.proxy, ProxyPool) and proxy:
                    self.proxy.mark_failed(proxy)
                    proxy = self._resolve_proxy()
                await asyncio.sleep(backoff)
                continue
            # 5xx 与 429（被限流）均重试；其余直接返回
            should_retry = raw.status_code >= 500 or raw.status_code == 429
            if not should_retry:
                # 成功响应：清零该代理的失败计数
                if isinstance(self.proxy, ProxyPool) and proxy:
                    self.proxy.mark_success(proxy)
                return self._to_response(raw, merged_headers)
            if attempt == self.retries:
                return self._to_response(raw, merged_headers)
            # 429/5xx 视为代理问题信号：标记失败并换下一个
            if isinstance(self.proxy, ProxyPool) and proxy:
                self.proxy.mark_failed(proxy)
                proxy = self._resolve_proxy()
            # 429 时尊重 Retry-After；否则指数退避
            delay = _parse_retry_after(raw.headers.get("Retry-After")) or backoff
            await asyncio.sleep(delay)
        if last_exc is not None:  # pragma: no cover - 重试循环在最后一次必定 return 或 raise
            raise last_exc
        raise RuntimeError(
            f"request to {url} failed without a captured exception"
        )  # pragma: no cover


class Fetcher(_FetcherCore):
    """使用 ``curl_cffi`` TLS 伪装的隐身 HTTP fetcher（同步）。

    安装了 ``curl_cffi`` 时（默认预期）持有 :class:`curl_cffi.requests.Session`
    并伪装成真实浏览器。``curl_cffi`` 缺失时回退到 ``httpx`` 并发出告警，
    让调用者知道指纹隐身已禁用。

    本类同时暴露异步方法（``async_get`` / ``async_request``），单个实例
    即可同时服务同步与异步调用方。若需要纯异步 API 面，请使用
    :class:`AsyncFetcher`。

    Parameters
    ----------
    impersonate:
        要伪装的 ``curl_cffi`` 浏览器指纹（默认 ``"chrome131"``）。
    http2:
        启用 HTTP/2（默认 ``True``）。
    max_redirects:
        手动跟随重定向的最大跳数（默认 ``5``）。每一跳都会重新校验 URL
        scheme（SSRF 防护），跨源跳转会剥离 ``Authorization`` 请求头。
    ja3_fingerprint:
        可选的 JA3 TLS 指纹字符串，用于覆盖伪装预设（如自定义加密套件/
        扩展顺序）。仅 ``curl_cffi`` 后端使用；回退 ``httpx`` 时忽略。
    """

    # -- 同步传输 -------------------------------------------------------------
    def _send_once_sync(
        self,
        method: str,
        url: str,
        params: Any,
        data: Any,
        json: Any,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
        allow_redirects: bool,
        verify: bool,
    ) -> Any:
        if self._use_curl:
            session = self._ensure_sync_session()
            return session.request(
                method=method,
                url=url,
                params=params,
                data=data,
                json=json,
                headers=headers,
                proxy=proxy,
                timeout=timeout,
                allow_redirects=allow_redirects,
                verify=verify,
            )
        # httpx 兜底：无代理复用共享会话；代理客户端按 proxy 缓存复用
        client = (
            self._ensure_sync_session()
            if proxy is None
            else self._ensure_proxied_sync_client(proxy)
        )
        request_url, extra_headers, extensions = self._pin_target(url, proxy)
        if extra_headers:
            headers = {**headers, **extra_headers}
        content, data_arg = _httpx_body(data)
        return client.request(
            method=method,
            url=request_url,
            params=params,
            content=content,
            data=data_arg,
            json=json,
            headers=headers,
            timeout=timeout,
            follow_redirects=allow_redirects,
            extensions=extensions,
        )

    def _send_with_redirects_sync(
        self,
        method: str,
        url: str,
        params: Any,
        data: Any,
        json: Any,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
        allow_redirects: bool,
        verify: bool,
    ) -> Any:
        """发送一次请求并手动跟随重定向（最多 max_redirects 跳，逐跳校验 scheme）。"""
        if not allow_redirects:
            return self._send_once_sync(
                method, url, params, data, json, headers, proxy, timeout, False, verify
            )
        state = _RedirectState(url, method, params, data, json, headers)
        for _ in range(self.max_redirects + 1):
            raw = self._send_once_sync(
                state.method,
                state.url,
                state.params,
                state.data,
                state.json,
                state.headers,
                proxy,
                timeout,
                False,
                verify,
            )
            if not self._advance_redirect(state, raw):
                return raw
        raise RuntimeError(f"too many redirects (max {self.max_redirects}) for {url}")

    def _send_sync(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json: Any = None,
        **kwargs: Any,
    ) -> Response:
        self._validate_target(url)
        merged_headers = self._merge_headers(headers)
        timeout = kwargs.pop("timeout", self.timeout)
        verify = kwargs.pop("verify", self.verify)
        allow_redirects = kwargs.pop("allow_redirects", self.follow_redirects)
        if kwargs:
            raise TypeError(f"unexpected keyword argument(s): {', '.join(sorted(kwargs))}")
        proxy = self._resolve_proxy()
        retry_errors = self._retry_errors()
        last_exc: BaseException | None = None
        for attempt in range(self.retries + 1):
            backoff = min(2.0**attempt, 10.0) + random.random() * 0.25
            try:
                raw = self._send_with_redirects_sync(
                    method,
                    url,
                    params,
                    data,
                    json,
                    merged_headers,
                    proxy,
                    timeout,
                    allow_redirects,
                    verify,
                )
            except retry_errors as exc:
                last_exc = exc
                if attempt == self.retries:
                    raise
                # 连接错误/超时：若有代理池则标记当前代理失败并轮换，避免死代理原地重试
                if isinstance(self.proxy, ProxyPool) and proxy:
                    self.proxy.mark_failed(proxy)
                    proxy = self._resolve_proxy()
                time.sleep(backoff)
                continue
            # 5xx 与 429（被限流）均重试；其余直接返回
            should_retry = raw.status_code >= 500 or raw.status_code == 429
            if not should_retry:
                # 成功响应：清零该代理的失败计数
                if isinstance(self.proxy, ProxyPool) and proxy:
                    self.proxy.mark_success(proxy)
                return self._to_response(raw, merged_headers)
            if attempt == self.retries:
                return self._to_response(raw, merged_headers)
            # 429/5xx 视为代理问题信号：标记失败并换下一个
            if isinstance(self.proxy, ProxyPool) and proxy:
                self.proxy.mark_failed(proxy)
                proxy = self._resolve_proxy()
            # 429 时尊重 Retry-After；否则指数退避
            delay = _parse_retry_after(raw.headers.get("Retry-After")) or backoff
            time.sleep(delay)
        if last_exc is not None:  # pragma: no cover - 重试循环在最后一次必定 return 或 raise
            raise last_exc
        raise RuntimeError(
            f"request to {url} failed without a captured exception"
        )  # pragma: no cover

    # -- 公开同步 API ----------------------------------------------------------
    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        """以 ``method`` 发送请求并返回 :class:`Response`。"""
        return self._send_sync(method, url, **kwargs)

    def get(
        self, url: str, *, params: Any = None, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Response:
        kwargs.setdefault("params", params)
        kwargs.setdefault("headers", headers)
        return self.request("GET", url, **kwargs)

    def post(
        self,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json: Any = None,
        **kwargs: Any,
    ) -> Response:
        kwargs.setdefault("params", params)
        kwargs.setdefault("headers", headers)
        kwargs.setdefault("data", data)
        kwargs.setdefault("json", json)
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Response:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Response:
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> Response:
        return self.request("HEAD", url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> Response:
        return self.request("OPTIONS", url, **kwargs)

    # -- 公开异步 API ----------------------------------------------------------
    async def async_request(self, method: str, url: str, **kwargs: Any) -> Response:
        """异步以 ``method`` 发送请求并返回 :class:`Response`。"""
        return await self._send_async(method, url, **kwargs)

    async def async_get(self, url: str, **kwargs: Any) -> Response:
        return await self.async_request("GET", url, **kwargs)

    async def async_post(self, url: str, **kwargs: Any) -> Response:
        return await self.async_request("POST", url, **kwargs)

    # -- 生命周期 --------------------------------------------------------------
    def close(self) -> None:
        """关闭底层同步会话。

        异步会话无法在同步上下文中安全关闭（需要事件循环），请使用 ``aclose()``
        或 ``async with`` 上下文管理器来清理异步会话资源。
        """
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
        for client in self._proxied_sync_clients.values():
            try:
                client.close()
            except Exception:
                pass
        self._proxied_sync_clients.clear()
        # 异步 session 不在这里强行关闭，避免在无事件循环时抛 RuntimeError；
        # 保留引用（不置 None），之后仍可 aclose()，由 GC 兜底释放连接池
        if self._async_session is not None:
            warnings.warn(
                "Fetcher.close() 跳过了异步会话的关闭；请使用 await fetcher.aclose() "
                "或 ``async with Fetcher(...)`` 来正确释放异步资源。",
                ResourceWarning,
                stacklevel=2,
            )

    async def aclose(self) -> None:
        """异步关闭同步与异步会话。"""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
        if self._async_session is not None:
            try:
                await self._async_session.close()
            except Exception:
                pass
            self._async_session = None
        for client in self._proxied_sync_clients.values():
            try:
                client.close()
            except Exception:
                pass
        self._proxied_sync_clients.clear()
        for client in self._proxied_async_clients.values():
            try:
                await client.aclose()
            except Exception:
                pass
        self._proxied_async_clients.clear()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


class AsyncFetcher(_FetcherCore):
    """纯异步隐身 HTTP fetcher（对齐 Scrapling 的 ``AsyncFetcher``）。

    与 :class:`Fetcher` 共享全部配置、后端选择与重试逻辑，但**只**暴露
    异步 API。没有同步 ``get``/``post`` 方法，意图明确且绝不创建同步会话。

    异步应用中使用它，可避免同步调用意外阻塞事件循环。
    """

    # -- 公开异步 API ----------------------------------------------------------
    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        """异步以 ``method`` 发送请求并返回 :class:`Response`。"""
        return await self._send_async(method, url, **kwargs)

    async def get(
        self, url: str, *, params: Any = None, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Response:
        kwargs.setdefault("params", params)
        kwargs.setdefault("headers", headers)
        return await self.request("GET", url, **kwargs)

    async def post(
        self,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json: Any = None,
        **kwargs: Any,
    ) -> Response:
        kwargs.setdefault("params", params)
        kwargs.setdefault("headers", headers)
        kwargs.setdefault("data", data)
        kwargs.setdefault("json", json)
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> Response:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> Response:
        return await self.request("DELETE", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> Response:
        return await self.request("HEAD", url, **kwargs)

    async def options(self, url: str, **kwargs: Any) -> Response:
        return await self.request("OPTIONS", url, **kwargs)

    # -- 生命周期 --------------------------------------------------------------
    async def aclose(self) -> None:
        """异步关闭异步会话（同步会话从不创建）。"""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
        if self._async_session is not None:
            try:
                await self._async_session.close()
            except Exception:
                pass
            self._async_session = None
        for client in self._proxied_async_clients.values():
            try:
                await client.aclose()
            except Exception:
                pass
        self._proxied_async_clients.clear()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


__all__ = ["AsyncFetcher", "Fetcher"]

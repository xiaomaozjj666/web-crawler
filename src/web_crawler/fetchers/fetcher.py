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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from typing_extensions import Self

from ..compat import HAS_CURL_CFFI, HAS_HTTPX
from ..exceptions import ConfigError, FetchError
from ._base import BaseFetcher
from ._download import StreamContext, download_async, download_sync
from ._pin import (
    CurlResolvePinner,
    apply_curl_resolve,
    env_proxy_configured,
    host_header_for,
    pin_url,
)
from .proxy import ProxyPool

if TYPE_CHECKING:
    from ..parser.adaptive import AdaptiveStorage
    from ..response import Response
    from .session import SessionHandle, SessionPool


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


# httpx 兜底路径实际可解压编码的探测结果（惰性一次，模块级缓存）。
# 失败 import 的代价是全 sys.path 遍历（实测每次 ~1.7ms）——逐请求重复
# 探测会把 _merge_headers 推到毫秒级；而已装包集合在进程内不变，探测
# 一次即够。None 表示尚未探测。
_DECODABLE_ENCODINGS: frozenset[str] | None = None


def _decodable_encodings() -> frozenset[str]:
    """返回 httpx 运行时实际可解压的编码集合（首次调用探测一次后缓存）。"""
    global _DECODABLE_ENCODINGS
    if _DECODABLE_ENCODINGS is None:
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
        _DECODABLE_ENCODINGS = frozenset(decodable)
    return _DECODABLE_ENCODINGS


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
        impersonate: str | None = "chrome131",
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
        session_pool: SessionPool | None = None,
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
            raise ConfigError("max_redirects must be >= 0")
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
        # 可选会话池（见 fetchers/session.py）：启用时每次请求借还池化会话
        # ——指纹随会话轮换、空闲回收、失败淘汰；None（默认）走上面的单会话
        # 路径，行为与历史版本完全一致。传入时自动把本 fetcher 的会话构建
        # 方法绑进池（first-wins，见 SessionPool.bind_factories），池化会话
        # 继承本 fetcher 的超时/校验/HTTP2/JA3 等全部配置。
        self.session_pool = session_pool
        if session_pool is not None:
            session_pool.bind_factories(
                session_factory=self._pooled_sync_session_factory,
                async_session_factory=self._pooled_async_session_factory,
            )
        # curl_cffi 路径的 DNS 钉扎器（CURLOPT_RESOLVE 条目 LRU；httpx 路径
        # 走 _pin_target 的 URL 改写方案，两条路径共用 _pin.py 的即时解析校验）。
        self._curl_pinner = CurlResolvePinner()
        # 代理路径的专用 httpx 客户端（按 proxy 字符串缓存）。原实现每次
        # 请求都重建客户端——重定向每一跳、每次重试都全新 TLS 握手，零
        # 连接复用；缓存后与无代理会话同样复用连接池，close()/aclose() 统一释放。
        self._proxied_sync_clients: dict[str, Any] = {}
        self._proxied_async_clients: dict[str, Any] = {}
        # 重试异常元组的实例级缓存（_retry_errors 惰性填充）：后端符号
        # 同一进程内不变，逐请求重复加载只付无谓的 import 开销
        self._retry_errors_cache: tuple[type[BaseException], ...] | None = None

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

    def _build_curl_sync_session(self, impersonate: str | None = None) -> Any:
        """构建 curl_cffi 同步会话；``impersonate`` 非 None 时覆盖默认指纹。

        覆盖入参只被 SessionPool 的轮换指纹使用——普通路径不传，行为不变。
        """
        CurlHttpVersion, CurlSession, _, _ = _load_curl_backend()
        kwargs = self._curl_session_kwargs()
        if impersonate is not None:
            kwargs["impersonate"] = impersonate
        if not self.http2:
            kwargs["http_version"] = CurlHttpVersion.V1_1
        return CurlSession(**kwargs)

    def _build_curl_async_session(self, impersonate: str | None = None) -> Any:
        """构建 curl_cffi 异步会话；``impersonate`` 非 None 时覆盖默认指纹。"""
        CurlHttpVersion, _, CurlAsyncSession, _ = _load_curl_backend()
        kwargs = self._curl_session_kwargs()
        if impersonate is not None:
            kwargs["impersonate"] = impersonate
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

    # -- 池化会话借还（session_pool 启用时；默认 None 不走这里） ---------------
    def _pooled_sync_session_factory(self, impersonate: str | None) -> Any:
        """SessionPool 的同步会话工厂：fetcher 全套配置 + 池轮换的指纹。"""
        if self._use_curl:
            return self._build_curl_sync_session(impersonate)
        # httpx 兜底无 TLS 指纹能力：忽略指纹维度，仅保留连接池化收益
        return self._build_httpx_sync_client()

    def _pooled_async_session_factory(self, impersonate: str | None) -> Any:
        """SessionPool 的异步会话工厂（同 :meth:`_pooled_sync_session_factory`）。"""
        if self._use_curl:
            return self._build_curl_async_session(impersonate)
        return self._build_httpx_async_client()

    def _acquire_pooled(self, proxy: str | None) -> SessionHandle | None:
        """借出池化同步会话；未启用或 httpx 兜底走代理时不适用（返回 None）。

        httpx 的代理挂载是客户端构造级配置（mounts），而代理按请求轮换
        （ProxyPool），池化客户端无法跟随轮换——该组合退回按 proxy 缓存的
        原路径。curl_cffi 的 proxy 是请求级参数，不受此限制。
        """
        pool = self.session_pool
        if pool is None or (not self._use_curl and proxy is not None):
            return None
        return pool.acquire()

    async def _acquire_pooled_async(self, proxy: str | None) -> SessionHandle | None:
        """异步版 :meth:`_acquire_pooled`（语义一致）。"""
        pool = self.session_pool
        if pool is None or (not self._use_curl and proxy is not None):
            return None
        return await pool.acquire_async()

    def _release_pooled(self, handle: SessionHandle | None, *, success: bool) -> None:
        """归还池化同步会话；请求成败决定其健康计数（连续失败会被池淘汰）。"""
        pool = self.session_pool
        if handle is not None and pool is not None:
            pool.release(handle, success=success)

    async def _release_pooled_async(self, handle: SessionHandle | None, *, success: bool) -> None:
        """异步版 :meth:`_release_pooled`。"""
        pool = self.session_pool
        if handle is not None and pool is not None:
            await pool.release_async(handle, success=success)

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

        解码能力探测经 :func:`_decodable_encodings` 进程内只做一次（失败
        import 需全 sys.path 遍历，逐请求重复探测在毫秒级）；收缩逻辑本身
        每次对传入 headers 原地执行，语义与逐次探测完全一致。
        """
        value = headers.get("Accept-Encoding")
        if not value:
            return
        decodable = _decodable_encodings()
        kept = [e.strip() for e in value.split(",") if e.strip().lower() in decodable]
        if kept:
            headers["Accept-Encoding"] = ", ".join(kept)
        else:
            headers.pop("Accept-Encoding", None)

    def _retry_errors(self) -> tuple[type[BaseException], ...]:
        """返回重试循环要捕获的异常元组（实例级缓存，首次请求时填充）。

        后端选型在构造时定死（``_use_curl``）、符号在进程内不变，缓存只
        免掉逐请求的后端符号重复加载；测试以「patch 后端加载器 + 新建
        实例」的顺序注入 fake 时，首次调用仍走注入路径，语义不变。
        """
        cached = self._retry_errors_cache
        if cached is not None:
            return cached
        if self._use_curl:
            _, _, _, CurlRequestError = _load_curl_backend()
            errors: tuple[type[BaseException], ...] = (CurlRequestError, OSError)
        elif HAS_HTTPX:
            httpx = _load_httpx_backend()
            errors = (httpx.HTTPError, OSError)
        else:  # pragma: no cover - defensive
            errors = ()
        self._retry_errors_cache = errors
        return errors

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

    # -- TOCTOU 钉扎（httpx / curl_cffi 双路径；sync/async 发送共用） --------
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

    def _pin_curl_session(self, session: Any, url: str, proxy: str | None) -> None:
        """curl_cffi 路径的连接级 DNS 钉扎（CURLOPT_RESOLVE）。

        与 :meth:`_pin_target` 同一生效条件（直连 + DNS 复查开启）：先绕过
        60s 判定缓存**即时解析并逐 IP 复核**（任一结果落入拒绝段即抛
        :class:`ValueError` 中止请求），再把全族已校验 IP 钉给 libcurl——
        连接强制使用已校验地址，"校验的那个 IP"就是"连接的那个 IP"。
        URL / Host 头 / SNI / 证书校验保持原始主机名，无需改写。

        curl_cffi 过旧或缺失时 :func:`apply_curl_resolve` 返回 False 放弃
        钉扎（降级），但上面的即时解析校验仍然生效——TOCTOU 窗口已从
        60s 判定缓存缩到单次请求内（残余风险见 _pin.py 模块文档）。
        """
        if (
            proxy is not None
            or env_proxy_configured()
            or not self.resolve_hosts
            or self.allow_private_hosts
        ):
            return
        entries = self._curl_pinner.entries_for(url)
        if entries:
            apply_curl_resolve(session, entries)

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
            # 会话池启用时借出池化会话（指纹随会话轮换），否则用惰性单会话
            handle = await self._acquire_pooled_async(proxy)
            try:
                session = handle.session if handle is not None else self._ensure_async_session()
                # 连接级 DNS 钉扎（CURLOPT_RESOLVE）：DNS 翻转到私网时抛
                # ValueError 即时中止，否则 libcurl 强制用已校验 IP 连接
                self._pin_curl_session(session, url, proxy)
                raw = await session.request(
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
            except BaseException:
                await self._release_pooled_async(handle, success=False)
                raise
            await self._release_pooled_async(handle, success=True)
            return raw
        # httpx 异步兜底：无代理复用共享会话（或池化会话）；代理客户端按 proxy 缓存复用
        handle = await self._acquire_pooled_async(proxy)
        try:
            client = (
                (handle.session if handle is not None else self._ensure_async_session())
                if proxy is None
                else self._ensure_proxied_async_client(proxy)
            )
            request_url, extra_headers, extensions = self._pin_target(url, proxy)
            if extra_headers:
                headers = {**headers, **extra_headers}
            content, data_arg = _httpx_body(data)
            raw = await client.request(
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
        except BaseException:
            await self._release_pooled_async(handle, success=False)
            raise
        await self._release_pooled_async(handle, success=True)
        return raw

    # -- 共享异步流式原语（download 用；Fetcher 与 AsyncFetcher 共用） --------
    async def _stream_once_async(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
        verify: bool,
    ) -> StreamContext:
        """打开异步流式请求（``stream=True``，不消费 body），返回统一包装。

        与 :meth:`_send_once_async` 走完全相同的校验链（会话池借出、DNS
        钉扎、代理客户端选择），仅两处差异：响应 body 保持未读状态由调用
        方分块消费；池化句柄**不**在请求返回时归还——归还延迟到流关闭
        （:meth:`StreamContext.aclose <web_crawler.fetchers._download.StreamContext.aclose>`），
        流式读取期间会话保持独占。重定向由 :meth:`_stream_with_redirects_async`
        手动跟随（恒以 ``allow_redirects=False`` 发送，逐跳校验目标）。
        """
        if self._use_curl:
            handle = await self._acquire_pooled_async(proxy)
            try:
                session = handle.session if handle is not None else self._ensure_async_session()
                # 连接级 DNS 钉扎（CURLOPT_RESOLVE）：DNS 翻转到私网时抛
                # ValueError 即时中止，否则 libcurl 强制用已校验 IP 连接
                self._pin_curl_session(session, url, proxy)
                raw = await session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    proxy=proxy,
                    timeout=timeout,
                    allow_redirects=False,
                    verify=verify,
                    stream=True,
                )
            except BaseException:
                await self._release_pooled_async(handle, success=False)
                raise
            return StreamContext(raw, handle, fetcher=self, use_curl=True)
        # httpx 异步兜底：无代理复用共享会话（或池化会话）；代理客户端按 proxy 缓存复用
        handle = await self._acquire_pooled_async(proxy)
        try:
            client = (
                (handle.session if handle is not None else self._ensure_async_session())
                if proxy is None
                else self._ensure_proxied_async_client(proxy)
            )
            request_url, extra_headers, extensions = self._pin_target(url, proxy)
            if extra_headers:
                headers = {**headers, **extra_headers}
            raw = await client.request(
                method=method,
                url=request_url,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
                extensions=extensions,
                stream=True,
            )
        except BaseException:
            await self._release_pooled_async(handle, success=False)
            raise
        return StreamContext(raw, handle, fetcher=self, use_curl=False)

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
        raise FetchError(f"too many redirects (max {self.max_redirects}) for {url}")

    async def _stream_with_redirects_async(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
        verify: bool,
    ) -> StreamContext:
        """打开异步流式请求并手动跟随重定向（每跳校验 scheme/host）。

        对齐 :meth:`_send_with_redirects_async` 的状态机（复用
        :class:`_RedirectState` 与 :meth:`_advance_redirect`），差异仅在
        3xx 跳转时需要先关闭上一跳的流（未消费的流式响应不关会泄漏连接），
        以及最终返回 :class:`StreamContext` 而非整包响应。
        """
        state = _RedirectState(url, method, None, None, None, headers)
        for _ in range(self.max_redirects + 1):
            stream = await self._stream_once_async(
                state.method, state.url, state.headers, proxy, timeout, verify
            )
            try:
                if not self._advance_redirect(state, stream.raw):
                    return stream
            except BaseException:
                # 跳转目标校验失败（如 UnsafeTargetError）时当前跳的流与池化
                # 句柄已借出——必须先 aclose 归还再传播异常，否则泄漏
                await stream.aclose()
                raise
            await stream.aclose()
        raise FetchError(f"too many redirects (max {self.max_redirects}) for {url}")

    def _stream_once_sync(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
        verify: bool,
    ) -> StreamContext:
        """打开同步流式请求（``stream=True``，不消费 body），返回统一包装。

        与 :meth:`_send_once_sync` 走完全相同的校验链（会话池借出、DNS
        钉扎、代理客户端选择），仅两处差异：响应 body 保持未读状态由调用
        方分块消费；池化句柄**不**在请求返回时归还——归还延迟到流关闭
        （:meth:`StreamContext.close <web_crawler.fetchers._download.StreamContext.close>`），
        流式读取期间会话保持独占。重定向由 :meth:`_stream_with_redirects_sync`
        手动跟随（恒以 ``allow_redirects=False`` 发送，逐跳校验目标）。
        """
        if self._use_curl:
            handle = self._acquire_pooled(proxy)
            try:
                session = handle.session if handle is not None else self._ensure_sync_session()
                # 连接级 DNS 钉扎（CURLOPT_RESOLVE）：DNS 翻转到私网时抛
                # ValueError 即时中止，否则 libcurl 强制用已校验 IP 连接
                self._pin_curl_session(session, url, proxy)
                raw = session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    proxy=proxy,
                    timeout=timeout,
                    allow_redirects=False,
                    verify=verify,
                    stream=True,
                )
            except BaseException:
                self._release_pooled(handle, success=False)
                raise
            return StreamContext(raw, handle, fetcher=self, use_curl=True)
        # httpx 兜底：无代理复用共享会话（或池化会话）；代理客户端按 proxy 缓存复用
        handle = self._acquire_pooled(proxy)
        try:
            client = (
                (handle.session if handle is not None else self._ensure_sync_session())
                if proxy is None
                else self._ensure_proxied_sync_client(proxy)
            )
            request_url, extra_headers, extensions = self._pin_target(url, proxy)
            if extra_headers:
                headers = {**headers, **extra_headers}
            raw = client.request(
                method=method,
                url=request_url,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
                extensions=extensions,
                stream=True,
            )
        except BaseException:
            self._release_pooled(handle, success=False)
            raise
        return StreamContext(raw, handle, fetcher=self, use_curl=False)

    def _stream_with_redirects_sync(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
        verify: bool,
    ) -> StreamContext:
        """打开同步流式请求并手动跟随重定向（每跳校验 scheme/host）。

        对齐 :meth:`_send_with_redirects_sync` 的状态机（复用
        :class:`_RedirectState` 与 :meth:`_advance_redirect`），差异仅在
        3xx 跳转时需要先关闭上一跳的流，以及最终返回
        :class:`StreamContext` 而非整包响应。
        """
        state = _RedirectState(url, method, None, None, None, headers)
        for _ in range(self.max_redirects + 1):
            stream = self._stream_once_sync(
                state.method, state.url, state.headers, proxy, timeout, verify
            )
            try:
                if not self._advance_redirect(state, stream.raw):
                    return stream
            except BaseException:
                # 跳转目标校验失败（如 UnsafeTargetError）时当前跳的流与池化
                # 句柄已借出——必须先 close 归还再传播异常，否则泄漏
                stream.close()
                raise
            stream.close()
        raise FetchError(f"too many redirects (max {self.max_redirects}) for {url}")

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
        raise FetchError(
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
    session_pool:
        可选的 :class:`~web_crawler.fetchers.session.SessionPool`。启用时
        每次请求从池中借出会话——指纹随会话轮换、空闲回收、连续失败淘汰，
        请求（含 DNS 钉扎）完成后归还；``None``（默认）走单会话路径，
        行为不变。传入的池会自动绑定本 fetcher 的会话工厂（超时/校验/
        HTTP2 等配置随之生效，且 ``impersonate`` 参数退化为池降级时的
        兜底指纹）。
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
            # 会话池启用时借出池化会话（指纹随会话轮换），否则用惰性单会话
            handle = self._acquire_pooled(proxy)
            try:
                session = handle.session if handle is not None else self._ensure_sync_session()
                # 连接级 DNS 钉扎（CURLOPT_RESOLVE）：DNS 翻转到私网时抛
                # ValueError 即时中止，否则 libcurl 强制用已校验 IP 连接
                self._pin_curl_session(session, url, proxy)
                raw = session.request(
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
            except BaseException:
                self._release_pooled(handle, success=False)
                raise
            self._release_pooled(handle, success=True)
            return raw
        # httpx 兜底：无代理复用共享会话（或池化会话）；代理客户端按 proxy 缓存复用
        handle = self._acquire_pooled(proxy)
        try:
            client = (
                (handle.session if handle is not None else self._ensure_sync_session())
                if proxy is None
                else self._ensure_proxied_sync_client(proxy)
            )
            request_url, extra_headers, extensions = self._pin_target(url, proxy)
            if extra_headers:
                headers = {**headers, **extra_headers}
            content, data_arg = _httpx_body(data)
            raw = client.request(
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
        except BaseException:
            self._release_pooled(handle, success=False)
            raise
        self._release_pooled(handle, success=True)
        return raw

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
        raise FetchError(f"too many redirects (max {self.max_redirects}) for {url}")

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
        raise FetchError(
            f"request to {url} failed without a captured exception"
        )  # pragma: no cover

    # -- 公开下载 API ----------------------------------------------------------
    def download(
        self,
        url: str,
        dest: str | Path,
        *,
        chunk_size: int = 1 << 20,
        resume: bool = True,
        max_bytes: int | None = None,
        headers: dict[str, str] | None = None,
        should_abort: Callable[[], bool] | None = None,
        on_headers: Callable[[int, Mapping[str, str]], None] | None = None,
    ) -> Path:
        """流式下载 *url* 到 *dest*（支持断点续传），返回最终绝对路径。

        走与 :meth:`get` 完全相同的请求管线（SSRF host 校验、代理、会话
        池、DNS 钉扎、重定向逐跳校验），只是响应 body 不整包缓冲而是按
        ``chunk_size`` 分块写盘——大文件下载不再受内存约束。断点续传：
        半成品写在 ``<dest>.part``，``resume=True`` 且 ``.part`` 存在时
        发送 ``Range`` 续传（206 追加、200 截断重写），完成后
        ``os.replace`` 原子替换到 *dest*。

        Parameters
        ----------
        dest:
            目标文件路径（目录不存在时自动创建）。
        chunk_size:
            分块字节数（默认 1MB；仅 httpx 后端严格生效，curl_cffi 后端
            由接收缓冲队列决定块粒度）。
        resume:
            断点续传开关（默认开启）。
        max_bytes:
            累计写入字节上限（防磁盘炸弹）；超限即中止、清理 ``.part``
            并抛 :class:`~web_crawler.exceptions.FetchError`。
        headers:
            每请求附加头（与 ``extra_headers`` 合并；``Range`` 由本方法
            内部管理，调用方无需设置）。
        should_abort:
            每分块写入前轮询；返回 ``True`` 时中止下载（保留 ``.part``
            供续传）并抛 :class:`FetchError`。
        on_headers:
            收到最终（重定向解析完）响应时回调一次，参数为
            ``(status, headers)``——应用层可借此读取 Content-Type 等。

        Raises
        ------
        ConfigError
            ``chunk_size`` / ``max_bytes`` 参数非法。
        FetchError
            服务器错误状态、``max_bytes`` 超限、重定向超限、用户中止或
            传输层错误重试耗尽（附 ``status`` 属性，可用
            ``getattr(exc, "status", None)`` 读取）。
        """
        return download_sync(
            self,
            url,
            dest,
            chunk_size=chunk_size,
            resume=resume,
            max_bytes=max_bytes,
            headers=headers,
            should_abort=should_abort,
            on_headers=on_headers,
        )

    async def async_download(
        self,
        url: str,
        dest: str | Path,
        *,
        chunk_size: int = 1 << 20,
        resume: bool = True,
        max_bytes: int | None = None,
        headers: dict[str, str] | None = None,
        should_abort: Callable[[], bool] | None = None,
        on_headers: Callable[[int, Mapping[str, str]], None] | None = None,
    ) -> Path:
        """异步流式下载（语义与 :meth:`download` 完全一致）。"""
        return await download_async(
            self,
            url,
            dest,
            chunk_size=chunk_size,
            resume=resume,
            max_bytes=max_bytes,
            headers=headers,
            should_abort=should_abort,
            on_headers=on_headers,
        )

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

    异步应用中使用它，可避免同步调用意外阻塞事件循环。构造参数与
    :class:`Fetcher` 完全一致——包括可选的 ``session_pool``（异步借还
    走池的 ``acquire_async`` / ``release_async``）。
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

    # -- 公开下载 API ----------------------------------------------------------
    async def download(
        self,
        url: str,
        dest: str | Path,
        *,
        chunk_size: int = 1 << 20,
        resume: bool = True,
        max_bytes: int | None = None,
        headers: dict[str, str] | None = None,
        should_abort: Callable[[], bool] | None = None,
        on_headers: Callable[[int, Mapping[str, str]], None] | None = None,
    ) -> Path:
        """异步流式下载 *url* 到 *dest*（支持断点续传），返回最终绝对路径。

        语义与 :meth:`Fetcher.download <web_crawler.fetchers.fetcher.Fetcher.download>`
        完全一致（同一套校验链、重试/退避与 ``.part`` 生命周期管理），
        仅传输原语为异步版。
        """
        return await download_async(
            self,
            url,
            dest,
            chunk_size=chunk_size,
            resume=resume,
            max_bytes=max_bytes,
            headers=headers,
            should_abort=should_abort,
            on_headers=on_headers,
        )

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

"""连接层 DNS 钉扎（消除 check-then-connect TOCTOU）。

SSRF 门禁时校验的 DNS 结果与传输层实际发起连接时的解析是两个独立查询：
低 TTL DNS 在两次查询之间翻转 A 记录（门禁时公网、连接时 169.254.169.254）
即可绕过 host 层校验。本模块在发送前**自行解析**并逐地址校验，让"校验的
那个地址"就是"连接的那个地址"，窗口归零：

* **httpx 兜底路径**（:func:`pin_url`）：把 URL 的 host 替换为已校验 IP，
  配合 httpcore 的 ``sni_hostname`` 扩展让 SNI 与证书校验仍针对原始
  主机名。
* **curl_cffi 主路径**（:class:`CurlResolvePinner` + :func:`apply_curl_resolve`）：
  把全族已校验 IP 以 ``CURLOPT_RESOLVE`` 条目（``["host:port:ip", ...]``）
  钉给 libcurl，连接强制使用已校验 IP——URL / Host 头 / SNI / 证书校验
  全部保持原始主机名，无需改写。

curl_cffi 0.16.3 的 ``Session.request`` 不暴露按请求的 ``resolve`` 参数，
但每次请求会把 ``session.curl_options`` 透传给底层（``set_curl_options``
在设置 URL 等常规选项之后逐项 setopt），且 ``perform`` 结束后自动清理
resolve slist、请求结束 ``reset``——条目不跨请求累积，按请求动态赋值
``session.curl_options`` 即等价于官方的 ``resolve`` 透传。

边界（如实声明，详见 SECURITY.md）：
- 代理路径无需钉扎：解析由代理完成，客户端侧不存在 DNS TOCTOU；libcurl
  走显式 / 环境变量代理时 ``CURLOPT_RESOLVE`` 也不会被用到。
- IP 字面量 URL 无解析步骤，天然无 TOCTOU，原样返回。
- 若 curl_cffi 过旧（无 ``curl_options`` 属性）或缺失，:func:`apply_curl_resolve`
  返回 False 放弃钉扎——此时发送前的即时解析校验（:func:`_resolve_validated_ips`）
  仍然生效，TOCTOU 窗口已从"60s 判定缓存"缩到"单次请求内"，残余风险
  仅剩校验与连接之间的一次 getaddrinfo 间隙。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
from typing import Any
from urllib.parse import urlparse, urlunparse

from .._ssrf import is_private_ip
from ..exceptions import UnsafeTargetError

logger = logging.getLogger(__name__)

__all__ = [
    "CurlResolvePinner",
    "apply_curl_resolve",
    "env_proxy_configured",
    "host_header_for",
    "pin_url",
]

# httpx 默认 trust_env=True 会读取这些变量走代理；走代理时解析由代理完成，
# 客户端侧钉扎既无意义也不可用（CONNECT 到裸 IP 可能被代理拒绝）。
# libcurl 同样读取这些环境变量，走代理时 CURLOPT_RESOLVE 被绕过。
_ENV_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def env_proxy_configured() -> bool:
    """环境变量是否配置了 HTTP 代理（httpx trust_env / libcurl 都会读取）。"""
    return any(os.environ.get(var) for var in _ENV_PROXY_VARS)


def _is_ip_literal(host: str) -> bool:
    """host 是否已是 IP 字面量（含 IPv6；调用方保证已剥离方括号）。"""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _default_port(scheme: str) -> int:
    return 443 if scheme.lower() == "https" else 80


def host_header_for(url: str) -> str:
    """取 URL 的 Host 头值（hostname[:port]，非默认端口才带端口）。"""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.port is not None and parsed.port != _default_port(parsed.scheme):
        return f"{host}:{parsed.port}"
    return host


def _resolve_validated_ips(host: str, port: int | None) -> list[str]:
    """解析 ``host`` 并逐地址校验拒绝段，返回全部公网解析结果（TOCTOU 拦截）。

    这是两条钉扎路径共用的"即时解析 + 二次校验"核心：**绕过** host 层
    门禁的 60s 判定缓存，在发送前一刻重新解析。任一解析结果落入
    私网/环回/链路本地等拒绝段时抛 :class:`UnsafeTargetError`（门禁校验后 DNS
    翻转的即时拦截）；解析失败时 :class:`OSError` 原样上抛（传输层随后
    也会失败，无需二次查询）。返回顺序保持 getaddrinfo 原始顺序（含
    IPv4 与 IPv6 全族——见 :class:`CurlResolvePinner` 的全族钉扎要求）。
    """
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    if not infos:  # pragma: no cover - getaddrinfo 失败走 OSError
        raise ValueError(f"DNS resolution returned no addresses for {host!r}")
    ips: list[str] = []
    for info in infos:
        ip = str(info[4][0])
        if is_private_ip(ip):
            raise UnsafeTargetError(
                f"blocked URL whose DNS resolves to a private/link-local address: "
                f"{host!r} -> {ip} (TOCTOU guard)"
            )
        ips.append(ip)
    return ips


def pin_url(url: str) -> str:
    """解析 ``url`` 的主机名并把 host 替换为已校验的 IP，返回钉扎后的 URL。

    任一解析结果落入私网/环回/链路本地等拒绝段时抛 :class:`UnsafeTargetError`
    （门禁校验后 DNS 翻转的即时拦截）。IP 字面量原样返回；解析失败时
    :class:`OSError` 原样上抛（传输层随后也会失败，无需二次查询）。
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host or _is_ip_literal(host):
        return url
    port = parsed.port
    ips = _resolve_validated_ips(host, port)
    chosen = ips[0]
    pinned_host = f"[{chosen}]" if ":" in chosen else chosen
    # 保留 userinfo（如存在）与非默认端口，其余部分原样
    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    netloc = f"{userinfo}{pinned_host}:{port}" if port else f"{userinfo}{pinned_host}"
    pinned = urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )
    logger.debug("pinned %s -> %s", host, chosen)
    return pinned


# curl_cffi 路径：CURLOPT_RESOLVE 条目的 LRU 上限（按 host 计）。
_CURL_PIN_LRU_MAX_HOSTS = 8


class CurlResolvePinner:
    """curl_cffi 会话的 ``CURLOPT_RESOLVE`` 钉扎器（host 条目 LRU 快照）。

    把 ``CurlOpt.RESOLVE`` 设为 ``["host:port:ip", ...]`` 即可让 libcurl
    连接强制使用已校验 IP——SNI / Host 头 / 证书校验仍针对原始主机名。

    两个关键约束：

    1. **全族钉扎**：条目必须覆盖该 host 的**全部**解析结果（A + AAAA）。
       libcurl 的 Happy Eyeballs 会对未覆盖的地址族继续走系统解析——
       只钉 IPv4 而系统解析出 IPv6 时，钉扎形同虚设（实测复现）。
    2. **异步竞态**：共享 ``AsyncSession`` 上 ``curl_options`` 的赋值与
       请求内部 ``set_curl_options`` 的读取之间存在协程交错点（``pop_curl``
       挂起时）；若后赋值的条目列表不包含先前协程的条目，先恢复的请求
       会静默失去钉扎。``CURLOPT_RESOLVE`` 条目按 ``host:port`` 精确匹配、
       跨 host 无副作用，因此维护最近若干 host 的条目 LRU，每次赋值其
       快照并集即可消除竞态（上限防止长爬取任务无限累积，被挤出 LRU 的
       在途请求退回入口门禁的 60s 判定缓存保护）。

    线程安全：同一 Fetcher 被多线程共享时，``entries_for`` 的 LRU 维护
    序列（pop / reinsert / 超限淘汰 / 快照构造）由 :class:`threading.Lock`
    保护——两个线程交错修改同一 dict 最坏会双 pop 同一 key 抛
    :class:`KeyError`，或刚写入的条目被另一线程的淘汰循环挤出（该请求
    静默失去 ``CURLOPT_RESOLVE`` 条目，TOCTOU 窗口重新打开）。锁内是纯
    内存的纳秒级操作；DNS 解析（可能阻塞数百毫秒）刻意留在锁外。
    """

    def __init__(self, max_hosts: int = _CURL_PIN_LRU_MAX_HOSTS) -> None:
        self._max_hosts = max_hosts
        self._entries: dict[str, list[str]] = {}
        # 只保护 _entries 的 LRU 维护与快照构造（见类 docstring 线程安全段）
        self._lock = threading.Lock()

    def entries_for(self, url: str) -> list[str]:
        """即时解析校验 ``url`` 的主机并返回钉扎条目快照（可能为空）。

        - IP 字面量 / 无 host：不解析，返回当前快照（无 TOCTOU 需消除）。
        - 任一解析结果落入拒绝段：抛 :class:`UnsafeTargetError` 中止请求。
        - 解析失败：:class:`OSError` 原样上抛（与 :func:`pin_url` 一致）。

        返回的快照在锁内构造，保证必含本次请求 host 的条目（并发下不会
        被其他线程的 LRU 淘汰挤出）。
        """
        parsed = urlparse(url)
        host = parsed.hostname
        pin_host: str | None = None
        host_entries: list[str] = []
        if host and not _is_ip_literal(host):
            # DNS 解析与校验在锁外：网络操作可能阻塞，绝不能进临界区
            ips = _resolve_validated_ips(host, parsed.port)
            port = parsed.port if parsed.port is not None else _default_port(parsed.scheme)
            pin_host = host
            host_entries = [f"{host}:{port}:{ip}" for ip in ips]
        # LRU 维护与快照构造必须在**同一个**临界区内完成。拆成两次加锁时，
        # 中间的空隙足以让并发线程把本次 host 淘汰出去，快照就会缺少调用方
        # 自己的条目（该请求静默失去 CURLOPT_RESOLVE，TOCTOU 窗口重新打开）。
        with self._lock:
            if pin_host is not None:
                # LRU 触碰：最近使用的 host 移到末尾，超限淘汰最旧条目
                self._entries.pop(pin_host, None)
                self._entries[pin_host] = host_entries
                while len(self._entries) > self._max_hosts:
                    self._entries.pop(next(iter(self._entries)))
            # 快照也在锁内：迭代期间其他线程修改 dict 会抛 RuntimeError
            snapshot = [entry for entries in self._entries.values() for entry in entries]
        logger.debug("curl resolve pin entries: %s", snapshot)
        return snapshot


def _curl_opt_resolve() -> Any | None:
    """返回 ``CurlOpt.RESOLVE`` 枚举；curl_cffi 缺失时返回 ``None``。"""
    try:
        from curl_cffi.const import CurlOpt  # type: ignore[import-not-found]
    except ImportError:
        return None
    return CurlOpt.RESOLVE


def apply_curl_resolve(session: Any, entries: list[str]) -> bool:
    """把钉扎条目写入 curl session 的 ``curl_options``（CURLOPT_RESOLVE）。

    curl_cffi 0.16.3 起每次请求会把 ``session.curl_options`` 逐项 setopt
    （在 URL 等常规选项之后），``perform`` 结束自动清理 resolve slist、
    请求结束 ``reset``——条目不跨请求累积，按请求动态赋值即可。已有的
    session 级 curl options 会保留（同名键除外）。

    返回 ``False`` 表示当前环境无法钉扎（curl_cffi 缺失，或 session 结构
    不支持 ``curl_options``）——此时调用方在钉扎前已完成的即时解析校验
    仍然生效（降级保护：TOCTOU 窗口已缩到单次请求内，详见模块文档）。
    """
    option = _curl_opt_resolve()
    if option is None or not hasattr(session, "curl_options"):
        return False
    options: dict[Any, Any] = dict(session.curl_options)
    options[option] = entries
    session.curl_options = options
    return True

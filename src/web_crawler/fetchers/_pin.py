"""httpx 兜底路径的连接层 DNS 钉扎（消除 check-then-connect TOCTOU）。

SSRF 门禁时校验的 DNS 结果与传输层实际发起连接时的解析是两个独立查询：
低 TTL DNS 在两次查询之间翻转 A 记录（门禁时公网、连接时 169.254.169.254）
即可绕过 host 层校验。本模块在发送前**自行解析**并把 URL 的 host 替换为
已逐一校验的 IP，配合 httpcore 的 ``sni_hostname`` 扩展让 SNI 与证书校验
仍针对原始主机名——"校验的那个地址"就是"连接的那个地址"，窗口归零。

边界（如实声明，详见 SECURITY.md）：
- curl_cffi 主路径暂无法钉扎：其 requests 式 API 未暴露按请求的
  ``CURLOPT_RESOLVE``（0.16.3 核实），该路径依赖入口解析复查 + 60s 判定
  缓存把窗口缩到有限时长。
- 代理路径无需钉扎：解析由代理完成，客户端侧不存在 DNS TOCTOU。
- IP 字面量 URL 无解析步骤，天然无 TOCTOU，原样返回。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse, urlunparse

from .._ssrf import is_private_ip

logger = logging.getLogger(__name__)

__all__ = ["env_proxy_configured", "host_header_for", "pin_url"]

# httpx 默认 trust_env=True 会读取这些变量走代理；走代理时解析由代理完成，
# 客户端侧钉扎既无意义也不可用（CONNECT 到裸 IP 可能被代理拒绝）。
_ENV_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def env_proxy_configured() -> bool:
    """环境变量是否配置了 HTTP 代理（httpx trust_env 会读取）。"""
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


def pin_url(url: str) -> str:
    """解析 ``url`` 的主机名并把 host 替换为已校验的 IP，返回钉扎后的 URL。

    任一解析结果落入私网/环回/链路本地等拒绝段时抛 :class:`ValueError`
    （门禁校验后 DNS 翻转的即时拦截）。IP 字面量原样返回；解析失败时
    :class:`OSError` 原样上抛（传输层随后也会失败，无需二次查询）。
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host or _is_ip_literal(host):
        return url
    port = parsed.port
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    if not infos:  # pragma: no cover - getaddrinfo 失败走 OSError
        raise ValueError(f"DNS resolution returned no addresses for {host!r}")
    chosen: str | None = None
    for info in infos:
        ip = str(info[4][0])
        if is_private_ip(ip):
            raise ValueError(
                f"blocked URL whose DNS resolves to a private/link-local address: "
                f"{host!r} -> {ip} (TOCTOU guard)"
            )
        if chosen is None:
            chosen = ip
    assert chosen is not None
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

"""MCP 工具的 URL / 目标安全门禁。

从 ``server.py`` 拆出:所有会向外部发起请求的工具(浏览器导航、pentest 扫描)
都经过这里的校验——仅允许 http/https、拒绝私网/环回/链路本地/云元数据等地址。
解析失败的未知主机一律按"非公网"拒绝(deny by default)。
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

# -- 目标/URL 安全门禁 ---------------------------------------------------------
#
# 本模块所有会向外部发起请求的工具（浏览器导航、pentest 扫描）都经过这里的
# 校验：仅允许 http/https、拒绝私网/环回/链路本地/云元数据等地址，pentest
# 额外要求显式授权确认。解析失败的未知主机一律按"非公网"拒绝（deny by default）。


def _ip_is_global(ip: Any) -> bool:
    """判断 IP 是否可视为公网可达（排除私网/环回/链路本地/保留/组播/未指定）。"""
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_host_ips(host: str) -> list[str] | None:
    """解析主机名到 IP 列表（去重）；解析失败返回 None。"""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, socket.herror, OSError):
        return None
    ips: list[str] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        host_addr = str(sockaddr[0]) if sockaddr else ""
        if host_addr and host_addr not in seen:
            seen.add(host_addr)
            ips.append(host_addr)
    return ips or None


def _host_is_public(host: str) -> bool:
    """判断主机是否为公网可达地址。

    字面 IP 直接判定；主机名解析后逐一判定，任一地址非公网即视为否；
    解析失败视为非公网（保守拒绝）。
    """
    try:
        ip = ipaddress.ip_address(host)
        return _ip_is_global(ip)
    except ValueError:
        pass
    ips = _resolve_host_ips(host)
    if not ips:
        return False
    return all(_ip_is_global(ipaddress.ip_address(ip)) for ip in ips)


def _check_target_public(host: str) -> str | None:
    """校验 pentest 目标为公网地址；非公网返回错误信息，否则 None。"""
    if _host_is_public(host):
        return None
    return (
        f"target not allowed: {host!r} 解析为私网/环回/链路本地等非公网地址；"
        "pentest 仅允许已获书面授权的公网目标（可传 allow_private=true 显式放行）"
    )


def _check_url(url: str) -> str | None:
    """校验浏览器工具 URL：仅 http/https、无 userinfo、主机为公网地址。返回错误信息或 None。"""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return f"invalid url: {url!r}"
    if parsed.scheme not in ("http", "https"):
        return f"scheme not allowed: {parsed.scheme!r}（仅支持 http/https）"
    host = parsed.hostname
    if not host:
        return f"invalid url: missing host in {url!r}"
    if parsed.username or parsed.password:
        return "url must not contain userinfo (user:pass@)"
    if not _host_is_public(host):
        return f"target host not allowed: {host!r} 解析为私网/环回等非公网地址"
    return None

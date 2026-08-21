"""Host-level SSRF guard shared by the fetchers and the ``app`` layer.

The HTTP fetch paths already whitelist URL schemes (http/https only). This
module closes the second gap: the **host** of a request target is validated
before anything is sent, so private / loopback / link-local destinations
(including the cloud metadata endpoints ``169.254.169.254`` and
``100.100.100.200``) are rejected.

Policy
------
* IP-literal hosts are matched against blocked ranges: ``0.0.0.0/8``,
  ``10.0.0.0/8``, ``100.64.0.0/10`` (CGNAT), ``127.0.0.0/8`` (loopback),
  ``169.254.0.0/16`` (link-local, incl. cloud metadata), ``172.16.0.0/12``,
  ``192.168.0.0/16``, ``::1``, ``fc00::/7`` (IPv6 ULA) and ``fe80::/10``
  (IPv6 link-local), plus multicast and unspecified addresses. IPv4-mapped
  IPv6 literals (``::ffff:127.0.0.1``) are unwrapped and re-checked as IPv4.
* Hostnames containing ``localhost`` (e.g. ``localhost``, ``*.localhost``,
  ``localhost.localdomain``), ending in ``.local`` (mDNS) or equal to
  well-known cloud-metadata names are rejected statically.
* With ``resolve=True`` the hostname is additionally resolved via
  :func:`socket.getaddrinfo`; if any resolved address falls into a blocked
  range the host is rejected. A resolution failure is **conservatively**
  treated as unsafe and logged (public domains such as ``quotes.toscrape.com``
  pass as long as DNS resolves them to public addresses).

This module is stdlib-only on purpose so it can be imported by
``web_crawler.fetchers``, ``app.crawler_net`` and the test suite without
pulling in lxml / playwright / curl_cffi.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

_log = logging.getLogger(__name__)

# 被拒绝的 IP 段：私网 / 环回 / 链路本地 / CGNAT / 本网络。
_PRIVATE_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),      # 本网络 / 未指定
    ipaddress.ip_network("10.0.0.0/8"),     # 私网
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT（含阿里云元数据 100.100.100.200）
    ipaddress.ip_network("127.0.0.0/8"),    # 环回
    ipaddress.ip_network("169.254.0.0/16"), # 链路本地（含云元数据 169.254.169.254）
    ipaddress.ip_network("172.16.0.0/12"),  # 私网
    ipaddress.ip_network("192.168.0.0/16"), # 私网
    ipaddress.ip_network("::1/128"),        # IPv6 环回
    ipaddress.ip_network("fc00::/7"),       # IPv6 唯一本地地址（ULA）
    ipaddress.ip_network("fe80::/10"),      # IPv6 链路本地
)

# 静态黑名单主机名：即使不做 DNS 解析也能拦截的本地 / 云元数据名称。
_UNSAFE_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "metadata.google.internal",  # GCP 元数据
        "metadata.azure.internal",   # Azure IMDS 元数据
        "metadata.aws.internal",     # AWS 元数据变体
        "instance-data",             # 部分系统上 169.254.169.254 的别名
    }
)


def _is_blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True 表示 ``addr`` 落在被拒绝的范围内（含 IPv4-mapped IPv6 的解包）。"""
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is not None:
            addr = mapped
    return (
        any(addr in net for net in _PRIVATE_NETWORKS)
        or addr.is_multicast
        or addr.is_unspecified
    )


def is_private_ip(host: str) -> bool:
    """``host`` 是 IP 字面量且落在被拒绝范围时返回 True，否则 False。"""
    candidate = host.strip().lower()
    # IPv6 zone id（如 fe80::1%eth0）无法被 ipaddress 解析，先剥离
    if "%" in candidate and ":" in candidate:
        candidate = candidate.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return _is_blocked_ip(addr)


def is_unsafe_hostname(host: str) -> bool:
    """静态主机名检查：localhost / *.localhost / *.local（mDNS）/ 云元数据名称。

    含 ``localhost`` 字样的主机名（如 ``localhost.localdomain``、子域名）与
    以 ``.local`` 结尾的 mDNS 名称均视为不安全。
    """
    lowered = host.strip().lower().rstrip(".")
    if lowered in _UNSAFE_HOSTNAMES:
        return True
    return "localhost" in lowered or lowered.endswith(".local")


def host_is_unsafe(host: str | None, *, resolve: bool = False) -> bool:
    """组合 host 级 SSRF 检查（静态 + 可选 DNS 解析）。

    ``None`` / 空 host 视为不安全。``resolve=True`` 时对主机名做一次
    :func:`socket.getaddrinfo` 解析：任一地址落入被拒绝范围即拒绝；
    解析失败按保守策略拒绝并记录 warning（公共域名正常解析时不受影响）。
    """
    if not host:
        return True
    host = host.strip().rstrip(".")
    if not host:
        return True
    if is_private_ip(host):
        return True
    if is_unsafe_hostname(host):
        return True
    if resolve:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except OSError:
            _log.warning(
                "SSRF guard: DNS resolution failed for host %r — rejecting", host
            )
            return True
        for info in infos:
            ip = info[4][0]
            if is_private_ip(ip):
                return True
    return False


def validate_url_host(url: str, *, resolve: bool = False) -> None:
    """URL 的 host 不安全（私网/环回/链路本地等）时抛出 :class:`ValueError`。"""
    host = urlparse(url).hostname
    if host_is_unsafe(host, resolve=resolve):
        raise ValueError(f"blocked URL with unsafe host: {url}")


__all__ = [
    "host_is_unsafe",
    "is_private_ip",
    "is_unsafe_hostname",
    "validate_url_host",
]

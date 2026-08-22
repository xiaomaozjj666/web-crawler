"""fetchers 与 ``app`` 层共享的 host 级 SSRF 防护。

HTTP 抓取路径已对 URL scheme 做白名单（仅 http/https）。本模块补上第二道
防线：任何请求发出前先校验目标的 **host**，私网 / 环回 / 链路本地地址
（包括云元数据端点 ``169.254.169.254`` 与 ``100.100.100.200``）一律拒绝。

策略
----
* IP 字面量 host 与被封锁网段比对：``0.0.0.0/8``、``10.0.0.0/8``、
  ``100.64.0.0/10``（CGNAT）、``127.0.0.0/8``（环回）、``169.254.0.0/16``
  （链路本地，含云元数据）、``172.16.0.0/12``、``192.168.0.0/16``、``::1``、
  ``fc00::/7``（IPv6 ULA）与 ``fe80::/10``（IPv6 链路本地），另加组播与
  未指定地址。IPv4-mapped IPv6 字面量（``::ffff:127.0.0.1``）会先解包再按
  IPv4 复查。
* 含 ``localhost`` 的主机名（如 ``localhost``、``*.localhost``、
  ``localhost.localdomain``）、以 ``.local`` 结尾的 mDNS 名称，以及知名的
  云元数据主机名会被静态拒绝。
* ``resolve=True`` 时额外通过 :func:`socket.getaddrinfo` 解析主机名；任一
  解析结果落入被封锁网段即拒绝。解析失败按**保守策略**视为不安全并记录
  日志（``quotes.toscrape.com`` 等公共域名只要 DNS 解析到公网地址即可通过）。

Power Mode
----------
设置 ``WEB_CRAWLER_POWER_MODE=1`` 可跳过上述 host 级校验（http/https
scheme 白名单始终保留）。它面向确需访问私网 / 链路本地目标（局域网服务、
云元数据）的可信个人环境——公开或共享部署切勿开启。

本模块刻意只用标准库，以便 ``web_crawler.fetchers``、``app.crawler_net``
与测试套件直接导入，而无需引入 lxml / playwright / curl_cffi。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
import time
from urllib.parse import urlparse

_log = logging.getLogger(__name__)

# 个人 Power Mode 开关（默认关闭）：设置 WEB_CRAWLER_POWER_MODE=1 后，
# host 层 SSRF 校验整体放行（http/https scheme 白名单仍保留）。仅供可信的
# 个人环境解锁内网 / 云元数据等目标；公开部署与共享环境不要开启。
_POWER_MODE_ENV = "WEB_CRAWLER_POWER_MODE"


def is_power_mode() -> bool:
    """Power Mode 是否开启（个人全解锁开关，默认关闭）。"""
    return os.environ.get(_POWER_MODE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


# ── DNS 判定缓存（防重复解析）─────────────────────────────────────────────
# host_is_unsafe(resolve=True) 对同一主机名重复 getaddrinfo 是无谓开销，且
# 解析结果在 TTL 内稳定；缓存按 host 存最终判定（不安全 True / 安全 False），
# 线程安全、有界（LRU 淘汰）。解析失败按短 TTL 负缓存，避免对坏域名反复查询。
_DNS_CACHE_TTL_SECONDS = 60.0  # 正常判定缓存时长
_DNS_CACHE_NEGATIVE_TTL_SECONDS = 10.0  # 解析失败（负缓存）时长
_DNS_CACHE_MAX_ENTRIES = 512


class _DnsVerdictCache:
    """线程安全、有界、带 TTL 的 DNS 判定缓存（host -> (判定, 过期时间戳)）。"""

    def __init__(self) -> None:
        self._data: dict[str, tuple[bool, float]] = {}
        self._lock = threading.Lock()

    def get(self, host: str) -> bool | None:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(host)
            if entry is None:
                return None
            verdict, expiry = entry
            if expiry <= now:
                del self._data[host]
                return None
            # LRU 触碰：最近使用移到末尾
            del self._data[host]
            self._data[host] = (verdict, expiry)
            return verdict

    def put(self, host: str, verdict: bool, ttl: float) -> None:
        expiry = time.monotonic() + ttl
        with self._lock:
            self._data.pop(host, None)
            self._data[host] = (verdict, expiry)
            while len(self._data) > _DNS_CACHE_MAX_ENTRIES:
                self._data.pop(next(iter(self._data)))


_dns_verdict_cache = _DnsVerdictCache()

# 被拒绝的 IP 段：私网 / 环回 / 链路本地 / CGNAT / 本网络。
_PRIVATE_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),  # 本网络 / 未指定
    ipaddress.ip_network("10.0.0.0/8"),  # 私网
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT（含阿里云元数据 100.100.100.200）
    ipaddress.ip_network("127.0.0.0/8"),  # 环回
    ipaddress.ip_network("169.254.0.0/16"),  # 链路本地（含云元数据 169.254.169.254）
    ipaddress.ip_network("172.16.0.0/12"),  # 私网
    ipaddress.ip_network("192.168.0.0/16"),  # 私网
    ipaddress.ip_network("::1/128"),  # IPv6 环回
    ipaddress.ip_network("fc00::/7"),  # IPv6 唯一本地地址（ULA）
    ipaddress.ip_network("fe80::/10"),  # IPv6 链路本地
)

# 静态黑名单主机名：即使不做 DNS 解析也能拦截的本地 / 云元数据名称。
_UNSAFE_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "metadata.google.internal",  # GCP 元数据
        "metadata.azure.internal",  # Azure IMDS 元数据
        "metadata.aws.internal",  # AWS 元数据变体
        "instance-data",  # 部分系统上 169.254.169.254 的别名
    }
)


def _is_blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True 表示 ``addr`` 落在被拒绝的范围内（含 IPv4-mapped IPv6 的解包）。"""
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is not None:
            addr = mapped
    return any(addr in net for net in _PRIVATE_NETWORKS) or addr.is_multicast or addr.is_unspecified


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
        cached = _dns_verdict_cache.get(host)
        if cached is not None:
            return cached
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except OSError:
            _log.warning("SSRF guard: DNS resolution failed for host %r — rejecting", host)
            _dns_verdict_cache.put(host, True, _DNS_CACHE_NEGATIVE_TTL_SECONDS)
            return True
        for info in infos:
            ip = str(info[4][0])
            if is_private_ip(ip):
                _dns_verdict_cache.put(host, True, _DNS_CACHE_TTL_SECONDS)
                return True
        _dns_verdict_cache.put(host, False, _DNS_CACHE_TTL_SECONDS)
    return False


def validate_url_host(url: str, *, resolve: bool = False) -> None:
    """URL 的 host 不安全（私网/环回/链路本地等）时抛出 :class:`ValueError`。

    Power Mode（``WEB_CRAWLER_POWER_MODE=1``）下放行 host 校验（scheme 白名单
    由 :func:`validate_url_scheme` 独立保证，始终生效）。
    """
    if is_power_mode():
        return
    host = urlparse(url).hostname
    if host_is_unsafe(host, resolve=resolve):
        raise ValueError(f"blocked URL with unsafe host: {url}")


__all__ = [
    "host_is_unsafe",
    "is_power_mode",
    "is_private_ip",
    "is_unsafe_hostname",
    "validate_url_host",
]

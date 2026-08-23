"""SSRF host-guard tests.

These tests exercise the host-level validation added on top of the existing
scheme whitelist. They only import the guard module (:mod:`web_crawler._ssrf`)
plus the standard library, so they run without curl_cffi / playwright / lxml.

Coverage mirrors the security audit findings:

* ``169.254.169.254`` (cloud metadata) must be rejected.
* ``127.0.0.1`` / ``::1`` (loopback) must be rejected.
* ``192.168.1.1`` and the other private ranges must be rejected.
* ``localhost`` / ``*.localhost`` / ``*.local`` must be rejected.
* Public hosts such as ``example.com`` must keep working (also with ports and
  credentials, and without requiring a live DNS lookup by default).
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from web_crawler._ssrf import (
    _DNS_CACHE_MAX_ENTRIES,
    _dns_verdict_cache,
    host_is_unsafe,
    is_private_ip,
    is_unsafe_hostname,
    validate_url_host,
)


def _url_for_host(host: str, path: str = "/") -> str:
    """Build a URL, bracketing IPv6 literals as required by urllib.parse."""
    literal = f"[{host}]" if ":" in host else host
    return f"http://{literal}{path}"


def _public_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[tuple]:
    """Fake ``getaddrinfo`` that resolves every hostname to a public address."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


# ── IP 字面量：云元数据 / 环回 / 私网 / 链路本地 ─────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.169.254:80/latest",
        "https://169.254.169.254/",
        "http://100.100.100.200/latest/meta-data/",  # 阿里云元数据（CGNAT）
    ],
)
def test_rejects_cloud_metadata(url: str) -> None:
    with pytest.raises(ValueError, match="unsafe host"):
        validate_url_host(url)
    assert host_is_unsafe("169.254.169.254")
    assert is_private_ip("169.254.169.254")


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.1.2.3",  # 127.0.0.0/8 任意地址
        "0.0.0.0",
        "::1",
        "::ffff:127.0.0.1",  # IPv4-mapped 环回
    ],
)
def test_rejects_loopback_ips(host: str) -> None:
    with pytest.raises(ValueError, match="unsafe host"):
        validate_url_host(_url_for_host(host))
    assert is_private_ip(host)


def test_rejects_loopback_with_port() -> None:
    """带端口的环回 URL（127.0.0.1:8080）同样被拒绝。"""
    with pytest.raises(ValueError, match="unsafe host"):
        validate_url_host("http://127.0.0.1:8080/admin")
    with pytest.raises(ValueError, match="unsafe host"):
        validate_url_host("http://user:pass@127.0.0.1:8080/")  # 带凭据


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.1",
        "100.64.0.1",  # CGNAT
        "169.254.1.1",  # 链路本地
        "fe80::1",  # IPv6 链路本地
        "fc00::1",  # IPv6 ULA
        "224.0.0.1",  # 组播
    ],
)
def test_rejects_private_and_link_local_ips(host: str) -> None:
    with pytest.raises(ValueError, match="unsafe host"):
        validate_url_host(_url_for_host(host, "/x"))
    assert host_is_unsafe(host)


# ── 主机名：localhost / *.localhost / *.local / 云元数据名称 ──────────────────


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost.localdomain",
        "api.localhost",
        "device.local",  # mDNS
        "printer.local",
        "metadata.google.internal",
        "metadata.azure.internal",
    ],
)
def test_rejects_localhost_and_mdns_hostnames(host: str) -> None:
    with pytest.raises(ValueError, match="unsafe host"):
        validate_url_host(f"http://{host}/")
    assert is_unsafe_hostname(host)
    assert host_is_unsafe(host)


def test_empty_or_missing_host_is_unsafe() -> None:
    assert host_is_unsafe(None)
    assert host_is_unsafe("")
    with pytest.raises(ValueError, match="unsafe host"):
        validate_url_host("http:///")


# ── 公开目标：必须保持放行 ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "https://example.com/page?q=1#frag",
        "http://user:pass@example.com:8080/private",  # 带凭据与端口
        "https://sub.example.com/x",
        "http://quotes.toscrape.com/",
        "http://8.8.8.8/dns",  # 公开 IP
        "https://[2001:4860:4860::8888]/",  # 公开 IPv6
    ],
)
def test_allows_public_urls(url: str) -> None:
    # 默认不触发 DNS 解析：纯静态检查即可放行公开 hostname/IP
    validate_url_host(url)
    assert not host_is_unsafe("example.com")
    assert not is_private_ip("8.8.8.8")


def test_allows_public_domain_with_public_dns() -> None:
    """resolve=True 时，解析到公开地址的域名必须通过（quotes.toscrape.com 场景）。"""
    with patch("web_crawler._ssrf.socket.getaddrinfo", side_effect=_public_getaddrinfo):
        validate_url_host("http://quotes.toscrape.com/page", resolve=True)
        assert not host_is_unsafe("quotes.toscrape.com", resolve=True)


# ── DNS 解析检查（可选开启，保守拒绝） ───────────────────────────────────────


def test_rejects_hostname_resolving_to_private_ip() -> None:
    """主机名解析落到私网（如 internal.example.com -> 127.0.0.1）必须被拒绝。"""
    with patch(
        "web_crawler._ssrf.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
    ):
        with pytest.raises(ValueError, match="unsafe host"):
            validate_url_host("http://internal.example.com/", resolve=True)
        assert host_is_unsafe("internal.example.com", resolve=True)


def test_conservative_reject_when_dns_fails() -> None:
    """resolve=True 且 DNS 解析失败时按保守策略拒绝并记录日志。"""
    with patch(
        "web_crawler._ssrf.socket.getaddrinfo",
        side_effect=socket.gaierror("Name or service not known"),
    ):
        with pytest.raises(ValueError, match="unsafe host"):
            validate_url_host("http://no-such-host.invalid/", resolve=True)
        assert host_is_unsafe("no-such-host.invalid", resolve=True)


def test_dns_check_is_opt_in() -> None:
    """resolve=False（默认）不做 DNS 解析：mock 抛错也不影响静态放行。"""
    with patch(
        "web_crawler._ssrf.socket.getaddrinfo",
        side_effect=socket.gaierror("offline"),
    ):
        validate_url_host("http://example.com/")  # 不触发解析，放行
        assert not host_is_unsafe("example.com")


# ── DNS 判定缓存（防重复解析） ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_dns_cache() -> None:
    """每个用例前清空 DNS 判定缓存，避免用例间相互污染。"""
    _dns_verdict_cache._data.clear()


def test_dns_resolution_is_deduplicated() -> None:
    """同一主机在 TTL 内只解析一次：重复调用命中缓存。"""
    calls = 0

    def counting_getaddrinfo(host: str, *args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    with patch("web_crawler._ssrf.socket.getaddrinfo", side_effect=counting_getaddrinfo):
        assert not host_is_unsafe("dedup.example.com", resolve=True)
        assert not host_is_unsafe("dedup.example.com", resolve=True)
        assert not host_is_unsafe("dedup.example.com", resolve=True)
        assert calls == 1  # 3 次调用只发生 1 次真实解析


def test_dns_negative_result_is_cached() -> None:
    """解析失败（负缓存）在短 TTL 内不重复查询坏域名。"""
    calls = 0

    def failing_getaddrinfo(host: str, *args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        raise socket.gaierror("Name or service not known")

    with patch("web_crawler._ssrf.socket.getaddrinfo", side_effect=failing_getaddrinfo):
        assert host_is_unsafe("no-such.invalid", resolve=True)
        assert host_is_unsafe("no-such.invalid", resolve=True)
        assert calls == 1


def test_dns_cache_expires_after_ttl() -> None:
    """TTL 到期后重新解析（验证缓存不是永久记忆）。"""
    calls = 0

    def counting_getaddrinfo(host: str, *args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    import time as _time

    with patch("web_crawler._ssrf.socket.getaddrinfo", side_effect=counting_getaddrinfo):
        assert not host_is_unsafe("ttl.example.com", resolve=True)
        assert calls == 1
        # 手动把缓存项的过期时间拨到过去，模拟 TTL 到期
        _dns_verdict_cache._data["ttl.example.com"] = (
            False,
            _time.monotonic() - 1,
        )
        assert not host_is_unsafe("ttl.example.com", resolve=True)
        assert calls == 2  # 过期后重新解析


def test_dns_cache_is_bounded() -> None:
    """缓存有界：超过上限后淘汰最旧条目，被淘汰主机重新解析。"""
    calls: dict[str, int] = {}

    def counting_getaddrinfo(host: str, *args: object, **kwargs: object):
        calls[host] = calls.get(host, 0) + 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    with patch("web_crawler._ssrf.socket.getaddrinfo", side_effect=counting_getaddrinfo):
        # 填满缓存（每个主机一次解析）
        for i in range(_DNS_CACHE_MAX_ENTRIES + 8):
            assert not host_is_unsafe(f"host-{i}.example.com", resolve=True)
        assert len(_dns_verdict_cache._data) <= _DNS_CACHE_MAX_ENTRIES
        # 最早的主机已被淘汰 → 再次调用会重新解析
        assert not host_is_unsafe("host-0.example.com", resolve=True)
        assert calls["host-0.example.com"] == 2


# ── IPv6 zone id / host 清洗边界 / Power Mode ───────────────────────────────


def test_ipv6_zone_id_is_stripped_before_check() -> None:
    """IPv6 带 zone id（fe80::1%eth0）时先剥离 zone id 再判定：链路本地不安全。"""
    assert is_private_ip("fe80::1%eth0")
    assert host_is_unsafe("fe80::1%eth0")


def test_host_of_only_dots_is_unsafe() -> None:
    """host 仅由点组成（如 "."）时 strip/rstrip 清洗后为空，按不安全处理。"""
    assert host_is_unsafe(".") is True
    assert host_is_unsafe("...") is True


def test_power_mode_bypasses_host_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Power Mode 下 validate_url_host 跳过 host 校验（私网/云元数据地址放行）。"""
    monkeypatch.setenv("WEB_CRAWLER_POWER_MODE", "1")
    # 私网 / 环回 / 云元数据地址均不再抛 ValueError
    validate_url_host("http://169.254.169.254/latest/meta-data/")
    validate_url_host("http://127.0.0.1:8080/")
    validate_url_host("http://10.0.0.1/internal")

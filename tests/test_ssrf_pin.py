"""curl_cffi / httpx 双路径 DNS 钉扎（TOCTOU）与 ``_ssrf_gate`` 归一化测试。

对应两个安全缺口的修复验证：

* **缺口 1（curl_cffi 路径 TOCTOU）**：入口门禁校验 DNS 后、libcurl 连接前，
  DNS 记录翻转（校验时公网、连接时私网）必须被拦截——钉扎路径下发送前
  即时重新解析并逐 IP 复核（任一私网结果抛 ``ValueError``），全族已校验
  IP 通过 ``CURLOPT_RESOLVE`` 钉给 libcurl，连接强制使用已校验地址。
* **缺口 2（``_ssrf_gate`` 归一化）**：``::ffff:127.0.0.1``、``2130706433``、
  ``0177.0.0.1``、``0x7f000001``、``127.1`` 等非规范写法必须复用
  ``_ssrf.normalize_ip_literal`` 归一化后被拒绝，deny-by-default 语义保留。

curl 分支测试通过 monkeypatch ``_load_curl_backend`` 注入 fake session，
不依赖真实 curl_cffi 安装；真实 ``CurlOpt.RESOLVE`` 键契约由 skipif 用例
在有 curl_cffi 的环境补充验证。httpx 分支沿用 fake httpx 模块模式。
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from web_crawler._ssrf import _dns_verdict_cache, normalize_ip_literal
from web_crawler.compat import HAS_CURL_CFFI
from web_crawler.fetchers import _pin as _pin_mod
from web_crawler.fetchers import fetcher as fetcher_mod
from web_crawler.fetchers._pin import CurlResolvePinner
from web_crawler.fetchers.fetcher import AsyncFetcher, Fetcher
from web_crawler.mcp._ssrf_gate import _check_url, _host_is_public

# 代替 CurlOpt.RESOLVE 的哨兵键：curl 分支主测试环境无关（无需安装 curl_cffi）
_RESOLVE_OPT = object()


# ── fake 设施 ────────────────────────────────────────────────────────────────


class _FakeCurlVersion:
    """curl_cffi.CurlHttpVersion 的最小替身（仅需 V1_1 属性存在）。"""

    V1_1 = "v1_1"


class _FakeRawResponse:
    """传输层原始响应的最小替身（200 / 无重定向 / 空 body）。"""

    url = "https://example.com/"
    status_code = 200
    content = b"ok"
    headers: dict[str, str] = {}


class _FakeCurlSession:
    """curl_cffi 同步 Session 替身：记录 request 参数与 curl_options 写入。"""

    def __init__(self, **kwargs: object) -> None:
        self.curl_options: dict[Any, Any] = {}
        self.captured: dict[str, Any] | None = None
        self.closed = False

    def request(self, **kwargs: Any) -> _FakeRawResponse:
        self.captured = kwargs
        return _FakeRawResponse()

    def close(self) -> None:
        self.closed = True


class _FakeAsyncCurlSession:
    """curl_cffi AsyncSession 替身（异步版，同 _FakeCurlSession）。"""

    def __init__(self, **kwargs: object) -> None:
        self.curl_options: dict[Any, Any] = {}
        self.captured: dict[str, Any] | None = None

    async def request(self, **kwargs: Any) -> _FakeRawResponse:
        self.captured = kwargs
        return _FakeRawResponse()

    async def close(self) -> None:
        return None


class _RoundsResolver:
    """按调用轮次依次返回不同解析结果的有状态 fake ``getaddrinfo``。

    模拟 DNS 重绑定（TOCTOU）：第 1 次调用（入口门禁校验）返回公网 IP，
    第 2 次调用（发送前钉扎解析）返回私网 IP；超出预置轮次后重复最后一轮
    （重试等意外多次调用时不至于 IndexError）。
    """

    def __init__(self, rounds: list[list[str]]) -> None:
        assert rounds, "至少预置一轮解析结果"
        self._rounds = list(rounds)
        self.calls: list[str] = []

    def __call__(self, host: str, port: object, *args: object, **kwargs: object) -> list[tuple]:
        self.calls.append(host)
        ips = self._rounds.pop(0) if len(self._rounds) > 1 else self._rounds[0]
        infos: list[tuple] = []
        for ip in ips:
            if ":" in ip:
                infos.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0)))
            else:
                infos.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)))
        return infos


@pytest.fixture(autouse=True)
def _isolated_pin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例独立环境：清代理环境变量（钉扎跳过条件）与 DNS 判定缓存。

    开发机 shell 常驻 HTTP_PROXY/HTTPS_PROXY 会让 ``env_proxy_configured``
    命中跳过条件；host 级门禁的 60s 判定缓存则会跨用例串扰解析次数计数。
    """
    for var in _pin_mod._ENV_PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    _dns_verdict_cache._data.clear()


def _use_fake_curl(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 Fetcher 走 curl 分支但绑定 fake session（无需安装 curl_cffi）。"""
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", True)
    monkeypatch.setattr(
        fetcher_mod,
        "_load_curl_backend",
        lambda: (_FakeCurlVersion, _FakeCurlSession, _FakeAsyncCurlSession, Exception),
    )
    # 哨兵 RESOLVE 键：钉扎写入 curl_options 的键与真实 CurlOpt.RESOLVE 等价
    monkeypatch.setattr(_pin_mod, "_curl_opt_resolve", lambda: _RESOLVE_OPT)


# ── 一、curl_cffi 路径：连接级 DNS 钉扎（缺口 1 核心） ───────────────────────


def test_curl_sync_pins_resolved_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """同步 curl 分支：全族已校验 IP 钉给 CURLOPT_RESOLVE，URL 保持原样。"""
    _use_fake_curl(monkeypatch)
    monkeypatch.setattr(socket, "getaddrinfo", _RoundsResolver([["93.184.216.34"]]))

    f = Fetcher(allow_private_hosts=False, timeout=5.0)
    try:
        f.get("https://example.com/")
        session = f._session
        assert isinstance(session, _FakeCurlSession)
        # 钉扎条目包含已校验 IP（连接强制使用该地址），且 URL 不改写
        assert session.curl_options[_RESOLVE_OPT] == ["example.com:443:93.184.216.34"]
        assert session.captured is not None
        assert session.captured["url"] == "https://example.com/"
    finally:
        f.close()


async def test_curl_async_pins_resolved_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步 curl 分支：钉扎行为与同步一致（CURLOPT_RESOLVE 条目写入）。"""
    _use_fake_curl(monkeypatch)
    monkeypatch.setattr(socket, "getaddrinfo", _RoundsResolver([["93.184.216.34"]]))

    async with AsyncFetcher(allow_private_hosts=False, timeout=5.0) as f:
        await f.get("https://example.com/")
        session = f._async_session
        assert isinstance(session, _FakeAsyncCurlSession)
        assert session.curl_options[_RESOLVE_OPT] == ["example.com:443:93.184.216.34"]
        assert session.captured is not None
        assert session.captured["url"] == "https://example.com/"


def test_curl_rebind_blocked_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """全链路 TOCTOU 攻击（同步）：门禁时公网、发送时私网 → 即时拦截。

    第一次解析（入口门禁）返回公网 IP 通过校验；发送前钉扎路径绕过 60s
    判定缓存重新解析，得到私网 IP 时抛 ``ValueError``，且底层连接从未发起。
    """
    _use_fake_curl(monkeypatch)
    resolver = _RoundsResolver([["93.184.216.34"], ["169.254.169.254"]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    f = Fetcher(allow_private_hosts=False, timeout=5.0)
    try:
        with pytest.raises(ValueError, match="TOCTOU"):
            f.get("https://rebind-sync.test/")
        # 连接从未发起：session.request 未被调用（或无新调用）
        session = f._session
        assert isinstance(session, _FakeCurlSession)
        assert session.captured is None
        assert resolver.calls == ["rebind-sync.test", "rebind-sync.test"]
    finally:
        f.close()


async def test_curl_rebind_blocked_async(monkeypatch: pytest.MonkeyPatch) -> None:
    """全链路 TOCTOU 攻击（异步）：DNS 翻转到私网同样即时拦截。"""
    _use_fake_curl(monkeypatch)
    resolver = _RoundsResolver([["93.184.216.34"], ["10.0.0.1"]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    async with AsyncFetcher(allow_private_hosts=False, timeout=5.0) as f:
        with pytest.raises(ValueError, match="TOCTOU"):
            await f.get("https://rebind-async.test/")
        session = f._async_session
        assert isinstance(session, _FakeAsyncCurlSession)
        assert session.captured is None


def test_curl_pin_covers_all_address_families(monkeypatch: pytest.MonkeyPatch) -> None:
    """全族钉扎：条目必须覆盖 A + AAAA 全部解析结果。

    libcurl 的 Happy Eyeballs 会对未覆盖的地址族继续走系统解析——只钉
    IPv4 而系统解析出 IPv6 时钉扎形同虚设（实测复现），故全族都必须钉。
    """
    _use_fake_curl(monkeypatch)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _RoundsResolver([["93.184.216.34", "2606:2800:220:1::1946"]]),
    )

    f = Fetcher(allow_private_hosts=False, timeout=5.0)
    try:
        f.get("https://dualstack.test/")
        session = f._session
        assert isinstance(session, _FakeCurlSession)
        assert session.curl_options[_RESOLVE_OPT] == [
            "dualstack.test:443:93.184.216.34",
            "dualstack.test:443:2606:2800:220:1::1946",
        ]
    finally:
        f.close()


def test_curl_pin_blocks_partial_family_rebind(monkeypatch: pytest.MonkeyPatch) -> None:
    """混合解析中任一地址翻转到私网（含 IPv6 私网）即整体拦截。"""
    _use_fake_curl(monkeypatch)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _RoundsResolver([["93.184.216.34"], ["93.184.216.34", "fd00::1"]]),
    )

    f = Fetcher(allow_private_hosts=False, timeout=5.0)
    try:
        with pytest.raises(ValueError, match="TOCTOU"):
            f.get("https://partial-rebind.test/")
    finally:
        f.close()


def test_curl_pin_skips_ip_literal_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """IP 字面量 URL 无解析步骤：不产生新钉扎条目、不额外解析。"""
    _use_fake_curl(monkeypatch)
    resolver = _RoundsResolver([["93.184.216.34"]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    f = Fetcher(allow_private_hosts=False, timeout=5.0)
    try:
        f.get("https://93.184.216.34/")
        session = f._session
        assert isinstance(session, _FakeCurlSession)
        # IP 字面量无钉扎步骤：不写入任何条目（入口校验对 numeric host 的
        # getaddrinfo 调用由系统直接应答，不经过 DNS，无 TOCTOU 需消除）
        assert _RESOLVE_OPT not in session.curl_options
    finally:
        f.close()


def test_curl_pin_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """代理 / 环境代理 / 关闭解析复查 / 放行私网时均不钉扎（行为不变）。"""
    _use_fake_curl(monkeypatch)
    monkeypatch.setattr(socket, "getaddrinfo", _RoundsResolver([["93.184.216.34"]]))

    # 显式代理：解析由代理解析，客户端侧无 DNS TOCTOU
    f_proxy = Fetcher(allow_private_hosts=False, timeout=5.0, proxy="http://127.0.0.1:9")
    try:
        f_proxy.get("https://gate-proxy.test/")
        assert _RESOLVE_OPT not in f_proxy._session.curl_options
    finally:
        f_proxy.close()

    # 环境变量代理（httpx trust_env / libcurl 同样读取）
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    f_env = Fetcher(allow_private_hosts=False, timeout=5.0)
    try:
        f_env.get("https://gate-env.test/")
        assert _RESOLVE_OPT not in f_env._session.curl_options
    finally:
        f_env.close()
    monkeypatch.delenv("HTTPS_PROXY", raising=False)

    # resolve_hosts=False：用户显式关闭 DNS 复查，钉扎随之关闭
    f_nores = Fetcher(allow_private_hosts=False, timeout=5.0, resolve_hosts=False)
    try:
        f_nores.get("https://gate-nores.test/")
        assert _RESOLVE_OPT not in f_nores._session.curl_options
    finally:
        f_nores.close()

    # Power Mode（allow_private_hosts=True）：放行私网时钉扎无意义，跳过
    f_power = Fetcher(allow_private_hosts=True, timeout=5.0)
    try:
        f_power.get("https://gate-power.test/")
        assert f_power._session.curl_options == {}
    finally:
        f_power.close()


def test_curl_pin_lru_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """钉扎条目按 host 计的 LRU 有界：超限淘汰最旧、最近使用被保留。"""
    ips = {
        "h1.test": "1.1.1.1",
        "h2.test": "2.2.2.2",
        "h3.test": "3.3.3.3",
        "h4.test": "4.4.4.4",
        "h5.test": "5.5.5.5",
    }

    def resolver(host: str, port: object, *args: object, **kwargs: object) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ips[host], port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    pinner = CurlResolvePinner(max_hosts=3)

    pinner.entries_for("https://h1.test/")
    pinner.entries_for("https://h2.test/")
    pinner.entries_for("https://h3.test/")
    assert set(pinner._entries) == {"h1.test", "h2.test", "h3.test"}

    # 第 4 个 host 挤出最旧的 h1
    pinner.entries_for("https://h4.test/")
    assert set(pinner._entries) == {"h2.test", "h3.test", "h4.test"}

    # 触碰 h2 后，h3 变为最旧；第 5 个 host 挤出 h3
    pinner.entries_for("https://h2.test/")
    pinner.entries_for("https://h5.test/")
    assert set(pinner._entries) == {"h2.test", "h4.test", "h5.test"}

    # 快照是 LRU 内全部 host 条目的并集（异步竞态保护，见 _pin.py 文档）
    snapshot = pinner.entries_for("https://h4.test/")
    assert set(snapshot) == {
        "h2.test:443:2.2.2.2",
        "h4.test:443:4.4.4.4",
        "h5.test:443:5.5.5.5",
    }


def test_curl_pin_snapshot_merges_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """多个 host 的钉扎条目在快照中并存（同一会话并发请求不同 host）。"""
    ips = {"a.test": "1.2.3.4", "b.test": "5.6.7.8"}

    def resolver(host: str, port: object, *args: object, **kwargs: object) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ips[host], port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    pinner = CurlResolvePinner(max_hosts=8)
    pinner.entries_for("https://a.test:8443/")
    snapshot = pinner.entries_for("https://b.test/")
    # a.test 非默认端口保留在条目里；b.test 默认 443
    assert set(snapshot) == {"a.test:8443:1.2.3.4", "b.test:443:5.6.7.8"}


def test_pinner_concurrent_entries_for_thread_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """多线程并发 entries_for：无异常、条目数不超过 max_hosts、快照必含自身条目。

    无锁时两个线程交错维护同一 dict：最坏双 pop 同一 key 抛 ``KeyError``，
    或刚写入的条目被另一线程的淘汰循环挤出——快照缺失该 host 的
    ``CURLOPT_RESOLVE`` 条目，SSRF DNS rebinding 的 TOCTOU 窗口重新打开。
    host 池（32 个）远大于 max_hosts（8），制造高频淘汰竞态。
    """
    ip = "93.184.216.34"

    def resolver(host: str, port: object, *args: object, **kwargs: object) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    max_hosts = 8
    hosts = [f"h{i}.test" for i in range(32)]
    n_threads, n_rounds = 8, 300
    pinner = CurlResolvePinner(max_hosts=max_hosts)
    errors: list[BaseException] = []
    missing: list[str] = []
    barrier = threading.Barrier(n_threads)

    def worker(tid: int) -> None:
        try:
            barrier.wait()  # 同步起跑，最大化交错概率
            for i in range(n_rounds):
                host = hosts[(tid * 31 + i * 7) % len(hosts)]
                snapshot = pinner.entries_for(f"https://{host}/")
                expected = f"{host}:443:{ip}"
                # LRU 维护与快照构造在锁内原子完成：自身条目必在快照中
                if expected not in snapshot:
                    missing.append(expected)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert missing == []
    assert len(pinner._entries) <= max_hosts


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi 未安装，无法验证真实 RESOLVE 键")
def test_curl_real_resolve_option_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实契约（需安装 curl_cffi）：钉扎写入的是 CurlOpt.RESOLVE 键。"""
    from curl_cffi.const import CurlOpt

    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", True)
    monkeypatch.setattr(
        fetcher_mod,
        "_load_curl_backend",
        lambda: (_FakeCurlVersion, _FakeCurlSession, _FakeAsyncCurlSession, Exception),
    )
    monkeypatch.setattr(socket, "getaddrinfo", _RoundsResolver([["93.184.216.34"]]))

    f = Fetcher(allow_private_hosts=False, timeout=5.0)
    try:
        f.get("https://contract.test/")
        session = f._session
        assert isinstance(session, _FakeCurlSession)
        assert session.curl_options.get(CurlOpt.RESOLVE) == ["contract.test:443:93.184.216.34"]
    finally:
        f.close()


def test_apply_curl_resolve_degrades_without_support(monkeypatch: pytest.MonkeyPatch) -> None:
    """降级路径：curl_cffi 缺失（无 RESOLVE 枚举）或 session 无 curl_options。"""
    monkeypatch.setattr(_pin_mod, "_curl_opt_resolve", lambda: None)
    assert _pin_mod.apply_curl_resolve(_FakeCurlSession(), ["h:443:1.2.3.4"]) is False

    class _NoOptions:
        pass

    monkeypatch.setattr(_pin_mod, "_curl_opt_resolve", lambda: _RESOLVE_OPT)
    assert _pin_mod.apply_curl_resolve(_NoOptions(), ["h:443:1.2.3.4"]) is False


# ── 二、httpx 路径：钉扎行为回归（不破坏现有契约） ──────────────────────────


def _use_fake_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 Fetcher 降级到 httpx 分支并绑定 fake client。

    调用方通过 ``f._session`` 取 fake client 实例，断言 ``request`` 收到的
    参数（钉扎改写后的 URL / Host 头 / sni 扩展）。
    """

    class _FakeHttpClient:
        def __init__(self, **kwargs: object) -> None:
            self.requests: list[dict[str, Any]] = []

        def request(self, **kwargs: Any) -> _FakeRawResponse:
            self.requests.append(kwargs)
            return _FakeRawResponse()

        def close(self) -> None:
            return None

    fake_httpx = MagicMock()
    fake_httpx.Client = _FakeHttpClient
    # _retry_errors 会取 httpx.HTTPError 作为 except 元组元素，必须是真实异常类
    fake_httpx.HTTPError = type("HTTPError", (BaseException,), {})
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: fake_httpx)


def test_httpx_pin_contract_on_rebind_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx 路径钉扎契约：连接 URL 是发送时刻已校验的 IP，Host/SNI 保持原名。

    模拟公网→公网的 DNS 变更（如 CDN 轮换）：门禁时 93.184.216.34 通过，
    发送时解析到另一公网 IP 198.51.100.7——钉扎后连接的就是后者（发送
    时刻校验的那个地址），Host 头与 sni_hostname 仍指向原始主机名。
    """
    _use_fake_httpx(monkeypatch)
    monkeypatch.setattr(
        socket, "getaddrinfo", _RoundsResolver([["93.184.216.34"], ["198.51.100.7"]])
    )

    with pytest.warns(RuntimeWarning):
        f = Fetcher(allow_private_hosts=False, timeout=5.0)
    try:
        f.get("https://example.com/")
        client = f._session
        assert client is not None
        assert len(client.requests) == 1
        request = client.requests[0]
        assert request["url"] == "https://198.51.100.7/"
        assert request["headers"]["Host"] == "example.com"
        assert request["extensions"] == {"sni_hostname": "example.com"}
    finally:
        f.close()


def test_httpx_pin_rebind_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx 路径 TOCTOU 攻击：DNS 翻转到私网 → 发送前即时拦截。"""
    _use_fake_httpx(monkeypatch)
    monkeypatch.setattr(
        socket, "getaddrinfo", _RoundsResolver([["93.184.216.34"], ["192.168.0.1"]])
    )

    with pytest.warns(RuntimeWarning):
        f = Fetcher(allow_private_hosts=False, timeout=5.0)
    try:
        with pytest.raises(ValueError, match="TOCTOU"):
            f.get("https://rebind-httpx.test/")
        client = f._session
        assert client is not None
        assert client.requests == []  # 连接从未发起
    finally:
        f.close()


def test_httpx_power_mode_keeps_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Power Mode（allow_private_hosts=True）：httpx 路径 URL 原样直发。"""
    _use_fake_httpx(monkeypatch)
    resolver = _RoundsResolver([["169.254.169.254"]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    with pytest.warns(RuntimeWarning):
        f = Fetcher(allow_private_hosts=True, timeout=5.0)
    try:
        f.get("https://metadata-power.test/")
        client = f._session
        assert client is not None
        assert len(client.requests) == 1
        request = client.requests[0]
        assert request["url"] == "https://metadata-power.test/"
        assert "Host" not in request["headers"]
        assert request["extensions"] == {}
        # 入口校验与钉扎均跳过，全程无 DNS 解析
        assert resolver.calls == []
    finally:
        f.close()


# ── 三、normalize_ip_literal 归一化（共享归一化核心） ─────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("93.184.216.34", "93.184.216.34"),
        ("2130706433", "127.0.0.1"),  # 单段十进制
        ("0177.0.0.1", "127.0.0.1"),  # 八进制段
        ("0x7f000001", "127.0.0.1"),  # 0x 十六进制
        ("127.1", "127.0.0.1"),  # 短形式（inet_aton 语义）
        ("::ffff:127.0.0.1", "127.0.0.1"),  # IPv4-mapped IPv6 解包
        ("::FFFF:10.0.0.1", "10.0.0.1"),  # 大写 FFFF 与小写归一
        ("fe80::1%eth0", "fe80::1"),  # zone id 剥离
        ("2001:db8::1", "2001:db8::1"),
    ],
)
def test_normalize_ip_literal_variants(raw: str, expected: str) -> None:
    """非规范 IP 写法全部归一化到正确地址（含 mapped IPv6 显式解包）。"""
    addr = normalize_ip_literal(raw)
    assert addr is not None
    assert str(addr) == expected


@pytest.mark.parametrize(
    "raw",
    ["example.com", "", " ", "999.999.999.999", "1.2.3.4.5", "0x", "localhost"],
)
def test_normalize_ip_literal_rejects_non_ip(raw: str) -> None:
    """非 IP 字面量（域名 / 空串 / 超界段数 / 裸 0x）返回 None。"""
    assert normalize_ip_literal(raw) is None


def test_normalize_ip_literal_types() -> None:
    """返回类型正确：IPv4 写法给 IPv4Address，纯 IPv6 给 IPv6Address。"""
    assert isinstance(normalize_ip_literal("127.0.0.1"), ipaddress.IPv4Address)
    assert isinstance(normalize_ip_literal("::1"), ipaddress.IPv6Address)


# ── 四、_ssrf_gate 非规范 IP 归一化（缺口 2） ────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        "2130706433",
        "0177.0.0.1",
        "0x7f000001",
        "127.1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",  # mapped 云元数据
        "::ffff:10.0.0.1",
        "::ffff:192.168.0.1",
    ],
)
def test_ssrf_gate_rejects_noncanonical_private(host: str) -> None:
    """非规范写法的私网/环回/云元数据地址全部被拒绝（不进公网判定）。"""
    assert _host_is_public(host) is False


@pytest.mark.parametrize(
    "host",
    ["8.8.8.8", "1.1.1.1", "93.184.216.34", "::ffff:8.8.8.8", "2001:4860:4860::8888"],
)
def test_ssrf_gate_allows_public_literals(host: str) -> None:
    """公网字面量（含 mapped IPv6 形式）仍正常放行——正向回归。"""
    assert _host_is_public(host) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://0x7f000001/",
        "http://127.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:169.254.169.254]/",
    ],
)
def test_ssrf_gate_check_url_rejects_noncanonical(url: str) -> None:
    """URL 门禁集成层面：非规范 host 写法返回错误信息（拒绝）。"""
    assert _check_url(url) is not None


def test_ssrf_gate_deny_by_default_on_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """解析失败的未知主机按"非公网"拒绝（deny by default 语义保留）。"""

    def _fail(host: str, *args: object, **kwargs: object) -> list[tuple]:
        raise socket.gaierror("dns lookup failed")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    assert _host_is_public("nonexistent.invalid") is False


def test_ssrf_gate_rejects_hostname_resolving_to_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """主机名解析到私网（含非规范形式回填）同样整体拒绝。"""
    monkeypatch.setattr(socket, "getaddrinfo", _RoundsResolver([["10.0.0.5"]]))
    assert _host_is_public("internal.example") is False

    monkeypatch.setattr(socket, "getaddrinfo", _RoundsResolver([["93.184.216.34", "fd00::1"]]))
    assert _host_is_public("mixed.example") is False


def test_ssrf_gate_allows_hostname_resolving_to_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """主机名全部解析到公网时放行（现有行为不变）。"""
    monkeypatch.setattr(socket, "getaddrinfo", _RoundsResolver([["93.184.216.34"]]))
    assert _host_is_public("public.example") is True

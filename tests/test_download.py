"""Tests for ``Fetcher.download`` / ``AsyncFetcher.download``（流式下载 + 断点续传）。

全程 mock 网络（不发真实请求）：httpx 路径以 fake ``Client`` 拦截
``request(stream=True)``；curl_cffi 路径 patch ``_load_curl_backend`` 注入
fake ``Session`` / ``AsyncSession``。功能测试统一 ``allow_private_hosts=True``
+ 环回 URL（跳过 DNS 解析），SSRF 生效性由独立测试验证。

审查缺陷回归（docstring 中标注 H-1/H-3/M-1/L-2/L-3）与覆盖补齐
（resume 开关、StreamContext 幂等、代理池轮换、async 镜像套件等）
均在此文件。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from web_crawler.exceptions import ConfigError, FetchError, UnsafeTargetError
from web_crawler.fetchers import _download as download_mod
from web_crawler.fetchers import fetcher as fetcher_mod
from web_crawler.fetchers.fetcher import AsyncFetcher, Fetcher
from web_crawler.fetchers.proxy import ProxyPool

# 功能测试统一使用的环回目标（allow_private_hosts=True 时跳过 DNS 复查）
_TARGET = "http://127.0.0.1:9/files/x.bin"

# SSRF 校验可通过的公网 IP 字面量（getaddrinfo 对数字 IP 纯本地解析，无 DNS 查询；
# 用于 allow_private_hosts=False 场景下构造「初始目标合法 → 重定向目标非法」）
_PUBLIC_TARGET = "http://8.8.8.8/files/x.bin"

# 云元数据链路本地地址（allow_private_hosts=False 时被 SSRF 防护拒绝）
_UNSAFE_LOCATION = "http://169.254.169.254/latest/meta-data/"


class _Canned:
    """按序回放的响应队列（元素为响应对象或异常实例）。"""

    def __init__(self, *items: Any) -> None:
        self.items: list[Any] = list(items)
        self.requests: list[dict[str, Any]] = []

    def pop(self) -> Any:
        assert self.items, "unexpected extra request (canned responses exhausted)"
        return self.items.pop(0)


class _FakeStreamResponse:
    """流式响应桩：同步/异步、curl/httpx 四套消费接口全量提供。"""

    def __init__(
        self,
        status_code: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._chunks = (
            chunks if chunks is not None else [body[i : i + 8] for i in range(0, len(body), 8)]
        )
        self.close_count = 0

    def iter_bytes(self, chunk_size: int) -> Iterator[bytes]:
        """httpx 同步路径。"""
        yield from self._chunks

    async def aiter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
        """httpx 异步路径。"""
        for chunk in self._chunks:
            yield chunk

    def iter_content(self) -> Iterator[bytes]:
        """curl_cffi 同步路径（不传 chunk_size，规避 0.16 的 ignored 警告）。"""
        yield from self._chunks

    async def aiter_content(self) -> AsyncIterator[bytes]:
        """curl_cffi 异步路径。"""
        for chunk in self._chunks:
            yield chunk

    def close(self) -> None:
        self.close_count += 1

    async def aclose(self) -> None:
        self.close_count += 1


class _EnospcFile:
    """写盘句柄包装：``write`` 抛 OSError(28)（磁盘满模拟），``close`` 透传。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def write(self, data: bytes) -> int:
        raise OSError(28, "No space left on device")

    def close(self) -> None:
        self._inner.close()


class _BoomCloseFile:
    """写盘句柄包装：``write`` 透传、``close`` 先真关再抛（缓冲刷盘失败模拟）。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def write(self, data: bytes) -> int:
        return self._inner.write(data)

    def close(self) -> None:
        self._inner.close()
        raise OSError(28, "No space left on device (flush)")


def _fake_httpx(canned: _Canned) -> Any:
    """构造 fake httpx 模块：同步/异步客户端共用同一 canned 回放队列。"""

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: Any) -> Any:
            canned.requests.append(kwargs)
            item = canned.pop()
            if isinstance(item, BaseException):
                raise item
            return item

        def close(self) -> None:
            pass

    class _FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: Any) -> Any:
            canned.requests.append(kwargs)
            item = canned.pop()
            if isinstance(item, BaseException):
                raise item
            return item

        async def aclose(self) -> None:
            pass

    fake = MagicMock(spec=["Client", "AsyncClient", "HTTPError"])
    fake.Client = _FakeClient
    fake.AsyncClient = _FakeAsyncClient
    fake.HTTPError = type("HTTPError", (Exception,), {})
    return fake


def _fake_curl_backend(canned_sync: _Canned, canned_async: _Canned) -> Any:
    """构造 fake curl_cffi 后端四元组（Session / AsyncSession 按序回放）。"""

    class _FakeCurlSession:
        def __init__(self, **kwargs: object) -> None:
            pass

        def request(self, **kwargs: Any) -> Any:
            canned_sync.requests.append(kwargs)
            item = canned_sync.pop()
            if isinstance(item, BaseException):
                raise item
            return item

        def close(self) -> None:
            pass

    class _FakeCurlAsyncSession:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def request(self, **kwargs: Any) -> Any:
            canned_async.requests.append(kwargs)
            item = canned_async.pop()
            if isinstance(item, BaseException):
                raise item
            return item

        async def close(self) -> None:
            pass

    curl_error = type("CurlRequestError", (OSError,), {})
    return (MagicMock(), _FakeCurlSession, _FakeCurlAsyncSession, curl_error)


def _sync_fetcher_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *items: Any,
    retries: int = 0,
    proxy: str | ProxyPool | None = None,
) -> tuple[Fetcher, _Canned]:
    """构造走 httpx 兜底路径的同步 Fetcher（canned 响应按序回放）。"""
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)
    canned = _Canned(*items)
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: _fake_httpx(canned))
    with pytest.warns(RuntimeWarning):
        return Fetcher(allow_private_hosts=True, retries=retries, proxy=proxy), canned


def _async_fetcher_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *items: Any,
    retries: int = 0,
    proxy: str | ProxyPool | None = None,
) -> tuple[AsyncFetcher, _Canned]:
    """构造走 httpx 兜底路径的 AsyncFetcher。"""
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)
    canned = _Canned(*items)
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: _fake_httpx(canned))
    with pytest.warns(RuntimeWarning):
        return AsyncFetcher(allow_private_hosts=True, retries=retries, proxy=proxy), canned


def _sync_fetcher_curl(
    monkeypatch: pytest.MonkeyPatch, *items: Any, retries: int = 0
) -> tuple[Fetcher, _Canned]:
    """构造走 curl_cffi 主路径的同步 Fetcher。"""
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", True)
    canned = _Canned(*items)
    backend = _fake_curl_backend(canned, _Canned())
    monkeypatch.setattr(fetcher_mod, "_load_curl_backend", lambda: backend)
    return Fetcher(allow_private_hosts=True, retries=retries), canned


def _async_fetcher_curl(
    monkeypatch: pytest.MonkeyPatch, *items: Any, retries: int = 0
) -> tuple[AsyncFetcher, _Canned]:
    """构造走 curl_cffi 主路径的 AsyncFetcher。"""
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", True)
    canned = _Canned(*items)
    backend = _fake_curl_backend(_Canned(), canned)
    monkeypatch.setattr(fetcher_mod, "_load_curl_backend", lambda: backend)
    return AsyncFetcher(allow_private_hosts=True, retries=retries), canned


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """干掉下载重试循环的退避 sleep（重试类测试不等真实退避）。"""
    monkeypatch.setattr(download_mod, "time", SimpleNamespace(sleep=lambda *_: None))


def _no_async_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """干掉异步下载重试循环的退避 sleep（async 版 _no_sleep）。"""

    async def _sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(download_mod, "asyncio", SimpleNamespace(sleep=_sleep))


def _patch_local_file_error(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_open: bool = False,
    on_write: bool = False,
    on_close: bool = False,
) -> None:
    """让 ``.part`` 的写盘句柄在指定环节抛本地 OSError（磁盘满/权限模拟）。

    仅拦截 ``.part`` 的写模式（``w``/``a``）打开，读取路径不受影响（断言
    阶段仍可 ``read_bytes`` 校验半成品内容）。
    """
    real_open = Path.open

    def _patched_open(self: Path, mode: str = "r", **kwargs: Any) -> Any:
        writable = "w" in mode or "a" in mode
        if not (writable and self.name.endswith(download_mod.PART_SUFFIX)):
            return real_open(self, mode, **kwargs)
        if on_open:
            raise OSError(5, "Access is denied")
        handle = real_open(self, mode, **kwargs)
        if on_write:
            return _EnospcFile(handle)
        if on_close:
            return _BoomCloseFile(handle)
        return handle

    monkeypatch.setattr(Path, "open", _patched_open)


def _has_range_header(sent: dict[str, Any]) -> bool:
    """发出的请求头里是否含任意大小写形式的 Range 头。"""
    return any(k.casefold() == "range" for k in sent)


# ── Content-Range 解析辅助（H-3 / M-1 的容错分支） ───────────────────────


def test_range_start_parsing_branches() -> None:
    """_range_start：标准/省略尾部/无头/416 形式/非标准变形各分支。"""
    assert download_mod._range_start({"Content-Range": "bytes 0-15/16"}) == 0
    assert download_mod._range_start({"Content-Range": "bytes 8-15/16"}) == 8
    assert download_mod._range_start({"Content-Range": "bytes 2-"}) == 2
    assert download_mod._range_start({}) is None
    assert download_mod._range_start(None) is None
    assert download_mod._range_start({"Content-Range": "bytes */16"}) is None
    assert download_mod._range_start({"Content-Range": "bytes=0-15/16"}) is None
    assert download_mod._range_start({"Content-Range": "garbage"}) is None
    assert download_mod._range_start({"Content-Range": 123}) is None  # type: ignore[dict-item]


def test_range_total_parsing_branches() -> None:
    """_range_total：bytes */N 形式解析，其余（含 206 形式/无头）返回 None。"""
    assert download_mod._range_total({"Content-Range": "bytes */21"}) == 21
    assert download_mod._range_total({"Content-Range": "bytes */0"}) == 0
    assert download_mod._range_total({"Content-Range": "bytes 0-15/16"}) is None
    assert download_mod._range_total({}) is None
    assert download_mod._range_total(None) is None
    assert download_mod._range_total({"Content-Range": "items 0-1/2"}) is None


# ── StreamContext 幂等 / 句柄归还 ────────────────────────────────────────


def test_stream_context_close_idempotent_releases_once() -> None:
    """StreamContext 幂等：重复 close 只关流一次、只归还池句柄一次。"""
    releases: list[bool] = []
    raw = _FakeStreamResponse(200, b"")
    fake_fetcher = SimpleNamespace(
        _release_pooled=lambda handle, *, success: releases.append(success)
    )
    ctx = download_mod.StreamContext(raw, object(), fetcher=fake_fetcher, use_curl=False)
    assert list(ctx.iter_chunks(8)) == []
    ctx.close()
    ctx.close()
    assert raw.close_count == 1
    assert releases == [True]  # 流正常耗尽 → 健康归还，且只归还一次


def test_stream_context_close_swallows_raw_close_error() -> None:
    """raw.close() 抛异常：句柄仍归还、异常不外抛（best-effort 关闭）。"""
    releases: list[bool] = []

    class _EvilRaw(_FakeStreamResponse):
        def close(self) -> None:
            raise RuntimeError("backend close blew up")

    raw = _EvilRaw(200)
    fake_fetcher = SimpleNamespace(
        _release_pooled=lambda handle, *, success: releases.append(success)
    )
    ctx = download_mod.StreamContext(raw, object(), fetcher=fake_fetcher, use_curl=False)
    ctx.close()  # 不外抛
    assert releases == [False]
    ctx.close()  # 幂等：不再重复归还
    assert releases == [False]


async def test_async_stream_context_aclose_idempotent_releases_once() -> None:
    """aclose 幂等：重复调用只关流一次、只归还池句柄一次。"""
    releases: list[bool] = []

    async def _release(handle: object, *, success: bool) -> None:
        releases.append(success)

    raw = _FakeStreamResponse(200, b"")
    fake_fetcher = SimpleNamespace(_release_pooled_async=_release)
    ctx = download_mod.StreamContext(raw, object(), fetcher=fake_fetcher, use_curl=False)
    assert [chunk async for chunk in ctx.aiter_chunks(8)] == []
    await ctx.aclose()
    await ctx.aclose()
    assert raw.close_count == 1
    assert releases == [True]


async def test_async_stream_context_aclose_swallows_raw_error() -> None:
    """raw.aclose() 抛异常：句柄仍归还、异常不外抛。"""
    releases: list[bool] = []

    async def _release(handle: object, *, success: bool) -> None:
        releases.append(success)

    class _EvilRaw(_FakeStreamResponse):
        async def aclose(self) -> None:
            raise RuntimeError("backend aclose blew up")

    raw = _EvilRaw(200)
    fake_fetcher = SimpleNamespace(_release_pooled_async=_release)
    ctx = download_mod.StreamContext(raw, object(), fetcher=fake_fetcher, use_curl=False)
    await ctx.aclose()  # 不外抛
    assert releases == [False]
    await ctx.aclose()
    assert releases == [False]


# ── 同步：httpx 兜底路径 ─────────────────────────────────────────────────


def test_download_sync_httpx_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """完整下载：分块拼接落盘、返回绝对 Path、.part 已原子替换。"""
    body = b"0123456789abcdef" * 4
    f, canned = _sync_fetcher_httpx(
        monkeypatch, _FakeStreamResponse(200, body, {"content-type": "application/x-bin"})
    )
    dest = tmp_path / "x.bin"
    result = f.download(_TARGET, dest, chunk_size=8)
    assert result.is_absolute()
    assert result == dest
    assert dest.read_bytes() == body
    assert not (tmp_path / "x.bin.part").exists()
    # 无半成品时不发 Range 头
    assert "Range" not in canned.requests[0]["headers"]
    assert canned.requests[0]["stream"] is True


def test_download_sync_resume_206_appends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """断点续传：预置 .part + 206（Content-Range 对齐）→ 发 Range 并追加。"""
    dest = tmp_path / "x.bin"
    part = tmp_path / "x.bin.part"
    part.write_bytes(b"AB")
    f, canned = _sync_fetcher_httpx(
        monkeypatch, _FakeStreamResponse(206, b"CD", {"Content-Range": "bytes 2-3/4"})
    )
    result = f.download(_TARGET, dest)
    assert result == dest
    assert dest.read_bytes() == b"ABCD"
    assert canned.requests[0]["headers"]["Range"] == "bytes=2-"


def test_download_sync_resume_200_rewrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """服务器不支持 Range（回 200）→ 截断重写半成品。"""
    dest = tmp_path / "x.bin"
    (tmp_path / "x.bin.part").write_bytes(b"AB")
    f, canned = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, b"XYZ"))
    f.download(_TARGET, dest)
    assert dest.read_bytes() == b"XYZ"
    assert canned.requests[0]["headers"]["Range"] == "bytes=2-"


def test_download_sync_resume_206_misaligned_start_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-3 回归：206 起始偏移错位（服务器重发全量）→ 截断重写，不拼接。

    审查实验复现场景：.part 4 字节 + 206 + ``bytes 0-15/16`` + 全量 body
    ——修复前 append 拼接产出 20 字节损坏内容，修复后重写为 16 字节正确内容。
    """
    body = b"0123456789abcdef"
    dest = tmp_path / "x.bin"
    (tmp_path / "x.bin.part").write_bytes(b"ABCD")
    f, canned = _sync_fetcher_httpx(
        monkeypatch,
        _FakeStreamResponse(206, body, {"Content-Range": f"bytes 0-{len(body) - 1}/{len(body)}"}),
    )
    f.download(_TARGET, dest)
    assert dest.read_bytes() == body
    assert canned.requests[0]["headers"]["Range"] == "bytes=4-"


def test_download_sync_resume_206_other_start_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-3 回归：206 Content-Range 起始为其他错位值（bytes 8-）同样回退重写。"""
    dest = tmp_path / "x.bin"
    (tmp_path / "x.bin.part").write_bytes(b"AB")
    f, _ = _sync_fetcher_httpx(
        monkeypatch, _FakeStreamResponse(206, b"01234567", {"Content-Range": "bytes 8-15/16"})
    )
    f.download(_TARGET, dest)
    assert dest.read_bytes() == b"01234567"  # 重写而非 b"AB01234567"


def test_download_sync_resume_206_missing_content_range_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-3 回归：206 无 Content-Range 头（服务器不支持正确续传）→ 截断重写。"""
    dest = tmp_path / "x.bin"
    (tmp_path / "x.bin.part").write_bytes(b"AB")
    f, _ = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(206, b"XY"))
    f.download(_TARGET, dest)
    assert dest.read_bytes() == b"XY"


def test_download_sync_416_part_already_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-1 回归：416 + Content-Range: bytes */N 且 N == .part 大小 → 直接落盘。

    修复前 416 无条件删 .part——已完整的半成品数据全丢；修复后原子落盘收尾。
    """
    body = b"0123456789abcdefghijk"
    dest = tmp_path / "x.bin"
    part = tmp_path / "x.bin.part"
    part.write_bytes(body)
    f, canned = _sync_fetcher_httpx(
        monkeypatch, _FakeStreamResponse(416, headers={"Content-Range": f"bytes */{len(body)}"})
    )
    result = f.download(_TARGET, dest)
    assert result == dest
    assert dest.read_bytes() == body
    assert not part.exists()
    assert canned.requests[0]["headers"]["Range"] == f"bytes={len(body)}-"


def test_download_sync_416_declared_mismatch_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-1：416 声明的完整长度与 .part 大小不符 → 按 416 报错并清理半成品。"""
    dest = tmp_path / "x.bin"
    (tmp_path / "x.bin.part").write_bytes(b"AB")
    f, _ = _sync_fetcher_httpx(
        monkeypatch, _FakeStreamResponse(416, headers={"Content-Range": "bytes */21"})
    )
    with pytest.raises(FetchError, match="HTTP Error 416") as exc_info:
        f.download(_TARGET, dest)
    assert getattr(exc_info.value, "status", None) == 416
    assert not (tmp_path / "x.bin.part").exists()
    assert not dest.exists()


def test_download_sync_416_without_content_range_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-1：416 无 Content-Range 头 → 按现有错误路径抛 FetchError。"""
    dest = tmp_path / "x.bin"
    f, _ = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(416))
    with pytest.raises(FetchError, match="HTTP Error 416"):
        f.download(_TARGET, dest)


def test_download_sync_user_range_header_stripped_without_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-2 回归：无 .part 时剔除用户显式传入的 Range 头（防静默部分文件）。"""
    dest = tmp_path / "x.bin"
    body = b"full-file-body"
    f, canned = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, body))
    f.download(_TARGET, dest, headers={"Range": "bytes=100-"})
    assert dest.read_bytes() == body
    assert not _has_range_header(canned.requests[0]["headers"])


def test_download_sync_user_range_header_casefold_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-2：用户 Range 头大小写不敏感剔除（HTTP 头名大小写不敏感）。"""
    dest = tmp_path / "x.bin"
    f, canned = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, b"ok"))
    f.download(_TARGET, dest, headers={"rAnGe": "bytes=5-"})
    assert dest.read_bytes() == b"ok"
    assert not _has_range_header(canned.requests[0]["headers"])


def test_download_sync_user_range_overridden_when_resuming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-2：offset > 0 时内部 Range 覆盖用户显式值（以 .part 实际大小为准）。"""
    dest = tmp_path / "x.bin"
    (tmp_path / "x.bin.part").write_bytes(b"pre")
    f, canned = _sync_fetcher_httpx(
        monkeypatch, _FakeStreamResponse(206, b"fix", {"Content-Range": "bytes 3-"})
    )
    f.download(_TARGET, dest, headers={"Range": "bytes=100-"})
    assert dest.read_bytes() == b"prefix"
    assert canned.requests[0]["headers"]["Range"] == "bytes=3-"


def test_download_sync_local_write_failure_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-3 回归：写盘 OSError（磁盘满模拟）不当网络错误重试。

    断言：请求只发一次（不触发退避重试）、抛 FetchError、.part 保留。
    """
    dest = tmp_path / "x.bin"
    _patch_local_file_error(monkeypatch, on_write=True)
    f, canned = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, b"z" * 16), retries=1)
    with pytest.raises(FetchError, match="local write failure"):
        f.download(_TARGET, dest)
    assert len(canned.requests) == 1
    assert (tmp_path / "x.bin.part").exists()
    assert not dest.exists()


def test_download_sync_local_replace_failure_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-3：原子落盘阶段（os.replace）的本地 OSError 同样不重试、.part 保留。"""
    dest = tmp_path / "x.bin"
    body = b"fully-downloaded"

    def _boom_replace(src: str, dst: str) -> None:
        raise OSError(13, "target file busy")

    monkeypatch.setattr(download_mod.os, "replace", _boom_replace)
    f, canned = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, body), retries=1)
    with pytest.raises(FetchError, match="local write failure"):
        f.download(_TARGET, dest)
    assert len(canned.requests) == 1
    assert (tmp_path / "x.bin.part").read_bytes() == body
    assert not dest.exists()


def test_download_sync_resume_false_starts_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resume=False：预置 .part 仍从头下载（无 Range 头、wb 截断重写）。"""
    dest = tmp_path / "x.bin"
    body = b"fresh-content"
    (tmp_path / "x.bin.part").write_bytes(b"STALE")
    f, canned = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, body))
    f.download(_TARGET, dest, resume=False)
    assert dest.read_bytes() == body
    assert "Range" not in canned.requests[0]["headers"]
    assert not (tmp_path / "x.bin.part").exists()


def test_download_sync_local_open_failure_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-3：``.part`` 打开失败（权限/磁盘满）不重试、抛 FetchError。"""
    dest = tmp_path / "x.bin"
    _patch_local_file_error(monkeypatch, on_open=True)
    f, canned = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, b"z" * 16), retries=1)
    with pytest.raises(FetchError, match="local write failure"):
        f.download(_TARGET, dest)
    assert len(canned.requests) == 1
    assert not dest.exists()


def test_download_sync_local_close_failure_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-3：close 刷盘失败不重试、.part 内容完整保留（write 均已成功）。"""
    dest = tmp_path / "x.bin"
    body = b"fully-buffered"
    _patch_local_file_error(monkeypatch, on_close=True)
    f, canned = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, body), retries=1)
    with pytest.raises(FetchError, match="local write failure"):
        f.download(_TARGET, dest)
    assert len(canned.requests) == 1
    assert (tmp_path / "x.bin.part").read_bytes() == body
    assert not dest.exists()


def test_download_sync_max_bytes_exceeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """max_bytes 超限：中止、清理 .part、抛 FetchError、dest 不落盘。"""
    dest = tmp_path / "x.bin"
    f, _ = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, b"z" * 64))
    with pytest.raises(FetchError, match="max_bytes"):
        f.download(_TARGET, dest, max_bytes=10)
    assert not (tmp_path / "x.bin.part").exists()
    assert not dest.exists()


def test_download_sync_server_error_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """服务器 4xx 状态：FetchError（带 status 属性）且 .part 被清理。"""
    dest = tmp_path / "x.bin"
    (tmp_path / "x.bin.part").write_bytes(b"stale")
    f, _ = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(404))
    with pytest.raises(FetchError, match="HTTP Error 404") as exc_info:
        f.download(_TARGET, dest)
    assert getattr(exc_info.value, "status", None) == 404
    assert not (tmp_path / "x.bin.part").exists()
    assert not dest.exists()


def test_download_sync_discard_part_unlink_failure_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败清理时 .part 删除失败（权限等）：不掩盖原始 FetchError。"""
    dest = tmp_path / "x.bin"
    (tmp_path / "x.bin.part").write_bytes(b"stale")

    def _boom_unlink(self: Path, missing_ok: bool = False) -> None:
        raise OSError("cannot unlink")

    monkeypatch.setattr(Path, "unlink", _boom_unlink)
    f, _ = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(404))
    with pytest.raises(FetchError, match="HTTP Error 404"):
        f.download(_TARGET, dest)


def test_download_sync_abort_keeps_part(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """should_abort 返回 True：中止并保留 .part 半成品供续传。"""
    dest = tmp_path / "x.bin"
    part = tmp_path / "x.bin.part"
    calls = {"n": 0}

    def _abort() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # 第一个分块放行，第二个分块中止

    f, _ = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, chunks=[b"AB", b"CD"]))
    with pytest.raises(FetchError, match="aborted"):
        f.download(_TARGET, dest, should_abort=_abort)
    assert part.read_bytes() == b"AB"
    assert not dest.exists()


def test_download_sync_on_headers_callback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """on_headers 回调收到最终响应的 (status, headers)。"""
    seen: list[tuple[int, dict[str, str]]] = []
    f, _ = _sync_fetcher_httpx(
        monkeypatch,
        _FakeStreamResponse(200, b"ok", {"content-type": "image/png"}),
    )
    f.download(_TARGET, tmp_path / "x.png", on_headers=lambda s, h: seen.append((s, dict(h))))
    assert seen == [(200, {"content-type": "image/png"})]


def test_download_sync_creates_missing_parent_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dest 的父目录不存在时自动创建。"""
    f, _ = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, b"data"))
    dest = tmp_path / "a" / "b" / "x.bin"
    f.download(_TARGET, dest)
    assert dest.read_bytes() == b"data"


def test_download_sync_invalid_args(tmp_path: Path) -> None:
    """chunk_size / max_bytes 非法时立即抛 ConfigError（不发请求）。"""
    f = Fetcher(allow_private_hosts=True)
    with pytest.raises(ConfigError, match="chunk_size"):
        f.download(_TARGET, tmp_path / "x.bin", chunk_size=0)
    with pytest.raises(ConfigError, match="max_bytes"):
        f.download(_TARGET, tmp_path / "x.bin", max_bytes=-1)


def test_download_sync_429_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """429（Retry-After: 0）按退避重试后成功落盘。"""
    _no_sleep(monkeypatch)
    dest = tmp_path / "x.bin"
    f, canned = _sync_fetcher_httpx(
        monkeypatch,
        _FakeStreamResponse(429, headers={"Retry-After": "0"}),
        _FakeStreamResponse(200, b"payload"),
        retries=1,
    )
    f.download(_TARGET, dest)
    assert dest.read_bytes() == b"payload"
    assert len(canned.requests) == 2


def test_download_sync_429_exhausted_raises_status_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """429 重试耗尽：抛 FetchError（带 status 属性）并清理 .part。"""
    _no_sleep(monkeypatch)
    dest = tmp_path / "x.bin"
    f, canned = _sync_fetcher_httpx(
        monkeypatch,
        _FakeStreamResponse(429, headers={"Retry-After": "0"}),
        _FakeStreamResponse(429, headers={"Retry-After": "0"}),
        retries=1,
    )
    with pytest.raises(FetchError, match="HTTP Error 429") as exc_info:
        f.download(_TARGET, dest)
    assert getattr(exc_info.value, "status", None) == 429
    assert len(canned.requests) == 2
    assert not (tmp_path / "x.bin.part").exists()


def test_download_sync_stream_setup_error_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """流建立阶段传输错误（canned 放异常实例）：退避重试后成功。"""
    _no_sleep(monkeypatch)
    dest = tmp_path / "x.bin"
    f, canned = _sync_fetcher_httpx(
        monkeypatch, OSError("conn refused"), _FakeStreamResponse(200, b"retry-ok"), retries=1
    )
    f.download(_TARGET, dest)
    assert dest.read_bytes() == b"retry-ok"
    assert len(canned.requests) == 2


def test_download_sync_stream_setup_exhausted_raises_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """流建立错误重试耗尽：抛原始 OSError（非 FetchError）。"""
    _no_sleep(monkeypatch)
    dest = tmp_path / "x.bin"
    f, canned = _sync_fetcher_httpx(
        monkeypatch, OSError("conn refused"), OSError("conn refused"), retries=1
    )
    with pytest.raises(OSError, match="conn refused"):
        f.download(_TARGET, dest)
    assert len(canned.requests) == 2
    assert not dest.exists()


def test_download_sync_network_error_retries_then_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """传输错误保留 .part，重试自动转 Range 续传完成下载。"""
    _no_sleep(monkeypatch)
    dest = tmp_path / "x.bin"

    class _Broken(_FakeStreamResponse):
        def iter_bytes(self, chunk_size: int) -> Iterator[bytes]:
            yield b"AB"
            raise OSError("connection reset")

    f, canned = _sync_fetcher_httpx(
        monkeypatch,
        _Broken(200),
        _FakeStreamResponse(206, b"CD", {"Content-Range": "bytes 2-3/4"}),
        retries=1,
    )
    f.download(_TARGET, dest)
    assert dest.read_bytes() == b"ABCD"
    # 第二次请求基于 .part 实际大小续传
    assert canned.requests[1]["headers"]["Range"] == "bytes=2-"


def test_download_sync_transport_exhausted_raises_original_keeps_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """传输错误重试耗尽：抛原始 OSError（非 FetchError）且 .part 保留。"""
    _no_sleep(monkeypatch)
    dest = tmp_path / "x.bin"

    class _Broken(_FakeStreamResponse):
        def iter_bytes(self, chunk_size: int) -> Iterator[bytes]:
            yield self._chunks[0]
            raise OSError("connection reset")

    f, canned = _sync_fetcher_httpx(
        monkeypatch, _Broken(200, chunks=[b"AB"]), _Broken(200, chunks=[b"XY"]), retries=1
    )
    with pytest.raises(OSError, match="connection reset"):
        f.download(_TARGET, dest)
    # 第二次重试走 200 截断重写后同样断流：耗尽抛原异常，半成品保留
    assert (tmp_path / "x.bin.part").read_bytes() == b"XY"
    assert len(canned.requests) == 2
    assert not dest.exists()


def test_download_sync_proxy_pool_rotates_on_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """代理池轮换：传输失败标记当前代理失败并换下一个代理重试成功。"""
    _no_sleep(monkeypatch)
    dest = tmp_path / "x.bin"
    pool = ProxyPool(["http://p1:9", "http://p2:9"])
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)
    canned = _Canned(OSError("proxy p1 unreachable"), _FakeStreamResponse(200, b"via-p2"))
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: _fake_httpx(canned))
    with pytest.warns(RuntimeWarning):
        f = Fetcher(allow_private_hosts=True, retries=1, proxy=pool)
    f.download(_TARGET, dest)
    assert dest.read_bytes() == b"via-p2"
    assert len(canned.requests) == 2
    # 死代理被标记失败（max_failures 未达阈值不冷却，仅计数）
    assert pool._failures["http://p1:9"] == 1


def test_download_sync_redirect_to_unsafe_target_closes_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-1 回归：重定向目标被 SSRF 拒绝时，当前跳的流必须关闭（句柄不泄漏）。

    初始目标用公网 IP 字面量（SSRF 校验纯本地、无 DNS），302 跳向云元数据
    地址在 ``_advance_redirect`` 内抛 UnsafeTargetError——修复前借出的流
    不会关闭（池句柄泄漏），修复后 close 归还。
    """
    dest = tmp_path / "x.bin"
    redirect = _FakeStreamResponse(302, headers={"Location": _UNSAFE_LOCATION})
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)
    canned = _Canned(redirect)
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: _fake_httpx(canned))
    with pytest.warns(RuntimeWarning):
        f = Fetcher(allow_private_hosts=False)  # 生产默认：拒绝私网/环回/链路本地
    with pytest.raises(UnsafeTargetError):
        f.download(_PUBLIC_TARGET, dest)
    assert redirect.close_count == 1
    assert len(canned.requests) == 1
    assert not dest.exists()


def test_download_sync_ssrf_private_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SSRF 生效验证：下载到云元数据/私网目标在请求发出前被拒绝。"""
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)
    canned = _Canned()  # 永远不该被消费
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: _fake_httpx(canned))
    with pytest.warns(RuntimeWarning):
        f = Fetcher(allow_private_hosts=False)  # 生产默认：拒绝私网/环回/链路本地 host
    with pytest.raises(UnsafeTargetError):
        f.download("http://169.254.169.254/latest/meta-data/", tmp_path / "secret")
    with pytest.raises(UnsafeTargetError):
        f.download("http://127.0.0.1:9/x", tmp_path / "secret")
    assert not canned.requests


# ── 同步：curl_cffi 主路径 ───────────────────────────────────────────────


def test_download_sync_curl_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """curl_cffi 主路径：stream=True 打开、iter_content 分块落盘。"""
    dest = tmp_path / "x.bin"
    f, canned = _sync_fetcher_curl(
        monkeypatch, _FakeStreamResponse(200, b"curl-body", {"content-type": "text/plain"})
    )
    f.download(_TARGET, dest)
    assert dest.read_bytes() == b"curl-body"
    kwargs = canned.requests[0]
    assert kwargs["stream"] is True
    assert kwargs["allow_redirects"] is False


def test_download_sync_curl_backend_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """curl_cffi 主路径断点续传：.part + 206（对齐）→ Range + 追加。"""
    dest = tmp_path / "x.bin"
    (tmp_path / "x.bin.part").write_bytes(b"pre")
    f, canned = _sync_fetcher_curl(
        monkeypatch, _FakeStreamResponse(206, b"fix", {"Content-Range": "bytes 3-"})
    )
    f.download(_TARGET, dest)
    assert dest.read_bytes() == b"prefix"
    assert canned.requests[0]["headers"]["Range"] == "bytes=3-"


# ── 异步 ─────────────────────────────────────────────────────────────────


async def test_async_download_httpx_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """异步基础下载：AsyncFetcher.download 落盘并返回绝对 Path。"""
    body = b"async-body-0123456789"
    f, canned = _async_fetcher_httpx(
        monkeypatch, _FakeStreamResponse(200, body, {"content-type": "application/pdf"})
    )
    dest = tmp_path / "y.bin"
    result = await f.download(_TARGET, dest, chunk_size=4)
    assert result.is_absolute()
    assert result == dest
    assert dest.read_bytes() == body
    assert canned.requests[0]["stream"] is True


async def test_async_download_httpx_resume_206(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步断点续传：预置 .part + 206（对齐）→ Range 头 + 追加写入。"""
    dest = tmp_path / "y.bin"
    (tmp_path / "y.bin.part").write_bytes(b"half")
    f, canned = _async_fetcher_httpx(
        monkeypatch, _FakeStreamResponse(206, b"-full", {"Content-Range": "bytes 4-"})
    )
    await f.download(_TARGET, dest)
    assert dest.read_bytes() == b"half-full"
    assert canned.requests[0]["headers"]["Range"] == "bytes=4-"


async def test_async_download_resume_206_misaligned_start_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-3 异步镜像：206 起始错位 → 截断重写，不拼接。"""
    body = b"async-0123456789"
    dest = tmp_path / "y.bin"
    (tmp_path / "y.bin.part").write_bytes(b"AB")
    f, _ = _async_fetcher_httpx(
        monkeypatch,
        _FakeStreamResponse(206, body, {"Content-Range": f"bytes 0-{len(body) - 1}/{len(body)}"}),
    )
    await f.download(_TARGET, dest)
    assert dest.read_bytes() == body


async def test_async_download_416_part_already_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-1 异步镜像：416 + bytes */N 且 N == .part 大小 → 直接落盘。"""
    body = b"async-complete-body"
    dest = tmp_path / "y.bin"
    part = tmp_path / "y.bin.part"
    part.write_bytes(body)
    f, _ = _async_fetcher_httpx(
        monkeypatch, _FakeStreamResponse(416, headers={"Content-Range": f"bytes */{len(body)}"})
    )
    result = await f.download(_TARGET, dest)
    assert result == dest
    assert dest.read_bytes() == body
    assert not part.exists()


async def test_async_download_416_declared_mismatch_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-1 异步镜像：416 声明长度与 .part 不符 → FetchError(status=416)。"""
    dest = tmp_path / "y.bin"
    (tmp_path / "y.bin.part").write_bytes(b"AB")
    f, _ = _async_fetcher_httpx(
        monkeypatch, _FakeStreamResponse(416, headers={"Content-Range": "bytes */21"})
    )
    with pytest.raises(FetchError, match="HTTP Error 416") as exc_info:
        await f.download(_TARGET, dest)
    assert getattr(exc_info.value, "status", None) == 416


async def test_async_download_user_range_header_stripped_without_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-2 异步镜像：无 .part 时剔除用户 Range 头，服务器回 200 全量。"""
    dest = tmp_path / "y.bin"
    body = b"async-full-body"
    f, canned = _async_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, body))
    await f.download(_TARGET, dest, headers={"Range": "bytes=100-"})
    assert dest.read_bytes() == body
    assert not _has_range_header(canned.requests[0]["headers"])


async def test_async_download_local_write_failure_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-3 异步镜像：写盘 OSError 不触发网络重试（请求只发一次）且 .part 保留。"""
    dest = tmp_path / "y.bin"
    _patch_local_file_error(monkeypatch, on_write=True)
    f, canned = _async_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, b"z" * 16), retries=1)
    with pytest.raises(FetchError, match="local write failure"):
        await f.download(_TARGET, dest)
    assert len(canned.requests) == 1
    assert (tmp_path / "y.bin.part").exists()
    assert not dest.exists()


async def test_async_download_resume_false_starts_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resume=False 异步镜像：忽略预置 .part、无 Range、截断重写。"""
    dest = tmp_path / "y.bin"
    body = b"async-fresh"
    (tmp_path / "y.bin.part").write_bytes(b"STALE")
    f, canned = _async_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, body))
    await f.download(_TARGET, dest, resume=False)
    assert dest.read_bytes() == body
    assert "Range" not in canned.requests[0]["headers"]
    assert not (tmp_path / "y.bin.part").exists()


async def test_async_download_httpx_server_error_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步 4xx 状态：FetchError（带 status 属性）且 .part 被清理。"""
    dest = tmp_path / "y.bin"
    (tmp_path / "y.bin.part").write_bytes(b"stale")
    f, _ = _async_fetcher_httpx(monkeypatch, _FakeStreamResponse(404))
    with pytest.raises(FetchError, match="HTTP Error 404") as exc_info:
        await f.download(_TARGET, dest)
    assert getattr(exc_info.value, "status", None) == 404
    assert not (tmp_path / "y.bin.part").exists()
    assert not dest.exists()


async def test_async_download_stream_setup_error_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步流建立阶段传输错误（canned 放异常实例）：退避重试后成功。"""
    _no_async_sleep(monkeypatch)
    dest = tmp_path / "y.bin"
    f, canned = _async_fetcher_httpx(
        monkeypatch, OSError("conn refused"), _FakeStreamResponse(200, b"retry-ok"), retries=1
    )
    await f.download(_TARGET, dest)
    assert dest.read_bytes() == b"retry-ok"
    assert len(canned.requests) == 2


async def test_async_download_stream_setup_exhausted_raises_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步流建立错误重试耗尽：抛原始 OSError（非 FetchError）。"""
    _no_async_sleep(monkeypatch)
    dest = tmp_path / "y.bin"
    f, canned = _async_fetcher_httpx(
        monkeypatch, OSError("conn refused"), OSError("conn refused"), retries=1
    )
    with pytest.raises(OSError, match="conn refused"):
        await f.download(_TARGET, dest)
    assert len(canned.requests) == 2
    assert not dest.exists()


async def test_async_download_local_open_failure_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-3 异步镜像：``.part`` 打开失败不重试、抛 FetchError。"""
    dest = tmp_path / "y.bin"
    _patch_local_file_error(monkeypatch, on_open=True)
    f, canned = _async_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, b"z" * 16), retries=1)
    with pytest.raises(FetchError, match="local write failure"):
        await f.download(_TARGET, dest)
    assert len(canned.requests) == 1
    assert not dest.exists()


async def test_async_download_local_close_failure_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-3 异步镜像：close 刷盘失败不重试、.part 内容完整保留。"""
    dest = tmp_path / "y.bin"
    body = b"async-buffered"
    _patch_local_file_error(monkeypatch, on_close=True)
    f, canned = _async_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, body), retries=1)
    with pytest.raises(FetchError, match="local write failure"):
        await f.download(_TARGET, dest)
    assert len(canned.requests) == 1
    assert (tmp_path / "y.bin.part").read_bytes() == body
    assert not dest.exists()


async def test_async_download_httpx_max_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步 max_bytes 超限：中止、清理 .part、抛 FetchError。"""
    dest = tmp_path / "y.bin"
    f, _ = _async_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, b"n" * 32))
    with pytest.raises(FetchError, match="max_bytes"):
        await f.download(_TARGET, dest, max_bytes=8)
    assert not (tmp_path / "y.bin.part").exists()
    assert not dest.exists()


async def test_async_download_abort_keeps_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步 should_abort：中止并保留 .part 半成品。"""
    dest = tmp_path / "y.bin"
    part = tmp_path / "y.bin.part"
    calls = {"n": 0}

    def _abort() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    f, _ = _async_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, chunks=[b"AB", b"CD"]))
    with pytest.raises(FetchError, match="aborted"):
        await f.download(_TARGET, dest, should_abort=_abort)
    assert part.read_bytes() == b"AB"
    assert not dest.exists()


async def test_async_download_on_headers_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步 on_headers：收到最终响应的 (status, headers)。"""
    seen: list[tuple[int, dict[str, str]]] = []
    f, _ = _async_fetcher_httpx(
        monkeypatch, _FakeStreamResponse(200, b"ok", {"content-type": "image/jpeg"})
    )
    await f.download(_TARGET, tmp_path / "y.jpg", on_headers=lambda s, h: seen.append((s, dict(h))))
    assert seen == [(200, {"content-type": "image/jpeg"})]


async def test_async_download_creates_missing_parent_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步：dest 的父目录不存在时自动创建。"""
    f, _ = _async_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, b"data"))
    dest = tmp_path / "a" / "b" / "y.bin"
    await f.download(_TARGET, dest)
    assert dest.read_bytes() == b"data"


async def test_async_download_429_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步 429（Retry-After: 0）重试后成功。"""
    _no_async_sleep(monkeypatch)
    dest = tmp_path / "y.bin"
    f, canned = _async_fetcher_httpx(
        monkeypatch,
        _FakeStreamResponse(429, headers={"Retry-After": "0"}),
        _FakeStreamResponse(200, b"payload"),
        retries=1,
    )
    await f.download(_TARGET, dest)
    assert dest.read_bytes() == b"payload"
    assert len(canned.requests) == 2


async def test_async_download_429_exhausted_raises_status_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步 429 重试耗尽：FetchError（带 status）并清理 .part。"""
    _no_async_sleep(monkeypatch)
    dest = tmp_path / "y.bin"
    f, canned = _async_fetcher_httpx(
        monkeypatch,
        _FakeStreamResponse(429, headers={"Retry-After": "0"}),
        _FakeStreamResponse(429, headers={"Retry-After": "0"}),
        retries=1,
    )
    with pytest.raises(FetchError, match="HTTP Error 429") as exc_info:
        await f.download(_TARGET, dest)
    assert getattr(exc_info.value, "status", None) == 429
    assert len(canned.requests) == 2
    assert not (tmp_path / "y.bin.part").exists()


async def test_async_download_network_error_retries_then_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步传输错误：保留 .part，重试自动转 Range 续传完成下载。"""
    _no_async_sleep(monkeypatch)
    dest = tmp_path / "y.bin"

    class _Broken(_FakeStreamResponse):
        async def aiter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
            yield b"AB"
            raise OSError("connection reset")

    f, canned = _async_fetcher_httpx(
        monkeypatch,
        _Broken(200),
        _FakeStreamResponse(206, b"CD", {"Content-Range": "bytes 2-3/4"}),
        retries=1,
    )
    await f.download(_TARGET, dest)
    assert dest.read_bytes() == b"ABCD"
    assert canned.requests[1]["headers"]["Range"] == "bytes=2-"


async def test_async_download_transport_exhausted_raises_original_keeps_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步传输错误重试耗尽：抛原始 OSError 且 .part 保留。"""
    _no_async_sleep(monkeypatch)
    dest = tmp_path / "y.bin"

    class _Broken(_FakeStreamResponse):
        async def aiter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
            yield self._chunks[0]
            raise OSError("connection reset")

    f, canned = _async_fetcher_httpx(
        monkeypatch, _Broken(200, chunks=[b"AB"]), _Broken(200, chunks=[b"XY"]), retries=1
    )
    with pytest.raises(OSError, match="connection reset"):
        await f.download(_TARGET, dest)
    assert (tmp_path / "y.bin.part").read_bytes() == b"XY"
    assert len(canned.requests) == 2
    assert not dest.exists()


async def test_async_download_redirect_to_unsafe_target_closes_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-1 异步镜像：重定向 SSRF 拒绝时流必须 aclose（句柄不泄漏）。"""
    dest = tmp_path / "y.bin"
    redirect = _FakeStreamResponse(302, headers={"Location": _UNSAFE_LOCATION})
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)
    canned = _Canned(redirect)
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: _fake_httpx(canned))
    with pytest.warns(RuntimeWarning):
        f = AsyncFetcher(allow_private_hosts=False)
    with pytest.raises(UnsafeTargetError):
        await f.download(_PUBLIC_TARGET, dest)
    assert redirect.close_count == 1
    assert len(canned.requests) == 1


async def test_async_download_ssrf_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步 SSRF 生效验证：私网目标在请求发出前被拒（UnsafeTargetError）。"""
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)
    canned = _Canned()
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: _fake_httpx(canned))
    with pytest.warns(RuntimeWarning):
        f = AsyncFetcher(allow_private_hosts=False)
    with pytest.raises(UnsafeTargetError):
        await f.download("http://192.168.1.1/admin", tmp_path / "secret")
    assert not canned.requests


async def test_async_download_curl_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """curl_cffi 异步主路径：AsyncSession stream=True + aiter_content 落盘。"""
    dest = tmp_path / "y.bin"
    (tmp_path / "y.bin.part").write_bytes(b"a")
    f, canned = _async_fetcher_curl(
        monkeypatch, _FakeStreamResponse(206, b"b", {"Content-Range": "bytes 1-"})
    )
    await f.download(_TARGET, dest)
    assert dest.read_bytes() == b"ab"
    kwargs = canned.requests[0]
    assert kwargs["stream"] is True
    assert kwargs["headers"]["Range"] == "bytes=1-"


async def test_async_download_fetcher_async_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同步 Fetcher 的 async_download 与 AsyncFetcher.download 语义一致。"""
    f, _ = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(200, b"via-fetcher"))
    dest = tmp_path / "z.bin"
    result = await f.async_download(_TARGET, dest)
    assert result == dest
    assert dest.read_bytes() == b"via-fetcher"

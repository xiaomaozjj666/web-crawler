"""Tests for ``Fetcher.download`` / ``AsyncFetcher.download``（流式下载 + 断点续传）。

全程 mock 网络（不发真实请求）：httpx 路径以 fake ``Client`` 拦截
``request(stream=True)``；curl_cffi 路径 patch ``_load_curl_backend`` 注入
fake ``Session`` / ``AsyncSession``。功能测试统一 ``allow_private_hosts=True``
+ 环回 URL（跳过 DNS 解析），SSRF 生效性由独立测试验证。
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

# 功能测试统一使用的环回目标（allow_private_hosts=True 时跳过 DNS 复查）
_TARGET = "http://127.0.0.1:9/files/x.bin"


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
    monkeypatch: pytest.MonkeyPatch, *items: Any, retries: int = 0
) -> tuple[Fetcher, _Canned]:
    """构造走 httpx 兜底路径的同步 Fetcher（canned 响应按序回放）。"""
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)
    canned = _Canned(*items)
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: _fake_httpx(canned))
    with pytest.warns(RuntimeWarning):
        return Fetcher(allow_private_hosts=True, retries=retries), canned


def _async_fetcher_httpx(
    monkeypatch: pytest.MonkeyPatch, *items: Any, retries: int = 0
) -> tuple[AsyncFetcher, _Canned]:
    """构造走 httpx 兜底路径的 AsyncFetcher。"""
    monkeypatch.setattr(fetcher_mod, "HAS_CURL_CFFI", False)
    monkeypatch.setattr(fetcher_mod, "HAS_HTTPX", True)
    canned = _Canned(*items)
    monkeypatch.setattr(fetcher_mod, "_load_httpx_backend", lambda: _fake_httpx(canned))
    with pytest.warns(RuntimeWarning):
        return AsyncFetcher(allow_private_hosts=True, retries=retries), canned


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
    """断点续传：预置 .part + 206 响应 → 发 Range 头并追加写入。"""
    dest = tmp_path / "x.bin"
    part = tmp_path / "x.bin.part"
    part.write_bytes(b"AB")
    f, canned = _sync_fetcher_httpx(monkeypatch, _FakeStreamResponse(206, b"CD"))
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
        _FakeStreamResponse(206, b"CD"),
        retries=1,
    )
    f.download(_TARGET, dest)
    assert dest.read_bytes() == b"ABCD"
    # 第二次请求基于 .part 实际大小续传
    assert canned.requests[1]["headers"]["Range"] == "bytes=2-"


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
    """curl_cffi 主路径断点续传：.part + 206 → Range + 追加。"""
    dest = tmp_path / "x.bin"
    (tmp_path / "x.bin.part").write_bytes(b"pre")
    f, canned = _sync_fetcher_curl(monkeypatch, _FakeStreamResponse(206, b"fix"))
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
    """异步断点续传：预置 .part + 206 → Range 头 + 追加写入。"""
    dest = tmp_path / "y.bin"
    (tmp_path / "y.bin.part").write_bytes(b"half")
    f, canned = _async_fetcher_httpx(monkeypatch, _FakeStreamResponse(206, b"-full"))
    await f.download(_TARGET, dest)
    assert dest.read_bytes() == b"half-full"
    assert canned.requests[0]["headers"]["Range"] == "bytes=4-"


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
    f, canned = _async_fetcher_curl(monkeypatch, _FakeStreamResponse(206, b"b"))
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

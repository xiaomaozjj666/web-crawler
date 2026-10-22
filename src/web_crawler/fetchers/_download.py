"""流式下载核心：断点续传、.part 临时文件与流式响应的统一包装。

:class:`~web_crawler.fetchers.fetcher.Fetcher.download` /
``async_download`` 的实现支撑，把「发请求 → 手动跟随重定向 → 分块写盘 →
原子落盘」的下载管线从应用层（CLI 爬虫）下沉到库层。下载路径与普通
请求共用同一套校验链（SSRF host 校验、代理、会话池、DNS 钉扎），由
``fetcher._stream_with_redirects_sync/_async``（见 fetcher.py）承载。

断点续传语义（对齐应用层已验证的实现）：

* 目标文件 ``dest`` 的半成品写在 ``dest.part``；``resume=True`` 且
  ``.part`` 存在时发送 ``Range: bytes=<size>-``；
* 响应 ``206 Partial Content`` → 追加模式续写（从 offset 起累计）；
* 响应 ``200``（服务器不支持 Range）→ 截断重写；
* 其余状态码 → :class:`~web_crawler.exceptions.FetchError` 并清理
  ``.part``（资源不可得，半成品无意义）；
* 传输层错误中断 → 保留 ``.part``（下次调用可继续续传）；
* 用户经 ``should_abort`` 回调主动中止 → 保留 ``.part``；
* ``max_bytes`` 超限 → 清理 ``.part``（防磁盘炸弹）；
* 全部字节写入完成后 ``os.replace`` 原子替换到 ``dest``（Windows 兼容）。
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..exceptions import ConfigError, FetchError
from .proxy import ProxyPool

if TYPE_CHECKING:
    from .fetcher import _FetcherCore
    from .session import SessionHandle

__all__ = ["StreamContext", "download_async", "download_sync", "part_path_for"]

# 半成品临时文件后缀（与应用层历史实现一致，不带 pid：跨进程/跨次运行续传
# 正是断点续传的核心场景，带 pid 会让其他进程的半成品无法复用）。
PART_SUFFIX = ".part"


def part_path_for(dest: Path) -> Path:
    """返回 *dest* 对应的 ``.part`` 临时文件路径。"""
    return dest.with_name(dest.name + PART_SUFFIX)


def resume_offset_for(part: Path, resume: bool) -> int:
    """断点续传的起始偏移：``resume`` 开启且 ``.part`` 存在时取其大小。"""
    if not resume:
        return 0
    try:
        return part.stat().st_size
    except OSError:
        return 0


def validate_download_args(chunk_size: int, max_bytes: int | None) -> None:
    """校验 download 的数值参数（非法立即抛 :class:`ConfigError`）。"""
    if chunk_size <= 0:
        raise ConfigError("chunk_size must be positive")
    if max_bytes is not None and max_bytes < 0:
        raise ConfigError("max_bytes must be >= 0")


def download_error(
    message: str, *, status: int | None = None, keep_part: bool = False
) -> FetchError:
    """构造带结构化元数据的下载 :class:`FetchError`。

    ``status`` 为服务器返回的 HTTP 状态码（无网络层状态时 ``None``），
    应用层可用 ``getattr(exc, "status", None)`` 读取做限速/分类决策；
    ``keep_part=True`` 表示中止时保留 ``.part`` 半成品（用户主动中止，
    续传价值仍在）。
    """
    exc = FetchError(message)
    exc.status = status  # type: ignore[attr-defined]
    exc.keep_part = keep_part  # type: ignore[attr-defined]
    return exc


def _discard_part(part: Path) -> None:
    """best-effort 删除 ``.part`` 半成品（失败清理，删不掉不外抛）。"""
    try:
        part.unlink(missing_ok=True)
    except OSError:
        pass


class StreamContext:
    """流式响应的统一包装：分块迭代 + 关闭（含池句柄归还）。

    curl_cffi 与 httpx 的流式响应生命周期不同（curl 克隆独立 curl 句柄、
    httpx 绑定客户端连接池），且会话池句柄必须在流关闭后才可归还——本类
    把「迭代 + 关闭 + 归还」收口为单一职责对象，同步/异步 API 成对提供。
    ``close`` 幂等，任何路径重复调用都安全。
    """

    def __init__(
        self,
        raw: Any,
        handle: SessionHandle | None,
        *,
        fetcher: _FetcherCore,
        use_curl: bool,
    ) -> None:
        self._raw = raw
        self._handle = handle
        self._fetcher = fetcher
        self._use_curl = use_curl
        self._completed = False
        self._closed = False

    @property
    def raw(self) -> Any:
        """原始后端响应（重定向状态机读取状态码/响应头用，不消费 body）。"""
        return self._raw

    @property
    def status_code(self) -> int:
        return int(self._raw.status_code)

    @property
    def headers(self) -> Any:
        return self._raw.headers

    def iter_chunks(self, chunk_size: int) -> Iterator[bytes]:
        """逐块产出响应体（生成器；正常耗尽后标记 ``_completed``）。"""
        if self._use_curl:
            # curl_cffi >= 0.16 对 iter_content(chunk_size) 发
            # CurlCffiWarning("chunk_size is ignored")——严格警告模式
            # （filterwarnings=error）下会炸，且 libcurl 本就没有按块读取
            # 的概念；不传参，块粒度由接收缓冲队列决定。
            for chunk in self._raw.iter_content():
                yield chunk
        else:
            for chunk in self._raw.iter_bytes(chunk_size):
                yield chunk
        self._completed = True

    async def aiter_chunks(self, chunk_size: int) -> AsyncIterator[bytes]:
        """异步逐块产出响应体（语义同 :meth:`iter_chunks`）。"""
        if self._use_curl:
            # 同步版注释的原因，aiter_content 同样不传 chunk_size
            async for chunk in self._raw.aiter_content():
                yield chunk
        else:
            async for chunk in self._raw.aiter_bytes(chunk_size):
                yield chunk
        self._completed = True

    def close(self) -> None:
        """关闭流并归还池化会话（幂等；归还健康度按是否读完计）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._raw.close()
        except Exception:
            pass  # best-effort：流关闭失败不掩盖业务异常
        if self._handle is not None:
            self._fetcher._release_pooled(self._handle, success=self._completed)

    async def aclose(self) -> None:
        """异步关闭流并归还池化会话（幂等；语义同 :meth:`close`）。"""
        if self._closed:
            return
        self._closed = True
        try:
            await self._raw.aclose()
        except Exception:
            pass
        if self._handle is not None:
            await self._fetcher._release_pooled_async(self._handle, success=self._completed)


def _proxy_rotated(fetcher: _FetcherCore, proxy: str | None) -> str | None:
    """代理池失败轮换：标记当前代理失败并解析下一个（非池配置原样返回）。"""
    if isinstance(fetcher.proxy, ProxyPool) and proxy:
        fetcher.proxy.mark_failed(proxy)
        return fetcher._resolve_proxy()
    return proxy


def download_sync(
    fetcher: _FetcherCore,
    url: str,
    dest: str | Path,
    *,
    chunk_size: int,
    resume: bool,
    max_bytes: int | None,
    headers: dict[str, str] | None,
    should_abort: Callable[[], bool] | None,
    on_headers: Callable[[int, Mapping[str, str]], None] | None,
) -> Path:
    """同步流式下载核心（``Fetcher.download`` 的实现体）。

    复用 fetcher 的全套请求管线（SSRF 校验、代理、会话池、DNS 钉扎，
    由 ``_stream_with_redirects_sync`` 承载），重试语义对齐
    ``_send_sync``：网络层错误与 429/5xx 按指数退避重试（代理池自动
    轮换），4xx 状态直接失败。写盘中途的传输错误会保留 ``.part``，
    下一次重试自动以 ``Range`` 续传。
    """
    from .fetcher import _parse_retry_after

    validate_download_args(chunk_size, max_bytes)
    fetcher._validate_target(url)
    dest_path = Path(dest).expanduser()
    if not dest_path.parent.exists():
        dest_path.parent.mkdir(parents=True, exist_ok=True)
    merged_headers = fetcher._merge_headers(headers)
    proxy = fetcher._resolve_proxy()
    retry_errors = fetcher._retry_errors()
    part = part_path_for(dest_path)
    last_exc: BaseException | None = None
    for attempt in range(fetcher.retries + 1):
        backoff = min(2.0**attempt, 10.0) + random.random() * 0.25
        # 每次尝试基于 .part 当前实际大小重算偏移：上一次写盘中途断流的
        # 半成品在下一次重试里自动转为 Range 续传
        offset = resume_offset_for(part, resume)
        send_headers = dict(merged_headers)
        if offset > 0:
            send_headers["Range"] = f"bytes={offset}-"
        try:
            stream = fetcher._stream_with_redirects_sync(
                "GET", url, send_headers, proxy, fetcher.timeout, fetcher.verify
            )
        except retry_errors as exc:
            last_exc = exc
            if attempt == fetcher.retries:
                raise
            proxy = _proxy_rotated(fetcher, proxy)
            time.sleep(backoff)
            continue
        try:
            status = stream.status_code
            if status == 429 or status >= 500:
                retry_after = _parse_retry_after(stream.headers.get("Retry-After"))
                if attempt == fetcher.retries:
                    raise download_error(
                        f"HTTP Error {status}: download failed for {url}", status=status
                    )
                proxy = _proxy_rotated(fetcher, proxy)
                time.sleep(retry_after or backoff)
                continue
            if status not in (200, 206):
                raise download_error(
                    f"HTTP Error {status}: download failed for {url}", status=status
                )
            if on_headers is not None:
                on_headers(status, stream.headers)
            # 206 且已有半成品 → 追加续写；200（或未发 Range 却收到 206）
            # → 截断重写，对齐应用层历史语义
            append = status == 206 and offset > 0
            total = offset if append else 0
            file_handle = part.open("ab" if append else "wb")
            try:
                for chunk in stream.iter_chunks(chunk_size):
                    if should_abort is not None and should_abort():
                        raise download_error(
                            "download aborted by should_abort callback", keep_part=True
                        )
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise download_error(f"file exceeds max_bytes ({max_bytes})")
                    file_handle.write(chunk)
            finally:
                file_handle.close()
            os.replace(part, dest_path)
            if isinstance(fetcher.proxy, ProxyPool) and proxy:
                fetcher.proxy.mark_success(proxy)
            return dest_path.absolute()
        except FetchError as exc:
            # 状态错误 / 超限 → 清理 .part；用户主动中止（keep_part）→ 保留
            if not getattr(exc, "keep_part", False):
                _discard_part(part)
            raise
        except retry_errors as exc:
            # 传输错误：保留 .part（下次重试/调用可续传），按退避重试
            last_exc = exc
            if attempt == fetcher.retries:
                raise
            proxy = _proxy_rotated(fetcher, proxy)
            time.sleep(backoff)
            continue
        finally:
            stream.close()
    if last_exc is not None:  # pragma: no cover - 重试循环在最后一次必定 return 或 raise
        raise last_exc
    raise download_error(
        f"download of {url} failed without a captured exception"
    )  # pragma: no cover


async def download_async(
    fetcher: _FetcherCore,
    url: str,
    dest: str | Path,
    *,
    chunk_size: int,
    resume: bool,
    max_bytes: int | None,
    headers: dict[str, str] | None,
    should_abort: Callable[[], bool] | None,
    on_headers: Callable[[int, Mapping[str, str]], None] | None,
) -> Path:
    """异步流式下载核心（``AsyncFetcher.download`` / ``Fetcher.async_download`` 的实现体）。

    语义与 :func:`download_sync` 完全一致（同一套校验链、重试/退避、
    ``.part`` 生命周期管理），仅把传输原语换成异步版。
    """
    from .fetcher import _parse_retry_after

    validate_download_args(chunk_size, max_bytes)
    fetcher._validate_target(url)
    dest_path = Path(dest).expanduser()
    if not dest_path.parent.exists():
        dest_path.parent.mkdir(parents=True, exist_ok=True)
    merged_headers = fetcher._merge_headers(headers)
    proxy = fetcher._resolve_proxy()
    retry_errors = fetcher._retry_errors()
    part = part_path_for(dest_path)
    last_exc: BaseException | None = None
    for attempt in range(fetcher.retries + 1):
        backoff = min(2.0**attempt, 10.0) + random.random() * 0.25
        offset = resume_offset_for(part, resume)
        send_headers = dict(merged_headers)
        if offset > 0:
            send_headers["Range"] = f"bytes={offset}-"
        try:
            stream = await fetcher._stream_with_redirects_async(
                "GET", url, send_headers, proxy, fetcher.timeout, fetcher.verify
            )
        except retry_errors as exc:
            last_exc = exc
            if attempt == fetcher.retries:
                raise
            proxy = _proxy_rotated(fetcher, proxy)
            await asyncio.sleep(backoff)
            continue
        try:
            status = stream.status_code
            if status == 429 or status >= 500:
                retry_after = _parse_retry_after(stream.headers.get("Retry-After"))
                if attempt == fetcher.retries:
                    raise download_error(
                        f"HTTP Error {status}: download failed for {url}", status=status
                    )
                proxy = _proxy_rotated(fetcher, proxy)
                await asyncio.sleep(retry_after or backoff)
                continue
            if status not in (200, 206):
                raise download_error(
                    f"HTTP Error {status}: download failed for {url}", status=status
                )
            if on_headers is not None:
                on_headers(status, stream.headers)
            append = status == 206 and offset > 0
            total = offset if append else 0
            file_handle = part.open("ab" if append else "wb")
            try:
                async for chunk in stream.aiter_chunks(chunk_size):
                    if should_abort is not None and should_abort():
                        raise download_error(
                            "download aborted by should_abort callback", keep_part=True
                        )
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise download_error(f"file exceeds max_bytes ({max_bytes})")
                    file_handle.write(chunk)
            finally:
                file_handle.close()
            os.replace(part, dest_path)
            if isinstance(fetcher.proxy, ProxyPool) and proxy:
                fetcher.proxy.mark_success(proxy)
            return dest_path.absolute()
        except FetchError as exc:
            if not getattr(exc, "keep_part", False):
                _discard_part(part)
            raise
        except retry_errors as exc:
            last_exc = exc
            if attempt == fetcher.retries:
                raise
            proxy = _proxy_rotated(fetcher, proxy)
            await asyncio.sleep(backoff)
            continue
        finally:
            await stream.aclose()
    if last_exc is not None:  # pragma: no cover - 重试循环在最后一次必定 return 或 raise
        raise last_exc
    raise download_error(
        f"download of {url} failed without a captured exception"
    )  # pragma: no cover

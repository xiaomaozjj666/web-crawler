"""crawler 基础层:日志挂钩、AES/分段密钥、opener、stealth 下载与 fetch。

从 ``crawler.py`` 下沉的底层能力;阶段子模块(:mod:`_crawler_discovery` /
:mod:`_crawler_download` / :mod:`_crawler_scan` / :mod:`_crawler_post`)与编排层
(:mod:`crawler`)统一从本模块按属性访问(``cr.fetch(...)``),保证对
``_crawler_core`` 全局名(fetch / should_stop / HAS_AES / _get_opener 等)的
patch 对全部阶段生效。
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import (
    HTTPHandler,
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

from web_crawler._ssrf import is_power_mode, validate_url_host
from web_crawler.app._crawler_context import DEFAULT_USER_AGENT
from web_crawler.app.crawler_net import (
    DomainRateLimiter,
    SafeRedirectHandler,
    _is_safe_hostname,
)

_log = logging.getLogger("crawler")


def attach_log_handler(handler: logging.Handler) -> None:
    """把外部日志 handler 挂到 crawler 的 logger（UI 任务日志用），重复挂载自动去重。"""
    if handler not in _log.handlers:
        _log.addHandler(handler)


def detach_log_handler(handler: logging.Handler) -> None:
    """卸载外部日志 handler（任务结束/异常时调用）。"""
    if handler in _log.handlers:
        _log.removeHandler(handler)


# ── 控制台辅助 ──────────────────────────────────────────────────────────

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── AES 解密支持 ──────────────────────────────────────────────────────

try:
    from Crypto.Cipher import AES as _AES

    HAS_AES = True  # pragma: no cover - pycryptodome 未安装时不可达
except ImportError:
    HAS_AES = False


def decrypt_aes128(data: bytes, key: bytes, iv: bytes) -> bytes:
    """解密 AES-128-CBC 数据并做 PKCS7 去填充。"""
    cipher = _AES.new(key, _AES.MODE_CBC, iv=iv)
    decrypted = cipher.decrypt(data)
    # PKCS7 去填充
    pad_len = decrypted[-1]
    if 1 <= pad_len <= 16:
        return decrypted[:-pad_len]
    return decrypted


_segment_keys: dict[str, tuple[bytes, bytes]] = {}  # url -> (key_bytes, iv_bytes)
_segment_keys_lock = threading.Lock()


def register_segment_key(url: str, key: bytes, iv: bytes) -> None:
    with _segment_keys_lock:
        _segment_keys[url] = (key, iv)


def get_segment_key(url: str) -> tuple[bytes, bytes] | None:
    with _segment_keys_lock:
        return _segment_keys.get(url)


# ── HTTP opener（连接复用）──────────────────────────────────────────

_opener: OpenerDirector | None = None
_opener_lock = threading.Lock()

# 重定向最大跳数（与 urllib 默认一致），防止无限跳转
_MAX_REDIRECTS = 10


def _get_opener(proxy: str | None = None) -> OpenerDirector:
    global _opener
    if _opener is None or proxy:
        handlers: list[Any] = [SafeRedirectHandler(), HTTPSHandler(), HTTPHandler()]
        if proxy:
            handlers.append(ProxyHandler({"http": proxy, "https": proxy}))
        opener = build_opener(*handlers)
        if not proxy:
            _opener = opener
        return opener
    return _opener


# ── 隐身 fetcher 桥接（复用 src/web_crawler 库）─────────────────────────
#
# 设置 ``--stealth`` 后，不需要流式断点续传的页面/资源抓取会改走
# ``web_crawler.fetchers.Fetcher``（curl_cffi TLS 指纹伪装），让请求在
# 网络层与真实浏览器无法区分，破解拦截普通 urllib 的 JA3/JA4 指纹检测。
# 大文件/可续传下载仍走原有流式路径。


def _import_stealth_fetcher() -> Any:
    """延迟导入库级 ``Fetcher``；不可用时返回 None。"""
    try:
        from web_crawler import Fetcher  # type: ignore[import-not-found]
    except ImportError:
        # app 运行时 src/ 可能不在 PYTHONPATH 上 —— 尝试项目目录布局。
        import sys

        _src = Path(__file__).resolve().parent.parent / "src"
        if str(_src) not in sys.path:
            sys.path.insert(0, str(_src))
        try:
            from web_crawler import Fetcher  # type: ignore[import-not-found]
        except ImportError:
            _log.warning(
                "stealth mode requested but web_crawler.Fetcher is not importable; "
                "falling back to urllib. Install the library or run with PYTHONPATH=src."
            )
            return None
    return Fetcher


# 模块级隐身 fetcher 缓存：同一任务内的多次请求复用 curl_cffi 的 TLS 会话，
# 而不是每次调用重建。
_stealth_fetcher: Any = None
_stealth_fetcher_key: tuple[str, str | None, str] = ("", None, "")


def _get_stealth_fetcher(
    impersonate: str,
    timeout: float,
    proxy: str | None,
) -> Any:
    """返回与 *impersonate*/*timeout*/*proxy* 匹配的缓存 :class:`Fetcher`。

    返回对象支持 ``get(url, headers=…)``，可在会话伪装默认值之上叠加
    每次请求单独传入的 headers。
    """
    global _stealth_fetcher, _stealth_fetcher_key
    key = (impersonate, proxy, str(timeout))
    if _stealth_fetcher is not None and _stealth_fetcher_key == key:
        return _stealth_fetcher
    if _stealth_fetcher is not None:
        try:
            _stealth_fetcher.close()
        except Exception:
            pass
    Fetcher_cls = _import_stealth_fetcher()
    if Fetcher_cls is None:
        raise RuntimeError("stealth fetcher unavailable")
    _stealth_fetcher = Fetcher_cls(
        impersonate=impersonate,
        timeout=float(timeout),
        proxy=proxy,
        retries=0,
        follow_redirects=False,  # 重定向由 _stealth_fetch 手动跟随并逐跳校验（防 SSRF）
    )
    _stealth_fetcher_key = key
    return _stealth_fetcher


def _stealth_fetch(
    url: str,
    headers: dict[str, str],
    timeout: int,
    proxy: str | None,
    impersonate: str,
    max_bytes: int | None = None,
) -> tuple[bytes, str, int]:
    """通过 curl_cffi TLS 指纹伪装 GET ``url``。

    返回 ``(content, content_type, status)``；传输错误会抛异常，
    由调用方的重试循环统一处理。

    底层 ``Fetcher``（TLS 会话）缓存于模块级，重复隐身请求复用同一连接池。

    注：curl_cffi 会话默认整包缓冲,无法真正按块计数;这里用 Content-Length
    做预检（超过 max_bytes 直接拒绝,不等整包缓冲完成）,实际长度由调用方
    在返回后再校验一次兜底。
    """
    fetcher = _get_stealth_fetcher(impersonate, float(timeout), proxy)
    # 手动跟随重定向并逐跳校验目标（fetcher 以 follow_redirects=False 构造）
    current_url = url
    for _hop in range(_MAX_REDIRECTS + 1):
        resp = fetcher.get(current_url, headers=headers)
        status = int(resp.status)
        if status in (301, 302, 303, 307, 308):
            location = resp.headers.get("location") or resp.headers.get("Location")
            if not location:
                break
            next_url = urljoin(current_url, location.strip())
            parsed = urlparse(next_url)
            if parsed.scheme not in {"http", "https"} or not _is_safe_hostname(parsed.hostname):
                raise ValueError(f"blocked redirect to unsafe URL: {next_url}")
            current_url = next_url
            continue
        # Content-Length 预检：明显超限直接拒绝,避免整包缓冲后才失败
        if max_bytes:
            content_length = resp.headers.get("content-length") or resp.headers.get(
                "Content-Length"
            )
            if content_length:
                try:
                    declared = int(content_length)
                except ValueError:
                    declared = None  # 非数字 Content-Length 忽略,由调用方按实际长度兜底
                if declared is not None and declared > max_bytes:
                    raise ValueError(f"file exceeds --max-bytes ({max_bytes})")
        content_type = resp.headers.get("content-type", "") or resp.headers.get("Content-Type", "")
        return resp.content, content_type, status
    raise ValueError(f"too many redirects fetching {url}")


# ── Fetch（增强：429 处理、连接复用、流式下载）─────────────────────


def fetch(
    url: str,
    timeout: int,
    headers: dict[str, str],
    retries: int,
    max_bytes: int | None,
    resume_path: Path | None = None,
    rate_limiter: DomainRateLimiter | None = None,
    control_args: argparse.Namespace | None = None,
) -> tuple[bytes, str]:
    # SSRF 防护：请求前校验 host，拒绝私网/环回/链路本地目标（含云元数据地址）；
    # 重定向目标由 SafeRedirectHandler / _stealth_fetch 逐跳校验。
    # Power Mode（WEB_CRAWLER_POWER_MODE=1）放行 host 校验（仅保留 scheme 白名单）。
    # resolve=True：主机名先做 DNS 解析复查（防重绑定），结果带缓存防重复解析。
    if not is_power_mode():
        validate_url_host(url, resolve=True)
    last_error: Exception | None = None
    method_headers = dict(headers)

    for attempt in range(retries + 1):
        try:
            # 等待域级限速
            if rate_limiter:
                rate_limiter.wait_if_needed(url)

            # 隐身路径：不需要断点续传的抓取（页面 HTML 发现 + 小资源）走
            # curl_cffi TLS 指纹 fetcher。流式续传仍走下方 urllib 路径，
            # 因为隐身 fetcher 会把整个 body 缓冲在内存里。
            stealth = bool(control_args and getattr(control_args, "stealth", False))
            if stealth and resume_path is None:
                impersonate = (
                    getattr(control_args, "impersonate", "chrome131")
                    if control_args
                    else "chrome131"
                )
                proxy = getattr(control_args, "proxy", None) if control_args else None
                content, content_type, _status = _stealth_fetch(
                    url, method_headers, timeout, proxy, impersonate, max_bytes=max_bytes
                )
                if max_bytes and len(content) > max_bytes:
                    raise ValueError(f"file exceeds --max-bytes ({max_bytes})")
                if rate_limiter:
                    rate_limiter.record_request(url)
                return content, content_type

            part_path: Path | None = None
            existing_size = 0
            mode = "wb"
            if resume_path is not None:
                part_path = resume_path.with_name(resume_path.name + ".part")
                if part_path.exists():
                    existing_size = part_path.stat().st_size
                    if existing_size > 0:
                        method_headers["Range"] = f"bytes={existing_size}-"
                        mode = "ab"

            request_headers = {
                "User-Agent": method_headers.get("User-Agent", DEFAULT_USER_AGENT),
                "Accept": "*/*",
                **{k: v for k, v in method_headers.items() if k.lower() != "user-agent"},
            }
            if "referer" not in {k.lower() for k in request_headers}:
                request_headers["Referer"] = url

            request = Request(url, headers=request_headers)
            proxy = getattr(control_args, "proxy", None) if control_args else None
            opener = _get_opener(proxy)
            response = opener.open(request, timeout=timeout)

            content_type = response.headers.get("content-type", "")
            if existing_size and getattr(response, "status", None) != 206:
                existing_size = 0
                mode = "wb"
            total = existing_size
            file_handle = part_path.open(mode) if part_path else None
            chunks: list[bytes] | None = [] if not file_handle else None
            while True:
                wait_if_paused(control_args)
                if should_stop(control_args):
                    raise RuntimeError("cancelled by user")
                chunk = response.read(1024 * 64)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes and total > max_bytes:
                    raise ValueError(f"file exceeds --max-bytes ({max_bytes})")
                if file_handle:
                    file_handle.write(chunk)
                elif chunks is not None:
                    chunks.append(chunk)

            if file_handle:
                file_handle.close()
                if part_path is not None and resume_path is not None:
                    part_path.replace(resume_path)

            if rate_limiter:
                rate_limiter.record_request(url)
            if chunks is not None:
                return b"".join(chunks), content_type
            return b"", content_type

        except HTTPError as exc:
            status = exc.code
            if status == 429 and rate_limiter:
                retry_after = exc.headers.get("Retry-After")
                delay = rate_limiter.handle_429(url, retry_after)
                if delay and attempt < retries:
                    wait_if_paused(control_args)  # 退避期间暂停/取消同样生效
                    time.sleep(min(delay, 30))
                    continue
            last_error = exc
            if attempt < retries and status not in (401, 403, 404):
                wait_if_paused(control_args)
                time.sleep(1 + attempt * 2)

        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                wait_if_paused(control_args)
                time.sleep(1 + attempt * 2)

        finally:
            try:
                if "file_handle" in locals() and file_handle:
                    file_handle.close()
            except Exception as _close_err:
                _log.warning("file handle close error: %s", _close_err)

    assert last_error is not None
    raise last_error


def should_stop(args: argparse.Namespace | None) -> bool:
    callback = getattr(args, "should_stop", None) if args is not None else None
    return bool(callback and callback())


def wait_if_paused(args: argparse.Namespace | None) -> None:
    callback = getattr(args, "wait_if_paused", None) if args is not None else None
    if callback:
        callback()


def report_progress(args: argparse.Namespace | None, **payload: object) -> None:
    callback = getattr(args, "progress_callback", None) if args is not None else None
    if callback:
        callback(payload)


# ── 抓取主函数 ───────────────────────────────────────────────────────

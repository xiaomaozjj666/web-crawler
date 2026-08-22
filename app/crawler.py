#!/usr/bin/env python3
"""
Compliant web resource crawler

Downloads public or authorized resources referenced by web pages. It does not
try to bypass paywalls, login checks, DRM, signatures, or other access controls.

核心特性:
  - 并发下载（--workers 控制，默认 8 线程）
  - 自适应域级限速（自动处理 429 + Retry-After）
  - 内容 SHA256 去重
  - Sitemap.xml 页面发现
  - 配置保存/加载（--save-config / --load-config）
  - HTTP 连接复用（Keep-Alive）
  - 结构化日志 + JSONL 实时输出

模块结构（为消除巨型单文件拆分）:
  - app.crawler         本模块：CLI / crawl 主流程 / fetch 网络编排 / AES / 状态持久化
  - app.crawler_models  共享数据类 Resource / ManifestRow
  - app.crawler_net     网络/解析/工具层（限速、去重、URL 分类、HTML 解析等）
  - app.crawler_report  报告/格式层（清单、摘要、MD/HTML 报告、HTML 重写、智能抽取等）

Examples:
  python crawler.py --url https://example.com
  python crawler.py --url https://example.com --workers 16 --include-css-urls
  python crawler.py --url https://example.com --same-domain --crawl-pages --max-pages 20
  python crawler.py --url https://example.com --sitemap --workers 4
  python web_resource_crawler.py --load-config my_project.json
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import asdict
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
from urllib.robotparser import RobotFileParser

# 拆分后各层的同名再导出：保证 `app.crawler` 模块的所有属性仍可访问，
# 测试对 cr.xxx 的访问与 patch.object(cr, ...) 不受影响（函数体内对模块
# 全局名的解析在 patch 后拿到替换值）。
from app.crawler_models import ManifestRow, Resource
from app.crawler_net import *
from app.crawler_report import *

# ── Logging ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
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


# ── Console helpers ──────────────────────────────────────────────────────

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── Constants ────────────────────────────────────────────────────────────

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; ResourceCrawler/3.0)"
DEFAULT_WORKERS = 8
CRAWL_STATE_FILE = ".crawl_state.json"


# ── AES decryption support ──────────────────────────────────────────────

try:
    from Crypto.Cipher import AES as _AES

    HAS_AES = True  # pragma: no cover - pycryptodome 未安装时不可达
except ImportError:
    HAS_AES = False


def decrypt_aes128(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt AES-128-CBC data with PKCS7 unpadding."""
    cipher = _AES.new(key, _AES.MODE_CBC, iv=iv)
    decrypted = cipher.decrypt(data)
    # PKCS7 unpadding
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


# ── HTTP opener with connection reuse ────────────────────────────────────

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


# ── Stealth fetcher bridge (reuses the src/web_crawler library) ──────────
#
# When ``--stealth`` is set, page/resource fetches that do not need streaming
# resume are routed through ``web_crawler.fetchers.Fetcher`` (curl_cffi TLS
# fingerprint impersonation). This makes requests indistinguishable from a real
# browser at the network layer, defeating JA3/JA4 fingerprinting that blocks
# plain urllib. Large/resumable downloads keep the original streaming path.


def _import_stealth_fetcher() -> Any:
    """Lazily import the library ``Fetcher``; return None if unavailable."""
    try:
        from web_crawler import Fetcher  # type: ignore[import-not-found]
    except ImportError:
        # App may run with src/ not on PYTHONPATH — try the project layout.
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


# Module-level stealth fetcher cache so curl_cffi's TLS session is reused
# across requests within the same job rather than re-created each call.
_stealth_fetcher: Any = None
_stealth_fetcher_key: tuple[str, str | None, str] = ("", None, "")


def _get_stealth_fetcher(
    impersonate: str,
    timeout: float,
    proxy: str | None,
) -> Any:
    """Return a cached :class:`Fetcher` matching *impersonate*/*timeout*/*proxy*.

    The returned object supports ``get(url, headers=…)`` with per-request
    headers layered on top of the session's impersonation defaults.
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
    """GET ``url`` via curl_cffi TLS-fingerprint impersonation.

    Returns ``(content, content_type, status)``. Raises on transport errors so
    the caller's retry loop can handle them uniformly.

    The underlying ``Fetcher`` (TLS session) is cached at module level so
    repeated stealth requests reuse the same connection pool.

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


# ── Fetch (enhanced with 429 handling, connection reuse, streaming) ──────


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
            # Wait for rate limiter
            if rate_limiter:
                rate_limiter.wait_if_needed(url)

            # Stealth path: route through curl_cffi TLS-fingerprint fetcher for
            # non-resumable fetches (page HTML discovery + small resources).
            # Streaming resume still uses the urllib path below because the
            # stealth fetcher buffers the full body in memory.
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


# ── Sitemap / robots / playlist discovery（依赖 fetch，留在主模块以便 patch 生效）──


def make_robots_parser(root_url: str, headers: dict[str, str], timeout: int) -> RobotFileParser:
    parsed = urlparse(root_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    robot_parser = RobotFileParser()
    robot_parser.set_url(robots_url)
    try:
        data, _ = fetch(robots_url, timeout, headers, retries=0, max_bytes=1024 * 1024)
        robot_parser.parse(data.decode("utf-8", errors="replace").splitlines())
    except Exception:
        robot_parser.parse([])
    return robot_parser


def discover_playlist_resources(
    text: str,
    playlist_url: str,
    page_url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
    retries: int = 1,
    decrypt: bool = False,
) -> tuple[list[Resource], str]:
    """Expand m3u8/mpd playlists. Returns (resource_list, diagnostic_message)."""
    found: list[Resource] = []
    parsed = urlparse(playlist_url)
    is_m3u8 = parsed.path.lower().endswith(".m3u8") or "#EXTM3U" in text
    is_mpd = parsed.path.lower().endswith(".mpd") or "<MPD" in text

    if is_m3u8:
        # Parse EXT-X-KEY if present
        key_url: str | None = None
        key_iv: str | None = None
        key_method: str | None = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-KEY:"):
                params = line[len("#EXT-X-KEY:") :]
                for param in params.split(","):
                    param = param.strip()
                    if param.startswith("METHOD="):
                        key_method = param.split("=", 1)[1].strip().strip('"')
                    elif param.startswith("URI="):
                        key_url = param.split("=", 1)[1].strip().strip('"')
                    elif param.startswith("IV=0x"):
                        key_iv = param.split("=", 1)[1].strip()

        is_encrypted = key_method and key_method != "NONE"

        # Try to fetch key for encrypted playlists
        key_bytes: bytes | None = None
        iv_bytes: bytes | None = None
        if is_encrypted and decrypt and key_url and HAS_AES:
            absolute_key_url = normalize_url(urljoin(playlist_url, key_url))
            if absolute_key_url:
                try:
                    hdrs = dict(headers or {})
                    key_data, _ = fetch(absolute_key_url, timeout, hdrs, retries, None)
                    key_bytes = key_data
                    if key_iv:
                        iv_bytes = bytes.fromhex(key_iv.removeprefix("0x"))
                    else:
                        iv_bytes = b"\x00" * 16
                    _log.info("decryption key fetched for %s", playlist_url)
                except Exception as exc:
                    _log.warning("failed to fetch decryption key for %s: %s", playlist_url, exc)

        for line in text.splitlines():
            value = line.strip().lstrip("﻿")
            if not value or value.startswith("#") or not looks_like_downloadable(value):
                continue
            absolute = normalize_url(urljoin(playlist_url, value))
            if absolute:
                found.append(Resource(absolute, "playlist[m3u8]", "source", page_url))
                if key_bytes and iv_bytes:
                    register_segment_key(absolute, key_bytes, iv_bytes)

        if is_encrypted and key_bytes is None:
            return found, (
                f"encrypted playlist detected (method: {key_method}); "
                f"{'install pycryptodome for AES-128 decryption' if decrypt else 'use --decrypt to attempt decryption'}"
            )
        if is_encrypted and key_bytes:
            return (
                found,
                f"encrypted playlist detected, segments will be decrypted (method: {key_method})",
            )
        return found, ""

    if is_mpd:
        for match in re.finditer(r"<BaseURL[^>]*>(.*?)</BaseURL>", text, re.IGNORECASE | re.DOTALL):
            value = html_lib.unescape(match.group(1).strip())
            if not looks_like_downloadable(value):
                continue
            absolute = normalize_url(urljoin(playlist_url, value))
            if absolute:
                found.append(Resource(absolute, "playlist[mpd]", "source", page_url))
        return found, ""

    return [], ""


def discover_sitemap_urls(sitemap_url: str, headers: dict[str, str], timeout: int) -> list[str]:
    """Parse sitemap.xml and return discovered page URLs."""
    urls: list[str] = []
    try:
        data, _ = fetch(sitemap_url, timeout, headers, retries=1, max_bytes=10 * 1024 * 1024)
        text = data.decode("utf-8", errors="replace")
        # Standard sitemap <loc> elements
        for match in re.finditer(r"<loc[^>]*>(.*?)</loc>", text, re.IGNORECASE | re.DOTALL):
            url = html_lib.unescape(match.group(1).strip())
            normalized = normalize_url(url)
            if normalized:
                urls.append(normalized)
        # Sitemap index → nested sitemaps (one level deep)
        if any(tag in text for tag in ("<sitemapindex", "<sitemap>")):
            nested_sitemaps = urls
            urls = []
            for nested in nested_sitemaps:
                try:
                    sub_data, _ = fetch(
                        nested, timeout, headers, retries=1, max_bytes=10 * 1024 * 1024
                    )
                    sub_text = sub_data.decode("utf-8", errors="replace")
                    for match in re.finditer(
                        r"<loc[^>]*>(.*?)</loc>", sub_text, re.IGNORECASE | re.DOTALL
                    ):
                        url = html_lib.unescape(match.group(1).strip())
                        normalized = normalize_url(url)
                        if normalized:
                            urls.append(normalized)
                except Exception as exc:
                    _log.warning("failed to fetch nested sitemap %s: %s", nested, exc)
        _log.info("discovered %d URLs from sitemap", len(urls))
    except Exception as exc:
        _log.warning("failed to fetch sitemap %s: %s", sitemap_url, exc)
    return urls


# ── Config save/load ──────────────────────────────────────────────────────


def save_config_to_file(args: argparse.Namespace, filepath: str) -> None:
    """Save crawl configuration as JSON."""
    config = {
        "url": args.url,
        "out": str(Path(args.out).resolve()),
        "same_domain": args.same_domain,
        "crawl_pages": args.crawl_pages,
        "max_pages": args.max_pages,
        "include_css_urls": args.include_css_urls,
        "rewrite_html": args.rewrite_html,
        "strip_overlays": args.strip_overlays,
        "decrypt": args.decrypt,
        "video_mode": args.video_mode,
        "video_only": args.video_only,
        "list_only": args.list_only,
        "expand_playlists": args.expand_playlists,
        "respect_robots": args.respect_robots,
        "timeout": args.timeout,
        "retries": args.retries,
        "delay": args.delay,
        "workers": args.workers,
        "max_bytes": args.max_bytes,
        "encoding": args.encoding,
        "user_agent": args.user_agent,
        "header": args.header,
        "block_keyword": args.block_keyword,
        "resume": args.resume,
        "organize": args.organize,
        "dedup": args.dedup,
        "sitemap": args.sitemap,
        "smart_extract": args.smart_extract,
        "resume_crawl": args.resume_crawl,
        "extract_text": args.extract_text,
        "include_pattern": args.include_pattern,
        "exclude_pattern": args.exclude_pattern,
        "proxy": args.proxy,
        "stealth": args.stealth,
        "impersonate": args.impersonate,
    }
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    _log.info("config saved to %s", path)


def load_config_from_file(filepath: str) -> dict:
    """Load crawl configuration from JSON file and return as dict."""
    path = Path(filepath)
    if not path.exists():
        _log.error("config file not found: %s", path)
        sys.exit(2)  # 配置错误退出码 2（区别于 1=取消、0=成功）
    config = json.loads(path.read_text(encoding="utf-8"))
    _log.info("config loaded from %s", path)
    return config


# ── Crawl state persistence (--resume-crawl) ──


def save_crawl_state(output_dir: Path, **state: object) -> None:
    """Save current crawl progress to a JSON state file."""
    path = output_dir / CRAWL_STATE_FILE
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def load_crawl_state(output_dir: Path) -> dict:
    """Load saved crawl state if it exists."""
    path = output_dir / CRAWL_STATE_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log.warning("failed to load crawl state: %s", exc)
        return {}


def clear_crawl_state(output_dir: Path) -> None:
    path = output_dir / CRAWL_STATE_FILE
    if path.exists():
        path.unlink()


# ── The main crawl function ───────────────────────────────────────────────


def crawl(args: argparse.Namespace) -> int:
    crawl_start_time = time.time()
    report_config: dict[str, object] = {
        "url": getattr(args, "url", ""),
        "workers": getattr(args, "workers", ""),
        "delay": getattr(args, "delay", ""),
        "timeout": getattr(args, "timeout", ""),
        "retries": getattr(args, "retries", ""),
        "same_domain": getattr(args, "same_domain", ""),
        "respect_robots": getattr(args, "respect_robots", ""),
        "dedup": getattr(args, "dedup", ""),
        "resume": getattr(args, "resume", ""),
        "organize": getattr(args, "organize", ""),
        "list_only": getattr(args, "list_only", ""),
        "video_only": getattr(args, "video_only", ""),
        "expand_playlists": getattr(args, "expand_playlists", ""),
        "smart_extract": getattr(args, "smart_extract", ""),
        "extract_text": getattr(args, "extract_text", ""),
        "strip_overlays": getattr(args, "strip_overlays", ""),
        "decrypt": getattr(args, "decrypt", ""),
        "sitemap": getattr(args, "sitemap", ""),
        "crawl_pages": getattr(args, "crawl_pages", ""),
        "max_pages": getattr(args, "max_pages", ""),
        "max_bytes": getattr(args, "max_bytes", ""),
    }
    try:
        extra_headers = parse_headers(args.header)
    except ValueError as exc:
        _log.error("header parse error: %s", exc)
        return 2

    headers = {"User-Agent": args.user_agent, **extra_headers}
    output_dir = Path(args.out).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    max_bytes = args.max_bytes if args.max_bytes > 0 else None
    block_keywords = parse_block_keywords(args.block_keyword)
    robots = make_robots_parser(args.url, headers, args.timeout) if args.respect_robots else None
    rate_limiter = DomainRateLimiter(default_delay=max(0.0, args.delay))
    dedup = ContentDedup() if args.dedup else None

    # ── Page discovery ──
    page_queue: deque[str] = deque()
    seen_pages: set[str] = set()

    # Start with the --url
    root_url = normalize_url(args.url)
    if root_url:
        page_queue.append(root_url)

    # Sitemap discovery
    if args.sitemap:
        parsed = urlparse(args.url)
        sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
        _log.info("discovering pages from %s", sitemap_url)
        sitemap_urls = discover_sitemap_urls(sitemap_url, headers, args.timeout)
        for su in sitemap_urls:
            if su not in seen_pages and su not in page_queue:
                if args.same_domain and not same_domain(su, args.url):
                    continue
                if is_blocked_url(su, block_keywords):
                    continue
                page_queue.append(su)

    all_resources: list[Resource] = []
    page_html: dict[str, str] = {}
    page_titles: dict[str, str] = {}

    # ── Resume from saved state? ──
    if getattr(args, "resume_crawl", False):
        saved = load_crawl_state(output_dir)
        if saved:
            page_queue = deque(saved.get("page_queue", []))
            seen_pages = set(saved.get("seen_pages", []))
            page_titles = {k: str(v) for k, v in saved.get("page_titles", {}).items()}
            for rdict in saved.get("resources", []):
                all_resources.append(Resource(**rdict))
            if dedup and saved.get("sha256_set"):
                for h in saved["sha256_set"]:
                    dedup.mark_hash_seen(str(h))
            _log.info(
                "resumed: %d pages seen, %d pages queued, %d resources, %d dedup hashes",
                len(seen_pages),
                len(page_queue),
                len(all_resources),
                dedup.seen_count() if dedup else 0,
            )
        else:
            _log.info("no saved state found, starting fresh")

    # ── Scan pages ──
    cancelled = False  # 统一取消标志：任何阶段触发取消后跳过后续耗时步骤并返回 1
    while page_queue and len(seen_pages) < args.max_pages:
        try:
            wait_if_paused(args)
        except RuntimeError:
            # 暂停期间被取消 → wait_for_resume 抛 cancelled,按取消处理而非任务出错
            _log.info("cancelled by user")
            cancelled = True
            break
        if should_stop(args):
            _log.info("cancelled before scanning next page")
            cancelled = True
            break
        page_url = page_queue.popleft()
        if not page_url or page_url in seen_pages:  # pragma: no cover - 防御性：队列已预过滤
            continue
        if is_blocked_url(page_url, block_keywords):
            _log.info("blocked page: %s", page_url)
            continue
        if args.same_domain and not same_domain(
            page_url, args.url
        ):  # pragma: no cover - 防御性：入队时已过滤
            continue
        if robots and not robots.can_fetch(
            args.user_agent, page_url
        ):  # pragma: no cover - 防御性：页面 robots 检查
            _log.info("robots.txt skipped page: %s", page_url)
            continue

        _log.info("scanning page: %s", page_url)
        report_progress(
            args,
            phase="page",
            current_url=page_url,
            pages_scanned=len(seen_pages),
            total_resources=0,
            processed_resources=0,
        )
        seen_pages.add(page_url)
        try:
            data, content_type = fetch(
                page_url,
                args.timeout,
                headers,
                args.retries,
                max_bytes,
                rate_limiter=rate_limiter,
                control_args=args,
            )
        except Exception as exc:
            _log.warning("page fetch failed: %s", exc)
            continue

        page_path = output_path_for_url(page_url, output_dir, "text/html", prefix="pages")
        if not page_path.suffix:  # pragma: no cover - text/html 总是返回带后缀的路径
            page_path = page_path.with_suffix(".html")
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_bytes(data)

        html = decode_text(data, content_type, args.encoding)
        page_html[page_url] = html
        page_titles[page_url] = extract_title(html)
        parser = PageParser(page_url)
        parser.feed(html)
        all_resources.extend(parser.resources)

        if args.crawl_pages:
            for link in parser.page_links:
                if link not in seen_pages and link not in page_queue:
                    if is_blocked_url(
                        link, block_keywords
                    ):  # pragma: no cover - 防御性：资源级 block 检查已在更上层处理
                        continue
                    if not args.same_domain or same_domain(link, args.url):
                        page_queue.append(link)

            # Save crawl state 节流：每 5 页或队列耗尽时保存一次,避免每页全量重写
            if getattr(args, "resume_crawl", False) and (
                len(seen_pages) % 5 == 0 or not page_queue
            ):
                try:
                    save_crawl_state(
                        output_dir,
                        page_queue=list(page_queue),
                        seen_pages=list(seen_pages),
                        page_titles=page_titles,
                        resources=[asdict(r) for r in all_resources],
                        sha256_set=dedup.seen_hashes() if dedup else [],
                    )
                except Exception as exc:  # pragma: no cover - 防御性：状态保存异常吞掉
                    _log.warning("failed to save crawl state: %s", exc)

    resources = unique_resources(all_resources)
    if block_keywords:
        resources = [r for r in resources if not is_blocked_url(r.url, block_keywords)]
    if args.same_domain:
        resources = [r for r in resources if same_domain(r.url, args.url)]
    if args.video_only:
        resources = [r for r in resources if is_video_candidate(r)]
    # URL 正则过滤
    _include_re = re.compile(args.include_pattern) if args.include_pattern else None
    _exclude_re = re.compile(args.exclude_pattern) if args.exclude_pattern else None
    if _include_re:
        resources = [r for r in resources if _include_re.search(r.url)]
    if _exclude_re:
        resources = [r for r in resources if not _exclude_re.search(r.url)]

    # 阶段入口守卫：页面扫描期间已取消 → 跳过下载阶段与后处理
    if cancelled or should_stop(args):
        _log.info("crawl cancelled; skipping download phase")
        return 1

    if not resources:
        _log.info("no resources found")
        write_manifests(output_dir, [])
        write_summary(
            output_dir,
            [],
            len(seen_pages),
            start_time=crawl_start_time,
            end_time=time.time(),
            config=report_config,
        )
        write_run_report(
            output_dir,
            [],
            len(seen_pages),
            start_time=crawl_start_time,
            end_time=time.time(),
            config=report_config,
        )
        return 0

    # ── Download phase (concurrent) ──
    manifest_rows: list[ManifestRow] = []
    manifest_lock = threading.Lock()
    queue = list(resources)
    queued_urls = {r.url for r in queue}

    # JSONL 实时清单：每下载完成一个资源立即追加一行（供监控/断点查看），
    # 打开失败（磁盘满等）降级为仅内存收集，不影响抓取
    jsonl_path = output_dir / "resources_manifest.jsonl"
    try:
        jsonl_file = jsonl_path.open("w", encoding="utf-8")
    except OSError as exc:
        _log.warning("failed to open JSONL manifest: %s", exc)
        jsonl_file = None

    def _append_jsonl(row: ManifestRow) -> None:
        """best-effort 逐条写 JSONL；失败仅记一次 warning，不影响主流程。"""
        if jsonl_file is None:
            return
        try:
            jsonl_file.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
        except OSError as exc:
            _log.warning("failed to write JSONL manifest: %s", exc)

    _log.info("downloading %d resources with %d workers...", len(queue), args.workers)
    report_progress(
        args,
        phase="resources",
        total_resources=len(queue),
        processed_resources=0,
        pages_scanned=len(seen_pages),
    )

    processed_count = [0]
    new_discoveries: list[Resource] = []
    discovery_lock = threading.Lock()

    def process_one(resource: Resource) -> ManifestRow | None:
        if robots and not robots.can_fetch(args.user_agent, resource.url):
            return row_for("skipped by robots.txt", resource, "", "", 0, page_titles)

        _log.debug("[%d/%d] %s", processed_count[0] + 1, len(queue), resource.url)

        if args.list_only:
            report_progress(
                args,
                phase="download",
                current_url=resource.url,
                total_resources=len(queue),
                processed_resources=processed_count[0] + 1,
                pages_scanned=len(seen_pages),
            )
            return row_for("listed only", resource, "", "", 0, page_titles)

        try:
            initial_prefix = output_prefix_for_resource(args, resource, "", page_titles)
            target = output_path_for_url(resource.url, output_dir, "", prefix=initial_prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            # 需要完整内容（去重/解密/CSS 解析/播放清单展开）时走内存路径;
            # 否则流式写入 .part 临时文件,避免大文件整包驻留内存（fetch 返回 b""）
            needs_content = (
                bool(dedup)
                or (getattr(args, "decrypt", False) and HAS_AES)
                or bool(args.include_css_urls)
                or bool(args.expand_playlists)
            )
            stream_to_disk = not getattr(args, "resume", False) and not needs_content
            data, content_type = fetch(
                resource.url,
                args.timeout,
                headers,
                args.retries,
                max_bytes,
                resume_path=target if (getattr(args, "resume", False) or stream_to_disk) else None,
                rate_limiter=rate_limiter,
                control_args=args,
            )
            byte_count = (
                target.stat().st_size if (stream_to_disk and target.exists()) else len(data)
            )

            # Content deduplication
            sha256 = ""
            if dedup:
                is_dup, sha256 = dedup.is_duplicate(data, resource.url)
                if is_dup:
                    return row_for(
                        "skipped by dedup",
                        resource,
                        "",
                        content_type,
                        len(data),
                        page_titles,
                        sha256=sha256,
                    )

            final_prefix = output_prefix_for_resource(args, resource, content_type, page_titles)
            final_target = output_path_for_url(
                resource.url, output_dir, content_type, prefix=final_prefix
            )
            if final_target != target and target.exists():  # pragma: no cover - 仅 resume 场景触发
                final_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(final_target))
                target = final_target
            target.parent.mkdir(parents=True, exist_ok=True)
            if not getattr(args, "resume", False) and not stream_to_disk:
                write_data = data
                if (
                    getattr(args, "decrypt", False) and HAS_AES
                ):  # pragma: no cover - pycryptodome 未安装时不可达
                    key_info = get_segment_key(resource.url)
                    if key_info:
                        try:
                            write_data = decrypt_aes128(data, key_info[0], key_info[1])
                            _log.info("decrypted segment: %s", resource.url)
                        except Exception as exc:
                            _log.warning("decryption failed for %s: %s", resource.url, exc)
                            raise ValueError(f"decryption failed, skipped: {resource.url}") from exc
                target.write_bytes(write_data)

            status = "ok"
            local_discoveries: list[Resource] = []

            # CSS resource discovery
            if args.include_css_urls and (
                "text/css" in content_type or resource.url.lower().endswith(".css")
            ):
                css_text = decode_text(data, content_type, args.encoding)
                for extra in discover_css_resources(css_text, resource.url, resource.page_url):
                    if args.same_domain and not same_domain(
                        extra.url, args.url
                    ):  # pragma: no cover - 防御性：CSS 资源跨域过滤
                        continue
                    if is_blocked_url(
                        extra.url, block_keywords
                    ):  # pragma: no cover - 防御性：CSS 资源 block 过滤
                        continue
                    local_discoveries.append(extra)

            # Playlist expansion
            if (
                args.expand_playlists
                and category_for(resource.url, content_type, resource.kind, resource.found_in)
                == "playlist"
            ):
                playlist_text = decode_text(data, content_type, args.encoding)
                extra_resources, playlist_note = discover_playlist_resources(
                    playlist_text,
                    resource.url,
                    resource.page_url,
                    headers=headers,
                    timeout=args.timeout,
                    retries=args.retries,
                    decrypt=getattr(args, "decrypt", False),
                )
                if playlist_note:  # pragma: no cover - 播放列表 note 极少出现
                    status = f"{status}; {playlist_note}"
                for extra in extra_resources:
                    if args.same_domain and not same_domain(
                        extra.url, args.url
                    ):  # pragma: no cover - 防御性：播放列表跨域过滤
                        continue
                    if is_blocked_url(
                        extra.url, block_keywords
                    ):  # pragma: no cover - 防御性：播放列表 block 过滤
                        continue
                    if args.video_only and not is_video_candidate(
                        extra
                    ):  # pragma: no cover - 防御性：播放列表 video_only 过滤
                        continue
                    local_discoveries.append(extra)

            # Thread-safe: add discoveries to global list
            if local_discoveries:
                with discovery_lock:
                    for extra in local_discoveries:
                        if extra.url not in queued_urls:
                            queued_urls.add(extra.url)
                            new_discoveries.append(extra)
                            queue.append(extra)

            manifest_row = row_for(
                status, resource, str(target), content_type, byte_count, page_titles, sha256=sha256
            )

        except Exception as exc:
            if "cancelled by user" in str(exc):
                return None  # signal cancellation
            target = Path("")
            content_type = ""
            data = b""
            status = f"error: {exc}"
            manifest_row = row_for(status, resource, "", content_type, 0, page_titles)

        return manifest_row

    # Concurrent download loop
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_one, r): r for r in queue[:]}
        index = 0
        download_queue_size = len(queue)

        while futures and not cancelled:
            try:
                wait_if_paused(args)
            except RuntimeError:
                # 暂停期间被取消 → 按取消处理,统一退出码 1
                _log.info("cancelled by user")
                cancelled = True
                break
            if should_stop(args):
                for f in futures:
                    f.cancel()
                _log.info("cancelled by user")
                cancelled = True
                break

            done, _pending = wait(futures.keys(), timeout=0.5, return_when=FIRST_EXCEPTION)
            for future in done:
                resource_for_future = futures.pop(future)
                index += 1
                processed_count[0] = index
                try:
                    result = future.result()
                    if result is None:
                        # worker 检测到取消 → 与主线程取消路径统一：置标志后退出循环
                        _log.info("cancelled by user")
                        cancelled = True
                        break
                    with manifest_lock:
                        manifest_rows.append(result)
                        _append_jsonl(result)
                except Exception as exc:  # pragma: no cover - 防御性：process_one 已捕获所有异常
                    _log.error("unexpected worker error: %s", exc)
                    with manifest_lock:
                        failed_row = row_for(
                            "error: worker crashed", resource_for_future, "", "", 0, page_titles
                        )
                        manifest_rows.append(failed_row)
                        _append_jsonl(failed_row)

                report_progress(
                    args,
                    phase="download",
                    current_url=getattr(resource_for_future, "url", ""),
                    total_resources=download_queue_size,
                    processed_resources=index,
                    pages_scanned=len(seen_pages),
                )

            # Add newly discovered resources
            if new_discoveries and not cancelled:
                with discovery_lock:
                    while new_discoveries:
                        r = new_discoveries.pop(0)
                        fut = executor.submit(process_one, r)
                        futures[fut] = r
                        download_queue_size += 1

    # ── Post-processing ──
    # 每个阶段入口检查取消标志（含后处理期间新到达的取消请求），取消后跳过剩余耗时步骤

    def _post_pause_check() -> bool:
        """后处理阶段入口的暂停/取消检查：暂停会阻塞到恢复；取消（含暂停中取消）返回 True。"""
        try:
            wait_if_paused(args)
        except RuntimeError:
            pass  # 暂停期间取消 → should_stop 为 True,由返回值统一处理
        return should_stop(args)

    if cancelled or should_stop(args):
        _log.info("crawl cancelled; skipping post-processing")
        video_count = 0
        failed_count = 0
    else:
        # 每个后处理阶段独立 try/except：单步失败仅记 warning，保证清单与报告尽量生成
        if args.rewrite_html and not _post_pause_check():
            try:
                for page_url, html in page_html.items():
                    rewritten = rewrite_html(html, manifest_rows, page_url, output_dir)
                    if getattr(args, "strip_overlays", False):
                        rewritten = strip_page_overlays(rewritten)
                    rewritten_path = output_path_for_url(
                        page_url, output_dir, "text/html", prefix="offline_pages"
                    )
                    rewritten_path = rewritten_path.with_suffix(".html")
                    rewritten_path.parent.mkdir(parents=True, exist_ok=True)
                    rewritten_path.write_text(rewritten, encoding="utf-8")
            except Exception as exc:
                _log.warning("offline HTML rewrite failed: %s", exc)

        try:
            write_manifests(output_dir, manifest_rows)
        except Exception as exc:
            _log.warning("failed to write manifests: %s", exc)
        video_count = 0
        failed_count = 0
        if not _post_pause_check():
            try:
                video_count = (
                    write_video_manifests(output_dir, manifest_rows) if args.video_mode else 0
                )
                failed_count = write_failed_manifests(output_dir, manifest_rows)
            except Exception as exc:
                _log.warning("failed to write video/failed manifests: %s", exc)

        # Smart data extraction
        if getattr(args, "smart_extract", False) and not _post_pause_check():
            try:
                extracted_data: list[dict[str, object]] = []
                for page_url, html in page_html.items():
                    extracted_data.append(smart_extract(html, page_url))
                if extracted_data:
                    write_extracted_data(output_dir, extracted_data)
            except Exception as exc:
                _log.warning("smart extraction failed: %s", exc)

        # Readable text extraction
        if getattr(args, "extract_text", False) and not _post_pause_check():
            try:
                text_dir = output_dir / "extracted_text"
                text_dir.mkdir(parents=True, exist_ok=True)
                count = 0
                for page_url, html in page_html.items():
                    text = extract_readable_text(html)
                    if not text:
                        continue
                    name = urlparse(page_url).path.strip("/").replace("/", "_") or "index"
                    (text_dir / f"{name}.txt").write_text(text, encoding="utf-8")
                    count += 1
                _log.info("extracted text: %d pages -> %s", count, text_dir)
            except Exception as exc:
                _log.warning("text extraction failed: %s", exc)

        if not _post_pause_check():
            try:
                write_summary(
                    output_dir,
                    manifest_rows,
                    len(seen_pages),
                    start_time=crawl_start_time,
                    end_time=time.time(),
                    config=report_config,
                )
                write_run_report(
                    output_dir,
                    manifest_rows,
                    len(seen_pages),
                    start_time=crawl_start_time,
                    end_time=time.time(),
                    config=report_config,
                )
            except Exception as exc:
                _log.warning("failed to write run report: %s", exc)

        # JSONL 清单已在下载阶段逐条追加；此处仅收尾 flush/关闭
        if jsonl_file is not None:
            try:
                jsonl_file.flush()
                jsonl_file.close()
            except OSError as _jsonl_err:
                _log.warning("failed to finalize JSONL manifest: %s", _jsonl_err)

        # Clear resume state on successful completion
        if getattr(args, "resume_crawl", False) and not _post_pause_check():
            try:
                clear_crawl_state(output_dir)
                _log.info("crawl state cleared")
            except Exception as exc:
                _log.warning("failed to clear crawl state: %s", exc)

    ok_count = sum(1 for row in manifest_rows if row.status.startswith("ok"))
    dedup_count = sum(1 for row in manifest_rows if "dedup" in row.status)
    total_bytes = sum(row.bytes for row in manifest_rows if row.status == "ok")

    _log.info("")
    _log.info("Pages scanned:       %d", len(seen_pages))
    _log.info("Resources found:     %d", len(resources))
    _log.info("Downloaded:          %d (%s)", ok_count, _format_bytes(total_bytes))
    _log.info("Deduplicated:        %d", dedup_count)
    _log.info("Failed:              %d", failed_count)
    _log.info("Duration:            %s", format_duration(time.time() - crawl_start_time))
    _log.info("Output:              %s", output_dir)
    if not (cancelled or should_stop(args)):
        _log.info("CSV manifest:        %s", output_dir / "resources_manifest.csv")
        _log.info("JSON manifest:       %s", output_dir / "resources_manifest.json")
        _log.info("Summary:             %s", output_dir / "summary.txt")
        _log.info("Run report (JSON):   %s", output_dir / "run_report.json")
        _log.info("Run report (MD):     %s", output_dir / "run_report.md")
        _log.info("Run report (HTML):   %s", output_dir / "run_report.html")
        if args.video_mode:
            _log.info("Video resources:     %d", video_count)

    # 统一退出码：0 成功 / 1 用户取消 / 2 参数错误
    return 1 if (cancelled or should_stop(args)) else 0


# ── CLI parser ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download resources referenced by web pages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --url https://example.com
  %(prog)s --url https://example.com --workers 16 --include-css-urls
  %(prog)s --url https://example.com --sitemap --same-domain --max-pages 50
  %(prog)s --load-config my_project.json
  %(prog)s --url https://example.com --save-config my_project.json
        """,
    )
    parser.add_argument("--url", help="Start page URL.")
    parser.add_argument(
        "--out", default=os.path.join(os.getcwd(), "crawler_output"), help="Output directory."
    )
    parser.add_argument(
        "--same-domain", action="store_true", help="Only scan/download same-domain URLs."
    )
    parser.add_argument("--crawl-pages", action="store_true", help="Follow same-domain page links.")
    parser.add_argument("--max-pages", type=int, default=1, help="Maximum pages to scan.")
    parser.add_argument(
        "--include-css-urls",
        action="store_true",
        help="Download CSS url(...) and @import resources.",
    )
    parser.add_argument(
        "--rewrite-html",
        action="store_true",
        help="Write offline HTML with downloaded absolute URLs rewritten.",
    )
    parser.add_argument(
        "--strip-overlays",
        action="store_true",
        help="Remove overlay/popup/paywall elements from saved HTML (use with --rewrite-html).",
    )
    parser.add_argument(
        "--decrypt",
        action="store_true",
        help="Decrypt AES-128 encrypted m3u8 segments (requires pycryptodome).",
    )
    parser.add_argument(
        "--video-mode",
        action="store_true",
        help="Also write video_manifest files for video/playlist/subtitle/poster resources.",
    )
    parser.add_argument(
        "--video-only",
        action="store_true",
        help="Only list/download video/playlist/subtitle/poster resources.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only list discovered resources in manifests; do not download resource files.",
    )
    parser.add_argument(
        "--expand-playlists",
        action="store_true",
        help="Expand unencrypted m3u8/mpd playlists into nested media entries.",
    )
    parser.add_argument(
        "--respect-robots", action="store_true", help="Respect robots.txt crawl rules."
    )
    parser.add_argument("--timeout", type=int, default=20, help="Request timeout in seconds.")
    parser.add_argument(
        "--retries", type=int, default=1, help="Retries per URL after a failed request."
    )
    parser.add_argument(
        "--delay", type=float, default=0.5, help="Delay between requests (per-domain adaptive)."
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="Number of concurrent download threads.",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=0,
        help="Skip files larger than this many bytes; 0 disables.",
    )
    parser.add_argument("--encoding", help="Force text decoding, e.g. utf-8 or gbk.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="HTTP User-Agent header.")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Extra request header, e.g. 'Cookie: name=value' (repeatable).",
    )
    parser.add_argument(
        "--block-keyword",
        action="append",
        default=[],
        help="Skip URLs containing this keyword/domain (repeatable).",
    )
    parser.add_argument("--include-pattern", help="Only download URLs matching this regex pattern.")
    parser.add_argument("--exclude-pattern", help="Skip URLs matching this regex pattern.")
    parser.add_argument("--proxy", help="HTTP/HTTPS proxy address, e.g. http://127.0.0.1:7890")
    parser.add_argument(
        "--stealth",
        action="store_true",
        help="Use the library's curl_cffi TLS-fingerprint fetcher (browser JA3/JA4 "
        "impersonation) for page HTML and non-resumable fetches. Defeats fingerprint "
        "blocking that plain urllib cannot. Resumable/large downloads keep streaming.",
    )
    parser.add_argument(
        "--impersonate",
        default="chrome131",
        help="Browser fingerprint to impersonate in --stealth mode (default: chrome131). "
        "See curl_cffi BrowserType for the full list (chrome120, edge101, firefox135, ...).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume interrupted downloads using .part files and HTTP Range.",
    )
    parser.add_argument(
        "--organize", action="store_true", help="Save resources into category/page-title folders."
    )
    parser.add_argument(
        "--dedup", action="store_true", help="Skip downloading files with identical SHA256 content."
    )
    parser.add_argument(
        "--sitemap", action="store_true", help="Discover pages from /sitemap.xml before crawling."
    )
    parser.add_argument(
        "--smart-extract", action="store_true", help="Extract structured data from scanned pages."
    )
    parser.add_argument(
        "--resume-crawl", action="store_true", help="Resume interrupted crawl from saved state."
    )
    parser.add_argument(
        "--extract-text",
        action="store_true",
        help="Extract readable article text from saved HTML pages.",
    )
    parser.add_argument("--save-config", help="Save crawl configuration to a JSON file and exit.")
    parser.add_argument("--load-config", help="Load crawl configuration from a JSON file.")
    return parser


# ── Entry point ───────────────────────────────────────────────────────────


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Config save/load
    if args.load_config:
        config = load_config_from_file(args.load_config)
        # Override with CLI arguments if provided
        for key, value in vars(args).items():
            if key == "load_config":
                continue
            if value is not None and value != parser.get_default(key):
                config[key] = value
        # 以 parser 默认值为底，配置文件/CLI 覆盖其上：保证保存文件缺省
        # 的字段（如 include_pattern/stealth）在 load 后仍然存在，
        # 避免 crawl() 访问缺失属性崩溃（save→load 往返兼容）
        defaults = {key: parser.get_default(key) for key in vars(args)}
        defaults.update(config)
        args = argparse.Namespace(**defaults)

    if args.save_config:
        if not args.url:
            parser.error("--url is required when using --save-config")
        save_config_to_file(args, args.save_config)
        return

    if not args.url:
        parser.error("the following arguments are required: --url")

    raise SystemExit(crawl(args))


if __name__ == "__main__":  # pragma: no cover
    main()

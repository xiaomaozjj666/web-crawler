"""crawler 基础层:日志挂钩、AES/分段密钥、库级 Fetcher 桥接与 fetch。

从 ``crawler.py`` 下沉的底层能力;阶段子模块(:mod:`_crawler_discovery` /
:mod:`_crawler_download` / :mod:`_crawler_scan` / :mod:`_crawler_post`)与编排层
(:mod:`crawler`)统一从本模块按属性访问(``cr.fetch(...)``),保证对
``_crawler_core`` 全局名(fetch / should_stop / HAS_AES / _get_app_fetcher 等)的
patch 对全部阶段生效。

网络传输已整体下沉到库级 :class:`web_crawler.fetchers.Fetcher`(curl_cffi
TLS 指纹主路径 + httpx 兜底):本模块只保留应用层横切关注点——SSRF 入口
校验、请求头基底、域级限速、429 退避与暂停/取消钩子;流式断点续传、
重试退避、代理、会话池与 DNS 钉扎全部由库层请求管线承担。
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from httpx import HTTPError as _HttpxTransportError

from web_crawler._ssrf import is_power_mode, validate_url_host
from web_crawler.app._crawler_context import DEFAULT_USER_AGENT
from web_crawler.app.crawler_net import DomainRateLimiter
from web_crawler.exceptions import FetchError

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


# ── HTTP 网络层（库级 Fetcher 桥接）──────────────────────────────────

# 重定向最大跳数（与历史 urllib 行为一致），传给库级 Fetcher 的 max_redirects；
# 每一跳由库层重新校验目标 scheme + host（SSRF 防护）。
_MAX_REDIRECTS = 10


def _import_lib_fetcher() -> Any:
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
                "web_crawler.Fetcher is not importable; install the library or run "
                "with PYTHONPATH=src."
            )
            return None
    return Fetcher


# 应用 fetcher 按线程缓存：下载/抓取跑在 ThreadPoolExecutor 里，同一
# fetcher 的 curl 会话不可跨线程并发使用（有状态资源）；threading.local
# 让每个 worker 线程持有独立会话，线程结束时随 local 存储一起回收。
# 键为 (impersonate, proxy, timeout)：任一变化即换新实例。
_fetchers_local = threading.local()


def _get_app_fetcher(
    impersonate: str | None,
    timeout: float,
    proxy: str | None,
) -> Any:
    """返回与 *impersonate*/*timeout*/*proxy* 匹配的线程本地库级 ``Fetcher``。

    ``impersonate=None``（默认抓取路径）不做 TLS 指纹伪装——User-Agent
    语义与历史 urllib 路径一致；``--stealth`` 时传入目标指纹走 curl_cffi
    伪装。``retries=0``：应用层 fetch() 自持重试循环（429 退避 / 域级
    限速自适应在应用层），库层不做内建重试。
    """
    cache: dict[tuple[str | None, str | None, str], Any] | None = getattr(
        _fetchers_local, "cache", None
    )
    if cache is None:
        cache = {}
        _fetchers_local.cache = cache
    key = (impersonate, proxy, str(timeout))
    if key in cache:
        return cache[key]
    fetcher_cls = _import_lib_fetcher()
    if fetcher_cls is None:
        raise FetchError("web_crawler.Fetcher unavailable; install the library")
    cache[key] = fetcher_cls(
        impersonate=impersonate,
        timeout=float(timeout),
        proxy=proxy,
        retries=0,
        follow_redirects=True,  # 库层手动逐跳跟随并校验（scheme + host，防 SSRF）
        max_redirects=_MAX_REDIRECTS,
    )
    return cache[key]


def _request_headers(url: str, headers: dict[str, str]) -> dict[str, str]:
    """构造请求头基底：默认 User-Agent + Accept + Referer（历史语义）。"""
    request_headers = {
        "User-Agent": headers.get("User-Agent", DEFAULT_USER_AGENT),
        "Accept": "*/*",
        **{k: v for k, v in headers.items() if k.lower() != "user-agent"},
    }
    if "referer" not in {k.lower() for k in request_headers}:
        request_headers["Referer"] = url
    return request_headers


# --stealth 的 UA 与 TLS 指纹必须成对：curl_cffi 伪装成 Chrome 的 JA3/JA4，
# 请求头里却还是爬虫自己的 UA，反爬侧一眼识破（2026-11-17 实测：julius.ai
# 对「chrome131 指纹 + ResourceCrawler UA」返回 403，换成浏览器 UA 即 200）。
# 仅当用户未显式指定 UA（仍是默认值）时才替换；显式 --user-agent 尊重用户选择。
def _stealth_user_agent(impersonate: str | None) -> str:
    """返回与 *impersonate* 指纹预设语义匹配的浏览器 User-Agent。"""
    preset = (impersonate or "chrome131").strip().lower()
    match = re.search(r"(\d+)", preset)
    version = match.group(1) if match else "131"
    if preset.startswith("firefox"):
        return (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{version}.0) "
            f"Gecko/20100101 Firefox/{version}.0"
        )
    if preset.startswith("safari"):
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            f"(KHTML, like Gecko) Version/{version}.0 Safari/605.1.15"
        )
    if preset.startswith("edge"):
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36 Edg/{version}.0.0.0"
        )
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"
    )


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


# ── Fetch（应用层横切关注点 + 库级网络管线）─────────────────────


def _header_value(headers: Any, name: str) -> str:
    """大小写不敏感地取一个响应头（httpx 全小写，curl_cffi 常见 Title-Case）。"""
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if getter is not None:
        try:
            value = getter(name)
        except Exception:  # pragma: no cover - 畸形映射的防御
            value = None
        if value:
            return str(value)
    items = getattr(headers, "items", None)
    if items is not None:
        lowered = name.lower()
        try:
            for key, value in items():
                if str(key).lower() == lowered and value:
                    return str(value)
        except Exception:  # pragma: no cover - 畸形映射的防御
            return ""
    return ""


def fetch(
    url: str,
    timeout: int,
    headers: dict[str, str],
    retries: int,
    max_bytes: int | None,
    resume_path: Path | None = None,
    rate_limiter: DomainRateLimiter | None = None,
    control_args: argparse.Namespace | None = None,
    meta: dict[str, object] | None = None,
    *,
    extra_headers: dict[str, str] | None = None,
    allow_not_modified: bool = False,
    response_meta: dict[str, object] | None = None,
) -> tuple[bytes, str]:
    """抓取 ``url``：``resume_path`` 时流式落盘（断点续传），否则整包返回。

    网络传输全部走库级 :class:`~web_crawler.fetchers.Fetcher`——SSRF 校验/
    代理/会话池/DNS 钉扎/流式断点续传在库层管线内生效；本函数保留应用层
    职责——入口 SSRF 校验、请求头基底、域级限速、429 退避与暂停/取消钩子。

    返回 ``(body, content_type)``；落盘路径 body 为 ``b""``（内容在盘上）。

    ``meta`` 非空时写入本次**成功**响应的 ``status`` / ``content_type``
    （页面扫描用它填充逐页元数据；失败响应不写，避免把重试中的中间状态
    带进报告）。

    ``extra_headers`` 追加到请求（``--cache`` 的条件请求头）。``allow_not_modified``
    为真时 ``304 Not Modified`` 不再是失败：不重试、不写盘，返回 ``(b"", "")``，
    调用方据 ``response_meta["status"]`` 判定复用。``response_meta`` 是出参，
    写入本次响应的 ``status`` / ``content_type`` / ``etag`` / ``last_modified``
    （含 304；与只记成功响应的 ``meta`` 区分）。

    注意：条件请求头只应在 ``allow_not_modified=True`` 时发送——否则 304 的
    空 body 会被内存路径当成「成功下载 0 字节」，覆盖掉好产物。
    """
    # SSRF 防护：请求前校验 host，拒绝私网/环回/链路本地目标（含云元数据地址）；
    # 重定向目标由库层 Fetcher 逐跳校验（scheme + host）。
    # Power Mode（WEB_CRAWLER_POWER_MODE=1）放行 host 校验（仅保留 scheme 白名单）。
    # resolve=True：主机名先做 DNS 解析复查（防重绑定），结果带缓存防重复解析。
    if not is_power_mode():
        validate_url_host(url, resolve=True)

    stealth = bool(control_args and getattr(control_args, "stealth", False))
    impersonate: str | None = getattr(control_args, "impersonate", "chrome131") if stealth else None
    proxy = getattr(control_args, "proxy", None) if control_args else None
    request_headers = _request_headers(url, headers)
    if extra_headers:
        request_headers.update(extra_headers)
    if stealth and request_headers.get("User-Agent") == DEFAULT_USER_AGENT:
        # TLS 指纹已伪装成浏览器，UA 不能还停留在爬虫默认值（见 _stealth_user_agent）。
        request_headers["User-Agent"] = _stealth_user_agent(impersonate)

    # 最终响应头暂存（on_headers 回调写入）：content_type 供返回值，
    # Retry-After 供 429 退避使用。
    _state: dict[str, str] = {}

    def _record_response(status: int, resp_headers: Any) -> None:
        """把本次响应（含 304）的状态/类型/校验器写进出参 response_meta。"""
        if response_meta is None:
            return
        response_meta["status"] = status
        response_meta["content_type"] = _header_value(resp_headers, "content-type")
        response_meta["etag"] = _header_value(resp_headers, "etag")
        response_meta["last_modified"] = _header_value(resp_headers, "last-modified")

    def _on_headers(status: int, resp_headers: Any) -> None:
        content_type = _header_value(resp_headers, "content-type")
        _state["content_type"] = content_type
        if meta is not None:
            meta["status"] = status
            meta["content_type"] = content_type
        _record_response(status, resp_headers)
        _state["retry_after"] = _header_value(resp_headers, "retry-after")

    def _should_abort() -> bool:
        # 每分块写入前的暂停/取消探针：先阻塞等待暂停解除，再返回是否取消。
        wait_if_paused(control_args)
        return should_stop(control_args)

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            # 等待域级限速
            if rate_limiter:
                rate_limiter.wait_if_needed(url)

            fetcher = _get_app_fetcher(impersonate, float(timeout), proxy)
            if resume_path is not None:
                # 流式落盘路径：断点续传 / 分块写盘 / max_bytes / 取消钩子全部
                # 由库层 download() 承担（暂停经 _should_abort 每分块生效）。
                fetcher.download(
                    url,
                    resume_path,
                    chunk_size=1024 * 64,
                    resume=True,
                    max_bytes=max_bytes,
                    headers=request_headers,
                    should_abort=_should_abort,
                    on_headers=_on_headers,
                )
                if rate_limiter:
                    rate_limiter.record_request(url)
                return b"", _state.get("content_type", "")

            # 内存路径：整包抓取（页面/robots/sitemap/CSS 等需要完整内容的资源）
            resp = fetcher.get(url, headers=request_headers)
            if resp.status == 304 and allow_not_modified:
                # 条件请求命中：上游确认内容未变，调用方复用上次产物（不写盘）
                _record_response(304, resp.headers)
                if rate_limiter:
                    rate_limiter.record_request(url)
                return b"", ""
            if resp.status >= 400:
                exc = FetchError(f"HTTP Error {resp.status}")
                exc.status = resp.status  # type: ignore[attr-defined]
                if resp.status == 429:
                    _state["retry_after"] = _header_value(resp.headers, "retry-after")
                raise exc
            content = resp.content
            if max_bytes and len(content) > max_bytes:
                raise ValueError(f"file exceeds --max-bytes ({max_bytes})")
            if rate_limiter:
                rate_limiter.record_request(url)
            content_type = _header_value(resp.headers, "content-type")
            if meta is not None:
                meta["status"] = resp.status
                meta["content_type"] = content_type
            _record_response(resp.status, resp.headers)
            return content, content_type

        except FetchError as exc:
            message = str(exc)
            status = getattr(exc, "status", None)
            if allow_not_modified and status == 304:
                # 流式路径（Fetcher.download 只接受 200/206）把 304 当错误上抛：
                # 按条件请求命中处理，不重试、不写盘（.part 保持原样）。
                _record_response(304, None)
                if rate_limiter:
                    rate_limiter.record_request(url)
                return b"", ""
            if "aborted" in message:
                # 用户主动中止（download 的 should_abort 探针触发）
                raise RuntimeError("cancelled by user") from exc
            if "max_bytes" in message:
                # 保留历史 ValueError 语义（max-bytes 超限不重试，直接上抛）
                raise ValueError(f"file exceeds --max-bytes ({max_bytes})") from exc
            if status is not None:
                if status == 429 and rate_limiter:
                    delay = rate_limiter.handle_429(url, _state.get("retry_after"))
                    if delay and attempt < retries:
                        wait_if_paused(control_args)  # 退避期间暂停/取消同样生效
                        time.sleep(min(delay, 30))
                        continue
                last_error = exc
                if attempt < retries and status not in (401, 403, 404):
                    wait_if_paused(control_args)
                    time.sleep(1 + attempt * 2)
            else:
                last_error = exc
                if attempt < retries:
                    wait_if_paused(control_args)
                    time.sleep(1 + attempt * 2)

        except (OSError, _HttpxTransportError) as exc:
            # 网络层错误（URLError/TimeoutError/curl_cffi/httpx 的传输异常）
            last_error = exc
            if attempt < retries:
                wait_if_paused(control_args)
                time.sleep(1 + attempt * 2)

    assert last_error is not None
    raise last_error


# ── JS 渲染抓取（--render-js，库级 DynamicFetcher 桥接）──────────────
# 应用层此前只有静态 HTTP 路径：JS 单页应用渲染不出正文、纯 TLS 伪装也
# 过不了 Cloudflare 类 bot 管理页（2026-11-17 调研实测 julius.ai）。
# 库级 DynamicFetcher（Playwright Chromium + 逐跳导航守卫）一直存在，
# 这里把它桥接进应用层 fetch 管线：页面扫描阶段可选用，资源下载不受影响。

# DynamicFetcher 持有真实浏览器进程，同样不可跨线程并发使用：
# 每个 worker 线程按 (timeout, proxy, headers) 缓存独立实例。
_render_local = threading.local()


def _import_dynamic_fetcher() -> Any:
    """延迟导入库级 ``DynamicFetcher``；不可用时抛 ``FetchError``。"""
    try:
        from web_crawler import DynamicFetcher  # type: ignore[import-not-found]
    except ImportError:
        _src = Path(__file__).resolve().parent.parent / "src"
        if str(_src) not in sys.path:
            sys.path.insert(0, str(_src))
        try:
            from web_crawler import DynamicFetcher  # type: ignore[import-not-found]
        except ImportError:
            raise FetchError(
                "web_crawler.DynamicFetcher is not importable; "
                "install the library with the [all] extra"
            ) from None
    return DynamicFetcher


def _get_render_fetcher(timeout: float, proxy: str | None, headers: dict[str, str]) -> Any:
    """返回与 (timeout, proxy, headers) 匹配的线程本地 ``DynamicFetcher``。

    UA 处理：headers 里的默认爬虫 UA 会被剥掉（浏览器用自己的真实 Chrome
    UA）；用户显式指定的 UA 保留透传。
    """
    cache: dict[tuple[str, str | None, tuple[tuple[str, str], ...]], Any] | None = getattr(
        _render_local, "cache", None
    )
    if cache is None:
        cache = {}
        _render_local.cache = cache
    key = (
        str(timeout),
        proxy,
        tuple(sorted((k, v) for k, v in headers.items() if k.lower() != "user-agent")),
    )
    if key in cache:
        return cache[key]
    fetcher_cls = _import_dynamic_fetcher()
    browser_headers = {k: v for k, v in headers.items() if k.lower() != "user-agent"}
    cache[key] = fetcher_cls(
        timeout=float(timeout),
        proxy=proxy,
        extra_headers=browser_headers or None,
        block_images=True,
        network_idle=True,
    )
    return cache[key]


def _collect_blocked_resources(fetcher: Any) -> None:
    """把本次渲染期间守卫拦截的子资源/WebSocket 追加到线程本地累计列表。"""
    blocked = getattr(fetcher, "last_blocked_resources", None)
    if blocked:
        bucket = getattr(_render_local, "blocked_resources", None)
        if bucket is None:
            bucket = []
            _render_local.blocked_resources = bucket
        bucket.extend(str(item) for item in blocked)
    # 取走即清空：同一 fetcher 会跨多个页面复用，不清理会把上一页的拦截
    # 重复计入下一页（fetch 抛异常时由 render_page 的 finally 兜底）。
    fetcher.last_blocked_resources = []


def drain_blocked_resources() -> list[str]:
    """取出并清空当前线程累计的守卫拦截记录（去重、保持首次出现顺序）。"""
    bucket = getattr(_render_local, "blocked_resources", None)
    if not bucket:
        return []
    _render_local.blocked_resources = []
    return list(dict.fromkeys(bucket))


def render_page(
    url: str,
    timeout: int,
    headers: dict[str, str],
    control_args: argparse.Namespace | None = None,
    meta: dict[str, object] | None = None,
) -> tuple[bytes, str]:
    """用 headless Chromium 渲染 ``url``，返回 ``(body, content_type)``。

    与 :func:`fetch` 同源的安全与控制面：SSRF 入口校验、代理透传、
    4xx 视为失败（FetchError 携带 status，供上层统一分类）。渲染由库级
    ``DynamicFetcher`` 完成——导航逐跳守卫（预检 + route 拦截 + 落点复核）
    在其内部生效。

    ``meta`` 非空时写入成功渲染响应的 ``status`` / ``content_type``。
    """
    if not is_power_mode():
        validate_url_host(url, resolve=True)
    proxy = getattr(control_args, "proxy", None) if control_args else None
    fetcher = _get_render_fetcher(float(timeout), proxy, headers)
    try:
        resp = fetcher.fetch(url)
    finally:
        # 渲染中途抛异常（超时/浏览器崩溃）也要把已发生的拦截记下来：
        # 否则报告里只会看到「页面失败」，看不到同时被门禁挡掉的资源。
        _collect_blocked_resources(fetcher)
    if resp.status >= 400:
        exc = FetchError(f"HTTP Error {resp.status}")
        exc.status = resp.status  # type: ignore[attr-defined]
        raise exc
    content_type = resp.headers.get("content-type") or resp.headers.get("Content-Type") or ""
    if meta is not None:
        meta["status"] = resp.status
        meta["content_type"] = str(content_type)
    return resp.content, str(content_type)


def close_render_fetchers() -> None:
    """关闭当前线程持有的渲染 fetcher（crawl 收尾调用，释放浏览器进程）。"""
    _render_local.blocked_resources = []
    cache: dict[Any, Any] | None = getattr(_render_local, "cache", None)
    if not cache:
        return
    for fetcher in cache.values():
        try:
            fetcher.close()
        except Exception as exc:  # pragma: no cover - 浏览器已崩溃时 best-effort 清理
            _log.warning("failed to close render fetcher: %s", exc)
    cache.clear()


# ── 抓取主函数 ───────────────────────────────────────────────────────

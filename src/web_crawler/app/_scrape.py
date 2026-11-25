"""一次调用返回全部产物（对标 Firecrawl ``POST /v1/scrape``）。

本模块只做「编排 + 结构化错误」，抓取与提取全部复用采集器同源的实现：

- 网络抓取：:func:`web_crawler.app._crawler_core.fetch`（SSRF 入口校验、
  scheme 白名单、大小上限、重试退避与采集器完全一致）；
- 正文/Markdown：:func:`web_crawler.app._report_extract.extract_markdown`
  （与 ``--extract-markdown`` 同一套容器选优 + 语义渲染）；
- 标题 / 逐页元数据：:func:`~web_crawler.app.crawler_net.extract_title` 与
  :func:`~web_crawler.app._crawler_scan._extract_page_meta`（逐页元数据逻辑
  只读复用，不改动）；
- 链接：:class:`~web_crawler.app.crawler_net.PageParser`（与采集器
  ``--crawl-pages`` 入队同一套 ``<a href>`` 提取：``<base>`` 解析、相对地址
  绝对化、非 http(s) 过滤）。

HTTP 层（:mod:`web_crawler.app._ui_http`）负责 Host / CSRF 校验与请求解析；
本模块不落盘、不下载资源，返回纯 JSON 可序列化字典。失败一律抛
:class:`ScrapeError`（携带 HTTP 状态码与机器可读错误码），不向调用方漏出
裸异常。
"""

from __future__ import annotations

import logging
from urllib.parse import urldefrag, urlparse

from web_crawler._ssrf import is_power_mode, validate_url_host
from web_crawler.app import _crawler_core as cr
from web_crawler.app._crawler_scan import _extract_page_meta, _is_page_document
from web_crawler.app.crawler_net import PageParser, decode_text, extract_title
from web_crawler.app.crawler_report import extract_markdown
from web_crawler.exceptions import UnsafeTargetError

_log = logging.getLogger(__name__)

#: 与 Web UI 采集表单的默认值一致（``_ui_helpers.build_args``：timeout=30 /
#: retries=2 / max_bytes=0），保证「同一次请求」在两条通路上行为一致。
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_RETRIES = 2

#: URL 长度上限：超长 URL 几乎只可能是畸形/攻击输入，提前拒绝省一次解析。
MAX_URL_LENGTH = 2048


class ScrapeError(Exception):
    """``/v1/scrape`` 的结构化失败（映射为 JSON + 对应 HTTP 状态码）。

    ``status`` 是返回给客户端的 HTTP 状态码；``code`` 是稳定不变的机器可读
    错误码（便于脚本分支），``message`` 是面向人的说明，``details`` 会原样
    合并进错误 JSON（如上游状态码 / 内容类型）。
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details: dict[str, object] = dict(details or {})


def _validated_url(raw: str) -> str:
    """入参 URL 校验：非空、长度、scheme、主机名与 SSRF host 门禁。

    与 :func:`~web_crawler.app._crawler_core.fetch` 的入口校验同源（同一
    ``validate_url_host``），但先跑一遍是为了把「非法 URL」与「上游抓取失败」
    映射到不同的错误码/状态码，而不是混成 502。
    """
    url = raw.strip()
    if not url:
        raise ScrapeError(400, "missing_url", "url 参数不能为空")
    if len(url) > MAX_URL_LENGTH:
        raise ScrapeError(400, "invalid_url", f"url 长度超过上限 {MAX_URL_LENGTH}")
    url, _fragment = urldefrag(url)
    try:
        parsed = urlparse(url)
    except ValueError as exc:  # 畸形 IPv6 字面量等
        raise ScrapeError(400, "invalid_url", f"无法解析 URL：{exc}") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ScrapeError(400, "invalid_url", "仅支持 http/https URL")
    if not parsed.hostname:
        raise ScrapeError(400, "invalid_url", "URL 缺少主机名")
    if not is_power_mode():
        try:
            validate_url_host(url, resolve=True)
        except UnsafeTargetError as exc:
            raise ScrapeError(400, "blocked_url", "目标指向私网/环回/链路本地地址，已拒绝") from exc
    return url


def _fetch_error(message: str, exc: Exception) -> ScrapeError:
    """把抓取层异常收敛成 502 结构化错误（上游 HTTP 状态码单独透出）。"""
    upstream_status = getattr(exc, "status", None)
    if isinstance(upstream_status, int) and not isinstance(upstream_status, bool):
        return ScrapeError(
            502,
            "upstream_error",
            f"上游返回 HTTP {upstream_status}",
            details={"upstream_status": upstream_status},
        )
    return ScrapeError(502, "fetch_failed", f"{message}：{exc}")


def _decode_html(data: bytes, content_type: str) -> str:
    """按响应声明的 charset 解码；声明非法时退回 UTF-8 替换解码。

    ``content_type`` 里的 charset 由上游控制，未知编码会让
    ``bytes.decode`` 抛 ``LookupError``——不能因此把整次抓取判失败。
    """
    try:
        return decode_text(data, content_type, None)
    except (LookupError, UnicodeDecodeError):
        _log.warning("unknown charset in %r; falling back to utf-8", content_type)
        return data.decode("utf-8", errors="replace")


def _page_links(html: str, page_url: str) -> list[str]:
    """复用采集器的 ``<a href>`` 提取（``<base>``、绝对化、去重保序）。"""
    parser = PageParser(page_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # pragma: no cover - html.parser 对畸形输入的兜底
        _log.warning("link extraction failed for %s: %s", page_url, exc)
        return []
    return list(dict.fromkeys(parser.page_links))


def _render_markdown(html: str, page_url: str) -> str:
    """渲染正文 Markdown；渲染失败降级为空串（页面本身仍算抓取成功）。"""
    try:
        return extract_markdown(html)
    except Exception as exc:  # pragma: no cover - lxml 对畸形 DOM 的兜底
        _log.warning("markdown extraction failed for %s: %s", page_url, exc)
        return ""


def scrape_page(
    url: str,
    *,
    include_html: bool = False,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """抓取单个 URL 并同步返回全部产物（不落盘、不下载子资源）。

    Returns
    -------
    dict
        ``url`` / ``status_code`` / ``content_type`` / ``title`` / ``markdown``
        / ``html``（未请求时为 ``None``）/ ``links`` / ``metadata``
        （``description`` / ``lang``）。

    Raises
    ------
    ScrapeError
        非法 URL（400）、SSRF 拒绝（400）、上游失败（502）、非 HTML（415）。
    """
    target = _validated_url(url)

    response_meta: dict[str, object] = {}
    try:
        data, content_type = cr.fetch(
            target,
            timeout,
            {},
            DEFAULT_RETRIES,
            None,
            meta=response_meta,
        )
    except UnsafeTargetError as exc:  # 重定向落点被 SSRF 守卫拦截
        raise ScrapeError(
            400, "blocked_url", "重定向目标指向私网/环回/链路本地地址，已拒绝"
        ) from exc
    except Exception as exc:
        raise _fetch_error("抓取失败", exc) from exc

    if not _is_page_document(content_type, target):
        # 与采集器同一判定口径：text/html、XML 与 text/plain（部分站点用它
        # 返回 HTML）算文档；application/pdf、image/*、application/json 等
        # 明确是二进制/数据响应，不在「网页抓取」范围内。
        raise ScrapeError(
            415,
            "not_html",
            f"上游返回非 HTML 内容（{content_type or 'unknown'}）",
            details={"content_type": content_type},
        )

    html = _decode_html(data, content_type)
    status_code = response_meta.get("status")
    if not isinstance(status_code, int) or isinstance(status_code, bool):
        status_code = 200  # 抓取成功但上游未回传状态（自定义 fetcher）时的兜底

    if not html.strip():
        description, lang = "", ""
        markdown = ""
        links: list[str] = []
        title = ""
    else:
        title = extract_title(html)
        description, lang = _extract_page_meta(html)
        markdown = _render_markdown(html, target)
        links = _page_links(html, target)

    return {
        "url": target,
        "status_code": status_code,
        "content_type": content_type,
        "title": title,
        "markdown": markdown,
        "html": html if include_html else None,
        "links": links,
        "metadata": {"description": description, "lang": lang},
    }


__all__ = [
    "DEFAULT_RETRIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_URL_LENGTH",
    "ScrapeError",
    "scrape_page",
]

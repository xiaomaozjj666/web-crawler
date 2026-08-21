"""Network / parsing / utility layer of the web resource crawler.

Extracted from :mod:`app.crawler` during the module split. Contains the
HTTP/robots-independent leaf helpers: URL classification and normalization,
HTML page parsing, content deduplication, per-domain rate limiting, output
path computation and CSS resource discovery.

This module never imports ``app.crawler`` (that would be circular); it only
depends on :mod:`app.crawler_models` for the shared data classes. Functions
that must observe patched module globals of ``app.crawler`` (e.g.
``fetch``/``_get_opener``/``discover_sitemap_urls``) intentionally remain in
``app.crawler`` itself.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import logging
import mimetypes
import re
import threading
import time
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urldefrag, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request

from app.crawler_models import ManifestRow, Resource
from web_crawler._ssrf import host_is_unsafe, is_power_mode, validate_url_host

# 与 app.crawler 共用同一个 logger：UI 通过 attach_log_handler 挂到
# "crawler" logger 的 handler 对所有模块日志生效，行为与拆分前一致。
_log = logging.getLogger("crawler")

# app.crawler 用 `from app.crawler_net import *` 同名再导出，保证拆分后
# `app.crawler` 模块的所有属性仍可访问（兼容 cr.xxx 访问与 patch.object(cr, ...)）。
__all__ = [
    "CSS_IMPORT_RE",
    "CSS_URL_RE",
    "HTML_RESOURCE_ATTRS",
    "META_URL_NAMES",
    "PAGE_LINK_ATTRS",
    "RESOURCE_LINK_RELS",
    "VIDEO_CONTENT_HINTS",
    "VIDEO_EXTENSIONS",
    "ContentDedup",
    "DomainRateLimiter",
    "PageParser",
    "SafeRedirectHandler",
    "_is_safe_hostname",
    "category_for",
    "decode_text",
    "discover_css_resources",
    "extract_title",
    "is_blocked_url",
    "is_html",
    "is_power_mode",
    "is_video_candidate",
    "is_video_resource",
    "looks_like_downloadable",
    "looks_like_resource_url",
    "looks_like_url",
    "normalize_url",
    "output_path_for_url",
    "output_prefix_for_resource",
    "parse_block_keywords",
    "parse_headers",
    "parse_srcset",
    "safe_segment",
    "same_domain",
    "unique_resources",
    "validate_url_host",
]

# ── Constants ────────────────────────────────────────────────────────────

HTML_RESOURCE_ATTRS = {
    "img": ("src", "srcset", "data-src", "data-original", "data-lazy-src"),
    "script": ("src",),
    "link": ("href", "imagesrcset"),
    "source": ("src", "srcset"),
    "video": ("src", "poster"),
    "audio": ("src",),
    "track": ("src",),
    "iframe": ("src",),
    "embed": ("src",),
    "object": ("data",),
    "input": ("src",),
    "meta": ("content",),
}
PAGE_LINK_ATTRS = {"a": ("href",), "area": ("href",)}
RESOURCE_LINK_RELS = {
    "apple-touch-icon",
    "apple-touch-icon-precomposed",
    "dns-prefetch",
    "icon",
    "manifest",
    "modulepreload",
    "pingback",
    "preconnect",
    "prefetch",
    "preload",
    "prerender",
    "search",
    "stylesheet",
}
META_URL_NAMES = {
    "msapplication-config",
    "msapplication-tileimage",
    "twitter:image",
    "twitter:image:src",
    "og:audio",
    "og:image",
    "og:video",
}
VIDEO_EXTENSIONS = {
    ".3gp",
    ".ass",
    ".avi",
    ".m3u8",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpd",
    ".srt",
    ".ts",
    ".vtt",
    ".webm",
}
VIDEO_CONTENT_HINTS = (
    "application/dash+xml",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "text/vtt",
    "video/",
)
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*)?(['\"]?)([^'\"\);\s]+)\1\s*\)?",
    re.IGNORECASE,
)


# ── Domain rate limiter ──────────────────────────────────────────────────


class DomainRateLimiter:
    """Per-domain adaptive rate limiter with 429 Retry-After support."""

    def __init__(self, default_delay: float = 0.0):
        self._default_delay = default_delay
        self._last_times: dict[str, float] = {}
        self._lock = threading.Lock()
        self._blocked_until: dict[str, float] = {}

    def wait_if_needed(self, url: str) -> None:
        domain = urlparse(url).netloc.lower()
        with self._lock:
            blocked = self._blocked_until.get(domain, 0.0)
            if blocked > time.time():
                wait_sec = blocked - time.time()
                _log.debug("rate-limited domain %s, waiting %.1fs", domain, wait_sec)
            else:
                wait_sec = 0.0
        if wait_sec > 0:
            time.sleep(min(wait_sec, 120))

        if self._default_delay <= 0:
            return
        with self._lock:
            last = self._last_times.get(domain, 0.0)
            elapsed = time.time() - last
            if elapsed < self._default_delay:
                sleep_for = self._default_delay - elapsed
            else:
                sleep_for = 0.0
        if sleep_for > 0:
            time.sleep(sleep_for)

    def record_request(self, url: str) -> None:
        domain = urlparse(url).netloc.lower()
        with self._lock:
            self._last_times[domain] = time.time()

    def handle_429(self, url: str, retry_after: str | None) -> float | None:
        """Handle 429 response. Returns seconds to wait if caller should retry."""
        domain = urlparse(url).netloc.lower()
        wait = 10  # default
        if retry_after:
            try:
                wait = int(retry_after)
            except ValueError:
                pass
        with self._lock:
            self._blocked_until[domain] = time.time() + wait
        _log.warning("429 on %s, backing off %ds for domain %s", url, wait, domain)
        return wait


# ── Content deduplication ────────────────────────────────────────────────


class ContentDedup:
    """SHA256-based content deduplication registry."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}  # sha256 -> url of first occurrence
        self._lock = threading.Lock()

    def is_duplicate(self, data: bytes, url: str) -> tuple[bool, str]:
        """Returns (is_duplicate, sha256_hex)."""
        sha = hashlib.sha256(data).hexdigest()
        with self._lock:
            existing = self._seen.get(sha)
            if existing and existing != url:
                return True, sha
            if existing is None:
                self._seen[sha] = url
        return False, sha

    def mark_hash_seen(self, sha256: str) -> None:
        """Mark a hash as already seen (used when resuming crawl state)."""
        with self._lock:
            if sha256 not in self._seen:
                self._seen[sha256] = ""

    def seen_hashes(self) -> list[str]:
        """Return list of all seen SHA256 hashes."""
        with self._lock:
            return list(self._seen.keys())

    def seen_count(self) -> int:
        with self._lock:
            return len(self._seen)


# ── HTTP redirect safety (防 SSRF) ───────────────────────────────────────


class SafeRedirectHandler(HTTPRedirectHandler):
    """HTTP 重定向安全校验（防 SSRF）。

    urllib 默认会自动跟随重定向，且对目标地址不做任何校验；恶意页面可以
    用 302 把请求转到内网/回环地址（如云元数据服务 169.254.169.254）。
    此 handler 在每次跳转前重新校验目标 URL 的 scheme 与 hostname。
    """

    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Request | None:
        parsed = urlparse(newurl)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"blocked redirect to non-http(s) URL: {newurl}")
        if not _is_safe_hostname(parsed.hostname):
            raise ValueError(f"blocked redirect to unsafe host: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ── Page HTML parser ─────────────────────────────────────────────────────


class PageParser(HTMLParser):
    def __init__(self, page_url: str) -> None:
        super().__init__()
        self.page_url = page_url
        self.resources: list[Resource] = []
        self.page_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {name.lower(): value for name, value in attrs if value}

        if tag == "base" and attr_map.get("href"):
            self.page_url = urljoin(self.page_url, attr_map["href"])
            return

        for attr in HTML_RESOURCE_ATTRS.get(tag, ()):
            value = attr_map.get(attr)
            if not value:
                continue
            if tag == "link" and attr == "href" and not self._is_resource_link(attr_map):
                continue
            if tag == "meta" and not self._is_resource_meta(attr_map):
                continue
            if attr in {"srcset", "imagesrcset"}:
                for src_url in parse_srcset(value):
                    self._add_resource(src_url, f"{tag}[{attr}]", tag)
            else:
                self._add_resource(value, f"{tag}[{attr}]", tag)

        for attr in PAGE_LINK_ATTRS.get(tag, ()):
            value = attr_map.get(attr)
            if value:
                page_link = normalize_url(urljoin(self.page_url, value))
                if page_link:
                    self.page_links.append(page_link)

    def _is_resource_link(self, attrs: dict[str, str]) -> bool:
        rel_values = {item.lower() for item in attrs.get("rel", "").split()}
        href = attrs.get("href", "")
        return bool(rel_values & RESOURCE_LINK_RELS) or looks_like_resource_url(href)

    def _is_resource_meta(self, attrs: dict[str, str]) -> bool:
        key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or ""
        content = attrs.get("content", "")
        return key.lower() in META_URL_NAMES and looks_like_url(content)

    def _add_resource(self, value: str, found_in: str, kind: str) -> None:
        if not looks_like_downloadable(value):
            return
        absolute = normalize_url(urljoin(self.page_url, value))
        if absolute:
            self.resources.append(Resource(absolute, found_in, kind, self.page_url))


# ── Utility functions ────────────────────────────────────────────────────


def parse_headers(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for value in values:
        if ":" not in value:
            raise ValueError(f"Invalid header format: {value!r}. Use 'Name: value'.")
        name, header_value = value.split(":", 1)
        headers[name.strip()] = header_value.strip()
    return headers


def parse_srcset(value: str) -> Iterable[str]:
    for part in value.split(","):
        candidate = part.strip().split()
        if candidate:
            yield candidate[0]


def normalize_url(url: str) -> str:
    url, _fragment = urldefrag(url.strip())
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    # 防止 SSRF：检查是否为内网/回环地址
    if not _is_safe_hostname(parsed.hostname):
        return ""
    return url


def _is_safe_hostname(hostname: str | None) -> bool:
    """检查 hostname 是否为安全的外部地址（防止 SSRF）。

    静态检查（不做 DNS 解析）：IP 字面量命中私网/环回/链路本地/CGNAT/组播
    等范围，或主机名为 localhost/*.localhost/*.local/已知云元数据名称时返回
    ``False``。与 :mod:`web_crawler._ssrf` 共享同一策略，保证库与 app 行为一致。
    Power Mode（``WEB_CRAWLER_POWER_MODE=1``）下放行 host 校验（仅保留 scheme
    白名单），供可信的个人环境访问内网/云元数据目标。
    """
    if is_power_mode():
        return True
    return not host_is_unsafe(hostname)


def looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://", "//", "/"))


def looks_like_downloadable(value: str) -> bool:
    value = value.strip()
    return not (not value or value.startswith(("data:", "javascript:", "mailto:", "tel:", "#")))


def looks_like_resource_url(url: str) -> bool:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix in {
        ".apng",
        ".avif",
        ".bmp",
        ".css",
        ".gif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".js",
        ".json",
        ".m3u8",
        ".mpd",
        ".map",
        ".mp3",
        ".mp4",
        ".srt",
        ".ts",
        ".ogg",
        ".otf",
        ".pdf",
        ".png",
        ".svg",
        ".ttf",
        ".txt",
        ".wav",
        ".webm",
        ".webmanifest",
        ".webp",
        ".woff",
        ".woff2",
        ".xml",
    }


def is_video_resource(row: ManifestRow) -> bool:
    suffix = Path(urlparse(row.url).path).suffix.lower()
    ct = row.content_type.lower()
    found_in = row.found_in.lower()
    kind = row.kind.lower()
    return (
        suffix in VIDEO_EXTENSIONS
        or any(hint in ct for hint in VIDEO_CONTENT_HINTS)
        or kind in {"video", "audio", "track", "source"}
        or "video" in found_in
        or "poster" in found_in
    )


def category_for(url: str, content_type: str, kind: str, found_in: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    ct = content_type.lower()
    kind_l = kind.lower()
    found_in_l = found_in.lower()
    if suffix in {".m3u8", ".mpd"} or "mpegurl" in ct or "dash+xml" in ct:
        return "playlist"
    if suffix in {".vtt", ".srt", ".ass"} or kind_l == "track" or "text/vtt" in ct:
        return "subtitle"
    if suffix in {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi", ".ts", ".3gp"} or ct.startswith(
        "video/"
    ):
        return "video"
    if "poster" in found_in_l:
        return "poster"
    if suffix in {".mp3", ".wav", ".ogg"} or ct.startswith("audio/"):
        return "audio"
    if suffix in {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".avif",
        ".svg",
        ".ico",
    } or ct.startswith("image/"):
        return "image"
    if suffix == ".css" or "text/css" in ct:
        return "css"
    if suffix == ".js" or "javascript" in ct:
        return "script"
    if suffix in {".woff", ".woff2", ".ttf", ".otf"} or "font/" in ct:
        return "font"
    return "other"


def is_video_candidate(resource: Resource) -> bool:
    suffix = Path(urlparse(resource.url).path).suffix.lower()
    found_in = resource.found_in.lower()
    kind = resource.kind.lower()
    return (
        suffix in VIDEO_EXTENSIONS
        or kind in {"video", "audio", "track", "source"}
        or "video" in found_in
        or "poster" in found_in
    )


def same_domain(url: str, root_url: str) -> bool:
    return urlparse(url).netloc.lower() == urlparse(root_url).netloc.lower()


def parse_block_keywords(values: list[str]) -> list[str]:
    keywords: list[str] = []
    for value in values:
        for part in re.split(r"[\n,]+", value):
            item = part.strip().lower()
            if item:
                keywords.append(item)
    return keywords


def is_blocked_url(url: str, keywords: list[str]) -> bool:
    lowered = url.lower()
    return any(keyword in lowered for keyword in keywords)


# ── Text/parsing functions ────────────────────────────────────────────────


def decode_text(data: bytes, content_type: str, fallback: str | None) -> str:
    encoding = fallback
    if not encoding and "charset=" in content_type.lower():
        encoding = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip()
    return data.decode(encoding or "utf-8", errors="replace")


def extract_title(html_text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return html_lib.unescape(title)[:200]


def output_path_for_url(
    url: str, output_dir: Path, content_type: str, prefix: str = "assets"
) -> Path:
    parsed = urlparse(url)
    host = safe_segment(parsed.netloc or "site")
    raw_path = unquote(parsed.path).strip("/")
    if not raw_path or raw_path.endswith("/"):
        raw_path = f"{raw_path}index"
    parts = [safe_segment(part) for part in raw_path.split("/") if part]
    relative = Path(prefix) / host / Path(*parts)
    if not relative.suffix:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            relative = relative.with_suffix(guessed)
    if parsed.query:
        digest = hashlib.sha1(parsed.query.encode("utf-8")).hexdigest()[:10]
        relative = relative.with_name(f"{relative.stem}_{digest}{relative.suffix}")
    return output_dir / relative


def output_prefix_for_resource(
    args: argparse.Namespace, resource: Resource, content_type: str, page_titles: dict[str, str]
) -> str:
    if not getattr(args, "organize", False):
        return "assets"
    cat = category_for(resource.url, content_type, resource.kind, resource.found_in)
    title = safe_segment(page_titles.get(resource.page_url, "") or "untitled-page")
    return f"assets/{cat}/{title}"


def safe_segment(value: str) -> str:
    value = value.strip() or "unnamed"
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    # 防止路径遍历（过滤 . 和 ..）
    value = re.sub(r"\.\.+", "_", value)
    value = value.strip("._")
    return value[:120]


def discover_css_resources(css_text: str, css_url: str, page_url: str) -> list[Resource]:
    found: list[Resource] = []
    for regex, label in ((CSS_IMPORT_RE, "css[@import]"), (CSS_URL_RE, "css[url()]")):
        for match in regex.finditer(css_text):
            value = match.group(2).strip()
            if not looks_like_downloadable(value):
                continue
            absolute = normalize_url(urljoin(css_url, value))
            if absolute:
                found.append(Resource(absolute, label, "css-url", page_url))
    return found


def unique_resources(resources: Iterable[Resource]) -> list[Resource]:
    seen: set[str] = set()
    result: list[Resource] = []
    for resource in resources:
        if resource.url in seen:
            continue
        seen.add(resource.url)
        result.append(resource)
    return result


def is_html(content_type: str, url: str) -> bool:
    path = urlparse(url).path.lower()
    return (
        "text/html" in content_type
        or path.endswith(("/", ".html", ".htm"))
        or not Path(path).suffix
    )

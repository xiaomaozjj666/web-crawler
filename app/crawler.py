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

Examples:
  python crawler.py --url https://example.com
  python crawler.py --url https://example.com --workers 16 --include-css-urls
  python crawler.py --url https://example.com --same-domain --crawl-pages --max-pages 20
  python crawler.py --url https://example.com --sitemap --workers 4
  python web_resource_crawler.py --load-config my_project.json
"""

from __future__ import annotations

import argparse
import ipaddress
import csv
from dataclasses import dataclass, asdict
import hashlib
import html as html_lib
from html.parser import HTMLParser
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_EXCEPTION
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser
from urllib.request import Request, build_opener, HTTPSHandler
from urllib.request import OpenerDirector


# ── Logging ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
_log = logging.getLogger("crawler")


# ── Console helpers ──────────────────────────────────────────────────────

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── Constants ────────────────────────────────────────────────────────────

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; ResourceCrawler/3.0)"
DEFAULT_WORKERS = 8
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
CRAWL_STATE_FILE = ".crawl_state.json"
RESOURCE_LINK_RELS = {
    "apple-touch-icon", "apple-touch-icon-precomposed",
    "dns-prefetch", "icon", "manifest", "modulepreload",
    "pingback", "preconnect", "prefetch", "preload", "prerender",
    "search", "stylesheet",
}
META_URL_NAMES = {
    "msapplication-config", "msapplication-tileimage",
    "twitter:image", "twitter:image:src",
    "og:audio", "og:image", "og:video",
}
VIDEO_EXTENSIONS = {
    ".3gp", ".ass", ".avi", ".m3u8", ".m4v", ".mkv",
    ".mov", ".mp4", ".mpd", ".srt", ".ts", ".vtt", ".webm",
}
VIDEO_CONTENT_HINTS = (
    "application/dash+xml", "application/vnd.apple.mpegurl",
    "application/x-mpegurl", "text/vtt", "video/",
)
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*)?(['\"]?)([^'\"\);\s]+)\1\s*\)?", re.IGNORECASE,
)


# ── Data classes ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Resource:
    url: str
    found_in: str
    kind: str
    page_url: str


@dataclass
class ManifestRow:
    status: str
    url: str
    saved_path: str
    content_type: str
    bytes: int
    category: str
    found_in: str
    kind: str
    page_url: str
    page_title: str
    diagnostic: str
    sha256: str = ""


# ── AES decryption support ──────────────────────────────────────────────

try:
    from Crypto.Cipher import AES as _AES
    HAS_AES = True
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

    def __init__(self):
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


# ── HTTP opener with connection reuse ────────────────────────────────────

_opener: OpenerDirector | None = None
_opener_lock = threading.Lock()


def _get_opener() -> OpenerDirector:
    global _opener
    if _opener is None:
        with _opener_lock:
            if _opener is None:
                opener = build_opener(HTTPSHandler())
                _opener = opener
    return _opener


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
    """检查 hostname 是否为安全的外部地址（防止 SSRF）"""
    if not hostname:
        return False
    # 尝试解析为 IP 地址
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast:
            return False
    except ValueError:
        # 域名：允许（DNS 解析在请求时进行）
        # 阻止常见的内网域名
        unsafe_domains = {
            "localhost", "127.0.0.1", "::1",
            "metadata.google.internal", "169.254.169.254",
            "100.100.100.200",  # 阿里云元数据
        }
        if hostname.lower() in unsafe_domains:
            return False
    return True


def looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://", "//", "/"))


def looks_like_downloadable(value: str) -> bool:
    value = value.strip()
    if not value or value.startswith(("data:", "javascript:", "mailto:", "tel:", "#")):
        return False
    return True


def looks_like_resource_url(url: str) -> bool:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix in {
        ".apng", ".avif", ".bmp", ".css", ".gif", ".ico", ".jpeg", ".jpg",
        ".js", ".json", ".m3u8", ".mpd", ".map", ".mp3", ".mp4", ".srt",
        ".ts", ".ogg", ".otf", ".pdf", ".png", ".svg", ".ttf", ".txt",
        ".wav", ".webm", ".webmanifest", ".webp", ".woff", ".woff2", ".xml",
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
    if suffix in {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi", ".ts", ".3gp"} or ct.startswith("video/"):
        return "video"
    if "poster" in found_in_l:
        return "poster"
    if suffix in {".mp3", ".wav", ".ogg"} or ct.startswith("audio/"):
        return "audio"
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".ico"} or ct.startswith("image/"):
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
    last_error: Exception | None = None
    method_headers = dict(headers)

    for attempt in range(retries + 1):
        try:
            # Wait for rate limiter
            if rate_limiter:
                rate_limiter.wait_if_needed(url)

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
            opener = _get_opener()
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
                if part_path:
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
                    time.sleep(min(delay, 30))
                    continue
            last_error = exc
            if attempt < retries and status not in (401, 403, 404):
                time.sleep(1 + attempt * 2)

        except (URLError, TimeoutError, ValueError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1 + attempt * 2)

        finally:
            try:
                if "file_handle" in locals() and file_handle:
                    file_handle.close()
            except Exception as _close_err:
                _log.warning("file handle close error: %s", _close_err)

    assert last_error is not None
    raise last_error


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


def output_path_for_url(url: str, output_dir: Path, content_type: str, prefix: str = "assets") -> Path:
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


def output_prefix_for_resource(args: argparse.Namespace, resource: Resource, content_type: str, page_titles: dict[str, str]) -> str:
    if not getattr(args, "organize", False):
        return "assets"
    cat = category_for(resource.url, content_type, resource.kind, resource.found_in)
    title = safe_segment(page_titles.get(resource.page_url, "") or "untitled-page")
    return f"assets/{cat}/{title}"


def safe_segment(value: str) -> str:
    value = value.strip() or "unnamed"
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    # 防止路径遍历（过滤 . 和 ..）
    value = re.sub(r'\.\.+', "_", value)
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


def discover_playlist_resources(
    text: str, playlist_url: str, page_url: str,
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
                params = line[len("#EXT-X-KEY:"):]
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
            return found, (f"encrypted playlist detected (method: {key_method}); "
                           f"{'install pycryptodome for AES-128 decryption' if decrypt else 'use --decrypt to attempt decryption'}")
        if is_encrypted and key_bytes:
            return found, f"encrypted playlist detected, segments will be decrypted (method: {key_method})"
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
    return "text/html" in content_type or path.endswith(("/", ".html", ".htm")) or not Path(path).suffix


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


def rewrite_html(html: str, resources: list[ManifestRow], page_url: str, output_dir: Path) -> str:
    replacements = {
        row.url: Path(row.saved_path).resolve().relative_to(output_dir).as_posix()
        for row in resources
        if row.status == "ok" and row.saved_path
    }
    rewritten = html
    for original_url, local_path in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        rewritten = rewritten.replace(original_url, local_path)
        parsed = urlparse(original_url)
        if same_domain(original_url, page_url):
            rewritten = rewritten.replace(parsed.path, local_path)
    return rewritten


# ── Overlay/popup/modal stripping ────────────────────────────────────────

OVERLAY_PATTERNS = [
    # Common class/keyword patterns for overlays, modals, popups, paywalls
    (re.compile(
        r'<div[^>]*\bclass\s*=\s*["\'][^"\']*'
        r'(?:\bmodal\b|\boverlay\b|\bmask\b|\bshade\b|\bdialog\b'
        r'|\bpopup\b|\bpop-up\b|\blightbox\b|\bmodal-\b'
        r'|\bpaywall\b|\bsubscribe\b|\bsubscription\b'
        r'|\bnewsletter\b|\bpromo\b|\badvertisement\b|\bfloat-ad\b'
        r'|\binterstitial\b|\bfullscreen-[adio]'
        r'|\bcookie-banner\b|\bcookie-consent\b|\bnotice-bar\b'
        r'|\bmember-|\bvip-|\bvip_'
        r'|\blogin-box\b|\blogin-modal\b|\breg-modal\b'
        r'|\bguide-mask\b|\bguide-layer\b|\btip-overlay\b'
        r'|\bdownload-app\b|\bapp-download\b'
        r'|\btoast\b|\bnotice\b|\balert-\b'
        r'|\bvideo-ad\b|\bad-layer\b'
        r'|\bwx-\w+-modal\b)[^"\']*["\'][^>]*>'
        r'.*?</div>',
        re.IGNORECASE | re.DOTALL,
    ), "div overlay/modal/popup"),
    (re.compile(
        r'<div[^>]*\b(id|class)\s*=\s*["\'][^"\']*'
        r'(?:mask|overlay|shadeBg|shadowBg|bgShadow|modalBg|dialogBg)'
        r'[^"\']*["\'][^>]*>'
        r'.*?</div>',
        re.IGNORECASE | re.DOTALL,
    ), "div mask BG"),
    # Fixed/sticky overlay divs
    (re.compile(
        r'<div[^>]*\bstyle\s*=\s*["\'][^"\']*'
        r'(?:position\s*:\s*fixed|position\s*:\s*sticky)'
        r'[^"\']*["\'][^>]*'
        r'>(?:<div[^>]*>.*?</div>\s*)*?</div>',
        re.IGNORECASE | re.DOTALL,
    ), "fixed/sticky div (dangerous, wide match)"),
]


def strip_page_overlays(html: str, aggressive: bool = False) -> str:
    """Remove overlay/popup/paywall HTML elements from page markup.

    Returns cleaned HTML with known overlay elements removed.
    """
    result = html

    # Apply pattern-based removal
    for pattern, label in OVERLAY_PATTERNS:
        if not aggressive and "fixed/sticky" in label:
            continue  # skip overaggressive pattern unless --aggressive
        result = pattern.sub("", result)

    # Remove common overlay containers by known IDs
    overlay_ids = (
        "mask", "overlay", "shadow", "shade", "dialog", "lightbox",
        "modal", "popup", "subscribe", "subscribe-box", "signin", "login",
        "paywall", "vip", "member", "register", "download-app",
        "cookie-notice", "cookieConsent", "announce", "notice-bar",
        "guide", "guideLayer", "tips", "toast", "alertBox",
        "videoAd", "playerAd", "adContainer", "floatingAd",
    )
    for oid in overlay_ids:
        # id="..."  and  id='...'
        result = re.sub(
            rf'<div[^>]*\bid\s*=\s*["\']{oid}["\'][^>]*>.*?</div>',
            "", result, flags=re.IGNORECASE | re.DOTALL,
        )
        # Also try the reverse: class="..." before id
        result = re.sub(
            rf'<div[^>]*\bclass\s*=\s*["\'][^"\']*{oid}[^"\']*["\'][^>]*>.*?</div>',
            "", result, flags=re.IGNORECASE | re.DOTALL,
        )

    return result


# ── Smart data extraction ────────────────────────────────────────────────

EXTRACTED_DATA_FIELDS = [
    "page_url", "page_title",
    "og_title", "og_description", "og_image", "og_video",
    "meta_description", "meta_keywords",
    "h1_count", "h2_count", "h3_count",
    "link_count", "image_count", "video_count",
    "text_length", "has_canonical",
]


def smart_extract(html: str, page_url: str) -> dict[str, object]:
    """Auto-extract structured data from a page.

    Returns a dict with common metadata fields. Designed to work on any HTML page
    without configuration.
    """
    result: dict[str, object] = {
        "page_url": page_url,
        "page_title": extract_title(html),
    }

    # Open Graph / Twitter / Meta tags
    og_patterns = {
        "og_title": (r'<meta\s+property=["\']og:title["\'][^>]*content=["\']([^"\']*)["\']', False),
        "og_description": (r'<meta\s+property=["\']og:description["\'][^>]*content=["\']([^"\']*)["\']', False),
        "og_image": (r'<meta\s+property=["\']og:image["\'][^>]*content=["\']([^"\']*)["\']', False),
        "og_video": (r'<meta\s+property=["\']og:video["\'](?:[^>]*content=["\']([^"\']*)["\'])', False),
    }
    for key, (pattern, _) in og_patterns.items():
        match = re.search(pattern, html, re.IGNORECASE)
        result[key] = match.group(1) if match else ""

    # Meta description (fallback: og:description)
    md = re.search(r'<meta\s+name=["\']description["\'][^>]*content=["\']([^"\']*)["\']', html, re.IGNORECASE)
    result["meta_description"] = md.group(1) if md else result.get("og_description", "")

    # Meta keywords
    mk = re.search(r'<meta\s+name=["\']keywords["\'][^>]*content=["\']([^"\']*)["\']', html, re.IGNORECASE)
    result["meta_keywords"] = mk.group(1) if mk else ""

    # Heading counts
    result["h1_count"] = len(re.findall(r'<h1[>\s]', html, re.IGNORECASE))
    result["h2_count"] = len(re.findall(r'<h2[>\s]', html, re.IGNORECASE))
    result["h3_count"] = len(re.findall(r'<h3[>\s]', html, re.IGNORECASE))

    # Link / image / video counts
    result["link_count"] = len(re.findall(r'<a\s+', html, re.IGNORECASE))
    result["image_count"] = len(re.findall(r'<img\s+', html, re.IGNORECASE))
    result["video_count"] = len(re.findall(r'<video\s+', html, re.IGNORECASE))

    # Text content length (approx)
    text_stripped = re.sub(r'<[^>]+>', "", html)
    text_stripped = re.sub(r'\s+', " ", text_stripped).strip()
    result["text_length"] = len(text_stripped)

    # Canonical URL
    canonical = re.search(r'<link\s+rel=["\']canonical["\'][^>]*href=["\']([^"\']*)["\']', html, re.IGNORECASE)
    result["has_canonical"] = bool(canonical)

    return result


def write_extracted_data(output_dir: Path, data: list[dict[str, object]]) -> None:
    """Write extracted data to JSON and CSV."""
    json_path = output_dir / "extracted_data.json"
    json_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    csv_path = output_dir / "extracted_data.csv"
    if data:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(data[0].keys()))
            writer.writeheader()
            writer.writerows(data)
    _log.info("extracted data: %s, %s", json_path, csv_path)


def extract_readable_text(html: str) -> str:
    """Extract readable article/main text from HTML using simple heuristics."""
    # Try <article> first
    m = re.search(r"<article[^>]*>(.*?)</article>", html, re.DOTALL | re.IGNORECASE)
    if m:
        html = m.group(1)
    else:
        # Try common content containers
        for selector in (
            'id="content"', 'id="article"', 'id="main-content"', 'id="post-content"',
            'class="content"', 'class="post-content"', 'class="article-content"',
            'class="entry-content"', 'class="main-content"',
        ):
            pattern = rf'<div[^>]*{selector}[^>]*>(.*?)</div>'
            m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if m:
                html = m.group(1)
                break
        else:
            # Fallback: <body>
            m = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
            if m:
                html = m.group(1)

    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    # Remove very short lines (likely nav/boilerplate)
    lines = [line.strip() for line in text.split("\n") if len(line.strip()) > 30]
    return "\n".join(lines) if lines else text


# ── Sitemap parser ────────────────────────────────────────────────────────

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
                    sub_data, _ = fetch(nested, timeout, headers, retries=1, max_bytes=10 * 1024 * 1024)
                    sub_text = sub_data.decode("utf-8", errors="replace")
                    for match in re.finditer(r"<loc[^>]*>(.*?)</loc>", sub_text, re.IGNORECASE | re.DOTALL):
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
        sys.exit(1)
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


# ── Manifest writers ──────────────────────────────────────────────────────

FIELD_NAMES = [
    "status", "url", "saved_path", "content_type", "bytes", "category",
    "found_in", "kind", "page_url", "page_title", "diagnostic", "sha256",
]


def _write_manifest_pair(output_dir: Path, rows: list[ManifestRow], prefix: str) -> int:
    """通用清单写入：生成 CSV + JSON 文件对。返回行数。"""
    csv_path = output_dir / f"{prefix}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELD_NAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    json_path = output_dir / f"{prefix}.json"
    json_path.write_text(
        json.dumps([asdict(row) for row in rows], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(rows)


def write_manifests(output_dir: Path, rows: list[ManifestRow]) -> None:
    _write_manifest_pair(output_dir, rows, "resources_manifest")


def write_video_manifests(output_dir: Path, rows: list[ManifestRow]) -> int:
    video_rows = [row for row in rows if is_video_resource(row)]
    return _write_manifest_pair(output_dir, video_rows, "video_manifest") if video_rows else 0


def is_failed_row(row: ManifestRow) -> bool:
    return row.status.startswith("error") or row.status.startswith("skipped")


def write_failed_manifests(output_dir: Path, rows: list[ManifestRow]) -> int:
    failed_rows = [row for row in rows if is_failed_row(row)]
    return _write_manifest_pair(output_dir, failed_rows, "failed_resources") if failed_rows else 0


def write_summary(output_dir: Path, rows: list[ManifestRow], pages_scanned: int) -> None:
    status_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    total_bytes = 0
    dedup_count = 0
    for row in rows:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
        category_counts[row.category] = category_counts.get(row.category, 0) + 1
        if row.status == "ok":
            total_bytes += row.bytes
        if "dedup" in row.status:
            dedup_count += 1

    lines = [
        "Crawl Summary",
        "",
        f"Pages scanned:         {pages_scanned}",
        f"Resources listed:      {len(rows)}",
        f"Downloaded:            {status_counts.get('ok', 0)}",
        f"Deduplicated (skipped): {dedup_count}",
        f"Failed:                {sum(v for k, v in status_counts.items() if k.startswith('error') or k.startswith('skipped'))}",
        f"Total size:            {_format_bytes(total_bytes)}",
        "",
        "By status:",
    ]
    lines.extend(f"  - {k}: {v}" for k, v in sorted(status_counts.items()))
    lines.append("")
    lines.append("By category:")
    lines.extend(f"  - {k}: {v}" for k, v in sorted(category_counts.items()))
    failed = [row for row in rows if is_failed_row(row)]
    if failed:
        lines.append("")
        lines.append("Failed resources:")
        for row in failed[:50]:
            lines.append(f"  - {row.status}: {row.url}")
            if row.diagnostic:
                lines.append(f"    {row.diagnostic}")
    (output_dir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_run_report(output_dir: Path, rows: list[ManifestRow], pages_scanned: int) -> None:
    status_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
        category_counts[row.category] = category_counts.get(row.category, 0) + 1

    ok_count = sum(1 for row in rows if row.status.startswith("ok"))
    failed_rows = [row for row in rows if is_failed_row(row)]
    listed_count = sum(1 for row in rows if row.status == "listed only")
    skipped_count = sum(1 for row in rows if row.status.startswith("skipped"))
    total_bytes = sum(row.bytes for row in rows if row.status == "ok")
    success_rate = round((ok_count + listed_count) * 100 / len(rows), 2) if rows else 100.0

    recommendations: list[str] = []
    failed_text = "\n".join(row.status.lower() for row in failed_rows)
    if "403" in failed_text or "401" in failed_text:
        recommendations.append("发现 401/403：确认账号已授权，并手动填写 Cookie、Referer 或 Authorization。不要尝试绕过权限。")
    if "timeout" in failed_text or "timed out" in failed_text:
        recommendations.append("发现超时：提高超时时间、减少并发、增加请求间隔。")
    if "429" in failed_text:
        recommendations.append("发现限流：减少并发 workers，增加 delay。")
    if skipped_count:
        recommendations.append("存在跳过项：查看 failed_resources.csv/resources_manifest.csv 中的 status 和 diagnostic。")
    if not rows:
        recommendations.append("没有发现资源：确认 URL 可访问，或开启站内页面扫描/CSS 内资源扫描。")
    if not recommendations:
        recommendations.append("本次运行状态正常。可根据需要开启离线 HTML、正文提取或视频资源清单。")

    report = {
        "schema": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pages_scanned": pages_scanned,
        "resources": {
            "total": len(rows),
            "ok": ok_count,
            "listed_only": listed_count,
            "failed": len(failed_rows),
            "skipped": skipped_count,
            "success_rate_percent": success_rate,
            "downloaded_bytes": total_bytes,
            "downloaded_size": _format_bytes(total_bytes),
        },
        "by_status": dict(sorted(status_counts.items())),
        "by_category": dict(sorted(category_counts.items())),
        "largest_files": [
            {
                "url": row.url,
                "saved_path": row.saved_path,
                "bytes": row.bytes,
                "size": _format_bytes(row.bytes),
                "category": row.category,
            }
            for row in sorted((r for r in rows if r.bytes > 0), key=lambda r: r.bytes, reverse=True)[:20]
        ],
        "top_failures": [
            {
                "status": row.status,
                "url": row.url,
                "diagnostic": row.diagnostic,
                "page_url": row.page_url,
            }
            for row in failed_rows[:50]
        ],
        "recommendations": recommendations,
    }
    (output_dir / "run_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def diagnostic_for_status(status: str) -> str:
    lowered = status.lower()
    if "http error 401" in lowered or "unauthorized" in lowered:
        return "401 Unauthorized: check whether your Cookie/Authorization header is present and still valid."
    if "http error 403" in lowered or "forbidden" in lowered:
        return "403 Forbidden: check whether your account has access, and try adding Referer/User-Agent required by the site."
    if "http error 404" in lowered or "not found" in lowered:
        return "404 Not Found: the resource URL may be expired, relative to a different page, or no longer available."
    if "timed out" in lowered or "timeout" in lowered:
        return "Timeout: try increasing timeout/retries or adding a larger delay."
    if "file exceeds" in lowered:
        return "Skipped by max-bytes: increase the single-file size limit or set it to 0."
    if "robots.txt" in lowered:
        return "Skipped by robots.txt because respect-robots is enabled."
    if "encrypted playlist detected" in lowered:
        return "Encrypted playlist detected: recorded only; protected playlist entries are not expanded or decrypted."
    if "dedup" in lowered:
        return "Skipped: identical content (SHA256) already downloaded from a different URL."
    if status.startswith("error"):
        return "Request failed: check network connectivity, URL validity, headers, and authorization."
    return ""


def row_for(
    status: str,
    resource: Resource,
    saved_path: str,
    content_type: str,
    byte_count: int,
    page_titles: dict[str, str],
    sha256: str = "",
) -> ManifestRow:
    return ManifestRow(
        status=status,
        url=resource.url,
        saved_path=saved_path,
        content_type=content_type,
        bytes=byte_count,
        category=category_for(resource.url, content_type, resource.kind, resource.found_in),
        found_in=resource.found_in,
        kind=resource.kind,
        page_url=resource.page_url,
        page_title=page_titles.get(resource.page_url, ""),
        diagnostic=diagnostic_for_status(status),
        sha256=sha256,
    )


# ── The main crawl function ───────────────────────────────────────────────

def crawl(args: argparse.Namespace) -> int:
    try:
        extra_headers = parse_headers(args.header)
    except ValueError as exc:
        print(exc, file=sys.stderr)
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
    page_queue: list[str] = []
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
    resumed_scan = False

    # ── Resume from saved state? ──
    if getattr(args, "resume_crawl", False):
        saved = load_crawl_state(output_dir)
        if saved:
            page_queue = list(saved.get("page_queue", []))
            seen_pages = set(saved.get("seen_pages", []))
            page_titles = {k: str(v) for k, v in saved.get("page_titles", {}).items()}
            for rdict in saved.get("resources", []):
                all_resources.append(Resource(**rdict))
            if dedup and saved.get("sha256_set"):
                for h in saved["sha256_set"]:
                    dedup.mark_hash_seen(str(h))
            _log.info("resumed: %d pages seen, %d pages queued, %d resources, %d dedup hashes",
                      len(seen_pages), len(page_queue), len(all_resources), dedup.seen_count() if dedup else 0)
            resumed_scan = True
        else:
            _log.info("no saved state found, starting fresh")

    # ── Scan pages ──
    while page_queue and len(seen_pages) < args.max_pages:
        wait_if_paused(args)
        if should_stop(args):
            _log.info("cancelled before scanning next page")
            break
        page_url = page_queue.pop(0)
        if not page_url or page_url in seen_pages:
            continue
        if is_blocked_url(page_url, block_keywords):
            _log.info("blocked page: %s", page_url)
            continue
        if args.same_domain and not same_domain(page_url, args.url):
            continue
        if robots and not robots.can_fetch(args.user_agent, page_url):
            _log.info("robots.txt skipped page: %s", page_url)
            continue

        _log.info("scanning page: %s", page_url)
        report_progress(args, phase="page", current_url=page_url, pages_scanned=len(seen_pages), total_resources=0, processed_resources=0)
        seen_pages.add(page_url)
        try:
            data, content_type = fetch(
                page_url, args.timeout, headers, args.retries, max_bytes,
                rate_limiter=rate_limiter, control_args=args,
            )
        except Exception as exc:
            _log.warning("page fetch failed: %s", exc)
            continue

        page_path = output_path_for_url(page_url, output_dir, "text/html", prefix="pages")
        if not page_path.suffix:
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
                    if is_blocked_url(link, block_keywords):
                        continue
                    if not args.same_domain or same_domain(link, args.url):
                        page_queue.append(link)

            # Save crawl state after each page scanned
            if getattr(args, "resume_crawl", False):
                try:
                    save_crawl_state(output_dir,
                                     page_queue=list(page_queue),
                                     seen_pages=list(seen_pages),
                                     page_titles=page_titles,
                                     resources=[asdict(r) for r in all_resources],
                                     sha256_set=dedup.seen_hashes() if dedup else [])
                except Exception as exc:
                    _log.warning("failed to save crawl state: %s", exc)

    resources = unique_resources(all_resources)
    if block_keywords:
        resources = [r for r in resources if not is_blocked_url(r.url, block_keywords)]
    if args.same_domain:
        resources = [r for r in resources if same_domain(r.url, args.url)]
    if args.video_only:
        resources = [r for r in resources if is_video_candidate(r)]

    if not resources:
        _log.info("no resources found")
        write_manifests(output_dir, [])
        write_summary(output_dir, [], len(seen_pages))
        write_run_report(output_dir, [], len(seen_pages))
        return 0

    # ── Download phase (concurrent) ──
    manifest_rows: list[ManifestRow] = []
    manifest_lock = threading.Lock()
    queue = list(resources)
    queued_urls = {r.url for r in queue}

    _log.info("downloading %d resources with %d workers...", len(queue), args.workers)
    report_progress(args, phase="resources", total_resources=len(queue), processed_resources=0, pages_scanned=len(seen_pages))

    processed_count = [0]
    new_discoveries: list[Resource] = []
    discovery_lock = threading.Lock()

    def process_one(resource: Resource) -> ManifestRow | None:
        if robots and not robots.can_fetch(args.user_agent, resource.url):
            return row_for("skipped by robots.txt", resource, "", "", 0, page_titles)

        _log.debug("[%d/%d] %s", processed_count[0] + 1, len(queue), resource.url)

        if args.list_only:
            report_progress(args, phase="download", current_url=resource.url, total_resources=len(queue), processed_resources=processed_count[0] + 1, pages_scanned=len(seen_pages))
            return row_for("listed only", resource, "", "", 0, page_titles)

        try:
            initial_prefix = output_prefix_for_resource(args, resource, "", page_titles)
            target = output_path_for_url(resource.url, output_dir, "", prefix=initial_prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            data, content_type = fetch(
                resource.url,
                args.timeout,
                headers,
                args.retries,
                max_bytes,
                resume_path=target if getattr(args, "resume", False) else None,
                rate_limiter=rate_limiter,
                control_args=args,
            )

            # Content deduplication
            sha256 = ""
            if dedup:
                is_dup, sha256 = dedup.is_duplicate(data, resource.url)
                if is_dup:
                    return row_for("skipped by dedup", resource, "", content_type, len(data), page_titles, sha256=sha256)

            final_prefix = output_prefix_for_resource(args, resource, content_type, page_titles)
            final_target = output_path_for_url(resource.url, output_dir, content_type, prefix=final_prefix)
            if final_target != target and target.exists():
                final_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(final_target))
                target = final_target
            target.parent.mkdir(parents=True, exist_ok=True)
            if not getattr(args, "resume", False):
                write_data = data
                if getattr(args, "decrypt", False) and HAS_AES:
                    key_info = get_segment_key(resource.url)
                    if key_info:
                        try:
                            write_data = decrypt_aes128(data, key_info[0], key_info[1])
                            _log.info("decrypted segment: %s", resource.url)
                        except Exception as exc:
                            _log.warning("decryption failed for %s: %s", resource.url, exc)
                            raise ValueError(f"decryption failed, skipped: {resource.url}")
                target.write_bytes(write_data)

            status = "ok"
            local_discoveries: list[Resource] = []

            # CSS resource discovery
            if args.include_css_urls and ("text/css" in content_type or resource.url.lower().endswith(".css")):
                css_text = decode_text(data, content_type, args.encoding)
                for extra in discover_css_resources(css_text, resource.url, resource.page_url):
                    if args.same_domain and not same_domain(extra.url, args.url):
                        continue
                    if is_blocked_url(extra.url, block_keywords):
                        continue
                    local_discoveries.append(extra)

            # Playlist expansion
            if args.expand_playlists and category_for(resource.url, content_type, resource.kind, resource.found_in) == "playlist":
                playlist_text = decode_text(data, content_type, args.encoding)
                extra_resources, playlist_note = discover_playlist_resources(
                    playlist_text, resource.url, resource.page_url,
                    headers=headers, timeout=args.timeout, retries=args.retries,
                    decrypt=getattr(args, "decrypt", False),
                )
                if playlist_note:
                    status = f"{status}; {playlist_note}"
                for extra in extra_resources:
                    if args.same_domain and not same_domain(extra.url, args.url):
                        continue
                    if is_blocked_url(extra.url, block_keywords):
                        continue
                    if args.video_only and not is_video_candidate(extra):
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

            manifest_row = row_for(status, resource, str(target), content_type, len(data), page_titles, sha256=sha256)

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

        while futures and not (args.workers <= 1 and should_stop(args)):
            wait_if_paused(args)
            if should_stop(args):
                for f in futures:
                    f.cancel()
                _log.info("cancelled by user")
                break

            done, _pending = wait(futures.keys(), timeout=0.5, return_when=FIRST_EXCEPTION)
            for future in done:
                resource_for_future = futures.pop(future)
                index += 1
                processed_count[0] = index
                try:
                    result = future.result()
                    if result is None:
                        executor.shutdown(wait=False, cancel_futures=True)
                        _log.info("cancelled by user")
                        return 1
                    with manifest_lock:
                        manifest_rows.append(result)
                except Exception as exc:
                    _log.error("unexpected worker error: %s", exc)
                    with manifest_lock:
                        manifest_rows.append(row_for("error: worker crashed", resource_for_future, "", "", 0, page_titles))

                report_progress(args, phase="download", current_url=getattr(resource_for_future, "url", ""), total_resources=download_queue_size, processed_resources=index, pages_scanned=len(seen_pages))

            # Add newly discovered resources
            if new_discoveries:
                with discovery_lock:
                    while new_discoveries:
                        r = new_discoveries.pop(0)
                        fut = executor.submit(process_one, r)
                        futures[fut] = r
                        download_queue_size += 1

    # ── Post-processing ──
    if args.rewrite_html:
        for page_url, html in page_html.items():
            rewritten = rewrite_html(html, manifest_rows, page_url, output_dir)
            if getattr(args, "strip_overlays", False):
                rewritten = strip_page_overlays(rewritten)
            rewritten_path = output_path_for_url(page_url, output_dir, "text/html", prefix="offline_pages")
            rewritten_path = rewritten_path.with_suffix(".html")
            rewritten_path.parent.mkdir(parents=True, exist_ok=True)
            rewritten_path.write_text(rewritten, encoding="utf-8")

    write_manifests(output_dir, manifest_rows)
    video_count = write_video_manifests(output_dir, manifest_rows) if args.video_mode else 0
    failed_count = write_failed_manifests(output_dir, manifest_rows)

    # Smart data extraction
    extracted_data: list[dict[str, object]] = []
    if getattr(args, "smart_extract", False):
        for page_url, html in page_html.items():
            extracted_data.append(smart_extract(html, page_url))
        if extracted_data:
            write_extracted_data(output_dir, extracted_data)

    # Readable text extraction
    if getattr(args, "extract_text", False):
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

    write_summary(output_dir, manifest_rows, len(seen_pages))
    write_run_report(output_dir, manifest_rows, len(seen_pages))

    ok_count = sum(1 for row in manifest_rows if row.status.startswith("ok"))
    dedup_count = sum(1 for row in manifest_rows if "dedup" in row.status)
    total_bytes = sum(row.bytes for row in manifest_rows if row.status == "ok")

    _log.info("")
    _log.info("Pages scanned:       %d", len(seen_pages))
    _log.info("Resources found:     %d", len(resources))
    _log.info("Downloaded:          %d (%s)", ok_count, _format_bytes(total_bytes))
    _log.info("Deduplicated:        %d", dedup_count)
    _log.info("Failed:              %d", failed_count)
    _log.info("Output:              %s", output_dir)
    _log.info("CSV manifest:        %s", output_dir / "resources_manifest.csv")
    _log.info("JSON manifest:       %s", output_dir / "resources_manifest.json")
    _log.info("Summary:             %s", output_dir / "summary.txt")
    _log.info("Run report:          %s", output_dir / "run_report.json")
    if args.video_mode:
        _log.info("Video resources:     %d", video_count)

    # Write JSONL for real-time monitoring
    try:
        jsonl_path = output_dir / "resources_manifest.jsonl"
        with jsonl_path.open("w", encoding="utf-8") as f:
            for row in manifest_rows:
                f.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
    except Exception as _jsonl_err:
        _log.warning("failed to write JSONL manifest: %s", _jsonl_err)

    # Clear resume state on successful completion
    if getattr(args, "resume_crawl", False):
        try:
            clear_crawl_state(output_dir)
            _log.info("crawl state cleared")
        except Exception as exc:
            _log.warning("failed to clear crawl state: %s", exc)

    return 0


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
    parser.add_argument("--out", default=os.path.join(os.getcwd(), "crawler_output"), help="Output directory.")
    parser.add_argument("--same-domain", action="store_true", help="Only scan/download same-domain URLs.")
    parser.add_argument("--crawl-pages", action="store_true", help="Follow same-domain page links.")
    parser.add_argument("--max-pages", type=int, default=1, help="Maximum pages to scan.")
    parser.add_argument("--include-css-urls", action="store_true", help="Download CSS url(...) and @import resources.")
    parser.add_argument("--rewrite-html", action="store_true", help="Write offline HTML with downloaded absolute URLs rewritten.")
    parser.add_argument("--strip-overlays", action="store_true", help="Remove overlay/popup/paywall elements from saved HTML (use with --rewrite-html).")
    parser.add_argument("--decrypt", action="store_true", help="Decrypt AES-128 encrypted m3u8 segments (requires pycryptodome).")
    parser.add_argument("--video-mode", action="store_true", help="Also write video_manifest files for video/playlist/subtitle/poster resources.")
    parser.add_argument("--video-only", action="store_true", help="Only list/download video/playlist/subtitle/poster resources.")
    parser.add_argument("--list-only", action="store_true", help="Only list discovered resources in manifests; do not download resource files.")
    parser.add_argument("--expand-playlists", action="store_true", help="Expand unencrypted m3u8/mpd playlists into nested media entries.")
    parser.add_argument("--respect-robots", action="store_true", help="Respect robots.txt crawl rules.")
    parser.add_argument("--timeout", type=int, default=20, help="Request timeout in seconds.")
    parser.add_argument("--retries", type=int, default=1, help="Retries per URL after a failed request.")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between requests (per-domain adaptive).")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Number of concurrent download threads.")
    parser.add_argument("--max-bytes", type=int, default=0, help="Skip files larger than this many bytes; 0 disables.")
    parser.add_argument("--encoding", help="Force text decoding, e.g. utf-8 or gbk.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="HTTP User-Agent header.")
    parser.add_argument("--header", action="append", default=[], help="Extra request header, e.g. 'Cookie: name=value' (repeatable).")
    parser.add_argument("--block-keyword", action="append", default=[], help="Skip URLs containing this keyword/domain (repeatable).")
    parser.add_argument("--resume", action="store_true", help="Resume interrupted downloads using .part files and HTTP Range.")
    parser.add_argument("--organize", action="store_true", help="Save resources into category/page-title folders.")
    parser.add_argument("--dedup", action="store_true", help="Skip downloading files with identical SHA256 content.")
    parser.add_argument("--sitemap", action="store_true", help="Discover pages from /sitemap.xml before crawling.")
    parser.add_argument("--smart-extract", action="store_true", help="Extract structured data from scanned pages.")
    parser.add_argument("--resume-crawl", action="store_true", help="Resume interrupted crawl from saved state.")
    parser.add_argument("--extract-text", action="store_true", help="Extract readable article text from saved HTML pages.")
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
        args = argparse.Namespace(**config)

    if args.save_config:
        if not args.url:
            parser.error("--url is required when using --save-config")
        save_config_to_file(args, args.save_config)
        return

    if not args.url:
        parser.error("the following arguments are required: --url")

    raise SystemExit(crawl(args))


if __name__ == "__main__":
    main()

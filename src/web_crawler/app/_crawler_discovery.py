"""爬虫的辅助资源发现层（robots / sitemap / 播放列表）。

从 :mod:`app.crawler` 拆出的"发现"内聚模块：robots.txt 解析、
sitemap.xml 页面发现、m3u8/mpd 播放列表展开。

本模块内部调用 ``fetch``（网络请求）与 ``HAS_AES``/``register_segment_key``
（AES 密钥注册）——测试对这些名字的 patch 落点在 ``app.crawler`` 模块全局名上
（``patch.object(cr, "fetch", ...)`` 等），因此这里通过 facade 导入
``app.crawler`` 并在运行期按属性访问（``cr.fetch(...)``），保证 patch
语义与拆分前完全一致。

导入时序约定：本模块由 ``app.crawler`` 在其模块顶部导入；仅绑定
``app.crawler`` 模块对象，不在导入期读取其任何属性，循环导入安全。
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from web_crawler.app import _crawler_core as cr
from web_crawler.app.crawler_models import Resource
from web_crawler.app.crawler_net import looks_like_downloadable, normalize_url

# 与 app.crawler 共用同一个 logger：UI 通过 attach_log_handler 挂到
# "crawler" logger 的 handler 对所有模块日志生效，行为与拆分前一致。
_log = logging.getLogger("crawler")


def make_robots_parser(root_url: str, headers: dict[str, str], timeout: int) -> RobotFileParser:
    parsed = urlparse(root_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    robot_parser = RobotFileParser()
    robot_parser.set_url(robots_url)
    try:
        data, _ = cr.fetch(robots_url, timeout, headers, retries=0, max_bytes=1024 * 1024)
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
    """展开 m3u8/mpd 播放列表。返回 (resource_list, diagnostic_message)。"""
    found: list[Resource] = []
    parsed = urlparse(playlist_url)
    is_m3u8 = parsed.path.lower().endswith(".m3u8") or "#EXTM3U" in text
    is_mpd = parsed.path.lower().endswith(".mpd") or "<MPD" in text

    if is_m3u8:
        # 解析 EXT-X-KEY（如存在）
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

        # 加密播放列表尝试拉取解密密钥
        key_bytes: bytes | None = None
        iv_bytes: bytes | None = None
        if is_encrypted and decrypt and key_url and cr.HAS_AES:
            absolute_key_url = normalize_url(urljoin(playlist_url, key_url))
            if absolute_key_url:
                try:
                    hdrs = dict(headers or {})
                    key_data, _ = cr.fetch(absolute_key_url, timeout, hdrs, retries, None)
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
                    cr.register_segment_key(absolute, key_bytes, iv_bytes)

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
    """解析 sitemap.xml 并返回发现的页面 URL。"""
    urls: list[str] = []
    try:
        data, _ = cr.fetch(sitemap_url, timeout, headers, retries=1, max_bytes=10 * 1024 * 1024)
        text = data.decode("utf-8", errors="replace")
        # 标准 sitemap 的 <loc> 元素
        for match in re.finditer(r"<loc[^>]*>(.*?)</loc>", text, re.IGNORECASE | re.DOTALL):
            url = html_lib.unescape(match.group(1).strip())
            normalized = normalize_url(url)
            if normalized:
                urls.append(normalized)
        # sitemap index → 嵌套 sitemap（只展开一层）
        if any(tag in text for tag in ("<sitemapindex", "<sitemap>")):
            nested_sitemaps = urls
            urls = []
            for nested in nested_sitemaps:
                try:
                    sub_data, _ = cr.fetch(
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

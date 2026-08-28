"""爬虫的 CLI 参数解析（``build_parser``）。

从 :mod:`app.crawler` 拆出的命令行解析层；``main()`` 入口保留在
``app.crawler`` 本体（``python -m web_crawler.app.crawler`` 用法不变）。

本模块是叶子模块，仅依赖 :mod:`app._crawler_context` 提供的默认值常量，
不导入 ``app.crawler``。
"""

from __future__ import annotations

import argparse
import os

from web_crawler.app._crawler_context import DEFAULT_USER_AGENT, DEFAULT_WORKERS


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

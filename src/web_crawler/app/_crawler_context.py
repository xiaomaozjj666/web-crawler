"""爬虫的任务模型与配置层。

从 :mod:`app.crawler` 拆出的"任务模型与配置"内聚模块：

- 常量：默认 User-Agent / 并发数 / 抓取状态文件名。
- :class:`_CrawlContext` —— crawl() 各阶段共享的可变状态 dataclass，
  是页面扫描（:mod:`._crawler_scan`）/ 下载执行（:mod:`._crawler_download`）/
  报告后处理（:mod:`._crawler_post`）三个阶段模块之间的契约。
- 配置保存/加载（--save-config / --load-config）。
- 抓取状态持久化（--resume-crawl）。

本模块是叶子模块，绝不导入 ``app.crawler``（否则循环依赖）；
与 :mod:`app.crawler_net` 相同，与 ``app.crawler`` 共用 "crawler" logger，
UI 通过 attach_log_handler 挂到该 logger 的 handler 对所有模块日志生效。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.robotparser import RobotFileParser

from web_crawler.app.crawler_models import Resource
from web_crawler.app.crawler_net import ContentDedup, DomainRateLimiter

# 与 app.crawler 共用同一个 logger：UI 通过 attach_log_handler 挂到
# "crawler" logger 的 handler 对所有模块日志生效，行为与拆分前一致。
_log = logging.getLogger("crawler")

# ── 常量 ──────────────────────────────────────────────────────────────

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; ResourceCrawler/3.0)"
DEFAULT_WORKERS = 8
CRAWL_STATE_FILE = ".crawl_state.json"


# ── 抓取上下文（各阶段共享契约）─────────────────────────────────────


@dataclass
class _CrawlContext:
    """crawl() 各阶段共享的可变状态（页面扫描 / 下载 / 后处理之间传递）。"""

    args: argparse.Namespace
    headers: dict[str, str]
    output_dir: Path
    max_bytes: int | None
    block_keywords: list[str]
    robots: RobotFileParser | None
    rate_limiter: DomainRateLimiter
    dedup: ContentDedup | None
    page_queue: deque[str] = field(default_factory=deque)
    seen_pages: set[str] = field(default_factory=set)
    page_html: dict[str, str] = field(default_factory=dict)
    page_titles: dict[str, str] = field(default_factory=dict)
    all_resources: list[Resource] = field(default_factory=list)
    # 下载阶段状态
    queue: list[Resource] = field(default_factory=list)
    queued_urls: set[str] = field(default_factory=set)
    new_discoveries: list[Resource] = field(default_factory=list)
    processed_count: list[int] = field(default_factory=lambda: [0])
    discovery_lock: threading.Lock = field(default_factory=threading.Lock)
    manifest_lock: threading.Lock = field(default_factory=threading.Lock)
    jsonl_file: Any = None


# ── 配置保存/加载 ──────────────────────────────────────────────────────


def save_config_to_file(args: argparse.Namespace, filepath: str) -> None:
    """把抓取配置保存为 JSON。"""
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
    """从 JSON 文件加载抓取配置并以 dict 返回。"""
    path = Path(filepath)
    if not path.exists():
        _log.error("config file not found: %s", path)
        sys.exit(2)  # 配置错误退出码 2（区别于 1=取消、0=成功）
    config = json.loads(path.read_text(encoding="utf-8"))
    _log.info("config loaded from %s", path)
    return config


# ── 抓取状态持久化（--resume-crawl）──


def save_crawl_state(output_dir: Path, **state: object) -> None:
    """把当前抓取进度保存为 JSON 状态文件。"""
    path = output_dir / CRAWL_STATE_FILE
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def load_crawl_state(output_dir: Path) -> dict:
    """加载已保存的抓取状态（不存在时返回空 dict）。"""
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

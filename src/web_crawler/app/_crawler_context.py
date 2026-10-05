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
from dataclasses import asdict, dataclass, field
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
    # page_queue 的成员索引（enqueue_page / popleft 处同步维护），
    # 避免对 deque 做线性 ``in`` 检查（大爬取下越扫越慢）
    queued_pages: set[str] = field(default_factory=set)
    # 本轮扫描已落盘的页面：page_url -> (页面文件路径, 解码用编码)。
    # 页面 HTML 不再整体驻留内存（万页 × 数百 KB 会把堆撑爆），后处理
    # 阶段按需从磁盘逐页重读（见 _crawler_post._iter_page_html）。
    page_files: dict[str, tuple[Path, str]] = field(default_factory=dict)
    page_titles: dict[str, str] = field(default_factory=dict)
    all_resources: list[Resource] = field(default_factory=list)
    # all_resources 的 dict 形态缓存（只增不减；resource_dicts() 按长度差增量补齐）
    _resource_dicts: list[dict[str, str]] = field(default_factory=list, repr=False)
    # 下载阶段状态
    queue: list[Resource] = field(default_factory=list)
    queued_urls: set[str] = field(default_factory=set)
    new_discoveries: list[Resource] = field(default_factory=list)
    processed_count: list[int] = field(default_factory=lambda: [0])
    # 上次 --resume-crawl 状态快照的保存时刻（monotonic 秒；单元素列表模仿
    # processed_count 的可变持有模式，避免把 dataclass 改成非 Equatable）
    last_state_save: list[float] = field(default_factory=lambda: [0.0])
    discovery_lock: threading.Lock = field(default_factory=threading.Lock)
    manifest_lock: threading.Lock = field(default_factory=threading.Lock)
    jsonl_file: Any = None

    def enqueue_page(self, url: str) -> None:
        """URL 入待扫队列并同步成员索引。"""
        self.page_queue.append(url)
        self.queued_pages.add(url)

    def pop_page(self) -> str:
        """待扫队列出队并同步成员索引。"""
        url = self.page_queue.popleft()
        self.queued_pages.discard(url)
        return url

    def resource_dicts(self) -> list[dict[str, str]]:
        """all_resources 的 dict 形态（增量缓存，供状态快照复用）。

        ``dataclasses.asdict`` 带反射与递归拷贝，每次快照对全部资源重算是
        --resume-crawl 写放大的主要常数。列表只追加、元素不原地修改（唯一
        写点是页面扫描的 extend），按长度差增量补齐即与逐次全量转换等价。
        """
        resources = self.all_resources
        if len(self._resource_dicts) > len(resources):
            # 防御性：列表被异常收缩时重算，保证缓存不脏
            self._resource_dicts.clear()
        if len(self._resource_dicts) < len(resources):
            self._resource_dicts.extend(asdict(r) for r in resources[len(self._resource_dicts) :])
        return self._resource_dicts


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

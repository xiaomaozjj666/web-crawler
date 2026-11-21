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
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.robotparser import RobotFileParser

from web_crawler._cli import PageScanSummary
from web_crawler.app._offline_verify import OfflineCheck
from web_crawler.app.crawler_models import Resource
from web_crawler.app.crawler_net import ContentDedup, DomainRateLimiter
from web_crawler.exceptions import ConfigError
from web_crawler.parser.rules import ExtractionRules, RuleCoverage

# 与 app.crawler 共用同一个 logger：UI 通过 attach_log_handler 挂到
# "crawler" logger 的 handler 对所有模块日志生效，行为与拆分前一致。
_log = logging.getLogger("crawler")

# ── 常量 ──────────────────────────────────────────────────────────────

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; ResourceCrawler/3.0)"
DEFAULT_WORKERS = 8
CRAWL_STATE_FILE = ".crawl_state.json"


def reserve_page_path(path: Path, page_url: str, used_paths: set[str]) -> Path:
    """分配一条本次运行独占的页面路径，兼容大小写不敏感的文件系统。"""
    if str(path).casefold() in used_paths:
        digest = sha256(page_url.encode("utf-8")).hexdigest()[:16]
        base = path.with_name(f"{path.stem[:100]}_{digest}{path.suffix}")
        path = base
        suffix = 1
        while str(path).casefold() in used_paths:
            path = base.with_name(f"{base.stem}_{suffix}{base.suffix}")
            suffix += 1
    used_paths.add(str(path).casefold())
    return path


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
    page_scan: PageScanSummary = field(
        default_factory=lambda: PageScanSummary(
            failed=0, skipped=0, first_error="", blocked_assets=0
        )
    )
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
    resource_dependencies: set[tuple[str, str]] = field(default_factory=set)
    processed_count: list[int] = field(default_factory=lambda: [0])
    # 上次 --resume-crawl 状态快照的保存时刻（monotonic 秒；单元素列表模仿
    # processed_count 的可变持有模式，避免把 dataclass 改成非 Equatable）
    last_state_save: list[float] = field(default_factory=lambda: [0.0])
    discovery_lock: threading.Lock = field(default_factory=threading.Lock)
    manifest_lock: threading.Lock = field(default_factory=threading.Lock)
    jsonl_file: Any = None
    result_committed: bool = False
    offline_page_count: int = 0
    offline_check: OfflineCheck | None = None
    rules: ExtractionRules | None = None
    rule_coverage: RuleCoverage | None = None
    rule_write_errors: int = 0

    def rule_summary(self) -> dict[str, int] | None:
        if self.rule_coverage is None:
            return None
        coverage = self.rule_coverage.report()
        return {
            "pages": coverage["pages_extracted"],
            "pages_missing_required": coverage["pages_missing_required"],
            "errors": coverage["pages_error"] + self.rule_write_errors,
        }

    def enqueue_page(self, url: str) -> None:
        """URL 入待扫队列并同步成员索引。"""
        self.page_queue.append(url)
        self.queued_pages.add(url)

    def pop_page(self) -> str:
        """待扫队列出队并同步成员索引。"""
        url = self.page_queue.popleft()
        self.queued_pages.discard(url)
        return url

    def page_artifact_path(self, page_url: str, directory: str) -> Path:
        """用已分配的页面路径生成正文或离线页面的保存路径。"""
        saved_page, _ = self.page_files[page_url]
        pages_dir = self.output_dir / "pages"
        relative = saved_page.relative_to(
            pages_dir if saved_page.is_relative_to(pages_dir) else self.output_dir
        )
        return self.output_dir / directory / relative

    def resource_dicts(self) -> list[dict[str, str]]:
        """all_resources 的 dict 形态（增量缓存，供状态快照复用）。

        ``dataclasses.asdict`` 带反射与递归拷贝，每次快照对全部资源重算是
        --resume-crawl 写放大的主要常数。列表只追加、元素不原地修改，
        按长度差增量补齐即与逐次全量转换等价。
        """
        resources = self.all_resources
        if len(self._resource_dicts) > len(resources):
            # 防御性：列表被异常收缩时重算，保证缓存不脏
            self._resource_dicts.clear()
        if len(self._resource_dicts) < len(resources):
            self._resource_dicts.extend(asdict(r) for r in resources[len(self._resource_dicts) :])
        return self._resource_dicts


# ── 配置保存/加载 ──────────────────────────────────────────────────────


def load_extraction_rules(args: argparse.Namespace) -> ExtractionRules | None:
    path = getattr(args, "rules", None)
    if path:
        return ExtractionRules.from_file(path)
    embedded = getattr(args, "field_rules", None)
    return ExtractionRules.from_dict(embedded) if embedded is not None else None


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写配置/续跑状态（委托 :func:`app._fsutil.atomic_write_text`）。

    统一走「唯一 tmp + fsync + os.replace」的实现：两份逻辑在补洞时只会改
    一边（例如 Windows 目标被占用时的降级），状态文件与报告必须同一套语义。
    """
    from web_crawler.app._fsutil import atomic_write_text

    atomic_write_text(path, text)


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
        "download_linked_data": args.download_linked_data,
        "resume_crawl": args.resume_crawl,
        "extract_text": args.extract_text,
        "extract_markdown": getattr(args, "extract_markdown", False),
        "include_pattern": args.include_pattern,
        "exclude_pattern": args.exclude_pattern,
        "proxy": args.proxy,
        "stealth": args.stealth,
        "impersonate": args.impersonate,
        "render_js": args.render_js,
    }
    rules = load_extraction_rules(args)
    if rules is not None:
        config["field_rules"] = rules.to_dict()
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(config, ensure_ascii=False, indent=2))
    _log.info("config saved to %s", path)


def load_config_from_file(filepath: str) -> dict:
    """从 JSON 文件加载抓取配置并以 dict 返回。"""
    path = Path(filepath)
    if not path.exists():
        _log.error("config file not found: %s", path)
        sys.exit(2)  # 配置错误退出码 2（区别于 1=取消、0=成功）
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ConfigError("采集配置必须是 JSON 对象")
    _log.info("config loaded from %s", path)
    return config


# ── 抓取状态持久化（--resume-crawl）──


def save_crawl_state(output_dir: Path, **state: object) -> None:
    """把当前抓取进度保存为 JSON 状态文件。"""
    path = output_dir / CRAWL_STATE_FILE
    _atomic_write_text(path, json.dumps(state, ensure_ascii=False))


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

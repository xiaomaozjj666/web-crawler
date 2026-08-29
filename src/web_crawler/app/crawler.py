#!/usr/bin/env python3
"""
合规的网页资源爬虫

下载网页引用的公开或已授权资源；不会尝试绕过付费墙、登录校验、DRM、
签名或其他访问控制。

核心特性:
  - 并发下载（--workers 控制，默认 8 线程）
  - 自适应域级限速（自动处理 429 + Retry-After）
  - 内容 SHA256 去重
  - Sitemap.xml 页面发现
  - 配置保存/加载（--save-config / --load-config）
  - HTTP 连接复用（Keep-Alive）
  - 结构化日志 + JSONL 实时输出

模块结构（为消除巨型单文件拆分，与 mcp/server.py、ui.py 同型）:
  - app.crawler             本模块：crawl 主流程编排 / fetch 网络层（fetch、
                            opener、stealth、AES——被测试 patch 的模块全局名
                            有意留在这里，patch 语义不变）/ CLI 入口 main()
  - app._crawler_context    任务模型与配置（_CrawlContext / 配置与状态持久化）
  - app._crawler_discovery  robots / sitemap / 播放列表发现
  - app._crawler_scan       crawl() 页面扫描阶段
  - app._crawler_download   crawl() 下载执行阶段
  - app._crawler_post       crawl() 报告与后处理阶段
  - app._crawler_cli        CLI 参数解析（build_parser）
  - app.crawler_models      共享数据类 Resource / ManifestRow
  - app.crawler_net         网络/解析/工具层（限速、去重、URL 分类、HTML 解析等）
  - app.crawler_report      报告/格式层（清单、摘要、MD/HTML 报告、HTML 重写、智能抽取等）

阶段子模块通过 facade 导入本模块并按属性访问（``cr.fetch(...)``），
测试对 ``app.crawler`` 全局名（fetch / should_stop / HAS_AES /
clear_crawl_state 等）的 patch 因此对全部阶段生效。

Examples:
  python crawler.py --url https://example.com
  python crawler.py --url https://example.com --workers 16 --include-css-urls
  python crawler.py --url https://example.com --same-domain --crawl-pages --max-pages 20
  python crawler.py --url https://example.com --sitemap --workers 4
  python web_resource_crawler.py --load-config my_project.json
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# 拆分后各层的同名再导出：保证 `app.crawler` 模块的所有属性仍可访问，
# 测试对 cr.xxx 的访问与 patch.object(cr, ...) 不受影响（阶段子模块内部
# 通过 cr.<name> 运行期属性访问本模块全局名，patch 后拿到替换值）。
from web_crawler.app._crawler_cli import build_parser
from web_crawler.app._crawler_context import (  # noqa: F401
    CRAWL_STATE_FILE,
    DEFAULT_USER_AGENT,
    DEFAULT_WORKERS,
    _CrawlContext,
    clear_crawl_state,
    load_config_from_file,
    load_crawl_state,
    save_config_to_file,
    save_crawl_state,
)
from web_crawler.app._crawler_discovery import (  # noqa: F401
    discover_playlist_resources,
    discover_sitemap_urls,
    make_robots_parser,
)
from web_crawler.app._crawler_download import (  # noqa: F401
    _append_jsonl_row,
    _filter_resources,
    _process_resource,
    _run_downloads,
)
from web_crawler.app._crawler_post import (  # noqa: F401
    _close_jsonl,
    _log_crawl_summary,
    _post_pause_check,
    _post_process,
)
from web_crawler.app._crawler_scan import (
    _restore_state,
    _scan_pages,
    _seed_page_queue,
)
from web_crawler.app.crawler_models import ManifestRow, Resource  # noqa: F401
from web_crawler.app.crawler_net import *
from web_crawler.app.crawler_report import *

# ── 日志 ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
# 基础层下沉 _crawler_core:按属性访问保证 patch 全阶段生效;此处 re-export 保持历史导入路径
from web_crawler.app import _crawler_core as core
from web_crawler.app._crawler_core import (  # noqa: F401
    _MAX_REDIRECTS,
    _get_opener,
    _get_stealth_fetcher,
    _import_stealth_fetcher,
    _log,
    _stealth_fetch,
    attach_log_handler,
    decrypt_aes128,
    detach_log_handler,
    fetch,
    get_segment_key,
    register_segment_key,
    report_progress,
    should_stop,
    wait_if_paused,
)


def crawl(args: argparse.Namespace) -> int:
    """应用层爬取主流程：页面扫描 → 资源过滤 → 并发下载 → 后处理与报告。"""
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

    ctx = _CrawlContext(
        args=args,
        headers=headers,
        output_dir=output_dir,
        max_bytes=args.max_bytes if args.max_bytes > 0 else None,
        block_keywords=parse_block_keywords(args.block_keyword),
        robots=make_robots_parser(args.url, headers, args.timeout) if args.respect_robots else None,
        rate_limiter=DomainRateLimiter(default_delay=max(0.0, args.delay)),
        dedup=ContentDedup() if args.dedup else None,
    )

    # ── 页面发现与扫描 ──
    _seed_page_queue(ctx)
    _restore_state(ctx)
    cancelled = _scan_pages(ctx)

    resources = _filter_resources(ctx)

    # 阶段入口守卫：页面扫描期间已取消 → 跳过下载阶段与后处理
    if cancelled or core.should_stop(args):
        _log.info("crawl cancelled; skipping download phase")
        return 1

    if not resources:
        _log.info("no resources found")
        write_manifests(output_dir, [])
        write_summary(
            output_dir,
            [],
            len(ctx.seen_pages),
            start_time=crawl_start_time,
            end_time=time.time(),
            config=report_config,
        )
        write_run_report(
            output_dir,
            [],
            len(ctx.seen_pages),
            start_time=crawl_start_time,
            end_time=time.time(),
            config=report_config,
        )
        return 0

    # ── 下载阶段（并发）──
    ctx.queue = list(resources)
    ctx.queued_urls = {r.url for r in ctx.queue}

    # JSONL 实时清单：每下载完成一个资源立即追加一行（供监控/断点查看），
    # 打开失败（磁盘满等）降级为仅内存收集，不影响抓取
    jsonl_path = output_dir / "resources_manifest.jsonl"
    try:
        ctx.jsonl_file = jsonl_path.open("w", encoding="utf-8")
    except OSError as exc:
        _log.warning("failed to open JSONL manifest: %s", exc)

    manifest_rows: list[ManifestRow] = []
    cancelled = _run_downloads(ctx, manifest_rows) or cancelled

    # ── 后处理 ──
    video_count, failed_count = _post_process(
        ctx, manifest_rows, crawl_start_time, report_config, cancelled=cancelled
    )
    _log_crawl_summary(
        ctx, manifest_rows, resources, crawl_start_time, cancelled, video_count, failed_count
    )

    # 统一退出码：0 成功 / 1 用户取消 / 2 参数错误
    return 1 if (cancelled or core.should_stop(args)) else 0


# ── 入口 ───────────────────────────────────────────────────────────


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # 配置保存/加载
    if args.load_config:
        config = load_config_from_file(args.load_config)
        # CLI 显式提供的参数覆盖配置文件
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

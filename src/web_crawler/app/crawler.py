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
from web_crawler._cli import (
    ProgressReporter,
    configure_logging,
    crawl_summary,
    emit_crawl_summary,
    emit_error,
    emit_json,
    output_mode,
)
from web_crawler.app._crawler_cache import ValidatorCache, cache_path
from web_crawler.app._crawler_cli import build_parser
from web_crawler.app._crawler_context import (  # noqa: F401
    CRAWL_STATE_FILE,
    DEFAULT_USER_AGENT,
    DEFAULT_WORKERS,
    _CrawlContext,
    clear_crawl_state,
    load_config_from_file,
    load_crawl_state,
    load_extraction_rules,
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
from web_crawler.app._offline import OFFLINE_INDEX
from web_crawler.app._offline_verify import format_offline_check, verify_offline
from web_crawler.app.crawler_models import ManifestRow, Resource, ResourceCrawlResult
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
    _get_app_fetcher,
    _import_lib_fetcher,
    _log,
    _stealth_user_agent,
    attach_log_handler,
    close_render_fetchers,
    decrypt_aes128,
    detach_log_handler,
    fetch,
    get_segment_key,
    register_segment_key,
    render_page,
    report_progress,
    should_stop,
    wait_if_paused,
)


def crawl(args: argparse.Namespace) -> int:
    """为爬取主流程安装统一进度回调，并保证动态行与外部回调正确收尾。"""
    reporter = ProgressReporter(output_mode(args), stream=getattr(args, "output_stream", None))
    had_callback = hasattr(args, "progress_callback")
    previous_callback = getattr(args, "progress_callback", None)

    def forward_progress(payload: dict[str, object]) -> None:
        reporter.update(payload)
        if previous_callback:
            previous_callback(payload)

    args.progress_callback = forward_progress
    try:
        return _crawl(args)
    finally:
        # --render-js 的渲染 fetcher 持有浏览器进程：无论成功/取消/异常都释放，
        # 避免长驻会话泄漏（UI 反复起任务时会累积 Chromium 实例）。
        close_render_fetchers()
        reporter.close()
        if had_callback:
            args.progress_callback = previous_callback
        else:
            del args.progress_callback


def _emit_summary(
    args: argparse.Namespace,
    ctx: _CrawlContext,
    resources: list[Resource],
    rows: list[ManifestRow],
    started_at: float,
    *,
    cancelled: bool = False,
) -> int:
    summary = crawl_summary(
        pages=len(ctx.page_titles),
        resources=len(ctx.queue) or len(resources),
        rows=rows,
        started_at=started_at,
        output_dir=str(ctx.output_dir),
        cancelled=cancelled,
        page_scan=ctx.page_scan,
        rule_results=ctx.rule_summary(),
    )
    if ctx.offline_page_count:
        summary["offline_pages"] = {"count": ctx.offline_page_count, "index": OFFLINE_INDEX}
    if ctx.offline_check is not None:
        summary["offline_check"] = ctx.offline_check
    on_result = getattr(args, "result_callback", None)
    if on_result is not None:
        on_result(ResourceCrawlResult(summary, rows, ctx.all_resources, ctx.resource_dependencies))
    emit_crawl_summary(summary, args, stream=getattr(args, "output_stream", None))
    return {"cancelled": 1, "error": 3}.get(summary["status"], 0)


def _redact_proxy(value: object) -> object:
    """报告 config 中的代理 URL 脱敏（``user:pass@host`` → ``***@host``）。

    run_report.json 等报告默认会外发/归档；代理 URL 常内嵌账号密码，
    原样写入等于明文泄密。仅保留主机与端口用于事后审计。
    """
    text = str(value or "")
    scheme, sep, rest = text.partition("://")
    if not sep or "@" not in rest:
        return value
    return f"{scheme}://***@{rest.rsplit('@', 1)[1]}"


def _crawl(args: argparse.Namespace) -> int:
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
        "extract_markdown": getattr(args, "extract_markdown", ""),
        "download_linked_data": getattr(args, "download_linked_data", ""),
        "strip_overlays": getattr(args, "strip_overlays", ""),
        "decrypt": getattr(args, "decrypt", ""),
        "sitemap": getattr(args, "sitemap", ""),
        "crawl_pages": getattr(args, "crawl_pages", ""),
        "resume_crawl": getattr(args, "resume_crawl", ""),
        "cache": getattr(args, "cache", ""),
        "max_pages": getattr(args, "max_pages", ""),
        "max_bytes": getattr(args, "max_bytes", ""),
        # 反爬相关开关进报告 config：事后才能区分普通 / stealth / 渲染运行
        # （2026-11 调研实测：两批 run_report 因缺 stealth 字段无法对照审计）
        "stealth": getattr(args, "stealth", ""),
        "impersonate": getattr(args, "impersonate", ""),
        "render_js": getattr(args, "render_js", ""),
        # 代理凭据脱敏：报告会外发，host/端口足够审计
        "proxy": _redact_proxy(getattr(args, "proxy", "")),
    }
    try:
        extra_headers = parse_headers(args.header)
    except ValueError as exc:
        emit_error(exc, args, context="--header", stream=getattr(args, "output_stream", None))
        return 2
    try:
        rules = load_extraction_rules(args)
    except (OSError, ValueError) as exc:
        emit_error(exc, args, context="--rules", stream=getattr(args, "output_stream", None))
        return 2

    headers = {"User-Agent": args.user_agent, **extra_headers}
    output_dir = Path(args.out).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # --cache：条件请求校验器表（损坏/旧格式在 load 内降级为空表）。
    # 未开启时保持 None，下载层走完全不变的老路径。
    validator_cache = ValidatorCache.load(output_dir) if getattr(args, "cache", False) else None
    try:
        root_url = normalize_url(args.url)
    except ValueError as exc:
        _log.error("%s", exc)
        root_url = ""

    ctx = _CrawlContext(
        args=args,
        headers=headers,
        output_dir=output_dir,
        max_bytes=args.max_bytes if args.max_bytes > 0 else None,
        block_keywords=parse_block_keywords(args.block_keyword),
        robots=(
            make_robots_parser(root_url, headers, args.timeout, control_args=args)
            if root_url and args.respect_robots
            else None
        ),
        rate_limiter=DomainRateLimiter(default_delay=max(0.0, args.delay)),
        dedup=ContentDedup() if args.dedup else None,
        rules=rules,
        rule_coverage=rules.coverage() if rules is not None else None,
        validator_cache=validator_cache,
    )

    # ── 页面发现与扫描 ──
    cancelled = False
    if root_url:
        ctx.seed_url = root_url
        _seed_page_queue(ctx, root_url)
        _restore_state(ctx)
        cancelled = _scan_pages(ctx)
    else:
        ctx.page_scan["failed"] = 1
        ctx.page_scan["first_error"] = "无法采集起始 URL，请检查 URL 格式和目标访问限制。"
        _log.error("%s", ctx.page_scan["first_error"])

    resources = _filter_resources(ctx)

    # 阶段入口守卫：页面扫描期间已取消 → 跳过下载阶段与后处理
    if cancelled or core.should_stop(args):
        _log.info("crawl cancelled; skipping download phase")
        return _emit_summary(args, ctx, resources, [], crawl_start_time, cancelled=True)

    if not resources:
        _log.info("no resources found")

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
    if resources:
        cancelled = _run_downloads(ctx, manifest_rows)

    # --cache：校验器落盘（取消时也保存已完成条目；无变更不写文件）。
    # 与 --resume-crawl 的状态文件是两个文件，互不覆盖。
    if validator_cache is not None:
        try:
            if validator_cache.save(output_dir):
                _log.info("validator cache saved: %s", cache_path(output_dir))
        except OSError as exc:
            _log.warning("failed to save validator cache: %s", exc)

    # ── 后处理 ──
    video_count, failed_count = _post_process(
        ctx, manifest_rows, crawl_start_time, report_config, cancelled=cancelled
    )
    _log_crawl_summary(ctx, manifest_rows, resources, crawl_start_time, video_count, failed_count)

    # 统一退出码：0 已完成 / 1 用户取消 / 2 参数错误 / 3 页面全部失败
    is_cancelled = cancelled or (not ctx.result_committed and core.should_stop(args))
    return _emit_summary(
        args, ctx, resources, manifest_rows, crawl_start_time, cancelled=is_cancelled
    )


# ── 入口 ───────────────────────────────────────────────────────────


def main() -> None:
    parser = build_parser()
    try:
        args = parser.parse_args()
    except SystemExit as exc:
        # 参数错误（退出码 2）在 --json 模式下也要给机器可解析的文档：
        # 此前 argparse 只往 stderr 打印用法，stdout 是 0 字节，--json 的契约被打破。
        if exc.code == 2 and "--json" in sys.argv[1:]:
            emit_error(
                ValueError("命令行参数无效，请检查 stderr 中的用法提示"),
                argparse.Namespace(json_output=True),
                context="--args",
            )
        raise
    configure_logging(args)

    if args.verify_offline:
        if any((args.url, args.load_config, args.save_config, args.sample, args.rules)):
            parser.error("--verify-offline is a standalone check")
        try:
            check = verify_offline(Path(args.verify_offline))
        except (OSError, RuntimeError, ValueError) as exc:
            emit_error(exc, args, context="--verify-offline")
            raise SystemExit(2) from exc
        if output_mode(args) == "json":
            emit_json(check)
        else:
            print(format_offline_check(check))
            for example in check["examples"]:
                print(f"  {example['source']} · {example['detail']}")
        raise SystemExit(0 if check["status"] == "passed" else 1)

    # 配置保存/加载
    if args.load_config:
        if not Path(args.load_config).is_file():
            emit_error(
                ValueError(f"配置文件不存在：{args.load_config}"),
                args,
                context=args.load_config,
            )
            raise SystemExit(2)
        try:
            config = load_config_from_file(args.load_config)
        except (OSError, ValueError) as exc:
            emit_error(exc, args, context=args.load_config)
            raise SystemExit(2) from exc
        # 显式参数即使恰好等于默认值，也要覆盖文件中的配置。
        defaults = vars(args).copy()
        allowed = defaults.keys() | {"field_rules"}
        allowed -= {"load_config", "save_config", "rules", "sample", "verify_offline"}
        defaults.update({key: value for key, value in config.items() if key in allowed})
        parser.set_defaults(**dict.fromkeys(vars(args)))
        defaults.update(
            {key: value for key, value in vars(parser.parse_args()).items() if value is not None}
        )
        args = argparse.Namespace(**defaults)

    if args.sample:
        try:
            rules = load_extraction_rules(args)
            if rules is None:
                raise ValueError("--sample 需要 --rules 或含字段规则的采集配置")
            html = Path(args.sample).read_text(encoding=args.encoding or "utf-8-sig")
            result = rules.extract(html, page_url=args.url or "")
        except (OSError, ValueError, LookupError) as exc:
            emit_error(exc, args, context="--sample")
            raise SystemExit(2) from exc
        emit_json(result)
        return

    if args.save_config:
        if not args.url:
            parser.error("--url is required when using --save-config")
        try:
            save_config_to_file(args, args.save_config)
        except (OSError, ValueError) as exc:
            emit_error(exc, args, context=args.save_config)
            raise SystemExit(2) from exc
        if output_mode(args) == "json":
            emit_json(
                {
                    "status": "success",
                    "command": "web-crawler",
                    "action": "save-config",
                    "path": str(Path(args.save_config).resolve()),
                }
            )
        return

    if not args.url:
        parser.error("the following arguments are required: --url")

    raise SystemExit(crawl(args))


if __name__ == "__main__":  # pragma: no cover
    main()

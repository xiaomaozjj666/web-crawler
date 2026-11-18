"""爬虫的页面扫描阶段（种子入队 / 状态恢复 / 逐页扫描）。

从 :mod:`app.crawler` 拆出的 crawl() 前段阶段函数：
``_seed_page_queue`` / ``_restore_state`` / ``_scan_pages``。

本模块通过 facade 导入 ``app.crawler`` 并在运行期按属性访问被测试
patch 的模块全局名（``cr.fetch`` / ``cr.should_stop`` /
``cr.report_progress`` / ``cr.save_crawl_state`` 等），保证 patch
语义与拆分前完全一致。

导入时序约定：本模块由 ``app.crawler`` 在其模块顶部导入；仅绑定
``app.crawler`` 模块对象，不在导入期读取其任何属性，循环导入安全。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from urllib.parse import urlparse

from web_crawler._cli import render_error
from web_crawler.app import _crawler_context as ctx_mod
from web_crawler.app import _crawler_core as cr
from web_crawler.app._crawler_context import _CrawlContext
from web_crawler.app._crawler_discovery import discover_sitemap_urls
from web_crawler.app.crawler_models import Resource
from web_crawler.app.crawler_net import (
    PageParser,
    decode_text,
    extract_title,
    is_blocked_url,
    output_path_for_url,
    same_domain,
)

# 与 app.crawler 共用同一个 logger：UI 通过 attach_log_handler 挂到
# "crawler" logger 的 handler 对所有模块日志生效，行为与拆分前一致。
_log = logging.getLogger("crawler")

# --resume-crawl 状态快照的最小保存间隔（秒）。状态体随爬取规模线性增长，
# 固定"每 5 页全量重写一次"会造成 O(P²) 写放大——大爬取后期每 5 页卡顿数秒。
# 除队列耗尽的最终保存外，两次保存至少间隔该时长，成本占比保持有界。
_STATE_SAVE_MIN_INTERVAL_SECONDS = 2.0


def _resolved_encoding(fallback: str | None, content_type: str) -> str:
    """复现 :func:`decode_text` 的编码选择，供后处理从磁盘重读时使用。"""
    if fallback:
        return fallback
    lowered = content_type.lower()
    if "charset=" in lowered:
        charset = lowered.split("charset=", 1)[1].split(";", 1)[0].strip()
        if charset:
            return charset
    return "utf-8"


def _seed_page_queue(ctx: _CrawlContext, root_url: str) -> None:
    """种子 URL 入队并按 --sitemap 做站点地图页面发现。"""
    args = ctx.args
    ctx.enqueue_page(root_url)

    if args.sitemap:
        parsed = urlparse(root_url)
        sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
        _log.info("discovering pages from %s", sitemap_url)
        sitemap_urls = discover_sitemap_urls(sitemap_url, ctx.headers, args.timeout)
        for su in sitemap_urls:
            if su not in ctx.seen_pages and su not in ctx.queued_pages:
                if args.same_domain and not same_domain(su, args.url):
                    continue
                if is_blocked_url(su, ctx.block_keywords):
                    continue
                ctx.enqueue_page(su)


def _restore_state(ctx: _CrawlContext) -> None:
    """--resume-crawl：从上次保存的状态恢复队列/页面/资源/去重哈希。"""
    args = ctx.args
    if not getattr(args, "resume_crawl", False):
        return
    saved = ctx_mod.load_crawl_state(ctx.output_dir)
    if not saved:
        _log.info("no saved state found, starting fresh")
        return
    ctx.page_queue = deque(saved.get("page_queue", []))
    ctx.queued_pages = set(ctx.page_queue)
    ctx.seen_pages = set(saved.get("seen_pages", []))
    ctx.page_titles = {k: str(v) for k, v in saved.get("page_titles", {}).items()}
    saved_files = saved.get("page_files", {})
    if isinstance(saved_files, dict):
        for url, entry in saved_files.items():
            if (
                url in ctx.page_titles
                and isinstance(entry, list)
                and len(entry) == 2
                and all(isinstance(value, str) for value in entry)
            ):
                try:
                    path = (ctx.output_dir / entry[0]).resolve()
                except (OSError, ValueError, RuntimeError):
                    continue
                if path.is_relative_to(ctx.output_dir / "pages"):
                    ctx.page_files[url] = (path, entry[1])
    stats = saved.get("page_scan")
    if isinstance(stats, dict):
        failed = stats.get("failed")
        skipped = stats.get("skipped")
        first_error = stats.get("first_error")
        blocked_assets = stats.get("blocked_assets")
        ctx.page_scan = {
            "failed": max(0, failed) if isinstance(failed, int) else 0,
            "skipped": max(0, skipped) if isinstance(skipped, int) else 0,
            "first_error": first_error if isinstance(first_error, str) else "",
            "blocked_assets": max(0, blocked_assets) if isinstance(blocked_assets, int) else 0,
        }
    for rdict in saved.get("resources", []):
        ctx.all_resources.append(Resource(**rdict))
    if ctx.dedup and saved.get("sha256_set"):
        for h in saved["sha256_set"]:
            ctx.dedup.mark_hash_seen(str(h))
    _log.info(
        "resumed: %d pages seen, %d pages queued, %d resources, %d dedup hashes",
        len(ctx.seen_pages),
        len(ctx.page_queue),
        len(ctx.all_resources),
        ctx.dedup.seen_count() if ctx.dedup else 0,
    )


def _fetch_page_html(ctx: _CrawlContext, page_url: str) -> tuple[bytes, str]:
    """``--render-js`` 路径：headless Chromium 渲染页面，失败退回静态 HTTP。

    渲染走库级 ``DynamicFetcher``（导航逐跳守卫 / 代理 / SSRF 校验在库层
    生效）；渲染失败（浏览器崩溃、超时、目标站对 headless 报 4xx 等）不
    终止扫描，退回 :func:`cr.fetch` 的静态路径，让错误统一走既有分类。
    域级限速两条路径共用同一个 limiter。
    """
    args = ctx.args
    try:
        ctx.rate_limiter.wait_if_needed(page_url)
        data, content_type = cr.render_page(page_url, args.timeout, ctx.headers, control_args=args)
        ctx.rate_limiter.record_request(page_url)
        blocked = cr.drain_blocked_resources()
        if blocked:
            ctx.page_scan["blocked_assets"] += len(blocked)
            _log.warning(
                "%d subresource(s) blocked by navigation guard for %s: %s",
                len(blocked),
                page_url,
                ", ".join(blocked[:3]),
            )
        if ctx.max_bytes and len(data) > ctx.max_bytes:
            raise ValueError(f"file exceeds --max-bytes ({ctx.max_bytes})")
        return data, content_type
    except Exception as exc:
        # 渲染中途失败也把已累计的拦截记进报告（否则「部分资源没下载」无迹可查）
        ctx.page_scan["blocked_assets"] += len(cr.drain_blocked_resources())
        if isinstance(exc, RuntimeError) and str(exc) == "cancelled by user":
            raise
        _log.warning("JS render failed for %s: %s; falling back to HTTP fetch", page_url, exc)
        return cr.fetch(
            page_url,
            args.timeout,
            ctx.headers,
            args.retries,
            ctx.max_bytes,
            rate_limiter=ctx.rate_limiter,
            control_args=args,
        )


def _scan_pages(ctx: _CrawlContext) -> bool:
    """逐页扫描：抓取页面、解析资源、按 --crawl-pages 扩展待扫队列。

    返回是否被用户取消（取消后调用方应跳过下载与后处理）。
    """
    args = ctx.args
    cancelled = False  # 统一取消标志：任何阶段触发取消后跳过后续耗时步骤并返回 1
    saved_paths = {str(path).casefold() for path, _ in ctx.page_files.values()}
    while ctx.page_queue and len(ctx.seen_pages) < args.max_pages:
        try:
            cr.wait_if_paused(args)
        except RuntimeError:
            # 暂停期间被取消 → wait_for_resume 抛 cancelled,按取消处理而非任务出错
            _log.info("cancelled by user")
            cancelled = True
            break
        if cr.should_stop(args):
            _log.info("cancelled before scanning next page")
            cancelled = True
            break
        page_url = ctx.pop_page()
        if not page_url or page_url in ctx.seen_pages:  # pragma: no cover - 防御性：队列已预过滤
            continue
        if is_blocked_url(page_url, ctx.block_keywords):
            ctx.page_scan["skipped"] += 1
            _log.info("blocked page: %s", page_url)
            continue
        if args.same_domain and not same_domain(
            page_url, args.url
        ):  # pragma: no cover - 防御性：入队时已过滤
            ctx.page_scan["skipped"] += 1
            continue
        if ctx.robots and not ctx.robots.can_fetch(
            args.user_agent, page_url
        ):  # pragma: no cover - 防御性：页面 robots 检查
            ctx.page_scan["skipped"] += 1
            _log.info("robots.txt skipped page: %s", page_url)
            continue

        _log.info("scanning page: %s", page_url)
        cr.report_progress(
            args,
            phase="page",
            current_url=page_url,
            pages_scanned=len(ctx.page_titles),
            total_pages=args.max_pages,
            total_resources=0,
            processed_resources=0,
        )
        ctx.seen_pages.add(page_url)
        try:
            if getattr(args, "render_js", False):
                data, content_type = _fetch_page_html(ctx, page_url)
            else:
                data, content_type = cr.fetch(
                    page_url,
                    args.timeout,
                    ctx.headers,
                    args.retries,
                    ctx.max_bytes,
                    rate_limiter=ctx.rate_limiter,
                    control_args=args,
                )
        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc) == "cancelled by user":
                return True
            diagnostic = render_error(exc, context=page_url)
            _log.error("%s", diagnostic)
            ctx.page_scan["failed"] += 1
            if not ctx.page_scan["first_error"]:
                ctx.page_scan["first_error"] = diagnostic[:4000]
            continue

        page_path = output_path_for_url(page_url, ctx.output_dir, "text/html", prefix="pages")
        if not page_path.suffix:  # pragma: no cover - text/html 总是返回带后缀的路径
            page_path = page_path.with_suffix(".html")
        page_path = ctx_mod.reserve_page_path(page_path, page_url, saved_paths)
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_bytes(data)

        html = decode_text(data, content_type, args.encoding)
        ctx.page_files[page_url] = (page_path, _resolved_encoding(args.encoding, content_type))
        ctx.page_titles[page_url] = extract_title(html)
        parser = PageParser(
            page_url,
            download_linked_data=bool(getattr(args, "download_linked_data", False)),
        )
        parser.feed(html)
        ctx.all_resources.extend(parser.resources)

        if args.crawl_pages:
            for link in parser.page_links:
                if link not in ctx.seen_pages and link not in ctx.queued_pages:
                    if is_blocked_url(
                        link, ctx.block_keywords
                    ):  # pragma: no cover - 防御性：资源级 block 检查已在更上层处理
                        continue
                    if not args.same_domain or same_domain(link, args.url):
                        ctx.enqueue_page(link)

            # 状态保存节流：每 5 页或队列耗尽时尝试保存；状态体大时再叠加
            # 最小时间间隔（见 _STATE_SAVE_MIN_INTERVAL_SECONDS），防止 O(P²) 写放大
            if getattr(args, "resume_crawl", False) and (
                len(ctx.seen_pages) % 5 == 0 or not ctx.page_queue
            ):
                now = time.monotonic()
                if not ctx.page_queue or (
                    now - ctx.last_state_save[0] >= _STATE_SAVE_MIN_INTERVAL_SECONDS
                ):
                    ctx.last_state_save[0] = now
                    try:
                        ctx_mod.save_crawl_state(
                            ctx.output_dir,
                            page_queue=list(ctx.page_queue),
                            seen_pages=list(ctx.seen_pages),
                            page_titles=ctx.page_titles,
                            page_files={
                                url: [path.relative_to(ctx.output_dir).as_posix(), encoding]
                                for url, (path, encoding) in ctx.page_files.items()
                            },
                            page_scan=ctx.page_scan,
                            resources=ctx.resource_dicts(),
                            sha256_set=ctx.dedup.seen_hashes() if ctx.dedup else [],
                        )
                    except Exception as exc:  # pragma: no cover - 防御性：状态保存异常吞掉
                        _log.warning("failed to save crawl state: %s", exc)
    return cancelled

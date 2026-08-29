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
from collections import deque
from dataclasses import asdict
from urllib.parse import urlparse

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
    normalize_url,
    output_path_for_url,
    same_domain,
)

# 与 app.crawler 共用同一个 logger：UI 通过 attach_log_handler 挂到
# "crawler" logger 的 handler 对所有模块日志生效，行为与拆分前一致。
_log = logging.getLogger("crawler")


def _seed_page_queue(ctx: _CrawlContext) -> None:
    """种子 URL 入队并按 --sitemap 做站点地图页面发现。"""
    args = ctx.args
    root_url = normalize_url(args.url)
    if root_url:
        ctx.page_queue.append(root_url)

    if args.sitemap:
        parsed = urlparse(args.url)
        sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
        _log.info("discovering pages from %s", sitemap_url)
        sitemap_urls = discover_sitemap_urls(sitemap_url, ctx.headers, args.timeout)
        for su in sitemap_urls:
            if su not in ctx.seen_pages and su not in ctx.page_queue:
                if args.same_domain and not same_domain(su, args.url):
                    continue
                if is_blocked_url(su, ctx.block_keywords):
                    continue
                ctx.page_queue.append(su)


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
    ctx.seen_pages = set(saved.get("seen_pages", []))
    ctx.page_titles = {k: str(v) for k, v in saved.get("page_titles", {}).items()}
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


def _scan_pages(ctx: _CrawlContext) -> bool:
    """逐页扫描：抓取页面、解析资源、按 --crawl-pages 扩展待扫队列。

    返回是否被用户取消（取消后调用方应跳过下载与后处理）。
    """
    args = ctx.args
    cancelled = False  # 统一取消标志：任何阶段触发取消后跳过后续耗时步骤并返回 1
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
        page_url = ctx.page_queue.popleft()
        if not page_url or page_url in ctx.seen_pages:  # pragma: no cover - 防御性：队列已预过滤
            continue
        if is_blocked_url(page_url, ctx.block_keywords):
            _log.info("blocked page: %s", page_url)
            continue
        if args.same_domain and not same_domain(
            page_url, args.url
        ):  # pragma: no cover - 防御性：入队时已过滤
            continue
        if ctx.robots and not ctx.robots.can_fetch(
            args.user_agent, page_url
        ):  # pragma: no cover - 防御性：页面 robots 检查
            _log.info("robots.txt skipped page: %s", page_url)
            continue

        _log.info("scanning page: %s", page_url)
        cr.report_progress(
            args,
            phase="page",
            current_url=page_url,
            pages_scanned=len(ctx.seen_pages),
            total_resources=0,
            processed_resources=0,
        )
        ctx.seen_pages.add(page_url)
        try:
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
            _log.warning("page fetch failed: %s", exc)
            continue

        page_path = output_path_for_url(page_url, ctx.output_dir, "text/html", prefix="pages")
        if not page_path.suffix:  # pragma: no cover - text/html 总是返回带后缀的路径
            page_path = page_path.with_suffix(".html")
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_bytes(data)

        html = decode_text(data, content_type, args.encoding)
        ctx.page_html[page_url] = html
        ctx.page_titles[page_url] = extract_title(html)
        parser = PageParser(page_url)
        parser.feed(html)
        ctx.all_resources.extend(parser.resources)

        if args.crawl_pages:
            for link in parser.page_links:
                if link not in ctx.seen_pages and link not in ctx.page_queue:
                    if is_blocked_url(
                        link, ctx.block_keywords
                    ):  # pragma: no cover - 防御性：资源级 block 检查已在更上层处理
                        continue
                    if not args.same_domain or same_domain(link, args.url):
                        ctx.page_queue.append(link)

            # 状态保存节流：每 5 页或队列耗尽时保存一次,避免每页全量重写
            if getattr(args, "resume_crawl", False) and (
                len(ctx.seen_pages) % 5 == 0 or not ctx.page_queue
            ):
                try:
                    ctx_mod.save_crawl_state(
                        ctx.output_dir,
                        page_queue=list(ctx.page_queue),
                        seen_pages=list(ctx.seen_pages),
                        page_titles=ctx.page_titles,
                        resources=[asdict(r) for r in ctx.all_resources],
                        sha256_set=ctx.dedup.seen_hashes() if ctx.dedup else [],
                    )
                except Exception as exc:  # pragma: no cover - 防御性：状态保存异常吞掉
                    _log.warning("failed to save crawl state: %s", exc)
    return cancelled

"""爬虫的页面扫描阶段（种子入队 / 状态恢复 / 并发扫描）。

从 :mod:`app.crawler` 拆出的 crawl() 前段阶段函数：
``_seed_page_queue`` / ``_restore_state`` / ``_scan_pages``。

页面扫描走「asyncio 调度 + 固定大小线程池」的并发管线：事件循环独占队列、
去重、robots/域过滤、限速账本与续跑状态（单线程写入，语义与串行实现一致），
阻塞的 HTTP/渲染抓取与 HTML 解析在工作线程执行（线程本地 fetcher 缓存保证
会话不跨线程复用）。``--workers`` 同时控制扫描并发与下载并发。

本模块通过 facade 导入 ``app.crawler`` 并在运行期按属性访问被测试
patch 的模块全局名（``cr.fetch`` / ``cr.should_stop`` /
``cr.report_progress`` / ``cr.save_crawl_state`` 等），保证 patch
语义与拆分前完全一致。

导入时序约定：本模块由 ``app.crawler`` 在其模块顶部导入；仅绑定
``app.crawler`` 模块对象，不在导入期读取其任何属性，循环导入安全。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
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
    normalize_url,
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


#: 明确按文档处理的非 HTML content-type（部分站点用 text/plain 返回 HTML）。
_DOCUMENT_CONTENT_TYPES = frozenset({"text/plain", "application/xhtml+xml"})

# ── 逐页元数据（<meta name="description"> / <html lang>）──
# 只扫描文档头部：元数据在 head 内，且大页面（数 MB 内联脚本）不该为两个
# 字段整篇再解析一遍。截断 HTML（无 </head>）按前 256 KiB 处理。
_META_SCAN_LIMIT = 256 * 1024
_HEAD_END_RE = re.compile(r"</head\s*>", re.IGNORECASE)
_DESCRIPTION_MAX_CHARS = 500
_LANG_MAX_CHARS = 50
_WHITESPACE_RE = re.compile(r"\s+")


def _clean_meta_text(value: str, limit: int) -> str:
    """折叠空白并限长：meta 内容常带换行/缩进，超过上限的按上限截断。"""
    return _WHITESPACE_RE.sub(" ", value).strip()[:limit]


class _PageMetaParser(HTMLParser):
    """采集首个非空 ``<html lang>`` 与 ``<meta name="description">``。

    ``html.parser`` 对大小写、单双引号、无引号属性与截断标签一律容错：
    属性名自动小写、属性值自动反转义；EOF 处未闭合的标签被丢弃（不会抛错）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.description = ""
        self.lang = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "html" and not self.lang:
            for name, value in attrs:
                if name == "lang" and value and value.strip():
                    self.lang = _clean_meta_text(value, _LANG_MAX_CHARS)
                    break
        elif tag == "meta" and not self.description:
            attributes = {name: value for name, value in attrs}
            if (attributes.get("name") or "").strip().lower() == "description":
                content = attributes.get("content") or ""
                if content.strip():
                    self.description = _clean_meta_text(content, _DESCRIPTION_MAX_CHARS)


def _extract_page_meta(html_text: str) -> tuple[str, str]:
    """从页面 HTML 提取 ``(description, lang)``；缺失时均为空串。

    非 HTML、截断 HTML、超大页面都只做尽力而为的提取，绝不抛异常——
    元数据是报告增值字段，不能因为解析失败影响抓取主流程。
    """
    if not html_text:
        return "", ""
    excerpt = html_text[:_META_SCAN_LIMIT]
    head_end = _HEAD_END_RE.search(excerpt)
    if head_end is not None:
        excerpt = excerpt[: head_end.end()]
    parser = _PageMetaParser()
    try:
        parser.feed(excerpt)
        parser.close()
    except Exception:  # pragma: no cover - html.parser 对畸形输入的兜底
        _log.debug("page meta extraction failed", exc_info=True)
    return parser.description, parser.lang


def _is_page_document(content_type: str, url: str) -> bool:
    """响应是否应作为 HTML 页面落盘（无 content-type 时保持旧行为）。

    ``--crawl-pages`` 会把 ``<a href>`` 指向的 PDF/CSV/ZIP 等也入队；若不按
    响应类型过滤，这些二进制会被保存成页面、做正文/Markdown 抽取并生成
    离线页（本地实测 CSV 变成 data.html/.md/.txt 三份垃圾产物）。
    """
    ctype = content_type.split(";", 1)[0].strip().lower()
    if not ctype:
        return True
    if "html" in ctype or "xml" in ctype:
        return True
    return ctype in _DOCUMENT_CONTENT_TYPES


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
    # 状态必须属于同一个起始站点：否则复用输出目录换站续跑会把旧站页面/资源带进来。
    saved_seed = saved.get("seed_url")
    if not saved_seed:
        _log.warning("saved state has no seed_url (旧版本状态), ignoring it and starting fresh")
        return
    if saved_seed != ctx.seed_url:
        _log.warning(
            "saved state belongs to %s, not %s; ignoring it and starting fresh",
            saved_seed,
            ctx.seed_url,
        )
        return
    ctx.page_queue = deque(saved.get("page_queue", []))
    ctx.queued_pages = set(ctx.page_queue)
    ctx.seen_pages = set(saved.get("seen_pages", []))
    ctx.page_titles = {k: str(v) for k, v in saved.get("page_titles", {}).items()}
    # 逐页元数据：旧版状态无该字段（按空处理），残缺条目逐项兜底，保证
    # 恢复后的报告字段形状与新抓取一致。
    saved_meta = saved.get("page_meta")
    if isinstance(saved_meta, dict):
        for url, entry in saved_meta.items():
            if not isinstance(url, str) or not isinstance(entry, dict):
                continue
            status = entry.get("status_code")
            ctx.page_meta[url] = {
                "status_code": (
                    status if isinstance(status, int) and not isinstance(status, bool) else None
                ),
                "content_type": str(entry.get("content_type") or ""),
                "description": str(entry.get("description") or ""),
                "lang": str(entry.get("lang") or ""),
            }
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


def _fetch_page_html(
    ctx: _CrawlContext, page_url: str, meta: dict[str, object] | None = None
) -> tuple[bytes, str]:
    """``--render-js`` 路径：headless Chromium 渲染页面，失败退回静态 HTTP。

    渲染走库级 ``DynamicFetcher``（导航逐跳守卫 / 代理 / SSRF 校验在库层
    生效）；渲染失败（浏览器崩溃、超时、目标站对 headless 报 4xx 等）不
    终止扫描，退回 :func:`cr.fetch` 的静态路径，让错误统一走既有分类。
    域级限速两条路径共用同一个 limiter。``meta`` 透传给底层抓取函数，
    用于回填页面状态码 / Content-Type（失败路径不写，保持未知态）。
    """
    args = ctx.args
    try:
        ctx.rate_limiter.wait_if_needed(page_url)
        data, content_type = cr.render_page(
            page_url, args.timeout, ctx.headers, control_args=args, meta=meta
        )
        ctx.rate_limiter.record_request(page_url)
        blocked = cr.drain_blocked_resources()
        if blocked:
            with ctx.page_scan_lock:
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
        blocked = cr.drain_blocked_resources()
        if blocked:
            with ctx.page_scan_lock:
                ctx.page_scan["blocked_assets"] += len(blocked)
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
            meta=meta,
        )


@dataclass
class _PageOutcome:
    """单个页面在工作线程中抓取/解析后的结果（调度线程据此更新 ctx）。"""

    url: str
    page_path: Path
    content_type: str = ""
    encoding: str = "utf-8"
    title: str = ""
    description: str = ""
    lang: str = ""
    status_code: int | None = None
    resources: list[Resource] = field(default_factory=list)
    page_links: list[str] = field(default_factory=list)
    # 取消（用户中止）/ 抓取失败 / 非文档响应三种跳过原因互斥
    cancelled: bool = False
    error: str = ""
    non_document: bool = False


def _scan_page(
    ctx: _CrawlContext,
    page_url: str,
    seed_url: str,
    page_path: Path,
) -> _PageOutcome:
    """工作线程：抓取页面（必要时渲染）、落盘并解析资源（不触碰 ctx 集合）。"""
    args = ctx.args
    response_meta: dict[str, object] = {}
    try:
        if getattr(args, "render_js", False):
            data, content_type = _fetch_page_html(ctx, page_url, meta=response_meta)
        else:
            data, content_type = cr.fetch(
                page_url,
                args.timeout,
                ctx.headers,
                args.retries,
                ctx.max_bytes,
                rate_limiter=ctx.rate_limiter,
                control_args=args,
                meta=response_meta,
            )
    except Exception as exc:
        if isinstance(exc, RuntimeError) and str(exc) == "cancelled by user":
            return _PageOutcome(url=page_url, page_path=page_path, cancelled=True)
        return _PageOutcome(
            url=page_url, page_path=page_path, error=render_error(exc, context=page_url)
        )

    if page_url != seed_url and not _is_page_document(content_type, page_url):
        # 非种子链接明确是二进制/数据响应：不落成页面，避免产生垃圾离线产物
        return _PageOutcome(
            url=page_url, page_path=page_path, content_type=content_type, non_document=True
        )

    try:
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_bytes(data)
        html = decode_text(data, content_type, args.encoding)
        description, lang = _extract_page_meta(html)
        status_code = response_meta.get("status")
        parser = PageParser(
            page_url,
            download_linked_data=bool(getattr(args, "download_linked_data", False)),
        )
        parser.feed(html)
    except Exception as exc:
        # 落盘/解码/解析失败按单页失败处理：非法 charset 名、磁盘错误等不能
        # 终止整个并发扫描（与下载阶段的 worker 兜底一致，且串行时代这里会
        # 直接崩掉整个 crawl）。
        return _PageOutcome(
            url=page_url, page_path=page_path, error=render_error(exc, context=page_url)
        )
    return _PageOutcome(
        url=page_url,
        page_path=page_path,
        # 报告里的 content_type 优先取响应头（与拆分前一致），
        # 解码/后缀判定仍用抓取返回值
        content_type=str(response_meta.get("content_type") or content_type or ""),
        encoding=_resolved_encoding(args.encoding, content_type),
        title=extract_title(html),
        description=description,
        lang=lang,
        status_code=(
            status_code
            if isinstance(status_code, int) and not isinstance(status_code, bool)
            else None
        ),
        resources=parser.resources,
        page_links=list(parser.page_links),
    )


def _next_scan_page(ctx: _CrawlContext) -> str | None:
    """取下一个可扫描页面（应用 block / 域名 / robots 过滤）；队列耗尽返回 None。

    过滤计数（skipped）与日志只在调度线程执行，保证并发下计数不丢。
    """
    args = ctx.args
    while ctx.page_queue:
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
        return page_url
    return None


def _apply_page_outcome(ctx: _CrawlContext, outcome: _PageOutcome) -> None:
    """把页面抓取结果并入上下文（只在调度线程调用，无需额外锁）。"""
    args = ctx.args
    page_url = outcome.url
    if outcome.error:
        _log.error("%s", outcome.error)
        ctx.page_scan["failed"] += 1
        if not ctx.page_scan["first_error"]:
            ctx.page_scan["first_error"] = outcome.error[:4000]
        return
    if outcome.non_document:
        ctx.page_scan["skipped"] += 1
        _log.info("skipped non-document page: %s (%s)", page_url, outcome.content_type or "unknown")
        return

    ctx.page_files[page_url] = (outcome.page_path, outcome.encoding)
    ctx.page_titles[page_url] = outcome.title
    ctx.page_meta[page_url] = {
        "status_code": outcome.status_code,
        "content_type": str(outcome.content_type or ""),
        "description": outcome.description,
        "lang": outcome.lang,
    }
    ctx.all_resources.extend(outcome.resources)

    if args.crawl_pages:
        for link in outcome.page_links:
            if link not in ctx.seen_pages and link not in ctx.queued_pages:
                if is_blocked_url(
                    link, ctx.block_keywords
                ):  # pragma: no cover - 防御性：资源级 block 检查已在更上层处理
                    continue
                if not args.same_domain or same_domain(link, args.url):
                    ctx.enqueue_page(link)


def _save_scan_state_if_due(
    ctx: _CrawlContext, *, idle: bool, in_flight: list[str] | None = None
) -> None:
    """按节流策略保存 --resume-crawl 状态（idle=队列与在途都为空）。

    状态保存只在调度线程发生：页面列表、资源与去重哈希都是单写者，
    并发 worker 不会与快照交错。``idle`` 对应串行实现里「队列耗尽」
    的无条件保存；其余情况按每 5 页 + 最小时间间隔节流，避免 O(P²) 写放大。

    ``in_flight`` 是已出队、worker 尚未回填的页面。它们已计入 ``seen_pages``
    但没有页面文件/资源：若原样写入快照，进程中断后续跑会把这些页面当成
    「已扫描」永久漏掉。快照把它们从 ``seen_pages`` 剔除并按原出队顺序放回
    ``page_queue`` 头部，续跑时按未完成页面重新抓取。
    """
    args = ctx.args
    if not getattr(args, "resume_crawl", False):
        return
    # 与串行实现一致：只有开启 --crawl-pages 时才维护续跑状态
    # （不扩展队列的单页抓取没有可续的页面集合）
    if not getattr(args, "crawl_pages", False):
        return
    if not idle and len(ctx.seen_pages) % 5 != 0:
        return
    now = time.monotonic()
    if not idle and now - ctx.last_state_save[0] < _STATE_SAVE_MIN_INTERVAL_SECONDS:
        return
    ctx.last_state_save[0] = now
    pending_pages = list(in_flight or ())
    pending_set = set(pending_pages)
    try:
        ctx_mod.save_crawl_state(
            ctx.output_dir,
            seed_url=ctx.seed_url,
            # 在途页面放回队列头部（它们比剩余队列出队更早），续跑时重抓
            page_queue=pending_pages + list(ctx.page_queue),
            seen_pages=[url for url in ctx.seen_pages if url not in pending_set],
            page_titles=ctx.page_titles,
            page_meta=ctx.page_meta,
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


def _scan_worker_count(args: argparse.Namespace) -> int:
    """页面扫描并发数：全局线程上限 + ``--render-js`` 的浏览器进程上限。

    ``--render-js`` 下每个 worker 线程各持有一个 Chromium（线程本地缓存，
    不可跨线程复用），进程数等价于并发数；超出上限的部分只减少扫描并发，
    下载阶段仍按用户配置的 workers 执行（下载不渲染）。
    """
    workers = ctx_mod.clamp_workers(getattr(args, "workers", 1))
    if getattr(args, "render_js", False) and workers > ctx_mod.MAX_RENDER_WORKERS:
        _log.warning(
            "--render-js uses one Chromium per worker; capping page-scan workers "
            "%d -> %d (downloads still use %d)",
            workers,
            ctx_mod.MAX_RENDER_WORKERS,
            workers,
        )
        return ctx_mod.MAX_RENDER_WORKERS
    return workers


async def _scan_pages_async(ctx: _CrawlContext, seed_url: str, saved_paths: set[str]) -> bool:
    """并发扫描调度：填满并发槽位 → 等任一完成 → 并入结果 → 状态节流保存。"""
    args = ctx.args
    workers = _scan_worker_count(args)
    loop = asyncio.get_running_loop()
    pending: dict[asyncio.Future[_PageOutcome], str] = {}
    cancelled = False
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="page-scan") as pool:
        while True:
            # 补槽位：暂停/取消每派发一页检查一次（与串行实现的检查频次一致）；
            # max_pages 以「已入 seen 的页面」为计数，与旧行为同源。
            while not cancelled and len(pending) < workers and len(ctx.seen_pages) < args.max_pages:
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
                page_url = _next_scan_page(ctx)
                if page_url is None:
                    break
                page_path = output_path_for_url(
                    page_url, ctx.output_dir, "text/html", prefix="pages"
                )
                if not page_path.suffix:  # pragma: no cover - text/html 总是返回带后缀的路径
                    page_path = page_path.with_suffix(".html")
                # 路径在调度线程按队列顺序预留：与串行实现的最长命中顺序一致
                page_path = ctx_mod.reserve_page_path(page_path, page_url, saved_paths)
                ctx.seen_pages.add(page_url)
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
                future = loop.run_in_executor(pool, _scan_page, ctx, page_url, seed_url, page_path)
                pending[future] = page_url
            if not pending:
                break
            done, _ = await asyncio.wait(set(pending), return_when=asyncio.FIRST_COMPLETED)
            for future in done:
                pending.pop(future)
                outcome = future.result()
                if outcome.cancelled:
                    # 在途页面已开始：其结果照常并入，只是不再派发新页面
                    cancelled = True
                    continue
                _apply_page_outcome(ctx, outcome)
                _save_scan_state_if_due(
                    ctx,
                    idle=not ctx.page_queue and not pending,
                    in_flight=list(pending.values()),
                )
    return cancelled


def _scan_pages(ctx: _CrawlContext) -> bool:
    """并发扫描页面：抓取、解析资源、按 --crawl-pages 扩展待扫队列。

    返回是否被用户取消（取消后调用方应跳过下载与后处理）。
    """
    args = ctx.args
    saved_paths = {str(path).casefold() for path, _ in ctx.page_files.values()}
    try:
        seed_url = normalize_url(args.url)
    except ValueError:  # pragma: no cover - 入口已校验过 URL
        seed_url = args.url
    return asyncio.run(_scan_pages_async(ctx, seed_url, saved_paths))

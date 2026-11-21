"""爬虫的报告与后处理阶段（取消检查 / JSONL 收尾 / 各类清单与运行报告）。

从 :mod:`app.crawler` 拆出的 crawl() 尾段阶段函数：
``_post_pause_check`` / ``_close_jsonl`` / ``_post_process`` /
``_log_crawl_summary``。

本模块通过 facade 导入 ``app.crawler`` 并在运行期按属性访问被测试
patch 的模块全局名（``cr.wait_if_paused`` / ``cr.should_stop`` /
``cr.clear_crawl_state``），保证 patch 语义与拆分前完全一致；
清单/报告写入函数本身不是 patch 目标，直接从 :mod:`app.crawler_report`
叶子模块导入。

导入时序约定：本模块由 ``app.crawler`` 在其模块顶部导入；仅绑定
``app.crawler`` 模块对象，不在导入期读取其任何属性，循环导入安全。
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Callable, Iterator
from typing import TextIO

from web_crawler.app import _crawler_context as ctx_mod
from web_crawler.app import _crawler_core as cr
from web_crawler.app._crawler_context import _CrawlContext
from web_crawler.app._offline import OFFLINE_INDEX, OFFLINE_PAGES_DIRNAME, write_offline_index
from web_crawler.app._offline_verify import unavailable_offline_check, verify_offline
from web_crawler.app._report_extract import _extraction_selector
from web_crawler.app.crawler_models import ManifestRow, PageExtraction, Resource
from web_crawler.app.crawler_report import (
    RewriteTable,
    _format_bytes,
    extract_markdown,
    extract_readable_text,
    format_duration,
    smart_extract,
    strip_page_overlays,
    write_extracted_data,
    write_failed_manifests,
    write_manifests,
    write_run_report,
    write_summary,
    write_video_manifests,
)
from web_crawler.exceptions import ConfigError

# 与 app.crawler 共用同一个 logger：UI 通过 attach_log_handler 挂到
# "crawler" logger 的 handler 对所有模块日志生效，行为与拆分前一致。
_log = logging.getLogger("crawler")


def _post_pause_check(args: argparse.Namespace) -> bool:
    """后处理阶段入口的暂停/取消检查：暂停会阻塞到恢复；取消（含暂停中取消）返回 True。"""
    try:
        cr.wait_if_paused(args)
    except RuntimeError:
        pass  # 暂停期间取消 → should_stop 为 True,由返回值统一处理
    return cr.should_stop(args)


def _close_jsonl(ctx: _CrawlContext) -> None:
    """收尾 JSONL 实时清单（flush + close）。

    取消路径同样必须调用：JSONL 句柄若只靠 GC 兜底关闭，会在解释器
    回收时产生 unraisable 警告并延迟释放文件锁（Windows 上还会阻止
    临时目录清理）。
    """
    if ctx.jsonl_file is not None:
        try:
            ctx.jsonl_file.flush()
            ctx.jsonl_file.close()
        except OSError as _jsonl_err:
            _log.warning("failed to finalize JSONL manifest: %s", _jsonl_err)


def _iter_page_html(
    ctx: _CrawlContext, on_error: Callable[[str, Exception], None] | None = None
) -> Iterator[tuple[str, str]]:
    """逐页从磁盘重读扫描阶段落盘的 HTML（page_url, html）。

    页面 HTML 不再全量驻留内存（万页 × 数百 KB 会把堆撑爆），此处按需
    逐页读取，峰值内存为一页；编码用扫描时从 Content-Type/--encoding
    解析出的同款，保证重读结果与扫描期解码一致。落盘文件丢失时跳过并
    记 warning（后处理不因单页 IO 失败整体中断）。
    """
    for page_url, (page_path, encoding) in ctx.page_files.items():
        try:
            yield page_url, page_path.read_text(encoding=encoding, errors="replace")
        except (OSError, LookupError) as exc:
            _log.warning("failed to re-read saved page %s: %s", page_url, exc)
            if on_error is not None:
                on_error(page_url, exc)


def _extract_pages(ctx: _CrawlContext) -> None:
    args = ctx.args
    if not (
        getattr(args, "smart_extract", False)
        or getattr(args, "extract_text", False)
        or getattr(args, "extract_markdown", False)
        or getattr(args, "page_sample_callback", None)
        or ctx.rules is not None
    ) or _post_pause_check(args):
        return
    feed = None
    if ctx.rules is not None:
        try:
            feed = (ctx.output_dir / "rule_items.jsonl").open("w", encoding="utf-8", newline="\n")
        except OSError as exc:
            ctx.rule_write_errors += 1
            _log.warning("could not open field result file: %s", exc)
    try:
        _extract_page_contents(ctx, feed)
    finally:
        if feed is not None:
            try:
                feed.close()
            except OSError as exc:
                ctx.rule_write_errors += 1
                _log.warning("could not finish field result file: %s", exc)
    if ctx.rule_coverage is not None:
        try:
            ctx.rule_coverage.write(ctx.output_dir / "rule_report.json")
        except OSError as exc:
            ctx.rule_write_errors += 1
            _log.warning("could not write field coverage report: %s", exc)


def _extract_page_contents(ctx: _CrawlContext, feed: TextIO | None) -> None:
    args = ctx.args
    structured = getattr(args, "smart_extract", False)
    readable = getattr(args, "extract_text", False)
    markdown = getattr(args, "extract_markdown", False)
    on_extraction = getattr(args, "extraction_callback", None)
    on_sample = getattr(args, "page_sample_callback", None)
    extracted_data: list[dict[str, object]] = []
    text_dir = ctx.output_dir / "extracted_text"
    if readable:
        try:
            text_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _log.warning("could not create extracted text directory: %s", exc)
    md_dir = ctx.output_dir / "extracted_markdown"
    if markdown:
        try:
            md_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _log.warning("could not create extracted markdown directory: %s", exc)

    text_count = 0
    markdown_count = 0
    coverage = ctx.rule_coverage
    for page_url, html in _iter_page_html(ctx, coverage.add_error if coverage else None):
        if _post_pause_check(args):
            break
        title = ctx.page_titles.get(page_url, "")
        if on_sample is not None:
            on_sample(page_url, title, html)
        if not (structured or readable or markdown or ctx.rules is not None):
            continue
        selector = _extraction_selector(html)
        data = None
        rule_result = None
        text = ""
        if structured:
            try:
                data = smart_extract(selector, page_url)
                extracted_data.append(data)
            except Exception as exc:
                _log.warning("smart extraction failed for %s: %s", page_url, exc)
        if readable:
            try:
                text = extract_readable_text(selector)
                if text:
                    path = ctx.page_artifact_path(page_url, "extracted_text")
                    destination = path.with_name(path.name + ".txt")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(text, encoding="utf-8")
                    text_count += 1
                else:
                    # JS 壳页面此前静默「成功」：正文 0 字但 status=success，
                    # 调研会把「没渲染」误判成「页面没内容」——留 warning 并
                    # 指向渲染路径（2026-11 实测 docs.trae.ai 1.87MB → 0 字）。
                    _log.warning(
                        "no extractable text for %s (page may be a JS shell); consider --render-js",
                        page_url,
                    )
            except Exception as exc:
                _log.warning("text extraction failed for %s: %s", page_url, exc)
        markdown_text = ""
        if markdown:
            try:
                content = extract_markdown(selector)
                markdown_text = content
                if content.strip():
                    path = ctx.page_artifact_path(page_url, "extracted_markdown")
                    destination = path.with_name(path.name + ".md")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(content, encoding="utf-8")
                    markdown_count += 1
                elif not readable:
                    _log.warning(
                        "no extractable markdown for %s (page may be a JS shell); consider --render-js",
                        page_url,
                    )
            except Exception as exc:
                _log.warning("markdown extraction failed for %s: %s", page_url, exc)
        if ctx.rules is not None and coverage is not None:
            try:
                rule_result = ctx.rules.extract(selector, page_url=page_url)
            except ConfigError as exc:
                coverage.add_error(page_url, exc)
                _log.warning("field extraction failed for %s: %s", page_url, exc)
            else:
                coverage.add_result(rule_result)
                if feed is not None:
                    try:
                        feed.write(json.dumps(rule_result, ensure_ascii=False) + "\n")
                    except OSError as exc:
                        feed = None
                        ctx.rule_write_errors += 1
                        _log.warning("could not write field result file: %s", exc)
        if on_extraction is not None and (
            text or markdown_text or data is not None or rule_result is not None
        ):
            on_extraction(PageExtraction(page_url, title, text, data, rule_result))

    if extracted_data:
        try:
            write_extracted_data(ctx.output_dir, extracted_data)
        except OSError as exc:
            _log.warning("could not write extracted data: %s", exc)
    if markdown:
        _log.info(
            "extracted markdown: %d pages -> %s",
            markdown_count,
            ctx.output_dir / "extracted_markdown",
        )
    if readable:
        _log.info("extracted text: %d pages -> %s", text_count, text_dir)


def _post_process(
    ctx: _CrawlContext,
    manifest_rows: list[ManifestRow],
    crawl_start_time: float,
    report_config: dict[str, object],
    *,
    cancelled: bool,
) -> tuple[int, int]:
    """后处理阶段：离线 HTML 重写、各类清单、智能/文本抽取与运行报告。

    每个阶段入口检查取消标志（含后处理期间新到达的取消请求），取消后
    跳过剩余耗时步骤。返回 ``(video_count, failed_count)``。
    """
    args = ctx.args
    output_dir = ctx.output_dir

    if cancelled or cr.should_stop(args):
        _log.info("crawl cancelled; skipping post-processing")
        _close_jsonl(ctx)
        return 0, 0

    # 每个后处理阶段独立 try/except：单步失败仅记 warning，保证清单与报告尽量生成
    if args.rewrite_html and not _post_pause_check(args):
        ctx.offline_check = unavailable_offline_check()
        try:
            table = RewriteTable(manifest_rows, output_dir)
            rewritten_paths: set[str] = set()
            pages = {
                url: ctx_mod.reserve_page_path(
                    ctx.page_artifact_path(url, OFFLINE_PAGES_DIRNAME).with_suffix(".html"),
                    url,
                    rewritten_paths,
                )
                for url in ctx.page_files
            }
            table.set_pages(pages)
            table.rewrite_stylesheets(
                manifest_rows, lambda: _post_pause_check(args), encoding=args.encoding
            )
            written = {}
            for page_url, html in _iter_page_html(ctx):
                if _post_pause_check(args):
                    break
                path = pages[page_url]
                try:
                    rewritten = table.apply(html, page_url, destination=path)
                    if getattr(args, "strip_overlays", False):
                        rewritten = strip_page_overlays(rewritten)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(rewritten, encoding="utf-8")
                    written[page_url] = path
                except Exception as exc:
                    _log.warning("offline HTML rewrite failed for %s: %s", page_url, exc)
            if written:
                write_offline_index(output_dir, written, ctx.page_titles)
                ctx.offline_page_count = len(written)
        except Exception as exc:
            _log.warning("offline HTML rewrite failed: %s", exc)

        if ctx.offline_page_count:
            ctx.offline_check = unavailable_offline_check(index_written=True)
            try:
                ctx.offline_check = verify_offline(
                    output_dir, should_stop=lambda: _post_pause_check(args)
                )
            except Exception as exc:
                _log.warning("offline verification failed: %s", exc)

    try:
        write_manifests(output_dir, manifest_rows)
    except Exception as exc:
        _log.warning("failed to write manifests: %s", exc)
    video_count = 0
    failed_count = 0
    if not _post_pause_check(args):
        try:
            video_count = write_video_manifests(output_dir, manifest_rows) if args.video_mode else 0
            failed_count = write_failed_manifests(output_dir, manifest_rows)
        except Exception as exc:
            _log.warning("failed to write video/failed manifests: %s", exc)

    _extract_pages(ctx)

    if not _post_pause_check(args):
        begin_finalization = getattr(args, "begin_finalization", None)
        ctx.result_committed = begin_finalization is None or begin_finalization()
    if ctx.result_committed:
        try:
            write_summary(
                output_dir,
                manifest_rows,
                len(ctx.page_titles),
                start_time=crawl_start_time,
                end_time=time.time(),
                config=report_config,
                page_scan=ctx.page_scan,
                rule_results=ctx.rule_summary(),
                offline_pages=ctx.offline_page_count,
                offline_check=ctx.offline_check,
            )
            write_run_report(
                output_dir,
                manifest_rows,
                len(ctx.page_titles),
                start_time=crawl_start_time,
                end_time=time.time(),
                config=report_config,
                page_scan=ctx.page_scan,
                rule_results=ctx.rule_summary(),
                offline_pages=ctx.offline_page_count,
                offline_check=ctx.offline_check,
            )
        except Exception as exc:
            _log.warning("failed to write run report: %s", exc)

    # JSONL 清单已在下载阶段逐条追加；此处收尾 flush/关闭（正常完成路径）
    _close_jsonl(ctx)

    # 成功完成后清除续跑状态
    if (
        getattr(args, "resume_crawl", False)
        and (ctx.page_titles or not ctx.page_scan["failed"])
        and ctx.result_committed
    ):
        try:
            ctx_mod.clear_crawl_state(output_dir)
            _log.info("crawl state cleared")
        except Exception as exc:
            _log.warning("failed to clear crawl state: %s", exc)

    return video_count, failed_count


def _log_crawl_summary(
    ctx: _CrawlContext,
    manifest_rows: list[ManifestRow],
    resources: list[Resource],
    crawl_start_time: float,
    video_count: int,
    failed_count: int,
) -> None:
    """打印本次运行汇总：页数 / 下载 / 去重 / 失败 / 时长与产物路径。"""
    args = ctx.args
    output_dir = ctx.output_dir
    ok_count = sum(1 for row in manifest_rows if row.status.startswith("ok"))
    dedup_count = sum(1 for row in manifest_rows if "dedup" in row.status)
    total_bytes = sum(row.bytes for row in manifest_rows if row.status == "ok")

    _log.info("")
    _log.info("Pages scanned:       %d", len(ctx.page_titles))
    _log.info("Resources found:     %d", len(ctx.queue) or len(resources))
    _log.info("Downloaded:          %d (%s)", ok_count, _format_bytes(total_bytes))
    _log.info("Deduplicated:        %d", dedup_count)
    _log.info("Failed/skipped:      %d", failed_count)
    _log.info("Duration:            %s", format_duration(time.time() - crawl_start_time))
    _log.info("Output:              %s", output_dir)
    if ctx.result_committed:
        _log.info("CSV manifest:        %s", output_dir / "resources_manifest.csv")
        _log.info("JSON manifest:       %s", output_dir / "resources_manifest.json")
        _log.info("Summary:             %s", output_dir / "summary.txt")
        _log.info("Run report (JSON):   %s", output_dir / "run_report.json")
        _log.info("Run report (MD):     %s", output_dir / "run_report.md")
        _log.info("Run report (HTML):   %s", output_dir / "run_report.html")
        if ctx.offline_page_count:
            _log.info("Offline page index:  %s", output_dir / OFFLINE_INDEX)
        if args.video_mode:
            _log.info("Video resources:     %d", video_count)

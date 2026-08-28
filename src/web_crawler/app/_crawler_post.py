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
import logging
import time
from urllib.parse import urlparse

from web_crawler.app import crawler as cr
from web_crawler.app._crawler_context import _CrawlContext
from web_crawler.app.crawler_models import ManifestRow, Resource
from web_crawler.app.crawler_net import output_path_for_url
from web_crawler.app.crawler_report import (
    _format_bytes,
    extract_readable_text,
    format_duration,
    rewrite_html,
    smart_extract,
    strip_page_overlays,
    write_extracted_data,
    write_failed_manifests,
    write_manifests,
    write_run_report,
    write_summary,
    write_video_manifests,
)

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
        try:
            for page_url, html in ctx.page_html.items():
                rewritten = rewrite_html(html, manifest_rows, page_url, output_dir)
                if getattr(args, "strip_overlays", False):
                    rewritten = strip_page_overlays(rewritten)
                rewritten_path = output_path_for_url(
                    page_url, output_dir, "text/html", prefix="offline_pages"
                )
                rewritten_path = rewritten_path.with_suffix(".html")
                rewritten_path.parent.mkdir(parents=True, exist_ok=True)
                rewritten_path.write_text(rewritten, encoding="utf-8")
        except Exception as exc:
            _log.warning("offline HTML rewrite failed: %s", exc)

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

    # 智能数据抽取
    if getattr(args, "smart_extract", False) and not _post_pause_check(args):
        try:
            extracted_data: list[dict[str, object]] = []
            for page_url, html in ctx.page_html.items():
                extracted_data.append(smart_extract(html, page_url))
            if extracted_data:
                write_extracted_data(output_dir, extracted_data)
        except Exception as exc:
            _log.warning("smart extraction failed: %s", exc)

    # 正文抽取
    if getattr(args, "extract_text", False) and not _post_pause_check(args):
        try:
            text_dir = output_dir / "extracted_text"
            text_dir.mkdir(parents=True, exist_ok=True)
            count = 0
            for page_url, html in ctx.page_html.items():
                text = extract_readable_text(html)
                if not text:
                    continue
                name = urlparse(page_url).path.strip("/").replace("/", "_") or "index"
                (text_dir / f"{name}.txt").write_text(text, encoding="utf-8")
                count += 1
            _log.info("extracted text: %d pages -> %s", count, text_dir)
        except Exception as exc:
            _log.warning("text extraction failed: %s", exc)

    if not _post_pause_check(args):
        try:
            write_summary(
                output_dir,
                manifest_rows,
                len(ctx.seen_pages),
                start_time=crawl_start_time,
                end_time=time.time(),
                config=report_config,
            )
            write_run_report(
                output_dir,
                manifest_rows,
                len(ctx.seen_pages),
                start_time=crawl_start_time,
                end_time=time.time(),
                config=report_config,
            )
        except Exception as exc:
            _log.warning("failed to write run report: %s", exc)

    # JSONL 清单已在下载阶段逐条追加；此处收尾 flush/关闭（正常完成路径）
    _close_jsonl(ctx)

    # 成功完成后清除续跑状态
    if getattr(args, "resume_crawl", False) and not _post_pause_check(args):
        try:
            cr.clear_crawl_state(output_dir)
            _log.info("crawl state cleared")
        except Exception as exc:
            _log.warning("failed to clear crawl state: %s", exc)

    return video_count, failed_count


def _log_crawl_summary(
    ctx: _CrawlContext,
    manifest_rows: list[ManifestRow],
    resources: list[Resource],
    crawl_start_time: float,
    cancelled: bool,
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
    _log.info("Pages scanned:       %d", len(ctx.seen_pages))
    _log.info("Resources found:     %d", len(resources))
    _log.info("Downloaded:          %d (%s)", ok_count, _format_bytes(total_bytes))
    _log.info("Deduplicated:        %d", dedup_count)
    _log.info("Failed:              %d", failed_count)
    _log.info("Duration:            %s", format_duration(time.time() - crawl_start_time))
    _log.info("Output:              %s", output_dir)
    if not (cancelled or cr.should_stop(args)):
        _log.info("CSV manifest:        %s", output_dir / "resources_manifest.csv")
        _log.info("JSON manifest:       %s", output_dir / "resources_manifest.json")
        _log.info("Summary:             %s", output_dir / "summary.txt")
        _log.info("Run report (JSON):   %s", output_dir / "run_report.json")
        _log.info("Run report (MD):     %s", output_dir / "run_report.md")
        _log.info("Run report (HTML):   %s", output_dir / "run_report.html")
        if args.video_mode:
            _log.info("Video resources:     %d", video_count)

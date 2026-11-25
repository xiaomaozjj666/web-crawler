"""爬虫的下载执行阶段（资源过滤 / JSONL 清单 / 单资源处理 / 并发下载）。

从 :mod:`app.crawler` 拆出的 crawl() 中段阶段函数：
``_filter_resources`` / ``_append_jsonl_row`` / ``_process_resource`` /
``_run_downloads``。

本模块通过 facade 导入 ``app.crawler`` 并在运行期按属性访问被测试
patch 的模块全局名（``cr.fetch`` / ``cr.HAS_AES`` / ``cr.get_segment_key`` /
``cr.decrypt_aes128`` / ``cr.discover_playlist_resources`` /
``cr.wait_if_paused`` / ``cr.should_stop`` / ``cr.report_progress``），
保证 patch 语义与拆分前完全一致。

导入时序约定：本模块由 ``app.crawler`` 在其模块顶部导入；仅绑定
``app.crawler`` 模块对象，不在导入期读取其任何属性，循环导入安全。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import asdict
from pathlib import Path
from typing import Any

from web_crawler.app import _crawler_core as cr
from web_crawler.app._crawler_cache import NOT_MODIFIED_STATUS, ValidatorCache
from web_crawler.app._crawler_context import _CrawlContext
from web_crawler.app._crawler_discovery import discover_playlist_resources
from web_crawler.app.crawler_models import ManifestRow, Resource
from web_crawler.app.crawler_net import (
    category_for,
    decode_stylesheet,
    decode_text,
    discover_css_resources,
    is_blocked_url,
    is_video_candidate,
    output_path_for_url,
    output_prefix_for_resource,
    same_domain,
    unique_resources,
)
from web_crawler.app.crawler_report import row_for

# 与 app.crawler 共用同一个 logger：UI 通过 attach_log_handler 挂到
# "crawler" logger 的 handler 对所有模块日志生效，行为与拆分前一致。
_log = logging.getLogger("crawler")


def _filter_resources(ctx: _CrawlContext) -> list[Resource]:
    """合并去重后按 block 关键词 / 域名 / 类型 / URL 正则过滤资源列表。"""
    args = ctx.args
    resources = unique_resources(ctx.all_resources)
    if ctx.block_keywords:
        resources = [r for r in resources if not is_blocked_url(r.url, ctx.block_keywords)]
    if args.same_domain:
        resources = [r for r in resources if same_domain(r.url, args.url)]
    if args.video_only:
        resources = [r for r in resources if is_video_candidate(r)]
    _include_re = re.compile(args.include_pattern) if args.include_pattern else None
    _exclude_re = re.compile(args.exclude_pattern) if args.exclude_pattern else None
    if _include_re:
        resources = [r for r in resources if _include_re.search(r.url)]
    if _exclude_re:
        resources = [r for r in resources if not _exclude_re.search(r.url)]
    # 资源总量上限（--max-resources）：只限页面时，大站的 CSS/播放列表展开
    # 仍可能把队列撑到数万条；这里在进入下载前统一截断并如实记录。
    max_resources = int(getattr(args, "max_resources", 0) or 0)
    if max_resources > 0 and len(resources) > max_resources:
        _log.warning(
            "resource limit reached: keeping first %d of %d resources (--max-resources)",
            max_resources,
            len(resources),
        )
        resources = resources[:max_resources]
    return resources


def _append_jsonl_row(ctx: _CrawlContext, row: ManifestRow) -> None:
    """best-effort 逐条写 JSONL；失败仅记 warning，不影响主流程。"""
    if ctx.jsonl_file is None:
        return
    try:
        ctx.jsonl_file.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
    except OSError as exc:
        _log.warning("failed to write JSONL manifest: %s", exc)


def _fetch_resource(
    ctx: _CrawlContext,
    resource: Resource,
    target: Path,
    stream_to_disk: bool,
) -> tuple[bytes, str, Path | None, dict[str, object]]:
    """抓取单个资源；``--cache`` 时带条件请求，304 命中返回复用的本地产物。

    返回 ``(data, content_type, reused_path, response_meta)``：
    ``reused_path`` 非空表示上游答 304、本轮没有下载（产物是上次的
    ``saved_path``）；``response_meta`` 由 :func:`cr.fetch` 填充，含本次
    响应的 status / etag / last_modified（缓存更新用）。

    缓存只做「要不要下载」的优化：条件请求失败、304 但本地产物已缺失，
    都会退回普通抓取——缓存永远不能把任务变成失败。
    """
    args = ctx.args
    cache: ValidatorCache | None = ctx.validator_cache
    kwargs: dict[str, Any] = {
        "resume_path": target if stream_to_disk else None,
        "rate_limiter": ctx.rate_limiter,
        "control_args": args,
    }
    response_meta: dict[str, object] = {}
    conditional: dict[str, str] = {}
    if cache is not None:
        conditional = cache.conditional_headers(resource.url, ctx.output_dir)
        if conditional:
            kwargs["extra_headers"] = conditional
            kwargs["allow_not_modified"] = True
        kwargs["response_meta"] = response_meta
    data, content_type = cr.fetch(
        resource.url, args.timeout, ctx.headers, args.retries, ctx.max_bytes, **kwargs
    )
    if conditional and response_meta.get("status") == 304:
        reused = cache.reuse(resource.url, ctx.output_dir) if cache is not None else None
        if reused is not None:
            path, cached_type, _ = reused
            _log.info("not modified (304), reusing %s", path)
            return b"", cached_type or content_type, path, response_meta
        # 缓存条目存在但产物在查询后被删/移走：退回无条件抓取，保证如实下载
        _log.warning(
            "upstream 304 for %s but the cached artifact is gone; refetching", resource.url
        )
        for key in ("extra_headers", "allow_not_modified"):
            kwargs.pop(key, None)
        data, content_type = cr.fetch(
            resource.url, args.timeout, ctx.headers, args.retries, ctx.max_bytes, **kwargs
        )
    return data, content_type, None, response_meta


def _process_resource(ctx: _CrawlContext, resource: Resource) -> ManifestRow | None:
    """下载单个资源：去重 / 解密 / CSS 与播放列表二级发现，产出清单行。

    返回 None 表示 worker 检测到用户取消（调用方据此中止下载循环）。
    """
    args = ctx.args
    if cr.should_stop(args):
        return None
    if ctx.robots and not ctx.robots.can_fetch(args.user_agent, resource.url):
        return row_for("skipped by robots.txt", resource, "", "", 0, ctx.page_titles)

    _log.debug("[%d/%d] %s", ctx.processed_count[0] + 1, len(ctx.queue), resource.url)

    if args.list_only:
        cr.report_progress(
            args,
            phase="download",
            current_url=resource.url,
            total_resources=len(ctx.queue),
            processed_resources=ctx.processed_count[0] + 1,
            pages_scanned=len(ctx.page_titles),
        )
        return row_for("listed only", resource, "", "", 0, ctx.page_titles)

    try:
        initial_prefix = output_prefix_for_resource(args, resource, "", ctx.page_titles)
        target = output_path_for_url(resource.url, ctx.output_dir, "", prefix=initial_prefix)
        target.parent.mkdir(parents=True, exist_ok=True)
        # fetch 的流式路径只返回响应类型。断点续传后如需检查内容，读回完整文件。
        needs_content = (
            bool(ctx.dedup)
            or (getattr(args, "decrypt", False) and cr.HAS_AES)
            or bool(args.include_css_urls)
            or bool(args.expand_playlists)
        )
        stream_to_disk = getattr(args, "resume", False) or not needs_content
        data, content_type, reused_path, response_meta = _fetch_resource(
            ctx, resource, target, stream_to_disk
        )
        cache_hit = reused_path is not None
        if cache_hit:
            # 304：不写盘，产物就是上次的 saved_path；需要内容（CSS/播放列表/
            # 去重/解密）时从盘上读回，保证二级发现与首次下载一致。
            assert reused_path is not None
            target = reused_path
            data = target.read_bytes() if needs_content else b""
            byte_count = target.stat().st_size
        elif stream_to_disk and target.is_file():
            if needs_content:
                data = target.read_bytes()
            byte_count = target.stat().st_size
        else:
            byte_count = len(data)

        # 内容去重（缓存命中没有新内容，不参与去重）
        if cache_hit:
            is_dup, sha256 = False, ""
        else:
            is_dup, sha256 = (
                ctx.dedup.is_duplicate(data, resource.url) if ctx.dedup else (False, "")
            )

        if not is_dup and not cache_hit:
            final_prefix = output_prefix_for_resource(args, resource, content_type, ctx.page_titles)
            final_target = output_path_for_url(
                resource.url, ctx.output_dir, content_type, prefix=final_prefix
            )
            if final_target != target and target.exists():  # pragma: no cover - 仅 resume 场景触发
                final_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(final_target))
                target = final_target
            target.parent.mkdir(parents=True, exist_ok=True)
            if not stream_to_disk:
                write_data = data
                if (
                    getattr(args, "decrypt", False) and cr.HAS_AES
                ):  # pragma: no cover - pycryptodome 未安装时不可达
                    key_info = cr.get_segment_key(resource.url)
                    if key_info:
                        try:
                            write_data = cr.decrypt_aes128(data, key_info[0], key_info[1])
                            _log.info("decrypted segment: %s", resource.url)
                        except Exception as exc:
                            _log.warning("decryption failed for %s: %s", resource.url, exc)
                            raise ValueError(f"decryption failed, skipped: {resource.url}") from exc
                target.write_bytes(write_data)

        if ctx.validator_cache is not None and not is_dup and not cache_hit:
            # 只有真下载过才更新校验器：304 命中沿用旧条目（上游仍匹配）
            ctx.validator_cache.record(
                resource.url,
                ctx.output_dir,
                target,
                content_type=content_type,
                byte_count=byte_count,
                etag=str(response_meta.get("etag") or ""),
                last_modified=str(response_meta.get("last_modified") or ""),
            )

        status = NOT_MODIFIED_STATUS if cache_hit else "ok"
        local_discoveries: list[Resource] = []

        # CSS 资源发现
        if args.include_css_urls and (
            "text/css" in content_type or resource.url.lower().endswith(".css")
        ):
            css_text = decode_stylesheet(data, content_type, args.encoding)
            for extra in discover_css_resources(css_text, resource.url, resource.page_url):
                if args.same_domain and not same_domain(
                    extra.url, args.url
                ):  # pragma: no cover - 防御性：CSS 资源跨域过滤
                    continue
                if is_blocked_url(
                    extra.url, ctx.block_keywords
                ):  # pragma: no cover - 防御性：CSS 资源 block 过滤
                    continue
                local_discoveries.append(extra)

        # 播放列表展开
        if (
            not is_dup
            and args.expand_playlists
            and category_for(resource.url, content_type, resource.kind, resource.found_in)
            == "playlist"
        ):
            playlist_text = decode_text(data, content_type, args.encoding)
            extra_resources, playlist_note = discover_playlist_resources(
                playlist_text,
                resource.url,
                resource.page_url,
                headers=ctx.headers,
                timeout=args.timeout,
                retries=args.retries,
                decrypt=getattr(args, "decrypt", False),
            )
            if playlist_note:  # pragma: no cover - 播放列表 note 极少出现
                status = f"{status}; {playlist_note}"
            for extra in extra_resources:
                if args.same_domain and not same_domain(
                    extra.url, args.url
                ):  # pragma: no cover - 防御性：播放列表跨域过滤
                    continue
                if is_blocked_url(
                    extra.url, ctx.block_keywords
                ):  # pragma: no cover - 防御性：播放列表 block 过滤
                    continue
                if args.video_only and not is_video_candidate(
                    extra
                ):  # pragma: no cover - 防御性：播放列表 video_only 过滤
                    continue
                local_discoveries.append(extra)

        # 线程安全：把新发现的资源加入全局列表
        if local_discoveries:
            with ctx.discovery_lock:
                if getattr(args, "result_callback", None) is not None:
                    ctx.resource_dependencies.update(
                        (resource.url, extra.url) for extra in local_discoveries
                    )
                for extra in local_discoveries:
                    if extra.url not in ctx.queued_urls:
                        ctx.queued_urls.add(extra.url)
                        ctx.new_discoveries.append(extra)
                        ctx.queue.append(extra)

        manifest_row = row_for(
            "skipped by dedup" if is_dup else status,
            resource,
            "" if is_dup else str(target),
            content_type,
            byte_count,
            ctx.page_titles,
            sha256=sha256,
        )

    except Exception as exc:
        if "cancelled by user" in str(exc):
            return None  # 取消信号
        from web_crawler._cli import render_error

        _log.error("%s", render_error(exc, context=resource.url))
        status = f"error: {exc}"
        manifest_row = row_for(status, resource, "", "", 0, ctx.page_titles)

    return manifest_row


def _run_downloads(ctx: _CrawlContext, manifest_rows: list[ManifestRow]) -> bool:
    """并发下载阶段：线程池消费队列并吸收运行期新发现的资源。

    返回是否被用户取消。
    """
    args = ctx.args
    cancelled = False
    _log.info("downloading %d resources with %d workers...", len(ctx.queue), args.workers)
    cr.report_progress(
        args,
        phase="resources",
        total_resources=len(ctx.queue),
        processed_resources=0,
        pages_scanned=len(ctx.page_titles),
    )

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(_process_resource, ctx, r): r for r in ctx.queue[:]}
        # 增量等待集：只随完成收缩，wait() 不再每 0.5 秒对全部在途 future
        # 重挂一遍回调；FIRST_COMPLETED 让任一完成立即醒来（原实现
        # FIRST_EXCEPTION 语义下无异常时要等满 timeout 才处理完成项）
        pending: set[Future[ManifestRow | None]] = set(futures)
        index = 0
        download_queue_size = len(ctx.queue)
        failed_count = 0
        bytes_downloaded = 0

        while pending:
            if not cancelled:
                try:
                    cr.wait_if_paused(args)
                except RuntimeError:
                    cancelled = True
                else:
                    cancelled = cr.should_stop(args)
            if cancelled:
                # 撤销未启动的下载，收集已完成与仍在收尾的下载结果。
                pending = {future for future in pending if not future.cancel()}
                if not pending:
                    break

            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                resource_for_future = futures.pop(future)
                completed_row: ManifestRow | None = None
                try:
                    result = future.result()
                    if result is None:
                        cancelled = True
                        continue
                    with ctx.manifest_lock:
                        manifest_rows.append(result)
                        _append_jsonl_row(ctx, result)
                    completed_row = result
                except (
                    Exception
                ) as exc:  # pragma: no cover - 防御性：_process_resource 已捕获所有异常
                    _log.error("unexpected worker error: %s", exc)
                    with ctx.manifest_lock:
                        failed_row = row_for(
                            "error: worker crashed", resource_for_future, "", "", 0, ctx.page_titles
                        )
                        manifest_rows.append(failed_row)
                        _append_jsonl_row(ctx, failed_row)
                    completed_row = failed_row

                index += 1
                ctx.processed_count[0] = index
                # 增量计数，避免每个资源完成时扫描完整 manifest 导致 O(n²)。
                if completed_row is not None:
                    if completed_row.status.startswith("error"):
                        failed_count += 1
                    elif completed_row.status.startswith("ok"):
                        bytes_downloaded += completed_row.bytes
                cr.report_progress(
                    args,
                    phase="download",
                    current_url=getattr(resource_for_future, "url", ""),
                    total_resources=download_queue_size,
                    processed_resources=index,
                    pages_scanned=len(ctx.page_titles),
                    failed_resources=failed_count,
                    bytes_downloaded=bytes_downloaded,
                )

            # 把运行期新发现的资源加入队列
            if ctx.new_discoveries and not cancelled:
                with ctx.discovery_lock:
                    while ctx.new_discoveries:
                        r = ctx.new_discoveries.pop(0)
                        fut = executor.submit(_process_resource, ctx, r)
                        futures[fut] = r
                        pending.add(fut)
                        download_queue_size += 1
    if cancelled:
        _log.info("cancelled by user")
    return cancelled

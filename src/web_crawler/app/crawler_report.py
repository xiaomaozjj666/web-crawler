"""网页资源爬虫的报告/格式化层。

模块拆分时从 :mod:`app.crawler` 抽出，包含清单写入器（CSV/JSON）、
人类可读的格式化工具、错误分类、报告上下文构建、摘要/Markdown/HTML
报告写入器、离线 HTML 重写、遮罩层剥离与智能数据抽取。

本模块绝不导入 ``app.crawler``（否则会循环依赖）；依赖
:mod:`app.crawler_models` 提供共享数据类，依赖 :mod:`app.crawler_net`
提供分类/解析工具。
"""

from __future__ import annotations

import csv
import html as html_lib
import json
import logging
import re
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from web_crawler.app.crawler_models import ManifestRow, Resource
from web_crawler.app.crawler_net import category_for, extract_title, is_video_resource, same_domain

# 与 app.crawler 共用同一个 logger：UI 通过 attach_log_handler 挂到
# "crawler" logger 的 handler 对所有模块日志生效，行为与拆分前一致。
_log = logging.getLogger("crawler")

# web_crawler.app.crawler 用 `from web_crawler.app.crawler_report import *` 同名再导出，保证拆分后
# `app.crawler` 模块的所有属性仍可访问（兼容 cr.xxx 访问与 patch.object(cr, ...)）。
__all__ = [
    "EXTRACTED_DATA_FIELDS",
    "FIELD_NAMES",
    "OVERLAY_PATTERNS",
    "_ERROR_CLASSES",
    "_ERROR_LABELS",
    "_HTML_CSS",
    "_format_bytes",
    "_tag_class",
    "_write_html_report",
    "_write_manifest_pair",
    "_write_markdown_report",
    "build_recommendations",
    "build_report_context",
    "classify_error",
    "diagnostic_for_status",
    "extract_readable_text",
    "format_duration",
    "is_failed_row",
    "rewrite_html",
    "row_for",
    "smart_extract",
    "strip_page_overlays",
    "write_extracted_data",
    "write_failed_manifests",
    "write_manifests",
    "write_run_report",
    "write_summary",
    "write_video_manifests",
]

# pycryptodome 是否可用（build_recommendations 的加密提示文案用）。
# 与 app.crawler 中的探测保持一致。
try:
    from Crypto.Cipher import AES as _AES  # noqa: F401 - 仅探测 pycryptodome 可用性

    HAS_AES = True  # pragma: no cover - pycryptodome 未安装时不可达
except ImportError:
    HAS_AES = False

# ── 清单写入器 ──────────────────────────────────────────────────────

FIELD_NAMES = [
    "status",
    "url",
    "saved_path",
    "content_type",
    "bytes",
    "category",
    "found_in",
    "kind",
    "page_url",
    "page_title",
    "diagnostic",
    "sha256",
]


def _write_manifest_pair(output_dir: Path, rows: list[ManifestRow], prefix: str) -> int:
    """通用清单写入：生成 CSV + JSON 文件对。返回行数。"""
    csv_path = output_dir / f"{prefix}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELD_NAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    json_path = output_dir / f"{prefix}.json"
    json_path.write_text(
        json.dumps([asdict(row) for row in rows], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(rows)


def write_manifests(output_dir: Path, rows: list[ManifestRow]) -> None:
    _write_manifest_pair(output_dir, rows, "resources_manifest")


def write_video_manifests(output_dir: Path, rows: list[ManifestRow]) -> int:
    video_rows = [row for row in rows if is_video_resource(row)]
    return _write_manifest_pair(output_dir, video_rows, "video_manifest") if video_rows else 0


def is_failed_row(row: ManifestRow) -> bool:
    return row.status.startswith("error") or row.status.startswith("skipped")


def write_failed_manifests(output_dir: Path, rows: list[ManifestRow]) -> int:
    failed_rows = [row for row in rows if is_failed_row(row)]
    return _write_manifest_pair(output_dir, failed_rows, "failed_resources") if failed_rows else 0


def _format_bytes(n: float) -> str:
    value = float(n)
    if value < 1024:
        # 字节级用整数显示更自然（0 B / 500 B），带小数时才保留一位
        return f"{int(value)} B" if value == int(value) else f"{value:.1f} B"
    # 逐级换算：先除后比,保证 KB/MB/GB/TB 各档都能正确显示
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    # TB 循环后 value 仍以 TB 计,需再除一次才是 PB（否则 1 PB 会显示成 1024.0 PB）
    return f"{value / 1024:.1f} PB"


def format_duration(seconds: float) -> str:
    """把秒数格式化为人类可读的耗时字符串。"""
    if seconds < 0:
        return "未知"
    if seconds < 1:
        return f"{seconds * 1000:.0f} 毫秒"
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 时 {minutes} 分 {sec} 秒"


# ── 错误分类（用于报告里把失败原因归类汇总）─────────────────────────────────

_ERROR_CLASSES: list[tuple[str, tuple[str, ...]]] = [
    ("auth", ("401", "unauthorized", "403", "forbidden")),
    ("not_found", ("404", "not found")),
    ("rate_limit", ("429", "rate limit", "too many requests")),
    ("timeout", ("timeout", "timed out")),
    ("size_limit", ("file exceeds", "max-bytes", "max_bytes")),
    ("robots", ("robots.txt", "robots")),
    ("dedup", ("dedup",)),
    ("encrypted", ("encrypted playlist",)),
    ("network", ("urlerror", "connection", "dns", "refused", "reset", "unreachable", "socket")),
    ("cancelled", ("cancelled", "canceled")),
]

_ERROR_LABELS: dict[str, str] = {
    "auth": "认证/权限失败 (401/403)",
    "not_found": "资源不存在 (404)",
    "rate_limit": "被限流 (429)",
    "timeout": "请求超时",
    "size_limit": "超出大小限制",
    "robots": "被 robots.txt 拦截",
    "dedup": "内容去重跳过",
    "encrypted": "加密播放清单",
    "network": "网络/连接错误",
    "cancelled": "用户取消",
    "other_error": "其它错误",
    "skipped_other": "其它跳过",
    "other": "其它",
}


def classify_error(status: str) -> str:
    """把一行 status 文本归入一个错误大类，便于在报告里汇总。"""
    lowered = status.lower()
    for label, keywords in _ERROR_CLASSES:
        if any(kw in lowered for kw in keywords):
            return label
    if status.startswith("error"):
        return "other_error"
    if status.startswith("skipped"):
        return "skipped_other"
    return "other"


# ── 报告上下文构建 ────────────────────────────────────────────────────────


def build_report_context(
    rows: list[ManifestRow],
    pages_scanned: int,
    start_time: float,
    end_time: float,
    config: dict[str, object] | None = None,
) -> dict[str, object]:
    """从抓取结果构建一份完整的报告上下文字典，供 JSON / Markdown / HTML 共用。"""
    status_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    category_bytes: dict[str, int] = {}
    page_resource_counts: dict[str, int] = {}
    page_title_map: dict[str, str] = {}
    error_classes: dict[str, int] = {}
    skip_classes: dict[str, int] = {}
    total_bytes = 0
    dedup_count = 0
    dedup_saved_bytes = 0
    listed_count = 0

    for row in rows:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
        category_counts[row.category] = category_counts.get(row.category, 0) + 1
        if row.status == "ok":
            total_bytes += row.bytes
            category_bytes[row.category] = category_bytes.get(row.category, 0) + row.bytes
        if "dedup" in row.status:
            dedup_count += 1
            dedup_saved_bytes += row.bytes
        if row.status == "listed only":
            listed_count += 1
        # 真正的请求失败（error:*）与跳过（skipped:*）分开统计，避免把去重/robots 当作失败
        if row.status.startswith("error"):
            cls = classify_error(row.status)
            error_classes[cls] = error_classes.get(cls, 0) + 1
        elif row.status.startswith("skipped"):
            cls = classify_error(row.status)
            skip_classes[cls] = skip_classes.get(cls, 0) + 1
        if row.page_url:
            page_resource_counts[row.page_url] = page_resource_counts.get(row.page_url, 0) + 1
            if row.page_title and row.page_url not in page_title_map:
                page_title_map[row.page_url] = row.page_title

    ok_count = sum(1 for row in rows if row.status.startswith("ok"))
    failed_rows = [row for row in rows if row.status.startswith("error")]
    skipped_rows = [row for row in rows if row.status.startswith("skipped")]
    skipped_count = len(skipped_rows)
    success_rate = round((ok_count + listed_count) * 100 / len(rows), 2) if rows else 100.0
    duration = max(0.0, end_time - start_time)

    # 按页面资源数量排序，取前 15（携带真实页面标题）
    top_pages = sorted(page_resource_counts.items(), key=lambda item: item[1], reverse=True)[:15]

    # 按错误大类汇总失败项明细（每类取前若干条 URL）
    failures_by_class: dict[str, list[dict[str, str]]] = {}
    for row in failed_rows:
        cls = classify_error(row.status)
        failures_by_class.setdefault(cls, []).append(
            {
                "status": row.status,
                "url": row.url,
                "diagnostic": row.diagnostic,
                "page_url": row.page_url,
            }
        )
    for cls in failures_by_class:
        failures_by_class[cls] = failures_by_class[cls][:20]

    largest = [
        {
            "url": row.url,
            "saved_path": row.saved_path,
            "bytes": row.bytes,
            "size": _format_bytes(row.bytes),
            "category": row.category,
        }
        for row in sorted((r for r in rows if r.bytes > 0), key=lambda r: r.bytes, reverse=True)[
            :20
        ]
    ]

    return {
        "schema": 2,
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%S%z", time.localtime(end_time or time.time())
        ),
        "start_time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(start_time))
        if start_time
        else "",
        "end_time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(end_time))
        if end_time
        else "",
        "duration_seconds": round(duration, 2),
        "duration_human": format_duration(duration),
        "pages_scanned": pages_scanned,
        "config": config or {},
        "resources": {
            "total": len(rows),
            "ok": ok_count,
            "listed_only": listed_count,
            "failed": len(failed_rows),
            "skipped": skipped_count,
            "deduped": dedup_count,
            "success_rate_percent": success_rate,
            "downloaded_bytes": total_bytes,
            "downloaded_size": _format_bytes(total_bytes),
            "dedup_saved_bytes": dedup_saved_bytes,
            "dedup_saved_size": _format_bytes(dedup_saved_bytes),
        },
        "by_status": dict(sorted(status_counts.items())),
        "by_category": {
            cat: {
                "count": category_counts.get(cat, 0),
                "bytes": category_bytes.get(cat, 0),
                "size": _format_bytes(category_bytes.get(cat, 0)),
            }
            for cat in sorted(category_counts)
        },
        "by_error_class": {cls: error_classes[cls] for cls in sorted(error_classes)},
        "by_skip_class": {cls: skip_classes[cls] for cls in sorted(skip_classes)},
        "top_pages": [
            {"page_url": url, "page_title": page_title_map.get(url, ""), "resource_count": count}
            for url, count in top_pages
        ],
        "largest_files": largest,
        "top_failures": [
            {
                "status": row.status,
                "url": row.url,
                "diagnostic": row.diagnostic,
                "page_url": row.page_url,
            }
            for row in failed_rows[:50]
        ],
        "failures_by_class": failures_by_class,
    }


def build_recommendations(ctx: dict[str, object]) -> list[dict[str, str]]:
    """根据报告上下文生成带优先级的、可操作的建议清单。"""
    recs: list[dict[str, str]] = []
    resources = ctx["resources"]
    assert isinstance(resources, dict)
    by_error: dict[str, int] = ctx.get("by_error_class", {})  # type: ignore[assignment]
    total = int(resources.get("total", 0))

    def add(priority: str, title: str, detail: str) -> None:
        recs.append({"priority": priority, "title": title, "detail": detail})

    # 无资源
    if total == 0:
        add(
            "高",
            "未发现任何资源",
            "确认起始 URL 可正常访问；开启「扫描站内页面」「抓 CSS 内资源」或「从 Sitemap 发现页面」可扩大发现范围。",
        )
        return recs

    # 认证/权限
    if by_error.get("auth"):
        n = by_error["auth"]
        add(
            "高",
            f"{n} 个资源因 401/403 被拒",
            "确认账号已获得授权，在请求头里填入有效 Cookie / Authorization / Referer。请勿尝试绕过登录或付费墙。",
        )

    # 限流
    if by_error.get("rate_limit"):
        n = by_error["rate_limit"]
        add(
            "高",
            f"{n} 个资源触发 429 限流",
            "减少并发线程数 (workers)、增大请求间隔 (delay)；程序已自动按 Retry-After 退避，但过高的并发仍会持续触发。",
        )

    # 超时
    if by_error.get("timeout"):
        n = by_error["timeout"]
        add(
            "中",
            f"{n} 个资源请求超时",
            "提高 --timeout、减少并发、增加重试次数 (--retries)；对慢速大文件可单独提高超时。",
        )

    # 网络
    if by_error.get("network"):
        n = by_error["network"]
        add(
            "中",
            f"{n} 个资源出现网络/连接错误",
            "检查网络与代理设置；部分 CDN 域名可能需要加入同域名白名单或关闭 same-domain 限制。",
        )

    # 不存在
    if by_error.get("not_found"):
        n = by_error["not_found"]
        add(
            "低",
            f"{n} 个资源返回 404",
            "这些 URL 可能已过期或来自过期页面；通常无需处理，可在 failed_resources.csv 中查看明细。",
        )

    # 大小限制
    if by_error.get("size_limit"):
        n = by_error["size_limit"]
        add(
            "低",
            f"{n} 个资源超过单文件大小上限",
            "如需下载，请调大 --max-bytes 或设为 0（不限制）。",
        )

    # 加密播放清单
    if by_error.get("encrypted"):
        n = by_error["encrypted"]
        tip = (
            "安装 pycryptodome 并开启 --decrypt 以尝试解密"
            if not HAS_AES
            else "开启 --decrypt 以解密分片"
        )
        add(
            "中",
            f"{n} 个加密播放清单未展开",
            f"检测到 AES-128 加密的 m3u8。{tip}；受版权保护的播放清单不会被绕过。",
        )

    # 去重效果提示
    deduped = int(resources.get("deduped", 0))
    if deduped:
        saved = str(resources.get("dedup_saved_size", "0"))
        add(
            "信息",
            f"内容去重节省 {saved}",
            f"共跳过 {deduped} 个 SHA256 重复文件，未重复写入磁盘。",
        )

    # 成功率低
    rate = float(resources.get("success_rate_percent", 100.0))
    if total > 10 and rate < 80 and not by_error.get("auth"):
        add(
            "中",
            f"整体成功率仅 {rate}%",
            "失败比例较高，建议结合上面的分类建议逐项排查；查看 failed_resources.csv 可获取每条失败的诊断信息。",
        )

    # 正常
    if not recs:
        add(
            "信息",
            "本次运行状态正常",
            "可根据需要开启离线 HTML 重写 (--rewrite-html)、正文提取 (--extract-text) 或视频资源清单 (--video-mode)。",
        )

    priority_order = {"高": 0, "中": 1, "低": 2, "信息": 3}
    recs.sort(key=lambda r: priority_order.get(r["priority"], 9))
    return recs


def write_summary(
    output_dir: Path,
    rows: list[ManifestRow],
    pages_scanned: int,
    *,
    start_time: float = 0.0,
    end_time: float = 0.0,
    config: dict[str, object] | None = None,
) -> None:
    ctx = build_report_context(rows, pages_scanned, start_time, end_time, config=config)
    resources = ctx["resources"]
    assert isinstance(resources, dict)
    duration = str(ctx.get("duration_human", "未知"))
    cfg = ctx.get("config", {})
    assert isinstance(cfg, dict)

    lines = [
        "═" * 60,
        "  网页资源采集 · 摘要报告",
        "═" * 60,
        "",
        f"  生成时间      : {ctx.get('generated_at', '')}",
        f"  耗时          : {duration}",
        f"  扫描页面数    : {ctx.get('pages_scanned', 0)}",
        f"  资源总数      : {resources.get('total', 0)}",
        "",
        "  ── 抓取配置 ──────────────────────────────────",
        f"  起始 URL      : {cfg.get('url', '')}",
        f"  并发线程      : {cfg.get('workers', '')}    请求间隔: {cfg.get('delay', '')}s",
        f"  超时          : {cfg.get('timeout', '')}s    重试: {cfg.get('retries', '')}",
        f"  同域名限制    : {cfg.get('same_domain', '')}    遵守 robots: {cfg.get('respect_robots', '')}",
        f"  内容去重      : {cfg.get('dedup', '')}    断点续传: {cfg.get('resume', '')}",
        "",
        "  ── 下载统计 ──────────────────────────────────",
        f"  成功下载      : {resources.get('ok', 0)}",
        f"  仅列出        : {resources.get('listed_only', 0)}",
        f"  失败          : {resources.get('failed', 0)}",
        f"  跳过(去重等)  : {resources.get('skipped', 0)}  (其中去重 {resources.get('deduped', 0)})",
        f"  成功率        : {resources.get('success_rate_percent', 100.0)}%",
        f"  下载总量      : {resources.get('downloaded_size', '0 B')}",
        f"  去重节省      : {resources.get('dedup_saved_size', '0 B')}",
        "",
        "  ── 按状态分布 ────────────────────────────────",
    ]
    by_status = ctx.get("by_status", {})
    assert isinstance(by_status, dict)
    for k, v in by_status.items():
        lines.append(f"  {k:<40} {v}")
    lines.append("")
    lines.append("  ── 按类别分布 ────────────────────────────────")
    by_category = ctx.get("by_category", {})
    assert isinstance(by_category, dict)
    for cat, info in by_category.items():
        assert isinstance(info, dict)
        lines.append(f"  {cat:<16} 数量 {info.get('count', 0):>6}   大小 {info.get('size', '0 B')}")
    lines.append("")
    error_classes = ctx.get("by_error_class", {})
    assert isinstance(error_classes, dict)
    if error_classes:
        lines.append("  ── 失败原因分类 ──────────────────────────────")
        for cls, count in sorted(error_classes.items(), key=lambda item: item[1], reverse=True):
            lines.append(f"  {_ERROR_LABELS.get(cls, cls):<28} {count}")
        lines.append("")
    skip_classes = ctx.get("by_skip_class", {})
    assert isinstance(skip_classes, dict)
    if skip_classes:
        lines.append("  ── 跳过原因分类 ──────────────────────────────")
        for cls, count in sorted(skip_classes.items(), key=lambda item: item[1], reverse=True):
            lines.append(f"  {_ERROR_LABELS.get(cls, cls):<28} {count}")
        lines.append("")

    recs = build_recommendations(ctx)
    if recs:
        lines.append("  ── 建议 ──────────────────────────────────────")
        for rec in recs:
            lines.append(f"  [{rec['priority']}] {rec['title']}")
            if rec["detail"]:
                lines.append(f"      {rec['detail']}")
        lines.append("")

    failed = [row for row in rows if row.status.startswith("error")]
    if failed:
        lines.append("  ── 失败资源 (前 30 条) ───────────────────────")
        for row in failed[:30]:
            lines.append(f"  · {row.status}")
            lines.append(f"    {row.url}")
            if row.diagnostic:
                lines.append(f"    → {row.diagnostic}")
        lines.append("")

    lines.append("═" * 60)
    lines.append("  详细报告: run_report.json / run_report.md / run_report.html")
    lines.append("═" * 60)
    (output_dir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_run_report(
    output_dir: Path,
    rows: list[ManifestRow],
    pages_scanned: int,
    *,
    start_time: float = 0.0,
    end_time: float = 0.0,
    config: dict[str, object] | None = None,
) -> None:
    ctx = build_report_context(rows, pages_scanned, start_time, end_time, config=config)
    ctx["recommendations"] = build_recommendations(ctx)

    # ── JSON 报告 ──
    (output_dir / "run_report.json").write_text(
        json.dumps(ctx, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ── Markdown 报告 ──
    _write_markdown_report(output_dir / "run_report.md", ctx)

    # ── HTML 报告 ──
    _write_html_report(output_dir / "run_report.html", ctx)


def _write_markdown_report(path: Path, ctx: dict[str, object]) -> None:
    resources = ctx["resources"]
    assert isinstance(resources, dict)
    lines: list[str] = []
    lines.append("# 网页资源采集报告")
    lines.append("")
    lines.append(f"- **生成时间**: {ctx.get('generated_at', '')}")
    if ctx.get("start_time"):
        lines.append(f"- **开始时间**: {ctx.get('start_time')}")
        lines.append(f"- **结束时间**: {ctx.get('end_time')}")
    lines.append(f"- **总耗时**: {ctx.get('duration_human', '未知')}")
    lines.append(f"- **扫描页面数**: {ctx.get('pages_scanned', 0)}")
    lines.append("")

    cfg = ctx.get("config", {})
    assert isinstance(cfg, dict)
    if cfg:
        lines.append("## 抓取配置")
        lines.append("")
        lines.append("| 参数 | 值 |")
        lines.append("| --- | --- |")
        for key in (
            "url",
            "workers",
            "delay",
            "timeout",
            "retries",
            "same_domain",
            "respect_robots",
            "dedup",
            "resume",
            "organize",
            "list_only",
            "video_only",
            "expand_playlists",
            "smart_extract",
            "extract_text",
            "strip_overlays",
            "decrypt",
            "sitemap",
            "crawl_pages",
            "max_pages",
            "max_bytes",
        ):
            if key in cfg:
                lines.append(f"| {key} | {cfg[key]} |")
        lines.append("")

    lines.append("## 资源统计")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 资源总数 | {resources.get('total', 0)} |")
    lines.append(f"| 成功下载 | {resources.get('ok', 0)} |")
    lines.append(f"| 仅列出 | {resources.get('listed_only', 0)} |")
    lines.append(f"| 失败 | {resources.get('failed', 0)} |")
    lines.append(f"| 跳过 (含去重 {resources.get('deduped', 0)}) | {resources.get('skipped', 0)} |")
    lines.append(f"| 成功率 | {resources.get('success_rate_percent', 100.0)}% |")
    lines.append(f"| 下载总量 | {resources.get('downloaded_size', '0 B')} |")
    lines.append(f"| 去重节省 | {resources.get('dedup_saved_size', '0 B')} |")
    lines.append("")

    by_category = ctx.get("by_category", {})
    assert isinstance(by_category, dict)
    if by_category:
        lines.append("## 按类别分布")
        lines.append("")
        lines.append("| 类别 | 数量 | 大小 |")
        lines.append("| --- | ---: | ---: |")
        for cat, info in by_category.items():
            assert isinstance(info, dict)
            lines.append(f"| {cat} | {info.get('count', 0)} | {info.get('size', '0 B')} |")
        lines.append("")

    by_status = ctx.get("by_status", {})
    assert isinstance(by_status, dict)
    if by_status:
        lines.append("## 按状态分布")
        lines.append("")
        lines.append("| 状态 | 数量 |")
        lines.append("| --- | ---: |")
        for k, v in by_status.items():
            lines.append(f"| `{k}` | {v} |")
        lines.append("")

    error_classes = ctx.get("by_error_class", {})
    assert isinstance(error_classes, dict)
    if error_classes:
        lines.append("## 失败原因分类")
        lines.append("")
        lines.append("| 原因 | 数量 |")
        lines.append("| --- | ---: |")
        for cls, count in sorted(error_classes.items(), key=lambda item: item[1], reverse=True):
            lines.append(f"| {_ERROR_LABELS.get(cls, cls)} | {count} |")
        lines.append("")

    skip_classes = ctx.get("by_skip_class", {})
    assert isinstance(skip_classes, dict)
    if skip_classes:
        lines.append("## 跳过原因分类")
        lines.append("")
        lines.append("| 原因 | 数量 |")
        lines.append("| --- | ---: |")
        for cls, count in sorted(skip_classes.items(), key=lambda item: item[1], reverse=True):
            lines.append(f"| {_ERROR_LABELS.get(cls, cls)} | {count} |")
        lines.append("")

    largest = ctx.get("largest_files", [])
    assert isinstance(largest, list)
    if largest:
        lines.append("## 最大的文件 (前 20)")
        lines.append("")
        lines.append("| URL | 大小 | 类别 |")
        lines.append("| --- | ---: | --- |")
        for item in largest:
            assert isinstance(item, dict)
            url = item.get("url", "")
            short = url if len(url) <= 70 else url[:67] + "..."
            lines.append(f"| {short} | {item.get('size', '')} | {item.get('category', '')} |")
        lines.append("")

    top_pages = ctx.get("top_pages", [])
    assert isinstance(top_pages, list)
    if top_pages:
        lines.append("## 资源最多的页面 (前 15)")
        lines.append("")
        lines.append("| 页面 | 标题 | 资源数 |")
        lines.append("| --- | --- | ---: |")
        for item in top_pages:
            assert isinstance(item, dict)
            url = item.get("page_url", "")
            short = url if len(url) <= 60 else url[:57] + "..."
            title = item.get("page_title", "") or "-"
            lines.append(f"| {short} | {title} | {item.get('resource_count', 0)} |")
        lines.append("")

    failures_by_class = ctx.get("failures_by_class", {})
    assert isinstance(failures_by_class, dict)
    if failures_by_class:
        lines.append("## 失败资源明细 (按原因分组)")
        lines.append("")
        for cls in sorted(failures_by_class, key=lambda c: len(failures_by_class[c]), reverse=True):
            items = failures_by_class[cls]
            lines.append(f"### {_ERROR_LABELS.get(cls, cls)} ({len(items)} 条)")
            lines.append("")
            for item in items:
                assert isinstance(item, dict)
                lines.append(f"- `{item.get('status', '')}` — {item.get('url', '')}")
                if item.get("diagnostic"):
                    lines.append(f"  - {item['diagnostic']}")
            lines.append("")

    recs = ctx.get("recommendations", [])
    assert isinstance(recs, list)
    if recs:
        lines.append("## 建议")
        lines.append("")
        for rec in recs:
            assert isinstance(rec, dict)
            lines.append(f"### [{rec.get('priority', '')}] {rec.get('title', '')}")
            lines.append("")
            lines.append(f"{rec.get('detail', '')}")
            lines.append("")

    lines.append("---")
    lines.append("*本报告由网页资源采集器自动生成。*")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


_HTML_CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #f4f5f7; color: #1f2937;
  font-family: "Microsoft YaHei", "PingFang SC", -apple-system, Arial, sans-serif;
  line-height: 1.6; }
.container { max-width: 1040px; margin: 0 auto; padding: 32px 20px 64px; }
header { margin-bottom: 28px; }
header h1 { font-size: 26px; margin: 0 0 6px; }
header .meta { color: #6b7280; font-size: 14px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 14px; margin: 20px 0 28px; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 16px 18px; }
.card .label { font-size: 13px; color: #6b7280; margin-bottom: 4px; }
.card .value { font-size: 24px; font-weight: 700; }
.card .sub { font-size: 12px; color: #9ca3af; margin-top: 2px; }
.card.ok { border-left: 4px solid #16a34a; }
.card.fail { border-left: 4px solid #dc2626; }
.card.info { border-left: 4px solid #2563eb; }
.card.warn { border-left: 4px solid #d97706; }
section { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 20px 22px; margin-bottom: 20px; }
section h2 { font-size: 18px; margin: 0 0 14px; padding-bottom: 10px; border-bottom: 1px solid #f0f0f0; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
th { color: #6b7280; font-weight: 600; white-space: nowrap; }
td.num, th.num { text-align: right; }
tr:hover td { background: #f9fafb; }
td.url { word-break: break-all; max-width: 480px; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }
.tag-high { background: #fee2e2; color: #b91c1c; }
.tag-mid { background: #fef3c7; color: #b45309; }
.tag-low { background: #dbeafe; color: #1d4ed8; }
.tag-info { background: #e0e7ff; color: #4338ca; }
.recs { list-style: none; padding: 0; margin: 0; }
.recs li { padding: 12px 0; border-bottom: 1px solid #f0f0f0; }
.recs li:last-child { border-bottom: 0; }
.recs .rtitle { font-weight: 600; }
.recs .rdetail { color: #6b7280; font-size: 14px; margin-top: 2px; }
.empty { color: #9ca3af; font-style: italic; }
footer { text-align: center; color: #9ca3af; font-size: 13px; margin-top: 24px; }
@media (prefers-color-scheme: dark) {
  body { background: #0f172a; color: #e2e8f0; }
  .card, section { background: #1e293b; border-color: #334155; }
  th, td { border-color: #334155; }
  tr:hover td { background: #243349; }
  header .meta, .card .label { color: #94a3b8; }
  section h2 { border-color: #334155; }
}
"""


def _tag_class(priority: str) -> str:
    return {"高": "tag-high", "中": "tag-mid", "低": "tag-low", "信息": "tag-info"}.get(
        priority, "tag-info"
    )


def _write_html_report(path: Path, ctx: dict[str, object]) -> None:
    resources = ctx["resources"]
    assert isinstance(resources, dict)

    def esc(value: object) -> str:
        return html_lib.escape(str(value))

    # 概览卡片
    cards = [
        ("ok", "扫描页面", ctx.get("pages_scanned", 0), ""),
        ("info", "资源总数", resources.get("total", 0), ""),
        ("ok", "成功下载", resources.get("ok", 0), resources.get("downloaded_size", "")),
        ("warn", "仅列出", resources.get("listed_only", 0), ""),
        ("fail", "失败", resources.get("failed", 0), ""),
        ("info", "跳过/去重", resources.get("skipped", 0), f"去重 {resources.get('deduped', 0)}"),
        ("ok", "成功率", f"{resources.get('success_rate_percent', 100.0)}%", ""),
        ("info", "总耗时", ctx.get("duration_human", "未知"), ""),
    ]
    card_html = []
    for css, label, value, sub in cards:
        card_html.append(
            f'<div class="card {css}"><div class="label">{esc(label)}</div>'
            f'<div class="value">{esc(value)}</div>'
            f'<div class="sub">{esc(sub)}</div></div>'
        )

    # 抓取配置表
    cfg = ctx.get("config", {})
    assert isinstance(cfg, dict)
    if cfg:
        cfg_rows = []
        for key in (
            "url",
            "workers",
            "delay",
            "timeout",
            "retries",
            "same_domain",
            "respect_robots",
            "dedup",
            "resume",
            "organize",
            "list_only",
            "video_only",
            "expand_playlists",
            "smart_extract",
            "extract_text",
            "strip_overlays",
            "decrypt",
            "sitemap",
            "crawl_pages",
            "max_pages",
            "max_bytes",
        ):
            if key in cfg:
                cfg_rows.append(f"<tr><td>{esc(key)}</td><td>{esc(cfg[key])}</td></tr>")
        config_table = "<table><tr><th>参数</th><th>值</th></tr>" + "".join(cfg_rows) + "</table>"
    else:
        config_table = '<p class="empty">无配置信息</p>'

    # 类别表
    by_category = ctx.get("by_category", {})
    assert isinstance(by_category, dict)
    if by_category:
        cat_rows = []
        for cat, info in by_category.items():
            assert isinstance(info, dict)
            cat_rows.append(
                f"<tr><td>{esc(cat)}</td><td class='num'>{info.get('count', 0)}</td>"
                f"<td class='num'>{esc(info.get('size', '0 B'))}</td></tr>"
            )
        cat_table = (
            "<table><tr><th>类别</th><th class='num'>数量</th><th class='num'>大小</th></tr>"
            + "".join(cat_rows)
            + "</table>"
        )
    else:
        cat_table = '<p class="empty">无数据</p>'

    # 状态表
    by_status = ctx.get("by_status", {})
    assert isinstance(by_status, dict)
    if by_status:
        status_rows = []
        for k, v in by_status.items():
            status_rows.append(f"<tr><td><code>{esc(k)}</code></td><td class='num'>{v}</td></tr>")
        status_table = (
            "<table><tr><th>状态</th><th class='num'>数量</th></tr>"
            + "".join(status_rows)
            + "</table>"
        )
    else:
        status_table = '<p class="empty">无数据</p>'

    # 错误分类
    error_classes = ctx.get("by_error_class", {})
    assert isinstance(error_classes, dict)
    if error_classes:
        err_rows = []
        for cls, count in sorted(error_classes.items(), key=lambda item: item[1], reverse=True):
            err_rows.append(
                f"<tr><td>{esc(_ERROR_LABELS.get(cls, cls))}</td><td class='num'>{count}</td></tr>"
            )
        err_table = (
            "<table><tr><th>失败原因</th><th class='num'>数量</th></tr>"
            + "".join(err_rows)
            + "</table>"
        )
    else:
        err_table = '<p class="empty">无失败记录</p>'

    # 跳过原因分类
    skip_classes = ctx.get("by_skip_class", {})
    assert isinstance(skip_classes, dict)
    if skip_classes:
        skip_rows = []
        for cls, count in sorted(skip_classes.items(), key=lambda item: item[1], reverse=True):
            skip_rows.append(
                f"<tr><td>{esc(_ERROR_LABELS.get(cls, cls))}</td><td class='num'>{count}</td></tr>"
            )
        skip_table = (
            "<table><tr><th>跳过原因</th><th class='num'>数量</th></tr>"
            + "".join(skip_rows)
            + "</table>"
        )
    else:
        skip_table = '<p class="empty">无跳过记录</p>'

    # 最大文件
    largest = ctx.get("largest_files", [])
    assert isinstance(largest, list)
    if largest:
        big_rows = []
        for item in largest:
            assert isinstance(item, dict)
            big_rows.append(
                f"<tr><td class='url'>{esc(item.get('url', ''))}</td>"
                f"<td class='num'>{esc(item.get('size', ''))}</td>"
                f"<td>{esc(item.get('category', ''))}</td></tr>"
            )
        big_table = (
            "<table><tr><th>URL</th><th class='num'>大小</th><th>类别</th></tr>"
            + "".join(big_rows)
            + "</table>"
        )
    else:
        big_table = '<p class="empty">无下载文件</p>'

    # 资源最多的页面
    top_pages = ctx.get("top_pages", [])
    assert isinstance(top_pages, list)
    if top_pages:
        page_rows = []
        for item in top_pages:
            assert isinstance(item, dict)
            title = item.get("page_title", "") or "-"
            page_rows.append(
                f"<tr><td class='url'>{esc(item.get('page_url', ''))}</td>"
                f"<td>{esc(title)}</td>"
                f"<td class='num'>{item.get('resource_count', 0)}</td></tr>"
            )
        pages_table = (
            "<table><tr><th>页面</th><th>标题</th><th class='num'>资源数</th></tr>"
            + "".join(page_rows)
            + "</table>"
        )
    else:
        pages_table = '<p class="empty">无页面数据</p>'

    # 失败明细
    failures_by_class = ctx.get("failures_by_class", {})
    assert isinstance(failures_by_class, dict)
    if failures_by_class:
        fail_parts = []
        for cls in sorted(failures_by_class, key=lambda c: len(failures_by_class[c]), reverse=True):
            items = failures_by_class[cls]
            fail_parts.append(f"<h3>{esc(_ERROR_LABELS.get(cls, cls))} ({len(items)} 条)</h3>")
            item_rows = []
            for item in items:
                assert isinstance(item, dict)
                diag = esc(item.get("diagnostic", "")) or "&nbsp;"
                item_rows.append(
                    f"<tr><td><code>{esc(item.get('status', ''))}</code></td>"
                    f"<td class='url'>{esc(item.get('url', ''))}</td>"
                    f"<td>{diag}</td></tr>"
                )
            fail_parts.append(
                "<table><tr><th>状态</th><th>URL</th><th>诊断</th></tr>"
                + "".join(item_rows)
                + "</table>"
            )
        fail_html = "".join(fail_parts)
    else:
        fail_html = '<p class="empty">无失败记录</p>'

    # 建议
    recs = ctx.get("recommendations", [])
    assert isinstance(recs, list)
    if recs:
        rec_items = []
        for rec in recs:
            assert isinstance(rec, dict)
            rec_items.append(
                f"<li><span class='tag {_tag_class(rec.get('priority', ''))}'>{esc(rec.get('priority', ''))}</span> "
                f"<span class='rtitle'>{esc(rec.get('title', ''))}</span>"
                f"<div class='rdetail'>{esc(rec.get('detail', ''))}</div></li>"
            )
        rec_html = '<ul class="recs">' + "".join(rec_items) + "</ul>"
    else:
        rec_html = '<p class="empty">无</p>'

    html = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>网页资源采集报告</title><style>" + _HTML_CSS + "</style></head><body>"
        "<div class='container'>"
        "<header><h1>网页资源采集报告</h1>"
        f"<div class='meta'>生成时间：{esc(ctx.get('generated_at', ''))}　·　"
        f"耗时：{esc(ctx.get('duration_human', '未知'))}　·　"
        f"扫描页面：{esc(ctx.get('pages_scanned', 0))}</div></header>"
        "<div class='cards'>" + "".join(card_html) + "</div>"
        "<section><h2>抓取配置</h2>" + config_table + "</section>"
        "<section><h2>按类别分布</h2>" + cat_table + "</section>"
        "<section><h2>按状态分布</h2>" + status_table + "</section>"
        "<section><h2>失败原因分类</h2>" + err_table + "</section>"
        "<section><h2>跳过原因分类</h2>" + skip_table + "</section>"
        "<section><h2>最大的文件（前 20）</h2>" + big_table + "</section>"
        "<section><h2>资源最多的页面（前 15）</h2>" + pages_table + "</section>"
        "<section><h2>失败资源明细</h2>" + fail_html + "</section>"
        "<section><h2>建议</h2>" + rec_html + "</section>"
        "<footer>本报告由网页资源采集器自动生成 · 详见 run_report.json / run_report.md</footer>"
        "</div></body></html>"
    )
    path.write_text(html, encoding="utf-8")


def diagnostic_for_status(status: str) -> str:
    lowered = status.lower()
    if "http error 401" in lowered or "unauthorized" in lowered:
        return "401 Unauthorized: check whether your Cookie/Authorization header is present and still valid."
    if "http error 403" in lowered or "forbidden" in lowered:
        return "403 Forbidden: check whether your account has access, and try adding Referer/User-Agent required by the site."
    if "http error 404" in lowered or "not found" in lowered:
        return "404 Not Found: the resource URL may be expired, relative to a different page, or no longer available."
    if "timed out" in lowered or "timeout" in lowered:
        return "Timeout: try increasing timeout/retries or adding a larger delay."
    if "file exceeds" in lowered:
        return "Skipped by max-bytes: increase the single-file size limit or set it to 0."
    if "robots.txt" in lowered:
        return "Skipped by robots.txt because respect-robots is enabled."
    if "encrypted playlist detected" in lowered:
        return "Encrypted playlist detected: recorded only; protected playlist entries are not expanded or decrypted."
    if "dedup" in lowered:
        return "Skipped: identical content (SHA256) already downloaded from a different URL."
    if status.startswith("error"):
        return (
            "Request failed: check network connectivity, URL validity, headers, and authorization."
        )
    return ""


def row_for(
    status: str,
    resource: Resource,
    saved_path: str,
    content_type: str,
    byte_count: int,
    page_titles: dict[str, str],
    sha256: str = "",
) -> ManifestRow:
    return ManifestRow(
        status=status,
        url=resource.url,
        saved_path=saved_path,
        content_type=content_type,
        bytes=byte_count,
        category=category_for(resource.url, content_type, resource.kind, resource.found_in),
        found_in=resource.found_in,
        kind=resource.kind,
        page_url=resource.page_url,
        page_title=page_titles.get(resource.page_url, ""),
        diagnostic=diagnostic_for_status(status),
        sha256=sha256,
    )


def rewrite_html(html: str, resources: list[ManifestRow], page_url: str, output_dir: Path) -> str:
    replacements = {
        row.url: Path(row.saved_path).resolve().relative_to(output_dir).as_posix()
        for row in resources
        if row.status == "ok" and row.saved_path
    }
    rewritten = html
    for original_url, local_path in sorted(
        replacements.items(), key=lambda item: len(item[0]), reverse=True
    ):
        rewritten = rewritten.replace(original_url, local_path)
        parsed = urlparse(original_url)
        if same_domain(original_url, page_url):
            rewritten = rewritten.replace(parsed.path, local_path)
    return rewritten


# ── 遮罩层/弹窗/模态框剥离 ────────────────────────────────────────

OVERLAY_PATTERNS = [
    # 遮罩层/模态框/弹窗/付费墙的常见 class/关键词模式
    (
        re.compile(
            r'<div[^>]*\bclass\s*=\s*["\'][^"\']*'
            r"(?:\bmodal\b|\boverlay\b|\bmask\b|\bshade\b|\bdialog\b"
            r"|\bpopup\b|\bpop-up\b|\blightbox\b|\bmodal-\b"
            r"|\bpaywall\b|\bsubscribe\b|\bsubscription\b"
            r"|\bnewsletter\b|\bpromo\b|\badvertisement\b|\bfloat-ad\b"
            r"|\binterstitial\b|\bfullscreen-[adio]"
            r"|\bcookie-banner\b|\bcookie-consent\b|\bnotice-bar\b"
            r"|\bmember-|\bvip-|\bvip_"
            r"|\blogin-box\b|\blogin-modal\b|\breg-modal\b"
            r"|\bguide-mask\b|\bguide-layer\b|\btip-overlay\b"
            r"|\bdownload-app\b|\bapp-download\b"
            r"|\btoast\b|\bnotice\b|\balert-\b"
            r"|\bvideo-ad\b|\bad-layer\b"
            r'|\bwx-\w+-modal\b)[^"\']*["\'][^>]*>'
            r".*?</div>",
            re.IGNORECASE | re.DOTALL,
        ),
        "div overlay/modal/popup",
    ),
    (
        re.compile(
            r'<div[^>]*\b(id|class)\s*=\s*["\'][^"\']*'
            r"(?:mask|overlay|shadeBg|shadowBg|bgShadow|modalBg|dialogBg)"
            r'[^"\']*["\'][^>]*>'
            r".*?</div>",
            re.IGNORECASE | re.DOTALL,
        ),
        "div mask BG",
    ),
    # fixed/sticky 定位的遮罩 div
    (
        re.compile(
            r'<div[^>]*\bstyle\s*=\s*["\'][^"\']*'
            r"(?:position\s*:\s*fixed|position\s*:\s*sticky)"
            r'[^"\']*["\'][^>]*'
            r">(?:<div[^>]*>.*?</div>\s*)*?</div>",
            re.IGNORECASE | re.DOTALL,
        ),
        "fixed/sticky div (dangerous, wide match)",
    ),
]


def strip_page_overlays(html: str, aggressive: bool = False) -> str:
    """移除页面标记中的遮罩层/弹窗/付费墙 HTML 元素。

    返回剔除已知遮罩元素后的 HTML。
    """
    result = html

    # 按模式移除
    for pattern, label in OVERLAY_PATTERNS:
        if not aggressive and "fixed/sticky" in label:
            continue  # 非激进模式跳过匹配过宽的正则
        result = pattern.sub("", result)

    # 按已知 ID 移除常见遮罩容器 —— 每个属性（id= / class=）各用一条编译好的
    # 交替正则，每个页面只扫描两次，而不是 2 * len(ids) ≈ 60 次。
    overlay_ids_alt = (
        "mask|overlay|shadow|shade|dialog|lightbox|modal|popup|subscribe|"
        "subscribe-box|signin|login|paywall|vip|member|register|download-app|"
        "cookie-notice|cookieConsent|announce|notice-bar|guide|guideLayer|"
        "tips|toast|alertBox|videoAd|playerAd|adContainer|floatingAd"
    )
    result = re.sub(
        rf'<div[^>]*\bid\s*=\s*["\']({overlay_ids_alt})["\'][^>]*>.*?</div>',
        "",
        result,
        flags=re.IGNORECASE | re.DOTALL,
    )
    result = re.sub(
        rf'<div[^>]*\bclass\s*=\s*["\'][^"\']*({overlay_ids_alt})[^"\']*["\'][^>]*>.*?</div>',
        "",
        result,
        flags=re.IGNORECASE | re.DOTALL,
    )

    return result


# ── 智能数据抽取 ────────────────────────────────────────────────

EXTRACTED_DATA_FIELDS = [
    "page_url",
    "page_title",
    "og_title",
    "og_description",
    "og_image",
    "og_video",
    "meta_description",
    "meta_keywords",
    "h1_count",
    "h2_count",
    "h3_count",
    "link_count",
    "image_count",
    "video_count",
    "text_length",
    "has_canonical",
]


def smart_extract(html: str, page_url: str) -> dict[str, object]:
    """自动从页面抽取结构化数据。

    返回含常见元数据字段的 dict；无需配置即可用于任意 HTML 页面。
    """
    result: dict[str, object] = {
        "page_url": page_url,
        "page_title": extract_title(html),
    }

    # Open Graph / Twitter / Meta 标签
    og_patterns = {
        "og_title": (r'<meta\s+property=["\']og:title["\'][^>]*content=["\']([^"\']*)["\']', False),
        "og_description": (
            r'<meta\s+property=["\']og:description["\'][^>]*content=["\']([^"\']*)["\']',
            False,
        ),
        "og_image": (r'<meta\s+property=["\']og:image["\'][^>]*content=["\']([^"\']*)["\']', False),
        "og_video": (
            r'<meta\s+property=["\']og:video["\'](?:[^>]*content=["\']([^"\']*)["\'])',
            False,
        ),
    }
    for key, (pattern, _) in og_patterns.items():
        match = re.search(pattern, html, re.IGNORECASE)
        result[key] = match.group(1) if match else ""

    # Meta description（回退：og:description）
    md = re.search(
        r'<meta\s+name=["\']description["\'][^>]*content=["\']([^"\']*)["\']', html, re.IGNORECASE
    )
    result["meta_description"] = md.group(1) if md else result.get("og_description", "")

    # Meta 关键词
    mk = re.search(
        r'<meta\s+name=["\']keywords["\'][^>]*content=["\']([^"\']*)["\']', html, re.IGNORECASE
    )
    result["meta_keywords"] = mk.group(1) if mk else ""

    # 标题计数
    result["h1_count"] = len(re.findall(r"<h1[>\s]", html, re.IGNORECASE))
    result["h2_count"] = len(re.findall(r"<h2[>\s]", html, re.IGNORECASE))
    result["h3_count"] = len(re.findall(r"<h3[>\s]", html, re.IGNORECASE))

    # 链接/图片/视频计数
    result["link_count"] = len(re.findall(r"<a\s+", html, re.IGNORECASE))
    result["image_count"] = len(re.findall(r"<img\s+", html, re.IGNORECASE))
    result["video_count"] = len(re.findall(r"<video\s+", html, re.IGNORECASE))

    # 文本长度（近似值）
    text_stripped = re.sub(r"<[^>]+>", "", html)
    text_stripped = re.sub(r"\s+", " ", text_stripped).strip()
    result["text_length"] = len(text_stripped)

    # 规范链接（canonical URL）
    canonical = re.search(
        r'<link\s+rel=["\']canonical["\'][^>]*href=["\']([^"\']*)["\']', html, re.IGNORECASE
    )
    result["has_canonical"] = bool(canonical)

    return result


def write_extracted_data(output_dir: Path, data: list[dict[str, object]]) -> None:
    """把抽取数据写入 JSON 与 CSV。"""
    json_path = output_dir / "extracted_data.json"
    json_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    csv_path = output_dir / "extracted_data.csv"
    if data:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(data[0].keys()))
            writer.writeheader()
            writer.writerows(data)
    _log.info("extracted data: %s, %s", json_path, csv_path)


def extract_readable_text(html: str) -> str:
    """用简单启发式从 HTML 抽取可读的正文/主要文本。"""
    # 优先尝试 <article>
    m = re.search(r"<article[^>]*>(.*?)</article>", html, re.DOTALL | re.IGNORECASE)
    if m:
        html = m.group(1)
    else:
        # 尝试常见内容容器
        for selector in (
            'id="content"',
            'id="article"',
            'id="main-content"',
            'id="post-content"',
            'class="content"',
            'class="post-content"',
            'class="article-content"',
            'class="entry-content"',
            'class="main-content"',
        ):
            pattern = rf"<div[^>]*{selector}[^>]*>(.*?)</div>"
            m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if m:
                html = m.group(1)
                break
        else:
            # 兜底：<body>
            m = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
            if m:
                html = m.group(1)

    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    # 去掉过短的行（多为导航/模板文字）
    lines = [line.strip() for line in text.split("\n") if len(line.strip()) > 30]
    return "\n".join(lines) if lines else text

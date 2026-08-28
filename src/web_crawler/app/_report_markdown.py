"""摘要与 Markdown / 运行报告写入器。

从 :mod:`app.crawler_report` 拆出：``write_summary`` 写人类可读的
summary.txt 摘要，``write_run_report`` 汇总写出 run_report.json /
run_report.md / run_report.html 三种格式，``_write_markdown_report``
负责 Markdown 渲染。

依赖 :mod:`app._report_context` 提供上下文构建与建议，
依赖 :mod:`app._report_html` / :mod:`app._report_manifests` 提供
HTML 渲染与错误分类标签。
"""

from __future__ import annotations

import json
from pathlib import Path

from web_crawler.app._report_context import build_recommendations, build_report_context
from web_crawler.app._report_html import _write_html_report
from web_crawler.app._report_manifests import _ERROR_LABELS
from web_crawler.app.crawler_models import ManifestRow


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

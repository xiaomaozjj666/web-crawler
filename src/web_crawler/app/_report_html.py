"""HTML 报告渲染与行构造。

从 :mod:`app.crawler_report` 拆出：``_write_html_report`` 把报告上下文
渲染成自包含的单文件 HTML（内嵌 ``_HTML_CSS`` 样式），``_tag_class``
把建议优先级映射为 CSS tag 类；``diagnostic_for_status`` 与 ``row_for``
负责把下载结果构造为清单行（含失败诊断文案）。

依赖 :mod:`app._report_manifests` 提供错误分类标签。
"""

from __future__ import annotations

import html as html_lib
from pathlib import Path

from web_crawler.app._report_manifests import _ERROR_LABELS
from web_crawler.app.crawler_models import ManifestRow, Resource
from web_crawler.app.crawler_net import category_for

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

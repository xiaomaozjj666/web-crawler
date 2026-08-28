"""报告上下文构建与建议生成。

从 :mod:`app.crawler_report` 拆出：``build_report_context`` 把抓取结果
汇总成 JSON / Markdown / HTML 三种报告共用的上下文字典；
``build_recommendations`` 根据上下文生成带优先级的可操作建议。

依赖 :mod:`app._report_manifests` 提供格式化与错误分类工具。
"""

from __future__ import annotations

import time

from web_crawler.app._report_manifests import _format_bytes, classify_error, format_duration
from web_crawler.app.crawler_models import ManifestRow

# pycryptodome 是否可用（build_recommendations 的加密提示文案用）。
# 与 app.crawler 中的探测保持一致。
try:
    from Crypto.Cipher import AES as _AES  # noqa: F401 - 仅探测 pycryptodome 可用性

    HAS_AES = True  # pragma: no cover - pycryptodome 未安装时不可达
except ImportError:
    HAS_AES = False


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

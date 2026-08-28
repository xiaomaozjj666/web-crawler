"""清单写入器与格式化/错误分类纯函数。

从 :mod:`app.crawler_report` 拆出的叶子模块：CSV/JSON 清单文件对写入、
字节/耗时的人类可读格式化、错误大类归类（``classify_error`` 与
``_ERROR_CLASSES``/``_ERROR_LABELS`` 常量）。

本模块只依赖 :mod:`app.crawler_models` 与 :mod:`app.crawler_net`，
供 ``_report_context`` / ``_report_markdown`` / ``_report_html`` 复用。
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from web_crawler.app.crawler_models import ManifestRow
from web_crawler.app.crawler_net import is_video_resource

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

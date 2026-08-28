"""网页资源爬虫的报告/格式化层（facade）。

原单文件已按域拆分为下划线私有模块（与 ``mcp/server.py``、``app/ui.py``
同型拆分）：

- :mod:`web_crawler.app._report_manifests` — 清单写入器（CSV/JSON 文件对）、
  字节/耗时格式化、错误分类（``classify_error`` 与 ``_ERROR_CLASSES``/
  ``_ERROR_LABELS`` 常量）
- :mod:`web_crawler.app._report_context` — 报告上下文构建
  （``build_report_context``）与建议生成（``build_recommendations``）
- :mod:`web_crawler.app._report_markdown` — summary.txt 摘要与
  run_report.json / run_report.md / run_report.html 三格式写入器
- :mod:`web_crawler.app._report_html` — HTML 报告渲染与清单行构造
  （``row_for`` / ``diagnostic_for_status``）
- :mod:`web_crawler.app._report_extract` — 离线 HTML 重写、遮罩层剥离、
  智能数据抽取与正文抽取

本模块仅 re-export 全部历史导入路径：``app.crawler`` 的
``from web_crawler.app.crawler_report import *``、``app._crawler_post`` /
``app._crawler_download`` 的按名导入，以及测试对 ``cr.xxx`` 的访问与
``patch.object(cr, ...)`` 语义均不受影响。

本模块绝不导入 ``app.crawler``（否则会循环依赖）；依赖
:mod:`app.crawler_models` 提供共享数据类，依赖 :mod:`app.crawler_net`
提供分类/解析工具。
"""

from __future__ import annotations

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

# -- 从子模块汇入(保持历史导入路径可用) ------------------------------------
from web_crawler.app._report_context import (  # noqa: F401
    HAS_AES,  # pycryptodome 探测结果（build_recommendations 加密提示文案用）
    build_recommendations,
    build_report_context,
)
from web_crawler.app._report_extract import (
    EXTRACTED_DATA_FIELDS,
    OVERLAY_PATTERNS,
    extract_readable_text,
    rewrite_html,
    smart_extract,
    strip_page_overlays,
    write_extracted_data,
)
from web_crawler.app._report_html import (
    _HTML_CSS,
    _tag_class,
    _write_html_report,
    diagnostic_for_status,
    row_for,
)
from web_crawler.app._report_manifests import (
    _ERROR_CLASSES,
    _ERROR_LABELS,
    FIELD_NAMES,
    _format_bytes,
    _write_manifest_pair,
    classify_error,
    format_duration,
    is_failed_row,
    write_failed_manifests,
    write_manifests,
    write_video_manifests,
)
from web_crawler.app._report_markdown import (
    _write_markdown_report,
    write_run_report,
    write_summary,
)

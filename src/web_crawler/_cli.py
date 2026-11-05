"""六个命令行入口共用的输出、进度与错误展示工具。

设计约束：
* 正常结果写 stdout，诊断与进度写 stderr，``--json`` 的 stdout 永远是单个 JSON 文档；
* TTY 进度原地刷新，重定向/CI 等非 TTY 场景退化为节流日志；
* 本模块仅依赖标准库，避免给轻量 CLI 启动增加重型依赖。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal, TextIO

from web_crawler.exceptions import ConfigError, FetchError, ParseError, UnsafeTargetError

OutputMode = Literal["normal", "quiet", "verbose", "json"]


def add_output_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """为 CLI 添加互斥的 ``--quiet/--verbose/--json`` 输出模式参数。"""
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-q", "--quiet", action="store_true", help="仅输出错误与命令结果")
    group.add_argument("-v", "--verbose", action="store_true", help="输出调试日志与进度上下文")
    group.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="以机器可解析 JSON 输出结果（诊断写 stderr）",
    )
    return parser


def output_mode(args: argparse.Namespace | object | None) -> OutputMode:
    """读取参数对象的输出模式；程序内构造的旧 Namespace 自动按 normal 处理。"""
    if getattr(args, "json_output", False):
        return "json"
    if getattr(args, "quiet", False):
        return "quiet"
    if getattr(args, "verbose", False):
        return "verbose"
    return "normal"


def configure_logging(args: argparse.Namespace | object | None) -> None:
    """按统一输出模式调整根日志与 crawler 日志级别。"""
    level = {
        "quiet": logging.ERROR,
        "verbose": logging.DEBUG,
        "json": logging.ERROR,
        "normal": logging.INFO,
    }[output_mode(args)]
    logging.getLogger().setLevel(level)
    logging.getLogger("crawler").setLevel(level)


@dataclass(frozen=True)
class ErrorInfo:
    """分级错误及其可执行修复建议。"""

    category: str
    label: str
    message: str
    hint: str


def classify_exception(exc: BaseException) -> ErrorInfo:
    """把网络、解析、配置和内部错误转换为一致的用户提示。"""
    message = str(exc) or type(exc).__name__
    if isinstance(exc, UnsafeTargetError):
        return ErrorInfo(
            "security",
            "安全拦截",
            message,
            "改用已获授权且可公开访问的 http(s) URL；不要抓取未授权的内网目标。",
        )
    if isinstance(exc, ParseError):
        return ErrorInfo(
            "parse",
            "解析错误",
            message,
            "检查响应类型、页面结构与选择器；必要时调整 --encoding，并用 --verbose 查看上下文。",
        )
    if isinstance(exc, ConfigError | json.JSONDecodeError | UnicodeError | ValueError):
        return ErrorInfo(
            "configuration",
            "配置错误",
            message,
            "按提示修正命令行参数或 JSON 配置；使用 --help 查看合法示例。",
        )
    module = type(exc).__module__.split(".", 1)[0]
    if isinstance(exc, FetchError | OSError | TimeoutError) or module in {
        "httpx",
        "httpcore",
        "requests",
        "urllib3",
    }:
        return ErrorInfo(
            "network",
            "网络错误",
            message,
            "检查 URL、DNS、网络、代理与证书；遇到限流可提高 --delay 或 --retries 后重试。",
        )
    return ErrorInfo(
        "internal",
        "运行错误",
        message,
        "使用 --verbose 获取详细日志；若可复现，请附最小命令、版本号与目标行为提交 issue。",
    )


def render_error(exc: BaseException, *, context: str = "") -> str:
    """渲染带级别和下一步操作的多行错误；``context`` 可附 URL/文件路径。"""
    info = classify_exception(exc)
    prefix = f"[{info.label}]"
    location = f" {context}：" if context else " "
    return f"{prefix}{location}{info.message}\n下一步：{info.hint}"


def emit_json(payload: object, *, stream: TextIO | None = None) -> None:
    """输出单个紧凑 JSON 文档，非标准类型以字符串兜底。"""
    target = sys.stdout if stream is None else stream
    target.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
    target.flush()


def emit_error(
    exc: BaseException,
    args: argparse.Namespace | object | None = None,
    *,
    context: str = "",
) -> None:
    """按当前模式输出错误；JSON 模式仍保证 stdout 只有一个 JSON 文档。"""
    if output_mode(args) == "json":
        info = classify_exception(exc)
        emit_json(
            {
                "status": "error",
                "error": {
                    "type": info.category,
                    "message": info.message,
                    "hint": info.hint,
                    **({"context": context} if context else {}),
                },
            }
        )
        return
    print(render_error(exc, context=context), file=sys.stderr)


def format_bytes(size: float) -> str:
    """以二进制单位格式化数据量。"""
    value = float(max(0, size))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"  # pragma: no cover - 循环终点已覆盖


class ProgressReporter:
    """线程安全的爬取进度显示器。

    TTY 使用 ``\\r`` 动态刷新；非 TTY 默认每两秒输出一次，并在首条/末条
    强制输出，避免 CI 日志被每个资源刷屏。显示失败不会中断爬取。
    """

    def __init__(
        self,
        mode: OutputMode = "normal",
        *,
        stream: TextIO | None = None,
        interval: float = 2.0,
        clock: Any = time.monotonic,
    ) -> None:
        self.mode = mode
        self.stream = sys.stderr if stream is None else stream
        self.interval = max(0.0, interval)
        self.clock = clock
        self.started_at = float(clock())
        self._last_render = 0.0
        self._active = False
        self._lock = threading.Lock()

    def update(self, payload: dict[str, Any]) -> None:
        """消费 crawler 的 progress payload 并按输出设备刷新。"""
        if self.mode in ("quiet", "json"):
            return
        phase = str(payload.get("phase", ""))
        if phase == "page":
            label = "扫描页面"
            completed = int(payload.get("pages_scanned", 0))
            total = int(payload.get("total_pages", 0))
        else:
            label = "处理资源"
            completed = int(payload.get("processed_resources", 0))
            total = int(payload.get("total_resources", 0))
        failed = int(payload.get("failed_resources", 0))
        byte_count = int(payload.get("bytes_downloaded", 0))
        now = float(self.clock())
        elapsed = max(0.001, now - self.started_at)
        rate = completed / elapsed
        total_text = str(total) if total > 0 else "?"
        text = (
            f"{label} {completed}/{total_text} | {rate:.1f} 项/s | "
            f"失败 {failed} | 数据 {format_bytes(byte_count)}"
        )
        if self.mode == "verbose" and payload.get("current_url"):
            text += f" | {payload['current_url']}"
        is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        is_final = total > 0 and completed >= total
        with self._lock:
            due = now - self._last_render >= (0.1 if is_tty else self.interval)
            if self._active and not due and not is_final:
                return
            try:
                if is_tty:
                    self.stream.write(f"\r\x1b[K{text}")
                else:
                    self.stream.write(f"[进度] {text}\n")
                self.stream.flush()
            except (OSError, ValueError):
                return
            self._active = True
            self._last_render = now

    def close(self) -> None:
        """结束 TTY 动态行，避免后续摘要与进度粘在同一行。"""
        with self._lock:
            if not self._active:
                return
            if bool(getattr(self.stream, "isatty", lambda: False)()):
                try:
                    self.stream.write("\n")
                    self.stream.flush()
                except (OSError, ValueError):
                    pass
            self._active = False


def crawl_summary(
    *,
    pages: int,
    resources: int,
    rows: list[Any],
    started_at: float,
    output_dir: str,
    cancelled: bool = False,
) -> dict[str, Any]:
    """从 ManifestRow 列表构造稳定的机器/人类共用摘要。"""
    succeeded = sum(1 for row in rows if str(row.status).startswith("ok"))
    failed = sum(1 for row in rows if str(row.status).startswith("error"))
    skipped = max(0, len(rows) - succeeded - failed)
    byte_count = sum(int(row.bytes) for row in rows if str(row.status).startswith("ok"))
    status = "cancelled" if cancelled else ("partial" if failed else "success")
    return {
        "schema_version": 1,
        "command": "web-crawler",
        "status": status,
        "pages_scanned": pages,
        "resources_found": resources,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "duration_seconds": round(max(0.0, time.time() - started_at), 3),
        "bytes": byte_count,
        "output_dir": output_dir,
    }


def emit_crawl_summary(
    summary: dict[str, Any],
    args: argparse.Namespace | object | None = None,
    *,
    stream: TextIO | None = None,
) -> None:
    """输出结构化摘要；quiet 静默，JSON 紧凑单行，其余模式人读友好。"""
    mode = output_mode(args)
    if mode == "quiet":
        return
    target = sys.stdout if stream is None else stream
    if mode == "json":
        emit_json(summary, stream=target)
        return
    title = {"success": "爬取完成", "partial": "爬取完成（有失败）", "cancelled": "爬取已取消"}.get(
        str(summary.get("status")), "爬取结束"
    )
    target.write(
        f"\n{title}\n"
        f"  成功/失败/跳过：{summary['succeeded']}/{summary['failed']}/{summary['skipped']}\n"
        f"  页面/资源：{summary['pages_scanned']}/{summary['resources_found']}\n"
        f"  耗时：{summary['duration_seconds']:.3f}s\n"
        f"  数据量：{format_bytes(int(summary['bytes']))}\n"
        f"  输出目录：{summary['output_dir']}\n"
    )
    target.flush()

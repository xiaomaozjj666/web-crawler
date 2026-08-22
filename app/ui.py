#!/usr/bin/env python3
"""Local web UI for crawler.py."""

from __future__ import annotations

import argparse
import contextlib
import io
import ipaddress
import json
import logging
import os
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

sys.dont_write_bytecode = True
# 确保 app 目录在 sys.path 中，无论作为脚本运行还是作为模块导入
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
_log = logging.getLogger(__name__)

import crawler as web_resource_crawler  # 需先设 sys.dont_write_bytecode 再导入
import db as database

HOST = "127.0.0.1"
PORT = 8765
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = os.path.join(os.getcwd(), "crawler_output")
DEFAULT_BLOCK_KEYWORDS = (
    "ads, adservice, adserver, adclick, doubleclick, googlesyndication, "
    "google-analytics, banner, promo, promotion, popup, popunder, modal, "
    "overlay, interstitial, floating, float-ad, layer-ad, dialog-ad, lightbox, "
    "subscribe, webpush, push-notification, affiliate, tracker, tracking, "
    "analytics, tongji, stat, hm.baidu, cnzz, umeng, "
    "recaptcha, captcha, hcaptcha, turnstile, challenge, verification, "
    "verify, security-check, bot-detect, botdetect"
)
JOBS: dict[str, JobState] = {}
JOBS_LOCK = threading.Lock()
MAX_JOBS = 50


def _is_loopback_host(host: str) -> bool:
    """只允许绑定回环地址（127.x / ::1 / localhost），防止控制面暴露到局域网。"""
    if host in ("localhost", "::1", "127.0.0.1"):
        return True
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback
    except ValueError:
        return False


def _open_folder(path: str) -> None:
    """跨平台打开文件夹（Windows explorer / macOS open / Linux xdg-open）。"""
    import sys as _sys

    if _sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif _sys.platform == "darwin":  # pragma: no cover
        import subprocess

        subprocess.Popen(["open", path])
    else:  # pragma: no cover
        import subprocess

        subprocess.Popen(["xdg-open", path])


# JS 逆向 Agent 任务注册表
REVERSE_JOBS: dict[str, ReverseJobState] = {}
REVERSE_JOBS_LOCK = threading.Lock()
MAX_REVERSE_JOBS = 20


def _as_int(value: object, default: int) -> int:
    """把 payload 中的数值安全转为 int;非数字/None 回退默认值。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


def _as_float(value: object, default: float) -> float:
    """把 payload 中的数值安全转为 float;非数字/None 回退默认值。"""
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return default


@dataclass
class JobState:
    id: str
    args: argparse.Namespace
    output_dir: str
    log: str = ""
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    total_resources: int = 0
    processed_resources: int = 0
    current_url: str = ""
    pages_scanned: int = 0
    exit_code: int | None = None
    pause_event: threading.Event = field(default_factory=threading.Event)
    stop_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.pause_event.set()

    def append(self, text: str) -> None:
        with self.lock:
            self.log += text
            self.log = self.log[-80000:]

    def progress(self, payload: dict[str, object]) -> None:
        with self.lock:
            self.total_resources = _as_int(
                payload.get("total_resources", self.total_resources), self.total_resources
            )
            self.processed_resources = _as_int(
                payload.get("processed_resources", self.processed_resources),
                self.processed_resources,
            )
            self.current_url = str(payload.get("current_url", self.current_url) or "")
            self.pages_scanned = _as_int(
                payload.get("pages_scanned", self.pages_scanned), self.pages_scanned
            )

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            percent = 0
            if self.total_resources:
                percent = min(100, int(self.processed_resources * 100 / self.total_resources))
            return {
                "id": self.id,
                "status": self.status,
                "log": self.log,
                "total_resources": self.total_resources,
                "processed_resources": self.processed_resources,
                "current_url": self.current_url,
                "pages_scanned": self.pages_scanned,
                "percent": percent,
                "output_dir": self.output_dir,
                "exit_code": self.exit_code,
            }


@dataclass
class ReverseJobState:
    """JS 逆向 Agent 任务的实时状态。"""

    id: str
    url: str
    task: str
    config: dict  # ReverseAgentConfig 的可序列化形式
    status: str = "running"  # running / done / error / cancelled
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None  # 进入终态的时间（前端据此展示固定耗时）
    current_step: int = 0
    max_steps: int = 20

    # 实时事件流（保留最近 200 条，避免内存爆炸）
    events: list[dict] = field(default_factory=list)
    # state_lock 保护所有共享可变字段（events/steps/.../status），
    # 避免子线程写入与主线程 snapshot 读取之间的数据竞争。
    # 使用 RLock 支持嵌套获取（append_event 在锁内被调用时不死锁）
    state_lock: Any = field(default_factory=threading.RLock)

    # 步骤列表（每个 step 一条，含 action_type/reasoning/duration/confidence）
    steps: list[dict] = field(default_factory=list)

    # 当前观察
    current_observation: dict = field(default_factory=dict)

    # 置信度
    last_confidence: dict = field(default_factory=dict)  # {score, reasons, action_type}

    # 护栏
    guard_blocks: list[dict] = field(default_factory=list)

    # Hook 捕获
    hook_records: list[dict] = field(default_factory=list)
    hook_count: int = 0
    network_requests: list[dict] = field(default_factory=list)

    # 目标参数发现状态
    target_params: list[str] = field(default_factory=list)
    target_params_found: dict[str, str] = field(default_factory=dict)

    # Checkpoints
    checkpoints: list[dict] = field(default_factory=list)

    # 最终结果
    success: bool = False
    analysis: str = ""
    compiled_script: str = ""
    judge_result: dict = field(default_factory=dict)

    # 截图列表（每项含 step/path/ts；错误截图标记 error=True）
    screenshots: list[dict] = field(default_factory=list)
    # 最近一次错误截图路径（供前端高亮展示）
    error_screenshot: str = ""
    # 每步耗时（秒），与 steps 列表对齐
    step_durations: list[float] = field(default_factory=list)

    # 控制
    stop_event: threading.Event = field(default_factory=threading.Event)
    exit_code: int | None = None
    error: str = ""

    # 内部：步号 → 起始时间戳，用于计算单步耗时
    _step_starts: dict = field(default_factory=dict)

    def append_event(self, event: dict) -> None:
        """追加一条事件到流，保留最近 200 条。"""
        with self.state_lock:
            self.events.append(event)
            if len(self.events) > 200:
                self.events = self.events[-200:]

    def events_since(self, ts: float) -> list[dict]:
        """返回 ts 之后的所有事件（增量查询用）。"""
        with self.state_lock:
            return [e for e in self.events if e.get("ts", 0) > ts]

    def clear_runtime(self) -> None:
        """清空运行时事件/步骤/快照（保留最终结果）。"""
        with self.state_lock:
            self.events = []
            self.steps = []
            self.current_observation = {}
            self.guard_blocks = []
            self.hook_records = []
            self.hook_count = 0
            self.network_requests = []
            self.checkpoints = []
            self.screenshots = []
            self.error_screenshot = ""
            self.step_durations = []
            self._step_starts = {}

    def snapshot(self) -> dict[str, object]:
        """返回可 JSON 序列化的完整状态快照。"""
        with self.state_lock:
            events_copy = list(self.events)
            steps_copy = list(self.steps)
            current_observation_copy = dict(self.current_observation)
            last_confidence_copy = dict(self.last_confidence)
            guard_blocks_copy = list(self.guard_blocks)
            hook_records_copy = list(self.hook_records[-50:])
            hook_count_copy = self.hook_count
            network_requests_copy = list(self.network_requests[-20:])
            network_count_copy = len(self.network_requests)
            target_params_copy = list(self.target_params)
            target_params_found_copy = dict(self.target_params_found)
            checkpoints_copy = list(self.checkpoints)
            success_copy = self.success
            analysis_copy = self.analysis
            compiled_script_copy = self.compiled_script
            judge_result_copy = dict(self.judge_result)
            screenshots_copy = list(self.screenshots)
            error_screenshot_copy = self.error_screenshot
            step_durations_copy = list(self.step_durations)
            status_copy = self.status
            current_step_copy = self.current_step
            exit_code_copy = self.exit_code
            error_copy = self.error
            finished_at_copy = self.finished_at
        # 计算平均步时（毫秒）—— 锁外计算避免长持有
        durations_ms = [d * 1000.0 for d in step_durations_copy if d >= 0]
        avg_step_ms = sum(durations_ms) / len(durations_ms) if durations_ms else 0.0
        return {
            "id": self.id,
            "url": self.url,
            "task": self.task,
            "status": status_copy,
            "created_at": self.created_at,
            "current_step": current_step_copy,
            "max_steps": self.max_steps,
            "events": events_copy,
            "steps": steps_copy,
            "current_observation": current_observation_copy,
            "last_confidence": last_confidence_copy,
            "guard_blocks": guard_blocks_copy,
            "hook_records": hook_records_copy,
            "hook_count": hook_count_copy,
            "network_requests": network_requests_copy,
            "network_count": network_count_copy,
            "target_params": target_params_copy,
            "target_params_found": target_params_found_copy,
            "checkpoints": checkpoints_copy,
            "success": success_copy,
            "analysis": analysis_copy,
            "compiled_script": compiled_script_copy,
            "judge_result": judge_result_copy,
            "screenshots": screenshots_copy,
            "error_screenshot": error_screenshot_copy,
            "step_durations": step_durations_copy,
            "avg_step_ms": round(avg_step_ms, 1),
            "exit_code": exit_code_copy,
            "error": error_copy,
            "finished_at": finished_at_copy,
        }

    def job_summary(self) -> dict[str, object]:
        """返回任务列表所需的最小字段（用于 /reverse/jobs）。"""
        return {
            "id": self.id,
            "url": self.url,
            "task": self.task,
            "status": self.status,
            "created_at": self.created_at,
            "current_step": self.current_step,
            "max_steps": self.max_steps,
            "success": self.success,
        }


class JobWriter(io.StringIO):
    def __init__(self, job: JobState) -> None:
        super().__init__()
        self.job = job

    def write(self, text: str) -> int:
        self.job.append(text)
        return len(text)

    def flush(self) -> None:
        return


class JobLogHandler(logging.Handler):
    """把 crawler logger 的日志记录转发到 job.log。

    替代失效的进程级 redirect_stdout/redirect_stderr:logging 的 StreamHandler
    在 import 时就绑定了原始 stderr,redirect 对它无效;而 crawler 的全部输出
    都走 logging,因此直接挂一个自定义 handler 转发最可靠。
    """

    def __init__(self, job: JobState) -> None:
        super().__init__()
        self.job = job

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.job.append(self.format(record) + "\n")
        except Exception:  # pragma: no cover - 防御性：日志失败不能影响任务
            self.handleError(record)


# 前端页面模板：独立文件便于维护（app/static/index.html），运行时读取。
# 模板含 {block_keywords} 占位符，由 do_GET 在响应时替换。
_PAGE_TEMPLATE_PATH = Path(__file__).resolve().parent / "static" / "index.html"


def _load_page_template() -> str:
    """读取前端模板；文件缺失时返回占位页（打包异常时兜底）。"""
    try:
        return _PAGE_TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - 仅打包缺失时触发
        return (
            "<!doctype html><html><body>"
            "<h1>模板缺失</h1><p>缺少 app/static/index.html，请重新安装。</p>"
            "</body></html>"
        )


PAGE = _load_page_template()


def output_path(value: str) -> str:
    path = Path(value or DEFAULT_OUTPUT)
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path.resolve())


def header_values(form: dict[str, list[str]]) -> list[str]:
    values: list[str] = []
    cookie = form.get("cookie", [""])[0].strip()
    referer = form.get("referer", [""])[0].strip() or form.get("url", [""])[0].strip()
    extra = form.get("headers", [""])[0]
    if cookie:
        values.append(f"Cookie: {cookie}")
    if referer:
        values.append(f"Referer: {referer}")
    for line in extra.splitlines():
        line = line.strip()
        if line:
            values.append(line)
    return values


def _validate_int_field(
    name: str, raw: str, default: int, minimum: int, maximum: int | None
) -> int:
    """解析并校验表单整数字段；非法值抛 ValueError（由 handler 转 JSON 错误）。"""
    text = (raw or "").strip()
    if not text:
        return default
    try:
        parsed = int(text)
    except ValueError:
        raise ValueError(f"{name} 必须是整数") from None
    if parsed < minimum or (maximum is not None and parsed > maximum):
        limit = f"{minimum}~{maximum}" if maximum is not None else f">={minimum}"
        raise ValueError(f"{name} 超出范围（{limit}）")
    return parsed


def _validate_float_field(name: str, raw: str, default: float, minimum: float) -> float:
    """解析并校验表单浮点字段；非法值抛 ValueError。"""
    text = (raw or "").strip()
    if not text:
        return default
    try:
        parsed = float(text)
    except ValueError:
        raise ValueError(f"{name} 必须是数字") from None
    if parsed < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return parsed


def build_args(form: dict[str, list[str]]) -> argparse.Namespace:
    def value(name: str, default: str = "") -> str:
        return form.get(name, [default])[0]

    def checked(name: str) -> bool:
        return name in form

    # 服务端范围校验（非法值在 parse_args 前拦截,避免 SystemExit 杀死 handler 线程）
    workers = _validate_int_field("workers", value("workers", "8"), 8, 1, 64)
    retries = _validate_int_field("retries", value("retries", "2"), 2, 0, None)
    timeout = _validate_int_field("timeout", value("timeout", "30"), 30, 1, None)
    max_pages = _validate_int_field("max_pages", value("max_pages", "1"), 1, 1, None)
    max_bytes = _validate_int_field("max_bytes", value("max_bytes", "0"), 0, 0, None)
    delay = _validate_float_field("delay", value("delay", "0.5"), 0.5, 0.0)

    out_val = value("out", "")
    if not out_val:
        out_val = DEFAULT_OUTPUT
    args = web_resource_crawler.build_parser().parse_args(
        [
            "--url",
            value("url"),
            "--out",
            output_path(out_val),
            "--max-pages",
            str(max_pages),
            "--workers",
            str(workers),
            "--delay",
            str(delay),
            "--timeout",
            str(timeout),
            "--retries",
            str(retries),
            "--max-bytes",
            str(max_bytes),
            "--block-keyword",
            value("block_keywords", DEFAULT_BLOCK_KEYWORDS),
        ]
    )
    args.header = header_values(form)
    args.same_domain = checked("same_domain")
    args.include_css_urls = checked("include_css_urls")
    args.video_mode = checked("video_mode")
    args.video_only = checked("video_only")
    args.list_only = checked("list_only")
    args.expand_playlists = checked("expand_playlists")
    args.resume = checked("resume")
    args.organize = checked("organize")
    args.dedup = checked("dedup")
    args.sitemap = checked("sitemap")
    args.strip_overlays = checked("strip_overlays")
    args.rewrite_html = checked("rewrite_html")
    args.smart_extract = checked("smart_extract")
    args.resume_crawl = checked("resume_crawl")
    args.extract_text = checked("extract_text")
    args.crawl_pages = checked("crawl_pages")
    args.respect_robots = checked("respect_robots")
    args.stealth = checked("stealth")
    args.save_config = ""
    args.load_config = ""
    return args


# 入库配置白名单：显式列出可序列化字段，剔除 header（含 Cookie/Authorization）等敏感项
_DB_CONFIG_FIELDS = (
    "url",
    "out",
    "max_pages",
    "workers",
    "delay",
    "timeout",
    "retries",
    "max_bytes",
    "same_domain",
    "include_css_urls",
    "video_mode",
    "video_only",
    "list_only",
    "expand_playlists",
    "resume",
    "organize",
    "dedup",
    "sitemap",
    "strip_overlays",
    "rewrite_html",
    "smart_extract",
    "resume_crawl",
    "extract_text",
    "crawl_pages",
    "respect_robots",
    "stealth",
)


def _task_config_for_db(args: argparse.Namespace) -> dict[str, object]:
    """把任务参数序列化为可入库 dict（白名单字段,不含 Cookie 等敏感头）。"""
    return {name: getattr(args, name) for name in _DB_CONFIG_FIELDS}


def run_job(job: JobState) -> None:
    job.args.wait_if_paused = lambda: wait_for_resume(job)
    job.args.should_stop = job.stop_event.is_set
    job.args.progress_callback = job.progress
    writer = JobWriter(job)
    log_handler = JobLogHandler(job)
    log_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    # 挂载 crawler 日志 handler：crawler 的全部输出走 logging,redirect_stdout 对其无效
    web_resource_crawler.attach_log_handler(log_handler)
    try:
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            code = web_resource_crawler.crawl(job.args)
        with job.lock:
            # 取消任务的退出码统一为 1（crawl 已返回 1,此处兜底确保语义一致）
            job.exit_code = 1 if job.stop_event.is_set() else code
            job.status = "cancelled" if job.stop_event.is_set() else "done"
        report_html = Path(job.output_dir) / "run_report.html"
        report_md = Path(job.output_dir) / "run_report.md"
        job.append(f"\n完成，退出码：{code}\n输出目录：{job.output_dir}\n")
        if report_html.exists():
            job.append(f"可视化报告：{report_html}\n")
        if report_md.exists():
            job.append(f"Markdown 报告：{report_md}\n")
    except Exception as exc:
        with job.lock:
            job.exit_code = 1
            job.status = "error"
        job.append(f"\n任务出错：{exc}\n")
    finally:
        # 卸载日志 handler：任务结束后 crawler 日志不再写入该 job
        web_resource_crawler.detach_log_handler(log_handler)
        # 持久化到数据库
        try:
            database.update_task_status(
                job.id,
                job.status,
                exit_code=job.exit_code,
                log=job.log,
                total_resources=job.total_resources,
                processed_resources=job.processed_resources,
                pages_scanned=job.pages_scanned,
                current_url=job.current_url,
            )
            # 采集成功时导入结果清单
            if job.status == "done":
                count = database.import_results(job.id, job.output_dir)
                job.append(f"已导入 {count} 条结果到数据库\n")
        except Exception:  # pragma: no cover
            _log.debug("db persist failed for %s", job.id, exc_info=True)


def wait_for_resume(job: JobState) -> None:
    """循环等待 pause_event 被设置（resume）。

    所有对 job.status 的读写都在 job.lock 下完成，
    与 snapshot() 互斥，避免 TOCTOU 竞争。
    """
    while True:
        with job.lock:
            if job.pause_event.is_set():
                if job.status == "paused":
                    job.status = "running"
                return
            job.status = "paused"
        if job.stop_event.is_set():
            raise RuntimeError("cancelled by user")
        time.sleep(0.2)


# ---------------------------------------------------------------------------
# JS 逆向 Agent：表单 → ReverseAgentConfig + 子线程运行 + 事件订阅
# ---------------------------------------------------------------------------


def build_reverse_config(form: dict[str, list[str]]) -> dict[str, object]:
    """从表单构造 ReverseAgentConfig 的可序列化字段字典。"""

    def value(name: str, default: str = "") -> str:
        return form.get(name, [default])[0]

    def checked(name: str) -> bool:
        return name in form

    target_params_str = value("target_params", "")
    target_params = [p.strip() for p in target_params_str.split(",") if p.strip()]

    allowed_domains_str = value("allowed_domains", "")
    allowed_domains: list[str] | None = None
    if allowed_domains_str.strip():
        allowed_domains = [d.strip() for d in allowed_domains_str.split(",") if d.strip()]

    # dom_prune 复选框启用时才使用 max_chars，否则为 0（禁用）
    dom_prune_max_chars = (
        int(value("dom_prune_max_chars", "4000") or "0") if checked("dom_prune") else 0
    )

    return {
        "max_steps": int(value("max_steps", "20") or "20"),
        "target_params": target_params,
        "headless": value("headless", "false") == "true",
        "proxy": value("proxy", "") or None,
        "os_name": value("os_name", "windows"),
        "dom_prune_max_chars": dom_prune_max_chars,
        "dom_prune_llm_rank": checked("dom_prune_llm_rank"),
        "enable_checkpoint": checked("enable_checkpoint"),
        "checkpoint_interval": int(value("checkpoint_interval", "1") or "1"),
        "checkpoint_keep": int(value("checkpoint_keep", "5") or "5"),
        "min_confidence": float(value("min_confidence", "0.4") or "0.4"),
        "confidence_llm_score": checked("confidence_llm_score"),
        "enable_guard": checked("enable_guard"),
        "allowed_domains": allowed_domains,
        "enable_screenshot": checked("enable_screenshot"),
    }


# 配置导入时识别的字段及其默认值/类型转换器
_CONFIG_FIELD_SPECS: tuple[tuple[str, type, object], ...] = (
    ("max_steps", int, 20),
    ("target_params", list, []),
    ("headless", bool, False),
    ("proxy", str, None),
    ("os_name", str, "windows"),
    ("dom_prune_max_chars", int, 0),
    ("dom_prune_llm_rank", bool, False),
    ("enable_checkpoint", bool, False),
    ("checkpoint_interval", int, 1),
    ("checkpoint_keep", int, 5),
    ("min_confidence", float, 0.4),
    ("confidence_llm_score", bool, False),
    ("enable_guard", bool, True),
    ("allowed_domains", list, None),
    ("enable_screenshot", bool, True),
)


def _normalize_imported_config(data: dict[str, object]) -> dict[str, object]:
    """把导入的 JSON 配置标准化为 build_reverse_config 兼容的 dict。

    - 仅保留已知字段（剔除未知键）
    - 按字段类型做安全转换（int/float/bool/list/str）
    - 缺失字段补默认值
    """
    result: dict[str, object] = {}
    for name, ftype, default in _CONFIG_FIELD_SPECS:
        raw = data.get(name, default)
        if raw is None or raw == "":
            result[name] = default
            continue
        try:
            if ftype is int:
                result[name] = _as_int(raw, cast(int, default))
            elif ftype is float:
                result[name] = _as_float(raw, cast(float, default))
            elif ftype is bool:
                # 字符串 "true"/"false" / 数字 1/0 都支持
                if isinstance(raw, str):
                    result[name] = raw.lower() in ("true", "1", "yes", "on")
                else:
                    result[name] = bool(raw)
            elif ftype is list:
                if isinstance(raw, str):
                    result[name] = [s.strip() for s in raw.split(",") if s.strip()]
                elif isinstance(raw, list):
                    result[name] = [str(x) for x in raw]
                else:
                    result[name] = []
            else:
                result[name] = str(raw)
        except (TypeError, ValueError):
            result[name] = default
    return result


class ReverseAgentRunner:
    """在子线程中启动 ReverseAgent，订阅 EventBus，把事件推到 ReverseJobState。

    停止策略（收尾阶段统一接线）：UI 的"停止"按钮目前仅设置 stop_event——
    agent.run 是同步阻塞调用,停止后任务会在 Agent 自然结束时被标记为
    cancelled,不能立即中断正在执行的循环。库侧 ReverseAgentConfig.should_stop
    可选回调（默认 None）即将支持,届时在构造 agent 处透传
    should_stop=job.stop_event.is_set 即可真正中断（见构造处的 TODO 注释）。
    """

    def run_job(self, job: ReverseJobState) -> None:
        """子线程入口：构造 Agent 并同步运行，事件实时推到 job。"""
        # 延迟导入：避免 UI 启动时加载 camoufox 等重依赖
        try:
            from web_crawler.ai.llm import DEFAULT_MODEL, DeepSeekProvider
            from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig
            from web_crawler.ai.watchdog import EventBus

            cfg_dict = job.config
            config = ReverseAgentConfig(
                max_steps=int(cfg_dict.get("max_steps", 20)),
                target_params=cfg_dict.get("target_params") or None,
                headless=bool(cfg_dict.get("headless", False)),
                proxy=cfg_dict.get("proxy") or None,
                os_name=str(cfg_dict.get("os_name", "windows")),
                dom_prune_max_chars=int(cfg_dict.get("dom_prune_max_chars", 0)),
                # 单一模型策略：DomPruner/Confidence 都用规则路径，不调用 LLM 重排/评分
                enable_checkpoint=bool(cfg_dict.get("enable_checkpoint", False)),
                checkpoint_interval=int(cfg_dict.get("checkpoint_interval", 1)),
                checkpoint_keep=int(cfg_dict.get("checkpoint_keep", 5)),
                min_confidence=float(cfg_dict.get("min_confidence", 0.4)),
                enable_guard=bool(cfg_dict.get("enable_guard", True)),
                allowed_domains=cfg_dict.get("allowed_domains") or None,
                enable_screenshot=bool(cfg_dict.get("enable_screenshot", True)),
            )

            # 创建独立 EventBus 并订阅
            bus = EventBus()
            provider = DeepSeekProvider(model=DEFAULT_MODEL)
            # "停止"按钮接线：should_stop 回调由库侧每步循环顶部检查，
            # 返回 True 时立即中断循环并标记 stopped（见下方最终状态判断）。
            config.should_stop = job.stop_event.is_set
            agent = ReverseAgent(config=config, provider=provider, event_bus=bus)
            bus.subscribe(lambda evt: self._on_event(job, evt, agent))

            # 同步运行（在子线程中阻塞）
            result = agent.run(url=job.url, task=job.task)

            # 写回结果（加锁保护共享字段）
            analysis = result.get("analysis")
            judge = result.get("judge_result")
            with job.state_lock:
                job.success = bool(result.get("success", False))
                job.analysis = _serialize_analysis(analysis)
                job.compiled_script = str(result.get("compiled_script") or "")
                job.target_params_found = dict(result.get("target_params_found") or {})
                job.judge_result = dict(judge) if isinstance(judge, dict) else {}

                # stop_event 优先于 success 判断（避免成功完成时被误标为完成）
                if job.stop_event.is_set():
                    job.status = "cancelled"
                elif job.success:
                    job.status = "done"
                else:
                    job.status = "error"
                    if not job.error:
                        job.error = "Agent 未成功完成目标参数提取"
                job.finished_at = time.time()
        except Exception as exc:
            with job.state_lock:
                job.status = "error"
                job.error = str(exc)
                job.exit_code = 1

    def _on_event(self, job: ReverseJobState, event: object, agent: object) -> None:
        """EventBus 订阅器：把 AgentEvent 推到 ReverseJobState。

        处理异常时不能让单个事件处理失败导致 agent 崩溃（EventBus 本身
        也会捕获订阅者异常，但这里额外做一层保护）。

        所有对 ReverseJobState 共享字段的写入都在 state_lock 下完成，
        与 snapshot()/clear_runtime() 互斥，避免数据竞争。
        """
        try:
            evt_type = getattr(event, "type", "")
            evt_step = getattr(event, "step", 0)
            evt_payload = getattr(event, "payload", {}) or {}
            ts = time.time()

            # 序列化事件并追加到流（append_event 自身获取 state_lock）
            evt_dict = {"type": evt_type, "step": evt_step, "payload": evt_payload, "ts": ts}
            job.append_event(evt_dict)

            # 根据事件类型更新对应字段 —— 所有写入都在 state_lock 下
            with job.state_lock:
                if evt_type == "step.start":
                    job.current_step = evt_step
                    job._step_starts[evt_step] = ts
                elif evt_type == "step.end":
                    # _finalize_step 需要单独调用以访问 agent 属性，
                    # 这里只更新步骤计数；耗时/置信度等由 _finalize_step 在锁外读 agent 后再加锁写
                    pass
                elif evt_type == "action":
                    self._update_step_action_locked(job, evt_step, evt_payload)
                elif evt_type == "observation":
                    job.current_observation = {
                        "url": evt_payload.get("url", ""),
                        "hook_count": evt_payload.get("hook_count", 0),
                        "network_count": evt_payload.get("network_count", 0),
                        "script_count": evt_payload.get("script_count", 0),
                    }
                elif evt_type == "confidence.low":
                    job.last_confidence = {
                        "score": evt_payload.get("score", 0.0),
                        "reasons": list(evt_payload.get("reasons") or []),
                        "action_type": "",
                    }
                elif evt_type == "guard.deny":
                    rules = list(evt_payload.get("matched_rules") or [])
                    details = list(evt_payload.get("details") or [])
                    for i, rule in enumerate(rules):
                        detail = details[i] if i < len(details) else ""
                        job.guard_blocks.append({"rule": rule, "detail": detail})
                elif evt_type == "judge.result":
                    job.judge_result = {
                        "verified": evt_payload.get("verified", False),
                        "missing": list(evt_payload.get("missing") or []),
                    }
                elif evt_type == "checkpoint.resume":
                    job.checkpoints.append(
                        {
                            "step": evt_step,
                            "url": evt_payload.get("url", ""),
                            "type": "resume",
                        }
                    )
                elif evt_type == "screenshot":
                    # 截图事件：追加到 screenshots 列表（含 step/path/error/ts）
                    shot_entry = {
                        "step": evt_step,
                        "path": evt_payload.get("path", ""),
                        "error": bool(evt_payload.get("error", False)),
                        "ts": ts,
                    }
                    job.screenshots.append(shot_entry)
                    if shot_entry["error"]:
                        job.error_screenshot = shot_entry["path"]

            # step.end 需要读取 agent 属性（非共享状态），在锁外读取后加锁写入
            if evt_type == "step.end":
                self._finalize_step(job, evt_step, agent)
        except Exception:
            # 静默吞掉订阅者异常，不能影响 agent 主循环
            pass

    def _update_step_locked(self, job: ReverseJobState, step: int) -> dict:
        """获取或创建步骤字典（用于累积 action / confidence 等字段）。

        调用者必须持有 job.state_lock。
        """
        for s in job.steps:
            if s.get("step") == step:
                return s
        entry = {
            "step": step,
            "action_type": "",
            "reasoning": "",
            "duration_ms": 0,
            "confidence": None,
        }
        job.steps.append(entry)
        return entry

    def _update_step_action_locked(self, job: ReverseJobState, step: int, payload: dict) -> None:
        """收到 action 事件时更新步骤的 action_type / reasoning。

        调用者必须持有 job.state_lock。
        """
        entry = self._update_step_locked(job, step)
        entry["action_type"] = str(payload.get("action_type", ""))
        entry["reasoning"] = str(payload.get("reasoning", ""))

    def _finalize_step(self, job: ReverseJobState, step: int, agent: object) -> None:
        """step.end 时计算耗时、置信度，完成步骤卡片。

        先在锁外读取 agent 属性（非共享状态），再加锁写入共享字段。
        """
        # 锁外读取 agent 属性（避免在锁内访问 agent 导致死锁）
        conf_score: float | None = None
        conf_reasons: list = []
        conf_action_type: str = ""
        hook_records: list[dict] = []
        net_log: list[dict] = []
        try:
            conf = getattr(agent, "_last_confidence", None)
            if conf is not None:
                score = getattr(conf, "score", None)
                if score is not None:
                    conf_score = float(score)
                    conf_reasons = list(getattr(conf, "reasons", []) or [])
                    conf_action_type = str(getattr(conf, "action_type", ""))
        except Exception:
            pass
        try:
            hook_cache = getattr(agent, "_hook_data_cache", {})
            records = hook_cache.get("records", []) if isinstance(hook_cache, dict) else []
            if records:
                hook_records = list(records)
        except Exception:
            pass
        try:
            net = getattr(agent, "_network_log", [])
            if net:
                net_log = list(net)
        except Exception:
            pass

        # 加锁写入所有共享字段
        with job.state_lock:
            entry = self._update_step_locked(job, step)
            start_ts = job._step_starts.pop(step, None)
            step_duration_sec = 0.0
            if start_ts is not None:
                step_duration_sec = max(0.0, time.time() - start_ts)
                entry["duration_ms"] = int(step_duration_sec * 1000)
            job.step_durations.append(step_duration_sec)

            if conf_score is not None:
                entry["confidence"] = conf_score
                job.last_confidence = {
                    "score": conf_score,
                    "reasons": conf_reasons,
                    "action_type": conf_action_type,
                }

            if hook_records:
                job.hook_records = hook_records
                job.hook_count = len(hook_records)

            if net_log:
                job.network_requests = net_log


def _serialize_analysis(analysis: object) -> str:
    """把 AnalysisResult dataclass 序列化为可读字符串。"""
    if analysis is None:
        return ""
    if isinstance(analysis, str):
        return analysis
    if hasattr(analysis, "__dataclass_fields__"):
        try:
            from dataclasses import asdict

            return json.dumps(asdict(analysis), ensure_ascii=False, indent=2, default=str)  # type: ignore[call-overload]
        except Exception:
            pass
    return str(analysis)


def run_reverse_job(job: ReverseJobState) -> None:
    """子线程入口：启动 ReverseAgentRunner。"""
    job.status = "running"
    ReverseAgentRunner().run_job(job)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if self.path in {"/", "/index.html"}:
            self.respond(
                200,
                PAGE.replace("{block_keywords}", DEFAULT_BLOCK_KEYWORDS).encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return
        if self.path.startswith("/status"):
            query = parse_qs(urlparse(self.path).query)
            job = JOBS.get(query.get("id", [""])[0])
            if not job:
                self.respond_json({"status": "missing", "log": "任务不存在"})
                return
            self.respond_json(job.snapshot())
            return
        if self.path.startswith("/reverse/status"):
            query = parse_qs(urlparse(self.path).query)
            rjob = REVERSE_JOBS.get(query.get("id", [""])[0])
            if not rjob:
                self.respond_json({"status": "missing", "error": "任务不存在"})
                return
            self.respond_json(rjob.snapshot())
            return
        if self.path.startswith("/reverse/stream"):
            # SSE 实时推送：建立长连接，把事件增量推给前端
            query = parse_qs(urlparse(self.path).query)
            rjob = REVERSE_JOBS.get(query.get("id", [""])[0])
            if not rjob:
                self.respond_json({"error": "任务不存在"})
                return
            self._stream_reverse_sse(rjob)
            return
        if self.path.startswith("/reverse/jobs"):
            # 返回所有历史任务列表（按创建时间倒序）
            with REVERSE_JOBS_LOCK:
                jobs = [rj.job_summary() for rj in REVERSE_JOBS.values()]
            jobs.sort(key=lambda j: cast(float, j.get("created_at", 0.0)), reverse=True)
            self.respond_json({"jobs": jobs, "count": len(jobs)})
            return
        if self.path.startswith("/reverse/script"):
            # 下载成功路径脚本：返回 JSON，含 Content-Disposition
            query = parse_qs(urlparse(self.path).query)
            rjob = REVERSE_JOBS.get(query.get("id", [""])[0])
            if not rjob:
                self.respond_json({"error": "任务不存在"})
                return
            script = rjob.compiled_script or ""
            if not script:
                self.respond_json({"error": "无可用脚本"})
                return
            filename = f"reverse_{rjob.id}.py"
            body = script.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/x-python; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("content-disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/reverse/screenshot"):
            # 返回 PNG 截图：?id=...&step=N[&error=1]
            query = parse_qs(urlparse(self.path).query)
            rjob = REVERSE_JOBS.get(query.get("id", [""])[0])
            if not rjob:
                self.send_error(404, "Task not found")
                return  # pragma: no cover - 截图端点防御性 404
            try:
                step_num = int(query.get("step", ["0"])[0])
            except ValueError:
                step_num = 0
            want_error = query.get("error", ["0"])[0] == "1"
            # 在 screenshots 列表中查找匹配的截图路径
            shot_path = ""
            for s in rjob.screenshots:
                if s.get("step") == step_num and bool(s.get("error")) == want_error:
                    shot_path = s.get("path", "")
                    break
            # 回退：取该 step 的任一截图
            if not shot_path:
                for s in rjob.screenshots:
                    if s.get("step") == step_num:
                        shot_path = s.get("path", "")
                        break
            if not shot_path:
                self.send_error(404, "Screenshot not found")
                return  # pragma: no cover - 截图端点防御性 404
            try:
                png = Path(shot_path).read_bytes()
            except OSError:
                self.send_error(404, "Screenshot file lost")
                return  # pragma: no cover - 截图端点防御性 404
            self.respond(200, png, "image/png")
            return
        # ── 任务历史 API ──────────────────────────────────────────
        query = parse_qs(urlparse(self.path).query)
        if path == "/jobs":
            page = int(query.get("page", ["1"])[0])
            page_size = int(query.get("page_size", ["20"])[0])
            status = query.get("status", [None])[0]
            self.respond_json(database.list_tasks(page, page_size, status))
            return
        if path.startswith("/jobs/") and "/results" not in path:
            task_id = path.split("/jobs/")[1]
            task = database.get_task(task_id)
            if not task:
                self.respond(404, b'{"error":"task not found"}', "application/json; charset=utf-8")
                return
            self.respond_json(task)
            return
        if path.startswith("/jobs/") and "/results" in path:
            task_id = path.split("/jobs/")[1].split("/results")[0]
            page = int(query.get("page", ["1"])[0])
            page_size = int(query.get("page_size", ["50"])[0])
            search = query.get("q", [None])[0]
            self.respond_json(database.get_results(task_id, page, page_size, search))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if not self._is_same_origin_request():
            # 先消费请求体再响应：未读尽的 body + keep-alive 会在 Windows 上
            # 触发连接重置（WinError 10053），客户端收不到 JSON 错误
            self.close_connection = True
            self.read_form()
            self.respond_json({"ok": False, "error": "跨站请求被拒绝"})
            return
        query = parse_qs(urlparse(self.path).query)
        path = urlparse(self.path).path
        if path == "/run":
            form = self.read_form()
            try:
                args = build_args(form)
            except ValueError as exc:
                self.respond_json({"ok": False, "error": str(exc)})
                return
            with JOBS_LOCK:
                # 并发保护：采集任务日志/共享 opener 非并发安全,同一时间只允许一个任务
                if any(j.status in ("running", "paused") for j in JOBS.values()):
                    self.respond_json(
                        {
                            "ok": False,
                            "error": "已有采集任务正在运行，请等待完成或先取消",
                        }
                    )
                    return
                job_id = uuid.uuid4().hex[:12]
                job = JobState(id=job_id, args=args, output_dir=str(Path(args.out).resolve()))
                JOBS[job_id] = job
                # 清理已完成的任务，防止内存泄漏
                if len(JOBS) > MAX_JOBS:
                    for jid in list(JOBS.keys()):
                        j = JOBS[jid]
                        if j.status in ("done", "error", "cancelled"):
                            del JOBS[jid]
            # 写入数据库（剔除 Cookie 等敏感头,不落明文）
            try:
                config = _task_config_for_db(args)
                database.create_task(job_id, args.url, config, job.output_dir)
            except Exception:  # pragma: no cover
                _log.debug("db create_task failed", exc_info=True)
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
            self.respond_json({"id": job_id, "status": "running"})
            return
        if path in {"/pause", "/resume", "/cancel"}:
            existing = JOBS.get(query.get("id", [""])[0])
            if not existing:
                self.respond_json({"ok": False, "message": "任务不存在"})
                return
            if path == "/pause":
                existing.pause_event.clear()
                with existing.lock:
                    existing.status = "paused"
            elif path == "/resume":
                existing.pause_event.set()
                with existing.lock:
                    existing.status = "running"
            elif path == "/cancel":
                existing.stop_event.set()
                existing.pause_event.set()
                with existing.lock:
                    existing.status = "cancelled"
            # 同步状态到数据库：终态不覆盖（避免迟到的控制请求改写已完成任务的状态）
            with existing.lock:
                db_status = existing.status
            if db_status not in ("done", "error", "cancelled", "missing"):
                try:
                    database.update_task_status(existing.id, db_status)
                except Exception:  # pragma: no cover
                    _log.debug("db update_task_status failed", exc_info=True)
            self.respond_json({"ok": True})
            return
        if path == "/open-output":
            form = self.read_form()
            out_dir = Path(output_path(form.get("out", [DEFAULT_OUTPUT])[0]))
            # 白名单：仅允许打开默认输出目录或已登记任务的输出目录（含子目录），
            # 防止任意路径被 os.startfile 打开/启动（Windows 上可启动可执行文件）
            resolved = out_dir.resolve()
            allowed_roots = {str(Path(j.output_dir).resolve()) for j in JOBS.values()}
            allowed_roots.add(str(Path(DEFAULT_OUTPUT).resolve()))
            if not any(
                resolved == Path(root).resolve() or Path(root).resolve() in resolved.parents
                for root in allowed_roots
            ):
                self.respond_json({"ok": False, "message": "目录不在允许范围内"})
                return
            try:
                if not out_dir.exists():
                    out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.respond_json({"ok": False, "message": f"无法创建目录：{exc}"})
                return
            if not out_dir.is_dir():
                self.respond_json({"ok": False, "message": "路径不是目录"})
                return
            try:
                _open_folder(str(out_dir))
                self.respond_json({"ok": True, "message": f"已打开：{out_dir}"})
            except Exception as exc:
                self.respond_json({"ok": False, "message": f"无法打开：{exc}"})
            return
        if path == "/reverse/run":
            form = self.read_form()
            url = form.get("url", [""])[0].strip()
            task = form.get("task", [""])[0].strip()
            if not url:
                self.respond_json({"ok": False, "error": "URL 不能为空"})
                return
            rev_config: dict[str, Any] = build_reverse_config(form)
            job_id = uuid.uuid4().hex[:12]
            rjob = ReverseJobState(
                id=job_id,
                url=url,
                task=task,
                config=rev_config,
                max_steps=int(rev_config.get("max_steps", 20)),
                target_params=list(rev_config.get("target_params") or []),
            )
            with REVERSE_JOBS_LOCK:
                REVERSE_JOBS[job_id] = rjob
                # 清理已完成的任务
                if len(REVERSE_JOBS) > MAX_REVERSE_JOBS:
                    for jid in list(REVERSE_JOBS.keys()):
                        rj = REVERSE_JOBS[jid]
                        if rj.status in ("done", "error", "cancelled"):
                            del REVERSE_JOBS[jid]
            threading.Thread(target=run_reverse_job, args=(rjob,), daemon=True).start()
            self.respond_json({"id": job_id, "status": "running"})
            return
        if path == "/reverse/stop":
            rstop = REVERSE_JOBS.get(query.get("id", [""])[0])
            if not rstop:
                self.respond_json({"ok": False, "message": "任务不存在"})
                return
            # 仅设置 stop_event：Agent 最终状态会被标记为 cancelled；
            # 库侧接口不支持中断,运行中的 Agent 循环会继续跑完
            rstop.stop_event.set()
            self.respond_json(
                {
                    "ok": True,
                    "message": "已请求停止：任务结束后将标记为取消；当前版本无法立即中断运行中的 Agent",
                }
            )
            return
        if path == "/reverse/clear":
            # 清空指定任务的运行时数据（保留最终结果）
            rjob_existing = REVERSE_JOBS.get(query.get("id", [""])[0])
            if not rjob_existing:
                self.respond_json({"ok": False, "message": "任务不存在"})
                return
            rjob_existing.clear_runtime()
            self.respond_json({"ok": True, "id": rjob_existing.id})
            return
        if path == "/reverse/config/export":
            # 接收表单，返回 JSON 配置文件下载
            form = self.read_form()
            rev_config = build_reverse_config(form)
            body = json.dumps(rev_config, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("content-disposition", 'attachment; filename="reverse_config.json"')
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/reverse/config/import":
            # 接收 JSON 配置，返回标准化后的 config dict（前端用来回填表单）
            data = self.read_json_body()
            if not isinstance(data, dict):
                self.respond_json({"error": "配置文件必须是 JSON 对象"})
                return
            # 标准化：补全缺失字段，剔除未知字段，类型转换
            normalized = _normalize_imported_config(data)
            self.respond_json({"config": normalized})
            return
        self.send_error(404)

    def do_DELETE(self) -> None:
        if not self._is_same_origin_request():
            self.close_connection = True  # 拒绝后关闭连接，避免 keep-alive 状态错乱
            self.respond_json({"ok": False, "error": "跨站请求被拒绝"})
            return
        path = urlparse(self.path).path
        if path.startswith("/jobs/"):
            task_id = path.split("/jobs/")[1]
            deleted = database.delete_task(task_id)
            self.respond_json({"ok": deleted})
            return
        self.send_error(404)  # pragma: no cover

    def read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(body)

    def _is_same_origin_request(self) -> bool:
        """CSRF 防护：校验 Origin/Referer 与请求 Host 同源。

        无 Origin/Referer 的请求（curl、本机脚本）放行；
        携带跨站 Origin/Referer 的请求（恶意网页表单/图片）一律拒绝。
        """
        host = self.headers.get("Host", "")
        for header_name in ("Origin", "Referer"):
            value = (self.headers.get(header_name) or "").strip()
            if not value:
                continue
            if value == "null":  # sandboxed iframe 的 Origin 为 null,不可信
                return False
            parsed = urlparse(value)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                if parsed.netloc != host:
                    return False
            else:
                return False
        return True

    def read_json_body(self) -> object:
        """读取请求体并解析为 JSON；解析失败返回 None。"""
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def respond_json(self, payload: dict[str, object]) -> None:
        self.respond(
            200,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _stream_reverse_sse(self, rjob: ReverseJobState) -> None:
        """SSE 长连接：把任务事件增量推给前端，直到任务结束。

        端点 ``GET /reverse/stream?id=<job_id>``，响应 ``text/event-stream``。
        每 800ms 检查一次事件流，把 ``ts > since`` 的事件以 SSE 格式推送。
        任务进入终态（done/error/cancelled）后发送一个 final 事件并关闭连接。
        """
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        self.end_headers()

        import time as _time

        last_ts = 0.0
        deadline = _time.time() + 3600  # 单连接最多 1 小时，避免泄漏
        try:
            while _time.time() < deadline:
                # 检查 job 是否已被清理（新任务达 MAX_REVERSE_JOBS 时可能被移除）
                with REVERSE_JOBS_LOCK:
                    if rjob.id not in REVERSE_JOBS:
                        self.wfile.write(
                            b"event: gone\ndata: "
                            + json.dumps(
                                {"status": "removed", "message": "任务已从注册表移除"},
                                ensure_ascii=False,
                            ).encode("utf-8")
                            + b"\n\n"
                        )
                        self.wfile.flush()
                        return
                # 任务终态：先发 final，再退出
                with rjob.state_lock:
                    cur_status = rjob.status
                if cur_status in {"done", "error", "cancelled"}:
                    payload = rjob.snapshot()
                    payload["events_since"] = last_ts
                    self.wfile.write(
                        b"event: final\ndata: "
                        + json.dumps(payload, ensure_ascii=False).encode("utf-8")
                        + b"\n\n"
                    )
                    self.wfile.flush()
                    return
                # 推送增量事件
                with rjob.state_lock:
                    new_events = [e for e in rjob.events if e.get("ts", 0) > last_ts]
                    if new_events:
                        last_ts = new_events[-1].get("ts", last_ts)
                for evt in new_events:
                    self.wfile.write(
                        b"event: step\ndata: "
                        + json.dumps(evt, ensure_ascii=False).encode("utf-8")
                        + b"\n\n"
                    )
                if new_events:
                    self.wfile.flush()
                    # 有新事件时才推送完整快照（前端用于更新统计卡片）
                    snap = rjob.snapshot()
                    self.wfile.write(
                        b"event: snapshot\ndata: "
                        + json.dumps(snap, ensure_ascii=False).encode("utf-8")
                        + b"\n\n"
                    )
                    self.wfile.flush()
                else:
                    # 无新事件时：发送 SSE 保活注释，避免浏览器超时断开
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                _time.sleep(0.8)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - 客户端断开连接时触发
            # 客户端关闭连接
            return

    def respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    import argparse as _ap

    database.init_db()

    _parser = _ap.ArgumentParser(description="Web Resource Crawler UI")
    _parser.add_argument("--open", action="store_true", help="Automatically open browser")
    _parser.add_argument("--host", default=HOST, help=f"Server host (default: {HOST})")
    _parser.add_argument("--port", type=int, default=PORT, help=f"Server port (default: {PORT})")
    _parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="允许绑定非回环地址（如 0.0.0.0，供 Docker/远程访问）。"
        "注意：控制面无鉴权，仅建议在可信网络使用",
    )
    _args = _parser.parse_args()

    # 安全边界：控制面默认只允许绑定回环地址，禁止 0.0.0.0 暴露到局域网；
    # 容器/远程场景需显式 --allow-remote 放行（Dockerfile CMD 使用）
    if not _is_loopback_host(_args.host) and not _args.allow_remote:
        _parser.error(
            f"--host 只允许回环地址（127.x / ::1 / localhost），收到: {_args.host!r}；"
            "远程绑定请显式加 --allow-remote"
        )

    server = ThreadingHTTPServer((_args.host, _args.port), Handler)
    url = f"http://{_args.host}:{_args.port}"
    if _args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    _log.info("Web UI started: %s", url)
    _log.info("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log.info("Shutting down...")
        server.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()

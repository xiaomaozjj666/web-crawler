"""Web UI 的任务状态与注册表（从 ``ui.py`` 拆出）。

包含采集任务 / JS 逆向任务的状态 dataclass、进程内任务注册表
（JOBS / REVERSE_JOBS）以及日志转发适配器（JobWriter / JobLogHandler）。
"""

from __future__ import annotations

import argparse
import io
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ._ui_helpers import _as_int

# 采集任务注册表
JOBS: dict[str, JobState] = {}
JOBS_LOCK = threading.Lock()
MAX_JOBS = 50

# JS 逆向 Agent 任务注册表
REVERSE_JOBS: dict[str, ReverseJobState] = {}
REVERSE_JOBS_LOCK = threading.Lock()
MAX_REVERSE_JOBS = 20


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

    # Checkpoint 列表
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

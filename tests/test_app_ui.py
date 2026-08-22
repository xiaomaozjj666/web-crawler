"""app/ui.py 单元测试：Web UI 控制台、任务状态、HTTP 路由。

覆盖 JobState / ReverseJobState dataclass、辅助函数、ReverseAgentRunner
事件回调、以及 Handler 的全部 HTTP 路由（使用真实 ThreadingHTTPServer 绑定
临时端口）。所有外部依赖（ReverseAgent、crawl、webbrowser）均被 mock。
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest

from web_crawler.app import ui
from web_crawler.app.ui import (
    Handler,
    JobState,
    JobWriter,
    ReverseAgentRunner,
    ReverseJobState,
    _normalize_imported_config,
    _serialize_analysis,
    build_args,
    build_reverse_config,
    header_values,
    output_path,
    run_job,
    run_reverse_job,
    wait_for_resume,
)

# ========== Fixtures ==========


@pytest.fixture(scope="session")
def http_server() -> str:
    """启动 ThreadingHTTPServer 绑定临时端口，整个测试会话复用。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def clean_job_registries() -> None:
    """每个测试前后清空 JOBS / REVERSE_JOBS，避免状态泄漏。"""
    ui.JOBS.clear()
    ui.REVERSE_JOBS.clear()
    yield
    ui.JOBS.clear()
    ui.REVERSE_JOBS.clear()


def _make_job_state(**kwargs: Any) -> JobState:
    """构造 JobState 的便捷工厂。"""
    defaults: dict[str, Any] = {
        "id": "test-job",
        "args": Mock(),
        "output_dir": "/tmp/test-output",
    }
    defaults.update(kwargs)
    return JobState(**defaults)


def _make_reverse_job(**kwargs: Any) -> ReverseJobState:
    """构造 ReverseJobState 的便捷工厂。"""
    defaults: dict[str, Any] = {
        "id": "rev-job",
        "url": "https://example.com",
        "task": "提取签名",
        "config": {"max_steps": 5},
    }
    defaults.update(kwargs)
    return ReverseJobState(**defaults)


# ========== JobState ==========


class TestJobState:
    def test_initial_state(self) -> None:
        """JobState 初始字段正确，pause_event 被 __post_init__ 设置。"""
        job = _make_job_state()
        assert job.status == "running"
        assert job.log == ""
        assert job.total_resources == 0
        assert job.processed_resources == 0
        assert job.exit_code is None
        assert job.pause_event.is_set()

    def test_append(self) -> None:
        """append 追加文本到 log。"""
        job = _make_job_state()
        job.append("hello\n")
        job.append("world\n")
        assert job.log == "hello\nworld\n"

    def test_append_truncates(self) -> None:
        """log 超过 80000 字符时截断保留尾部。"""
        job = _make_job_state()
        job.append("x" * 100000)
        assert len(job.log) <= 80000
        assert job.log.endswith("x")

    def test_progress(self) -> None:
        """progress 更新统计字段。"""
        job = _make_job_state()
        job.progress(
            {
                "total_resources": 10,
                "processed_resources": 5,
                "current_url": "https://example.com/page",
                "pages_scanned": 3,
            }
        )
        assert job.total_resources == 10
        assert job.processed_resources == 5
        assert job.current_url == "https://example.com/page"
        assert job.pages_scanned == 3

    def test_progress_partial(self) -> None:
        """progress 部分字段缺失时保留旧值。"""
        job = _make_job_state()
        job.total_resources = 8
        job.progress({"processed_resources": 2})
        assert job.total_resources == 8
        assert job.processed_resources == 2

    def test_snapshot(self) -> None:
        """snapshot 返回包含所有字段的 dict。"""
        job = _make_job_state()
        job.status = "done"
        job.exit_code = 0
        job.append("done log")
        snap = job.snapshot()
        assert snap["id"] == "test-job"
        assert snap["status"] == "done"
        assert snap["log"] == "done log"
        assert snap["exit_code"] == 0
        assert snap["output_dir"] == "/tmp/test-output"
        assert snap["percent"] == 0

    def test_snapshot_percent(self) -> None:
        """percent = processed * 100 / total，且上限 100。"""
        job = _make_job_state()
        job.total_resources = 10
        job.processed_resources = 3
        assert job.snapshot()["percent"] == 30

        job.processed_resources = 20
        assert job.snapshot()["percent"] == 100

    def test_concurrent_append(self) -> None:
        """多线程并发 append 不会崩溃且 log 被正确截断。"""
        job = _make_job_state()

        def worker(text: str) -> None:
            for _ in range(100):
                job.append(text)

        threads = [threading.Thread(target=worker, args=(f"t{i} ",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(job.log) <= 80000


# ========== ReverseJobState ==========


class TestReverseJobState:
    def test_initial_state(self) -> None:
        rjob = _make_reverse_job()
        assert rjob.status == "running"
        assert rjob.events == []
        assert rjob.steps == []
        assert rjob.success is False
        assert rjob.max_steps == 20

    def test_append_event(self) -> None:
        """append_event 追加事件到流。"""
        rjob = _make_reverse_job()
        rjob.append_event({"type": "step.start", "ts": 1.0})
        rjob.append_event({"type": "action", "ts": 2.0})
        assert len(rjob.events) == 2
        assert rjob.events[0]["type"] == "step.start"

    def test_append_event_keeps_200(self) -> None:
        """事件流保留最近 200 条。"""
        rjob = _make_reverse_job()
        for i in range(250):
            rjob.append_event({"type": "evt", "ts": float(i)})
        assert len(rjob.events) == 200
        assert rjob.events[0]["ts"] == 50.0
        assert rjob.events[-1]["ts"] == 249.0

    def test_events_since(self) -> None:
        """events_since 返回 ts 之后的所有事件（严格大于）。"""
        rjob = _make_reverse_job()
        for i in range(5):
            rjob.append_event({"type": "evt", "ts": float(i)})
        result = rjob.events_since(2.0)
        assert len(result) == 2
        assert all(e["ts"] > 2.0 for e in result)

    def test_clear_runtime(self) -> None:
        """clear_runtime 清空运行时数据但保留最终结果。"""
        rjob = _make_reverse_job()
        rjob.append_event({"type": "step.start", "ts": 1.0})
        rjob.steps.append({"step": 1, "action_type": "navigate"})
        rjob.screenshots.append({"step": 1, "path": "/tmp/shot.png"})
        rjob.success = True
        rjob.analysis = "final analysis"
        rjob.compiled_script = "print(1)"

        rjob.clear_runtime()

        assert rjob.events == []
        assert rjob.steps == []
        assert rjob.screenshots == []
        assert rjob.error_screenshot == ""
        assert rjob.step_durations == []
        assert rjob._step_starts == {}
        # 最终结果保留
        assert rjob.success is True
        assert rjob.analysis == "final analysis"
        assert rjob.compiled_script == "print(1)"

    def test_snapshot(self) -> None:
        """snapshot 返回可序列化的完整状态。"""
        rjob = _make_reverse_job()
        rjob.append_event({"type": "step.start", "ts": 1.0})
        rjob.steps.append({"step": 1, "action_type": "navigate", "duration_ms": 100})
        rjob.step_durations = [0.1, 0.2]
        rjob.status = "done"
        rjob.success = True

        snap = rjob.snapshot()
        assert snap["id"] == "rev-job"
        assert snap["status"] == "done"
        assert snap["success"] is True
        assert len(snap["events"]) == 1
        assert len(snap["steps"]) == 1
        assert snap["avg_step_ms"] == 150.0  # (100 + 200) / 2

    def test_snapshot_empty_durations(self) -> None:
        """无 step_durations 时 avg_step_ms 为 0。"""
        rjob = _make_reverse_job()
        snap = rjob.snapshot()
        assert snap["avg_step_ms"] == 0.0

    def test_job_summary(self) -> None:
        """job_summary 返回最小字段集。"""
        rjob = _make_reverse_job()
        rjob.status = "done"
        rjob.success = True
        rjob.current_step = 5
        summary = rjob.job_summary()
        assert summary["id"] == "rev-job"
        assert summary["status"] == "done"
        assert summary["success"] is True
        assert summary["current_step"] == 5
        assert "events" not in summary

    def test_state_lock_reentrant(self) -> None:
        """state_lock 是 RLock，支持嵌套获取。"""
        rjob = _make_reverse_job()
        with rjob.state_lock, rjob.state_lock:
            rjob.append_event({"type": "test", "ts": 1.0})
        assert len(rjob.events) == 1


# ========== JobWriter ==========


class TestJobWriter:
    def test_write(self) -> None:
        """write 将文本追加到 job.log 并返回长度。"""
        job = _make_job_state()
        writer = JobWriter(job)
        n = writer.write("hello\n")
        assert n == len("hello\n")
        assert job.log == "hello\n"

    def test_flush(self) -> None:
        """flush 是空操作。"""
        job = _make_job_state()
        writer = JobWriter(job)
        writer.flush()  # 不应抛异常


# ========== 辅助函数 ==========


class TestOutputPath:
    def test_empty_value(self) -> None:
        """空值返回 DEFAULT_OUTPUT 的绝对路径。"""
        result = output_path("")
        assert Path(result).is_absolute()
        assert "crawler_output" in result

    def test_relative_path(self) -> None:
        """相对路径基于 BASE_DIR 解析。"""
        result = output_path("sub/dir")
        assert Path(result).is_absolute()

    def test_absolute_path(self) -> None:
        """绝对路径直接返回（已 resolve）。"""
        result = output_path("/tmp/abs_output")
        assert Path(result).is_absolute()


class TestHeaderValues:
    def test_all_headers(self) -> None:
        """cookie + referer + 额外头都存在时全部返回。"""
        form = {
            "cookie": ["name=value"],
            "referer": ["https://ref.example.com"],
            "headers": ["X-Custom: value\nAuthorization: Bearer token"],
            "url": ["https://example.com"],
        }
        values = header_values(form)
        assert "Cookie: name=value" in values
        assert "Referer: https://ref.example.com" in values
        assert "X-Custom: value" in values
        assert "Authorization: Bearer token" in values

    def test_referer_fallback_to_url(self) -> None:
        """referer 为空时使用 url 作为 Referer。"""
        form = {"url": ["https://example.com/page"], "cookie": [""], "headers": [""]}
        values = header_values(form)
        assert "Referer: https://example.com/page" in values

    def test_empty_form(self) -> None:
        """空表单返回空列表。"""
        assert header_values({}) == []

    def test_extra_headers_skip_empty(self) -> None:
        """额外头中的空行被跳过。"""
        form = {"headers": ["X-A: 1\n\n  \nX-B: 2"]}
        values = header_values(form)
        assert "X-A: 1" in values
        assert "X-B: 2" in values
        assert len(values) == 2


class TestBuildArgs:
    def test_full_form(self) -> None:
        """完整表单正确解析所有字段。"""
        form: dict[str, list[str]] = {
            "url": ["https://example.com"],
            "out": [""],
            "max_pages": ["5"],
            "workers": ["4"],
            "delay": ["1.0"],
            "timeout": ["60"],
            "retries": ["3"],
            "max_bytes": ["1000"],
            "block_keywords": ["ad, tracker"],
            "cookie": ["name=value"],
            "referer": ["https://ref.example.com"],
            "headers": ["X-Custom: value"],
            "same_domain": ["on"],
            "include_css_urls": ["on"],
            "video_mode": ["on"],
            "list_only": ["on"],
            "resume": ["on"],
            "organize": ["on"],
            "dedup": ["on"],
            "stealth": ["on"],
        }
        args = build_args(form)
        assert args.url == "https://example.com"
        assert args.max_pages == 5
        assert args.workers == 4
        assert args.delay == 1.0
        assert args.timeout == 60
        assert args.retries == 3
        assert args.max_bytes == 1000
        assert args.same_domain is True
        assert args.include_css_urls is True
        assert args.video_mode is True
        assert args.list_only is True
        assert args.resume is True
        assert args.organize is True
        assert args.dedup is True
        assert args.stealth is True
        assert "Cookie: name=value" in args.header
        assert args.save_config == ""
        assert args.load_config == ""

    def test_defaults(self) -> None:
        """最小表单使用默认值。"""
        form = {"url": ["https://example.com"]}
        args = build_args(form)
        assert args.url == "https://example.com"
        assert args.max_pages == 1
        assert args.workers == 8
        assert args.same_domain is False
        assert args.list_only is False

    def test_out_uses_default(self) -> None:
        """out 为空时使用 DEFAULT_OUTPUT。"""
        form = {"url": ["https://example.com"], "out": [""]}
        args = build_args(form)
        assert "crawler_output" in args.out


class TestBuildReverseConfig:
    def test_defaults(self) -> None:
        """空表单返回默认配置。"""
        config = build_reverse_config({})
        assert config["max_steps"] == 20
        assert config["headless"] is False
        assert config["proxy"] is None
        assert config["os_name"] == "windows"
        assert config["dom_prune_max_chars"] == 0
        assert config["enable_checkpoint"] is False
        assert config["enable_guard"] is False
        assert config["enable_screenshot"] is False
        assert config["min_confidence"] == 0.4
        assert config["target_params"] == []
        assert config["allowed_domains"] is None

    def test_with_options(self) -> None:
        """启用所有选项时配置正确。"""
        form = {
            "max_steps": ["10"],
            "target_params": ["sign, anti_content"],
            "headless": ["true"],
            "proxy": ["http://proxy:8080"],
            "os_name": ["macos"],
            "dom_prune": ["1"],
            "dom_prune_max_chars": ["8000"],
            "enable_checkpoint": ["1"],
            "checkpoint_interval": ["2"],
            "checkpoint_keep": ["3"],
            "min_confidence": ["0.6"],
            "enable_guard": ["1"],
            "allowed_domains": ["cdn.example.com, api.example.com"],
            "enable_screenshot": ["1"],
        }
        config = build_reverse_config(form)
        assert config["max_steps"] == 10
        assert config["target_params"] == ["sign", "anti_content"]
        assert config["headless"] is True
        assert config["proxy"] == "http://proxy:8080"
        assert config["os_name"] == "macos"
        assert config["dom_prune_max_chars"] == 8000
        assert config["enable_checkpoint"] is True
        assert config["checkpoint_interval"] == 2
        assert config["checkpoint_keep"] == 3
        assert config["min_confidence"] == 0.6
        assert config["enable_guard"] is True
        assert config["allowed_domains"] == ["cdn.example.com", "api.example.com"]
        assert config["enable_screenshot"] is True

    def test_dom_prune_disabled(self) -> None:
        """dom_prune 未勾选时 dom_prune_max_chars 为 0。"""
        form = {"dom_prune_max_chars": ["8000"]}
        config = build_reverse_config(form)
        assert config["dom_prune_max_chars"] == 0

    def test_empty_string_values(self) -> None:
        """空字符串值使用默认。"""
        form = {
            "max_steps": [""],
            "min_confidence": [""],
            "checkpoint_interval": [""],
            "checkpoint_keep": [""],
        }
        config = build_reverse_config(form)
        assert config["max_steps"] == 20
        assert config["min_confidence"] == 0.4
        assert config["checkpoint_interval"] == 1
        assert config["checkpoint_keep"] == 5


class TestNormalizeImportedConfig:
    def test_empty_dict(self) -> None:
        """空 dict 补全所有默认值。"""
        result = _normalize_imported_config({})
        assert result["max_steps"] == 20
        assert result["headless"] is False
        assert result["os_name"] == "windows"
        assert result["min_confidence"] == 0.4
        assert result["enable_guard"] is True
        assert result["enable_screenshot"] is True
        assert result["target_params"] == []
        assert result["allowed_domains"] is None

    def test_with_values(self) -> None:
        """有效值被保留。"""
        data = {
            "max_steps": 15,
            "headless": True,
            "proxy": "http://proxy:8080",
            "os_name": "linux",
            "min_confidence": 0.7,
            "target_params": ["sign", "token"],
            "allowed_domains": ["a.com", "b.com"],
            "enable_guard": False,
        }
        result = _normalize_imported_config(data)
        assert result["max_steps"] == 15
        assert result["headless"] is True
        assert result["proxy"] == "http://proxy:8080"
        assert result["os_name"] == "linux"
        assert result["min_confidence"] == 0.7
        assert result["target_params"] == ["sign", "token"]
        assert result["allowed_domains"] == ["a.com", "b.com"]
        assert result["enable_guard"] is False

    def test_string_bool_conversions(self) -> None:
        """字符串 bool 值正确转换。"""
        data = {
            "headless": "true",
            "enable_guard": "yes",
            "enable_screenshot": "1",
            "enable_checkpoint": "on",
        }
        result = _normalize_imported_config(data)
        assert result["headless"] is True
        assert result["enable_guard"] is True
        assert result["enable_screenshot"] is True
        assert result["enable_checkpoint"] is True

        data2 = {"headless": "false", "enable_guard": "no", "enable_screenshot": "0"}
        result2 = _normalize_imported_config(data2)
        assert result2["headless"] is False
        assert result2["enable_guard"] is False
        assert result2["enable_screenshot"] is False

    def test_list_from_string(self) -> None:
        """字符串列表正确解析。"""
        data = {"target_params": "sign, token, anti_content", "allowed_domains": "a.com, b.com"}
        result = _normalize_imported_config(data)
        assert result["target_params"] == ["sign", "token", "anti_content"]
        assert result["allowed_domains"] == ["a.com", "b.com"]

    def test_unknown_keys_ignored(self) -> None:
        """未知字段被剔除。"""
        data = {"max_steps": 10, "unknown_field": "value", "another": 123}
        result = _normalize_imported_config(data)
        assert "unknown_field" not in result
        assert "another" not in result
        assert result["max_steps"] == 10

    def test_none_and_empty_use_defaults(self) -> None:
        """None / 空字符串值使用默认。"""
        data = {"max_steps": None, "proxy": "", "os_name": ""}
        result = _normalize_imported_config(data)
        assert result["max_steps"] == 20
        assert result["proxy"] is None
        assert result["os_name"] == "windows"

    def test_invalid_types_fallback(self) -> None:
        """无效类型转换失败时回退默认。"""
        data = {"max_steps": "not_a_number", "min_confidence": "invalid"}
        result = _normalize_imported_config(data)
        assert result["max_steps"] == 20
        assert result["min_confidence"] == 0.4

    def test_conversion_exception_falls_back(self) -> None:
        """字段转换抛异常（__str__ 失败）时回退默认值。"""

        class _BadStr:
            def __str__(self) -> str:
                raise ValueError("boom")

        result = _normalize_imported_config({"os_name": _BadStr()})
        assert result["os_name"] == "windows"


class TestAsIntFloat:
    """_as_int / _as_float 安全转换的兜底分支。"""

    def test_as_int_bool(self) -> None:
        """bool 直接转 int（True→1 / False→0）。"""
        assert ui._as_int(True, 5) == 1
        assert ui._as_int(False, 5) == 0

    def test_as_int_non_numeric_type(self) -> None:
        """非数字类型（list/None/对象）回退默认值。"""
        assert ui._as_int([1, 2], 5) == 5
        assert ui._as_int(None, 5) == 5
        assert ui._as_int(object(), 5) == 5

    def test_as_float_non_numeric_type(self) -> None:
        """非数字类型（list/None/对象）回退默认值。"""
        assert ui._as_float([], 0.5) == 0.5
        assert ui._as_float(None, 0.5) == 0.5
        assert ui._as_float(object(), 0.5) == 0.5


class TestValidateFields:
    """服务端表单数字字段校验器的空值/非法值分支。"""

    def test_int_field_empty_uses_default(self) -> None:
        """空字符串整数字段回退默认值。"""
        assert ui._validate_int_field("workers", "", 8, 1, 64) == 8
        assert ui._validate_int_field("workers", "   ", 8, 1, 64) == 8

    def test_float_field_empty_uses_default(self) -> None:
        """空字符串浮点字段回退默认值。"""
        assert ui._validate_float_field("delay", "", 0.5, 0.0) == 0.5
        assert ui._validate_float_field("delay", "  ", 0.5, 0.0) == 0.5

    def test_float_field_non_numeric_raises(self) -> None:
        """非数字浮点字段抛 ValueError（handler 转 JSON 错误）。"""
        with pytest.raises(ValueError, match="delay 必须是数字"):
            ui._validate_float_field("delay", "abc", 0.5, 0.0)


class TestSerializeAnalysis:
    def test_none(self) -> None:
        assert _serialize_analysis(None) == ""

    def test_str(self) -> None:
        assert _serialize_analysis("plain text") == "plain text"

    def test_dataclass(self) -> None:
        """dataclass 实例被序列化为 JSON。"""

        @dataclass
        class FakeAnalysis:
            summary: str
            steps: list

        result = _serialize_analysis(FakeAnalysis(summary="test", steps=[1, 2]))
        data = json.loads(result)
        assert data["summary"] == "test"
        assert data["steps"] == [1, 2]

    def test_other(self) -> None:
        """其它类型调用 str()。"""
        result = _serialize_analysis(42)
        assert result == "42"


# ========== ReverseAgentRunner ==========


class _FakeEvent:
    """模拟 AgentEvent。"""

    def __init__(self, evt_type: str, step: int = 0, payload: dict | None = None) -> None:
        self.type = evt_type
        self.step = step
        self.payload = payload or {}


class TestReverseAgentRunnerOnEvent:
    def test_step_start(self) -> None:
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        runner._on_event(rjob, _FakeEvent("step.start", step=1), agent=Mock())
        assert rjob.current_step == 1
        assert 1 in rjob._step_starts

    def test_action_event(self) -> None:
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        runner._on_event(
            rjob,
            _FakeEvent(
                "action", step=1, payload={"action_type": "navigate", "reasoning": "go to page"}
            ),
            agent=Mock(),
        )
        assert len(rjob.steps) == 1
        assert rjob.steps[0]["action_type"] == "navigate"
        assert rjob.steps[0]["reasoning"] == "go to page"

    def test_observation_event(self) -> None:
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        runner._on_event(
            rjob,
            _FakeEvent(
                "observation",
                payload={"url": "https://example.com", "hook_count": 3, "network_count": 5},
            ),
            agent=Mock(),
        )
        assert rjob.current_observation["url"] == "https://example.com"
        assert rjob.current_observation["hook_count"] == 3

    def test_confidence_low_event(self) -> None:
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        runner._on_event(
            rjob,
            _FakeEvent(
                "confidence.low",
                payload={"score": 0.6, "reasons": ["reason1", "reason2"]},
            ),
            agent=Mock(),
        )
        assert rjob.last_confidence["score"] == 0.6
        assert rjob.last_confidence["reasons"] == ["reason1", "reason2"]

    def test_guard_deny_event(self) -> None:
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        runner._on_event(
            rjob,
            _FakeEvent(
                "guard.deny",
                payload={"matched_rules": ["block_submit"], "details": ["form not allowed"]},
            ),
            agent=Mock(),
        )
        assert len(rjob.guard_blocks) == 1
        assert rjob.guard_blocks[0]["rule"] == "block_submit"

    def test_judge_result_event(self) -> None:
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        runner._on_event(
            rjob,
            _FakeEvent(
                "judge.result",
                payload={"verified": True, "missing": []},
            ),
            agent=Mock(),
        )
        assert rjob.judge_result["verified"] is True

    def test_checkpoint_resume_event(self) -> None:
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        runner._on_event(
            rjob,
            _FakeEvent("checkpoint.resume", step=3, payload={"url": "https://example.com/step3"}),
            agent=Mock(),
        )
        assert len(rjob.checkpoints) == 1
        assert rjob.checkpoints[0]["step"] == 3
        assert rjob.checkpoints[0]["url"] == "https://example.com/step3"

    def test_screenshot_event(self) -> None:
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        runner._on_event(
            rjob,
            _FakeEvent(
                "screenshot",
                step=2,
                payload={"path": "/tmp/shot.png", "error": False},
            ),
            agent=Mock(),
        )
        assert len(rjob.screenshots) == 1
        assert rjob.screenshots[0]["step"] == 2
        assert rjob.screenshots[0]["path"] == "/tmp/shot.png"
        assert rjob.screenshots[0]["error"] is False

    def test_screenshot_error_event(self) -> None:
        """错误截图设置 error_screenshot。"""
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        runner._on_event(
            rjob,
            _FakeEvent(
                "screenshot",
                step=2,
                payload={"path": "/tmp/error.png", "error": True},
            ),
            agent=Mock(),
        )
        assert rjob.error_screenshot == "/tmp/error.png"

    def test_step_end_calls_finalize(self) -> None:
        """step.end 事件触发 _finalize_step。"""
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        rjob._step_starts[1] = time.time() - 0.5  # 0.5 秒前开始

        mock_agent = Mock()
        mock_agent._last_confidence = None
        mock_agent._hook_data_cache = {}
        mock_agent._network_log = []

        runner._on_event(rjob, _FakeEvent("step.end", step=1), agent=mock_agent)

        assert len(rjob.steps) == 1
        assert rjob.steps[0]["step"] == 1
        assert len(rjob.step_durations) == 1
        assert rjob.step_durations[0] >= 0
        # _step_starts 中该步的起始时间应被弹出
        assert 1 not in rjob._step_starts

    def test_on_event_exception_swallowed(self) -> None:
        """_on_event 内部异常被静默吞掉，不影响 agent。"""
        runner = ReverseAgentRunner()
        # 传入非对象作为 event，getattr 会失败但被 try/except 捕获
        runner._on_event(_make_reverse_job(), None, agent=Mock())  # 不应抛异常


class TestReverseAgentRunnerFinalizeStep:
    def test_finalize_with_confidence(self) -> None:
        """_finalize_step 正确记录置信度。"""
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        rjob._step_starts[1] = time.time()

        mock_conf = Mock()
        mock_conf.score = 0.85
        mock_conf.reasons = ["good action"]
        mock_conf.action_type = "extract"

        mock_agent = Mock()
        mock_agent._last_confidence = mock_conf
        mock_agent._hook_data_cache = {}
        mock_agent._network_log = []

        runner._finalize_step(rjob, 1, mock_agent)

        assert rjob.steps[0]["confidence"] == 0.85
        assert rjob.last_confidence["score"] == 0.85
        assert rjob.last_confidence["action_type"] == "extract"

    def test_finalize_with_hook_records(self) -> None:
        """_finalize_step 正确记录 hook_records。"""
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        rjob._step_starts[1] = time.time()

        mock_agent = Mock()
        mock_agent._last_confidence = None
        mock_agent._hook_data_cache = {"records": [{"hook": "fetch", "url": "https://x.com"}]}
        mock_agent._network_log = [{"url": "https://api.com", "method": "GET"}]

        runner._finalize_step(rjob, 1, mock_agent)

        assert rjob.hook_count == 1
        assert len(rjob.network_requests) == 1

    def test_finalize_no_step_start(self) -> None:
        """无 step_start 记录时 duration 为 0。"""
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()

        mock_agent = Mock()
        mock_agent._last_confidence = None
        mock_agent._hook_data_cache = {}
        mock_agent._network_log = []

        runner._finalize_step(rjob, 99, mock_agent)
        assert rjob.step_durations == [0.0]


class TestReverseAgentRunnerRunJob:
    def _patch_agent_imports(self, result: dict[str, Any]) -> Any:
        """注入 mock 模块到 sys.modules，模拟 ReverseAgent 等导入。"""
        mock_llm = MagicMock()
        mock_llm.DEFAULT_MODEL = "test-model"
        mock_llm.DeepSeekProvider = MagicMock()

        mock_reverse_agent = MagicMock()
        mock_agent_instance = MagicMock()
        mock_agent_instance.run.return_value = result
        mock_reverse_agent.ReverseAgent = MagicMock(return_value=mock_agent_instance)
        mock_reverse_agent.ReverseAgentConfig = MagicMock()

        mock_watchdog = MagicMock()
        mock_watchdog.EventBus = MagicMock()

        return patch.dict(
            "sys.modules",
            {
                "web_crawler.ai.llm": mock_llm,
                "web_crawler.ai.reverse_agent": mock_reverse_agent,
                "web_crawler.ai.watchdog": mock_watchdog,
            },
        )

    def test_run_job_success(self) -> None:
        """成功完成时 status=done。"""
        result = {
            "success": True,
            "analysis": "分析结果",
            "compiled_script": "print('hello')",
            "target_params_found": {"sign": "abc123"},
            "judge_result": {"verified": True},
        }
        rjob = _make_reverse_job()
        with self._patch_agent_imports(result):
            ReverseAgentRunner().run_job(rjob)

        assert rjob.status == "done"
        assert rjob.success is True
        assert rjob.analysis == "分析结果"
        assert rjob.compiled_script == "print('hello')"
        assert rjob.target_params_found == {"sign": "abc123"}

    def test_run_job_error_no_success(self) -> None:
        """未成功时 status=error。"""
        result = {"success": False, "analysis": "", "compiled_script": ""}
        rjob = _make_reverse_job()
        with self._patch_agent_imports(result):
            ReverseAgentRunner().run_job(rjob)

        assert rjob.status == "error"
        assert "未成功" in rjob.error

    def test_run_job_cancelled(self) -> None:
        """stop_event 设置时 status=cancelled。"""
        result = {"success": True, "analysis": "done", "compiled_script": ""}
        rjob = _make_reverse_job()
        rjob.stop_event.set()
        with self._patch_agent_imports(result):
            ReverseAgentRunner().run_job(rjob)

        assert rjob.status == "cancelled"

    def test_run_job_import_error(self) -> None:
        """导入失败时 status=error。"""
        rjob = _make_reverse_job()
        with (
            patch.dict("sys.modules", {}, clear=False),
            patch("builtins.__import__", side_effect=ImportError("no module")),
        ):
            ReverseAgentRunner().run_job(rjob)

        assert rjob.status == "error"
        assert rjob.exit_code == 1


# ========== run_reverse_job / run_job / wait_for_resume ==========


class TestRunReverseJob:
    def test_sets_running_and_calls_runner(self) -> None:
        """run_reverse_job 设置 status=running 并调用 ReverseAgentRunner.run_job。"""
        rjob = _make_reverse_job()
        with patch.object(ReverseAgentRunner, "run_job") as mock_run:
            run_reverse_job(rjob)
        assert rjob.status == "running"
        mock_run.assert_called_once_with(rjob)


class TestRunJob:
    def test_success(self, tmp_path: Path) -> None:
        """crawl 返回 0 时 status=done。"""
        job = _make_job_state(output_dir=str(tmp_path))
        job.args = Mock()
        with patch.object(ui.web_resource_crawler, "crawl", return_value=0):
            run_job(job)
        assert job.status == "done"
        assert job.exit_code == 0

    def test_cancelled(self, tmp_path: Path) -> None:
        """stop_event 设置时 status=cancelled。"""
        job = _make_job_state(output_dir=str(tmp_path))
        job.args = Mock()
        job.stop_event.set()
        with patch.object(ui.web_resource_crawler, "crawl", return_value=0):
            run_job(job)
        assert job.status == "cancelled"

    def test_exception(self, tmp_path: Path) -> None:
        """crawl 抛异常时 status=error。"""
        job = _make_job_state(output_dir=str(tmp_path))
        job.args = Mock()
        with patch.object(ui.web_resource_crawler, "crawl", side_effect=RuntimeError("boom")):
            run_job(job)
        assert job.status == "error"
        assert job.exit_code == 1
        assert "boom" in job.log

    def test_reports_report_files(self, tmp_path: Path) -> None:
        """完成时如果报告文件存在，追加到 log。"""
        (tmp_path / "run_report.html").write_text("<html></html>")
        (tmp_path / "run_report.md").write_text("# Report")
        job = _make_job_state(output_dir=str(tmp_path))
        job.args = Mock()
        with patch.object(ui.web_resource_crawler, "crawl", return_value=0):
            run_job(job)
        assert "可视化报告" in job.log
        assert "Markdown 报告" in job.log


class TestWaitForResume:
    def test_immediate(self) -> None:
        """pause_event 已设置时立即返回。"""
        job = _make_job_state()
        wait_for_resume(job)  # 不应阻塞

    def test_cancelled(self) -> None:
        """stop_event 设置时抛 RuntimeError。"""
        job = _make_job_state()
        job.pause_event.clear()
        job.stop_event.set()
        with pytest.raises(RuntimeError, match="cancelled"):
            wait_for_resume(job)

    def test_pause_then_resume(self) -> None:
        """pause_event 清除后恢复时返回。"""
        job = _make_job_state()
        job.pause_event.clear()

        def resume_after_delay() -> None:
            time.sleep(0.1)
            job.pause_event.set()

        threading.Thread(target=resume_after_delay, daemon=True).start()
        wait_for_resume(job)
        assert job.status == "running"


# ========== Handler HTTP 路由 ==========


class TestHandlerGetRoutes:
    def test_get_index(self, http_server: str) -> None:
        """GET / 返回 HTML 页面。"""
        resp = httpx.get(f"{http_server}/")
        assert resp.status_code == 200
        assert "Web Crawler 控制台" in resp.text

    def test_get_index_html(self, http_server: str) -> None:
        """GET /index.html 返回 HTML 页面。"""
        resp = httpx.get(f"{http_server}/index.html")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_get_status_existing(self, http_server: str) -> None:
        """GET /status?id=X 返回 job snapshot。"""
        job = _make_job_state(id="s1")
        ui.JOBS["s1"] = job
        resp = httpx.get(f"{http_server}/status?id=s1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "s1"

    def test_get_status_missing(self, http_server: str) -> None:
        """GET /status?id=missing 返回 missing 状态。"""
        resp = httpx.get(f"{http_server}/status?id=nonexistent")
        assert resp.status_code == 200
        assert resp.json()["status"] == "missing"

    def test_get_reverse_status(self, http_server: str) -> None:
        """GET /reverse/status?id=X 返回 reverse snapshot。"""
        rjob = _make_reverse_job(id="rs1")
        ui.REVERSE_JOBS["rs1"] = rjob
        resp = httpx.get(f"{http_server}/reverse/status?id=rs1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "rs1"

    def test_get_reverse_status_missing(self, http_server: str) -> None:
        """GET /reverse/status?id=missing 返回 missing。"""
        resp = httpx.get(f"{http_server}/reverse/status?id=nonexistent")
        assert resp.status_code == 200
        assert resp.json()["status"] == "missing"

    def test_get_reverse_jobs(self, http_server: str) -> None:
        """GET /reverse/jobs 返回任务列表。"""
        rjob1 = _make_reverse_job(id="rj1")
        rjob1.created_at = 100.0
        rjob2 = _make_reverse_job(id="rj2")
        rjob2.created_at = 200.0
        ui.REVERSE_JOBS["rj1"] = rjob1
        ui.REVERSE_JOBS["rj2"] = rjob2
        resp = httpx.get(f"{http_server}/reverse/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        # 按创建时间倒序
        assert data["jobs"][0]["id"] == "rj2"

    def test_get_reverse_jobs_empty(self, http_server: str) -> None:
        """GET /reverse/jobs 空列表。"""
        resp = httpx.get(f"{http_server}/reverse/jobs")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_get_reverse_script(self, http_server: str) -> None:
        """GET /reverse/script?id=X 下载脚本。"""
        rjob = _make_reverse_job(id="sc1")
        rjob.compiled_script = "print('hello')"
        ui.REVERSE_JOBS["sc1"] = rjob
        resp = httpx.get(f"{http_server}/reverse/script?id=sc1")
        assert resp.status_code == 200
        assert "text/x-python" in resp.headers["content-type"]
        assert "attachment" in resp.headers["content-disposition"]
        assert resp.text == "print('hello')"

    def test_get_reverse_script_missing(self, http_server: str) -> None:
        """GET /reverse/script?id=missing 返回错误。"""
        resp = httpx.get(f"{http_server}/reverse/script?id=nonexistent")
        assert resp.status_code == 200
        assert "error" in resp.json()

    def test_get_reverse_script_no_script(self, http_server: str) -> None:
        """GET /reverse/script?id=X（无脚本）返回错误。"""
        rjob = _make_reverse_job(id="sc2")
        ui.REVERSE_JOBS["sc2"] = rjob
        resp = httpx.get(f"{http_server}/reverse/script?id=sc2")
        assert "error" in resp.json()

    def test_get_reverse_screenshot(self, http_server: str, tmp_path: Path) -> None:
        """GET /reverse/screenshot?id=X&step=N 返回 PNG。"""
        png_file = tmp_path / "shot.png"
        png_file.write_bytes(b"\x89PNG\r\n\x1a\nfake_png_data")

        rjob = _make_reverse_job(id="sh1")
        rjob.screenshots.append({"step": 1, "path": str(png_file), "error": False})
        ui.REVERSE_JOBS["sh1"] = rjob

        resp = httpx.get(f"{http_server}/reverse/screenshot?id=sh1&step=1")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert b"fake_png_data" in resp.content

    def test_get_reverse_screenshot_missing(self, http_server: str) -> None:
        """GET /reverse/screenshot?id=missing 任务不存在时返回错误响应。"""
        resp = httpx.get(f"{http_server}/reverse/screenshot?id=nonexistent&step=1")
        assert resp.status_code >= 400

    def test_get_reverse_screenshot_not_found(self, http_server: str) -> None:
        """GET /reverse/screenshot?id=X&step=99（无匹配截图）返回错误响应。"""
        rjob = _make_reverse_job(id="sh2")
        ui.REVERSE_JOBS["sh2"] = rjob
        resp = httpx.get(f"{http_server}/reverse/screenshot?id=sh2&step=99")
        assert resp.status_code >= 400

    def test_get_reverse_screenshot_file_lost(self, http_server: str, tmp_path: Path) -> None:
        """截图文件丢失时返回错误响应。"""
        rjob = _make_reverse_job(id="sh3")
        rjob.screenshots.append(
            {"step": 1, "path": str(tmp_path / "nonexistent.png"), "error": False}
        )
        ui.REVERSE_JOBS["sh3"] = rjob
        resp = httpx.get(f"{http_server}/reverse/screenshot?id=sh3&step=1")
        assert resp.status_code >= 400

    def test_get_404(self, http_server: str) -> None:
        """GET /unknown 返回 404。"""
        resp = httpx.get(f"{http_server}/unknown-path")
        assert resp.status_code == 404


class TestHandlerSSE:
    def test_sse_terminal_state(self, http_server: str) -> None:
        """SSE 对终态任务发送 final 事件后关闭。"""
        rjob = _make_reverse_job(id="sse1")
        rjob.status = "done"
        rjob.success = True
        rjob.compiled_script = "print('done')"
        ui.REVERSE_JOBS["sse1"] = rjob

        with httpx.stream("GET", f"{http_server}/reverse/stream?id=sse1", timeout=5) as resp:
            assert resp.status_code == 200
            events = []
            for line in resp.iter_lines():
                events.append(line)
                if "event: final" in line:
                    break

        # 验证收到了 final 事件
        joined = "\n".join(events)
        assert "event: final" in joined

    def test_sse_missing_job(self, http_server: str) -> None:
        """SSE 对不存在的任务返回错误 JSON。"""
        resp = httpx.get(f"{http_server}/reverse/stream?id=nonexistent")
        assert resp.status_code == 200
        assert "error" in resp.json()

    def test_sse_running_job(self, http_server: str) -> None:
        """SSE 对运行中任务发送 snapshot 事件。"""
        rjob = _make_reverse_job(id="sse2")
        rjob.status = "running"
        rjob.append_event({"type": "step.start", "ts": time.time()})
        ui.REVERSE_JOBS["sse2"] = rjob

        with httpx.stream("GET", f"{http_server}/reverse/stream?id=sse2", timeout=5) as resp:
            assert resp.status_code == 200
            lines = []
            for line in resp.iter_lines():
                lines.append(line)
                if "event: snapshot" in line:
                    break

        joined = "\n".join(lines)
        assert "event: snapshot" in joined

    def test_sse_keepalive_without_events(self, http_server: str) -> None:
        """运行中且无新事件的任务发送 SSE 保活注释。"""
        rjob = _make_reverse_job(id="sse3")
        rjob.status = "running"
        ui.REVERSE_JOBS["sse3"] = rjob

        with httpx.stream("GET", f"{http_server}/reverse/stream?id=sse3", timeout=5) as resp:
            assert resp.status_code == 200
            lines = []
            for line in resp.iter_lines():
                lines.append(line)
                if ": keep-alive" in line:
                    break

        assert any(": keep-alive" in line for line in lines)


class TestHandlerPostRoutes:
    def test_post_run(self, http_server: str) -> None:
        """POST /run 启动采集任务并返回 job id。"""
        with patch.object(ui, "run_job"):
            resp = httpx.post(
                f"{http_server}/run",
                data={"url": "https://example.com", "out": "", "max_pages": "1"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["status"] == "running"
        assert data["id"] in ui.JOBS

    def test_post_pause_resume_cancel(self, http_server: str) -> None:
        """POST /pause /resume /cancel 控制任务状态。"""
        job = _make_job_state(id="ctrl1")
        ui.JOBS["ctrl1"] = job

        # pause
        resp = httpx.post(f"{http_server}/pause?id=ctrl1")
        assert resp.json()["ok"] is True
        assert job.status == "paused"
        assert not job.pause_event.is_set()

        # resume
        resp = httpx.post(f"{http_server}/resume?id=ctrl1")
        assert resp.json()["ok"] is True
        assert job.status == "running"
        assert job.pause_event.is_set()

        # cancel
        resp = httpx.post(f"{http_server}/cancel?id=ctrl1")
        assert resp.json()["ok"] is True
        assert job.status == "cancelled"
        assert job.stop_event.is_set()

    def test_post_pause_missing(self, http_server: str) -> None:
        """POST /pause?id=missing 返回任务不存在。"""
        resp = httpx.post(f"{http_server}/pause?id=nonexistent")
        assert resp.json()["ok"] is False

    def test_post_open_output(self, http_server: str, tmp_path: Path) -> None:
        """POST /open-output 打开已登记任务的输出目录（白名单内）。"""
        job = _make_job_state(id="open1", output_dir=str(tmp_path))
        ui.JOBS["open1"] = job
        with patch.object(ui, "_open_folder") as mock_open:
            resp = httpx.post(
                f"{http_server}/open-output",
                data={"out": str(tmp_path)},
            )
        assert resp.json()["ok"] is True
        mock_open.assert_called_once()

    def test_post_open_output_rejects_non_whitelisted(
        self, http_server: str, tmp_path: Path
    ) -> None:
        """POST /open-output 拒绝白名单之外的任意路径（防任意路径启动）。"""
        with patch.object(ui, "_open_folder") as mock_open:
            resp = httpx.post(
                f"{http_server}/open-output",
                data={"out": str(tmp_path)},
            )
        assert resp.json()["ok"] is False
        mock_open.assert_not_called()

    def test_post_open_output_error(self, http_server: str, tmp_path: Path) -> None:
        """POST /open-output 打开失败时返回错误 JSON。"""
        job = _make_job_state(id="open2", output_dir=str(tmp_path))
        ui.JOBS["open2"] = job
        with patch.object(ui, "_open_folder", side_effect=OSError("denied")):
            resp = httpx.post(
                f"{http_server}/open-output",
                data={"out": str(tmp_path)},
            )
        assert resp.json()["ok"] is False

    def test_post_open_output_mkdir_error(self, http_server: str, tmp_path: Path) -> None:
        """mkdir 失败时返回错误 JSON。"""
        job = _make_job_state(id="open3", output_dir=str(tmp_path))
        ui.JOBS["open3"] = job
        non_existent = tmp_path / "sub" / "dir"  # 白名单内但不存在的路径 → 触发 mkdir
        with patch.object(ui.Path, "mkdir", side_effect=OSError("denied")):
            resp = httpx.post(
                f"{http_server}/open-output",
                data={"out": str(non_existent)},
            )
        assert resp.json()["ok"] is False
        assert "无法创建目录" in resp.json()["message"]

    def test_post_open_output_not_a_directory(self, http_server: str, tmp_path: Path) -> None:
        """白名单内但路径是文件而非目录时拒绝。"""
        target = tmp_path / "file.txt"
        target.write_text("x")
        job = _make_job_state(id="open4", output_dir=str(tmp_path))
        ui.JOBS["open4"] = job
        resp = httpx.post(
            f"{http_server}/open-output",
            data={"out": str(target)},
        )
        assert resp.json()["ok"] is False
        assert "不是目录" in resp.json()["message"]

    def test_post_reverse_run(self, http_server: str) -> None:
        """POST /reverse/run 启动逆向任务。"""
        with patch.object(ui, "run_reverse_job"):
            resp = httpx.post(
                f"{http_server}/reverse/run",
                data={"url": "https://example.com", "task": "提取签名"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["status"] == "running"
        assert data["id"] in ui.REVERSE_JOBS

    def test_post_reverse_run_no_url(self, http_server: str) -> None:
        """POST /reverse/run 无 URL 时返回错误。"""
        resp = httpx.post(
            f"{http_server}/reverse/run",
            data={"url": "", "task": ""},
        )
        assert resp.json()["ok"] is False
        assert "error" in resp.json()

    def test_post_reverse_stop(self, http_server: str) -> None:
        """POST /reverse/stop 设置 stop_event。"""
        rjob = _make_reverse_job(id="stop1")
        ui.REVERSE_JOBS["stop1"] = rjob
        resp = httpx.post(f"{http_server}/reverse/stop?id=stop1")
        assert resp.json()["ok"] is True
        assert rjob.stop_event.is_set()

    def test_post_reverse_stop_missing(self, http_server: str) -> None:
        """POST /reverse/stop?id=missing 返回错误。"""
        resp = httpx.post(f"{http_server}/reverse/stop?id=nonexistent")
        assert resp.json()["ok"] is False

    def test_post_reverse_clear(self, http_server: str) -> None:
        """POST /reverse/clear 清空运行时数据。"""
        rjob = _make_reverse_job(id="clr1")
        rjob.append_event({"type": "evt", "ts": 1.0})
        ui.REVERSE_JOBS["clr1"] = rjob
        resp = httpx.post(f"{http_server}/reverse/clear?id=clr1")
        assert resp.json()["ok"] is True
        assert rjob.events == []

    def test_post_reverse_clear_missing(self, http_server: str) -> None:
        """POST /reverse/clear?id=missing 返回错误。"""
        resp = httpx.post(f"{http_server}/reverse/clear?id=nonexistent")
        assert resp.json()["ok"] is False

    def test_post_reverse_config_export(self, http_server: str) -> None:
        """POST /reverse/config/export 导出配置 JSON。"""
        resp = httpx.post(
            f"{http_server}/reverse/config/export",
            data={"max_steps": ["10"], "target_params": ["sign"]},
        )
        assert resp.status_code == 200
        assert "application/json" in resp.headers["content-type"]
        config = resp.json()
        assert config["max_steps"] == 10
        assert config["target_params"] == ["sign"]

    def test_post_reverse_config_import(self, http_server: str) -> None:
        """POST /reverse/config/import 导入并标准化配置。"""
        resp = httpx.post(
            f"{http_server}/reverse/config/import",
            json={"max_steps": 15, "headless": True, "unknown_key": "ignored"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "config" in data
        assert data["config"]["max_steps"] == 15
        assert data["config"]["headless"] is True
        assert "unknown_key" not in data["config"]

    def test_post_reverse_config_import_invalid_json(self, http_server: str) -> None:
        """POST /reverse/config/import 非 JSON 对象返回错误。"""
        resp = httpx.post(
            f"{http_server}/reverse/config/import",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert "error" in resp.json()

    def test_post_404(self, http_server: str) -> None:
        """POST /unknown 返回 404。"""
        resp = httpx.post(f"{http_server}/unknown-path")
        assert resp.status_code == 404


# ========== main ==========


class TestPageTemplate:
    def test_template_file_exists_and_readable(self) -> None:
        """前端模板独立文件存在且可读取（打包依赖 package-data 携带）。"""
        assert ui._PAGE_TEMPLATE_PATH.exists()
        content = ui._PAGE_TEMPLATE_PATH.read_text(encoding="utf-8")
        assert content.startswith("<!doctype html>")
        assert len(content) > 1000

    def test_page_loaded_from_template_with_placeholder(self) -> None:
        """PAGE 来自模板文件，且保留 {block_keywords} 占位符供响应时替换。"""
        assert ui.PAGE == ui._PAGE_TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "{block_keywords}" in ui.PAGE

    def test_load_page_template_missing_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模板文件缺失时返回占位页（不崩溃）。"""
        monkeypatch.setattr(
            ui, "_PAGE_TEMPLATE_PATH", Path(__file__).parent / "no_such_template.html"
        )
        page = ui._load_page_template()
        assert "模板缺失" in page


class TestMain:
    def test_main_starts_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() 启动 ThreadingHTTPServer 并调用 serve_forever。"""
        monkeypatch.setattr("sys.argv", ["ui", "--host", "127.0.0.1", "--port", "8765"])
        mock_server = MagicMock()
        with patch("web_crawler.app.ui.ThreadingHTTPServer", return_value=mock_server):
            ui.main()
        mock_server.serve_forever.assert_called_once()

    def test_main_with_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() --open 启动定时器打开浏览器。"""
        monkeypatch.setattr("sys.argv", ["ui", "--open"])
        mock_server = MagicMock()
        with (
            patch("web_crawler.app.ui.ThreadingHTTPServer", return_value=mock_server),
            patch("threading.Timer") as mock_timer,
        ):
            ui.main()
        mock_timer.assert_called_once()

    def test_main_keyboard_interrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() Ctrl+C 时关闭服务器。"""
        monkeypatch.setattr("sys.argv", ["ui"])
        mock_server = MagicMock()
        mock_server.serve_forever.side_effect = KeyboardInterrupt()
        with patch("web_crawler.app.ui.ThreadingHTTPServer", return_value=mock_server):
            ui.main()  # 不应抛异常
        mock_server.server_close.assert_called_once()


# ========== 补充分支测试：覆盖剩余未覆盖行 ==========


class TestOpenFolder:
    @pytest.mark.skipif(
        not sys.platform.startswith("win"), reason="Windows-only: uses os.startfile"
    )
    def test_open_folder_win32(self, tmp_path: Path) -> None:
        """_open_folder 在 Windows 上调用 os.startfile。"""
        with patch("os.startfile") as mock_startfile:
            ui._open_folder(str(tmp_path))
        mock_startfile.assert_called_once_with(str(tmp_path))


class TestNormalizeImportedConfigListEdge:
    def test_list_field_with_int_value(self) -> None:
        """list 类型字段传入非 str/非 list（如 int）时返回空列表。"""
        data: dict[str, object] = {"target_params": 123}
        result = _normalize_imported_config(data)
        assert result["target_params"] == []


class TestSerializeAnalysisEdge:
    def test_dataclass_asdict_fails(self) -> None:
        """dataclass 的 asdict 失败时回退到 str()。"""

        @dataclass
        class BadAnalysis:
            value: object

        # 构造一个会让 asdict 递归失败的对象
        obj = BadAnalysis(value=BadAnalysis(value=None))
        obj.value.value = obj  # type: ignore[attr-defined]  # 创建循环引用
        result = _serialize_analysis(obj)
        assert isinstance(result, str)


class TestOnEventException:
    def test_on_event_swallows_exception(self) -> None:
        """_on_event 内部异常被静默吞掉，不向外抛出。"""
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()

        # 构造一个会让 getattr(event, "type") 正常但后续处理抛异常的 event
        bad_event = Mock()
        bad_event.type = "step.start"
        bad_event.step = 1
        bad_event.payload = {}

        # 让 append_event 抛异常
        with patch.object(rjob, "append_event", side_effect=RuntimeError("boom")):
            runner._on_event(rjob, bad_event, agent=Mock())  # 不应抛异常


class TestUpdateStepLockedExisting:
    def test_find_existing_step(self) -> None:
        """_update_step_locked 找到已存在的步骤时返回该步骤。"""
        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        # 第一次调用创建步骤
        entry1 = runner._update_step_locked(rjob, 1)
        assert entry1["step"] == 1
        # 第二次调用应找到已存在的步骤
        entry2 = runner._update_step_locked(rjob, 1)
        assert entry2 is entry1
        assert len(rjob.steps) == 1


class TestFinalizeStepExceptions:
    def test_confidence_attr_raises(self) -> None:
        """_finalize_step 中读取 _last_confidence 抛异常时被捕获。"""

        class RaisingConfAgent:
            @property
            def _last_confidence(self):  # type: ignore[override]
                raise RuntimeError("conf err")

            _hook_data_cache: dict = {}
            _network_log: list = []

        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        rjob._step_starts[1] = time.time()

        runner._finalize_step(rjob, 1, RaisingConfAgent())  # 不应抛异常

    def test_hook_cache_attr_raises(self) -> None:
        """_finalize_step 中读取 _hook_data_cache 抛异常时被捕获。"""

        class RaisingHookAgent:
            _last_confidence = None

            @property
            def _hook_data_cache(self):  # type: ignore[override]
                raise RuntimeError("hook err")

            _network_log: list = []

        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        rjob._step_starts[1] = time.time()

        runner._finalize_step(rjob, 1, RaisingHookAgent())  # 不应抛异常

    def test_network_log_attr_raises(self) -> None:
        """_finalize_step 中读取 _network_log 抛异常时被捕获。"""

        class RaisingNetAgent:
            _last_confidence = None
            _hook_data_cache: dict = {}

            @property
            def _network_log(self):  # type: ignore[override]
                raise RuntimeError("net err")

        runner = ReverseAgentRunner()
        rjob = _make_reverse_job()
        rjob._step_starts[1] = time.time()

        runner._finalize_step(rjob, 1, RaisingNetAgent())  # 不应抛异常


class TestHandlerScreenshotEdge:
    def test_screenshot_invalid_step(self, http_server: str, tmp_path: Path) -> None:
        """step 参数非数字时回退为 0。"""
        png_file = tmp_path / "shot.png"
        png_file.write_bytes(b"png_data")

        rjob = _make_reverse_job(id="si1")
        rjob.screenshots.append({"step": 0, "path": str(png_file), "error": False})
        ui.REVERSE_JOBS["si1"] = rjob

        resp = httpx.get(f"{http_server}/reverse/screenshot?id=si1&step=abc")
        assert resp.status_code == 200
        assert b"png_data" in resp.content

    def test_screenshot_fallback_any_step(self, http_server: str, tmp_path: Path) -> None:
        """无精确匹配时回退到该 step 的任一截图。"""
        png_file = tmp_path / "shot.png"
        png_file.write_bytes(b"fallback_png")

        rjob = _make_reverse_job(id="sf1")
        # 有 step=1 的截图但不匹配 error=1
        rjob.screenshots.append({"step": 1, "path": str(png_file), "error": False})
        ui.REVERSE_JOBS["sf1"] = rjob

        # 请求 error=1 截图，无精确匹配 → 回退到 step=1 的任一截图
        resp = httpx.get(f"{http_server}/reverse/screenshot?id=sf1&step=1&error=1")
        assert resp.status_code == 200
        assert b"fallback_png" in resp.content

    def test_screenshot_not_found_after_fallback(self, http_server: str) -> None:
        """回退后仍无匹配截图时返回 404。"""
        rjob = _make_reverse_job(id="sn1")
        rjob.screenshots.append({"step": 1, "path": "/tmp/x.png", "error": False})
        ui.REVERSE_JOBS["sn1"] = rjob

        resp = httpx.get(f"{http_server}/reverse/screenshot?id=sn1&step=99")
        assert resp.status_code >= 400


class TestJobsCleanup:
    def test_jobs_cleanup_over_max(self, http_server: str) -> None:
        """JOBS 超过 MAX_JOBS 时清理已完成任务。"""
        # 填充大量已完成任务
        for i in range(ui.MAX_JOBS + 5):
            job = _make_job_state(id=f"old{i}")
            job.status = "done"
            ui.JOBS[f"old{i}"] = job

        # 发起 /run 请求触发清理
        with patch.object(ui, "run_job"):
            resp = httpx.post(
                f"{http_server}/run",
                data={"url": "https://example.com", "out": "", "max_pages": "1"},
            )
        assert resp.status_code == 200
        # 已完成的任务应被清理
        assert len(ui.JOBS) <= ui.MAX_JOBS + 1


class TestReverseJobsCleanup:
    def test_reverse_jobs_cleanup_over_max(self, http_server: str) -> None:
        """REVERSE_JOBS 超过 MAX_REVERSE_JOBS 时清理已完成任务。"""
        for i in range(ui.MAX_REVERSE_JOBS + 5):
            rjob = _make_reverse_job(id=f"rold{i}")
            rjob.status = "done"
            ui.REVERSE_JOBS[f"rold{i}"] = rjob

        with patch.object(ui, "run_reverse_job"):
            resp = httpx.post(
                f"{http_server}/reverse/run",
                data={"url": "https://example.com", "task": "提取签名"},
            )
        assert resp.status_code == 200
        assert len(ui.REVERSE_JOBS) <= ui.MAX_REVERSE_JOBS + 1


class TestReadJsonBodyEmpty:
    def test_import_empty_body(self, http_server: str) -> None:
        """POST /reverse/config/import 空请求体返回错误。"""
        resp = httpx.post(
            f"{http_server}/reverse/config/import",
            content=b"",
            headers={"content-type": "application/json"},
        )
        assert "error" in resp.json()


# ========== 任务历史 API ==========


class TestJobsHistoryApi:
    """/jobs 系列任务历史 API 集成测试，使用临时 SQLite 数据库隔离。"""

    @pytest.fixture(autouse=True)
    def temp_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """每个测试使用独立的临时数据库，避免污染真实数据。"""
        monkeypatch.setattr(ui.database, "_DB_PATH", str(tmp_path / "test_jobs.db"))
        ui.database.init_db()
        yield

    def test_get_jobs_empty(self, http_server: str) -> None:
        """GET /jobs 在空数据库上返回空任务列表。"""
        resp = httpx.get(f"{http_server}/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tasks"] == []
        assert data["total"] == 0
        assert data["page"] == 1
        assert data["page_size"] == 20

    def test_get_jobs_after_create(self, http_server: str) -> None:
        """创建任务后 GET /jobs 返回该任务。"""
        ui.database.create_task("job-1", "https://example.com", {"max_pages": 1}, "/tmp/out")
        resp = httpx.get(f"{http_server}/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["tasks"][0]["id"] == "job-1"
        assert data["tasks"][0]["url"] == "https://example.com"

    def test_get_job_detail(self, http_server: str) -> None:
        """GET /jobs/<id> 返回单个任务详情。"""
        ui.database.create_task("job-2", "https://detail.example.com", {"k": "v"}, "/tmp/out2")
        resp = httpx.get(f"{http_server}/jobs/job-2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "job-2"
        assert data["url"] == "https://detail.example.com"
        assert data["config"] == {"k": "v"}

    def test_get_job_detail_not_found(self, http_server: str) -> None:
        """GET /jobs/<不存在的id> 返回 404。"""
        resp = httpx.get(f"{http_server}/jobs/nonexistent-id")
        assert resp.status_code == 404

    def test_get_job_results_empty(self, http_server: str) -> None:
        """GET /jobs/<id>/results 在无采集结果时返回空列表。"""
        ui.database.create_task("job-3", "https://results.example.com", {}, "/tmp/out3")
        resp = httpx.get(f"{http_server}/jobs/job-3/results")
        assert resp.status_code == 200
        data = resp.json()
        assert data["results"] == []
        assert data["total"] == 0

    def test_delete_job_success(self, http_server: str) -> None:
        """DELETE /jobs/<id> 成功删除已存在任务。"""
        ui.database.create_task("job-4", "https://del.example.com", {}, "/tmp/out4")
        resp = httpx.delete(f"{http_server}/jobs/job-4")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert ui.database.get_task("job-4") is None

    def test_delete_job_not_found(self, http_server: str) -> None:
        """DELETE /jobs/<不存在的id> 返回 ok:false。"""
        resp = httpx.delete(f"{http_server}/jobs/nonexistent-id")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_post_run_writes_db(self, http_server: str) -> None:
        """POST /run 后数据库中存在对应的任务记录。"""
        with patch.object(ui, "run_job"):
            resp = httpx.post(
                f"{http_server}/run",
                data={"url": "https://run.example.com", "out": "", "max_pages": "1"},
            )
            assert resp.status_code == 200
            job_id = resp.json()["id"]
            task = ui.database.get_task(job_id)
        assert task is not None
        assert task["url"] == "https://run.example.com"


class TestCsrfOriginCheck:
    """跨站请求（Origin/Referer 非本机）应被拒绝。"""

    def test_post_run_cross_origin_rejected(self, http_server: str) -> None:
        with patch.object(ui, "run_job") as mock_run:
            resp = httpx.post(
                f"{http_server}/run",
                data={"url": "https://example.com", "out": "", "max_pages": "1"},
                headers={"Origin": "https://evil.example.com"},
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        mock_run.assert_not_called()

    def test_post_run_cross_origin_referer_rejected(self, http_server: str) -> None:
        with patch.object(ui, "run_job") as mock_run:
            resp = httpx.post(
                f"{http_server}/run",
                data={"url": "https://example.com", "out": "", "max_pages": "1"},
                headers={"Referer": "https://evil.example.com/attack.html"},
            )
        assert resp.json()["ok"] is False
        mock_run.assert_not_called()

    def test_post_run_same_origin_allowed(self, http_server: str) -> None:
        with patch.object(ui, "run_job"):
            resp = httpx.post(
                f"{http_server}/run",
                data={"url": "https://example.com", "out": "", "max_pages": "1"},
                headers={"Origin": http_server},
            )
        assert resp.json().get("ok") is not False

    def test_delete_cross_origin_rejected(self, http_server: str) -> None:
        resp = httpx.delete(
            f"{http_server}/jobs/some-id",
            headers={"Origin": "https://evil.example.com"},
        )
        assert resp.json()["ok"] is False

    def test_origin_null_rejected(self, http_server: str) -> None:
        """sandboxed iframe 的 Origin: null 不可信,应被拒绝。"""
        with patch.object(ui, "run_job") as mock_run:
            resp = httpx.post(
                f"{http_server}/run",
                data={"url": "https://example.com", "out": "", "max_pages": "1"},
                headers={"Origin": "null"},
            )
        assert resp.json()["ok"] is False
        mock_run.assert_not_called()

    def test_origin_non_http_scheme_rejected(self, http_server: str) -> None:
        """Origin 不是 http(s) 方案（如 file://）时拒绝。"""
        with patch.object(ui, "run_job") as mock_run:
            resp = httpx.post(
                f"{http_server}/run",
                data={"url": "https://example.com", "out": "", "max_pages": "1"},
                headers={"Origin": "file:///etc/passwd"},
            )
        assert resp.json()["ok"] is False
        mock_run.assert_not_called()


class TestLoopbackHost:
    def test_loopback_hosts_allowed(self) -> None:
        assert ui._is_loopback_host("127.0.0.1") is True
        assert ui._is_loopback_host("localhost") is True
        assert ui._is_loopback_host("::1") is True
        assert ui._is_loopback_host("127.0.0.2") is True

    def test_non_loopback_hosts_rejected(self) -> None:
        assert ui._is_loopback_host("0.0.0.0") is False
        assert ui._is_loopback_host("192.168.1.10") is False
        assert ui._is_loopback_host("example.com") is False

    def test_main_rejects_non_loopback_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["ui", "--host", "0.0.0.0"])
        with pytest.raises(SystemExit):
            ui.main()


class TestRunValidation:
    """表单数字字段服务端校验（防止线程爆炸/SystemExit 杀 handler）。"""

    def test_workers_out_of_range(self, http_server: str) -> None:
        resp = httpx.post(
            f"{http_server}/run",
            data={"url": "https://example.com", "workers": "100000", "out": ""},
        )
        assert resp.json()["ok"] is False
        assert "workers" in resp.json()["error"]

    def test_workers_non_numeric(self, http_server: str) -> None:
        resp = httpx.post(
            f"{http_server}/run",
            data={"url": "https://example.com", "workers": "abc", "out": ""},
        )
        assert resp.json()["ok"] is False
        assert "workers" in resp.json()["error"]

    def test_retries_negative(self, http_server: str) -> None:
        resp = httpx.post(
            f"{http_server}/run",
            data={"url": "https://example.com", "retries": "-1", "out": ""},
        )
        assert resp.json()["ok"] is False
        assert "retries" in resp.json()["error"]

    def test_valid_bounds_accepted(self, http_server: str) -> None:
        with patch.object(ui, "run_job"):
            resp = httpx.post(
                f"{http_server}/run",
                data={"url": "https://example.com", "workers": "64", "out": ""},
            )
        assert "id" in resp.json()

    def test_empty_numeric_fields_use_defaults(self, http_server: str) -> None:
        """空字符串数字字段回退默认值（覆盖校验器默认分支）。"""
        with patch.object(ui, "run_job"):
            resp = httpx.post(
                f"{http_server}/run",
                data={
                    "url": "https://example.com",
                    "workers": "",
                    "retries": "",
                    "delay": "",
                    "timeout": "",
                    "max_bytes": "",
                    "max_pages": "",
                    "out": "",
                },
            )
        assert "id" in resp.json()

    def test_delay_negative_rejected(self, http_server: str) -> None:
        resp = httpx.post(
            f"{http_server}/run",
            data={"url": "https://example.com", "delay": "-1", "out": ""},
        )
        assert resp.json()["ok"] is False
        assert "delay" in resp.json()["error"]

    def test_timeout_too_small_rejected(self, http_server: str) -> None:
        resp = httpx.post(
            f"{http_server}/run",
            data={"url": "https://example.com", "timeout": "0", "out": ""},
        )
        assert resp.json()["ok"] is False
        assert "timeout" in resp.json()["error"]


class TestSingleCrawlGuard:
    """同一时间只允许一个采集任务,防止日志/共享 opener 串线。"""

    def test_run_rejected_while_running(self, http_server: str) -> None:
        job = _make_job_state(id="busy1")
        ui.JOBS["busy1"] = job  # status 默认为 running
        with patch.object(ui, "run_job") as mock_run:
            resp = httpx.post(
                f"{http_server}/run",
                data={"url": "https://example.com", "out": "", "max_pages": "1"},
            )
        assert resp.json()["ok"] is False
        assert "已有采集任务" in resp.json()["error"]
        mock_run.assert_not_called()

    def test_run_allowed_after_terminal(self, http_server: str) -> None:
        job = _make_job_state(id="done1")
        job.status = "done"
        ui.JOBS["done1"] = job
        with patch.object(ui, "run_job"):
            resp = httpx.post(
                f"{http_server}/run",
                data={"url": "https://example.com", "out": "", "max_pages": "1"},
            )
        assert "id" in resp.json()


class TestJobLogHandler:
    """crawler 日志经 JobLogHandler 转发到 job.log（替代失效的 redirect_stdout）。"""

    def test_crawler_logs_captured(self, tmp_path: Path) -> None:
        job = _make_job_state(output_dir=str(tmp_path))
        job.args = Mock()
        logger = ui.web_resource_crawler._log
        old_level = logger.level
        logger.setLevel(logging.INFO)  # pytest 下 root 默认 WARNING,显式放开 INFO

        def fake_crawl(args: Any) -> int:
            ui.web_resource_crawler._log.info("hello from crawler logger")
            return 0

        try:
            with patch.object(ui.web_resource_crawler, "crawl", side_effect=fake_crawl):
                run_job(job)
        finally:
            logger.setLevel(old_level)
        assert "hello from crawler logger" in job.log

    def test_handler_detached_after_run(self, tmp_path: Path) -> None:
        job = _make_job_state(output_dir=str(tmp_path))
        job.args = Mock()
        with patch.object(ui.web_resource_crawler, "crawl", return_value=0):
            run_job(job)
        # 任务结束后 logger 不再向该 job 写日志
        ui.web_resource_crawler._log.info("after run")
        assert "after run" not in job.log


class TestTaskConfigForDb:
    def test_header_excluded(self) -> None:
        """入库配置剔除 header（含 Cookie）,避免明文落库。"""
        form = {
            "url": ["https://example.com"],
            "cookie": ["session=secret"],
            "headers": ["Authorization: Bearer tok"],
        }
        args = build_args(form)
        config = ui._task_config_for_db(args)
        assert "header" not in config
        assert "session=secret" not in json.dumps(config)
        assert "Bearer tok" not in json.dumps(config)

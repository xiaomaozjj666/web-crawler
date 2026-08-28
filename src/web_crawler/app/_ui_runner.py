"""Web UI 的任务执行 runner（从 ``ui.py`` 拆出）。

包含采集任务子线程入口（run_job / wait_for_resume）与 JS 逆向
Agent 的子线程执行器（ReverseAgentRunner / run_reverse_job）。
"""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path

from web_crawler.app import crawler as web_resource_crawler
from web_crawler.app import db as database

from ._ui_helpers import _serialize_analysis
from ._ui_state import JobLogHandler, JobState, JobWriter, ReverseJobState

_log = logging.getLogger(__name__)


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


class ReverseAgentRunner:
    """在子线程中启动 ReverseAgent，订阅 EventBus，把事件推到 ReverseJobState。

    停止策略：UI 的"停止"按钮设置 job.stop_event；构造 agent 时透传
    should_stop=job.stop_event.is_set，库侧在每步循环顶部检查该回调，
    返回 True 立即中断循环并把结果标记为 stopped——正在执行的
    Playwright 动作完成后不再进入下一步，无需等 Agent 自然结束。
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


def run_reverse_job(job: ReverseJobState) -> None:
    """子线程入口：启动 ReverseAgentRunner。"""
    job.status = "running"
    ReverseAgentRunner().run_job(job)

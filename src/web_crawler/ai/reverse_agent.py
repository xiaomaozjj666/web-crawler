"""JS 逆向 Agent 主循环模块。

编排浏览器、Hook、AI 分析器、验证码处理，形成"观察-思考-行动"的自主循环，
用于定位网页前端动态生成的加密参数（如 ``Anti-Content``、``X-Bogus``、
``_signature`` 等）。

工作流程
--------
1. 启动 CamoufoxFetcher（默认 headless=False，便于人工介入）；
2. 创建新的浏览器上下文，在导航前通过 ``add_init_script`` 注入 Hook 脚本；
3. 进入循环：
   - 观察：收集 Hook 捕获数据、网络请求、页面脚本、验证码检测；
   - 思考：把观察结果交给 DeepSeek-V4-Pro 决定下一步动作；
   - 行动：执行 AI 决定的动作（注入新 Hook / 分析 JS / 等待 /
     提取参数 / 处理验证码 / 完成）；
4. 达到 ``max_steps`` 或 AI 返回 ``done`` 时停止；
5. 返回包含 ``success``、``target_params_found``、``analysis``、
   ``hook_data``、``steps``、``history`` 的结果字典。

错误处理
--------
- 浏览器崩溃：尝试重启一次；
- Hook 注入失败：记录到 history 后继续；
- AI 分析失败：降级为纯 Hook 模式（仅靠 Hook 数据推进循环）；
- 所有异常都写入 history，便于事后审计。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from typing_extensions import Self

from ..fetchers.camoufox import CamoufoxFetcher
from .analyzer import AnalysisResult, JSAnalyzer, JSFragment
from .budget import BudgetTracker, TokenBudget
from .captcha import CaptchaManager, CaptchaType
from .checkpoint import Checkpoint, CheckpointManager, CheckpointStore
from .confidence import ConfidenceResult, ConfidenceScorer
from .dom_pruner import DomPruner, PrunedDom
from .guardrails import ActionGuard, GuardrailResult
from .hooks import collect_hook_data, generate_combined_script
from .judge import JudgeResult, TaskJudge
from .llm import DEFAULT_MODEL, DeepSeekProvider, LLMMessage, LLMProvider
from .loop import ContextCompressor, LoopDetector
from .planner import Plan, Planner
from .recorder import RunRecorder
from .schema import SchemaValidator
from .watchdog import (
    EVENT_ACTION,
    EVENT_DONE,
    EVENT_OBSERVATION,
    EVENT_OBSERVE_ERROR,
    EVENT_STEP_END,
    EVENT_STEP_START,
    EVENT_THINK_ERROR,
    CrashRecovery,
    EventBus,
    Heartbeat,
)

# ---------------------------------------------------------------------------
# 常量与 Prompt
# ---------------------------------------------------------------------------

_THINK_SYSTEM_PROMPT = (
    "你是 JS 逆向专家 Agent。你的任务是分析网页的加密参数生成逻辑。"
    "你会收到当前页面的观察结果（URL、Hook 捕获数据、网络请求、脚本列表、"
    "验证码类型、DOM 摘要）以及历史动作。请基于这些信息决定下一步动作。"
)

_THINK_USER_TEMPLATE = (
    "## 任务\n{task}\n\n"
    "## 当前观察\n"
    "- URL: {url}\n"
    "- 页面标题: {page_title}\n"
    "- 验证码类型: {captcha_type}\n"
    "- Hook 数据条数: {hook_count}\n"
    "- 网络请求数: {network_count}\n"
    "- 页面脚本数: {script_count}\n\n"
    "## Hook 数据摘录（最多 20 条）\n{hook_summary}\n\n"
    "## 网络请求摘录（最多 20 条）\n{network_summary}\n\n"
    "## 页面脚本列表（最多 20 个）\n{script_summary}\n\n"
    "## DOM 摘要（前 2000 字符）\n{dom_summary}\n\n"
    "## 历史动作（最近 10 步）\n{history_summary}\n\n"
    "## 目标参数\n{target_params}\n\n"
    "请决定下一步动作，仅输出一个 JSON 对象（不要任何额外文字，不要 Markdown 代码块标记），格式如下：\n"
    "{{\n"
    '  "action_type": "navigate | inject_hook | analyze_js | wait | extract | solve_captcha | done",\n'
    '  "params": {{...}},\n'
    '  "reasoning": "你的推理过程"\n'
    "}}\n\n"
    "动作说明：\n"
    '- navigate: 导航到新 URL，params: {{"url": "..."}}\n'
    '- inject_hook: 注入新的 Hook，params: {{"hooks": ["fetch_hook", ...]}}\n'
    '- analyze_js: 分析捕获的 JS，params: {{"script_urls": ["..."], "target_params": [...]}}\n'
    '- wait: 等待一段时间，params: {{"seconds": 3.0}}\n'
    '- extract: 尝试从 Hook 数据中提取目标参数，params: {{"param_name": "..."}}\n'
    "- solve_captcha: 处理验证码，params: {}\n"
    '- done: 任务完成，params: {{"success": true/false, "summary": "..."}}\n'
)

# JSON / 代码块解析正则
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"^```(?:\w+)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)

# 拉取 JS 源码用的默认 UA
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _extract_json(text: str) -> dict[str, Any]:
    """容错解析模型回复中的 JSON 对象，兼容代码块包裹与正文嵌入。"""
    text = text.strip()
    fence = _CODE_FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    return {}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """单步观察结果，描述当前页面状态。"""

    url: str
    hook_data: dict
    network_requests: list[dict]
    scripts: list[str]
    captcha_type: CaptchaType
    page_title: str
    dom_summary: str
    # 当前步截图保存路径（启用 enable_screenshot 时由 _observe 写入）
    screenshot_path: str = ""


@dataclass
class Action:
    """AI 决定的下一步动作。"""

    action_type: str
    params: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        """从 LLM 返回的 dict 构造 Action。"""
        return cls(
            action_type=str(data.get("action_type", "wait")),
            params=dict(data.get("params") or {}),
            reasoning=str(data.get("reasoning") or ""),
        )


@dataclass
class ReverseAgentConfig:
    """JS 逆向 Agent 配置。"""

    max_steps: int = 20
    hooks: list[str] | None = None
    headless: bool = False
    wait_after_navigate: float = 3.0
    target_params: list[str] | None = None
    proxy: str | None = None
    os_name: str = "windows"
    # Planner：周期重规划间隔（步），None 表示禁用 Planner
    planner_interval: int | None = 5
    # LoopDetector：触发循环的重复次数阈值
    loop_threshold: int = 3
    # ContextCompressor：历史压缩阈值（步）
    max_history: int = 25
    # Judge：是否启用 done 二次验证
    enable_judge: bool = True
    # Judge：严格模式（缺任一目标参数直接判失败）
    judge_strict: bool = True
    # Recorder：是否启用成功路径编译
    enable_recorder: bool = True
    # Watchdog：步进心跳超时（秒），超过即视为卡死
    heartbeat_timeout: float = 120.0
    # Watchdog：崩溃重试次数
    max_retries: int = 2
    # DomPruner：DOM 焦点裁剪字符上限，0 表示禁用
    dom_prune_max_chars: int = 0
    # DomPruner：是否启用 LLM 重要性评分
    dom_prune_llm_rank: bool = False
    # Checkpoint：是否启用断点续跑
    enable_checkpoint: bool = False
    # Checkpoint：保存间隔（步）
    checkpoint_interval: int = 1
    # Checkpoint：滚动保留数量
    checkpoint_keep: int = 5
    # Budget：全局 token 上限，None 表示不限制
    budget_total: int | None = 100_000
    # Budget：单步 token 上限
    budget_per_step: int | None = 8_000
    # Confidence：动作置信度阈值，低于此值触发 fallback（0-1）
    min_confidence: float = 0.4
    # Confidence：是否启用 LLM 评分
    confidence_llm_score: bool = False
    # Guard：是否启用危险动作护栏
    enable_guard: bool = True
    # Guard：允许导航的域名白名单（None 不限制）
    allowed_domains: list[str] | None = None
    # Screenshot：是否在每步观察和错误时保存页面截图（PNG）
    enable_screenshot: bool = True


# ---------------------------------------------------------------------------
# 主 Agent
# ---------------------------------------------------------------------------


class ReverseAgent:
    """JS 逆向 Agent 主循环。

    编排浏览器、Hook、AI 分析器、验证码处理，形成"观察-思考-行动"的自主循环。
    同步入口 :meth:`run` 与异步入口 :meth:`arun` 共享同一套配置与分析器，
    适用于定位前端动态生成的加密参数（Anti-Content / X-Bogus / _signature 等）。
    """

    def __init__(
        self,
        config: ReverseAgentConfig | None = None,
        provider: LLMProvider | None = None,
        analyzer: JSAnalyzer | None = None,
        *,
        event_bus: EventBus | None = None,
    ) -> None:
        self.config = config or ReverseAgentConfig()
        self.provider = provider or DeepSeekProvider(model=DEFAULT_MODEL)
        self.analyzer = analyzer or JSAnalyzer(provider=self.provider)
        self.captcha_manager = CaptchaManager()
        self.fetcher: CamoufoxFetcher | None = None
        self._context: Any = None
        self._page: Any = None
        # 网络请求监听日志（由 page.on("request") 写入）
        self._network_log: list[dict] = []
        # 最近一次观察的 hook 数据缓存，供 _try_extract_param 复用
        self._hook_data_cache: dict = {"records": [], "count": 0}

        # -- 双脑分离组件 ----------------------------------------------------
        # Planner 用与 Actor 相同 provider；外部可注入更强模型做 planner。
        self.planner: Planner | None = (
            Planner(self.provider, planner_interval=self.config.planner_interval)
            if self.config.planner_interval
            else None
        )
        self._current_plan: Plan | None = None

        # -- 循环检测 + 上下文压缩 -------------------------------------------
        self.loop_detector = LoopDetector(threshold=self.config.loop_threshold)
        self.context_compressor = ContextCompressor(
            self.provider,
            max_history=self.config.max_history,
        )

        # -- 任务完成二次验证 ------------------------------------------------
        self.judge: TaskJudge | None = (
            TaskJudge(self.provider, strict=self.config.judge_strict)
            if self.config.enable_judge
            else None
        )
        self._last_judge_result: JudgeResult | None = None

        # -- 成功路径编译 ----------------------------------------------------
        self.recorder: RunRecorder | None = RunRecorder() if self.config.enable_recorder else None
        # 最近一次编译产出的脚本源码（run 结束后可用）
        self._compiled_script: str = ""

        # -- 事件总线 + 心跳 + 崩溃恢复 -------------------------------------
        self.event_bus = event_bus or EventBus()
        self.heartbeat = Heartbeat(
            max_interval=self.config.heartbeat_timeout,
            on_stall=self._on_stall,
        )
        self.crash_recovery = CrashRecovery(
            max_retries=self.config.max_retries,
            bus=self.event_bus,
        )

        # -- 结构化抽取 schema 验证 -----------------------------------------
        # 默认 schema 为目标参数表（dict[str, str]），可在 extract 时启用
        self.schema_validator: SchemaValidator | None = None

        # -- DOM 焦点裁剪（Skyvern/browser-use 风格） ----------------------
        # 仅当 dom_prune_max_chars > 0 时启用
        self.dom_pruner: DomPruner | None = (
            DomPruner(
                max_chars=self.config.dom_prune_max_chars,
                enable_llm_rank=self.config.dom_prune_llm_rank,
                provider=self.provider,
            )
            if self.config.dom_prune_max_chars > 0
            else None
        )
        # 最近一次裁剪结果（便于上游调试与事件订阅）
        self._last_pruned_dom: PrunedDom | None = None

        # -- 断点续跑 --------------------------------------------------------
        self.checkpoint_manager: CheckpointManager = CheckpointManager(
            enable=self.config.enable_checkpoint,
            save_interval=self.config.checkpoint_interval,
            store=CheckpointStore(keep=self.config.checkpoint_keep),
        )
        # 最近一次 resume 加载的 checkpoint（None 表示非 resume 启动）
        self._resume_from: Checkpoint | None = None

        # -- Token 预算管理 --------------------------------------------------
        self.budget_tracker: BudgetTracker = BudgetTracker(
            budget=TokenBudget(
                total=self.config.budget_total,
                per_step=self.config.budget_per_step,
            )
        )

        # -- 动作置信度评分 -------------------------------------------------
        self.confidence_scorer: ConfidenceScorer = ConfidenceScorer(
            min_confidence=self.config.min_confidence,
            enable_llm_score=self.config.confidence_llm_score,
            provider=self.provider,
        )
        self._last_confidence: ConfidenceResult | None = None

        # -- 危险动作护栏 ---------------------------------------------------
        self.guard: ActionGuard | None = (
            ActionGuard(allowed_domains=self.config.allowed_domains)
            if self.config.enable_guard
            else None
        )
        self._last_guard_result: GuardrailResult | None = None

        # -- Budget / Confidence 共享的 LLM 调用缓存 ---------------------
        # _think 内部更新这两个字段，主循环用它们做 token 估算
        self._last_think_prompt: str = ""
        self._last_think_completion: str = ""
        # provider 返回的真实 usage dict（若有），优先用于 budget 记账
        self._last_llm_usage: dict[str, Any] | None = None

        # -- 截图缓存（每步截图路径收集，run/arun 结束后写入 result dict）---
        self._screenshots: list[dict[str, Any]] = []
        # 最近一次错误截图路径（供 UI 高亮展示）
        self._last_error_screenshot: str = ""

    # ------------------------------------------------------------------
    # 事件总线便捷方法
    # ------------------------------------------------------------------

    def _emit(self, type_: str, *, step: int = 0, **payload: Any) -> None:
        """便捷：通过事件总线发布事件。"""
        self.event_bus.emit(type_, step=step, **payload)

    def _on_stall(self, step: int, elapsed: float) -> None:
        """Heartbeat 触发卡死时的回调。"""
        self._emit(
            "stall",
            step=step,
            elapsed=elapsed,
            message=f"no step progress for {elapsed:.1f}s",
        )

    # ------------------------------------------------------------------
    # 主入口（同步）
    # ------------------------------------------------------------------

    def run(self, url: str, task: str = "") -> dict:
        """启动浏览器、注入 Hook、循环观察-思考-行动，返回结果字典。"""
        self.fetcher = CamoufoxFetcher(
            headless=self.config.headless,
            os=self.config.os_name,
            proxy=self.config.proxy,
            network_idle=False,
        )
        # 重置所有有状态组件
        self.loop_detector.reset()
        self.context_compressor.reset()
        self.heartbeat.reset()
        self.crash_recovery.reset()
        if self.recorder is not None:
            self.recorder.reset()
            self.recorder.set_target(url)
        self._current_plan = None
        self._last_judge_result = None
        self._compiled_script = ""
        # 重置新组件
        self.budget_tracker = BudgetTracker(
            budget=TokenBudget(
                total=self.config.budget_total,
                per_step=self.config.budget_per_step,
            )
        )
        self._last_pruned_dom = None
        self._last_confidence = None
        self._last_guard_result = None
        # 重置截图缓存
        self._screenshots = []
        self._last_error_screenshot = ""

        history: list[dict] = []
        target_params_found: dict[str, str] = {}
        analysis: AnalysisResult | None = None
        last_observation: Observation | None = None

        # 尝试加载断点续跑
        if self.config.enable_checkpoint:
            self._resume_from = self.checkpoint_manager.load_latest()
            if self._resume_from is not None:
                cp = self._resume_from
                history = list(cp.history)
                target_params_found = dict(cp.target_params_found)
                # 还原累积摘要（直接写内部字段，因为 property 是只读的）
                self.context_compressor._cumulative_summary = cp.cumulative_summary
                self._emit(
                    "checkpoint.resume",
                    step=cp.step,
                    url=cp.url,
                    target_params_found=list(target_params_found.keys()),
                )

        try:
            context, page = self._create_page(self.config.hooks)
            self._context = context
            self._page = page

            # resume 时导航回上次 URL，否则导航到入口 url
            nav_url = self._resume_from.url if self._resume_from and self._resume_from.url else url
            try:
                page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                history.append({"step": 0, "event": "navigate_error", "error": str(exc)})
            time.sleep(self.config.wait_after_navigate)

            # resume 时重新注入已记录的 hooks
            if self._resume_from and self._resume_from.hooks:
                self._inject_hooks(page, self._resume_from.hooks)

            # resume 时跳过已完成的步号
            start_step = (self._resume_from.step + 1) if self._resume_from else 1
            if start_step > self.config.max_steps:
                self._emit(EVENT_DONE, step=0, success=True, reason="resume已完成所有步骤")
            for step in range(start_step, self.config.max_steps + 1):
                self._emit(EVENT_STEP_START, step=step)
                try:
                    observation = self._observe(page, step=step)
                    last_observation = observation
                    self._emit(
                        EVENT_OBSERVATION,
                        step=step,
                        url=observation.url,
                        hook_count=observation.hook_data.get("count", 0),
                        network_count=len(observation.network_requests),
                        script_count=len(observation.scripts),
                        screenshot_path=observation.screenshot_path,
                    )
                except Exception as exc:
                    history.append({"step": step, "event": "observe_error", "error": str(exc)})
                    self._emit(EVENT_OBSERVE_ERROR, step=step, error=str(exc))
                    # 崩溃恢复策略：CrashRecovery 控制重试次数
                    should_continue = self.crash_recovery.attempt(
                        lambda: self._try_recover_page(url),
                        step=step,
                        error=exc,
                    )
                    if should_continue:
                        continue
                    break

                # -- 循环检测：触发即重规划 -----------------------------
                loop_result = self.loop_detector.observe(observation, step=step)
                if loop_result.detected:
                    self._emit(
                        "loop.detected",
                        step=step,
                        repeated_count=loop_result.repeated_count,
                    )
                    if self.planner is not None:
                        # 强制重规划
                        self._current_plan = self.planner.make_plan(
                            task,
                            observation,
                            step=step,
                            history_summary=self.context_compressor.cumulative_summary,
                            target_params=self.config.target_params,
                        )
                        self._emit(
                            "plan",
                            step=step,
                            trigger="loop",
                            subgoals=[sg.to_dict() for sg in self._current_plan.subgoals],
                        )
                        self.loop_detector.reset()

                # -- 周期重规划 -----------------------------------------
                if self.planner is not None and (
                    self._current_plan is None
                    or self._current_plan.is_complete
                    or (step - (self._current_plan.created_at_step or 0))
                    >= self.planner.planner_interval
                ):
                    self._current_plan = self.planner.make_plan(
                        task,
                        observation,
                        step=step,
                        history_summary=self.context_compressor.cumulative_summary,
                        target_params=self.config.target_params,
                    )
                    self._emit(
                        "plan",
                        step=step,
                        trigger="interval",
                        subgoals=[sg.to_dict() for sg in self._current_plan.subgoals],
                    )

                # -- 上下文压缩 -----------------------------------------
                history, compressed = self.context_compressor.maybe_compress(history)
                if compressed:
                    self._emit("context.compressed", step=step)

                try:
                    action = self._think(observation, task, history, plan=self._current_plan)
                    # Token 预算记账：用 think prompt 估算 + completion 估算
                    self.budget_tracker.record_call(
                        step=step,
                        prompt_text=self._last_think_prompt or "",
                        completion_text=self._last_think_completion or "",
                        usage=self._last_llm_usage,
                    )
                    self._emit(
                        EVENT_ACTION,
                        step=step,
                        action_type=action.action_type,
                        reasoning=action.reasoning,
                    )
                except Exception as exc:
                    history.append({"step": step, "event": "think_error", "error": str(exc)})
                    self._emit(EVENT_THINK_ERROR, step=step, error=str(exc))
                    # 错误时截图（失败不抛异常）
                    self._take_screenshot(page, step, error=True)
                    action = self._fallback_action(observation)

                # -- Confidence 评分：低分动作触发 fallback ----------------
                conf_result = self.confidence_scorer.score(
                    action,
                    task=task,
                    target_params=self.config.target_params,
                    history=history,
                )
                self._last_confidence = conf_result
                if conf_result.score < self.config.min_confidence:
                    self._emit(
                        "confidence.low",
                        step=step,
                        score=conf_result.score,
                        reasons=conf_result.reasons,
                    )
                    action = self._fallback_action(observation)

                # -- Token 预算检查 ----------------------------------------
                if self.budget_tracker.should_compress():
                    self._emit("budget.compress", step=step, **self.budget_tracker.summary())
                    history, _ = self.context_compressor.force_compress(history)
                if self.budget_tracker.should_stop():
                    self._emit("budget.exceeded", step=step, **self.budget_tracker.summary())
                    history.append(
                        {
                            "step": step,
                            "event": "budget_exceeded",
                            "summary": self.budget_tracker.summary(),
                        }
                    )
                    break

                history.append(
                    {
                        "step": step,
                        "action": action.action_type,
                        "params": action.params,
                        "reasoning": action.reasoning,
                        "observation": {
                            "url": observation.url,
                            "hook_count": observation.hook_data.get("count", 0),
                            "network_count": len(observation.network_requests),
                            "script_count": len(observation.scripts),
                            "captcha_type": observation.captcha_type.value,
                        },
                        "current_subgoal": (
                            self._current_plan.current_subgoal.description
                            if self._current_plan and self._current_plan.current_subgoal
                            else None
                        ),
                        "confidence": conf_result.score,
                    }
                )

                # -- 心跳：步进成功 -----------------------------------
                self.heartbeat.tick(step)

                # -- 危险动作护栏：DENY 时跳过执行 ----------------------
                guard_result: GuardrailResult | None = None
                if self.guard is not None:
                    guard_result = self.guard.check(
                        action,
                        context={"url": observation.url, "task": task, "step": step},
                    )
                    self._last_guard_result = guard_result
                    if guard_result.denied:
                        self._emit(
                            "guard.deny",
                            step=step,
                            matched_rules=guard_result.matched_rules,
                            details=guard_result.details,
                        )
                        history.append(
                            {
                                "step": step,
                                "event": "guard_denied",
                                "matched_rules": guard_result.matched_rules,
                                "details": guard_result.details,
                            }
                        )
                        # 跳过本步执行，直接进入下一步
                        self._emit(EVENT_STEP_END, step=step)
                        continue

                if action.action_type == "done":
                    # -- Judge：done 动作二次验证 -------------------------
                    if self.judge is not None and last_observation is not None:
                        judge_result = self.judge.validate(
                            action=action,
                            observation=last_observation,
                            target_params_found=target_params_found,
                            task=task,
                            target_params=self.config.target_params,
                        )
                        self._last_judge_result = judge_result
                        self._emit(
                            "judge.result",
                            step=step,
                            verified=judge_result.verified,
                            missing=judge_result.missing,
                        )
                        if not judge_result.verified:
                            # 验证失败：覆盖 done 动作为 fallback，继续循环
                            history.append(
                                {
                                    "step": step,
                                    "event": "judge_failed",
                                    "missing": judge_result.missing,
                                    "reasoning": judge_result.reasoning,
                                }
                            )
                            action = self._fallback_action(observation)
                        else:
                            self._emit(EVENT_DONE, step=step, success=True)
                            break
                    else:
                        self._emit(EVENT_DONE, step=step)
                        break

                try:
                    result = self._act(page, action)
                    # -- Recorder：记录成功路径 -------------------------
                    if self.recorder is not None:
                        self.recorder.record(
                            step=step,
                            action_type=action.action_type,
                            params=action.params,
                            result_value=(
                                str(result) if action.action_type == "extract" and result else None
                            ),
                            success=True,
                        )
                    if action.action_type == "inject_hook" and result is False:
                        history.append({"step": step, "event": "inject_hook_failed"})
                        if self.recorder is not None:
                            # 标记本步失败，编译时跳过
                            self.recorder._records[-1].success = False
                    elif action.action_type == "extract" and result:
                        param_name = action.params.get("param_name", "")
                        if param_name:
                            target_params_found[param_name] = result
                    elif action.action_type == "analyze_js" and isinstance(result, AnalysisResult):
                        analysis = result
                except Exception as exc:
                    history.append({"step": step, "event": "act_error", "error": str(exc)})
                    self._emit("act.error", step=step, error=str(exc))
                    # 错误时截图（失败不抛异常）
                    self._take_screenshot(page, step, error=True)
                    if self.recorder is not None and self.recorder._records:
                        self.recorder._records[-1].success = False

                self._emit(EVENT_STEP_END, step=step)

                # -- Checkpoint：步末保存断点 ----------------------------------
                if self.config.enable_checkpoint:
                    cp = self.checkpoint_manager.build_checkpoint(
                        step=step,
                        url=last_observation.url if last_observation else "",
                        task=task,
                        target_params_found=target_params_found,
                        target_params=self.config.target_params,
                        hooks=self.config.hooks,
                        history=history,
                        cumulative_summary=self.context_compressor.cumulative_summary,
                        metadata={
                            "confidence": conf_result.score,
                            "budget": self.budget_tracker.summary(),
                            "guard_denied": bool(guard_result and guard_result.denied),
                        },
                    )
                    self.checkpoint_manager.save(cp)

            final_hook_data = self._read_hook_data(page)
            success = bool(target_params_found)
            if self.config.target_params:
                success = all(p in target_params_found for p in self.config.target_params)
            # Judge 验证过的成功才是真成功
            if self.judge is not None and self._last_judge_result is not None:
                success = success and self._last_judge_result.verified

            # -- Recorder：编译成功路径为脚本 -----------------------------
            if self.recorder is not None and success:
                try:
                    self._compiled_script = self.recorder.compile_script()
                except Exception as exc:
                    self._emit("recorder.compile_error", step=0, error=str(exc))
                    self._compiled_script = ""

            return {
                "success": success,
                "target_params_found": target_params_found,
                "analysis": analysis,
                "hook_data": final_hook_data,
                "steps": len(history),
                "history": history,
                "plan": self._current_plan.to_dict() if self._current_plan else None,
                "judge_result": (
                    self._last_judge_result.to_dict() if self._last_judge_result else None
                ),
                "compiled_script": self._compiled_script or None,
                "budget_summary": self.budget_tracker.summary(),
                "last_confidence": (
                    {
                        "score": self._last_confidence.score,
                        "reasons": list(self._last_confidence.reasons),
                        "action_type": getattr(self._last_confidence, "action_type", ""),
                    }
                    if self._last_confidence is not None
                    else None
                ),
                "checkpoints": list(self.checkpoints_snapshot()),
                "screenshots": list(self._screenshots),
                "error_screenshot": self._last_error_screenshot or None,
            }
        finally:
            self._cleanup_sync()

    # ------------------------------------------------------------------
    # 主入口（异步）
    # ------------------------------------------------------------------

    async def arun(self, url: str, task: str = "") -> dict:
        """异步版本的主循环。与 :meth:`run` 行为一致，但所有 IO 都走 async。"""
        self.fetcher = CamoufoxFetcher(
            headless=self.config.headless,
            os=self.config.os_name,
            proxy=self.config.proxy,
            network_idle=False,
        )
        # 重置所有有状态组件
        self.loop_detector.reset()
        self.context_compressor.reset()
        self.heartbeat.reset()
        self.crash_recovery.reset()
        if self.recorder is not None:
            self.recorder.reset()
            self.recorder.set_target(url)
        self._current_plan = None
        self._last_judge_result = None
        self._compiled_script = ""
        # 重置新组件
        self.budget_tracker = BudgetTracker(
            budget=TokenBudget(
                total=self.config.budget_total,
                per_step=self.config.budget_per_step,
            )
        )
        self._last_pruned_dom = None
        self._last_confidence = None
        self._last_guard_result = None
        self._last_think_prompt = ""
        self._last_think_completion = ""
        self._last_llm_usage = None
        # 重置截图缓存
        self._screenshots = []
        self._last_error_screenshot = ""

        history: list[dict] = []
        target_params_found: dict[str, str] = {}
        analysis: AnalysisResult | None = None
        last_observation: Observation | None = None

        # 尝试加载断点续跑
        if self.config.enable_checkpoint:
            self._resume_from = self.checkpoint_manager.load_latest()
            if self._resume_from is not None:
                cp = self._resume_from
                history = list(cp.history)
                target_params_found = dict(cp.target_params_found)
                self.context_compressor._cumulative_summary = cp.cumulative_summary
                self._emit(
                    "checkpoint.resume",
                    step=cp.step,
                    url=cp.url,
                    target_params_found=list(target_params_found.keys()),
                )

        try:
            context, page = await self._create_page_async(self.config.hooks)
            self._context = context
            self._page = page

            # resume 时导航回上次 URL，否则导航到入口 url
            nav_url = self._resume_from.url if self._resume_from and self._resume_from.url else url
            try:
                await page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                history.append({"step": 0, "event": "navigate_error", "error": str(exc)})
            await asyncio.sleep(self.config.wait_after_navigate)

            # resume 时重新注入已记录的 hooks
            if self._resume_from and self._resume_from.hooks:
                await self._inject_hooks_async(page, self._resume_from.hooks)

            # resume 时跳过已完成的步号
            start_step = (self._resume_from.step + 1) if self._resume_from else 1
            if start_step > self.config.max_steps:
                self._emit(EVENT_DONE, step=0, success=True, reason="resume已完成所有步骤")
            for step in range(start_step, self.config.max_steps + 1):
                self._emit(EVENT_STEP_START, step=step)
                try:
                    observation = await self._observe_async(page, step=step)
                    last_observation = observation
                    self._emit(
                        EVENT_OBSERVATION,
                        step=step,
                        url=observation.url,
                        hook_count=observation.hook_data.get("count", 0),
                        network_count=len(observation.network_requests),
                        script_count=len(observation.scripts),
                        screenshot_path=observation.screenshot_path,
                    )
                except Exception as exc:
                    history.append({"step": step, "event": "observe_error", "error": str(exc)})
                    self._emit(EVENT_OBSERVE_ERROR, step=step, error=str(exc))
                    # 异步崩溃恢复：CrashRecovery 同步控制器，但调用异步恢复函数
                    # 已 await 完成恢复；用默认参数绑定避免 B023 闭包陷阱。
                    recovered = await self._try_recover_page_async(url)

                    def _recovered_fn(_r: bool = recovered) -> bool:
                        return _r

                    should_continue = self.crash_recovery.attempt(
                        _recovered_fn,
                        step=step,
                        error=exc,
                    )
                    if should_continue:
                        continue
                    break

                # -- 循环检测 ------------------------------------------
                loop_result = self.loop_detector.observe(observation, step=step)
                if loop_result.detected:
                    self._emit(
                        "loop.detected",
                        step=step,
                        repeated_count=loop_result.repeated_count,
                    )
                    if self.planner is not None:
                        self._current_plan = await self.planner.make_plan_async(
                            task,
                            observation,
                            step=step,
                            history_summary=self.context_compressor.cumulative_summary,
                            target_params=self.config.target_params,
                        )
                        self._emit(
                            "plan",
                            step=step,
                            trigger="loop",
                            subgoals=[sg.to_dict() for sg in self._current_plan.subgoals],
                        )
                        self.loop_detector.reset()

                # -- 周期重规划 ----------------------------------------
                if self.planner is not None and (
                    self._current_plan is None
                    or self._current_plan.is_complete
                    or (step - (self._current_plan.created_at_step or 0))
                    >= self.planner.planner_interval
                ):
                    self._current_plan = await self.planner.make_plan_async(
                        task,
                        observation,
                        step=step,
                        history_summary=self.context_compressor.cumulative_summary,
                        target_params=self.config.target_params,
                    )
                    self._emit(
                        "plan",
                        step=step,
                        trigger="interval",
                        subgoals=[sg.to_dict() for sg in self._current_plan.subgoals],
                    )

                # -- 上下文压缩（异步） --------------------------------
                history, compressed = await self.context_compressor.maybe_compress_async(history)
                if compressed:
                    self._emit("context.compressed", step=step)

                try:
                    action = await self._think_async(
                        observation, task, history, plan=self._current_plan
                    )
                    # Token 预算记账
                    self.budget_tracker.record_call(
                        step=step,
                        prompt_text=self._last_think_prompt or "",
                        completion_text=self._last_think_completion or "",
                        usage=self._last_llm_usage,
                    )
                    self._emit(
                        EVENT_ACTION,
                        step=step,
                        action_type=action.action_type,
                        reasoning=action.reasoning,
                    )
                except Exception as exc:
                    history.append({"step": step, "event": "think_error", "error": str(exc)})
                    self._emit(EVENT_THINK_ERROR, step=step, error=str(exc))
                    # 错误时截图（失败不抛异常）
                    await self._take_screenshot_async(page, step, error=True)
                    action = self._fallback_action(observation)

                # -- Confidence 评分：低分动作触发 fallback ----------------
                conf_result = await self.confidence_scorer.score_async(
                    action,
                    task=task,
                    target_params=self.config.target_params,
                    history=history,
                )
                self._last_confidence = conf_result
                if conf_result.score < self.config.min_confidence:
                    self._emit(
                        "confidence.low",
                        step=step,
                        score=conf_result.score,
                        reasons=conf_result.reasons,
                    )
                    action = self._fallback_action(observation)

                # -- Token 预算检查 ----------------------------------------
                if self.budget_tracker.should_compress():
                    self._emit("budget.compress", step=step, **self.budget_tracker.summary())
                    history, _ = await self.context_compressor.force_compress_async(history)
                if self.budget_tracker.should_stop():
                    self._emit("budget.exceeded", step=step, **self.budget_tracker.summary())
                    history.append(
                        {
                            "step": step,
                            "event": "budget_exceeded",
                            "summary": self.budget_tracker.summary(),
                        }
                    )
                    break

                history.append(
                    {
                        "step": step,
                        "action": action.action_type,
                        "params": action.params,
                        "reasoning": action.reasoning,
                        "observation": {
                            "url": observation.url,
                            "hook_count": observation.hook_data.get("count", 0),
                            "network_count": len(observation.network_requests),
                            "script_count": len(observation.scripts),
                            "captcha_type": observation.captcha_type.value,
                        },
                        "current_subgoal": (
                            self._current_plan.current_subgoal.description
                            if self._current_plan and self._current_plan.current_subgoal
                            else None
                        ),
                        "confidence": conf_result.score,
                    }
                )

                self.heartbeat.tick(step)

                # -- 危险动作护栏：DENY 时跳过执行 ----------------------
                guard_result: GuardrailResult | None = None
                if self.guard is not None:
                    guard_result = await self.guard.check_async(
                        action,
                        context={"url": observation.url, "task": task, "step": step},
                    )
                    self._last_guard_result = guard_result
                    if guard_result.denied:
                        self._emit(
                            "guard.deny",
                            step=step,
                            matched_rules=guard_result.matched_rules,
                            details=guard_result.details,
                        )
                        history.append(
                            {
                                "step": step,
                                "event": "guard_denied",
                                "matched_rules": guard_result.matched_rules,
                                "details": guard_result.details,
                            }
                        )
                        self._emit(EVENT_STEP_END, step=step)
                        continue

                if action.action_type == "done":
                    if self.judge is not None and last_observation is not None:
                        judge_result = await self.judge.validate_async(
                            action=action,
                            observation=last_observation,
                            target_params_found=target_params_found,
                            task=task,
                            target_params=self.config.target_params,
                        )
                        self._last_judge_result = judge_result
                        self._emit(
                            "judge.result",
                            step=step,
                            verified=judge_result.verified,
                            missing=judge_result.missing,
                        )
                        if not judge_result.verified:
                            history.append(
                                {
                                    "step": step,
                                    "event": "judge_failed",
                                    "missing": judge_result.missing,
                                    "reasoning": judge_result.reasoning,
                                }
                            )
                            action = self._fallback_action(observation)
                        else:
                            self._emit(EVENT_DONE, step=step, success=True)
                            break
                    else:
                        self._emit(EVENT_DONE, step=step)
                        break

                try:
                    result = await self._act_async(page, action)
                    if self.recorder is not None:
                        self.recorder.record(
                            step=step,
                            action_type=action.action_type,
                            params=action.params,
                            result_value=(
                                str(result) if action.action_type == "extract" and result else None
                            ),
                            success=True,
                        )
                    if action.action_type == "inject_hook" and result is False:
                        history.append({"step": step, "event": "inject_hook_failed"})
                        if self.recorder is not None:
                            self.recorder._records[-1].success = False
                    elif action.action_type == "extract" and result:
                        param_name = action.params.get("param_name", "")
                        if param_name:
                            target_params_found[param_name] = result
                    elif action.action_type == "analyze_js" and isinstance(result, AnalysisResult):
                        analysis = result
                except Exception as exc:
                    history.append({"step": step, "event": "act_error", "error": str(exc)})
                    self._emit("act.error", step=step, error=str(exc))
                    # 错误时截图（失败不抛异常）
                    await self._take_screenshot_async(page, step, error=True)
                    if self.recorder is not None and self.recorder._records:
                        self.recorder._records[-1].success = False

                self._emit(EVENT_STEP_END, step=step)

                # -- Checkpoint：步末保存断点 ----------------------------------
                if self.config.enable_checkpoint:
                    cp = self.checkpoint_manager.build_checkpoint(
                        step=step,
                        url=last_observation.url if last_observation else "",
                        task=task,
                        target_params_found=target_params_found,
                        target_params=self.config.target_params,
                        hooks=self.config.hooks,
                        history=history,
                        cumulative_summary=self.context_compressor.cumulative_summary,
                        metadata={
                            "confidence": conf_result.score,
                            "budget": self.budget_tracker.summary(),
                            "guard_denied": bool(guard_result and guard_result.denied),
                        },
                    )
                    self.checkpoint_manager.save(cp)

            final_hook_data = await self._read_hook_data_async(page)
            success = bool(target_params_found)
            if self.config.target_params:
                success = all(p in target_params_found for p in self.config.target_params)
            if self.judge is not None and self._last_judge_result is not None:
                success = success and self._last_judge_result.verified

            if self.recorder is not None and success:
                try:
                    self._compiled_script = self.recorder.compile_script()
                except Exception as exc:
                    self._emit("recorder.compile_error", step=0, error=str(exc))
                    self._compiled_script = ""

            return {
                "success": success,
                "target_params_found": target_params_found,
                "analysis": analysis,
                "hook_data": final_hook_data,
                "steps": len(history),
                "history": history,
                "plan": self._current_plan.to_dict() if self._current_plan else None,
                "judge_result": (
                    self._last_judge_result.to_dict() if self._last_judge_result else None
                ),
                "compiled_script": self._compiled_script or None,
                "budget_summary": self.budget_tracker.summary(),
                "last_confidence": (
                    {
                        "score": self._last_confidence.score,
                        "reasons": list(self._last_confidence.reasons),
                        "action_type": getattr(self._last_confidence, "action_type", ""),
                    }
                    if self._last_confidence is not None
                    else None
                ),
                "checkpoints": list(self.checkpoints_snapshot()),
                "screenshots": list(self._screenshots),
                "error_screenshot": self._last_error_screenshot or None,
            }
        finally:
            await self._cleanup_async()

    # ------------------------------------------------------------------
    # 观察
    # ------------------------------------------------------------------

    def _observe(self, page: Any, *, step: int = 0) -> Observation:
        """收集页面状态。"""
        url = self._safe_page_url(page)
        # collect_hook_data 会清空浏览器侧数组，缓存一份供 _try_extract_param 复用
        hook_data = collect_hook_data(page)
        self._hook_data_cache = hook_data
        network_requests = list(self._network_log)
        scripts = self._collect_scripts(page)
        captcha_info = self.captcha_manager.detector.detect(page)
        captcha_type = captcha_info.type if captcha_info else CaptchaType.NONE
        try:
            page_title = page.title()
        except Exception:
            page_title = ""
        try:
            dom_raw = page.content()
        except Exception:
            dom_raw = ""
        # 启用 DomPruner 时把全文裁剪为精简结构，节省下游 LLM token
        if self.dom_pruner is not None and dom_raw:
            pruned = self.dom_pruner.prune(dom_raw)
            self._last_pruned_dom = pruned
            dom_summary = pruned.text or dom_raw[:2000]
        else:
            dom_summary = dom_raw[:2000]
        # 步末截图：失败不抛异常，仅留空路径
        screenshot_path = self._take_screenshot(page, step)
        return Observation(
            url=url,
            hook_data=hook_data,
            network_requests=network_requests,
            scripts=scripts,
            captcha_type=captcha_type,
            page_title=page_title,
            dom_summary=dom_summary,
            screenshot_path=screenshot_path,
        )

    async def _observe_async(self, page: Any, *, step: int = 0) -> Observation:
        """异步收集页面状态。"""
        url = self._safe_page_url(page)
        # 异步版 collect_hook_data：内联 evaluate 以支持 await
        records = (
            await page.evaluate(
                """() => {
                const data = window.__hook_data__ || [];
                const snapshot = data.slice();
                try { window.__hook_data__ = []; } catch (e) {}
                return snapshot;
            }"""
            )
            or []
        )
        hook_data = {"records": list(records), "count": len(records)}
        self._hook_data_cache = hook_data
        network_requests = list(self._network_log)
        scripts = await self._collect_scripts_async(page)
        captcha_info = self.captcha_manager.detector.detect(page)
        captcha_type = captcha_info.type if captcha_info else CaptchaType.NONE
        try:
            page_title = await page.title()
        except Exception:
            page_title = ""
        try:
            dom_raw = await page.content()
        except Exception:
            dom_raw = ""
        if self.dom_pruner is not None and dom_raw:
            pruned = await self.dom_pruner.prune_async(dom_raw)
            self._last_pruned_dom = pruned
            dom_summary = pruned.text or dom_raw[:2000]
        else:
            dom_summary = dom_raw[:2000]
        # 步末截图：失败不抛异常，仅留空路径
        screenshot_path = await self._take_screenshot_async(page, step)
        return Observation(
            url=url,
            hook_data=hook_data,
            network_requests=network_requests,
            scripts=scripts,
            captcha_type=captcha_type,
            page_title=page_title,
            dom_summary=dom_summary,
            screenshot_path=screenshot_path,
        )

    # ------------------------------------------------------------------
    # 思考
    # ------------------------------------------------------------------

    def _think(
        self,
        observation: Observation,
        task: str,
        history: list,
        *,
        plan: Plan | None = None,
    ) -> Action:
        """调 DeepSeek 分析当前状态，决定下一步。"""
        prompt = self._build_think_prompt(observation, task, history, plan=plan)
        # 暂存 prompt / completion 供 BudgetTracker 记账使用
        self._last_think_prompt = prompt
        self._last_think_completion = ""
        messages = [LLMMessage("system", _THINK_SYSTEM_PROMPT), LLMMessage("user", prompt)]
        resp = self.provider.chat(messages, temperature=0.0)
        self._last_think_completion = resp.content or ""
        # 暂存 LLM 真实用量（若 provider 返回）
        self._last_llm_usage = getattr(resp, "usage", None)
        return self._parse_action(resp.content or "")

    async def _think_async(
        self,
        observation: Observation,
        task: str,
        history: list,
        *,
        plan: Plan | None = None,
    ) -> Action:
        """异步调 DeepSeek 分析当前状态。"""
        prompt = self._build_think_prompt(observation, task, history, plan=plan)
        self._last_think_prompt = prompt
        self._last_think_completion = ""
        messages = [LLMMessage("system", _THINK_SYSTEM_PROMPT), LLMMessage("user", prompt)]
        if hasattr(self.provider, "achat"):
            resp = await self.provider.achat(messages, temperature=0.0)
        else:
            resp = self.provider.chat(messages, temperature=0.0)
        self._last_think_completion = resp.content or ""
        self._last_llm_usage = getattr(resp, "usage", None)
        return self._parse_action(resp.content or "")

    def _parse_action(self, content: str) -> Action:
        """解析 LLM 返回的动作为 Action 对象，解析失败时降级为 wait。"""
        data = _extract_json(content)
        if not data:
            return Action(
                action_type="wait",
                params={"seconds": 1.0},
                reasoning="LLM 返回无法解析，默认等待",
            )
        return Action.from_dict(data)

    def _fallback_action(self, observation: Observation) -> Action:
        """AI 分析失败时的降级动作：纯 Hook 模式提取目标参数。"""
        target = (self.config.target_params or [""])[0]
        return Action(
            action_type="extract",
            params={"param_name": target},
            reasoning="AI 分析失败，降级为纯 Hook 模式提取",
        )

    # ------------------------------------------------------------------
    # 行动
    # ------------------------------------------------------------------

    def _act(self, page: Any, action: Action) -> Any:
        """执行动作。"""
        atype = action.action_type
        if atype == "navigate":
            url = action.params.get("url")
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(self.config.wait_after_navigate)
            return None
        if atype == "inject_hook":
            hooks = action.params.get("hooks")
            return self._inject_hooks(page, hooks)
        if atype == "analyze_js":
            scripts = action.params.get("script_urls", [])
            target_params = action.params.get("target_params", self.config.target_params or [])
            return self._analyze_captured_js(scripts, target_params)
        if atype == "wait":
            seconds = float(action.params.get("seconds", 1.0))
            time.sleep(max(0.1, min(seconds, 30.0)))
            return None
        if atype == "extract":
            param_name = action.params.get("param_name", "")
            if not param_name:
                return None
            return self._try_extract_param(page, param_name)
        if atype == "solve_captcha":
            return self.captcha_manager.handle(page)
        return None

    async def _act_async(self, page: Any, action: Action) -> Any:
        """异步执行动作。"""
        atype = action.action_type
        if atype == "navigate":
            url = action.params.get("url")
            if url:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(self.config.wait_after_navigate)
            return None
        if atype == "inject_hook":
            hooks = action.params.get("hooks")
            return await self._inject_hooks_async(page, hooks)
        if atype == "analyze_js":
            scripts = action.params.get("script_urls", [])
            target_params = action.params.get("target_params", self.config.target_params or [])
            # _analyze_captured_js 内部用 httpx 同步拉取，无需 await
            return self._analyze_captured_js(scripts, target_params)
        if atype == "wait":
            seconds = float(action.params.get("seconds", 1.0))
            await asyncio.sleep(max(0.1, min(seconds, 30.0)))
            return None
        if atype == "extract":
            param_name = action.params.get("param_name", "")
            if not param_name:
                return None
            return await self._try_extract_param_async(page, param_name)
        if atype == "solve_captcha":
            # CaptchaManager.handle 是同步的
            return self.captcha_manager.handle(page)
        return None

    # ------------------------------------------------------------------
    # Hook 注入
    # ------------------------------------------------------------------

    def _inject_hooks(self, page: Any, hook_names: list[str] | None) -> bool:
        """注入 Hook 脚本（运行时通过 evaluate 执行 IIFE）。"""
        try:
            script = generate_combined_script(hook_names)
            page.evaluate(script)
            return True
        except Exception:
            return False

    async def _inject_hooks_async(self, page: Any, hook_names: list[str] | None) -> bool:
        """异步注入 Hook 脚本。"""
        try:
            script = generate_combined_script(hook_names)
            await page.evaluate(script)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 脚本收集
    # ------------------------------------------------------------------

    def _collect_scripts(self, page: Any) -> list[str]:
        """收集页面所有 JS URL。"""
        try:
            return (
                page.evaluate("""
                () => {
                    const scripts = document.querySelectorAll('script[src]');
                    return Array.from(scripts).map(s => s.src).filter(Boolean);
                }
            """)
                or []
            )
        except Exception:
            return []

    async def _collect_scripts_async(self, page: Any) -> list[str]:
        """异步收集页面所有 JS URL。"""
        try:
            return (
                await page.evaluate("""
                () => {
                    const scripts = document.querySelectorAll('script[src]');
                    return Array.from(scripts).map(s => s.src).filter(Boolean);
                }
            """)
                or []
            )
        except Exception:
            return []

    # ------------------------------------------------------------------
    # JS 分析
    # ------------------------------------------------------------------

    def _analyze_captured_js(
        self,
        scripts: list[str],
        target_params: list[str],
    ) -> AnalysisResult | None:
        """分析捕获的 JS，返回最相关的分析结果。

        限制分析前 10 个脚本以避免耗时过长；用 httpx 拉取脚本源码后交给
        :class:`JSAnalyzer` 逐段分析，按置信度与目标参数命中率选最优结果。
        """
        import httpx

        fragments: list[JSFragment] = []
        for url in scripts[:10]:
            try:
                resp = httpx.get(
                    url,
                    timeout=15.0,
                    follow_redirects=True,
                    headers={"User-Agent": _DEFAULT_UA},
                )
                if resp.status_code == 200 and resp.text:
                    text = resp.text
                    fragments.append(
                        JSFragment(
                            source=text,
                            url=url,
                            size=len(text),
                            is_minified=len(text.splitlines()) < 5,
                        )
                    )
            except Exception:
                continue

        if not fragments:
            return None

        target = target_params[0] if target_params else ""
        best_result: AnalysisResult | None = None
        best_score = 0.0
        for frag in fragments:
            try:
                result = self.analyzer.analyze_fragment(frag)
            except Exception:
                continue
            score = result.confidence
            if target and any(target in inp for inp in result.inputs):
                score += 0.5
            if score > best_score:
                best_score = score
                best_result = result

        return best_result

    # ------------------------------------------------------------------
    # 参数提取
    # ------------------------------------------------------------------

    def _try_extract_param(self, page: Any, param_name: str) -> str | None:
        """尝试从 Hook 数据中提取目标参数。"""
        records = self._read_hook_records(page)
        return self._search_param_in_records(records, param_name)

    async def _try_extract_param_async(self, page: Any, param_name: str) -> str | None:
        """异步尝试从 Hook 数据中提取目标参数。"""
        records = await self._read_hook_records_async(page)
        return self._search_param_in_records(records, param_name)

    @staticmethod
    def _search_param_in_records(records: list[dict], param_name: str) -> str | None:
        """在 hook 记录中搜索目标参数，返回首个命中的值。

        依次在 headers / url query / body（JSON 或 form）中做大小写不敏感匹配。
        """
        if not records:
            return None
        target_lower = param_name.lower()
        for rec in records:
            # 1. headers 中匹配键名
            headers = rec.get("headers") or {}
            if isinstance(headers, dict):
                for k, v in headers.items():
                    if target_lower in k.lower():
                        return str(v)
            # 2. url query 中匹配参数名
            url = rec.get("url") or ""
            if target_lower in url.lower():
                qs = parse_qs(urlparse(url).query)
                for k, v in qs.items():
                    if target_lower in k.lower():
                        return v[0] if v else None
            # 3. body 中匹配（先 JSON 后 form）
            body = rec.get("body")
            if isinstance(body, str) and target_lower in body.lower():
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        for k, v in parsed.items():
                            if target_lower in k.lower():
                                return str(v)
                except json.JSONDecodeError:
                    pass
                form = parse_qs(body)
                for k, v in form.items():
                    if target_lower in k.lower():
                        return v[0] if v else None
        return None

    # ------------------------------------------------------------------
    # 页面创建与恢复
    # ------------------------------------------------------------------

    def _create_page(self, hook_names: list[str] | None) -> tuple[Any, Any]:
        """创建带 Hook 注入的 (context, page)。"""
        assert self.fetcher is not None
        browser = self.fetcher._ensure_browser()
        context = browser.new_context(
            extra_http_headers=self.fetcher.extra_headers or None,
            ignore_https_errors=not self.fetcher.verify,
        )
        # 导航前注入 Hook，确保页面加载时即生效
        combined = generate_combined_script(hook_names or self.config.hooks)
        try:
            context.add_init_script(combined)
        except Exception:
            pass
        page = context.new_page()
        try:
            self.fetcher._setup_page(page)
        except Exception:
            pass
        self._setup_page_listeners(page)
        return context, page

    async def _create_page_async(self, hook_names: list[str] | None) -> tuple[Any, Any]:
        """异步创建带 Hook 注入的 (context, page)。"""
        assert self.fetcher is not None
        browser = await self.fetcher._ensure_async_browser()
        context = await browser.new_context(
            extra_http_headers=self.fetcher.extra_headers or None,
            ignore_https_errors=not self.fetcher.verify,
        )
        combined = generate_combined_script(hook_names or self.config.hooks)
        try:
            await context.add_init_script(combined)
        except Exception:
            pass
        page = await context.new_page()
        try:
            await self.fetcher._setup_page_async(page)
        except Exception:
            pass
        self._setup_page_listeners(page)
        return context, page

    def _setup_page_listeners(self, page: Any) -> None:
        """设置页面事件监听器，收集网络请求到 ``self._network_log``。"""
        self._network_log.clear()

        def on_request(req: Any) -> None:
            try:
                self._network_log.append(
                    {
                        "url": req.url,
                        "method": req.method,
                        "resource_type": req.resource_type,
                        "headers": dict(req.headers),
                        "post_data": req.post_data,
                    }
                )
            except Exception:
                pass

        try:
            page.on("request", on_request)
        except Exception:
            pass

    def _try_recover_page(self, url: str) -> bool:
        """浏览器崩溃后尝试重新创建 page 并导航到 url。"""
        try:
            self._cleanup_page_sync()
            context, page = self._create_page(self.config.hooks)
            self._context = context
            self._page = page
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(self.config.wait_after_navigate)
            return True
        except Exception:
            return False

    async def _try_recover_page_async(self, url: str) -> bool:
        """异步浏览器崩溃恢复。"""
        try:
            await self._cleanup_page_async()
            context, page = await self._create_page_async(self.config.hooks)
            self._context = context
            self._page = page
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(self.config.wait_after_navigate)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Hook 数据读取
    # ------------------------------------------------------------------

    def _read_hook_data(self, page: Any) -> dict:
        """非破坏性读取 Hook 数据（不清除浏览器侧数组）。"""
        try:
            records = page.evaluate("() => (window.__hook_data__ || []).slice()") or []
            return {"records": list(records), "count": len(records)}
        except Exception:
            return {"records": [], "count": 0}

    async def _read_hook_data_async(self, page: Any) -> dict:
        """异步非破坏性读取 Hook 数据。"""
        try:
            records = await page.evaluate("() => (window.__hook_data__ || []).slice()") or []
            return {"records": list(records), "count": len(records)}
        except Exception:
            return {"records": [], "count": 0}

    def _read_hook_records(self, page: Any) -> list[dict]:
        """读取 Hook 记录列表，合并页面实时数据与缓存。"""
        records: list[dict] = []
        try:
            fresh = page.evaluate("() => (window.__hook_data__ || []).slice()") or []
            records.extend(fresh)
        except Exception:
            pass
        cached = self._hook_data_cache.get("records", [])
        records.extend(cached)
        return records

    async def _read_hook_records_async(self, page: Any) -> list[dict]:
        """异步读取 Hook 记录列表。"""
        records: list[dict] = []
        try:
            fresh = await page.evaluate("() => (window.__hook_data__ || []).slice()") or []
            records.extend(fresh)
        except Exception:
            pass
        cached = self._hook_data_cache.get("records", [])
        records.extend(cached)
        return records

    # ------------------------------------------------------------------
    # Prompt 构建
    # ------------------------------------------------------------------

    def _build_think_prompt(
        self,
        observation: Observation,
        task: str,
        history: list,
        *,
        plan: Plan | None = None,
    ) -> str:
        """构建喂给 DeepSeek 的思考 prompt。"""
        target_params = (
            ", ".join(self.config.target_params) if self.config.target_params else "(未指定)"
        )
        base = _THINK_USER_TEMPLATE.format(
            task=task or "(未指定)",
            url=observation.url,
            page_title=observation.page_title,
            captcha_type=observation.captcha_type.value,
            hook_count=observation.hook_data.get("count", 0),
            network_count=len(observation.network_requests),
            script_count=len(observation.scripts),
            hook_summary=self._format_hook_summary(observation.hook_data),
            network_summary=self._format_network_summary(observation.network_requests),
            script_summary=self._format_script_summary(observation.scripts),
            dom_summary=observation.dom_summary,
            history_summary=self._format_history_summary(history),
            target_params=target_params,
        )
        # Planner 产出的当前子目标作为额外约束注入到 prompt 末尾
        if plan is not None and plan.current_subgoal is not None:
            sg = plan.current_subgoal
            base += (
                f"\n\n## 当前子目标（来自 Planner）\n{sg.description}\n"
                f"完成判据：{sg.success_criteria or '(未指定)'}\n"
                "你的下一步动作应服务于完成此子目标；若已完成，"
                "请输出 done 并说明成果。"
            )
        # 上下文压缩的累积摘要也作为额外背景注入
        if self.context_compressor.cumulative_summary:
            base += f"\n\n## 历史摘要（已压缩）\n{self.context_compressor.cumulative_summary}"
        return base

    @staticmethod
    def _format_hook_summary(hook_data: dict) -> str:
        """格式化 Hook 数据摘录。"""
        records = hook_data.get("records", [])
        if not records:
            return "(无)"
        lines: list[str] = []
        for rec in records[-20:]:
            rtype = rec.get("type", "?")
            method = rec.get("method", "")
            url = rec.get("url", "")
            headers = rec.get("headers") or {}
            body = rec.get("body")
            line = f"[{rtype}] {method} {url}"
            if isinstance(headers, dict) and headers:
                key_str = ", ".join(f"{k}={v}" for k, v in list(headers.items())[:5])
                line += f" | headers: {key_str}"
            if body:
                line += f" | body: {str(body)[:200]}"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _format_network_summary(network_requests: list[dict]) -> str:
        """格式化网络请求摘录。"""
        if not network_requests:
            return "(无)"
        lines: list[str] = []
        for req in network_requests[-20:]:
            method = req.get("method", "?")
            url = req.get("url", "?")
            rtype = req.get("resource_type", "?")
            lines.append(f"[{rtype}] {method} {url}")
        return "\n".join(lines)

    @staticmethod
    def _format_script_summary(scripts: list[str]) -> str:
        """格式化脚本列表。"""
        if not scripts:
            return "(无)"
        return "\n".join(scripts[:20])

    @staticmethod
    def _format_history_summary(history: list) -> str:
        """格式化历史动作摘要。"""
        if not history:
            return "(无)"
        lines: list[str] = []
        for entry in history[-10:]:
            step = entry.get("step", "?")
            atype = entry.get("action", entry.get("event", "?"))
            reasoning = entry.get("reasoning", entry.get("error", ""))
            line = f"step {step}: {atype}"
            if reasoning:
                line += f" - {reasoning[:150]}"
            lines.append(line)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 辅助工具
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_page_url(page: Any) -> str:
        try:
            return page.url
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------

    def _screenshot_dir(self) -> Path:
        """截图保存目录：``$CWD/reverse_screenshots``。"""
        return Path.cwd() / "reverse_screenshots"

    def _screenshot_task_id(self) -> str:
        """获取当前任务 ID（checkpoint_manager 未设置时回退为 default）。"""
        try:
            tid = getattr(self.checkpoint_manager, "task_id", "") or ""
            return tid or "default"
        except Exception:
            return "default"

    def checkpoints_snapshot(self) -> list[dict[str, Any]]:
        """读取已保存的 checkpoint 列表（step + path），供结果汇总使用。"""
        if not self.config.enable_checkpoint or not self.checkpoint_manager.task_id:
            return []
        try:
            paths = self.checkpoint_manager.store.list_checkpoints(self.checkpoint_manager.task_id)
            result: list[dict[str, Any]] = []
            for p in paths:
                step = 0
                name = p.stem  # 形如 step-0007
                if name.startswith("step-"):
                    try:
                        step = int(name[5:])
                    except ValueError:
                        pass
                result.append({"step": step, "path": str(p)})
            return result
        except Exception:
            return []

    def _take_screenshot(self, page: Any, step: int, *, error: bool = False) -> str:
        """同步截图：失败返回空字符串，绝不抛异常。

        ``error=True`` 时文件名加 ``_error`` 后缀，供 think/act 异常路径使用。
        """
        if not self.config.enable_screenshot or page is None:
            return ""
        try:
            task_id = self._screenshot_task_id()
            suffix = "_error" if error else ""
            filename = f"{task_id}_step{step}{suffix}.png"
            out_dir = self._screenshot_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / filename
            page.screenshot(path=str(path))
            self._emit("screenshot", step=step, path=str(path), error=error)
            entry = {"step": step, "path": str(path), "error": error, "ts": time.time()}
            self._screenshots.append(entry)
            if error:
                self._last_error_screenshot = str(path)
            return str(path)
        except Exception:
            # 截图失败不影响主循环：仅返回空路径
            return ""

    async def _take_screenshot_async(self, page: Any, step: int, *, error: bool = False) -> str:
        """异步截图：失败返回空字符串，绝不抛异常。"""
        if not self.config.enable_screenshot or page is None:
            return ""
        try:
            task_id = self._screenshot_task_id()
            suffix = "_error" if error else ""
            filename = f"{task_id}_step{step}{suffix}.png"
            out_dir = self._screenshot_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / filename
            await page.screenshot(path=str(path))
            self._emit("screenshot", step=step, path=str(path), error=error)
            entry = {"step": step, "path": str(path), "error": error, "ts": time.time()}
            self._screenshots.append(entry)
            if error:
                self._last_error_screenshot = str(path)
            return str(path)
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 资源清理
    # ------------------------------------------------------------------

    def _cleanup_page_sync(self) -> None:
        """关闭当前 page 与 context（同步）。"""
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None

    async def _cleanup_page_async(self) -> None:
        """关闭当前 page 与 context（异步）。"""
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

    def _cleanup_sync(self) -> None:
        """同步清理全部资源。"""
        self._cleanup_page_sync()
        if self.fetcher is not None:
            try:
                self.fetcher.close()
            except Exception:
                pass
            self.fetcher = None

    async def _cleanup_async(self) -> None:
        """异步清理全部资源。"""
        await self._cleanup_page_async()
        if self.fetcher is not None:
            try:
                await self.fetcher.aclose()
            except Exception:
                pass
            self.fetcher = None

    def close(self) -> None:
        """清理资源。"""
        self._cleanup_sync()

    async def aclose(self) -> None:
        """异步清理资源。"""
        await self._cleanup_async()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


__all__ = [
    "Action",
    "Observation",
    "ReverseAgent",
    "ReverseAgentConfig",
]

# 运行后可通过 ReverseAgent 实例访问的扩展属性：
# - agent.event_bus: EventBus — 订阅事件做日志/指标
# - agent.planner: Planner | None
# - agent.judge: TaskJudge | None
# - agent.recorder: RunRecorder | None
# - agent._current_plan: Plan | None
# - agent._last_judge_result: JudgeResult | None
# - agent._compiled_script: str (成功路径编译产物)

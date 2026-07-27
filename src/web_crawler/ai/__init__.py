"""AI-assisted, compliance-aware extraction layer.

This subpackage is purely additive on top of the core library:

- :mod:`~web_crawler.ai.llm` — pluggable LLM provider layer (default model
  ``DeepSeek-V4-Pro`` via DeepSeek's OpenAI-compatible API).
- :mod:`~web_crawler.ai.extractor` — LLM-generated CSS selectors validated
  against the project's own :class:`~web_crawler.parser.selector.Selector`,
  with self-healing.
- :mod:`~web_crawler.ai.agent` — polite orchestrator combining a fetcher with
  the extractor (robots.txt, rate limiting, 429/503 backoff).
- :mod:`~web_crawler.ai.hooks` — JS Hook 脚本库（fetch/XHR/cookie/crypto 拦截）。
- :mod:`~web_crawler.ai.analyzer` — AI webpack 混淆代码分析器。
- :mod:`~web_crawler.ai.captcha` — 验证码检测与处理（hCaptcha/Turnstile/极验）。
- :mod:`~web_crawler.ai.reverse_agent` — JS 逆向 Agent 主循环（观察-思考-行动）。
- :mod:`~web_crawler.ai.vision` — Vision-LLM 截图感知模块（双模态页面理解）。
- :mod:`~web_crawler.ai.planner` — Planner/Actor 双脑分离 + 周期重规划。
- :mod:`~web_crawler.ai.loop` — 循环检测 + 上下文压缩。
- :mod:`~web_crawler.ai.judge` — 任务完成 Judge/Validator（done 二次验证）。
- :mod:`~web_crawler.ai.watchdog` — Watchdog 事件总线 + 崩溃自愈。
- :mod:`~web_crawler.ai.recorder` — 成功路径编译为确定性脚本。
- :mod:`~web_crawler.ai.schema` — 结构化抽取 schema 验证（Pydantic）。
- :mod:`~web_crawler.ai.dom_pruner` — DOM 焦点裁剪（Skyvern/browser-use 风格）。
- :mod:`~web_crawler.ai.checkpoint` — 任务断点续跑（崩溃自愈 + 状态持久化）。
- :mod:`~web_crawler.ai.budget` — Token 预算管理（单步/全局/单次三维度）。
- :mod:`~web_crawler.ai.confidence` — 动作置信度评分（规则 + LLM 双路径）。
- :mod:`~web_crawler.ai.guardrails` — 危险动作护栏（白名单 + 跨域拦截）。
"""

from __future__ import annotations

from .agent import AIScrapeAgent, RobotsPolicy, ScrapeResult, detect_block
from .extractor import AIExtractor, ExtractionResult
from .llm import (
    DEFAULT_MODEL,
    DeepSeekProvider,
    LLMMessage,
    LLMProvider,
    LLMResponse,
    OpenAICompatibleProvider,
    available_providers,
    get_provider,
    register_provider,
)

__all__ = [
    "DEFAULT_MODEL",
    "AIExtractor",
    "AIScrapeAgent",
    "ActionGuard",
    "AnalysisResult",
    "BudgetExceeded",
    "BudgetPolicy",
    "BudgetTracker",
    "CaptchaDetector",
    "CaptchaManager",
    "CaptchaSolver",
    "CaptchaType",
    "Checkpoint",
    "CheckpointManager",
    "CheckpointStore",
    "ConfidenceResult",
    "ConfidenceScorer",
    "CrashRecovery",
    "DeepSeekProvider",
    "DomPruner",
    "EventBus",
    "ExtractionResult",
    "GuardrailAction",
    "GuardrailResult",
    "GuardrailRule",
    "Heartbeat",
    "HookLibrary",
    "JSAnalyzer",
    "JSFragment",
    "JudgeResult",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "OpenAICompatibleProvider",
    "Plan",
    "Planner",
    "PrunedDom",
    "ReverseAgent",
    "ReverseAgentConfig",
    "RobotsPolicy",
    "RunRecorder",
    "SchemaValidator",
    "ScrapeResult",
    "SubGoal",
    "TaskJudge",
    "TokenBudget",
    "VisionObserver",
    "WebPageState",
    "available_providers",
    "collect_hook_data",
    "detect_block",
    "generate_combined_script",
    "get_provider",
    "register_provider",
]


def __getattr__(name: str):
    """懒加载逆向相关模块，避免 import 时强制依赖 camoufox/playwright。"""
    _lazy = {
        "HookLibrary": ("web_crawler.ai.hooks", "HookLibrary"),
        "generate_combined_script": ("web_crawler.ai.hooks", "generate_combined_script"),
        "collect_hook_data": ("web_crawler.ai.hooks", "collect_hook_data"),
        "JSAnalyzer": ("web_crawler.ai.analyzer", "JSAnalyzer"),
        "JSFragment": ("web_crawler.ai.analyzer", "JSFragment"),
        "AnalysisResult": ("web_crawler.ai.analyzer", "AnalysisResult"),
        "CaptchaType": ("web_crawler.ai.captcha", "CaptchaType"),
        "CaptchaDetector": ("web_crawler.ai.captcha", "CaptchaDetector"),
        "CaptchaSolver": ("web_crawler.ai.captcha", "CaptchaSolver"),
        "CaptchaManager": ("web_crawler.ai.captcha", "CaptchaManager"),
        "ReverseAgent": ("web_crawler.ai.reverse_agent", "ReverseAgent"),
        "ReverseAgentConfig": ("web_crawler.ai.reverse_agent", "ReverseAgentConfig"),
        # Agent 增强模块
        "VisionObserver": ("web_crawler.ai.vision", "VisionObserver"),
        "Planner": ("web_crawler.ai.planner", "Planner"),
        "Plan": ("web_crawler.ai.planner", "Plan"),
        "SubGoal": ("web_crawler.ai.planner", "SubGoal"),
        "TaskJudge": ("web_crawler.ai.judge", "TaskJudge"),
        "JudgeResult": ("web_crawler.ai.judge", "JudgeResult"),
        "EventBus": ("web_crawler.ai.watchdog", "EventBus"),
        "Heartbeat": ("web_crawler.ai.watchdog", "Heartbeat"),
        "CrashRecovery": ("web_crawler.ai.watchdog", "CrashRecovery"),
        "RunRecorder": ("web_crawler.ai.recorder", "RunRecorder"),
        "SchemaValidator": ("web_crawler.ai.schema", "SchemaValidator"),
        "WebPageState": ("web_crawler.ai.schema", "WebPageState"),
        # 主流 Agent 对齐模块（DomPruner / Checkpoint / Budget / Confidence / Guardrails）
        "DomPruner": ("web_crawler.ai.dom_pruner", "DomPruner"),
        "PrunedDom": ("web_crawler.ai.dom_pruner", "PrunedDom"),
        "Checkpoint": ("web_crawler.ai.checkpoint", "Checkpoint"),
        "CheckpointManager": ("web_crawler.ai.checkpoint", "CheckpointManager"),
        "CheckpointStore": ("web_crawler.ai.checkpoint", "CheckpointStore"),
        "BudgetTracker": ("web_crawler.ai.budget", "BudgetTracker"),
        "TokenBudget": ("web_crawler.ai.budget", "TokenBudget"),
        "BudgetPolicy": ("web_crawler.ai.budget", "BudgetPolicy"),
        "BudgetExceeded": ("web_crawler.ai.budget", "BudgetExceeded"),
        "ConfidenceScorer": ("web_crawler.ai.confidence", "ConfidenceScorer"),
        "ConfidenceResult": ("web_crawler.ai.confidence", "ConfidenceResult"),
        "ActionGuard": ("web_crawler.ai.guardrails", "ActionGuard"),
        "GuardrailResult": ("web_crawler.ai.guardrails", "GuardrailResult"),
        "GuardrailAction": ("web_crawler.ai.guardrails", "GuardrailAction"),
        "GuardrailRule": ("web_crawler.ai.guardrails", "GuardrailRule"),
    }
    if name in _lazy:
        import importlib

        module_path, attr_name = _lazy[name]
        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

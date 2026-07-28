"""Token 预算管理模块。

借鉴 Skyvern / Nanobrowser / Anthropic Computer Use 的成本控制策略：在 Agent
主循环里维护单步与全局 token 预算，超预算即触发压缩、降级或停止。

能力清单
--------
- :class:`TokenBudget` — 单步/全局 token 预算与记账；
- :class:`BudgetExceeded` — 超预算异常，附带诊断信息；
- :class:`BudgetPolicy` — 策略枚举（``STOP`` / ``COMPRESS`` / ``DOWNGRADE``）；
- :class:`BudgetTracker` — Agent 主循环接入点：
  * :meth:`record_call` — 每次 LLM 调用后记账；
  * :meth:`check` — 检查当前预算状态；
  * :meth:`should_stop` / :meth:`should_compress` / :meth:`should_downgrade`
    便捷判断；
  * :meth:`summary` — 返回预算消耗摘要 dict。

设计要点
--------
- token 估算：优先用 ``LLMResponse.usage`` 里的真实数据，缺失时按
  ``len(text) / 4`` 近似（OpenAI 经验值，4 字符 ≈ 1 token）；
- 不依赖任何第三方 tokenizer，保持核心库轻量；
- 预算维度：单步（``per_step``）+ 全局（``total``）+ 单次调用（``per_call``）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# 字符 → token 的近似转换比（OpenAI 经验值，中文会偏多但不影响策略判断）
_CHARS_PER_TOKEN = 4


class BudgetExceeded(Exception):
    """预算超限异常，附带诊断信息。"""

    def __init__(self, reason: str, *, usage: dict[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.usage = usage


class BudgetPolicy(str, Enum):
    """超预算时的处理策略。

    注意：本项目采用单一模型策略（DeepSeek V4 Pro），所有子组件
    Planner / Actor / Judge / DomPruner / ConfidenceScorer 共享同一
    provider 实例，因此 ``DOWNGRADE`` 策略实际不适用——超预算请使用
    ``COMPRESS``（强制压缩历史）或 ``STOP``（直接停止）。
    """

    STOP = "stop"  # 直接停止 Agent 主循环
    COMPRESS = "compress"  # 触发 ContextCompressor 强制压缩历史
    DOWNGRADE = "downgrade"  # 切换到更便宜的模型（单一模型策略下不适用）


@dataclass
class _StepUsage:
    """单步用量记账。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0


@dataclass
class TokenBudget:
    """Token 预算配置。

    Attributes
    ----------
    total:
        全局 token 上限（所有 LLM 调用累计）。``None`` 表示不限制。
    per_step:
        单步 token 上限。``None`` 表示不限制。
    per_call:
        单次 LLM 调用上限（防止意外大 prompt）。``None`` 表示不限制。
    policy:
        超预算时的策略（默认 :attr:`BudgetPolicy.COMPRESS`）。
    """

    total: int | None = 200_000
    per_step: int | None = 20_000
    per_call: int | None = 16_000
    policy: BudgetPolicy = BudgetPolicy.COMPRESS


@dataclass
class _BudgetState:
    """预算运行时状态。"""

    used_total: int = 0
    current_step: int = 0
    steps: dict[int, _StepUsage] = field(default_factory=dict)
    calls: int = 0
    started_at: float = field(default_factory=time.time)
    last_violation: str | None = None

    def step_usage(self, step: int) -> _StepUsage:
        return self.steps.setdefault(step, _StepUsage())


class BudgetTracker:
    """Agent 主循环的 token 预算追踪器。

    Parameters
    ----------
    budget:
        :class:`TokenBudget` 配置。
    """

    def __init__(self, budget: TokenBudget | None = None) -> None:
        self.budget = budget or TokenBudget()
        self.state = _BudgetState()

    # ------------------------------------------------------------------
    # 记账
    # ------------------------------------------------------------------

    def record_call(
        self,
        step: int,
        *,
        prompt_text: str = "",
        completion_text: str = "",
        usage: dict[str, Any] | None = None,
    ) -> _StepUsage:
        """一次 LLM 调用后记账，返回更新后的步用量。

        Parameters
        ----------
        step:
            当前 Agent 步号。
        prompt_text:
            prompt 文本（用于估算 token）。
        completion_text:
            completion 文本（用于估算 token）。
        usage:
            LLM 返回的真实 usage dict，优先用。键名兼容 OpenAI 风格：
            ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``。
        """
        if usage and isinstance(usage, dict):
            pt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            ct = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            tt = int(usage.get("total_tokens") or (pt + ct))
        else:
            pt = self._estimate(prompt_text)
            ct = self._estimate(completion_text)
            tt = pt + ct

        step_usage = self.state.step_usage(step)
        step_usage.prompt_tokens += pt
        step_usage.completion_tokens += ct
        step_usage.total_tokens += tt
        step_usage.calls += 1

        self.state.used_total += tt
        self.state.calls += 1
        self.state.current_step = step
        return step_usage

    @staticmethod
    def _estimate(text: str) -> int:
        """字符 → token 近似估算。"""
        if not text:
            return 0
        return max(1, len(text) // _CHARS_PER_TOKEN)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def check(self) -> BudgetExceeded | None:
        """检查当前预算，超限返回 :class:`BudgetExceeded`，否则返回 None。"""
        # 单次调用上限（在 record_call 时已通过 prompt 长度间接控制，这里只是兜底）
        # 实际上单次调用超限应当在外层提前拦截，这里主要检查 step 与 total
        if self.budget.per_step is not None:
            step_usage = self.state.step_usage(self.state.current_step)
            if step_usage.total_tokens > self.budget.per_step:
                self.state.last_violation = "per_step"
                return BudgetExceeded(
                    f"step {self.state.current_step} token usage "
                    f"{step_usage.total_tokens} > per_step budget "
                    f"{self.budget.per_step}",
                    usage=self.summary(),
                )
        if self.budget.total is not None and self.state.used_total > self.budget.total:
            self.state.last_violation = "total"
            return BudgetExceeded(
                f"total token usage {self.state.used_total} > total budget {self.budget.total}",
                usage=self.summary(),
            )
        return None

    def should_stop(self) -> bool:
        """超预算且策略为 STOP 时返回 True。"""
        exc = self.check()
        return exc is not None and self.budget.policy == BudgetPolicy.STOP

    def should_compress(self) -> bool:
        """超预算且策略为 COMPRESS 时返回 True。"""
        exc = self.check()
        return exc is not None and self.budget.policy == BudgetPolicy.COMPRESS

    def should_downgrade(self) -> bool:
        """超预算且策略为 DOWNGRADE 时返回 True。"""
        exc = self.check()
        return exc is not None and self.budget.policy == BudgetPolicy.DOWNGRADE

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def reset_step(self, step: int) -> None:
        """重置某一步的用量（用于压缩后重新计数）。"""
        if step in self.state.steps:
            old = self.state.steps[step]
            self.state.used_total -= old.total_tokens
            del self.state.steps[step]

    def summary(self) -> dict[str, Any]:
        """返回预算消耗摘要。"""
        return {
            "used_total": self.state.used_total,
            "calls": self.state.calls,
            "current_step": self.state.current_step,
            "budget_total": self.budget.total,
            "budget_per_step": self.budget.per_step,
            "policy": self.budget.policy.value,
            "last_violation": self.state.last_violation,
            "elapsed_seconds": round(time.time() - self.state.started_at, 2),
        }

    def is_step_over_prompt_budget(self, prompt_text: str) -> bool:
        """预检：当前 prompt 文本是否超过单次调用预算。

        Agent 在发起 LLM 调用前可用此方法决定是否触发压缩。
        """
        if self.budget.per_call is None:
            return False
        estimated = self._estimate(prompt_text)
        return estimated > self.budget.per_call


__all__ = [
    "BudgetExceeded",
    "BudgetPolicy",
    "BudgetTracker",
    "TokenBudget",
]

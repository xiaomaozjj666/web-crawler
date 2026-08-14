"""Planner / Actor 双脑分离 + 周期重规划。

借鉴 browser-use 的 ``planner_llm`` / ``planner_interval`` 与 Skyvern 的
"Planner / Actor / Validator 三脑分离"思路，把单一 LLM 循环拆成两层：

- **Planner**：低频、高层。每 N 步或检测到偏差时调用一次，输出一个粗粒度
  ``Plan``（子目标列表）。Planner 看到"任务描述 + 当前观察 + 历史摘要"，
  不关心具体动作；
- **Actor**：高频、低层。每步调用，输出 ``Action``，输入多了"当前 Plan 的
  当前子目标"作为约束。Actor 只关心"如何完成当前子目标"，上下文窗口稳定。

设计要点
--------
- ``Plan`` 是子目标列表，每个子目标有 ``description`` / ``success_criteria``
  / ``completed`` 三字段；
- Planner 默认每 ``planner_interval`` 步重规划一次（默认 5 步），或在
  :class:`~web_crawler.ai.loop.LoopDetector` 触发时立即重规划；
- Actor prompt 与原 ReverseAgent 一致，但增加 ``current_subgoal`` 字段；
- ``Plan.advance()`` 标记当前子目标完成时推进到下一个。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._jsonutil import extract_json as _extract_json
from .llm import LLMMessage, LLMProvider

_PLANNER_SYSTEM_PROMPT = (
    "你是一名 JS 逆向任务的总规划师。你的职责是把高层任务拆成 3-7 个有序子目标，"
    "每个子目标要可被 Actor 在 1-5 步内完成，并给出明确的完成判据。"
    '输出 JSON：{"subgoals": [{"description": str, '
    '"success_criteria": str}, ...]}。'
    "不要在子目标里写具体动作（如点击某按钮），而是描述要达成的状态。"
)


@dataclass
class SubGoal:
    """Plan 中的一个子目标。"""

    description: str
    success_criteria: str = ""
    completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "success_criteria": self.success_criteria,
            "completed": self.completed,
        }


@dataclass
class Plan:
    """Planner 产出的整体规划。"""

    subgoals: list[SubGoal] = field(default_factory=list)
    # 当前子目标 index（next subgoal to work on）
    current_index: int = 0
    # Plan 创建时间（步号），便于判断是否需要重规划
    created_at_step: int = 0

    @property
    def current_subgoal(self) -> SubGoal | None:
        if 0 <= self.current_index < len(self.subgoals):
            return self.subgoals[self.current_index]
        return None

    @property
    def is_complete(self) -> bool:
        return all(s.completed for s in self.subgoals) or self.current_index >= len(self.subgoals)

    def advance(self) -> SubGoal | None:
        """标记当前子目标完成，推进到下一个。返回推进后的当前子目标。"""
        if 0 <= self.current_index < len(self.subgoals):
            self.subgoals[self.current_index].completed = True
            self.current_index += 1
        return self.current_subgoal

    def to_dict(self) -> dict[str, Any]:
        return {
            "subgoals": [s.to_dict() for s in self.subgoals],
            "current_index": self.current_index,
            "created_at_step": self.created_at_step,
        }


class Planner:
    """生成 :class:`Plan` 的高层规划器。

    Parameters
    ----------
    provider:
        LLM 提供商。建议 Planner 用更强的模型（Actor 用便宜模型），
        因为子目标拆解对推理质量更敏感。
    planner_interval:
        自动重规划的步数间隔。默认 5 步。在 :class:`LoopDetector` 触发时
        也会强制重规划。
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        planner_interval: int = 5,
    ) -> None:
        self.provider = provider
        self.planner_interval = max(1, planner_interval)

    def make_plan(
        self,
        task: str,
        observation: Any,
        *,
        step: int = 0,
        history_summary: str = "",
        target_params: list[str] | None = None,
    ) -> Plan:
        """同步：根据任务 + 当前观察生成 Plan。"""
        prompt = self._build_prompt(task, observation, history_summary, target_params)
        messages = [LLMMessage("system", _PLANNER_SYSTEM_PROMPT), LLMMessage("user", prompt)]
        try:
            resp = self.provider.chat(messages, temperature=0.0)
        except Exception:
            return Plan(created_at_step=step)
        parsed = _extract_json(resp.content or "")
        subgoals_raw = parsed.get("subgoals") if isinstance(parsed, dict) else None
        if not isinstance(subgoals_raw, list):
            return Plan(created_at_step=step)
        subgoals: list[SubGoal] = []
        for sg in subgoals_raw:
            if not isinstance(sg, dict):
                continue
            subgoals.append(
                SubGoal(
                    description=str(sg.get("description", "")),
                    success_criteria=str(sg.get("success_criteria", "")),
                )
            )
        return Plan(subgoals=subgoals, created_at_step=step)

    async def make_plan_async(
        self,
        task: str,
        observation: Any,
        *,
        step: int = 0,
        history_summary: str = "",
        target_params: list[str] | None = None,
    ) -> Plan:
        prompt = self._build_prompt(task, observation, history_summary, target_params)
        messages = [LLMMessage("system", _PLANNER_SYSTEM_PROMPT), LLMMessage("user", prompt)]
        try:
            if hasattr(self.provider, "achat"):
                resp = await self.provider.achat(messages, temperature=0.0)
            else:
                resp = self.provider.chat(messages, temperature=0.0)
        except Exception:
            return Plan(created_at_step=step)
        parsed = _extract_json(resp.content or "")
        subgoals_raw = parsed.get("subgoals") if isinstance(parsed, dict) else None
        if not isinstance(subgoals_raw, list):
            return Plan(created_at_step=step)
        subgoals: list[SubGoal] = []
        for sg in subgoals_raw:
            if not isinstance(sg, dict):
                continue
            subgoals.append(
                SubGoal(
                    description=str(sg.get("description", "")),
                    success_criteria=str(sg.get("success_criteria", "")),
                )
            )
        return Plan(subgoals=subgoals, created_at_step=step)

    @staticmethod
    def _build_prompt(
        task: str,
        observation: Any,
        history_summary: str,
        target_params: list[str] | None,
    ) -> str:
        target_str = ", ".join(target_params) if target_params else "(未指定)"
        url = getattr(observation, "url", "")
        page_title = getattr(observation, "page_title", "")
        captcha_type = getattr(observation, "captcha_type", None)
        captcha_value = getattr(captcha_type, "value", None)
        captcha_str = captcha_value if captcha_value is not None else str(captcha_type or "")
        hook_count = (
            getattr(observation, "hook_data", {}).get("count", 0)
            if isinstance(getattr(observation, "hook_data", None), dict)
            else 0
        )
        network_count = len(getattr(observation, "network_requests", []) or [])
        script_count = len(getattr(observation, "scripts", []) or [])
        return (
            f"## 任务\n{task or '(未指定)'}\n\n"
            f"## 目标参数\n{target_str}\n\n"
            "## 当前观察\n"
            f"- URL: {url}\n"
            f"- 页面标题: {page_title}\n"
            f"- 验证码类型: {captcha_str}\n"
            f"- Hook 数据条数: {hook_count}\n"
            f"- 网络请求数: {network_count}\n"
            f"- 页面脚本数: {script_count}\n\n"
            "## 历史摘要\n"
            f"{history_summary or '(无)'}\n\n"
            "## 输出要求\n"
            "把任务拆成 3-7 个有序子目标。第一个子目标应是基于当前观察的下一步合理推进，"
            "最后一个子目标应能直接产出目标参数。"
        )


__all__ = ["Plan", "Planner", "SubGoal"]

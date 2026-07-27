"""任务完成 Judge / Validator 模块。

借鉴 Skyvern 的 Validator 与 browser-use 的 is_done 二次验证思路：当 Actor
返回 done 动作时，不直接信任，而是把 (action, observation, target_params_found,
task) 交给独立的 LLM 视角再做一次判断，避免 Actor 因幻觉或上下文不足提前收尾。

能力清单
--------
- :class:`TaskJudge` — 同步/异步双入口的二次验证器；
- :class:`JudgeResult` — 归一化验证结果，包含 verified / missing / reasoning /
  raw 四字段；
- 严格模式（默认）：先做目标参数集合差集检查，缺一个就直接判失败，节省 LLM 调用；
- 灵活模式：完全交给 LLM 判断任务是否完成（适用于无法预先列举目标参数的场景）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMMessage, LLMProvider

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"^```(?:\w+)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)

_JUDGE_SYSTEM_PROMPT = (
    "你是一名严谨的任务验收专家。用户会给你一个 Agent 自评完成的任务，"
    "包括任务描述、Agent 的最后动作、当时的页面观察、已找到的目标参数。"
    "你的职责是判断任务是否真正完成。"
    "判定标准：目标参数是否全部命中且取值非空；任务描述中隐含的"
    "成功条件是否满足。"
    '输出 JSON：{"verified": bool, "missing": [str, ...], '
    '"reasoning": str}。'
    "verified=true 时 missing 必须为空数组；verified=false 时 reasoning 要"
    "明确指出缺什么、为什么未完成。"
)


def _extract_json(text: str) -> dict[str, Any]:
    """容错解析 LLM 回复中的 JSON 对象。"""
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


@dataclass
class JudgeResult:
    """二次验证结果。"""

    verified: bool
    missing: list[str] = field(default_factory=list)
    reasoning: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """``verified`` 的语义化别名。"""
        return self.verified

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "missing": list(self.missing),
            "reasoning": self.reasoning,
            "raw": self.raw,
        }


class TaskJudge:
    """对 Actor 的 done 动作做二次验证。

    Parameters
    ----------
    provider:
        任意 LLM 提供商。建议用与 Actor 不同模型/温度以降低同源幻觉概率。
    strict:
        严格模式（默认 True）。先做目标参数集合差集检查，缺任一参数直接判
        ``verified=False``，不调用 LLM。灵活模式跳过这一步，完全交给 LLM
        判断（适用于无法预先列举目标参数的开放任务）。
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        strict: bool = True,
    ) -> None:
        self.provider = provider
        self.strict = strict

    def validate(
        self,
        action: Any,
        observation: Any,
        target_params_found: dict[str, Any],
        task: str,
        target_params: list[str] | None = None,
    ) -> JudgeResult:
        """同步：验证任务是否真正完成。"""
        if self.strict and target_params:
            missing = [p for p in target_params if p not in target_params_found]
            if missing:
                return JudgeResult(
                    verified=False,
                    missing=missing,
                    reasoning=f"严格模式：目标参数 {missing} 未找到",
                )

        prompt = self._build_prompt(action, observation, target_params_found, task, target_params)
        messages = [LLMMessage("system", _JUDGE_SYSTEM_PROMPT), LLMMessage("user", prompt)]
        try:
            resp = self.provider.chat(messages, temperature=0.0)
        except Exception as exc:
            return JudgeResult(
                verified=False,
                missing=list(target_params or []),
                reasoning=f"Judge LLM 调用失败：{exc}",
            )
        parsed = _extract_json(resp.content or "")
        verified = bool(parsed.get("verified", False))
        missing_raw = parsed.get("missing") or []
        missing = [str(m) for m in missing_raw] if isinstance(missing_raw, list) else []
        if self.strict and verified and missing:
            verified = False
        if self.strict and not verified and not missing and target_params:
            missing = [p for p in target_params if p not in target_params_found]
        return JudgeResult(
            verified=verified,
            missing=missing,
            reasoning=str(parsed.get("reasoning", "")),
            raw=parsed,
        )

    async def validate_async(
        self,
        action: Any,
        observation: Any,
        target_params_found: dict[str, Any],
        task: str,
        target_params: list[str] | None = None,
    ) -> JudgeResult:
        """异步：验证任务是否真正完成。"""
        if self.strict and target_params:
            missing = [p for p in target_params if p not in target_params_found]
            if missing:
                return JudgeResult(
                    verified=False,
                    missing=missing,
                    reasoning=f"严格模式：目标参数 {missing} 未找到",
                )

        prompt = self._build_prompt(action, observation, target_params_found, task, target_params)
        messages = [LLMMessage("system", _JUDGE_SYSTEM_PROMPT), LLMMessage("user", prompt)]
        try:
            if hasattr(self.provider, "achat"):
                resp = await self.provider.achat(messages, temperature=0.0)
            else:
                resp = self.provider.chat(messages, temperature=0.0)
        except Exception as exc:
            return JudgeResult(
                verified=False,
                missing=list(target_params or []),
                reasoning=f"Judge LLM 调用失败：{exc}",
            )
        parsed = _extract_json(resp.content or "")
        verified = bool(parsed.get("verified", False))
        missing_raw = parsed.get("missing") or []
        missing = [str(m) for m in missing_raw] if isinstance(missing_raw, list) else []
        if self.strict and verified and missing:
            verified = False
        if self.strict and not verified and not missing and target_params:
            missing = [p for p in target_params if p not in target_params_found]
        return JudgeResult(
            verified=verified,
            missing=missing,
            reasoning=str(parsed.get("reasoning", "")),
            raw=parsed,
        )

    @staticmethod
    def _build_prompt(
        action: Any,
        observation: Any,
        target_params_found: dict[str, Any],
        task: str,
        target_params: list[str] | None,
    ) -> str:
        """构建喂给 Judge LLM 的验证 prompt。"""
        if hasattr(action, "to_dict"):
            action_str = json.dumps(action.to_dict(), ensure_ascii=False, default=str)
        elif hasattr(action, "__dict__"):
            action_str = json.dumps(action.__dict__, ensure_ascii=False, default=str)
        else:
            action_str = str(action)
        obs_dict: dict[str, Any] = {}
        for key in (
            "url",
            "page_title",
            "captcha_type",
            "hook_data",
            "network_requests",
            "scripts",
        ):
            value = getattr(observation, key, None)
            if value is None:
                continue
            if hasattr(value, "value"):
                value = value.value
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False, default=str)[:1500]
            obs_dict[key] = value
        obs_str = json.dumps(obs_dict, ensure_ascii=False, default=str)
        target_str = ", ".join(target_params) if target_params else "(未指定)"
        found_str = json.dumps(target_params_found, ensure_ascii=False, default=str)
        return (
            f"## 任务描述\n{task or '(未指定)'}\n\n"
            f"## 预期目标参数\n{target_str}\n\n"
            f"## Agent 声称已找到的参数\n{found_str}\n\n"
            f"## Agent 的最后动作\n{action_str}\n\n"
            f"## 当时的页面观察\n{obs_str}\n\n"
            "请判断任务是否真正完成。"
        )


__all__ = [
    "JudgeResult",
    "TaskJudge",
]

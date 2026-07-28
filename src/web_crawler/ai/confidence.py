"""动作置信度评分模块（Confidence Scoring）。

借鉴 Skyvern / browser-use 的"动作置信度"思路：在 Actor 输出动作后、执行前
做一次轻量评分，低分动作触发回流（重新思考）或 fallback，避免执行明显
不合理的动作浪费步数与 token。

能力清单
--------
- :class:`ConfidenceScorer` — 同步/异步置信度评分器；
- :class:`ConfidenceResult` — 评分结果，含 ``score`` / ``reasons`` / ``action``；
- 规则评分 + LLM 评分双路径：
  * 规则评分：检查动作格式完整性、参数必填、与历史重复度、与目标参数相关性；
  * LLM 评分：可选，让 LLM 对动作合理性做 0-1 浮点评分；
- 与 :class:`ReverseAgent` 集成：``min_confidence=0.6`` 时低分动作被拦截。

设计要点
--------
- 零成本默认：``enable_llm_score=False`` 时只走规则评分；
- 规则评分可解释：``reasons`` 列出每一项扣分明细；
- 与 LoopDetector 互补：LoopDetector 检测"完全重复"，ConfidenceScorer
  检测"低质量但未重复"的动作（如 navigate 到无关 URL）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMMessage, LLMProvider

# 规则评分各项权重
_W_FORMAT = 0.20  # 动作格式完整（action_type 合法、params 非空）
_W_PARAMS = 0.20  # 关键参数非空（如 navigate 的 url、inject_hook 的 hooks）
_W_NOVELTY = 0.20  # 与最近 N 步动作的相异度
_W_RELEVANCE = 0.25  # 与目标参数的相关性
_W_REASONING = 0.15  # reasoning 字段非空且合理长度

# 合法 action_type 白名单
_VALID_ACTIONS: frozenset[str] = frozenset(
    {
        "navigate",
        "inject_hook",
        "analyze_js",
        "wait",
        "extract",
        "solve_captcha",
        "done",
        # 浏览器交互动作（click / type / scroll / press / hover / select_option）
        "click",
        "type",
        "scroll",
        "press",
        "hover",
        "select_option",
    }
)

# LLM 评分 prompt
_SCORE_SYSTEM_PROMPT = (
    "你是一名 JS 逆向 Agent 动作质量评估专家。用户会给你一个动作（action_type + "
    "params + reasoning）以及当前任务、目标参数、最近历史。请输出 0-1 的浮点分数："
    "1.0 表示动作明显正确且高价值；0.5 表示可接受但非最优；0.0 表示明显错误或浪费。"
    '返回严格 JSON：{"score": 0.78, "reason": "..."}。'
)


@dataclass
class ConfidenceResult:
    """置信度评分结果。

    Attributes
    ----------
    score:
        0-1 浮点分数，1 表示完全可信。
    reasons:
        扣分明细列表，每项形如 ``"format: -0.10 (action_type missing)"``。
    action_type:
        被评分的动作类型。
    raw:
        LLM 原始返回（仅 ``enable_llm_score=True`` 时非空）。
    """

    score: float
    reasons: list[str] = field(default_factory=list)
    action_type: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """便捷：score >= 0.5 视为通过。"""
        return self.score >= 0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "reasons": list(self.reasons),
            "action_type": self.action_type,
            "raw": self.raw,
        }


class ConfidenceScorer:
    """动作置信度评分器。

    Parameters
    ----------
    min_confidence:
        最低通过分数，低于此值即视为低质量动作。默认 0.5。
    enable_llm_score:
        是否启用 LLM 评分。默认 False（只走规则评分，零成本）。
    provider:
        LLM 提供商，仅在 ``enable_llm_score=True`` 时使用。
    novelty_window:
        计算新颖度时回看的历史步数，默认 5。
    """

    def __init__(
        self,
        *,
        min_confidence: float = 0.5,
        enable_llm_score: bool = False,
        provider: LLMProvider | None = None,
        novelty_window: int = 5,
    ) -> None:
        self.min_confidence = max(0.0, min(min_confidence, 1.0))
        self.enable_llm_score = enable_llm_score and provider is not None
        self.provider = provider
        self.novelty_window = max(1, novelty_window)

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    def score(
        self,
        action: Any,
        *,
        task: str = "",
        target_params: list[str] | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> ConfidenceResult:
        """同步：对动作打分。

        Parameters
        ----------
        action:
            ``Action`` dataclass 或 dict，至少含 ``action_type`` / ``params``。
        task:
            当前任务描述（用于相关性检查）。
        target_params:
            目标参数名列表。
        history:
            历史动作列表（最近 N 步用于新颖度计算）。
        """
        action_dict = self._action_to_dict(action)
        reasons: list[str] = []
        rule_score = self._rule_score(
            action_dict, task, target_params or [], history or [], reasons
        )

        if self.enable_llm_score and self.provider is not None:
            llm_score, llm_reason, raw = self._llm_score(
                action_dict, task, target_params or [], history or []
            )
            if llm_reason:
                reasons.append(f"llm: {llm_reason}")
            # 综合：规则 60% + LLM 40%
            final_score = rule_score * 0.6 + llm_score * 0.4
        else:
            final_score = rule_score
            raw = {}

        return ConfidenceResult(
            score=round(final_score, 3),
            reasons=reasons,
            action_type=str(action_dict.get("action_type", "")),
            raw=raw,
        )

    async def score_async(
        self,
        action: Any,
        *,
        task: str = "",
        target_params: list[str] | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> ConfidenceResult:
        """异步：与 :meth:`score` 行为一致。"""
        action_dict = self._action_to_dict(action)
        reasons: list[str] = []
        rule_score = self._rule_score(
            action_dict, task, target_params or [], history or [], reasons
        )

        if self.enable_llm_score and self.provider is not None:
            llm_score, llm_reason, raw = await self._llm_score_async(
                action_dict, task, target_params or [], history or []
            )
            if llm_reason:
                reasons.append(f"llm: {llm_reason}")
            final_score = rule_score * 0.6 + llm_score * 0.4
        else:
            final_score = rule_score
            raw = {}

        return ConfidenceResult(
            score=round(final_score, 3),
            reasons=reasons,
            action_type=str(action_dict.get("action_type", "")),
            raw=raw,
        )

    def should_reject(self, result: ConfidenceResult) -> bool:
        """便捷：分数是否低于阈值（用于触发 fallback）。"""
        return result.score < self.min_confidence

    # ------------------------------------------------------------------
    # 规则评分
    # ------------------------------------------------------------------

    def _rule_score(
        self,
        action: dict[str, Any],
        task: str,
        target_params: list[str],
        history: list[dict[str, Any]],
        reasons: list[str],
    ) -> float:
        """规则评分：5 项各占一定权重，总满分 1.0。"""
        # 1. 格式完整
        format_score = self._score_format(action, reasons)
        # 2. 参数完整
        params_score = self._score_params(action, reasons)
        # 3. 新颖度
        novelty_score = self._score_novelty(action, history, reasons)
        # 4. 与目标参数相关性
        relevance_score = self._score_relevance(action, target_params, task, reasons)
        # 5. reasoning 质量
        reasoning_score = self._score_reasoning(action, reasons)

        total = (
            _W_FORMAT * format_score
            + _W_PARAMS * params_score
            + _W_NOVELTY * novelty_score
            + _W_RELEVANCE * relevance_score
            + _W_REASONING * reasoning_score
        )
        return max(0.0, min(1.0, total))

    @staticmethod
    def _score_format(action: dict[str, Any], reasons: list[str]) -> float:
        """格式完整：action_type 合法 + params 字段存在。"""
        at = str(action.get("action_type", "")).lower()
        if not at:
            reasons.append("format: -1.0 (missing action_type)")
            return 0.0
        if at not in _VALID_ACTIONS:
            reasons.append(f"format: -0.5 (unknown action_type {at!r})")
            return 0.5
        if "params" not in action:
            reasons.append("format: -0.3 (missing params field)")
            return 0.7
        return 1.0

    @staticmethod
    def _score_params(action: dict[str, Any], reasons: list[str]) -> float:
        """关键参数非空检查。"""
        at = str(action.get("action_type", "")).lower()
        params = action.get("params") or {}
        if not isinstance(params, dict):
            reasons.append("params: -0.5 (params is not dict)")
            return 0.5
        # 未知 action_type：无法校验参数，按低质量处理
        if at and at not in _VALID_ACTIONS:
            reasons.append(f"params: -0.7 (can't validate unknown action_type {at!r})")
            return 0.3
        required: dict[str, dict[str, type | tuple[type, ...]]] = {
            "navigate": {"url": str},
            "inject_hook": {"hooks": list},
            "analyze_js": {"script_urls": list},
            "wait": {"seconds": (int, float)},
            "extract": {"param_name": str},
            "solve_captcha": {},
            "done": {},
            # 浏览器交互动作的必填参数校验
            "click": {"selector": str},
            "type": {"selector": str, "text": str},
            "scroll": {},  # x / y 均有默认值，不强校验
            "press": {"key": str},
            "hover": {"selector": str},
            "select_option": {"selector": str, "value": str},
        }
        req: dict[str, type | tuple[type, ...]] = required.get(at, {})
        if not req:
            return 1.0
        score = 1.0
        for key, expected_type in req.items():
            val = params.get(key)
            if val is None or val == "":
                reasons.append(f"params: -0.3 (missing required {key!r})")
                score -= 0.3
            elif not isinstance(val, expected_type):
                reasons.append(f"params: -0.2 (wrong type for {key!r})")
                score -= 0.2
        return max(0.0, score)

    def _score_novelty(
        self,
        action: dict[str, Any],
        history: list[dict[str, Any]],
        reasons: list[str],
    ) -> float:
        """与最近 N 步动作的相异度。"""
        if not history:
            return 1.0
        recent = history[-self.novelty_window :]
        at = str(action.get("action_type", ""))
        params = action.get("params") or {}

        # 简单指纹：action_type + params 关键字段
        def _fingerprint(h: dict[str, Any]) -> str:
            return f"{h.get('action', '')}|{json.dumps(h.get('params') or {}, sort_keys=True)}"

        current_fp = f"{at}|{json.dumps(params, sort_keys=True)}"
        duplicates = sum(1 for h in recent if _fingerprint(h) == current_fp)
        if duplicates == 0:
            return 1.0
        # 完全重复动作扣分：第 1 次重复 -0.5，第 2 次 -0.8，第 3+ 次 -1.0
        score = max(0.0, 1.0 - 0.5 * duplicates)
        reasons.append(f"novelty: -{1.0 - score:.2f} (duplicated {duplicates} times)")
        return score

    @staticmethod
    def _score_relevance(
        action: dict[str, Any],
        target_params: list[str],
        task: str,
        reasons: list[str],
    ) -> float:
        """与目标参数 / 任务描述的相关性。"""
        at = str(action.get("action_type", "")).lower()
        # 未知 action_type：与任务不可能相关
        if at and at not in _VALID_ACTIONS:
            reasons.append(f"relevance: -0.7 (unknown action_type {at!r})")
            return 0.3
        if not target_params and not task:
            return 0.8  # 无目标信息时不强扣分
        params = action.get("params") or {}
        reasoning = str(action.get("reasoning") or "").lower()

        # done 动作：检查是否提及目标参数
        if at == "done":
            if not target_params:
                return 0.9
            mentioned = sum(
                1
                for p in target_params
                if p.lower() in reasoning or p.lower() in str(params).lower()
            )
            if mentioned == len(target_params):
                return 1.0
            score = 0.3 + 0.7 * (mentioned / max(1, len(target_params)))
            reasons.append(
                f"relevance: done but only mentioned {mentioned}/{len(target_params)} target params"
            )
            return score

        # extract 动作：检查 param_name 是否在目标参数列表里
        if at == "extract":
            pn = str(params.get("param_name", "")).lower()
            if not target_params:
                return 0.8
            if pn and any(p.lower() == pn for p in target_params):
                return 1.0
            reasons.append(f"relevance: extract {pn!r} not in target_params")
            return 0.3

        # 其他动作：检查 reasoning 是否提及目标参数或任务关键词
        if target_params:
            hit = sum(1 for p in target_params if p.lower() in reasoning)
            if hit > 0:
                return min(1.0, 0.6 + 0.1 * hit)
        return 0.7

    @staticmethod
    def _score_reasoning(action: dict[str, Any], reasons: list[str]) -> float:
        """reasoning 字段质量评分。"""
        reasoning = str(action.get("reasoning") or "").strip()
        if not reasoning:
            reasons.append("reasoning: -1.0 (empty)")
            return 0.0
        if len(reasoning) < 10:
            reasons.append("reasoning: -0.5 (too short)")
            return 0.5
        if len(reasoning) > 500:
            reasons.append("reasoning: -0.2 (too verbose)")
            return 0.8
        return 1.0

    # ------------------------------------------------------------------
    # LLM 评分
    # ------------------------------------------------------------------

    def _llm_score(
        self,
        action: dict[str, Any],
        task: str,
        target_params: list[str],
        history: list[dict[str, Any]],
    ) -> tuple[float, str, dict[str, Any]]:
        """同步：LLM 评分。返回 (score, reason, raw)。"""
        assert self.provider is not None  # 由 enable_llm_score 守护
        prompt = self._build_score_prompt(action, task, target_params, history)
        messages = [LLMMessage("system", _SCORE_SYSTEM_PROMPT), LLMMessage("user", prompt)]
        try:
            resp = self.provider.chat(messages, temperature=0.0)
        except Exception as exc:
            return 0.5, f"llm_error: {exc}", {}
        parsed = self._parse_score_response(resp.content or "")
        score = float(parsed.get("score", 0.5))
        reason = str(parsed.get("reason", ""))
        return max(0.0, min(1.0, score)), reason, parsed

    async def _llm_score_async(
        self,
        action: dict[str, Any],
        task: str,
        target_params: list[str],
        history: list[dict[str, Any]],
    ) -> tuple[float, str, dict[str, Any]]:
        """异步：LLM 评分。返回 (score, reason, raw)。"""
        assert self.provider is not None  # 由 enable_llm_score 守护
        prompt = self._build_score_prompt(action, task, target_params, history)
        messages = [LLMMessage("system", _SCORE_SYSTEM_PROMPT), LLMMessage("user", prompt)]
        try:
            if hasattr(self.provider, "achat"):
                resp = await self.provider.achat(messages, temperature=0.0)
            else:
                resp = self.provider.chat(messages, temperature=0.0)
        except Exception as exc:
            return 0.5, f"llm_error: {exc}", {}
        parsed = self._parse_score_response(resp.content or "")
        score = float(parsed.get("score", 0.5))
        reason = str(parsed.get("reason", ""))
        return max(0.0, min(1.0, score)), reason, parsed

    @staticmethod
    def _build_score_prompt(
        action: dict[str, Any],
        task: str,
        target_params: list[str],
        history: list[dict[str, Any]],
    ) -> str:
        recent = history[-5:] if len(history) > 5 else history
        return (
            f"## 任务\n{task or '(未指定)'}\n\n"
            f"## 目标参数\n{', '.join(target_params) if target_params else '(未指定)'}\n\n"
            f"## 待评分动作\n{json.dumps(action, ensure_ascii=False, default=str)}\n\n"
            f"## 最近历史\n{json.dumps(recent, ensure_ascii=False, default=str)}\n\n"
            "请输出 0-1 的分数，并给出简短理由。"
        )

    @staticmethod
    def _parse_score_response(text: str) -> dict[str, Any]:
        """容错解析 LLM 返回的打分 JSON。"""
        text = text.strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {"score": 0.5, "reason": "parse_failed"}
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {"score": 0.5, "reason": "parse_failed"}

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _action_to_dict(action: Any) -> dict[str, Any]:
        """把 Action dataclass / dict 归一为 dict。"""
        if isinstance(action, dict):
            return action
        if hasattr(action, "to_dict"):
            return action.to_dict()
        if hasattr(action, "__dict__"):
            return dict(action.__dict__)
        return {"action_type": str(action)}


__all__ = [
    "ConfidenceResult",
    "ConfidenceScorer",
]

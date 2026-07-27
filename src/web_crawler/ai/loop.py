"""循环检测 + 上下文压缩模块。

借鉴 browser-use 的循环检测（页面状态指纹：URL + 元素数 + 文本哈希）与
每 25 步摘要的上下文压缩机制。两个能力放在一起是因为它们都服务于
"突破硬上限，让长流程任务可用"这一个目标。

能力清单
--------
- :class:`StateFingerprint` — 页面状态指纹，可比较、可哈希；
- :class:`LoopDetector` — 滚动窗口内重复状态检测，触发即提示重规划；
- :class:`ContextCompressor` — 超过阈值的历史动作做摘要压缩，避免 token 爆炸。
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

from .llm import LLMMessage, LLMProvider

# 触发循环检测的最小重复次数
_DEFAULT_LOOP_THRESHOLD = 3
# 滚动窗口大小（最近多少步参与重复检测）
_DEFAULT_WINDOW = 8

_COMPRESS_SYSTEM_PROMPT = (
    "你是 Agent 历史压缩器。用户会给你一段 agent 的历史动作列表，请用 "
    "≤200 字中文摘要其中关键事实：已访问过的 URL、已注入的 Hook、已发现的"
    "加密参数、已捕获的关键网络请求、已失败的尝试。不要罗列细节，"
    "只保留对后续决策有用的信息。"
)


@dataclass(frozen=True, slots=True)
class StateFingerprint:
    """页面状态指纹：URL + 元素数 + 文本哈希。

    设计参考 browser-use：单一字段不重复但组合重复即视为同一状态，
    可显著降低误报。文本哈希取 DOM 文本内容（截断后 md5）。
    """

    url: str
    element_count: int
    text_hash: str

    @classmethod
    def from_observation(cls, observation: Any) -> StateFingerprint:
        """从 Observation dataclass 提取指纹。"""
        url = getattr(observation, "url", "") or ""
        dom_summary = getattr(observation, "dom_summary", "") or ""
        hook_data = getattr(observation, "hook_data", None)
        hook_count = hook_data.get("count", 0) if isinstance(hook_data, dict) else 0
        network_count = len(getattr(observation, "network_requests", []) or [])
        scripts_count = len(getattr(observation, "scripts", []) or [])
        # 元素数 ≈ DOM 字符数 / 200 + hook/network/scripts 计数
        # 不追求精确，只要可比较
        element_count = (len(dom_summary) // 200) + hook_count + network_count + scripts_count
        text_hash = hashlib.md5(dom_summary.encode("utf-8", errors="ignore")).hexdigest()
        return cls(url=url, element_count=element_count, text_hash=text_hash)

    def short(self) -> str:
        """短表示，便于日志输出。"""
        return f"{self.url[:60]}|el={self.element_count}|h={self.text_hash[:8]}"


@dataclass
class LoopDetectionResult:
    """循环检测结果。"""

    detected: bool
    repeated_count: int = 0
    repeated_state: StateFingerprint | None = None
    last_change_step: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "repeated_count": self.repeated_count,
            "repeated_state": self.repeated_state.short() if self.repeated_state else None,
            "last_change_step": self.last_change_step,
        }


class LoopDetector:
    """滚动窗口内重复状态检测器。

    在最近 ``window`` 步内，同一指纹出现 ≥ ``threshold`` 次即视为循环。
    触发后调用方应：
    1. 让 Planner 重规划；
    2. 或注入随机扰动（如 navigate 到不同 URL 再回退）。
    """

    def __init__(
        self,
        *,
        threshold: int = _DEFAULT_LOOP_THRESHOLD,
        window: int = _DEFAULT_WINDOW,
    ) -> None:
        self.threshold = max(2, threshold)
        self.window = max(self.threshold, window)
        self._history: deque[StateFingerprint] = deque(maxlen=self.window)
        # 最近一次状态变化的步号（0-based）
        self._last_change_step = 0
        self._current_step = 0

    def observe(self, observation: Any, *, step: int | None = None) -> LoopDetectionResult:
        """同步：喂入一次观察，返回是否检测到循环。"""
        if step is not None:
            self._current_step = step
        else:
            self._current_step += 1
        fp = StateFingerprint.from_observation(observation)
        # 检测是否有变化
        if self._history and self._history[-1] != fp:
            self._last_change_step = self._current_step
        self._history.append(fp)

        # 统计最近窗口内的重复次数
        count = sum(1 for h in self._history if h == fp)
        detected = count >= self.threshold
        return LoopDetectionResult(
            detected=detected,
            repeated_count=count,
            repeated_state=fp,
            last_change_step=self._last_change_step,
        )

    def reset(self) -> None:
        """重规划后调用，清空历史避免立刻再次触发。"""
        self._history.clear()
        self._last_change_step = self._current_step


class ContextCompressor:
    """历史动作摘要压缩器。

    当历史动作数超过 ``max_history`` 时，把最旧的若干条压成一段自然语言摘要，
    替换原条目。新条目保持原样，保证 Actor 看到的是"最近 N 条 + 摘要"。

    设计参考 browser-use 每 25 步摘要 + PentAGI Chain-of-Abstraction。
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_history: int = 25,
        compress_to: int = 5,
    ) -> None:
        if compress_to >= max_history:
            raise ValueError("compress_to must be smaller than max_history")
        self.provider = provider
        self.max_history = max_history
        self.compress_to = compress_to
        # 跨次压缩时累加的摘要（已压缩过的部分）
        self._cumulative_summary = ""

    @property
    def cumulative_summary(self) -> str:
        return self._cumulative_summary

    def maybe_compress(self, history: list[dict]) -> tuple[list[dict], bool]:
        """同步：若历史超过阈值，触发压缩，返回 (新历史, 是否压缩过)。"""
        if len(history) <= self.max_history:
            return history, False
        # 取最旧的若干条做压缩（保留最近 compress_to 条）
        to_compress = history[: -self.compress_to]
        recent = history[-self.compress_to :]
        summary = self._summarize(to_compress)
        # 累加到历史摘要
        if self._cumulative_summary:
            self._cumulative_summary = f"{self._cumulative_summary}\n\n{summary}"
        else:
            self._cumulative_summary = summary
        # 把摘要作为一条"meta"动作插到最近历史前
        return (
            [
                {
                    "step": -1,
                    "action": "_history_compressed",
                    "reasoning": self._cumulative_summary,
                },
                *recent,
            ],
            True,
        )

    async def maybe_compress_async(self, history: list[dict]) -> tuple[list[dict], bool]:
        if len(history) <= self.max_history:
            return history, False
        to_compress = history[: -self.compress_to]
        recent = history[-self.compress_to :]
        summary = await self._summarize_async(to_compress)
        if self._cumulative_summary:
            self._cumulative_summary = f"{self._cumulative_summary}\n\n{summary}"
        else:
            self._cumulative_summary = summary
        return (
            [
                {
                    "step": -1,
                    "action": "_history_compressed",
                    "reasoning": self._cumulative_summary,
                },
                *recent,
            ],
            True,
        )

    def force_compress(self, history: list[dict]) -> tuple[list[dict], bool]:
        """强制压缩：不论历史长度是否超过阈值都触发一次压缩。

        用于预算超限 / 循环检测 / 上游决策触发的强制瘦身。当 history 长度
        小于 ``compress_to`` 时直接返回原历史不做改动（避免空摘要）。
        """
        if len(history) <= self.compress_to:
            return history, False
        to_compress = history[: -self.compress_to]
        recent = history[-self.compress_to :]
        summary = self._summarize(to_compress)
        if self._cumulative_summary:
            self._cumulative_summary = f"{self._cumulative_summary}\n\n{summary}"
        else:
            self._cumulative_summary = summary
        return (
            [
                {
                    "step": -1,
                    "action": "_history_compressed",
                    "reasoning": self._cumulative_summary,
                },
                *recent,
            ],
            True,
        )

    async def force_compress_async(self, history: list[dict]) -> tuple[list[dict], bool]:
        """异步强制压缩。"""
        if len(history) <= self.compress_to:
            return history, False
        to_compress = history[: -self.compress_to]
        recent = history[-self.compress_to :]
        summary = await self._summarize_async(to_compress)
        if self._cumulative_summary:
            self._cumulative_summary = f"{self._cumulative_summary}\n\n{summary}"
        else:
            self._cumulative_summary = summary
        return (
            [
                {
                    "step": -1,
                    "action": "_history_compressed",
                    "reasoning": self._cumulative_summary,
                },
                *recent,
            ],
            True,
        )

    def _summarize(self, entries: list[dict]) -> str:
        """调用 LLM 把 entries 摘要成一段中文文本。"""
        if not entries:
            return ""
        try:
            body = json.dumps(entries, ensure_ascii=False, default=str)
            messages = [
                LLMMessage("system", _COMPRESS_SYSTEM_PROMPT),
                LLMMessage("user", f"历史动作列表：\n{body}"),
            ]
            resp = self.provider.chat(messages, temperature=0.0, max_tokens=400)
            return (resp.content or "").strip()
        except Exception as exc:
            return f"[history compression failed: {exc}]"

    async def _summarize_async(self, entries: list[dict]) -> str:
        if not entries:
            return ""
        try:
            body = json.dumps(entries, ensure_ascii=False, default=str)
            messages = [
                LLMMessage("system", _COMPRESS_SYSTEM_PROMPT),
                LLMMessage("user", f"历史动作列表：\n{body}"),
            ]
            if hasattr(self.provider, "achat"):
                resp = await self.provider.achat(messages, temperature=0.0, max_tokens=400)
            else:
                resp = self.provider.chat(messages, temperature=0.0, max_tokens=400)
            return (resp.content or "").strip()
        except Exception as exc:
            return f"[history compression failed: {exc}]"

    def reset(self) -> None:
        """新任务开始时清空累积摘要。"""
        self._cumulative_summary = ""


__all__ = [
    "ContextCompressor",
    "LoopDetectionResult",
    "LoopDetector",
    "StateFingerprint",
]

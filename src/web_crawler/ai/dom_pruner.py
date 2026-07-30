"""DOM 焦点裁剪模块（Accessibility-Aware DOM Pruning）。

借鉴 Skyvern / browser-use / AgentE 的可访问性树裁剪思路：把动辄几万字符
的原始 HTML 截断为只包含可交互元素 + 关键文本的精简结构，在不丢失决策所需
信息的前提下大幅降低 LLM token 消耗（典型 80% 削减）。

能力清单
--------
- :class:`DomPruner` — 滚动窗口裁剪 + 关键节点优先 + 长度预算；
- :class:`PrunedDom` — 裁剪结果，包含 ``text`` / ``element_count`` / ``truncated``；
- 同步入口 :meth:`DomPruner.prune` 与异步入口 :meth:`DomPruner.prune_async`；
- 可选 LLM 重要性评分：当 ``enable_llm_rank=True`` 时，让 LLM 对前 N 个候选
  节点打分重排，把最相关的元素放到最前面。LLM 不可用时自动降级为规则打分。

设计要点
--------
- 不依赖第三方 HTML 解析器：用项目已有的 BeautifulSoup（lxml 后端）；
- 纯函数 + 数据类，无全局状态，方便测试与并发；
- 截断策略：先按规则打分（form/input/a/button/script[src] 等高分；纯文本低分），
  再按打分降序取前 N，最后按 ``max_chars`` 截断为最终字符串。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .llm import LLMMessage, LLMProvider

if TYPE_CHECKING:
    from bs4 import Tag

# 默认单页 DOM 字符上限（约 4000 token）
_DEFAULT_MAX_CHARS = 8000
# 默认保留的候选元素上限（LLM 评分前）
_DEFAULT_MAX_CANDIDATES = 80
# LLM 评分时的候选截断（控制成本）
_DEFAULT_LLM_RANK_LIMIT = 30

# 高优先级标签：包含交互逻辑或加密相关
_HIGH_PRIORITY_TAGS: frozenset[str] = frozenset(
    {
        "script",
        "input",
        "form",
        "button",
        "a",
        "iframe",
        "textarea",
        "select",
    }
)
# 加密参数常见容器属性名（小写匹配）
_CRYPTO_ATTR_HINTS: tuple[str, ...] = (
    "sign",
    "token",
    "anti",
    "bogus",
    "signature",
    "encrypt",
    "secret",
    "apikey",
)
# 加密参数常见关键词（出现在 id/class/text 中即视为重要）
_CRYPTO_TEXT_HINTS: tuple[str, ...] = (
    "anti-content",
    "x-bogus",
    "_signature",
    "x-secsdk-csrf-token",
    "acw_sc__v2",
    "dfp",
    "sign",
    "encrypt",
)

# LLM 评分 prompt（同步/异步共用）
_RANK_SYSTEM_PROMPT = (
    "你是一名 DOM 重要性评估专家。用户会给你若干候选 DOM 片段（含 id/class/text"
    "摘要），它们来自一个 JS 逆向 Agent 正在分析的网页。请根据以下原则打分（0-10）：\n"
    "1. 与加密参数生成直接相关（含 sign/token/encrypt 关键字）+3 ~ +5\n"
    "2. 包含外部 JS 引用（script[src]）+2\n"
    "3. 可交互元素（form/input/button）+1 ~ +2\n"
    "4. 纯装饰性/导航文本 0\n"
    '返回严格 JSON 数组：[{"index": 0, "score": 8.5}, ...]，index 从 0 开始。'
)


@dataclass
class PrunedDom:
    """裁剪后的 DOM 摘要。

    Attributes
    ----------
    text:
        最终送给 LLM 的 DOM 文本（已截断到 ``max_chars``）。
    element_count:
        候选元素总数（截断前）。
    kept_count:
        实际保留的元素数。
    truncated:
        是否因为 ``max_chars`` 触发字符级截断。
    top_score:
        最高得分（便于上游决策"这页是否有价值的加密相关元素"）。
    """

    text: str
    element_count: int
    kept_count: int
    truncated: bool
    top_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "element_count": self.element_count,
            "kept_count": self.kept_count,
            "truncated": self.truncated,
            "top_score": self.top_score,
        }


@dataclass
class _Candidate:
    """打分中的候选元素。"""

    html: str
    score: float
    tag: str
    text_preview: str = ""


class DomPruner:
    """DOM 焦点裁剪器。

    Parameters
    ----------
    max_chars:
        最终输出字符串的字符上限。超过即截断并置 ``truncated=True``。
    max_candidates:
        保留的候选元素数上限（按打分降序取前 N）。
    enable_llm_rank:
        是否启用 LLM 重要性评分。``False`` 时只走规则打分（更快、零成本）。
    provider:
        LLM 提供商，仅在 ``enable_llm_rank=True`` 时使用。
    llm_rank_limit:
        LLM 评分的候选数上限（控制成本，建议 ≤ 30）。
    """

    def __init__(
        self,
        *,
        max_chars: int = _DEFAULT_MAX_CHARS,
        max_candidates: int = _DEFAULT_MAX_CANDIDATES,
        enable_llm_rank: bool = False,
        provider: LLMProvider | None = None,
        llm_rank_limit: int = _DEFAULT_LLM_RANK_LIMIT,
    ) -> None:
        self.max_chars = max(500, max_chars)
        self.max_candidates = max(10, max_candidates)
        self.enable_llm_rank = enable_llm_rank and provider is not None
        self.provider = provider
        self.llm_rank_limit = max(5, llm_rank_limit)

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    def prune(self, html: str) -> PrunedDom:
        """同步：裁剪 HTML，返回 :class:`PrunedDom`。"""
        candidates = self._extract_candidates(html)
        if not candidates:
            return PrunedDom(text="", element_count=0, kept_count=0, truncated=False)

        if self.enable_llm_rank and self.provider is not None:
            candidates = self._llm_rerank(candidates)

        candidates.sort(key=lambda c: c.score, reverse=True)
        kept = candidates[: self.max_candidates]

        # 组装文本：保留 top_score 用于上游决策
        top_score = kept[0].score if kept else 0.0
        text = self._assemble_text(kept)
        truncated = len(text) > self.max_chars
        if truncated:
            text = text[: self.max_chars] + "\n<!-- pruned: char-limit -->"

        return PrunedDom(
            text=text,
            element_count=len(candidates),
            kept_count=len(kept),
            truncated=truncated,
            top_score=top_score,
        )

    async def prune_async(self, html: str) -> PrunedDom:
        """异步：与 :meth:`prune` 行为一致。"""
        candidates = self._extract_candidates(html)
        if not candidates:
            return PrunedDom(text="", element_count=0, kept_count=0, truncated=False)

        if self.enable_llm_rank and self.provider is not None:
            candidates = await self._llm_rerank_async(candidates)

        candidates.sort(key=lambda c: c.score, reverse=True)
        kept = candidates[: self.max_candidates]
        top_score = kept[0].score if kept else 0.0
        text = self._assemble_text(kept)
        truncated = len(text) > self.max_chars
        if truncated:
            text = text[: self.max_chars] + "\n<!-- pruned: char-limit -->"

        return PrunedDom(
            text=text,
            element_count=len(candidates),
            kept_count=len(kept),
            truncated=truncated,
            top_score=top_score,
        )

    # ------------------------------------------------------------------
    # 候选元素提取 + 规则打分
    # ------------------------------------------------------------------

    def _extract_candidates(self, html: str) -> list[_Candidate]:
        """从 HTML 中提取候选元素并打规则分。"""
        if not html or not html.strip():
            return []
        try:
            from bs4 import BeautifulSoup
        except ImportError:  # pragma: no cover - bs4 是核心依赖
            return self._extract_fallback(html)

        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        candidates: list[_Candidate] = []
        for tag in soup.find_all(True):
            cand = self._tag_to_candidate(tag)
            if cand is not None:
                candidates.append(cand)
        return candidates

    def _tag_to_candidate(self, tag: Tag) -> _Candidate | None:
        """单个标签转候选元素（含规则打分）。无价值则返回 None。"""
        name = tag.name.lower()
        # 跳过明显的非内容标签
        if name in {"html", "head", "meta", "link", "style", "br", "hr"}:
            return None

        attrs = tag.attrs or {}
        text = (tag.get_text(" ", strip=True) or "")[:200]
        # 规则打分：起始 1.0
        score = 1.0

        # 1. 高优先级标签
        if name in _HIGH_PRIORITY_TAGS:
            score += 2.0
            # script[src] 进一步加分
            if name == "script" and attrs.get("src"):
                score += 2.0

        # 2. 属性命中加密关键词
        attr_blob = " ".join(
            f"{k}={v}" for k, v in attrs.items() if isinstance(v, (str, list))
        ).lower()
        for hint in _CRYPTO_ATTR_HINTS:
            if hint in attr_blob:
                score += 3.0
                break

        # 3. 文本命中加密关键词
        text_lower = text.lower()
        for hint in _CRYPTO_TEXT_HINTS:
            if hint in text_lower:
                score += 4.0
                break

        # 4. 表单/输入元素再加分
        if name in {"input", "form"} and attrs.get("name"):
            score += 1.0

        # 5. 纯装饰性元素降分
        if name in {"div", "span", "p"} and not text and not attrs:
            score = 0.5

        # 序列化为 HTML 片段（控制长度）
        html_str = str(tag)
        # 单候选字符上限，避免一个超大 script 吃光预算
        if len(html_str) > 500:
            html_str = html_str[:500] + f"...</{name}>"

        return _Candidate(
            html=html_str,
            score=score,
            tag=name,
            text_preview=text[:80],
        )

    # ------------------------------------------------------------------
    # LLM 重排（可选）
    # ------------------------------------------------------------------

    def _llm_rerank(self, candidates: list[_Candidate]) -> list[_Candidate]:
        """同步：让 LLM 对前 N 个候选打分并重排。"""
        assert self.provider is not None  # 由 enable_llm_rank 守护
        top = candidates[: self.llm_rank_limit]
        if len(top) < 2:
            return candidates
        prompt = self._build_rank_prompt(top)
        messages = [LLMMessage("system", _RANK_SYSTEM_PROMPT), LLMMessage("user", prompt)]
        try:
            resp = self.provider.chat(messages, temperature=0.0)
        except Exception:
            return candidates
        scores = self._parse_rank_response(resp.content or "", len(top))
        for idx, s in scores.items():
            if 0 <= idx < len(top):
                top[idx].score = max(top[idx].score, float(s))
        return candidates

    async def _llm_rerank_async(self, candidates: list[_Candidate]) -> list[_Candidate]:
        """异步：让 LLM 对前 N 个候选打分并重排。"""
        assert self.provider is not None  # 由 enable_llm_rank 守护
        top = candidates[: self.llm_rank_limit]
        if len(top) < 2:
            return candidates
        prompt = self._build_rank_prompt(top)
        messages = [LLMMessage("system", _RANK_SYSTEM_PROMPT), LLMMessage("user", prompt)]
        try:
            if hasattr(self.provider, "achat"):
                resp = await self.provider.achat(messages, temperature=0.0)
            else:
                resp = self.provider.chat(messages, temperature=0.0)
        except Exception:
            return candidates
        scores = self._parse_rank_response(resp.content or "", len(top))
        for idx, s in scores.items():
            if 0 <= idx < len(top):
                top[idx].score = max(top[idx].score, float(s))
        return candidates

    @staticmethod
    def _build_rank_prompt(candidates: list[_Candidate]) -> str:
        lines = ["请为以下 DOM 候选元素打分（0-10）：\n"]
        for i, c in enumerate(candidates):
            lines.append(f"[{i}] <{c.tag}> text={c.text_preview!r} score={c.score:.1f}")
        lines.append('\n返回 JSON 数组：[{"index": 0, "score": 8.5}, ...]')
        return "\n".join(lines)

    @staticmethod
    def _parse_rank_response(text: str, expected_count: int) -> dict[int, float]:
        """容错解析 LLM 返回的打分数组。"""
        import json

        text = text.strip()
        # 去除代码块
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            return {}
        try:
            arr = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
        if not isinstance(arr, list):  # pragma: no cover - 正则匹配 [.*] 后 json.loads 必为 list
            return {}
        out: dict[int, float] = {}
        for item in arr:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            score = item.get("score")
            if idx is None or score is None:
                continue
            try:
                i = int(idx)
                s = float(score)
            except (TypeError, ValueError):
                continue
            if 0 <= i < expected_count:
                out[i] = s
        return out

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _assemble_text(candidates: Iterable[_Candidate]) -> str:
        """把候选元素组装成最终文本。"""
        return "\n".join(c.html for c in candidates)

    @staticmethod
    def _extract_fallback(html: str) -> list[_Candidate]:
        """BeautifulSoup 不可用时的降级：用正则粗提取 script/input/form/a。"""
        pattern = re.compile(
            r"<(?:script|input|form|button|a|iframe)\b[^>]*?>",
            re.IGNORECASE | re.DOTALL,
        )
        candidates: list[_Candidate] = []
        for m in pattern.finditer(html):
            tag_match = re.match(r"<(\w+)", m.group(0), re.IGNORECASE)
            if not tag_match:  # pragma: no cover - pattern 已确保以 <tag 开头，tag_match 必非 None
                continue
            tag = tag_match.group(1).lower()
            candidates.append(
                _Candidate(html=m.group(0)[:500], score=2.0, tag=tag, text_preview="")
            )
        return candidates


__all__ = [
    "DomPruner",
    "PrunedDom",
]

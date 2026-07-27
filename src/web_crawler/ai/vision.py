"""Vision-LLM 截图感知模块。

借鉴 browser-use / Skyvern / OpenAI Operator 的双模态感知思路，给
:class:`~web_crawler.ai.reverse_agent.ReverseAgent` 增加 vision 通道：
截屏后交给支持 vision 的 LLM（如 DeepSeek-Vision / gpt-4o / qwen-vl-max /
claude-sonnet-4-5），让它对页面做自然语言问答、定位元素、判断当前状态。

能力清单
--------
- :meth:`VisionObserver.capture` — 截屏并返回 PNG bytes / base64；
- :meth:`VisionObserver.describe` — 让 LLM 描述当前截图；
- :meth:`VisionObserver.answer` — 针对截图提问；
- :meth:`VisionObserver.locate` — 用文本描述定位元素，返回 bbox 列表；
- :meth:`VisionObserver.detect_state` — 判断页面处于哪一类状态
  （加载中 / 登录页 / 验证码 / 业务页 / 错误页 / 已完成）。

设计要点
--------
- 自动协商能力：若传入的 provider 不支持 vision（``caps.vision=False``），
  :meth:`describe` 等方法直接抛 ``RuntimeError`` 而不是发出注定失败的请求；
- 同步与异步接口并存，对应 :class:`ReverseAgent` 的 ``run`` / ``arun``；
- 不强依赖 playwright / camoufox，``page`` 接收任何带 ``screenshot`` 方法的对象。
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .llm import LLMMessage, LLMProvider

if TYPE_CHECKING:
    from typing import Protocol

    class _Screenshotable(Protocol):
        def screenshot(self, *, full_page: bool = ...) -> bytes: ...


# 让 LLM 输出可解析的 JSON 时的系统提示
_VISION_SYSTEM_PROMPT = (
    "你是一名视觉理解专家。用户会给你一张网页截图，请按用户的问题精确作答。"
    "如果用户要求输出 JSON，请只输出 JSON 对象，不要 Markdown 代码块标记，"
    "不要任何额外说明文字。"
)

_STATE_SYSTEM_PROMPT = (
    "你是网页状态分类器。给定一张截图，判断页面当前所处的状态类别，"
    "只能从以下枚举中选择："
    "loading / login / captcha / business / error / done / unknown。"
    '输出 JSON：{"state": "...", "reason": "..."}。'
)

# JSON / 代码块解析正则
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"^```(?:\w+)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)

# 截图最大尺寸（像素），超过会等比缩放（在 page 端做）
_MAX_SCREENSHOT_DIM = 1920


@dataclass
class BoundingBox:
    """视觉定位返回的元素包围盒（与 Playwright 一致的 x/y/width/height）。"""

    x: float
    y: float
    width: float
    height: float
    label: str = ""

    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "label": self.label,
        }


@dataclass
class VisionResult:
    """一次 vision 调用的归一化结果。"""

    text: str
    parsed: dict[str, Any] | None = None
    raw_b64: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


def _extract_json(text: str) -> dict[str, Any]:
    """容错解析 LLM 回复中的 JSON 对象。"""
    text = text.strip()
    fence = _CODE_FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return __import__("json").loads(text)
    except Exception:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return __import__("json").loads(m.group(0))
        except Exception:
            return {}
    return {}


class VisionObserver:
    """浏览器截图 → VLM 问答的桥接器。

    Parameters
    ----------
    provider:
        必须是支持 vision 的 :class:`~web_crawler.ai.llm.LLMProvider`
        （``provider.capabilities.vision == True``）。否则调用方法会抛错。
    detail:
        OpenAI vision 协议的 ``detail`` 字段，``low`` 省钱、``high`` 精细、
        ``auto`` 让模型自选。默认 ``high``，逆向场景对细节敏感。
    full_page:
        截图是否覆盖整页（默认 ``False``，只截视口；逆向通常关心首屏）。
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        detail: str = "high",
        full_page: bool = False,
    ) -> None:
        caps = getattr(provider, "capabilities", None)
        if caps is None or not getattr(caps, "vision", False):
            raise RuntimeError(
                f"provider {getattr(provider, 'name', provider)!r} does not "
                "declare vision capability; pass a vision-capable provider "
                "(e.g. OpenAIProvider, AnthropicProvider, QwenProvider, "
                "or DeepSeekProvider(model='deepseek-vision-*'))"
            )
        self.provider = provider
        self.detail = detail
        self.full_page = full_page

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------

    def capture(self, page: Any) -> bytes:
        """同步截图，返回 PNG bytes。"""
        return page.screenshot(full_page=self.full_page, type="png")

    async def capture_async(self, page: Any) -> bytes:
        """异步截图。"""
        return await page.screenshot(full_page=self.full_page, type="png")

    @staticmethod
    def _to_b64(png: bytes) -> str:
        """PNG bytes → 不带前缀的 base64 字符串。"""
        return base64.b64encode(png).decode("ascii")

    # ------------------------------------------------------------------
    # 通用 vision 问答
    # ------------------------------------------------------------------

    def ask(
        self,
        page: Any,
        question: str,
        *,
        system: str = _VISION_SYSTEM_PROMPT,
        temperature: float = 0.0,
    ) -> VisionResult:
        """同步：截图后让 LLM 回答 ``question``。"""
        b64 = self._to_b64(self.capture(page))
        return self._ask_with_b64(b64, question, system=system, temperature=temperature)

    async def ask_async(
        self,
        page: Any,
        question: str,
        *,
        system: str = _VISION_SYSTEM_PROMPT,
        temperature: float = 0.0,
    ) -> VisionResult:
        """异步：截图后让 LLM 回答 ``question``。"""
        b64 = self._to_b64(await self.capture_async(page))
        return self._ask_with_b64(b64, question, system=system, temperature=temperature)

    def _ask_with_b64(
        self,
        b64: str,
        question: str,
        *,
        system: str,
        temperature: float,
    ) -> VisionResult:
        msg = LLMMessage.vision("user", question, b64, detail=self.detail)
        messages: list[Any] = [LLMMessage("system", system), msg]
        if hasattr(self.provider, "achat"):
            # 调用方用同步入口，provider 暴露 achat 也不主动走异步
            pass
        resp = self.provider.chat(messages, temperature=temperature)
        return VisionResult(
            text=resp.content or "",
            raw_b64=b64,
            usage=resp.usage,
        )

    # ------------------------------------------------------------------
    # 描述
    # ------------------------------------------------------------------

    def describe(self, page: Any, *, focus: str = "") -> str:
        """让 LLM 用自然语言描述截图内容。"""
        q = "请描述这张网页截图的内容。"
        if focus:
            q += f" 重点：{focus}"
        return self.ask(page, q).text

    async def describe_async(self, page: Any, *, focus: str = "") -> str:
        q = "请描述这张网页截图的内容。"
        if focus:
            q += f" 重点：{focus}"
        return (await self.ask_async(page, q)).text

    # ------------------------------------------------------------------
    # 提问
    # ------------------------------------------------------------------

    def answer(self, page: Any, question: str) -> str:
        """针对截图自由提问。"""
        return self.ask(page, question).text

    async def answer_async(self, page: Any, question: str) -> str:
        return (await self.ask_async(page, question)).text

    # ------------------------------------------------------------------
    # 元素定位
    # ------------------------------------------------------------------

    def locate(self, page: Any, description: str) -> list[BoundingBox]:
        """用自然语言描述定位元素，返回 BoundingBox 列表。

        LLM 应输出 ``{"elements": [{"x":..., "y":..., "width":..., "height":..., "label":...}, ...]}``。
        解析失败或无元素返回空列表。
        """
        prompt = (
            f"在截图中找出符合描述的元素：{description}\n"
            '输出 JSON：{"elements": [{"x": 0, "y": 0, "width": 0, '
            '"height": 0, "label": "..."}, ...]}。坐标以截图左上角为原点，'
            "单位为像素。如未找到，返回空数组。"
        )
        result = self.ask(page, prompt)
        parsed = _extract_json(result.text)
        elements = parsed.get("elements") if isinstance(parsed, dict) else None
        if not isinstance(elements, list):
            return []
        out: list[BoundingBox] = []
        for el in elements:
            if not isinstance(el, dict):
                continue
            try:
                out.append(
                    BoundingBox(
                        x=float(el.get("x", 0)),
                        y=float(el.get("y", 0)),
                        width=float(el.get("width", 0)),
                        height=float(el.get("height", 0)),
                        label=str(el.get("label", "")),
                    )
                )
            except (TypeError, ValueError):
                continue
        return out

    async def locate_async(self, page: Any, description: str) -> list[BoundingBox]:
        prompt = (
            f"在截图中找出符合描述的元素：{description}\n"
            '输出 JSON：{"elements": [{"x": 0, "y": 0, "width": 0, '
            '"height": 0, "label": "..."}, ...]}。坐标以截图左上角为原点，'
            "单位为像素。如未找到，返回空数组。"
        )
        result = await self.ask_async(page, prompt)
        parsed = _extract_json(result.text)
        elements = parsed.get("elements") if isinstance(parsed, dict) else None
        if not isinstance(elements, list):
            return []
        out: list[BoundingBox] = []
        for el in elements:
            if not isinstance(el, dict):
                continue
            try:
                out.append(
                    BoundingBox(
                        x=float(el.get("x", 0)),
                        y=float(el.get("y", 0)),
                        width=float(el.get("width", 0)),
                        height=float(el.get("height", 0)),
                        label=str(el.get("label", "")),
                    )
                )
            except (TypeError, ValueError):
                continue
        return out

    # ------------------------------------------------------------------
    # 状态识别
    # ------------------------------------------------------------------

    def detect_state(self, page: Any) -> dict[str, Any]:
        """判断页面处于哪一类状态，返回 ``{"state": ..., "reason": ...}``。"""
        prompt = "判断这张截图对应的页面状态类别。"
        result = self.ask(page, prompt, system=_STATE_SYSTEM_PROMPT)
        parsed = _extract_json(result.text)
        if not isinstance(parsed, dict):
            return {"state": "unknown", "reason": result.text[:200]}
        return {
            "state": str(parsed.get("state", "unknown")),
            "reason": str(parsed.get("reason", "")),
        }

    async def detect_state_async(self, page: Any) -> dict[str, Any]:
        prompt = "判断这张截图对应的页面状态类别。"
        result = await self.ask_async(page, prompt, system=_STATE_SYSTEM_PROMPT)
        parsed = _extract_json(result.text)
        if not isinstance(parsed, dict):
            return {"state": "unknown", "reason": result.text[:200]}
        return {
            "state": str(parsed.get("state", "unknown")),
            "reason": str(parsed.get("reason", "")),
        }


__all__ = [
    "BoundingBox",
    "VisionObserver",
    "VisionResult",
]

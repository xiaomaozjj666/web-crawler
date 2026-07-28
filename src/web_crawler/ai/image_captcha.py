"""图片验证码破解模块。

合规声明：仅用于已获书面授权的安全测试与自动化场景，未经授权对他人系统
使用本模块违法。

提供三类图片验证码识别能力：

- **文本字符 OCR**：识别 4-8 位字母数字验证码
- **滑块缺口定位**：识别滑块需要移动到的目标 x 坐标
- **点选坐标识别**：识别需要按顺序点击的元素坐标

支持多种后端，按优先级自动降级：

1. **本地 ddddocr**（可选依赖，OCR 文本字符）
2. **Pillow + numpy 模板匹配**（滑块专用，已有 Pillow 依赖）
3. **LLM Vision**（OpenAI gpt-4o / Anthropic Claude / Qwen-vl-max /
   DeepSeek-vision 等支持 vision 的模型，由 LLMProvider 协商）

设计要点
--------
- 不强制任何依赖：未安装 ddddocr / numpy 时自动降级，方法返回 None / 空串
- 单一模型策略兼容：默认走本地路径，LLM Vision 仅在 provider 支持 vision
  能力时启用；与项目"统一 DeepSeek-V4-Pro"策略不冲突（用户可额外配置
  vision-capable provider 仅供图片识别）
- 失败兜底：所有方法返回 None / 空字符串表示识别失败，调用方决定是否
  交人工
- 同步与异步接口并存，对应 :class:`ReverseAgent` 的 ``run`` / ``arun``
"""

from __future__ import annotations

import base64
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMMessage, LLMProvider

# JSON / 代码块解析正则
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"^```(?:\w+)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


@dataclass(slots=True)
class ImageSolverConfig:
    """图片验证码识别配置。"""

    use_llm: bool = True
    use_local_ocr: bool = True
    use_pillow_slider: bool = True
    detail: str = "high"
    temperature: float = 0.0
    max_retries: int = 2
    ocr_charset: str = (
        "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    )
    ocr_max_length: int = 8


@dataclass(slots=True)
class SliderSolution:
    """滑块破解结果。"""

    x: int
    y: int = 0
    method: str = ""
    confidence: float = 0.0


@dataclass(slots=True)
class ClickSolution:
    """点选破解结果。"""

    points: list[tuple[int, int]] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    method: str = ""


def _to_b64(image: bytes | str) -> str:
    """bytes 或带前缀的 base64 字符串 → 不带前缀的 base64 字符串。"""
    if isinstance(image, str):
        if image.startswith("data:"):
            return image.split(",", 1)[-1]
        return image
    return base64.b64encode(image).decode("ascii")


def _b64_to_bytes(image: bytes | str) -> bytes:
    """bytes 或 base64 字符串 → 原始 bytes。"""
    if isinstance(image, str):
        if image.startswith("data:"):
            image = image.split(",", 1)[-1]
        return base64.b64decode(image)
    return image


def _extract_json(text: str) -> dict[str, Any]:
    """容错解析 LLM 回复中的 JSON 对象。"""
    text = text.strip()
    fence = _CODE_FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = _JSON_OBJ_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}
    return {}


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_OCR_SYSTEM_PROMPT = (
    "你是验证码识别专家。用户会给你一张验证码图片，请识别其中的字符。"
    "只输出识别到的字符本身，不要任何额外文字、标点、Markdown、换行。"
    "区分大小写。如不确定，输出最可能的字符序列。"
)

_SLIDER_SYSTEM_PROMPT = (
    "你是滑块验证码识别专家。用户会给你一张滑块验证码背景图（含缺口）和"
    "一张滑块图（参考形状）。请识别滑块需要移动到的目标 x 坐标，"
    "即缺口左边界相对背景图左上角的像素偏移。"
    '只输出 JSON：{"x": <int>, "confidence": <0-1>}。不要任何额外文字。'
)

_CLICK_SYSTEM_PROMPT = (
    "你是点选验证码识别专家。用户会给你一张点选验证码图片和一段提示文字，"
    "请识别需要按顺序点击的元素坐标。"
    '只输出 JSON：{"points": [{"x": <int>, "y": <int>, "label": "..."}, ...]}。'
    "坐标以图片左上角为原点，单位为像素。不要任何额外文字。"
)


class ImageCaptchaSolver:
    """图片验证码破解器。

    Parameters
    ----------
    provider:
        LLM Provider 实例。若为 None 或不支持 vision，则仅启用本地路径
        （ddddocr OCR + Pillow/numpy 滑块匹配），点选验证码无法处理。
    config:
        配置；不传用默认值。
    """

    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        config: ImageSolverConfig | None = None,
    ) -> None:
        self.provider = provider
        self.config = config or ImageSolverConfig()

    @property
    def llm_vision_available(self) -> bool:
        """是否可用 LLM vision 路径。"""
        if not self.config.use_llm or self.provider is None:
            return False
        caps = getattr(self.provider, "capabilities", None)
        return caps is not None and getattr(caps, "vision", False)

    # ------------------------------------------------------------------
    # 文本字符 OCR
    # ------------------------------------------------------------------

    def solve_text(self, image: bytes | str, *, mime: str = "image/png") -> str:
        """识别图片中的文本字符（4-8 位字母数字）。"""
        if self.config.use_local_ocr:
            text = self._local_ocr(image)
            if text:
                return text
        if self.llm_vision_available:
            return self._llm_ocr(image, mime)
        return ""

    async def solve_text_async(
        self,
        image: bytes | str,
        *,
        mime: str = "image/png",
    ) -> str:
        if self.config.use_local_ocr:
            text = self._local_ocr(image)
            if text:
                return text
        if self.llm_vision_available:
            return await self._llm_ocr_async(image, mime)
        return ""

    def _local_ocr(self, image: bytes | str) -> str:
        """尝试 ddddocr 本地识别；未安装或识别失败返回空。"""
        try:
            import ddddocr  # type: ignore[import-untyped]
        except ImportError:
            return ""
        try:
            data = _b64_to_bytes(image)
            # show_ad=False 关闭作者广告输出
            ocr = ddddocr.DdddOcr(show_ad=False)
            text = ocr.classification(data)
            allowed = set(self.config.ocr_charset)
            text = "".join(c for c in text if c in allowed)
            if 0 < len(text) <= self.config.ocr_max_length:
                return text
            return ""
        except Exception:
            return ""

    def _llm_ocr(self, image: bytes | str, mime: str) -> str:
        assert self.provider is not None
        b64 = _to_b64(image)
        msg = LLMMessage.vision(
            "user",
            "请识别这张验证码图片中的字符。",
            b64,
            mime=mime,
            detail=self.config.detail,
        )
        resp = self.provider.chat(
            [LLMMessage("system", _OCR_SYSTEM_PROMPT), msg],
            temperature=self.config.temperature,
        )
        text = "".join((resp.content or "").split())
        return text[: self.config.ocr_max_length]

    async def _llm_ocr_async(self, image: bytes | str, mime: str) -> str:
        assert self.provider is not None
        if not hasattr(self.provider, "achat"):
            return self._llm_ocr(image, mime)
        b64 = _to_b64(image)
        msg = LLMMessage.vision(
            "user",
            "请识别这张验证码图片中的字符。",
            b64,
            mime=mime,
            detail=self.config.detail,
        )
        resp = await self.provider.achat(
            [LLMMessage("system", _OCR_SYSTEM_PROMPT), msg],
            temperature=self.config.temperature,
        )
        text = "".join((resp.content or "").split())
        return text[: self.config.ocr_max_length]

    # ------------------------------------------------------------------
    # 滑块缺口定位
    # ------------------------------------------------------------------

    def solve_slider(
        self,
        bg: bytes | str,
        slider: bytes | str,
    ) -> SliderSolution | None:
        """识别滑块缺口 x 坐标。"""
        if self.config.use_pillow_slider:
            x = self._local_slider(bg, slider)
            if x is not None and x > 0:
                return SliderSolution(x=x, method="pillow", confidence=0.7)
        if self.llm_vision_available:
            return self._llm_slider(bg, slider)
        return None

    async def solve_slider_async(
        self,
        bg: bytes | str,
        slider: bytes | str,
    ) -> SliderSolution | None:
        if self.config.use_pillow_slider:
            x = self._local_slider(bg, slider)
            if x is not None and x > 0:
                return SliderSolution(x=x, method="pillow", confidence=0.7)
        if self.llm_vision_available:
            return await self._llm_slider_async(bg, slider)
        return None

    def _local_slider(
        self,
        bg: bytes | str,
        slider: bytes | str,
    ) -> int | None:
        """Pillow + numpy 模板匹配定位缺口 x 坐标。"""
        bg_data = _b64_to_bytes(bg)
        slider_data = _b64_to_bytes(slider)
        # 优先 numpy 加速路径
        try:
            import numpy as np  # type: ignore[import-untyped]
            from PIL import Image
        except ImportError:
            return self._pillow_only_slider(bg_data, slider_data)

        try:
            bg_img = np.asarray(
                Image.open(io.BytesIO(bg_data)).convert("L"), dtype=np.float32
            )
            slider_img = np.asarray(
                Image.open(io.BytesIO(slider_data)).convert("L"), dtype=np.float32
            )
        except Exception:
            return None

        if bg_img.ndim != 2 or slider_img.ndim != 2:
            return None
        bh, bw = bg_img.shape
        sh, sw = slider_img.shape
        if sw > bw or sh > bh:
            return None

        # 滑块掩码：亮度 > 30 视为非透明
        mask = slider_img > 30
        mask_count = max(1, int(mask.sum()))

        min_ssd = float("inf")
        min_x = sw
        # 步长 2 加速；从 sw 开始（缺口不会在 slider 起始位置）
        for x in range(sw, bw - sw, 2):
            region = bg_img[:sh, x : x + sw]
            diff = (region - slider_img) * mask
            ssd = float((diff * diff).sum()) / mask_count
            if ssd < min_ssd:
                min_ssd = ssd
                min_x = x
        return min_x

    def _pillow_only_slider(
        self,
        bg_data: bytes,
        slider_data: bytes,
    ) -> int | None:
        """无 numpy 时的纯 Pillow 滑窗匹配（子采样加速）。"""
        try:
            from PIL import Image
        except ImportError:
            return None
        try:
            bg_img = Image.open(io.BytesIO(bg_data)).convert("L")
            slider_img = Image.open(io.BytesIO(slider_data)).convert("L")
        except Exception:
            return None

        bw, bh = bg_img.size
        sw, sh = slider_img.size
        if sw > bw or sh > bh:
            return None

        bg_px = bg_img.load()
        slider_px = slider_img.load()

        min_ssd = float("inf")
        min_x = sw
        # x 步长 3，y 步长 2 子采样
        for x in range(sw, bw - sw, 3):
            ssd = 0.0
            count = 0
            for sy in range(0, sh, 2):
                for sx in range(0, sw, 2):
                    sp = int(slider_px[sx, sy])  # type: ignore[index,arg-type]
                    if sp < 30:
                        continue
                    bp = int(bg_px[x + sx, sy])  # type: ignore[index,arg-type]
                    d = bp - sp
                    ssd += d * d
                    count += 1
            if count > 0 and ssd < min_ssd:
                min_ssd = ssd
                min_x = x
        return min_x

    def _llm_slider(
        self,
        bg: bytes | str,
        slider: bytes | str,
    ) -> SliderSolution | None:
        assert self.provider is not None
        bg_b64 = _to_b64(bg)
        slider_b64 = _to_b64(slider)
        msg = LLMMessage(
            role="user",
            content=[
                {"type": "text", "text": "请识别这张滑块验证码背景图中的缺口位置。"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{bg_b64}",
                        "detail": self.config.detail,
                    },
                },
                {"type": "text", "text": "这是滑块图（参考形状）："},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{slider_b64}",
                        "detail": "low",
                    },
                },
            ],
        )
        resp = self.provider.chat(
            [LLMMessage("system", _SLIDER_SYSTEM_PROMPT), msg],
            temperature=self.config.temperature,
        )
        return self._parse_slider_response(resp.content or "")

    async def _llm_slider_async(
        self,
        bg: bytes | str,
        slider: bytes | str,
    ) -> SliderSolution | None:
        assert self.provider is not None
        if not hasattr(self.provider, "achat"):
            return self._llm_slider(bg, slider)
        bg_b64 = _to_b64(bg)
        slider_b64 = _to_b64(slider)
        msg = LLMMessage(
            role="user",
            content=[
                {"type": "text", "text": "请识别这张滑块验证码背景图中的缺口位置。"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{bg_b64}",
                        "detail": self.config.detail,
                    },
                },
                {"type": "text", "text": "这是滑块图（参考形状）："},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{slider_b64}",
                        "detail": "low",
                    },
                },
            ],
        )
        resp = await self.provider.achat(
            [LLMMessage("system", _SLIDER_SYSTEM_PROMPT), msg],
            temperature=self.config.temperature,
        )
        return self._parse_slider_response(resp.content or "")

    @staticmethod
    def _parse_slider_response(text: str) -> SliderSolution | None:
        parsed = _extract_json(text)
        try:
            x = int(parsed.get("x", -1))
        except (TypeError, ValueError):
            return None
        if x < 0:
            return None
        try:
            conf = float(parsed.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        return SliderSolution(x=x, method="llm", confidence=conf)

    # ------------------------------------------------------------------
    # 点选坐标识别
    # ------------------------------------------------------------------

    def solve_click(
        self,
        image: bytes | str,
        prompt: str,
        *,
        mime: str = "image/png",
    ) -> ClickSolution | None:
        """识别点选验证码需要按顺序点击的元素坐标。"""
        if not self.llm_vision_available:
            return None
        assert self.provider is not None
        b64 = _to_b64(image)
        msg = LLMMessage.vision(
            "user",
            f"提示：{prompt}\n请识别这张点选验证码图片中需要按顺序点击的元素坐标。",
            b64,
            mime=mime,
            detail=self.config.detail,
        )
        resp = self.provider.chat(
            [LLMMessage("system", _CLICK_SYSTEM_PROMPT), msg],
            temperature=self.config.temperature,
        )
        return self._parse_click_response(resp.content or "")

    async def solve_click_async(
        self,
        image: bytes | str,
        prompt: str,
        *,
        mime: str = "image/png",
    ) -> ClickSolution | None:
        if not self.llm_vision_available:
            return None
        assert self.provider is not None
        b64 = _to_b64(image)
        msg = LLMMessage.vision(
            "user",
            f"提示：{prompt}\n请识别这张点选验证码图片中需要按顺序点击的元素坐标。",
            b64,
            mime=mime,
            detail=self.config.detail,
        )
        if not hasattr(self.provider, "achat"):
            resp = self.provider.chat(
                [LLMMessage("system", _CLICK_SYSTEM_PROMPT), msg],
                temperature=self.config.temperature,
            )
            return self._parse_click_response(resp.content or "")
        resp = await self.provider.achat(
            [LLMMessage("system", _CLICK_SYSTEM_PROMPT), msg],
            temperature=self.config.temperature,
        )
        return self._parse_click_response(resp.content or "")

    @staticmethod
    def _parse_click_response(text: str) -> ClickSolution | None:
        parsed = _extract_json(text)
        points_raw = parsed.get("points") if isinstance(parsed, dict) else None
        if not isinstance(points_raw, list):
            return None
        points: list[tuple[int, int]] = []
        labels: list[str] = []
        for p in points_raw:
            if not isinstance(p, dict):
                continue
            try:
                x = int(p.get("x", -1))
                y = int(p.get("y", -1))
            except (TypeError, ValueError):
                continue
            if x < 0 or y < 0:
                continue
            points.append((x, y))
            labels.append(str(p.get("label", "")))
        if not points:
            return None
        return ClickSolution(points=points, labels=labels, method="llm")


__all__ = [
    "ClickSolution",
    "ImageCaptchaSolver",
    "ImageSolverConfig",
    "SliderSolution",
]

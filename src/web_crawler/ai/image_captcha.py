"""图片验证码破解模块。

合规声明：仅用于已获书面授权的安全测试与自动化场景，未经授权对他人系统
使用本模块违法。

**数据流提示（隐私）**：启用 LLM Vision / math_mode 时，目标站点的验证码
图片会以 base64 上传到你配置的 LLM 提供商（DeepSeek 等）。请确认所处理
站点授权你这样做，且该外发数据不含敏感内容；key 与端点见 SECURITY.md
（``LLM_VISION_API_KEY`` 可与文本 key 分离计费）。

提供三类图片验证码识别能力：

- **文本字符 OCR**：识别 4-8 位字母数字验证码
- **滑块缺口定位**：识别滑块需要移动到的目标 x 坐标
- **点选坐标识别**：识别需要按顺序点击的元素坐标

支持多种后端，按优先级自动降级：

1. **本地 ddddocr**（可选依赖，OCR 文本字符）
2. **Pillow + numpy 掩码互相关**（滑块专用，已有 Pillow 依赖；对"缺口
   被变暗"类验证码用 ZNCC 抗亮度缩放，低置信分自动让位给 LLM 路径）
3. **LLM Vision**（OpenAI gpt-4o / Anthropic Claude / Qwen-vl-max /
   DeepSeek 等支持 vision 的模型，由 LLMProvider 协商；deepseek-flash
   已实测支持 image_url 消息，key 可用 ``LLM_VISION_API_KEY`` 单独注入）

设计要点
--------
- 不强制任何依赖：未安装 ddddocr / numpy 时自动降级，方法返回 None / 空串
- 单一模型策略兼容：默认走本地路径，LLM Vision 仅在 provider 支持 vision
  能力时启用；与项目"统一 DeepSeek-V4.1-Flash"策略不冲突（用户可额外配置
  vision-capable provider 仅供图片识别）
- 失败兜底：所有方法返回 None / 空字符串表示识别失败，调用方决定是否
  交人工
- 同步与异步接口并存，对应 :class:`ReverseAgent` 的 ``run`` / ``arun``
"""

from __future__ import annotations

import base64
import io
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

from ._jsonutil import extract_json as _extract_json
from .llm import LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

# 送 LLM 前的图片体积/尺寸/像素上限（防超大截图撑爆请求体或 OOM）
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_PIXELS = 40_000_000
_VISION_MAX_SIDE = 1280
# 剪影异常探测器（ZNCC 失败后的二级定位）的三重门限，阈值由 2026-11-14
# 的 8 对真图标定（yidun/geetest 命中、ZNCC 通过家族全部拒绝）：
_SILHOUETTE_MIN_SCORE = 50.0
_SILHOUETTE_MIN_DIFF = 45.0
_SILHOUETTE_MIN_SALIENCE = 15.0
# 本地匹配的资源上限：目标站点的验证码图是不可信输入，超大图会灼烧 CPU
# （ZNCC/剪影两级的计算量都随 O(bw × blob) 增长）。超过即拒绝（fail-closed，
# 与 _prepare_vision_image 对 LLM 路径的上限同思路）。
_MATCH_MAX_BG_PIXELS = 4_000_000
_MATCH_MAX_PIECE_PIXELS = 500_000


@dataclass(slots=True)
class ImageSolverConfig:
    """图片验证码识别配置。

    LLM 调用的重试/退避由 provider 层（``llm.OpenAICompatibleProvider``）
    统一负责，这里不再声明 ``max_retries``。
    """

    use_llm: bool = True
    use_local_ocr: bool = True
    use_pillow_slider: bool = True
    detail: str = "high"
    temperature: float = 0.0
    ocr_charset: str = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    ocr_max_length: int = 8
    # 本地滑块匹配（掩码 ZNCC）的最低置信分：低于该值视为"本地识别失败"，
    # 交由 LLM vision 路径兜底，而不是给出一个自信但错误的坐标。
    slider_min_score: float = 0.45
    # 数学运算验证码（如 "7*1=?"）：开启后 solve_text 返回计算结果而非算式
    # 原文。本地路径允许运算符字符并在可解析时直接求值；LLM 路径改用计算
    # 提示词直接输出答案。
    math_mode: bool = False


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
    """bytes 或 base64 字符串 → 原始 bytes。

    非法 base64（坏 padding 等）可能抛 ``binascii.Error``，调用方需自行
    捕获并按识别失败处理。
    """
    if isinstance(image, str):
        if image.startswith("data:"):
            image = image.split(",", 1)[-1]
        return base64.b64decode(image)
    return image


def _prepare_vision_image(
    image: bytes | str,
    mime: str = "image/png",
    max_side: int = _VISION_MAX_SIDE,
) -> tuple[str, str, float] | None:
    """归一化送 LLM 的图片：体积/像素上限检查 + 超长边降采样。

    返回 ``(base64_str, mime, scale)``：
    - ``mime``：降采样后统一重编码为 PNG，故降采样时返回 ``image/png``；
    - ``scale``：原图最长边 / 处理后最长边，用于把 LLM 返回的坐标还原回
      原始像素坐标系（滑块缺口等场景）。
    坏 base64、超体积、超像素返回 ``None``（识别失败）；无法解码的图片
    原样透传（识别失败由上层解析兜底，不做降采样）。
    """
    try:
        raw = _b64_to_bytes(image)
    except Exception as exc:
        logger.warning("failed to decode base64 image data: %s", exc)
        return None
    if len(raw) > _MAX_IMAGE_BYTES:
        return None
    try:
        from PIL import Image
    except ImportError:
        # 无 Pillow：不做尺寸检查/降采样，原样送（体积上限已检查）
        logger.debug("Pillow not installed; skipping image size check and downscale")
        return _to_b64(raw), mime, 1.0
    try:
        img: Image.Image = Image.open(io.BytesIO(raw))
        w, h = img.size
    except Exception as exc:
        # 无法解码的图片：原样透传
        logger.debug("image decode failed; passing through unchanged: %s", exc)
        return _to_b64(raw), mime, 1.0
    if w * h > _MAX_IMAGE_PIXELS:
        return None
    if max(w, h) <= max_side:
        return _to_b64(raw), mime, 1.0
    ratio = max_side / max(w, h)
    new_size = (max(1, round(w * ratio)), max(1, round(h * ratio)))
    img = img.resize(new_size, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    scale = w / max(1, new_size[0])
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/png", scale


# ---------------------------------------------------------------------------
# 系统提示词
# ---------------------------------------------------------------------------

_OCR_SYSTEM_PROMPT = (
    "你是验证码识别专家。用户会给你一张验证码图片，请识别其中的字符。"
    "只输出识别到的字符本身，不要任何额外文字、标点、Markdown、换行。"
    "区分大小写。如不确定，输出最可能的字符序列。"
)

_MATH_SYSTEM_PROMPT = (
    "你是数学验证码识别专家。用户会给你一张算式验证码图片（如 7*1=?）。"
    "请识别算式并计算结果，只输出最终答案的数字本身，"
    "不要任何额外文字、标点、Markdown、换行。"
)

_MATH_EXPR_RE = re.compile(r"^(\d+)\s*([+\-×xX*])\s*(\d+)$")
_MATH_OCR_CHARSET = "0123456789+-×xX*="
_MATH_DIGITS = "0123456789"


def _evaluate_math_expr(text: str) -> str:
    """解析 "a op b" 型算式并返回计算结果字符串；无法解析返回空串。

    支持 + - ×（含 x/X/* 的误识别变体）；不支持除法（验证码算式极少出现
    且结果非整数会引入舍入歧义）。
    """
    cleaned = text.strip().rstrip("=?＝？")
    m = _MATH_EXPR_RE.match(cleaned)
    if not m:
        return ""
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    if op == "+":
        return str(a + b)
    if op == "-":
        return str(a - b)
    return str(a * b)


_SLIDER_SYSTEM_PROMPT = (
    "你是滑块验证码识别专家。用户会给你一张滑块验证码背景图（含缺口）和"
    "一张滑块图（参考形状）。请识别滑块需要移动到的目标 x 坐标，"
    "即缺口左边界相对背景图左上角的像素偏移。"
    '只输出 JSON：{"x": <int>, "confidence": <0-1>}。不要任何额外文字。'
)

_CLICK_SYSTEM_PROMPT = (
    "你是点选验证码识别专家。用户会给你一张点选验证码图片和一段提示文字，"
    "请找出所有（或按提示顺序的）符合提示的元素，返回每个元素的**外接矩形**。"
    "两类题型都要支持：语序点选（按提示顺序逐个返回）与宫格/散点多选"
    "（返回所有符合提示的元素）。先定位元素再画外接框，框要贴住元素本身。"
    '只输出 JSON：{"boxes": [{"x": <int>, "y": <int>, "w": <int>, "h": <int>, '
    '"label": "..."}, ...]}。x/y 为外接框左上角，w/h 为宽高，'
    "以图片左上角为原点、单位像素。不要任何额外文字。"
    '（若无法给出外接框，也可返回 {"points": [...]} 中心点格式。）'
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
        # ddddocr 实例延迟加载并缓存，避免每次识别都重新加载 ONNX 模型
        self._ddddocr_instance: Any = None

    @property
    def llm_vision_available(self) -> bool:
        """是否可用 LLM vision 路径。"""
        if not self.config.use_llm or self.provider is None:
            return False
        caps = getattr(self.provider, "capabilities", None)
        return caps is not None and getattr(caps, "vision", False)

    # -- LLM 消息构造与后处理（sync/async 双路径共用，防镜像分叉） ------------

    def _ocr_message(self, image: bytes | str, mime: str) -> LLMMessage | None:
        """构造 OCR 视觉消息；图片预处理失败返回 None。"""
        prepared = _prepare_vision_image(image, mime)
        if prepared is None:
            return None
        b64, mime_out, _ = prepared
        return LLMMessage.vision(
            "user",
            "请识别这张验证码图片中的字符。",
            b64,
            mime=mime_out,
            detail=self.config.detail,
        )

    @staticmethod
    def _postprocess_ocr(content: str, max_length: int) -> str:
        """剥离空白并截断到允许长度。"""
        text = "".join(content.split())
        return text[:max_length]

    def _slider_message(
        self, bg: bytes | str, slider: bytes | str
    ) -> tuple[LLMMessage, float] | None:
        """构造滑块双图消息，附带背景图缩放系数；预处理失败返回 None。"""
        bg_prepared = _prepare_vision_image(bg)
        slider_prepared = _prepare_vision_image(slider)
        if bg_prepared is None or slider_prepared is None:
            return None
        bg_b64, _, bg_scale = bg_prepared
        slider_b64, _, _ = slider_prepared
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
        return msg, bg_scale

    @staticmethod
    def _scale_slider_x(sol: SliderSolution | None, bg_scale: float) -> SliderSolution | None:
        """背景图被降采样时，把 LLM 返回的 x 还原回原始像素坐标系。"""
        if sol is not None and bg_scale != 1.0:
            sol.x = round(sol.x * bg_scale)
        return sol

    def _click_message(self, image: bytes | str, prompt: str, mime: str) -> LLMMessage | None:
        """构造点选题视觉消息；图片预处理失败返回 None。"""
        prepared = _prepare_vision_image(image, mime)
        if prepared is None:
            return None
        b64, mime_out, _ = prepared
        return LLMMessage.vision(
            "user",
            f"提示：{prompt}\n请识别这张点选验证码图片中需要点击的元素，"
            "返回每个元素的外接矩形（语序点选按提示顺序排列）。",
            b64,
            mime=mime_out,
            detail=self.config.detail,
        )

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
        """尝试 ddddocr 本地识别；未安装或识别失败返回空。

        math_mode 下允许运算符字符，识别结果匹配算式模式时直接返回计算
        结果（2026-11-14 实测：数学验证码的校验方要答案而非算式原文，
        旧实现会把 "7*1" 过滤成 "1" 直接交出去）。
        """
        ocr = self._get_ddddocr()
        if ocr is None:
            return ""
        try:
            data = _b64_to_bytes(image)
            text = ocr.classification(data)
            allowed = set(self.config.ocr_charset)
            if self.config.math_mode:
                allowed |= set(_MATH_OCR_CHARSET)
            text = "".join(c for c in text if c in allowed)
            if self.config.math_mode:
                result = _evaluate_math_expr(text)
                if result:
                    return result
                # 算式无法解析 → 交 LLM 计算路径兜底
                return ""
            if 0 < len(text) <= self.config.ocr_max_length:
                return text
            return ""
        except Exception as exc:
            logger.warning("local OCR (ddddocr) failed: %s", exc)
            return ""

    def _get_ddddocr(self) -> Any:
        """延迟加载并缓存 ddddocr 实例（首次约 10-50ms 加载 ONNX 模型）。"""
        if self._ddddocr_instance is not None:
            return self._ddddocr_instance
        try:
            import ddddocr  # type: ignore[import-untyped]
        except ImportError:
            logger.debug("ddddocr not installed; local OCR unavailable")
            return None
        try:
            # show_ad=False 关闭作者广告输出
            self._ddddocr_instance = ddddocr.DdddOcr(show_ad=False)
        except Exception as exc:
            logger.warning("failed to initialize ddddocr: %s", exc)
            self._ddddocr_instance = None
        return self._ddddocr_instance

    def _llm_ocr(self, image: bytes | str, mime: str) -> str:
        assert self.provider is not None
        msg = self._ocr_message(image, mime)
        if msg is None:
            return ""
        system_prompt = _MATH_SYSTEM_PROMPT if self.config.math_mode else _OCR_SYSTEM_PROMPT
        resp = self.provider.chat(
            [LLMMessage("system", system_prompt), msg],
            temperature=self.config.temperature,
        )
        text = self._postprocess_ocr(resp.content or "", self.config.ocr_max_length)
        # math_mode 答案只可能是数字：LLM 偏差输出（标点/文字/注入内容）就地消毒。
        # 用显式 ASCII 数字而非 isdigit（后者会放过 ² 等上标字符）。
        return "".join(c for c in text if c in _MATH_DIGITS) if self.config.math_mode else text

    async def _llm_ocr_async(self, image: bytes | str, mime: str) -> str:
        assert self.provider is not None
        if not hasattr(self.provider, "achat"):
            return self._llm_ocr(image, mime)
        msg = self._ocr_message(image, mime)
        if msg is None:
            return ""
        system_prompt = _MATH_SYSTEM_PROMPT if self.config.math_mode else _OCR_SYSTEM_PROMPT
        resp = await self.provider.achat(
            [LLMMessage("system", system_prompt), msg],
            temperature=self.config.temperature,
        )
        text = self._postprocess_ocr(resp.content or "", self.config.ocr_max_length)
        return "".join(c for c in text if c in _MATH_DIGITS) if self.config.math_mode else text

    # ------------------------------------------------------------------
    # 滑块缺口定位
    # ------------------------------------------------------------------

    def solve_slider(
        self,
        bg: bytes | str,
        slider: bytes | str,
    ) -> SliderSolution | None:
        """识别滑块缺口 x 坐标。

        本地路径返回的 ``confidence`` 为掩码归一化互相关（ZNCC）得分，
        低于 ``config.slider_min_score`` 视为本地识别失败，交由 LLM vision
        兜底而不是返回一个可能错误的坐标。
        """
        if self.config.use_pillow_slider:
            matched = self._match_slider(bg, slider)
            if matched is not None:
                x, score = matched
                return SliderSolution(x=x, method="pillow", confidence=round(min(1.0, score), 3))
        if self.llm_vision_available:
            return self._llm_slider(bg, slider)
        return None

    async def solve_slider_async(
        self,
        bg: bytes | str,
        slider: bytes | str,
    ) -> SliderSolution | None:
        if self.config.use_pillow_slider:
            matched = self._match_slider(bg, slider)
            if matched is not None:
                x, score = matched
                return SliderSolution(x=x, method="pillow", confidence=round(min(1.0, score), 3))
        if self.llm_vision_available:
            return await self._llm_slider_async(bg, slider)
        return None

    def _local_slider(
        self,
        bg: bytes | str,
        slider: bytes | str,
    ) -> int | None:
        """本地滑块匹配的兼容包装：只返回 x（不带得分）。

        供旧调用方/测试使用；新代码请用 :meth:`_match_slider`（含置信分，
        供 solve_slider 做低分门控）。
        """
        matched = self._match_slider(bg, slider)
        return matched[0] if matched is not None else None

    def _match_slider(
        self,
        bg: bytes | str,
        slider: bytes | str,
    ) -> tuple[int, float] | None:
        """掩码 ZNCC 定位缺口，返回 ``(x, score)``；失败或低于门限返回 None。

        与旧 SSD 匹配的区别：

        - **掩码来源**：滑块件带透明通道时以 alpha 为权威掩码（调色板透明
          PNG 经 ``convert("L")`` 会把透明区按调色板 RGB 计入，污染掩码）；
          完全不透明的件退回"亮度 > 30"的旧规则。
        - **打分**：SSD 对"缺口被半透明黑罩变暗"的真实滑块验证码（SF、
          极验等）系统性失配——拼图件内容与变暗后的缺口内容绝对差极大，
          反而匹配到背景里亮度相近的其他纹理。ZNCC 对线性亮度缩放不变，
          恰好覆盖"缺口 = 原内容 × 系数"的渲染方式。
        - **低分门控**：背景与件毫无相关结构（score < ``slider_min_score``）
          时返回 None，由上层走 LLM 或交人工，而非给出自信的错误解。

        滑块件 PNG 与背景图等高（件内容的 y 偏移即缺口的 y 偏移），故只
        在水平方向滑窗；base64/图片解码失败一律返回 None。
        """
        try:
            bg_data = _b64_to_bytes(bg)
            slider_data = _b64_to_bytes(slider)
        except Exception as exc:
            logger.warning("failed to decode slider captcha image data: %s", exc)
            return None
        # 优先 numpy 加速路径
        try:
            import numpy as np  # type: ignore[import-untyped]
            from PIL import Image
        except ImportError:
            logger.debug("numpy not installed; falling back to pure-Pillow slider matching")
            return self._match_slider_pillow(bg_data, slider_data)

        try:
            bg_img = np.asarray(Image.open(io.BytesIO(bg_data)).convert("L"), dtype=np.float32)
            piece_img = Image.open(io.BytesIO(slider_data)).convert("RGBA")
        except Exception as exc:
            logger.warning("failed to decode slider/background images: %s", exc)
            return None

        piece_gray = np.asarray(piece_img.convert("L"), dtype=np.float32)
        alpha = np.asarray(piece_img.getchannel("A"), dtype=np.float32)
        bh, bw = bg_img.shape
        if bw * bh > _MATCH_MAX_BG_PIXELS or piece_gray.size > _MATCH_MAX_PIECE_PIXELS:
            logger.warning(
                "slider images exceed resource cap (bg=%dx%d piece=%dx%d); rejecting",
                bw,
                bh,
                piece_gray.shape[1],
                piece_gray.shape[0],
            )
            return None

        if float(alpha.min()) < 128.0:
            mask = alpha > 128.0
        else:
            mask = piece_gray > 30.0
        mask_count = int(mask.sum())
        if mask_count < 16:
            return None

        # 极验等控件把拼图件画在与背景同宽的全幅 canvas 上；裁剪到 blob
        # bbox 再滑窗，否则 sw==bw 时滑窗范围为空、必然无解。返回的 x
        # 折算回原始 png 左缘坐标系（widget 移动的是整幅 canvas）。
        ys, xs = np.where(mask)
        bx0, bx1 = int(xs.min()), int(xs.max())
        by0, by1 = int(ys.min()), int(ys.max())
        piece_gray_c = piece_gray[by0 : by1 + 1, bx0 : bx1 + 1]
        mask_c = mask[by0 : by1 + 1, bx0 : bx1 + 1]
        ch, cw = mask_c.shape
        if cw > bw or ch > bh:
            return None

        piece_vals = piece_gray_c[mask_c]
        piece_center = piece_vals - piece_vals.mean()
        piece_denom = float(np.sqrt((piece_center * piece_center).sum()))
        if piece_denom <= 1e-6:
            # 均匀亮度件无位置信号，ZNCC 无法排序候选位置
            return None

        best_x = -1
        best_score = 0.0
        # 步长 1；从 cw 开始（缺口不会在滑块起始位置覆盖区内）。
        # 行带取 bg 的 [by0..by1]：件 canvas 与背景同高叠放，blob 的 y 偏移
        # 即缺口的 y 偏移，裁剪后必须保持该行带对齐。
        for x in range(cw, bw - cw + 1):
            region_vals = bg_img[by0 : by0 + ch, x : x + cw][mask_c]
            region_center = region_vals - region_vals.mean()
            denom = piece_denom * float(np.sqrt((region_center * region_center).sum()))
            score = float((piece_center * region_center).sum()) / denom if denom > 1e-6 else 0.0
            if score > best_score:
                best_score = score
                best_x = x
        if best_x < 0 or best_score < self.config.slider_min_score:
            # ZNCC 无信号（平黑洞/抗模板填充等家族）→ 二级剪影异常探测器
            return self._detect_silhouette_anomaly(bg_img, mask_c, by0, cw, ch, bx0)
        return best_x - bx0, min(1.0, best_score)

    @staticmethod
    def _dilate_bool(mask: np.ndarray, radius: int) -> np.ndarray:
        """布尔掩码按 4 邻域迭代膨胀 radius 轮。"""
        out = mask.copy()
        for _ in range(radius):
            n = out.copy()
            n[1:, :] |= out[:-1, :]
            n[:-1, :] |= out[1:, :]
            n[:, 1:] |= out[:, :-1]
            n[:, :-1] |= out[:, 1:]
            out = n
        return out

    def _detect_silhouette_anomaly(
        self,
        bg_img: np.ndarray,
        mask_c: np.ndarray,
        by0: int,
        cw: int,
        ch: int,
        bx0: int,
    ) -> tuple[int, float] | None:
        """二级滑块定位：剪影填充异常（ZNCC 无信号时的兜底，2026-11-14）。

        针对"缺口 = 平色/提亮/变暗填充、内容结构被破坏"的家族（易盾半透明
        提亮、极验平黑洞、SF captcha-D 平黑）：件内容与缺口内容相关性弱，
        ZNCC 无峰，但缺口的**剪影**仍在——blob 足印内的填充亮度与足印外
        环带亮度存在显著台阶。对每个候选 x 计算异常分：

            diff     = ring_mean - fp_mean     正=暗洞，负=亮洞
            flatness = 1 - min(1, fp_std/(ring_std+6))   填充平坦度先验
            score    = |diff| * (0.6 + 0.8*flatness)

        三重门限（score/|diff|/峰值显著性，阈值由 8 对真图标定：yidun
        score 57.3、geetest 58.8 全过；ZNCC 通过家族 26~45.2 全拒）全过
        才接受，否则返回 None 交 LLM/人工。返回 x 折算回原始 png 左缘。
        """
        import numpy as np  # type: ignore[import-untyped]

        bw = bg_img.shape[1]
        scores: dict[int, float] = {}
        diffs: dict[int, float] = {}
        # 环带窗口外扩 M px：矩形 blob 贴满裁剪框时框内无环带，必须从背景
        # 借两侧余量；贴近图像边缘（<M px）的候选位置直接跳过。
        M = 8
        Ww = cw + 2 * M
        pad = np.zeros((ch, Ww), dtype=bool)
        pad[:, M : M + cw] = mask_c
        fp_w = pad
        ring_w = self._dilate_bool(pad, 6) & ~self._dilate_bool(pad, 2)
        for x in range(cw, bw - cw + 1):
            xa = x - M
            if xa < 0 or xa + Ww > bw:
                continue
            region = bg_img[by0 : by0 + ch, xa : xa + Ww]
            fp = region[fp_w]
            rg = region[ring_w]
            if fp.size < 8 or rg.size < 8:
                continue
            diff = float(rg.mean() - fp.mean())
            fp_std = float(fp.std())
            rg_std = float(rg.std())
            flat = 1.0 - min(1.0, fp_std / (rg_std + 6.0))
            scores[x] = abs(diff) * (0.6 + 0.8 * flat)
            diffs[x] = diff
        if not scores:
            return None
        best_x = max(scores, key=lambda k: scores[k])
        best_score = scores[best_x]
        best_diff = abs(diffs[best_x])
        # 峰值显著性：排除半径取一个洞宽（cw+8）——平洞是一条与件等宽的
        # 高原，±8 邻域排除会把同洞高原误判成"次峰"压低显著性；两个独立
        # 的洞至少相隔一个洞宽。
        salience = best_score
        for x, s in scores.items():
            if abs(x - best_x) > cw + 8 and best_score - s < salience:
                salience = best_score - s
        if (
            best_score < _SILHOUETTE_MIN_SCORE
            or best_diff < _SILHOUETTE_MIN_DIFF
            or salience < _SILHOUETTE_MIN_SALIENCE
        ):
            logger.debug(
                "silhouette anomaly rejected: score=%.1f diff=%.1f salience=%.1f",
                best_score,
                best_diff,
                salience,
            )
            return None
        return best_x - bx0, min(0.7, best_score / 100.0)

    def _match_slider_pillow(
        self,
        bg_data: bytes,
        slider_data: bytes,
    ) -> tuple[int, float] | None:
        """无 numpy 时的纯 Pillow 掩码 ZNCC 匹配（子采样加速）。"""
        try:
            from PIL import Image
        except ImportError:
            logger.debug("Pillow not installed; slider matching unavailable")
            return None
        try:
            bg_img = Image.open(io.BytesIO(bg_data)).convert("L")
            piece_img = Image.open(io.BytesIO(slider_data)).convert("RGBA")
        except Exception as exc:
            logger.warning("failed to decode slider/background images: %s", exc)
            return None

        bw, bh = bg_img.size
        sw, sh = piece_img.size
        if (
            sw > bw
            or sh > bh
            or bw * bh > _MATCH_MAX_BG_PIXELS
            or sw * sh > _MATCH_MAX_PIECE_PIXELS
        ):
            return None

        alpha_flat = list(piece_img.getchannel("A").tobytes())
        lum_flat = list(piece_img.convert("L").tobytes())
        if min(alpha_flat) < 128:
            mask_flat = [a > 128 for a in alpha_flat]
        else:
            mask_flat = [v > 30 for v in lum_flat]

        # 极验等控件把拼图件画在与背景同宽的全幅 canvas 上；裁剪到 blob
        # bbox 再采样滑窗（与 numpy 路径一致），返回 x 折算回原始 png 左缘。
        mask_cols = [i % sw for i, m in enumerate(mask_flat) if m]
        mask_rows = [i // sw for i, m in enumerate(mask_flat) if m]
        if not mask_cols:
            return None
        bx0, bx1 = min(mask_cols), max(mask_cols)
        by0, by1 = min(mask_rows), max(mask_rows)
        cw, ch = bx1 - bx0 + 1, by1 - by0 + 1
        if cw > bw or ch > bh:
            return None

        # 件内 x/y 步长 2 子采样（外层扫描 x 步长 1）；采样位置固定，件与区域
        # 逐点对齐（行带取 bg 的 [by0..by1]，件 canvas 与背景同高叠放）。
        # 信号量门限按全掩码像素数（与 numpy 路径一致）；子采样命中数仅防
        # 退化（掩码像素极少且恰好全落在偶数网格外时退回 None）。
        samples = [
            (sx, sy)
            for sy in range(by0, by1 + 1, 2)
            for sx in range(bx0, bx1 + 1, 2)
            if mask_flat[sy * sw + sx]
        ]
        if sum(mask_flat) < 16 or len(samples) < 4:
            return None
        piece_vals = [float(lum_flat[sy * sw + sx]) for sx, sy in samples]
        piece_mean = sum(piece_vals) / len(piece_vals)
        piece_center = [v - piece_mean for v in piece_vals]
        piece_denom = sum(v * v for v in piece_center) ** 0.5
        if piece_denom <= 1e-6:
            return None

        bg_px = bg_img.load()
        best_x = -1
        best_score = 0.0
        for x in range(cw, bw - cw + 1):
            region_vals = [float(bg_px[x + sx - bx0, sy]) for sx, sy in samples]  # type: ignore[index,arg-type]
            region_mean = sum(region_vals) / len(region_vals)
            region_center = [v - region_mean for v in region_vals]
            denom = piece_denom * (sum(v * v for v in region_center) ** 0.5)
            score = (
                sum(a * b for a, b in zip(piece_center, region_center, strict=True)) / denom
                if denom > 1e-6
                else 0.0
            )
            if score > best_score:
                best_score = score
                best_x = x
        if best_x < 0 or best_score < self.config.slider_min_score:
            return None
        return best_x - bx0, min(1.0, best_score)

    def _pillow_only_slider(
        self,
        bg_data: bytes,
        slider_data: bytes,
    ) -> int | None:
        """无 numpy 时的纯 Pillow 滑块匹配兼容包装（只返回 x）。"""
        matched = self._match_slider_pillow(bg_data, slider_data)
        return matched[0] if matched is not None else None

    def _llm_slider(
        self,
        bg: bytes | str,
        slider: bytes | str,
    ) -> SliderSolution | None:
        assert self.provider is not None
        built = self._slider_message(bg, slider)
        if built is None:
            return None
        msg, bg_scale = built
        resp = self.provider.chat(
            [LLMMessage("system", _SLIDER_SYSTEM_PROMPT), msg],
            temperature=self.config.temperature,
        )
        sol = self._scale_slider_x(self._parse_slider_response(resp.content or ""), bg_scale)
        return self._reject_out_of_bounds(sol, bg)

    async def _llm_slider_async(
        self,
        bg: bytes | str,
        slider: bytes | str,
    ) -> SliderSolution | None:
        assert self.provider is not None
        if not hasattr(self.provider, "achat"):
            return self._llm_slider(bg, slider)
        built = self._slider_message(bg, slider)
        if built is None:
            return None
        msg, bg_scale = built
        resp = await self.provider.achat(
            [LLMMessage("system", _SLIDER_SYSTEM_PROMPT), msg],
            temperature=self.config.temperature,
        )
        sol = self._scale_slider_x(self._parse_slider_response(resp.content or ""), bg_scale)
        return self._reject_out_of_bounds(sol, bg)

    def _reject_out_of_bounds(
        self,
        sol: SliderSolution | None,
        bg: bytes | str,
    ) -> SliderSolution | None:
        """LLM 幻觉防线：x 越出背景图宽度时视为识别失败。

        2026-11-14 易盾真图实测：LLM 曾对 320px 宽的背景回出 x=340（越界
        161px），旧实现只拦负值，越界值会直接穿透给调用方。
        """
        if sol is None:
            return None
        try:
            from PIL import Image

            w = Image.open(io.BytesIO(_b64_to_bytes(bg))).size[0]
        except Exception as exc:  # 图片无法解码时无从校验，放行（由调用方兜底）
            logger.debug("skip slider x bounds check, bg undecodable: %s", exc)
            return sol
        if 0 <= sol.x < w:
            return sol
        logger.warning(
            "LLM slider x out of bounds: %s not in [0, %s); treating as failure",
            sol.x,
            w,
        )
        return None

    @staticmethod
    def _parse_slider_response(text: str) -> SliderSolution | None:
        parsed = _extract_json(text)
        try:
            x = int(parsed.get("x", -1))
        except (TypeError, ValueError) as exc:
            logger.warning("failed to parse slider x from LLM response: %s", exc)
            return None
        if x < 0:
            return None
        try:
            conf = float(parsed.get("confidence", 0.5))
        except (TypeError, ValueError) as exc:
            logger.debug("invalid confidence in slider response, using default: %s", exc)
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
        msg = self._click_message(image, prompt, mime)
        if msg is None:
            return None
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
        msg = self._click_message(image, prompt, mime)
        if msg is None:
            return None
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
        """解析点选响应。优先外接框协议（中心点=点击坐标），兼容旧 points 格式。

        2026-11-14 hCaptcha 真图实测：真实照片散点上"裸中心点"误差可达
        ±75px 甚至落在错误元素上，而外接框 3/3 全中——框定位比点定位稳定。
        """
        parsed = _extract_json(text)
        if not isinstance(parsed, dict):
            return None
        boxes_raw = parsed.get("boxes")
        if isinstance(boxes_raw, list) and boxes_raw:
            points: list[tuple[int, int]] = []
            labels: list[str] = []
            for b in boxes_raw:
                if not isinstance(b, dict):
                    continue
                try:
                    x = int(b.get("x", -1))
                    y = int(b.get("y", -1))
                    w = int(b.get("w", 0))
                    h = int(b.get("h", 0))
                except (TypeError, ValueError) as exc:
                    logger.debug("skipping invalid click box in LLM response: %s", exc)
                    continue
                if x < 0 or y < 0 or w <= 0 or h <= 0:
                    continue
                points.append((x + w // 2, y + h // 2))
                labels.append(str(b.get("label", "")))
            if points:
                return ClickSolution(points=points, labels=labels, method="llm")
            return None
        points_raw = parsed.get("points")
        if not isinstance(points_raw, list):
            return None
        points = []
        labels = []
        for p in points_raw:
            if not isinstance(p, dict):
                continue
            try:
                x = int(p.get("x", -1))
                y = int(p.get("y", -1))
            except (TypeError, ValueError) as exc:
                logger.debug("skipping invalid click point in LLM response: %s", exc)
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

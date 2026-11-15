"""图片验证码破解模块测试（mock provider，不发起真实 LLM 调用）。"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from web_crawler.ai.image_captcha import (
    ClickSolution,
    ImageCaptchaSolver,
    ImageSolverConfig,
    SliderSolution,
    _b64_to_bytes,
    _extract_json,
    _prepare_vision_image,
    _to_b64,
)
from web_crawler.ai.llm import LLMResponse, ProviderCapabilities

# ---------------------------------------------------------------------------
# 测试用 mock provider
# ---------------------------------------------------------------------------


class _FakeVisionProvider:
    """模拟支持 vision 的 LLM Provider。"""

    name = "fake-vision"
    model = "fake-vision-model"
    capabilities = ProviderCapabilities(
        vision=True,
        json_mode=True,
        tools=False,
        streaming=False,
    )

    def __init__(
        self,
        *,
        chat_response: str = "",
        achat_response: str | None = None,
    ) -> None:
        self._chat_response = chat_response
        self._achat_response = achat_response if achat_response is not None else chat_response
        self.chat_calls: list[tuple[Any, dict[str, Any]]] = []
        self.achat_calls: list[tuple[Any, dict[str, Any]]] = []

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.chat_calls.append((messages, kwargs))
        return LLMResponse(content=self._chat_response, model=self.model)

    async def achat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.achat_calls.append((messages, kwargs))
        return LLMResponse(content=self._achat_response, model=self.model)


class _FakeNoVisionProvider:
    """模拟不支持 vision 的 LLM Provider。"""

    name = "fake-text"
    model = "fake-text-model"
    capabilities = ProviderCapabilities(vision=False)

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="", model=self.model)


# ---------------------------------------------------------------------------
# ImageSolverConfig / dataclass 默认值
# ---------------------------------------------------------------------------


def test_image_solver_config_defaults() -> None:
    """ImageSolverConfig 默认开启 LLM 与本地 OCR。"""
    cfg = ImageSolverConfig()
    assert cfg.use_llm is True
    assert cfg.use_local_ocr is True
    assert cfg.use_pillow_slider is True
    assert cfg.detail == "high"
    assert cfg.temperature == 0.0
    assert cfg.ocr_max_length == 8
    assert cfg.slider_min_score == pytest.approx(0.45)


def test_slider_solution_defaults() -> None:
    """SliderSolution 默认 y 为 0。"""
    s = SliderSolution(x=120)
    assert s.x == 120
    assert s.y == 0
    assert s.method == ""
    assert s.confidence == 0.0


def test_click_solution_defaults() -> None:
    """ClickSolution 默认点位与标签为空。"""
    c = ClickSolution()
    assert c.points == []
    assert c.labels == []
    assert c.method == ""


# ---------------------------------------------------------------------------
# _to_b64 / _b64_to_bytes
# ---------------------------------------------------------------------------


def test_to_b64_from_bytes() -> None:
    """_to_b64 将原始字节编码为 base64。"""
    raw = b"hello"
    assert _to_b64(raw) == base64.b64encode(b"hello").decode("ascii")


def test_to_b64_from_plain_string() -> None:
    """_to_b64 对已编码字符串原样返回。"""
    b64_str = base64.b64encode(b"abc").decode("ascii")
    assert _to_b64(b64_str) == b64_str


def test_to_b64_from_data_uri() -> None:
    """_to_b64 从 data URI 提取 base64 部分。"""
    b64_str = base64.b64encode(b"abc").decode("ascii")
    data_uri = f"data:image/png;base64,{b64_str}"
    assert _to_b64(data_uri) == b64_str


def test_b64_to_bytes_roundtrip() -> None:
    """_b64_to_bytes 将 base64 还原为原始字节。"""
    raw = b"binary data"
    encoded = base64.b64encode(raw).decode("ascii")
    assert _b64_to_bytes(encoded) == raw


def test_b64_to_bytes_from_data_uri() -> None:
    """_b64_to_bytes 从 data URI 还原为原始字节。"""
    raw = b"binary"
    encoded = base64.b64encode(raw).decode("ascii")
    assert _b64_to_bytes(f"data:image/jpeg;base64,{encoded}") == raw


def test_b64_to_bytes_passthrough_bytes() -> None:
    """_b64_to_bytes 对字节输入原样返回。"""
    raw = b"already bytes"
    assert _b64_to_bytes(raw) == raw


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


def test_extract_json_pure_object() -> None:
    """_extract_json 解析纯 JSON 对象。"""
    text = '{"x": 100, "confidence": 0.9}'
    parsed = _extract_json(text)
    assert parsed == {"x": 100, "confidence": 0.9}


def test_extract_json_with_code_fence() -> None:
    """_extract_json 解析代码围栏包裹的 JSON。"""
    text = '```json\n{"x": 50, "confidence": 0.7}\n```'
    parsed = _extract_json(text)
    assert parsed == {"x": 50, "confidence": 0.7}


def test_extract_json_with_surrounding_text() -> None:
    """_extract_json 从周围文本中提取 JSON。"""
    text = '识别结果：{"points": [{"x": 1, "y": 2}]} 完成'
    parsed = _extract_json(text)
    assert parsed == {"points": [{"x": 1, "y": 2}]}


def test_extract_json_invalid_returns_empty() -> None:
    """_extract_json 对非法文本返回空字典。"""
    assert _extract_json("not json at all") == {}


def test_extract_json_empty_string() -> None:
    """_extract_json 对空串返回空字典。"""
    assert _extract_json("") == {}


def test_extract_json_skips_escaped_chars_inside_string() -> None:
    """括号配平应跳过字符串内的转义反斜杠与转义引号。"""
    # "a\"}" 中的 \" 与 } 都在字符串内，不参与配平；最外层 } 才闭合对象
    data = _extract_json('{"k": "a\\"}"} tail')
    assert data == {"k": 'a"}'}


def test_extract_json_balanced_scan_unterminated_returns_empty() -> None:
    """首个 { 无法配平（字符串未闭合吞掉 }）时应返回空 dict。"""
    # "b} 未闭合：} 在字符串内被跳过，扫描到底仍不配平 → 返回 {}
    assert _extract_json('{"a": "b}') == {}


# ---------------------------------------------------------------------------
# ImageCaptchaSolver - llm_vision_available
# ---------------------------------------------------------------------------


def test_llm_vision_available_none_provider() -> None:
    """无 provider 时 LLM 视觉不可用。"""
    solver = ImageCaptchaSolver(provider=None)
    assert solver.llm_vision_available is False


def test_llm_vision_available_no_vision_provider() -> None:
    """provider 无视觉能力时 LLM 视觉不可用。"""
    solver = ImageCaptchaSolver(provider=_FakeNoVisionProvider())  # type: ignore[arg-type]
    assert solver.llm_vision_available is False


def test_llm_vision_available_vision_provider() -> None:
    """provider 有视觉能力时 LLM 视觉可用。"""
    solver = ImageCaptchaSolver(provider=_FakeVisionProvider())  # type: ignore[arg-type]
    assert solver.llm_vision_available is True


def test_llm_vision_available_disabled_in_config() -> None:
    """配置禁用 LLM 时视觉不可用。"""
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=_FakeVisionProvider(), config=cfg)  # type: ignore[arg-type]
    assert solver.llm_vision_available is False


# ---------------------------------------------------------------------------
# OCR 文本识别
# ---------------------------------------------------------------------------


def test_solve_text_no_provider_no_local_returns_empty() -> None:
    # 默认环境没装 ddddocr，本地 OCR 返回空；无 provider 也无 LLM 路径
    """solve_text 无 provider 与本地 OCR 时返回空。"""
    cfg = ImageSolverConfig(use_pillow_slider=False)  # 不影响 OCR
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    assert solver.solve_text(b"fake-png-bytes") == ""


def test_solve_text_local_ocr_returns_text() -> None:
    # mock ddddocr 模块，让 _local_ocr 返回 "abc123"
    """solve_text 本地 OCR 返回识别文本。"""
    fake_ddddocr = MagicMock()
    fake_instance = MagicMock()
    fake_instance.classification.return_value = "abc123"
    fake_ddddocr.DdddOcr.return_value = fake_instance

    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    with patch.dict("sys.modules", {"ddddocr": fake_ddddocr}):
        result = solver.solve_text(b"fake-png-bytes")
    assert result == "abc123"
    fake_instance.classification.assert_called_once()


def test_solve_text_local_ocr_filters_invalid_chars() -> None:
    """solve_text 本地 OCR 过滤非法字符。"""
    fake_ddddocr = MagicMock()
    fake_instance = MagicMock()
    # 含特殊字符，应被过滤
    fake_instance.classification.return_value = "a!b@c#1"
    fake_ddddocr.DdddOcr.return_value = fake_instance

    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    with patch.dict("sys.modules", {"ddddocr": fake_ddddocr}):
        result = solver.solve_text(b"fake-png-bytes")
    assert result == "abc1"


def test_solve_text_llm_path() -> None:
    """solve_text 走 LLM 路径返回识别文本。"""
    provider = _FakeVisionProvider(chat_response="XYZ789")
    cfg = ImageSolverConfig(use_local_ocr=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    result = solver.solve_text(b"fake-png-bytes")
    assert result == "XYZ789"
    assert len(provider.chat_calls) == 1


def test_solve_text_llm_truncates_to_max_length() -> None:
    """solve_text 将 LLM 结果截断到最大长度。"""
    provider = _FakeVisionProvider(chat_response="abcdefghijklmnop" * 5)
    cfg = ImageSolverConfig(use_local_ocr=False, ocr_max_length=6)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    result = solver.solve_text(b"fake-png-bytes")
    assert len(result) == 6


def test_solve_text_local_then_llm_fallback() -> None:
    # ddddocr 返回空（识别失败），降级到 LLM
    """solve_text 本地 OCR 空时回退到 LLM。"""
    fake_ddddocr = MagicMock()
    fake_instance = MagicMock()
    fake_instance.classification.return_value = ""
    fake_ddddocr.DdddOcr.return_value = fake_instance

    provider = _FakeVisionProvider(chat_response="LLM123")
    solver = ImageCaptchaSolver(provider=provider)  # type: ignore[arg-type]
    with patch.dict("sys.modules", {"ddddocr": fake_ddddocr}):
        result = solver.solve_text(b"fake-png-bytes")
    assert result == "LLM123"


def test_solve_text_async_llm_path() -> None:
    """solve_text 异步走 LLM 路径返回识别文本。"""
    provider = _FakeVisionProvider(achat_response="ASY456")
    cfg = ImageSolverConfig(use_local_ocr=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    result = asyncio.run(solver.solve_text_async(b"fake-png-bytes"))
    assert result == "ASY456"
    assert len(provider.achat_calls) == 1


def test_solve_text_async_falls_back_to_sync_when_no_achat() -> None:
    """solve_text 异步无 achat 时回退到同步。"""

    @dataclass
    class _SyncOnlyProvider:
        name: str = "sync-only"
        model: str = "sync-model"
        capabilities: ProviderCapabilities = field(
            default_factory=lambda: ProviderCapabilities(vision=True)
        )

        def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
            return LLMResponse(content="SYNC789", model=self.model)

    provider = _SyncOnlyProvider()
    cfg = ImageSolverConfig(use_local_ocr=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    result = asyncio.run(solver.solve_text_async(b"fake-png-bytes"))
    assert result == "SYNC789"


# ---------------------------------------------------------------------------
# 滑块缺口定位
# ---------------------------------------------------------------------------


def test_solve_slider_no_provider_no_pillow_returns_none() -> None:
    # 关闭 Pillow 路径 + 无 provider → 返回 None
    """solve_slider 无 provider 与 pillow 时返回 None。"""
    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    assert solver.solve_slider(b"bg", b"slider") is None


def test_solve_slider_llm_path() -> None:
    """solve_slider 走 LLM 路径返回偏移解。"""
    provider = _FakeVisionProvider(chat_response='{"x": 180, "confidence": 0.85}')
    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    result = solver.solve_slider(b"bg", b"slider")
    assert result is not None
    assert result.x == 180
    assert result.method == "llm"
    assert result.confidence == pytest.approx(0.85)


def test_solve_slider_llm_invalid_json_returns_none() -> None:
    """solve_slider 对 LLM 非法 JSON 返回 None。"""
    provider = _FakeVisionProvider(chat_response="not json")
    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    assert solver.solve_slider(b"bg", b"slider") is None


def test_solve_slider_llm_negative_x_returns_none() -> None:
    """solve_slider 对 LLM 负 x 返回 None。"""
    provider = _FakeVisionProvider(chat_response='{"x": -1}')
    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    assert solver.solve_slider(b"bg", b"slider") is None


def test_solve_slider_async_llm_path() -> None:
    """solve_slider 异步走 LLM 路径返回偏移解。"""
    provider = _FakeVisionProvider(achat_response='{"x": 220, "confidence": 0.6}')
    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    result = asyncio.run(solver.solve_slider_async(b"bg", b"slider"))
    assert result is not None
    assert result.x == 220


def test_parse_slider_response_with_confidence() -> None:
    """_parse_slider_response 解析含置信度的响应。"""
    sol = ImageCaptchaSolver._parse_slider_response('{"x": 100, "confidence": 0.9}')
    assert sol is not None
    assert sol.x == 100
    assert sol.confidence == pytest.approx(0.9)


def test_parse_slider_response_missing_confidence_defaults() -> None:
    """_parse_slider_response 缺置信度时用默认值。"""
    sol = ImageCaptchaSolver._parse_slider_response('{"x": 50}')
    assert sol is not None
    assert sol.x == 50
    assert sol.confidence == pytest.approx(0.5)


def test_parse_slider_response_invalid_returns_none() -> None:
    """_parse_slider_response 对非法响应返回 None。"""
    assert ImageCaptchaSolver._parse_slider_response("garbage") is None
    assert ImageCaptchaSolver._parse_slider_response('{"x": "abc"}') is None


# ---------------------------------------------------------------------------
# 点选坐标识别
# ---------------------------------------------------------------------------


def test_solve_click_no_vision_returns_none() -> None:
    """solve_click 无视觉能力时返回 None。"""
    solver = ImageCaptchaSolver(provider=None)
    assert solver.solve_click(b"img", "请点击") is None


def test_solve_click_llm_path() -> None:
    """solve_click 走 LLM 路径返回点击解。"""
    provider = _FakeVisionProvider(
        chat_response='{"points": [{"x": 10, "y": 20, "label": "A"}, {"x": 30, "y": 40}]}'
    )
    solver = ImageCaptchaSolver(provider=provider)  # type: ignore[arg-type]
    result = solver.solve_click(b"img", "请按顺序点击")
    assert result is not None
    assert result.points == [(10, 20), (30, 40)]
    assert result.labels == ["A", ""]
    assert result.method == "llm"


def test_solve_click_bbox_protocol_returns_centers() -> None:
    """回归（2026-11-14 hCaptcha 实测）：外接框协议中心点=点击坐标。

    真实照片散点上"裸中心点"误差可达 ±75px 甚至落在错误元素上，外接框
    3/3 全中——点选 LLM 路径改用 boxes 协议，points 旧格式保持兼容。
    """
    provider = _FakeVisionProvider(
        chat_response='{"boxes": [{"x": 116, "y": 190, "w": 82, "h": 84, "label": "玻璃水壶"},'
        '{"x": 331, "y": 196, "w": 70, "h": 80, "label": "马克杯"}]}'
    )
    solver = ImageCaptchaSolver(provider=provider)  # type: ignore[arg-type]
    result = solver.solve_click(b"img", "找出可以热喝或冷喝的饮料")
    assert result is not None
    assert result.points == [(157, 232), (366, 236)]
    assert result.labels == ["玻璃水壶", "马克杯"]
    assert result.method == "llm"


def test_parse_click_response_skips_invalid_boxes() -> None:
    """boxes 里 w/h 非正的元素被跳过。"""
    sol = ImageCaptchaSolver._parse_click_response(
        '{"boxes": [{"x": 10, "y": 10, "w": 0, "h": 5}, {"x": 20, "y": 20, "w": 30, "h": 40}]}'
    )
    assert sol is not None
    assert sol.points == [(35, 40)]


def test_solve_click_empty_points_returns_none() -> None:
    """solve_click 对空点位返回 None。"""
    provider = _FakeVisionProvider(chat_response='{"points": []}')
    solver = ImageCaptchaSolver(provider=provider)  # type: ignore[arg-type]
    assert solver.solve_click(b"img", "请点击") is None


def test_solve_click_invalid_json_returns_none() -> None:
    """solve_click 对 LLM 非法 JSON 返回 None。"""
    provider = _FakeVisionProvider(chat_response="no json")
    solver = ImageCaptchaSolver(provider=provider)  # type: ignore[arg-type]
    assert solver.solve_click(b"img", "请点击") is None


def test_solve_click_filters_negative_coords() -> None:
    """solve_click 过滤负坐标点位。"""
    provider = _FakeVisionProvider(
        chat_response='{"points": [{"x": -1, "y": 10}, {"x": 5, "y": 5}]}'
    )
    solver = ImageCaptchaSolver(provider=provider)  # type: ignore[arg-type]
    result = solver.solve_click(b"img", "请点击")
    assert result is not None
    assert result.points == [(5, 5)]


def test_solve_click_async_llm_path() -> None:
    """solve_click 异步走 LLM 路径返回点击解。"""
    provider = _FakeVisionProvider(achat_response='{"points": [{"x": 100, "y": 200}]}')
    solver = ImageCaptchaSolver(provider=provider)  # type: ignore[arg-type]
    result = asyncio.run(solver.solve_click_async(b"img", "请点击"))
    assert result is not None
    assert result.points == [(100, 200)]


def test_solve_click_async_falls_back_to_sync() -> None:
    """solve_click 异步无 achat 时回退到同步。"""

    @dataclass
    class _SyncOnlyProvider:
        name: str = "sync-only"
        model: str = "sync-model"
        capabilities: ProviderCapabilities = field(
            default_factory=lambda: ProviderCapabilities(vision=True)
        )

        def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
            return LLMResponse(content='{"points": [{"x": 7, "y": 8}]}', model=self.model)

    provider = _SyncOnlyProvider()
    solver = ImageCaptchaSolver(provider=provider)  # type: ignore[arg-type]
    result = asyncio.run(solver.solve_click_async(b"img", "请点击"))
    assert result is not None
    assert result.points == [(7, 8)]


def test_parse_click_response_with_labels() -> None:
    """_parse_click_response 解析含标签的点位。"""
    text = '{"points": [{"x": 1, "y": 2, "label": "车"}, {"x": 3, "y": 4, "label": "狗"}]}'
    sol = ImageCaptchaSolver._parse_click_response(text)
    assert sol is not None
    assert sol.points == [(1, 2), (3, 4)]
    assert sol.labels == ["车", "狗"]


def test_parse_click_response_skips_non_dict_entries() -> None:
    """_parse_click_response 跳过非字典条目。"""
    text = '{"points": ["invalid", {"x": 5, "y": 6}, null]}'
    sol = ImageCaptchaSolver._parse_click_response(text)
    assert sol is not None
    assert sol.points == [(5, 6)]


def test_parse_click_response_invalid_returns_none() -> None:
    """_parse_click_response 对非法响应返回 None。"""
    assert ImageCaptchaSolver._parse_click_response("garbage") is None
    assert ImageCaptchaSolver._parse_click_response('{"wrong_key": []}') is None


# ---------------------------------------------------------------------------
# 本地滑块匹配（Pillow/numpy 路径）
# ---------------------------------------------------------------------------


def test_local_slider_returns_none_when_pillow_unavailable() -> None:
    """本地滑块在 pillow 不可用时返回 None。"""
    cfg = ImageSolverConfig(use_pillow_slider=True, use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    # 同时禁用 numpy 和 PIL 导入，模拟两者都没装
    with patch.dict("sys.modules", {"PIL": None, "numpy": None}):
        result = solver._local_slider(b"bg", b"slider")
    assert result is None


def test_pillow_only_slider_returns_none_when_pillow_unavailable() -> None:
    """仅 pillow 滑块在 pillow 不可用时返回 None。"""
    cfg = ImageSolverConfig(use_pillow_slider=True, use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    with patch.dict("sys.modules", {"PIL": None}):
        result = solver._pillow_only_slider(b"bg", b"slider")
    assert result is None


# ---------------------------------------------------------------------------
# 以下为补充测试：覆盖 ddddocr 异常、Pillow/numpy 路径、async 兜底等
# ---------------------------------------------------------------------------


def test_extract_json_fence_with_invalid_json_returns_empty() -> None:
    """fence 包裹的 JSON 非法时应回退到 _JSON_OBJ_RE 搜索并返回空 dict。"""
    text = "```json\n{not legal json}\n```"
    assert _extract_json(text) == {}


def test_extract_json_fence_without_lang_tag() -> None:
    """无语言标签的代码围栏也能解析。"""
    text = '```\n{"x": 7}\n```'
    parsed = _extract_json(text)
    assert parsed == {"x": 7}


def test_solve_text_async_local_ocr_returns_text() -> None:
    """solve_text_async 走本地 OCR 路径返回结果。"""
    fake_ddddocr = MagicMock()
    fake_instance = MagicMock()
    fake_instance.classification.return_value = "abc99"
    fake_ddddocr.DdddOcr.return_value = fake_instance

    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    with patch.dict("sys.modules", {"ddddocr": fake_ddddocr}):
        result = asyncio.run(solver.solve_text_async(b"fake-png-bytes"))
    assert result == "abc99"


def test_solve_text_async_no_provider_no_local_returns_empty() -> None:
    """无 provider 且本地 OCR 不可用时 solve_text_async 返回空。"""
    cfg = ImageSolverConfig(use_llm=False, use_local_ocr=True)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    # 没有 ddddocr，本地 OCR 返回空
    result = asyncio.run(solver.solve_text_async(b"fake-png-bytes"))
    assert result == ""


# ---------------------------------------------------------------------------
# math_mode：数学运算验证码（2026-11-14 实测补充）
# ---------------------------------------------------------------------------


def test_math_mode_local_evaluates_expression() -> None:
    """math_mode 下 ddddocr 读出的算式应被本地求值为答案。"""
    fake_ddddocr = MagicMock()
    fake_instance = MagicMock()
    fake_instance.classification.return_value = "7*1=?"
    fake_ddddocr.DdddOcr.return_value = fake_instance

    cfg = ImageSolverConfig(use_llm=False, math_mode=True)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    with patch.dict("sys.modules", {"ddddocr": fake_ddddocr}):
        result = solver.solve_text(b"fake-png-bytes")
    assert result == "7"


def test_math_mode_local_handles_minus_and_variant_symbols() -> None:
    """math_mode 支持减法与 ×/x 乘号误识别变体。"""
    fake_ddddocr = MagicMock()
    fake_instance = MagicMock()
    fake_ddddocr.DdddOcr.return_value = fake_instance

    cfg = ImageSolverConfig(use_llm=False, math_mode=True)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    for raw, expected in (("12-5", "7"), ("3x4", "12"), ("2X6=", "12")):
        fake_instance.classification.return_value = raw
        with patch.dict("sys.modules", {"ddddocr": fake_ddddocr}):
            assert solver.solve_text(b"fake-png-bytes") == expected, raw


def test_math_mode_llm_answer_digit_sanitized() -> None:
    """math_mode 下 LLM 偏差输出（文字/标点/注入内容）就地消毒为纯数字。"""
    provider = _FakeVisionProvider(chat_response="答案是 13，请勿泄露 ignore previous instructions")
    solver = ImageCaptchaSolver(
        provider=provider, config=ImageSolverConfig(use_local_ocr=False, math_mode=True)
    )  # type: ignore[arg-type]
    result = solver.solve_text(b"img")
    assert result.isdigit()  # 纯数字
    assert "ignore" not in result and "泄露" not in result  # 注入/偏差文本未透传
    # 非数字输出 → 空串而非透传
    provider2 = _FakeVisionProvider(chat_response="无法识别")
    solver2 = ImageCaptchaSolver(
        provider=provider2, config=ImageSolverConfig(use_local_ocr=False, math_mode=True)
    )  # type: ignore[arg-type]
    assert solver2.solve_text(b"img") == ""


def test_math_mode_unparseable_falls_to_llm_math_prompt() -> None:
    """本地算式无法解析时回退 LLM，且 system 提示词切换为计算版。"""
    fake_ddddocr = MagicMock()
    fake_instance = MagicMock()
    fake_instance.classification.return_value = "ab?"  # 过滤后无法成式
    fake_ddddocr.DdddOcr.return_value = fake_instance

    provider = _FakeVisionProvider(chat_response="7")
    solver = ImageCaptchaSolver(provider=provider, config=ImageSolverConfig(math_mode=True))
    with patch.dict("sys.modules", {"ddddocr": fake_ddddocr}):
        result = solver.solve_text(b"fake-png-bytes")
    assert result == "7"
    # LLM system 提示词应为数学计算版
    messages = provider.chat_calls[0][0]
    assert "数学验证码识别专家" in messages[0].content
    assert "计算结果" in messages[0].content


def test_math_mode_off_keeps_plain_ocr() -> None:
    """未开 math_mode 时默认行为不变（运算符被字符集过滤）。"""
    fake_ddddocr = MagicMock()
    fake_instance = MagicMock()
    fake_instance.classification.return_value = "7*1=?"
    fake_ddddocr.DdddOcr.return_value = fake_instance

    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    with patch.dict("sys.modules", {"ddddocr": fake_ddddocr}):
        result = solver.solve_text(b"fake-png-bytes")
    assert result == "71"


def test_local_ocr_returns_empty_on_exception() -> None:
    """_local_ocr 内部异常时应吞掉返回空字符串。"""
    fake_ddddocr = MagicMock()
    fake_instance = MagicMock()
    # classification 抛异常
    fake_instance.classification.side_effect = RuntimeError("model error")
    fake_ddddocr.DdddOcr.return_value = fake_instance

    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    with patch.dict("sys.modules", {"ddddocr": fake_ddddocr}):
        result = solver._local_ocr(b"fake-png-bytes")
    assert result == ""


def test_local_ocr_returns_empty_when_text_too_long() -> None:
    """OCR 识别结果超过 ocr_max_length 时返回空。"""
    fake_ddddocr = MagicMock()
    fake_instance = MagicMock()
    # 返回 10 个字符，超过默认 ocr_max_length=8
    fake_instance.classification.return_value = "abcdefghij"
    fake_ddddocr.DdddOcr.return_value = fake_instance

    cfg = ImageSolverConfig(use_llm=False, ocr_max_length=8)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    with patch.dict("sys.modules", {"ddddocr": fake_ddddocr}):
        result = solver._local_ocr(b"fake-png-bytes")
    assert result == ""


def test_get_ddddocr_returns_none_on_import_error() -> None:
    """未安装 ddddocr 时 _get_ddddocr 返回 None。"""
    cfg = ImageSolverConfig()
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    # 清空缓存
    solver._ddddocr_instance = None
    # sys.modules[name] = None 会让 import name 抛 ImportError
    with patch.dict("sys.modules", {"ddddocr": None}):
        assert solver._get_ddddocr() is None


def test_get_ddddocr_returns_none_on_init_exception() -> None:
    """ddddocr 已安装但 DdddOcr() 构造抛异常时返回 None。"""
    fake_ddddocr = MagicMock()
    fake_ddddocr.DdddOcr.side_effect = RuntimeError("model load failed")

    cfg = ImageSolverConfig()
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    solver._ddddocr_instance = None
    with patch.dict("sys.modules", {"ddddocr": fake_ddddocr}):
        assert solver._get_ddddocr() is None


def test_get_ddddocr_caches_instance() -> None:
    """_get_ddddocr 第二次调用应返回缓存实例。"""
    fake_ddddocr = MagicMock()
    fake_instance = MagicMock()
    fake_ddddocr.DdddOcr.return_value = fake_instance

    cfg = ImageSolverConfig()
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    solver._ddddocr_instance = None
    with patch.dict("sys.modules", {"ddddocr": fake_ddddocr}):
        first = solver._get_ddddocr()
        second = solver._get_ddddocr()
    assert first is second
    # DdddOcr 仅构造一次
    fake_ddddocr.DdddOcr.assert_called_once()


# ---------------------------------------------------------------------------
# Pillow/numpy 滑块匹配路径
# ---------------------------------------------------------------------------


def _make_test_image_bytes(size: tuple[int, int], pattern: str = "blank") -> bytes:
    """生成最小 PNG 字节流（用 Pillow 真实生成）。

    "slider"/"bg_with_gap" 是一对配对夹具，建模真实滑块验证码：

    - 拼图件 12×12：深底（10，亮度掩码排除）+ 左缘 0..5 × y 2..7 的渐变
      纹理块（150..229，无透明通道的 L 模式）；
    - 背景 40×12：灰 128，缺口在列 20..25 = **件纹理 × 0.35**（SF/极验等
      "半透明黑罩"渲染：缺口是原内容变暗，不是空洞）。

    掩码 ZNCC 对线性亮度缩放不变，应在 x=20 处拿到接近 1.0 的匹配分。
    旧夹具（亮块找暗洞）是 SSD 时代的世界模型，不能回归真实失败模式。
    """
    from PIL import Image

    if pattern == "slider":
        img = Image.new("L", size, color=10)
        for x in range(6):
            for y in range(2, 8):
                img.putpixel((x, y), 150 + ((x * 37 + y * 61) % 80))
    elif pattern == "bg_with_gap":
        img = Image.new("L", size, color=128)
        for x in range(6):
            for y in range(2, 8):
                img.putpixel((20 + x, y), int((150 + ((x * 37 + y * 61) % 80)) * 0.35))
    else:
        img = Image.new("L", size, color=128)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_local_slider_numpy_path_returns_x() -> None:
    """numpy 路径下 _local_slider 应精确返回缺口左缘 x=20。"""
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    slider = _make_test_image_bytes((12, 12), "slider")
    x = solver._local_slider(bg, slider)
    assert x == 20


def test_match_slider_darkened_gap_high_confidence() -> None:
    """回归（2026-11-14 SF 实测失败模式）：缺口=件内容×0.35 时 ZNCC 精确定位。

    旧 SSD 实现在这样的图上会匹配到背景里亮度相近的其他纹理；ZNCC 对
    线性亮度缩放不变，应拿到接近 1.0 的分数。
    """
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    slider = _make_test_image_bytes((12, 12), "slider")
    matched = solver._match_slider(bg, slider)
    assert matched is not None
    x, score = matched
    assert x == 20
    assert score > 0.8


def test_match_slider_opaque_luminance_mask_still_works() -> None:
    """无透明通道的 L 模式件走亮度掩码（>30），行为向后兼容。"""
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    slider = _make_test_image_bytes((12, 12), "slider")  # L 模式，无 alpha
    assert solver._match_slider(bg, slider) is not None


def test_match_slider_palette_transparency_uses_alpha_mask() -> None:
    """调色板透明 PNG（P 模式 + tRNS）应以 alpha 为掩码，不被调色板 RGB 污染。

    回归背景：SF piece 实图为 P/RGBA 透明件；旧实现 convert("L") 后按
    亮度设掩码，透明区的调色板 RGB 若非黑会整体污染匹配。
    """
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    import io

    from PIL import Image

    # 件：P 模式，索引 0 = 透明（调色板 RGB 是纯白！），索引 1..4 = 纹理块
    # 索引 1..4 的 convert("L") 亮度 = 0.299R+0.587G+0.114B ≈ 87/111/140/105
    piece = Image.new("P", (12, 12), color=0)
    piece.putpalette(
        [255, 255, 255, 200, 40, 30, 60, 120, 200, 30, 200, 120, 105, 105, 105] + [0] * (768 - 15)
    )
    texture = [1, 2, 3, 4, 3, 2]
    for x in range(6):
        for y in range(2, 8):
            piece.putpixel((x, y), texture[(x + y) % len(texture)])
    piece.info["transparency"] = 0
    buf = io.BytesIO()
    piece.save(buf, format="PNG", transparency=0)
    slider = buf.getvalue()

    # 背景：灰 128，缺口列 20..25 = 件亮度 × 0.5（线性缩放，ZNCC 应精确命中）
    bg = Image.new("L", (40, 12), color=128)
    lums = [87, 111, 140, 105]
    for x in range(6):
        for y in range(2, 8):
            bg.putpixel((20 + x, y), int(lums[texture[(x + y) % len(texture)] - 1] * 0.5))
    buf = io.BytesIO()
    bg.save(buf, format="PNG")
    bgr = buf.getvalue()

    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    matched = solver._match_slider(bgr, slider)
    assert matched is not None
    x, _score = matched
    assert x == 20


def test_match_slider_low_score_returns_none() -> None:
    """背景与件无相关结构（分数低于门限）时返回 None，不输出自信的错误解。"""
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    slider = _make_test_image_bytes((12, 12), "slider")
    # 均匀背景 + 均匀"缺口"：无位置信号
    bg = _make_test_image_bytes((40, 12), "blank")
    assert solver._match_slider(bg, slider) is None


def test_match_slider_low_score_falls_to_llm() -> None:
    """本地匹配低于门限时 solve_slider 应回退 LLM 而非返回低分坐标。"""
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    provider = _FakeVisionProvider(chat_response='{"x": 25, "confidence": 0.5}')
    cfg = ImageSolverConfig(use_llm=True)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    bg = _make_test_image_bytes((40, 12), "blank")
    slider = _make_test_image_bytes((12, 12), "slider")
    result = solver.solve_slider(bg, slider)
    assert result is not None
    assert result.method == "llm"
    assert result.x == 25


def test_match_slider_full_canvas_piece() -> None:
    """回归（2026-11-14 极验实测）：全宽 canvas 拼图件必须裁剪到 blob 再滑窗。

    极验把拼图件画在与背景同宽的全幅 canvas 上（blob 在 canvas 左缘附近）；
    旧实现 sw==bw 时滑窗范围为空、必然无解。裁剪后应精确定位，且返回的
    x 折算回原始 png 左缘坐标系（canvas-left = 匹配位置 - blob 左偏移）。
    """
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    import io

    from PIL import Image

    # 件：全幅 canvas 40×12（与背景同宽），blob 位于 x[4..9] y[2..7] 纹理
    piece = Image.new("RGBA", (40, 12), (0, 0, 0, 0))
    tex = {}
    for dx in range(6):
        for y in range(2, 8):
            v = 150 + ((dx * 37 + y * 61) % 80)
            tex[(dx, y)] = v
            piece.putpixel((4 + dx, y), (v, v, v, 255))
    buf = io.BytesIO()
    piece.save(buf, format="PNG")
    slider = buf.getvalue()

    # 背景：灰 128，缺口在列 24..29 = 件纹理 × 0.35（canvas-left 目标 = 20）
    bg = Image.new("L", (40, 12), color=128)
    for dx in range(6):
        for y in range(2, 8):
            bg.putpixel((24 + dx, y), int(tex[(dx, y)] * 0.35))
    buf = io.BytesIO()
    bg.save(buf, format="PNG")
    bgr = buf.getvalue()

    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    matched = solver._match_slider(bgr, slider)
    assert matched is not None
    x, score = matched
    assert x == 20  # 缺口左缘 24 - blob 左偏移 4
    assert score > 0.8


def test_match_slider_silhouette_flat_dark_hole() -> None:
    """回归（2026-11-14 易盾/极验实测）：平色填充洞走剪影异常二级探测器。

    缺口填纯色（无内容结构）时 ZNCC 无峰（门控拒绝）；探测器应凭
    "足印亮度 vs 环带亮度的台阶" 定位洞的位置。暗洞与亮洞两个方向都要命中。
    """
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    import io

    from PIL import Image

    # 件：RGBA 20×12，blob x[2..7] y[3..8] 带纹理（150..229）
    piece = Image.new("RGBA", (20, 12), (0, 0, 0, 0))
    tex = {}
    for dx in range(6):
        for y in range(3, 9):
            v = 150 + ((dx * 41 + y * 53) % 80)
            tex[(dx, y)] = v
            piece.putpixel((2 + dx, y), (v, v, v, 255))
    buf = io.BytesIO()
    piece.save(buf, format="PNG")
    slider = buf.getvalue()

    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)

    # 暗洞：背景灰 140，缺口列 30..35 填纯色 45（无内容结构）
    bg = Image.new("L", (80, 12), color=140)
    for dx in range(6):
        for y in range(3, 9):
            bg.putpixel((30 + dx, y), 45)
    buf = io.BytesIO()
    bg.save(buf, format="PNG")
    matched = solver._match_slider(buf.getvalue(), slider)
    assert matched is not None
    x, score = matched
    assert x == 28  # 洞左缘 30 - blob 左偏移 2
    assert 0 < score <= 0.7

    # 亮洞：背景灰 60，缺口填纯色 200
    bg2 = Image.new("L", (80, 12), color=60)
    for dx in range(6):
        for y in range(3, 9):
            bg2.putpixel((50 + dx, y), 200)
    buf = io.BytesIO()
    bg2.save(buf, format="PNG")
    matched2 = solver._match_slider(buf.getvalue(), slider)
    assert matched2 is not None
    x2, _score2 = matched2
    assert x2 == 48  # 洞左缘 50 - blob 左偏移 2


def test_match_slider_rejects_oversize_images() -> None:
    """资源上限：超大图（不可信输入灼烧 CPU）直接拒绝，两路径一致。"""
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((2600, 1800), "blank")  # 468 万像素 > 400 万上限
    slider = _make_test_image_bytes((12, 12), "slider")
    assert solver._match_slider(bg, slider) is None
    # 纯 Pillow 路径同样拒绝
    with patch.dict("sys.modules", {"numpy": None}):
        assert solver._match_slider(bg, slider) is None


def test_silhouette_detector_rejects_uniform_background() -> None:
    """无洞的均匀背景：探测器不得误报（三重门限全不过 → None）。"""
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    slider = _make_test_image_bytes((12, 12), "slider")
    bg = _make_test_image_bytes((40, 12), "blank")
    assert solver._match_slider(bg, slider) is None


def test_llm_slider_rejects_out_of_bounds_x() -> None:
    """回归（2026-11-14 易盾实测）：LLM 幻觉越界 x 必须被拒绝。

    背景图 40px 宽，LLM 回 x=340（越界）应返回 None 而不是穿透给调用方。
    """
    pytest.importorskip("PIL")
    provider = _FakeVisionProvider(chat_response='{"x": 340, "confidence": 0.72}')
    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    bg = _make_test_image_bytes((40, 12), "blank")
    slider = _make_test_image_bytes((12, 12), "slider")
    assert solver.solve_slider(bg, slider) is None
    # 界内合法值不受影响
    provider2 = _FakeVisionProvider(chat_response='{"x": 25, "confidence": 0.72}')
    solver2 = ImageCaptchaSolver(provider=provider2, config=cfg)  # type: ignore[arg-type]
    result = solver2.solve_slider(bg, slider)
    assert result is not None
    assert result.x == 25


def test_local_slider_numpy_skips_dark_pixels() -> None:
    """件内暗色像素（亮度掩码 < 30）不参与匹配，仍精确定位缺口。"""
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    # 配对夹具的件本身就以深底（10）包裹纹理块，暗色像素被掩码排除
    slider = _make_test_image_bytes((12, 12), "slider")
    x = solver._local_slider(bg, slider)
    assert x == 20


def test_local_slider_numpy_returns_none_on_invalid_image() -> None:
    """numpy 路径下图片打开失败应返回 None。"""
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    x = solver._local_slider(b"not-an-image", b"also-not")
    assert x is None


def test_local_slider_returns_none_when_slider_larger_than_bg() -> None:
    """slider 比 bg 大时返回 None。"""
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((10, 10))
    slider = _make_test_image_bytes((20, 20))
    x = solver._local_slider(bg, slider)
    assert x is None


def test_local_slider_falls_back_to_pillow_only_when_numpy_missing() -> None:
    """numpy 不可用时回退到 _pillow_only_slider 路径，同样精确定位。"""
    pytest.importorskip("PIL")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    slider = _make_test_image_bytes((12, 12), "slider")
    # 让 numpy 导入失败但 PIL 可用
    with patch.dict("sys.modules", {"numpy": None}):
        x = solver._local_slider(bg, slider)
    assert x == 20


def test_pillow_only_slider_returns_x() -> None:
    """纯 Pillow 路径下 _pillow_only_slider 应精确返回缺口 x=20。"""
    pytest.importorskip("PIL")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    slider = _make_test_image_bytes((12, 12), "slider")
    x = solver._pillow_only_slider(bg, slider)
    assert x == 20


def test_pillow_only_slider_skips_dark_pixels() -> None:
    """纯 Pillow 路径下件内暗色像素（亮度掩码 < 30）不参与匹配。"""
    pytest.importorskip("PIL")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    # 配对夹具的件以深底（10）包裹纹理块，暗色像素被掩码排除
    slider = _make_test_image_bytes((12, 12), "slider")
    x = solver._pillow_only_slider(bg, slider)
    assert x == 20


def test_pillow_only_slider_returns_none_on_invalid_image() -> None:
    """纯 Pillow 路径下图片打开失败返回 None。"""
    pytest.importorskip("PIL")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    x = solver._pillow_only_slider(b"not-an-image", b"also-not")
    assert x is None


def test_pillow_only_slider_returns_none_when_slider_larger_than_bg() -> None:
    """slider 大于 bg 时纯 Pillow 路径返回 None。"""
    pytest.importorskip("PIL")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((10, 10))
    slider = _make_test_image_bytes((20, 20))
    x = solver._pillow_only_slider(bg, slider)
    assert x is None


def test_solve_slider_local_pillow_returns_solution() -> None:
    """solve_slider 在 Pillow 路径成功时返回 SliderSolution(method='pillow')。

    confidence 改为实测 ZNCC 分（旧值为硬编码 0.7），夹具缺口=件×0.35
    应得到接近 1.0 的分数。
    """
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    cfg = ImageSolverConfig(use_pillow_slider=True, use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    slider = _make_test_image_bytes((12, 12), "slider")
    result = solver.solve_slider(bg, slider)
    assert result is not None
    assert result.method == "pillow"
    assert result.x == 20
    assert 0.45 <= result.confidence <= 1.0


def test_solve_slider_async_local_pillow_returns_solution() -> None:
    """solve_slider_async 在 Pillow 路径成功时返回 SliderSolution。"""
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    cfg = ImageSolverConfig(use_pillow_slider=True, use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    slider = _make_test_image_bytes((12, 12), "slider")
    result = asyncio.run(solver.solve_slider_async(bg, slider))
    assert result is not None
    assert result.method == "pillow"


# ---------------------------------------------------------------------------
# _llm_slider_async / _llm_ocr_async 兜底分支
# ---------------------------------------------------------------------------


def test_llm_slider_async_falls_back_to_sync_when_no_achat() -> None:
    """provider 不含 achat 时 _llm_slider_async 回退到同步路径。"""

    @dataclass
    class _SyncOnlyProvider:
        name: str = "sync-only"
        model: str = "sync-model"
        capabilities: ProviderCapabilities = field(
            default_factory=lambda: ProviderCapabilities(vision=True)
        )

        def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
            return LLMResponse(content='{"x": 150, "confidence": 0.7}', model=self.model)

    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=_SyncOnlyProvider(), config=cfg)  # type: ignore[arg-type]
    result = asyncio.run(solver.solve_slider_async(b"bg", b"slider"))
    assert result is not None
    assert result.x == 150
    assert result.method == "llm"


# ---------------------------------------------------------------------------
# solve_click_async 早返回与 _parse_click_response 边界
# ---------------------------------------------------------------------------


def test_solve_click_async_no_vision_returns_none() -> None:
    """solve_click_async 在无 vision 时直接返回 None（早返回）。"""
    solver = ImageCaptchaSolver(provider=None)
    result = asyncio.run(solver.solve_click_async(b"img", "请点击"))
    assert result is None


def test_parse_click_response_skips_invalid_xy_values() -> None:
    """x/y 非数字时该点被跳过。"""
    text = '{"points": [{"x": "abc", "y": 1}, {"x": 2, "y": 3}]}'
    sol = ImageCaptchaSolver._parse_click_response(text)
    assert sol is not None
    assert sol.points == [(2, 3)]


def test_parse_click_response_with_invalid_label_skipped() -> None:
    """非 dict 元素被跳过；空 points 列表返回 None。"""
    text = '{"points": []}'
    assert ImageCaptchaSolver._parse_click_response(text) is None


def test_parse_click_response_not_dict_returns_none() -> None:
    """顶层 JSON 不是 dict 时返回 None。"""
    text = "[1, 2, 3]"
    assert ImageCaptchaSolver._parse_click_response(text) is None


# ---------------------------------------------------------------------------
# solve_slider / solve_text 边界
# ---------------------------------------------------------------------------


def test_solve_slider_local_returns_zero_falls_to_llm() -> None:
    """本地 slider 返回 0 时（视为无效）回退到 LLM 路径。"""
    provider = _FakeVisionProvider(chat_response='{"x": 88, "confidence": 0.5}')
    cfg = ImageSolverConfig(use_pillow_slider=True, use_llm=True)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    # mock _local_slider 返回 0
    with patch.object(solver, "_local_slider", return_value=0):
        result = solver.solve_slider(b"bg", b"slider")
    assert result is not None
    assert result.method == "llm"
    assert result.x == 88


def test_solve_slider_local_returns_none_falls_to_llm() -> None:
    """本地 slider 返回 None 时回退到 LLM 路径。"""
    provider = _FakeVisionProvider(chat_response='{"x": 99, "confidence": 0.6}')
    cfg = ImageSolverConfig(use_pillow_slider=True, use_llm=True)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    with patch.object(solver, "_local_slider", return_value=None):
        result = solver.solve_slider(b"bg", b"slider")
    assert result is not None
    assert result.method == "llm"


def test_solve_slider_async_local_returns_none_falls_to_llm() -> None:
    """solve_slider_async 在本地路径返回 None 时回退到 LLM。"""
    provider = _FakeVisionProvider(achat_response='{"x": 77, "confidence": 0.5}')
    cfg = ImageSolverConfig(use_pillow_slider=True, use_llm=True)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    with patch.object(solver, "_local_slider", return_value=None):
        result = asyncio.run(solver.solve_slider_async(b"bg", b"slider"))
    assert result is not None
    assert result.method == "llm"
    assert result.x == 77


def test_solve_slider_async_no_provider_returns_none() -> None:
    """无 provider 且本地不可用时 solve_slider_async 返回 None。"""
    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    result = asyncio.run(solver.solve_slider_async(b"bg", b"slider"))
    assert result is None


# ---------------------------------------------------------------------------
# _parse_slider_response 边界
# ---------------------------------------------------------------------------


def test_parse_slider_response_with_invalid_x_type_returns_none() -> None:
    """x 为非数字字符串时返回 None。"""
    assert ImageCaptchaSolver._parse_slider_response('{"x": "abc"}') is None


def test_parse_slider_response_with_invalid_confidence_defaults() -> None:
    """confidence 非数字时回退到 0.5。"""
    sol = ImageCaptchaSolver._parse_slider_response('{"x": 30, "confidence": "high"}')
    assert sol is not None
    assert sol.x == 30
    assert sol.confidence == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# _to_b64 / _b64_to_bytes 额外边界
# ---------------------------------------------------------------------------


def test_to_b64_from_bytes_roundtrip() -> None:
    """bytes → base64 → bytes 应可往返。"""
    raw = b"\x00\x01\x02\xff"
    encoded = _to_b64(raw)
    assert _b64_to_bytes(encoded) == raw


def test_b64_to_bytes_with_invalid_base64_returns_garbage() -> None:
    """非法 base64 字符串（无 data: 前缀）走 b64decode，行为依赖 binascii。

    这里仅验证不抛异常（base64.b64decode 默认 validate=False 会跳过非法字符）。
    """
    # 短字符串可能解码为空或短字节，主要验证不抛
    result = _b64_to_bytes("!!!")
    assert isinstance(result, bytes)


# ---------------------------------------------------------------------------
# 配置项与 provider 缺失组合
# ---------------------------------------------------------------------------


def test_llm_vision_available_provider_without_capabilities_attr() -> None:
    """provider 没有 capabilities 属性时 llm_vision_available 为 False。"""

    class _NoCaps:
        name = "no-caps"
        model = "m"

        def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
            return LLMResponse(content="", model="m")

    solver = ImageCaptchaSolver(provider=_NoCaps(), config=ImageSolverConfig())  # type: ignore[arg-type]
    assert solver.llm_vision_available is False


def test_solve_text_local_ocr_filters_to_empty_then_llm() -> None:
    """本地 OCR 结果过滤后为空（无有效字符），应降级到 LLM。"""
    fake_ddddocr = MagicMock()
    fake_instance = MagicMock()
    # 全部是特殊字符，过滤后为空
    fake_instance.classification.return_value = "!!!@@@"
    fake_ddddocr.DdddOcr.return_value = fake_instance

    provider = _FakeVisionProvider(chat_response="LLM99")
    solver = ImageCaptchaSolver(provider=provider)  # type: ignore[arg-type]
    with patch.dict("sys.modules", {"ddddocr": fake_ddddocr}):
        result = solver.solve_text(b"fake-png-bytes")
    assert result == "LLM99"


def test_solve_text_local_ocr_too_long_falls_back_to_llm() -> None:
    """本地 OCR 结果过长被丢弃，应降级到 LLM。"""
    fake_ddddocr = MagicMock()
    fake_instance = MagicMock()
    fake_instance.classification.return_value = "abcdefghij"  # 10 字符
    fake_ddddocr.DdddOcr.return_value = fake_instance

    provider = _FakeVisionProvider(chat_response="LLM42")
    cfg = ImageSolverConfig(ocr_max_length=8)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    with patch.dict("sys.modules", {"ddddocr": fake_ddddocr}):
        result = solver.solve_text(b"fake-png-bytes")
    assert result == "LLM42"


# ===========================================================================
# 回归：base64 解码异常兜底（坏 padding 不抛异常）
# ===========================================================================


def test_local_slider_bad_base64_returns_none() -> None:
    """_local_slider 遇到坏 padding 的 base64 应返回 None 而非抛异常。"""
    solver = ImageCaptchaSolver(provider=None, config=ImageSolverConfig(use_llm=False))
    # "abcde=" 数据字符数 % 4 == 1，binascii 拒绝解码
    assert solver._local_slider("abcde=", "aGVsbG8=") is None


def test_solve_slider_bad_base64_returns_none() -> None:
    """solve_slider 对坏 base64 输入返回 None（失败兜底契约）。"""
    solver = ImageCaptchaSolver(provider=None)  # use_pillow_slider=True
    assert solver.solve_slider("abcde=", "abcde=") is None


def test_b64_to_bytes_bad_padding_raises_binascii() -> None:
    """坏 padding 的 base64 在 _b64_to_bytes 层抛 binascii.Error（由调用方兜底）。"""
    import binascii

    with pytest.raises(binascii.Error):
        _b64_to_bytes("abcde=")


# ===========================================================================
# 回归：送 LLM 前图片尺寸/体积上限与降采样
# ===========================================================================


def _make_big_image_bytes(width: int, height: int) -> bytes:
    """生成指定尺寸的纯色 PNG。"""
    from PIL import Image

    img = Image.new("L", (width, height), color=128)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_prepare_vision_image_resizes_large_image() -> None:
    """超过最长边上限的图片应被等比降采样到上限内。"""
    pytest.importorskip("PIL")
    raw = _make_big_image_bytes(2000, 100)
    result = _prepare_vision_image(raw, mime="image/png")
    assert result is not None
    b64, mime, scale = result
    assert mime == "image/png"  # 降采样后重编码为 PNG
    assert scale > 1.0
    from PIL import Image as PILImage

    decoded = PILImage.open(io.BytesIO(base64.b64decode(b64)))
    assert decoded.size[0] <= 1280


def test_prepare_vision_image_small_image_unchanged() -> None:
    """小图不应被降采样（scale == 1.0，mime 保持原值）。"""
    pytest.importorskip("PIL")
    raw = _make_big_image_bytes(100, 50)
    result = _prepare_vision_image(raw, mime="image/jpeg")
    assert result is not None
    b64, mime, scale = result
    assert scale == 1.0
    assert mime == "image/jpeg"
    assert base64.b64decode(b64) == raw


def test_prepare_vision_image_undecodable_passes_through() -> None:
    """无法解码的图片原样透传（scale=1.0，mime 不变），由识别层兜底。"""
    pytest.importorskip("PIL")
    result = _prepare_vision_image(b"not an image")
    assert result is not None
    b64, mime, scale = result
    assert scale == 1.0
    assert mime == "image/png"
    assert base64.b64decode(b64) == b"not an image"


def test_prepare_vision_image_oversize_bytes_returns_none() -> None:
    """超过体积上限的输入直接拒绝（不依赖 PIL）。"""
    import web_crawler.ai.image_captcha as ic_mod

    big = b"x" * (ic_mod._MAX_IMAGE_BYTES + 1)
    assert _prepare_vision_image(big) is None


def test_prepare_vision_image_bad_base64_returns_none() -> None:
    """坏 base64 输入返回 None。"""
    assert _prepare_vision_image("abcde=") is None


def test_llm_slider_rescales_coordinates_after_downscale() -> None:
    """背景图被降采样后，LLM 返回的 x 应还原回原始像素坐标系。"""
    pytest.importorskip("PIL")
    provider = _FakeVisionProvider(chat_response='{"x": 100, "confidence": 0.9}')
    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    bg = _make_big_image_bytes(2000, 100)
    slider = _make_test_image_bytes((12, 12), "slider")
    sol = solver.solve_slider(bg, slider)
    assert sol is not None
    # scale = 2000 / 1280 ≈ 1.5625；100 * 1.5625 = 156.25 → round 156
    assert sol.x == round(100 * (2000 / 1280))


# ===========================================================================
# 扩展：_prepare_vision_image 边界（PIL 缺失 / 超像素）与 prepared=None 分支
# ===========================================================================


def test_prepare_vision_image_without_pillow_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 Pillow 时不做尺寸检查/降采样，原样透传（scale=1.0）。"""
    monkeypatch.setitem(sys.modules, "PIL", None)
    result = _prepare_vision_image(b"raw image bytes", mime="image/png")
    assert result is not None
    b64, mime, scale = result
    assert base64.b64decode(b64) == b"raw image bytes"
    assert mime == "image/png"
    assert scale == 1.0


def test_prepare_vision_image_pixel_limit_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超过像素上限的图片返回 None（拒绝送 LLM）。"""
    pytest.importorskip("PIL")
    import web_crawler.ai.image_captcha as ic_mod

    monkeypatch.setattr(ic_mod, "_MAX_IMAGE_PIXELS", 100)
    raw = _make_big_image_bytes(50, 50)  # 2500 像素 > 100
    assert _prepare_vision_image(raw) is None


def test_llm_ocr_prepared_none_returns_empty() -> None:
    """_llm_ocr 在图片预处理失败时返回空串。"""
    solver = ImageCaptchaSolver(provider=_FakeVisionProvider())  # type: ignore[arg-type]
    assert solver._llm_ocr("abcde=", "image/png") == ""


def test_llm_ocr_async_prepared_none_returns_empty() -> None:
    """_llm_ocr_async 在图片预处理失败时返回空串。"""
    solver = ImageCaptchaSolver(provider=_FakeVisionProvider())  # type: ignore[arg-type]
    assert asyncio.run(solver._llm_ocr_async("abcde=", "image/png")) == ""


def test_llm_slider_prepared_none_returns_none() -> None:
    """_llm_slider 在任一图片预处理失败时返回 None。"""
    solver = ImageCaptchaSolver(provider=_FakeVisionProvider())  # type: ignore[arg-type]
    assert solver._llm_slider("abcde=", b"slider") is None
    assert solver._llm_slider(b"bg", "abcde=") is None


def test_llm_slider_async_prepared_none_returns_none() -> None:
    """_llm_slider_async 在任一图片预处理失败时返回 None。"""
    solver = ImageCaptchaSolver(provider=_FakeVisionProvider())  # type: ignore[arg-type]
    assert asyncio.run(solver._llm_slider_async("abcde=", b"slider")) is None


def test_llm_slider_async_rescales_coordinates_after_downscale() -> None:
    """异步路径背景图降采样后，LLM 返回的 x 同样还原回原始坐标系。"""
    pytest.importorskip("PIL")
    provider = _FakeVisionProvider(achat_response='{"x": 100, "confidence": 0.9}')
    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    bg = _make_big_image_bytes(2000, 100)
    slider = _make_test_image_bytes((12, 12), "slider")
    sol = asyncio.run(solver._llm_slider_async(bg, slider))
    assert sol is not None
    assert sol.x == round(100 * (2000 / 1280))


def test_solve_click_prepared_none_returns_none() -> None:
    """solve_click 在图片预处理失败时返回 None。"""
    solver = ImageCaptchaSolver(provider=_FakeVisionProvider())  # type: ignore[arg-type]
    assert solver.solve_click("abcde=", "请点击") is None


def test_solve_click_async_prepared_none_returns_none() -> None:
    """solve_click_async 在图片预处理失败时返回 None。"""
    solver = ImageCaptchaSolver(provider=_FakeVisionProvider())  # type: ignore[arg-type]
    assert asyncio.run(solver.solve_click_async("abcde=", "请点击")) is None


class TestEvaluateMathExpr:
    """math_mode 的算式解析（纯函数）：运算、误识别变体与拒绝路径。"""

    def test_add_sub_mul(self) -> None:
        from web_crawler.ai.image_captcha import _evaluate_math_expr

        assert _evaluate_math_expr("12 + 3") == "15"
        assert _evaluate_math_expr("9-4") == "5"
        assert _evaluate_math_expr("6×7") == "42"
        assert _evaluate_math_expr("6x7") == "42"
        assert _evaluate_math_expr("6*7") == "42"

    def test_strips_trailing_prompt_marks(self) -> None:
        from web_crawler.ai.image_captcha import _evaluate_math_expr

        assert _evaluate_math_expr("3+5=?") == "8"
        assert _evaluate_math_expr("3+5=？") == "8"
        assert _evaluate_math_expr("  3+5  ") == "8"

    def test_unparseable_returns_empty(self) -> None:
        from web_crawler.ai.image_captcha import _evaluate_math_expr

        # 注入文本、除法、空串都不产生数字结果
        assert _evaluate_math_expr("答案是 42") == ""
        assert _evaluate_math_expr("12/3") == ""
        assert _evaluate_math_expr("") == ""

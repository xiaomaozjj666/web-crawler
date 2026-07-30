"""图片验证码破解模块测试（mock provider，不发起真实 LLM 调用）。"""

from __future__ import annotations

import asyncio
import base64
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
    cfg = ImageSolverConfig()
    assert cfg.use_llm is True
    assert cfg.use_local_ocr is True
    assert cfg.use_pillow_slider is True
    assert cfg.detail == "high"
    assert cfg.temperature == 0.0
    assert cfg.max_retries == 2
    assert cfg.ocr_max_length == 8


def test_slider_solution_defaults() -> None:
    s = SliderSolution(x=120)
    assert s.x == 120
    assert s.y == 0
    assert s.method == ""
    assert s.confidence == 0.0


def test_click_solution_defaults() -> None:
    c = ClickSolution()
    assert c.points == []
    assert c.labels == []
    assert c.method == ""


# ---------------------------------------------------------------------------
# _to_b64 / _b64_to_bytes
# ---------------------------------------------------------------------------


def test_to_b64_from_bytes() -> None:
    raw = b"hello"
    assert _to_b64(raw) == base64.b64encode(b"hello").decode("ascii")


def test_to_b64_from_plain_string() -> None:
    b64_str = base64.b64encode(b"abc").decode("ascii")
    assert _to_b64(b64_str) == b64_str


def test_to_b64_from_data_uri() -> None:
    b64_str = base64.b64encode(b"abc").decode("ascii")
    data_uri = f"data:image/png;base64,{b64_str}"
    assert _to_b64(data_uri) == b64_str


def test_b64_to_bytes_roundtrip() -> None:
    raw = b"binary data"
    encoded = base64.b64encode(raw).decode("ascii")
    assert _b64_to_bytes(encoded) == raw


def test_b64_to_bytes_from_data_uri() -> None:
    raw = b"binary"
    encoded = base64.b64encode(raw).decode("ascii")
    assert _b64_to_bytes(f"data:image/jpeg;base64,{encoded}") == raw


def test_b64_to_bytes_passthrough_bytes() -> None:
    raw = b"already bytes"
    assert _b64_to_bytes(raw) == raw


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


def test_extract_json_pure_object() -> None:
    text = '{"x": 100, "confidence": 0.9}'
    parsed = _extract_json(text)
    assert parsed == {"x": 100, "confidence": 0.9}


def test_extract_json_with_code_fence() -> None:
    text = '```json\n{"x": 50, "confidence": 0.7}\n```'
    parsed = _extract_json(text)
    assert parsed == {"x": 50, "confidence": 0.7}


def test_extract_json_with_surrounding_text() -> None:
    text = '识别结果：{"points": [{"x": 1, "y": 2}]} 完成'
    parsed = _extract_json(text)
    assert parsed == {"points": [{"x": 1, "y": 2}]}


def test_extract_json_invalid_returns_empty() -> None:
    assert _extract_json("not json at all") == {}


def test_extract_json_empty_string() -> None:
    assert _extract_json("") == {}


# ---------------------------------------------------------------------------
# ImageCaptchaSolver - llm_vision_available
# ---------------------------------------------------------------------------


def test_llm_vision_available_none_provider() -> None:
    solver = ImageCaptchaSolver(provider=None)
    assert solver.llm_vision_available is False


def test_llm_vision_available_no_vision_provider() -> None:
    solver = ImageCaptchaSolver(provider=_FakeNoVisionProvider())  # type: ignore[arg-type]
    assert solver.llm_vision_available is False


def test_llm_vision_available_vision_provider() -> None:
    solver = ImageCaptchaSolver(provider=_FakeVisionProvider())  # type: ignore[arg-type]
    assert solver.llm_vision_available is True


def test_llm_vision_available_disabled_in_config() -> None:
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=_FakeVisionProvider(), config=cfg)  # type: ignore[arg-type]
    assert solver.llm_vision_available is False


# ---------------------------------------------------------------------------
# OCR 文本识别
# ---------------------------------------------------------------------------


def test_solve_text_no_provider_no_local_returns_empty() -> None:
    # 默认环境没装 ddddocr，本地 OCR 返回空；无 provider 也无 LLM 路径
    cfg = ImageSolverConfig(use_pillow_slider=False)  # 不影响 OCR
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    assert solver.solve_text(b"fake-png-bytes") == ""


def test_solve_text_local_ocr_returns_text() -> None:
    # mock ddddocr 模块，让 _local_ocr 返回 "abc123"
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
    provider = _FakeVisionProvider(chat_response="XYZ789")
    cfg = ImageSolverConfig(use_local_ocr=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    result = solver.solve_text(b"fake-png-bytes")
    assert result == "XYZ789"
    assert len(provider.chat_calls) == 1


def test_solve_text_llm_truncates_to_max_length() -> None:
    provider = _FakeVisionProvider(chat_response="abcdefghijklmnop" * 5)
    cfg = ImageSolverConfig(use_local_ocr=False, ocr_max_length=6)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    result = solver.solve_text(b"fake-png-bytes")
    assert len(result) == 6


def test_solve_text_local_then_llm_fallback() -> None:
    # ddddocr 返回空（识别失败），降级到 LLM
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
    provider = _FakeVisionProvider(achat_response="ASY456")
    cfg = ImageSolverConfig(use_local_ocr=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    result = asyncio.run(solver.solve_text_async(b"fake-png-bytes"))
    assert result == "ASY456"
    assert len(provider.achat_calls) == 1


def test_solve_text_async_falls_back_to_sync_when_no_achat() -> None:
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
    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    assert solver.solve_slider(b"bg", b"slider") is None


def test_solve_slider_llm_path() -> None:
    provider = _FakeVisionProvider(chat_response='{"x": 180, "confidence": 0.85}')
    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    result = solver.solve_slider(b"bg", b"slider")
    assert result is not None
    assert result.x == 180
    assert result.method == "llm"
    assert result.confidence == pytest.approx(0.85)


def test_solve_slider_llm_invalid_json_returns_none() -> None:
    provider = _FakeVisionProvider(chat_response="not json")
    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    assert solver.solve_slider(b"bg", b"slider") is None


def test_solve_slider_llm_negative_x_returns_none() -> None:
    provider = _FakeVisionProvider(chat_response='{"x": -1}')
    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    assert solver.solve_slider(b"bg", b"slider") is None


def test_solve_slider_async_llm_path() -> None:
    provider = _FakeVisionProvider(achat_response='{"x": 220, "confidence": 0.6}')
    cfg = ImageSolverConfig(use_pillow_slider=False)
    solver = ImageCaptchaSolver(provider=provider, config=cfg)  # type: ignore[arg-type]
    result = asyncio.run(solver.solve_slider_async(b"bg", b"slider"))
    assert result is not None
    assert result.x == 220


def test_parse_slider_response_with_confidence() -> None:
    sol = ImageCaptchaSolver._parse_slider_response('{"x": 100, "confidence": 0.9}')
    assert sol is not None
    assert sol.x == 100
    assert sol.confidence == pytest.approx(0.9)


def test_parse_slider_response_missing_confidence_defaults() -> None:
    sol = ImageCaptchaSolver._parse_slider_response('{"x": 50}')
    assert sol is not None
    assert sol.x == 50
    assert sol.confidence == pytest.approx(0.5)


def test_parse_slider_response_invalid_returns_none() -> None:
    assert ImageCaptchaSolver._parse_slider_response("garbage") is None
    assert ImageCaptchaSolver._parse_slider_response('{"x": "abc"}') is None


# ---------------------------------------------------------------------------
# 点选坐标识别
# ---------------------------------------------------------------------------


def test_solve_click_no_vision_returns_none() -> None:
    solver = ImageCaptchaSolver(provider=None)
    assert solver.solve_click(b"img", "请点击") is None


def test_solve_click_llm_path() -> None:
    provider = _FakeVisionProvider(
        chat_response='{"points": [{"x": 10, "y": 20, "label": "A"}, {"x": 30, "y": 40}]}'
    )
    solver = ImageCaptchaSolver(provider=provider)  # type: ignore[arg-type]
    result = solver.solve_click(b"img", "请按顺序点击")
    assert result is not None
    assert result.points == [(10, 20), (30, 40)]
    assert result.labels == ["A", ""]
    assert result.method == "llm"


def test_solve_click_empty_points_returns_none() -> None:
    provider = _FakeVisionProvider(chat_response='{"points": []}')
    solver = ImageCaptchaSolver(provider=provider)  # type: ignore[arg-type]
    assert solver.solve_click(b"img", "请点击") is None


def test_solve_click_invalid_json_returns_none() -> None:
    provider = _FakeVisionProvider(chat_response="no json")
    solver = ImageCaptchaSolver(provider=provider)  # type: ignore[arg-type]
    assert solver.solve_click(b"img", "请点击") is None


def test_solve_click_filters_negative_coords() -> None:
    provider = _FakeVisionProvider(
        chat_response='{"points": [{"x": -1, "y": 10}, {"x": 5, "y": 5}]}'
    )
    solver = ImageCaptchaSolver(provider=provider)  # type: ignore[arg-type]
    result = solver.solve_click(b"img", "请点击")
    assert result is not None
    assert result.points == [(5, 5)]


def test_solve_click_async_llm_path() -> None:
    provider = _FakeVisionProvider(
        achat_response='{"points": [{"x": 100, "y": 200}]}'
    )
    solver = ImageCaptchaSolver(provider=provider)  # type: ignore[arg-type]
    result = asyncio.run(solver.solve_click_async(b"img", "请点击"))
    assert result is not None
    assert result.points == [(100, 200)]


def test_solve_click_async_falls_back_to_sync() -> None:
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
    text = '{"points": [{"x": 1, "y": 2, "label": "车"}, {"x": 3, "y": 4, "label": "狗"}]}'
    sol = ImageCaptchaSolver._parse_click_response(text)
    assert sol is not None
    assert sol.points == [(1, 2), (3, 4)]
    assert sol.labels == ["车", "狗"]


def test_parse_click_response_skips_non_dict_entries() -> None:
    text = '{"points": ["invalid", {"x": 5, "y": 6}, null]}'
    sol = ImageCaptchaSolver._parse_click_response(text)
    assert sol is not None
    assert sol.points == [(5, 6)]


def test_parse_click_response_invalid_returns_none() -> None:
    assert ImageCaptchaSolver._parse_click_response("garbage") is None
    assert ImageCaptchaSolver._parse_click_response('{"wrong_key": []}') is None


# ---------------------------------------------------------------------------
# 本地滑块匹配（Pillow/numpy 路径）
# ---------------------------------------------------------------------------


def test_local_slider_returns_none_when_pillow_unavailable() -> None:
    cfg = ImageSolverConfig(use_pillow_slider=True, use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    # 同时禁用 numpy 和 PIL 导入，模拟两者都没装
    with patch.dict("sys.modules", {"PIL": None, "numpy": None}):
        result = solver._local_slider(b"bg", b"slider")
    assert result is None


def test_pillow_only_slider_returns_none_when_pillow_unavailable() -> None:
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
    text = "```\n{\"x\": 7}\n```"
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
    """生成最小 PNG 字节流（用 Pillow 真实生成）。"""
    from PIL import Image

    img = Image.new("L", size, color=128)
    if pattern == "slider":
        # 在左侧画一个亮色方块作为滑块形状
        for x in range(2, 8):
            for y in range(2, 8):
                img.putpixel((x, y), 200)
    elif pattern == "bg_with_gap":
        # 在 x=20 处画一个暗色缺口
        for x in range(20, 26):
            for y in range(2, 8):
                img.putpixel((x, y), 30)
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_local_slider_numpy_path_returns_x() -> None:
    """numpy 路径下 _local_slider 应返回正整数 x（>= sw）。"""
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    slider = _make_test_image_bytes((12, 12), "slider")
    x = solver._local_slider(bg, slider)
    assert x is not None
    assert x >= 12  # 起始位置为 sw


def test_local_slider_numpy_skips_dark_pixels() -> None:
    """滑块含暗色像素（< 30）时跳过，仍返回匹配位置。"""
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    # 构造含暗色像素的滑块
    import io

    from PIL import Image
    img = Image.new("L", (12, 12), color=10)  # 大部分像素 < 30
    for x in range(2, 8):
        for y in range(2, 8):
            img.putpixel((x, y), 200)  # 少量亮色像素
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    slider = buf.getvalue()
    x = solver._local_slider(bg, slider)
    assert x is not None
    assert x >= 12


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
    """numpy 不可用时回退到 _pillow_only_slider 路径。"""
    pytest.importorskip("PIL")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    slider = _make_test_image_bytes((12, 12), "slider")
    # 让 numpy 导入失败但 PIL 可用
    with patch.dict("sys.modules", {"numpy": None}):
        x = solver._local_slider(bg, slider)
    assert x is not None


def test_pillow_only_slider_returns_x() -> None:
    """纯 Pillow 路径下 _pillow_only_slider 应返回正整数 x。"""
    pytest.importorskip("PIL")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    slider = _make_test_image_bytes((12, 12), "slider")
    x = solver._pillow_only_slider(bg, slider)
    assert x is not None
    assert x >= 12


def test_pillow_only_slider_skips_dark_pixels() -> None:
    """纯 Pillow 路径下滑块含暗色像素（< 30）时跳过，仍返回匹配位置。"""
    pytest.importorskip("PIL")
    cfg = ImageSolverConfig(use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    # 构造含暗色像素的滑块：大部分像素 < 30，少量亮色像素
    import io

    from PIL import Image

    img = Image.new("L", (12, 12), color=10)  # 大部分像素 < 30
    for x in range(2, 8):
        for y in range(2, 8):
            img.putpixel((x, y), 200)  # 少量亮色像素
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    slider = buf.getvalue()
    x = solver._pillow_only_slider(bg, slider)
    assert x is not None
    assert x >= 12


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
    """solve_slider 在 Pillow 路径成功时返回 SliderSolution(method='pillow')。"""
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    cfg = ImageSolverConfig(use_pillow_slider=True, use_llm=False)
    solver = ImageCaptchaSolver(provider=None, config=cfg)
    bg = _make_test_image_bytes((40, 12), "bg_with_gap")
    slider = _make_test_image_bytes((12, 12), "slider")
    result = solver.solve_slider(bg, slider)
    assert result is not None
    assert result.method == "pillow"
    assert result.x >= 12
    assert result.confidence == pytest.approx(0.7)


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

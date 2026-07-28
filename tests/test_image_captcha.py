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

"""VisualExtractor 单元测试。

mock urllib 与可选的 OpenAI client，覆盖 extract / _call_api /
extract_with_client 的所有分支，不发起真实网络请求。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from web_crawler.parser.visual import VisualExtractor

# ---------------------------------------------------------------------------
# 构造与初始化
# ---------------------------------------------------------------------------


def test_init_strips_trailing_slash_from_base_url() -> None:
    """base_url 末尾的 / 应被去掉。"""
    extractor = VisualExtractor(api_key="k", base_url="https://api.x.com/v1/")
    assert extractor.base_url == "https://api.x.com/v1"
    assert extractor.model == "gpt-4o"
    assert extractor.max_tokens == 4096
    assert extractor.timeout == 120.0


def test_init_keeps_base_url_without_slash() -> None:
    extractor = VisualExtractor(api_key="k", base_url="https://api.x.com/v1")
    assert extractor.base_url == "https://api.x.com/v1"


def test_init_custom_parameters() -> None:
    extractor = VisualExtractor(
        api_key="secret",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-vl-max",
        max_tokens=2048,
        timeout=30.0,
    )
    assert extractor.api_key == "secret"
    assert extractor.model == "qwen-vl-max"
    assert extractor.max_tokens == 2048
    assert extractor.timeout == 30.0


# ---------------------------------------------------------------------------
# extract：瓦片组装与 _call_api 调度
# ---------------------------------------------------------------------------


def test_extract_empty_tiles_raises_value_error() -> None:
    extractor = VisualExtractor(api_key="k")
    with pytest.raises(ValueError, match="tiles must not be empty"):
        extractor.extract([])


def test_extract_single_tile_calls_api_and_returns_content() -> None:
    """单瓦片不插入分节标注，直接返回 _call_api 的结果（extract 不再 strip）。"""
    extractor = VisualExtractor(api_key="k")
    with patch.object(extractor, "_call_api", return_value="hello") as mock_call:
        result = extractor.extract([{"b64": "AAAA"}], "提取正文")

    assert result == "hello"  # extract 直接透传 _call_api 返回值
    assert mock_call.called
    messages = mock_call.call_args.args[0]
    temperature = mock_call.call_args.args[1]
    assert temperature == 0.3
    # 单瓦片：content = [prompt_text, image_url]（无分节文本）
    content = messages[0]["content"]
    assert content[0] == {"type": "text", "text": "提取正文"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "data:image/png;base64,AAAA"
    assert content[1]["image_url"]["detail"] == "auto"


def test_extract_multiple_tiles_inserts_section_annotations() -> None:
    """多瓦片时在每个图片前插入分节文本标注。"""
    extractor = VisualExtractor(api_key="k")
    with patch.object(extractor, "_call_api", return_value="ok") as mock_call:
        extractor.extract([{"b64": "A"}, {"b64": "B"}], "p")

    content = mock_call.call_args.args[0][0]["content"]
    # 期望顺序：prompt, section1, img1, section2, img2
    assert content[0] == {"type": "text", "text": "p"}
    assert content[1]["text"] == "\n--- Page section 1/2 ---\n"
    assert content[2]["type"] == "image_url"
    assert content[3]["text"] == "\n--- Page section 2/2 ---\n"
    assert content[4]["type"] == "image_url"


def test_extract_uses_default_prompt_when_none_given() -> None:
    extractor = VisualExtractor(api_key="k")
    with patch.object(extractor, "_call_api", return_value="ok") as mock_call:
        extractor.extract([{"b64": "A"}])

    prompt_text = mock_call.call_args.args[0][0]["content"][0]["text"]
    assert "main content" in prompt_text


def test_extract_caps_tiles_to_max_tiles() -> None:
    """tile 数量超过 max_tiles 时只发送前 max_tiles 个。"""
    extractor = VisualExtractor(api_key="k")
    tiles = [{"b64": str(i)} for i in range(50)]
    with patch.object(extractor, "_call_api", return_value="ok") as mock_call:
        extractor.extract(tiles, "p", max_tiles=5)

    content = mock_call.call_args.args[0][0]["content"]
    # 5 个分节文本 + 5 个图片 + 1 个 prompt = 11
    image_count = sum(1 for c in content if c.get("type") == "image_url")
    assert image_count == 5


def test_extract_passes_temperature_to_call_api() -> None:
    extractor = VisualExtractor(api_key="k")
    with patch.object(extractor, "_call_api", return_value="ok") as mock_call:
        extractor.extract([{"b64": "A"}], "p", temperature=0.7)
    assert mock_call.call_args.args[1] == 0.7


# ---------------------------------------------------------------------------
# _call_api：urllib 请求与响应解析
# ---------------------------------------------------------------------------


def _make_urlopen_response(payload: dict[str, Any]) -> MagicMock:
    """构造 urlopen context manager 返回的对象。"""
    resp_ctx = MagicMock()
    resp_ctx.__enter__.return_value = resp_ctx
    resp_ctx.__exit__.return_value = False
    resp_ctx.read.return_value = json.dumps(payload).encode("utf-8")
    return resp_ctx


def test_call_api_success_returns_stripped_content() -> None:
    extractor = VisualExtractor(api_key="key", base_url="https://api.x.com/v1")
    payload = {"choices": [{"message": {"content": "  extracted text  "}, "finish_reason": "stop"}]}
    with patch(
        "web_crawler.parser.visual.urlopen", return_value=_make_urlopen_response(payload)
    ) as mock_open:
        result = extractor._call_api([{"role": "user", "content": "hi"}], 0.3)

    assert result == "extracted text"
    # 验证请求构造
    req = mock_open.call_args.args[0]
    assert req.full_url == "https://api.x.com/v1/chat/completions"
    assert req.method == "POST"
    assert req.headers["Authorization"] == "Bearer key"
    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == "gpt-4o"
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 4096


def test_call_api_urlopen_exception_raises_runtime_error() -> None:
    extractor = VisualExtractor(api_key="k")
    with (
        patch("web_crawler.parser.visual.urlopen", side_effect=ConnectionError("net down")),
        pytest.raises(RuntimeError, match="VLM API call failed"),
    ):
        extractor._call_api([{"role": "user", "content": "hi"}], 0.3)


def test_call_api_no_choices_with_error_message_raises_runtime_error() -> None:
    extractor = VisualExtractor(api_key="k")
    payload = {"choices": [], "error": {"message": "rate limited"}}
    with (
        patch("web_crawler.parser.visual.urlopen", return_value=_make_urlopen_response(payload)),
        pytest.raises(RuntimeError, match="rate limited"),
    ):
        extractor._call_api([{"role": "user", "content": "hi"}], 0.3)


def test_call_api_no_choices_without_error_key_uses_unknown_error() -> None:
    extractor = VisualExtractor(api_key="k")
    payload = {"choices": []}
    with (
        patch("web_crawler.parser.visual.urlopen", return_value=_make_urlopen_response(payload)),
        pytest.raises(RuntimeError, match="unknown error"),
    ):
        extractor._call_api([{"role": "user", "content": "hi"}], 0.3)


def test_call_api_null_content_raises_with_finish_reason() -> None:
    """模型返回 null content（拒绝时）应报 finish_reason。"""
    extractor = VisualExtractor(api_key="k")
    payload = {"choices": [{"message": {"content": None}, "finish_reason": "content_filter"}]}
    with (
        patch("web_crawler.parser.visual.urlopen", return_value=_make_urlopen_response(payload)),
        pytest.raises(RuntimeError, match="finish_reason=content_filter"),
    ):
        extractor._call_api([{"role": "user", "content": "hi"}], 0.3)


def test_call_api_empty_string_content_returns_empty() -> None:
    """content 为空字符串时 strip 后返回空串（不抛错）。"""
    extractor = VisualExtractor(api_key="k")
    payload = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
    with patch("web_crawler.parser.visual.urlopen", return_value=_make_urlopen_response(payload)):
        result = extractor._call_api([{"role": "user", "content": "hi"}], 0.3)
    assert result == ""


# ---------------------------------------------------------------------------
# extract_with_client：OpenAI client 路径
# ---------------------------------------------------------------------------


def _make_openai_response(content: str | None) -> MagicMock:
    """构造 OpenAI client 的响应对象。"""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    return response


def test_extract_with_client_none_falls_back_to_extract() -> None:
    extractor = VisualExtractor(api_key="k")
    with patch.object(extractor, "extract", return_value="fallback") as mock_extract:
        result = extractor.extract_with_client([{"b64": "A"}], "p", client=None)
    assert result == "fallback"
    mock_extract.assert_called_once()


def test_extract_with_client_empty_tiles_raises_value_error() -> None:
    extractor = VisualExtractor(api_key="k")
    client = MagicMock()
    with pytest.raises(ValueError, match="tiles must not be empty"):
        extractor.extract_with_client([], "p", client=client)


def test_extract_with_client_success_returns_content() -> None:
    extractor = VisualExtractor(api_key="k", model="gpt-4o")
    client = MagicMock()
    client.chat.completions.create.return_value = _make_openai_response("  result  ")

    result = extractor.extract_with_client([{"b64": "A"}], "p", client=client)

    assert result == "result"
    create_kwargs = client.chat.completions.create.call_args.kwargs
    assert create_kwargs["model"] == "gpt-4o"
    assert create_kwargs["temperature"] == 0.3
    assert create_kwargs["max_tokens"] == 4096
    # 验证 messages 结构
    messages = create_kwargs["messages"]
    assert messages[0]["role"] == "user"


def test_extract_with_client_multiple_tiles_annotates_sections() -> None:
    extractor = VisualExtractor(api_key="k")
    client = MagicMock()
    client.chat.completions.create.return_value = _make_openai_response("ok")

    extractor.extract_with_client([{"b64": "A"}, {"b64": "B"}], "p", client=client)

    messages = client.chat.completions.create.call_args.kwargs["messages"]
    content = messages[0]["content"]
    section_texts = [
        c for c in content if c.get("type") == "text" and "Page section" in c.get("text", "")
    ]
    assert len(section_texts) == 2


def test_extract_with_client_exception_raises_runtime_error() -> None:
    extractor = VisualExtractor(api_key="k")
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("timeout")

    with pytest.raises(RuntimeError, match="VLM client call failed"):
        extractor.extract_with_client([{"b64": "A"}], "p", client=client)


def test_extract_with_client_null_content_raises_runtime_error() -> None:
    extractor = VisualExtractor(api_key="k")
    client = MagicMock()
    client.chat.completions.create.return_value = _make_openai_response(None)

    with pytest.raises(RuntimeError, match="VLM returned empty content"):
        extractor.extract_with_client([{"b64": "A"}], "p", client=client)


def test_extract_with_client_passes_temperature_and_max_tiles() -> None:
    extractor = VisualExtractor(api_key="k")
    client = MagicMock()
    client.chat.completions.create.return_value = _make_openai_response("ok")

    tiles = [{"b64": str(i)} for i in range(10)]
    with patch.object(extractor, "extract") as mock_extract:
        extractor.extract_with_client(tiles, "p", client=client, temperature=0.9, max_tiles=3)

    create_kwargs = client.chat.completions.create.call_args.kwargs
    assert create_kwargs["temperature"] == 0.9
    # 只发送了 3 个瓦片
    content = create_kwargs["messages"][0]["content"]
    image_count = sum(1 for c in content if c.get("type") == "image_url")
    assert image_count == 3
    mock_extract.assert_not_called()


# ---------------------------------------------------------------------------
# 端到端：extract → 真实 _call_api → mock urlopen
# ---------------------------------------------------------------------------


def test_extract_end_to_end_with_mocked_urlopen() -> None:
    """完整流程：extract 构造 messages，_call_api 通过 mock urlopen 返回。"""
    extractor = VisualExtractor(api_key="k", base_url="https://api.x.com/v1", model="qwen-vl-max")
    payload = {"choices": [{"message": {"content": "标题：示例"}, "finish_reason": "stop"}]}
    with patch("web_crawler.parser.visual.urlopen", return_value=_make_urlopen_response(payload)):
        result = extractor.extract([{"b64": "PNGDATA"}], "提取标题")

    assert result == "标题：示例"

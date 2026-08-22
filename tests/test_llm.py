"""Tests for the LLM layer: provider registry and message normalization."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from web_crawler import (
    DeepSeekProvider,
    LLMMessage,
    LLMProvider,
    LLMResponse,
    OpenAICompatibleProvider,
    available_providers,
    get_provider,
    register_provider,
)
from web_crawler.ai import llm as llm_mod
from web_crawler.ai.llm import (
    DEFAULT_MODEL,
    AnthropicProvider,
    OpenAIProvider,
    ProviderCapabilities,
    QwenProvider,
    _normalize_messages,
    select_provider,
)


class FakeProvider:
    """Deterministic provider that replays canned JSON replies (no HTTP)."""

    model = "fake-model"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append(_normalize_messages(messages))
        content = self._replies.pop(0) if self._replies else "{}"
        return LLMResponse(content=content, model=self.model)


# ---------------------------------------------------------------------------
# _normalize_messages / LLMMessage / LLMResponse
# ---------------------------------------------------------------------------


def test_normalize_messages_accepts_str_dict_and_message() -> None:
    out = _normalize_messages([LLMMessage("system", "s"), {"role": "user", "content": "u"}, "bare"])
    assert out == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "user", "content": "bare"},
    ]
    assert _normalize_messages("hello") == [{"role": "user", "content": "hello"}]


def test_llm_message_text_classmethod() -> None:
    """LLMMessage.text 便捷构造纯文本消息。"""
    msg = LLMMessage.text("user", "hello world")
    assert msg.role == "user"
    assert msg.content == "hello world"
    assert msg.to_dict() == {"role": "user", "content": "hello world"}


def test_llm_message_vision_classmethod() -> None:
    """LLMMessage.vision 构造带图片的多模态消息。"""
    msg = LLMMessage.vision(
        "user",
        "describe this",
        "aGVsbG8=",  # base64 of "hello"
        mime="image/jpeg",
        detail="high",
    )
    assert msg.role == "user"
    assert isinstance(msg.content, list)
    assert msg.content[0] == {"type": "text", "text": "describe this"}
    img_block = msg.content[1]
    assert img_block["type"] == "image_url"
    assert img_block["image_url"]["url"] == "data:image/jpeg;base64,aGVsbG8="
    assert img_block["image_url"]["detail"] == "high"
    # to_dict 透传多模态结构
    dumped = msg.to_dict()
    assert dumped["role"] == "user"
    assert isinstance(dumped["content"], list)


def test_llm_response_text_property_and_str() -> None:
    """LLMResponse.text property 返回 content。"""
    resp = LLMResponse(content="hi there", model="m", finish_reason="stop", usage={"x": 1})
    assert resp.text == "hi there"
    assert str(resp) == "hi there"
    assert resp.usage == {"x": 1}


# ---------------------------------------------------------------------------
# _load_dotenv_once
# ---------------------------------------------------------------------------


def test_load_dotenv_once_loads_env_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_load_dotenv_once 应从 .env 读取键值并不覆盖已有变量。"""
    # 准备一个临时 .env 文件
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# comment line\nLLM_TEST_KEY="secret_value"\nLLM_TEST_EMPTY=\nLLM_TEST_NOQUOTE=plain\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_TEST_KEY", raising=False)
    monkeypatch.delenv("LLM_TEST_EMPTY", raising=False)
    monkeypatch.delenv("LLM_TEST_NOQUOTE", raising=False)
    # 重置进程级 sentinel，强制重新加载
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", False)

    llm_mod._load_dotenv_once()

    assert os.environ.get("LLM_TEST_KEY") == "secret_value"
    assert os.environ.get("LLM_TEST_NOQUOTE") == "plain"
    # 空值也会被 setdefault 写入
    assert os.environ.get("LLM_TEST_EMPTY") == ""

    # 第二次调用应短路返回（已加载）
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    # 不会抛错
    llm_mod._load_dotenv_once()


def test_load_dotenv_once_skips_when_no_env_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 .env 文件时应静默返回。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", False)
    # 不应抛异常
    llm_mod._load_dotenv_once()


def test_load_dotenv_once_does_not_overwrite_existing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已存在的环境变量不应被 .env 覆盖。"""
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_EXISTING_KEY=from_file\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_EXISTING_KEY", "from_env")
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", False)

    llm_mod._load_dotenv_once()
    assert os.environ["LLM_EXISTING_KEY"] == "from_env"


# ---------------------------------------------------------------------------
# OpenAICompatibleProvider 基础方法
# ---------------------------------------------------------------------------


def test_openai_compatible_provider_init_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """_headers / _endpoint / capabilities 覆盖路径。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    caps = ProviderCapabilities(vision=True, json_mode=True)
    provider = OpenAICompatibleProvider(
        model="m1",
        api_key="key-abc",
        base_url="https://api.example.com/v1/",
        timeout=42.0,
        api_key_env="MY_KEY_ENV",
        default_headers={"X-Custom": "yes"},
        capabilities=caps,
    )
    # base_url 末尾斜杠应被去除
    assert provider.base_url == "https://api.example.com/v1"
    assert provider.timeout == 42.0
    assert provider.api_key_env == "MY_KEY_ENV"
    assert provider.api_key == "key-abc"
    # 实例级 capabilities 覆盖
    assert provider.capabilities is caps
    # _headers 包含 Authorization 与自定义头
    headers = provider._headers()
    assert headers["Authorization"] == "Bearer key-abc"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Custom"] == "yes"
    # _endpoint
    assert provider._endpoint() == "https://api.example.com/v1/chat/completions"


def test_openai_compatible_provider_headers_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 api_key 时 _headers 不应包含 Authorization。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    provider = OpenAICompatibleProvider(api_key="", api_key_env="LLM_API_KEY")
    headers = provider._headers()
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"


def test_openai_compatible_provider_parse_static() -> None:
    """_parse 静态方法从 choices/usage 中构造 LLMResponse。"""
    data = {
        "model": "real-model",
        "choices": [
            {"message": {"content": "hello"}, "finish_reason": "stop"},
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    resp = OpenAICompatibleProvider._parse(data, "fallback-model")
    assert resp.content == "hello"
    assert resp.model == "real-model"
    assert resp.finish_reason == "stop"
    assert resp.usage == {"prompt_tokens": 5, "completion_tokens": 3}
    assert resp.raw is data


def test_openai_compatible_provider_parse_handles_empty_choices() -> None:
    """空 choices / 缺失字段时 _parse 应给出默认值。"""
    resp = OpenAICompatibleProvider._parse({}, "fallback-model")
    assert resp.content == ""
    assert resp.model == "fallback-model"
    assert resp.finish_reason is None
    assert resp.usage == {}


# ---------------------------------------------------------------------------
# chat / achat / complete —— 用 mock httpx 避免真实 HTTP
# ---------------------------------------------------------------------------


def _mock_httpx_response(json_data: dict[str, Any]) -> MagicMock:
    """构造一个模拟 httpx.Response 的对象。"""
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


def test_chat_calls_httpx_and_parses_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """chat 应通过持久 httpx.Client 发送请求并解析响应。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k", model="m")
    fake_resp = _mock_httpx_response(
        {
            "model": "m",
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 1},
        }
    )
    fake_client = MagicMock()
    fake_client.post.return_value = fake_resp
    fake_httpx = MagicMock()
    fake_httpx.Client.return_value = fake_client
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)

    result = provider.chat(
        "hello",
        temperature=0.5,
        max_tokens=128,
        response_format={"type": "json_object"},
        extra_arg="x",
    )
    assert result.content == "hi"
    assert result.model == "m"
    # 验证持久客户端被创建并使用
    fake_client.post.assert_called_once()
    _, kwargs = fake_client.post.call_args
    payload = kwargs["json"]
    assert payload["model"] == "m"
    assert payload["temperature"] == 0.5
    assert payload["max_tokens"] == 128
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["extra_arg"] == "x"


def test_chat_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """raise_for_status 抛错时应向上传播。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k")
    fake_resp = MagicMock()
    fake_resp.raise_for_status.side_effect = RuntimeError("http 500")
    fake_client = MagicMock()
    fake_client.post.return_value = fake_resp
    fake_httpx = MagicMock()
    fake_httpx.Client.return_value = fake_client
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)

    with pytest.raises(RuntimeError, match="http 500"):
        provider.chat("hi")


@pytest.mark.asyncio
async def test_achat_calls_async_client_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """achat 应使用 httpx.AsyncClient 并解析响应。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k", model="async-model")

    fake_resp = _mock_httpx_response(
        {
            "model": "async-model",
            "choices": [{"message": {"content": "async hi"}, "finish_reason": "stop"}],
        }
    )

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> Any:
            self.post_kwargs = kwargs
            return fake_resp

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeAsyncClient
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)

    result = await provider.achat(
        "hello",
        temperature=0.7,
        max_tokens=64,
        response_format={"type": "text"},
        extra="y",
    )
    assert result.content == "async hi"
    assert result.model == "async-model"
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_achat_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """无 api_key 时 achat 应抛 RuntimeError。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    provider = OpenAICompatibleProvider(api_key="", api_key_env="LLM_API_KEY")
    with pytest.raises(RuntimeError, match="no API key"):
        await provider.achat("hi")


def test_complete_helper_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """complete 便捷方法应只返回 assistant 文本。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k", model="m")
    fake_resp = _mock_httpx_response(
        {
            "model": "m",
            "choices": [{"message": {"content": "only text"}, "finish_reason": "stop"}],
        }
    )
    fake_client = MagicMock()
    fake_client.post.return_value = fake_resp
    fake_httpx = MagicMock()
    fake_httpx.Client.return_value = fake_client
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)

    out = provider.complete("hello", system="be brief", temperature=0.1)
    assert out == "only text"
    _, kwargs = fake_client.post.call_args
    msgs = kwargs["json"]["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "be brief"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "hello"


def test_complete_without_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """system=None 时 complete 只发送 user 消息。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k", model="m")
    fake_resp = _mock_httpx_response({"choices": [{"message": {"content": "ok"}}]})
    fake_client = MagicMock()
    fake_client.post.return_value = fake_resp
    fake_httpx = MagicMock()
    fake_httpx.Client.return_value = fake_client
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)

    out = provider.complete("hello")
    assert out == "ok"
    _, kwargs = fake_client.post.call_args
    msgs = kwargs["json"]["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"


# ---------------------------------------------------------------------------
# Provider 预置子类
# ---------------------------------------------------------------------------


def test_deepseek_provider_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeekProvider 默认模型与 api_key_env。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    p = DeepSeekProvider(api_key="k")
    assert p.model == DEFAULT_MODEL
    assert p.api_key_env == "DEEPSEEK_API_KEY"
    assert p.base_url == llm_mod.DEEPSEEK_BASE_URL
    assert p.name == "deepseek"
    # 默认不支持 vision
    assert p.capabilities.vision is False
    assert p.capabilities.json_mode is True
    assert p.capabilities.streaming is True


def test_deepseek_provider_vision_model_enables_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型名含 vision 时应启用 vision 能力。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    p = DeepSeekProvider(model="deepseek-vision-v1", api_key="k")
    assert p.capabilities.vision is True
    # known_models 应保持原样
    assert "deepseek-v4-pro" in p.capabilities.known_models


def test_openai_provider_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAIProvider 预置：gpt-4o / OPENAI_API_KEY / vision+tools。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    p = OpenAIProvider(api_key="k")
    assert p.model == "gpt-4o"
    assert p.api_key_env == "OPENAI_API_KEY"
    assert p.base_url == "https://api.openai.com/v1"
    assert p.name == "openai"
    assert p.capabilities.vision is True
    assert p.capabilities.tools is True
    assert p.capabilities.json_mode is True


def test_anthropic_provider_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """AnthropicProvider 预置：claude-sonnet-4-5 / ANTHROPIC_API_KEY。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    p = AnthropicProvider(api_key="k")
    assert p.model == "claude-sonnet-4-5"
    assert p.api_key_env == "ANTHROPIC_API_KEY"
    assert p.name == "anthropic"
    assert p.capabilities.vision is True
    # Anthropic 不支持 json_mode
    assert p.capabilities.json_mode is False
    assert p.capabilities.tools is True


def test_qwen_provider_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """QwenProvider 预置：qwen-vl-max / DASHSCOPE_API_KEY。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    p = QwenProvider(api_key="k")
    assert p.model == "qwen-vl-max"
    assert p.api_key_env == "DASHSCOPE_API_KEY"
    assert p.name == "qwen"
    assert p.capabilities.vision is True
    assert p.capabilities.json_mode is True
    assert p.capabilities.tools is False


# ---------------------------------------------------------------------------
# 注册表 / get_provider / select_provider
# ---------------------------------------------------------------------------


def test_get_provider_defaults_to_deepseek_v4_pro() -> None:
    provider = get_provider(api_key="dummy")
    assert isinstance(provider, DeepSeekProvider)
    assert provider.model == "deepseek-v4-pro"
    assert "deepseek" in available_providers()


def test_deepseek_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """无 api_key 时 chat 应抛 RuntimeError（保持测试 hermetic）。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = DeepSeekProvider(api_key="")
    with pytest.raises(RuntimeError, match="no API key"):
        provider.chat("hi")


def test_get_provider_unknown_raises() -> None:
    """未注册的 provider 名应抛 ValueError。"""
    with pytest.raises(ValueError, match="unknown LLM provider"):
        get_provider("nonexistent_provider_xyz")


def test_get_provider_case_insensitive() -> None:
    """provider 名大小写不敏感。"""
    p = get_provider("DeepSeek", api_key="dummy")
    assert isinstance(p, DeepSeekProvider)


def test_register_custom_provider() -> None:
    register_provider("fakereg", lambda **kw: FakeProvider(["{}"]))
    assert "fakereg" in available_providers()
    assert isinstance(get_provider("fakereg"), FakeProvider)


def test_available_providers_contains_all_builtins() -> None:
    names = available_providers()
    for n in ("anthropic", "deepseek", "openai", "openai-compatible", "qwen"):
        assert n in names


def test_select_provider_prefer_satisfied(monkeypatch: pytest.MonkeyPatch) -> None:
    """prefer 指定且能力满足时直接返回该 provider。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    p = select_provider(prefer="openai", api_key="k")
    assert isinstance(p, OpenAIProvider)
    assert p.capabilities.vision is True


def test_select_provider_demands_vision_picks_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    """要求 vision 时应选择支持 vision 的 provider。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    p = select_provider(vision=True, api_key="k")
    assert p.capabilities.vision is True


def test_select_provider_demands_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """要求 json_mode 时应选择支持 json_mode 的 provider。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    p = select_provider(json_mode=True, api_key="k")
    assert p.capabilities.json_mode is True


def test_select_provider_demands_tools_and_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    """同时要求 tools + streaming。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    p = select_provider(tools=True, streaming=True, api_key="k")
    assert p.capabilities.tools is True
    assert p.capabilities.streaming is True


def test_select_provider_no_match_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有 provider 都不满足时抛 ValueError（覆盖 failures 分支）。"""

    class _NoCapProvider:
        model = "x"
        capabilities = ProviderCapabilities()  # 所有能力都为 False

        def __init__(self, **kw: Any) -> None: ...

    # 临时替换注册表为只含不满足能力的 provider，强制触发 raise
    monkeypatch.setattr(llm_mod, "_PROVIDERS", {"nocap_only": _NoCapProvider})
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    with pytest.raises(ValueError, match="no registered provider satisfies all demands"):
        select_provider(vision=True, api_key="k")


def test_select_provider_no_match_records_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """failures 列表应记录每个不满足 provider 的缺失能力。"""

    class _PartialProvider:
        model = "x"
        capabilities = ProviderCapabilities(vision=False, json_mode=True)

        def __init__(self, **kw: Any) -> None: ...

    monkeypatch.setattr(llm_mod, "_PROVIDERS", {"partial_only": _PartialProvider})
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    with pytest.raises(ValueError) as exc_info:
        select_provider(vision=True, json_mode=False, api_key="k")
    # 错误信息应包含 failures 记录
    assert "missing" in str(exc_info.value)
    assert "vision" in str(exc_info.value)


def test_select_provider_skips_unknown_prefer(monkeypatch: pytest.MonkeyPatch) -> None:
    """prefer 指向不存在的 provider 时应跳过并继续。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    p = select_provider(prefer="not_registered_xyz", api_key="k")
    # 应该回退到其他 provider
    assert p is not None


def test_select_provider_skips_constructor_typeerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """构造函数参数不匹配（TypeError）时应跳过该 provider。"""

    class _StrictProvider:
        model = "strict"
        capabilities = ProviderCapabilities(vision=True)

        def __init__(self, *, required_arg: str) -> None:  # 不接受 api_key
            self.required_arg = required_arg

    # 把 _StrictProvider 放在第一位，确保它会被检查到
    monkeypatch.setattr(
        llm_mod,
        "_PROVIDERS",
        {"strict_first": _StrictProvider, "openai": OpenAIProvider},
    )
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    # _StrictProvider(api_key="k") -> TypeError -> 跳过，回退到 OpenAIProvider
    p = select_provider(vision=True, api_key="k")
    assert isinstance(p, OpenAIProvider)
    assert p.capabilities.vision is True


def test_select_provider_skips_provider_without_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 capabilities 属性的 provider 应被跳过。"""

    class _NoCapsProvider:
        model = "nocaps"

        def __init__(self, **kw: Any) -> None: ...

    # 把 _NoCapsProvider 放在第一位，确保它会被检查到
    monkeypatch.setattr(
        llm_mod,
        "_PROVIDERS",
        {"nocaps_first": _NoCapsProvider, "openai": OpenAIProvider},
    )
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    p = select_provider(api_key="k")
    # 应跳过 _NoCapsProvider（caps is None），回退到 OpenAIProvider
    assert isinstance(p, OpenAIProvider)
    assert hasattr(p, "capabilities")


def test_llm_provider_protocol_runtime_checkable() -> None:
    """LLMProvider Protocol 应是 runtime_checkable。"""
    # OpenAICompatibleProvider 有 chat/model/capabilities -> 应被识别为 LLMProvider
    real = OpenAICompatibleProvider(api_key="k")
    # Protocol 运行时检查只验证方法存在性，不验证属性
    assert isinstance(real, LLMProvider)


# ===========================================================================
# 回归：缺 role 的 dict / 多模态 content 列表归一化
# ===========================================================================


def test_normalize_messages_dict_missing_role_defaults_user() -> None:
    """缺 role 键的 dict 应兜底为 user 而不是 KeyError。"""
    out = _normalize_messages([{"content": "bare content"}])
    assert out == [{"role": "user", "content": "bare content"}]


def test_parse_handles_list_content() -> None:
    """content 为多模态片段数组时应归一化为纯文本。"""
    data = {
        "model": "m",
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "a"},
                        {"type": "text", "text": "b"},
                    ]
                }
            }
        ],
    }
    resp = OpenAICompatibleProvider._parse(data, "m")
    assert resp.content == "ab"


def test_anthropic_provider_endpoint_matches_openai_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AnthropicProvider 端点应与 docstring 声明的 OpenAI 兼容路径一致。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    p = AnthropicProvider(api_key="k")
    assert p._endpoint() == "https://api.anthropic.com/v1/openai/v1/chat/completions"


# ===========================================================================
# 回归：chat/achat 指数退避重试（429/5xx/网络错误）
# ===========================================================================


def _retryable_http_error(status: int) -> httpx.HTTPStatusError:
    """构造指定状态码的 httpx.HTTPStatusError（需真实 httpx 类型）。"""
    req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"http {status}", request=req, response=resp)


def test_chat_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 触发指数退避重试，重试后成功。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k", model="m")
    ok_resp = _mock_httpx_response({"model": "m", "choices": [{"message": {"content": "hi"}}]})
    fake_client = MagicMock()
    fake_client.post.side_effect = [_retryable_http_error(429), _retryable_http_error(429), ok_resp]
    provider._client = fake_client
    with patch("web_crawler.ai.llm.time.sleep") as mock_sleep:
        result = provider.chat("hello")
    assert result.content == "hi"
    assert fake_client.post.call_count == 3
    assert mock_sleep.call_count == 2


def test_chat_raises_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试超过上限后抛出原始异常。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k")
    fake_client = MagicMock()
    fake_client.post.side_effect = _retryable_http_error(503)
    provider._client = fake_client
    with (
        patch("web_crawler.ai.llm.time.sleep"),
        pytest.raises(httpx.HTTPStatusError),
    ):
        provider.chat("hi")
    # 1 次初始 + 3 次重试 = 4 次请求
    assert fake_client.post.call_count == 4


def test_chat_does_not_retry_non_retryable_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """400 等不可重试状态码立即抛出，不重试。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k")
    fake_client = MagicMock()
    fake_client.post.side_effect = _retryable_http_error(400)
    provider._client = fake_client
    with (
        patch("web_crawler.ai.llm.time.sleep") as mock_sleep,
        pytest.raises(httpx.HTTPStatusError),
    ):
        provider.chat("hi")
    assert fake_client.post.call_count == 1
    assert mock_sleep.call_count == 0


def test_chat_retries_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络错误（TransportError）同样重试。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k")
    ok_resp = _mock_httpx_response({"choices": [{"message": {"content": "ok"}}]})
    fake_client = MagicMock()
    fake_client.post.side_effect = [httpx.ConnectError("conn refused"), ok_resp]
    provider._client = fake_client
    with patch("web_crawler.ai.llm.time.sleep"):
        result = provider.chat("hi")
    assert result.content == "ok"
    assert fake_client.post.call_count == 2


@pytest.mark.asyncio
async def test_achat_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步路径 429 重试后成功。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k")
    ok_resp = _mock_httpx_response({"choices": [{"message": {"content": "async ok"}}]})
    fake_client = MagicMock()
    fake_client.post = AsyncMock(side_effect=[_retryable_http_error(429), ok_resp])
    provider._async_client = fake_client
    with patch("web_crawler.ai.llm.asyncio.sleep"):
        result = await provider.achat("hi")
    assert result.content == "async ok"
    assert fake_client.post.call_count == 2


@pytest.mark.asyncio
async def test_achat_retries_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步路径网络错误（无 HTTP 响应上下文）同样触发重试。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k")
    ok_resp = _mock_httpx_response({"choices": [{"message": {"content": "ok"}}]})
    fake_client = MagicMock()
    fake_client.post = AsyncMock(side_effect=[httpx.ConnectError("conn refused"), ok_resp])
    provider._async_client = fake_client
    with patch("web_crawler.ai.llm.asyncio.sleep"):
        result = await provider.achat("hi")
    assert result.content == "ok"
    assert fake_client.post.call_count == 2


@pytest.mark.asyncio
async def test_achat_does_not_retry_non_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异步路径非传输类异常（无 response 上下文）立即抛出，不重试。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k")
    fake_client = MagicMock()
    fake_client.post = AsyncMock(side_effect=RuntimeError("boom"))
    provider._async_client = fake_client
    with (
        patch("web_crawler.ai.llm.asyncio.sleep") as mock_sleep,
        pytest.raises(RuntimeError, match="boom"),
    ):
        await provider.achat("hi")
    assert fake_client.post.call_count == 1
    assert mock_sleep.call_count == 0


# ===========================================================================
# 回归：close / aclose 生命周期
# ===========================================================================


def test_close_closes_client_and_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    """close() 关闭持久客户端并置空。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k")
    fake_client = MagicMock()
    provider._client = fake_client
    provider.close()
    fake_client.close.assert_called_once()
    assert provider._client is None


def test_close_swallows_client_close_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """客户端 close 抛错时应静默吞掉。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k")
    fake_client = MagicMock()
    fake_client.close.side_effect = RuntimeError("boom")
    provider._client = fake_client
    provider.close()  # 不抛错
    assert provider._client is None


@pytest.mark.asyncio
async def test_aclose_closes_async_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """aclose() 关闭异步客户端并置空。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k")
    fake_client = MagicMock()
    fake_client.aclose = AsyncMock()
    provider._async_client = fake_client
    await provider.aclose()
    fake_client.aclose.assert_awaited_once()
    assert provider._async_client is None


@pytest.mark.asyncio
async def test_aclose_swallows_client_aclose_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异步客户端 aclose 抛错时应静默吞掉并置空。"""
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", True)
    provider = OpenAICompatibleProvider(api_key="k")
    fake_client = MagicMock()
    fake_client.aclose = AsyncMock(side_effect=RuntimeError("boom"))
    provider._async_client = fake_client
    await provider.aclose()  # 不抛错
    assert provider._async_client is None


# ===========================================================================
# 回归：.env 搜索范围限制在 cwd + 有限级祖先目录
# ===========================================================================


def test_load_dotenv_once_scoped_search(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd 无 .env 时，应只搜模块文件有限级祖先目录内的 .env。"""
    env_file = tmp_path / "a" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("LLM_SCOPE_KEY=scoped\n", encoding="utf-8")
    # 模块文件位于 a/b/c/llm.py：a/ 在 parents[:4] 内
    monkeypatch.setattr(llm_mod, "__file__", str(tmp_path / "a" / "b" / "c" / "llm.py"))
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", False)
    monkeypatch.delenv("LLM_SCOPE_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # cwd 无 .env

    llm_mod._load_dotenv_once()

    assert os.environ.get("LLM_SCOPE_KEY") == "scoped"


def test_load_dotenv_once_does_not_search_too_far(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过级数上限的祖先目录（如盘符根/主目录）不应被搜索。"""
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_FAR_KEY=should_not_load\n", encoding="utf-8")
    # 模块文件位于 x/y/z/w/llm.py：tmp_path 是 parents[4]，超出 [:4] 范围
    deep = tmp_path / "x" / "y" / "z" / "w"
    deep.mkdir(parents=True)
    monkeypatch.setattr(llm_mod, "__file__", str(deep / "llm.py"))
    monkeypatch.setattr(llm_mod, "_DOTENV_LOADED", False)
    monkeypatch.delenv("LLM_FAR_KEY", raising=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    llm_mod._load_dotenv_once()

    assert "LLM_FAR_KEY" not in os.environ

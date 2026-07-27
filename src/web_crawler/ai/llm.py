"""Pluggable LLM provider layer.

A thin, dependency-light abstraction over OpenAI-compatible chat-completions
endpoints so the rest of the library can talk to a model without hard-coding a
vendor. The default provider targets **DeepSeek-V4-Pro** via DeepSeek's
OpenAI-compatible ``/chat/completions`` API.

Design notes
------------
- Only depends on ``httpx`` (already a core dependency); no extra install.
- Providers are looked up through a small registry so new backends can be
  registered with :func:`register_provider` without touching call sites.
- ``httpx`` is imported lazily inside :meth:`OpenAICompatibleProvider.chat` to
  keep ``import web_crawler`` cheap, matching the library's lazy-import style.
- Each provider exposes a :class:`ProviderCapabilities` snapshot so callers
  can negotiate features (vision / json_mode / tools / streaming) without
  try/except'ing the model name.

Quick start
-----------
>>> from web_crawler import get_provider, LLMMessage
>>> llm = get_provider("deepseek", api_key="sk-...")   # or DEEPSEEK_API_KEY env
>>> reply = llm.complete("Say hi in one word.")
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# 默认模型与端点：DeepSeek 的 OpenAI 兼容接口。
# 注意：模型 ID 大小写敏感，DeepSeek API 的合法值为小写 "deepseek-v4-pro"。
DEFAULT_MODEL = "deepseek-v4-pro"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

# .env 只加载一次（进程级）
_DOTENV_LOADED = False


def _load_dotenv_once() -> None:
    """Best-effort, dependency-free ``.env`` loader.

    Looks for a ``.env`` file in the current working directory and then in the
    package's parent directories (project root), loading ``KEY=VALUE`` lines
    into :data:`os.environ` **without overwriting** already-set variables.
    Runs at most once per process; silently does nothing if no file is found.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    from pathlib import Path

    candidates = [Path.cwd(), *Path(__file__).resolve().parents]
    for base in candidates:
        env_path = base / ".env"
        if not env_path.is_file():
            continue
        try:
            text = env_path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable file
            return
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        return


# 允许作为 chat() 输入的消息形态
MessageLike = "str | LLMMessage | dict[str, str]"


@dataclass
class LLMMessage:
    """A single chat message (``role`` is one of system/user/assistant).

    ``content`` 可以是纯文本字符串，也可以是 OpenAI 多模态消息内容列表
    （``[{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {...}}]``），
    用于 Vision-LLM 场景。``to_dict`` 透传该结构。
    """

    role: str
    content: str | list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}

    @classmethod
    def text(cls, role: str, text: str) -> LLMMessage:
        """便捷构造纯文本消息。"""
        return cls(role=role, content=text)

    @classmethod
    def vision(
        cls,
        role: str,
        text: str,
        image_b64: str,
        *,
        mime: str = "image/png",
        detail: str = "auto",
    ) -> LLMMessage:
        """便捷构造带图片的多模态消息（OpenAI vision 格式）。

        ``image_b64`` 是不带 ``data:`` 前缀的 base64 字符串。
        """
        return cls(
            role=role,
            content=[
                {"type": "text", "text": text},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{image_b64}",
                        "detail": detail,
                    },
                },
            ],
        )


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """声明某 LLM 提供商支持的能力，供上层做能力协商。

    字段默认值都是"最保守假设"，新提供商按真实支持情况显式覆盖。
    """

    vision: bool = False
    json_mode: bool = False
    tools: bool = False
    streaming: bool = False
    max_output_tokens: int = 4096
    # 已知此 provider 支持的模型名前缀（用于自动协商），空列表表示不做前缀校验
    known_models: tuple[str, ...] = ()


@dataclass
class LLMResponse:
    """Normalized chat-completion result."""

    content: str
    model: str
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.content

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.content


@runtime_checkable
class LLMProvider(Protocol):
    """Structural type every provider satisfies."""

    model: str
    capabilities: ProviderCapabilities

    def chat(
        self,
        messages: Sequence[str | LLMMessage | dict[str, str]] | str,
        **kwargs: Any,
    ) -> LLMResponse: ...


def _normalize_messages(
    messages: Sequence[str | LLMMessage | dict[str, str]] | str,
) -> list[dict[str, Any]]:
    """Coerce assorted message forms into the OpenAI ``messages`` list.

    接受纯字符串（视为单条 user 消息）、``LLMMessage`` 实例（支持纯文本与
    多模态 vision 列表）、或原始 ``{"role", "content"}`` dict。``content``
    可以是字符串或 OpenAI 多模态内容数组。
    """
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, LLMMessage):
            out.append(m.to_dict())
        elif isinstance(m, dict):
            out.append({"role": m["role"], "content": m["content"]})
        elif isinstance(m, str):
            out.append({"role": "user", "content": m})
        else:  # pragma: no cover - defensive
            raise TypeError(f"unsupported message type: {type(m)!r}")
    return out


class OpenAICompatibleProvider:
    """Provider for any OpenAI-compatible ``/chat/completions`` endpoint.

    Parameters
    ----------
    model:
        Model name sent in the request body.
    api_key:
        Bearer token. Falls back to ``api_key_env`` environment variable.
    base_url:
        API root, e.g. ``https://api.deepseek.com/v1``.
    timeout:
        Per-request timeout in seconds.
    default_headers:
        Extra headers merged into every request.
    """

    name = "openai-compatible"

    # 子类可覆盖：默认按保守假设，能力都不支持
    capabilities: ProviderCapabilities = ProviderCapabilities()

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        base_url: str = DEEPSEEK_BASE_URL,
        timeout: float = 60.0,
        api_key_env: str = "LLM_API_KEY",
        default_headers: dict[str, str] | None = None,
        capabilities: ProviderCapabilities | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key_env = api_key_env
        if not api_key:
            _load_dotenv_once()
        self.api_key = api_key or os.environ.get(api_key_env, "")
        self.default_headers = default_headers or {}
        # 允许实例级覆盖类级 capabilities，便于按模型名动态协商
        if capabilities is not None:
            self.capabilities = capabilities

    # -- helpers ------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.default_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    @staticmethod
    def _parse(data: dict[str, Any], fallback_model: str) -> LLMResponse:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return LLMResponse(
            content=message.get("content", "") or "",
            model=data.get("model", fallback_model),
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage", {}) or {},
            raw=data,
        )

    # -- sync ---------------------------------------------------------------
    def chat(
        self,
        messages: Sequence[str | LLMMessage | dict[str, str]] | str,
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Call the chat-completions endpoint and return an :class:`LLMResponse`."""
        if not self.api_key:
            raise RuntimeError(
                f"no API key for provider {self.name!r}; pass api_key= or set "
                f"the {self.api_key_env} environment variable"
            )
        import httpx  # 延迟导入，保持顶层 import 轻量

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _normalize_messages(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        payload.update(kwargs)

        resp = httpx.post(
            self._endpoint(),
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return self._parse(resp.json(), self.model)

    async def achat(
        self,
        messages: Sequence[str | LLMMessage | dict[str, str]] | str,
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Async counterpart of :meth:`chat`."""
        if not self.api_key:
            raise RuntimeError(
                f"no API key for provider {self.name!r}; pass api_key= or set "
                f"the {self.api_key_env} environment variable"
            )
        import httpx

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _normalize_messages(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        payload.update(kwargs)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self._endpoint(), headers=self._headers(), json=payload)
            resp.raise_for_status()
            return self._parse(resp.json(), self.model)

    # -- convenience --------------------------------------------------------
    def complete(self, prompt: str, *, system: str | None = None, **kwargs: Any) -> str:
        """One-shot helper: return just the assistant text for ``prompt``."""
        messages: list[str | LLMMessage | dict[str, str]] = []
        if system:
            messages.append(LLMMessage("system", system))
        messages.append(LLMMessage("user", prompt))
        return self.chat(messages, **kwargs).content


class DeepSeekProvider(OpenAICompatibleProvider):
    """DeepSeek preset. Defaults to ``DeepSeek-V4-Pro`` and reads
    ``DEEPSEEK_API_KEY`` from the environment when no key is passed."""

    name = "deepseek"

    # DeepSeek-V4-Pro 支持 JSON 模式与流式输出；vision 由调用方按模型名
    # 通过 capabilities 覆盖（DeepSeek-Vision 系列）。
    capabilities = ProviderCapabilities(
        vision=False,
        json_mode=True,
        tools=False,
        streaming=True,
        max_output_tokens=8192,
        known_models=("deepseek-v4-pro", "deepseek-vision"),
    )

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        base_url: str = DEEPSEEK_BASE_URL,
        timeout: float = 60.0,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            api_key_env="DEEPSEEK_API_KEY",
            default_headers=default_headers,
        )
        # deepseek-vision-* 视为支持 vision
        if "vision" in model.lower():
            self.capabilities = ProviderCapabilities(
                vision=True,
                json_mode=True,
                tools=False,
                streaming=True,
                max_output_tokens=8192,
                known_models=self.capabilities.known_models,
            )


class OpenAIProvider(OpenAICompatibleProvider):
    """OpenAI 官方预置。默认模型 ``gpt-4o``，支持 vision / json_mode / tools。

    从 ``OPENAI_API_KEY`` 读密钥；如需指向 Azure 等兼容端点，传 ``base_url``。
    """

    name = "openai"

    capabilities = ProviderCapabilities(
        vision=True,
        json_mode=True,
        tools=True,
        streaming=True,
        max_output_tokens=16384,
        known_models=("gpt-4o", "gpt-4-turbo", "gpt-4-vision", "gpt-4.1", "o1", "o3"),
    )

    def __init__(
        self,
        *,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            api_key_env="OPENAI_API_KEY",
            default_headers=default_headers,
        )


class AnthropicProvider(OpenAICompatibleProvider):
    """Anthropic Claude 预置（通过 Anthropic 的 OpenAI 兼容端点）。

    默认模型 ``claude-sonnet-4-5``，支持 vision；从 ``ANTHROPIC_API_KEY``
    读密钥。Anthropic 也提供 OpenAI 兼容端点 ``/v1/openai/v1/chat/completions``，
    本预置走该路径以便复用 :class:`OpenAICompatibleProvider` 的实现。
    """

    name = "anthropic"

    capabilities = ProviderCapabilities(
        vision=True,
        json_mode=False,
        tools=True,
        streaming=True,
        max_output_tokens=8192,
        known_models=("claude-sonnet-4-5", "claude-opus-4", "claude-haiku-4"),
    )

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-5",
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com/v1",
        timeout: float = 60.0,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            api_key_env="ANTHROPIC_API_KEY",
            default_headers=default_headers,
        )


class QwenProvider(OpenAICompatibleProvider):
    """阿里通义千问 DashScope 兼容预置。

    默认模型 ``qwen-vl-max``（支持 vision），从 ``DASHSCOPE_API_KEY`` 读密钥。
    """

    name = "qwen"

    capabilities = ProviderCapabilities(
        vision=True,
        json_mode=True,
        tools=False,
        streaming=True,
        max_output_tokens=8192,
        known_models=("qwen-vl", "qwen-max", "qwen-plus", "qwen-turbo"),
    )

    def __init__(
        self,
        *,
        model: str = "qwen-vl-max",
        api_key: str | None = None,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        timeout: float = 60.0,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            api_key_env="DASHSCOPE_API_KEY",
            default_headers=default_headers,
        )


# -- registry ---------------------------------------------------------------
_PROVIDERS: dict[str, Callable[..., LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "deepseek": DeepSeekProvider,
    "openai": OpenAIProvider,
    "openai-compatible": OpenAICompatibleProvider,
    "qwen": QwenProvider,
}


def register_provider(name: str, factory: Callable[..., LLMProvider]) -> None:
    """Register a new provider factory under ``name`` (case-insensitive)."""
    _PROVIDERS[name.lower()] = factory


def available_providers() -> list[str]:
    """Return the registered provider names."""
    return sorted(_PROVIDERS)


def select_provider(
    *,
    vision: bool = False,
    json_mode: bool = False,
    tools: bool = False,
    streaming: bool = False,
    prefer: str | None = None,
    **kwargs: Any,
) -> LLMProvider:
    """按任务需求协商选择 LLM 提供商。

    优先级：
    1. ``prefer`` 指定的 provider 若已注册且满足全部需求 → 直接用；
    2. 否则在已注册 provider 中按 capabilities 过滤，第一个全满足的胜出；
    3. 都不满足时抛 ``ValueError``，附带哪些 provider 缺哪些能力。
    """
    demands = {
        "vision": vision,
        "json_mode": json_mode,
        "tools": tools,
        "streaming": streaming,
    }
    order: list[str] = []
    if prefer:
        order.append(prefer.lower())
    order.extend(p for p in _PROVIDERS if p != (prefer or "").lower())
    order = list(dict.fromkeys(order))  # 去重保序

    failures: list[str] = []
    for name in order:
        factory = _PROVIDERS.get(name)
        if factory is None:
            continue
        try:
            provider = factory(**kwargs)
        except TypeError:
            # 构造参数不匹配，跳过
            continue
        caps = getattr(provider, "capabilities", None)
        if caps is None:
            continue
        ok = all(getattr(caps, k) == v or (v is False) for k, v in demands.items())
        if ok:
            return provider
        missing = [k for k, v in demands.items() if v and not getattr(caps, k)]
        failures.append(f"{name}: missing {missing}")

    raise ValueError(
        f"no registered provider satisfies all demands; checked={order}, failures={failures}"
    )


def get_provider(name: str = "deepseek", **kwargs: Any) -> LLMProvider:
    """Instantiate a registered provider (default: DeepSeek / DeepSeek-V4-Pro).

    Extra keyword arguments (``model``, ``api_key``, ``base_url``, …) are
    forwarded to the provider constructor.
    """
    key = name.lower()
    if key not in _PROVIDERS:
        raise ValueError(f"unknown LLM provider {name!r}; available: {available_providers()}")
    return _PROVIDERS[key](**kwargs)


__all__ = [
    "DEEPSEEK_BASE_URL",
    "DEFAULT_MODEL",
    "AnthropicProvider",
    "DeepSeekProvider",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "ProviderCapabilities",
    "QwenProvider",
    "available_providers",
    "get_provider",
    "register_provider",
    "select_provider",
]

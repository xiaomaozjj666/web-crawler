"""可插拔的 LLM 供应商层。

一个轻量、低依赖的抽象，覆盖 OpenAI 兼容的 chat-completions 端点，
使库的其余部分无需硬编码某个厂商即可与模型对话。默认供应商通过
DeepSeek 的 OpenAI 兼容 ``/chat/completions`` API 指向
**DeepSeek-V4-Pro**。

设计说明
--------
- 仅依赖 ``httpx``（已是核心依赖），无需额外安装。
- 供应商通过小型注册表查找，新后端可用 :func:`register_provider` 注册，
  无需修改调用点。
- ``httpx`` 在 :meth:`OpenAICompatibleProvider.chat` 内部延迟导入，
  以保持 ``import web_crawler`` 轻量，与库的延迟导入风格一致。
- 每个供应商暴露一份 :class:`ProviderCapabilities` 快照，调用方可直接
  协商能力（vision / json_mode / tools / streaming），无需对模型名
  做 try/except。

快速上手
--------
>>> from web_crawler import get_provider, LLMMessage
>>> llm = get_provider("deepseek", api_key="sk-...")   # 或 DEEPSEEK_API_KEY 环境变量
>>> reply = llm.complete("Say hi in one word.")
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# 默认模型与端点：DeepSeek 的 OpenAI 兼容接口。
# 注意：模型 ID 大小写敏感，DeepSeek API 的合法值为小写 "deepseek-v4-pro"。
DEFAULT_MODEL = "deepseek-v4-pro"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

# .env 只加载一次（进程级）
_DOTENV_LOADED = False
# 除 cwd 外向上搜索 .env 的祖先目录级数上限：
# ai 目录 → 包目录 → src → 项目根。避免误加载主目录/盘符根等无关 .env。
_ENV_SEARCH_PARENT_DEPTH = 4

# LLM 调用重试：429 / 5xx / 网络错误走指数退避，最多重试 _MAX_LLM_RETRIES 次
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_LLM_RETRIES = 3
_MAX_BACKOFF_SECONDS = 30.0


def _is_httpx_transport_error(exc: BaseException) -> bool:
    """判定是否为 httpx 传输层错误（连接/超时等网络错误）。

    httpx 被 mock 成非类型对象（测试环境）时安全降级为 False。
    """
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx 是核心依赖
        return False
    try:
        return isinstance(exc, httpx.TransportError)
    except TypeError:  # pragma: no cover - httpx 被 mock 成非类型对象
        return False


def _load_dotenv_once() -> None:
    """尽力而为、零依赖的 ``.env`` 加载器。

    依次在当前工作目录、以及本模块至多 ``_ENV_SEARCH_PARENT_DEPTH`` 层
    祖先目录（即包根附近）查找 ``.env`` 文件，把 ``KEY=VALUE`` 行载入
    :data:`os.environ`，**不覆盖**已设置的变量。每个进程至多运行一次；
    找不到文件时静默跳过。
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    from pathlib import Path

    candidates = [
        Path.cwd(),
        *list(Path(__file__).resolve().parents)[:_ENV_SEARCH_PARENT_DEPTH],
    ]
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
    """单条聊天消息（``role`` 取 system/user/assistant 之一）。

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
    """归一化的 chat-completion 结果。"""

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
    """所有供应商都满足的结构化类型。"""

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
    """把多种消息形态统一转换为 OpenAI ``messages`` 列表。

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
            # 缺 role/content 的 dict 做兜底，避免 KeyError
            out.append({"role": m.get("role", "user"), "content": m.get("content", "")})
        elif isinstance(m, str):
            out.append({"role": "user", "content": m})
        else:  # pragma: no cover - defensive
            raise TypeError(f"unsupported message type: {type(m)!r}")
    return out


class OpenAICompatibleProvider:
    """面向任意 OpenAI 兼容 ``/chat/completions`` 端点的供应商。

    Parameters
    ----------
    model:
        请求体中发送的模型名。
    api_key:
        Bearer token。缺省时回退到 ``api_key_env`` 环境变量。
    base_url:
        API 根地址，例如 ``https://api.deepseek.com/v1``。
    timeout:
        单次请求超时（秒）。
    default_headers:
        合并进每个请求的额外 header。
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
        # 延迟初始化的持久连接（复用连接池，避免每次调用新建 TCP/TSL 握手）
        self._client: Any = None
        self._async_client: Any = None

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
        content = message.get("content", "") or ""
        if isinstance(content, list):
            # 多模态/兼容端点可能返回 content 片段数组，归一化为纯文本
            content = "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        return LLMResponse(
            content=content,
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
        """调用 chat-completions 端点并返回 :class:`LLMResponse`。"""
        if not self.api_key:
            raise RuntimeError(
                f"no API key for provider {self.name!r}; pass api_key= or set "
                f"the {self.api_key_env} environment variable"
            )
        import httpx  # 延迟导入：模块级 import 会拖慢 web_crawler 首包

        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)

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

        # 429/5xx/网络错误指数退避重试；其他状态码与不可重试错误直接抛出
        attempt = 0
        while True:
            try:
                resp = self._client.post(
                    self._endpoint(),
                    headers=self._headers(),
                    json=payload,
                )
                resp.raise_for_status()
                return self._parse(resp.json(), self.model)
            except Exception as exc:
                resp_obj = getattr(exc, "response", None)
                status = getattr(resp_obj, "status_code", None) if resp_obj is not None else None
                if status is not None:
                    retryable = status in _RETRYABLE_STATUS
                else:
                    # 无 HTTP 响应上下文：仅 httpx 传输层错误（连接/超时等）可重试
                    retryable = _is_httpx_transport_error(exc)
                if not retryable or attempt >= _MAX_LLM_RETRIES:
                    raise
                attempt += 1
                time.sleep(min(2.0**attempt, _MAX_BACKOFF_SECONDS))

    async def achat(
        self,
        messages: Sequence[str | LLMMessage | dict[str, str]] | str,
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """:meth:`chat` 的异步版本。"""
        if not self.api_key:
            raise RuntimeError(
                f"no API key for provider {self.name!r}; pass api_key= or set "
                f"the {self.api_key_env} environment variable"
            )
        import httpx  # 延迟导入：模块级 import 会拖慢 web_crawler 首包

        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=self.timeout)

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

        # 429/5xx/网络错误指数退避重试；其他状态码与不可重试错误直接抛出
        attempt = 0
        while True:
            try:
                resp = await self._async_client.post(
                    self._endpoint(), headers=self._headers(), json=payload
                )
                resp.raise_for_status()
                return self._parse(resp.json(), self.model)
            except Exception as exc:
                resp_obj = getattr(exc, "response", None)
                status = getattr(resp_obj, "status_code", None) if resp_obj is not None else None
                if status is not None:
                    retryable = status in _RETRYABLE_STATUS
                else:
                    # 无 HTTP 响应上下文：仅 httpx 传输层错误（连接/超时等）可重试
                    retryable = _is_httpx_transport_error(exc)
                if not retryable or attempt >= _MAX_LLM_RETRIES:
                    raise
                attempt += 1
                await asyncio.sleep(min(2.0**attempt, _MAX_BACKOFF_SECONDS))

    # -- convenience --------------------------------------------------------
    def complete(self, prompt: str, *, system: str | None = None, **kwargs: Any) -> str:
        """一次性辅助方法：仅返回 ``prompt`` 对应的助手文本。"""
        messages: list[str | LLMMessage | dict[str, str]] = []
        if system:
            messages.append(LLMMessage("system", system))
        messages.append(LLMMessage("user", prompt))
        return self.chat(messages, **kwargs).content

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        """关闭持久的同步 HTTP 客户端。"""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    async def aclose(self) -> None:
        """关闭持久的异步 HTTP 客户端。"""
        if self._async_client is not None:
            try:
                await self._async_client.aclose()
            except Exception:
                pass
            self._async_client = None


class DeepSeekProvider(OpenAICompatibleProvider):
    """DeepSeek 预置。默认 ``DeepSeek-V4-Pro``，未传密钥时从环境变量
    ``DEEPSEEK_API_KEY`` 读取。"""

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
    本预置的 ``base_url`` 对齐该路径以便复用 :class:`OpenAICompatibleProvider`
    的实现。
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
        base_url: str = "https://api.anthropic.com/v1/openai/v1",
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
    """以 ``name`` 注册新的供应商工厂（大小写不敏感）。"""
    _PROVIDERS[name.lower()] = factory


def available_providers() -> list[str]:
    """返回已注册的供应商名称列表。"""
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
    """实例化一个已注册的供应商（默认：DeepSeek / DeepSeek-V4-Pro）。

    其余关键字参数（``model``、``api_key``、``base_url`` 等）会透传给
    供应商构造器。
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

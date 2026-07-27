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
    """A single chat message (``role`` is one of system/user/assistant)."""

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


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

    def chat(
        self,
        messages: Sequence[str | LLMMessage | dict[str, str]] | str,
        **kwargs: Any,
    ) -> LLMResponse: ...


def _normalize_messages(
    messages: Sequence[str | LLMMessage | dict[str, str]] | str,
) -> list[dict[str, str]]:
    """Coerce assorted message forms into the OpenAI ``messages`` list.

    Accepts a bare string (treated as a single user turn), ``LLMMessage``
    instances, or raw ``{"role", "content"}`` dicts.
    """
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    out: list[dict[str, str]] = []
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

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        base_url: str = DEEPSEEK_BASE_URL,
        timeout: float = 60.0,
        api_key_env: str = "LLM_API_KEY",
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key_env = api_key_env
        if not api_key:
            _load_dotenv_once()
        self.api_key = api_key or os.environ.get(api_key_env, "")
        self.default_headers = default_headers or {}

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


# -- registry ---------------------------------------------------------------
_PROVIDERS: dict[str, Callable[..., LLMProvider]] = {
    "deepseek": DeepSeekProvider,
    "openai-compatible": OpenAICompatibleProvider,
}


def register_provider(name: str, factory: Callable[..., LLMProvider]) -> None:
    """Register a new provider factory under ``name`` (case-insensitive)."""
    _PROVIDERS[name.lower()] = factory


def available_providers() -> list[str]:
    """Return the registered provider names."""
    return sorted(_PROVIDERS)


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
    "DeepSeekProvider",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "OpenAICompatibleProvider",
    "available_providers",
    "get_provider",
    "register_provider",
]

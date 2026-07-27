"""AI-assisted, compliance-aware extraction layer.

This subpackage is purely additive on top of the core library:

- :mod:`~web_crawler.ai.llm` — pluggable LLM provider layer (default model
  ``DeepSeek-V4-Pro`` via DeepSeek's OpenAI-compatible API).
- :mod:`~web_crawler.ai.extractor` — LLM-generated CSS selectors validated
  against the project's own :class:`~web_crawler.parser.selector.Selector`,
  with self-healing.
- :mod:`~web_crawler.ai.agent` — polite orchestrator combining a fetcher with
  the extractor (robots.txt, rate limiting, 429/503 backoff).
- :mod:`~web_crawler.ai.hooks` — JS Hook 脚本库（fetch/XHR/cookie/crypto 拦截）。
- :mod:`~web_crawler.ai.analyzer` — AI webpack 混淆代码分析器。
- :mod:`~web_crawler.ai.captcha` — 验证码检测与处理（hCaptcha/Turnstile/极验）。
- :mod:`~web_crawler.ai.reverse_agent` — JS 逆向 Agent 主循环（观察-思考-行动）。
"""

from __future__ import annotations

from .agent import AIScrapeAgent, RobotsPolicy, ScrapeResult, detect_block
from .extractor import AIExtractor, ExtractionResult
from .llm import (
    DEFAULT_MODEL,
    DeepSeekProvider,
    LLMMessage,
    LLMProvider,
    LLMResponse,
    OpenAICompatibleProvider,
    available_providers,
    get_provider,
    register_provider,
)

__all__ = [
    # llm
    "DEFAULT_MODEL",
    # extractor
    "AIExtractor",
    # agent
    "AIScrapeAgent",
    "AnalysisResult",
    "CaptchaDetector",
    "CaptchaManager",
    "CaptchaSolver",
    "CaptchaType",
    "DeepSeekProvider",
    "ExtractionResult",
    # hooks / analyzer / captcha / reverse_agent — 懒加载，按需 import
    "HookLibrary",
    "JSAnalyzer",
    "JSFragment",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "OpenAICompatibleProvider",
    "ReverseAgent",
    "ReverseAgentConfig",
    "RobotsPolicy",
    "ScrapeResult",
    "available_providers",
    "collect_hook_data",
    "detect_block",
    "generate_combined_script",
    "get_provider",
    "register_provider",
]


def __getattr__(name: str):
    """懒加载逆向相关模块，避免 import 时强制依赖 camoufox/playwright。"""
    _lazy = {
        "HookLibrary": ("web_crawler.ai.hooks", "HookLibrary"),
        "generate_combined_script": ("web_crawler.ai.hooks", "generate_combined_script"),
        "collect_hook_data": ("web_crawler.ai.hooks", "collect_hook_data"),
        "JSAnalyzer": ("web_crawler.ai.analyzer", "JSAnalyzer"),
        "JSFragment": ("web_crawler.ai.analyzer", "JSFragment"),
        "AnalysisResult": ("web_crawler.ai.analyzer", "AnalysisResult"),
        "CaptchaType": ("web_crawler.ai.captcha", "CaptchaType"),
        "CaptchaDetector": ("web_crawler.ai.captcha", "CaptchaDetector"),
        "CaptchaSolver": ("web_crawler.ai.captcha", "CaptchaSolver"),
        "CaptchaManager": ("web_crawler.ai.captcha", "CaptchaManager"),
        "ReverseAgent": ("web_crawler.ai.reverse_agent", "ReverseAgent"),
        "ReverseAgentConfig": ("web_crawler.ai.reverse_agent", "ReverseAgentConfig"),
    }
    if name in _lazy:
        import importlib

        module_path, attr_name = _lazy[name]
        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

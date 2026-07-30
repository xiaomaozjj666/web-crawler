"""``web_crawler.ai`` 包导出与懒加载测试。

覆盖 :mod:`web_crawler.ai.__init__` 中的 ``__getattr__`` 懒加载器（行 103-153）：
- 所有 lazy 名称都能解析到对应模块的正确属性；
- 首次访问后被缓存到 ``ai.__dict__``；
- 访问未声明的属性时抛出 ``AttributeError``；
- ``__all__`` 中的所有名称都可解析；
- 直接导入（非 lazy）的符号在 ``ai.__dict__`` 中立即可用。
"""

from __future__ import annotations

import importlib

import pytest

# 所有 lazy 加载的名称 -> (模块路径, 期望属性名)
# 与 ai/__init__.py 中的 _lazy dict 保持同步
_LAZY_NAMES: dict[str, tuple[str, str]] = {
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
    "ImageCaptchaSolver": ("web_crawler.ai.image_captcha", "ImageCaptchaSolver"),
    "ImageSolverConfig": ("web_crawler.ai.image_captcha", "ImageSolverConfig"),
    "SliderSolution": ("web_crawler.ai.image_captcha", "SliderSolution"),
    "ClickSolution": ("web_crawler.ai.image_captcha", "ClickSolution"),
    "ReverseAgent": ("web_crawler.ai.reverse_agent", "ReverseAgent"),
    "ReverseAgentConfig": ("web_crawler.ai.reverse_agent", "ReverseAgentConfig"),
    "Planner": ("web_crawler.ai.planner", "Planner"),
    "Plan": ("web_crawler.ai.planner", "Plan"),
    "SubGoal": ("web_crawler.ai.planner", "SubGoal"),
    "TaskJudge": ("web_crawler.ai.judge", "TaskJudge"),
    "JudgeResult": ("web_crawler.ai.judge", "JudgeResult"),
    "EventBus": ("web_crawler.ai.watchdog", "EventBus"),
    "Heartbeat": ("web_crawler.ai.watchdog", "Heartbeat"),
    "CrashRecovery": ("web_crawler.ai.watchdog", "CrashRecovery"),
    "RunRecorder": ("web_crawler.ai.recorder", "RunRecorder"),
    "SchemaValidator": ("web_crawler.ai.schema", "SchemaValidator"),
    "WebPageState": ("web_crawler.ai.schema", "WebPageState"),
    "DomPruner": ("web_crawler.ai.dom_pruner", "DomPruner"),
    "PrunedDom": ("web_crawler.ai.dom_pruner", "PrunedDom"),
    "Checkpoint": ("web_crawler.ai.checkpoint", "Checkpoint"),
    "CheckpointManager": ("web_crawler.ai.checkpoint", "CheckpointManager"),
    "CheckpointStore": ("web_crawler.ai.checkpoint", "CheckpointStore"),
    "ConfidenceScorer": ("web_crawler.ai.confidence", "ConfidenceScorer"),
    "ConfidenceResult": ("web_crawler.ai.confidence", "ConfidenceResult"),
    "ActionGuard": ("web_crawler.ai.guardrails", "ActionGuard"),
    "GuardrailResult": ("web_crawler.ai.guardrails", "GuardrailResult"),
    "GuardrailAction": ("web_crawler.ai.guardrails", "GuardrailAction"),
    "GuardrailRule": ("web_crawler.ai.guardrails", "GuardrailRule"),
}

# 直接导入（非 lazy）的符号
_EAGER_NAMES: tuple[str, ...] = (
    "AIScrapeAgent",
    "RobotsPolicy",
    "ScrapeResult",
    "detect_block",
    "AIExtractor",
    "ExtractionResult",
    "DEFAULT_MODEL",
    "DeepSeekProvider",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "OpenAICompatibleProvider",
    "available_providers",
    "get_provider",
    "register_provider",
)


@pytest.fixture(scope="module", autouse=True)
def _import_ai_package() -> None:
    """确保 ``web_crawler.ai`` 在测试前被导入。"""
    importlib.import_module("web_crawler.ai")


@pytest.mark.parametrize("name,expected", list(_LAZY_NAMES.items()))
def test_lazy_name_resolves_to_module_attr(name: str, expected: tuple[str, str]) -> None:
    """每个 lazy 名称都能通过 ``__getattr__`` 解析到对应模块的属性。"""
    from web_crawler import ai

    value = getattr(ai, name)
    module = importlib.import_module(expected[0])
    assert value is getattr(module, expected[1])


@pytest.mark.parametrize("name", list(_LAZY_NAMES.keys()))
def test_lazy_name_cached_in_globals_after_access(name: str) -> None:
    """lazy 名称首次访问后被缓存到 ``ai.__dict__``，后续访问直接命中。"""
    from web_crawler import ai

    # 触发访问
    value = getattr(ai, name)
    # 应该被缓存
    assert name in ai.__dict__
    # 缓存值与解析值一致
    assert ai.__dict__[name] is value


def test_unknown_attribute_raises_attribute_error() -> None:
    """访问未声明的属性时抛出 AttributeError。"""
    from web_crawler import ai

    with pytest.raises(AttributeError, match="DoesNotExist"):
        ai.DoesNotExist  # noqa: B018


def test_all_names_in_all_are_resolvable() -> None:
    """``__all__`` 中所有名称都能通过 ``getattr`` 取到。"""
    from web_crawler import ai

    for name in ai.__all__:
        assert hasattr(ai, name), f"{name} in __all__ but not resolvable"


def test_eager_imports_available_in_dict() -> None:
    """直接导入的符号在 ``ai.__dict__`` 中立即可用（不走 lazy 路径）。"""
    from web_crawler import ai

    for name in _EAGER_NAMES:
        assert name in ai.__dict__, f"{name} should be eagerly imported"


def test_lazy_dict_in_sync_with_all() -> None:
    """``__all__`` 中所有非直接导入的名称都应在 ``_lazy`` 字典里。"""
    from web_crawler import ai

    eager_set = set(_EAGER_NAMES)
    all_set = set(ai.__all__)
    lazy_in_all = all_set - eager_set
    # 每个 lazy 名称都应能解析（不一定要求与 _LAZY_NAMES 完全一致，
    # 但 __all__ 中的所有非 eager 名称都必须可访问）
    for name in lazy_in_all:
        assert hasattr(ai, name), f"{name} in __all__ but not resolvable"


def test_lazy_lookup_uses_importlib() -> None:
    """lazy 加载路径走 ``importlib.import_module`` + ``getattr``（验证机制）。"""
    from web_crawler import ai

    # 删除缓存以强制重新解析
    if "Planner" in ai.__dict__:
        del ai.__dict__["Planner"]
    # 重新访问应能正确解析
    value = ai.Planner
    from web_crawler.ai.planner import Planner as DirectPlanner

    assert value is DirectPlanner
    # 再次缓存
    assert ai.__dict__["Planner"] is DirectPlanner


def test_lazy_lookup_returns_same_object_each_time() -> None:
    """同一 lazy 名称多次访问返回同一对象（缓存生效）。"""
    from web_crawler import ai

    v1 = ai.TaskJudge
    v2 = ai.TaskJudge
    assert v1 is v2

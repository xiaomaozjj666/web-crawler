"""_reverse_actions（动作执行层 mixin）守卫测试。

ReverseAgent 保守拆分（纯移动，API 不变）的回归守卫：
1. 模块可独立导入（无循环依赖、不依赖 reverse_agent 导入顺序）；
2. mixin MRO 正确：动作执行方法来自 ActionExecutor、编排方法留在
   ReverseAgent 本模块、交互超时常量随动作层迁移；
3. reverse_agent 的历史 re-export 完整（外部导入路径不变的硬性要求）；
4. mixin 宿主契约 stub 显式报错（宿主未实现时防静默降级）；
5. 关键行为锚点：未知动作的异常消息逐字不变（有测试断言消息文本）。

动作执行器的行为路径（click / type / scroll / press / hover /
select_option / new_tab / switch_tab / close_tab / humanize 变体、
缺参异常、事件发布）由 test_reverse_agent_browser.py 等既有测试
全量覆盖，此处不重复。
"""

from __future__ import annotations

import importlib

import pytest

from web_crawler.ai._jsonutil import extract_json as _jsonutil_extract_json
from web_crawler.ai._reverse_actions import ActionExecutor
from web_crawler.ai._reverse_prompt import THINK_USER_TEMPLATE as _prompt_template
from web_crawler.ai._reverse_support import Action as _support_action
from web_crawler.ai._reverse_support import Observation as _support_observation
from web_crawler.ai._reverse_support import ReverseAgentConfig as _support_config
from web_crawler.ai._reverse_support import js_str as _support_js_str
from web_crawler.ai.captcha import CaptchaType
from web_crawler.ai.reverse_agent import (
    _THINK_USER_TEMPLATE,
    Action,
    Observation,
    ReverseAgent,
    ReverseAgentConfig,
    _extract_json,
    _js_str,
)

# 迁移动作执行器族清单（ReverseAgent 上的历史名字 → ActionExecutor 提供）
_MOVED_ACTION_METHODS = (
    "_act_async",
    "_fallback_action",
    "_do_click_async",
    "_humanize_click_async",
    "_do_type_async",
    "_humanize_type_async",
    "_do_scroll_async",
    "_do_press_async",
    "_do_hover_async",
    "_do_select_option_async",
    "_do_new_tab_async",
    "_do_switch_tab_async",
    "_do_close_tab_async",
    "_resolve_tab",
)

# 编排层方法清单（必须留在 reverse_agent 本模块，未被误移）
_ORCHESTRATION_METHODS = (
    "run",
    "arun",
    "_observe_async",
    "_think_async",
    "_parse_action",
    "_collect_scripts_async",
    "_create_page_async",
    "_try_recover_page_async",
    "_read_hook_data_async",
    "_read_hook_records_async",
    "_build_think_prompt",
    "_take_screenshot_async",
    "checkpoints_snapshot",
    "close",
    "aclose",
)

# 编排实现 + mixin 宿主契约 stub 的共享方法：
# 真实实现必须在 reverse_agent 本模块，mixin 上只允许契约 stub
# （运行时 MRO 上宿主版本优先，stub 仅供类型检查与防呆守卫）
_SHARED_HOST_CONTRACT_METHODS = (
    "_emit",
    "_inject_hooks_async",
    "_analyze_captured_js_async",
    "_try_extract_param_async",
    "_setup_page_listeners",
)


def test_module_importable_standalone() -> None:
    """_reverse_actions 可独立导入，不依赖 reverse_agent 的导入副作用。"""
    mod = importlib.import_module("web_crawler.ai._reverse_actions")
    assert mod.ActionExecutor is ActionExecutor
    # 交互超时常量随动作执行层迁移（历史值 10s 保持不变）
    assert ActionExecutor.__dict__.get("_INTERACTION_TIMEOUT") == 10000
    # ReverseAgent 通过继承可见同名常量（历史类属性访问方式不变）
    assert ReverseAgent._INTERACTION_TIMEOUT == 10000


def test_mro_layout() -> None:
    """ReverseAgent 继承 ActionExecutor 且自身在 MRO 最前（mixin 语义）。"""
    assert ReverseAgent.__mro__[:2] == (ReverseAgent, ActionExecutor)
    # 动作执行方法由 mixin 提供：归属 _reverse_actions 且宿主未重复定义
    for name in _MOVED_ACTION_METHODS:
        mixin_attr = getattr(ActionExecutor, name)
        assert mixin_attr.__module__ == "web_crawler.ai._reverse_actions", name
        assert name not in ReverseAgent.__dict__, name
        assert getattr(ReverseAgent, name) is mixin_attr
    # 编排层方法留在 reverse_agent 本模块，且未混入 ActionExecutor
    for name in _ORCHESTRATION_METHODS:
        assert name not in ActionExecutor.__dict__, name
        assert getattr(ReverseAgent, name).__module__ == "web_crawler.ai.reverse_agent", name
    # 共享方法：真实实现定义在宿主（reverse_agent 模块）且 MRO 优先于 mixin stub
    for name in _SHARED_HOST_CONTRACT_METHODS:
        host_impl = ReverseAgent.__dict__.get(name)
        assert host_impl is not None, name
        assert host_impl.__module__ == "web_crawler.ai.reverse_agent", name
        assert getattr(ReverseAgent, name) is host_impl
        assert getattr(ReverseAgent, name) is not ActionExecutor.__dict__[name]


def test_reverse_agent_reexports() -> None:
    """reverse_agent 的历史外部导入路径全部可用且指向同一实现。"""
    import web_crawler.ai.reverse_agent as ra

    assert ra.Action is _support_action
    assert Action is _support_action
    assert ra.Observation is _support_observation
    assert Observation is _support_observation
    assert ra.ReverseAgentConfig is _support_config
    assert ReverseAgentConfig is _support_config
    assert ra._THINK_USER_TEMPLATE is _prompt_template
    assert _THINK_USER_TEMPLATE is _prompt_template
    assert ra._js_str is _support_js_str
    assert _js_str is _support_js_str
    assert ra._extract_json is _jsonutil_extract_json
    assert _extract_json is _jsonutil_extract_json
    assert ra.ReverseAgent is ReverseAgent
    # __all__ 历史契约保持不变（影响 `import *` 语义）
    assert list(ra.__all__) == ["Action", "Observation", "ReverseAgent", "ReverseAgentConfig"]


async def test_host_contract_stubs_raise() -> None:
    """mixin 宿主契约 stub 显式报错：宿主未实现时防静默降级。

    ReverseAgent（MRO 更靠前）实现了全部契约方法，运行时永远走宿主版本；
    stub 仅在缺失实现时兜底报错，避免静默返回 None 造成难排查的退化。
    """
    executor = ActionExecutor()
    with pytest.raises(NotImplementedError):
        executor._emit("x")
    with pytest.raises(NotImplementedError):
        await executor._inject_hooks_async(None, None)
    with pytest.raises(NotImplementedError):
        await executor._analyze_captured_js_async([], [])
    with pytest.raises(NotImplementedError):
        await executor._try_extract_param_async(None, "p")
    with pytest.raises(NotImplementedError):
        executor._setup_page_listeners(None)


async def test_unknown_action_error_message_unchanged() -> None:
    """_act_async 未知动作类型的异常消息逐字不变（进入 act_error 路径）。"""
    cfg = ReverseAgentConfig(enable_screenshot=False)
    agent = ReverseAgent(config=cfg)
    try:
        action = Action(action_type="no_such_action", params={})
        with pytest.raises(ValueError, match="未知动作类型: 'no_such_action'"):
            await agent._act_async(None, action, step=1)
    finally:
        agent.close()


async def test_fallback_action_delegates_unchanged() -> None:
    """_fallback_action 仍委托 _reverse_support.fallback_action（纯移动锚点）。"""
    cfg = ReverseAgentConfig(enable_screenshot=False, target_params=["X-Bogus"])
    agent = ReverseAgent(config=cfg)
    try:
        observation = Observation(
            url="https://example.com",
            hook_data={"records": [], "count": 0},
            network_requests=[],
            scripts=[],
            captcha_type=CaptchaType.NONE,
            page_title="",
            dom_summary="",
        )
        result = agent._fallback_action(observation)
        # 历史签名含 observation 参数；降级策略只依赖目标参数配置
        assert result.action_type == "extract"
        assert result.params.get("param_name") == "X-Bogus"
    finally:
        agent.close()

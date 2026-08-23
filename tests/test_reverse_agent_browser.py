"""Tests for ReverseAgent browser interactions: actions, recorder, multi-tab, and humanize input."""

from __future__ import annotations

from typing import Any

import pytest

# -- 浏览器交互动作（click / type / scroll / press / hover / select_option）--


class _FakeBrowserPage:
    """模拟 Playwright Page 的浏览器交互方法（同步版本）。

    记录所有调用以便断言。click/fill/type/hover/select_option/focus 接受
    selector 与 timeout 关键字；evaluate 接受 JS 字符串；press 接受 key。
    """

    def __init__(self) -> None:
        self.click_calls: list[dict[str, Any]] = []
        self.fill_calls: list[dict[str, Any]] = []
        self.type_calls: list[dict[str, Any]] = []
        self.evaluate_calls: list[str] = []
        self.focus_calls: list[dict[str, Any]] = []
        self.press_calls: list[str] = []
        self.hover_calls: list[dict[str, Any]] = []
        self.select_option_calls: list[dict[str, Any]] = []
        self.goto_calls: list[dict[str, Any]] = []
        self.bring_to_front_calls: int = 0
        self.close_calls: int = 0

    def click(self, selector: str, *, button: str = "left", timeout: int = 0) -> None:
        self.click_calls.append({"selector": selector, "button": button, "timeout": timeout})

    def fill(self, selector: str, value: str, *, timeout: int = 0) -> None:
        self.fill_calls.append({"selector": selector, "value": value, "timeout": timeout})

    def type(self, selector: str, text: str, *, timeout: int = 0) -> None:
        self.type_calls.append({"selector": selector, "text": text, "timeout": timeout})

    def evaluate(self, script: str) -> Any:
        self.evaluate_calls.append(script)
        return None

    def focus(self, selector: str, *, timeout: int = 0) -> None:
        self.focus_calls.append({"selector": selector, "timeout": timeout})

    def press(self, key: str) -> None:
        self.press_calls.append(key)

    def hover(self, selector: str, *, timeout: int = 0) -> None:
        self.hover_calls.append({"selector": selector, "timeout": timeout})

    def select_option(self, selector: str, value: str, *, timeout: int = 0) -> None:
        self.select_option_calls.append({"selector": selector, "value": value, "timeout": timeout})

    def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append({"url": url, **kwargs})

    def bring_to_front(self) -> None:
        self.bring_to_front_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def on(self, event: str, handler: Any) -> None:
        pass


class _FakeAsyncBrowserPage:
    """模拟 Playwright Page 的浏览器交互方法（异步版本）。"""

    def __init__(self) -> None:
        self.click_calls: list[dict[str, Any]] = []
        self.fill_calls: list[dict[str, Any]] = []
        self.type_calls: list[dict[str, Any]] = []
        self.evaluate_calls: list[str] = []
        self.focus_calls: list[dict[str, Any]] = []
        self.press_calls: list[str] = []
        self.hover_calls: list[dict[str, Any]] = []
        self.select_option_calls: list[dict[str, Any]] = []
        self.goto_calls: list[dict[str, Any]] = []
        self.bring_to_front_calls: int = 0
        self.close_calls: int = 0

    async def click(self, selector: str, *, button: str = "left", timeout: int = 0) -> None:
        self.click_calls.append({"selector": selector, "button": button, "timeout": timeout})

    async def fill(self, selector: str, value: str, *, timeout: int = 0) -> None:
        self.fill_calls.append({"selector": selector, "value": value, "timeout": timeout})

    async def type(self, selector: str, text: str, *, timeout: int = 0) -> None:
        self.type_calls.append({"selector": selector, "text": text, "timeout": timeout})

    async def evaluate(self, script: str) -> Any:
        self.evaluate_calls.append(script)
        return None

    async def focus(self, selector: str, *, timeout: int = 0) -> None:
        self.focus_calls.append({"selector": selector, "timeout": timeout})

    async def press(self, key: str) -> None:
        self.press_calls.append(key)

    async def hover(self, selector: str, *, timeout: int = 0) -> None:
        self.hover_calls.append({"selector": selector, "timeout": timeout})

    async def select_option(self, selector: str, value: str, *, timeout: int = 0) -> None:
        self.select_option_calls.append({"selector": selector, "value": value, "timeout": timeout})

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append({"url": url, **kwargs})

    async def bring_to_front(self) -> None:
        self.bring_to_front_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    def on(self, event: str, handler: Any) -> None:
        pass


class _FakeBrowserContext:
    """模拟 Playwright BrowserContext，用于多标签页测试。"""

    def __init__(self) -> None:
        self.pages: list[_FakeBrowserPage] = []
        self.next_id = 0

    async def new_page(self) -> _FakeAsyncBrowserPage:
        page = _FakeAsyncBrowserPage()
        page._tab_id = self.next_id  # type: ignore[attr-defined]
        self.next_id += 1
        self.pages.append(page)
        return page


def _make_agent_for_browser_actions() -> Any:
    """构造一个用于浏览器交互动作测试的 ReverseAgent（禁用截图/校验）。"""
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    cfg = ReverseAgentConfig(
        enable_screenshot=False,
        enable_guard=False,
        enable_judge=False,
        enable_recorder=False,
        planner_interval=None,
    )
    return ReverseAgent(config=cfg)


def _make_agent_with_context() -> Any:
    """构造带 _context 的 ReverseAgent，用于多标签页测试。"""
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    cfg = ReverseAgentConfig(
        enable_screenshot=False,
        enable_guard=False,
        enable_judge=False,
        enable_recorder=False,
        planner_interval=None,
        humanize_input=False,
        wait_after_navigate=0.0,
    )
    agent = ReverseAgent(config=cfg)
    agent._context = _FakeBrowserContext()
    return agent


# -- 浏览器交互动作 ---------------------------------------------------------


async def test_execute_click_action() -> None:
    """click 动作应调用 page.click 并传递 selector / button / timeout。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeAsyncBrowserPage()
        action_dict: dict[str, Any] = {
            "action_type": "click",
            "params": {"selector": "button#submit", "button": "right"},
        }
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(action_dict)
        await agent._act_async(page, action, step=1)
        assert len(page.click_calls) == 1
        assert page.click_calls[0]["selector"] == "button#submit"
        assert page.click_calls[0]["button"] == "right"
        assert page.click_calls[0]["timeout"] == 10000
    finally:
        agent.close()


def test_execute_click_action_async() -> None:
    """click 动作异步路径同样生效。"""
    import asyncio

    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "click", "params": {"selector": "button#x"}})

        async def _run() -> None:
            await agent._act_async(page, action, step=1)

        asyncio.run(_run())
        assert len(page.click_calls) == 1
        assert page.click_calls[0]["selector"] == "button#x"
        assert page.click_calls[0]["button"] == "left"
    finally:
        agent.close()


async def test_execute_type_action() -> None:
    """type 动作默认 clear=true，应先 fill 清空再 type 输入。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {
                "action_type": "type",
                "params": {"selector": "input#username", "text": "user123"},
            }
        )
        await agent._act_async(page, action, step=2)
        # clear=True 时应先调 fill 清空
        assert len(page.fill_calls) == 1
        assert page.fill_calls[0]["selector"] == "input#username"
        assert page.fill_calls[0]["value"] == ""
        assert len(page.type_calls) == 1
        assert page.type_calls[0]["text"] == "user123"
    finally:
        agent.close()


async def test_execute_type_action_no_clear() -> None:
    """clear=False 时跳过 fill 直接 type。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {
                "action_type": "type",
                "params": {"selector": "input#q", "text": "hello", "clear": False},
            }
        )
        await agent._act_async(page, action, step=1)
        assert page.fill_calls == []
        assert len(page.type_calls) == 1
    finally:
        agent.close()


async def test_execute_scroll_action_window() -> None:
    """scroll 无 selector 时调 window.scrollBy。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "scroll", "params": {"x": 0, "y": 800}})
        await agent._act_async(page, action, step=3)
        assert len(page.evaluate_calls) == 1
        assert "window.scrollBy" in page.evaluate_calls[0]
        assert "0" in page.evaluate_calls[0]
        assert "800" in page.evaluate_calls[0]
    finally:
        agent.close()


async def test_execute_scroll_action_element() -> None:
    """scroll 带 selector 时调 querySelector.scrollBy。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {
                "action_type": "scroll",
                "params": {"selector": ".list", "y": 500},
            }
        )
        await agent._act_async(page, action, step=1)
        assert len(page.evaluate_calls) == 1
        js = page.evaluate_calls[0]
        assert "querySelector" in js
        assert ".list" in js
        assert "scrollBy" in js
    finally:
        agent.close()


async def test_execute_press_action() -> None:
    """press 动作应调 page.press；带 selector 时先 focus。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {
                "action_type": "press",
                "params": {"selector": "input#q", "key": "Enter"},
            }
        )
        await agent._act_async(page, action, step=4)
        assert len(page.focus_calls) == 1
        assert page.focus_calls[0]["selector"] == "input#q"
        assert page.press_calls == ["Enter"]
    finally:
        agent.close()


async def test_execute_press_action_no_selector() -> None:
    """press 无 selector 时只调 press。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "press", "params": {"key": "Escape"}})
        await agent._act_async(page, action, step=1)
        assert page.focus_calls == []
        assert page.press_calls == ["Escape"]
    finally:
        agent.close()


async def test_execute_hover_action() -> None:
    """hover 动作应调 page.hover 并传递 selector / timeout。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "hover", "params": {"selector": ".menu-item"}})
        await agent._act_async(page, action, step=5)
        assert len(page.hover_calls) == 1
        assert page.hover_calls[0]["selector"] == ".menu-item"
        assert page.hover_calls[0]["timeout"] == 10000
    finally:
        agent.close()


async def test_execute_select_action() -> None:
    """select_option 动作应调 page.select_option。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {
                "action_type": "select_option",
                "params": {"selector": "select#country", "value": "CN"},
            }
        )
        await agent._act_async(page, action, step=6)
        assert len(page.select_option_calls) == 1
        assert page.select_option_calls[0]["selector"] == "select#country"
        assert page.select_option_calls[0]["value"] == "CN"
        assert page.select_option_calls[0]["timeout"] == 10000
    finally:
        agent.close()


def test_execute_select_action_async() -> None:
    """select_option 异步路径。"""
    import asyncio

    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {
                "action_type": "select_option",
                "params": {"selector": "select#c", "value": "US"},
            }
        )

        async def _run() -> None:
            await agent._act_async(page, action, step=1)

        asyncio.run(_run())
        assert len(page.select_option_calls) == 1
        assert page.select_option_calls[0]["value"] == "US"
    finally:
        agent.close()


async def test_execute_click_missing_selector_raises() -> None:
    """click 缺少 selector 应抛 ValueError。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "click", "params": {}})
        with pytest.raises(ValueError, match="selector"):
            await agent._act_async(page, action, step=1)
    finally:
        agent.close()


async def test_browser_action_emits_event() -> None:
    """浏览器交互动作应发布 browser.action 事件。"""
    agent = _make_agent_for_browser_actions()
    try:
        events: list[dict[str, Any]] = []

        def _handler(event: Any) -> None:
            events.append(
                {
                    "type": event.type,
                    "step": event.step,
                    **event.payload,
                }
            )

        agent.event_bus.subscribe(_handler)
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "click", "params": {"selector": "button#x"}})
        await agent._act_async(page, action, step=7)
        browser_events = [e for e in events if e["type"] == "browser.action"]
        assert len(browser_events) == 1
        assert browser_events[0]["action"] == "click"
        assert browser_events[0]["selector"] == "button#x"
        assert browser_events[0]["step"] == 7
    finally:
        agent.close()


# -- Recorder: 编译浏览器交互动作 -------------------------------------------
def test_recorder_compiles_browser_actions() -> None:
    """成功路径脚本应包含 6 类浏览器交互动作的对应代码。"""
    from web_crawler.ai.recorder import RunRecorder

    recorder = RunRecorder()
    recorder.set_target("https://example.com")
    # 依次记录 6 类动作
    recorder.record(
        step=1,
        action_type="navigate",
        params={"url": "https://example.com"},
    )
    recorder.record(
        step=2,
        action_type="click",
        params={"selector": "button#login", "button": "left"},
    )
    recorder.record(
        step=3,
        action_type="type",
        params={"selector": "input#user", "text": "alice", "clear": True},
    )
    recorder.record(
        step=4,
        action_type="scroll",
        params={"x": 0, "y": 500},
    )
    recorder.record(
        step=5,
        action_type="press",
        params={"key": "Enter"},
    )
    recorder.record(
        step=6,
        action_type="hover",
        params={"selector": ".tooltip"},
    )
    recorder.record(
        step=7,
        action_type="select_option",
        params={"selector": "select#lang", "value": "zh"},
    )
    recorder.record(step=8, action_type="done", params={"success": True})

    script = recorder.compile_script()
    # 验证脚本中包含各类动作的编译产物
    assert "page.click" in script
    assert "button='left'" in script or 'button="left"' in script
    assert "page.fill" in script  # clear=True 时应生成 fill
    assert "page.type" in script
    assert "window.scrollBy" in script
    assert "page.press" in script
    assert "page.hover" in script
    assert "page.select_option" in script
    # 脚本应是合法 Python 源码
    compile(script, "<test>", "exec")  # 不抛异常即合法


def test_recorder_compiles_scroll_with_selector() -> None:
    """scroll 带 selector 时编译产物应包含 querySelector。"""
    from web_crawler.ai.recorder import RunRecorder

    recorder = RunRecorder()
    recorder.set_target("https://example.com")
    recorder.record(
        step=1,
        action_type="scroll",
        params={"selector": ".list", "y": 300},
    )
    script = recorder.compile_script()
    assert "querySelector" in script
    assert "scrollBy" in script
    compile(script, "<test>", "exec")


def test_recorder_skips_failed_browser_action() -> None:
    """失败的浏览器交互动作不应编译进成功路径脚本。"""
    from web_crawler.ai.recorder import RunRecorder

    recorder = RunRecorder()
    recorder.set_target("https://example.com")
    recorder.record(
        step=1,
        action_type="click",
        params={"selector": "button#x"},
        success=False,  # 失败步
    )
    recorder.record(
        step=2,
        action_type="click",
        params={"selector": "button#y"},
        success=True,
    )
    script = recorder.compile_script()
    assert "button#y" in script
    assert "button#x" not in script


# -- Action.from_dict 解析浏览器交互动作 ------------------------------------
def test_action_from_dict_parses_browser_actions() -> None:
    """Action.from_dict 应正确解析 6 类浏览器交互动作。"""
    from web_crawler.ai.reverse_agent import Action

    for atype, params in [
        ("click", {"selector": "button#x", "button": "right"}),
        ("type", {"selector": "input", "text": "hello", "clear": False}),
        ("scroll", {"x": 0, "y": 100}),
        ("press", {"key": "Tab"}),
        ("hover", {"selector": ".item"}),
        ("select_option", {"selector": "select#c", "value": "US"}),
    ]:
        action = Action.from_dict({"action_type": atype, "params": params})
        assert action.action_type == atype
        assert action.params == params


def test_reverse_agent_prompt_lists_browser_actions() -> None:
    """_THINK_USER_TEMPLATE 应在动作列表中包含 6 类浏览器交互动作。"""
    from web_crawler.ai.reverse_agent import _THINK_USER_TEMPLATE

    for atype in ["click", "type", "scroll", "press", "hover", "select_option"]:
        assert atype in _THINK_USER_TEMPLATE


# -- 多标签页管理（new_tab / switch_tab / close_tab） -----------------------


async def test_execute_new_tab_action() -> None:
    """new_tab 动作应创建新 page 并切到新标签；主页面登记为 'main'。"""
    agent = _make_agent_with_context()
    try:
        main_page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {
                "action_type": "new_tab",
                "params": {"url": "https://example.com/tab2", "name": "second"},
            }
        )
        await agent._do_new_tab_async(main_page, action, step=1)
        # _tabs 应包含 'main' 和 'second'
        assert "main" in agent._tabs
        assert "second" in agent._tabs
        assert agent._tabs["main"] is main_page
        # 当前 _page 应切到新标签
        assert agent._page is agent._tabs["second"]
        assert agent._page is not main_page
        # 新标签应被导航到 url
        new_tab = agent._tabs["second"]
        assert len(new_tab.goto_calls) == 1
        assert new_tab.goto_calls[0]["url"] == "https://example.com/tab2"
    finally:
        agent.close()


async def test_execute_new_tab_default_name() -> None:
    """new_tab 不传 name 时应使用 tab_N 作为默认名。"""
    agent = _make_agent_with_context()
    try:
        main_page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "new_tab", "params": {"url": ""}})
        await agent._do_new_tab_async(main_page, action, step=1)
        # 默认名应为 tab_0（首次新建，main 不计入计数）
        assert "tab_0" in agent._tabs
    finally:
        agent.close()


async def test_execute_switch_tab_by_name() -> None:
    """switch_tab 按 name 切换应更新 self._page。"""
    agent = _make_agent_with_context()
    try:
        main_page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        # 先新建一个 tab
        await agent._do_new_tab_async(
            main_page,
            Action.from_dict({"action_type": "new_tab", "params": {"url": "", "name": "t2"}}),
            step=1,
        )
        new_tab = agent._tabs["t2"]
        # 切回 main
        await agent._do_switch_tab_async(
            new_tab,
            Action.from_dict({"action_type": "switch_tab", "params": {"name": "main"}}),
            step=2,
        )
        assert agent._page is main_page
        # 再切到 t2
        await agent._do_switch_tab_async(
            main_page,
            Action.from_dict({"action_type": "switch_tab", "params": {"name": "t2"}}),
            step=3,
        )
        assert agent._page is new_tab
    finally:
        agent.close()


async def test_execute_switch_tab_by_index() -> None:
    """switch_tab 按 index 切换应按 _tabs 插入顺序解析。"""
    agent = _make_agent_with_context()
    try:
        main_page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        await agent._do_new_tab_async(
            main_page,
            Action.from_dict({"action_type": "new_tab", "params": {"url": "", "name": "second"}}),
            step=1,
        )
        # _do_new_tab 先插入 "second" 再插入 "main"，所以 index=0 是 second，index=1 是 main
        await agent._do_switch_tab_async(
            agent._tabs["second"],
            Action.from_dict({"action_type": "switch_tab", "params": {"index": 1}}),
            step=2,
        )
        assert agent._page is main_page
        # index=0 应回到 second
        await agent._do_switch_tab_async(
            main_page,
            Action.from_dict({"action_type": "switch_tab", "params": {"index": 0}}),
            step=3,
        )
        assert agent._page is agent._tabs["second"]
    finally:
        agent.close()


async def test_execute_switch_tab_unknown_raises() -> None:
    """switch_tab 找不到标签时应抛 ValueError。"""
    agent = _make_agent_with_context()
    try:
        main_page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "switch_tab", "params": {"name": "nonexistent"}})
        with pytest.raises(ValueError, match="switch_tab"):
            await agent._do_switch_tab_async(main_page, action, step=1)
    finally:
        agent.close()


async def test_execute_close_tab_action() -> None:
    """close_tab 应从 _tabs 移除并把 _page 回退到 main。"""
    agent = _make_agent_with_context()
    try:
        main_page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        # 新建 second tab
        await agent._do_new_tab_async(
            main_page,
            Action.from_dict({"action_type": "new_tab", "params": {"url": "", "name": "second"}}),
            step=1,
        )
        assert "second" in agent._tabs
        # 关闭 second
        await agent._do_close_tab_async(
            agent._tabs["second"],
            Action.from_dict({"action_type": "close_tab", "params": {"name": "second"}}),
            step=2,
        )
        assert "second" not in agent._tabs
        # _page 应回退到 main
        assert agent._page is main_page
    finally:
        agent.close()


async def test_execute_close_tab_unknown_raises() -> None:
    """close_tab 找不到 name 时应抛 ValueError。"""
    agent = _make_agent_with_context()
    try:
        main_page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "close_tab", "params": {"name": "ghost"}})
        with pytest.raises(ValueError, match="close_tab"):
            await agent._do_close_tab_async(main_page, action, step=1)
    finally:
        agent.close()


async def test_new_tab_action_emits_event() -> None:
    """new_tab 动作应发布 browser.action 事件。"""
    agent = _make_agent_with_context()
    try:
        events: list[dict[str, Any]] = []

        def _handler(event: Any) -> None:
            events.append({"type": event.type, "step": event.step, **event.payload})

        agent.event_bus.subscribe(_handler)
        main_page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {"action_type": "new_tab", "params": {"url": "https://x.example", "name": "n1"}}
        )
        await agent._do_new_tab_async(main_page, action, step=5)
        tab_events = [
            e for e in events if e["type"] == "browser.action" and e.get("action") == "new_tab"
        ]
        assert len(tab_events) == 1
        assert tab_events[0]["name"] == "n1"
        assert tab_events[0]["url"] == "https://x.example"
        assert tab_events[0]["step"] == 5
    finally:
        agent.close()


# -- 人类化输入轨迹（humanize_input） ---------------------------------------


async def test_humanize_click_hovers_before_click() -> None:
    """humanize_input=True 时 click 应先 hover 再 click。"""
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    cfg = ReverseAgentConfig(
        enable_screenshot=False,
        enable_guard=False,
        enable_judge=False,
        enable_recorder=False,
        planner_interval=None,
        humanize_input=True,
    )
    agent = ReverseAgent(config=cfg)
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "click", "params": {"selector": "button#x"}})
        await agent._act_async(page, action, step=1)
        # hover 应被调用（人类化先 hover）
        assert len(page.hover_calls) == 1
        assert page.hover_calls[0]["selector"] == "button#x"
        # click 也应被调用
        assert len(page.click_calls) == 1
        assert page.click_calls[0]["selector"] == "button#x"
    finally:
        agent.close()


async def test_humanize_type_focuses_before_type() -> None:
    """humanize_input=True 时 type 应先 focus 再 type。"""
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    cfg = ReverseAgentConfig(
        enable_screenshot=False,
        enable_guard=False,
        enable_judge=False,
        enable_recorder=False,
        planner_interval=None,
        humanize_input=True,
    )
    agent = ReverseAgent(config=cfg)
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {"action_type": "type", "params": {"selector": "input#q", "text": "hi"}}
        )
        await agent._act_async(page, action, step=1)
        # focus 应被调用
        assert len(page.focus_calls) == 1
        assert page.focus_calls[0]["selector"] == "input#q"
        # type 也应被调用（mock 不支持 delay，TypeError 退化为不带 delay）
        assert len(page.type_calls) == 1
        assert page.type_calls[0]["text"] == "hi"
    finally:
        agent.close()


async def test_humanize_disabled_skips_hover_and_focus() -> None:
    """humanize_input=False 时 click 不 hover，type 不 focus。"""
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    cfg = ReverseAgentConfig(
        enable_screenshot=False,
        enable_guard=False,
        enable_judge=False,
        enable_recorder=False,
        planner_interval=None,
        humanize_input=False,
    )
    agent = ReverseAgent(config=cfg)
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        # click：不应 hover
        await agent._act_async(
            page,
            Action.from_dict({"action_type": "click", "params": {"selector": "button#x"}}),
            step=1,
        )
        assert page.hover_calls == []
        assert len(page.click_calls) == 1
        # type：不应 focus
        await agent._act_async(
            page,
            Action.from_dict(
                {
                    "action_type": "type",
                    "params": {"selector": "input#q", "text": "hi", "clear": False},
                }
            ),
            step=2,
        )
        assert page.focus_calls == []
        assert len(page.type_calls) == 1
    finally:
        agent.close()


def test_humanize_click_async_hovers_before_click() -> None:
    """异步路径 humanize_input=True 时 click 应先 hover 再 click。"""
    import asyncio

    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    cfg = ReverseAgentConfig(
        enable_screenshot=False,
        enable_guard=False,
        enable_judge=False,
        enable_recorder=False,
        planner_interval=None,
        humanize_input=True,
    )
    agent = ReverseAgent(config=cfg)
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "click", "params": {"selector": "button#async"}})

        async def _run() -> None:
            await agent._act_async(page, action, step=1)

        asyncio.run(_run())
        assert len(page.hover_calls) == 1
        assert page.hover_calls[0]["selector"] == "button#async"
        assert len(page.click_calls) == 1
    finally:
        agent.close()


def test_humanize_type_async_focuses_before_type() -> None:
    """异步路径 humanize_input=True 时 type 应先 focus 再 type。"""
    import asyncio

    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    cfg = ReverseAgentConfig(
        enable_screenshot=False,
        enable_guard=False,
        enable_judge=False,
        enable_recorder=False,
        planner_interval=None,
        humanize_input=True,
    )
    agent = ReverseAgent(config=cfg)
    try:
        page = _FakeAsyncBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {"action_type": "type", "params": {"selector": "input#q", "text": "abc"}}
        )

        async def _run() -> None:
            await agent._act_async(page, action, step=1)

        asyncio.run(_run())
        assert len(page.focus_calls) == 1
        assert page.focus_calls[0]["selector"] == "input#q"
        assert len(page.type_calls) == 1
    finally:
        agent.close()


# -- Prompt 应包含多标签页与 humanize 说明 ----------------------------------


def test_prompt_lists_multi_tab_actions() -> None:
    """_THINK_USER_TEMPLATE 应在动作列表中包含 new_tab / switch_tab / close_tab。"""
    from web_crawler.ai.reverse_agent import _THINK_USER_TEMPLATE

    for atype in ["new_tab", "switch_tab", "close_tab"]:
        assert atype in _THINK_USER_TEMPLATE, f"prompt 缺少 {atype} 动作说明"


def test_reverse_agent_config_humanize_input_default_true() -> None:
    """ReverseAgentConfig.humanize_input 默认应为 True。"""
    from web_crawler.ai.reverse_agent import ReverseAgentConfig

    cfg = ReverseAgentConfig()
    assert cfg.humanize_input is True

"""ReverseAgent 动作执行层（``ActionExecutor`` mixin）。

分层结构（对标 browser-use 的 Agent 编排层 / Action 执行层分离）
----------------------------------------------------------------
本模块承载从 ``reverse_agent.py`` **纯移动**而来的动作执行器族：

- 动作分发器 ``_act_async``（navigate / inject_hook / analyze_js /
  wait / extract / solve_captcha / 各浏览器动作的内联分发）与
  降级动作构造 ``_fallback_action``；
- 浏览器交互动作 ``_do_click_async`` / ``_do_type_async`` /
  ``_do_scroll_async`` / ``_do_press_async`` / ``_do_hover_async`` /
  ``_do_select_option_async`` 及 humanize 变体；
- 多标签页管理 ``_do_new_tab_async`` / ``_do_switch_tab_async`` /
  ``_do_close_tab_async`` 与 ``_resolve_tab`` 辅助；
- 交互超时常量 ``_INTERACTION_TIMEOUT``。

纯移动原则：所有方法体逐字迁移，行为、异常类型与异常消息零变化，
不引入任何新状态。``ActionExecutor`` 以 mixin 形式被 ``ReverseAgent``
继承——方法体大量引用宿主状态（``self.config`` / ``self._tabs`` /
``self._emit`` / ``self.guard`` 等），mixin 让 ``self`` 引用原样工作，
是零行为变化的最小改动路径；``ReverseAgent`` 的公开签名与
``from web_crawler.ai.reverse_agent import ...`` 的历史导入路径不变。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from ..fetchers.camoufox import CamoufoxFetcher
from ._reverse_support import (
    Action,
    Observation,
    ReverseAgentConfig,
    fallback_action,
)
from ._reverse_support import (
    js_str as _js_str,
)
from .analyzer import AnalysisResult
from .captcha import CaptchaManager
from .guardrails import ActionGuard


class ActionExecutor:
    """ReverseAgent 的动作执行层 mixin（无独立状态，纯方法迁移）。

    宿主契约
    --------
    下方属性注解与方法 stub 声明本 mixin 对宿主（``ReverseAgent``）的依赖：

    - 属性注解仅做类型标注，运行时不赋值，实际值由宿主 ``__init__`` 设置；
    - 方法 stub 在 MRO 上位于宿主实现之后，运行时永远调用宿主版本；
      stub 仅用于 mypy 类型检查，以及"宿主未实现即显式报错"的防呆守卫
      （避免静默返回 ``None`` 造成难排查的行为退化）。
    """

    # -- 宿主契约：属性（仅类型注解，值由 ReverseAgent.__init__ 设置）------
    config: ReverseAgentConfig
    captcha_manager: CaptchaManager
    fetcher: CamoufoxFetcher | None
    guard: ActionGuard | None
    _context: Any
    _page: Any
    _tabs: dict[str, Any]

    # -- 宿主契约：方法（实现由 ReverseAgent 提供，签名保持一致）----------
    def _emit(self, type_: str, *, step: int = 0, **payload: Any) -> None:
        """宿主契约 stub：事件总线发布（实现见 ReverseAgent._emit）。"""
        raise NotImplementedError

    async def _inject_hooks_async(self, page: Any, hook_names: list[str] | None) -> bool:
        """宿主契约 stub：Hook 注入（实现见 ReverseAgent._inject_hooks_async）。"""
        raise NotImplementedError

    async def _analyze_captured_js_async(
        self,
        scripts: list[str],
        target_params: list[str],
    ) -> AnalysisResult | None:
        """宿主契约 stub：JS 分析（实现见 ReverseAgent._analyze_captured_js_async）。"""
        raise NotImplementedError

    async def _try_extract_param_async(self, page: Any, param_name: str) -> str | None:
        """宿主契约 stub：参数提取（实现见 ReverseAgent._try_extract_param_async）。"""
        raise NotImplementedError

    def _setup_page_listeners(self, page: Any) -> None:
        """宿主契约 stub：页面事件监听（实现见 ReverseAgent._setup_page_listeners）。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 动作分发与降级
    # ------------------------------------------------------------------

    def _fallback_action(self, observation: Observation) -> Action:
        """AI 分析失败时的降级动作（实现见 _reverse_support.fallback_action）。

        ``observation`` 仅为保持历史签名；降级策略只依赖目标参数配置。
        """
        return fallback_action(self.config.target_params)

    async def _act_async(self, page: Any, action: Action, *, step: int = 0) -> Any:
        """异步执行动作。"""
        atype = action.action_type
        if atype == "navigate":
            url = action.params.get("url")
            if url:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(self.config.wait_after_navigate)
            return None
        if atype == "inject_hook":
            hooks = action.params.get("hooks")
            return await self._inject_hooks_async(page, hooks)
        if atype == "analyze_js":
            scripts = action.params.get("script_urls", [])
            target_params = action.params.get("target_params", self.config.target_params or [])
            return await self._analyze_captured_js_async(scripts, target_params)
        if atype == "wait":
            seconds = float(action.params.get("seconds", 1.0))
            await asyncio.sleep(max(0.1, min(seconds, 30.0)))
            return None
        if atype == "extract":
            param_name = action.params.get("param_name", "")
            if not param_name:
                return None
            return await self._try_extract_param_async(page, param_name)
        if atype == "solve_captcha":
            # CaptchaManager.handle 是同步的，丢到线程池避免阻塞事件循环
            return await asyncio.to_thread(self.captcha_manager.handle, page)
        if atype == "click":
            await self._do_click_async(page, action, step=step)
            return None
        if atype == "type":
            await self._do_type_async(page, action, step=step)
            return None
        if atype == "scroll":
            await self._do_scroll_async(page, action, step=step)
            return None
        if atype == "press":
            await self._do_press_async(page, action, step=step)
            return None
        if atype == "hover":
            await self._do_hover_async(page, action, step=step)
            return None
        if atype == "select_option":
            await self._do_select_option_async(page, action, step=step)
            return None
        if atype == "new_tab":
            await self._do_new_tab_async(page, action, step=step)
            return None
        if atype == "switch_tab":
            await self._do_switch_tab_async(page, action, step=step)
            return None
        if atype == "close_tab":
            await self._do_close_tab_async(page, action, step=step)
            return None
        if atype == "done":
            # done 由主循环在外层处理，_act_async 内不执行
            return None
        # 未知动作类型：抛错进入 act_error 路径，写入 history 便于审计
        raise ValueError(f"未知动作类型: {atype!r}")

    # ------------------------------------------------------------------
    # 浏览器交互动作（click / type / scroll / press / hover / select_option）
    # ------------------------------------------------------------------

    # 所有交互动作统一设 10s 超时；超时抛 TimeoutError 由外层 _act 调用处捕获
    _INTERACTION_TIMEOUT = 10000

    async def _do_click_async(self, page: Any, action: Action, *, step: int) -> None:
        """异步：点击元素。``humanize_input`` 启用时先 hover 再随机延迟后点击。"""
        selector = action.params.get("selector", "")
        if not selector:
            raise ValueError("click 动作需要 selector 参数")
        button = action.params.get("button", "left")
        if self.config.humanize_input:
            await self._humanize_click_async(page, selector, button=button)
            self._emit("browser.action.humanized", step=step, action="click")
        else:
            await page.click(selector, button=button, timeout=self._INTERACTION_TIMEOUT)
        self._emit(
            "browser.action",
            step=step,
            action="click",
            selector=selector,
            button=button,
        )

    async def _humanize_click_async(
        self, page: Any, selector: str, *, button: str = "left"
    ) -> None:
        """异步人类化点击：先 hover 移动鼠标，随机延迟 50-200ms 后再 click。"""
        try:
            await page.hover(selector, timeout=self._INTERACTION_TIMEOUT)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.05, 0.2))
        await page.click(selector, button=button, timeout=self._INTERACTION_TIMEOUT)

    async def _do_type_async(self, page: Any, action: Action, *, step: int) -> None:
        """异步：在输入框输入文本（默认先清空）。``humanize_input`` 启用时逐字符随机延迟。"""
        selector = action.params.get("selector", "")
        text = action.params.get("text", "")
        if not selector:
            raise ValueError("type 动作需要 selector 参数")
        clear = action.params.get("clear", True)
        if clear:
            await page.fill(selector, "", timeout=self._INTERACTION_TIMEOUT)
        if self.config.humanize_input:
            await self._humanize_type_async(page, selector, str(text))
            self._emit("browser.action.humanized", step=step, action="type")
        else:
            await page.type(selector, text, timeout=self._INTERACTION_TIMEOUT)
        self._emit(
            "browser.action",
            step=step,
            action="type",
            selector=selector,
            text_length=len(str(text)),
        )

    async def _humanize_type_async(self, page: Any, selector: str, text: str) -> None:
        """异步人类化输入：先 focus 元素，随机停顿后用 delay 逐键输入。"""
        try:
            await page.focus(selector, timeout=self._INTERACTION_TIMEOUT)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.1, 0.3))
        delay_ms = random.randint(30, 150)
        try:
            await page.type(selector, text, delay=delay_ms)
        except TypeError:
            await page.type(selector, text)

    async def _do_scroll_async(self, page: Any, action: Action, *, step: int) -> None:
        """异步：滚动页面或元素。"""
        selector = action.params.get("selector")
        x = int(action.params.get("x", 0))
        y = int(action.params.get("y", 800))
        if selector:
            await page.evaluate(f"document.querySelector({_js_str(selector)})?.scrollBy({x}, {y})")
        else:
            await page.evaluate(f"window.scrollBy({x}, {y})")
        self._emit(
            "browser.action",
            step=step,
            action="scroll",
            selector=selector,
            x=x,
            y=y,
        )

    async def _do_press_async(self, page: Any, action: Action, *, step: int) -> None:
        """异步：按键（可先聚焦到指定元素）。"""
        key = action.params.get("key", "Enter")
        selector = action.params.get("selector")
        if selector:
            await page.focus(selector, timeout=self._INTERACTION_TIMEOUT)
        await page.press(key)
        self._emit(
            "browser.action",
            step=step,
            action="press",
            key=key,
            selector=selector,
        )

    async def _do_hover_async(self, page: Any, action: Action, *, step: int) -> None:
        """异步：鼠标悬停。"""
        selector = action.params.get("selector", "")
        if not selector:
            raise ValueError("hover 动作需要 selector 参数")
        await page.hover(selector, timeout=self._INTERACTION_TIMEOUT)
        self._emit(
            "browser.action",
            step=step,
            action="hover",
            selector=selector,
        )

    async def _do_select_option_async(self, page: Any, action: Action, *, step: int) -> None:
        """异步：下拉选择。"""
        selector = action.params.get("selector", "")
        value = action.params.get("value", "")
        if not selector:
            raise ValueError("select_option 动作需要 selector 参数")
        await page.select_option(selector, value, timeout=self._INTERACTION_TIMEOUT)
        self._emit(
            "browser.action",
            step=step,
            action="select_option",
            selector=selector,
            value=value,
        )

    # ------------------------------------------------------------------
    # 多标签页管理（new_tab / switch_tab / close_tab）
    # ------------------------------------------------------------------

    async def _do_new_tab_async(self, page: Any, action: Action, *, step: int) -> None:
        """异步：新建标签页并导航到指定 URL，切换 self._page 到新标签。"""
        url = action.params.get("url", "")
        name = action.params.get("name") or f"tab_{len(self._tabs)}"
        assert self._context is not None
        new_page = await self._context.new_page()
        try:
            await self.fetcher._setup_page_async(new_page)  # type: ignore[union-attr]
        except Exception:
            pass
        self._setup_page_listeners(new_page)
        self._tabs[name] = new_page
        if "main" not in self._tabs:
            self._tabs["main"] = page
        if url:
            # 深度防御：new_tab 的导航 URL 再走一次护栏检查（循环层已检查 new_tab 动作）
            nav_check = self.guard.check_navigation_url(url) if self.guard is not None else None
            if nav_check is not None and nav_check.denied:
                self._emit(
                    "guard.deny",
                    step=step,
                    matched_rules=nav_check.matched_rules,
                    details=nav_check.details,
                )
            else:
                await new_page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(self.config.wait_after_navigate)
        self._page = new_page
        self._emit(
            "browser.action",
            step=step,
            action="new_tab",
            name=name,
            url=url,
        )

    async def _do_switch_tab_async(self, page: Any, action: Action, *, step: int) -> None:
        """异步：切换到指定标签页（按 name 或 index）。"""
        name = action.params.get("name")
        index = action.params.get("index")
        target = self._resolve_tab(name=name, index=index)
        if target is None:
            raise ValueError(f"switch_tab: 找不到标签页 (name={name!r}, index={index!r})")
        self._page = target
        try:
            await target.bring_to_front()
        except Exception:
            pass
        self._emit("browser.action", step=step, action="switch_tab", name=name, index=index)

    async def _do_close_tab_async(self, page: Any, action: Action, *, step: int) -> None:
        """异步：关闭指定标签页。关闭后 self._page 回退到 main（若存在）。"""
        name = action.params.get("name", "")
        target = self._tabs.pop(name, None)
        if target is None:
            raise ValueError(f"close_tab: 找不到标签页 name={name!r}")
        try:
            await target.close()
        except Exception:
            pass
        if self._page is target:
            self._page = self._tabs.get("main")
        self._emit("browser.action", step=step, action="close_tab", name=name)

    def _resolve_tab(self, *, name: str | None, index: int | None) -> Any:
        """按 name 或 index 解析标签页对象。name 优先，index 按 _tabs 插入顺序。"""
        if name is not None:
            return self._tabs.get(name)
        if index is not None:
            try:
                key = list(self._tabs.keys())[int(index)]
            except (IndexError, ValueError):
                return None
            return self._tabs.get(key)
        return None

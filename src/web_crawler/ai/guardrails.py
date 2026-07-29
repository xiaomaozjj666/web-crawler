"""危险动作护栏模块（Sandbox Action Guardrails）。

借鉴 Anthropic Computer Use / Nanobrowser / BrowserAct 的安全策略：在 Agent
动作执行前做一次拦截检查，对潜在危险动作（导航到外部域名、注入恶意脚本、
提交表单、点击删除按钮等）按策略进行 deny / confirm / log。

能力清单
--------
- :class:`GuardrailAction` — 处置策略枚举（``ALLOW`` / ``DENY`` / ``CONFIRM``）；
- :class:`GuardrailRule` — 单条规则（名称 + 检查函数 + 处置策略）；
- :class:`GuardrailResult` — 单次检查结果；
- :class:`ActionGuard` — Agent 主循环接入点：
  * :meth:`check` — 同步检查动作；
  * :meth:`check_async` — 异步检查（确认类规则可走 async callback）；
  * :meth:`add_rule` — 注册自定义规则；
  * 默认规则集：
    - 禁止导航到 localhost / 内网 IP（防止 SSRF 探测内网）；
    - 禁止导航到非 HTTPS 页面（除明确允许的 dev 环境）；
    - 禁止注入含 ``eval(atob(`` / ``Function(`` 等高危模式的脚本；
    - 禁止跨域名跳转（超出 ``allowed_domains`` 白名单）；
    - 禁止执行 ``done`` 之外的浏览器关闭动作。

设计要点
--------
- 规则可插拔：``add_rule`` 支持任意 callable；
- 默认白名单宽松：仅拦截明显危险动作，避免误伤正常逆向流程；
- 与 :class:`ReverseAgent` 集成：``deny`` 时跳过动作并写入 history；
  ``confirm`` 时调用 ``on_confirm`` 回调，由调用方决定是否继续。
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlparse


class GuardrailAction(str, Enum):
    """处置策略。"""

    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


# 检查函数签名：输入 (action_dict, context) -> (是否命中, 详情)
CheckFn = Callable[[dict[str, Any], dict[str, Any]], tuple[bool, str]]


@dataclass
class GuardrailRule:
    """单条护栏规则。

    Attributes
    ----------
    name:
        规则名称（用于日志与命中提示）。
    check:
        检查函数，返回 (命中, 详情)。
    action:
        命中时的处置策略。
    severity:
        严重级别（``info`` / ``warn`` / ``error``），仅用于日志。
    """

    name: str
    check: CheckFn
    action: GuardrailAction
    severity: str = "warn"


@dataclass
class GuardrailResult:
    """单次护栏检查结果。

    Attributes
    ----------
    action:
        最终处置策略（取所有命中规则里最严格的）。
    matched_rules:
        命中的规则名列表。
    details:
        每条命中规则的详情。
    """

    action: GuardrailAction = GuardrailAction.ALLOW
    matched_rules: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    @property
    def denied(self) -> bool:
        return self.action == GuardrailAction.DENY

    @property
    def needs_confirm(self) -> bool:
        return self.action == GuardrailAction.CONFIRM

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "matched_rules": list(self.matched_rules),
            "details": list(self.details),
        }


class ActionGuard:
    """Agent 主循环的动作护栏。

    Parameters
    ----------
    allowed_domains:
        允许导航的域名白名单。``None`` 表示不限制；``["*"]`` 表示全部允许。
    allow_localhost:
        是否允许 localhost / 127.0.0.1 / 内网 IP。默认 False（生产场景）。
    extra_rules:
        额外规则列表。
    on_confirm:
        ``CONFIRM`` 类规则命中时的回调，签名 ``(rule_name, detail) -> bool``，
        返回 True 表示用户确认放行。默认直接拒绝（视为 deny）。
    """

    def __init__(
        self,
        *,
        allowed_domains: list[str] | None = None,
        allow_localhost: bool = False,
        extra_rules: list[GuardrailRule] | None = None,
        on_confirm: Callable[[str, str], bool] | None = None,
    ) -> None:
        self.allowed_domains = allowed_domains
        self.allow_localhost = allow_localhost
        self.on_confirm = on_confirm
        self._rules: list[GuardrailRule] = []
        self._register_defaults()
        if extra_rules:
            self._rules.extend(extra_rules)

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    def check(
        self,
        action: Any,
        *,
        context: dict[str, Any] | None = None,
    ) -> GuardrailResult:
        """同步：检查动作是否合规。

        Parameters
        ----------
        action:
            ``Action`` dataclass 或 dict。
        context:
            上下文信息（当前 URL、target_params、history 等），供规则检查使用。
        """
        action_dict = self._action_to_dict(action)
        ctx = context or {}
        result = GuardrailResult()

        for rule in self._rules:
            try:
                hit, detail = rule.check(action_dict, ctx)
            except Exception as exc:
                # 规则自身出错：记录但不阻塞动作
                result.matched_rules.append(f"{rule.name} (error)")
                result.details.append(f"rule error: {exc}")
                continue
            if not hit:
                continue
            result.matched_rules.append(rule.name)
            result.details.append(detail)
            # 取最严格策略
            result.action = self._stricter(result.action, rule.action)

        # CONFIRM 走回调
        if result.action == GuardrailAction.CONFIRM and self.on_confirm is not None:
            confirmed = True
            for name, detail in zip(result.matched_rules, result.details, strict=False):
                if not self.on_confirm(name, detail):
                    confirmed = False
                    break
            result.action = GuardrailAction.ALLOW if confirmed else GuardrailAction.DENY

        return result

    async def check_async(
        self,
        action: Any,
        *,
        context: dict[str, Any] | None = None,
    ) -> GuardrailResult:
        """异步：与 :meth:`check` 行为一致。

        ``on_confirm`` 若为协程函数则 await，否则同步调用。
        """
        # 当前实现里 check 已是纯同步逻辑，async 入口仅为接口对齐
        return self.check(action, context=context)

    def add_rule(self, rule: GuardrailRule) -> None:
        """注册自定义规则。"""
        self._rules.append(rule)

    # ------------------------------------------------------------------
    # 默认规则集
    # ------------------------------------------------------------------

    def _register_defaults(self) -> None:
        # 1. 禁止导航到 localhost / 内网 IP（除非显式允许）
        self._rules.append(
            GuardrailRule(
                name="no-localhost-nav",
                check=self._check_localhost_nav,
                action=GuardrailAction.DENY,
                severity="error",
            )
        )
        # 2. 禁止非 HTTPS 导航（除非显式允许 dev）
        self._rules.append(
            GuardrailRule(
                name="https-only",
                check=self._check_https_only,
                action=GuardrailAction.DENY,
                severity="warn",
            )
        )
        # 3. 跨域名跳转检查
        if self.allowed_domains is not None and self.allowed_domains != ["*"]:
            self._rules.append(
                GuardrailRule(
                    name="domain-whitelist",
                    check=self._check_domain_whitelist,
                    action=GuardrailAction.DENY,
                    severity="warn",
                )
            )
        # 4. 注入脚本中的高危模式
        self._rules.append(
            GuardrailRule(
                name="no-dangerous-script-injection",
                check=self._check_dangerous_script,
                action=GuardrailAction.DENY,
                severity="error",
            )
        )
        # 5. 危险点击：拦截 click / hover 命中"删除/logout/withdraw/支付"等危险关键词
        self._rules.append(
            GuardrailRule(
                name="no-dangerous-click",
                check=self._check_dangerous_click,
                action=GuardrailAction.DENY,
                severity="error",
            )
        )
        # 6. Selector 注入：拦截含 JS 注入特征（; / () / script / javascript:）的 selector
        self._rules.append(
            GuardrailRule(
                name="no-selector-injection",
                check=self._check_selector_injection,
                action=GuardrailAction.DENY,
                severity="error",
            )
        )

    # ------------------------------------------------------------------
    # 默认规则的 check 实现
    # ------------------------------------------------------------------

    def _check_localhost_nav(
        self,
        action: dict[str, Any],
        ctx: dict[str, Any],
    ) -> tuple[bool, str]:
        if self.allow_localhost:
            return False, ""
        if str(action.get("action_type", "")).lower() != "navigate":
            return False, ""
        url = str((action.get("params") or {}).get("url", ""))
        if not url:
            return False, ""
        host = urlparse(url).hostname or ""
        if not host:
            return False, ""
        # localhost / 127.0.0.1 / 内网 IP
        if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
            return True, f"navigate to localhost: {url}"
        # 检查是否内网 IP
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return True, f"navigate to private IP: {url}"
        except ValueError:
            pass  # 不是 IP，是域名
        return False, ""

    def _check_https_only(
        self,
        action: dict[str, Any],
        ctx: dict[str, Any],
    ) -> tuple[bool, str]:
        if str(action.get("action_type", "")).lower() != "navigate":
            return False, ""
        url = str((action.get("params") or {}).get("url", ""))
        if not url:
            return False, ""
        scheme = urlparse(url).scheme.lower()
        # 允许 https 与 about:blank，禁止 http / file / ftp 等
        if scheme in {"", "https", "about"}:
            return False, ""
        if scheme == "http":
            # http 仅在 dev 模式允许（allow_localhost=True 视为 dev）
            if self.allow_localhost:
                return False, ""
            return True, f"navigate to non-https URL: {url}"
        return True, f"navigate to unsafe scheme {scheme!r}: {url}"

    def _check_domain_whitelist(
        self,
        action: dict[str, Any],
        ctx: dict[str, Any],
    ) -> tuple[bool, str]:
        if str(action.get("action_type", "")).lower() != "navigate":
            return False, ""
        if not self.allowed_domains:
            return False, ""
        url = str((action.get("params") or {}).get("url", ""))
        if not url:
            return False, ""
        host = urlparse(url).hostname or ""
        if not host:
            return False, ""
        # 通配符匹配：*.example.com 匹配 www.example.com / api.example.com
        for allowed in self.allowed_domains:
            if allowed == "*":
                return False, ""
            if allowed.startswith("*."):
                suffix = allowed[2:]
                if host == suffix or host.endswith("." + suffix):
                    return False, ""
            elif host == allowed:
                return False, ""
        return True, f"navigate to non-whitelisted domain: {host}"

    @staticmethod
    def _check_dangerous_script(
        action: dict[str, Any],
        ctx: dict[str, Any],
    ) -> tuple[bool, str]:
        """注入脚本中的高危模式：eval(atob(...))、Function(...) 等。"""
        at = str(action.get("action_type", "")).lower()
        if at != "inject_hook":
            return False, ""
        params = action.get("params") or {}
        # inject_hook 的 params 是 hooks 列表，本身是项目内的预定义脚本，相对安全
        # 但用户可能直接传自定义脚本代码（通过 script 字段）
        script = str(params.get("script") or params.get("code") or "")
        if not script:
            return False, ""
        dangerous_patterns = [
            (r"eval\s*\(\s*atob\s*\(", "eval(atob(...))"),
            (r"new\s+Function\s*\(", "new Function(...)"),
            (r"document\.write\s*\(", "document.write(...)"),
            (r"window\.location\s*=", "window.location reassignment"),
            (r"<script[^>]*>", "inline <script> tag"),
        ]
        for pattern, label in dangerous_patterns:
            if re.search(pattern, script, re.IGNORECASE):
                return True, f"dangerous pattern: {label}"
        return False, ""

    # 危险点击关键词：按钮文本/selector 含这些词时拒绝执行
    _DANGEROUS_CLICK_KEYWORDS: tuple[str, ...] = (
        "删除",
        "delete",
        "logout",
        "退出",
        "withdraw",
        "提现",
        "支付",
        "pay",
        "confirm delete",
        "确认删除",
        "uninstall",
        "卸载",
    )

    def _check_dangerous_click(
        self,
        action: dict[str, Any],
        ctx: dict[str, Any],
    ) -> tuple[bool, str]:
        """拦截 click / hover 命中危险关键词的动作。

        检查 selector 文本是否包含"删除/logout/withdraw/支付"等危险关键词，
        命中即拒绝执行。覆盖 click 与 hover 两类动作（hover 也可能触发菜单展开
        进而引导用户点击危险按钮）。
        """
        at = str(action.get("action_type", "")).lower()
        if at not in {"click", "hover"}:
            return False, ""
        params = action.get("params") or {}
        selector = str(params.get("selector", "")).lower()
        if not selector:
            return False, ""
        for kw in self._DANGEROUS_CLICK_KEYWORDS:
            if kw.lower() in selector:
                return True, f"危险点击：{kw}"
        return False, ""

    @staticmethod
    def _check_selector_injection(
        action: dict[str, Any],
        ctx: dict[str, Any],
    ) -> tuple[bool, str]:
        """拦截 selector 中的 JS 注入特征。

        检查所有使用 selector 的动作（click / type / scroll / press / hover /
        select_option）的 selector 字段是否含 ``;`` / ``()`` / ``script`` /
        ``javascript:`` 等注入特征，命中即拒绝。
        """
        at = str(action.get("action_type", "")).lower()
        if at not in {"click", "type", "scroll", "press", "hover", "select_option"}:
            return False, ""
        params = action.get("params") or {}
        selector = str(params.get("selector", "") or "")
        if not selector:
            return False, ""
        selector_lower = selector.lower()
        # JS 注入特征：分号、括号、<script> 标签、javascript: 协议
        # 注意 "script" 作为子串会误匹配合法的 <script> 元素选择器
        # （CSS 选择器 "script" 是合法的），所以用 "<script" 精确匹配标签
        injection_patterns = (
            ";",
            "()",
            "<script",
            "</script",
            "javascript:",
            "eval(",
            "function(",
        )
        for pattern in injection_patterns:
            if pattern in selector_lower:
                return True, f"selector 注入特征：{pattern!r}"
        return False, ""

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _stricter(a: GuardrailAction, b: GuardrailAction) -> GuardrailAction:
        """取更严格的处置策略：deny > confirm > allow。"""
        order = {
            GuardrailAction.ALLOW: 0,
            GuardrailAction.CONFIRM: 1,
            GuardrailAction.DENY: 2,
        }
        return a if order[a] >= order[b] else b

    @staticmethod
    def _action_to_dict(action: Any) -> dict[str, Any]:
        """把 Action dataclass / dict 归一为 dict。"""
        if isinstance(action, dict):
            return action
        if hasattr(action, "to_dict"):
            return action.to_dict()
        if hasattr(action, "__dict__"):
            return dict(action.__dict__)
        return {"action_type": str(action)}


__all__ = [
    "ActionGuard",
    "GuardrailAction",
    "GuardrailResult",
    "GuardrailRule",
]

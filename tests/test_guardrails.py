"""Tests for the Guardrails: dangerous action blocking and selector injection."""

from __future__ import annotations


def test_guard_denies_localhost_navigation() -> None:
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allow_localhost=False)
    action = {"action_type": "navigate", "params": {"url": "http://127.0.0.1/admin"}}
    result = guard.check(action)
    assert result.denied
    assert "no-localhost-nav" in result.matched_rules


def test_guard_denies_non_https_url() -> None:
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allow_localhost=False)
    action = {"action_type": "navigate", "params": {"url": "http://example.com/login"}}
    result = guard.check(action)
    assert result.denied


def test_guard_allows_https_url() -> None:
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    action = {"action_type": "navigate", "params": {"url": "https://example.com/page"}}
    result = guard.check(action)
    assert not result.denied


def test_guard_domain_whitelist() -> None:
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allowed_domains=["*.example.com", "cdn.test.com"])
    # 允许子域名
    ok = guard.check({"action_type": "navigate", "params": {"url": "https://api.example.com/x"}})
    assert not ok.denied
    # 允许精确匹配
    ok = guard.check({"action_type": "navigate", "params": {"url": "https://cdn.test.com/x"}})
    assert not ok.denied
    # 拒绝其他域名
    blocked = guard.check({"action_type": "navigate", "params": {"url": "https://evil.com/x"}})
    assert blocked.denied
    assert "domain-whitelist" in blocked.matched_rules


def test_guard_blocks_dangerous_script_injection() -> None:
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    action = {
        "action_type": "inject_hook",
        "params": {"script": "eval(atob('cmV0dXJuIDE7'))"},
    }
    result = guard.check(action)
    assert result.denied
    assert "no-dangerous-script-injection" in result.matched_rules


def test_guard_custom_rule_and_confirm_callback() -> None:
    from web_crawler.ai.guardrails import (
        ActionGuard,
        GuardrailAction,
        GuardrailRule,
    )

    custom = GuardrailRule(
        name="no-form-submit",
        check=lambda action, ctx: (
            action.get("action_type") == "navigate"
            and "submit" in str(action.get("params", {}).get("url", "")),
            "submitting forms blocked",
        ),
        action=GuardrailAction.CONFIRM,
    )

    confirmed: list[str] = []

    def on_confirm(name: str, detail: str) -> bool:
        confirmed.append(name)
        return True  # 用户确认放行

    guard = ActionGuard(extra_rules=[custom], on_confirm=on_confirm)
    action = {"action_type": "navigate", "params": {"url": "https://example.com/submit"}}
    result = guard.check(action)
    # CONFIRM 走回调，用户确认 → ALLOW
    assert not result.denied
    assert confirmed == ["no-form-submit"]


def test_guard_blocks_dangerous_click() -> None:
    """点击'删除'按钮应被 no-dangerous-click 规则拦截。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    action = {
        "action_type": "click",
        "params": {"selector": "button:has-text('删除')"},
    }
    result = guard.check(action)
    assert result.denied
    assert "no-dangerous-click" in result.matched_rules


def test_guard_blocks_dangerous_click_english() -> None:
    """点击 logout / delete / withdraw 按钮也应被拦截。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    for selector in [
        "button:has-text('Logout')",
        "a:has-text('Delete account')",
        "button:has-text('Withdraw')",
    ]:
        action = {"action_type": "click", "params": {"selector": selector}}
        result = guard.check(action)
        assert result.denied, f"应拦截 {selector}"


def test_guard_allows_safe_click() -> None:
    """安全点击（如 'Login' / 'Search'）不应被拦截。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    action = {
        "action_type": "click",
        "params": {"selector": "button:has-text('Login')"},
    }
    result = guard.check(action)
    assert not result.denied


def test_guard_blocks_selector_injection() -> None:
    """selector 含 JS 注入特征（; / () / <script> / javascript:）应被拦截。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    bad_selectors = [
        "button;alert(1)",
        "img<script>alert(1)</script>",
        "a[javascript:alert(1)]",
        "input[onerror=eval(]",
    ]
    for selector in bad_selectors:
        action = {"action_type": "click", "params": {"selector": selector}}
        result = guard.check(action)
        assert result.denied, f"应拦截 selector 注入：{selector}"
        assert "no-selector-injection" in result.matched_rules


def test_guard_blocks_injection_on_type_action() -> None:
    """selector 注入规则应覆盖 type / hover 等所有使用 selector 的动作。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    action = {
        "action_type": "type",
        "params": {"selector": "input;evil()", "text": "x"},
    }
    result = guard.check(action)
    assert result.denied
    assert "no-selector-injection" in result.matched_rules


def test_guard_allows_normal_selector() -> None:
    """正常 selector 不应被注入规则误拦。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    for selector in [
        "button#submit",
        "input.login-form[name='user']",
        ".menu > li:nth-child(2)",
        "select#country",
        "script",  # 合法的 <script> 元素选择器，不应误判为注入
        "div.description",  # 类名含 "script" 子串的合法 selector
    ]:
        action = {"action_type": "click", "params": {"selector": selector}}
        result = guard.check(action)
        assert not result.denied, f"不应拦截合法 selector：{selector}"

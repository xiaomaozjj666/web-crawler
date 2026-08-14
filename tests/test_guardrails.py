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


# ===========================================================================
# 扩展：GuardrailResult 属性 / to_dict
# ===========================================================================


def test_guardrail_result_needs_confirm_property() -> None:
    """needs_confirm 在 action=CONFIRM 时为 True。"""
    from web_crawler.ai.guardrails import GuardrailAction, GuardrailResult

    result = GuardrailResult(action=GuardrailAction.CONFIRM)
    assert result.needs_confirm is True
    assert result.denied is False

    allow_result = GuardrailResult(action=GuardrailAction.ALLOW)
    assert allow_result.needs_confirm is False
    assert allow_result.denied is False

    deny_result = GuardrailResult(action=GuardrailAction.DENY)
    assert deny_result.needs_confirm is False
    assert deny_result.denied is True


def test_guardrail_result_to_dict() -> None:
    """to_dict 序列化 action/matched_rules/details。"""
    from web_crawler.ai.guardrails import GuardrailAction, GuardrailResult

    result = GuardrailResult(
        action=GuardrailAction.DENY,
        matched_rules=["rule-a"],
        details=["detail-a"],
    )
    d = result.to_dict()
    assert d["action"] == "deny"
    assert d["matched_rules"] == ["rule-a"]
    assert d["details"] == ["detail-a"]


# ===========================================================================
# 扩展：check 方法 - 规则异常处理 / confirm 回调拒绝
# ===========================================================================


def test_guard_check_swallows_rule_exception() -> None:
    """规则 check 抛异常时记录 (error) 但不阻塞动作。"""
    from web_crawler.ai.guardrails import (
        ActionGuard,
        GuardrailAction,
        GuardrailRule,
    )

    def _boom(action, ctx):
        raise RuntimeError("rule crashed")

    bad_rule = GuardrailRule(name="crashy", check=_boom, action=GuardrailAction.DENY)
    guard = ActionGuard(extra_rules=[bad_rule])
    result = guard.check({"action_type": "navigate", "params": {"url": "https://example.com"}})
    # 异常规则不应阻塞
    assert not result.denied
    assert any("crashy" in name and "error" in name for name in result.matched_rules)


def test_guard_confirm_callback_returns_false_denies() -> None:
    """on_confirm 返回 False 时降级为 DENY。"""
    from web_crawler.ai.guardrails import (
        ActionGuard,
        GuardrailAction,
        GuardrailRule,
    )

    custom = GuardrailRule(
        name="needs-confirm",
        check=lambda action, ctx: (True, "needs user confirm"),
        action=GuardrailAction.CONFIRM,
    )
    guard = ActionGuard(extra_rules=[custom], on_confirm=lambda name, detail: False)
    result = guard.check({"action_type": "navigate", "params": {"url": "https://example.com"}})
    assert result.denied


def test_guard_confirm_callback_all_rules_confirmed_allows() -> None:
    """多条 confirm 规则全部确认时 ALLOW。"""
    from web_crawler.ai.guardrails import (
        ActionGuard,
        GuardrailAction,
        GuardrailRule,
    )

    rules = [
        GuardrailRule(
            name="c1", check=lambda a, c: (True, "1"), action=GuardrailAction.CONFIRM
        ),
        GuardrailRule(
            name="c2", check=lambda a, c: (True, "2"), action=GuardrailAction.CONFIRM
        ),
    ]
    guard = ActionGuard(extra_rules=rules, on_confirm=lambda name, detail: True)
    result = guard.check({"action_type": "navigate", "params": {"url": "https://example.com"}})
    assert not result.denied


# ===========================================================================
# 扩展：async check / add_rule
# ===========================================================================


def test_guard_check_async_behaves_like_sync() -> None:
    """check_async 行为与 check 一致。"""
    import asyncio

    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    action = {"action_type": "navigate", "params": {"url": "http://example.com"}}
    result = asyncio.run(guard.check_async(action))
    assert result.denied


def test_guard_add_rule_appends() -> None:
    """add_rule 追加自定义规则。"""
    from web_crawler.ai.guardrails import (
        ActionGuard,
        GuardrailAction,
        GuardrailRule,
    )

    guard = ActionGuard()
    initial_count = len(guard._rules)
    guard.add_rule(
        GuardrailRule(
            name="custom-block",
            check=lambda a, c: (a.get("action_type") == "navigate", "blocked"),
            action=GuardrailAction.DENY,
        )
    )
    assert len(guard._rules) == initial_count + 1


# ===========================================================================
# 扩展：_check_localhost_nav 分支
# ===========================================================================


def test_guard_localhost_allows_when_allow_localhost_true() -> None:
    """allow_localhost=True 时 localhost 导航不被拦截。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allow_localhost=True)
    result = guard.check({"action_type": "navigate", "params": {"url": "http://127.0.0.1/admin"}})
    assert not result.denied


def test_guard_localhost_not_navigate_action_skips() -> None:
    """非 navigate 动作不触发 localhost 检查。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allow_localhost=False)
    result = guard.check({"action_type": "click", "params": {"selector": "a"}})
    assert not result.denied


def test_guard_localhost_empty_url_skips() -> None:
    """空 URL 不触发 localhost 检查。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allow_localhost=False)
    result = guard.check({"action_type": "navigate", "params": {"url": ""}})
    assert not result.denied


def test_guard_localhost_no_host_skips() -> None:
    """URL 无 host 时不触发 localhost 检查。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allow_localhost=False)
    result = guard.check({"action_type": "navigate", "params": {"url": "about:blank"}})
    assert not result.denied


def test_guard_localhost_blocks_private_ip() -> None:
    """内网私有 IP 被拦截。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allow_localhost=False)
    for ip in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
        result = guard.check({"action_type": "navigate", "params": {"url": f"http://{ip}/"}})
        assert result.denied, f"应拦截 {ip}"


# ===========================================================================
# 扩展：_check_https_only 分支
# ===========================================================================


def test_guard_https_only_empty_url_skips() -> None:
    """空 URL 不触发 https 检查。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    result = guard.check({"action_type": "navigate", "params": {"url": ""}})
    assert not result.denied


def test_guard_https_only_http_with_allow_localhost_allowed() -> None:
    """allow_localhost=True 时 http URL 允许（dev 模式）。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allow_localhost=True)
    result = guard.check({"action_type": "navigate", "params": {"url": "http://example.com"}})
    assert not result.denied


def test_guard_https_only_blocks_unsafe_scheme() -> None:
    """file/ftp 等非 https/http/about 协议被拦截。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    result = guard.check({"action_type": "navigate", "params": {"url": "file:///etc/passwd"}})
    assert result.denied


def test_guard_https_only_about_blank_allowed() -> None:
    """about:blank 允许。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    result = guard.check({"action_type": "navigate", "params": {"url": "about:blank"}})
    assert not result.denied


# ===========================================================================
# 扩展：_check_domain_whitelist 分支
# ===========================================================================


def test_guard_whitelist_not_navigate_skips() -> None:
    """非 navigate 动作不触发域名白名单检查。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allowed_domains=["example.com"])
    result = guard.check({"action_type": "click", "params": {"selector": "a"}})
    assert not result.denied


def test_guard_whitelist_empty_url_skips() -> None:
    """空 URL 不触发域名白名单检查。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allowed_domains=["example.com"])
    result = guard.check({"action_type": "navigate", "params": {"url": ""}})
    assert not result.denied


def test_guard_whitelist_no_host_skips() -> None:
    """URL 无 host 时不触发白名单检查。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allowed_domains=["example.com"])
    result = guard.check({"action_type": "navigate", "params": {"url": "about:blank"}})
    assert not result.denied


def test_guard_whitelist_wildcard_star_allowed() -> None:
    """allowed_domains 含 '*' 时全部允许。"""
    from web_crawler.ai.guardrails import ActionGuard

    # ["*"] 不会注册 domain-whitelist 规则（__init__ 中过滤）
    guard = ActionGuard(allowed_domains=["*"])
    result = guard.check({"action_type": "navigate", "params": {"url": "https://evil.com/"}})
    assert not result.denied


def test_guard_whitelist_star_mixed_with_domain_allows_any() -> None:
    """allowed_domains=['*', 'example.com'] 时 '*' 分支允许任意域名。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allowed_domains=["*", "example.com"])
    # '*' 在遍历时命中 → return False, ""（放行），覆盖 _check_domain_whitelist 的 * 分支
    result = guard.check({"action_type": "navigate", "params": {"url": "https://evil.com/"}})
    assert not result.denied


def test_guard_whitelist_empty_allowed_domains_skips() -> None:
    """空 allowed_domains 列表不触发拦截。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allowed_domains=[])
    result = guard.check({"action_type": "navigate", "params": {"url": "https://example.com/"}})
    assert not result.denied


# ===========================================================================
# 扩展：_check_dangerous_script 分支
# ===========================================================================


def test_guard_dangerous_script_empty_script_skips() -> None:
    """inject_hook 但 script 为空时不触发。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    result = guard.check({"action_type": "inject_hook", "params": {"script": ""}})
    assert not result.denied


def test_guard_dangerous_script_no_pattern_matched() -> None:
    """inject_hook 含安全脚本时不触发。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    result = guard.check(
        {"action_type": "inject_hook", "params": {"script": "console.log('safe')"}}
    )
    assert not result.denied


def test_guard_dangerous_script_not_inject_hook_skips() -> None:
    """非 inject_hook 动作不触发危险脚本检查。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    result = guard.check({"action_type": "click", "params": {"selector": "a"}})
    assert not result.denied


# ===========================================================================
# 扩展：_check_dangerous_click 分支
# ===========================================================================


def test_guard_dangerous_click_empty_selector_skips() -> None:
    """click 但 selector 为空时不触发。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    result = guard.check({"action_type": "click", "params": {"selector": ""}})
    assert not result.denied


def test_guard_dangerous_click_not_click_hover_skips() -> None:
    """非 click/hover 动作不触发危险点击检查。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    result = guard.check({"action_type": "type", "params": {"selector": "input"}})
    assert not result.denied


# ===========================================================================
# 扩展：_check_selector_injection 分支
# ===========================================================================


def test_guard_selector_injection_empty_selector_skips() -> None:
    """selector 为空时不触发注入检查。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    result = guard.check({"action_type": "click", "params": {"selector": ""}})
    assert not result.denied


def test_guard_selector_injection_not_selector_action_skips() -> None:
    """非使用 selector 的动作不触发注入检查。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    result = guard.check({"action_type": "navigate", "params": {"url": "https://example.com"}})
    assert not result.denied


# ===========================================================================
# 扩展：_action_to_dict 分支
# ===========================================================================


def test_guard_action_to_dict_dict_passthrough() -> None:
    """dict 直接返回。"""
    from web_crawler.ai.guardrails import ActionGuard

    action = {"action_type": "click", "params": {"selector": "a"}}
    assert ActionGuard._action_to_dict(action) is action


def test_guard_action_to_dict_to_dict_method() -> None:
    """有 to_dict 方法的对象调用 to_dict。"""

    class FakeAction:
        def to_dict(self) -> dict:
            return {"action_type": "fake"}

    from web_crawler.ai.guardrails import ActionGuard

    assert ActionGuard._action_to_dict(FakeAction()) == {"action_type": "fake"}


def test_guard_action_to_dict_object_with_dict_attr() -> None:
    """有 __dict__ 的对象转为 dict。"""
    from web_crawler.ai.guardrails import ActionGuard

    class FakeAction:
        def __init__(self) -> None:
            self.action_type = "click"
            self.params = {"selector": "a"}

    result = ActionGuard._action_to_dict(FakeAction())
    assert result["action_type"] == "click"
    assert result["params"] == {"selector": "a"}


def test_guard_action_to_dict_fallback_to_str() -> None:
    """无 dict 属性的对象回退到 {'action_type': str(obj)}。"""
    from web_crawler.ai.guardrails import ActionGuard

    # int 无 __dict__ 也无 to_dict
    result = ActionGuard._action_to_dict(42)  # type: ignore[arg-type]
    assert result == {"action_type": "42"}


# ===========================================================================
# 扩展：new_tab 动作也应过 URL 护栏（防绕过）
# ===========================================================================


def test_guard_denies_new_tab_to_localhost() -> None:
    """new_tab 导航到 localhost 应被拦截（原 navigate 独享检查的绕过）。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allow_localhost=False)
    result = guard.check(
        {"action_type": "new_tab", "params": {"url": "http://127.0.0.1/admin"}}
    )
    assert result.denied
    assert "no-localhost-nav" in result.matched_rules


def test_guard_denies_new_tab_non_https() -> None:
    """new_tab 导航到 http 应被拦截。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    result = guard.check(
        {"action_type": "new_tab", "params": {"url": "http://example.com/login"}}
    )
    assert result.denied
    assert "https-only" in result.matched_rules


def test_guard_new_tab_domain_whitelist() -> None:
    """new_tab 跨白名单域名应被拦截，白名单内放行。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allowed_domains=["example.com"])
    blocked = guard.check(
        {"action_type": "new_tab", "params": {"url": "https://evil.com/x"}}
    )
    assert blocked.denied
    assert "domain-whitelist" in blocked.matched_rules
    ok = guard.check(
        {"action_type": "new_tab", "params": {"url": "https://example.com/x"}}
    )
    assert not ok.denied


def test_guard_check_navigation_url_api() -> None:
    """check_navigation_url 对裸 URL 做 URL 类规则检查。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allowed_domains=["example.com"], allow_localhost=False)
    assert guard.check_navigation_url("http://127.0.0.1/x").denied
    assert guard.check_navigation_url("https://evil.com/x").denied
    assert not guard.check_navigation_url("https://example.com/x").denied


# ===========================================================================
# 扩展：编码 IP（十进制/十六进制/八进制）SSRF 绕过
# ===========================================================================


def test_guard_blocks_decimal_encoded_ip() -> None:
    """十进制编码的 127.0.0.1（2130706433）应被拦截。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allow_localhost=False)
    result = guard.check(
        {"action_type": "navigate", "params": {"url": "http://2130706433/"}}
    )
    assert result.denied
    assert "no-localhost-nav" in result.matched_rules


def test_guard_blocks_hex_encoded_ip() -> None:
    """十六进制编码的 127.0.0.1（0x7f000001）应被拦截。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allow_localhost=False)
    result = guard.check(
        {"action_type": "navigate", "params": {"url": "http://0x7f000001/"}}
    )
    assert result.denied


def test_guard_blocks_octal_dotted_ip() -> None:
    """八进制点分 127.0.0.1（0177.0.0.1）应被拦截。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allow_localhost=False)
    result = guard.check(
        {"action_type": "navigate", "params": {"url": "http://0177.0.0.1/"}}
    )
    assert result.denied


def test_guard_encoded_ip_new_tab_also_blocked() -> None:
    """new_tab 使用编码 IP 同样应被拦截。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard(allow_localhost=False)
    result = guard.check(
        {"action_type": "new_tab", "params": {"url": "http://2130706433/"}}
    )
    assert result.denied


# ===========================================================================
# 扩展：CONFIRM 无 on_confirm 时降级 DENY + async on_confirm
# ===========================================================================


def test_guard_confirm_without_callback_denies() -> None:
    """CONFIRM 规则命中但未提供 on_confirm 时，应降级为 DENY 而非放行。"""
    from web_crawler.ai.guardrails import ActionGuard, GuardrailAction, GuardrailRule

    custom = GuardrailRule(
        name="needs-confirm",
        check=lambda action, ctx: (True, "needs user confirm"),
        action=GuardrailAction.CONFIRM,
    )
    guard = ActionGuard(extra_rules=[custom])  # 无 on_confirm
    result = guard.check({"action_type": "navigate", "params": {"url": "https://example.com"}})
    assert result.denied


def test_guard_check_async_awaits_coroutine_confirm() -> None:
    """check_async 对协程版 on_confirm 应 await 而非当 truthy 处理。"""
    import asyncio

    from web_crawler.ai.guardrails import ActionGuard, GuardrailAction, GuardrailRule

    confirmed: list[str] = []

    async def on_confirm(name: str, detail: str) -> bool:
        confirmed.append(name)
        return True

    custom = GuardrailRule(
        name="needs-confirm-async",
        check=lambda action, ctx: (True, "needs confirm"),
        action=GuardrailAction.CONFIRM,
    )
    guard = ActionGuard(extra_rules=[custom], on_confirm=on_confirm)
    result = asyncio.run(
        guard.check_async({"action_type": "navigate", "params": {"url": "https://example.com"}})
    )
    assert not result.denied
    assert confirmed == ["needs-confirm-async"]


def test_guard_check_async_confirm_without_callback_denies() -> None:
    """check_async 下 CONFIRM 规则命中但无 on_confirm 时，应降级为 DENY。"""
    import asyncio

    from web_crawler.ai.guardrails import ActionGuard, GuardrailAction, GuardrailRule

    custom = GuardrailRule(
        name="needs-confirm-async",
        check=lambda action, ctx: (True, "needs confirm"),
        action=GuardrailAction.CONFIRM,
    )
    guard = ActionGuard(extra_rules=[custom])  # 无 on_confirm
    result = asyncio.run(
        guard.check_async({"action_type": "navigate", "params": {"url": "https://example.com"}})
    )
    assert result.denied


def test_guard_check_async_sync_confirm_callback_called_directly() -> None:
    """check_async 对同步 on_confirm 直接调用（不 await）。"""
    import asyncio

    from web_crawler.ai.guardrails import ActionGuard, GuardrailAction, GuardrailRule

    calls: list[str] = []

    def on_confirm(name: str, detail: str) -> bool:
        calls.append(name)
        return True

    custom = GuardrailRule(
        name="sync-confirm",
        check=lambda action, ctx: (True, "needs confirm"),
        action=GuardrailAction.CONFIRM,
    )
    guard = ActionGuard(extra_rules=[custom], on_confirm=on_confirm)
    result = asyncio.run(
        guard.check_async({"action_type": "navigate", "params": {"url": "https://example.com"}})
    )
    assert not result.denied
    assert calls == ["sync-confirm"]


def test_guard_check_async_confirm_callback_returns_false_denies() -> None:
    """check_async 下异步 on_confirm 返回 False 时降级为 DENY。"""
    import asyncio

    from web_crawler.ai.guardrails import ActionGuard, GuardrailAction, GuardrailRule

    async def on_confirm(name: str, detail: str) -> bool:
        return False

    custom = GuardrailRule(
        name="deny-me",
        check=lambda action, ctx: (True, "needs confirm"),
        action=GuardrailAction.CONFIRM,
    )
    guard = ActionGuard(extra_rules=[custom], on_confirm=on_confirm)
    result = asyncio.run(
        guard.check_async({"action_type": "navigate", "params": {"url": "https://example.com"}})
    )
    assert result.denied


# ===========================================================================
# 扩展：_decode_encoded_ip 各失败分支（空 host / 非法 hex / 越界 / 非法八进制）
# ===========================================================================


def test_decode_encoded_ip_empty_host_returns_none() -> None:
    """空 host 直接返回 None。"""
    from web_crawler.ai.guardrails import ActionGuard

    assert ActionGuard._decode_encoded_ip("") is None
    assert ActionGuard._decode_encoded_ip("   ") is None


def test_decode_encoded_ip_invalid_hex_returns_none() -> None:
    """非法十六进制编码返回 None。"""
    from web_crawler.ai.guardrails import ActionGuard

    assert ActionGuard._decode_encoded_ip("0xzzzz") is None
    assert ActionGuard._decode_encoded_ip("0x") is None


def test_decode_encoded_ip_decimal_out_of_ip_range_returns_none() -> None:
    """十进制整数超出 IPv4/IPv6 范围时返回 None。"""
    from web_crawler.ai.guardrails import ActionGuard

    assert ActionGuard._decode_encoded_ip(str(10**40)) is None


def test_decode_encoded_ip_invalid_octal_returns_none() -> None:
    """八进制点分含非法数字（如 9）时返回 None。"""
    from web_crawler.ai.guardrails import ActionGuard

    assert ActionGuard._decode_encoded_ip("0177.0.0.9") is None


def test_decode_encoded_ip_wrong_shape_returns_none() -> None:
    """非 4 段或段值越界的八进制点分返回 None。"""
    from web_crawler.ai.guardrails import ActionGuard

    assert ActionGuard._decode_encoded_ip("0177.0.1") is None
    assert ActionGuard._decode_encoded_ip("0777.0777.0777.0777") is None


# ===========================================================================
# 扩展：_host_is_private DNS 兜底分支（编码 IP 形态 host 的 getaddrinfo 解析）
# ===========================================================================


def test_host_is_private_dns_fallback_resolves_private() -> None:
    """编码 IP 形态 host（127.1）经 DNS 兜底解析为私网地址 → 判定为私网。"""
    from unittest import mock

    from web_crawler.ai.guardrails import ActionGuard

    addrs = [(2, 1, 6, "", ("127.0.0.1", 0))]
    with mock.patch("web_crawler.ai.guardrails.socket.getaddrinfo", return_value=addrs):
        assert ActionGuard._host_is_private("127.1") is True


def test_host_is_private_dns_fallback_resolves_public() -> None:
    """DNS 兜底解析到公网地址 → 非私网。"""
    from unittest import mock

    from web_crawler.ai.guardrails import ActionGuard

    addrs = [(2, 1, 6, "", ("93.184.216.34", 0))]
    with mock.patch("web_crawler.ai.guardrails.socket.getaddrinfo", return_value=addrs):
        assert ActionGuard._host_is_private("127.1") is False


def test_host_is_private_dns_getaddrinfo_oserror_returns_false() -> None:
    """getaddrinfo 抛 OSError（解析失败）→ 视为非私网（不阻塞导航）。"""
    from unittest import mock

    from web_crawler.ai.guardrails import ActionGuard

    with mock.patch(
        "web_crawler.ai.guardrails.socket.getaddrinfo",
        side_effect=OSError("resolve failed"),
    ):
        assert ActionGuard._host_is_private("127.1") is False


def test_host_is_private_dns_invalid_addr_entry_skipped() -> None:
    """DNS 返回的地址条目无法解析为 IP 时跳过该条目并返回 False。"""
    from unittest import mock

    from web_crawler.ai.guardrails import ActionGuard

    addrs = [(2, 1, 6, "", ("not-an-ip", 0))]
    with mock.patch("web_crawler.ai.guardrails.socket.getaddrinfo", return_value=addrs):
        assert ActionGuard._host_is_private("127.1") is False


def test_host_is_private_decode_result_invalid_ip_skipped() -> None:
    """_decode_encoded_ip 返回的字符串无法解析为 IP 时跳过直译分支（防御性）。"""
    from unittest import mock

    from web_crawler.ai.guardrails import ActionGuard

    with (
        mock.patch.object(ActionGuard, "_decode_encoded_ip", return_value="999.999.999.999"),
        mock.patch(
            "web_crawler.ai.guardrails.socket.getaddrinfo",
            side_effect=OSError("resolve failed"),
        ),
    ):
        assert ActionGuard._host_is_private("127.1") is False


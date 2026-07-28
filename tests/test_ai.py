"""Tests for the additive AI layer (no network; a fake LLM provider is used)."""

from __future__ import annotations

from typing import Any

import pytest

from web_crawler import (
    AIExtractor,
    DeepSeekProvider,
    LLMMessage,
    LLMResponse,
    Response,
    Selector,
    available_providers,
    get_provider,
    register_provider,
)
from web_crawler.ai.llm import _normalize_messages


class FakeProvider:
    """Deterministic provider that replays canned JSON replies (no HTTP)."""

    model = "fake-model"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append(_normalize_messages(messages))
        content = self._replies.pop(0) if self._replies else "{}"
        return LLMResponse(content=content, model=self.model)


# -- llm layer --------------------------------------------------------------
def test_normalize_messages_accepts_str_dict_and_message() -> None:
    out = _normalize_messages([LLMMessage("system", "s"), {"role": "user", "content": "u"}, "bare"])
    assert out == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "user", "content": "bare"},
    ]
    assert _normalize_messages("hello") == [{"role": "user", "content": "hello"}]


def test_get_provider_defaults_to_deepseek_v4_pro() -> None:
    provider = get_provider(api_key="dummy")
    assert isinstance(provider, DeepSeekProvider)
    assert provider.model == "deepseek-v4-pro"
    assert "deepseek" in available_providers()


def test_deepseek_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the test hermetic: ignore any real key from the environment / .env file.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("web_crawler.ai.llm._DOTENV_LOADED", True)
    provider = DeepSeekProvider(api_key="")
    with pytest.raises(RuntimeError, match="no API key"):
        provider.chat("hi")


def test_register_custom_provider() -> None:
    register_provider("fakereg", lambda **kw: FakeProvider(["{}"]))
    assert "fakereg" in available_providers()
    assert isinstance(get_provider("fakereg"), FakeProvider)


# -- extractor --------------------------------------------------------------
_HTML = (
    '<html><body><h1 class="title">Hello World</h1>'
    '<a class="more" href="/next">next</a></body></html>'
)


def _response() -> Response:
    return Response("https://example.com", 200, _HTML.encode("utf-8"))


def test_extractor_generates_and_validates_selectors() -> None:
    provider = FakeProvider(['{"title": "h1.title", "link": "a.more::attr(href)"}'])
    extractor = AIExtractor(provider=provider)
    result = extractor.extract(_response(), {"title": "heading", "link": "the link"})

    assert result.ok
    assert result.data["title"] == "Hello World"
    assert result.data["link"] == "/next"
    assert result.selectors["link"] == "a.more::attr(href)"


def test_extractor_self_heals_failing_field() -> None:
    # First reply has a wrong selector for `title`; heal round fixes it.
    provider = FakeProvider(
        [
            '{"title": "h1.wrong", "link": "a.more::attr(href)"}',
            '{"title": "h1.title"}',
        ]
    )
    extractor = AIExtractor(provider=provider)
    result = extractor.extract(_response(), {"title": "heading", "link": "the link"})

    assert result.data["title"] == "Hello World"
    assert result.rounds == 2
    assert result.ok


def test_extractor_reports_missing_when_unhealable() -> None:
    provider = FakeProvider(['{"title": "h1.nope"}'])
    extractor = AIExtractor(provider=provider, max_heal_rounds=0)
    result = extractor.extract(_response(), {"title": "heading"})

    assert not result.ok
    assert "title" in result.missing


def test_extractor_accepts_selector_directly() -> None:
    provider = FakeProvider(['{"title": "h1.title"}'])
    extractor = AIExtractor(provider=provider)
    sel = Selector(_HTML, url="https://example.com")
    result = extractor.extract(sel, {"title": "heading"})
    assert result.data["title"] == "Hello World"


# -- agent: block detection & human handoff (BrowserAct-inspired) -----------
class FakeFetcher:
    """Minimal fetcher returning canned responses; records fetched URLs."""

    def __init__(self, resp: Response) -> None:
        self._resp = resp
        self.fetched: list[str] = []

    def get(self, url: str) -> Response:
        self.fetched.append(url)
        return self._resp


class ExplodingProvider:
    """Provider that fails if used — proves extraction was skipped."""

    model = "boom"

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        raise AssertionError("extractor must not be called on a blocked page")


def test_detect_block_on_captcha_body() -> None:
    from web_crawler.ai.agent import detect_block

    resp = Response("https://x.example", 200, b"<html>Please complete the captcha</html>")
    assert detect_block(resp) is not None


def test_detect_block_on_forbidden_status() -> None:
    from web_crawler.ai.agent import detect_block

    resp = Response("https://x.example", 403, b"<html>nope</html>")
    assert detect_block(resp) == "http 403"


def test_detect_block_none_on_normal_page() -> None:
    from web_crawler.ai.agent import detect_block

    assert detect_block(_response()) is None


def test_agent_hands_off_to_human_on_block() -> None:
    from web_crawler import AIScrapeAgent

    captcha = Response("https://x.example", 200, b"<html>hcaptcha challenge</html>")
    fetcher = FakeFetcher(captcha)
    seen: list[Any] = []
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=AIExtractor(provider=ExplodingProvider()),
        respect_robots=False,
        min_delay=0.0,
        on_block=seen.append,
    )
    result = agent.scrape("https://x.example", {"title": "heading"})

    assert result.needs_human is True
    assert result.block_reason is not None
    assert not result.ok
    assert result.data == {}
    assert len(seen) == 1  # on_block callback fired once


def test_agent_extracts_normally_when_not_blocked() -> None:
    from web_crawler import AIScrapeAgent

    fetcher = FakeFetcher(_response())
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=AIExtractor(provider=FakeProvider(['{"title": "h1.title"}'])),
        respect_robots=False,
        min_delay=0.0,
    )
    result = agent.scrape("https://example.com", {"title": "heading"})

    assert not result.needs_human
    assert result.data["title"] == "Hello World"


# -- camoufox fetcher (graceful degrade when camoufox is absent) ------------
def test_camoufox_fetcher_requires_camoufox() -> None:
    from web_crawler import CamoufoxFetcher
    from web_crawler.compat import HAS_CAMOUFOX

    if HAS_CAMOUFOX:
        # Only build launch kwargs; do not actually launch a browser in CI.
        f = CamoufoxFetcher(os="windows", humanize=True, geoip=True, block_webrtc=True)
        kwargs = f._launch_kwargs()
        assert kwargs["os"] == "windows"
        assert kwargs["humanize"] is True
        assert kwargs["geoip"] is True
        assert kwargs["block_webrtc"] is True
    else:
        with pytest.raises(ImportError, match="camoufox"):
            CamoufoxFetcher()


# -- DomPruner: DOM 焦点裁剪（Skyvern/browser-use 风格） --------------------
_HTML_FULL = """
<!doctype html>
<html><head>
<title>Anti-Content Test Page</title>
<script src="https://cdn.example.com/vendor.min.js"></script>
<script src="https://api.example.com/sign.js"></script>
<style>body { color: red; }</style>
</head><body>
<div class="nav"><a href="/home">Home</a><a href="/about">About</a></div>
<form id="login-form" action="/login">
  <input name="username" type="text">
  <input name="password" type="password">
  <input name="anti_content" type="hidden" value="encrypted_value_here">
  <button type="submit">Login</button>
</form>
<div class="footer">Copyright 2026</div>
<script>window.__sign = function(x) { return btoa(x); };</script>
</body></html>
"""


def test_dom_pruner_extracts_candidates_and_truncates() -> None:
    from web_crawler.ai.dom_pruner import DomPruner

    pruner = DomPruner(max_chars=600, max_candidates=10)
    result = pruner.prune(_HTML_FULL)
    assert result.element_count > 0
    assert result.kept_count <= 10
    assert len(result.text) <= 700  # 600 + 截断标记
    assert "script" in result.text or "input" in result.text or "form" in result.text


def test_dom_pruner_prioritizes_crypto_keywords() -> None:
    from web_crawler.ai.dom_pruner import DomPruner

    pruner = DomPruner(max_chars=8000, max_candidates=20)
    result = pruner.prune(_HTML_FULL)
    # top_score 应该比较高，因为页面有 anti_content / sign 等关键词
    assert result.top_score > 3.0


def test_dom_pruner_handles_empty_html() -> None:
    from web_crawler.ai.dom_pruner import DomPruner

    pruner = DomPruner()
    result = pruner.prune("")
    assert result.text == ""
    assert result.element_count == 0
    assert result.kept_count == 0


def test_dom_pruner_llm_rerank_fallback_on_error() -> None:
    """LLM 评分失败时应自动降级为规则评分。"""
    from web_crawler.ai.dom_pruner import DomPruner

    class BrokenProvider:
        def chat(self, messages: Any, **kw: Any) -> Any:
            raise RuntimeError("llm broken")

    pruner = DomPruner(max_chars=8000, enable_llm_rank=True, provider=BrokenProvider())
    result = pruner.prune(_HTML_FULL)
    # 不应抛异常，结果仍合法
    assert result.element_count > 0


# -- Checkpoint: 断点续跑 ---------------------------------------------------
def test_checkpoint_roundtrip(tmp_path) -> None:
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(tmp_path / "cps", keep=3)
    cp = Checkpoint(
        task_id="task-1",
        step=5,
        url="https://example.com/page",
        task="extract sign param",
        target_params_found={"sign": "abc123"},
        target_params=["sign"],
        hooks=["fetch_hook", "xhr_hook"],
        history=[{"step": 1, "action": "navigate"}],
        cumulative_summary="已注入 fetch_hook",
    )
    saved_path = store.save(cp)
    assert saved_path.exists()

    loaded = store.load_latest("task-1")
    assert loaded is not None
    assert loaded.step == 5
    assert loaded.url == "https://example.com/page"
    assert loaded.target_params_found == {"sign": "abc123"}
    assert loaded.hooks == ["fetch_hook", "xhr_hook"]


def test_checkpoint_rotation_keeps_recent(tmp_path) -> None:
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(tmp_path / "cps", keep=2)
    for step in range(1, 5):
        store.save(Checkpoint(task_id="task-2", step=step, url="https://x.example"))
    files = store.list_checkpoints("task-2")
    assert len(files) == 2
    # 仅保留最新两个
    loaded = store.load_latest("task-2")
    assert loaded is not None
    assert loaded.step == 4


def test_checkpoint_load_at_specific_step(tmp_path) -> None:
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    for step in [1, 5, 10]:
        store.save(Checkpoint(task_id="t3", step=step, url=f"https://x.example/{step}"))
    loaded = store.load_at("t3", 5)
    assert loaded is not None
    assert loaded.url == "https://x.example/5"
    # 加载不存在的 step
    assert store.load_at("t3", 99) is None


def test_checkpoint_manager_disabled_returns_none() -> None:
    from web_crawler.ai.checkpoint import CheckpointManager

    mgr = CheckpointManager(enable=False)
    mgr.task_id = "some-task"
    assert mgr.load_latest() is None


def test_checkpoint_clear(tmp_path) -> None:
    from web_crawler.ai.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(tmp_path / "cps")
    store.save(Checkpoint(task_id="clear-me", step=1, url="https://x.example"))
    assert store.load_latest("clear-me") is not None
    store.clear("clear-me")
    assert store.load_latest("clear-me") is None


# -- Budget: Token 预算管理 --------------------------------------------------
def test_budget_records_and_summarizes() -> None:
    from web_crawler.ai.budget import BudgetTracker, TokenBudget

    tracker = BudgetTracker(budget=TokenBudget(total=1000, per_step=500))
    tracker.record_call(step=1, prompt_text="hello" * 100, completion_text="hi" * 20)
    summary = tracker.summary()
    assert summary["used_total"] > 0
    assert summary["calls"] == 1
    assert summary["current_step"] == 1


def test_budget_detects_step_overflow() -> None:
    from web_crawler.ai.budget import BudgetPolicy, BudgetTracker, TokenBudget

    tracker = BudgetTracker(
        budget=TokenBudget(total=10000, per_step=100, policy=BudgetPolicy.COMPRESS)
    )
    # 一个超大 prompt 让单步超 100 token
    tracker.record_call(step=1, prompt_text="x" * 1000, completion_text="")
    assert tracker.should_compress()
    assert not tracker.should_stop()


def test_budget_stop_policy() -> None:
    from web_crawler.ai.budget import BudgetPolicy, BudgetTracker, TokenBudget

    tracker = BudgetTracker(budget=TokenBudget(total=100, per_step=10, policy=BudgetPolicy.STOP))
    tracker.record_call(step=1, prompt_text="x" * 1000, completion_text="")
    assert tracker.should_stop()
    assert not tracker.should_compress()


def test_budget_real_usage_dict_preferred_over_estimate() -> None:
    from web_crawler.ai.budget import BudgetTracker, TokenBudget

    tracker = BudgetTracker(budget=TokenBudget(total=100000))
    # 长 prompt 但 usage dict 显示真实 token 仅 5
    tracker.record_call(
        step=1,
        prompt_text="x" * 1000,
        completion_text="y",
        usage={"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
    )
    assert tracker.summary()["used_total"] == 5


# -- Confidence: 动作置信度评分 ----------------------------------------------
def test_confidence_high_score_for_well_formed_action() -> None:
    from web_crawler.ai.confidence import ConfidenceScorer

    scorer = ConfidenceScorer(min_confidence=0.5)
    action = {
        "action_type": "extract",
        "params": {"param_name": "Anti-Content"},
        "reasoning": "通过 hook 捕获到 Anti-Content 头，提取该参数",
    }
    result = scorer.score(action, task="提取 Anti-Content", target_params=["Anti-Content"])
    assert result.score >= 0.5
    assert result.action_type == "extract"


def test_confidence_low_score_for_invalid_action() -> None:
    from web_crawler.ai.confidence import ConfidenceScorer

    scorer = ConfidenceScorer(min_confidence=0.5)
    action = {"action_type": "unknown_action", "params": {}, "reasoning": ""}
    result = scorer.score(action)
    assert result.score < 0.5
    assert scorer.should_reject(result)


def test_confidence_dedup_history_penalizes_repeat() -> None:
    from web_crawler.ai.confidence import ConfidenceScorer

    scorer = ConfidenceScorer(min_confidence=0.5)
    action = {
        "action_type": "navigate",
        "params": {"url": "https://example.com/page"},
        "reasoning": "navigate to page",
    }
    history = [
        {"action": "navigate", "params": {"url": "https://example.com/page"}},
        {"action": "navigate", "params": {"url": "https://example.com/page"}},
    ]
    result = scorer.score(action, history=history)
    # 应被 novelty 规则扣分
    assert any("novelty" in r for r in result.reasons)


def test_confidence_llm_score_with_fake_provider() -> None:
    """LLM 评分路径：FakeProvider 返回固定 score。"""
    from web_crawler.ai.confidence import ConfidenceScorer

    provider = FakeProvider(['{"score": 0.9, "reason": "valid action"}'])
    scorer = ConfidenceScorer(min_confidence=0.5, enable_llm_score=True, provider=provider)
    action = {
        "action_type": "extract",
        "params": {"param_name": "sign"},
        "reasoning": "从 hook 数据中提取 sign 参数",
    }
    result = scorer.score(action, task="extract sign", target_params=["sign"])
    # 综合 score = 规则分*0.6 + 0.9*0.4
    assert 0 < result.score <= 1.0
    assert result.raw.get("score") == 0.9


# -- Guardrails: 危险动作护栏 ------------------------------------------------
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


# -- ReverseAgent 集成新组件的 smoke 测试 -----------------------------------
def test_reverse_agent_config_has_new_fields() -> None:
    """ReverseAgentConfig 新增字段应有合理默认值。"""
    from web_crawler.ai.reverse_agent import ReverseAgentConfig

    cfg = ReverseAgentConfig()
    assert cfg.dom_prune_max_chars == 0  # 默认禁用 DomPruner
    assert cfg.enable_checkpoint is False
    assert cfg.budget_total is None  # 默认禁用预算
    assert cfg.budget_per_step is None
    assert cfg.min_confidence == 0.4
    assert cfg.enable_guard is True
    assert cfg.allowed_domains is None


def test_reverse_agent_init_new_components() -> None:
    """ReverseAgent 实例化后应有所有新组件实例。"""
    from web_crawler.ai.reverse_agent import ReverseAgent

    agent = ReverseAgent()
    # 5 个新组件都应存在
    assert agent.budget_tracker is not None
    assert agent.confidence_scorer is not None
    assert agent.guard is not None  # 默认启用
    assert agent.checkpoint_manager is not None
    # DomPruner 默认禁用
    assert agent.dom_pruner is None
    # 启用 DomPruner 的配置
    from web_crawler.ai.reverse_agent import ReverseAgentConfig

    cfg = ReverseAgentConfig(dom_prune_max_chars=4000)
    agent2 = ReverseAgent(config=cfg)
    assert agent2.dom_pruner is not None
    assert agent2.dom_pruner.max_chars == 4000


def test_reverse_agent_arun_signature_unchanged() -> None:
    """arun 仍是 async 协程且签名不变。"""
    import inspect

    from web_crawler.ai.reverse_agent import ReverseAgent

    assert inspect.iscoroutinefunction(ReverseAgent.arun)
    sig = inspect.signature(ReverseAgent.arun)
    assert "url" in sig.parameters
    assert "task" in sig.parameters


# -- 截图功能 ---------------------------------------------------------------


class _FakePage:
    """模拟 Playwright Page 对象，仅实现 screenshot 方法。"""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.screenshot_calls: list[dict[str, Any]] = []

    def screenshot(self, *, path: str = "", **kwargs: Any) -> bytes:
        self.screenshot_calls.append({"path": path, **kwargs})
        if self._fail:
            raise RuntimeError("screenshot not available")
        # 写一个占位文件，模拟真实截图行为
        from pathlib import Path

        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return b"\x89PNG\r\n\x1a\n"


class _FakeAsyncPage:
    """模拟异步 Playwright Page 对象，screenshot 为协程。"""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.screenshot_calls: list[dict[str, Any]] = []

    async def screenshot(self, *, path: str = "", **kwargs: Any) -> bytes:
        self.screenshot_calls.append({"path": path, **kwargs})
        if self._fail:
            raise RuntimeError("screenshot not available")
        from pathlib import Path

        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return b"\x89PNG\r\n\x1a\n"


def test_screenshot_disabled_returns_empty() -> None:
    """enable_screenshot=False 时截图返回空字符串。"""
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    agent = ReverseAgent(config=ReverseAgentConfig(enable_screenshot=False))
    page = _FakePage()
    result = agent._take_screenshot(page, step=1)
    assert result == ""
    assert agent._screenshots == []
    agent.close()


def test_screenshot_success_returns_path_and_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """启用截图时成功保存文件，返回路径并记录到 _screenshots。"""
    import os

    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    monkeypatch.chdir(tmp_path)
    agent = ReverseAgent(config=ReverseAgentConfig(enable_screenshot=True))
    try:
        page = _FakePage()
        result = agent._take_screenshot(page, step=1)
        assert result != ""
        assert os.path.exists(result)
        assert result.endswith("_step1.png")
        assert len(agent._screenshots) == 1
        assert agent._screenshots[0]["step"] == 1
        assert agent._screenshots[0]["error"] is False
        assert agent._last_error_screenshot == ""
    finally:
        agent.close()


def test_screenshot_error_marked_and_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """error=True 时截图路径带 _error 后缀并标记 error_screenshot。"""
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    monkeypatch.chdir(tmp_path)
    agent = ReverseAgent(config=ReverseAgentConfig(enable_screenshot=True))
    try:
        page = _FakePage()
        result = agent._take_screenshot(page, step=3, error=True)
        assert result != ""
        assert "_error" in result
        assert agent._screenshots[0]["error"] is True
        assert agent._last_error_screenshot == result
    finally:
        agent.close()


def test_screenshot_failure_returns_empty_and_no_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """page.screenshot 抛异常时返回空字符串，不崩溃，不记录。"""
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    monkeypatch.chdir(tmp_path)
    agent = ReverseAgent(config=ReverseAgentConfig(enable_screenshot=True))
    try:
        page = _FakePage(fail=True)
        result = agent._take_screenshot(page, step=1)
        assert result == ""
        assert agent._screenshots == []
    finally:
        agent.close()


def test_screenshot_none_page_returns_empty() -> None:
    """page=None 时截图返回空字符串。"""
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    agent = ReverseAgent(config=ReverseAgentConfig(enable_screenshot=True))
    result = agent._take_screenshot(None, step=1)
    assert result == ""
    agent.close()


def test_async_screenshot_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """异步截图版本也正确保存文件。"""
    import asyncio
    import os

    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    monkeypatch.chdir(tmp_path)
    agent = ReverseAgent(config=ReverseAgentConfig(enable_screenshot=True))
    try:
        page = _FakeAsyncPage()

        async def _run() -> str:
            return await agent._take_screenshot_async(page, step=1)

        result = asyncio.run(_run())
        assert result != ""
        assert os.path.exists(result)
        assert len(agent._screenshots) == 1
    finally:
        agent.close()


# -- CLI run 子命令 ---------------------------------------------------------


def test_cli_run_subcommand_parses_args() -> None:
    """run 子命令能正确解析所有参数，包括 --enable-screenshot。"""
    from web_crawler.mcp.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--url",
            "http://example.com",
            "--task",
            "提取 Anti-Content",
            "--target-params",
            "anti_content,sign",
            "--max-steps",
            "10",
            "--headless",
            "--enable-checkpoint",
            "--budget-total",
            "50000",
            "--budget-per-step",
            "4000",
            "--min-confidence",
            "0.5",
            "--no-enable-guard",
            "--allowed-domains",
            "example.com,cdn.example.com",
            "--no-enable-screenshot",
            "--output",
            "-",
        ]
    )
    assert args.command == "run"
    assert args.url == "http://example.com"
    assert args.task == "提取 Anti-Content"
    assert args.target_params == "anti_content,sign"
    assert args.max_steps == 10
    assert args.headless is True
    assert args.enable_checkpoint is True
    assert args.budget_total == 50000
    assert args.budget_per_step == 4000
    assert args.min_confidence == 0.5
    assert args.enable_guard is False
    assert args.allowed_domains == "example.com,cdn.example.com"
    assert args.enable_screenshot is False
    assert args.output == "-"


def test_cli_run_subcommand_defaults() -> None:
    """run 子命令的默认值正确。"""
    from web_crawler.mcp.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["run", "--url", "http://x"])
    assert args.max_steps == 20
    assert args.headless is False
    assert args.enable_checkpoint is False
    assert args.budget_total == 100_000
    assert args.budget_per_step == 8_000
    assert args.min_confidence == 0.4
    assert args.enable_guard is True
    assert args.enable_screenshot is True
    assert args.output == "-"


def test_cli_run_executes_with_mocked_agent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_run 用 mock 的 ReverseAgent.run 验证完整流程。"""
    import json

    from web_crawler.mcp import cli as cli_module

    # 模拟 ReverseAgent 类
    class _MockAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.config = kwargs.get("config")
            self.closed = False

        def run(self, url: str, task: str = "") -> dict:
            return {
                "success": True,
                "target_params_found": {"anti_content": "abc123"},
                "steps": 5,
                "compiled_script": "print('hello')",
            }

        def close(self) -> None:
            self.closed = True

    # 模拟 DeepSeekProvider
    class _MockProvider:
        def __init__(self, **kwargs: Any) -> None:
            pass

    # 注入 mock
    import web_crawler.ai.llm as llm_module

    monkeypatch.setattr(llm_module, "DeepSeekProvider", _MockProvider)
    monkeypatch.setattr("web_crawler.ai.reverse_agent.ReverseAgent", _MockAgent)

    # 构造 args
    from web_crawler.mcp.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--url",
            "http://test.example.com",
            "--task",
            "test task",
            "--target-params",
            "anti_content",
            "--max-steps",
            "5",
        ]
    )

    exit_code = cli_module.cmd_run(args)
    assert exit_code == 0

    # 验证 stdout 输出是合法 JSON
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["success"] is True
    assert result["target_params_found"]["anti_content"] == "abc123"
    assert result["steps"] == 5


def test_cli_run_save_script_to_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_run 的 --save-script 参数正确保存脚本到文件。"""
    from web_crawler.mcp import cli as cli_module

    class _MockAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, url: str, task: str = "") -> dict:
            return {
                "success": True,
                "compiled_script": "print('mock script')",
                "steps": 1,
            }

        def close(self) -> None:
            pass

    class _MockProvider:
        def __init__(self, **kwargs: Any) -> None:
            pass

    import web_crawler.ai.llm as llm_module

    monkeypatch.setattr(llm_module, "DeepSeekProvider", _MockProvider)
    monkeypatch.setattr("web_crawler.ai.reverse_agent.ReverseAgent", _MockAgent)

    script_path = tmp_path / "output_script.py"
    from web_crawler.mcp.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--url",
            "http://x",
            "--save-script",
            str(script_path),
            "--output",
            str(tmp_path / "result.json"),
        ]
    )
    exit_code = cli_module.cmd_run(args)
    assert exit_code == 0

    # 脚本文件应存在且内容正确
    assert script_path.exists()
    assert script_path.read_text(encoding="utf-8") == "print('mock script')"

    # JSON 结果也应写入文件
    result_file = tmp_path / "result.json"
    assert result_file.exists()
    import json

    result = json.loads(result_file.read_text(encoding="utf-8"))
    assert result["success"] is True


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


def test_execute_click_action() -> None:
    """click 动作应调用 page.click 并传递 selector / button / timeout。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeBrowserPage()
        action_dict: dict[str, Any] = {
            "action_type": "click",
            "params": {"selector": "button#submit", "button": "right"},
        }
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(action_dict)
        agent._act(page, action, step=1)
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


def test_execute_type_action() -> None:
    """type 动作默认 clear=true，应先 fill 清空再 type 输入。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {
                "action_type": "type",
                "params": {"selector": "input#username", "text": "user123"},
            }
        )
        agent._act(page, action, step=2)
        # clear=True 时应先调 fill 清空
        assert len(page.fill_calls) == 1
        assert page.fill_calls[0]["selector"] == "input#username"
        assert page.fill_calls[0]["value"] == ""
        assert len(page.type_calls) == 1
        assert page.type_calls[0]["text"] == "user123"
    finally:
        agent.close()


def test_execute_type_action_no_clear() -> None:
    """clear=False 时跳过 fill 直接 type。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {
                "action_type": "type",
                "params": {"selector": "input#q", "text": "hello", "clear": False},
            }
        )
        agent._act(page, action, step=1)
        assert page.fill_calls == []
        assert len(page.type_calls) == 1
    finally:
        agent.close()


def test_execute_scroll_action_window() -> None:
    """scroll 无 selector 时调 window.scrollBy。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "scroll", "params": {"x": 0, "y": 800}})
        agent._act(page, action, step=3)
        assert len(page.evaluate_calls) == 1
        assert "window.scrollBy" in page.evaluate_calls[0]
        assert "0" in page.evaluate_calls[0]
        assert "800" in page.evaluate_calls[0]
    finally:
        agent.close()


def test_execute_scroll_action_element() -> None:
    """scroll 带 selector 时调 querySelector.scrollBy。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {
                "action_type": "scroll",
                "params": {"selector": ".list", "y": 500},
            }
        )
        agent._act(page, action, step=1)
        assert len(page.evaluate_calls) == 1
        js = page.evaluate_calls[0]
        assert "querySelector" in js
        assert ".list" in js
        assert "scrollBy" in js
    finally:
        agent.close()


def test_execute_press_action() -> None:
    """press 动作应调 page.press；带 selector 时先 focus。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {
                "action_type": "press",
                "params": {"selector": "input#q", "key": "Enter"},
            }
        )
        agent._act(page, action, step=4)
        assert len(page.focus_calls) == 1
        assert page.focus_calls[0]["selector"] == "input#q"
        assert page.press_calls == ["Enter"]
    finally:
        agent.close()


def test_execute_press_action_no_selector() -> None:
    """press 无 selector 时只调 press。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "press", "params": {"key": "Escape"}})
        agent._act(page, action, step=1)
        assert page.focus_calls == []
        assert page.press_calls == ["Escape"]
    finally:
        agent.close()


def test_execute_hover_action() -> None:
    """hover 动作应调 page.hover 并传递 selector / timeout。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "hover", "params": {"selector": ".menu-item"}})
        agent._act(page, action, step=5)
        assert len(page.hover_calls) == 1
        assert page.hover_calls[0]["selector"] == ".menu-item"
        assert page.hover_calls[0]["timeout"] == 10000
    finally:
        agent.close()


def test_execute_select_action() -> None:
    """select_option 动作应调 page.select_option。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {
                "action_type": "select_option",
                "params": {"selector": "select#country", "value": "CN"},
            }
        )
        agent._act(page, action, step=6)
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


def test_execute_click_missing_selector_raises() -> None:
    """click 缺少 selector 应抛 ValueError。"""
    agent = _make_agent_for_browser_actions()
    try:
        page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "click", "params": {}})
        with pytest.raises(ValueError, match="selector"):
            agent._act(page, action, step=1)
    finally:
        agent.close()


def test_browser_action_emits_event() -> None:
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
        page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "click", "params": {"selector": "button#x"}})
        agent._act(page, action, step=7)
        browser_events = [e for e in events if e["type"] == "browser.action"]
        assert len(browser_events) == 1
        assert browser_events[0]["action"] == "click"
        assert browser_events[0]["selector"] == "button#x"
        assert browser_events[0]["step"] == 7
    finally:
        agent.close()


# -- Confidence: 浏览器交互动作的置信度评分 ----------------------------------
def test_confidence_scores_browser_actions() -> None:
    """click / type / scroll 等浏览器动作应得到合理置信度。"""
    from web_crawler.ai.confidence import ConfidenceScorer

    scorer = ConfidenceScorer(min_confidence=0.4)
    # 完整 click 动作应得高分
    click_action = {
        "action_type": "click",
        "params": {"selector": "button#submit"},
        "reasoning": "点击提交按钮以触发加密参数生成",
    }
    result = scorer.score(click_action)
    assert result.action_type == "click"
    assert result.score >= 0.4
    # type 缺 text 应被扣分
    type_action = {
        "action_type": "type",
        "params": {"selector": "input#q"},
        "reasoning": "输入查询关键词",
    }
    type_result = scorer.score(type_action)
    assert type_result.score < 1.0
    assert any("text" in r for r in type_result.reasons)
    # scroll 无必填参数，应得高分
    scroll_action = {
        "action_type": "scroll",
        "params": {"x": 0, "y": 800},
        "reasoning": "向下滚动加载更多内容",
    }
    scroll_result = scorer.score(scroll_action)
    assert scroll_result.action_type == "scroll"
    assert scroll_result.score >= 0.5
    # 未知动作仍低分
    unknown = {
        "action_type": "frob",
        "params": {},
        "reasoning": "",
    }
    unknown_result = scorer.score(unknown)
    assert unknown_result.score < 0.5


def test_confidence_valid_actions_includes_browser_types() -> None:
    """_VALID_ACTIONS 应包含 6 类浏览器交互动作。"""
    from web_crawler.ai.confidence import _VALID_ACTIONS

    for at in ("click", "type", "scroll", "press", "hover", "select_option"):
        assert at in _VALID_ACTIONS


# -- Guardrails: 危险点击与 selector 注入 -----------------------------------
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
    """selector 含 JS 注入特征（; / () / script）应被拦截。"""
    from web_crawler.ai.guardrails import ActionGuard

    guard = ActionGuard()
    bad_selectors = [
        "button;alert(1)",
        "img)script(",
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
    ]:
        action = {"action_type": "click", "params": {"selector": selector}}
        result = guard.check(action)
        assert not result.denied, f"不应拦截合法 selector：{selector}"


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


class _FakeBrowserContext:
    """模拟 Playwright BrowserContext，用于多标签页测试。"""

    def __init__(self) -> None:
        self.pages: list[_FakeBrowserPage] = []
        self.next_id = 0

    def new_page(self) -> _FakeBrowserPage:
        page = _FakeBrowserPage()
        page._tab_id = self.next_id  # type: ignore[attr-defined]
        self.next_id += 1
        self.pages.append(page)
        return page


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


def test_execute_new_tab_action() -> None:
    """new_tab 动作应创建新 page 并切到新标签；主页面登记为 'main'。"""
    agent = _make_agent_with_context()
    try:
        main_page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {
                "action_type": "new_tab",
                "params": {"url": "https://example.com/tab2", "name": "second"},
            }
        )
        agent._do_new_tab(main_page, action, step=1)
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


def test_execute_new_tab_default_name() -> None:
    """new_tab 不传 name 时应使用 tab_N 作为默认名。"""
    agent = _make_agent_with_context()
    try:
        main_page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "new_tab", "params": {"url": ""}})
        agent._do_new_tab(main_page, action, step=1)
        # 默认名应为 tab_0（首次新建，main 不计入计数）
        assert "tab_0" in agent._tabs
    finally:
        agent.close()


def test_execute_switch_tab_by_name() -> None:
    """switch_tab 按 name 切换应更新 self._page。"""
    agent = _make_agent_with_context()
    try:
        main_page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        # 先新建一个 tab
        agent._do_new_tab(
            main_page,
            Action.from_dict({"action_type": "new_tab", "params": {"url": "", "name": "t2"}}),
            step=1,
        )
        new_tab = agent._tabs["t2"]
        # 切回 main
        agent._do_switch_tab(
            new_tab,
            Action.from_dict({"action_type": "switch_tab", "params": {"name": "main"}}),
            step=2,
        )
        assert agent._page is main_page
        # 再切到 t2
        agent._do_switch_tab(
            main_page,
            Action.from_dict({"action_type": "switch_tab", "params": {"name": "t2"}}),
            step=3,
        )
        assert agent._page is new_tab
    finally:
        agent.close()


def test_execute_switch_tab_by_index() -> None:
    """switch_tab 按 index 切换应按 _tabs 插入顺序解析。"""
    agent = _make_agent_with_context()
    try:
        main_page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        agent._do_new_tab(
            main_page,
            Action.from_dict({"action_type": "new_tab", "params": {"url": "", "name": "second"}}),
            step=1,
        )
        # _do_new_tab 先插入 "second" 再插入 "main"，所以 index=0 是 second，index=1 是 main
        agent._do_switch_tab(
            agent._tabs["second"],
            Action.from_dict({"action_type": "switch_tab", "params": {"index": 1}}),
            step=2,
        )
        assert agent._page is main_page
        # index=0 应回到 second
        agent._do_switch_tab(
            main_page,
            Action.from_dict({"action_type": "switch_tab", "params": {"index": 0}}),
            step=3,
        )
        assert agent._page is agent._tabs["second"]
    finally:
        agent.close()


def test_execute_switch_tab_unknown_raises() -> None:
    """switch_tab 找不到标签时应抛 ValueError。"""
    agent = _make_agent_with_context()
    try:
        main_page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "switch_tab", "params": {"name": "nonexistent"}})
        with pytest.raises(ValueError, match="switch_tab"):
            agent._do_switch_tab(main_page, action, step=1)
    finally:
        agent.close()


def test_execute_close_tab_action() -> None:
    """close_tab 应从 _tabs 移除并把 _page 回退到 main。"""
    agent = _make_agent_with_context()
    try:
        main_page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        # 新建 second tab
        agent._do_new_tab(
            main_page,
            Action.from_dict({"action_type": "new_tab", "params": {"url": "", "name": "second"}}),
            step=1,
        )
        assert "second" in agent._tabs
        # 关闭 second
        agent._do_close_tab(
            agent._tabs["second"],
            Action.from_dict({"action_type": "close_tab", "params": {"name": "second"}}),
            step=2,
        )
        assert "second" not in agent._tabs
        # _page 应回退到 main
        assert agent._page is main_page
    finally:
        agent.close()


def test_execute_close_tab_unknown_raises() -> None:
    """close_tab 找不到 name 时应抛 ValueError。"""
    agent = _make_agent_with_context()
    try:
        main_page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "close_tab", "params": {"name": "ghost"}})
        with pytest.raises(ValueError, match="close_tab"):
            agent._do_close_tab(main_page, action, step=1)
    finally:
        agent.close()


def test_new_tab_action_emits_event() -> None:
    """new_tab 动作应发布 browser.action 事件。"""
    agent = _make_agent_with_context()
    try:
        events: list[dict[str, Any]] = []

        def _handler(event: Any) -> None:
            events.append({"type": event.type, "step": event.step, **event.payload})

        agent.event_bus.subscribe(_handler)
        main_page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {"action_type": "new_tab", "params": {"url": "https://x.example", "name": "n1"}}
        )
        agent._do_new_tab(main_page, action, step=5)
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


def test_humanize_click_hovers_before_click() -> None:
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
        page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict({"action_type": "click", "params": {"selector": "button#x"}})
        agent._act(page, action, step=1)
        # hover 应被调用（人类化先 hover）
        assert len(page.hover_calls) == 1
        assert page.hover_calls[0]["selector"] == "button#x"
        # click 也应被调用
        assert len(page.click_calls) == 1
        assert page.click_calls[0]["selector"] == "button#x"
    finally:
        agent.close()


def test_humanize_type_focuses_before_type() -> None:
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
        page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        action = Action.from_dict(
            {"action_type": "type", "params": {"selector": "input#q", "text": "hi"}}
        )
        agent._act(page, action, step=1)
        # focus 应被调用
        assert len(page.focus_calls) == 1
        assert page.focus_calls[0]["selector"] == "input#q"
        # type 也应被调用（mock 不支持 delay，TypeError 退化为不带 delay）
        assert len(page.type_calls) == 1
        assert page.type_calls[0]["text"] == "hi"
    finally:
        agent.close()


def test_humanize_disabled_skips_hover_and_focus() -> None:
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
        page = _FakeBrowserPage()
        from web_crawler.ai.reverse_agent import Action

        # click：不应 hover
        agent._act(
            page,
            Action.from_dict({"action_type": "click", "params": {"selector": "button#x"}}),
            step=1,
        )
        assert page.hover_calls == []
        assert len(page.click_calls) == 1
        # type：不应 focus
        agent._act(
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

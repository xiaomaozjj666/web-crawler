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
    assert cfg.budget_total == 100_000
    assert cfg.budget_per_step == 8_000
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

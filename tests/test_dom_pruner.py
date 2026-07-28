"""Tests for the DomPruner: DOM focus pruning (Skyvern/browser-use style)."""

from __future__ import annotations

from typing import Any

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

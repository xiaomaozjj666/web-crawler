"""Tests for the DomPruner: DOM focus pruning (Skyvern/browser-use style)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from web_crawler import LLMResponse
from web_crawler.ai.dom_pruner import DomPruner, PrunedDom, _Candidate

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


# ---------------------------------------------------------------------------
# 基础剪枝流程
# ---------------------------------------------------------------------------


def test_dom_pruner_extracts_candidates_and_truncates() -> None:
    pruner = DomPruner(max_chars=600, max_candidates=10)
    result = pruner.prune(_HTML_FULL)
    assert result.element_count > 0
    assert result.kept_count <= 10
    assert len(result.text) <= 700  # 600 + 截断标记
    assert "script" in result.text or "input" in result.text or "form" in result.text


def test_dom_pruner_prioritizes_crypto_keywords() -> None:
    pruner = DomPruner(max_chars=8000, max_candidates=20)
    result = pruner.prune(_HTML_FULL)
    # top_score 应该比较高，因为页面有 anti_content / sign 等关键词
    assert result.top_score > 3.0


def test_dom_pruner_handles_empty_html() -> None:
    pruner = DomPruner()
    result = pruner.prune("")
    assert result.text == ""
    assert result.element_count == 0
    assert result.kept_count == 0


def test_dom_pruner_handles_whitespace_only_html() -> None:
    """纯空白 HTML 应返回空结果。"""
    pruner = DomPruner()
    result = pruner.prune("   \n\t  \n  ")
    assert result.text == ""
    assert result.element_count == 0
    assert result.kept_count == 0


def test_dom_pruner_truncation_marks_flag() -> None:
    """超长输出应置 truncated=True 并附加截断标记。"""
    pruner = DomPruner(max_chars=500, max_candidates=50)
    result = pruner.prune(_HTML_FULL)
    assert result.truncated is True
    assert "<!-- pruned: char-limit -->" in result.text


def test_dom_pruner_no_truncation_when_under_limit() -> None:
    """输出未超 max_chars 时 truncated=False。"""
    pruner = DomPruner(max_chars=8000, max_candidates=5)
    result = pruner.prune("<div>short</div>")
    assert result.truncated is False


def test_pruned_dom_to_dict() -> None:
    """PrunedDom.to_dict 应返回完整字段。"""
    pruned = PrunedDom(
        text="hello",
        element_count=10,
        kept_count=3,
        truncated=True,
        top_score=5.5,
    )
    d = pruned.to_dict()
    assert d == {
        "text": "hello",
        "element_count": 10,
        "kept_count": 3,
        "truncated": True,
        "top_score": 5.5,
    }


def test_dom_pruner_init_clamps_min_values() -> None:
    """max_chars / max_candidates / llm_rank_limit 应被钳制到最小值。"""
    pruner = DomPruner(max_chars=10, max_candidates=1, llm_rank_limit=1)
    assert pruner.max_chars == 500
    assert pruner.max_candidates == 10
    assert pruner.llm_rank_limit == 5


def test_dom_pruner_enable_llm_rank_requires_provider() -> None:
    """无 provider 时 enable_llm_rank 应被强制为 False。"""
    pruner = DomPruner(enable_llm_rank=True, provider=None)
    assert pruner.enable_llm_rank is False


# ---------------------------------------------------------------------------
# 异步 prune_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_async_basic() -> None:
    """prune_async 应与 prune 行为一致。"""
    pruner = DomPruner(max_chars=8000, max_candidates=20)
    result = await pruner.prune_async(_HTML_FULL)
    assert result.element_count > 0
    assert result.kept_count > 0
    assert result.top_score > 3.0


@pytest.mark.asyncio
async def test_prune_async_empty_html() -> None:
    """prune_async 处理空 HTML。"""
    pruner = DomPruner()
    result = await pruner.prune_async("")
    assert result.text == ""
    assert result.element_count == 0


@pytest.mark.asyncio
async def test_prune_async_truncation() -> None:
    """prune_async 超长截断。"""
    pruner = DomPruner(max_chars=500, max_candidates=50)
    result = await pruner.prune_async(_HTML_FULL)
    assert result.truncated is True
    assert "<!-- pruned: char-limit -->" in result.text


# ---------------------------------------------------------------------------
# 规则打分细节
# ---------------------------------------------------------------------------


def test_dom_pruner_decorative_element_gets_low_score() -> None:
    """纯装饰性 div/span/p（无文本无属性）应得低分 0.5。"""
    pruner = DomPruner(max_chars=8000, max_candidates=50)
    html = "<div></div><span></span><p></p>"
    result = pruner.prune(html)
    # 这些元素应该被保留但得分低
    assert result.element_count >= 0
    # top_score 应该很低（装饰性元素）
    assert result.top_score <= 1.0


def test_dom_pruner_high_priority_tags_scored() -> None:
    """高优先级标签（script/input/form/button/a）应得高分。"""
    pruner = DomPruner(max_chars=8000, max_candidates=50)
    html = """
    <form name="login"><input name="user"></form>
    <a href="/page">link</a>
    <button>click me</button>
    <script src="external.js"></script>
    """
    result = pruner.prune(html)
    # script[src] 得分最高：1.0 + 2.0（高优先级）+ 2.0（src）= 5.0
    assert result.top_score >= 5.0


def test_dom_pruner_script_with_src_scores_higher() -> None:
    """script[src] 应比 inline script 得分更高。"""
    pruner = DomPruner(max_chars=8000, max_candidates=50)
    html = '<script src="ext.js"></script><script>var x = 1;</script>'
    result = pruner.prune(html)
    # src script 得 5.0，inline script 得 3.0
    assert result.top_score >= 5.0


def test_dom_pruner_crypto_attr_hint() -> None:
    """属性命中加密关键词（sign/token/encrypt 等）应加分。"""
    pruner = DomPruner(max_chars=8000, max_candidates=50)
    html = '<div data-sign="abc" id="signer">sign container</div>'
    result = pruner.prune(html)
    # data-sign 命中 "sign" -> +3.0
    assert result.top_score >= 4.0


def test_dom_pruner_crypto_text_hint() -> None:
    """文本命中加密关键词应加分。"""
    pruner = DomPruner(max_chars=8000, max_candidates=50)
    html = "<div>acw_sc__v2 anti-content x-bogus</div>"
    result = pruner.prune(html)
    # 文本命中 -> +4.0
    assert result.top_score >= 5.0


def test_dom_pruner_form_with_name_attr() -> None:
    """form[name] 应额外加分。"""
    pruner = DomPruner(max_chars=8000, max_candidates=50)
    html = '<form name="login_form"></form>'
    result = pruner.prune(html)
    # form 高优先级 +2.0, name 属性 +1.0 -> 4.0
    assert result.top_score >= 4.0


def test_dom_pruner_input_with_name_attr() -> None:
    """input[name] 应额外加分。"""
    pruner = DomPruner(max_chars=8000, max_candidates=50)
    html = '<input name="username">'
    result = pruner.prune(html)
    # input 高优先级 +2.0, name 属性 +1.0 -> 4.0
    assert result.top_score >= 4.0


def test_dom_pruner_skips_non_content_tags() -> None:
    """html/head/meta/link/style/br/hr 应被跳过。"""
    pruner = DomPruner(max_chars=8000, max_candidates=50)
    html = '<html><head><meta charset="utf-8"><link rel="stylesheet"><style>x</style></head><body><br><hr></body></html>'
    result = pruner.prune(html)
    # 这些标签不应出现在候选中
    assert "<meta" not in result.text
    assert "<link" not in result.text
    assert "<style" not in result.text


def test_dom_pruner_long_candidate_truncated() -> None:
    """单个超长候选（>500 字符）应被截断并附加 </tag>。"""
    pruner = DomPruner(max_chars=8000, max_candidates=50)
    # 超长属性会让开标签片段超过 500 字符，触发截断后缀
    long_attr = "x" * 600
    html = f'<script src="{long_attr}"></script>'
    result = pruner.prune(html)
    assert "...</script>" in result.text


# ---------------------------------------------------------------------------
# LLM 重排
# ---------------------------------------------------------------------------


class _FakeLLMProvider:
    """可控的 LLM provider mock。"""

    model = "fake"
    capabilities = MagicMock()

    def __init__(self, response_content: str) -> None:
        self._content = response_content
        self.chat_calls: list[Any] = []

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.chat_calls.append(messages)
        return LLMResponse(content=self._content, model="fake")


class _AsyncFakeLLMProvider(_FakeLLMProvider):
    """带 achat 方法的异步 provider。"""

    async def achat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.chat_calls.append(messages)
        return LLMResponse(content=self._content, model="fake")


class _BrokenProvider:
    """chat 抛异常的 provider。"""

    model = "broken"
    capabilities = MagicMock()

    def chat(self, messages: Any, **kwargs: Any) -> Any:
        raise RuntimeError("llm broken")


class _BrokenAsyncProvider:
    """achat 抛异常的异步 provider。"""

    model = "broken-async"
    capabilities = MagicMock()

    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        raise RuntimeError("async llm broken")


def test_dom_pruner_llm_rerank_fallback_on_error() -> None:
    """LLM 评分失败时应自动降级为规则评分。"""
    pruner = DomPruner(max_chars=8000, enable_llm_rank=True, provider=_BrokenProvider())
    result = pruner.prune(_HTML_FULL)
    # 不应抛异常，结果仍合法
    assert result.element_count > 0


def test_dom_pruner_llm_rerank_applies_scores() -> None:
    """LLM 返回合法 JSON 时应重排候选分数。"""
    # 返回 JSON 数组：把第 0 个候选打 10 分
    llm_resp = '[{"index": 0, "score": 10.0}, {"index": 1, "score": 1.0}]'
    provider = _FakeLLMProvider(llm_resp)
    pruner = DomPruner(max_chars=8000, enable_llm_rank=True, provider=provider)
    result = pruner.prune(_HTML_FULL)
    assert result.element_count > 0
    # provider.chat 应被调用
    assert len(provider.chat_calls) == 1


def test_dom_pruner_llm_rerank_skips_when_too_few_candidates() -> None:
    """候选数 < 2 时 LLM 重排应直接返回（不调用 provider）。"""
    provider = _FakeLLMProvider("[]")
    pruner = DomPruner(max_chars=8000, max_candidates=50, enable_llm_rank=True, provider=provider)
    # 直接调用 _llm_rerank，传入单元素列表触发 len(top) < 2 分支
    single = [_Candidate(html="<form></form>", score=3.0, tag="form")]
    out = pruner._llm_rerank(single)
    # provider.chat 不应被调用
    assert len(provider.chat_calls) == 0
    assert out is single  # 应原样返回


@pytest.mark.asyncio
async def test_dom_pruner_llm_rerank_async_with_achat() -> None:
    """异步路径优先使用 provider.achat。"""
    llm_resp = '[{"index": 0, "score": 9.5}]'
    provider = _AsyncFakeLLMProvider(llm_resp)
    pruner = DomPruner(max_chars=8000, enable_llm_rank=True, provider=provider)
    result = await pruner.prune_async(_HTML_FULL)
    assert result.element_count > 0
    assert len(provider.chat_calls) == 1


@pytest.mark.asyncio
async def test_dom_pruner_llm_rerank_async_fallback_on_error() -> None:
    """异步 LLM 评分失败时应降级。"""
    pruner = DomPruner(max_chars=8000, enable_llm_rank=True, provider=_BrokenAsyncProvider())
    result = await pruner.prune_async(_HTML_FULL)
    assert result.element_count > 0


@pytest.mark.asyncio
async def test_dom_pruner_llm_rerank_async_without_achat() -> None:
    """provider 无 achat 方法时应回退到同步 chat。"""
    llm_resp = '[{"index": 0, "score": 8.0}]'
    # _FakeLLMProvider 没有 achat 方法
    provider = _FakeLLMProvider(llm_resp)
    pruner = DomPruner(max_chars=8000, enable_llm_rank=True, provider=provider)
    result = await pruner.prune_async(_HTML_FULL)
    assert result.element_count > 0
    assert len(provider.chat_calls) == 1


@pytest.mark.asyncio
async def test_dom_pruner_llm_rerank_async_skips_few_candidates() -> None:
    """异步路径候选数 < 2 时跳过 LLM。"""
    provider = _AsyncFakeLLMProvider("[]")
    pruner = DomPruner(max_chars=8000, enable_llm_rank=True, provider=provider)
    # 直接调用 _llm_rerank_async，传入单元素列表触发 len(top) < 2 分支
    single = [_Candidate(html="<form></form>", score=3.0, tag="form")]
    out = await pruner._llm_rerank_async(single)
    assert len(provider.chat_calls) == 0
    assert out is single  # 应原样返回


# ---------------------------------------------------------------------------
# _parse_rank_response 静态方法
# ---------------------------------------------------------------------------


def test_parse_rank_response_valid_json() -> None:
    """合法 JSON 数组应正确解析。"""
    text = '[{"index": 0, "score": 8.5}, {"index": 1, "score": 3.2}]'
    out = DomPruner._parse_rank_response(text, expected_count=5)
    assert out == {0: 8.5, 1: 3.2}


def test_parse_rank_response_with_markdown_fence() -> None:
    """带 markdown 代码块的 JSON 应被提取。"""
    text = '```json\n[{"index": 0, "score": 7.0}]\n```'
    out = DomPruner._parse_rank_response(text, expected_count=3)
    assert out == {0: 7.0}


def test_parse_rank_response_with_surrounding_text() -> None:
    """带前后说明文字的 JSON 应被提取。"""
    text = 'Here is the result:\n[{"index": 0, "score": 9.0}]\nDone.'
    out = DomPruner._parse_rank_response(text, expected_count=3)
    assert out == {0: 9.0}


def test_parse_rank_response_no_array() -> None:
    """无 JSON 数组时应返回空 dict。"""
    out = DomPruner._parse_rank_response("no json here", expected_count=3)
    assert out == {}


def test_parse_rank_response_invalid_json() -> None:
    """非法 JSON 应返回空 dict。"""
    out = DomPruner._parse_rank_response("[not valid json]", expected_count=3)
    assert out == {}


def test_parse_rank_response_not_a_list() -> None:
    """解析结果非 list 时应返回空 dict。"""
    out = DomPruner._parse_rank_response('{"not": "an array"}', expected_count=3)
    assert out == {}


def test_parse_rank_response_item_not_dict() -> None:
    """数组元素非 dict 时应跳过。"""
    text = '[1, "str", {"index": 0, "score": 5.0}]'
    out = DomPruner._parse_rank_response(text, expected_count=3)
    assert out == {0: 5.0}


def test_parse_rank_response_missing_fields() -> None:
    """item 缺 index 或 score 时应跳过。"""
    text = '[{"index": 0}, {"score": 5.0}, {"index": 1, "score": 3.0}]'
    out = DomPruner._parse_rank_response(text, expected_count=3)
    assert out == {1: 3.0}


def test_parse_rank_response_invalid_index_type() -> None:
    """index/score 类型无法转换时应跳过。"""
    text = '[{"index": "abc", "score": 5.0}, {"index": 0, "score": "xyz"}]'
    out = DomPruner._parse_rank_response(text, expected_count=3)
    assert out == {}


def test_parse_rank_response_index_out_of_range() -> None:
    """index 超出 expected_count 范围时应跳过。"""
    text = '[{"index": 5, "score": 9.0}, {"index": 0, "score": 1.0}]'
    out = DomPruner._parse_rank_response(text, expected_count=3)
    assert out == {0: 1.0}


def test_parse_rank_response_negative_index() -> None:
    """负 index 应被跳过（0 <= i 条件）。"""
    text = '[{"index": -1, "score": 9.0}, {"index": 0, "score": 1.0}]'
    out = DomPruner._parse_rank_response(text, expected_count=3)
    assert out == {0: 1.0}


# ---------------------------------------------------------------------------
# _build_rank_prompt / _assemble_text
# ---------------------------------------------------------------------------


def test_build_rank_prompt_includes_candidates() -> None:
    """_build_rank_prompt 应包含候选索引、tag、text、score。"""
    cands = [
        _Candidate(html="<a>link</a>", score=3.0, tag="a", text_preview="link"),
        _Candidate(html="<form>x</form>", score=5.0, tag="form", text_preview="x"),
    ]
    prompt = DomPruner._build_rank_prompt(cands)
    assert "[0]" in prompt
    assert "[1]" in prompt
    assert "<a>" in prompt
    assert "<form>" in prompt
    assert "JSON" in prompt


def test_assemble_text_joins_candidates() -> None:
    """_assemble_text 应用 \n 连接候选 HTML。"""
    cands = [
        _Candidate(html="<a>1</a>", score=1.0, tag="a"),
        _Candidate(html="<b>2</b>", score=2.0, tag="b"),
    ]
    text = DomPruner._assemble_text(cands)
    assert text == "<a>1</a>\n<b>2</b>"


def test_assemble_text_empty() -> None:
    """空候选列表应返回空字符串。"""
    assert DomPruner._assemble_text([]) == ""


# ---------------------------------------------------------------------------
# _extract_fallback（bs4 不可用时的降级）
# ---------------------------------------------------------------------------


def test_extract_fallback_extracts_tags() -> None:
    """_extract_fallback 应正则提取 script/input/form/button/a/iframe。"""
    html = """
    <script src="a.js"></script>
    <input name="x">
    <form action="/login"></form>
    <button>click</button>
    <a href="/page">link</a>
    <iframe src="frame.html"></iframe>
    <div>not extracted</div>
    """
    cands = DomPruner._extract_fallback(html)
    tags = {c.tag for c in cands}
    assert "script" in tags
    assert "input" in tags
    assert "form" in tags
    assert "button" in tags
    assert "a" in tags
    assert "iframe" in tags
    # div 不在提取范围
    assert "div" not in tags
    # 所有候选 score 应为 2.0
    for c in cands:
        assert c.score == 2.0


def test_extract_fallback_truncates_long_html() -> None:
    """_extract_fallback 应截断超长 HTML 到 500 字符。"""
    long_attr = "x" * 600
    html = f'<script src="{long_attr}"></script>'
    cands = DomPruner._extract_fallback(html)
    assert len(cands) == 1
    assert len(cands[0].html) <= 500


def test_extract_fallback_empty_html() -> None:
    """空 HTML 应返回空列表。"""
    assert DomPruner._extract_fallback("") == []
    assert DomPruner._extract_fallback("no tags here") == []


# ---------------------------------------------------------------------------
# BeautifulSoup 解析降级（lxml 不可用时回退 html.parser）
# ---------------------------------------------------------------------------


def test_dom_pruner_falls_back_to_html_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """lxml 解析失败时应回退到 html.parser。"""
    from bs4 import BeautifulSoup

    original_init = BeautifulSoup.__init__
    call_count = {"lxml": 0, "html_parser": 0}

    def _mock_init(self, markup: Any = "", features: Any = None, **kwargs: Any) -> None:
        if features == "lxml":
            call_count["lxml"] += 1
            raise RuntimeError("lxml not available in test")
        if features == "html.parser":
            call_count["html_parser"] += 1
        original_init(self, markup, features=features, **kwargs)

    monkeypatch.setattr(BeautifulSoup, "__init__", _mock_init)
    pruner = DomPruner(max_chars=8000, max_candidates=50)
    result = pruner.prune("<div>hello</div>")
    assert call_count["lxml"] == 1
    assert call_count["html_parser"] == 1
    assert result.element_count >= 0


# ===========================================================================
# 回归：候选片段不做全树序列化（避免深层 DOM 的 O(n·depth) 复制）
# ===========================================================================


def test_tag_to_candidate_fragment_is_bounded() -> None:
    """大子树容器的候选片段只含开标签+受限文本，不含子孙标记。"""
    from bs4 import BeautifulSoup

    inner = "".join("<span>a</span>" for _ in range(2000))
    html = f"<div id='big'>{inner}</div>"
    soup = BeautifulSoup(html, "html.parser")
    div = soup.find("div")

    pruner = DomPruner(max_chars=8000, max_candidates=50)
    cand = pruner._tag_to_candidate(div)

    assert cand is not None
    assert "<span" not in cand.html  # 未把 2000 个 span 的标记复制进来
    assert len(cand.html) <= 500


# ===========================================================================
# 扩展：_tag_fragment 属性值边界（无值属性跳过 / 非字符串属性转 str）
# ===========================================================================


def test_tag_fragment_skips_valueless_attr() -> None:
    """无值属性（attr=None）应被跳过，不产生残缺片段。"""
    from types import SimpleNamespace

    from web_crawler.ai.dom_pruner import _tag_fragment

    # bs4 4.15 会把 None 属性值过滤掉，用命名空间对象直接注入 None 覆盖跳过分支
    tag = SimpleNamespace(name="div", attrs={"data-x": None})
    frag = _tag_fragment(tag, "text")
    assert frag == "<div>text"


def test_tag_fragment_stringifies_non_str_attr() -> None:
    """非字符串属性值（如 int）应转为 str 后序列化。"""
    from types import SimpleNamespace

    from web_crawler.ai.dom_pruner import _tag_fragment

    tag = SimpleNamespace(name="div", attrs={"data-n": 5})
    frag = _tag_fragment(tag, "hi")
    assert 'data-n="5"' in frag

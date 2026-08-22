"""JSAnalyzer 解析逻辑与 LLM 路径测试（不调用真实 LLM，不触网）。

本文件覆盖两类路径：
- 纯解析辅助函数（``_balanced_end`` / ``_split_top_level`` / ``_extract_*`` 等）；
- :class:`JSAnalyzer` 的 LLM 路径（``analyze_fragment`` / ``deobfuscate`` /
  ``suggest_reimplementation``），通过 scripted provider 注入回复。
"""

from __future__ import annotations

from typing import Any

from web_crawler.ai.analyzer import (
    JSAnalyzer,
    JSFragment,
    WebpackModule,
    _balanced_end,
    _extract_deps,
    _extract_export_keys,
    _extract_function_body,
    _extract_json,
    _split_top_level,
    _strip_code_fence,
    _third_param_name,
    _to_float,
)
from web_crawler.ai.llm import LLMResponse


class _StubProvider:
    """纯解析测试用的 stub provider；被调用即说明测试逻辑出错。"""

    model = "stub"

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        raise AssertionError("纯解析测试不应调用 LLM")


class _ScriptedProvider:
    """按预设回复序列返回的 provider，用于 LLM 路径测试。"""

    model = "scripted"

    def __init__(self, replies: list[str], finish_reason: str | None = None) -> None:
        self._replies = list(replies)
        self.finish_reason = finish_reason
        self.calls: list[Any] = []

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append(messages)
        content = self._replies.pop(0) if self._replies else ""
        return LLMResponse(content=content, model=self.model, finish_reason=self.finish_reason)


# 对象形态的 webpack 打包产物：模块 100 依赖模块 200
_WEBPACK_OBJECT_SOURCE = """
var __webpack_modules__ = {
  100: function(module, exports, __webpack_require__) {
    var crypto = __webpack_require__(200);
    module.exports = { sign: function(data) { return crypto.hash(data); } };
  },
  200: function(module, exports) {
    module.exports = { hash: function(d) { return "abc123"; } };
  }
};
"""

# 数组形态的 webpack 打包产物：模块 0 依赖模块 1
_WEBPACK_ARRAY_SOURCE = """
var __webpack_modules__ = [
  function(module, exports, __webpack_require__) {
    __webpack_require__(1);
  },
  function(module, exports) {
    module.exports = {};
  }
];
"""


def test_js_fragment_defaults() -> None:
    frag = JSFragment(source="var a = 1;")
    assert frag.source == "var a = 1;"
    assert frag.url == ""
    assert frag.size == 0
    assert frag.is_minified is False


def test_extract_webpack_modules_object() -> None:
    analyzer = JSAnalyzer(provider=_StubProvider())
    modules = analyzer.extract_webpack_modules(_WEBPACK_OBJECT_SOURCE)

    assert len(modules) == 2
    by_id = {m.id: m for m in modules}
    assert set(by_id) == {100, 200}
    # 模块 100 依赖 200
    assert 200 in by_id[100].dependencies
    # 模块 200 无出向依赖
    assert by_id[200].dependencies == []


def test_extract_webpack_modules_array() -> None:
    analyzer = JSAnalyzer(provider=_StubProvider())
    modules = analyzer.extract_webpack_modules(_WEBPACK_ARRAY_SOURCE)

    assert len(modules) == 2
    assert [m.id for m in modules] == [0, 1]
    assert 1 in modules[0].dependencies
    assert modules[1].dependencies == []


def test_identify_entry_point() -> None:
    analyzer = JSAnalyzer(provider=_StubProvider())
    modules = analyzer.extract_webpack_modules(_WEBPACK_OBJECT_SOURCE)
    # 100 依赖 200，200 被依赖；入口应是未被依赖的 100
    assert analyzer.identify_entry_point(modules) == 100


def test_trace_signing_flow() -> None:
    analyzer = JSAnalyzer(provider=_StubProvider())
    modules = analyzer.extract_webpack_modules(_WEBPACK_OBJECT_SOURCE)
    # "sign" 仅在模块 100 出现；依赖在前、产出者在后
    chain = analyzer.trace_signing_flow(modules, "sign")
    assert chain == [200, 100]


def test_truncate_code() -> None:
    analyzer = JSAnalyzer(provider=_StubProvider(), max_chars=10)
    # 超长代码前后截断，中间插入 TRUNCATED 标记
    out = analyzer._truncate_code("a" * 30)
    assert "[TRUNCATED 20 chars]" in out
    assert out.startswith("aaaaa")
    assert out.endswith("aaaaa")
    # 未超长原样返回
    assert analyzer._truncate_code("abc") == "abc"


def test_extract_json_plain() -> None:
    data = _extract_json('{"algorithm": "MD5", "confidence": 0.9}')
    assert data["algorithm"] == "MD5"
    assert data["confidence"] == 0.9


def test_extract_json_codeblock() -> None:
    text = '```json\n{"algorithm": "AES", "inputs": ["k"]}\n```'
    data = _extract_json(text)
    assert data["algorithm"] == "AES"
    assert data["inputs"] == ["k"]


# ===========================================================================
# _balanced_end 边界测试（覆盖字符串/注释/嵌套/未匹配等分支）
# ===========================================================================


def test_balanced_end_non_bracket_returns_none() -> None:
    """open_pos 指向非括号字符时返回 None。"""
    assert _balanced_end("abc", 0) is None


def test_balanced_end_unmatched_returns_none() -> None:
    """未匹配的闭括号返回 None。"""
    assert _balanced_end("{", 0) is None
    assert _balanced_end("{ { }", 0) is None  # 外层未闭合


def test_balanced_end_simple_pairs() -> None:
    """简单平衡的成对括号。"""
    assert _balanced_end("{}", 0) == 2
    assert _balanced_end("{abc}", 0) == 5
    assert _balanced_end("[]", 0) == 2
    assert _balanced_end("()", 0) == 2


def test_balanced_end_nested_same_type() -> None:
    """同类型嵌套括号正确匹配。"""
    assert _balanced_end("{a{b}c}", 0) == 7
    assert _balanced_end("[a[b]c]", 0) == 7
    assert _balanced_end("(a(b)c)", 0) == 7


def test_balanced_end_string_with_escape() -> None:
    """字符串字面量内的转义引号不结束字符串，括号被忽略。"""
    src = '{"key": "val\\"{extra}"}'
    assert _balanced_end(src, 0) == len(src)


def test_balanced_end_string_braces_ignored() -> None:
    """单/双/反引号字符串内的括号被忽略。"""
    for q in ('"', "'", "`"):
        src = "{a " + q + "{inside}" + q + "}"
        assert _balanced_end(src, 0) == len(src)


def test_balanced_end_line_comment_with_newline() -> None:
    """行注释中的括号被忽略，到换行后恢复。"""
    src = "{a // {comment} \n}"
    assert _balanced_end(src, 0) == len(src)


def test_balanced_end_line_comment_no_newline() -> None:
    """行注释到字符串结束（无换行）→ 未匹配返回 None。"""
    src = "{a // comment without newline"
    assert _balanced_end(src, 0) is None


def test_balanced_end_block_comment() -> None:
    """块注释中的括号被忽略。"""
    src = "{a /* {comment} */ }"
    assert _balanced_end(src, 0) == len(src)


def test_balanced_end_block_comment_unterminated() -> None:
    """未闭合的块注释跳到末尾 → 未匹配返回 None。"""
    src = "{a /* never ends"
    assert _balanced_end(src, 0) is None


# ===========================================================================
# _split_top_level 测试（覆盖字符串/注释/嵌套分隔符忽略分支）
# ===========================================================================


def test_split_top_level_basic() -> None:
    """基础分隔。"""
    assert _split_top_level("a,b,c") == ["a", "b", "c"]
    assert _split_top_level("") == [""]


def test_split_top_level_nested() -> None:
    """嵌套结构内的分隔符被忽略。"""
    assert _split_top_level("{a,b},c,[d,e]") == ["{a,b}", "c", "[d,e]"]


def test_split_top_level_string_escape() -> None:
    """字符串内的转义与分隔符被忽略。"""
    parts = _split_top_level('"a\\",b",c')
    assert parts == ['"a\\",b"', "c"]


def test_split_top_level_string_separator_ignored() -> None:
    """字符串内的分隔符被忽略。"""
    for q in ('"', "'", "`"):
        assert _split_top_level(f"{q}a,{q},b") == [f"{q}a,{q}", "b"]


def test_split_top_level_line_comment() -> None:
    """行注释中的分隔符被忽略，换行后恢复。"""
    # 注释内的 , 被忽略，换行后的 , 才是分隔符
    assert _split_top_level("a // comment,\nb,c") == ["a // comment,\nb", "c"]


def test_split_top_level_line_comment_no_newline() -> None:
    """行注释到末尾（无换行）。"""
    assert _split_top_level("a // comment, no newline") == ["a // comment, no newline"]


def test_split_top_level_block_comment() -> None:
    """块注释中的分隔符被忽略。"""
    assert _split_top_level("a /* , */,b") == ["a /* , */", "b"]


def test_split_top_level_block_comment_unterminated() -> None:
    """未闭合的块注释到末尾。"""
    assert _split_top_level("a /* never ends") == ["a /* never ends"]


# ===========================================================================
# _extract_function_body / _third_param_name / _extract_deps 测试
# ===========================================================================


def test_extract_function_body_no_brace() -> None:
    """无 { 时原样返回。"""
    assert _extract_function_body("function()") == "function()"


def test_extract_function_body_unmatched() -> None:
    """{ 不匹配时原样返回。"""
    assert _extract_function_body("function() {") == "function() {"


def test_extract_function_body_normal() -> None:
    """正常截取函数体到匹配的 }。"""
    val = "function() { return 1; } extra"
    assert _extract_function_body(val) == "function() { return 1; }"


def test_third_param_name_present() -> None:
    """有第 3 个参数时返回其名。"""
    assert _third_param_name("function(a, b, c) {}") == "c"


def test_third_param_name_missing_function() -> None:
    """无 function() 形态时返回 None。"""
    assert _third_param_name("var x = 1;") is None


def test_third_param_name_too_few_params() -> None:
    """参数不足 3 个时返回 None。"""
    assert _third_param_name("function(a, b) {}") is None


def test_extract_deps_alias() -> None:
    """提取 alias(N) 形态的依赖。"""
    assert _extract_deps("r(100); r(200);", "r") == [100, 200]
    assert _extract_deps("no match", "r") == []


# ===========================================================================
# _extract_export_keys 测试
# ===========================================================================


def test_extract_export_keys_no_alias_call() -> None:
    """无 alias.d 调用时返回空列表。"""
    assert _extract_export_keys("var x = 1;", "__webpack_require__") == []


def test_extract_export_keys_double_quoted() -> None:
    """正常提取双引号键。"""
    src = '__webpack_require__.d(exports, {"foo": 1, "bar": 2});'
    keys = _extract_export_keys(src, "__webpack_require__")
    assert "foo" in keys
    assert "bar" in keys


def test_extract_export_keys_single_quoted() -> None:
    """单引号键也能提取。"""
    src = "__webpack_require__.d(exports, {'k1': 1, 'k2': 2});"
    keys = _extract_export_keys(src, "__webpack_require__")
    assert "k1" in keys
    assert "k2" in keys


def test_extract_export_keys_unbalanced_paren() -> None:
    """括号不匹配时跳过。"""
    src = "__webpack_require__.d(exports, "
    assert _extract_export_keys(src, "__webpack_require__") == []


def test_extract_export_keys_no_object_body() -> None:
    """无对象字面量时返回空。"""
    src = "__webpack_require__.d(exports, 123);"
    assert _extract_export_keys(src, "__webpack_require__") == []


def test_extract_export_keys_unbalanced_object() -> None:
    """对象字面量不匹配时返回空。"""
    src = "__webpack_require__.d(exports, {key: 1);"
    assert _extract_export_keys(src, "__webpack_require__") == []


# ===========================================================================
# _extract_json 容错测试
# ===========================================================================


def test_extract_json_invalid_text_returns_empty() -> None:
    """完全无效的 JSON 返回空 dict。"""
    assert _extract_json("not json at all") == {}


def test_extract_json_embedded_in_text() -> None:
    """嵌入文本中的 JSON 对象。"""
    data = _extract_json('before {"k": "v"} after')
    assert data == {"k": "v"}


def test_extract_json_codeblock_invalid_inner_falls_back() -> None:
    """代码块包裹但内部非法 JSON → 回退搜索 JSON 子串。"""
    text = '```json\nnot json\n{"k": 1}\n```'
    data = _extract_json(text)
    assert data == {"k": 1}


def test_extract_json_codeblock_no_json_anywhere() -> None:
    """代码块包裹且完全无 JSON → 返回空 dict。"""
    assert _extract_json("```json\nnot json\n```") == {}


def test_extract_json_braces_present_but_invalid_returns_empty() -> None:
    """文本含 {...} 但内部非法 JSON → 触发 except 分支返回空 dict。"""
    # _JSON_BLOCK_RE 匹配 "{invalid }"，但 json.loads 失败 → 返回 {}
    assert _extract_json("{invalid }") == {}


# ===========================================================================
# _strip_code_fence / _to_float 测试
# ===========================================================================


def test_strip_code_fence_with_fence() -> None:
    """去掉代码块包裹。"""
    text = "```javascript\nvar x = 1;\n```"
    assert _strip_code_fence(text) == "var x = 1;"


def test_strip_code_fence_without_fence() -> None:
    """无代码块包裹时原样返回。"""
    assert _strip_code_fence("var x = 1;") == "var x = 1;"


def test_to_float_valid() -> None:
    """合法数值被转换。"""
    assert _to_float("0.5") == 0.5
    assert _to_float(1) == 1.0


def test_to_float_invalid_returns_default() -> None:
    """非法值返回默认值。"""
    assert _to_float("abc") == 0.0
    assert _to_float(None) == 0.0
    assert _to_float("abc", default=0.5) == 0.5


# ===========================================================================
# JSAnalyzer.__init__ 默认 provider / _call_llm 测试
# ===========================================================================


def test_analyzer_default_provider_is_deepseek() -> None:
    """未传 provider 时创建 DeepSeekProvider。"""
    from web_crawler.ai.llm import DeepSeekProvider

    analyzer = JSAnalyzer()
    assert isinstance(analyzer.provider, DeepSeekProvider)
    assert analyzer.model == "deepseek-v4-pro"


def test_analyzer_model_fallback_when_provider_model_empty() -> None:
    """provider.model 为空时回退到 model 参数。"""

    class _EmptyModel:
        model = ""

        def chat(self, *a: Any, **k: Any) -> LLMResponse: ...

    analyzer = JSAnalyzer(provider=_EmptyModel())  # type: ignore[arg-type]
    assert analyzer.model == "deepseek-v4-pro"


def test_call_llm_finish_reason_length_appends_marker() -> None:
    """finish_reason=length 时追加截断标记。"""
    provider = _ScriptedProvider(["hello"], finish_reason="length")
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    out = analyzer._call_llm("sys", "user")
    assert "hello" in out
    assert "被截断" in out


def test_call_llm_normal_returns_content() -> None:
    """正常 LLM 调用返回 content。"""
    provider = _ScriptedProvider(["world"])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    assert analyzer._call_llm("sys", "user") == "world"


def test_call_llm_empty_content_returns_empty() -> None:
    """content 为空时返回空字符串（不抛错）。"""
    provider = _ScriptedProvider([""])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    assert analyzer._call_llm("sys", "user") == ""


# ===========================================================================
# analyze_fragment 测试（覆盖 LLM 解析与回退分支）
# ===========================================================================


def test_analyze_fragment_normal_returns_structured_result() -> None:
    """LLM 返回结构化 JSON 时正确填充 AnalysisResult。"""
    reply = (
        '{"algorithm": "AES-CBC", "inputs": ["k", "v"], "output": "base64", '
        '"code_flow": "encrypt", "confidence": 0.9, "deobfuscated": "function(){}"}'
    )
    provider = _ScriptedProvider([reply])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    frag = JSFragment(source="var x = 1;", url="http://x", size=10, is_minified=True)
    result = analyzer.analyze_fragment(frag)
    assert result.algorithm == "AES-CBC"
    assert result.inputs == ["k", "v"]
    assert result.output == "base64"
    assert result.code_flow == "encrypt"
    assert result.confidence == 0.9
    assert result.deobfuscated == "function(){}"


def test_analyze_fragment_confidence_high_clamped() -> None:
    """置信度 > 1 被 clamp 到 1.0。"""
    reply = '{"algorithm": "x", "confidence": 1.5}'
    provider = _ScriptedProvider([reply])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    result = analyzer.analyze_fragment(JSFragment(source="x"))
    assert result.confidence == 1.0


def test_analyze_fragment_confidence_negative_clamped() -> None:
    """置信度 < 0 被 clamp 到 0.0。"""
    reply = '{"algorithm": "x", "confidence": -0.5}'
    provider = _ScriptedProvider([reply])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    assert analyzer.analyze_fragment(JSFragment(source="x")).confidence == 0.0


def test_analyze_fragment_unparseable_returns_raw_in_code_flow() -> None:
    """LLM 返回无法解析的文本时回退到 code_flow（截断 500 字符）。"""
    provider = _ScriptedProvider(["not json text"])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    result = analyzer.analyze_fragment(JSFragment(source="x"))
    assert result.algorithm == "unknown"
    assert "not json text" in result.code_flow


def test_analyze_fragment_empty_response_returns_unparseable_message() -> None:
    """LLM 返回空字符串时给出"无法解析"提示。"""
    provider = _ScriptedProvider([""])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    result = analyzer.analyze_fragment(JSFragment(source="x"))
    assert result.algorithm == "unknown"
    assert "无法解析" in result.code_flow


def test_analyze_fragment_size_fallback_to_source_len() -> None:
    """fragment.size 为 0 时回退到 len(source)（不抛错）。"""
    reply = '{"algorithm": "x"}'
    provider = _ScriptedProvider([reply])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    result = analyzer.analyze_fragment(JSFragment(source="abcdef"))
    assert result.algorithm == "x"


def test_analyze_fragment_filters_none_inputs() -> None:
    """inputs 中的 None 项被过滤。"""
    reply = '{"algorithm": "x", "inputs": ["k", null, "v"]}'
    provider = _ScriptedProvider([reply])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    result = analyzer.analyze_fragment(JSFragment(source="x"))
    assert result.inputs == ["k", "v"]


# ===========================================================================
# extract_webpack_modules 边界测试（覆盖各种 continue / 兜底分支）
# ===========================================================================


def test_extract_webpack_modules_no_assignment_match() -> None:
    """无 __webpack_modules__ 赋值时返回空。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    assert analyzer.extract_webpack_modules("var x = 1;") == []


def test_extract_webpack_modules_no_container() -> None:
    """有 __webpack_modules__ 但无容器时返回空。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    assert analyzer.extract_webpack_modules("__webpack_modules__ = 123;") == []


def test_extract_webpack_modules_unmatched_container() -> None:
    """容器未闭合时返回空。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    src = "__webpack_modules__ = { 100: function() {"
    assert analyzer.extract_webpack_modules(src) == []


def test_extract_webpack_modules_empty_segment_skipped() -> None:
    """空 segment 被跳过，但仍解析其他模块。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    src = "__webpack_modules__ = { , 100: function() {} }"
    modules = analyzer.extract_webpack_modules(src)
    assert any(m.id == 100 for m in modules)


def test_extract_webpack_modules_string_key_ignored() -> None:
    """非数字字符串键的模块被跳过。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    src = '__webpack_modules__ = { "abc": function() {}, 100: function() {} }'
    modules = analyzer.extract_webpack_modules(src)
    ids = [m.id for m in modules]
    assert 100 in ids
    assert "abc" not in ids  # 字符串键被跳过


def test_extract_webpack_modules_segment_without_key_skipped() -> None:
    """对象模式下，无 key 的 segment 被跳过（_MODULE_KEY_RE 不匹配）。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    src = "__webpack_modules__ = { function() {}, 100: function() {} }"
    modules = analyzer.extract_webpack_modules(src)
    # 仅 100 被解析
    assert [m.id for m in modules] == [100]


def test_extract_webpack_modules_empty_string_key_skipped() -> None:
    """空字符串 key（"" : value）触发 key_str is None 分支被跳过。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    src = '__webpack_modules__ = { "": function() {}, 100: function() {} }'
    modules = analyzer.extract_webpack_modules(src)
    # 仅 100 被解析（"" key 被跳过）
    assert [m.id for m in modules] == [100]


def test_extract_webpack_modules_alias_from_third_param() -> None:
    """使用第 3 形参作为 require 别名，提取 alias(N) 依赖与 alias.d 导出。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    src = """
    var __webpack_modules__ = {
      100: function(module, exports, r) {
        r(200);
        r.d(exports, {"key": 1});
      },
      200: function(module, exports) {}
    };
    """
    modules = analyzer.extract_webpack_modules(src)
    by_id = {m.id: m for m in modules}
    assert 200 in by_id[100].dependencies
    assert "key" in by_id[100].exports


def test_extract_webpack_modules_standard_alias_used() -> None:
    """mod_source 含 __webpack_require__ 时优先用标准别名。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    src = """
    __webpack_modules__ = {
      1: function(module, exports, __webpack_require__) {
        __webpack_require__(2);
        __webpack_require__.d(exports, {"exp": 1});
      },
      2: function(module, exports) {}
    };
    """
    modules = analyzer.extract_webpack_modules(src)
    by_id = {m.id: m for m in modules}
    assert 2 in by_id[1].dependencies
    assert "exp" in by_id[1].exports


def test_extract_webpack_modules_deduplicates_deps() -> None:
    """重复的依赖 ID 被去重。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    src = """
    __webpack_modules__ = {
      1: function(module, exports, __webpack_require__) {
        __webpack_require__(2);
        __webpack_require__(2);
      },
      2: function(module, exports) {}
    };
    """
    modules = analyzer.extract_webpack_modules(src)
    assert modules[0].dependencies == [2]


# ===========================================================================
# identify_entry_point / trace_signing_flow 边界测试
# ===========================================================================


def test_identify_entry_point_empty_modules_returns_none() -> None:
    """空模块列表返回 None。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    assert analyzer.identify_entry_point([]) is None


def test_identify_entry_point_all_depended_fallback() -> None:
    """所有模块都被依赖时回退到全部模块作为候选。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    modules = [
        WebpackModule(id=1, source="", dependencies=[2]),
        WebpackModule(id=2, source="", dependencies=[1]),
    ]
    # 不抛错，返回 1 或 2
    assert analyzer.identify_entry_point(modules) in (1, 2)


def test_identify_entry_point_prefers_esm_mark() -> None:
    """带 __webpack_require__.r 标记的模块优先作为入口。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    modules = [
        WebpackModule(id=1, source="var x = 1;", dependencies=[]),
        WebpackModule(id=2, source="__webpack_require__.r(exports);", dependencies=[]),
    ]
    # 都未被依赖；带 ESM 标记的 2 应胜出
    assert analyzer.identify_entry_point(modules) == 2


def test_trace_signing_flow_no_producers() -> None:
    """无产出者时返回空链。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    modules = [WebpackModule(id=1, source="abc", dependencies=[])]
    assert analyzer.trace_signing_flow(modules, "missing") == []


def test_trace_signing_flow_missing_dep_skipped() -> None:
    """依赖的模块不在列表中时跳过，但仍记录产出者。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    modules = [WebpackModule(id=1, source="target", dependencies=[999])]
    chain = analyzer.trace_signing_flow(modules, "target")
    # 999 不在 by_id 被跳过；1 仍入链
    assert chain == [1]


def test_trace_signing_flow_chain_order() -> None:
    """依赖在前、产出者在后。"""
    analyzer = JSAnalyzer(provider=_StubProvider())
    modules = [
        WebpackModule(id=1, source="target", dependencies=[2, 3]),
        WebpackModule(id=2, source="", dependencies=[]),
        WebpackModule(id=3, source="", dependencies=[]),
    ]
    chain = analyzer.trace_signing_flow(modules, "target")
    # 2, 3 在前，1 在后
    assert chain[-1] == 1
    assert set(chain[:-1]) == {2, 3}


# ===========================================================================
# deobfuscate / suggest_reimplementation 测试（LLM 路径）
# ===========================================================================


def test_deobfuscate_strips_code_fence() -> None:
    """反混淆：去掉代码块包裹后返回。"""
    provider = _ScriptedProvider(["```javascript\nvar x = 1;\n```"])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    assert analyzer.deobfuscate("var x=1;") == "var x = 1;"


def test_deobfuscate_no_fence_returns_as_is() -> None:
    """LLM 返回无代码块包裹时原样 strip 返回。"""
    provider = _ScriptedProvider(["var y = 2;"])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    assert analyzer.deobfuscate("var y=2;") == "var y = 2;"


def test_deobfuscate_truncates_long_code() -> None:
    """超长代码被截断后调用 LLM。"""
    provider = _ScriptedProvider(["ok"])
    analyzer = JSAnalyzer(provider=provider, max_chars=10)  # type: ignore[arg-type]
    analyzer.deobfuscate("a" * 100)
    assert len(provider.calls) == 1


def test_suggest_reimplementation_strips_code_fence() -> None:
    """重写：去掉代码块包裹后返回。"""
    provider = _ScriptedProvider(["```python\nimport hashlib\n```"])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    out = analyzer.suggest_reimplementation("function() {}", language="python")
    assert out == "import hashlib"


def test_suggest_reimplementation_default_language_python() -> None:
    """默认语言为 python。"""
    provider = _ScriptedProvider(["```python\nx = 1\n```"])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    out = analyzer.suggest_reimplementation("var x = 1;")
    assert "x = 1" in out


def test_suggest_reimplementation_truncates_long_code() -> None:
    """超长代码被截断后调用 LLM。"""
    provider = _ScriptedProvider(["ok"])
    analyzer = JSAnalyzer(provider=provider, max_chars=10)  # type: ignore[arg-type]
    analyzer.suggest_reimplementation("a" * 100)
    assert len(provider.calls) == 1


# ===========================================================================
# AnalysisResult / WebpackModule dataclass 默认值测试
# ===========================================================================


def test_analysis_result_defaults() -> None:
    """AnalysisResult 默认值正确。"""
    from web_crawler.ai.analyzer import AnalysisResult

    result = AnalysisResult()
    assert result.algorithm == "unknown"
    assert result.inputs == []
    assert result.output == ""
    assert result.code_flow == ""
    assert result.confidence == 0.0
    assert result.deobfuscated is None


def test_webpack_module_defaults() -> None:
    """WebpackModule 默认值正确。"""
    mod = WebpackModule(id=1, source="x")
    assert mod.id == 1
    assert mod.source == "x"
    assert mod.dependencies == []
    assert mod.exports == []


# ===========================================================================
# 回归：inputs 为逗号分隔字符串时按逗号拆分
# ===========================================================================


def test_analyze_fragment_inputs_string_split() -> None:
    """模型把 inputs 返回成字符串时应按逗号拆分而非逐字符迭代。"""
    reply = '{"algorithm": "x", "inputs": "timestamp, nonce, body"}'
    provider = _ScriptedProvider([reply])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    result = analyzer.analyze_fragment(JSFragment(source="x"))
    assert result.inputs == ["timestamp", "nonce", "body"]


def test_analyze_fragment_inputs_list_still_works() -> None:
    """inputs 为数组时行为不变。"""
    reply = '{"algorithm": "x", "inputs": ["a", "b"]}'
    provider = _ScriptedProvider([reply])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    result = analyzer.analyze_fragment(JSFragment(source="x"))
    assert result.inputs == ["a", "b"]


# ===========================================================================
# 回归：来源 URL（不可信输入）JSON 转义 + 不可信提示
# ===========================================================================


def test_analyze_fragment_escapes_untrusted_url() -> None:
    """URL 中的引号/换行应被转义，且 prompt 含不可信数据提示。"""
    reply = '{"algorithm": "x"}'
    provider = _ScriptedProvider([reply])
    analyzer = JSAnalyzer(provider=provider)  # type: ignore[arg-type]
    frag = JSFragment(source="x", url='https://x.example/"inj"\nend')
    analyzer.analyze_fragment(frag)
    user_prompt = str(provider.calls[0][1].content)
    assert '"inj"' not in user_prompt
    assert "不可信" in user_prompt

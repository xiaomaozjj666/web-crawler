"""JSAnalyzer 纯解析逻辑测试（不调用真实 LLM，不触网）。"""

from __future__ import annotations

from typing import Any

from web_crawler.ai.analyzer import (
    JSAnalyzer,
    JSFragment,
    _extract_json,
)
from web_crawler.ai.llm import LLMResponse


class _StubProvider:
    """纯解析测试用的 stub provider；被调用即说明测试逻辑出错。"""

    model = "stub"

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        raise AssertionError("纯解析测试不应调用 LLM")


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

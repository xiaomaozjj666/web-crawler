"""Tests for custom supporting types."""

from __future__ import annotations

from typing import Any

from web_crawler._types import Attrs, ResultList, TextHandler, ensure_list, iter_chunks


def test_text_handler_clean_collapses_whitespace() -> None:
    assert str(TextHandler("  hello   world  ").clean()) == "hello world"


def test_text_handler_chainable_returns_text_handler() -> None:
    result = TextHandler("  hi  ").clean()
    assert isinstance(result, TextHandler)
    assert str(result) == "hi"


def test_text_handler_remove_and_trim() -> None:
    assert str(TextHandler("a-b-c").remove("-")) == "abc"
    assert str(TextHandler("  x  ").trim()) == "x"


def test_text_handler_starts_ends() -> None:
    t = TextHandler("hello world")
    assert t.starts_with("hello")
    assert t.ends_with("world")
    assert not t.starts_with("world")


def test_attrs_case_insensitive() -> None:
    a = Attrs({"Class": "btn", "ID": "x"})
    assert a.get("class") == "btn"
    assert a.get("CLASS") == "btn"
    assert a.get("missing", "default") == "default"
    assert "id" in a
    assert "href" not in a


def test_attrs_non_str_key_not_in() -> None:
    a = Attrs({"x": "1"})
    assert (123) not in a  # type: ignore[operator]


def test_result_list_first_last() -> None:
    rl: ResultList[int] = ResultList([1, 2, 3])
    # Scrapling-style property access (no parens)
    assert rl.first == 1
    assert rl.last == 3
    assert ResultList[int]().first is None
    assert ResultList[int]().last is None


def test_result_list_text_concatenates() -> None:
    class FakeSel:
        def __init__(self, t: str) -> None:
            self.text = t

    rl = ResultList([FakeSel("a"), FakeSel("b"), FakeSel("c")])
    assert str(rl.text) == "a b c"
    assert rl.get_all_texts() == ["a", "b", "c"]


def test_ensure_list() -> None:
    assert ensure_list(None) == []
    assert ensure_list(5) == [5]
    assert ensure_list([1, 2]) == [1, 2]
    assert ensure_list((1, 2)) == [1, 2]


def test_iter_chunks() -> None:
    chunks = list(iter_chunks([1, 2, 3, 4, 5], 2))
    assert chunks == [[1, 2], [3, 4], [5]]


def test_result_list_get_default_when_empty() -> None:
    rl: ResultList[int] = ResultList()
    assert rl.get(99) == 99


def test_result_list_get_returns_first_when_non_empty() -> None:
    rl: ResultList[str] = ResultList(["a", "b"])
    assert rl.get() == "a"


def test_result_list_getall() -> None:
    rl: ResultList[int] = ResultList([1, 2, 3])
    assert rl.getall() == [1, 2, 3]
    assert ResultList[int]().getall() == []


def test_result_list_length_property() -> None:
    rl: ResultList[int] = ResultList([1, 2, 3])
    assert rl.length == 3
    assert ResultList[int]().length == 0


def test_result_list_slice_preserves_type() -> None:
    rl: ResultList[int] = ResultList([1, 2, 3, 4])
    sliced = rl[1:3]
    assert isinstance(sliced, ResultList)
    assert list(sliced) == [2, 3]


def test_result_list_index_access_returns_item() -> None:
    rl: ResultList[int] = ResultList([10, 20])
    assert rl[0] == 10
    assert rl[-1] == 20


def test_result_list_to_json_returns_string() -> None:
    rl: ResultList[dict] = ResultList([{"a": 1}, {"b": 2}])
    text = rl.to_json()
    assert '"a": 1' in text
    assert '"b": 2' in text


def test_result_list_to_json_writes_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    rl: ResultList[dict] = ResultList([{"x": 1}])
    path = tmp_path / "out.json"
    rl.to_json(path)
    # 默认 indent=2，解析回字典比较内容
    import json as _json

    assert _json.loads(path.read_text(encoding="utf-8")) == [{"x": 1}]


def test_result_list_to_jsonl_returns_string() -> None:
    rl: ResultList[dict] = ResultList([{"a": 1}, {"b": 2}])
    text = rl.to_jsonl()
    lines = text.strip().split("\n")
    assert lines[0] == '{"a": 1}'
    assert lines[1] == '{"b": 2}'


def test_result_list_to_jsonl_writes_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    rl: ResultList[dict] = ResultList([{"x": 1}, {"x": 2}])
    path = tmp_path / "out.jsonl"
    rl.to_jsonl(path)
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert lines[0] == '{"x": 1}'
    assert lines[1] == '{"x": 2}'


def test_result_list_to_json_coerces_dataclass() -> None:
    from dataclasses import dataclass

    @dataclass
    class Item:
        name: str
        price: float

    rl: ResultList = ResultList([Item("Widget", 9.99), Item("Gadget", 19.99)])  # type: ignore[arg-type]
    text = rl.to_json()
    assert "Widget" in text
    assert "19.99" in text


# ===========================================================================
# 扩展：未覆盖分支补齐
# ===========================================================================


def test_result_list_text_property_str_fallback_for_non_text_items() -> None:
    """text 属性在 item 无 text 属性时回退到 str(item)（line 104）。"""
    # int 没有 .text 属性 → text=None → str(item)
    rl: ResultList[int] = ResultList([42, 99])
    result = str(rl.text)
    assert "42" in result
    assert "99" in result


def test_result_list_texts_handles_non_text_items() -> None:
    """texts 属性在 item 无 text 时回退到空串。"""
    rl: ResultList[int] = ResultList([1, 2])
    texts = rl.texts
    assert len(texts) == 2
    # int 无 .text → text=None → TextHandler("")
    assert all(str(t) == "" for t in texts)


def test_result_list_attr_method_on_items_without_attr() -> None:
    """attr 方法对无 attr 方法的元素返回 default（lines 124-131）。"""
    # 混合：有 attr 方法的 Selector-like 和无 attr 方法的纯 str
    class HasAttr:
        def attr(self, name: str, default: Any = None) -> Any:
            return f"val-{name}"

    rl: ResultList = ResultList([HasAttr(), "plain-string", 42])  # type: ignore[arg-type]
    result = rl.attr("id", "default")
    assert result[0] == "val-id"
    assert result[1] == "default"
    assert result[2] == "default"


def test_result_list_attr_method_all_have_attr() -> None:
    """attr 方法在所有元素都有 attr 方法时正常返回。"""

    class HasAttr:
        def __init__(self, v: str) -> None:
            self._v = v

        def attr(self, name: str, default: Any = None) -> Any:
            return self._v if name == "id" else default

    rl: ResultList = ResultList([HasAttr("a"), HasAttr("b")])  # type: ignore[arg-type]
    assert rl.attr("id") == ["a", "b"]


def test_result_list_coerce_fallback_for_plain_items() -> None:
    """_coerce 对无 as_dict/__dict__ 的对象返回原值（line 163）。"""
    # int 无 as_dict 也无 __dict__
    rl: ResultList[int] = ResultList([1, 2])
    text = rl.to_json()
    # int 被直接序列化
    import json as _json

    assert _json.loads(text) == [1, 2]


def test_result_list_coerce_with_as_dict_method() -> None:
    """_coerce 优先调用 as_dict()。"""

    class WithAsDict:
        def as_dict(self) -> dict:
            return {"custom": True}

    rl: ResultList = ResultList([WithAsDict()])  # type: ignore[arg-type]
    import json as _json

    parsed = _json.loads(rl.to_json())
    assert parsed == [{"custom": True}]


def test_result_list_coerce_with_dict_attr() -> None:
    """_coerce 对有 __dict__ 的对象取 vars()。"""

    class Plain:
        def __init__(self) -> None:
            self.x = 1
            self.y = 2

    rl: ResultList = ResultList([Plain()])  # type: ignore[arg-type]
    import json as _json

    parsed = _json.loads(rl.to_json())
    assert parsed == [{"x": 1, "y": 2}]


def test_result_list_coerce_skips_private_attrs() -> None:
    """_coerce 过滤 _ 开头的私有属性。"""

    class WithPrivate:
        def __init__(self) -> None:
            self.public = "yes"
            self._private = "no"

    rl: ResultList = ResultList([WithPrivate()])  # type: ignore[arg-type]
    import json as _json

    parsed = _json.loads(rl.to_json())
    assert parsed == [{"public": "yes"}]


def test_result_list_text_property_filters_empty_parts() -> None:
    """text 属性过滤空字符串部分。"""

    class FakeSel:
        def __init__(self, t: str) -> None:
            self.text = t

    rl = ResultList([FakeSel(""), FakeSel("a"), FakeSel("")])
    assert str(rl.text) == "a"


def test_ensure_list_with_set() -> None:
    """ensure_list 处理 set 类型。"""
    assert sorted(ensure_list({1, 2, 3})) == [1, 2, 3]


def test_ensure_list_with_result_list() -> None:
    """ensure_list 处理 ResultList 类型。"""
    rl: ResultList[int] = ResultList([1, 2])
    assert ensure_list(rl) == [1, 2]


def test_attrs_get_with_none_default() -> None:
    """Attrs.get 默认返回 None。"""
    a = Attrs({"X": "1"})
    assert a.get("missing") is None


def test_attrs_contains_case_insensitive() -> None:
    """Attrs.__contains__ 大小写不敏感。"""
    a = Attrs({"Content-Type": "text/html"})
    assert "content-type" in a
    assert "CONTENT-TYPE" in a
    assert "x-missing" not in a


def test_ensure_list_passes_through_generators() -> None:
    """ensure_list 对生成器/range 等任意 Iterable 透传（Fix6）。"""
    assert ensure_list(iter([1, 2])) == [1, 2]
    assert ensure_list(range(3)) == [0, 1, 2]
    assert sorted(ensure_list({1, 2})) == [1, 2]


def test_ensure_list_keeps_str_and_bytes_as_single_item() -> None:
    """str/bytes 虽可迭代，但作为单个值包裹。"""
    assert ensure_list("ab") == ["ab"]
    assert ensure_list(b"ab") == [b"ab"]

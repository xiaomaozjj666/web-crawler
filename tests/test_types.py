"""Tests for custom supporting types."""

from __future__ import annotations

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

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

"""Tests for the public adaptive API: save / retrieve / relocate on Selector."""

from __future__ import annotations

import pytest

from web_crawler import AdaptiveStorage, Selector


def test_save_and_retrieve_roundtrip(tmp_storage: AdaptiveStorage) -> None:
    page = Selector(
        '<div><a id="p1" class="product">Widget</a></div>',
        url="https://shop.example.com",
        adaptive=True,
        storage=tmp_storage,
    )
    el = page.css_first("#p1")
    page.save(el, "my-product")

    record = page.retrieve("my-product")
    assert record is not None
    assert record["tag"] == "a"
    assert "Widget" in record["text"]


def test_retrieve_missing_returns_none(tmp_storage: AdaptiveStorage) -> None:
    page = Selector("<div>x</div>", url="https://x.example", adaptive=True, storage=tmp_storage)
    assert page.retrieve("nope") is None


def test_relocate_finds_element_after_markup_change(tmp_storage: AdaptiveStorage) -> None:
    # v1: element with id
    page1 = Selector(
        '<div><a id="p1" class="product">Widget</a></div>',
        url="https://shop.example.com",
        adaptive=True,
        storage=tmp_storage,
    )
    el = page1.css_first("#p1")
    page1.save(el, "p1")

    # v2: id gone, structure shifted, but text+class retained
    page2 = Selector(
        '<div><span class="wrap"><a data-id="p1" class="product new">Widget</a></span></div>',
        url="https://shop.example.com",
        adaptive=True,
        storage=tmp_storage,
    )
    record = page2.retrieve("p1")
    assert record is not None

    relocated = page2.relocate(record, threshold=0.3)
    assert len(relocated) == 1
    assert str(relocated.first.text) == "Widget"


def test_relocate_accepts_selector_input(tmp_storage: AdaptiveStorage) -> None:
    page1 = Selector(
        '<div><a id="p1" class="product">Gadget</a></div>',
        url="https://shop.example.com",
        adaptive=True,
        storage=tmp_storage,
    )
    el1 = page1.css_first("#p1")

    page2 = Selector(
        '<div><a id="x" class="product">Gadget</a></div>',
        url="https://shop.example.com",
        adaptive=True,
        storage=tmp_storage,
    )
    relocated = page2.relocate(el1, threshold=0.3)
    assert len(relocated) == 1
    assert str(relocated.first.text) == "Gadget"


def test_relocate_returns_empty_below_threshold(tmp_storage: AdaptiveStorage) -> None:
    page1 = Selector(
        '<div><a id="p1" class="product">Widget</a></div>',
        url="https://shop.example.com",
        adaptive=True,
        storage=tmp_storage,
    )
    el = page1.css_first("#p1")
    page1.save(el, "p1")

    page2 = Selector(
        '<div><div class="unrelated">completely different</div></div>',
        url="https://shop.example.com",
        adaptive=True,
        storage=tmp_storage,
    )
    record = page2.retrieve("p1")
    relocated = page2.relocate(record, threshold=0.99)
    assert len(relocated) == 0


def test_save_requires_adaptive_mode() -> None:
    page = Selector("<div><a>x</a></div>", adaptive=False)
    with pytest.raises(RuntimeError, match="adaptive"):
        page.save(page.css_first("a"), "id")


def test_retrieve_requires_adaptive_mode() -> None:
    page = Selector("<div>x</div>", adaptive=False)
    with pytest.raises(RuntimeError, match="adaptive"):
        page.retrieve("id")


def test_relocate_requires_adaptive_mode() -> None:
    page = Selector("<div>x</div>", adaptive=False)
    with pytest.raises(RuntimeError, match="adaptive"):
        page.relocate({}, threshold=0.5)

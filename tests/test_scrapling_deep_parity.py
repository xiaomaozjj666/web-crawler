"""Deep parity coverage for the Scrapling-style parser surface."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lxml import etree

import web_crawler
from web_crawler import ResultList, Selector, TextHandler
from web_crawler.parser.adaptive import best_match, compute_fingerprint, similarity_score
from web_crawler.parser.storage import AdaptiveStorage


def test_text_and_resultlist_regex_helpers() -> None:
    text = TextHandler("A &amp; B 42 7")
    assert text.re(r"(\d+)") == ["42", "7"]
    assert text.re_first(r"(\d+)") == "42"
    assert text.re_first(r"missing", default="fallback") == "fallback"
    assert text.re(r"&\w+;", replace_entities=False) == ["&amp;"]
    assert text.re(r"&\w+;") == ["&"]
    assert TextHandler("42 \n 7").re(r"42\s+7", clean_match=True) == ["42 7"]
    assert TextHandler("Alpha").re("alpha", case_sensitive=False) == ["Alpha"]

    items = ResultList([Selector("<span>42</span>"), TextHandler("7")])
    assert items.re(r"\d+") == ["42", "7"]
    assert items.re_first(r"\d+") == "42"
    assert items.re_first(r"missing", default="fallback") == "fallback"
    assert items.filter(lambda item: isinstance(item, Selector)).length == 1
    assert items.search(lambda item: isinstance(item, TextHandler)) == "7"
    assert list(items.map(lambda item: str(getattr(item, "text", item)))) == ["42", "7"]


def test_container_aliases_and_exports_are_consistent() -> None:
    from web_crawler import (
        Attrs,
        CoverageReport,
        ExtractionRules,
        RuleCoverage,
        Selectors,
        TextHandlers,
        _types,
    )
    from web_crawler import (
        best_match as exported_best_match,
    )
    from web_crawler.parser import Selectors as ParserSelectors
    from web_crawler.parser import selector as selector_module

    assert Selectors is ResultList
    assert TextHandlers is ResultList
    assert ParserSelectors is ResultList
    assert exported_best_match is best_match
    assert Attrs is _types.Attrs
    assert ExtractionRules is not None
    assert RuleCoverage is not None
    assert CoverageReport is not None
    assert "Callable" not in _types.__all__
    assert "Selectors" in _types.__all__
    assert "Adaptor" in selector_module.__all__
    assert selector_module.Adaptor is selector_module.Selector
    assert "CoverageReport" in web_crawler.__all__
    assert "CoverageReport" in web_crawler._LAZY_IMPORTS


def test_parser_subpackage_import_is_lazy() -> None:
    code = (
        "import sys; "
        "import web_crawler.parser as p; "
        "assert 'web_crawler.parser.links' not in sys.modules; "
        "assert 'web_crawler.parser.rules' not in sys.modules; "
        "assert 'web_crawler.parser.selector' not in sys.modules; "
        "assert p.Selector.__name__ == 'Selector'; "
        "assert 'web_crawler.parser.selector' in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_ignore_attributes_changes_fingerprint_and_matching() -> None:
    reference = etree.fromstring('<a class="item" data-price="10" href="/a">A</a>')
    candidate = etree.fromstring('<a class="item" href="/a">A</a>')

    full = similarity_score(compute_fingerprint(reference), compute_fingerprint(candidate))
    ignored = similarity_score(
        compute_fingerprint(reference, ["data-price"]),
        compute_fingerprint(candidate, ["data-price"]),
    )
    assert ignored > full
    attrs = json.loads(compute_fingerprint(reference, ("data-price", "href")))["attrs"]
    assert set(attrs) == {"class"}
    assert set(json.loads(compute_fingerprint(reference, "data-price"))["attrs"]) == {
        "class",
        "href",
    }

    matched, score = best_match(
        [candidate],
        compute_fingerprint(reference, ["data-price"]),
        0.5,
        ignore_attributes=["data-price"],
    )
    assert matched is not None
    assert score > 0.5


def test_selector_ignore_attributes_propagates_and_survives_relocation(tmp_path: Path) -> None:
    storage = AdaptiveStorage(tmp_path / "adaptive.sqlite3")
    try:
        html = '<div><a id="x" class="item" data-price="10">A</a></div>'
        page = Selector(html, adaptive=True, storage=storage, ignore_attributes=["data-price"])
        first = page.css_first("#x")
        assert first is not None
        assert first._ignore_attributes == ("data-price",)
        page.save(first, "item")
        record = page.retrieve("item")
        assert record is not None
        assert "data-price" not in json.loads(record["fingerprint"])["attrs"]

        changed = '<div><a id="changed" class="item" data-price="99">A</a></div>'
        page2 = Selector(
            changed,
            adaptive=True,
            storage=storage,
            ignore_attributes=["data-price"],
        )
        relocated = page2.relocate(record)
        assert relocated.first is not None
        assert relocated.first.attr("id") == "changed"
    finally:
        storage.close()


def test_dom_aliases_ancestors_and_document_relationships() -> None:
    html = (
        '<div><p id="first">one</p>'
        '<span id="mid">two</span>'
        '<b id="last" class="item">three</b></div>'
    )
    page = Selector(html)
    mid = page.css_first("#mid")
    last = page.css_first("#last")
    assert mid is not None and last is not None

    assert mid.next_sibling is not None
    assert mid.next_sibling.attr("id") == "last"
    assert mid.previous_sibling is not None
    assert mid.previous_sibling.attr("id") == "first"
    assert [node.tag for node in mid.ancestors] == ["div", "body", "html"]
    assert mid.find_ancestor(lambda node: node.tag == "div") is not None
    assert mid.has_class("item") is False
    assert last.has_class("item") is True
    assert any(node.attr("id") == "last" for node in mid.below_elements)
    assert any(node.attr("id") == "first" for node in mid.above_elements)
    assert all(node._element is not mid._element for node in mid.below_elements)
    assert all(node._element is not mid._element for node in mid.above_elements)

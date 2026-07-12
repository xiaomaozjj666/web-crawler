"""Tests for the Selector parser layer (CSS, XPath, text, similarity, adaptive)."""

from __future__ import annotations

from web_crawler.parser import AdaptiveStorage, Selector

HTML = """
<html><body>
  <div id="main">
    <a id="p1" class="product link">Product 1</a>
    <a id="p2" class="product">Product 2</a>
    <a id="ext" class="link">External</a>
  </div>
  <p class="desc">some description text</p>
</body></html>
"""


def test_css_returns_matching_elements() -> None:
    page = Selector(HTML, url="https://shop.example.com")
    products = page.css(".product")
    assert len(products) == 2
    assert [str(p.text) for p in products] == ["Product 1", "Product 2"]


def test_css_first_returns_first_match() -> None:
    page = Selector(HTML)
    assert str(page.css_first(".product").text) == "Product 1"


def test_css_first_missing_returns_default() -> None:
    page = Selector(HTML)
    assert page.css_first(".nonexistent") is None
    sentinel = Selector("<div/>")
    assert page.css_first(".nonexistent", sentinel) is sentinel


def test_xpath_selects_elements() -> None:
    page = Selector(HTML)
    anchors = page.xpath("//a")
    assert len(anchors) == 3


def test_xpath_first() -> None:
    page = Selector(HTML)
    assert str(page.xpath_first("//a").text) == "Product 1"


def test_tag_and_attrib() -> None:
    page = Selector(HTML)
    el = page.css_first("#p1")
    assert el.tag == "a"
    assert el.attr("id") == "p1"
    assert el.attr("class") == "product link"
    assert el.attr("missing", "fallback") == "fallback"
    assert el.attrib.get("class") == "product link"


def test_text_and_html() -> None:
    page = Selector(HTML)
    el = page.css_first("#p1")
    assert str(el.text) == "Product 1"
    assert "Product 1" in el.html


def test_parent_and_children() -> None:
    page = Selector(HTML)
    main = page.css_first("#main")
    children = main.children
    # 3 <a> children (whitespace text nodes are not elements)
    assert len(children) == 3
    parent = main.parent
    assert parent is not None
    assert parent.tag == "body"


def test_find_by_text_substring() -> None:
    page = Selector(HTML)
    matches = page.find_by_text("Product")
    assert len(matches) == 2


def test_find_by_text_exact_case_insensitive() -> None:
    page = Selector(HTML)
    matches = page.find_by_text("product 1", exact=True)
    assert len(matches) == 1
    assert matches[0].attr("id") == "p1"


def test_find_similar_returns_structural_neighbors() -> None:
    page = Selector(HTML)
    ref = page.css_first("#p1")
    similar = page.find_similar(ref, threshold=0.3)
    # Product 2 has nearly identical structure to Product 1
    tags = [s.tag for s in similar]
    assert "a" in tags


def test_selector_is_truthy_for_leaf_element() -> None:
    """Regression: leaf elements with no children must still be truthy."""
    page = Selector('<div><a id="leaf">text</a></div>')
    leaf = page.css_first("#leaf")
    assert leaf is not None
    assert bool(leaf) is True  # would be False under the old __len__ bug
    assert str(leaf.text) == "text"


def test_repr_contains_tag_and_text() -> None:
    page = Selector(HTML)
    el = page.css_first("#p1")
    assert "Selector" in repr(el)
    assert "a" in repr(el)


def test_iter_yields_children() -> None:
    page = Selector(HTML)
    main = page.css_first("#main")
    tags = [c.tag for c in main]
    assert tags == ["a", "a", "a"]


def test_adaptive_save_and_relocate(tmp_storage: AdaptiveStorage) -> None:
    page1 = Selector(
        '<div><a id="p1" class="product">Product 1</a></div>',
        url="https://shop.example.com",
        adaptive=True,
        storage=tmp_storage,
    )
    saved = page1.css_first("#p1", auto_save=True)
    assert saved is not None
    assert str(saved.text) == "Product 1"

    # Markup changed: id gone, structure shifted, but text + class retained.
    page2 = Selector(
        '<div><span class="wrap"><a data-id="p1" class="product new">Product 1</a></span></div>',
        url="https://shop.example.com",
        adaptive=True,
        storage=tmp_storage,
    )
    relocated = page2.css_first("#p1", adaptive=True)
    assert relocated is not None
    assert str(relocated.text) == "Product 1"
    assert bool(relocated) is True


def test_adaptive_lookup_without_storage_returns_none() -> None:
    # adaptive=True but no prior save -> returns empty ResultList, no crash.
    page = Selector("<div><a>just a</a></div>", url="https://x.example", adaptive=True)
    assert page.css_first("#missing", adaptive=True) is None


def test_adaptive_disabled_does_not_relocate() -> None:
    page = Selector('<div><a id="p1">x</a></div>', adaptive=False)
    assert page.css_first("#p1") is not None
    assert page.css_first("#missing", adaptive=True) is None


def test_bytes_input_is_parsed() -> None:
    page = Selector(b"<div><p>bytes</p></div>")
    assert str(page.css_first("p").text) == "bytes"


def test_xml_parser_mode() -> None:
    page = Selector(b"<root><item>1</item></root>", parser="xml")
    assert page.tag == "root"
    assert str(page.css_first("item").text) == "1"


def test_result_list_text_property_on_css_result() -> None:
    page = Selector(HTML)
    products = page.css(".product")
    assert "Product 1" in str(products.text)
    assert "Product 2" in str(products.text)


def test_xpath_with_auto_save(tmp_storage: AdaptiveStorage) -> None:
    page = Selector(
        '<div><a id="x1" class="c">Hi</a></div>',
        url="https://shop.example.com",
        adaptive=True,
        storage=tmp_storage,
    )
    el = page.xpath_first("//a", auto_save=True)
    assert el is not None
    assert str(el.text) == "Hi"


def test_find_similar_threshold_filters() -> None:
    page = Selector(HTML)
    ref = page.css_first("#p1")
    # Very high threshold excludes everything (no perfect structural twin)
    similar = page.find_similar(ref, threshold=0.99)
    assert len(similar) == 0


def test_css_id_selector() -> None:
    page = Selector(HTML)
    assert page.css_first("#ext") is not None
    assert str(page.css_first("#ext").text) == "External"


# --- Scrapling-parity methods -------------------------------------------


def test_find_by_regex_matches_direct_text() -> None:
    page = Selector(HTML)
    matches = page.find_by_regex(r"Product \d")
    assert len(matches) == 2
    tags = [m.tag for m in matches]
    assert tags == ["a", "a"]


def test_find_by_regex_case_insensitive() -> None:
    page = Selector(HTML)
    matches = page.find_by_regex("product 1", case_sensitive=False)
    assert len(matches) == 1
    assert matches[0].attr("id") == "p1"


def test_find_by_regex_no_match_returns_empty() -> None:
    page = Selector(HTML)
    assert len(page.find_by_regex(r"zzz\d+")) == 0


def test_find_by_regex_accepts_compiled_pattern() -> None:
    import re

    page = Selector(HTML)
    matches = page.find_by_regex(re.compile(r"Product"))
    assert len(matches) == 2


def test_re_returns_all_matches() -> None:
    page = Selector('<div id="x">price: 10 and 20</div>')
    el = page.css_first("#x")
    assert el.re(r"\d+") == ["10", "20"]


def test_re_with_capturing_groups() -> None:
    page = Selector('<div id="x">a=1 b=2</div>')
    el = page.css_first("#x")
    assert el.re(r"(\w)=(\d)") == ["a", "1", "b", "2"]


def test_re_first_returns_first_or_default() -> None:
    page = Selector('<div id="x">val 42</div>')
    el = page.css_first("#x")
    assert el.re_first(r"\d+") == "42"
    assert el.re_first(r"zzz", "fallback") == "fallback"


def test_re_clean_match_collapses_whitespace() -> None:
    page = Selector('<div id="x">  hello   world  123</div>')
    el = page.css_first("#x")
    # clean_match collapses internal whitespace before matching
    assert el.re(r"\d+", clean_match=True) == ["123"]


def test_get_all_text_joins_descendants() -> None:
    page = Selector("<div><p>hello</p><p>world</p></div>")
    text = str(page.css_first("div").get_all_text())
    assert "hello" in text
    assert "world" in text


def test_get_all_text_ignores_script_style_by_default() -> None:
    page = Selector("<div><p>keep</p><script>drop1</script><style>drop2</style></div>")
    text = str(page.css_first("div").get_all_text())
    assert "keep" in text
    assert "drop1" not in text
    assert "drop2" not in text


def test_get_all_text_custom_separator_and_strip() -> None:
    page = Selector("<div><p> a </p><p> b </p></div>")
    text = str(page.css_first("div").get_all_text(separator="|", strip=True))
    assert text == "a|b"


def test_prettify_returns_pretty_html() -> None:
    page = Selector("<div><span>x</span></div>")
    pretty = page.css_first("div").prettify()
    assert "<div>" in pretty
    assert "<span>x</span>" in pretty
    # pretty_print introduces newlines/indentation
    assert "\n" in pretty


def test_result_list_get_and_getall() -> None:
    page = Selector(HTML)
    products = page.css(".product")
    # get() returns text of first element
    assert str(products.get()) == "Product 1"
    # getall() returns list of all texts
    assert [str(t) for t in products.getall()] == ["Product 1", "Product 2"]


def test_result_list_get_default_when_empty() -> None:
    page = Selector(HTML)
    empty = page.css(".nonexistent")
    assert empty.get("none") == "none"
    assert empty.getall() == []


def test_result_list_first_last_are_properties() -> None:
    page = Selector(HTML)
    products = page.css(".product")
    # Scrapling-style property access (no parens)
    assert str(products.first.text) == "Product 1"
    assert str(products.last.text) == "Product 2"
    assert page.css(".nonexistent").first is None
    assert page.css(".nonexistent").last is None


def test_result_list_length_property() -> None:
    page = Selector(HTML)
    products = page.css(".product")
    assert products.length == 2


def test_result_list_slice_preserves_type() -> None:
    page = Selector(HTML)
    products = page.css(".product")
    sliced = products[0:1]
    assert isinstance(sliced, type(products))
    assert len(sliced) == 1


def test_result_list_css_batch_operation() -> None:
    page = Selector(HTML)
    items = page.css("#main")
    # batch css over the container finds the nested anchors
    anchors = items.css("a")
    assert len(anchors) == 3


def test_result_list_xpath_batch_operation() -> None:
    page = Selector(HTML)
    items = page.css("#main")
    anchors = items.xpath(".//a")
    assert len(anchors) == 3


# --- ::attr(name) 伪元素支持（Scrapling 风格） -----------------------------


def test_css_attr_pseudo_returns_attribute_values() -> None:
    """css('sel::attr(name)') 返回匹配元素的属性值列表，而非元素列表。"""
    page = Selector(HTML)
    ids = page.css(".product::attr(id)")
    assert list(ids) == ["p1", "p2"]
    # 返回值是 TextHandler（str 子类），可直接当字符串用
    assert ids[0] == "p1"
    assert isinstance(ids[0], str)


def test_css_attr_pseudo_first_returns_single_value() -> None:
    """css_first('sel::attr(name)') 返回单个属性值或 None。"""
    page = Selector(HTML)
    first_id = page.css_first(".product::attr(id)")
    assert first_id == "p1"


def test_css_attr_pseudo_missing_attribute_returns_empty_string() -> None:
    """属性不存在时返回空字符串（Scrapling 行为对齐）。"""
    page = Selector(HTML)
    # p2 没有 link class，但 .product 匹配的 p1/p2 都没有 data-x
    values = page.css(".product::attr(data-x)")
    assert list(values) == ["", ""]


def test_css_attr_pseudo_no_match_returns_empty_list() -> None:
    page = Selector(HTML)
    assert list(page.css(".nonexistent::attr(id)")) == []


def test_css_attr_pseudo_first_no_match_returns_default() -> None:
    page = Selector(HTML)
    assert page.css_first(".nonexistent::attr(id)") is None
    assert page.css_first(".nonexistent::attr(id)", "fallback") == "fallback"


def test_css_attr_pseudo_supports_quoted_name() -> None:
    """属性名可带单/双引号。"""
    page = Selector(HTML)
    assert list(page.css(".product::attr('id')")) == ["p1", "p2"]
    assert list(page.css('.product::attr("id")')) == ["p1", "p2"]


def test_xpath_attr_pseudo_returns_attribute_values() -> None:
    """xpath 也支持 ::attr(name) 伪元素（追加在 XPath 末尾）。"""
    page = Selector(HTML)
    # 注意：p1 的 class="product link"，用 contains 匹配
    ids = page.xpath("//a[contains(@class,'product')]::attr(id)")
    assert list(ids) == ["p1", "p2"]


def test_result_list_batch_css_attr_pseudo() -> None:
    """ResultList.css 批量操作也支持 ::attr 语法。"""
    page = Selector(HTML)
    # 先选容器 #main，再批量对内部 a 元素取 href
    items = page.css("#main")
    ids = items.css("a::attr(id)")
    assert list(ids) == ["p1", "p2", "ext"]


def test_css_attr_pseudo_class_with_space() -> None:
    """复合选择器（含 class 组合）配合 ::attr。"""
    page = Selector(HTML)
    # p1 同时有 product 和 link 两个 class
    val = page.css_first(".product.link::attr(id)")
    assert val == "p1"

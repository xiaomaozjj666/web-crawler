"""Tests for Selector DOM traversal: siblings / next / previous / path."""

from __future__ import annotations

from web_crawler import Selector

HTML = """
<html><body>
  <ul id="list">
    <li class="item" id="a">A</li>
    <li class="item" id="b">B</li>
    <li class="item" id="c">C</li>
    <li class="item" id="d">D</li>
  </ul>
  <p id="after">tail</p>
</body></html>
"""


def test_siblings_excludes_self() -> None:
    """siblings 排除自身。"""
    page = Selector(HTML)
    b = page.css_first("#b")
    sibs = b.siblings
    ids = [s.attr("id") for s in sibs]
    assert "b" not in ids
    assert "a" in ids
    assert "c" in ids
    assert "d" in ids


def test_siblings_empty_for_root() -> None:
    """根节点 siblings 为空。"""
    page = Selector("<div id='root'/>")
    root = page.css_first("#root")
    # Root has no parent → no siblings
    assert len(root.siblings) == 0


def test_next_sibling() -> None:
    """next_sibling 返回下一个兄弟节点。"""
    page = Selector(HTML)
    b = page.css_first("#b")
    nxt = b.next
    assert nxt is not None
    assert nxt.attr("id") == "c"


def test_next_none_for_last() -> None:
    """末节点的 next_sibling 为 None。"""
    page = Selector(HTML)
    d = page.css_first("#d")
    assert d.next is None


def test_previous_sibling() -> None:
    """previous_sibling 返回前一个兄弟节点。"""
    page = Selector(HTML)
    b = page.css_first("#b")
    prv = b.previous
    assert prv is not None
    assert prv.attr("id") == "a"


def test_previous_none_for_first() -> None:
    """首节点的 previous_sibling 为 None。"""
    page = Selector(HTML)
    a = page.css_first("#a")
    assert a.previous is None


def test_path_root_to_self() -> None:
    """path 从根到自身。"""
    page = Selector(HTML)
    b = page.css_first("#b")
    chain = [s.tag for s in b.path]
    # From root html down to li#b
    assert chain[0] == "html"
    assert chain[-1] == "li"
    assert "body" in chain
    assert "ul" in chain


def test_path_includes_self() -> None:
    """path 包含自身。"""
    page = Selector(HTML)
    ul = page.css_first("#list")
    chain = list(ul.path)
    assert chain[-1] is ul or chain[-1].attr("id") == "list"

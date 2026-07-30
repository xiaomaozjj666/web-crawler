"""Tests for the Response object."""

from __future__ import annotations

from web_crawler import Response


def test_response_text_decodes_content() -> None:
    r = Response("https://x.example", 200, b"hello world")
    assert r.text == "hello world"


def test_response_text_handles_encoding() -> None:
    r = Response("https://x.example", 200, "café".encode("latin-1"), encoding="latin-1")
    assert r.text == "café"


def test_response_ok_status() -> None:
    assert Response("u", 200, b"").ok is True
    assert Response("u", 301, b"").ok is True
    assert Response("u", 404, b"").ok is False
    assert Response("u", 500, b"").ok is False


def test_response_json() -> None:
    r = Response("u", 200, b'{"k": 42}')
    assert r.json() == {"k": 42}


def test_response_css_and_xpath_delegate_to_selector() -> None:
    r = Response("https://shop.example.com", 200, b"<div><a id='x'>Hi</a></div>")
    assert str(r.css_first("#x").text) == "Hi"
    assert str(r.xpath_first("//a").text) == "Hi"


def test_response_xpath_returns_list() -> None:
    """xpath() 方法应返回 ResultList（非 xpath_first）。"""
    r = Response("https://shop.example.com", 200, b"<div><a id='x'>Hi</a><a id='y'>Bye</a></div>")
    results = r.xpath("//a")
    assert len(results) == 2


def test_response_selector_is_lazy_and_cached() -> None:
    r = Response("u", 200, b"<p>x</p>")
    assert r._selector is None
    _ = r.selector
    assert r._selector is not None
    # Same instance returned on second access
    assert r.selector is r._selector


def test_response_urljoin() -> None:
    r = Response("https://example.com/a/b/", 200, b"")
    assert r.urljoin("c") == "https://example.com/a/b/c"
    assert r.urljoin("/d") == "https://example.com/d"
    assert r.urljoin("https://other.example/e") == "https://other.example/e"


def test_response_meta_is_dict() -> None:
    r = Response("u", 200, b"")
    assert isinstance(r.meta, dict)
    r.meta["key"] = "value"
    assert r.meta["key"] == "value"


def test_response_repr() -> None:
    assert repr(Response("https://x", 200, b"")) == "<Response 200 https://x>"


def test_response_headers_default_empty() -> None:
    r = Response("u", 200, b"")
    assert r.headers == {}
    assert r.request_headers == {}


def test_response_adaptive_propagated_to_selector() -> None:
    r = Response("https://shop.example.com", 200, b"<a id='p'>x</a>", adaptive=True)
    assert r.selector.adaptive is True

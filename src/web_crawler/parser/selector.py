"""Scrapling-style :class:`Selector` — a high-performance element selector.

Built on ``lxml`` for fast CSS/XPath evaluation, with an adaptive engine that
relocates elements by structural similarity when a website's markup changes.

Example
-------
>>> from web_crawler import Selector
>>> page = Selector('<div><a id="p1" class="product">Product 1</a></div>',
...                  url="https://shop.example.com", adaptive=True)
>>> el = page.css_first("#p1", auto_save=True)
>>> el.text
'Product 1'
>>> # Later, even after markup changes:
>>> page2 = Selector('<div><a data-id="p1" class="product new">Product 1</a></div>',
...                  url="https://shop.example.com", adaptive=True)
>>> page2.css_first("#p1", adaptive=True).text  # relocated by similarity
'Product 1'
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, Pattern  # noqa: UP035
from urllib.parse import urlparse

from lxml import etree
from lxml import html as lxml_html

from .._types import Attrs, ResultList, TextHandler
from .adaptive import (
    AdaptiveStorage,
    best_match,
    compute_fingerprint,
    similarity_score,
)
from .storage import DEFAULT_DB_PATH

# 匹配 CSS 选择器末尾的 Scrapling 风格伪元素 ::attr(name)，
# 属性名可带可选的单/双引号。例：".product::attr(href)"、'a::attr("data-id")'
_ATTR_PSEUDO_RE = re.compile(r"::attr\(\s*['\"]?([^'\"\)\s]+)['\"]?\s*\)\s*$")


def _split_attr_pseudo(selector: str) -> tuple[str, str | None]:
    """拆分选择器末尾的 ``::attr(name)`` 伪元素。

    返回 ``(纯CSS选择器, 属性名或None)``。若无伪元素后缀，属性名为 ``None``。
    """
    m = _ATTR_PSEUDO_RE.search(selector)
    if m:
        return selector[: m.start()].rstrip(), m.group(1)
    return selector, None


# Module-level default storage (lazily created so importing the module is cheap).
_default_storage: AdaptiveStorage | None = None


def _get_default_storage() -> AdaptiveStorage:
    global _default_storage
    if _default_storage is None:
        _default_storage = AdaptiveStorage(DEFAULT_DB_PATH)
    return _default_storage


def _domain_from_url(url: str | None) -> str:
    if not url:
        return "local"
    parsed = urlparse(url)
    return parsed.netloc.lower() or "local"


class Adaptors:
    """Facade over adaptive storage for a single domain."""

    def __init__(
        self,
        domain: str,
        storage: AdaptiveStorage | None = None,
    ) -> None:
        self.domain = domain
        self.storage = storage or _get_default_storage()

    def save(self, identifier: str, element: etree._Element, url: str = "") -> str:
        fingerprint = compute_fingerprint(element)
        self.storage.save(
            domain=self.domain,
            identifier=identifier,
            fingerprint=fingerprint,
            tag=str(element.tag) if isinstance(element.tag, str) else "",
            text="".join(element.itertext())[:500],
            url=url,
        )
        return fingerprint

    def find_adaptive(
        self,
        identifier: str,
        candidates: list[etree._Element],
        threshold: float = 0.5,
    ) -> tuple[etree._Element | None, float]:
        record = self.storage.load(self.domain, identifier)
        if not record:
            return None, 0.0
        return best_match(candidates, record["fingerprint"], threshold)

    def find_similar(
        self,
        reference: etree._Element,
        candidates: list[etree._Element],
        threshold: float = 0.5,
        limit: int = 10,
    ) -> list[tuple[etree._Element, float]]:
        ref_fp = compute_fingerprint(reference)
        scored: list[tuple[etree._Element, float]] = []
        for cand in candidates:
            if cand is reference:
                continue
            score = similarity_score(ref_fp, compute_fingerprint(cand))
            if score >= threshold:
                scored.append((cand, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]


class Selector:
    """A Scrapling-style selector wrapping an lxml element tree."""

    def __init__(
        self,
        page_source: str | bytes | etree._Element,
        url: str | None = None,
        *,
        adaptive: bool = False,
        adaptive_domain: str | None = None,
        storage: AdaptiveStorage | None = None,
        parser: str = "html",
    ) -> None:
        self.url = url
        self.adaptive = adaptive
        self._domain = adaptive_domain or _domain_from_url(url)
        self._adaptors = Adaptors(self._domain, storage) if adaptive else None
        self._element = self._parse(page_source, parser)
        self._storage = storage
        # 保留 adaptive_domain 以便 _wrap 构造子 Selector 时保持同一域名
        self._adaptive_domain = adaptive_domain

    # -- parsing -----------------------------------------------------------
    @staticmethod
    def _parse(source: str | bytes | etree._Element, parser: str) -> etree._Element:
        if isinstance(source, etree._Element):
            return source
        if isinstance(source, str):
            data = source.encode("utf-8", errors="replace")
        else:
            data = source
        if parser == "xml":
            return etree.fromstring(data)
        # lxml.html.fromstring handles fragments and full documents robustly.
        return lxml_html.fromstring(data)

    # -- basic properties --------------------------------------------------
    @property
    def element(self) -> etree._Element:
        return self._element

    @property
    def tag(self) -> str:
        return str(self._element.tag) if isinstance(self._element.tag, str) else ""

    @property
    def text(self) -> TextHandler:
        return TextHandler("".join(self._element.itertext()))

    @property
    def html(self) -> str:
        return etree.tostring(self._element, encoding="unicode", method="html")

    @property
    def attrib(self) -> Attrs:
        return Attrs(self._element.attrib)

    def attr(self, name: str, default: Any = None) -> Any:
        return self._element.get(name, default)

    @property
    def parent(self) -> Selector | None:
        p = self._element.getparent()
        return self._wrap(p) if p is not None else None

    @property
    def children(self) -> ResultList[Selector]:
        return ResultList(self._wrap(c) for c in self._element if isinstance(c.tag, str))

    # -- DOM traversal (Scrapling parity) ----------------------------------
    @property
    def siblings(self) -> ResultList[Selector]:
        """Sibling elements sharing the same parent (excluding self)."""
        parent = self._element.getparent()
        if parent is None:
            return ResultList()
        return ResultList(
            self._wrap(c) for c in parent if isinstance(c.tag, str) and c is not self._element
        )

    @property
    def next(self) -> Selector | None:
        """The next sibling element, or ``None``."""
        nxt = self._element.getnext()
        return self._wrap(nxt) if nxt is not None and isinstance(nxt.tag, str) else None

    @property
    def previous(self) -> Selector | None:
        """The previous sibling element, or ``None``."""
        prv = self._element.getprevious()
        return self._wrap(prv) if prv is not None and isinstance(prv.tag, str) else None

    @property
    def path(self) -> ResultList[Selector]:
        """The chain of ancestors from the document root down to this element."""
        chain: list[etree._Element] = []
        node: etree._Element | None = self._element
        while node is not None and isinstance(node.tag, str):
            chain.append(node)
            node = node.getparent()
        chain.reverse()
        return ResultList(self._wrap(c) for c in chain)

    # -- CSS / XPath -------------------------------------------------------
    def css(
        self,
        selector: str,
        *,
        auto_save: bool = False,
        adaptive: bool = False,
        threshold: float = 0.5,
    ) -> ResultList[Any]:
        """Select elements by CSS selector, with optional adaptive fallback.

        支持 Scrapling 风格的 ``::attr(name)`` 伪元素：若选择器以
        ``::attr(name)`` 结尾，则返回匹配元素的属性值列表
        （``ResultList[TextHandler]``，属性缺失时为空字符串），
        否则返回 ``ResultList[Selector]``。
        """
        pure_selector, attr_name = _split_attr_pseudo(selector)
        results = self._css_raw(pure_selector)
        if results:
            if auto_save and self._adaptors:
                self._adaptors.save(pure_selector, results[0], url=self.url or "")
            wrapped = [self._wrap(c) for c in results]
            if attr_name is not None:
                return ResultList(TextHandler(w.attr(attr_name) or "") for w in wrapped)
            return ResultList(wrapped)

        if adaptive and self._adaptors:
            relocated = self._adaptive_lookup(pure_selector, threshold)
            if relocated is not None:
                rel_sel = self._wrap(relocated)
                if attr_name is not None:
                    return ResultList([TextHandler(rel_sel.attr(attr_name) or "")])
                return ResultList([rel_sel])
        return ResultList()

    def css_first(
        self,
        selector: str,
        default: Any = None,
        *,
        auto_save: bool = False,
        adaptive: bool = False,
        threshold: float = 0.5,
    ) -> Any:
        """First match, or ``default``.

        若选择器带 ``::attr(name)``，返回属性值（``TextHandler``），否则返回
        ``Selector``。
        """
        result = self.css(selector, auto_save=auto_save, adaptive=adaptive, threshold=threshold)
        return result.first if result.first is not None else default

    def xpath(
        self,
        selector: str,
        *,
        auto_save: bool = False,
        adaptive: bool = False,
        threshold: float = 0.5,
    ) -> ResultList[Any]:
        """Select elements by XPath, with optional adaptive fallback.

        支持 Scrapling 风格的 ``::attr(name)`` 伪元素（追加在 XPath 末尾）。
        若未使用伪元素，建议直接用原生 XPath ``@attr`` 语法。
        """
        pure_selector, attr_name = _split_attr_pseudo(selector)
        results = self._element.xpath(pure_selector)
        # lxml 的 xpath 对 @attr 表达式会直接返回字符串而非元素
        wrapped = [self._wrap(r) for r in results if isinstance(r, etree._Element)]
        if wrapped:
            if auto_save and self._adaptors:
                self._adaptors.save(pure_selector, wrapped[0].element, url=self.url or "")
            if attr_name is not None:
                return ResultList(TextHandler(w.attr(attr_name) or "") for w in wrapped)
            return ResultList(wrapped)
        if adaptive and self._adaptors:
            relocated = self._adaptive_lookup(pure_selector, threshold)
            if relocated is not None:
                rel_sel = self._wrap(relocated)
                if attr_name is not None:
                    return ResultList([TextHandler(rel_sel.attr(attr_name) or "")])
                return ResultList([rel_sel])
        return ResultList()

    def xpath_first(
        self,
        selector: str,
        default: Any = None,
        *,
        auto_save: bool = False,
        adaptive: bool = False,
        threshold: float = 0.5,
    ) -> Any:
        """First match, or ``default``. 支持 ``::attr(name)`` 伪元素。"""
        result = self.xpath(selector, auto_save=auto_save, adaptive=adaptive, threshold=threshold)
        return result.first if result.first is not None else default

    # -- text / similarity search -----------------------------------------
    def find_by_text(
        self,
        text: str,
        *,
        exact: bool = False,
        case_sensitive: bool = False,
    ) -> ResultList[Selector]:
        """Find elements whose direct text matches ``text``."""
        needle = text if case_sensitive else text.lower()
        matches: list[etree._Element] = []
        for el in self._element.iter():
            if not isinstance(el.tag, str):
                continue
            direct = el.text or ""
            hay = direct if case_sensitive else direct.lower()
            if (exact and hay.strip() == needle) or (not exact and needle in hay):
                matches.append(el)
        return ResultList(self._wrap(m) for m in matches)

    def find_by_regex(
        self,
        query: str | Pattern[str],
        *,
        case_sensitive: bool = True,
    ) -> ResultList[Selector]:
        """Find elements whose direct text matches the regex ``query``.

        Mirrors Scrapling's ``find_by_regex``: scans every element's direct
        text content against a compiled or string pattern.
        """
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(query, flags) if isinstance(query, str) else query
        matches: list[etree._Element] = []
        for el in self._element.iter():
            if not isinstance(el.tag, str):
                continue
            direct = el.text or ""
            if pattern.search(direct):
                matches.append(el)
        return ResultList(self._wrap(m) for m in matches)

    def find_similar(
        self,
        reference: Selector | etree._Element,
        *,
        threshold: float = 0.5,
        limit: int = 10,
    ) -> ResultList[Selector]:
        """Find elements structurally similar to ``reference``."""
        ref_el = reference.element if isinstance(reference, Selector) else reference
        if self._adaptors:
            scored = self._adaptors.find_similar(
                ref_el, list(self._element.iter()), threshold, limit
            )
        else:
            # Stateless fallback when adaptive mode is off.
            ref_fp = compute_fingerprint(ref_el)
            scored = []
            for cand in self._element.iter():
                if cand is ref_el or not isinstance(cand.tag, str):
                    continue
                score = similarity_score(ref_fp, compute_fingerprint(cand))
                if score >= threshold:
                    scored.append((cand, score))
            scored.sort(key=lambda item: item[1], reverse=True)
            scored = scored[:limit]
        return ResultList(self._wrap(el) for el, _ in scored)

    # -- regex extraction (Scrapling parity) ------------------------------
    def re(self, regex: str | Pattern[str], *, clean_match: bool = False) -> list[str]:
        """Return all regex matches from this element's text content.

        Mirrors Scrapling's ``Adaptor.re``: searches the element's full text
        and returns a list of matched strings (or groups if the pattern has
        capturing groups).
        """
        text = str(self.text)
        if clean_match:
            text = " ".join(text.split())
        pattern = re.compile(regex) if isinstance(regex, str) else regex
        results: list[str] = []
        for match in pattern.finditer(text):
            if match.groups():
                # With capturing groups, return the group tuple joined-like.
                results.extend(g for g in match.groups() if g is not None)
            else:
                results.append(match.group(0))
        return results

    def re_first(
        self, regex: str | Pattern[str], default: str | None = None, *, clean_match: bool = False
    ) -> str | None:
        """Return the first regex match from this element's text, or ``default``."""
        matches = self.re(regex, clean_match=clean_match)
        return matches[0] if matches else default

    # -- serialization / text helpers (Scrapling parity) ------------------
    def get_all_text(
        self,
        *,
        separator: str = " ",
        strip: bool = False,
        ignore_tags: tuple[str, ...] = ("script", "style"),
    ) -> TextHandler:
        """Return the concatenated text of all descendant elements.

        Mirrors Scrapling's ``get_all_text``: walks the subtree, skipping
        ``script``/``style`` by default, joining text with ``separator``.
        """
        parts: list[str] = []
        for el in self._element.iter():
            if not isinstance(el.tag, str) or el.tag in ignore_tags:
                continue
            direct = el.text or ""
            tail = el.tail or ""
            if direct:
                parts.append(direct.strip() if strip else direct)
            if tail:
                parts.append(tail.strip() if strip else tail)
        return TextHandler(separator.join(p for p in parts if p))

    def prettify(self) -> str:
        """Return a pretty-printed serialization of this element (Scrapling parity)."""
        return etree.tostring(self._element, encoding="unicode", pretty_print=True, method="html")

    # -- adaptive public API (Scrapling parity) ---------------------------
    def save(self, element: Selector | etree._Element, identifier: str) -> None:
        """Persist ``element``'s fingerprint under ``identifier``.

        Mirrors Scrapling's ``Selector.save``: lets callers explicitly store an
        element's structural fingerprint for later adaptive relocation, without
        relying on ``auto_save=True`` during selection.
        """
        if self._adaptors is None:
            raise RuntimeError(
                "save() requires adaptive=True; construct Selector(adaptive=True, storage=...)"
            )
        el = element.element if isinstance(element, Selector) else element
        self._adaptors.save(identifier, el, url=self.url or "")

    def retrieve(self, identifier: str) -> dict[str, Any] | None:
        """Return the stored fingerprint record for ``identifier``, or ``None``.

        Mirrors Scrapling's ``Selector.retrieve``: fetch the previously saved
        fingerprint (tag/text/fingerprint/url) for manual inspection or custom
        matching.
        """
        if self._adaptors is None:
            raise RuntimeError(
                "retrieve() requires adaptive=True; construct Selector(adaptive=True, storage=...)"
            )
        return self._adaptors.storage.load(self._domain, identifier)

    def relocate(
        self,
        element: dict[str, Any] | Selector | etree._Element,
        threshold: float = 0.5,
    ) -> ResultList[Selector]:
        """Relocate ``element`` in the current document by structural similarity.

        Mirrors Scrapling's ``Selector.relocate``: given a previously stored
        fingerprint (as a dict from :meth:`retrieve`, a :class:`Selector`, or a
        raw lxml element), find the best-matching element(s) in this document.

        Returns a :class:`ResultList` of relocated selectors (empty if no
        candidate exceeds ``threshold``).
        """
        if self._adaptors is None:
            raise RuntimeError(
                "relocate() requires adaptive=True; construct Selector(adaptive=True, storage=...)"
            )
        # Normalize the input to a fingerprint string.
        if isinstance(element, dict):
            stored_fp = element.get("fingerprint", "")
            if not stored_fp:
                return ResultList()
        elif isinstance(element, Selector):
            stored_fp = compute_fingerprint(element.element)
        else:
            stored_fp = compute_fingerprint(element)

        candidates = [el for el in self._element.iter() if isinstance(el.tag, str)]
        matched, _score = best_match(candidates, stored_fp, threshold)
        if matched is None:
            return ResultList()
        return ResultList([self._wrap(matched)])

    # -- internal helpers --------------------------------------------------
    def _css_raw(self, selector: str) -> list[etree._Element]:
        # lxml.html elements expose a native ``cssselect`` method (cssselect is
        # a hard dependency) — faster and simpler than translating to XPath.
        return list(self._element.cssselect(selector))

    def _wrap(self, element: etree._Element) -> Selector:
        return Selector(
            element,
            url=self.url,
            adaptive=self.adaptive,
            adaptive_domain=self._adaptive_domain,
            storage=self._storage,
        )

    def _adaptive_lookup(self, identifier: str, threshold: float) -> etree._Element | None:
        assert self._adaptors is not None
        candidates = [el for el in self._element.iter() if isinstance(el.tag, str)]
        element, _score = self._adaptors.find_adaptive(identifier, candidates, threshold)
        return element

    # -- conveniences ------------------------------------------------------
    def __repr__(self) -> str:
        return f"<Selector tag={self.tag!r} text={str(self.text)[:40]!r}>"

    def __iter__(self) -> Iterator[Selector]:
        return iter(self.children)

    def __bool__(self) -> bool:
        # A Selector is always truthy when it wraps an element. We must NOT
        # define ``__len__`` to return the child count, otherwise
        # ``if selector:`` would be False for leaf elements (e.g. ``<a>`` with
        # no child elements), which breaks the ``el if el else default`` idiom.
        return self._element is not None


__all__ = ["Adaptors", "Selector"]

# Scrapling 向后兼容别名：Adaptor 指向主选择器类 Selector
Adaptor = Selector

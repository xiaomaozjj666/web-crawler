"""Scrapling 风格的 :class:`Selector` — 高性能元素选择器。

基于 ``lxml`` 实现快速的 CSS/XPath 求值，并带自适应引擎：网站标记变化时
按结构相似度重新定位元素。

示例
----
>>> from web_crawler import Selector
>>> page = Selector('<div><a id="p1" class="product">Product 1</a></div>',
...                  url="https://shop.example.com", adaptive=True)
>>> el = page.css_first("#p1", auto_save=True)
>>> el.text
'Product 1'
>>> # 之后即使标记变了：
>>> page2 = Selector('<div><a data-id="p1" class="product new">Product 1</a></div>',
...                  url="https://shop.example.com", adaptive=True)
>>> page2.css_first("#p1", adaptive=True).text  # 按相似度重新定位
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


# 模块级默认存储（惰性创建，让模块导入保持轻量）。
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
    """面向单个域名的自适应存储门面。"""

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
        # 用存储记录中的 tag 预筛候选：避免对整篇文档逐元素计算指纹
        # （大文档下 O(N²)），tag 不匹配的元素不可能命中。
        tag = record.get("tag")
        if tag:
            candidates = [c for c in candidates if isinstance(c.tag, str) and c.tag == tag]
            if not candidates:
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
    """包装 lxml 元素树的 Scrapling 风格选择器。"""

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

    # -- 解析 -----------------------------------------------------------------
    @staticmethod
    def _parse(source: str | bytes | etree._Element, parser: str) -> etree._Element:
        if isinstance(source, etree._Element):
            return source
        if parser == "xml":
            return etree.fromstring(source)
        # lxml.html.fromstring 对 str 直接按 Unicode 处理（内部编码为 UTF-8）；
        # 对 bytes 按 HTML 规范的 meta charset 判定编码（无声明时默认 latin-1，
        # 与浏览器一致）。注意：不要先把 str 预编码为 UTF-8 bytes 再传入——
        # 无 meta charset 时 libxml2 会把 UTF-8 中文按 latin-1 误解码成乱码。
        return lxml_html.fromstring(source)

    # -- 基础属性 -------------------------------------------------------------
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

    # -- DOM 遍历（对齐 Scrapling） -------------------------------------------
    @property
    def siblings(self) -> ResultList[Selector]:
        """与自身同父的兄弟元素（不含自身）。"""
        parent = self._element.getparent()
        if parent is None:
            return ResultList()
        return ResultList(
            self._wrap(c) for c in parent if isinstance(c.tag, str) and c is not self._element
        )

    @property
    def next(self) -> Selector | None:
        """下一个兄弟元素，没有时为 ``None``。"""
        nxt = self._element.getnext()
        return self._wrap(nxt) if nxt is not None and isinstance(nxt.tag, str) else None

    @property
    def previous(self) -> Selector | None:
        """上一个兄弟元素，没有时为 ``None``。"""
        prv = self._element.getprevious()
        return self._wrap(prv) if prv is not None and isinstance(prv.tag, str) else None

    @property
    def path(self) -> ResultList[Selector]:
        """从文档根到本元素的祖先链。"""
        chain: list[etree._Element] = []
        node: etree._Element | None = self._element
        while node is not None and isinstance(node.tag, str):
            chain.append(node)
            node = node.getparent()
        chain.reverse()
        return ResultList(self._wrap(c) for c in chain)

    # -- CSS / XPath ---------------------------------------------------------
    def css(
        self,
        selector: str,
        *,
        auto_save: bool = False,
        adaptive: bool = False,
        threshold: float = 0.5,
    ) -> ResultList[Any]:
        """按 CSS 选择器选取元素，可选自适应兜底。

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
        """首个匹配，无匹配时返回 ``default``。

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
        """按 XPath 选取元素，可选自适应兜底。

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
        """首个匹配，无匹配时返回 ``default``。支持 ``::attr(name)`` 伪元素。"""
        result = self.xpath(selector, auto_save=auto_save, adaptive=adaptive, threshold=threshold)
        return result.first if result.first is not None else default

    # -- 文本 / 相似度搜索 ----------------------------------------------------
    def find_by_text(
        self,
        text: str,
        *,
        exact: bool = False,
        case_sensitive: bool = False,
    ) -> ResultList[Selector]:
        """查找直接文本匹配 ``text`` 的元素。"""
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
        """查找直接文本匹配正则 ``query`` 的元素。

        对齐 Scrapling 的 ``find_by_regex``：用编译好的或字符串形式的模式
        扫描每个元素的直接文本。
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
        """查找与 ``reference`` 结构相似的元素。"""
        ref_el = reference.element if isinstance(reference, Selector) else reference
        if self._adaptors:
            scored = self._adaptors.find_similar(
                ref_el, list(self._element.iter()), threshold, limit
            )
        else:
            # 自适应模式关闭时的无状态兜底。
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

    # -- 正则提取（对齐 Scrapling） --------------------------------------------
    def re(self, regex: str | Pattern[str], *, clean_match: bool = False) -> list[str]:
        """返回本元素文本内容的全部正则匹配。

        对齐 Scrapling 的 ``Adaptor.re``：搜索元素的完整文本并返回匹配字符
        串列表（模式含捕获组时返回各组内容）。
        """
        text = str(self.text)
        if clean_match:
            text = " ".join(text.split())
        pattern = re.compile(regex) if isinstance(regex, str) else regex
        results: list[str] = []
        for match in pattern.finditer(text):
            if match.groups():
                # 有捕获组时，返回各捕获组内容。
                results.extend(g for g in match.groups() if g is not None)
            else:
                results.append(match.group(0))
        return results

    def re_first(
        self, regex: str | Pattern[str], default: str | None = None, *, clean_match: bool = False
    ) -> str | None:
        """返回本元素文本的首个正则匹配，无匹配时返回 ``default``。"""
        matches = self.re(regex, clean_match=clean_match)
        return matches[0] if matches else default

    # -- 序列化 / 文本辅助（对齐 Scrapling） -----------------------------------
    def get_all_text(
        self,
        *,
        separator: str = " ",
        strip: bool = False,
        ignore_tags: tuple[str, ...] = ("script", "style"),
    ) -> TextHandler:
        """返回所有后代元素文本的拼接结果。

        对齐 Scrapling 的 ``get_all_text``：遍历子树，默认跳过
        ``script``/``style``，用 ``separator`` 连接文本。
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
        """返回本元素格式化美化后的序列化结果（对齐 Scrapling）。"""
        return etree.tostring(self._element, encoding="unicode", pretty_print=True, method="html")

    # -- 自适应公开 API（对齐 Scrapling） --------------------------------------
    def save(self, element: Selector | etree._Element, identifier: str) -> None:
        """把 ``element`` 的指纹以 ``identifier`` 持久化。

        对齐 Scrapling 的 ``Selector.save``：让调用者显式存储元素的结构
        指纹，供之后自适应重定位使用，而不必依赖选择时的 ``auto_save=True``。
        """
        if self._adaptors is None:
            raise RuntimeError(
                "save() requires adaptive=True; construct Selector(adaptive=True, storage=...)"
            )
        el = element.element if isinstance(element, Selector) else element
        self._adaptors.save(identifier, el, url=self.url or "")

    def retrieve(self, identifier: str) -> dict[str, Any] | None:
        """返回 ``identifier`` 存储的指纹记录，没有时为 ``None``。

        对齐 Scrapling 的 ``Selector.retrieve``：取出先前保存的指纹
        （tag/text/fingerprint/url），供人工检查或自定义匹配。
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
        """按结构相似度在当前文档中重新定位 ``element``。

        对齐 Scrapling 的 ``Selector.relocate``：给定先前存储的指纹
        （:meth:`retrieve` 返回的 dict、:class:`Selector` 或原始 lxml 元素），
        在本文档中找出最匹配的元素。

        返回重定位选择器构成的 :class:`ResultList`（无候选超过 ``threshold``
        时为空）。
        """
        if self._adaptors is None:
            raise RuntimeError(
                "relocate() requires adaptive=True; construct Selector(adaptive=True, storage=...)"
            )
        # 把输入归一化为指纹字符串。
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

    # -- 内部辅助 -------------------------------------------------------------
    def _css_raw(self, selector: str) -> list[etree._Element]:
        # lxml.html 元素自带原生 ``cssselect`` 方法（cssselect 是硬依赖）——
        # 比翻译成 XPath 更快也更简单。
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

    # -- 便捷方法 -------------------------------------------------------------
    def __repr__(self) -> str:
        return f"<Selector tag={self.tag!r} text={str(self.text)[:40]!r}>"

    def __iter__(self) -> Iterator[Selector]:
        return iter(self.children)

    def __bool__(self) -> bool:
        # 包装了元素的 Selector 恒为真。绝不能定义 ``__len__`` 返回子元素
        # 数，否则 ``if selector:`` 对叶子元素（如无子元素的 ``<a>``）会是
        # False，破坏 ``el if el else default`` 惯用法。
        return self._element is not None


__all__ = ["Adaptors", "Selector"]

# Scrapling 向后兼容别名：Adaptor 指向主选择器类 Selector
Adaptor = Selector

"""Custom supporting types shared across the library.

These small helpers mirror Scrapling's public type surface so that selectors
and fetchers return rich, consistent objects instead of bare strings.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Any, Generic, TypeVar, cast

T = TypeVar("T")


class TextHandler(str):
    """A string subclass with chainable text helpers (Scrapling-style)."""

    __slots__ = ()

    def clean(self) -> TextHandler:
        """Collapse whitespace and return a new handler."""
        return TextHandler(" ".join(str(self).split()))

    def trim(self, chars: str | None = None) -> TextHandler:
        return TextHandler(str(self).strip(chars))

    def remove(self, substr: str) -> TextHandler:
        return TextHandler(str(self).replace(substr, ""))

    def starts_with(self, prefix: str) -> bool:
        return str(self).startswith(prefix)

    def ends_with(self, suffix: str) -> bool:
        return str(self).endswith(suffix)


class Attrs(dict):
    """A dict subclass for element attributes with case-insensitive lookups."""

    __slots__ = ()

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default

    def __contains__(self, key: object) -> bool:  # type: ignore[override]
        if not isinstance(key, str):
            return False
        lowered = key.lower()
        return any(k.lower() == lowered for k in super().keys())


class ResultList(list, Generic[T]):
    """A list subclass with Scrapling-style helper methods on selection results.

    Mirrors Scrapling's ``Selectors`` container: batch operations over a list
    of selectors with attribute-style ``.first`` / ``.last`` access, ``.get()``
    / ``.getall()`` extraction, and slice-safe typing (slices return a
    ``ResultList`` rather than a plain ``list``).
    """

    __slots__ = ()

    # -- first / last (Scrapling exposes these as properties) --------------
    @property
    def first(self) -> T | None:  # type: ignore[override]
        """The first element, or ``None`` if empty (Scrapling-style property)."""
        return self[0] if self else None

    @property
    def last(self) -> T | None:  # type: ignore[override]
        """The last element, or ``None`` if empty (Scrapling-style property)."""
        return self[-1] if self else None

    # -- Scrapling-style extraction ----------------------------------------
    def get(self, default: Any = None) -> Any:
        """Return the text of the first element, or ``default`` if empty."""
        if not self:
            return default
        first = self[0]
        text = getattr(first, "text", None)
        return text if text is not None else first

    def getall(self) -> list[Any]:
        """Return the text of every element as a list."""
        out: list[Any] = []
        for item in self:
            text = getattr(item, "text", None)
            out.append(text if text is not None else item)
        return out

    # -- batch text helpers ------------------------------------------------
    @property
    def text(self) -> TextHandler:
        """Concatenated text of all matched elements."""
        parts: list[str] = []
        for item in self:
            text = getattr(item, "text", None)
            if text is None:
                text = str(item)
            parts.append(str(text))
        return TextHandler(" ".join(p for p in parts if p))

    @property
    def texts(self) -> list[TextHandler]:
        out: list[TextHandler] = []
        for item in self:
            text = getattr(item, "text", None)
            out.append(TextHandler(str(text) if text is not None else ""))
        return out

    def get_all_texts(self) -> list[str]:
        return [str(t) for t in self.texts]

    def attr(self, name: str, default: Any = None) -> list[Any]:
        """对每个元素调用 ``.attr(name, default)``，返回属性值列表。

        没有 ``attr`` 方法的元素（如纯字符串）直接返回 ``default``。
        """
        results: list[Any] = []
        for item in self:
            attr_method = getattr(item, "attr", None)
            if callable(attr_method):
                results.append(attr_method(name, default))
            else:
                results.append(default)
        return results

    # -- batch selector operations (Scrapling Selectors parity) -----------
    def css(self, selector: str, **kwargs: Any) -> ResultList[Any]:
        """Apply ``.css(selector)`` to every element and flatten results."""
        out: ResultList[Any] = ResultList()
        for item in self:
            method = getattr(item, "css", None)
            if method is not None:
                out.extend(method(selector, **kwargs))
        return out

    def xpath(self, selector: str, **kwargs: Any) -> ResultList[Any]:
        """Apply ``.xpath(selector)`` to every element and flatten results."""
        out: ResultList[Any] = ResultList()
        for item in self:
            method = getattr(item, "xpath", None)
            if method is not None:
                out.extend(method(selector, **kwargs))
        return out

    @property
    def length(self) -> int:
        """Scrapling-style alias for ``len(self)``."""
        return len(self)

    # -- slice-safe typing -------------------------------------------------
    def __getitem__(self, index: int | slice) -> Any:  # type: ignore[override]
        if isinstance(index, slice):
            return ResultList(list.__getitem__(self, index))
        return list.__getitem__(self, index)


def ensure_list(value: Iterable[T] | T | None) -> list[T]:
    """Wrap a single value into a list; pass through iterables; ``None`` -> ``[]``."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, ResultList)):
        return list(value)
    # At this point ``value`` is a single item of type T (mypy can't narrow the
    # union that far, so help it with a cast).
    return [cast("T", value)]


def iter_chunks(seq: list[T], size: int) -> Iterator[list[T]]:
    """Yield successive ``size``-length chunks from ``seq``."""
    for start in range(0, len(seq), size):
        yield seq[start : start + size]


__all__ = [
    "TextHandler",
    "Attrs",
    "ResultList",
    "ensure_list",
    "iter_chunks",
    "Callable",
]

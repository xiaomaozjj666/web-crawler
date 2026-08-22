"""整个库共享的自定义辅助类型。

这些小工具对齐 Scrapling 的公开类型面，让选择器与 fetcher 返回丰富、
一致的对象而非裸字符串。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

T = TypeVar("T")


class TextHandler(str):
    """带链式文本处理方法的 str 子类（Scrapling 风格）。"""

    __slots__ = ()

    def clean(self) -> TextHandler:
        """折叠空白字符并返回新的 handler。"""
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
    """用于元素属性的 dict 子类，查找不区分大小写。"""

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
        return any(k.lower() == lowered for k in super().__iter__())


class ResultList(list, Generic[T]):
    """带 Scrapling 风格辅助方法的列表，用于选择结果。

    对齐 Scrapling 的 ``Selectors`` 容器：对选择器列表做批量操作，支持
    属性式的 ``.first`` / ``.last`` 访问、``.get()`` / ``.getall()`` 提取，
    且切片安全（切片返回 ``ResultList`` 而非普通 ``list``）。
    """

    __slots__ = ()

    # -- first / last（Scrapling 以 property 形式暴露） --------------------
    @property
    def first(self) -> T | None:  # type: ignore[override]
        """首个元素，为空时返回 ``None``（Scrapling 风格 property）。"""
        return self[0] if self else None

    @property
    def last(self) -> T | None:  # type: ignore[override]
        """最后一个元素，为空时返回 ``None``（Scrapling 风格 property）。"""
        return self[-1] if self else None

    # -- Scrapling 风格提取 --------------------------------------------------
    def get(self, default: Any = None) -> Any:
        """返回首个元素的文本，为空时返回 ``default``。"""
        if not self:
            return default
        first = self[0]
        text = getattr(first, "text", None)
        return text if text is not None else first

    def getall(self) -> list[Any]:
        """以列表返回每个元素的文本。"""
        out: list[Any] = []
        for item in self:
            text = getattr(item, "text", None)
            out.append(text if text is not None else item)
        return out

    # -- 批量文本辅助 --------------------------------------------------------
    @property
    def text(self) -> TextHandler:
        """所有匹配元素的文本拼接结果。"""
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

    # -- 批量选择器操作（对齐 Scrapling Selectors） --------------------------
    def css(self, selector: str, **kwargs: Any) -> ResultList[Any]:
        """对每个元素执行 ``.css(selector)`` 并展平结果。"""
        out: ResultList[Any] = ResultList()
        for item in self:
            method = getattr(item, "css", None)
            if method is not None:
                out.extend(method(selector, **kwargs))
        return out

    def xpath(self, selector: str, **kwargs: Any) -> ResultList[Any]:
        """对每个元素执行 ``.xpath(selector)`` 并展平结果。"""
        out: ResultList[Any] = ResultList()
        for item in self:
            method = getattr(item, "xpath", None)
            if method is not None:
                out.extend(method(selector, **kwargs))
        return out

    @property
    def length(self) -> int:
        """``len(self)`` 的 Scrapling 风格别名。"""
        return len(self)

    # -- Scrapling 风格导出 --------------------------------------------------
    @staticmethod
    def _coerce(item: Any) -> Any:
        """把元素转为 JSON 可序列化的形式。"""
        # dataclass / pydantic / 普通 dict / 自定义 as_dict()
        if hasattr(item, "as_dict") and callable(item.as_dict):
            return item.as_dict()
        if hasattr(item, "__dict__"):
            return {k: v for k, v in vars(item).items() if not k.startswith("_")}
        return item

    def to_json(self, path: str | Path | None = None, *, indent: int = 2) -> str:
        """将元素列表导出为 JSON 字符串，可选写入文件。

        >>> items.to_json("results.json")
        """
        data = [self._coerce(i) for i in self]
        text = json.dumps(data, ensure_ascii=False, indent=indent, default=str)
        if path is not None:
            Path(path).write_text(text, encoding="utf-8")
        return text

    def to_jsonl(self, path: str | Path | None = None) -> str:
        """将元素列表导出为 JSON Lines（每行一个 JSON 对象）。

        >>> items.to_jsonl("results.jsonl")
        """
        lines = [json.dumps(self._coerce(i), ensure_ascii=False, default=str) for i in self]
        text = "\n".join(lines)
        if path is not None:
            Path(path).write_text(text + "\n", encoding="utf-8")
        return text

    # -- 切片安全 -----------------------------------------------------------
    def __getitem__(self, index: int | slice) -> Any:  # type: ignore[override]
        if isinstance(index, slice):
            return ResultList(list.__getitem__(self, index))
        return list.__getitem__(self, index)


def ensure_list(value: Iterable[T] | T | None) -> list[T]:
    """把单个值包成列表；可迭代对象原样转列表；``None`` -> ``[]``。"""
    if value is None:
        return []
    # str/bytes 虽可迭代，但语义上是"单个值"；其余 Iterable（含生成器、
    # range、set、dict 等）一律透传为列表
    if isinstance(value, (str, bytes)):
        return [cast("T", value)]
    if isinstance(value, Iterable):
        return list(value)
    # 到这里 ``value`` 已是单个 T 类型的值（mypy 无法把联合类型收窄到这一步，
    # 用 cast 帮它一把）。
    return [cast("T", value)]


def iter_chunks(seq: list[T], size: int) -> Iterator[list[T]]:
    """从 ``seq`` 中依次产出长度为 ``size`` 的分块。"""
    for start in range(0, len(seq), size):
        yield seq[start : start + size]


__all__ = [
    "Attrs",
    "Callable",
    "ResultList",
    "TextHandler",
    "ensure_list",
    "iter_chunks",
]

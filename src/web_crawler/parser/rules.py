"""Portable field extraction from HTML without fetching or executing scripts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from cssselect import SelectorError
from lxml import etree

from web_crawler.exceptions import ConfigError

from .selector import Selector, _css_to_xpath, _split_attr_pseudo

if TYPE_CHECKING:
    from web_crawler.response import Response

MAX_RULES_BYTES = 128 * 1024
_MAX_CANONICAL_BYTES = 64 * 1024


class FieldResult(TypedDict):
    page_url: str
    item: dict[str, str | list[str] | None]
    missing_required: list[str]


@dataclass(frozen=True)
class _Field:
    name: str
    method: str
    selector: str
    many: bool
    required: bool
    query: etree.XPath = field(repr=False, compare=False)
    attribute: str | None = None


def parse_html(source: str) -> Selector:
    """Create a Selector from already decoded HTML or XHTML."""
    leading = source.lstrip("\ufeff \t\r\n")
    if leading.startswith("<?xml") and leading[5:6].isspace():
        _, terminator, content = leading.partition("?>")
        if terminator:
            source = content
    try:
        return Selector(source)
    except (etree.ParserError, ValueError):
        return Selector("<html></html>")


def _field_value(node: object, attribute: str | None) -> str:
    if isinstance(node, etree._Element):
        if not isinstance(node.tag, str):
            return ""
        text = node.get(attribute, "") if attribute else "".join(node.itertext())
    else:
        text = str(node)
    return " ".join(text.split())


class ExtractionRules:
    """Versioned CSS/XPath fields shared by the crawler UI, CLI and Spider."""

    def __init__(self, fields: tuple[_Field, ...]) -> None:
        self._fields = fields

    @classmethod
    def from_json(cls, text: str) -> ExtractionRules:
        if len(text.encode("utf-8")) > MAX_RULES_BYTES:
            raise ConfigError("字段规则文件最大为 128 KiB")
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"字段规则 JSON 无效（第 {exc.lineno} 行）") from exc

    @classmethod
    def from_file(cls, path: str | Path) -> ExtractionRules:
        with Path(path).open("rb") as source:
            raw = source.read(MAX_RULES_BYTES + 1)
        if len(raw) > MAX_RULES_BYTES:
            raise ConfigError("字段规则文件最大为 128 KiB")
        return cls.from_json(raw.decode("utf-8-sig"))

    @classmethod
    def from_dict(cls, rules: object) -> ExtractionRules:
        if not isinstance(rules, dict) or set(rules) != {"version", "fields"}:
            raise ConfigError("字段规则需要 version 和 fields")
        if type(rules["version"]) is not int or rules["version"] != 1:
            raise ConfigError("字段规则版本必须为 1")
        fields = rules["fields"]
        if not isinstance(fields, dict) or not 1 <= len(fields) <= 64:
            raise ConfigError("字段规则需要 1–64 个字段")
        compiled = cls(tuple(cls._parse_field(name, spec) for name, spec in fields.items()))
        canonical = json.dumps(compiled.to_dict(), ensure_ascii=False, separators=(",", ":"))
        if len(canonical.encode("utf-8")) > _MAX_CANONICAL_BYTES:
            raise ConfigError("字段规则内容最大为 64 KiB")
        return compiled

    @staticmethod
    def _parse_field(name: object, spec: object) -> _Field:
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ConfigError("字段名必须为 1–100 个字符")
        if not isinstance(spec, dict) or set(spec) - {"css", "xpath", "many", "required"}:
            raise ConfigError(f"字段 {name!r} 包含未知选项")
        methods = [method for method in ("css", "xpath") if method in spec]
        if len(methods) != 1:
            raise ConfigError(f"字段 {name!r} 需要一个 CSS 或 XPath 选择器")
        method = methods[0]
        expression = spec[method]
        if not isinstance(expression, str) or not expression.strip() or len(expression) > 2048:
            raise ConfigError(f"字段 {name!r} 的选择器长度必须为 1–2048 个字符")
        many, required = spec.get("many", False), spec.get("required", False)
        if not (isinstance(many, bool) and isinstance(required, bool)):
            raise ConfigError(f"字段 {name!r} 的 many 和 required 必须为布尔值")
        expression = expression.strip()
        attribute = None
        try:
            query_expression = expression
            if method == "css":
                css, attribute = _split_attr_pseudo(expression)
                query_expression = _css_to_xpath(css)
            query = etree.XPath(query_expression, smart_strings=False, regexp=False)
            query(etree.Element("html"))
        except (etree.XPathError, SelectorError, ValueError) as exc:
            raise ConfigError(f"字段 {name!r} 的 {method.upper()} 无效：{exc}") from exc
        return _Field(name, method, expression, many, required, query, attribute)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "fields": {
                rule.name: {
                    rule.method: rule.selector,
                    **({"many": True} if rule.many else {}),
                    **({"required": True} if rule.required else {}),
                }
                for rule in self._fields
            },
        }

    def extract(self, source: str | Selector, *, page_url: str = "") -> FieldResult:
        selector = parse_html(source) if isinstance(source, str) else source
        item: dict[str, str | list[str] | None] = {}
        missing = []
        for rule in self._fields:
            try:
                nodes = rule.query(selector.element)
                if not isinstance(nodes, list):
                    nodes = [nodes]
                values = (_field_value(node, rule.attribute) for node in nodes)
                matches = (value for value in values if value)
                value = list(matches) if rule.many else next(matches, None)
            except (etree.XPathError, ValueError) as exc:
                raise ConfigError(f"字段 {rule.name!r} 无法在此页执行：{exc}") from exc
            item[rule.name] = value
            if rule.required and not value:
                missing.append(rule.name)
        return {
            "page_url": page_url or selector.url or "",
            "item": item,
            "missing_required": missing,
        }

    def extract_response(self, response: Response) -> FieldResult:
        """Apply rules to the decoded page returned to a Spider callback."""
        return self.extract(response.text, page_url=response.url)

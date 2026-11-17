"""Check the static references reachable from an exported offline index.

Scope: HTML/CSS 静态引用、meta 资源（og:image 等）、link 预取、iframe/srcdoc、
SVG 引用与 meta refresh。**不覆盖**表单提交、脚本发起的请求、动态渲染后的
视觉结果——这些只能靠浏览器实测。

分类口径：`missing`（本地目标不存在）、`outside`（明确越出归档根，如盘符 /
`file:` / 含冒号路径）、`unverified`（目录引用、站点根路径、非 UTF-8、超限或
解析失败等无法静态确证的情况）、`remote`（指向原站的 HTTP(S) 资源）。
"""

from __future__ import annotations

import os
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, Literal, TypedDict
from urllib.parse import unquote, urlsplit

from lxml import etree
from tinycss2 import parse_component_value_list, parse_stylesheet, parse_stylesheet_bytes

from web_crawler._nav_guard import _REFRESH_UNPARSEABLE, _refresh_target
from web_crawler.app._offline import OFFLINE_INDEX
from web_crawler.app.crawler_net import (
    HTML_RESOURCE_ATTRS,
    META_URL_NAMES,
    PAGE_LINK_ATTRS,
    RESOURCE_LINK_RELS,
    _srcset_url_spans,
)
from web_crawler.parser.rules import parse_html

_DOCUMENT_BYTES = 8 * 1024 * 1024
_TOTAL_DOCUMENT_BYTES = 64 * 1024 * 1024
_REFERENCES = 100_000
_EXAMPLES = 20
_PAGE_SUFFIXES = {".html", ".htm", ".xhtml"}
_EMBEDDED_DOCUMENT_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml"}
_EMBEDDED_DOCUMENT_SUFFIXES = (".html", ".htm", ".xhtml", ".svg")
_SVG_URL_ATTRIBUTES = {
    "clip-path",
    "cursor",
    "fill",
    "filter",
    "marker-end",
    "marker-mid",
    "marker-start",
    "mask",
    "stroke",
}
# 与采集侧共用同一份 rel 集合；去掉只指向源站、不产生本地文件的提示项。
_SKIPPED_LINK_RELS = {"dns-prefetch", "preconnect", "pingback", "search"}
_RESOURCE_RELS = RESOURCE_LINK_RELS - _SKIPPED_LINK_RELS

_Purpose = Literal["asset", "navigation", "html", "css"]
_Issue = Literal["missing", "outside", "unverified"]


class OfflineIssue(TypedDict):
    kind: _Issue | Literal["remote"]
    source: str
    detail: str


class OfflineCheck(TypedDict):
    schema_version: int
    status: Literal["passed", "failed", "limited"]
    documents_checked: int
    local_references: int
    remote_resources: int
    missing: int
    outside: int
    unverified: int
    examples: list[OfflineIssue]


def unavailable_offline_check(*, index_written: bool = False) -> OfflineCheck:
    detail = "离线索引已生成，静态验收未完成" if index_written else "本次任务未生成离线索引"
    # 「没验」不是「有缺失」：不能伪造 missing=1 让报告显示「静态引用有缺失」。
    return {
        "schema_version": 1,
        "status": "limited",
        "documents_checked": 0,
        "local_references": 0,
        "remote_resources": 0,
        "missing": 0,
        "outside": 0,
        "unverified": 1,
        "examples": [{"kind": "unverified", "source": OFFLINE_INDEX, "detail": detail}],
    }


def _css_url(token: Any) -> str:
    if token.type in {"url", "string"}:
        return str(token.value)
    if token.type == "function" and token.lower_name == "url":
        argument = next((item for item in token.arguments if item.type != "whitespace"), None)
        if argument is not None and argument.type == "string":
            return str(argument.value)
    raise ValueError("unsupported CSS URL")


def _css_references(nodes: Iterable[Any]) -> Iterator[tuple[str, _Purpose]]:
    for node in nodes:
        if node.type == "error":
            raise ValueError("malformed CSS reference")
        if node.type == "at-rule" and node.lower_at_keyword == "import":
            first = next((item for item in node.prelude if item.type != "whitespace"), None)
            if first is not None:
                yield _css_url(first), "css"
        elif node.type == "url" or (node.type == "function" and node.lower_name == "url"):
            yield _css_url(node), "asset"
        elif node.type == "function":
            if node.lower_name in {"image-set", "-webkit-image-set"}:
                for item in node.arguments:
                    if item.type == "string":
                        yield _css_url(item), "asset"
            yield from _css_references(node.arguments)
        elif node.type in {"{} block", "[] block", "() block", "qualified-rule", "at-rule"}:
            yield from _css_references(node.content or ())


def _html_references(text: str) -> Iterator[tuple[str, _Purpose]]:
    root = parse_html(text).element.getroottree().getroot()
    if root is None:
        return
    if any(
        isinstance(node.tag, str) and node.tag.rsplit(":", 1)[-1] == "base" and node.get("href")
        for node in root.iter()
    ):
        raise ValueError("offline document contains an active base URL")
    for element in root.iter():
        tag = element.tag
        if not isinstance(tag, str):
            continue
        tag = tag.rsplit(":", 1)[-1]
        rel = set(element.get("rel", "").lower().split()) if tag == "link" else set()
        if tag == "iframe" and "srcdoc" in element.attrib:
            yield from _html_references(element.attrib["srcdoc"])
        elif tag in {"image", "use", "feimage"}:
            yield (
                element.get("href") or element.get("xlink:href", ""),
                "html" if tag == "use" else "asset",
            )
        elif tag == "meta":
            if element.get("http-equiv", "").strip().lower() == "refresh":
                destination = _refresh_target({"refresh": element.get("content", "")})
                if destination.startswith(_REFRESH_UNPARSEABLE):
                    raise ValueError("unsupported automatic navigation")
                yield destination, "html"
                continue
            # 写侧会把 og:image / twitter:image 等 meta content 改写成本地相对
            # 路径；验收侧必须按同一口径检查，否则删掉本地图片也会判 passed。
            key = (
                (element.get("property") or element.get("name") or element.get("itemprop") or "")
                .strip()
                .lower()
            )
            content = element.get("content", "")
            if key in META_URL_NAMES and content:
                yield content, "asset"
        elif tag != "meta" and (tag != "link" or rel & _RESOURCE_RELS):
            for name in HTML_RESOURCE_ATTRS.get(tag, ()):
                value = element.get(name, "")
                if name in {"srcset", "imagesrcset"}:
                    for start, end in _srcset_url_spans(value):
                        yield value[start:end], "asset"
                else:
                    embedded_document = tag in {"object", "embed"} and (
                        element.get("type", "").partition(";")[0].strip().lower()
                        in _EMBEDDED_DOCUMENT_TYPES
                        or unquote(value.partition("#")[0].partition("?")[0])
                        .lower()
                        .endswith(_EMBEDDED_DOCUMENT_SUFFIXES)
                    )
                    yield (
                        value,
                        "css"
                        if "stylesheet" in rel
                        else "html"
                        if tag == "iframe" or embedded_document
                        else "asset",
                    )
        for name in PAGE_LINK_ATTRS.get(tag, ()):
            yield element.get(name, ""), "navigation"
        for attribute, value in element.attrib.items():
            if value and (attribute == "style" or attribute in _SVG_URL_ATTRIBUTES):
                yield from _css_references(parse_component_value_list(value, skip_comments=True))
        if tag == "style" and element.text:
            yield from _css_references(parse_stylesheet(element.text, skip_comments=True))


def verify_offline(
    directory: Path, *, should_stop: Callable[[], bool] | None = None
) -> OfflineCheck:
    """Inspect the generated index and reachable HTML/CSS without following network URLs."""
    root = directory.resolve()
    index = root / OFFLINE_INDEX
    pending: deque[tuple[Path, Literal["html", "css"]]] = deque([(index, "html")])
    seen = set(pending)
    examples: list[OfflineIssue] = []
    remote_examples: list[OfflineIssue] = []
    issues: dict[_Issue, int] = {"missing": 0, "outside": 0, "unverified": 0}
    documents = local = remote = references = total_bytes = 0

    def example(kind: _Issue | Literal["remote"], source: Path, detail: str) -> None:
        bucket = remote_examples if kind == "remote" else examples
        if len(bucket) < _EXAMPLES:
            bucket.append(
                {
                    "kind": kind,
                    "source": source.relative_to(root).as_posix(),
                    "detail": detail[:240],
                }
            )

    def issue(kind: _Issue, source: Path, detail: str) -> None:
        issues[kind] += 1
        example(kind, source, detail)

    def contained(target: Path, source: Path) -> bool:
        try:
            if target.is_relative_to(root) and target.resolve().is_relative_to(root):
                return True
        except (OSError, ValueError, RuntimeError):
            issue("unverified", source, "无法解析文件位置")
            return False
        issue("outside", source, "引用的位置超出离线目录")
        return False

    while pending:
        source, document_kind = pending.popleft()
        if (
            (should_stop is not None and should_stop())
            or references >= _REFERENCES
            or total_bytes >= _TOTAL_DOCUMENT_BYTES
        ):
            issue("unverified", source, "检查提前结束，仍有文档未检查")
            break
        if not contained(source, source):
            continue
        try:
            if not source.is_file():
                issue("missing", source, "离线文档不存在")
                continue
            budget = min(_DOCUMENT_BYTES, _TOTAL_DOCUMENT_BYTES - total_bytes)
            with source.open("rb") as file:
                data = file.read(budget + 1)
            total_bytes += len(data)
            if len(data) > budget:
                issue("unverified", source, "超过静态检查的读取上限")
                continue
            if document_kind == "html":
                try:
                    found = _html_references(data.decode("utf-8-sig"))
                except UnicodeDecodeError:
                    issue("unverified", source, "文档不是 UTF-8 编码，未检查引用")
                    continue
            else:
                rules, _encoding = parse_stylesheet_bytes(data, skip_comments=True)
                found = _css_references(rules)
            index_has_page = False
            for value, purpose in found:
                value = value.strip()
                if not value or value.startswith("#"):
                    continue
                if references >= _REFERENCES:
                    issue("unverified", source, "超过静态引用检查数上限")
                    break
                references += 1
                try:
                    parts = urlsplit(value)
                except ValueError:
                    issue("unverified", source, "引用格式无效")
                    continue
                if parts.scheme in {"http", "https"} or (parts.netloc and not parts.scheme):
                    if purpose != "navigation":
                        remote += 1
                        example("remote", source, "外部资源 · " + (parts.hostname or "网址未识别"))
                    continue
                if len(parts.scheme) == 1:
                    issue("outside", source, "引用使用了绝对驱动器路径")
                    continue
                if parts.scheme and parts.scheme != "file":
                    continue
                # 浏览器把 URL 里的反斜杠归一成斜杠；这里保持一致，再交给
                # normpath + contained 处理 ../ 逃逸。
                path = unquote(parts.path).replace("\\", "/")
                if not path and not parts.scheme:
                    continue
                if parts.scheme == "file" or ":" in path or "\0" in path:
                    issue("outside", source, "引用使用了离线目录外的文件位置")
                    continue
                if path.startswith("/"):
                    # 站点根路径无法在归档内静态定位，但不是「越出目录」的硬错误。
                    issue("unverified", source, "引用使用站点根路径，离线无法定位")
                    continue
                target = Path(os.path.normpath(source.parent / path))
                if not contained(target, source):
                    continue
                local += 1
                suffix = target.suffix.lower()
                index_has_page |= (
                    purpose == "navigation" and suffix in _PAGE_SUFFIXES and target != index
                )
                if target.is_dir():
                    issue("unverified", source, "引用指向目录，未检查其入口文件")
                    continue
                if not target.is_file():
                    issue("missing", source, target.relative_to(root).as_posix())
                    continue
                if purpose == "css" or suffix == ".css":
                    item: tuple[Path, Literal["html", "css"]] = (target, "css")
                elif purpose == "html" or (purpose == "navigation" and suffix in _PAGE_SUFFIXES):
                    item = (target, "html")
                else:
                    continue
                if item not in seen:
                    seen.add(item)
                    pending.append(item)
            if source == index and not index_has_page:
                issue("unverified", source, "索引没有本地页面链接")
            documents += 1
        except (OSError, ValueError, RecursionError, etree.LxmlError):
            issue("unverified", source, "无法读取或解析文档引用")

    return {
        "schema_version": 1,
        "status": "failed"
        if issues["missing"] or issues["outside"]
        else "limited"
        if remote or issues["unverified"]
        else "passed",
        "documents_checked": documents,
        "local_references": local,
        "remote_resources": remote,
        "missing": issues["missing"],
        "outside": issues["outside"],
        "unverified": issues["unverified"],
        "examples": (examples + remote_examples)[:_EXAMPLES],
    }


def format_offline_check(check: Mapping[str, object]) -> str:
    title = {
        "passed": "静态引用通过",
        "failed": "静态引用有缺失或越界",
        "limited": "静态引用部分可验证",
    }.get(str(check.get("status")), "静态引用未验证")
    return (
        f"{title} · 检查 {check['documents_checked']} 份文档 · 本地 {check['local_references']} 项 · "
        f"缺失 {check['missing']} 项 · 越界 {check['outside']} 项 · 外部资源 {check['remote_resources']} 项 · "
        f"未检查或无法解析 {check['unverified']} 项"
    )

"""Rewrite captured documents to reference artifacts inside the output directory."""

from __future__ import annotations

import logging
import posixpath
import re
from collections import ChainMap
from collections.abc import Callable, Mapping
from hashlib import sha256
from html import escape
from pathlib import Path
from urllib.parse import quote, urldefrag, urljoin, urlsplit

from lxml import etree

from web_crawler.app._fsutil import atomic_write_text
from web_crawler.app.crawler_models import ManifestRow
from web_crawler.app.crawler_net import (
    HTML_RESOURCE_ATTRS,
    META_URL_NAMES,
    PAGE_LINK_ATTRS,
    _resolve_html_base,
    _srcset_url_spans,
    decode_stylesheet,
)
from web_crawler.parser.rules import parse_html

_log = logging.getLogger("crawler")
OFFLINE_PAGES_DIRNAME = "offline_pages"
OFFLINE_INDEX = f"{OFFLINE_PAGES_DIRNAME}/index.html"
_DOCTYPE = re.compile(r"^\s*(<!doctype\s+[^>]+>)", re.IGNORECASE)
_CSS_REFERENCE = re.compile(
    r"/\*.*?\*/"
    r"|@import\s+(?P<imported>\"[^\"\\]*\"|'[^'\\]*')"
    r"|(?<![\w-])url\(\s*(?P<url>\"[^\"\\]*\"|'[^'\\]*'|[^'\"\s()\\]+)\s*\)"
    r"|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'",
    re.IGNORECASE | re.DOTALL,
)


def _media_type(row: ManifestRow) -> str:
    return row.content_type.partition(";")[0].strip().lower()


class RewriteTable:
    """A shared lookup for downloaded assets and captured page destinations."""

    def __init__(self, resources: list[ManifestRow], output_dir: Path) -> None:
        self.output_dir = output_dir.resolve()
        self._assets: dict[str, str] = {}
        self._pages: dict[str, str] = {}
        by_content: dict[tuple[str, str], str] = {}
        for row in resources:
            if not row.status.startswith("ok") or not row.saved_path:
                continue
            try:
                local = Path(row.saved_path).resolve().relative_to(self.output_dir).as_posix()
            except ValueError:
                _log.warning("rewrite: saved_path outside output_dir, skipped: %s", row.saved_path)
                continue
            self._assets[row.url] = local
            if row.sha256:
                by_content.setdefault((row.sha256, _media_type(row)), local)
        for row in resources:
            if row.status == "skipped by dedup" and row.sha256:
                alias = by_content.get((row.sha256, _media_type(row)))
                if alias:
                    self._assets.setdefault(row.url, alias)

    def set_pages(self, pages: Mapping[str, Path]) -> None:
        self._pages = {
            url: path.relative_to(self.output_dir).as_posix() for url, path in pages.items()
        }

    @staticmethod
    def _reference(
        value: str,
        base: str,
        directory: str,
        paths: Mapping[str, str],
        *,
        page_url: str | None = None,
    ) -> str:
        raw = value.strip()
        if not raw:
            return value
        try:
            absolute = urljoin(base, raw)
            if urlsplit(absolute).scheme not in {"http", "https"}:
                return value
            key, fragment = urldefrag(absolute)
        except ValueError:
            return value
        if raw.startswith("#") and (page_url is None or key == urldefrag(page_url)[0]):
            return value
        local = paths.get(key)
        if local is None:
            return absolute
        relative = quote(posixpath.relpath(local, directory), safe="/")
        return relative + ("#" + fragment if fragment else "")

    def _css(self, css: str, base: str, directory: str) -> str:
        def rewrite(match: re.Match[str]) -> str:
            group = "imported" if match["imported"] is not None else "url"
            token = match[group]
            if token is None:
                return match[0]
            wrapped = token.startswith(("'", '"'))
            raw = token[1:-1] if wrapped else token
            replacement = self._reference(raw, base, directory, self._assets)
            if replacement == raw:
                return match[0]
            start, end = match.span(group)
            escaped = replacement.replace('"', "%22").replace("\\", "%5C")
            return (
                match[0][: start - match.start()]
                + '"'
                + escaped
                + '"'
                + match[0][end - match.start() :]
            )

        return _CSS_REFERENCE.sub(rewrite, css)

    def rewrite_stylesheets(
        self, resources: list[ManifestRow], should_stop: Callable[[], bool], *, encoding: str | None
    ) -> None:
        candidates: list[tuple[ManifestRow, str, Path]] = []
        for row in resources:
            source = self._assets.get(row.url)
            if source and (_media_type(row) == "text/css" or row.category == "css"):
                name = sha256(row.url.encode("utf-8")).hexdigest() + ".css"
                destination = Path(OFFLINE_PAGES_DIRNAME) / "_styles" / name
                candidates.append((row, source, destination))
                self._assets[row.url] = destination.as_posix()
        for row, source, destination in candidates:
            if should_stop():
                break
            try:
                data = (self.output_dir / source).read_bytes()
                css = decode_stylesheet(data, row.content_type, encoding)
                css = re.sub(
                    r"^\s*@charset\s+[\'\"][^\'\"]+[\'\"];", "", css, count=1, flags=re.IGNORECASE
                )
                path = self.output_dir / destination
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    '@charset "UTF-8";\n' + self._css(css, row.url, destination.parent.as_posix()),
                    encoding="utf-8",
                )
            except (OSError, LookupError) as exc:
                self._assets[row.url] = source
                _log.warning("offline stylesheet rewrite failed for %s: %s", row.url, exc)

    def apply(self, html: str, page_url: str, *, destination: Path | None = None) -> str:
        root = parse_html(html).element.getroottree().getroot()
        directory = (
            destination.parent.relative_to(self.output_dir).as_posix() if destination else "."
        )
        base = page_url
        links = ChainMap(self._pages, self._assets)
        seen_base = False
        for node in root.iter("base"):
            href = node.attrib.pop("href", None)
            if href is not None and not seen_base:
                seen_base = True
                base = _resolve_html_base(page_url, href)
        for element in root.iter():
            tag = element.tag
            if not isinstance(tag, str):
                continue
            attributes = HTML_RESOURCE_ATTRS.get(tag, ()) + PAGE_LINK_ATTRS.get(tag, ())
            if tag == "meta":
                prop = (
                    element.get("property") or element.get("name") or element.get("itemprop") or ""
                )
                attributes = attributes if prop.lower() in META_URL_NAMES else ()
            for name in attributes:
                original = element.get(name)
                if original is None:
                    continue
                value = original
                paths = links if tag in PAGE_LINK_ATTRS else self._assets
                if name in {"srcset", "imagesrcset"}:
                    for start, end in reversed(list(_srcset_url_spans(value))):
                        value = (
                            value[:start]
                            + self._reference(value[start:end], base, directory, paths)
                            + value[end:]
                        )
                else:
                    value = self._reference(value, base, directory, paths, page_url=page_url)
                if (
                    tag == "link"
                    and name == "href"
                    and "stylesheet" in element.get("rel", "").lower().split()
                    and value != original
                    and not urlsplit(value).scheme
                ):
                    element.attrib.pop("integrity", None)
                    element.attrib.pop("crossorigin", None)
                element.set(name, value)
            if element.get("style"):
                element.set("style", self._css(element.get("style"), base, directory))
            if tag == "style" and element.text:
                element.text = self._css(element.text, base, directory)
        head = root.find("head")
        if head is None:
            head = etree.Element("head")
            root.insert(0, head)
        for node in head.iter("meta"):
            if "charset" in node.attrib:
                node.set("charset", "utf-8")
            elif node.get("http-equiv", "").lower() == "content-type":
                node.set("content", "text/html; charset=utf-8")
        head.insert(0, etree.Element("meta", charset="utf-8"))
        doctype = _DOCTYPE.match(html)
        return etree.tostring(
            root, encoding="unicode", method="html", doctype=doctype[1] if doctype else None
        )


def write_offline_index(
    output_dir: Path, pages: Mapping[str, Path], titles: Mapping[str, str]
) -> None:
    directory = output_dir / OFFLINE_PAGES_DIRNAME
    entries = "".join(
        f'<li><a href="{escape(quote(path.relative_to(directory).as_posix(), safe="/"))}">'
        f"{escape(titles.get(url) or url)}</a></li>\n"
        for url, path in pages.items()
    )
    atomic_write_text(
        output_dir / OFFLINE_INDEX,
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>离线页面</title><style>body{font:1rem system-ui;max-width:64rem;"
        "margin:2rem auto;padding:0 1rem}li{margin:.6rem 0;overflow-wrap:anywhere}</style></head>"
        f"<body><main><h1>离线页面</h1><ul>{entries}</ul></main></body></html>",
    )

"""逐页元数据：<meta name="description"> / <html lang> 提取与报告落地。

覆盖三个层面：
- :func:`_extract_page_meta` 的防御性解析（大小写、单双引号、实体、截断）；
- ``run_report.json`` 的 ``top_pages`` 逐页字段（状态码 / Content-Type /
  description / lang，缺失回落 null / 空串）；
- ``--resume-crawl`` 状态对 ``page_meta`` 的保存与恢复（含旧状态兼容）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from web_crawler.app import _crawler_context as ctx_mod
from web_crawler.app import _crawler_core as core
from web_crawler.app import _crawler_scan as scan
from web_crawler.app import crawler as cr
from web_crawler.app._crawler_context import _CrawlContext

# ========== 页面元数据解析 ==========


class TestExtractPageMeta:
    def test_basic(self) -> None:
        html = (
            '<!DOCTYPE html><html lang="zh-CN"><head>'
            '<meta name="description" content="示例页面描述">'
            "</head><body></body></html>"
        )
        assert scan._extract_page_meta(html) == ("示例页面描述", "zh-CN")

    def test_case_and_single_quotes(self) -> None:
        html = (
            "<HTML LANG='en-US'><HEAD><META NAME='Description' CONTENT='A &amp; B'></HEAD></HTML>"
        )
        assert scan._extract_page_meta(html) == ("A & B", "en-US")

    def test_unquoted_and_self_closing(self) -> None:
        html = "<html lang=fr><head><meta name=description content=bonjour /></head>"
        assert scan._extract_page_meta(html) == ("bonjour", "fr")

    def test_whitespace_collapsed(self) -> None:
        html = (
            '<html lang="en"><head><meta name="description" '
            'content="  first\n\tsecond   third  "></head>'
        )
        assert scan._extract_page_meta(html) == ("first second third", "en")

    def test_missing_returns_empty_strings(self) -> None:
        assert scan._extract_page_meta("<html><head></head><body>x</body></html>") == ("", "")
        assert scan._extract_page_meta("") == ("", "")

    def test_non_html_is_tolerated(self) -> None:
        assert scan._extract_page_meta('{"json": true, "lang": "en"}') == ("", "")

    def test_truncated_html(self) -> None:
        # 截断在属性中间：不抛异常，已解析到的 lang 保留
        html = '<html lang="de"><head><meta name="description" content="cut off'
        assert scan._extract_page_meta(html) == ("", "de")

    def test_meta_after_head_end_ignored(self) -> None:
        html = (
            '<html><head></head><body><meta name="description" content="late meta"></body></html>'
        )
        assert scan._extract_page_meta(html) == ("", "")

    def test_first_non_empty_meta_wins(self) -> None:
        html = (
            '<html lang="en"><head>'
            '<meta name="description" content="">'
            '<meta name="description" content="second">'
            "</head>"
        )
        assert scan._extract_page_meta(html) == ("second", "en")

    def test_description_capped(self) -> None:
        long_text = "x" * 900
        html = f'<html><head><meta name="description" content="{long_text}"></head>'
        description, _ = scan._extract_page_meta(html)
        assert description == "x" * 500


# ========== 报告上下文字段 ==========


class TestReportTopPagesMeta:
    def _rows(self) -> list[cr.ManifestRow]:
        def row(url: str) -> cr.ManifestRow:
            return cr.ManifestRow(
                status="ok",
                url=url,
                saved_path="/tmp/a.js",
                content_type="application/javascript",
                bytes=1,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="Page",
                diagnostic="",
            )

        return [row("https://x.com/a.js"), row("https://x.com/b.js")]

    def test_meta_present(self) -> None:
        ctx = cr.build_report_context(
            self._rows(),
            1,
            0.0,
            1.0,
            page_meta={
                "https://x.com/": {
                    "status_code": 200,
                    "content_type": "text/html; charset=utf-8",
                    "description": "desc",
                    "lang": "ja",
                }
            },
        )
        assert ctx["top_pages"] == [
            {
                "page_url": "https://x.com/",
                "page_title": "Page",
                "resource_count": 2,
                "status_code": 200,
                "content_type": "text/html; charset=utf-8",
                "description": "desc",
                "lang": "ja",
            }
        ]

    def test_meta_missing_falls_back(self) -> None:
        ctx = cr.build_report_context(self._rows(), 1, 0.0, 1.0)
        page = ctx["top_pages"][0]  # type: ignore[index]
        assert page["status_code"] is None
        assert page["content_type"] == ""
        assert page["description"] == ""
        assert page["lang"] == ""
        # 原有字段不受影响
        assert page["page_url"] == "https://x.com/"
        assert page["page_title"] == "Page"
        assert page["resource_count"] == 2

    def test_malformed_meta_entry_is_coerced(self) -> None:
        ctx = cr.build_report_context(
            self._rows(),
            1,
            0.0,
            1.0,
            page_meta={"https://x.com/": {"status_code": "oops"}},  # type: ignore[dict-item]
        )
        page = ctx["top_pages"][0]  # type: ignore[index]
        assert page["status_code"] is None
        assert page["content_type"] == ""


# ========== 抓取层状态码捕获 ==========


class TestFetchMetaCapture:
    def test_fetch_writes_status_and_content_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = Mock()
        fake.get.return_value = Mock(
            status=200,
            content=b"<html></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )
        monkeypatch.setattr(core, "validate_url_host", lambda *args, **kwargs: None)
        monkeypatch.setattr(core, "_get_app_fetcher", lambda *args, **kwargs: fake)
        meta: dict[str, object] = {}
        body, content_type = core.fetch("https://example.com", 5, {}, 0, None, meta=meta)
        assert body == b"<html></html>"
        assert content_type == "text/html; charset=utf-8"
        assert meta == {"status": 200, "content_type": "text/html; charset=utf-8"}

    def test_render_page_writes_status_and_content_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetcher = Mock()
        fetcher.last_blocked_resources = []
        fetcher.fetch.return_value = Mock(
            status=200,
            content=b"<html><head></head></html>",
            headers={"content-type": "text/html"},
        )
        monkeypatch.setattr(core, "validate_url_host", lambda *args, **kwargs: None)
        monkeypatch.setattr(core, "_get_render_fetcher", lambda *args, **kwargs: fetcher)
        meta: dict[str, object] = {}
        body, content_type = core.render_page("https://example.com", 5, {}, meta=meta)
        assert body.startswith(b"<html>")
        assert content_type == "text/html"
        assert meta == {"status": 200, "content_type": "text/html"}


# ========== crawl 集成：报告落地 ==========


HTML_WITH_META = (
    '<!DOCTYPE html><html lang="zh-CN"><head>'
    '<meta name="description" content="  示例   页面  ">'
    "</head><body>"
    '<img src="/a.png"><script src="/b.js"></script>'
    "</body></html>"
)


def _run_crawl(tmp_path: Path, extra: list[str] | None = None) -> int:
    args = cr.build_parser().parse_args(
        [
            "--url",
            "https://example.com",
            "--out",
            str(tmp_path),
            "--list-only",
            "--workers",
            "1",
            *(extra or []),
        ]
    )
    return cr.crawl(args)


def _fake_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
    meta = kwargs.get("meta")
    if isinstance(meta, dict):
        meta["status"] = 200
        meta["content_type"] = "text/html; charset=utf-8"
    return HTML_WITH_META.encode("utf-8"), "text/html; charset=utf-8"


class TestCrawlReportPageMeta:
    def test_top_pages_carries_page_metadata(self, tmp_path: Path) -> None:
        with patch.object(core, "fetch", side_effect=_fake_fetch):
            assert _run_crawl(tmp_path) == 0
        report = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))
        page = report["top_pages"][0]
        assert page["page_url"].startswith("https://example.com")
        assert page["resource_count"] == 2
        assert page["status_code"] == 200
        assert page["content_type"] == "text/html; charset=utf-8"
        assert page["description"] == "示例 页面"
        assert page["lang"] == "zh-CN"

        markdown = (tmp_path / "run_report.md").read_text(encoding="utf-8")
        assert "zh-CN" in markdown and "示例 页面" in markdown
        html = (tmp_path / "run_report.html").read_text(encoding="utf-8")
        assert "zh-CN" in html and "示例 页面" in html

    def test_missing_meta_block_is_json_safe(self, tmp_path: Path) -> None:
        """页面没有 description/lang 时字段为空串，JSON 仍可解析。"""
        html = b'<html><body><img src="/a.png"></body></html>'

        def fake_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            # 模拟 patched fetch：不回填 meta，只返回 (body, content_type)
            return html, "text/html"

        with patch.object(core, "fetch", side_effect=fake_fetch):
            assert _run_crawl(tmp_path) == 0
        report = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))
        page = report["top_pages"][0]
        assert page["status_code"] is None  # 抓取桩未提供状态码 -> null
        assert page["content_type"] == "text/html"  # 回落到抓取返回值
        assert page["description"] == ""
        assert page["lang"] == ""


# ========== 续爬状态 ==========


def _state_ctx(tmp_path: Path) -> _CrawlContext:
    return _CrawlContext(
        args=argparse.Namespace(resume_crawl=True),
        headers={},
        output_dir=tmp_path,
        max_bytes=None,
        block_keywords=[],
        robots=None,
        rate_limiter=None,  # type: ignore[arg-type]
        dedup=None,
        seed_url="https://example.com",
    )


class TestCrawlStatePageMeta:
    def test_restore_reads_page_meta(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _state_ctx(tmp_path)
        monkeypatch.setattr(
            scan.ctx_mod,
            "load_crawl_state",
            lambda _root: {
                "seed_url": "https://example.com",
                "page_queue": [],
                "seen_pages": [],
                "page_titles": {"https://example.com/": "Home"},
                "page_meta": {
                    "https://example.com/": {
                        "status_code": 200,
                        "content_type": "text/html",
                        "description": "restored",
                        "lang": "en",
                    },
                    "https://example.com/bad": "not-a-dict",
                    "https://example.com/partial": {"status_code": True},
                },
                "page_files": {},
                "resources": [],
            },
        )
        scan._restore_state(ctx)
        assert ctx.page_meta["https://example.com/"] == {
            "status_code": 200,
            "content_type": "text/html",
            "description": "restored",
            "lang": "en",
        }
        # 非 dict 条目被丢弃；残缺条目按 null / 空串补齐（bool 不算状态码）
        assert "https://example.com/bad" not in ctx.page_meta
        assert ctx.page_meta["https://example.com/partial"] == {
            "status_code": None,
            "content_type": "",
            "description": "",
            "lang": "",
        }

    def test_restore_without_page_meta_field_keeps_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """旧版本状态文件没有 page_meta：恢复后为空，不报错。"""
        ctx = _state_ctx(tmp_path)
        monkeypatch.setattr(
            scan.ctx_mod,
            "load_crawl_state",
            lambda _root: {
                "seed_url": "https://example.com",
                "page_queue": [],
                "seen_pages": [],
                "page_titles": {},
                "page_files": {},
                "resources": [],
            },
        )
        scan._restore_state(ctx)
        assert ctx.page_meta == {}

    def test_scan_saves_page_meta_in_state(self, tmp_path: Path) -> None:
        with (
            patch.object(core, "fetch", side_effect=_fake_fetch),
            patch.object(ctx_mod, "save_crawl_state") as save,
        ):
            assert (
                _run_crawl(tmp_path, ["--crawl-pages", "--max-pages", "1", "--resume-crawl"]) == 0
            )
        save.assert_called()
        saved_meta = save.call_args.kwargs["page_meta"]
        entry = next(iter(saved_meta.values()))
        assert entry["status_code"] == 200
        assert entry["description"] == "示例 页面"
        assert entry["lang"] == "zh-CN"

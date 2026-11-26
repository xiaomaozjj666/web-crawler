"""边界分支回归：正文抽取、离线验收、编码与状态辅助。"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import logging
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from tinycss2 import parse_stylesheet

from web_crawler.app import _crawler_core as core
from web_crawler.app import _crawler_post as post
from web_crawler.app import _crawler_scan as scan
from web_crawler.app import _offline_verify as ov
from web_crawler.app import _report_extract as rx
from web_crawler.app import crawler as crawler_app
from web_crawler.app._crawler_cli import (
    _non_negative_float,
    _non_negative_int,
    _positive_int,
    _regex_arg,
    _worker_count,
)
from web_crawler.app._crawler_context import (
    MAX_WORKERS,
    _CrawlContext,
    clamp_workers,
    load_config_from_file,
    reserve_page_path,
)
from web_crawler.app._crawler_scan import _resolved_encoding
from web_crawler.app.crawler_models import Resource
from web_crawler.app.crawler_net import ContentDedup, DomainRateLimiter
from web_crawler.exceptions import ConfigError, FetchError


class TestProseFilter:
    def test_rejects_urls_code_and_short_text(self) -> None:
        assert not rx._looks_like_prose("https://example.com/x")
        assert not rx._looks_like_prose("var x = 1;")
        assert not rx._looks_like_prose("tiny")
        assert rx._looks_like_prose("这是一句中文正文")

    def test_inline_separator_branches(self) -> None:
        assert rx._inline_separator("", "a") == ""
        assert rx._inline_separator("a ", "b") == " "
        assert rx._inline_separator("，", "b") == ""
        assert rx._inline_separator("a", "中") == " "
        assert rx._inline_separator("a", ".") == ""
        assert rx._inline_separator("a", "b") == " "

    def test_collect_prose_depth_limit(self) -> None:
        out: list[str] = []
        rx._collect_prose({"deep": "x" * 40}, out, set(), depth=25)
        assert out == []

    def test_collect_prose_parses_double_encoded_json(self) -> None:
        out: list[str] = []
        inner = json.dumps({"t": "This is a long enough sentence with several words."})
        rx._collect_prose(inner, out, set())
        assert any("long enough sentence" in item for item in out)

    def test_collect_delta_short_text_is_ignored(self) -> None:
        out: list[str] = []
        rx._collect_delta_text({"ops": [{"insert": "short"}]}, out, set())
        assert out == []


class TestEmbeddedStateEdges:
    def test_script_variants_are_skipped(self) -> None:
        html = (
            '<script src="/external.js"></script>'
            "<script></script>"
            '<script type="application/json">{not json}</script>'
            "<script>window.state = {not json};</script>"
        )
        assert rx._embedded_json_text(rx.parse_html(html)) == ""

    def test_empty_pre_and_skip_tag_tail(self) -> None:
        assert rx.extract_readable_text("<article><pre>   </pre></article>") == ""
        result = rx.extract_readable_text("<article><p>a<script>x</script>b</p></article>")
        assert "a b" in result


class TestCssReferenceEdges:
    def test_nested_at_rules_and_image_set(self) -> None:
        nodes = parse_stylesheet(
            '@media all { .x { background: image-set(url("a.png") 1x) } }', skip_comments=True
        )
        refs = list(ov._css_references(nodes))
        assert ("a.png", "asset") in refs

    def test_error_token_raises(self) -> None:
        with pytest.raises(ValueError, match="malformed CSS"):
            list(ov._css_references([type("T", (), {"type": "error"})()]))


class TestOfflineVerifyEdges:
    def test_index_without_local_page_is_unverified(self, tmp_path: Path) -> None:
        (tmp_path / "offline_pages").mkdir()
        (tmp_path / "offline_pages/index.html").write_text(
            "<main>no links</main>", encoding="utf-8"
        )
        check = ov.verify_offline(tmp_path)
        assert check["status"] == "limited"
        assert any("索引没有本地页面链接" in item["detail"] for item in check["examples"])


class TestCrawlerHelpers:
    def test_resolved_encoding_fallbacks(self) -> None:
        assert _resolved_encoding("gbk", "text/html") == "gbk"
        assert _resolved_encoding(None, "text/html; charset=GB18030; x=1") == "gb18030"
        assert _resolved_encoding(None, "text/html; charset=") == "utf-8"
        assert _resolved_encoding(None, "text/html") == "utf-8"

    def test_page_meta_parser_skips_non_lang_attributes(self) -> None:
        html = '<html data-app="x" lang="zh-CN"><head></head><body></body></html>'
        assert scan._extract_page_meta(html) == ("", "zh-CN")

    def test_reserve_page_path_collision_adds_digest(self, tmp_path: Path) -> None:
        used = {str(tmp_path / "a.html").casefold()}
        path = reserve_page_path(tmp_path / "a.html", "https://x.com/a", used)
        assert path != tmp_path / "a.html"
        assert path.name.startswith("a_")

    def test_resource_dicts_rebuilds_when_shrunk(self) -> None:
        ctx = _CrawlContext(
            args=argparse.Namespace(),
            headers={},
            output_dir=Path("."),
            max_bytes=None,
            block_keywords=[],
            robots=None,
            rate_limiter=None,  # type: ignore[arg-type]
            dedup=None,
        )
        ctx.all_resources.append(Resource("https://x.com/a", "img", "image", "https://x.com/"))
        assert len(ctx.resource_dicts()) == 1
        ctx.all_resources.clear()
        assert ctx.resource_dicts() == []

    def test_load_config_errors(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as excinfo:
            load_config_from_file(tmp_path / "missing.json")
        assert excinfo.value.code == 2

        bad = tmp_path / "bad.json"
        bad.write_text("[]", encoding="utf-8")
        with pytest.raises(ConfigError, match="JSON 对象"):
            load_config_from_file(bad)


def test_restore_state_keeps_blocked_assets(tmp_path: Path, monkeypatch) -> None:
    from web_crawler.app import _crawler_scan as scan

    ctx = _CrawlContext(
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
            "page_scan": {"failed": 1, "skipped": 2, "first_error": "x", "blocked_assets": 4},
        },
    )
    scan._restore_state(ctx)
    assert ctx.page_scan == {
        "failed": 1,
        "skipped": 2,
        "first_error": "x",
        "blocked_assets": 4,
    }


def test_filter_resources_honours_max_resources(tmp_path: Path) -> None:
    """--max-resources 在进入下载前截断资源总量；0 表示不限制。"""
    from types import SimpleNamespace

    from web_crawler.app import _crawler_download as dl

    def make_ctx(max_resources: int) -> SimpleNamespace:
        return SimpleNamespace(
            args=SimpleNamespace(
                same_domain=False,
                video_only=False,
                include_pattern=None,
                exclude_pattern=None,
                max_resources=max_resources,
            ),
            block_keywords=[],
            all_resources=[
                Resource(
                    f"https://example.com/{i}.js", "script[src]", "script", "https://example.com/"
                )
                for i in range(5)
            ],
        )

    assert len(dl._filter_resources(make_ctx(2))) == 2
    assert len(dl._filter_resources(make_ctx(0))) == 5
    assert len(dl._filter_resources(make_ctx(99))) == 5


class TestMarkdownRenderEdges:
    """``extract_markdown`` 的可选结构分支（代码围栏 / 表格 / 列表 / 引用）。"""

    def test_pre_keeps_indent_and_trims_blank_edges(self) -> None:
        md = rx.extract_markdown("<article><pre>\n\n  code\n\n</pre><p>tail</p></article>")
        assert "```\n  code\n```" in md
        assert md.endswith("tail")

    def test_blank_pre_and_blank_heading_are_skipped(self) -> None:
        assert rx.extract_markdown("<article><pre>   </pre><p>only</p></article>") == "only"
        assert rx.extract_markdown("<article><h2>   </h2><p>x</p></article>") == "x"

    def test_table_escapes_pipes_and_pads_short_rows(self) -> None:
        md = rx.extract_markdown(
            "<article><table><tr><th>a|b</th><th>b</th></tr><tr><td>1</td></tr></table></article>"
        )
        assert "| a\\|b | b |" in md
        assert "| 1 |  |" in md

    def test_table_ignores_non_cell_children(self) -> None:
        md = rx.extract_markdown(
            "<article><table><tr><span>ignored</span><td>kept</td></tr></table></article>"
        )
        assert "| kept |" in md
        assert "ignored" not in md

    def test_table_without_rows_is_omitted(self) -> None:
        md = rx.extract_markdown("<article><table><caption>cap</caption></table></article>")
        assert "---" not in md

    def test_ordered_list_with_nested_list_and_block_children(self) -> None:
        md = rx.extract_markdown(
            "<article><ol><li>one</li>"
            "<li><p>two</p><ul><li>nested</li></ul></li>"
            "<li><table><tr><td>c</td></tr></table></li>"
            "<li><p></p></li></ol></article>"
        )
        assert "1. one" in md
        assert "2. two" in md
        assert "  - nested" in md
        assert "| c |" in md
        assert md.rstrip().endswith("4.")

    def test_list_ignores_non_li_children_and_keeps_block_tail(self) -> None:
        md = rx.extract_markdown(
            "<article><ul><span>skip</span><li><p>item</p>tail</li></ul></article>"
        )
        # li 的块级子节点单独渲染、行内文本（tail）仍作为列表项首行
        assert md.startswith("- tail")
        assert "item" in md

    def test_blockquote_block_and_inline_forms(self) -> None:
        block = rx.extract_markdown(
            "<article><blockquote><p>quote</p><p>more</p></blockquote></article>"
        )
        assert "> quote" in block and "> more" in block
        leaf = rx.extract_markdown("<article><blockquote>inline quote</blockquote></article>")
        assert leaf == "> inline quote"

    def test_loose_li_hr_and_heading(self) -> None:
        md = rx.extract_markdown("<article><li>loose</li><hr><h2>Title</h2></article>")
        assert "- loose" in md
        assert "---" in md
        assert "## Title" in md

    def test_inline_markup_and_empty_markers(self) -> None:
        md = rx.extract_markdown(
            '<article><p>see <a href="https://x.com">link</a> and '
            "<strong>bold</strong>/<em>i</em>/<code>c</code></p></article>"
        )
        assert "[link](https://x.com)" in md
        assert "**bold**" in md
        assert "*i*" in md
        assert "`c`" in md
        # 空 href / 空白强调文本不产生标记
        assert (
            rx.extract_markdown(
                "<article><p><a>plain</a> <strong> </strong> <em></em> <code> </code></p></article>"
            )
            == "plain"
        )

    def test_duplicate_consecutive_lines_are_collapsed(self) -> None:
        md = rx.extract_markdown("<article><pre>same\nsame\nother</pre></article>")
        assert md.count("same") == 1
        assert "other" in md

    def test_text_nodes_comments_and_tails(self) -> None:
        assert rx.extract_markdown("<article>lead<p>x</p>tail</article>") == "lead\n\nx\ntail"
        # 注释节点（非字符串 tag）与其文本不丢
        assert "a" in rx.extract_markdown("<article><p>a<!--c-->b</p></article>")
        # 骨架标签后的裸文本（chrome tail）保留
        assert "after" in rx.extract_markdown(
            "<article><h1>t</h1><script>x</script>after</article>"
        )
        # 顶层注释节点不参与块渲染
        assert rx.extract_markdown("<article><!--top--><p>x</p></article>") == "x"

    def test_embedded_state_fallback_for_tiny_visible_text(self) -> None:
        prose = "This is a long enough sentence rendered from embedded state data."
        html = (
            "<html><body><div id='app'></div>"
            f'<script type="application/json">{{"content": "{prose}"}}</script>'
            "</body></html>"
        )
        assert prose in rx.extract_markdown(html)
        assert prose in rx.extract_readable_text(html)

    def test_collect_prose_invalid_double_encoded_and_bare_delta_list(self) -> None:
        out: list[str] = []
        rx._collect_prose("{not json", out, set())
        assert out == []
        out = []
        rx._collect_prose(
            [{"insert": "A long enough delta sentence for the collector."}], out, set()
        )
        assert any("delta sentence" in item for item in out)

    def test_collect_prose_skips_invalid_long_json_and_mixed_list(self) -> None:
        out: list[str] = []
        # 以 { 开头且长度达标的非法 JSON：解析失败后静默跳过
        rx._collect_prose('{"broken": true, "note": "no closing brace"', out, set())
        assert out == []
        out = []
        rx._collect_prose(
            [{"insert": "A long enough delta sentence for the collector."}, 42],
            out,
            set(),
        )
        assert any("delta sentence" in item for item in out)

    def test_long_markdown_skips_embedded_fallback(self) -> None:
        paragraph = "Long paragraph text that is well above the embedded threshold. " * 5
        md = rx.extract_markdown(f"<article><p>{paragraph}</p></article>")
        assert paragraph.strip() in md

    def test_embedded_total_cap_stops_scanning(self) -> None:
        chunk = "A" * 19000
        scripts = "".join(
            f'<script type="application/json">{{"k{i}": "{chunk}"}}</script>' for i in range(12)
        )
        html = f"<html><body><div id='app'></div>{scripts}</body></html>"
        text = rx.extract_readable_text(html)
        # 总量上限（200 KiB）截断：不会把 12 × 19KB 全部塞进正文
        assert len(text) <= 200 * 1024 + 20000


class TestReadableTextRenderingEdges:
    """``extract_readable_text`` 的块级渲染（表格 / 代码块 / 注释 / 嵌套容器）。"""

    def test_blocks_lists_tables_pre_and_nested_sections(self) -> None:
        html = (
            "<article><h1>Title</h1>"
            "<p>para <strong>bold</strong> and <a href='/x'>link</a></p>"
            "<ul><li>one</li><li>two</li></ul>"
            "<table><tr><th>head</th></tr><tr><td>cell</td></tr></table>"
            "<blockquote>quote</blockquote>"
            "<pre>  \ncode\n  </pre>"
            "<section><p>nested</p></section>"
            "</article>"
        )
        text = rx.extract_readable_text(html)
        assert "Title" in text
        assert "one" in text
        assert "head" in text and "cell" in text
        assert "code" in text
        assert "nested" in text

    def test_empty_pre_comments_and_all_blank_table_rows(self) -> None:
        html = (
            "<article><pre></pre><!--comment--><p>text</p>"
            "<table><tr><td></td></tr><tr><td>value</td></tr></table></article>"
        )
        text = rx.extract_readable_text(html)
        assert "text" in text
        assert "value" in text


class TestMarkdownStructuralTails:
    """Markdown 渲染里容器/引用/骨架标签的 tail 与嵌套结构。"""

    def test_nav_tail_blockquote_tail_and_container_tail(self) -> None:
        md = rx.extract_markdown("<article><p>x</p><nav>menu</nav>after</article>")
        assert "after" in md
        assert "menu" not in md
        quote = rx.extract_markdown("<article><blockquote><p>q</p>tail</blockquote></article>")
        assert "> q" in quote and "> tail" in quote
        nested = rx.extract_markdown("<article><section><p>a</p>tail</section></article>")
        assert "a" in nested and "tail" in nested

    def test_nested_inline_visible_text_recursion(self) -> None:
        md = rx.extract_markdown(
            '<article><p>see <a href="/x"><strong>nested</strong> link</a></p></article>'
        )
        assert "[**nested** link](/x)" in md

    def test_empty_comment_and_empty_table_row(self) -> None:
        # 无文本注释（text=None）只保留 tail
        assert "a" in rx.extract_markdown("<article><p>a<!---->b</p></article>")
        md = rx.extract_markdown(
            "<article><table><tr></tr><tr><td>x</td><td>y</td></tr></table></article>"
        )
        assert "| x | y |" in md


class TestUiHttpHelpers:
    """Web UI HTTP 层的 Host 解析与分页参数校验（安全 / 契约边界）。"""

    def test_split_host_port_malformed_forms(self) -> None:
        from web_crawler.app import _ui_http as ui_http

        assert ui_http._split_host_port("[::1") == ("[::1", "")
        assert ui_http._split_host_port("[::1]x:80") == ("", "")
        assert ui_http._split_host_port("127.0.0.1:8765") == ("127.0.0.1", "8765")
        assert ui_http._split_host_port("localhost") == ("localhost", "")

    def test_pagination_rejects_bad_values(self) -> None:
        from web_crawler.app import _ui_http as ui_http

        assert ui_http._pagination({}, 20) == (1, 20)
        assert ui_http._pagination({"page": ["2"], "page_size": ["5"]}, 20) == (2, 5)
        with pytest.raises(ValueError, match="必须为整数"):
            ui_http._pagination({"page": ["abc"]}, 20)
        with pytest.raises(ValueError, match="页码必须为正数"):
            ui_http._pagination({"page_size": ["101"]}, 20)


class TestPostProcessEdges:
    """后处理阶段的 IO 失败降级分支（单页/单文件失败不中断整体）。"""

    @staticmethod
    def _ctx(tmp_path: Path, **args_kwargs: object) -> _CrawlContext:
        args_kwargs.setdefault("resume_crawl", False)
        return _CrawlContext(
            args=argparse.Namespace(**args_kwargs),
            headers={},
            output_dir=tmp_path,
            max_bytes=None,
            block_keywords=[],
            robots=None,
            rate_limiter=None,  # type: ignore[arg-type]
            dedup=None,
        )

    def test_iter_page_html_reports_read_failures(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        ctx.page_files["https://example.com/missing"] = (tmp_path / "missing.html", "utf-8")
        errors: list[tuple[str, Exception]] = []
        assert list(post._iter_page_html(ctx, lambda url, exc: errors.append((url, exc)))) == []
        assert errors and errors[0][0] == "https://example.com/missing"

    def test_sample_only_context_skips_extraction(self, tmp_path: Path) -> None:
        page = tmp_path / "pages" / "index.html"
        page.parent.mkdir(parents=True)
        page.write_text("<html><body>sample</body></html>", encoding="utf-8")
        ctx = self._ctx(tmp_path, page_sample_callback=None)
        ctx.page_files["https://example.com/"] = (page, "utf-8")
        samples: list[tuple[str, str, str]] = []
        ctx.args.page_sample_callback = lambda url, title, html: samples.append((url, title, html))
        post._extract_page_contents(ctx, None)
        assert samples and "sample" in samples[0][2]

    def test_mkdir_failures_are_logged_and_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = tmp_path / "pages" / "index.html"
        page.parent.mkdir(parents=True)
        page.write_text("<html><body><p>body text</p></body></html>", encoding="utf-8")
        ctx = self._ctx(tmp_path, extract_text=True, extract_markdown=True)
        ctx.page_files["https://example.com/"] = (page, "utf-8")
        real_mkdir = Path.mkdir

        def failing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
            if self.name in {"extracted_text", "extracted_markdown"}:
                raise OSError("denied")
            real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "mkdir", failing_mkdir)
        post._extract_page_contents(ctx, None)
        # 目录建不出来时正文/Markdown 落盘失败也不抛出（只记 warning）
        assert not (tmp_path / "extracted_text").exists()

    def test_rule_extraction_error_and_feed_write_failure(self, tmp_path: Path) -> None:
        page = tmp_path / "pages" / "index.html"
        page.parent.mkdir(parents=True)
        page.write_text("<html><body><p>body text</p></body></html>", encoding="utf-8")
        ctx = self._ctx(tmp_path, rules=True)
        ctx.page_files["https://example.com/"] = (page, "utf-8")
        ctx.rules = MagicMock()
        ctx.rules.extract.side_effect = ConfigError("bad rule")
        ctx.rule_coverage = MagicMock()
        post._extract_page_contents(ctx, None)
        ctx.rule_coverage.add_error.assert_called_once()

        # 字段结果写盘失败：计数 +1 且降级为不再写 feed
        ctx.rules = MagicMock()
        ctx.rules.extract.return_value = {"title": "x"}
        ctx.rule_coverage = MagicMock()
        feed = MagicMock()
        feed.write.side_effect = OSError("disk full")
        post._extract_page_contents(ctx, feed)
        assert ctx.rule_write_errors == 1

    def test_extracted_data_write_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = tmp_path / "pages" / "index.html"
        page.parent.mkdir(parents=True)
        page.write_text("<html><body><h1>Title</h1><p>body text</p></body></html>", "utf-8")
        ctx = self._ctx(tmp_path, smart_extract=True)
        ctx.page_files["https://example.com/"] = (page, "utf-8")

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(post, "write_extracted_data", boom)
        monkeypatch.setattr(post, "smart_extract", lambda *a, **k: {"title": "Title"})
        post._extract_page_contents(ctx, None)

    def test_iter_page_html_without_error_callback(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        ctx.page_files["https://example.com/missing"] = (tmp_path / "missing.html", "utf-8")
        assert list(post._iter_page_html(ctx)) == []

    def test_rule_feed_close_failure_is_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = tmp_path / "pages" / "index.html"
        page.parent.mkdir(parents=True)
        page.write_text("<html><body><p>body text</p></body></html>", encoding="utf-8")
        ctx = self._ctx(tmp_path)
        ctx.page_files["https://example.com/"] = (page, "utf-8")
        ctx.rules = MagicMock()
        ctx.rules.extract.return_value = {"title": "x"}
        ctx.rule_coverage = MagicMock()

        class _Feed:
            def write(self, _text: str) -> int:
                return 0

            def close(self) -> None:
                raise OSError("close failed")

        real_open = Path.open

        def fake_open(self: Path, *args: object, **kwargs: object) -> object:
            if self.name == "rule_items.jsonl":
                return _Feed()
            return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "open", fake_open)
        post._extract_pages(ctx)
        assert ctx.rule_write_errors == 1

    def test_extract_stops_when_cancelled_between_pages(self, tmp_path: Path) -> None:
        pages_dir = tmp_path / "pages"
        pages_dir.mkdir(parents=True)
        for name in ("a", "b"):
            (pages_dir / f"{name}.html").write_text(
                f"<html><body><p>{name} body</p></body></html>", encoding="utf-8"
            )
        ctx = self._ctx(tmp_path, extract_text=True)
        ctx.page_files["https://example.com/a"] = (pages_dir / "a.html", "utf-8")
        ctx.page_files["https://example.com/b"] = (pages_dir / "b.html", "utf-8")
        calls = {"n": 0}

        def should_stop() -> bool:
            calls["n"] += 1
            # 入口检查放行，第一页处理完后取消 → 第二页不再处理
            return calls["n"] > 2

        ctx.args.should_stop = should_stop
        post._extract_pages(ctx)
        assert not (tmp_path / "extracted_text" / "pages" / "b.html.txt").exists()

    def test_markdown_only_empty_page_logs_warning(self, tmp_path: Path) -> None:
        page = tmp_path / "pages" / "app.html"
        page.parent.mkdir(parents=True)
        page.write_text("<html><body><div id='app'></div></body></html>", encoding="utf-8")
        ctx = self._ctx(tmp_path, extract_markdown=True)
        ctx.page_files["https://example.com/app"] = (page, "utf-8")
        post._extract_page_contents(ctx, None)
        assert not list((tmp_path / "extracted_markdown").rglob("*.md"))


class TestScanStateSave:
    """--resume-crawl 状态保存节流：每 5 页 / 队列空闲 / 最小时间间隔。"""

    @staticmethod
    def _ctx(tmp_path: Path, *, resume_crawl: bool, seen: int) -> _CrawlContext:
        ctx = _CrawlContext(
            args=argparse.Namespace(resume_crawl=resume_crawl, crawl_pages=True),
            headers={},
            output_dir=tmp_path,
            max_bytes=None,
            block_keywords=[],
            robots=None,
            rate_limiter=None,  # type: ignore[arg-type]
            dedup=None,
            seed_url="https://example.com/",
        )
        for index in range(seen):
            ctx.seen_pages.add(f"https://example.com/{index}")
        return ctx

    def test_disabled_and_not_due_paths(self, tmp_path: Path, monkeypatch) -> None:
        calls: list[dict] = []
        monkeypatch.setattr(scan.ctx_mod, "save_crawl_state", lambda _out, **kw: calls.append(kw))

        scan._save_scan_state_if_due(self._ctx(tmp_path, resume_crawl=False, seen=1), idle=False)
        scan._save_scan_state_if_due(self._ctx(tmp_path, resume_crawl=True, seen=1), idle=False)
        # 未开 --crawl-pages 时不维护续跑状态（与串行实现一致）
        single = self._ctx(tmp_path, resume_crawl=True, seen=5)
        single.args.crawl_pages = False
        scan._save_scan_state_if_due(single, idle=True)
        assert calls == []

        throttled = self._ctx(tmp_path, resume_crawl=True, seen=5)
        throttled.last_state_save[0] = time.monotonic()
        scan._save_scan_state_if_due(throttled, idle=False)
        assert calls == []

        # 空闲（队列与在途都空）时无条件保存
        scan._save_scan_state_if_due(throttled, idle=True)
        assert len(calls) == 1
        # 每 5 页且超过最小间隔时保存
        throttled.last_state_save[0] = 0.0
        scan._save_scan_state_if_due(throttled, idle=False)
        assert len(calls) == 2

    def test_save_failure_is_swallowed(self, tmp_path: Path, monkeypatch) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(scan.ctx_mod, "save_crawl_state", boom)
        scan._save_scan_state_if_due(self._ctx(tmp_path, resume_crawl=True, seen=1), idle=True)


class TestCliTypeValidators:
    """argparse 类型校验：非法值给中文提示（退出码 2），不甩 traceback。"""

    def test_positive_int_rejects_non_int_and_zero(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="需要整数"):
            _positive_int("x")
        with pytest.raises(argparse.ArgumentTypeError, match=">= 1"):
            _positive_int("0")
        assert _positive_int("3") == 3

    def test_non_negative_int_rejects_non_int_and_negative(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="需要整数"):
            _non_negative_int("x")
        with pytest.raises(argparse.ArgumentTypeError, match=">= 0"):
            _non_negative_int("-1")
        assert _non_negative_int("0") == 0

    def test_non_negative_float_rejects_non_number_and_negative(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="需要数字"):
            _non_negative_float("x")
        with pytest.raises(argparse.ArgumentTypeError, match=">= 0"):
            _non_negative_float("-0.5")
        assert _non_negative_float("1.5") == 1.5

    def test_regex_arg_rejects_invalid_pattern(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="无效的正则"):
            _regex_arg("[")
        assert _regex_arg(r"a\d+") == r"a\d+"

    def test_worker_count_rejects_over_limit(self) -> None:
        assert _worker_count("1") == 1
        assert _worker_count(str(MAX_WORKERS)) == MAX_WORKERS
        with pytest.raises(argparse.ArgumentTypeError, match="并发数上限"):
            _worker_count(str(MAX_WORKERS + 1))


class TestWorkerThresholds:
    """并发上限：线程/连接池/浏览器进程不会随用户输入无限放大。"""

    def test_clamp_workers_bounds(self) -> None:
        assert clamp_workers(0) == 1
        assert clamp_workers(-5) == 1
        assert clamp_workers(MAX_WORKERS + 100) == MAX_WORKERS
        assert clamp_workers(None) == 8
        assert clamp_workers("not-a-number") == 8
        assert clamp_workers(3) == 3

    def test_scan_worker_count_caps_render_browsers(self) -> None:
        plain = argparse.Namespace(workers=MAX_WORKERS, render_js=False)
        assert scan._scan_worker_count(plain) == MAX_WORKERS
        render = argparse.Namespace(workers=16, render_js=True)
        assert scan._scan_worker_count(render) == 8
        assert scan._scan_worker_count(argparse.Namespace(workers=4, render_js=True)) == 4

    def test_crawl_clamps_programmatic_workers(self, tmp_path: Path) -> None:
        html = b"<html><body>ok</body></html>"
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = crawler_app.build_parser().parse_args(
                ["--url", "https://example.com", "--out", str(tmp_path), "--list-only"]
            )
            args.workers = MAX_WORKERS + 36  # 绕过 CLI 校验的编程调用
            assert crawler_app.crawl(args) == 0
        assert args.workers == MAX_WORKERS

    def test_cli_rejects_workers_over_limit(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            crawler_app.build_parser().parse_args(
                ["--url", "https://example.com", "--workers", str(MAX_WORKERS + 1)]
            )
        assert excinfo.value.code == 2


# ── 并发扫描管线：worker 结果 / 取消 / 限速 / 续跑一致性 ──────────────


def _make_scan_ctx(tmp_path: Path, **overrides: object) -> _CrawlContext:
    """构造页面扫描上下文（覆盖 _scan_page / 调度函数读取的 args 字段）。"""
    defaults: dict[str, object] = {
        "url": "https://example.com",
        "out": str(tmp_path),
        "timeout": 30,
        "retries": 0,
        "max_bytes": 0,
        "encoding": None,
        "workers": 2,
        "max_pages": 100,
        "same_domain": False,
        "user_agent": "UA",
        "crawl_pages": False,
        "render_js": False,
        "download_linked_data": False,
        "resume_crawl": False,
        "sitemap": False,
    }
    defaults.update(overrides)
    return _CrawlContext(
        args=argparse.Namespace(**defaults),
        headers={},
        output_dir=tmp_path,
        max_bytes=None,
        block_keywords=[],
        robots=None,
        rate_limiter=DomainRateLimiter(),
        dedup=None,
        seed_url="https://example.com",
    )


class TestScanWorkerOutcomes:
    """工作线程结果包：抓取取消 / 非文档 / 后处理异常的归类。"""

    def test_fetch_cancel_marks_outcome_cancelled(self, tmp_path: Path) -> None:
        ctx = _make_scan_ctx(tmp_path)
        page_path = tmp_path / "pages" / "x.html"
        with patch.object(core, "fetch", side_effect=RuntimeError("cancelled by user")):
            outcome = scan._scan_page(
                ctx, "https://example.com/x", "https://example.com", page_path
            )
        assert outcome.cancelled
        assert not outcome.error and not outcome.non_document

    def test_child_binary_response_is_non_document(self, tmp_path: Path) -> None:
        ctx = _make_scan_ctx(tmp_path)
        page_path = tmp_path / "pages" / "f.html"
        with patch.object(core, "fetch", return_value=(b"%PDF-1.4", "application/pdf")):
            outcome = scan._scan_page(
                ctx, "https://example.com/f.pdf", "https://example.com", page_path
            )
        assert outcome.non_document
        assert not page_path.exists()

    def test_empty_content_type_is_saved_as_page(self, tmp_path: Path) -> None:
        """无 Content-Type 时保持旧行为：子链接仍按页面落盘。"""
        ctx = _make_scan_ctx(tmp_path)
        page_path = tmp_path / "pages" / "plain.html"
        with patch.object(core, "fetch", return_value=(b"<html>plain</html>", "")):
            outcome = scan._scan_page(
                ctx, "https://example.com/plain", "https://example.com", page_path
            )
        assert not outcome.error and not outcome.non_document
        assert page_path.read_bytes() == b"<html>plain</html>"
        assert outcome.content_type == ""

    def test_post_processing_failure_is_reported_as_page_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非法 charset / 磁盘或解析异常不得让整个并发扫描崩溃。"""
        ctx = _make_scan_ctx(tmp_path)
        page_path = tmp_path / "pages" / "bad.html"

        def boom(*args: object, **kwargs: object) -> str:
            raise LookupError("unknown encoding: not-a-charset")

        monkeypatch.setattr(scan, "decode_text", boom)
        with patch.object(core, "fetch", return_value=(b"<html>bad</html>", "text/html")):
            outcome = scan._scan_page(
                ctx, "https://example.com/bad", "https://example.com", page_path
            )
        assert outcome.error
        assert not outcome.cancelled and not outcome.non_document


class TestRenderBlockedAccounting:
    """--render-js 的导航守卫拦截计数（worker 并发累加路径）。"""

    def test_render_success_counts_blocked_assets(self, tmp_path: Path) -> None:
        ctx = _make_scan_ctx(tmp_path, render_js=True)
        with (
            patch.object(core, "render_page", return_value=(b"<html>x</html>", "text/html")),
            patch.object(core, "drain_blocked_resources", return_value=["a", "b"]),
        ):
            data, _ct = scan._fetch_page_html(ctx, "https://example.com/")
        assert data == b"<html>x</html>"
        assert ctx.page_scan["blocked_assets"] == 2

    def test_render_failure_counts_blocked_assets_then_falls_back(self, tmp_path: Path) -> None:
        ctx = _make_scan_ctx(tmp_path, render_js=True)
        with (
            patch.object(core, "render_page", side_effect=FetchError("render crashed")),
            patch.object(core, "drain_blocked_resources", return_value=["blocked.png"]),
            patch.object(core, "fetch", return_value=(b"<html>static</html>", "text/html")),
        ):
            data, _ct = scan._fetch_page_html(ctx, "https://example.com/")
        assert data == b"<html>static</html>"
        assert ctx.page_scan["blocked_assets"] == 1


class TestScanSchedulerEdges:
    """调度线程的过滤计数 / sitemap 入队 / 结果合并边界。"""

    def test_next_scan_page_filters_blocked_domain_and_robots(self, tmp_path: Path) -> None:
        ctx = _make_scan_ctx(tmp_path, same_domain=True)
        ctx.block_keywords = ["blocked"]
        ctx.robots = MagicMock()
        ctx.robots.can_fetch.side_effect = lambda _ua, url: "robots" not in url
        for url in (
            "https://example.com/blocked",
            "https://other.com/x",
            "https://example.com/robots",
            "https://example.com/ok",
        ):
            ctx.enqueue_page(url)
        assert scan._next_scan_page(ctx) == "https://example.com/ok"
        assert ctx.page_scan["skipped"] == 3
        assert scan._next_scan_page(ctx) is None

    def test_seed_page_queue_sitemap_filters_duplicates_and_blocked(self, tmp_path: Path) -> None:
        ctx = _make_scan_ctx(tmp_path, sitemap=True, same_domain=True)
        ctx.block_keywords = ["blocked"]
        discovered = [
            "https://example.com",  # 与种子重复
            "https://example.com/blocked",
            "https://other.com/x",  # 跨域
            "https://example.com/ok",
        ]
        with patch.object(scan, "discover_sitemap_urls", return_value=discovered):
            scan._seed_page_queue(ctx, "https://example.com")
        assert list(ctx.page_queue) == ["https://example.com", "https://example.com/ok"]

    def test_apply_outcome_error_keeps_existing_first_error(self, tmp_path: Path) -> None:
        ctx = _make_scan_ctx(tmp_path)
        ctx.page_scan["first_error"] = "earlier"
        outcome = scan._PageOutcome(
            url="https://example.com/x",
            page_path=tmp_path / "pages" / "x.html",
            error="later",
        )
        scan._apply_page_outcome(ctx, outcome)
        assert ctx.page_scan["failed"] == 1
        assert ctx.page_scan["first_error"] == "earlier"

    def test_apply_outcome_skips_known_blocked_and_cross_domain_links(self, tmp_path: Path) -> None:
        ctx = _make_scan_ctx(tmp_path, crawl_pages=True, same_domain=True)
        ctx.block_keywords = ["blocked"]
        ctx.seen_pages.add("https://example.com/seen")
        ctx.enqueue_page("https://example.com/queued")
        outcome = scan._PageOutcome(
            url="https://example.com/page",
            page_path=tmp_path / "pages" / "p.html",
            page_links=[
                "https://example.com/seen",
                "https://example.com/queued",
                "https://example.com/blocked",
                "https://other.com/x",
                "https://example.com/new",
            ],
        )
        scan._apply_page_outcome(ctx, outcome)
        assert list(ctx.page_queue) == ["https://example.com/queued", "https://example.com/new"]


class TestReservePagePathLoop:
    def test_second_collision_appends_counter(self, tmp_path: Path) -> None:
        url = "https://x.com/a"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        base = tmp_path / "a.html"
        already = tmp_path / f"a_{digest}.html"
        used = {str(base).casefold(), str(already).casefold()}
        path = reserve_page_path(base, url, used)
        assert path.name == f"a_{digest}_1.html"


class TestRestoreStateCorruptEntries:
    """旧版/残缺状态条目的逐项兜底（不因单条脏数据整体失败）。"""

    def test_corrupt_entries_are_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _make_scan_ctx(tmp_path, resume_crawl=True)
        ctx.dedup = ContentDedup()
        pages_dir = tmp_path / "pages"
        pages_dir.mkdir()
        (pages_dir / "ok.html").write_text("<html>ok</html>", encoding="utf-8")
        (tmp_path / "outside.html").write_text("<html>x</html>", encoding="utf-8")
        saved = {
            "seed_url": "https://example.com",
            "page_queue": [],
            "seen_pages": [],
            "page_titles": {
                url: "T"
                for url in (
                    "https://example.com/ok",
                    "https://example.com/ok2",
                    "https://example.com/ok3",
                    "https://example.com/outside",
                    "https://example.com/boom",
                )
            },
            "page_meta": {"u1": "not-a-dict", "u2": {"status_code": True, "content_type": None}},
            "page_files": {
                "https://example.com/ok": ["pages/ok.html", "utf-8"],
                "https://example.com/nottitle": ["pages/ok.html", "utf-8"],
                "https://example.com/ok2": ["pages/ok.html", "utf-8", "extra"],
                "https://example.com/ok3": [123, "utf-8"],
                "https://example.com/outside": ["outside.html", "utf-8"],
                "https://example.com/boom": ["pages/boom.html", "utf-8"],
            },
            "resources": [
                {
                    "url": "https://example.com/a.js",
                    "found_in": "script[src]",
                    "kind": "script",
                    "page_url": "https://example.com/",
                }
            ],
            "sha256_set": ["a" * 64, 123],
            "page_scan": {
                "failed": -1,
                "skipped": "x",
                "first_error": 5,
                "blocked_assets": -2,
            },
        }
        real_resolve = Path.resolve

        def fake_resolve(self: Path, *args: object, **kwargs: object) -> Path:
            if self.name == "boom.html":
                raise OSError("cannot resolve")
            return real_resolve(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "resolve", fake_resolve)
        monkeypatch.setattr(scan.ctx_mod, "load_crawl_state", lambda _root: saved)
        scan._restore_state(ctx)

        assert set(ctx.page_files) == {"https://example.com/ok"}
        assert len(ctx.all_resources) == 1
        assert ctx.dedup is not None and ctx.dedup.seen_count() == 2
        assert ctx.page_meta["u2"]["status_code"] is None
        assert ctx.page_scan == {
            "failed": 0,
            "skipped": 0,
            "first_error": "",
            "blocked_assets": 0,
        }

    def test_non_dict_page_files_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _make_scan_ctx(tmp_path, resume_crawl=True)
        monkeypatch.setattr(
            scan.ctx_mod,
            "load_crawl_state",
            lambda _root: {
                "seed_url": "https://example.com",
                "page_titles": {},
                "page_files": [],
                "resources": [],
            },
        )
        scan._restore_state(ctx)
        assert ctx.page_files == {}


class TestResumeSnapshotInFlight:
    """续跑快照不能把在途页面写成已完成（并发下的断点一致性）。"""

    def _ctx_with_pending(self, tmp_path: Path) -> _CrawlContext:
        ctx = _make_scan_ctx(tmp_path, resume_crawl=True, crawl_pages=True)
        for index in range(4):
            ctx.seen_pages.add(f"https://example.com/{index}")
        ctx.seen_pages.add("https://example.com/inflight")
        ctx.enqueue_page("https://example.com/remaining")
        return ctx

    def test_in_flight_pages_requeued_and_excluded_from_seen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved: dict[str, object] = {}
        monkeypatch.setattr(scan.ctx_mod, "save_crawl_state", lambda _out, **kw: saved.update(kw))
        scan._save_scan_state_if_due(
            self._ctx_with_pending(tmp_path),
            idle=False,
            in_flight=["https://example.com/inflight"],
        )
        assert "https://example.com/inflight" not in saved["seen_pages"]
        assert saved["page_queue"] == [
            "https://example.com/inflight",
            "https://example.com/remaining",
        ]

    def test_idle_snapshot_keeps_every_seen_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved: dict[str, object] = {}
        monkeypatch.setattr(scan.ctx_mod, "save_crawl_state", lambda _out, **kw: saved.update(kw))
        ctx = self._ctx_with_pending(tmp_path)
        ctx.page_queue.clear()
        ctx.queued_pages.clear()
        scan._save_scan_state_if_due(ctx, idle=True)
        assert len(saved["seen_pages"]) == len(ctx.seen_pages)
        assert saved["page_queue"] == []

    def test_resume_from_snapshot_refetches_in_flight_page(self, tmp_path: Path) -> None:
        scan._save_scan_state_if_due(
            self._ctx_with_pending(tmp_path),
            idle=False,
            in_flight=["https://example.com/inflight"],
        )
        resumed = _make_scan_ctx(tmp_path, resume_crawl=True, crawl_pages=True)
        scan._restore_state(resumed)
        assert "https://example.com/inflight" in resumed.page_queue
        assert "https://example.com/inflight" not in resumed.seen_pages

        fetched: list[str] = []

        def fake_fetch(url: str, *args: object, **kwargs: object) -> tuple[bytes, str]:
            fetched.append(url)
            return b"<html>page</html>", "text/html"

        with patch.object(core, "fetch", side_effect=fake_fetch):
            assert scan._scan_pages(resumed) is False
        assert "https://example.com/inflight" in fetched
        assert "https://example.com/inflight" in resumed.page_titles


class TestConcurrentScanRobustness:
    """并发管线的端到端行为：取消合并 / 单页坏数据不拖垮整轮。"""

    def test_one_worker_cancels_while_other_page_is_merged(self, tmp_path: Path) -> None:
        index = b'<html><body><a href="/a">a</a><a href="/b">b</a></body></html>'

        def fake_fetch(url: str, *args: object, **kwargs: object) -> tuple[bytes, str]:
            if url.rstrip("/") == "https://example.com":
                return index, "text/html"
            if url.endswith("/a"):
                raise RuntimeError("cancelled by user")
            return b"<html><body>ok</body></html>", "text/html"

        with patch.object(core, "fetch", side_effect=fake_fetch):
            args = crawler_app.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--crawl-pages",
                    "--max-pages",
                    "3",
                    "--workers",
                    "2",
                    "--delay",
                    "0",
                    "--list-only",
                ]
            )
            assert crawler_app.crawl(args) == 1
        saved_pages = {path.name for path in (tmp_path / "pages").rglob("*.html")}
        assert "b.html" in saved_pages
        assert "a.html" not in saved_pages

    def test_post_processing_error_does_not_abort_crawl(self, tmp_path: Path) -> None:
        index = b'<html><body><a href="/broken">x</a><a href="/good">y</a></body></html>'

        def fake_fetch(url: str, *args: object, **kwargs: object) -> tuple[bytes, str]:
            if url.rstrip("/") == "https://example.com":
                return index, "text/html"
            if url.endswith("/broken"):
                return b"BROKEN<html>bad</html>", "text/html"
            return b"<html><body>ok</body></html>", "text/html"

        def fake_decode(data: bytes, content_type: str, fallback: str | None) -> str:
            if data.startswith(b"BROKEN"):
                raise LookupError("unknown encoding")
            return data.decode("utf-8", errors="replace")

        with (
            patch.object(core, "fetch", side_effect=fake_fetch),
            patch.object(scan, "decode_text", side_effect=fake_decode),
        ):
            args = crawler_app.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--crawl-pages",
                    "--max-pages",
                    "3",
                    "--workers",
                    "2",
                    "--delay",
                    "0",
                    "--list-only",
                ]
            )
            assert crawler_app.crawl(args) == 0
        report = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))
        assert report["page_scan"]["failed"] == 1


class TestRateLimiterConcurrency:
    """--delay 在并发 worker 下仍保持同域请求间隔（预约时隙而非齐发）。"""

    def test_concurrent_callers_are_spaced_by_default_delay(self) -> None:
        limiter = DomainRateLimiter(default_delay=0.05)
        starts: list[float] = []
        lock = threading.Lock()

        def worker() -> None:
            limiter.wait_if_needed("https://example.com/x")
            with lock:
                starts.append(time.monotonic())
            limiter.record_request("https://example.com/x")

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        starts.sort()
        gaps = [later - earlier for earlier, later in itertools.pairwise(starts)]
        assert len(gaps) == 2
        assert min(gaps) >= 0.03

    def test_record_request_does_not_rewind_future_reservation(self) -> None:
        limiter = DomainRateLimiter(default_delay=0.05)
        limiter._last_times["example.com"] = time.time() + 5
        limiter.record_request("https://example.com/x")
        assert limiter._last_times["example.com"] > time.time() + 4


class TestFetcherTeardownEdges:
    """fetcher 收尾：当前线程缓存 + 登记表去重；渲染实例多 worker 回收。"""

    def test_close_app_fetchers_closes_current_thread_cache_and_dedupes(self) -> None:
        class _FakeFetcher:
            instances: list[_FakeFetcher] = []

            def __init__(self, **kwargs: object) -> None:
                self.closes = 0
                _FakeFetcher.instances.append(self)

            def close(self) -> None:
                self.closes += 1

        core._app_fetchers.clear()
        core._fetchers_local.cache = {}
        try:
            with patch.object(core, "_import_lib_fetcher", return_value=_FakeFetcher):
                fetcher = core._get_app_fetcher(None, 30.0, None)
            # 登记表里已有一份；再追加一份模拟「线程缓存 + 登记表」双持有
            core._app_fetchers.append(fetcher)
            core.close_app_fetchers()
            assert fetcher.closes == 1
            assert core._fetchers_local.cache == {}
            assert core._app_fetchers == []
        finally:
            core._app_fetchers.clear()
            core._fetchers_local.cache = {}

    def test_import_dynamic_fetcher_fallback_after_first_import_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        import web_crawler

        expected = web_crawler.DynamicFetcher
        real_import = builtins.__import__
        state = {"calls": 0}

        def flaky(
            name: str,
            globals_: dict | None = None,
            locals_: dict | None = None,
            fromlist: tuple[str, ...] = (),
            level: int = 0,
        ) -> object:
            if name == "web_crawler" and "DynamicFetcher" in fromlist and state["calls"] == 0:
                state["calls"] += 1
                raise ImportError("simulated missing package")
            return real_import(name, globals_, locals_, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", flaky)
        assert core._import_dynamic_fetcher() is expected

    def test_render_instances_from_multiple_workers_are_all_closed(self) -> None:
        class _FakeRender:
            instances: list[_FakeRender] = []

            def __init__(self, **kwargs: object) -> None:
                self.closed = False
                _FakeRender.instances.append(self)

            def fetch(self, url: str) -> Mock:
                return Mock(
                    status=200, content=b"<html>r</html>", headers={"content-type": "text/html"}
                )

            def close(self) -> None:
                self.closed = True

        core._render_registry.clear()
        core._render_local.cache = {}
        try:
            with patch.object(core, "_import_dynamic_fetcher", return_value=_FakeRender):
                threads = [
                    threading.Thread(target=core.render_page, args=("https://example.com", 30, {}))
                    for _ in range(3)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            assert len(_FakeRender.instances) == 3
            core.close_render_fetchers()
            assert all(instance.closed for instance in _FakeRender.instances)
            assert core._render_registry == []
        finally:
            core._render_registry.clear()
            core._render_local.cache = {}


class TestFetchBranchCoverage:
    """fetch 的条件请求 / 429 / 无状态错误 / 中止探针 / power mode 分支。"""

    @staticmethod
    def _resp(status: int, body: bytes, headers: dict[str, str]) -> Mock:
        return Mock(status=status, content=body, headers=headers)

    def test_memory_304_without_rate_limiter(self) -> None:
        fake = Mock()
        fake.get.return_value = self._resp(304, b"", {"etag": "e1"})
        meta: dict[str, object] = {}
        with patch.object(core, "_get_app_fetcher", return_value=fake):
            data, ctype = core.fetch(
                "https://example.com",
                30,
                {},
                0,
                None,
                allow_not_modified=True,
                response_meta=meta,
            )
        assert (data, ctype) == (b"", "")
        assert meta["status"] == 304
        assert meta["etag"] == "e1"

    def test_streaming_304_is_treated_as_not_modified(self, tmp_path: Path) -> None:
        exc = FetchError("HTTP Error 304: download failed")
        exc.status = 304  # type: ignore[attr-defined]
        fake = Mock()
        fake.download.side_effect = exc
        meta: dict[str, object] = {}
        limiter = Mock()
        with patch.object(core, "_get_app_fetcher", return_value=fake):
            data, ctype = core.fetch(
                "https://example.com/f.bin",
                30,
                {},
                0,
                None,
                resume_path=tmp_path / "f.bin",
                allow_not_modified=True,
                response_meta=meta,
                rate_limiter=limiter,
            )
        assert (data, ctype) == (b"", "")
        assert meta["status"] == 304
        limiter.record_request.assert_called_once()

    def test_fetch_error_without_status_is_retried(self) -> None:
        fake = Mock()
        fake.get.side_effect = [FetchError("socket boom"), self._resp(200, b"ok", {})]
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            patch.object(core.time, "sleep") as sleep,
        ):
            data, _ctype = core.fetch("https://example.com", 30, {}, 1, None)
        assert data == b"ok"
        assert sleep.called

    def test_429_without_retry_budget_raises(self) -> None:
        fake = Mock()
        fake.get.return_value = self._resp(429, b"", {"Retry-After": "1"})
        limiter = Mock()
        limiter.handle_429.return_value = 1
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            patch.object(core.time, "sleep") as sleep,
            pytest.raises(FetchError, match="429"),
        ):
            core.fetch("https://example.com", 30, {}, 0, None, rate_limiter=limiter)
        sleep.assert_not_called()

    def test_streaming_meta_and_abort_probe(self, tmp_path: Path) -> None:
        control = Mock()
        control.stealth = False
        control.impersonate = None
        control.proxy = None
        control.should_stop = Mock(return_value=False)
        control.wait_if_paused = Mock()
        probe: dict[str, bool] = {}

        def fake_download(url: str, dest: Path, **kwargs: object) -> Path:
            kwargs["on_headers"](200, {"content-type": "image/png"})  # type: ignore[operator]
            probe["abort"] = kwargs["should_abort"]()  # type: ignore[operator]
            return dest

        fake = Mock()
        fake.download.side_effect = fake_download
        meta: dict[str, object] = {}
        with patch.object(core, "_get_app_fetcher", return_value=fake):
            core.fetch(
                "https://example.com/f.bin",
                30,
                {},
                0,
                None,
                resume_path=tmp_path / "f.bin",
                control_args=control,
                meta=meta,
            )
        assert meta == {"status": 200, "content_type": "image/png"}
        assert probe["abort"] is False
        control.wait_if_paused.assert_called_once()
        control.should_stop.assert_called_once()

    def test_power_mode_skips_host_validation(self) -> None:
        fake = Mock()
        fake.get.return_value = self._resp(200, b"ok", {})
        with (
            patch.object(core, "is_power_mode", return_value=True),
            patch.object(core, "validate_url_host") as validate,
            patch.object(core, "_get_app_fetcher", return_value=fake),
        ):
            assert core.fetch("https://example.com", 30, {}, 0, None) == (b"ok", "")
        validate.assert_not_called()

    def test_header_value_fallbacks(self) -> None:
        class _NoGet:
            def items(self) -> list[tuple[str, str]]:
                return [("Content-Type", "text/html")]

        class _BadGet:
            def get(self, name: str) -> None:
                raise RuntimeError("boom")

            def items(self) -> list[tuple[str, str]]:
                return [("Content-Type", "text/html")]

        class _BadItems:
            def get(self, name: str) -> None:
                return None

            def items(self) -> list[tuple[str, str]]:
                raise RuntimeError("boom")

        assert core._header_value(_NoGet(), "Content-Type") == "text/html"
        assert core._header_value(_BadGet(), "content-type") == "text/html"
        assert core._header_value(_BadItems(), "content-type") == ""

    def test_header_value_mapping_without_match(self) -> None:
        class _NoMatch:
            def get(self, name: str) -> None:
                return None

            def items(self) -> list[tuple[str, str]]:
                return [("X-Other", "v")]

        assert core._header_value(_NoMatch(), "content-type") == ""

    def test_streaming_304_without_rate_limiter(self, tmp_path: Path) -> None:
        exc = FetchError("HTTP Error 304: download failed")
        exc.status = 304  # type: ignore[attr-defined]
        fake = Mock()
        fake.download.side_effect = exc
        meta: dict[str, object] = {}
        with patch.object(core, "_get_app_fetcher", return_value=fake):
            assert core.fetch(
                "https://example.com/f.bin",
                30,
                {},
                0,
                None,
                resume_path=tmp_path / "f.bin",
                allow_not_modified=True,
                response_meta=meta,
            ) == (b"", "")
        assert meta["status"] == 304

    def test_fetch_error_without_status_and_no_retry_raises(self) -> None:
        fake = Mock()
        fake.get.side_effect = FetchError("socket boom")
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            patch.object(core.time, "sleep") as sleep,
            pytest.raises(FetchError, match="socket boom"),
        ):
            core.fetch("https://example.com", 30, {}, 0, None)
        sleep.assert_not_called()


class TestCoreLoggingAndHeaders:
    """日志 handler 幂等挂载 / 请求头 Referer 保留。"""

    def test_attach_and_detach_log_handler_are_idempotent(self) -> None:
        handler = logging.Handler()
        try:
            core.attach_log_handler(handler)
            core.attach_log_handler(handler)
            assert core._log.handlers.count(handler) == 1
        finally:
            core.detach_log_handler(handler)
            core.detach_log_handler(handler)
        assert handler not in core._log.handlers

    def test_request_headers_keep_explicit_referer(self) -> None:
        headers = core._request_headers(
            "https://example.com/page", {"User-Agent": "UA", "Referer": "https://r/"}
        )
        assert headers["User-Agent"] == "UA"
        assert headers["Referer"] == "https://r/"

    def test_dynamic_fetcher_unavailable_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with (
            patch.dict(sys.modules, {"web_crawler": None}),
            pytest.raises(FetchError, match="DynamicFetcher"),
        ):
            core._import_dynamic_fetcher()


class TestCrawlEntryFailurePaths:
    """crawl 入口的降级：无效 URL / 校验器落盘失败 / 配置保存失败。"""

    def test_invalid_seed_url_marks_page_scan_failed(self, tmp_path: Path) -> None:
        args = crawler_app.build_parser().parse_args(
            ["--url", "not a url", "--out", str(tmp_path), "--list-only"]
        )
        assert crawler_app.crawl(args) != 0
        report = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))
        assert report["page_scan"]["failed"] == 1

    def test_validator_cache_save_failure_is_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        html = b"<html><body>ok</body></html>"
        with (
            patch.object(core, "fetch", return_value=(html, "text/html")),
            patch.object(crawler_app.ValidatorCache, "save", side_effect=OSError("disk full")),
            caplog.at_level(logging.WARNING, logger="crawler"),
        ):
            args = crawler_app.build_parser().parse_args(
                ["--url", "https://example.com", "--out", str(tmp_path), "--list-only", "--cache"]
            )
            assert crawler_app.crawl(args) == 0
        assert "failed to save validator cache" in caplog.text

    def test_save_config_failure_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            crawler_app,
            "save_config_to_file",
            Mock(side_effect=OSError("read-only")),
        )
        monkeypatch.setattr(
            sys,
            "argv",
            ["web-crawler", "--url", "https://example.com", "--save-config", "out.json"],
        )
        with pytest.raises(SystemExit) as excinfo:
            crawler_app.main()
        assert excinfo.value.code == 2
        assert "read-only" in capsys.readouterr().err

    def test_main_json_mode_reports_bad_args_as_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["web-crawler", "--json", "--workers", "abc"])
        with pytest.raises(SystemExit) as excinfo:
            crawler_app.main()
        assert excinfo.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"

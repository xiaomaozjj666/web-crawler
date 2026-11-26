"""边界分支回归：正文抽取、离线验收、编码与状态辅助。"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tinycss2 import parse_stylesheet

from web_crawler.app import _crawler_post as post
from web_crawler.app import _crawler_scan as scan
from web_crawler.app import _offline_verify as ov
from web_crawler.app import _report_extract as rx
from web_crawler.app._crawler_cli import (
    _non_negative_float,
    _non_negative_int,
    _positive_int,
    _regex_arg,
)
from web_crawler.app._crawler_context import _CrawlContext, load_config_from_file, reserve_page_path
from web_crawler.app._crawler_scan import _resolved_encoding
from web_crawler.app.crawler_models import Resource
from web_crawler.exceptions import ConfigError


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
        assert _resolved_encoding(None, "text/html") == "utf-8"

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

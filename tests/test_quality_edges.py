"""边界分支回归：正文抽取、离线验收、编码与状态辅助。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from tinycss2 import parse_stylesheet

from web_crawler.app import _offline_verify as ov
from web_crawler.app import _report_extract as rx
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

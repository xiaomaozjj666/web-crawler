from __future__ import annotations

import json
import runpy
from pathlib import Path
from unittest.mock import patch

import pytest

from web_crawler.app import _crawler_core as core
from web_crawler.app import crawler, startproject
from web_crawler.exceptions import ConfigError
from web_crawler.parser import ExtractionRules
from web_crawler.parser.rules import MAX_RULES_BYTES, parse_html
from web_crawler.response import Response

URL = "https://example.com/article"
HTML = '<title>示例 &amp; 标题</title><h1>Article</h1><a href="/one">One</a><a href="/two">Two</a>'
RULES = {
    "version": 1,
    "fields": {
        "title": {"css": "title", "required": True},
        "links": {"css": "a::attr(href)", "many": True},
        "heading": {"xpath": "//h1/text()"},
        "subtitle": {"xpath": "//h2/text()", "required": True},
    },
}
RESULT = {
    "page_url": URL,
    "item": {
        "title": "示例 & 标题",
        "links": ["/one", "/two"],
        "heading": "Article",
        "subtitle": None,
    },
    "missing_required": ["subtitle"],
}


@pytest.fixture
def rule_file(tmp_path: Path) -> Path:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(RULES, ensure_ascii=False), encoding="utf-8-sig")
    return path


@pytest.mark.parametrize(
    "invalid",
    [
        {"version": True, "fields": RULES["fields"]},
        {"version": 1, "fields": {"title": {"css": "["}}},
        {"version": 1, "fields": {"title": {"xpath": "unknown-function()"}}},
        {"version": 1, "fields": {"title": {"css": "title", "many": "false"}}},
        {"version": 1, "fields": {"title": {"css": "title", "xpath": "//title"}}},
    ],
)
def test_invalid_rules_fail_before_running(invalid: object) -> None:
    with pytest.raises(ConfigError):
        ExtractionRules.from_dict(invalid)


def test_evaluation_errors_name_the_failing_field() -> None:
    rules = ExtractionRules.from_dict(
        {"version": 1, "fields": {"main": {"xpath": "//main[$flag]"}}}
    )
    with pytest.raises(ConfigError, match="main"):
        rules.extract("<main>Example</main>")


def test_large_valid_rules_survive_pretty_print_and_reload(tmp_path: Path) -> None:
    spec = {"version": 1, "fields": {f"f{i}": {"xpath": "'" + "x" * 975 + "'"} for i in range(64)}}
    rules = ExtractionRules.from_dict(spec)
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules.to_dict(), indent=2), encoding="utf-8")
    assert path.stat().st_size > 64000
    assert ExtractionRules.from_file(path).to_dict() == rules.to_dict()


def test_cli_sample_and_batch_use_the_same_rules(
    tmp_path: Path,
    rule_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sample = tmp_path / "sample.html"
    sample.write_text(HTML, encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["web-crawler", "--rules", str(rule_file), "--sample", str(sample), "--url", URL],
    )
    with patch.object(core, "fetch") as fetch:
        crawler.main()
    fetch.assert_not_called()
    assert json.loads(capsys.readouterr().out) == RESULT

    batch = tmp_path / "batch"
    monkeypatch.setattr(
        "sys.argv",
        ["web-crawler", "--url", URL, "--rules", str(rule_file), "--out", str(batch), "--json"],
    )
    with (
        patch.object(core, "fetch", return_value=(HTML.encode(), "text/html; charset=utf-8")),
        pytest.raises(SystemExit) as result,
    ):
        crawler.main()
    assert result.value.code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "partial"
    assert summary["rule_results"] == {"pages": 1, "pages_missing_required": 1, "errors": 0}
    report = json.loads((batch / "run_report.json").read_text(encoding="utf-8"))
    assert report["rule_results"] == summary["rule_results"]
    assert report["status"] == "partial"
    assert json.loads((batch / "rule_items.jsonl").read_text(encoding="utf-8")) == RESULT


def test_generated_spider_consumes_the_exported_rules(
    tmp_path: Path, rule_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert startproject.main(["demo_fields", "--rules", str(rule_file), "--quiet"]) == 0
    project = tmp_path / "demo_fields"
    rules = ExtractionRules.from_file(project / "field-rules.json")
    spider_class = runpy.run_path(str(project / "main.py"))["FieldSpider"]

    class FixtureFetcher:
        def get(self, url: str, **kwargs: object) -> Response:
            return Response(url, 200, HTML.encode("gb18030"), encoding="gb18030")

    feed = project / "items.jsonl"
    spider = spider_class([URL], rules, fetcher=FixtureFetcher(), feed_output=feed)
    # Rules are exercised without a network dependency on the demo host.
    spider.respect_robots = False
    assert spider.run() == [RESULT]
    assert json.loads(feed.read_text(encoding="utf-8")) == RESULT


def test_config_can_embed_rules_and_cli_can_override_with_default_value(
    tmp_path: Path, rule_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.json"
    args = crawler.build_parser().parse_args(
        ["--url", URL, "--rules", str(rule_file), "--workers", "3"]
    )
    crawler.save_config_to_file(args, str(config))
    monkeypatch.setattr("sys.argv", ["web-crawler", "--load-config", str(config), "--workers", "8"])
    with patch.object(crawler, "crawl", return_value=0) as run, pytest.raises(SystemExit):
        crawler.main()
    loaded = run.call_args.args[0]
    assert loaded.workers == 8
    assert ExtractionRules.from_dict(loaded.field_rules).extract(HTML, page_url=URL) == RESULT


def test_parse_html_accepts_xml_declaration_and_recovers_from_empty_source() -> None:
    rules = ExtractionRules.from_dict({"version": 1, "fields": {"title": {"css": "title"}}})
    declared = '<?xml version="1.0"?><html><head><title>XML</title></head></html>'
    assert rules.extract(declared)["item"]["title"] == "XML"
    assert parse_html("").element.tag == "html"


def test_xpath_scalar_and_comment_nodes_are_normalized() -> None:
    rules = ExtractionRules.from_dict(
        {
            "version": 1,
            "fields": {
                "heading": {"xpath": "string(//h1)"},
                "comments": {"xpath": "//comment()", "many": True},
            },
        }
    )
    result = rules.extract("<html><body><!-- ignored --><h1>Article</h1></body></html>")
    assert result["item"] == {"heading": "Article", "comments": []}


def test_rule_json_and_file_size_limits_are_enforced(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="128 KiB"):
        ExtractionRules.from_json(" " * (MAX_RULES_BYTES + 1))
    with pytest.raises(ConfigError, match="JSON 无效"):
        ExtractionRules.from_json("{")

    too_large = tmp_path / "too-large.json"
    too_large.write_bytes(b" " * (MAX_RULES_BYTES + 1))
    with pytest.raises(ConfigError, match="128 KiB"):
        ExtractionRules.from_file(too_large)


def test_canonical_rule_payload_is_capped_at_64_kib() -> None:
    spec = {
        "version": 1,
        "fields": {f"f{i}": {"xpath": "'" + "x" * 1020 + "'"} for i in range(64)},
    }
    with pytest.raises(ConfigError, match="内容最大为 64 KiB"):
        ExtractionRules.from_dict(spec)


@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        ({}, "version 和 fields"),
        ({"version": 1, "fields": []}, "1–64"),
        ({"version": 1, "fields": {}}, "1–64"),
        ({"version": 1, "fields": {"": {"css": "title"}}}, "字段名"),
        ({"version": 1, "fields": {"title": {"css": "title", "extra": 1}}}, "未知选项"),
        ({"version": 1, "fields": {"title": {}}}, "需要一个 CSS 或 XPath"),
        ({"version": 1, "fields": {"title": {"css": ""}}}, "选择器长度"),
        ({"version": 1, "fields": {"title": {"css": "title", "required": "true"}}}, "布尔值"),
    ],
)
def test_rule_schema_rejects_invalid_shapes(invalid: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        ExtractionRules.from_dict(invalid)


def test_extract_response_uses_response_text_and_url() -> None:
    rules = ExtractionRules.from_dict(RULES)
    response = Response(URL, 200, HTML.encode("utf-8"), headers={"Content-Type": "text/html"})
    assert rules.extract_response(response) == RESULT

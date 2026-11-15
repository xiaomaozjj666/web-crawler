"""Direct coverage for static replay compilation helpers and item pipelines."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from lxml import html as lxml_html

from web_crawler.ai.recorder import ActionRecord
from web_crawler.exceptions import ConfigError
from web_crawler.parser.adaptive import compute_fingerprint
from web_crawler.response import Response
from web_crawler.spider.spider import DropItem, Spider
from web_crawler.spider.static_replay import (
    ContractPipeline,
    IdempotentFeedPipeline,
    SelectorHealthStore,
    StaticReplayCompiler,
    _coerce,
    _field_value,
    _probability,
    _redact_text,
    _redact_url,
    _sanitize_headers,
    _sanitize_response,
    compile_static_replay,
    extract_static_items,
)

_URL = "https://fixture.local/items"
_HTML = '<section id="item"><span class="value" data-id="v1">42</span></section>'


def _response(body: str = _HTML, url: str = _URL) -> Response:
    return Response(url, 200, f"<html><body>{body}</body></html>".encode())


def _plan(response: Response, *, threshold: float = 0.5) -> dict[str, Any]:
    root = response.selector.css("#item")[0].element
    field = root.cssselect(".value")[0]
    return {
        "selector": "#item",
        "root_fingerprint": compute_fingerprint(root),
        "fields": {
            "value": {
                "selector": ".value",
                "attribute": "text",
                "type": "str",
                "required": True,
                "fingerprint": compute_fingerprint(field),
            },
        },
        "confidence": 0.95,
        "min_confidence": 0.8,
        "adaptive_threshold": threshold,
    }


def _valid_response() -> dict[str, Any]:
    body = f"<html><body>{_HTML}</body></html>".encode()
    return {
        "url": _URL,
        "status": 200,
        "headers": {"Content-Type": "text/html"},
        "body_base64": base64.b64encode(body).decode("ascii"),
    }


def _records(
    *,
    nav_params: dict[str, Any] | None = None,
    extract_params: dict[str, Any] | None = None,
    response: Any = ...,
    body: str = _HTML,
) -> list[ActionRecord]:
    nav: dict[str, Any] = {"url": _URL, "response": _valid_response()}
    nav.update(nav_params or {})
    if response is not ...:
        nav["response"] = response
    if body != _HTML:
        fresh = _valid_response()
        fresh["body_base64"] = base64.b64encode(
            f"<html><body>{body}</body></html>".encode()
        ).decode("ascii")
        nav["response"] = fresh
    extract: dict[str, Any] = {
        "selector": "#item",
        "fields": {"value": {"selector": ".value", "type": "str"}},
        "confidence": 0.9,
        "min_confidence": 0.8,
        "adaptive_threshold": 0.5,
    }
    extract.update(extract_params or {})
    return [
        ActionRecord(step=1, action_type="navigate", params=nav),
        ActionRecord(step=2, action_type="extract", params=extract),
    ]


def test_sanitizers_and_value_helpers() -> None:
    assert _redact_text("a=secret&b=secret", ["secret"]) == "a=[REDACTED]&b=[REDACTED]"
    redacted = _redact_url("https://x.test/p?token=secret&q=secret", ["secret"])
    assert "secret" not in redacted
    assert "token=%5BREDACTED%5D" in redacted
    assert _sanitize_headers(None, ["secret"]) == {}
    assert _sanitize_headers({"Authorization": "secret", "X-Note": "secret"}, ["secret"]) == {
        "Authorization": "[REDACTED]",
        "X-Note": "[REDACTED]",
    }
    with pytest.raises(ConfigError, match="must be a mapping"):
        _sanitize_headers([], [])

    body, status = _sanitize_response(
        {
            "body_base64": base64.b64encode(b"token=secret").decode("ascii"),
            "status": "201",
            "url": _URL,
        },
        _URL,
        ["secret"],
    )
    assert body == b"token=[REDACTED]"
    assert status == 201
    for invalid in (
        [],
        {"status": 200},
        {"body_base64": "!!!", "status": 200},
        {"body_base64": base64.b64encode(b"x").decode(), "status": "bad"},
        {"body_base64": base64.b64encode(b"x").decode(), "status": 404},
    ):
        with pytest.raises(ConfigError):
            _sanitize_response(invalid, _URL, [])

    assert _probability("0.25") == 0.25
    for bad in (None, "bad", -0.1, 1.1):
        with pytest.raises(ConfigError):
            _probability(bad)
    element = lxml_html.fromstring('<span data-id="v1"> 42  text </span>')
    assert _field_value(element, "text") == "42 text"
    assert _field_value(element, "data-id") == "v1"


def test_coercion_covers_scalar_types() -> None:
    assert _coerce("7", "int") == 7
    assert _coerce(True, "int") is True
    assert _coerce("1.5", "float") == 1.5
    assert _coerce(True, "float") is True
    assert _coerce(True, "bool") is True
    assert _coerce("TRUE", "bool") is True
    assert _coerce("1", "bool") is True
    assert _coerce("false", "bool") is False
    assert _coerce("0", "bool") is False
    assert _coerce("raw", "str") == "raw"
    with pytest.raises(ValueError):
        _coerce("maybe", "bool")


def test_extract_static_items_records_selector_health(tmp_path: Path) -> None:
    response = _response()
    plan = _plan(response)
    health_output = tmp_path / "nested" / "selector-health.jsonl"
    items = extract_static_items(response, plan, health_output=health_output)
    assert items[0]["value"] == "42"
    assert items[0]["_confidence"] == 0.95
    assert items[0]["_min_confidence"] == 0.8

    drifted_plan = _plan(response, threshold=0.1)
    drifted = _response('<section id="renamed"><span class="renamed">42</span></section>')
    drifted_items = extract_static_items(drifted, drifted_plan, health_output=health_output)
    assert drifted_items[0]["value"] == "42"
    assert 0.0 < drifted_items[0]["_confidence"] <= 1.0

    events = [json.loads(line) for line in health_output.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 2
    assert events[0]["root"] == {"source": "css", "score": 1.0}
    assert events[0]["fields"]["value"]["source"] == "css"
    assert events[1]["root"]["source"] == "fingerprint"
    assert events[1]["fields"]["value"]["source"] == "fingerprint"
    assert 0.0 < events[1]["confidence"] <= 1.0

    missing = _plan(response)
    missing["selector"] = "#missing"
    missing["root_fingerprint"] = compute_fingerprint(
        lxml_html.fromstring('<aside class="unrelated">other</aside>')
    )
    missing["adaptive_threshold"] = 1.0
    assert extract_static_items(response, missing, health_output=health_output) == [
        {"url": _URL, "_confidence": 0.0, "_min_confidence": 0.8}
    ]
    events = [json.loads(line) for line in health_output.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 3
    assert events[2]["root"]["source"] == "none"
    assert events[2]["fields"] == {}
    assert events[2]["confidence"] == 0.0


def test_selector_health_store_deduplicates_distinct_events(tmp_path: Path) -> None:
    output = tmp_path / "selector-health.jsonl"
    event = {"url": _URL, "root": {"source": "css", "score": 1.0}, "fields": {}}
    assert SelectorHealthStore(output).record(event) is True
    assert SelectorHealthStore(output).record(event) is False
    assert SelectorHealthStore(output).record({**event, "confidence": 0.5}) is True
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2


def test_contract_pipeline_coerces_quarantines_and_applies_defaults(tmp_path: Path) -> None:
    quarantine = tmp_path / "quarantine.jsonl"
    pipeline = ContractPipeline(quarantine)
    spider = Spider()

    valid = {
        "url": _URL,
        "value": "7",
        "_confidence": 1.0,
        "_min_confidence": 0.8,
        "_field_specs": {"value": {"type": "int", "required": True}},
    }
    result = pipeline.process_item(valid, spider)
    assert result["value"] == 7
    assert "_min_confidence" not in result
    assert "_field_specs" not in result

    with pytest.raises(DropItem):
        pipeline.process_item({"_confidence": 0.1, "_min_confidence": 0.8}, spider)
    with pytest.raises(DropItem):
        pipeline.process_item(["not", "a", "mapping"], spider)
    with pytest.raises(DropItem):
        pipeline.process_item(
            {
                "value": "bad",
                "_confidence": 1.0,
                "_min_confidence": 0.8,
                "_field_specs": {"value": {"type": "int", "required": True}},
            },
            spider,
        )
    defaulted = pipeline.process_item(
        {
            "_confidence": 1.0,
            "_min_confidence": 0.8,
            "_field_specs": {"value": {"type": "int", "required": False, "default": 5}},
        },
        spider,
    )
    assert isinstance(defaulted, dict)
    lines = [json.loads(line) for line in quarantine.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["reasons"] == ["low_confidence"]
    assert lines[1]["reasons"] == ["item_type"]
    assert "期望类型 int" in lines[2]["reasons"][0]


def test_idempotent_feed_pipeline_reloads_seen_keys(tmp_path: Path) -> None:
    output = tmp_path / "items.jsonl"
    spider = Spider()
    pipeline = IdempotentFeedPipeline(output)
    first = pipeline.process_item({"value": 1}, spider)
    assert first["_dedup_key"]
    with pytest.raises(DropItem):
        pipeline.process_item({"value": 1}, spider)
    assert pipeline.process_item("not a mapping", spider) == "not a mapping"

    reloaded = IdempotentFeedPipeline(output)
    with pytest.raises(DropItem):
        reloaded.process_item({"value": 1}, spider)
    reloaded.process_item({"value": 2}, spider)
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2


def test_compile_rejects_unsupported_or_ambiguous_records(tmp_path: Path) -> None:
    compiler = StaticReplayCompiler()
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be empty"):
        compiler.compile(_records(), occupied)

    valid_response = _valid_response()
    cases: list[list[ActionRecord]] = [
        _records(nav_params={"method": "POST"}),
        _records(nav_params={"headers": {"X": "1"}}),
        _records(nav_params={"body": "x"}),
        _records(response=[]),
        _records(nav_params={"url": ""}),
        _records(response={**valid_response, "url": "https://other.test"}),
        _records(response={**valid_response, "body_base64": "!!!"}),
        _records(response={**valid_response, "status": "bad"}),
        _records(response={**valid_response, "status": 404}),
        _records(response={**valid_response, "headers": []}),
        _records(extract_params={"source_step": 99}),
        _records(extract_params={"selector": ""}),
        _records(extract_params={"fields": None}),
        _records(extract_params={"fields": {"value": "bad"}}),
        _records(extract_params={"fields": {"value": {"selector": ".value", "type": "bytes"}}}),
        _records(extract_params={"fields": {"value": {"selector": ".missing", "type": "str"}}}),
        _records(extract_params={"selector": "["}),
        _records(extract_params={"confidence": "bad"}),
        _records(extract_params={"confidence": 2}),
        _records(body='<div id="item"><b>1</b></div><div id="item"><b>2</b></div>'),
        [ActionRecord(step=1, action_type="click", params={})],
        [_records()[1]],
        [_records()[0]],
        [ActionRecord(step=1, action_type="done", params={})],
    ]
    for index, records in enumerate(cases):
        with pytest.raises(ConfigError):
            compiler.compile(records, tmp_path / f"case-{index}")

    duplicate = _records()
    with pytest.raises(ConfigError, match="duplicate cached URL"):
        compiler.compile([duplicate[0], duplicate[0], duplicate[1]], tmp_path / "duplicate")


def test_compile_writes_project_with_defaulted_field_and_redaction(tmp_path: Path) -> None:
    records = _records(
        extract_params={
            "fields": {
                "value": {"selector": ".value", "type": "str", "default": "missing"},
            },
        },
    )
    target = compile_static_replay(records, tmp_path / "project", secrets=["42"])
    replay = json.loads((target / "replay.json").read_text(encoding="utf-8"))
    assert replay["version"] == 1
    assert replay["plans"][0]["extract"][0]["fields"]["value"]["default"] == "missing"
    assert b"42" not in next((target / "cache").glob("*.body")).read_bytes()
    assert (target / "spider.py").is_file()

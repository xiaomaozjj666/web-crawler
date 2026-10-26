"""End-to-end tests for deterministic static trace compilation and offline replay."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest

from web_crawler.ai.recorder import ActionRecord
from web_crawler.exceptions import ConfigError
from web_crawler.spider.static_replay import compile_static_replay


def _records(
    html: str, *, url: str = "https://fixture.local/items", field_type: str = "int"
) -> list[ActionRecord]:
    body = f"<html><body>{html}</body></html>".encode()
    return [
        ActionRecord(
            step=1,
            action_type="navigate",
            params={
                "url": url,
                "response": {
                    "url": url,
                    "status": 200,
                    "headers": {"Content-Type": "text/html"},
                    "body_base64": base64.b64encode(body).decode("ascii"),
                },
            },
        ),
        ActionRecord(
            step=2,
            action_type="extract",
            params={
                "selector": "#item",
                "fields": {"value": {"selector": ".value", "type": field_type}},
                "confidence": 0.95,
                "min_confidence": 0.8,
                "adaptive_threshold": 0.5,
            },
        ),
    ]


def _run(project: Path) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(project / "spider.py")],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_static_trace_replays_offline_and_repeat_is_idempotent(tmp_path: Path) -> None:
    project = compile_static_replay(
        _records('<section id="item"><span class="value">42</span></section>'),
        tmp_path / "project",
    )

    first = _run(project)
    feed_before = (project / "items.jsonl").read_bytes()
    second = _run(project)

    assert len(first["items"]) == 1
    assert first["items"][0]["url"] == "https://fixture.local/items"
    assert first["items"][0]["value"] == 42
    assert json.loads(feed_before) == first["items"][0]
    assert second == {"items": []}
    assert (project / "items.jsonl").read_bytes() == feed_before


def test_drift_uses_fingerprint_then_contract(tmp_path: Path) -> None:
    project = compile_static_replay(
        _records('<section id="item"><span class="value">42</span></section>'),
        tmp_path / "project",
    )
    # Force replay against a changed fixture without using the network.
    drifted = (
        b'<html><body><section id="renamed"><span class="renamed">42</span></section></body></html>'
    )
    meta_path = next((project / "cache").glob("*.meta.json"))
    body_path = meta_path.with_suffix("").with_suffix(".body")
    body_path.write_bytes(zlib.compress(drifted))

    result = _run(project)

    assert result["items"][0]["value"] == 42
    assert result["items"][0]["_confidence"] >= 0.8


def test_missing_and_wrong_type_are_quarantined_not_exported(tmp_path: Path) -> None:
    missing_project = compile_static_replay(
        _records('<section id="item"><span class="value">42</span></section>'),
        tmp_path / "missing",
    )
    missing_body = next((missing_project / "cache").glob("*.body"))
    missing_body.write_bytes(
        zlib.compress(b"<html><body><section id='item'></section></body></html>")
    )
    assert _run(missing_project) == {"items": []}
    missing_reason = json.loads((missing_project / "quarantine.jsonl").read_text(encoding="utf-8"))[
        "reasons"
    ][0]
    assert "缺失必填字段" in missing_reason

    wrong_project = compile_static_replay(
        _records('<section id="item"><span class="value">not-an-int</span></section>'),
        tmp_path / "wrong-type",
    )
    assert _run(wrong_project) == {"items": []}
    wrong_reason = json.loads((wrong_project / "quarantine.jsonl").read_text(encoding="utf-8"))[
        "reasons"
    ][0]
    assert "期望类型 int" in wrong_reason


def test_low_confidence_drift_is_quarantined(tmp_path: Path) -> None:
    project = compile_static_replay(
        _records('<section id="item"><span class="value">42</span></section>'),
        tmp_path / "project",
    )
    body_path = next((project / "cache").glob("*.body"))
    body_path.write_bytes(
        zlib.compress(b"<html><body><article><em>unknown</em></article></body></html>")
    )

    assert _run(project) == {"items": []}
    quarantined = json.loads((project / "quarantine.jsonl").read_text(encoding="utf-8"))
    assert quarantined["reasons"] == ["low_confidence"]


def test_unreplayable_action_fails_at_compile_time(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not statically replayable"):
        compile_static_replay([ActionRecord(step=1, action_type="analyze_js")], tmp_path / "bad")
    with pytest.raises(ConfigError, match="no statically replayable extraction"):
        compile_static_replay(_records("<section id='item'></section>")[:1], tmp_path / "empty")
    with pytest.raises(ConfigError, match="duplicate cached URL"):
        records = _records('<section id="item"><span class="value">42</span></section>')
        compile_static_replay([records[0], records[0], records[1]], tmp_path / "duplicate")
    assert not (tmp_path / "bad").exists()


def test_secret_values_are_redacted_from_project_and_output(tmp_path: Path) -> None:
    secret = "super-secret-value"
    url = f"https://fixture.local/items?token={secret}"
    project = compile_static_replay(
        _records(
            f'<section id="item"><span class="value">{secret}</span></section>',
            url=url,
            field_type="str",
        ),
        tmp_path / "project",
        secrets=[secret],
    )
    # Change the sensitive query value into a non-secret placeholder as part of compilation.
    for meta_path in (project / "cache").glob("*.meta.json"):
        meta_path.write_text(
            meta_path.read_text(encoding="utf-8").replace(
                "token=super-secret-value", "token=redacted"
            ),
            encoding="utf-8",
        )

    result = _run(project)

    assert result["items"]
    assert secret not in json.dumps(result, ensure_ascii=False)
    for path in project.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
    for body_path in (project / "cache").glob("*.body"):
        assert secret.encode() not in zlib.decompress(body_path.read_bytes())

"""统一 CLI 输出、进度与结构化摘要的行为测试。"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from web_crawler._cli import (
    ProgressReporter,
    add_output_arguments,
    classify_exception,
    crawl_summary,
    emit_crawl_summary,
    emit_error,
    output_mode,
    render_error,
)
from web_crawler.app import _crawler_core as core
from web_crawler.app import crawler as cr
from web_crawler.app.crawler_models import ManifestRow
from web_crawler.exceptions import ConfigError, FetchError, ParseError, UnsafeTargetError


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def _row(status: str, byte_count: int = 0) -> ManifestRow:
    return ManifestRow(status, "https://example.com/a", "", "", byte_count, "", "", "", "", "", "")


def _crawl_args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    return cr.build_parser().parse_args(
        ["--url", "https://example.com", "--out", str(tmp_path), "--delay", "0", *extra]
    )


def test_output_arguments_are_mutually_exclusive() -> None:
    parser = argparse.ArgumentParser()
    add_output_arguments(parser)
    assert output_mode(parser.parse_args([])) == "normal"
    assert output_mode(parser.parse_args(["--quiet"])) == "quiet"
    assert output_mode(parser.parse_args(["--verbose"])) == "verbose"
    assert output_mode(parser.parse_args(["--json"])) == "json"
    with pytest.raises(SystemExit):
        parser.parse_args(["--quiet", "--json"])


def test_error_classification_has_actionable_hints() -> None:
    assert classify_exception(FetchError("connection refused")).category == "network"
    assert classify_exception(ConfigError("bad option")).category == "configuration"
    assert classify_exception(ParseError("bad css")).category == "parse"
    assert classify_exception(UnsafeTargetError("private host")).category == "security"
    rendered = render_error(FetchError("timeout"), context="https://example.com")
    assert "[网络错误]" in rendered
    assert "下一步" in rendered
    assert "--retries" in rendered


def test_json_error_is_single_machine_readable_document(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = argparse.Namespace(json_output=True)
    emit_error(ConfigError("配置缺失"), args, context="crawler.json")
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.out.count("\n") == 1
    assert captured.err == ""
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "configuration"
    assert payload["error"]["context"] == "crawler.json"
    assert payload["error"]["hint"]


def test_tty_progress_refreshes_with_rate_failures_and_bytes() -> None:
    stream = _TTYBuffer()
    now = [0.0]
    reporter = ProgressReporter(stream=stream, clock=lambda: now[0])
    reporter.update({"phase": "resources", "processed_resources": 0, "total_resources": 2})
    now[0] = 0.2
    reporter.update(
        {
            "phase": "download",
            "processed_resources": 1,
            "total_resources": 2,
            "failed_resources": 1,
            "bytes_downloaded": 2048,
        }
    )
    now[0] = 0.4
    reporter.update(
        {
            "phase": "download",
            "processed_resources": 2,
            "total_resources": 2,
            "failed_resources": 1,
            "bytes_downloaded": 2048,
        }
    )
    reporter.close()
    output = stream.getvalue()
    assert "\r\x1b[K" in output
    assert "处理资源 2/2" in output
    assert "失败 1" in output
    assert "2.0 KiB" in output
    assert "项/s" in output
    assert output.endswith("\n")


def test_non_tty_progress_is_periodic_not_per_item() -> None:
    stream = io.StringIO()
    now = [0.0]
    reporter = ProgressReporter(stream=stream, interval=2.0, clock=lambda: now[0])
    reporter.update({"phase": "resources", "processed_resources": 0, "total_resources": 3})
    now[0] = 0.5
    reporter.update({"phase": "download", "processed_resources": 1, "total_resources": 3})
    assert stream.getvalue().count("[进度]") == 1
    now[0] = 2.1
    reporter.update({"phase": "download", "processed_resources": 2, "total_resources": 3})
    now[0] = 2.2
    reporter.update({"phase": "download", "processed_resources": 3, "total_resources": 3})
    lines = stream.getvalue().splitlines()
    assert len(lines) == 3
    assert all(line.startswith("[进度]") for line in lines)
    assert "\r" not in stream.getvalue()


@pytest.mark.parametrize("mode", ["quiet", "json"])
def test_progress_is_suppressed_for_quiet_and_json(mode: str) -> None:
    stream = io.StringIO()
    reporter = ProgressReporter(mode, stream=stream)  # type: ignore[arg-type]
    reporter.update({"phase": "download", "processed_resources": 1, "total_resources": 1})
    reporter.close()
    assert stream.getvalue() == ""


def test_verbose_progress_includes_current_url() -> None:
    stream = io.StringIO()
    reporter = ProgressReporter("verbose", stream=stream)
    reporter.update(
        {
            "phase": "page",
            "pages_scanned": 1,
            "total_pages": 4,
            "current_url": "https://example.com/page",
        }
    )
    assert "扫描页面 1/4" in stream.getvalue()
    assert "https://example.com/page" in stream.getvalue()


def test_crawl_summary_counts_success_failure_skip_and_bytes() -> None:
    summary = crawl_summary(
        pages=2,
        resources=3,
        rows=[_row("ok", 100), _row("error: timeout"), _row("skipped by dedup")],
        started_at=0.0,
        output_dir="out",
    )
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1
    assert summary["skipped"] == 1
    assert summary["bytes"] == 100
    assert summary["status"] == "partial"


def test_human_and_quiet_summary_modes() -> None:
    summary = {
        "status": "success",
        "succeeded": 2,
        "failed": 0,
        "skipped": 1,
        "pages_scanned": 1,
        "resources_found": 3,
        "duration_seconds": 1.25,
        "bytes": 1024,
        "output_dir": "out",
    }
    stream = io.StringIO()
    emit_crawl_summary(summary, argparse.Namespace(), stream=stream)
    assert "成功/失败/跳过：2/0/1" in stream.getvalue()
    assert "1.0 KiB" in stream.getvalue()
    quiet = io.StringIO()
    emit_crawl_summary(summary, argparse.Namespace(quiet=True), stream=quiet)
    assert quiet.getvalue() == ""


def test_crawl_json_no_resources_emits_only_json_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _crawl_args(tmp_path, "--json")
    with patch.object(core, "fetch", return_value=(b"<html><p>empty</p>", "text/html")):
        assert cr.crawl(args) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.out.count("\n") == 1
    assert payload["status"] == "success"
    assert payload["schema_version"] == 1
    assert payload["pages_scanned"] == 1
    assert payload["resources_found"] == 0
    assert payload["succeeded"] == payload["failed"] == 0
    assert "duration_seconds" in payload and "bytes" in payload


def test_crawl_json_partial_failure_and_network_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("ERROR", logger="crawler")
    args = _crawl_args(tmp_path, "--json", "--workers", "1")

    def fake_fetch(url: str, *args: object, **kwargs: object) -> tuple[bytes, str]:
        if url == "https://example.com":
            return b'<html><img src="https://example.com/a.png"></html>', "text/html"
        raise OSError("connection refused")

    with patch.object(core, "fetch", side_effect=fake_fetch):
        assert cr.crawl(args) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "partial"
    assert payload["failed"] == 1
    assert payload["succeeded"] == 0
    assert "[网络错误]" in caplog.text
    assert "下一步" in caplog.text


def test_crawl_quiet_suppresses_summary_and_progress(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _crawl_args(tmp_path, "--quiet")
    with patch.object(core, "fetch", return_value=(b"<html><p>empty</p>", "text/html")):
        assert cr.crawl(args) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[进度]" not in captured.err


def test_crawl_json_config_error_is_actionable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _crawl_args(tmp_path, "--json")
    args.header = ["not-a-header"]
    assert cr.crawl(args) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "configuration"
    assert "--help" in payload["error"]["hint"]

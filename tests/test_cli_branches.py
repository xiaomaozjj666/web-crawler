"""CLI 入口分支、下载/取消路径与验证码任务错误路径。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from web_crawler.app import _crawler_core as core
from web_crawler.app import _ui_runner
from web_crawler.app import crawler as cr
from web_crawler.app._ui_state import CaptchaJobState
from web_crawler.exceptions import FetchError, WebCrawlerError


def _run_main(argv: list[str]) -> None:
    with patch.object(sys, "argv", ["web-crawler", *argv]):
        cr.main()


class TestCliBranches:
    def test_verify_offline_success_and_json(self, tmp_path: Path, capsys) -> None:
        index = tmp_path / "offline_pages/index.html"
        index.parent.mkdir(parents=True)
        index.write_text('<a href="p.html">p</a>', encoding="utf-8")
        (index.parent / "p.html").write_text("<p>ok</p>", encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            _run_main(["--verify-offline", str(tmp_path), "--json"])
        assert excinfo.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "passed"

    def test_verify_offline_failure_exit_code(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "offline_pages").mkdir()
        (tmp_path / "offline_pages/index.html").write_text(
            '<a href="missing.html">missing</a>', encoding="utf-8"
        )
        with pytest.raises(SystemExit) as excinfo:
            _run_main(["--verify-offline", str(tmp_path), "--json"])
        assert excinfo.value.code == 1

    def test_verify_offline_exception_is_config_error(self, tmp_path: Path, capsys) -> None:
        with (
            patch.object(cr, "verify_offline", side_effect=OSError("disk")),
            pytest.raises(SystemExit) as excinfo,
        ):
            _run_main(["--verify-offline", str(tmp_path)])
        assert excinfo.value.code == 2
        assert "disk" in capsys.readouterr().err

    def test_verify_offline_rejects_network_args(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _run_main(["--verify-offline", ".", "--url", "https://example.com"])
        assert excinfo.value.code == 2

    def test_load_config_missing_and_invalid(self, tmp_path: Path, capsys) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _run_main(["--load-config", str(tmp_path / "absent.json")])
        assert excinfo.value.code == 2

        bad = tmp_path / "bad.json"
        bad.write_text("[]", encoding="utf-8")
        with pytest.raises(SystemExit) as excinfo:
            _run_main(["--load-config", str(bad)])
        assert excinfo.value.code == 2

    def test_invalid_url_marks_scan_failed(self, tmp_path: Path, caplog) -> None:
        caplog.set_level("ERROR", logger="crawler")
        with pytest.raises(SystemExit) as excinfo:
            _run_main(["--url", "not-a-url", "--out", str(tmp_path)])
        assert excinfo.value.code == 3
        assert "无法采集起始 URL" in caplog.text

    def test_sample_requires_rules(self, tmp_path: Path) -> None:
        sample = tmp_path / "page.html"
        sample.write_text("<html><title>T</title></html>", encoding="utf-8")
        with pytest.raises(SystemExit) as excinfo:
            _run_main(["--sample", str(sample)])
        assert excinfo.value.code == 2

    def test_sample_with_rules_emits_json(self, tmp_path: Path, capsys) -> None:
        sample = tmp_path / "page.html"
        sample.write_text("<html><title>T</title></html>", encoding="utf-8")
        rules = tmp_path / "rules.json"
        rules.write_text(
            json.dumps({"version": 1, "fields": {"title": {"css": "title"}}}), encoding="utf-8"
        )
        _run_main(["--sample", str(sample), "--rules", str(rules)])
        payload = json.loads(capsys.readouterr().out)
        assert payload["item"]["title"] == "T"

    def test_save_config_json_output(self, tmp_path: Path, capsys) -> None:
        target = tmp_path / "config.json"
        _run_main(["--url", "https://example.com", "--save-config", str(target), "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["action"] == "save-config"
        assert target.is_file()

    def test_invalid_rules_file(self, tmp_path: Path) -> None:
        bad = tmp_path / "rules.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(SystemExit) as excinfo:
            _run_main(["--url", "https://example.com", "--rules", str(bad), "--out", str(tmp_path)])
        assert excinfo.value.code == 2


class TestFetchBranches:
    def test_resume_download_honours_rate_limiter(self, tmp_path: Path) -> None:
        control_args = Mock(stealth=False, proxy=None)
        fake = Mock()

        def _download(url: str, dest: Path, **kwargs: object) -> Path:
            return dest

        fake.download.side_effect = _download
        limiter = Mock()
        with patch.object(core, "_get_app_fetcher", return_value=fake):
            body, content_type = cr.fetch(
                "https://example.com/f.bin",
                30,
                {},
                0,
                None,
                resume_path=tmp_path / "f.bin",
                rate_limiter=limiter,
                control_args=control_args,
            )
        assert body == b""
        assert content_type == ""
        limiter.wait_if_needed.assert_called_once()
        limiter.record_request.assert_called_once()

    def test_download_max_bytes_maps_to_value_error(self, tmp_path: Path) -> None:
        fake = Mock()
        fake.download.side_effect = FetchError("file exceeds max_bytes (100)")
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            pytest.raises(ValueError, match="max-bytes"),
        ):
            cr.fetch(
                "https://example.com/f.bin",
                30,
                {},
                0,
                100,
                resume_path=tmp_path / "f.bin",
                control_args=Mock(stealth=False, proxy=None),
            )

    def test_429_retries_after_backoff(self) -> None:
        r429 = Mock(status=429, headers={"Retry-After": "0"}, content=b"")
        ok = Mock(status=200, headers={"content-type": "text/html"}, content=b"ok")
        fake = Mock()
        fake.get.side_effect = [r429, ok]
        limiter = Mock()
        limiter.handle_429.return_value = 0.01
        control_args = Mock(stealth=False, proxy=None)
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            patch.object(core.time, "sleep"),
        ):
            data, content_type = cr.fetch(
                "https://example.com/",
                30,
                {},
                1,
                None,
                rate_limiter=limiter,
                control_args=control_args,
            )
        assert data == b"ok" and content_type == "text/html"

    def test_import_dynamic_fetcher_missing_raises(self) -> None:
        with (
            patch.dict(sys.modules, {"web_crawler": None}, clear=False),
            pytest.raises(WebCrawlerError, match="DynamicFetcher"),
        ):
            core._import_dynamic_fetcher()

    def test_close_render_fetchers_swallows_close_errors(self) -> None:
        bad = Mock()
        bad.close.side_effect = RuntimeError("browser gone")
        core._render_local.cache = {"k": bad}
        try:
            core.close_render_fetchers()
        finally:
            core._render_local.cache = {}
        bad.close.assert_called_once()


class TestCaptchaJobErrorPath:
    def test_solve_failure_sets_error_status(self, monkeypatch) -> None:
        job = CaptchaJobState(id="c1", url="https://example.com", headless=True)
        with patch(
            "web_crawler.ai.captcha.CaptchaManager.solve_url",
            side_effect=RuntimeError("captcha boom"),
        ):
            _ui_runner.run_captcha_job(job)
        assert job.status == "error"
        assert "captcha boom" in job.error
        assert job.finished_at is not None

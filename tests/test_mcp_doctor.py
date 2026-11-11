"""``web_crawler.mcp._doctor`` 与 ``doctor`` 子命令的单元测试。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from web_crawler.mcp import _doctor
from web_crawler.mcp import cli as cli_module
from web_crawler.mcp._doctor import (
    DoctorCheck,
    _check_evidence_limits,
    _check_module,
    collect_checks,
)


def test_check_module_detects_missing_optional_module() -> None:
    check = _check_module(
        "definitely_missing_module_for_doctor_test",
        required=False,
        missing_detail="缺失",
        missing_fix="pip install x",
    )
    assert check.status == "warn"
    assert check.fix == "pip install x"


def test_check_module_accepts_installed_module() -> None:
    check = _check_module("json", required=True, missing_detail="缺失", missing_fix="pip install x")
    assert check.status == "ok"


def test_check_evidence_limits_warns_on_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_CRAWLER_MCP_EVIDENCE_BYTES", "not-an-int")
    check = _check_evidence_limits()
    assert check.status == "warn"
    assert check.fix is not None


def test_check_evidence_limits_ok_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_CRAWLER_MCP_EVIDENCE_RECORDS", raising=False)
    monkeypatch.delenv("WEB_CRAWLER_MCP_EVIDENCE_BYTES", raising=False)
    check = _check_evidence_limits()
    assert check.status == "ok"
    assert "records=16" in check.detail


def test_check_llm_key_ok_when_env_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert _doctor._check_llm_key().status == "ok"


def test_check_llm_key_warns_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    check = _doctor._check_llm_key()
    assert check.status == "warn"
    assert check.fix is not None


def _install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch,
    executable: Path | None,
    *,
    explode: bool = False,
) -> None:
    """让浏览器探测直接返回给定路径（不启动 Playwright 子进程）。"""

    def _probe() -> str:
        if explode:
            raise RuntimeError("driver missing")
        return str(executable) if executable is not None else ""

    monkeypatch.setattr(_doctor, "_probe_playwright_chromium", _probe)
    # 在 minimal-deps 环境里 playwright 可能未安装：让 find_spec 假装存在，
    # 真正被测试的是「探测结果的三种走向」。
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *args: object() if name == "playwright" else real_find_spec(name, *args),
    )


def test_probe_playwright_chromium_parses_subprocess_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _doctor.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="/opt/chrome\n", stderr=""
        ),
    )
    assert _doctor._probe_playwright_chromium() == "/opt/chrome"


def test_probe_playwright_chromium_raises_with_stderr_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _doctor.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom\nlast line\n"
        ),
    )
    with pytest.raises(RuntimeError, match="last line"):
        _doctor._probe_playwright_chromium()


def test_probe_playwright_chromium_wraps_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(*args: object, **kwargs: object) -> None:
        raise OSError("cannot spawn")

    monkeypatch.setattr(_doctor.subprocess, "run", _run)
    with pytest.raises(RuntimeError, match="cannot spawn"):
        _doctor._probe_playwright_chromium()


def _install_fake_camoufox(
    monkeypatch: pytest.MonkeyPatch,
    version: str | None,
    *,
    explode: bool = False,
    installed: bool = True,
) -> None:
    """让 Camoufox 检查直接返回给定版本（不读取真实浏览器安装）。"""
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *args: (
            (object() if installed else None) if name == "camoufox" else real_find_spec(name, *args)
        ),
    )

    def _probe() -> str:
        if explode:
            raise RuntimeError("Camoufox 浏览器未下载")
        return version or ""

    monkeypatch.setattr(_doctor, "_probe_camoufox_version", _probe)


def test_check_camoufox_binary_warns_when_module_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_camoufox(monkeypatch, None, installed=False)
    check = _doctor._check_camoufox_binary()
    assert check.status == "warn"
    assert "未安装" in check.detail
    assert check.fix is not None
    assert "camoufox fetch" in check.fix


def test_check_camoufox_binary_warns_when_browser_not_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_camoufox(monkeypatch, None, explode=True)
    check = _doctor._check_camoufox_binary()
    assert check.status == "warn"
    assert "不可用" in check.detail
    assert check.fix is not None
    assert "camoufox fetch" in check.fix


def test_check_camoufox_binary_ok_with_installed_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_camoufox(monkeypatch, "156.0.1-beta.34")
    check = _doctor._check_camoufox_binary()
    assert check.status == "ok"
    assert check.detail == "Camoufox 156.0.1-beta.34"


def test_check_browser_binary_warns_when_playwright_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *args: None if name == "playwright" else real_find_spec(name, *args),
    )
    check = _doctor._check_browser_binary()
    assert check.status == "warn"
    assert "playwright 未安装" in check.detail


def test_check_browser_binary_ok_when_binary_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "chrome.exe"
    binary.write_text("x", encoding="utf-8")
    _install_fake_playwright(monkeypatch, binary)
    check = _doctor._check_browser_binary()
    assert check.status == "ok"
    assert check.detail == str(binary)


def test_check_browser_binary_warns_when_binary_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_playwright(monkeypatch, tmp_path / "missing.exe")
    check = _doctor._check_browser_binary()
    assert check.status == "warn"
    assert "不存在" in check.detail
    assert check.fix is not None


def test_check_browser_binary_warns_when_launch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_playwright(monkeypatch, None, explode=True)
    check = _doctor._check_browser_binary()
    assert check.status == "warn"
    assert "无法探测" in check.detail


def test_check_cache_dir_fails_when_temp_unwritable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(_doctor.tempfile, "gettempdir", lambda: str(blocker))
    check = _doctor._check_cache_dir()
    assert check.status == "fail"
    assert check.fix is not None


def test_check_schema_profile_defaults_to_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_CRAWLER_MCP_SCHEMA_PROFILE", raising=False)
    check = _doctor._check_schema_profile()
    assert check.status == "ok"
    assert "full" in check.detail


def test_check_schema_profile_reports_valid_and_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_CRAWLER_MCP_SCHEMA_PROFILE", "compact")
    assert _doctor._check_schema_profile().status == "ok"
    monkeypatch.setenv("WEB_CRAWLER_MCP_SCHEMA_PROFILE", "bogus")
    invalid = _doctor._check_schema_profile()
    assert invalid.status == "warn"
    assert invalid.fix is not None


def test_collect_checks_includes_core_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _doctor,
        "_check_browser_binary",
        lambda: DoctorCheck("browser_binary", "ok", "stub"),
    )
    monkeypatch.setattr(
        _doctor,
        "_check_camoufox_binary",
        lambda: DoctorCheck("camoufox_binary", "ok", "stub"),
    )
    names = {check.name for check in collect_checks()}
    assert {
        "python",
        "package",
        "mcp",
        "llm_api_key",
        "curl_cffi",
        "camoufox",
        "camoufox_binary",
        "browser_binary",
        "cache_dir",
        "schema_profile",
        "evidence_limits",
    } <= names


def test_doctor_check_to_dict_omits_missing_fix() -> None:
    assert "fix" not in DoctorCheck("x", "ok", "fine").to_dict()
    assert DoctorCheck("x", "warn", "detail", "fix").to_dict()["fix"] == "fix"


def _run_doctor(
    monkeypatch: pytest.MonkeyPatch,
    checks: list[DoctorCheck],
    argv: list[str],
) -> int:
    monkeypatch.setattr(_doctor, "collect_checks", lambda: checks)
    args = cli_module.build_parser().parse_args(argv)
    return cli_module.cmd_doctor(args)


def test_cmd_doctor_passes_when_no_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run_doctor(
        monkeypatch,
        [DoctorCheck("python", "ok", "3.13.0"), DoctorCheck("llm", "warn", "no key", "set it")],
        ["doctor"],
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "[ok  ] python" in output
    assert "修复：set it" in output
    assert "环境检查通过" in output


def test_cmd_doctor_fails_on_fail_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run_doctor(
        monkeypatch,
        [DoctorCheck("cache_dir", "fail", "不可写", "修权限")],
        ["doctor"],
    )
    assert code == 1
    assert "1 项检查失败" in capsys.readouterr().out


def test_cmd_doctor_json_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run_doctor(
        monkeypatch,
        [DoctorCheck("python", "ok", "3.13.0")],
        ["--json", "doctor"],
    )
    assert code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["ok"] is True
    assert parsed["checks"][0]["name"] == "python"

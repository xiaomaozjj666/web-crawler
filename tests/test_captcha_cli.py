"""web-captcha CLI 测试（mock solver，不启动浏览器、不调用 LLM）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from web_crawler import captcha_cli


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict[str, Any] | None]:
    """跑一次 main()：返回退出码与打印出的 JSON（无 JSON 输出时为 None）。"""
    code = captcha_cli.main(argv)
    out = capsys.readouterr().out
    try:
        return code, json.loads(out)
    except json.JSONDecodeError:
        return code, None


def test_image_subcommand_prints_answer(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """image 子命令：solver 结果转 JSON 输出，退出码 0。"""
    image = tmp_path / "captcha.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nstub")
    solver = MagicMock()
    solver.solve_text.return_value = "42"
    with patch.object(captcha_cli, "_build_image_solver", return_value=solver):
        code, data = _run(capsys, ["image", str(image), "--math"])
    assert code == 0
    assert data == {"ok": True, "text": "42", "math": True}
    solver.solve_text.assert_called_once_with(b"\x89PNG\r\n\x1a\nstub", mime="image/png")


def test_image_subcommand_no_result(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """image 子命令：本地与 LLM 路径均无结果 → ok=False，退出码 1。"""
    image = tmp_path / "captcha.png"
    image.write_bytes(b"stub")
    solver = MagicMock()
    solver.solve_text.return_value = ""
    with patch.object(captcha_cli, "_build_image_solver", return_value=solver):
        code, data = _run(capsys, ["image", str(image)])
    assert code == 1
    assert data is not None
    assert data["ok"] is False
    assert "识别失败" in data["error"]


def test_page_subcommand_no_captcha(capsys: pytest.CaptureFixture[str]) -> None:
    """page 子命令：页面无验证码（solved=None）→ 退出码 0。"""
    with (
        patch("web_crawler.captcha_cli.require_camoufox"),
        patch.object(captcha_cli, "_build_image_solver", return_value=MagicMock()),
        patch("web_crawler.ai.captcha.CaptchaManager") as manager_cls,
    ):
        manager_cls.return_value.solve_url.return_value = {
            "url": "https://example.com",
            "type": "none",
            "solved": None,
            "steps": [],
        }
        code, data = _run(capsys, ["page", "https://example.com"])
    assert code == 0
    assert data is not None
    assert data["solved"] is None
    # 构造参数落到 solve_url：默认有头（headless=False）
    assert manager_cls.return_value.solve_url.call_args.kwargs["headless"] is False


def test_page_subcommand_failed(capsys: pytest.CaptureFixture[str]) -> None:
    """page 子命令：求解未通过（solved=False）→ 退出码 1。"""
    with (
        patch("web_crawler.captcha_cli.require_camoufox"),
        patch.object(captcha_cli, "_build_image_solver", return_value=MagicMock()),
        patch("web_crawler.ai.captcha.CaptchaManager") as manager_cls,
    ):
        manager_cls.return_value.solve_url.return_value = {
            "url": "https://example.com",
            "type": "slider",
            "solved": False,
            "steps": [],
        }
        code, data = _run(capsys, ["page", "https://example.com", "--headless"])
    assert code == 1
    assert data is not None
    assert data["solved"] is False
    # --headless 透传给 solve_url
    assert manager_cls.return_value.solve_url.call_args.kwargs["headless"] is True


def test_page_subcommand_requires_camoufox(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """page 子命令：未安装 camoufox → JSON 错误提示，退出码 2。"""
    with patch(
        "web_crawler.captcha_cli.require_camoufox",
        side_effect=ImportError("camoufox is required for the anti-fingerprint Firefox fetcher"),
    ):
        code, data = _run(capsys, ["page", "https://example.com"])
    assert code == 2
    assert data is not None
    assert data["ok"] is False
    assert "camoufox" in data["error"]


def test_missing_subcommand_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    """缺子命令 → argparse 用法错误（SystemExit 2）。"""
    with pytest.raises(SystemExit) as excinfo:
        captcha_cli.main([])
    assert excinfo.value.code == 2

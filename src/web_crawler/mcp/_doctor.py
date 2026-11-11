"""MCP 运行环境自检（``web-crawler-reverse doctor``）。

借鉴 REA 的 doctor 设计：区分「缺什么」与「怎么修」，并把修复动作绑定到
具体组件（Python / mcp SDK / provider 密钥 / 浏览器 / 证据限额 / schema 档位）。
每项检查返回 ok / warn / fail，只有 fail 才让命令退出 1——可降级的缺失不应
阻断基本使用。
"""

from __future__ import annotations

import importlib.util
import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .. import __version__
from ._evidence import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_RECORDS,
    evidence_limits_from_env,
)
from ._specs import _PROFILE_ENV, SCHEMA_PROFILES


@dataclass(frozen=True)
class DoctorCheck:
    """单项检查结果。"""

    name: str
    status: str  # ok / warn / fail
    detail: str
    fix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data["fix"] is None:
            del data["fix"]
        return data


def _module_version(import_name: str) -> str | None:
    """返回已安装模块版本；查不到版本时返回 None（不等于未安装）。"""
    try:
        from importlib import metadata

        return metadata.version(import_name)
    except Exception:
        return None


def _check_module(
    import_name: str,
    *,
    required: bool,
    missing_detail: str,
    missing_fix: str,
) -> DoctorCheck:
    if importlib.util.find_spec(import_name) is None:
        return DoctorCheck(
            import_name,
            "fail" if required else "warn",
            missing_detail,
            missing_fix,
        )
    version = _module_version(import_name) or "installed"
    return DoctorCheck(import_name, "ok", version)


def _check_llm_key() -> DoctorCheck:
    if os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY"):
        return DoctorCheck("llm_api_key", "ok", "已通过环境变量提供")
    return DoctorCheck(
        "llm_api_key",
        "warn",
        "未设置 DEEPSEEK_API_KEY / LLM_API_KEY，LLM 相关工具不可用",
        "设置 DEEPSEEK_API_KEY=<key> 后重启 MCP 服务；环境变量只在进程启动时读取。",
    )


def _probe_playwright_chromium(timeout: float = 20.0) -> str:
    """在子进程里向 Playwright 询问 Chromium 路径。

    Playwright 的同步驱动在解释器退出时会向 stderr 打印 asyncio 清理噪声
    （Windows 已知现象）；放进独立进程并捕获输出，doctor 的输出保持干净。
    查询失败（驱动缺失 / 权限 / 超时）统一抛 ``RuntimeError``。
    """
    code = (
        "from playwright.sync_api import sync_playwright\n"
        "runtime = sync_playwright().start()\n"
        "try:\n"
        "    print(runtime.chromium.executable_path)\n"
        "finally:\n"
        "    runtime.stop()\n"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(str(exc)) from exc
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or not lines:
        detail = (completed.stderr.strip().splitlines() or ["Playwright 未返回路径"])[-1]
        raise RuntimeError(detail)
    return lines[-1].strip()


def _probe_camoufox_version() -> str:
    """返回已安装的 Camoufox 浏览器版本；未 fetch 时由 pkgman 抛异常。"""
    from camoufox.pkgman import installed_verstr

    return str(installed_verstr())


def _check_camoufox_binary() -> DoctorCheck:
    """检查 Camoufox 浏览器本体（MCP 浏览器类工具实际使用的 Firefox）。"""
    if importlib.util.find_spec("camoufox") is None:
        return DoctorCheck(
            "camoufox_binary",
            "warn",
            "camoufox 未安装，MCP 浏览器类工具不可用",
            "pip install 'camoufox[geoip]>=0.5.8,<1' && python -m camoufox fetch",
        )
    try:
        version = _probe_camoufox_version()
    except Exception as exc:  # CamoufoxNotInstalled / 权限 / 路径损坏
        return DoctorCheck(
            "camoufox_binary",
            "warn",
            f"Camoufox 浏览器不可用：{exc}",
            "python -m camoufox fetch（首次需下载浏览器，体积较大）",
        )
    return DoctorCheck("camoufox_binary", "ok", f"Camoufox {version}")


def _check_browser_binary() -> DoctorCheck:
    """探测 Playwright Chromium（DynamicFetcher / StealthyFetcher 使用）。"""
    if importlib.util.find_spec("playwright") is None:
        return DoctorCheck(
            "browser_binary",
            "warn",
            "playwright 未安装，跳过浏览器二进制探测",
            "pip install playwright && python -m playwright install chromium",
        )
    try:
        executable = _probe_playwright_chromium()
    except RuntimeError as exc:  # 驱动缺失/权限/无显示环境都归为不可探测
        return DoctorCheck(
            "browser_binary",
            "warn",
            f"无法探测 Chromium：{exc}",
            "python -m playwright install chromium；无显示环境时使用 headless。",
        )
    if Path(executable).is_file():
        return DoctorCheck("browser_binary", "ok", executable)
    return DoctorCheck(
        "browser_binary",
        "warn",
        f"Chromium 可执行文件不存在：{executable}",
        "python -m playwright install chromium",
    )


def _check_cache_dir() -> DoctorCheck:
    """验证进程临时目录可写（截图/缓存/断点默认落在这里）。"""
    tmp = Path(tempfile.gettempdir())
    probe = tmp / f".web-crawler-doctor-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return DoctorCheck(
            "cache_dir",
            "fail",
            f"临时目录不可写：{tmp}（{exc}）",
            "检查 TMP/TEMP 环境变量与目录权限后重启 MCP 服务。",
        )
    return DoctorCheck("cache_dir", "ok", str(tmp))


def _check_schema_profile() -> DoctorCheck:
    raw = os.environ.get(_PROFILE_ENV)
    if raw is None or not raw.strip():
        return DoctorCheck("schema_profile", "ok", "full（默认）")
    profile = raw.strip().lower()
    if profile in SCHEMA_PROFILES:
        return DoctorCheck("schema_profile", "ok", profile)
    return DoctorCheck(
        "schema_profile",
        "warn",
        f"{_PROFILE_ENV}={raw!r} 不是受支持档位",
        f"改为 {' / '.join(SCHEMA_PROFILES)} 之一，或删除该环境变量。",
    )


def _check_evidence_limits() -> DoctorCheck:
    records, max_bytes, warnings = evidence_limits_from_env()
    detail = f"records={records}, bytes={max_bytes}"
    if warnings:
        return DoctorCheck(
            "evidence_limits",
            "warn",
            "; ".join(warnings),
            f"不设置时默认 records={DEFAULT_MAX_RECORDS}, bytes={DEFAULT_MAX_BYTES}。",
        )
    return DoctorCheck("evidence_limits", "ok", detail)


def collect_checks() -> list[DoctorCheck]:
    """收集全部自检项（顺序稳定，便于人读与 JSON 消费）。"""
    checks: list[DoctorCheck] = []
    checks.append(DoctorCheck("python", "ok", platform.python_version()))

    checks.append(DoctorCheck("package", "ok", f"stealth-web-crawler {__version__}"))
    checks.append(
        _check_module(
            "mcp",
            required=False,
            missing_detail="mcp SDK 未安装，将使用手写 stdio 降级实现",
            missing_fix="pip install 'mcp>=2.3,<3' 以获得完整协议支持",
        )
    )
    checks.append(_check_llm_key())
    checks.append(
        _check_module(
            "curl_cffi",
            required=False,
            missing_detail="未安装，HTTP 抓取将回退到 httpx（TLS 指纹能力减弱）",
            missing_fix="pip install 'curl_cffi>=0.9,<1'",
        )
    )
    checks.append(
        _check_module(
            "camoufox",
            required=False,
            missing_detail="未安装，浏览器类工具不可用",
            missing_fix="pip install 'camoufox[geoip]>=0.5.8,<1' && python -m camoufox fetch",
        )
    )
    checks.append(_check_camoufox_binary())
    checks.append(_check_browser_binary())
    checks.append(_check_cache_dir())
    checks.append(_check_schema_profile())
    checks.append(_check_evidence_limits())
    return checks

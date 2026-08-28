"""Web UI 的常量与纯函数辅助（从 ``ui.py`` 拆出）。

包含服务配置常量、表单解析/校验、配置序列化等无状态逻辑；
不依赖 HTTP Handler 与任务运行时，供 ``_ui_http`` / ``_ui_runner`` 复用。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
from typing import cast

from web_crawler.app import crawler as web_resource_crawler

HOST = "127.0.0.1"
PORT = 8765
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = os.path.join(os.getcwd(), "crawler_output")
DEFAULT_BLOCK_KEYWORDS = (
    "ads, adservice, adserver, adclick, doubleclick, googlesyndication, "
    "google-analytics, banner, promo, promotion, popup, popunder, modal, "
    "overlay, interstitial, floating, float-ad, layer-ad, dialog-ad, lightbox, "
    "subscribe, webpush, push-notification, affiliate, tracker, tracking, "
    "analytics, tongji, stat, hm.baidu, cnzz, umeng, "
    "recaptcha, captcha, hcaptcha, turnstile, challenge, verification, "
    "verify, security-check, bot-detect, botdetect"
)


def _is_loopback_host(host: str) -> bool:
    """只允许绑定回环地址（127.x / ::1 / localhost），防止控制面暴露到局域网。"""
    if host in ("localhost", "::1", "127.0.0.1"):
        return True
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback
    except ValueError:
        return False


def _open_folder(path: str) -> None:
    """跨平台打开文件夹（Windows explorer / macOS open / Linux xdg-open）。"""
    import sys as _sys

    if _sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif _sys.platform == "darwin":  # pragma: no cover
        import subprocess

        subprocess.Popen(["open", path])
    else:  # pragma: no cover
        import subprocess

        subprocess.Popen(["xdg-open", path])


def _as_int(value: object, default: int) -> int:
    """把 payload 中的数值安全转为 int;非数字/None 回退默认值。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


def _as_float(value: object, default: float) -> float:
    """把 payload 中的数值安全转为 float;非数字/None 回退默认值。"""
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return default


# 前端页面模板：独立文件便于维护（web_crawler/app/static/index.html），运行时读取。
# 模板含 {block_keywords} 占位符，由 do_GET 在响应时替换。
_PAGE_TEMPLATE_PATH = Path(__file__).resolve().parent / "static" / "index.html"


def _load_page_template() -> str:
    """读取前端模板；文件缺失时返回占位页（打包异常时兜底）。"""
    try:
        return _PAGE_TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - 仅打包缺失时触发
        return (
            "<!doctype html><html><body>"
            "<h1>模板缺失</h1><p>缺少 web_crawler/app/static/index.html，请重新安装。</p>"
            "</body></html>"
        )


PAGE = _load_page_template()


def output_path(value: str) -> str:
    path = Path(value or DEFAULT_OUTPUT)
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path.resolve())


def header_values(form: dict[str, list[str]]) -> list[str]:
    values: list[str] = []
    cookie = form.get("cookie", [""])[0].strip()
    referer = form.get("referer", [""])[0].strip() or form.get("url", [""])[0].strip()
    extra = form.get("headers", [""])[0]
    if cookie:
        values.append(f"Cookie: {cookie}")
    if referer:
        values.append(f"Referer: {referer}")
    for line in extra.splitlines():
        line = line.strip()
        if line:
            values.append(line)
    return values


def _validate_int_field(
    name: str, raw: str, default: int, minimum: int, maximum: int | None
) -> int:
    """解析并校验表单整数字段；非法值抛 ValueError（由 handler 转 JSON 错误）。"""
    text = (raw or "").strip()
    if not text:
        return default
    try:
        parsed = int(text)
    except ValueError:
        raise ValueError(f"{name} 必须是整数") from None
    if parsed < minimum or (maximum is not None and parsed > maximum):
        limit = f"{minimum}~{maximum}" if maximum is not None else f">={minimum}"
        raise ValueError(f"{name} 超出范围（{limit}）")
    return parsed


def _validate_float_field(name: str, raw: str, default: float, minimum: float) -> float:
    """解析并校验表单浮点字段；非法值抛 ValueError。"""
    text = (raw or "").strip()
    if not text:
        return default
    try:
        parsed = float(text)
    except ValueError:
        raise ValueError(f"{name} 必须是数字") from None
    if parsed < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return parsed


def build_args(form: dict[str, list[str]]) -> argparse.Namespace:
    def value(name: str, default: str = "") -> str:
        return form.get(name, [default])[0]

    def checked(name: str) -> bool:
        return name in form

    # 服务端范围校验（非法值在 parse_args 前拦截,避免 SystemExit 杀死 handler 线程）
    workers = _validate_int_field("workers", value("workers", "8"), 8, 1, 64)
    retries = _validate_int_field("retries", value("retries", "2"), 2, 0, None)
    timeout = _validate_int_field("timeout", value("timeout", "30"), 30, 1, None)
    max_pages = _validate_int_field("max_pages", value("max_pages", "1"), 1, 1, None)
    max_bytes = _validate_int_field("max_bytes", value("max_bytes", "0"), 0, 0, None)
    delay = _validate_float_field("delay", value("delay", "0.5"), 0.5, 0.0)

    out_val = value("out", "")
    if not out_val:
        out_val = DEFAULT_OUTPUT
    args = web_resource_crawler.build_parser().parse_args(
        [
            "--url",
            value("url"),
            "--out",
            output_path(out_val),
            "--max-pages",
            str(max_pages),
            "--workers",
            str(workers),
            "--delay",
            str(delay),
            "--timeout",
            str(timeout),
            "--retries",
            str(retries),
            "--max-bytes",
            str(max_bytes),
            "--block-keyword",
            value("block_keywords", DEFAULT_BLOCK_KEYWORDS),
        ]
    )
    args.header = header_values(form)
    args.same_domain = checked("same_domain")
    args.include_css_urls = checked("include_css_urls")
    args.video_mode = checked("video_mode")
    args.video_only = checked("video_only")
    args.list_only = checked("list_only")
    args.expand_playlists = checked("expand_playlists")
    args.resume = checked("resume")
    args.organize = checked("organize")
    args.dedup = checked("dedup")
    args.sitemap = checked("sitemap")
    args.strip_overlays = checked("strip_overlays")
    args.rewrite_html = checked("rewrite_html")
    args.smart_extract = checked("smart_extract")
    args.resume_crawl = checked("resume_crawl")
    args.extract_text = checked("extract_text")
    args.crawl_pages = checked("crawl_pages")
    args.respect_robots = checked("respect_robots")
    args.stealth = checked("stealth")
    args.save_config = ""
    args.load_config = ""
    return args


# 入库配置白名单：显式列出可序列化字段，剔除 header（含 Cookie/Authorization）等敏感项
_DB_CONFIG_FIELDS = (
    "url",
    "out",
    "max_pages",
    "workers",
    "delay",
    "timeout",
    "retries",
    "max_bytes",
    "same_domain",
    "include_css_urls",
    "video_mode",
    "video_only",
    "list_only",
    "expand_playlists",
    "resume",
    "organize",
    "dedup",
    "sitemap",
    "strip_overlays",
    "rewrite_html",
    "smart_extract",
    "resume_crawl",
    "extract_text",
    "crawl_pages",
    "respect_robots",
    "stealth",
)


def _task_config_for_db(args: argparse.Namespace) -> dict[str, object]:
    """把任务参数序列化为可入库 dict（白名单字段,不含 Cookie 等敏感头）。"""
    return {name: getattr(args, name) for name in _DB_CONFIG_FIELDS}


def build_reverse_config(form: dict[str, list[str]]) -> dict[str, object]:
    """从表单构造 ReverseAgentConfig 的可序列化字段字典。"""

    def value(name: str, default: str = "") -> str:
        return form.get(name, [default])[0]

    def checked(name: str) -> bool:
        return name in form

    target_params_str = value("target_params", "")
    target_params = [p.strip() for p in target_params_str.split(",") if p.strip()]

    allowed_domains_str = value("allowed_domains", "")
    allowed_domains: list[str] | None = None
    if allowed_domains_str.strip():
        allowed_domains = [d.strip() for d in allowed_domains_str.split(",") if d.strip()]

    # dom_prune 复选框启用时才使用 max_chars，否则为 0（禁用）
    dom_prune_max_chars = (
        int(value("dom_prune_max_chars", "4000") or "0") if checked("dom_prune") else 0
    )

    return {
        "max_steps": int(value("max_steps", "20") or "20"),
        "target_params": target_params,
        "headless": value("headless", "false") == "true",
        "proxy": value("proxy", "") or None,
        "os_name": value("os_name", "windows"),
        "dom_prune_max_chars": dom_prune_max_chars,
        "dom_prune_llm_rank": checked("dom_prune_llm_rank"),
        "enable_checkpoint": checked("enable_checkpoint"),
        "checkpoint_interval": int(value("checkpoint_interval", "1") or "1"),
        "checkpoint_keep": int(value("checkpoint_keep", "5") or "5"),
        "min_confidence": float(value("min_confidence", "0.4") or "0.4"),
        "confidence_llm_score": checked("confidence_llm_score"),
        "enable_guard": checked("enable_guard"),
        "allowed_domains": allowed_domains,
        "enable_screenshot": checked("enable_screenshot"),
    }


# 配置导入时识别的字段及其默认值/类型转换器
_CONFIG_FIELD_SPECS: tuple[tuple[str, type, object], ...] = (
    ("max_steps", int, 20),
    ("target_params", list, []),
    ("headless", bool, False),
    ("proxy", str, None),
    ("os_name", str, "windows"),
    ("dom_prune_max_chars", int, 0),
    ("dom_prune_llm_rank", bool, False),
    ("enable_checkpoint", bool, False),
    ("checkpoint_interval", int, 1),
    ("checkpoint_keep", int, 5),
    ("min_confidence", float, 0.4),
    ("confidence_llm_score", bool, False),
    ("enable_guard", bool, True),
    ("allowed_domains", list, None),
    ("enable_screenshot", bool, True),
)


def _normalize_imported_config(data: dict[str, object]) -> dict[str, object]:
    """把导入的 JSON 配置标准化为 build_reverse_config 兼容的 dict。

    - 仅保留已知字段（剔除未知键）
    - 按字段类型做安全转换（int/float/bool/list/str）
    - 缺失字段补默认值
    """
    result: dict[str, object] = {}
    for name, ftype, default in _CONFIG_FIELD_SPECS:
        raw = data.get(name, default)
        if raw is None or raw == "":
            result[name] = default
            continue
        try:
            if ftype is int:
                result[name] = _as_int(raw, cast(int, default))
            elif ftype is float:
                result[name] = _as_float(raw, cast(float, default))
            elif ftype is bool:
                # 字符串 "true"/"false" / 数字 1/0 都支持
                if isinstance(raw, str):
                    result[name] = raw.lower() in ("true", "1", "yes", "on")
                else:
                    result[name] = bool(raw)
            elif ftype is list:
                if isinstance(raw, str):
                    result[name] = [s.strip() for s in raw.split(",") if s.strip()]
                elif isinstance(raw, list):
                    result[name] = [str(x) for x in raw]
                else:
                    result[name] = []
            else:
                result[name] = str(raw)
        except (TypeError, ValueError):
            result[name] = default
    return result


def _serialize_analysis(analysis: object) -> str:
    """把 AnalysisResult dataclass 序列化为可读字符串。"""
    if analysis is None:
        return ""
    if isinstance(analysis, str):
        return analysis
    if hasattr(analysis, "__dataclass_fields__"):
        try:
            from dataclasses import asdict

            return json.dumps(asdict(analysis), ensure_ascii=False, indent=2, default=str)  # type: ignore[call-overload]
        except Exception:
            pass
    return str(analysis)

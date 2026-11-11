"""MCP 工具目录定义(纯数据,从 ``server.py`` 拆出)。"""

from __future__ import annotations

import os

from ._server_helpers import (
    _DEFAULT_EVIDENCE_PAGE,
    _DEFAULT_PAGE_SIZE,
    _DEFAULT_TEXT_LIMIT,
    _DEFAULT_WAIT_TIME,
    _MAX_EVIDENCE_PAGE,
    _MAX_PAGE_SIZE,
    _MAX_TEXT_LIMIT,
)

SCHEMA_PROFILES = ("full", "compact")
_PROFILE_ENV = "WEB_CRAWLER_MCP_SCHEMA_PROFILE"
# REA 的渐进式发现只预览前 240 字符：紧凑档把描述压到首个句子。
_PREVIEW_CHARS = 240


def normalize_schema_profile(value: str | None = None) -> str:
    """把任意输入归一化为受支持的 schema 档位；非法值回退 full。"""
    raw = value if value is not None else os.environ.get(_PROFILE_ENV, "full")
    profile = str(raw).strip().lower()
    return profile if profile in SCHEMA_PROFILES else "full"


def _compact_description(text: str) -> str:
    """保留首个句子（不超过 240 字符），确保工具选择所需信息仍在首屏。"""
    for separator in ("。", "\n", ". "):
        index = text.find(separator)
        if 0 < index < _PREVIEW_CHARS:
            return text[: index + len(separator)].strip()
    if len(text) <= _PREVIEW_CHARS:
        return text
    return text[: _PREVIEW_CHARS - 1].rstrip() + "…"


def _compact_schema(node: dict) -> dict:
    """递归去掉 description，保留类型/必填/枚举/默认值与嵌套结构。

    REA 的 compact profile 会省略参数说明与嵌套形状；这里保留结构（Python
    客户端仍能据类型校验），只去掉纯注释性 prose——省上下文但不牺牲可调用性。
    """
    out: dict = {}
    for key, value in node.items():
        if key == "description":
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _compact_schema(spec) for name, spec in value.items()}
        elif key == "items" and isinstance(value, dict):
            out[key] = _compact_schema(value)
        else:
            out[key] = value
    return out


def _apply_schema_profile(tools: list[dict], profile: str) -> list[dict]:
    """按档位裁剪工具目录（full 原样返回）。"""
    if profile != "compact":
        return tools
    return [
        {
            "name": tool["name"],
            "description": _compact_description(tool["description"]),
            "inputSchema": _compact_schema(tool["inputSchema"]),
        }
        for tool in tools
    ]


def build_tool_definitions(profile: str = "full") -> list[dict]:
    """返回 MCP 工具列表(每个工具包含 name / description / inputSchema)。"""
    return _apply_schema_profile(_build_full_tool_definitions(), normalize_schema_profile(profile))


def _build_full_tool_definitions() -> list[dict]:
    return [
        {
            "name": "reverse_engineer_url",
            "description": (
                "逆向分析指定 URL 的加密参数。自动走完 Hook 注入 → 请求捕获 → "
                "JS 分析 → 算法重写的完整流程，返回加密参数的生成链路。"
                "结果包含 last_confidence"
                "（最近一次置信度评分）、checkpoints（断点列表）、screenshots"
                "（每步截图路径）与 error_screenshot（错误截图）字段，"
                "上游 AI 一次调用即可拿到全部运行时状态。"
                "result 内超长文本（反混淆 JS、hook 捕获的请求体等）默认超过 "
                f"{_DEFAULT_TEXT_LIMIT} 字符时截断，响应顶层的 truncations 列表"
                "标注被截断字段与原始长度，可传更大的 max_text_length 取更多内容。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "目标页面 URL"},
                    "target_params": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "重点关注的加密参数名列表（如 Anti-Content、X-Bogus）",
                    },
                    "task": {"type": "string", "description": "自然语言任务描述"},
                    "max_steps": {
                        "type": "integer",
                        "default": 20,
                        "description": "Agent 最大执行步数",
                    },
                    "max_text_length": {
                        "type": "integer",
                        "default": _DEFAULT_TEXT_LIMIT,
                        "description": (
                            "result 内单个字符串字段的最大字符数"
                            f"（上限 {_MAX_TEXT_LIMIT}，默认 {_DEFAULT_TEXT_LIMIT}）"
                        ),
                    },
                },
                "required": ["url"],
            },
        },
        {
            "name": "inject_hooks",
            "description": (
                "向页面注入 JS Hook（fetch / XHR / cookie / crypto.subtle / webpack / "
                "console），返回注入状态与 Hook 数据预览。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "目标页面 URL"},
                    "hooks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Hook 名称列表，留空注入全部（fetch_hook/xhr_hook/cookie_hook/...）",
                    },
                },
                "required": ["url"],
            },
        },
        {
            "name": "analyze_js_code",
            "description": (
                "用 AI 分析 JS 代码片段的加密逻辑，输出算法、输入参数、输出格式、"
                "执行流程与置信度。反混淆结果（deobfuscated 字段）默认超过 "
                f"{_DEFAULT_TEXT_LIMIT} 字符时截断，响应会标注 truncated=true 与 "
                "full_length，可传更大的 max_length 取更多内容。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "JS 代码片段"},
                    "url": {"type": "string", "description": "代码来源 URL（可选）"},
                    "target_param": {"type": "string", "description": "目标参数名（可选）"},
                    "max_length": {
                        "type": "integer",
                        "default": _DEFAULT_TEXT_LIMIT,
                        "description": (
                            "deobfuscated 字段的最大字符数"
                            f"（上限 {_MAX_TEXT_LIMIT}，默认 {_DEFAULT_TEXT_LIMIT}）"
                        ),
                    },
                },
                "required": ["code"],
            },
        },
        {
            "name": "extract_webpack_modules",
            "description": "从 JS 源码中用正则提取 webpack 模块（id、依赖、导出）。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "webpack bundle 源码"},
                },
                "required": ["source"],
            },
        },
        {
            "name": "deobfuscate_js",
            "description": (
                "用 AI 反混淆 JS 代码，返回可读的等价版本。"
                f"输出默认超过 {_DEFAULT_TEXT_LIMIT} 字符时截断，响应会标注 "
                "truncated=true 与 full_length，可传更大的 max_length 取更多内容。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "混淆后的 JS 代码"},
                    "max_length": {
                        "type": "integer",
                        "default": _DEFAULT_TEXT_LIMIT,
                        "description": (
                            f"输出最大字符数（上限 {_MAX_TEXT_LIMIT}，默认 {_DEFAULT_TEXT_LIMIT}）"
                        ),
                    },
                },
                "required": ["code"],
            },
        },
        {
            "name": "reimplement_algorithm",
            "description": (
                "用指定语言重写 JS 加密逻辑，输出可独立运行的等价代码。"
                f"输出默认超过 {_DEFAULT_TEXT_LIMIT} 字符时截断，响应会标注 "
                "truncated=true 与 full_length，可传更大的 max_length 取更多内容。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "JS 加密逻辑代码"},
                    "language": {
                        "type": "string",
                        "default": "python",
                        "description": "目标语言",
                    },
                    "max_length": {
                        "type": "integer",
                        "default": _DEFAULT_TEXT_LIMIT,
                        "description": (
                            f"输出最大字符数（上限 {_MAX_TEXT_LIMIT}，默认 {_DEFAULT_TEXT_LIMIT}）"
                        ),
                    },
                },
                "required": ["code"],
            },
        },
        {
            "name": "solve_captcha",
            "description": (
                "检测并尝试处理页面验证码（Turnstile / hCaptcha / reCAPTCHA v2&v3 / "
                "极验 GeeTest）。注入 ImageCaptchaSolver 后，遇到图片挑战会自动"
                "用 LLM Vision / 本地 OCR / Pillow 模板匹配识别图片，无需人工介入。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "目标页面 URL"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "solve_captcha_image",
            "description": (
                "直接识别图片验证码（无需浏览器）。支持三类识别："
                "(1) text — 文本字符 OCR；"
                "(2) slider — 滑块缺口 x 坐标，需提供 bg + slider 两张图；"
                "(3) click — 点选坐标，需提供 image + prompt。"
                "图片以 base64 字符串传入（不带 data: 前缀）。"
                "默认走 LLM Vision（需 provider 支持 vision）+ 本地 ddddocr/Pillow 兜底。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["text", "slider", "click"],
                        "description": "识别模式",
                    },
                    "image": {
                        "type": "string",
                        "description": "base64 编码的图片（text 模式传验证码图，click 模式传点选图）",
                    },
                    "bg": {
                        "type": "string",
                        "description": "slider 模式下的背景图 base64",
                    },
                    "slider": {
                        "type": "string",
                        "description": "slider 模式下的滑块图 base64",
                    },
                    "prompt": {
                        "type": "string",
                        "description": 'click 模式下的提示文字（如"请按顺序点击图中所有的红绿灯"）',
                    },
                    "mime": {
                        "type": "string",
                        "default": "image/png",
                        "description": "图片 MIME 类型",
                    },
                },
                "required": ["mode"],
            },
        },
        {
            "name": "pentest_recon",
            "description": (
                "对指定目标执行轻量级渗透侦察（合规声明：仅用于已获书面授权的目标）。"
                "支持五项独立检查，可任意组合："
                "ports（端口扫描）、dirs（目录爆破）、subdomains（子域名枚举）、"
                "vulns（SQL注入/XSS/路径穿越检测）、headers（HTTP 安全头检测）。"
                "返回聚合的 PentestReport（含 summary 统计与 grade 评级）。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "目标主机或 URL（如 example.com 或 https://example.com）",
                    },
                    "checks": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["ports", "dirs", "subdomains", "vulns", "headers"],
                        },
                        "description": "要执行的检查项列表，留空执行全部",
                    },
                    "ports": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "自定义端口列表（仅 ports 检查，最多 100 个，取值 1-65535）",
                    },
                    "timeout": {
                        "type": "number",
                        "default": 30.0,
                        "description": "整体超时（秒，1-300）",
                    },
                    "authorization_confirmed": {
                        "type": "boolean",
                        "default": False,
                        "description": "确认已获目标书面授权（必填，默认 false 拒绝执行）",
                    },
                    "allow_private": {
                        "type": "boolean",
                        "default": False,
                        "description": "显式放行私网/环回/链路本地等非公网目标（默认拒绝）",
                    },
                },
                "required": ["target", "authorization_confirmed"],
            },
        },
        {
            "name": "capture_network_requests",
            "description": (
                "捕获页面加载过程中的网络请求（url、method、headers、body）。"
                "结果分页返回：默认每页 50 条（上限 500），响应包含 total/offset/"
                "limit/has_more/next_offset 翻页元数据，可用 offset+limit 取下一页。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "目标页面 URL"},
                    "wait_time": {
                        "type": "number",
                        "default": _DEFAULT_WAIT_TIME,
                        "description": "采集等待时间（秒，上限 60）",
                    },
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": "分页起点（默认 0）",
                    },
                    "limit": {
                        "type": "integer",
                        "default": _DEFAULT_PAGE_SIZE,
                        "description": f"每页条数（1-{_MAX_PAGE_SIZE}，默认 {_DEFAULT_PAGE_SIZE}）",
                    },
                },
                "required": ["url"],
            },
        },
        {
            "name": "get_page_scripts",
            "description": (
                "获取页面加载的 JS 脚本 URL 列表。"
                "结果分页返回：默认每页 50 条（上限 500），响应包含 total/offset/"
                "limit/has_more/next_offset 翻页元数据，可用 offset+limit 取下一页。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "目标页面 URL"},
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": "分页起点（默认 0）",
                    },
                    "limit": {
                        "type": "integer",
                        "default": _DEFAULT_PAGE_SIZE,
                        "description": f"每页条数（1-{_MAX_PAGE_SIZE}，默认 {_DEFAULT_PAGE_SIZE}）",
                    },
                },
                "required": ["url"],
            },
        },
        {
            "name": "inspect_evidence",
            "description": (
                "按 evidence_id 分页读取本会话登记的工具结果。"
                "reverse_engineer_url 会在截断前自存完整结果（未超过存储字节上限时）；"
                "其他工具的 evidence 即本次返回内容。用 offset/limit 读取长结果，"
                "避免重跑浏览器/LLM。meta.evidence_stored=false 表示完整结果未入"
                "存储（响应本身即全部可得内容）。证据仅存于当前进程，重启后失效。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "evidence_id": {
                        "type": "string",
                        "description": "证据 id（形如 ev_0123456789abcdef）",
                    },
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": "字符偏移（Python 码点，默认 0）",
                    },
                    "limit": {
                        "type": "integer",
                        "default": _DEFAULT_EVIDENCE_PAGE,
                        "description": (
                            "本页最大字符数"
                            f"（1-{_MAX_EVIDENCE_PAGE}，默认 {_DEFAULT_EVIDENCE_PAGE}）"
                        ),
                    },
                },
                "required": ["evidence_id"],
            },
        },
        {
            "name": "list_evidence",
            "description": (
                "列出本会话登记的证据 id、来源工具、字节数与存储用量。"
                "用于发现可读取的 evidence_id，并如实报告被淘汰/拒收的条数。"
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]

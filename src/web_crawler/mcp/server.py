"""把 JS 逆向 Agent 的能力通过 MCP 协议暴露给 AI 客户端。

本模块实现一个 MCP（Model Context Protocol）服务器，将浏览器侧的 JS Hook
注入、网络请求捕获、webpack 模块拆分、AI 反混淆、算法重写、验证码处理等
能力封装为 MCP 工具，供 Claude Desktop、Cursor 等支持 MCP 的 AI 客户端
直接调用。

传输方式为 stdio（标准 MCP 传输）：客户端通过子进程方式拉起本服务，双方
以 JSON-RPC 2.0 over stdin/stdout 通信。当 ``mcp`` SDK 未安装时，自动降级
为手写的简化版 stdio 实现，覆盖 initialize / tools/list / tools/call 三类
核心方法，保证基本可用。

使用方式
--------
1. 安装 mcp SDK：``pip install mcp``
2. 设置 ``DEEPSEEK_API_KEY`` 环境变量
3. 在 AI 客户端的 MCP 配置中注册本服务：
   ``py -3.14 -m web_crawler.mcp.server`` 或直接调用 :func:`main`
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import Any

from ..ai.analyzer import JSAnalyzer, JSFragment
from ..ai.captcha import CaptchaManager, CaptchaType
from ..ai.hooks import HookLibrary, collect_hook_data, generate_combined_script
from ..ai.llm import get_provider
from ..fetchers.camoufox import CamoufoxFetcher

# ReverseAgent 模块在项目中可能尚未落地，做容错导入；缺失时相关工具降级。
try:  # pragma: no cover - 取决于项目是否已落地 reverse_agent
    from ..ai.reverse_agent import Action, Observation, ReverseAgent, ReverseAgentConfig

    _HAS_REVERSE_AGENT = True
except ImportError:  # pragma: no cover
    Action: Any = None  # type: ignore[no-redef]
    Observation: Any = None  # type: ignore[no-redef]
    ReverseAgent: Any = None  # type: ignore[no-redef]
    ReverseAgentConfig: Any = None  # type: ignore[no-redef]
    _HAS_REVERSE_AGENT = False

# MCP SDK 为可选依赖；缺失时走手动 stdio 实现的降级路径。
# 类型注解用 Any 以容纳 ImportError 降级路径下的 None 赋值（mypy strict 友好）。
Server: Any
stdio_server: Any
types: Any
try:  # pragma: no cover - 取决于是否安装了 mcp 包
    from mcp import types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    _HAS_MCP = True
except ImportError:  # pragma: no cover
    Server = None
    stdio_server = None
    types = None
    _HAS_MCP = False


_SERVER_NAME = "js-reverse-engineer"
_SERVER_VERSION = "0.1.0"
_PROTOCOL_VERSION = "2024-11-05"

# 浏览器侧默认采集等待时间（秒）
_DEFAULT_WAIT_TIME = 5.0
# Hook 数据预览条数上限
_PREVIEW_LIMIT = 10


# -- 序列化辅助 --------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    """dataclass / Enum 等对象的 JSON 序列化兜底。"""
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "value") and isinstance(obj.__class__, type):
        # 枚举类型
        if hasattr(obj.__class__, "__members__"):
            return obj.value
    return str(obj)


def _to_json(obj: Any) -> str:
    """序列化为 JSON 字符串，中文不转义，支持 dataclass / Enum。"""
    return json.dumps(obj, ensure_ascii=False, default=_json_default, indent=2)


def _error(error: str, **extra: Any) -> str:
    """构造标准错误响应 JSON。"""
    payload: dict[str, Any] = {"error": error}
    payload.update(extra)
    return _to_json(payload)


# -- MCP 服务器 --------------------------------------------------------------


class ReverseMCPServer:
    """通过 MCP 协议暴露 JS 逆向 Agent 能力的服务器。"""

    def __init__(self, provider_name: str = "deepseek", model: str = "deepseek-v4-pro") -> None:
        self.provider_name = provider_name
        self.model = model

        # LLM provider（DeepSeek / OpenAI 兼容）
        self.provider = get_provider(provider_name, model=model)

        # JS 分析引擎（webpack 拆分 + AI 反混淆 / 算法重写）
        self.analyzer = JSAnalyzer(provider=self.provider, model=model)

        # 验证码检测 + 处理
        self.captcha_manager = CaptchaManager()

        # ReverseAgent（可选）：缺失时 reverse_engineer_url 降级为基本采集
        self.agent = self._create_agent()

        # CamoufoxFetcher 实例（lazy 创建，跨工具调用复用同一浏览器）
        self._fetcher: CamoufoxFetcher | None = None
        self._closed = False

    # -- 资源管理 ----------------------------------------------------------

    def _create_agent(self) -> Any | None:
        """尝试创建 ReverseAgent 实例；模块缺失或接口不匹配时返回 None。"""
        if not _HAS_REVERSE_AGENT or ReverseAgent is None or ReverseAgentConfig is None:
            return None
        try:
            config = ReverseAgentConfig()
            return ReverseAgent(config=config, provider=self.provider)
        except Exception:
            return None

    def _get_fetcher(self) -> CamoufoxFetcher:
        """获取复用的 CamoufoxFetcher 实例。"""
        if self._fetcher is None:
            self._fetcher = CamoufoxFetcher(headless=True, network_idle=False)
        return self._fetcher

    def _run_browser_task(
        self,
        url: str,
        task_fn: Callable[[Any], Any],
        hooks: list[str] | None = None,
        wait_time: float = 0.0,
    ) -> Any:
        """在浏览器中执行一次性任务：打开页面 → 注入 Hook → 执行 task_fn → 关闭 context。

        浏览器实例跨调用复用，仅 context 每次新建并关闭，避免 cookie/state 泄漏。
        ``task_fn`` 接收 Playwright Page 对象，返回值原样透传。
        """
        fetcher = self._get_fetcher()
        # _ensure_browser 是 CamoufoxFetcher 的 protected 方法，这里需要拿到
        # browser 句柄以自定义 context（注入 Hook、读取 hook 数据等）。
        browser = fetcher._ensure_browser()
        context = browser.new_context(
            extra_http_headers=fetcher.extra_headers or None,
            ignore_https_errors=not fetcher.verify,
        )
        try:
            page = context.new_page()
            if hooks:
                page.add_init_script(generate_combined_script(hooks))
            page.goto(url, wait_until="domcontentloaded", timeout=fetcher.timeout * 1000)
            if wait_time > 0:
                page.wait_for_timeout(int(wait_time * 1000))
            return task_fn(page)
        finally:
            context.close()

    def close(self) -> None:
        """清理所有资源：关闭浏览器、标记服务已关闭。"""
        if self._fetcher is not None:
            try:
                self._fetcher.close()
            except Exception:
                pass
            self._fetcher = None
        self._closed = True

    # -- 工具列表 ------------------------------------------------------------

    def get_tools(self) -> list[dict]:
        """返回 MCP 工具列表（每个工具包含 name / description / inputSchema）。"""
        return [
            {
                "name": "reverse_engineer_url",
                "description": (
                    "逆向分析指定 URL 的加密参数。自动走完 Hook 注入 → 请求捕获 → "
                    "JS 分析 → 算法重写的完整流程，返回加密参数的生成链路。"
                    "结果包含 budget_summary（token 预算汇总）、last_confidence"
                    "（最近一次置信度评分）、checkpoints（断点列表）、screenshots"
                    "（每步截图路径）与 error_screenshot（错误截图）字段，"
                    "上游 AI 一次调用即可拿到全部运行时状态。"
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
                    "执行流程与置信度。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "JS 代码片段"},
                        "url": {"type": "string", "description": "代码来源 URL（可选）"},
                        "target_param": {"type": "string", "description": "目标参数名（可选）"},
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
                "description": "用 AI 反混淆 JS 代码，返回可读的等价版本。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "混淆后的 JS 代码"},
                    },
                    "required": ["code"],
                },
            },
            {
                "name": "reimplement_algorithm",
                "description": "用指定语言重写 JS 加密逻辑，输出可独立运行的等价代码。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "JS 加密逻辑代码"},
                        "language": {
                            "type": "string",
                            "default": "python",
                            "description": "目标语言",
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
                            "description": "click 模式下的提示文字（如\"请按顺序点击图中所有的红绿灯\"）",
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
                            "items": {"type": "string", "enum": ["ports", "dirs", "subdomains", "vulns", "headers"]},
                            "description": "要执行的检查项列表，留空执行全部",
                        },
                        "ports": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "自定义端口列表（仅 ports 检查，留空用 TOP 100）",
                        },
                        "timeout": {
                            "type": "number",
                            "default": 30.0,
                            "description": "整体超时（秒）",
                        },
                    },
                    "required": ["target"],
                },
            },
            {
                "name": "capture_network_requests",
                "description": "捕获页面加载过程中的网络请求（url、method、headers、body）。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "目标页面 URL"},
                        "wait_time": {
                            "type": "number",
                            "default": _DEFAULT_WAIT_TIME,
                            "description": "采集等待时间（秒）",
                        },
                    },
                    "required": ["url"],
                },
            },
            {
                "name": "get_page_scripts",
                "description": "获取页面加载的 JS 脚本 URL 列表。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "目标页面 URL"},
                    },
                    "required": ["url"],
                },
            },
        ]

    # -- Prompts / Resources / Progress --------------------------------------

    def get_prompts(self) -> list[dict]:
        """返回 MCP prompts 列表（预定义 prompt 模板）。"""
        return [
            {
                "name": "reverse_engineer_url",
                "description": (
                    "逆向分析指定 URL 的加密参数。返回一段可直接交给 Agent 执行的"
                    "任务描述，包含 URL、目标参数列表与执行约束。"
                ),
                "arguments": [
                    {
                        "name": "url",
                        "description": "目标页面 URL",
                        "required": True,
                    },
                    {
                        "name": "target_params",
                        "description": "重点关注的加密参数名（逗号分隔，如 Anti-Content,X-Bogus）",
                        "required": False,
                    },
                    {
                        "name": "max_steps",
                        "description": "Agent 最大执行步数（默认 20）",
                        "required": False,
                    },
                ],
            },
            {
                "name": "deobfuscate_js",
                "description": "反混淆 JS 代码片段，返回可读的等价版本与算法说明。",
                "arguments": [
                    {
                        "name": "code",
                        "description": "待反混淆的 JS 代码（可多行）",
                        "required": True,
                    },
                    {
                        "name": "focus_param",
                        "description": "重点关注的参数名（可选，用于定向反混淆）",
                        "required": False,
                    },
                ],
            },
            {
                "name": "reimplement_algorithm",
                "description": "把 JS 加密逻辑等价改写为指定语言（默认 Python）。",
                "arguments": [
                    {
                        "name": "code",
                        "description": "JS 加密逻辑代码",
                        "required": True,
                    },
                    {
                        "name": "language",
                        "description": "目标语言（python / go / node / rust）",
                        "required": False,
                    },
                ],
            },
        ]

    def render_prompt(self, name: str, arguments: dict) -> str:
        """渲染 prompt 模板，返回拼好的字符串。"""
        if name == "reverse_engineer_url":
            url = arguments.get("url", "")
            params = arguments.get("target_params", "")
            max_steps = arguments.get("max_steps", "20")
            param_list = [p.strip() for p in params.split(",") if p.strip()] if params else []
            param_str = "、".join(param_list) if param_list else "(自动识别)"
            return (
                f"请对以下 URL 做加密参数逆向分析：\n\n"
                f"- URL: {url}\n"
                f"- 目标参数: {param_str}\n"
                f"- 最大步数: {max_steps}\n\n"
                "执行约束：\n"
                "1. 优先注入 fetch_hook / xhr_hook / cookie_hook / crypto_subtle_hook，"
                "确保捕获所有出站请求与加密入参；\n"
                "2. 若发现 webpack 打包痕迹，提取入口模块并定位签名函数；\n"
                "3. 若出现验证码，先检测类型再尝试模拟用户交互（不破解图片挑战）；\n"
                "4. 任务完成时给出 success=true 与每个目标参数的取值，并说明来源 hook 记录。"
            )
        if name == "deobfuscate_js":
            code = arguments.get("code", "")
            focus = arguments.get("focus_param", "")
            focus_line = f"\n重点关注参数：{focus}" if focus else ""
            return (
                "请反混淆以下 JS 代码片段，输出可读的等价版本，并简要说明其中"
                "包含的加密算法（如 AES / RSA / HMAC / 自定义签名）。\n"
                f"{focus_line}\n\n"
                f"```\n{code}\n```"
            )
        if name == "reimplement_algorithm":
            code = arguments.get("code", "")
            language = arguments.get("language", "python")
            return (
                f"请把以下 JS 加密逻辑等价改写为 {language} 代码，"
                "要求可独立运行、依赖尽量少、保留原算法的输入输出语义：\n\n"
                f"```\n{code}\n```"
            )
        return f"unknown prompt: {name}"

    def get_resources(self) -> list[dict]:
        """返回 MCP resources 列表（动态资源模板）。"""
        return [
            {
                "uri": "agent://state",
                "name": "agent_state",
                "description": "当前 Agent 状态：是否已加载 ReverseAgent、provider 名、模型名",
                "mimeType": "application/json",
            },
            {
                "uri": "agent://history",
                "name": "agent_history",
                "description": "最近一次 reverse_engineer_url 调用的执行历史（step / action / observation）",
                "mimeType": "application/json",
            },
            {
                "uri": "hooks://library",
                "name": "hooks_library",
                "description": "可用的 Hook 名称列表与说明",
                "mimeType": "application/json",
            },
            {
                "uri": "schema://extracted_params",
                "name": "extracted_params_schema",
                "description": "ExtractedParams Pydantic schema 的 JSON Schema 定义",
                "mimeType": "application/json",
            },
        ]

    def read_resource(self, uri: str) -> str:
        """读取资源内容，返回 JSON 字符串。"""
        if uri == "agent://state":
            return _to_json(
                {
                    "has_reverse_agent": self.agent is not None,
                    "provider": getattr(self.provider, "name", str(self.provider)),
                    "model": getattr(self.provider, "model", ""),
                    "browser_reused": self._fetcher is not None,
                    "closed": self._closed,
                }
            )
        if uri == "agent://history":
            # 最近一次 reverse_engineer_url 调用历史；当前未持久化时返回空
            return _to_json({"history": [], "note": "history is per-call, not persisted"})
        if uri == "hooks://library":
            return _to_json({"hooks": HookLibrary.names()})
        if uri == "schema://extracted_params":
            try:
                from ..ai.schema import ExtractedParams

                if hasattr(ExtractedParams, "model_json_schema"):
                    return _to_json(ExtractedParams.model_json_schema())
                return _to_json({"note": "pydantic not available, schema unavailable"})
            except Exception as exc:
                return _error("schema unavailable", details=str(exc))
        return _error(f"unknown resource uri: {uri}")

    def make_progress_token(self, tool_name: str, total: int = 1) -> dict:
        """构造一个 progress token，供 MCP 客户端跟踪长任务进度。

        返回的 dict 形如 ``{"progressToken": "tool_name-<ts>", "total": total}``，
        调用方在工具执行过程中通过 :meth:`report_progress` 推送进度。
        """
        import time as _time

        return {
            "progressToken": f"{tool_name}-{int(_time.time() * 1000)}",
            "total": total,
        }

    def report_progress(self, token: str, current: int, total: int, *, message: str = "") -> None:
        """发送进度通知（仅在 mcp SDK 可用时生效；否则写入 stderr 日志）。"""
        if not _HAS_MCP:
            sys.stderr.write(f"[progress] {token}: {current}/{total} {message}\n")
            sys.stderr.flush()
            return
        # mcp SDK 的进度通知需要 server.run() 的上下文，这里仅做记录；
        # 真正发送由 _run_mcp 内的 progress 闭包负责（见 _run_mcp）。
        sys.stderr.write(f"[progress] {token}: {current}/{total} {message}\n")
        sys.stderr.flush()

    # -- 工具调用分发 --------------------------------------------------------

    def handle_tool(self, name: str, arguments: dict) -> str:
        """根据工具名分发调用，返回 JSON 字符串。"""
        handlers: dict[str, Callable[[dict], str]] = {
            "reverse_engineer_url": self._tool_reverse_engineer_url,
            "inject_hooks": self._tool_inject_hooks,
            "analyze_js_code": self._tool_analyze_js_code,
            "extract_webpack_modules": self._tool_extract_webpack_modules,
            "deobfuscate_js": self._tool_deobfuscate_js,
            "reimplement_algorithm": self._tool_reimplement_algorithm,
            "solve_captcha": self._tool_solve_captcha,
            "solve_captcha_image": self._tool_solve_captcha_image,
            "pentest_recon": self._tool_pentest_recon,
            "capture_network_requests": self._tool_capture_network_requests,
            "get_page_scripts": self._tool_get_page_scripts,
        }
        handler = handlers.get(name)
        if handler is None:
            return _error(f"unknown tool: {name}")
        try:
            return handler(arguments or {})
        except TimeoutError as exc:
            return _error("timeout", details=str(exc))
        except ImportError as exc:
            return _error("dependency missing", details=str(exc))
        except RuntimeError as exc:
            # LLM 调用失败 / 浏览器运行时错误
            return _error("runtime error", details=str(exc))
        except Exception as exc:
            return _error(str(exc), traceback=traceback.format_exc())

    # -- 各工具实现 ----------------------------------------------------------

    def _tool_reverse_engineer_url(self, args: dict) -> str:
        url = args["url"]
        target_params = args.get("target_params") or []
        task = args.get("task") or f"分析 {url} 的加密参数"
        max_steps = args.get("max_steps", 20)

        # 构造 progress token，让 MCP 客户端可订阅长任务进度
        progress = self.make_progress_token("reverse_engineer_url", total=max_steps)

        # 优先使用 ReverseAgent 走完整逆向流程
        if self.agent is not None:
            try:
                # ReverseAgent.run 签名为 run(url, task="")，max_steps 与
                # target_params 通过 config 注入；这里 clone config 再覆盖。
                from ..ai.reverse_agent import ReverseAgentConfig
                from ..ai.watchdog import EventBus

                base_config = self.agent.config
                run_config = ReverseAgentConfig(
                    max_steps=max_steps,
                    hooks=list(base_config.hooks) if base_config.hooks else None,
                    headless=base_config.headless,
                    wait_after_navigate=base_config.wait_after_navigate,
                    target_params=list(target_params) if target_params else None,
                    proxy=base_config.proxy,
                    os_name=base_config.os_name,
                )
                # 用同一 provider/analyzer 复用，避免重复加载模型配置
                # 注入独立 EventBus，订阅 step.end 事件推送 MCP progress
                progress_bus = EventBus()

                def _on_step_end(evt: Any) -> None:
                    step = getattr(evt, "step", 0) or 0
                    self.report_progress(
                        progress["progressToken"],
                        step,
                        max_steps,
                        message=f"step {step}/{max_steps}: {getattr(evt, 'type', '')}",
                    )

                progress_bus.subscribe(_on_step_end)
                run_agent = type(self.agent)(
                    config=run_config,
                    provider=self.agent.provider,
                    analyzer=self.agent.analyzer,
                    event_bus=progress_bus,
                )
                result = run_agent.run(url=url, task=task)
                self.report_progress(
                    progress["progressToken"], max_steps, max_steps, message="done"
                )
                return _to_json({"result": result, "agent": True})
            except Exception as exc:
                fallback_note = f"ReverseAgent 执行失败：{exc}，降级为基本采集"

        # 降级：注入全部 Hook + 采集网络请求 + 获取脚本列表
        all_hooks = HookLibrary.names()

        def _collect(page: Any) -> dict:
            hook_data = collect_hook_data(page)
            scripts = page.evaluate(
                """() => Array.from(document.scripts)
                    .map(s => s.src || '')
                    .filter(s => s)"""
            )
            return {
                "hook_records": hook_data["records"],
                "hook_count": hook_data["count"],
                "scripts": scripts,
            }

        try:
            collected = self._run_browser_task(
                url, _collect, hooks=all_hooks, wait_time=_DEFAULT_WAIT_TIME
            )
        except Exception as exc:
            return _error("浏览器操作失败", details=str(exc))

        note = (
            "ReverseAgent 模块不可用，仅返回基本采集结果" if self.agent is None else fallback_note
        )
        return _to_json(
            {
                "agent": False,
                "note": note,
                "url": url,
                "target_params": target_params,
                "task": task,
                **collected,
            }
        )

    def _tool_inject_hooks(self, args: dict) -> str:
        url = args["url"]
        hooks = args.get("hooks") or HookLibrary.names()
        # 校验 hook 名称，剔除未知项
        valid_names = set(HookLibrary.names())
        invalid = [h for h in hooks if h not in valid_names]
        hooks = [h for h in hooks if h in valid_names]
        if not hooks:
            return _error("no valid hooks specified", valid_hooks=list(valid_names))

        def _collect(page: Any) -> list[dict]:
            data = collect_hook_data(page)
            return data["records"][:_PREVIEW_LIMIT]

        try:
            preview = self._run_browser_task(url, _collect, hooks=hooks, wait_time=3.0)
        except Exception as exc:
            return _error("浏览器操作失败", details=str(exc))

        return _to_json(
            {
                "injected": hooks,
                "invalid_hooks": invalid,
                "preview_count": len(preview),
                "preview": preview,
            }
        )

    def _tool_analyze_js_code(self, args: dict) -> str:
        code = args["code"]
        url = args.get("url", "")
        target_param = args.get("target_param")

        fragment = JSFragment(source=code, url=url, is_minified=len(code) < 5000)
        try:
            result = self.analyzer.analyze_fragment(fragment)
        except Exception as exc:
            return _error("LLM call failed", details=str(exc))

        payload = {
            "algorithm": result.algorithm,
            "inputs": result.inputs,
            "output": result.output,
            "code_flow": result.code_flow,
            "confidence": result.confidence,
            "deobfuscated": result.deobfuscated,
        }
        if target_param:
            payload["target_param"] = target_param
        return _to_json(payload)

    def _tool_extract_webpack_modules(self, args: dict) -> str:
        source = args["source"]
        modules = self.analyzer.extract_webpack_modules(source)
        entry = self.analyzer.identify_entry_point(modules)
        return _to_json(
            {
                "modules": [
                    {
                        "id": m.id,
                        "dependencies": m.dependencies,
                        "exports": m.exports,
                        "source_preview": m.source[:500],
                        "source_length": len(m.source),
                    }
                    for m in modules
                ],
                "count": len(modules),
                "entry_point": entry,
            }
        )

    def _tool_deobfuscate_js(self, args: dict) -> str:
        code = args["code"]
        try:
            deobfuscated = self.analyzer.deobfuscate(code)
        except Exception as exc:
            return _error("LLM call failed", details=str(exc))
        return _to_json({"deobfuscated": deobfuscated, "length": len(deobfuscated)})

    def _tool_reimplement_algorithm(self, args: dict) -> str:
        code = args["code"]
        language = args.get("language", "python")
        try:
            reimplemented = self.analyzer.suggest_reimplementation(code, language=language)
        except Exception as exc:
            return _error("LLM call failed", details=str(exc))
        return _to_json({"language": language, "code": reimplemented, "length": len(reimplemented)})

    def _tool_solve_captcha(self, args: dict) -> str:
        url = args["url"]

        def _detect_and_solve(page: Any) -> dict:
            info = self.captcha_manager.detector.detect(page)
            if info is None or info.type is CaptchaType.NONE:
                return {
                    "type": "none",
                    "solved": True,
                    "message": "未检测到验证码",
                }
            solved = self.captcha_manager.solver.solve(page, info)
            return {
                "type": info.type.value,
                "iframe_url": info.iframe_url,
                "site_key": info.site_key,
                "container_selector": info.container_selector,
                "solved": solved,
                "message": "已处理" if solved else "需要人工介入",
            }

        try:
            result = self._run_browser_task(url, _detect_and_solve, wait_time=2.0)
        except Exception as exc:
            return _error("浏览器操作失败", details=str(exc))
        return _to_json(result)

    def _tool_solve_captcha_image(self, args: dict) -> str:
        """直接识别图片验证码（无需浏览器）。

        支持 text / slider / click 三种模式，自动协商 LLM Vision 能力。
        """
        from ..ai.image_captcha import ImageCaptchaSolver, ImageSolverConfig

        mode = args.get("mode", "").lower()
        if mode not in ("text", "slider", "click"):
            return _error("mode must be one of: text / slider / click")
        mime = args.get("mime", "image/png")

        # 用 self.provider（可能支持 vision）构造 ImageCaptchaSolver
        cfg = ImageSolverConfig()
        solver = ImageCaptchaSolver(provider=self.provider, config=cfg)

        try:
            if mode == "text":
                image = args.get("image", "")
                if not image:
                    return _error("image is required for text mode")
                text = solver.solve_text(image, mime=mime)
                return _to_json({"mode": "text", "text": text, "ok": bool(text)})
            if mode == "slider":
                bg = args.get("bg", "")
                slider = args.get("slider", "")
                if not bg or not slider:
                    return _error("bg and slider are required for slider mode")
                sol = solver.solve_slider(bg, slider)
                if sol is None:
                    return _to_json({"mode": "slider", "ok": False, "message": "识别失败"})
                return _to_json(
                    {
                        "mode": "slider",
                        "ok": True,
                        "x": sol.x,
                        "y": sol.y,
                        "method": sol.method,
                        "confidence": sol.confidence,
                    }
                )
            # click
            image = args.get("image", "")
            prompt = args.get("prompt", "")
            if not image:
                return _error("image is required for click mode")
            click_sol = solver.solve_click(image, prompt, mime=mime)
            if click_sol is None:
                return _to_json({"mode": "click", "ok": False, "message": "识别失败"})
            return _to_json(
                {
                    "mode": "click",
                    "ok": True,
                    "points": [{"x": x, "y": y} for x, y in click_sol.points],
                    "labels": click_sol.labels,
                    "method": click_sol.method,
                }
            )
        except Exception as exc:
            return _error("captcha image solve failed", details=str(exc))

    def _tool_pentest_recon(self, args: dict) -> str:
        """对指定目标执行渗透侦察（端口/目录/子域名/漏洞/安全头）。

        合规声明：仅用于已获书面授权的目标。
        """
        from urllib.parse import urlparse

        from ..pentest import (
            DirBruter,
            HeaderChecker,
            PentestReport,
            PortScanner,
            SubdomainEnumerator,
            VulnScanner,
        )

        target = args.get("target", "").strip()
        if not target:
            return _error("target is required")
        # 从 URL 提取 host，便于端口扫描与子域名枚举
        if "://" in target:
            parsed = urlparse(target)
            host = parsed.hostname or target
            base_url = target if target.endswith("/") else target + "/"
        else:
            host = target
            base_url = f"https://{host}/"
        checks = args.get("checks") or ["ports", "dirs", "subdomains", "vulns", "headers"]
        custom_ports = args.get("ports")

        report = PentestReport(target=target)
        try:
            if "ports" in checks:
                scanner = PortScanner()
                ports_to_scan = custom_ports if custom_ports else None
                report.port_scan = scanner.scan(host, ports_to_scan)
            if "dirs" in checks:
                bruter = DirBruter()
                report.dir_brute = bruter.brute(base_url)
            if "subdomains" in checks:
                enumerator = SubdomainEnumerator()
                report.subdomains = enumerator.enumerate(host)
            if "vulns" in checks:
                vuln_scanner = VulnScanner()
                report.vulns = vuln_scanner.scan_url(base_url)
            if "headers" in checks:
                checker = HeaderChecker()
                report.headers = checker.check(base_url)
        except Exception as exc:
            return _error("pentest recon failed", details=str(exc))
        return _to_json(report.to_dict())

    def _tool_capture_network_requests(self, args: dict) -> str:
        url = args["url"]
        wait_time = args.get("wait_time", _DEFAULT_WAIT_TIME)
        hooks = ["fetch_hook", "xhr_hook"]

        def _capture(page: Any) -> list[dict]:
            return collect_hook_data(page)["records"]

        try:
            records = self._run_browser_task(url, _capture, hooks=hooks, wait_time=wait_time)
        except Exception as exc:
            return _error("浏览器操作失败", details=str(exc))
        return _to_json({"requests": records, "count": len(records)})

    def _tool_get_page_scripts(self, args: dict) -> str:
        url = args["url"]

        def _get_scripts(page: Any) -> list[dict]:
            return page.evaluate(
                """() => Array.from(document.scripts)
                    .filter(s => s.src)
                    .map(s => ({
                        src: s.src,
                        type: s.type || '',
                        async: !!s.async,
                        defer: !!s.defer
                    }))"""
            )

        try:
            scripts = self._run_browser_task(url, _get_scripts, wait_time=2.0)
        except Exception as exc:
            return _error("浏览器操作失败", details=str(exc))
        return _to_json({"scripts": scripts, "count": len(scripts)})

    # -- MCP 协议入口 --------------------------------------------------------

    def run(self) -> None:
        """启动 MCP stdio 服务器。mcp SDK 可用时走官方实现，否则降级。"""
        if _HAS_MCP:
            asyncio.run(self._run_mcp())
        else:
            self._run_stdio_manual()

    def serve(self) -> None:
        """run 的别名。"""
        self.run()

    async def _run_mcp(self) -> None:
        """基于 mcp SDK 的 stdio 服务器实现。"""
        server: Any = Server(_SERVER_NAME)

        @server.list_tools()
        async def _list_tools() -> list[Any]:
            return [types.Tool(**tool) for tool in self.get_tools()]

        @server.call_tool()
        async def _call_tool(name: str, arguments: dict | None) -> list[Any]:
            result = self.handle_tool(name, arguments or {})
            return [types.TextContent(type="text", text=result)]

        # prompts：预定义 prompt 模板，供 MCP 客户端按场景渲染
        @server.list_prompts()
        async def _list_prompts() -> list[Any]:
            return [
                types.Prompt(
                    name=p["name"],
                    description=p.get("description", ""),
                    arguments=[
                        types.PromptArgument(
                            name=a["name"],
                            description=a.get("description", ""),
                            required=a.get("required", False),
                        )
                        for a in p.get("arguments", [])
                    ],
                )
                for p in self.get_prompts()
            ]

        @server.get_prompt()
        async def _get_prompt(name: str, arguments: dict | None) -> Any:
            rendered = self.render_prompt(name, arguments or {})
            return types.GetPromptResult(
                description=f"prompt {name}",
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(type="text", text=rendered),
                    )
                ],
            )

        # resources：暴露 Agent 状态、历史、Hook 列表、schema 定义
        @server.list_resources()
        async def _list_resources() -> list[Any]:
            return [
                types.Resource(
                    uri=r["uri"],
                    name=r["name"],
                    description=r.get("description", ""),
                    mimeType=r.get("mimeType", "application/json"),
                )
                for r in self.get_resources()
            ]

        @server.read_resource()
        async def _read_resource(uri: str) -> str:
            return self.read_resource(uri)

        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    def _run_stdio_manual(self) -> None:
        """mcp SDK 不可用时的降级 stdio 实现。

        手动读取 stdin / 写入 stdout，按 JSON-RPC 2.0 协议处理 initialize、
        tools/list、tools/call 三类核心方法。每行一个 JSON-RPC 消息。
        """
        if not _HAS_MCP:
            sys.stderr.write(
                f"[{_SERVER_NAME}] mcp SDK 未安装，使用降级 stdio 实现。"
                "建议 `pip install mcp` 以获得完整协议支持。\n"
            )
            sys.stderr.flush()

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(request, dict):
                continue

            req_id = request.get("id")
            method = request.get("method")
            params = request.get("params") or {}
            response = self._handle_jsonrpc(method, params, req_id)
            if response is None:
                # 通知（无 id）不回复
                continue
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def _handle_jsonrpc(self, method: str | None, params: dict, req_id: Any) -> dict | None:
        """处理单条 JSON-RPC 请求，返回响应 dict（通知返回 None）。"""
        # 通知（无 id）不需要回复
        if req_id is None:
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {},
                        "prompts": {},
                        "resources": {},
                    },
                    "serverInfo": {
                        "name": _SERVER_NAME,
                        "version": _SERVER_VERSION,
                    },
                },
            }

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": self.get_tools()},
            }

        if method == "tools/call":
            tool_name = params.get("name")
            if not isinstance(tool_name, str):
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32602,
                        "message": "invalid params: missing or invalid 'name'",
                    },
                }
            arguments = params.get("arguments") or {}
            result = self.handle_tool(tool_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": result}],
                    "isError": False,
                },
            }

        if method == "prompts/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"prompts": self.get_prompts()},
            }

        if method == "prompts/get":
            prompt_name = params.get("name")
            if not isinstance(prompt_name, str):
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32602,
                        "message": "invalid params: missing or invalid 'name'",
                    },
                }
            arguments = params.get("arguments") or {}
            rendered = self.render_prompt(prompt_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "description": f"prompt {prompt_name}",
                    "messages": [{"role": "user", "content": {"type": "text", "text": rendered}}],
                },
            }

        if method == "resources/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"resources": self.get_resources()},
            }

        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str):
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32602,
                        "message": "invalid params: missing or invalid 'uri'",
                    },
                }
            content = self.read_resource(uri)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [{"uri": uri, "mimeType": "application/json", "text": content}]
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": f"method not found: {method}",
            },
        }


# -- 入口函数 ----------------------------------------------------------------


def main() -> None:
    """独立入口：读取环境变量，创建服务器并启动。"""
    if not os.environ.get("DEEPSEEK_API_KEY"):
        sys.stderr.write("warning: DEEPSEEK_API_KEY 环境变量未设置，LLM 相关工具将不可用。\n")
        sys.stderr.flush()
    server = ReverseMCPServer()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


__all__ = ["ReverseMCPServer", "main"]

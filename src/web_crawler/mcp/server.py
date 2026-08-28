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

import os
import sys
import threading
import traceback
from collections.abc import Callable
from typing import Any

from ..ai.analyzer import JSAnalyzer
from ..ai.captcha import CaptchaManager
from ..ai.hooks import HookLibrary, generate_combined_script
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
    from mcp import types  # type: ignore[no-redef]
    from mcp.server import Server  # type: ignore[no-redef]
    from mcp.server.stdio import stdio_server  # type: ignore[no-redef]

    _HAS_MCP = True
except ImportError:  # pragma: no cover
    Server = None
    stdio_server = None
    types = None
    _HAS_MCP = False


_SERVER_NAME = "js-reverse-engineer"
_SERVER_VERSION = "0.1.0"
_PROTOCOL_VERSION = "2024-11-05"


# -- 工具参数校验 -------------------------------------------------------------
#
# handle_tool 分发前按 inputSchema 做必需字段/类型/枚举校验，并施加统一输入
# 上限（LLM 输入、图片 base64 等），避免恶意/异常客户端造成资源滥用。

# 字符串字段大小上限（字符数）：LLM 输入与图片 base64。
_FIELD_LIMITS: dict[str, int] = {
    "code": 2_000_000,  # analyze_js_code / deobfuscate_js / reimplement_algorithm
    "source": 5_000_000,  # extract_webpack_modules
    "image": 5_000_000,  # solve_captcha_image base64
    "bg": 5_000_000,
    "slider": 5_000_000,
}


# -- 从子模块汇入(保持历史导入路径可用) ------------------------------------
from ._server_helpers import (  # noqa: F401
    _DEFAULT_PAGE_SIZE,
    _DEFAULT_TEXT_LIMIT,
    _DEFAULT_WAIT_TIME,
    _MAX_PAGE_SIZE,
    _MAX_TEXT_LIMIT,
    _PREVIEW_LIMIT,
    _clamp_int,
    _error,
    _json_default,
    _paginate,
    _to_json,
    _truncate_result_strings,
    _truncate_text,
    _type_ok,
)
from ._specs import build_tool_definitions
from ._ssrf_gate import (  # noqa: F401
    _check_target_public,
    _check_url,
    _host_is_public,
    _ip_is_global,
    _resolve_host_ips,
)
from ._tools_pentest import PentestToolsMixin, _parse_pentest_target  # noqa: F401
from ._tools_reverse import ReverseToolsMixin
from ._transport import (
    StdioTransportMixin,
)

# -- MCP 服务器 --------------------------------------------------------------


class ReverseMCPServer(ReverseToolsMixin, PentestToolsMixin, StdioTransportMixin):
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
        # Playwright sync API 非线程安全：并发工具调用（to_thread 线程池）串行化浏览器访问
        self._browser_lock = threading.Lock()
        # SDK 路径的进度推送 sender（由 _call_tool 按请求注册/注销）
        self._progress_sender: Callable[[int, int, str], None] | None = None
        self._progress_lock = threading.Lock()
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

    def _run_browser_task(  # pragma: no cover - 需真实 Playwright 浏览器，属集成测试范畴
        self,
        url: str,
        task_fn: Callable[[Any], Any],
        hooks: list[str] | None = None,
        wait_time: float = 0.0,
    ) -> Any:
        """在浏览器中执行一次性任务：打开页面 → 注入 Hook → 执行 task_fn → 关闭 context。

        浏览器实例跨调用复用，仅 context 每次新建并关闭，避免 cookie/state 泄漏。
        ``task_fn`` 接收 Playwright Page 对象，返回值原样透传。

        安全门禁：URL 必须通过 :func:`_check_url`（仅 http/https、公网主机、
        无 userinfo），且整个任务在 ``_browser_lock`` 内串行执行 —— Playwright
        sync API 非线程安全，跨线程共享同一浏览器实例会崩溃。
        """
        gate_error = _check_url(url)
        if gate_error:
            raise ValueError(gate_error)
        fetcher = self._get_fetcher()
        with self._browser_lock:
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
        """返回 MCP 工具列表(每个工具包含 name / description / inputSchema)。"""
        return build_tool_definitions()

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
        """发送进度通知（SDK 路径经 :meth:`_run_mcp` 注册的 sender 推送；否则写 stderr 日志）。"""
        with self._progress_lock:
            sender = self._progress_sender
        if sender is not None:
            try:
                sender(current, total, message)
                return
            except Exception:
                pass  # 推送失败降级到 stderr 日志
        sys.stderr.write(f"[progress] {token}: {current}/{total} {message}\n")
        sys.stderr.flush()

    # -- 工具调用分发 --------------------------------------------------------

    def _validate_tool_args(self, name: str, args: dict) -> str | None:
        """按 inputSchema 校验工具参数：必需字段、类型、枚举与输入上限。

        返回错误信息或 None。未知工具/无 schema 时不校验（由分发表兜底）。
        """
        schema = next(
            (t["inputSchema"] for t in self.get_tools() if t["name"] == name),
            None,
        )
        if schema is None:
            return None
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in args:
                return f"invalid params: missing required argument {key!r}"
        for key, value in args.items():
            if key not in props:
                continue
            spec = props[key]
            ptype = spec.get("type")
            if value is None:
                continue
            if not _type_ok(ptype, value):
                return f"invalid params: argument {key!r} must be {ptype}"
            if ptype == "string" and "enum" in spec and value not in spec["enum"]:
                return f"invalid params: argument {key!r} must be one of {spec['enum']}"
            if key in _FIELD_LIMITS and isinstance(value, str) and len(value) > _FIELD_LIMITS[key]:
                return f"invalid params: argument {key!r} exceeds size limit {_FIELD_LIMITS[key]}"
            if ptype == "array" and "items" in spec:
                item_type = spec["items"].get("type")
                bad = [v for v in value if not _type_ok(item_type, v)]
                if bad:
                    return f"invalid params: argument {key!r} items must be {item_type}"
        return None

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
        args = arguments if isinstance(arguments, dict) else {}
        validation_error = self._validate_tool_args(name, args)
        if validation_error:
            return _error(validation_error, code=-32602)
        try:
            return handler(args)
        except TimeoutError as exc:
            return _error("timeout", details=str(exc))
        except ImportError as exc:
            return _error("dependency missing", details=str(exc))
        except RuntimeError as exc:
            # LLM 调用失败 / 浏览器运行时错误
            return _error("runtime error", details=str(exc))
        except Exception as exc:
            # 完整 traceback 只写 stderr 日志，客户端仅收到错误类别（防内部信息泄露）
            sys.stderr.write(f"[mcp] tool {name} failed:\n{traceback.format_exc()}\n")
            sys.stderr.flush()
            return _error("internal error", error_type=type(exc).__name__)

    # -- 各工具实现 ----------------------------------------------------------


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


if __name__ == "__main__":  # pragma: no cover
    main()

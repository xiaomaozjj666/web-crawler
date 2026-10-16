"""MCP stdio 传输层(从 ``server.py`` 拆出)。

SDK 路径(mcp 包)与手写 JSON-RPC 降级路径都以 Mixin 方法挂在宿主上。
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from ._host import HostContract

# MCP SDK 为可选依赖;缺失时走手动 stdio 实现的降级路径。
# 类型注解用 Any 以容纳 ImportError 降级路径下的 None 赋值(mypy strict 友好)。
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


class StdioTransportMixin(HostContract):
    """stdio 传输:MCP SDK 路径与手写 JSON-RPC 降级路径。"""

    def run(self) -> None:
        """启动 MCP stdio 服务器。mcp SDK 可用时走官方实现，否则降级。"""
        if _HAS_MCP:
            asyncio.run(self._run_mcp())
        else:
            self._run_stdio_manual()

    def serve(self) -> None:
        """run 的别名。"""
        self.run()

    async def _run_mcp(self) -> None:  # pragma: no cover - 需真实 MCP 客户端连接，属集成测试范畴
        """基于 mcp SDK 的 stdio 服务器实现。"""
        # 1.x 有装饰器 API（list_tools / call_tool …）；2.x 改成构造回调 + add_request_handler。
        # 判据优先认 1.x：MagicMock 之类「什么属性都有」的替身会同时满足两条判据，认 1.x 能保持
        # 既有测试与旧 SDK 的行为不变；真 SDK 的两种形态互斥，判据不会有歧义。
        if hasattr(Server, "list_tools"):
            await self._run_mcp_v1()
        else:
            await self._run_mcp_v2()

    def _make_progress_sender(self, ctx: Any) -> Any:
        """从 mcp 请求上下文取 progressToken / session，返回进度回调（无订阅时返回 None）。

        1.x 的 meta 是模型对象（属性 ``progressToken``），2.x 的 meta 是 TypedDict（键同名），
        两种形态都兼容；缺 session 或客户端没带 progressToken 时返回 None，调用方按
        「本次没有进度推送」处理。
        """
        meta = getattr(ctx, "meta", None)
        if isinstance(meta, dict):
            progress_token = meta.get("progressToken") or meta.get("progress_token")
        else:
            progress_token = getattr(meta, "progressToken", None) or getattr(
                meta, "progress_token", None
            )
        session = getattr(ctx, "session", None)
        if (
            not progress_token
            or session is None
            or not hasattr(session, "send_progress_notification")
        ):
            return None
        loop = asyncio.get_running_loop()

        def _push(current: int, total: int, message: str = "") -> None:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    session.send_progress_notification(progress_token, current, total),
                    loop,
                )
                fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            except Exception:
                pass

        return _push

    async def _run_mcp_v1(self) -> None:  # pragma: no cover - 需真实 MCP 客户端连接，属集成测试范畴
        """mcp 1.x 实现：handler 用装饰器注册（``@server.list_tools()`` 等）。"""
        server: Any = Server(_SERVER_NAME)

        @server.list_tools()
        async def _list_tools() -> list[Any]:
            return [types.Tool(**tool) for tool in self.get_tools()]

        @server.call_tool()
        async def _call_tool(name: str, arguments: dict | None) -> Any:
            # handle_tool 是同步阻塞实现（浏览器/LLM/扫描），放到线程池执行，
            # 避免阻塞 asyncio 事件循环导致并发工具调用被串行化、进度冻结。
            # 同时从请求上下文读取客户端订阅的 progressToken，注册进度推送。
            ctx = server.request_context()
            meta = getattr(ctx, "meta", None)
            progress_token = getattr(meta, "progressToken", None)
            sender: Any = None
            if progress_token:
                session = ctx.session
                loop = asyncio.get_running_loop()

                def _push(current: int, total: int, message: str = "") -> None:
                    try:
                        fut = asyncio.run_coroutine_threadsafe(
                            session.send_progress_notification(progress_token, current, total),
                            loop,
                        )
                        fut.add_done_callback(
                            lambda f: f.exception() if not f.cancelled() else None
                        )
                    except Exception:
                        pass

                sender = _push
                with self._progress_lock:
                    self._progress_sender = sender
            try:
                result = await asyncio.to_thread(self.handle_tool, name, arguments or {})
            finally:
                if sender is not None:
                    with self._progress_lock:
                        self._progress_sender = None
            # 解析结果判断是否为错误，设置 isError 标志（MCP 规范要求）
            is_error = False
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and "error" in parsed:
                    is_error = True
            except (json.JSONDecodeError, TypeError):
                pass
            content = [types.TextContent(type="text", text=result)]
            # CallToolResult 仅在较新 mcp SDK 中可用，不可用时退化为纯 content 列表
            if is_error and hasattr(types, "CallToolResult"):
                return types.CallToolResult(content=content, isError=True)
            return content

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

    async def _run_mcp_v2(self) -> None:
        """mcp 2.x 实现：handler 交给 Server 构造函数，请求上下文从回调第一个参数取。

        2.x 去掉了 ``@server.list_tools()`` 这类装饰器与 ``server.request_context()``，改为
        ``Server(name, on_list_tools=…, on_call_tool=…)`` 注册回调；回调签名是 ``(ctx, params)``，
        ctx 自带 ``session`` 与 ``meta``；协议版本由 SDK 自己协商。
        本函数由 SDK 替身驱动做单元测试（``tests/test_mcp_server.py`` 的 ``test_run_mcp_v2_*``），
        不依赖真实 MCP 客户端，故不加 ``pragma: no cover``（1.x 那条仍保持集成测试范畴）。
        """
        # 工具/提示/资源定义只构造一次，回调里直接复用（与 1.x 路径语义一致）
        tools = [types.Tool(**tool) for tool in self.get_tools()]
        prompts = [
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
        resources = [
            types.Resource(
                uri=r["uri"],
                name=r["name"],
                description=r.get("description", ""),
                mimeType=r.get("mimeType", "application/json"),
            )
            for r in self.get_resources()
        ]

        async def _list_tools(ctx: Any, params: Any) -> Any:
            return types.ListToolsResult(tools=tools)

        async def _call_tool(ctx: Any, params: Any) -> Any:
            # 与 1.x 同款语义：同步的 handle_tool 放线程池执行（不阻塞事件循环），
            # 从 ctx 取 progressToken 注册进度推送，并按结果置 isError。
            sender = self._make_progress_sender(ctx)
            if sender is not None:
                with self._progress_lock:
                    self._progress_sender = sender
            try:
                result = await asyncio.to_thread(
                    self.handle_tool, params.name, params.arguments or {}
                )
            finally:
                if sender is not None:
                    with self._progress_lock:
                        self._progress_sender = None
            is_error = False
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and "error" in parsed:
                    is_error = True
            except (json.JSONDecodeError, TypeError):
                pass
            # 2.x 的 on_call_tool 必须返回 CallToolResult（不再接受裸 content 列表）
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=result)],
                isError=is_error,
            )

        async def _list_prompts(ctx: Any, params: Any) -> Any:
            return types.ListPromptsResult(prompts=prompts)

        async def _get_prompt(ctx: Any, params: Any) -> Any:
            rendered = self.render_prompt(params.name, params.arguments or {})
            return types.GetPromptResult(
                description=f"prompt {params.name}",
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(type="text", text=rendered),
                    )
                ],
            )

        async def _list_resources(ctx: Any, params: Any) -> Any:
            return types.ListResourcesResult(resources=resources)

        async def _read_resource(ctx: Any, params: Any) -> Any:
            # 2.x 要完整的结果对象；1.x 里「回调返回裸字符串」的写法在 2.x 不成立
            return types.ReadResourceResult(
                contents=[
                    types.TextResourceContents(
                        uri=params.uri,
                        mimeType="application/json",
                        text=self.read_resource(params.uri),
                    )
                ]
            )

        server: Any = Server(
            _SERVER_NAME,
            version=_SERVER_VERSION,
            on_list_tools=_list_tools,
            on_call_tool=_call_tool,
            on_list_prompts=_list_prompts,
            on_get_prompt=_get_prompt,
            on_list_resources=_list_resources,
            on_read_resource=_read_resource,
        )

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
                # JSON-RPC 2.0 规范：解析失败返回 Parse error（id=null）
                sys.stdout.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "Parse error"},
                        }
                    )
                    + "\n"
                )
                sys.stdout.flush()
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
            # 检测结果是否为错误，设置 isError 标志（MCP 规范要求）
            is_error = False
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and "error" in parsed:
                    is_error = True
            except (json.JSONDecodeError, TypeError):
                pass
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": result}],
                    "isError": is_error,
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

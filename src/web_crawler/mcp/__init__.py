"""MCP server + CLI for JS reverse engineering agent.

与 ``pentest`` / ``ai`` 子包一致采用懒加载：``import web_crawler.mcp``
不会强制加载 camoufox/playwright 等重依赖，首次访问符号时才导入。
"""

from __future__ import annotations

from typing import Any

__all__ = ["ReverseMCPServer", "main"]

# 懒加载映射：符号名 → (模块路径, 属性名)。
_LAZY: dict[str, tuple[str, str]] = {
    "ReverseMCPServer": ("web_crawler.mcp.server", "ReverseMCPServer"),
    "main": ("web_crawler.mcp.server", "main"),
}


def __getattr__(name: str) -> Any:
    """懒加载各公开符号，避免 import 时强制加载浏览器/LLM 重依赖。"""
    if name in _LAZY:
        import importlib

        module_path, attr_name = _LAZY[name]
        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

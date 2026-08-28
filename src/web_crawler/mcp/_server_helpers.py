"""MCP server 的序列化 / 分页 / 截断辅助函数。

从 ``server.py`` 拆出:纯函数,不依赖服务器实例。
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

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


# -- 响应分页 / 截断 ----------------------------------------------------------
#
# 大响应分页（借鉴 ida-pro-mcp 的 pagination 设计）：列表类工具返回当前页 +
# 翻页元数据（total/offset/limit/has_more/next_offset），上游 AI 按需取下一页，
# 避免一次调用塞爆上下文；长文本（LLM 反混淆输出、逆向 result 内嵌的 JS 源码）
# 按 max_length 截断并显式标注 truncated 与 full_length。

# 列表类工具默认分页大小与硬上限（条数）
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 500
# 长文本默认截断阈值与硬上限（字符数）
_DEFAULT_TEXT_LIMIT = 50_000
_MAX_TEXT_LIMIT = 200_000


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    """把任意输入安全收敛为 [lo, hi] 内的整数（非法/越界回退默认或边界）。"""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(iv, hi))


def _paginate(items: list, offset: Any, limit: Any) -> tuple[list, dict]:
    """统一分页：返回 (当前页, 翻页元数据)。

    元数据字段与 MCP 客户端约定：``total``（总条数）、``offset``（本页起点）、
    ``limit``（本页大小）、``has_more``（是否还有下一页）、``next_offset``
    （下一页起点，无更多时为 None）。
    """
    total = len(items)
    start = _clamp_int(offset, 0, 0, max(0, total - 1) if total else 0)
    lim = _clamp_int(limit, _DEFAULT_PAGE_SIZE, 1, _MAX_PAGE_SIZE)
    end = start + lim
    page = items[start:end]
    has_more = end < total
    meta = {
        "total": total,
        "offset": start,
        "limit": lim,
        "has_more": has_more,
        "next_offset": end if has_more else None,
    }
    return page, meta


def _truncate_text(text: str, max_length: int) -> tuple[str, bool, int]:
    """按 max_length 截断字符串，返回 (截断后文本, 是否截断, 原始长度)。"""
    if max_length <= 0 or len(text) <= max_length:
        return text, False, len(text)
    return text[:max_length], True, len(text)


def _truncate_result_strings(
    obj: Any, max_length: int, *, path: str = ""
) -> tuple[Any, list[dict]]:
    """递归截断嵌套结构（dict/list）中的超长字符串。

    返回 (截断后的副本, 截断记录列表)；截断记录形如
    ``{"field": "result.analysis.deobfuscated", "original_length": 123456}``，
    便于上游 AI 知晓哪些字段不完整、需要换工具（如 deobfuscate_js）单独取全文。
    非字符串叶子节点原样保留，输入对象不被修改（返回新副本）。
    """
    truncations: list[dict] = []
    if isinstance(obj, dict):
        out: dict = {}
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else str(key)
            out[key], child_truncs = _truncate_result_strings(value, max_length, path=child_path)
            truncations.extend(child_truncs)
        return out, truncations
    if isinstance(obj, list):
        arr: list = []
        for idx, value in enumerate(obj):
            child_path = f"{path}[{idx}]"
            new_value, child_truncs = _truncate_result_strings(value, max_length, path=child_path)
            arr.append(new_value)
            truncations.extend(child_truncs)
        return arr, truncations
    if isinstance(obj, str):
        sliced, truncated, full_len = _truncate_text(obj, max_length)
        if truncated:
            return sliced, [{"field": path or "<root>", "original_length": full_len}]
        return obj, []
    return obj, []


def _type_ok(ptype: str, value: Any) -> bool:
    """按 JSON Schema 类型检查单个值（bool 不算 int）。"""
    if ptype == "string":
        return isinstance(value, str)
    if ptype == "boolean":
        return isinstance(value, bool)
    if ptype in ("integer", "number"):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if ptype == "array":
        return isinstance(value, list)
    return True


# -- MCP 服务器 --------------------------------------------------------------

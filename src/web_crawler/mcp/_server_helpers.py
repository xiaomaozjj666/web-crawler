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


def _classify_error(error: str, extra: dict[str, Any]) -> tuple[str, str | None, bool | None]:
    """按错误消息与上下文归类，返回 ``(kind, recovery, retryable)``。

    借鉴 REA 的失败分类：请求本身违规（invalid_request）、目标被安全门禁
    拒绝（unsafe_target）、缺依赖/缺密钥（*_missing）、超时（timeout）是不同
    根因，修复动作完全不同；只用一句 ``error`` 会让调用方（尤其是 agent）
    反复重试同一件注定失败的事。
    """
    message = error.lower()
    details = str(extra.get("details", "")).lower()
    text = f"{message} {details}"

    if "authorization required" in text:
        return (
            "authorization",
            "确认已获目标书面授权后再传 authorization_confirmed=true；未获授权时不要调用 pentest_recon。",
            False,
        )
    if any(
        token in text
        for token in (
            "unsafe target",
            "unsafe_target",
            "private address",
            "private host",
            "loopback",
            "link-local",
            "metadata",
            "not allowed",
            "unsupported target scheme",
            "scheme not allowed",
            "only http",
            "私网",
            "内网",
            "非公网",
            "安全门禁",
        )
    ):
        return (
            "unsafe_target",
            (
                "安全门禁拒绝了目标：改用公网 http/https 目标；确需内网/环回目标时，"
                "只在自有可控环境用 allow_private=true 或 WEB_CRAWLER_POWER_MODE=1"
                "（修改环境变量后需重启 MCP 服务）。"
            ),
            False,
        )
    if "dependency missing" in text or "no module named" in text:
        return (
            "dependency_missing",
            (
                "安装缺失依赖（如 pip install -e '.[all,mcp]'）后重启 MCP 服务；"
                "运行中的进程不会自动加载新装的依赖。"
            ),
            False,
        )
    if any(
        token in text for token in ("api key", "llm call failed", "provider", "deepseek", "openai")
    ):
        return (
            "provider_unavailable",
            (
                "设置 DEEPSEEK_API_KEY（或 LLM_API_KEY）后重启 MCP 服务；"
                "可用 web-crawler-reverse doctor 检查 provider 配置。"
            ),
            False,
        )
    if any(
        token in text
        for token in ("401", "403", "forbidden", "unauthorized", "access denied", "拒绝访问")
    ):
        return (
            "target_rejected",
            (
                "目标返回 401/403：检查登录态、Cookie/签名或访问权限；"
                "用同一凭证重复请求通常无效，必要时转人工处理。"
            ),
            False,
        )
    if "timeout" in text or "timed out" in text:
        return (
            "timeout",
            "增大 timeout/max_steps，或缩小采集范围后重试。",
            True,
        )
    if any(token in text for token in ("browser", "playwright", "camoufox", "浏览器", "chromium")):
        return (
            "browser_unavailable",
            (
                "检查浏览器依赖：pip install -e '.[camoufox,mcp]'，并按需执行 "
                "python -m camoufox fetch（或 playwright install chromium）；"
                "无显示环境时使用 headless。"
            ),
            False,
        )
    if (
        extra.get("code") == -32602
        or "invalid params" in text
        or "invalid url" in text
        or "missing host" in text
        or "userinfo" in text
        or "is required" in text
        or "must be" in text
        or "unknown check names" in text
    ):
        return (
            "invalid_request",
            "按 tools/list 返回的最新 inputSchema 检查参数名、类型、枚举与必填项后重试。",
            False,
        )
    if "internal error" in text:
        return (
            "internal",
            "查看 MCP 服务的 stderr 日志定位内部错误；该错误通常不能靠重试自动恢复。",
            False,
        )
    return "unavailable", None, None


def _error(
    error: str,
    *,
    kind: str | None = None,
    recovery: str | None = None,
    retryable: bool | None = None,
    **extra: Any,
) -> str:
    """构造标准错误响应 JSON，并附上根因分类与可执行修复建议。"""
    inferred_kind, inferred_recovery, inferred_retryable = _classify_error(error, extra)
    payload: dict[str, Any] = {
        "error": error,
        "kind": kind or inferred_kind,
    }
    final_recovery = recovery if recovery is not None else inferred_recovery
    if final_recovery:
        payload["recovery"] = final_recovery
    final_retryable = retryable if retryable is not None else inferred_retryable
    if final_retryable is not None:
        payload["retryable"] = final_retryable
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
# inspect_evidence 单页默认/最大字符数
_DEFAULT_EVIDENCE_PAGE = 20_000
_MAX_EVIDENCE_PAGE = 200_000


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

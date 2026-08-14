"""AI 模块共享的 JSON 容错解析工具。

模型回复常夹带 Markdown 代码块、前后说明文字或多余花括号。此前
analyzer / extractor / judge / image_captcha 各自复制了一份贪婪的
``\\{.*\\}`` 正则，贪婪匹配会把最后一个 ``}`` 之后的花括号噪音一并吞入，
导致明明存在合法 JSON 却解析失败。这里统一改为括号配平匹配：

1. 先整体 ``json.loads``（兼容纯 JSON 输出）；
2. 失败则剥离 ```json 代码块后再整体解析；
3. 仍失败则从第一个 ``{`` 起做括号配平，逐个尝试解析，取首个合法 JSON 对象。
"""

from __future__ import annotations

import json
import re
from typing import Any

# ```lang ... ``` 代码块（lang 可选）
_CODE_FENCE_RE = re.compile(r"^```(?:\w+)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _balanced_json_object(text: str, start: int) -> str | None:
    """从 ``text[start]``（须为 ``{``）返回配平后的 JSON 对象文本。

    跳过字符串字面量内的括号与转义字符；无法配平返回 ``None``。
    """
    depth = 0
    in_str = False
    escaped = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


def extract_json(text: str) -> dict[str, Any]:
    """容错解析模型回复中的 JSON 对象；失败返回空 dict。

    非 dict 顶层值（如数组/标量）视为解析失败，避免调用方 ``.get`` 崩掉。
    """
    stripped = text.strip()
    fence = _CODE_FENCE_RE.match(stripped)
    if fence:
        stripped = fence.group(1).strip()
    try:
        data = json.loads(stripped)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass
    pos = stripped.find("{")
    while pos != -1:
        obj = _balanced_json_object(stripped, pos)
        if obj is not None:
            try:
                data = json.loads(obj)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
        pos = stripped.find("{", pos + 1)
    return {}

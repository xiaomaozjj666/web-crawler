"""HookLibrary / generate_combined_script / collect_hook_data 单元测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, is_dataclass
from unittest.mock import MagicMock

import pytest

from web_crawler.ai.hooks import (
    HookLibrary,
    HookScript,
    collect_hook_data,
    generate_combined_script,
)

_EXPECTED_HOOK_NAMES = [
    "fetch_hook",
    "xhr_hook",
    "cookie_hook",
    "webcrypto_hook",
    "webpack_hook",
    "console_hook",
]


def test_hook_library_has_all_hooks() -> None:
    # 6 个预置 hook 全部就位，且能按名称检索
    names = HookLibrary.names()
    assert set(names) == set(_EXPECTED_HOOK_NAMES)
    assert len(names) == 6
    for name in _EXPECTED_HOOK_NAMES:
        assert hasattr(HookLibrary, name)
        hook = HookLibrary.get(name)
        assert hook is not None
        assert hook.name == name
        assert hook.script
        assert hook.description
    # 未知名称返回 None
    assert HookLibrary.get("does_not_exist") is None


def test_hook_script_is_dataclass() -> None:
    # frozen dataclass：字段不可变
    assert is_dataclass(HookScript)
    hs = HookScript(name="n", script="s", description="d")
    assert hs.name == "n"
    assert hs.script == "s"
    assert hs.description == "d"
    with pytest.raises(FrozenInstanceError):
        hs.name = "other"  # type: ignore[misc]


def test_generate_combined_script_all() -> None:
    # 不传参数时使用全部预置 hook
    script = generate_combined_script()
    for hook in HookLibrary.all():
        assert hook.script in script


def test_generate_combined_script_subset() -> None:
    # 仅拼接选定的 hook，其余 hook 的重入标记不应出现
    script = generate_combined_script(["fetch_hook", "xhr_hook"])
    assert "__hook_fetch__" in script
    assert "__hook_xhr__" in script
    assert "__hook_cookie__" not in script
    assert "__hook_webcrypto__" not in script
    assert "__hook_webpack__" not in script
    assert "__hook_console__" not in script


def test_generate_combined_script_unknown_ignored() -> None:
    # 未知名称被静默忽略，不抛异常；已知名称仍生效
    script = generate_combined_script(["fetch_hook", "totally_unknown"])
    assert "__hook_fetch__" in script
    assert "__hook_xhr__" not in script


def test_combined_script_contains_init() -> None:
    # 引导脚本在最前面初始化 window.__hook_data__ 容器
    script = generate_combined_script()
    assert "window.__hook_data__" in script


def test_collect_hook_data_with_mock() -> None:
    # mock page.evaluate 返回拦截记录，collect_hook_data 包装为统一结构
    page = MagicMock()
    captured = [
        {"type": "fetch", "url": "https://example.com/api", "method": "GET"},
        {"type": "xhr", "url": "https://example.com/x", "method": "POST"},
    ]
    page.evaluate.return_value = captured

    result = collect_hook_data(page)

    assert result["count"] == 2
    assert result["records"] == captured
    page.evaluate.assert_called_once()


def test_collect_hook_data_empty_when_evaluate_returns_none() -> None:
    # 浏览器侧 __hook_data__ 尚未初始化时 evaluate 可能返回 None
    page = MagicMock()
    page.evaluate.return_value = None

    result = collect_hook_data(page)

    assert result["count"] == 0
    assert result["records"] == []

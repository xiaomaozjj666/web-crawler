"""recorder.py 单元测试：覆盖 ActionRecord、RunRecorder、ScriptCompiler、
ReplayRunner 各分支，不依赖 camoufox / playwright 真实运行。
"""

from __future__ import annotations

import json

import pytest

from web_crawler.ai.recorder import (
    ActionRecord,
    ReplayRunner,
    RunRecorder,
    ScriptCompiler,
    _py_string_literal,
)

# ---------------------------------------------------------------------------
# ActionRecord
# ---------------------------------------------------------------------------


def test_action_record_defaults() -> None:
    """ActionRecord 默认值：params/result_value/success/result_summary。"""
    rec = ActionRecord(step=1, action_type="navigate")
    assert rec.step == 1
    assert rec.action_type == "navigate"
    assert rec.params == {}
    assert rec.result_value is None
    assert rec.success is True
    assert rec.result_summary == ""


def test_action_record_to_dict_roundtrip() -> None:
    """to_dict 应包含全部字段。"""
    rec = ActionRecord(
        step=3,
        action_type="extract",
        params={"param_name": "token"},
        result_value="abc",
        success=True,
        result_summary="found in headers",
    )
    d = rec.to_dict()
    assert d == {
        "step": 3,
        "action_type": "extract",
        "params": {"param_name": "token"},
        "result_value": "abc",
        "success": True,
        "result_summary": "found in headers",
    }


# ---------------------------------------------------------------------------
# RunRecorder 基础 API
# ---------------------------------------------------------------------------


def test_run_recorder_initial_state_empty() -> None:
    """新构造的 RunRecorder 应无记录。"""
    rec = RunRecorder()
    assert rec.records == []


def test_run_recorder_record_appends() -> None:
    """record() 添加一条记录。"""
    rec = RunRecorder()
    rec.record(step=1, action_type="navigate", params={"url": "https://x"})
    assert len(rec.records) == 1
    assert rec.records[0].action_type == "navigate"
    # records 属性返回列表浅拷贝，append 不影响内部状态
    records = rec.records
    records.append(ActionRecord(step=99, action_type="fake"))
    assert len(rec.records) == 1


def test_run_recorder_record_with_none_params() -> None:
    """record() params=None 应转为空 dict。"""
    rec = RunRecorder()
    rec.record(step=1, action_type="wait", params=None)
    assert rec.records[0].params == {}


def test_run_recorder_set_target_records_url() -> None:
    """set_target 记录目标 URL。"""
    rec = RunRecorder()
    rec.set_target("https://target.example/")
    # 通过编译产物验证
    script = rec.compile_script()
    assert "https://target.example/" in script


def test_run_recorder_reset_clears_all() -> None:
    """reset 清空 records / hooks / target。"""
    rec = RunRecorder()
    rec.set_target("https://x/")
    rec.record(step=1, action_type="navigate", params={"url": "https://x/"})
    rec.record(
        step=2,
        action_type="inject_hook",
        params={"hooks": ["fetch_hook"]},
    )
    rec.reset()
    assert rec.records == []


# ---------------------------------------------------------------------------
# ScriptCompiler — 各 action_type 的编译
# ---------------------------------------------------------------------------


def test_script_compiler_compile_empty_records() -> None:
    """空 records 列表应编译出仅含 return 的函数体。"""
    compiler = ScriptCompiler()
    src = compiler.compile([])
    assert "def replay(" in src
    assert "return {" in src
    # 没有 action 时 body 为 pass
    assert "pass" in src


def test_script_compiler_done_action_skipped_in_body() -> None:
    """done action 不编译为代码行，仅参与返回值组装。"""
    compiler = ScriptCompiler()
    src = compiler.compile([ActionRecord(step=5, action_type="done")])
    # done 在循环里被 continue 跳过；body 仍含 return dict
    assert "return {" in src
    assert "target_params_found" in src


def test_script_compiler_unsupported_action_logs_warning() -> None:
    """未知 action_type 写一条 NOTE 注释并触发 logger.warning。"""
    compiler = ScriptCompiler()
    src = compiler.compile([ActionRecord(step=2, action_type="unsupported_xxx")])
    assert "unsupported action 'unsupported_xxx'" in src
    assert "NOTE: unsupported action" in src


def test_script_compiler_navigate_action() -> None:
    """navigate 编译为 page.goto + history.append。"""
    compiler = ScriptCompiler()
    src = compiler.compile(
        [
            ActionRecord(
                step=1,
                action_type="navigate",
                params={"url": "https://example.com/page"},
            )
        ]
    )
    assert "page.goto(" in src
    assert "https://example.com/page" in src
    assert "'action': 'navigate'" in src


def test_script_compiler_inject_hook_dedup_skips() -> None:
    """已注入过的 hook 再次 inject 时跳过（dedup）。"""
    compiler = ScriptCompiler()
    records = [
        ActionRecord(step=1, action_type="inject_hook", params={"hooks": ["a", "b"]}),
        ActionRecord(step=2, action_type="inject_hook", params={"hooks": ["a", "b"]}),
    ]
    src = compiler.compile(records)
    # 第二次 inject_hook 出现 dedup 标记
    assert "dedup, skipped" in src
    # 仍编译第一次注入
    assert "generate_combined_script" in src


def test_script_compiler_inject_hook_all_new() -> None:
    """全部新 hook 编译为 page.evaluate(generate_combined_script)。"""
    compiler = ScriptCompiler()
    src = compiler.compile(
        [
            ActionRecord(
                step=1,
                action_type="inject_hook",
                params={"hooks": ["fetch_hook", "xhr_hook"]},
            )
        ]
    )
    assert "generate_combined_script" in src
    assert "_hook_names" in src


def test_script_compiler_inject_hook_empty_hooks_returns_skipped() -> None:
    """hooks=[] 时 dedup 直接跳过。"""
    compiler = ScriptCompiler()
    src = compiler.compile([ActionRecord(step=1, action_type="inject_hook", params={"hooks": []})])
    assert "dedup, skipped" in src


def test_script_compiler_wait_action_clamps_seconds() -> None:
    """wait 编译为 time.sleep，且 seconds 被 clamp 到 [0.1, 30.0]。"""
    compiler = ScriptCompiler()
    # seconds 超过 30
    src = compiler.compile([ActionRecord(step=1, action_type="wait", params={"seconds": 100.0})])
    assert "time.sleep(30.0" in src
    # seconds 过小
    src2 = compiler.compile([ActionRecord(step=1, action_type="wait", params={"seconds": 0.0})])
    assert "time.sleep(0.1" in src2


def test_script_compiler_wait_default_seconds() -> None:
    """wait 缺省 seconds=1.0。"""
    compiler = ScriptCompiler()
    src = compiler.compile([ActionRecord(step=1, action_type="wait", params={})])
    assert "time.sleep(1.0" in src


def test_script_compiler_extract_with_recorded_value_writes_assert() -> None:
    """extract 有 result_value 时编译为断言并加入返回值。"""
    compiler = ScriptCompiler()
    src = compiler.compile(
        [
            ActionRecord(
                step=1,
                action_type="extract",
                params={"param_name": "token"},
                result_value="abc123",
            )
        ]
    )
    assert "assert _found is not None" in src
    assert "'token'" in src
    # 返回值含 target_params_found 字典
    assert "target_params_found" in src


def test_script_compiler_extract_no_recorded_value_best_effort() -> None:
    """extract 无 result_value 时仅做 best-effort 提取。"""
    compiler = ScriptCompiler()
    src = compiler.compile(
        [
            ActionRecord(
                step=1,
                action_type="extract",
                params={"param_name": "session"},
                result_value=None,
            )
        ]
    )
    assert "no recorded value" in src
    # 不应含 assert
    assert "assert _found" not in src


def test_script_compiler_analyze_js_action() -> None:
    """analyze_js 编译为 history 占位（LLM 调用不复现）。"""
    compiler = ScriptCompiler()
    src = compiler.compile([ActionRecord(step=2, action_type="analyze_js")])
    assert "LLM analysis not replayed" in src


def test_script_compiler_solve_captcha_action() -> None:
    """solve_captcha 编译为 skipped 占位。"""
    compiler = ScriptCompiler()
    src = compiler.compile([ActionRecord(step=3, action_type="solve_captcha")])
    assert "skipped in replay" in src


def test_script_compiler_done_handler_emits_comment() -> None:
    """_compile_done 静态方法返回注释行。"""
    lines = ScriptCompiler._compile_done(ActionRecord(step=9, action_type="done"))
    assert any("step 9: done" in line for line in lines)


def test_script_compiler_click_action() -> None:
    """click 编译为 page.click + history.append。"""
    compiler = ScriptCompiler()
    src = compiler.compile(
        [
            ActionRecord(
                step=1,
                action_type="click",
                params={"selector": "#btn", "button": "right"},
            )
        ]
    )
    assert "page.click(" in src
    assert "'right'" in src
    # selector 走 _py_string_literal，使用双引号字面量
    assert '"#btn"' in src


def test_script_compiler_type_action_with_clear() -> None:
    """type 默认 clear=True，会先 page.fill 清空。"""
    compiler = ScriptCompiler()
    src = compiler.compile(
        [
            ActionRecord(
                step=1,
                action_type="type",
                params={"selector": "#input", "text": "hello", "clear": True},
            )
        ]
    )
    assert "page.fill(" in src
    assert '""' in src  # 清空
    assert "page.type(" in src


def test_script_compiler_type_action_without_clear() -> None:
    """type clear=False 时跳过 fill。"""
    compiler = ScriptCompiler()
    src = compiler.compile(
        [
            ActionRecord(
                step=1,
                action_type="type",
                params={"selector": "#input", "text": "hello", "clear": False},
            )
        ]
    )
    assert "page.fill(" not in src
    assert "page.type(" in src


def test_script_compiler_scroll_with_selector() -> None:
    """scroll 带 selector 时走 querySelector 路径。"""
    compiler = ScriptCompiler()
    src = compiler.compile(
        [
            ActionRecord(
                step=1,
                action_type="scroll",
                params={"selector": "#main", "x": 0, "y": 500},
            )
        ]
    )
    assert "document.querySelector(" in src
    assert "500" in src


def test_script_compiler_scroll_without_selector() -> None:
    """scroll 不带 selector 时走 window.scrollBy。"""
    compiler = ScriptCompiler()
    src = compiler.compile([ActionRecord(step=1, action_type="scroll", params={"x": 0, "y": 800})])
    assert "window.scrollBy(" in src


def test_script_compiler_press_with_selector_focuses() -> None:
    """press 带 selector 时先 page.focus。"""
    compiler = ScriptCompiler()
    src = compiler.compile(
        [
            ActionRecord(
                step=1,
                action_type="press",
                params={"key": "Enter", "selector": "#input"},
            )
        ]
    )
    assert "page.focus(" in src
    assert "page.press('Enter')" in src


def test_script_compiler_press_without_selector() -> None:
    """press 不带 selector 时仅 page.press。"""
    compiler = ScriptCompiler()
    src = compiler.compile([ActionRecord(step=1, action_type="press", params={"key": "Escape"})])
    assert "page.focus(" not in src
    assert "page.press('Escape')" in src


def test_script_compiler_hover_action() -> None:
    """hover 编译为 page.hover。"""
    compiler = ScriptCompiler()
    src = compiler.compile([ActionRecord(step=1, action_type="hover", params={"selector": "#el"})])
    assert "page.hover(" in src
    assert '"#el"' in src


def test_script_compiler_select_option_action() -> None:
    """select_option 编译为 page.select_option。"""
    compiler = ScriptCompiler()
    src = compiler.compile(
        [
            ActionRecord(
                step=1,
                action_type="select_option",
                params={"selector": "#sel", "value": "opt1"},
            )
        ]
    )
    assert "page.select_option(" in src
    assert '"opt1"' in src


def test_script_compiler_failed_records_skipped() -> None:
    """success=False 的记录应被跳过。"""
    compiler = ScriptCompiler()
    src = compiler.compile(
        [
            ActionRecord(
                step=1,
                action_type="navigate",
                params={"url": "https://x/"},
                success=False,
            ),
            ActionRecord(
                step=2,
                action_type="navigate",
                params={"url": "https://y/"},
            ),
        ]
    )
    # 仅含 y 不含 x
    assert "https://y/" in src
    assert "https://x/" not in src


def test_script_compiler_target_url_uses_url_param_when_empty() -> None:
    """target_url 为空时，函数签名 url 默认为 'url'。"""
    compiler = ScriptCompiler()
    src = compiler.compile([], target_url="")
    assert "url: str = url" in src


def test_script_compiler_target_url_uses_literal_when_set() -> None:
    """target_url 提供时，函数签名 url 默认为字面量。"""
    compiler = ScriptCompiler()
    src = compiler.compile([], target_url="https://t.example/")
    assert 'url: str = "https://t.example/"' in src


# ---------------------------------------------------------------------------
# _py_string_literal
# ---------------------------------------------------------------------------


def test_py_string_literal_plain() -> None:
    """普通字符串直接转双引号字面量。"""
    assert _py_string_literal("hello") == '"hello"'


def test_py_string_literal_escapes_quotes_and_backslash() -> None:
    """转义双引号与反斜杠。"""
    assert _py_string_literal('he said "hi"') == '"he said \\"hi\\""'
    assert _py_string_literal("back\\slash") == '"back\\\\slash"'


# ---------------------------------------------------------------------------
# RunRecorder.compile_script 端到端
# ---------------------------------------------------------------------------


def test_run_recorder_compile_script_end_to_end() -> None:
    """完整流程：record → compile_script → 校验产物。"""
    rec = RunRecorder()
    rec.set_target("https://target.example/")
    rec.record(step=1, action_type="navigate", params={"url": "https://target.example/"})
    rec.record(
        step=2,
        action_type="inject_hook",
        params={"hooks": ["fetch_hook"]},
    )
    rec.record(step=3, action_type="wait", params={"seconds": 0.5})
    rec.record(
        step=4,
        action_type="extract",
        params={"param_name": "sign"},
        result_value="abcdef",
    )
    rec.record(step=5, action_type="done")
    script = rec.compile_script(function_name="replay_test")
    assert "def replay_test(" in script
    assert "https://target.example/" in script
    assert "page.goto(" in script
    assert "generate_combined_script" in script
    assert "time.sleep(" in script
    assert "assert _found is not None" in script
    # 返回值含 extract 结果
    assert "'sign'" in script


def test_run_recorder_compile_script_failed_records_excluded() -> None:
    """compile_script 应过滤 success=False 的记录。"""
    rec = RunRecorder()
    rec.record(
        step=1,
        action_type="navigate",
        params={"url": "https://good/"},
        success=True,
    )
    rec.record(
        step=2,
        action_type="navigate",
        params={"url": "https://bad/"},
        success=False,
    )
    script = rec.compile_script()
    assert "https://good/" in script
    assert "https://bad/" not in script


# ---------------------------------------------------------------------------
# ReplayRunner
# ---------------------------------------------------------------------------


def test_replay_runner_init_state() -> None:
    """ReplayRunner 初始无脚本无 namespace。"""
    runner = ReplayRunner()
    assert runner._script_source == ""  # type: ignore[attr-defined]
    assert runner._namespace == {}  # type: ignore[attr-defined]


def test_replay_runner_load_script_defines_replay_fn() -> None:
    """load_script 执行源码后 namespace 内有 replay 函数。"""
    runner = ReplayRunner()
    # 用一个最简脚本，避免触发真实 camoufox 导入
    src = """
def replay(url: str = "", *, fetcher=None) -> dict:
    return {"success": True, "items": [url]}
"""
    runner.load_script(src)
    assert callable(runner._namespace.get("replay"))  # type: ignore[attr-defined]


def test_replay_runner_run_invokes_loaded_function() -> None:
    """run() 调用已加载的 replay 函数。"""
    runner = ReplayRunner()
    src = """
def replay(url: str = "", *, fetcher=None) -> dict:
    return {"success": True, "url": url, "fetcher_provided": fetcher is not None}
"""
    runner.load_script(src)
    result = runner.run(url="https://x.example/")
    assert result["success"] is True
    assert result["url"] == "https://x.example/"
    assert result["fetcher_provided"] is False


def test_replay_runner_run_with_fetcher() -> None:
    """run() 透传 fetcher。"""
    runner = ReplayRunner()
    src = """
def replay(url: str = "", *, fetcher=None) -> dict:
    return {"fetcher": fetcher}
"""
    runner.load_script(src)
    sentinel = object()
    result = runner.run(url="https://x/", fetcher=sentinel)
    assert result["fetcher"] is sentinel


def test_replay_runner_run_without_load_raises() -> None:
    """未加载脚本直接 run() 抛 RuntimeError。"""
    runner = ReplayRunner()
    with pytest.raises(RuntimeError, match="load_script"):
        runner.run(url="https://x/")


def test_replay_runner_load_real_compiled_script() -> None:
    """加载 ScriptCompiler 产物并验证可执行（不真正调浏览器）。"""
    rec = RunRecorder()
    rec.set_target("https://example.com/")
    rec.record(step=1, action_type="navigate", params={"url": "https://example.com/"})
    src = rec.compile_script()
    runner = ReplayRunner()
    # 仅验证 exec 不抛异常（不真正 run，因为依赖 camoufox）
    runner.load_script(src)
    assert callable(runner._namespace.get("replay"))  # type: ignore[attr-defined]


def test_replay_runner_run_real_script_requires_camoufox() -> None:
    """完整脚本 run 时会触发 camoufox 导入；用 mock fetcher 避开真实浏览器。

    这里仅验证 mock fetcher 路径下能跑到 finally 块且不抛未捕获异常。
    """
    rec = RunRecorder()
    rec.set_target("https://example.com/")
    # 一个失败 + done 的最小组合，body 应含 pass（无 success 记录）
    rec.record(
        step=1,
        action_type="navigate",
        params={"url": "https://example.com/"},
        success=False,
    )
    src = rec.compile_script()
    runner = ReplayRunner()
    runner.load_script(src)

    # mock fetcher，提供 _ensure_browser / new_context 链式调用
    fake_page = type(
        "P",
        (),
        {
            "goto": lambda *a, **kw: None,
            "wait_for_load_state": lambda *a, **kw: None,
            "evaluate": lambda *a, **kw: [],
        },
    )()
    fake_context = type(
        "C",
        (),
        {
            "new_page": lambda self: fake_page,
            "close": lambda self: None,
        },
    )()
    fake_browser = type("B", (), {"new_context": lambda self, **kw: fake_context})()
    fake_fetcher = type(
        "F",
        (),
        {
            "_ensure_browser": lambda self: fake_browser,
            "extra_headers": None,
            "verify": True,
            "close": lambda self: None,
        },
    )()
    result = runner.run(url="https://example.com/", fetcher=fake_fetcher)
    assert isinstance(result, dict)
    assert result["success"] is True
    assert result["target_params_found"] == {}
    assert "history" in result


# ---------------------------------------------------------------------------
# 综合场景
# ---------------------------------------------------------------------------


def test_compile_script_with_mixed_actions_includes_all_handlers() -> None:
    """混合多种 action_type 的脚本应包含各编译器的产物。"""
    rec = RunRecorder()
    rec.set_target("https://t/")
    rec.record(step=1, action_type="navigate", params={"url": "https://t/"})
    rec.record(step=2, action_type="inject_hook", params={"hooks": ["a"]})
    rec.record(step=3, action_type="wait", params={"seconds": 1})
    rec.record(step=4, action_type="click", params={"selector": "#b"})
    rec.record(step=5, action_type="type", params={"selector": "#i", "text": "x"})
    rec.record(step=6, action_type="scroll", params={"x": 0, "y": 100})
    rec.record(step=7, action_type="press", params={"key": "Enter"})
    rec.record(step=8, action_type="hover", params={"selector": "#h"})
    rec.record(step=9, action_type="select_option", params={"selector": "#s", "value": "v"})
    rec.record(step=10, action_type="analyze_js")
    rec.record(step=11, action_type="solve_captcha")
    rec.record(
        step=12,
        action_type="extract",
        params={"param_name": "tok"},
        result_value="v",
    )
    rec.record(step=13, action_type="done")
    src = rec.compile_script()
    for keyword in [
        "page.goto",
        "generate_combined_script",
        "time.sleep",
        "page.click",
        "page.type",
        "window.scrollBy",
        "page.press",
        "page.hover",
        "page.select_option",
        "LLM analysis not replayed",
        "skipped in replay",
        "assert _found is not None",
    ]:
        assert keyword in src, f"missing keyword in compiled script: {keyword}"


def test_action_record_to_dict_with_complex_params() -> None:
    """to_dict 保留复杂 params 结构。"""
    rec = ActionRecord(
        step=1,
        action_type="extract",
        params={"nested": {"a": 1}, "list": [1, 2, 3]},
        result_value=None,
    )
    d = rec.to_dict()
    # JSON 可序列化
    s = json.dumps(d)
    parsed = json.loads(s)
    assert parsed["params"]["nested"] == {"a": 1}
    assert parsed["params"]["list"] == [1, 2, 3]


def test_run_recorder_records_property_returns_copy() -> None:
    """records 属性返回副本，外部 append 不影响内部状态。"""
    rec = RunRecorder()
    rec.record(step=1, action_type="navigate")
    records = rec.records
    records.append(ActionRecord(step=99, action_type="fake"))
    assert len(rec.records) == 1


# ---------------------------------------------------------------------------
# 扩展：编译产物必须是合法 Python（SyntaxError 回归）
# ---------------------------------------------------------------------------


def _compile_ok(src: str) -> None:
    """断言编译产物是合法 Python 源码。"""
    compile(src, "<replay_script>", "exec")


def test_compile_extract_with_result_is_valid_python() -> None:
    """带 result_value 的 extract 产物可被 compile（原 {param_name!r} 破句法）。"""
    rec = RunRecorder()
    rec.set_target("https://target.example/")
    rec.record(step=1, action_type="navigate", params={"url": "https://target.example/"})
    rec.record(
        step=2,
        action_type="extract",
        params={"param_name": "sign"},
        result_value="abcdef",
    )
    script = rec.compile_script()
    _compile_ok(script)
    # 断言消息应使用安全字面量（不含裸引号拼接）
    assert 'assert _found is not None, "param sign not found in hook data"' in script


def test_compile_type_with_newline_text_is_valid_python() -> None:
    """type 文本含换行时产物仍可编译。"""
    rec = RunRecorder()
    rec.record(
        step=1,
        action_type="type",
        params={"selector": "#q", "text": "line1\nline2"},
    )
    script = rec.compile_script()
    _compile_ok(script)
    assert "\\n" in script


def test_compile_extract_value_with_newline_is_valid_python() -> None:
    """extract 结果值含换行时，返回值字面量仍可编译。"""
    rec = RunRecorder()
    rec.record(
        step=1,
        action_type="extract",
        params={"param_name": "body"},
        result_value="a\nb",
    )
    script = rec.compile_script()
    _compile_ok(script)


def test_compile_scroll_selector_with_quote_is_valid_python() -> None:
    """scroll 的 selector 含单引号/双引号时产物仍可编译。"""
    rec = RunRecorder()
    rec.record(
        step=1,
        action_type="scroll",
        params={"selector": "div[data-x='it\\'s']", "y": 100},
    )
    script = rec.compile_script()
    _compile_ok(script)


def test_compile_url_with_newline_is_valid_python() -> None:
    """navigate URL 含换行等控制符时产物仍可编译。"""
    rec = RunRecorder()
    rec.record(
        step=1,
        action_type="navigate",
        params={"url": "https://x.example/\npath"},
    )
    script = rec.compile_script()
    _compile_ok(script)


def test_compile_empty_records_self_check_passes() -> None:
    """空记录产物也应能通过 compile 自检。"""
    script = RunRecorder().compile_script()
    _compile_ok(script)


def test_run_recorder_update_last_updates_current_record() -> None:
    """update_last 只更新最近一条记录，不误标上一步。"""
    rec = RunRecorder()
    rec.record(step=1, action_type="wait")
    rec.record(step=2, action_type="click", params={"selector": "#x"})
    rec.update_last(success=False, result_value=None)
    records = rec.records
    assert records[0].success is True
    assert records[1].success is False
    # 空记录时 update_last 不抛错
    RunRecorder().update_last(success=False)


def test_compile_syntax_error_raises_runtime_error(monkeypatch) -> None:
    """编译产物含语法错误时抛 RuntimeError（不静默返回坏产物）。"""
    from web_crawler.ai.recorder import ScriptCompiler

    def _bad_compiler(rec: ActionRecord, **_: object) -> list[str]:
        return ["    this is not valid python !!!"]

    monkeypatch.setattr(ScriptCompiler, "_compile_click", staticmethod(_bad_compiler))
    compiler = ScriptCompiler()
    with pytest.raises(RuntimeError, match="compiled replay script invalid"):
        compiler.compile([ActionRecord(step=1, action_type="click", params={"selector": "#x"})])

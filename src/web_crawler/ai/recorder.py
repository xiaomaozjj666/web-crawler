"""成功路径编译为确定性脚本。

借鉴 browser-use 的 ``record_video`` + PentAGI 的 "deterministic replay" 思路：
当一次 Agent 运行成功后，把执行过的动作序列（navigate / inject_hook / wait /
extract 等）编译成一段独立可执行的 Python 脚本。下次面对同类站点时直接跑
脚本，不再消耗 LLM 调用。

能力清单
--------
- :class:`ActionRecord` — 单条动作记录（step / action_type / params / result_summary）；
- :class:`RunRecorder` — 运行期记录器，挂在 ReverseAgent 主循环上；
- :class:`ScriptCompiler` — 把记录序列编译成可执行 Python 源码；
- :class:`ReplayRunner` — 重放已编译脚本，无需 LLM。

设计要点
--------
- 仅记录"成功路径"：失败步（act_error / observe_error）跳过，保证脚本干净；
- ``extract`` 动作的结果会以"断言"形式写进脚本：``assert param_name == "..."``，
  让重放时自动验证产物仍存在；
- ``navigate`` / ``inject_hook`` / ``wait`` 编译为对应的 Camoufox 调用；
- 编译产物是一段 ``def replay(url: str, *, fetcher=None) -> dict:`` 函数，
  独立可运行，不依赖 LLM provider。
"""

from __future__ import annotations

import json
import logging
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ActionRecord:
    """单条动作记录。"""

    step: int
    action_type: str
    params: dict[str, Any] = field(default_factory=dict)
    # extract 动作的结果值（用于编译为断言）
    result_value: str | None = None
    # 动作是否成功（act_error 标记 False）
    success: bool = True
    # 简短的 result 摘要（便于调试）
    result_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "action_type": self.action_type,
            "params": self.params,
            "result_value": self.result_value,
            "success": self.success,
            "result_summary": self.result_summary,
        }


class RunRecorder:
    """运行期动作记录器。

    挂在 :class:`~web_crawler.ai.reverse_agent.ReverseAgent` 主循环上，每步
    通过 :meth:`record` 喂入一条 ``ActionRecord``。运行结束后调用
    :meth:`compile_script` 把成功路径编译成 Python 源码。
    """

    def __init__(self) -> None:
        self._records: list[ActionRecord] = []
        # 已注入的 hook 集合，避免脚本里重复注入
        self._injected_hooks: set[str] = set()
        self._target_url: str = ""

    @property
    def records(self) -> list[ActionRecord]:
        return list(self._records)

    def set_target(self, url: str) -> None:
        """记录初始目标 URL，编译为脚本入口。"""
        self._target_url = url

    def record(
        self,
        *,
        step: int,
        action_type: str,
        params: dict[str, Any] | None = None,
        result_value: str | None = None,
        success: bool = True,
        result_summary: str = "",
    ) -> None:
        """记录一条动作。"""
        rec = ActionRecord(
            step=step,
            action_type=action_type,
            params=dict(params or {}),
            result_value=result_value,
            success=success,
            result_summary=result_summary,
        )
        self._records.append(rec)

    def reset(self) -> None:
        """清空记录，新任务开始时调用。"""
        self._records.clear()
        self._injected_hooks.clear()
        self._target_url = ""

    # ------------------------------------------------------------------
    # 编译
    # ------------------------------------------------------------------

    def compile_script(self, *, function_name: str = "replay") -> str:
        """把记录的成功路径编译成 Python 源码字符串。"""
        compiler = ScriptCompiler()
        return compiler.compile(
            self._records,
            target_url=self._target_url,
            function_name=function_name,
        )


class ScriptCompiler:
    """把 ActionRecord 序列编译为可执行的 Python 源码。

    生成一段独立函数，签名 ``def replay(url: str, *, fetcher=None) -> dict:``。
    调用方负责传入 CamoufoxFetcher 实例；不传时函数内部按默认配置新建。
    """

    # 动作类型 → 编译器方法名的映射
    _HANDLER_MAP = {
        "navigate": "_compile_navigate",
        "inject_hook": "_compile_inject_hook",
        "wait": "_compile_wait",
        "extract": "_compile_extract",
        "analyze_js": "_compile_analyze_js",
        "solve_captcha": "_compile_solve_captcha",
        "done": "_compile_done",
        # 浏览器交互动作
        "click": "_compile_click",
        "type": "_compile_type",
        "scroll": "_compile_scroll",
        "press": "_compile_press",
        "hover": "_compile_hover",
        "select_option": "_compile_select_option",
    }

    def compile(
        self,
        records: list[ActionRecord],
        *,
        target_url: str = "",
        function_name: str = "replay",
    ) -> str:
        """编译记录序列为 Python 源码字符串。"""
        body_lines: list[str] = []
        # 只保留成功路径
        success_records = [r for r in records if r.success]
        seen_hooks: set[str] = set()

        # 收集所有 extract 结果，最后写入返回值
        extract_results: list[tuple[str, str]] = []

        for rec in success_records:
            if rec.action_type == "done":
                # done 不需要编译为代码，直接跳过；返回值在末尾组装
                continue
            handler_name = self._HANDLER_MAP.get(rec.action_type)
            if handler_name is None:
                # 未知动作类型：记录警告并写一条注释，便于人工补全
                logger.warning(
                    "unsupported action %r at step %d — replay script will skip it",
                    rec.action_type,
                    rec.step,
                )
                body_lines.append(
                    f"    # NOTE: unsupported action {rec.action_type!r} at step {rec.step}"
                )
                continue
            handler = getattr(self, handler_name)
            lines = handler(rec, seen_hooks=seen_hooks, extract_results=extract_results)
            body_lines.extend(lines)

        # 组装返回值
        return_dict_lines = ["    return {"]
        return_dict_lines.append('        "success": True,')
        if extract_results:
            return_dict_lines.append('        "target_params_found": {')
            for name, value in extract_results:
                escaped = _py_string_literal(value)
                return_dict_lines.append(f"            {name!r}: {escaped},")
            return_dict_lines.append("        },")
        else:
            return_dict_lines.append('        "target_params_found": {},')
        return_dict_lines.append('        "steps": len(history),')
        return_dict_lines.append('        "history": history,')
        return_dict_lines.append("    }")
        body_lines.extend(return_dict_lines)

        body = "\n".join(body_lines) if body_lines else "    pass"

        target_url_literal = _py_string_literal(target_url) if target_url else "url"
        return textwrap.dedent(f'''\
            """Auto-compiled deterministic replay script.

            生成自 RunRecorder.compile_script()，重放一次成功 Agent 运行的动作序列。
            调用方式：``result = {function_name}("{target_url or "https://example.com"}")``

            不依赖任何 LLM provider，仅依赖 camoufox / playwright。
            """

            from __future__ import annotations

            import time
            from typing import Any

            from web_crawler.ai.hooks import generate_combined_script
            from web_crawler.fetchers.camoufox import CamoufoxFetcher


            def {function_name}(url: str = {target_url_literal}, *, fetcher: Any = None) -> dict:
                """重放已记录的成功路径。返回与 ReverseAgent.run 一致结构的 dict。"""
                _fetcher_created = fetcher is None
                if fetcher is None:
                    fetcher = CamoufoxFetcher(headless=True, network_idle=False)
                history: list[dict] = []
                browser = fetcher._ensure_browser()
                context = browser.new_context(
                    extra_http_headers=getattr(fetcher, "extra_headers", None),
                    ignore_https_errors=not getattr(fetcher, "verify", True),
                )
                page = context.new_page()
                try:
{textwrap.indent(body, "                ")}
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
                    if _fetcher_created:
                        try:
                            fetcher.close()
                        except Exception:
                            pass
            ''')

    # -- 各动作的编译器 -----------------------------------------------------

    @staticmethod
    def _compile_navigate(rec: ActionRecord, **_: Any) -> list[str]:
        url = rec.params.get("url", "")
        url_literal = _py_string_literal(str(url))
        return [
            f"    # step {rec.step}: navigate",
            f'    page.goto({url_literal}, wait_until="domcontentloaded", timeout=30000)',
            '    try:',
            '        page.wait_for_load_state("domcontentloaded", timeout=3000)',
            '    except Exception:',
            '        pass',
            f"    history.append({{'step': {rec.step}, 'action': 'navigate', 'url': {url_literal}}})",
        ]

    @staticmethod
    def _compile_inject_hook(
        rec: ActionRecord,
        *,
        seen_hooks: set[str],
        **_: Any,
    ) -> list[str]:
        hooks = rec.params.get("hooks") or []
        # 去重：已注入过的不再注入
        new_hooks = [h for h in hooks if h not in seen_hooks]
        if not new_hooks:
            return [f"    # step {rec.step}: inject_hook (dedup, skipped)"]
        seen_hooks.update(new_hooks)
        hooks_literal = _py_string_literal(json.dumps(new_hooks, ensure_ascii=False))
        return [
            f"    # step {rec.step}: inject_hook {new_hooks}",
            f"    _hooks = {hooks_literal}",
            "    import json as _json",
            "    _hook_names = _json.loads(_hooks)",
            "    page.evaluate(generate_combined_script(_hook_names))",
            f"    history.append({{'step': {rec.step}, 'action': 'inject_hook', 'hooks': _hook_names}})",
        ]

    @staticmethod
    def _compile_wait(rec: ActionRecord, **_: Any) -> list[str]:
        seconds = float(rec.params.get("seconds", 1.0))
        seconds = max(0.1, min(seconds, 30.0))
        return [
            f"    # step {rec.step}: wait {seconds}s",
            f"    time.sleep({seconds!r})",
            f"    history.append({{'step': {rec.step}, 'action': 'wait', 'seconds': {seconds!r}}})",
        ]

    @staticmethod
    def _compile_extract(
        rec: ActionRecord,
        *,
        extract_results: list[tuple[str, str]],
        **_: Any,
    ) -> list[str]:
        param_name = str(rec.params.get("param_name", ""))
        result_value = rec.result_value or ""
        if result_value:
            extract_results.append((param_name, result_value))
            return [
                f"    # step {rec.step}: extract {param_name!r} (recorded value: redacted)",
                "    _hook_records = page.evaluate('() => (window.__hook_data__ || []).slice()') or []",
                "    _found = None",
                "    for _r in _hook_records:",
                "        _headers = _r.get('headers') or {}",
                "        if isinstance(_headers, dict):",
                "            for _k, _v in _headers.items():",
                f"                if {param_name.lower()!r} in _k.lower():",
                "                    _found = str(_v)",
                "                    break",
                "        if _found: break",
                f"    assert _found is not None, 'param {param_name!r} not found in hook data'",
                f"    history.append({{'step': {rec.step}, 'action': 'extract', 'param': {param_name!r}, 'value': _found}})",
            ]
        # 没记录到结果值：仅做 best-effort 提取
        return [
            f"    # step {rec.step}: extract {param_name!r} (no recorded value)",
            "    _hook_records = page.evaluate('() => (window.__hook_data__ || []).slice()') or []",
            f"    history.append({{'step': {rec.step}, 'action': 'extract', 'param': {param_name!r}}})",
        ]

    @staticmethod
    def _compile_analyze_js(rec: ActionRecord, **_: Any) -> list[str]:
        # JS 分析是 LLM 调用，重放时不复现；只写一条 history 占位
        return [
            f"    # step {rec.step}: analyze_js (LLM call skipped in replay)",
            f"    history.append({{'step': {rec.step}, 'action': 'analyze_js', 'note': 'LLM analysis not replayed'}})",
        ]

    @staticmethod
    def _compile_solve_captcha(rec: ActionRecord, **_: Any) -> list[str]:
        # 验证码处理也不在重放中复现（依赖运行时检测）
        return [
            f"    # step {rec.step}: solve_captcha (skipped, run-time detection required)",
            f"    history.append({{'step': {rec.step}, 'action': 'solve_captcha', 'note': 'skipped in replay'}})",
        ]

    @staticmethod
    def _compile_done(rec: ActionRecord, **_: Any) -> list[str]:
        return [f"    # step {rec.step}: done"]

    # -- 浏览器交互动作的编译器 ----------------------------------------------

    @staticmethod
    def _compile_click(rec: ActionRecord, **_: Any) -> list[str]:
        selector = str(rec.params.get("selector", ""))
        button = str(rec.params.get("button", "left"))
        selector_literal = _py_string_literal(selector)
        return [
            f"    # step {rec.step}: click {selector}",
            f"    page.click({selector_literal}, button={button!r}, timeout=10000)",
            f"    history.append({{'step': {rec.step}, 'action': 'click', 'selector': {selector_literal}}})",
        ]

    @staticmethod
    def _compile_type(rec: ActionRecord, **_: Any) -> list[str]:
        selector = str(rec.params.get("selector", ""))
        text = str(rec.params.get("text", ""))
        clear = bool(rec.params.get("clear", True))
        selector_literal = _py_string_literal(selector)
        text_literal = _py_string_literal(text)
        lines: list[str] = [f"    # step {rec.step}: type into {selector}"]
        if clear:
            lines.append(f'    page.fill({selector_literal}, "", timeout=10000)')
        lines.append(f"    page.type({selector_literal}, {text_literal}, timeout=10000)")
        lines.append(
            f"    history.append({{'step': {rec.step}, 'action': 'type', 'selector': {selector_literal}}})"
        )
        return lines

    @staticmethod
    def _compile_scroll(rec: ActionRecord, **_: Any) -> list[str]:
        selector = rec.params.get("selector")
        x = int(rec.params.get("x", 0))
        y = int(rec.params.get("y", 800))
        lines: list[str] = [f"    # step {rec.step}: scroll"]
        if selector:
            # selector 嵌入 JS 字符串：用 json.dumps 生成合法 JS 字符串字面量
            sel_js = json.dumps(str(selector))
            lines.append(
                f"    page.evaluate('document.querySelector({sel_js})?.scrollBy({x}, {y})')"
            )
        else:
            lines.append(f"    page.evaluate('window.scrollBy({x}, {y})')")
        lines.append(
            f"    history.append({{'step': {rec.step}, 'action': 'scroll', 'x': {x}, 'y': {y}}})"
        )
        return lines

    @staticmethod
    def _compile_press(rec: ActionRecord, **_: Any) -> list[str]:
        key = str(rec.params.get("key", "Enter"))
        selector = rec.params.get("selector")
        lines: list[str] = [f"    # step {rec.step}: press {key}"]
        if selector:
            sel_literal = _py_string_literal(str(selector))
            lines.append(f"    page.focus({sel_literal}, timeout=10000)")
        lines.append(f"    page.press({key!r})")
        lines.append(
            f"    history.append({{'step': {rec.step}, 'action': 'press', 'key': {key!r}}})"
        )
        return lines

    @staticmethod
    def _compile_hover(rec: ActionRecord, **_: Any) -> list[str]:
        selector = str(rec.params.get("selector", ""))
        selector_literal = _py_string_literal(selector)
        return [
            f"    # step {rec.step}: hover {selector}",
            f"    page.hover({selector_literal}, timeout=10000)",
            f"    history.append({{'step': {rec.step}, 'action': 'hover', 'selector': {selector_literal}}})",
        ]

    @staticmethod
    def _compile_select_option(rec: ActionRecord, **_: Any) -> list[str]:
        selector = str(rec.params.get("selector", ""))
        value = str(rec.params.get("value", ""))
        selector_literal = _py_string_literal(selector)
        value_literal = _py_string_literal(value)
        return [
            f"    # step {rec.step}: select_option {selector}={value}",
            f"    page.select_option({selector_literal}, {value_literal}, timeout=10000)",
            f"    history.append({{'step': {rec.step}, 'action': 'select_option', 'selector': {selector_literal}}})",
        ]


class ReplayRunner:
    """重放已编译脚本的最小运行器。

    用法
    ----
    >>> runner = ReplayRunner()
    >>> runner.load_script(script_source)
    >>> result = runner.run(url="https://example.com")
    """

    def __init__(self) -> None:
        self._script_source: str = ""
        self._namespace: dict[str, Any] = {}

    def load_script(self, source: str) -> None:
        """加载编译好的 Python 源码字符串。"""
        self._script_source = source
        # 执行源码以定义 replay 函数
        namespace: dict[str, Any] = {}
        exec(compile(source, "<replay_script>", "exec"), namespace)  # noqa: S102 - 受信脚本
        self._namespace = namespace

    def run(self, url: str = "", *, fetcher: Any = None) -> dict:
        """执行已加载的脚本。"""
        replay_fn = self._namespace.get("replay")
        if replay_fn is None:
            raise RuntimeError("no replay() function loaded; call load_script() first")
        return replay_fn(url=url, fetcher=fetcher)


# -- 辅助 -------------------------------------------------------------------

_PY_STRING_ESCAPE_RE = re.compile(r'[\\"]')


def _py_string_literal(value: str) -> str:
    """把字符串转成 Python 双引号字符串字面量。"""
    escaped = _PY_STRING_ESCAPE_RE.sub(lambda m: "\\" + m.group(0), value)
    return f'"{escaped}"'


__all__ = [
    "ActionRecord",
    "ReplayRunner",
    "RunRecorder",
    "ScriptCompiler",
]

"""MCP 逆向 / JS 分析 / 验证码类工具实现(从 ``server.py`` 拆出)。

Mixin 模式:方法通过 ``self`` 访问宿主 ``ReverseMCPServer`` 的运行时属性,
类型检查所需的属性声明集中在 :class:`_HostProtocol`。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from ..ai.analyzer import JSFragment
from ..ai.captcha import CaptchaType
from ..ai.hooks import HookLibrary, collect_hook_data
from ._host import HostContract
from ._server_helpers import (
    _DEFAULT_TEXT_LIMIT,
    _DEFAULT_WAIT_TIME,
    _MAX_TEXT_LIMIT,
    _PREVIEW_LIMIT,
    _clamp_int,
    _error,
    _to_json,
    _truncate_result_strings,
    _truncate_text,
)
from ._ssrf_gate import _check_url


class ReverseToolsMixin(HostContract):
    """逆向分析 / JS 分析 / 验证码处理类工具。"""

    def _tool_reverse_engineer_url(self, args: dict) -> str:
        url = args["url"]
        target_params = args.get("target_params") or []
        task = args.get("task") or f"分析 {url} 的加密参数"
        max_steps = max(1, min(int(args.get("max_steps", 20)), 100))
        max_text_length = _clamp_int(
            args.get("max_text_length"), _DEFAULT_TEXT_LIMIT, 1, _MAX_TEXT_LIMIT
        )

        # URL 门禁：仅 http/https、公网主机、无 userinfo（agent 路径不经 _run_browser_task）
        gate_error = _check_url(url)
        if gate_error:
            return _error(gate_error)

        # 构造 progress token，让 MCP 客户端可订阅长任务进度
        progress = self.make_progress_token("reverse_engineer_url", total=max_steps)

        # 优先使用 ReverseAgent 走完整逆向流程
        if self.agent is not None:
            try:
                # ReverseAgent.run 签名为 run(url, task="")，max_steps 与
                # target_params 通过 config 注入；用 dataclasses.replace 继承
                # base 全部配置，避免静默丢弃 planner/checkpoint/guard 等设置。
                from ..ai.watchdog import EventBus

                base_config = self.agent.config
                run_config = dataclasses.replace(
                    base_config,
                    max_steps=max_steps,
                    hooks=list(base_config.hooks) if base_config.hooks else None,
                    headless=True,
                    target_params=list(target_params) if target_params else None,
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
                try:
                    result = run_agent.run(url=url, task=task)
                finally:
                    run_agent.close()
                self.report_progress(
                    progress["progressToken"], max_steps, max_steps, message="done"
                )
                found = result.get("target_params_found") or {}
                if not isinstance(found, dict):
                    found = {}
                unknowns = [param for param in target_params if param not in found]
                if not target_params:
                    unknowns = ["未指定 target_params：无法确认站点全部加密参数是否都已覆盖"]
                limitations = [
                    (
                        "浏览器运行时逆向：只覆盖本次页面会话中被 Hook 记录的请求；"
                        "服务端渲染、原生客户端与未触发的懒加载分支不在范围内。"
                    ),
                    (
                        "置信度与算法说明由 LLM 生成，不构成等价性证明；"
                        "关键参数请用录制回放或重实现测试复核。"
                    ),
                    "hook_data 与 history 是采样快照，受 max_steps 与页面加载时机限制。",
                ]
                coverage = {
                    "mode": "agent",
                    "steps_executed": result.get("steps"),
                    "max_steps": max_steps,
                    "hook_records": len((result.get("hook_data") or {}).get("records", [])),
                    "target_params_requested": len(target_params),
                    "target_params_found": len(found),
                }
                payload: dict[str, Any] = {
                    "result": result,
                    "agent": True,
                    "truncations": [],
                    "limitations": limitations,
                    "unknowns": unknowns,
                    "coverage": coverage,
                }
                # 截断前自存完整结果：调用方可用 inspect_evidence 取回被截断的
                # history / Hook 请求体，而不必重跑一次浏览器/LLM。
                record = self._evidence_store().put(
                    "reverse_engineer_url",
                    _to_json(payload),
                    metadata={"url": url, "mode": "agent"},
                )
                # result 内嵌超长文本（反混淆 JS、hook 请求体、history 片段），
                # 递归截断并在顶层标注 truncations；路径相对于 result 展开，
                # 与历史响应格式保持一致，避免一次响应塞爆上游上下文。
                trimmed_result, truncations = _truncate_result_strings(result, max_text_length)
                response = dict(payload)
                response["result"] = trimmed_result
                response["truncations"] = truncations
                if record is not None:
                    response["evidence_id"] = record.evidence_id
                    response["evidence_note"] = (
                        "完整结果已登记为 evidence_id；字段被截断时用 "
                        "inspect_evidence 按 offset/limit 取回全文。"
                    )
                else:
                    response["_evidence_unavailable_reason"] = "full_result_exceeds_store_limit"
                return _to_json(response)
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
        limitations = [
            (
                "ReverseAgent 不可用或执行失败：本次只做页面 Hook 基本采集，"
                "没有 JS 反混淆、算法重实现或步进验证。"
            ),
            (
                "仅覆盖页面生命周期内 fetch/XHR/cookie 等 Hook 捕获到的请求；"
                "导航前已发出的请求不可见。"
            ),
        ]
        unknowns = (
            list(target_params) if target_params else ["未执行 JS 分析：目标参数的生成算法仍未知"]
        )
        coverage = {
            "mode": "fallback",
            "hooks": len(all_hooks),
            "hook_records": collected.get("hook_count", 0),
            "scripts": len(collected.get("scripts") or []),
            "wait_seconds": _DEFAULT_WAIT_TIME,
        }
        payload = {
            "agent": False,
            "note": note,
            "url": url,
            "target_params": target_params,
            "task": task,
            "truncations": [],
            "limitations": limitations,
            "unknowns": unknowns,
            "coverage": coverage,
            **collected,
        }
        # 降级采集同样在截断前自存完整结果（hook 捕获的请求体可能非常大）
        record = self._evidence_store().put(
            "reverse_engineer_url",
            _to_json(payload),
            metadata={"url": url, "mode": "fallback"},
        )
        trimmed, truncations = _truncate_result_strings(collected, max_text_length)
        response = {**payload, **trimmed, "truncations": truncations}
        if record is not None:
            response["evidence_id"] = record.evidence_id
            response["evidence_note"] = (
                "完整结果已登记为 evidence_id；字段被截断时用 inspect_evidence 取回全文。"
            )
        else:
            response["_evidence_unavailable_reason"] = "full_result_exceeds_store_limit"
        return _to_json(response)

    def _tool_inject_hooks(self, args: dict) -> str:
        url = args["url"]
        gate_error = _check_url(url)
        if gate_error:
            return _error(gate_error)
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
                "limitations": [
                    (
                        f"预览只保留前 {_PREVIEW_LIMIT} 条 Hook 记录；"
                        "完整记录请用 capture_network_requests 分页获取。"
                    ),
                    "Hook 只覆盖注入之后发生的请求；导航前已发出的请求不可见。",
                ],
                "coverage": {
                    "hooks_requested": len(hooks),
                    "preview_count": len(preview),
                },
            }
        )

    def _tool_analyze_js_code(self, args: dict) -> str:
        code = args["code"]
        url = args.get("url", "")
        target_param = args.get("target_param")
        max_length = _clamp_int(args.get("max_length"), _DEFAULT_TEXT_LIMIT, 1, _MAX_TEXT_LIMIT)

        fragment = JSFragment(source=code, url=url, is_minified=len(code) < 5000)
        try:
            result = self.analyzer.analyze_fragment(fragment)
        except Exception as exc:
            return _error("LLM call failed", details=str(exc))

        deobfuscated, truncated, full_len = _truncate_text(result.deobfuscated or "", max_length)
        payload = {
            "algorithm": result.algorithm,
            "inputs": result.inputs,
            "output": result.output,
            "code_flow": result.code_flow,
            "confidence": result.confidence,
            "deobfuscated": deobfuscated,
            "truncated": truncated,
            "full_length": full_len,
            "limitations": [
                "分析由 LLM 生成，需人工复核；confidence 是模型自评，不是正确性证明。",
                "deobfuscated 输出按 max_length 截断（truncated/full_length 标注）；需要全文时用 deobfuscate_js 单独获取。",
            ],
            "coverage": {
                "input_chars": len(code),
                "deobfuscated_chars": full_len,
            },
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
                "limitations": [
                    "正则提取只识别常见 webpack 模块语法；高度混淆或自定义 runtime 可能漏检。",
                    "source_preview 只保留每个模块前 500 字符，完整模块需按 id 从原始 bundle 中提取。",
                ],
                "coverage": {
                    "modules": len(modules),
                    "source_chars": len(source),
                },
            }
        )

    def _tool_deobfuscate_js(self, args: dict) -> str:
        code = args["code"]
        max_length = _clamp_int(args.get("max_length"), _DEFAULT_TEXT_LIMIT, 1, _MAX_TEXT_LIMIT)
        try:
            deobfuscated = self.analyzer.deobfuscate(code)
        except Exception as exc:
            return _error("LLM call failed", details=str(exc))
        sliced, truncated, full_len = _truncate_text(deobfuscated, max_length)
        return _to_json(
            {
                "deobfuscated": sliced,
                "length": len(sliced),
                "truncated": truncated,
                "full_length": full_len,
                "limitations": [
                    "反混淆结果由 LLM 生成，需人工复核；不保证与原代码逐行等价。",
                ],
                "coverage": {"input_chars": len(code), "output_chars": full_len},
            }
        )

    def _tool_reimplement_algorithm(self, args: dict) -> str:
        code = args["code"]
        language = args.get("language", "python")
        max_length = _clamp_int(args.get("max_length"), _DEFAULT_TEXT_LIMIT, 1, _MAX_TEXT_LIMIT)
        try:
            reimplemented = self.analyzer.suggest_reimplementation(code, language=language)
        except Exception as exc:
            return _error("LLM call failed", details=str(exc))
        sliced, truncated, full_len = _truncate_text(reimplemented, max_length)
        return _to_json(
            {
                "language": language,
                "code": sliced,
                "length": len(sliced),
                "truncated": truncated,
                "full_length": full_len,
                "limitations": [
                    "重实现由 LLM 生成，需用原始输入/输出样本做等价性测试后再上线。",
                ],
                "coverage": {"input_chars": len(code), "output_chars": full_len},
            }
        )

    def _tool_solve_captcha(self, args: dict) -> str:
        url = args["url"]
        gate_error = _check_url(url)
        if gate_error:
            return _error(gate_error)

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
        result["limitations"] = [
            "自动处理失败时需要人工介入；本工具不提供第三方验证码打码服务。",
            "检测基于常见选择器与 iframe 特征，页面自定义实现可能识别不到。",
        ]
        result["coverage"] = {
            "captcha_type": result.get("type"),
            "solved": result.get("solved"),
        }
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
        limitations = ["识别结果由 Vision/OCR 生成，需在提交前人工或回放校验。"]

        try:
            if mode == "text":
                image = args.get("image", "")
                if not image:
                    return _error("image is required for text mode")
                text = solver.solve_text(image, mime=mime)
                return _to_json(
                    {
                        "mode": "text",
                        "text": text,
                        "ok": bool(text),
                        "limitations": limitations,
                    }
                )
            if mode == "slider":
                bg = args.get("bg", "")
                slider = args.get("slider", "")
                if not bg or not slider:
                    return _error("bg and slider are required for slider mode")
                sol = solver.solve_slider(bg, slider)
                if sol is None:
                    return _to_json(
                        {
                            "mode": "slider",
                            "ok": False,
                            "message": "识别失败",
                            "limitations": limitations,
                        }
                    )
                return _to_json(
                    {
                        "mode": "slider",
                        "ok": True,
                        "x": sol.x,
                        "y": sol.y,
                        "method": sol.method,
                        "confidence": sol.confidence,
                        "limitations": limitations,
                    }
                )
            # click
            image = args.get("image", "")
            prompt = args.get("prompt", "")
            if not image:
                return _error("image is required for click mode")
            click_sol = solver.solve_click(image, prompt, mime=mime)
            if click_sol is None:
                return _to_json(
                    {
                        "mode": "click",
                        "ok": False,
                        "message": "识别失败",
                        "limitations": limitations,
                    }
                )
            return _to_json(
                {
                    "mode": "click",
                    "ok": True,
                    "points": [{"x": x, "y": y} for x, y in click_sol.points],
                    "labels": click_sol.labels,
                    "method": click_sol.method,
                    "limitations": limitations,
                }
            )
        except Exception as exc:
            return _error("captcha image solve failed", details=str(exc))

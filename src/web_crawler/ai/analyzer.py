"""AI-assisted analyzer for webpack-bundled, obfuscated JavaScript.

把已抓取到的 JS 代码片段交给 DeepSeek-V4-Pro 做离线分析：自动识别 webpack
模块结构、反混淆代码、还原加密/签名算法。本模块不直接对目标站点发起请求，
仅对已落地的代码文本做正则+字符串解析与 LLM 推理。

主要能力
--------
- :meth:`JSAnalyzer.analyze_fragment` 识别单段 JS 中的加密算法与输入输出；
- :meth:`JSAnalyzer.extract_webpack_modules` 用正则+字符串解析拆分 webpack 模块；
- :meth:`JSAnalyzer.identify_entry_point` 定位入口模块；
- :meth:`JSAnalyzer.trace_signing_flow` 追踪签名参数的生成链路；
- :meth:`JSAnalyzer.deobfuscate` / :meth:`JSAnalyzer.suggest_reimplementation`
  让模型反混淆或用指定语言重写加密逻辑。

示例
----
>>> from web_crawler.ai.analyzer import JSAnalyzer, JSFragment
>>> analyzer = JSAnalyzer()  # 默认 DeepSeek-V4-Pro
>>> modules = analyzer.extract_webpack_modules(bundle_source)
>>> entry = analyzer.identify_entry_point(modules)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .llm import DeepSeekProvider, LLMMessage, LLMProvider

# -- webpack 结构识别相关正则 -------------------------------------------------
# 定位 __webpack_modules__ 赋值语句
_WEBPACK_MODULES_RE = re.compile(r"__webpack_modules__\s*=\s*")
# 对象形态下的模块键：123: / "abc": / 'abc':
_MODULE_KEY_RE = re.compile(r'^\s*(?:"([^"]*)"|\'([^\']*)\'|(\d+))\s*:')
# 标准的 __webpack_require__(123) 调用
_REQUIRE_CALL_RE = re.compile(r"__webpack_require__\s*\(\s*(\d+)\s*\)")

# -- JSON / 代码块解析相关正则 -----------------------------------------------
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"^```(?:\w+)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)

# -- Prompt -----------------------------------------------------------------
_ANALYZE_SYSTEM_PROMPT = (
    "你是一名资深 JavaScript 逆向工程专家，精通 webpack 打包机制与各类代码混淆技术。"
    "你的任务是分析经过 webpack 打包和（或）混淆的 JavaScript 代码，识别其中的加密或签名算法、"
    "输入参数、输出格式与执行流程。你应熟悉 AES、RSA、HMAC、MD5、SHA 系列、Base64，"
    "以及自定义签名（如参数排序+拼接+加盐+哈希）等常见方案。"
    "仅依据代码本身下结论，不要臆测代码中未出现的逻辑；无法确定时给出较低置信度。"
)

_DEOBFUSCATE_SYSTEM_PROMPT = (
    "你是一名 JavaScript 逆向工程专家，擅长还原被压缩与混淆的代码。"
    "你会输出与原逻辑等价、可读性良好的代码，不增删功能。"
)

_REIMPL_SYSTEM_PROMPT = (
    "你是一名密码学与逆向工程专家，能把 JavaScript 实现的加密/签名逻辑等价改写为其他语言。"
    "你输出的代码可独立运行、依赖尽量少。"
)


# -- 字符串解析辅助 ----------------------------------------------------------
def _balanced_end(source: str, open_pos: int) -> int | None:
    """返回 ``source[open_pos]`` 处开括号匹配闭括号之后的位置（exclusive）。

    支持嵌套同类型括号，并跳过字符串字面量（``'`` ``"`` `` ` ``）与注释
    （``//``、``/* */``）中的括号。``open_pos`` 必须指向 ``{`` / ``[`` / ``(`` 之一。
    无法匹配时返回 ``None``。

    注意：不识别正则字面量，这是有意的启发式取舍，足以应对绝大多数 webpack 产物。
    """
    pairs = {"{": "}", "[": "]", "(": ")"}
    open_ch = source[open_pos]
    close_ch = pairs.get(open_ch)
    if close_ch is None:
        return None
    depth = 0
    i = open_pos
    n = len(source)
    quote: str | None = None
    while i < n:
        ch = source[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = source[i + 1]
            if nxt == "/":  # 行注释
                nl = source.find("\n", i)
                i = n if nl == -1 else nl + 1
                continue
            if nxt == "*":  # 块注释
                end = source.find("*/", i + 2)
                i = n if end == -1 else end + 2
                continue
        if ch in ('"', "'", "`"):
            quote = ch
            i += 1
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _split_top_level(body: str, sep: str = ",") -> list[str]:
    """在顶层（深度 0）按 ``sep`` 切分 ``body``，忽略字符串/注释/嵌套结构内的分隔符。"""
    parts: list[str] = []
    opens = {"{", "[", "("}
    closes = {"}", "]", ")"}
    depth = 0
    start = 0
    i = 0
    n = len(body)
    quote: str | None = None
    while i < n:
        ch = body[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = body[i + 1]
            if nxt == "/":
                nl = body.find("\n", i)
                i = n if nl == -1 else nl + 1
                continue
            if nxt == "*":
                end = body.find("*/", i + 2)
                i = n if end == -1 else end + 2
                continue
        if ch in ('"', "'", "`"):
            quote = ch
            i += 1
            continue
        if ch in opens:
            depth += 1
        elif ch in closes:
            if depth > 0:
                depth -= 1
        elif ch == sep and depth == 0:
            parts.append(body[start:i])
            start = i + 1
        i += 1
    parts.append(body[start:])
    return parts


def _extract_function_body(value: str) -> str:
    """从 ``function(...){...}`` 形态中截取到匹配闭括号为止的完整片段。

    找不到平衡的 ``{...}`` 时原样返回 ``value``，便于兜底分析。
    """
    fb = value.find("{")
    if fb == -1:
        return value
    end = _balanced_end(value, fb)
    if end is None:
        return value
    return value[:end]


def _third_param_name(value: str) -> str | None:
    """取函数形参列表的第 3 个参数名（webpack 模块里通常是 require 别名）。"""
    m = re.search(r"function\s*\(([^)]*)\)", value)
    if not m:
        return None
    params = [p.strip() for p in m.group(1).split(",") if p.strip()]
    return params[2] if len(params) >= 3 else None


def _extract_deps(src: str, alias: str) -> list[int]:
    """在 ``src`` 中查找 ``alias(数字)`` 形态的模块依赖调用。"""
    deps: list[int] = []
    for m in re.finditer(rf"\b{re.escape(alias)}\s*\(\s*(\d+)\s*\)", src):
        deps.append(int(m.group(1)))
    return deps


def _extract_export_keys(src: str, alias: str) -> list[str]:
    """提取 ``alias.d(exports, { "k": ..., "k2": ... })`` 中定义的导出名。"""
    keys: list[str] = []
    for m in re.finditer(rf"\b{re.escape(alias)}\.d\s*\(", src):
        open_paren = src.find("(", m.start())
        if open_paren == -1:  # pragma: no cover - 正则已保证 ( 存在
            continue
        end = _balanced_end(src, open_paren)
        if end is None:
            continue
        call_args = src[open_paren:end]
        ob = call_args.find("{")
        if ob == -1:
            continue
        ob_end = _balanced_end(call_args, ob)
        if ob_end is None:
            continue
        obj_body = call_args[ob + 1 : ob_end - 1]
        for entry in _split_top_level(obj_body, ","):
            km = _MODULE_KEY_RE.match(entry)
            if km:
                keys.append(km.group(1) or km.group(2) or km.group(3) or "")
    return keys


def _extract_json(text: str) -> dict[str, Any]:
    """容错解析模型回复中的 JSON 对象，兼容 ```json 代码块包裹与正文嵌入。"""
    text = text.strip()
    fence = _CODE_FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    return {}


def _strip_code_fence(text: str) -> str:
    """去掉外层 ```lang ... ``` 代码块包裹（若存在）。"""
    text = text.strip()
    m = _CODE_FENCE_RE.match(text)
    if m:
        return m.group(1).strip()
    return text


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# -- 数据结构 ----------------------------------------------------------------
@dataclass
class JSFragment:
    """待分析的一段 JS 代码片段。"""

    source: str
    url: str = ""
    size: int = 0
    is_minified: bool = False


@dataclass
class AnalysisResult:
    """单段 JS 代码的分析结论。"""

    algorithm: str = "unknown"
    inputs: list[str] = field(default_factory=list)
    output: str = ""
    code_flow: str = ""
    confidence: float = 0.0
    deobfuscated: str | None = None


@dataclass
class WebpackModule:
    """从 webpack bundle 中拆出的单个模块。"""

    id: int
    source: str
    dependencies: list[int] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)


# -- 分析引擎 ----------------------------------------------------------------
class JSAnalyzer:
    """基于 LLM 的 webpack 混淆 JS 分析引擎。

    provider 为 None 时默认创建 DeepSeekProvider（model=deepseek-v4-pro）。
    所有走 LLM 的方法都会先把超过 ``max_chars`` 的代码做前后截断处理。
    """

    def __init__(
        self,
        provider: LLMProvider | None = None,
        model: str = "deepseek-v4-pro",
        max_chars: int = 50000,
    ) -> None:
        if provider is None:
            provider = DeepSeekProvider(model=model)
        self.provider = provider
        self.model = provider.model or model
        self.max_chars = max_chars

    # -- 内部工具 -----------------------------------------------------------
    def _truncate_code(self, code: str) -> str:
        """超过 max_chars 的代码取前半+后半，中间标注 [TRUNCATED]。"""
        if len(code) <= self.max_chars:
            return code
        half = self.max_chars // 2
        head = code[:half]
        tail = code[len(code) - half :]
        omitted = len(code) - self.max_chars
        return f"{head}\n\n/* ... [TRUNCATED {omitted} chars] ... */\n\n{tail}"

    def _call_llm(self, system: str, user: str, temperature: float = 0.0) -> str:
        """统一调用 LLM，返回文本；模型因长度被截断时追加标记。"""
        messages = [LLMMessage("system", system), LLMMessage("user", user)]
        resp = self.provider.chat(messages, temperature=temperature)
        content = resp.content or ""
        if resp.finish_reason == "length":
            content = content.rstrip() + "\n\n[... 模型输出因长度限制被截断]"
        return content

    # -- 对外 API -----------------------------------------------------------
    def analyze_fragment(self, fragment: JSFragment) -> AnalysisResult:
        """分析单段 JS 代码，识别其中的加密算法、输入参数与输出格式。"""
        code = self._truncate_code(fragment.source)
        size = fragment.size or len(fragment.source)
        user = (
            "请分析下面这段 JavaScript 代码片段中的加密/签名逻辑。\n\n"
            f"来源：{fragment.url or '(unknown)'}\n"
            f"大小：{size} 字节\n"
            f"是否压缩/混淆：{fragment.is_minified}\n\n"
            "代码：\n```javascript\n"
            f"{code}\n"
            "```\n\n"
            "请仅输出一个 JSON 对象（不要任何额外文字，不要 Markdown 代码块标记），字段如下：\n"
            "{\n"
            '  "algorithm": "识别到的算法，如 AES-CBC / RSA / HMAC-SHA256 / MD5 / SHA-256 / Base64 / 自定义签名 / 无",\n'
            '  "inputs": ["参与加密/签名的输入参数名，按顺序，如 timestamp、nonce、body"],\n'
            '  "output": "输出格式说明，如 64 位十六进制字符串 / Base64 字符串",\n'
            '  "code_flow": "用简短自然语言描述加密流程的关键步骤",\n'
            '  "confidence": 0.0 到 1.0 的置信度（小数），\n'
            '  "deobfuscated": "反混淆后的可读代码；本段不涉及加密或无法反混淆时为 null"\n'
            "}\n"
        )
        raw = self._call_llm(_ANALYZE_SYSTEM_PROMPT, user, temperature=0.0)
        data = _extract_json(raw)
        if not data:
            return AnalysisResult(code_flow=raw.strip()[:500] or "无法解析模型输出")
        confidence = max(0.0, min(1.0, _to_float(data.get("confidence"), 0.0)))
        deob = data.get("deobfuscated")
        return AnalysisResult(
            algorithm=str(data.get("algorithm") or "unknown"),
            inputs=[str(x) for x in (data.get("inputs") or []) if x is not None],
            output=str(data.get("output") or ""),
            code_flow=str(data.get("code_flow") or ""),
            confidence=confidence,
            deobfuscated=str(deob) if deob else None,
        )

    def extract_webpack_modules(self, source: str) -> list[WebpackModule]:
        """用正则+字符串解析提取 webpack 模块。

        识别 ``__webpack_modules__`` 的对象/数组字面量，逐条解析模块 id、
        依赖（``__webpack_require__(N)`` 或压缩后的 require 别名）与导出
        （``alias.d(exports, {...})``）。不依赖外部 AST 库，结果为启发式。
        """
        m = _WEBPACK_MODULES_RE.search(source)
        if not m:
            return []
        rest = source[m.end() :]
        cm = re.search(r"[{\[]", rest)
        if not cm:
            return []
        container_pos = m.end() + cm.start()
        container_end = _balanced_end(source, container_pos)
        if container_end is None:
            return []
        body = source[container_pos + 1 : container_end - 1]
        is_array = source[container_pos] == "["
        segments = _split_top_level(body, ",")

        modules: list[WebpackModule] = []
        for idx, seg in enumerate(segments):
            seg = seg.strip()
            if not seg:
                continue
            if is_array:
                mid = idx
                value = seg
            else:
                km = _MODULE_KEY_RE.match(seg)
                if not km:
                    continue
                key_str = km.group(3) if km.group(3) is not None else (km.group(1) or km.group(2))
                if key_str is None:
                    continue
                # 仅保留数字 id，与 list[int] 依赖类型一致
                try:
                    mid = int(key_str)
                except ValueError:
                    continue
                value = seg[km.end() :].lstrip()

            mod_source = _extract_function_body(value)
            # require 别名：优先标准名，缺失时取函数第 3 个形参
            alias = "__webpack_require__"
            if alias not in mod_source:
                third = _third_param_name(value)
                if third:
                    alias = third

            deps = _extract_deps(mod_source, "__webpack_require__")
            if alias != "__webpack_require__":
                deps.extend(_extract_deps(mod_source, alias))
            seen: set[int] = set()
            uniq_deps: list[int] = []
            for d in deps:
                if d not in seen:
                    seen.add(d)
                    uniq_deps.append(d)

            exports = _extract_export_keys(mod_source, "__webpack_require__")
            if alias != "__webpack_require__":
                exports.extend(_extract_export_keys(mod_source, alias))

            modules.append(
                WebpackModule(id=mid, source=mod_source, dependencies=uniq_deps, exports=exports)
            )
        return modules

    def identify_entry_point(self, modules: list[WebpackModule]) -> int | None:
        """识别入口模块。

        启发式：入口通常不被其他模块依赖（根模块），且常带有 ESM 标记
        （``__webpack_require__.r``）或拥有最多出向依赖。无法判定时返回 None。
        """
        if not modules:
            return None
        depended: set[int] = set()
        for mod in modules:
            depended.update(mod.dependencies)
        candidates = [mod for mod in modules if mod.id not in depended] or list(modules)

        def score(mod: WebpackModule) -> tuple[int, int]:
            has_esm_mark = (
                1
                if ("__webpack_require__.r" in mod.source or re.search(r"\w+\.r\(", mod.source))
                else 0
            )
            return (has_esm_mark, len(mod.dependencies))

        candidates.sort(key=score, reverse=True)
        return candidates[0].id

    def trace_signing_flow(self, modules: list[WebpackModule], target_param: str) -> list[int]:
        """追踪某个签名参数的生成流程，返回相关模块 ID 链（依赖在前，产出者在后）。"""
        by_id = {mod.id: mod for mod in modules}
        # 命中目标参数名的模块视为产出者
        producers = [mod for mod in modules if target_param in mod.source]
        visited: set[int] = set()
        chain: list[int] = []

        def visit(mid: int) -> None:
            if mid in visited or mid not in by_id:
                return
            visited.add(mid)
            for dep in by_id[mid].dependencies:
                visit(dep)
            chain.append(mid)  # 后序追加：依赖先入链，产出者垫后

        for prod in producers:
            visit(prod.id)
        return chain

    def deobfuscate(self, code: str) -> str:
        """让 AI 反混淆代码，返回可读版本。"""
        truncated = self._truncate_code(code)
        user = (
            "请将下面经过压缩/混淆的 JavaScript 代码反混淆为可读的等价代码：\n"
            "- 还原有意义的变量名与函数名；\n"
            "- 展开压缩代码为多行；\n"
            "- 保留原始逻辑，不要增删功能；\n"
            "- 只输出反混淆后的代码，不要解释。\n\n"
            "代码：\n```javascript\n"
            f"{truncated}\n"
            "```"
        )
        raw = self._call_llm(_DEOBFUSCATE_SYSTEM_PROMPT, user, temperature=0.0)
        return _strip_code_fence(raw).strip()

    def suggest_reimplementation(self, code: str, language: str = "python") -> str:
        """让 AI 用指定语言重写加密逻辑，返回等价代码。"""
        truncated = self._truncate_code(code)
        user = (
            f"请用 {language} 重写下面 JavaScript 代码所实现的加密/签名逻辑，"
            "要求行为等价、可独立运行、依赖尽量少。\n"
            "- 只输出代码，不要解释；\n"
            "- 用注释标注关键步骤。\n\n"
            "JavaScript 代码：\n```javascript\n"
            f"{truncated}\n"
            "```"
        )
        raw = self._call_llm(_REIMPL_SYSTEM_PROMPT, user, temperature=0.0)
        return _strip_code_fence(raw).strip()


__all__ = ["AnalysisResult", "JSAnalyzer", "JSFragment", "WebpackModule"]

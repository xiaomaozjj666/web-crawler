"""JS 逆向 Agent 主循环模块。

编排浏览器、Hook、AI 分析器、验证码处理，形成"观察-思考-行动"的自主循环，
用于定位网页前端动态生成的加密参数（如 ``Anti-Content``、``X-Bogus``、
``_signature`` 等）。

工作流程
--------
1. 启动 CamoufoxFetcher（默认 headless=False，便于人工介入）；
2. 创建新的浏览器上下文，在导航前通过 ``add_init_script`` 注入 Hook 脚本；
3. 进入循环：
   - 观察：收集 Hook 捕获数据、网络请求、页面脚本、验证码检测；
   - 思考：把观察结果交给 DeepSeek-V4-Pro 决定下一步动作；
   - 行动：执行 AI 决定的动作（注入新 Hook / 分析 JS / 等待 /
     提取参数 / 处理验证码 / 完成）；
4. 达到 ``max_steps`` 或 AI 返回 ``done`` 时停止；
5. 返回包含 ``success``、``target_params_found``、``analysis``、
   ``hook_data``、``steps``、``history`` 的结果字典。

错误处理
--------
- 浏览器崩溃：尝试重启一次；
- Hook 注入失败：记录到 history 后继续；
- AI 分析失败：降级为纯 Hook 模式（仅靠 Hook 数据推进循环）；
- 所有异常都写入 history，便于事后审计。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from typing_extensions import Self

from ..fetchers.camoufox import CamoufoxFetcher
from .analyzer import AnalysisResult, JSAnalyzer, JSFragment
from .captcha import CaptchaManager, CaptchaType
from .hooks import collect_hook_data, generate_combined_script
from .llm import DEFAULT_MODEL, DeepSeekProvider, LLMMessage, LLMProvider

# ---------------------------------------------------------------------------
# 常量与 Prompt
# ---------------------------------------------------------------------------

_THINK_SYSTEM_PROMPT = (
    "你是 JS 逆向专家 Agent。你的任务是分析网页的加密参数生成逻辑。"
    "你会收到当前页面的观察结果（URL、Hook 捕获数据、网络请求、脚本列表、"
    "验证码类型、DOM 摘要）以及历史动作。请基于这些信息决定下一步动作。"
)

_THINK_USER_TEMPLATE = (
    "## 任务\n{task}\n\n"
    "## 当前观察\n"
    "- URL: {url}\n"
    "- 页面标题: {page_title}\n"
    "- 验证码类型: {captcha_type}\n"
    "- Hook 数据条数: {hook_count}\n"
    "- 网络请求数: {network_count}\n"
    "- 页面脚本数: {script_count}\n\n"
    "## Hook 数据摘录（最多 20 条）\n{hook_summary}\n\n"
    "## 网络请求摘录（最多 20 条）\n{network_summary}\n\n"
    "## 页面脚本列表（最多 20 个）\n{script_summary}\n\n"
    "## DOM 摘要（前 2000 字符）\n{dom_summary}\n\n"
    "## 历史动作（最近 10 步）\n{history_summary}\n\n"
    "## 目标参数\n{target_params}\n\n"
    "请决定下一步动作，仅输出一个 JSON 对象（不要任何额外文字，不要 Markdown 代码块标记），格式如下：\n"
    "{{\n"
    '  "action_type": "navigate | inject_hook | analyze_js | wait | extract | solve_captcha | done",\n'
    '  "params": {{...}},\n'
    '  "reasoning": "你的推理过程"\n'
    "}}\n\n"
    "动作说明：\n"
    '- navigate: 导航到新 URL，params: {{"url": "..."}}\n'
    '- inject_hook: 注入新的 Hook，params: {{"hooks": ["fetch_hook", ...]}}\n'
    '- analyze_js: 分析捕获的 JS，params: {{"script_urls": ["..."], "target_params": [...]}}\n'
    '- wait: 等待一段时间，params: {{"seconds": 3.0}}\n'
    '- extract: 尝试从 Hook 数据中提取目标参数，params: {{"param_name": "..."}}\n'
    "- solve_captcha: 处理验证码，params: {}\n"
    '- done: 任务完成，params: {{"success": true/false, "summary": "..."}}\n'
)

# JSON / 代码块解析正则
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"^```(?:\w+)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)

# 拉取 JS 源码用的默认 UA
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _extract_json(text: str) -> dict[str, Any]:
    """容错解析模型回复中的 JSON 对象，兼容代码块包裹与正文嵌入。"""
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


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """单步观察结果，描述当前页面状态。"""

    url: str
    hook_data: dict
    network_requests: list[dict]
    scripts: list[str]
    captcha_type: CaptchaType
    page_title: str
    dom_summary: str


@dataclass
class Action:
    """AI 决定的下一步动作。"""

    action_type: str
    params: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        """从 LLM 返回的 dict 构造 Action。"""
        return cls(
            action_type=str(data.get("action_type", "wait")),
            params=dict(data.get("params") or {}),
            reasoning=str(data.get("reasoning") or ""),
        )


@dataclass
class ReverseAgentConfig:
    """JS 逆向 Agent 配置。"""

    max_steps: int = 20
    hooks: list[str] | None = None
    headless: bool = False
    wait_after_navigate: float = 3.0
    target_params: list[str] | None = None
    proxy: str | None = None
    os_name: str = "windows"


# ---------------------------------------------------------------------------
# 主 Agent
# ---------------------------------------------------------------------------


class ReverseAgent:
    """JS 逆向 Agent 主循环。

    编排浏览器、Hook、AI 分析器、验证码处理，形成"观察-思考-行动"的自主循环。
    同步入口 :meth:`run` 与异步入口 :meth:`arun` 共享同一套配置与分析器，
    适用于定位前端动态生成的加密参数（Anti-Content / X-Bogus / _signature 等）。
    """

    def __init__(
        self,
        config: ReverseAgentConfig | None = None,
        provider: LLMProvider | None = None,
        analyzer: JSAnalyzer | None = None,
    ) -> None:
        self.config = config or ReverseAgentConfig()
        self.provider = provider or DeepSeekProvider(model=DEFAULT_MODEL)
        self.analyzer = analyzer or JSAnalyzer(provider=self.provider)
        self.captcha_manager = CaptchaManager()
        self.fetcher: CamoufoxFetcher | None = None
        self._context: Any = None
        self._page: Any = None
        # 网络请求监听日志（由 page.on("request") 写入）
        self._network_log: list[dict] = []
        # 最近一次观察的 hook 数据缓存，供 _try_extract_param 复用
        self._hook_data_cache: dict = {"records": [], "count": 0}

    # ------------------------------------------------------------------
    # 主入口（同步）
    # ------------------------------------------------------------------

    def run(self, url: str, task: str = "") -> dict:
        """启动浏览器、注入 Hook、循环观察-思考-行动，返回结果字典。"""
        self.fetcher = CamoufoxFetcher(
            headless=self.config.headless,
            os=self.config.os_name,
            proxy=self.config.proxy,
            network_idle=False,
        )
        history: list[dict] = []
        target_params_found: dict[str, str] = {}
        analysis: AnalysisResult | None = None

        try:
            context, page = self._create_page(self.config.hooks)
            self._context = context
            self._page = page

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                history.append({"step": 0, "event": "navigate_error", "error": str(exc)})
            time.sleep(self.config.wait_after_navigate)

            for step in range(1, self.config.max_steps + 1):
                try:
                    observation = self._observe(page)
                except Exception as exc:
                    history.append({"step": step, "event": "observe_error", "error": str(exc)})
                    if self._try_recover_page(url):
                        continue
                    break

                try:
                    action = self._think(observation, task, history)
                except Exception as exc:
                    history.append({"step": step, "event": "think_error", "error": str(exc)})
                    action = self._fallback_action(observation)

                history.append({
                    "step": step,
                    "action": action.action_type,
                    "params": action.params,
                    "reasoning": action.reasoning,
                    "observation": {
                        "url": observation.url,
                        "hook_count": observation.hook_data.get("count", 0),
                        "network_count": len(observation.network_requests),
                        "script_count": len(observation.scripts),
                        "captcha_type": observation.captcha_type.value,
                    },
                })

                if action.action_type == "done":
                    break

                try:
                    result = self._act(page, action)
                    if action.action_type == "inject_hook" and result is False:
                        history.append({"step": step, "event": "inject_hook_failed"})
                    elif action.action_type == "extract" and result:
                        param_name = action.params.get("param_name", "")
                        if param_name:
                            target_params_found[param_name] = result
                    elif action.action_type == "analyze_js" and isinstance(result, AnalysisResult):
                        analysis = result
                except Exception as exc:
                    history.append({"step": step, "event": "act_error", "error": str(exc)})

            final_hook_data = self._read_hook_data(page)
            success = bool(target_params_found)
            if self.config.target_params:
                success = all(p in target_params_found for p in self.config.target_params)

            return {
                "success": success,
                "target_params_found": target_params_found,
                "analysis": analysis,
                "hook_data": final_hook_data,
                "steps": len(history),
                "history": history,
            }
        finally:
            self._cleanup_sync()

    # ------------------------------------------------------------------
    # 主入口（异步）
    # ------------------------------------------------------------------

    async def arun(self, url: str, task: str = "") -> dict:
        """异步版本的主循环。"""
        self.fetcher = CamoufoxFetcher(
            headless=self.config.headless,
            os=self.config.os_name,
            proxy=self.config.proxy,
            network_idle=False,
        )
        history: list[dict] = []
        target_params_found: dict[str, str] = {}
        analysis: AnalysisResult | None = None

        try:
            context, page = await self._create_page_async(self.config.hooks)
            self._context = context
            self._page = page

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                history.append({"step": 0, "event": "navigate_error", "error": str(exc)})
            await asyncio.sleep(self.config.wait_after_navigate)

            for step in range(1, self.config.max_steps + 1):
                try:
                    observation = await self._observe_async(page)
                except Exception as exc:
                    history.append({"step": step, "event": "observe_error", "error": str(exc)})
                    if await self._try_recover_page_async(url):
                        continue
                    break

                try:
                    action = await self._think_async(observation, task, history)
                except Exception as exc:
                    history.append({"step": step, "event": "think_error", "error": str(exc)})
                    action = self._fallback_action(observation)

                history.append({
                    "step": step,
                    "action": action.action_type,
                    "params": action.params,
                    "reasoning": action.reasoning,
                    "observation": {
                        "url": observation.url,
                        "hook_count": observation.hook_data.get("count", 0),
                        "network_count": len(observation.network_requests),
                        "script_count": len(observation.scripts),
                        "captcha_type": observation.captcha_type.value,
                    },
                })

                if action.action_type == "done":
                    break

                try:
                    result = await self._act_async(page, action)
                    if action.action_type == "inject_hook" and result is False:
                        history.append({"step": step, "event": "inject_hook_failed"})
                    elif action.action_type == "extract" and result:
                        param_name = action.params.get("param_name", "")
                        if param_name:
                            target_params_found[param_name] = result
                    elif action.action_type == "analyze_js" and isinstance(result, AnalysisResult):
                        analysis = result
                except Exception as exc:
                    history.append({"step": step, "event": "act_error", "error": str(exc)})

            final_hook_data = await self._read_hook_data_async(page)
            success = bool(target_params_found)
            if self.config.target_params:
                success = all(p in target_params_found for p in self.config.target_params)

            return {
                "success": success,
                "target_params_found": target_params_found,
                "analysis": analysis,
                "hook_data": final_hook_data,
                "steps": len(history),
                "history": history,
            }
        finally:
            await self._cleanup_async()

    # ------------------------------------------------------------------
    # 观察
    # ------------------------------------------------------------------

    def _observe(self, page: Any) -> Observation:
        """收集页面状态。"""
        url = self._safe_page_url(page)
        # collect_hook_data 会清空浏览器侧数组，缓存一份供 _try_extract_param 复用
        hook_data = collect_hook_data(page)
        self._hook_data_cache = hook_data
        network_requests = list(self._network_log)
        scripts = self._collect_scripts(page)
        captcha_info = self.captcha_manager.detector.detect(page)
        captcha_type = captcha_info.type if captcha_info else CaptchaType.NONE
        try:
            page_title = page.title()
        except Exception:
            page_title = ""
        try:
            dom_summary = page.content()
        except Exception:
            dom_summary = ""
        return Observation(
            url=url,
            hook_data=hook_data,
            network_requests=network_requests,
            scripts=scripts,
            captcha_type=captcha_type,
            page_title=page_title,
            dom_summary=dom_summary[:2000],
        )

    async def _observe_async(self, page: Any) -> Observation:
        """异步收集页面状态。"""
        url = self._safe_page_url(page)
        # 异步版 collect_hook_data：内联 evaluate 以支持 await
        records = await page.evaluate(
            """() => {
                const data = window.__hook_data__ || [];
                const snapshot = data.slice();
                try { window.__hook_data__ = []; } catch (e) {}
                return snapshot;
            }"""
        ) or []
        hook_data = {"records": list(records), "count": len(records)}
        self._hook_data_cache = hook_data
        network_requests = list(self._network_log)
        scripts = await self._collect_scripts_async(page)
        captcha_info = self.captcha_manager.detector.detect(page)
        captcha_type = captcha_info.type if captcha_info else CaptchaType.NONE
        try:
            page_title = await page.title()
        except Exception:
            page_title = ""
        try:
            dom_summary = await page.content()
        except Exception:
            dom_summary = ""
        return Observation(
            url=url,
            hook_data=hook_data,
            network_requests=network_requests,
            scripts=scripts,
            captcha_type=captcha_type,
            page_title=page_title,
            dom_summary=dom_summary[:2000],
        )

    # ------------------------------------------------------------------
    # 思考
    # ------------------------------------------------------------------

    def _think(self, observation: Observation, task: str, history: list) -> Action:
        """调 DeepSeek 分析当前状态，决定下一步。"""
        prompt = self._build_think_prompt(observation, task, history)
        messages = [LLMMessage("system", _THINK_SYSTEM_PROMPT), LLMMessage("user", prompt)]
        resp = self.provider.chat(messages, temperature=0.0)
        return self._parse_action(resp.content or "")

    async def _think_async(self, observation: Observation, task: str, history: list) -> Action:
        """异步调 DeepSeek 分析当前状态。"""
        prompt = self._build_think_prompt(observation, task, history)
        messages = [LLMMessage("system", _THINK_SYSTEM_PROMPT), LLMMessage("user", prompt)]
        if hasattr(self.provider, "achat"):
            resp = await self.provider.achat(messages, temperature=0.0)
        else:
            resp = self.provider.chat(messages, temperature=0.0)
        return self._parse_action(resp.content or "")

    def _parse_action(self, content: str) -> Action:
        """解析 LLM 返回的动作为 Action 对象，解析失败时降级为 wait。"""
        data = _extract_json(content)
        if not data:
            return Action(
                action_type="wait",
                params={"seconds": 1.0},
                reasoning="LLM 返回无法解析，默认等待",
            )
        return Action.from_dict(data)

    def _fallback_action(self, observation: Observation) -> Action:
        """AI 分析失败时的降级动作：纯 Hook 模式提取目标参数。"""
        target = (self.config.target_params or [""])[0]
        return Action(
            action_type="extract",
            params={"param_name": target},
            reasoning="AI 分析失败，降级为纯 Hook 模式提取",
        )

    # ------------------------------------------------------------------
    # 行动
    # ------------------------------------------------------------------

    def _act(self, page: Any, action: Action) -> Any:
        """执行动作。"""
        atype = action.action_type
        if atype == "navigate":
            url = action.params.get("url")
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(self.config.wait_after_navigate)
            return None
        if atype == "inject_hook":
            hooks = action.params.get("hooks")
            return self._inject_hooks(page, hooks)
        if atype == "analyze_js":
            scripts = action.params.get("script_urls", [])
            target_params = action.params.get("target_params", self.config.target_params or [])
            return self._analyze_captured_js(scripts, target_params)
        if atype == "wait":
            seconds = float(action.params.get("seconds", 1.0))
            time.sleep(max(0.1, min(seconds, 30.0)))
            return None
        if atype == "extract":
            param_name = action.params.get("param_name", "")
            if not param_name:
                return None
            return self._try_extract_param(page, param_name)
        if atype == "solve_captcha":
            return self.captcha_manager.handle(page)
        return None

    async def _act_async(self, page: Any, action: Action) -> Any:
        """异步执行动作。"""
        atype = action.action_type
        if atype == "navigate":
            url = action.params.get("url")
            if url:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(self.config.wait_after_navigate)
            return None
        if atype == "inject_hook":
            hooks = action.params.get("hooks")
            return await self._inject_hooks_async(page, hooks)
        if atype == "analyze_js":
            scripts = action.params.get("script_urls", [])
            target_params = action.params.get("target_params", self.config.target_params or [])
            # _analyze_captured_js 内部用 httpx 同步拉取，无需 await
            return self._analyze_captured_js(scripts, target_params)
        if atype == "wait":
            seconds = float(action.params.get("seconds", 1.0))
            await asyncio.sleep(max(0.1, min(seconds, 30.0)))
            return None
        if atype == "extract":
            param_name = action.params.get("param_name", "")
            if not param_name:
                return None
            return await self._try_extract_param_async(page, param_name)
        if atype == "solve_captcha":
            # CaptchaManager.handle 是同步的
            return self.captcha_manager.handle(page)
        return None

    # ------------------------------------------------------------------
    # Hook 注入
    # ------------------------------------------------------------------

    def _inject_hooks(self, page: Any, hook_names: list[str] | None) -> bool:
        """注入 Hook 脚本（运行时通过 evaluate 执行 IIFE）。"""
        try:
            script = generate_combined_script(hook_names)
            page.evaluate(script)
            return True
        except Exception:
            return False

    async def _inject_hooks_async(self, page: Any, hook_names: list[str] | None) -> bool:
        """异步注入 Hook 脚本。"""
        try:
            script = generate_combined_script(hook_names)
            await page.evaluate(script)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 脚本收集
    # ------------------------------------------------------------------

    def _collect_scripts(self, page: Any) -> list[str]:
        """收集页面所有 JS URL。"""
        try:
            return page.evaluate("""
                () => {
                    const scripts = document.querySelectorAll('script[src]');
                    return Array.from(scripts).map(s => s.src).filter(Boolean);
                }
            """) or []
        except Exception:
            return []

    async def _collect_scripts_async(self, page: Any) -> list[str]:
        """异步收集页面所有 JS URL。"""
        try:
            return await page.evaluate("""
                () => {
                    const scripts = document.querySelectorAll('script[src]');
                    return Array.from(scripts).map(s => s.src).filter(Boolean);
                }
            """) or []
        except Exception:
            return []

    # ------------------------------------------------------------------
    # JS 分析
    # ------------------------------------------------------------------

    def _analyze_captured_js(
        self,
        scripts: list[str],
        target_params: list[str],
    ) -> AnalysisResult | None:
        """分析捕获的 JS，返回最相关的分析结果。

        限制分析前 10 个脚本以避免耗时过长；用 httpx 拉取脚本源码后交给
        :class:`JSAnalyzer` 逐段分析，按置信度与目标参数命中率选最优结果。
        """
        import httpx

        fragments: list[JSFragment] = []
        for url in scripts[:10]:
            try:
                resp = httpx.get(
                    url,
                    timeout=15.0,
                    follow_redirects=True,
                    headers={"User-Agent": _DEFAULT_UA},
                )
                if resp.status_code == 200 and resp.text:
                    text = resp.text
                    fragments.append(JSFragment(
                        source=text,
                        url=url,
                        size=len(text),
                        is_minified=len(text.splitlines()) < 5,
                    ))
            except Exception:
                continue

        if not fragments:
            return None

        target = target_params[0] if target_params else ""
        best_result: AnalysisResult | None = None
        best_score = 0.0
        for frag in fragments:
            try:
                result = self.analyzer.analyze_fragment(frag)
            except Exception:
                continue
            score = result.confidence
            if target and any(target in inp for inp in result.inputs):
                score += 0.5
            if score > best_score:
                best_score = score
                best_result = result

        return best_result

    # ------------------------------------------------------------------
    # 参数提取
    # ------------------------------------------------------------------

    def _try_extract_param(self, page: Any, param_name: str) -> str | None:
        """尝试从 Hook 数据中提取目标参数。"""
        records = self._read_hook_records(page)
        return self._search_param_in_records(records, param_name)

    async def _try_extract_param_async(self, page: Any, param_name: str) -> str | None:
        """异步尝试从 Hook 数据中提取目标参数。"""
        records = await self._read_hook_records_async(page)
        return self._search_param_in_records(records, param_name)

    @staticmethod
    def _search_param_in_records(records: list[dict], param_name: str) -> str | None:
        """在 hook 记录中搜索目标参数，返回首个命中的值。

        依次在 headers / url query / body（JSON 或 form）中做大小写不敏感匹配。
        """
        if not records:
            return None
        target_lower = param_name.lower()
        for rec in records:
            # 1. headers 中匹配键名
            headers = rec.get("headers") or {}
            if isinstance(headers, dict):
                for k, v in headers.items():
                    if target_lower in k.lower():
                        return str(v)
            # 2. url query 中匹配参数名
            url = rec.get("url") or ""
            if target_lower in url.lower():
                qs = parse_qs(urlparse(url).query)
                for k, v in qs.items():
                    if target_lower in k.lower():
                        return v[0] if v else None
            # 3. body 中匹配（先 JSON 后 form）
            body = rec.get("body")
            if isinstance(body, str) and target_lower in body.lower():
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        for k, v in parsed.items():
                            if target_lower in k.lower():
                                return str(v)
                except json.JSONDecodeError:
                    pass
                form = parse_qs(body)
                for k, v in form.items():
                    if target_lower in k.lower():
                        return v[0] if v else None
        return None

    # ------------------------------------------------------------------
    # 页面创建与恢复
    # ------------------------------------------------------------------

    def _create_page(self, hook_names: list[str] | None) -> tuple[Any, Any]:
        """创建带 Hook 注入的 (context, page)。"""
        assert self.fetcher is not None
        browser = self.fetcher._ensure_browser()
        context = browser.new_context(
            extra_http_headers=self.fetcher.extra_headers or None,
            ignore_https_errors=not self.fetcher.verify,
        )
        # 导航前注入 Hook，确保页面加载时即生效
        combined = generate_combined_script(hook_names or self.config.hooks)
        try:
            context.add_init_script(combined)
        except Exception:
            pass
        page = context.new_page()
        try:
            self.fetcher._setup_page(page)
        except Exception:
            pass
        self._setup_page_listeners(page)
        return context, page

    async def _create_page_async(self, hook_names: list[str] | None) -> tuple[Any, Any]:
        """异步创建带 Hook 注入的 (context, page)。"""
        assert self.fetcher is not None
        browser = await self.fetcher._ensure_async_browser()
        context = await browser.new_context(
            extra_http_headers=self.fetcher.extra_headers or None,
            ignore_https_errors=not self.fetcher.verify,
        )
        combined = generate_combined_script(hook_names or self.config.hooks)
        try:
            await context.add_init_script(combined)
        except Exception:
            pass
        page = await context.new_page()
        try:
            await self.fetcher._setup_page_async(page)
        except Exception:
            pass
        self._setup_page_listeners(page)
        return context, page

    def _setup_page_listeners(self, page: Any) -> None:
        """设置页面事件监听器，收集网络请求到 ``self._network_log``。"""
        self._network_log.clear()

        def on_request(req: Any) -> None:
            try:
                self._network_log.append({
                    "url": req.url,
                    "method": req.method,
                    "resource_type": req.resource_type,
                    "headers": dict(req.headers),
                    "post_data": req.post_data,
                })
            except Exception:
                pass

        try:
            page.on("request", on_request)
        except Exception:
            pass

    def _try_recover_page(self, url: str) -> bool:
        """浏览器崩溃后尝试重新创建 page 并导航到 url。"""
        try:
            self._cleanup_page_sync()
            context, page = self._create_page(self.config.hooks)
            self._context = context
            self._page = page
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(self.config.wait_after_navigate)
            return True
        except Exception:
            return False

    async def _try_recover_page_async(self, url: str) -> bool:
        """异步浏览器崩溃恢复。"""
        try:
            await self._cleanup_page_async()
            context, page = await self._create_page_async(self.config.hooks)
            self._context = context
            self._page = page
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(self.config.wait_after_navigate)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Hook 数据读取
    # ------------------------------------------------------------------

    def _read_hook_data(self, page: Any) -> dict:
        """非破坏性读取 Hook 数据（不清除浏览器侧数组）。"""
        try:
            records = page.evaluate("() => (window.__hook_data__ || []).slice()") or []
            return {"records": list(records), "count": len(records)}
        except Exception:
            return {"records": [], "count": 0}

    async def _read_hook_data_async(self, page: Any) -> dict:
        """异步非破坏性读取 Hook 数据。"""
        try:
            records = await page.evaluate(
                "() => (window.__hook_data__ || []).slice()"
            ) or []
            return {"records": list(records), "count": len(records)}
        except Exception:
            return {"records": [], "count": 0}

    def _read_hook_records(self, page: Any) -> list[dict]:
        """读取 Hook 记录列表，合并页面实时数据与缓存。"""
        records: list[dict] = []
        try:
            fresh = page.evaluate("() => (window.__hook_data__ || []).slice()") or []
            records.extend(fresh)
        except Exception:
            pass
        cached = self._hook_data_cache.get("records", [])
        records.extend(cached)
        return records

    async def _read_hook_records_async(self, page: Any) -> list[dict]:
        """异步读取 Hook 记录列表。"""
        records: list[dict] = []
        try:
            fresh = await page.evaluate(
                "() => (window.__hook_data__ || []).slice()"
            ) or []
            records.extend(fresh)
        except Exception:
            pass
        cached = self._hook_data_cache.get("records", [])
        records.extend(cached)
        return records

    # ------------------------------------------------------------------
    # Prompt 构建
    # ------------------------------------------------------------------

    def _build_think_prompt(
        self,
        observation: Observation,
        task: str,
        history: list,
    ) -> str:
        """构建喂给 DeepSeek 的思考 prompt。"""
        target_params = (
            ", ".join(self.config.target_params) if self.config.target_params else "(未指定)"
        )
        return _THINK_USER_TEMPLATE.format(
            task=task or "(未指定)",
            url=observation.url,
            page_title=observation.page_title,
            captcha_type=observation.captcha_type.value,
            hook_count=observation.hook_data.get("count", 0),
            network_count=len(observation.network_requests),
            script_count=len(observation.scripts),
            hook_summary=self._format_hook_summary(observation.hook_data),
            network_summary=self._format_network_summary(observation.network_requests),
            script_summary=self._format_script_summary(observation.scripts),
            dom_summary=observation.dom_summary,
            history_summary=self._format_history_summary(history),
            target_params=target_params,
        )

    @staticmethod
    def _format_hook_summary(hook_data: dict) -> str:
        """格式化 Hook 数据摘录。"""
        records = hook_data.get("records", [])
        if not records:
            return "(无)"
        lines: list[str] = []
        for rec in records[-20:]:
            rtype = rec.get("type", "?")
            method = rec.get("method", "")
            url = rec.get("url", "")
            headers = rec.get("headers") or {}
            body = rec.get("body")
            line = f"[{rtype}] {method} {url}"
            if isinstance(headers, dict) and headers:
                key_str = ", ".join(f"{k}={v}" for k, v in list(headers.items())[:5])
                line += f" | headers: {key_str}"
            if body:
                line += f" | body: {str(body)[:200]}"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _format_network_summary(network_requests: list[dict]) -> str:
        """格式化网络请求摘录。"""
        if not network_requests:
            return "(无)"
        lines: list[str] = []
        for req in network_requests[-20:]:
            method = req.get("method", "?")
            url = req.get("url", "?")
            rtype = req.get("resource_type", "?")
            lines.append(f"[{rtype}] {method} {url}")
        return "\n".join(lines)

    @staticmethod
    def _format_script_summary(scripts: list[str]) -> str:
        """格式化脚本列表。"""
        if not scripts:
            return "(无)"
        return "\n".join(scripts[:20])

    @staticmethod
    def _format_history_summary(history: list) -> str:
        """格式化历史动作摘要。"""
        if not history:
            return "(无)"
        lines: list[str] = []
        for entry in history[-10:]:
            step = entry.get("step", "?")
            atype = entry.get("action", entry.get("event", "?"))
            reasoning = entry.get("reasoning", entry.get("error", ""))
            line = f"step {step}: {atype}"
            if reasoning:
                line += f" - {reasoning[:150]}"
            lines.append(line)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 辅助工具
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_page_url(page: Any) -> str:
        try:
            return page.url
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 资源清理
    # ------------------------------------------------------------------

    def _cleanup_page_sync(self) -> None:
        """关闭当前 page 与 context（同步）。"""
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None

    async def _cleanup_page_async(self) -> None:
        """关闭当前 page 与 context（异步）。"""
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

    def _cleanup_sync(self) -> None:
        """同步清理全部资源。"""
        self._cleanup_page_sync()
        if self.fetcher is not None:
            try:
                self.fetcher.close()
            except Exception:
                pass
            self.fetcher = None

    async def _cleanup_async(self) -> None:
        """异步清理全部资源。"""
        await self._cleanup_page_async()
        if self.fetcher is not None:
            try:
                await self.fetcher.aclose()
            except Exception:
                pass
            self.fetcher = None

    def close(self) -> None:
        """清理资源。"""
        self._cleanup_sync()

    async def aclose(self) -> None:
        """异步清理资源。"""
        await self._cleanup_async()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


__all__ = [
    "Action",
    "Observation",
    "ReverseAgent",
    "ReverseAgentConfig",
]

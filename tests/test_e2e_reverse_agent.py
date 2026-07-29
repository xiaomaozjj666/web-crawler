"""端到端真实测试：启动 CamoufoxFetcher + ReverseAgent 跑通完整 Agent 循环。

与单元测试（全 mock）不同，此测试真实启动浏览器，访问本地测试服务器，
验证 Hook 注入、网络请求捕获、参数提取的完整链路。

测试策略
--------
- 本地 ``http.server`` 启动测试服务器（端口 8710，占用时回退临时端口）；
- 测试页面包含 ``window.__sign = function(x) { return btoa(x); }`` 加密逻辑，
  页面加载时发出携带 sign 参数的 fetch 请求；
- 使用 StubProvider 返回预设动作序列（inject_hook → extract → done），
  不依赖真实 DeepSeek API key；
- Camoufox 未安装时自动跳过（``@pytest.mark.skipif``）。
"""

from __future__ import annotations

import asyncio
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from web_crawler.ai.llm import LLMResponse, ProviderCapabilities
from web_crawler.compat import HAS_CAMOUFOX

# -- 测试页面 HTML -----------------------------------------------------------

_E2E_TEST_HTML = """<!DOCTYPE html>
<html>
<head><title>Sign Test Page</title></head>
<body>
  <h1>加密参数测试页</h1>
  <form id="form">
    <input type="text" id="input" name="q" value="test">
    <button type="button" id="submit">提交</button>
  </form>
  <script>
    // 加密参数生成逻辑（模拟 X-Bogus / Anti-Content 等）
    window.__sign = function(x) { return btoa(x); };
    // 页面加载时发出请求，携带 sign 参数（被 fetch_hook 捕获）
    var sign = window.__sign('test');
    fetch('/api?sign=' + sign).catch(function() {});
  </script>
</body>
</html>"""


# -- 本地测试服务器 ----------------------------------------------------------


class _E2EHandler(BaseHTTPRequestHandler):
    """端到端测试用的 HTTP 请求处理器。"""

    def log_message(self, *args: object) -> None:
        pass  # 静默测试输出

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, _E2E_TEST_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/api"):
            # 接受任何 /api 请求，返回简单 JSON
            self._send(200, b'{"ok": true}', "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def e2e_server_url() -> str:
    """启动端到端测试服务器，返回基地址 URL。优先用端口 8710，占用时回退。"""
    try:
        server: ThreadingHTTPServer = ThreadingHTTPServer(("127.0.0.1", 8710), _E2EHandler)
    except OSError:
        # 端口 8710 被占用时回退到临时端口
        server = ThreadingHTTPServer(("127.0.0.1", 0), _E2EHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join(timeout=5)


# -- StubProvider ------------------------------------------------------------


class StubProvider:
    """返回预设动作序列的桩 provider，用于端到端测试（不调真实 LLM API）。

    每次调用 ``chat`` 弹出一条预设回复（JSON 字符串），回复耗尽后返回 done。
    """

    model = "stub-model"
    capabilities = ProviderCapabilities()

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: int = 0

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self._replies:
            content = self._replies.pop(0)
        else:
            content = (
                '{"action_type": "done", "params": {"success": true}, '
                '"reasoning": "预设回复已耗尽，自动结束"}'
            )
        return LLMResponse(content=content, model=self.model)

    async def achat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        return self.chat(messages, **kwargs)


def _make_stub_provider() -> StubProvider:
    """构造返回 inject_hook → extract → done 序列的 StubProvider。"""
    return StubProvider(
        [
            # 第 1 步：注入 fetch_hook 捕获网络请求
            (
                '{"action_type": "inject_hook", '
                '"params": {"hooks": ["fetch_hook"]}, '
                '"reasoning": "注入 fetch_hook 以捕获页面发出的加密参数请求"}'
            ),
            # 第 2 步：从 hook 数据中提取 sign 参数
            (
                '{"action_type": "extract", '
                '"params": {"param_name": "sign"}, '
                '"reasoning": "从 hook 捕获的请求中提取 sign 加密参数"}'
            ),
            # 第 3 步：任务完成
            (
                '{"action_type": "done", '
                '"params": {"success": true, "summary": "成功定位 __sign 函数并提取 sign 参数"}, '
                '"reasoning": "sign 参数已提取，任务完成"}'
            ),
        ]
    )


# -- 端到端测试用例 ----------------------------------------------------------

# Camoufox 未安装时跳过真实浏览器测试（不影响 StubProvider 单元测试）
_skip_no_camoufox = pytest.mark.skipif(not HAS_CAMOUFOX, reason="camoufox 未安装，跳过端到端测试")


def _is_selector_loop_on_windows() -> bool:
    """检测当前是否为 Windows + SelectorEventLoopPolicy。

    conftest.py 为 curl_cffi 在 Windows 强制 SelectorEventLoopPolicy，而
    Playwright 同步启动浏览器需要 ProactorEventLoop 才能创建子进程；
    两者在同一进程内互斥，检测到即应跳过端到端真实浏览器测试。
    """
    if sys.platform != "win32":
        return False
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return isinstance(
            asyncio.get_event_loop_policy(), asyncio.WindowsSelectorEventLoopPolicy
        )


_skip_selector_loop = pytest.mark.skipif(
    _is_selector_loop_on_windows(),
    reason="Windows + SelectorEventLoop 与 Playwright 子进程启动互斥，跳过端到端测试",
)


@_skip_no_camoufox
@_skip_selector_loop
@pytest.mark.slow
def test_e2e_reverse_agent_full_loop(e2e_server_url: str) -> None:
    """端到端：ReverseAgent 真实启动浏览器，跑通 observe→think→act 完整循环。

    验证点：
    - agent.run() 返回 dict 包含 success=True；
    - hook_data 不为空（fetch_hook 真实捕获到页面请求）；
    - steps >= 3（至少 inject_hook / extract / done 三步）；
    - target_params_found 包含 sign 参数。
    """
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    config = ReverseAgentConfig(
        headless=True,
        hooks=["fetch_hook"],
        target_params=["sign"],
        max_steps=10,
        # 禁用辅助组件以聚焦端到端主流程
        enable_judge=False,
        enable_guard=False,
        enable_recorder=False,
        enable_screenshot=False,
        planner_interval=None,
        # 禁用人类化输入以加速测试
        humanize_input=False,
        wait_after_navigate=1.5,
    )
    provider = _make_stub_provider()
    agent = ReverseAgent(config=config, provider=provider)
    try:
        result = agent.run(e2e_server_url + "/", task="定位页面中的 __sign 加密函数")
    finally:
        agent.close()

    # 验证返回结构
    assert isinstance(result, dict)
    assert result["success"] is True, f"agent 未成功: {result.get('history', [])}"
    # hook_data 应有捕获记录（fetch_hook 真实注入并捕获了页面 fetch 请求）
    hook_data = result.get("hook_data", {})
    assert hook_data.get("count", 0) > 0, "fetch_hook 未捕获到任何请求"
    # 至少 3 步（inject_hook / extract / done）
    assert result["steps"] >= 3, f"步数不足: {result['steps']}"
    # sign 参数应被提取
    target_found = result.get("target_params_found", {})
    assert "sign" in target_found, f"未提取到 sign 参数: {target_found}"


@_skip_no_camoufox
@_skip_selector_loop
@pytest.mark.slow
def test_e2e_reverse_agent_hook_capture(e2e_server_url: str) -> None:
    """端到端：验证 fetch_hook 真实注入并捕获页面发出的请求。

    即使 agent 任务未完全成功，只要 hook 捕获到请求即说明 Hook 注入链路正常。
    """
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    config = ReverseAgentConfig(
        headless=True,
        hooks=["fetch_hook"],
        target_params=["sign"],
        max_steps=5,
        enable_judge=False,
        enable_guard=False,
        enable_recorder=False,
        enable_screenshot=False,
        planner_interval=None,
        humanize_input=False,
        wait_after_navigate=1.5,
    )
    # 只做 inject_hook 然后 done
    provider = StubProvider(
        [
            (
                '{"action_type": "inject_hook", '
                '"params": {"hooks": ["fetch_hook"]}, '
                '"reasoning": "注入 hook"}'
            ),
            (
                '{"action_type": "done", '
                '"params": {"success": true, "summary": "hook 已注入"}, '
                '"reasoning": "完成"}'
            ),
        ]
    )
    agent = ReverseAgent(config=config, provider=provider)
    try:
        result = agent.run(e2e_server_url + "/", task="验证 hook 注入")
    finally:
        agent.close()

    # fetch_hook 应在页面加载时（add_init_script 注入）就捕获到 fetch 请求
    hook_data = result.get("hook_data", {})
    assert hook_data.get("count", 0) > 0, "hook 未捕获到页面 fetch 请求"
    # 验证捕获的记录中有 /api 请求
    records = hook_data.get("records", [])
    api_found = any("/api" in str(r.get("url", "")) for r in records)
    assert api_found, f"未在 hook 记录中找到 /api 请求: {records}"


def test_e2e_stub_provider_replays_actions() -> None:
    """单元级验证：StubProvider 按顺序弹出预设回复。"""
    provider = _make_stub_provider()
    # 第 1 次调用应返回 inject_hook
    resp1 = provider.chat([])
    assert "inject_hook" in resp1.content
    # 第 2 次调用应返回 extract
    resp2 = provider.chat([])
    assert "extract" in resp2.content
    # 第 3 次调用应返回 done
    resp3 = provider.chat([])
    assert "done" in resp3.content
    # 第 4 次调用（回复耗尽）应返回默认 done
    resp4 = provider.chat([])
    assert "done" in resp4.content
    assert provider.calls == 4

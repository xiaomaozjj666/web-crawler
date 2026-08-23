"""AIScrapeAgent / RobotsPolicy / ScrapeResult 单元测试。

覆盖 :mod:`web_crawler.ai.agent` 模块中未被 ``test_agent_block.py`` 触达的分支：
- :class:`RobotsPolicy` — 缓存命中、fetch 异常回退、allowed 判定；
- :meth:`AIScrapeAgent._ensure_fetcher` — 注入/懒创建 ``Fetcher`` / ``DynamicFetcher``；
- :meth:`AIScrapeAgent._do_fetch` — ``fetch`` 入口兜底；
- :meth:`AIScrapeAgent._fetch_text` — 取 ``resp.text``；
- :meth:`AIScrapeAgent._throttle` — sleep 与不 sleep 分支；
- :meth:`AIScrapeAgent._retry_after` — Retry-After 头解析与指数退避；
- :meth:`AIScrapeAgent.fetch` — robots 禁止、429/503 重试、达到 max_retries；
- :meth:`AIScrapeAgent.close` / ``__enter__`` / ``__exit__`` — 资源清理；
- :class:`ScrapeResult.ok` — 各种判定分支。
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from web_crawler.ai.agent import AIScrapeAgent, RobotsPolicy, ScrapeResult, _http_get_text
from web_crawler.response import Response

# ---------------------------------------------------------------------------
# 辅助桩对象
# ---------------------------------------------------------------------------


class _DummyExtractor:
    """不应被调用的 extractor 桩（fetch/close 测试不需要 extract）。"""

    def extract(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("extract should not be called in fetch/close tests")


class _FakeFetcher:
    """支持 close() / get() 的桩 fetcher，记录调用。"""

    def __init__(
        self,
        resp: Response | None = None,
    ) -> None:
        self._resp = resp
        self.closed = False
        self.fetched: list[str] = []
        self.get_calls: list[str] = []

    def get(self, url: str) -> Response:
        self.get_calls.append(url)
        self.fetched.append(url)
        assert self._resp is not None
        return self._resp

    def close(self) -> None:
        self.closed = True


def _make_response(
    status: int = 200,
    body: bytes = b"<html></html>",
    headers: dict[str, str] | None = None,
    url: str = "https://example.com",
) -> Response:
    return Response(url, status, body, headers or {})


# ---------------------------------------------------------------------------
# RobotsPolicy 测试（覆盖 _parser_for 缓存与异常回退、allowed 判定）
# ---------------------------------------------------------------------------


def test_robots_policy_parser_cache_hit() -> None:
    """同 host 第二次访问命中缓存，不再调用 fetch_text。"""
    policy = RobotsPolicy()
    calls: list[str] = []

    def fetch_text(url: str) -> str:
        calls.append(url)
        return "User-agent: *\nDisallow: /private"

    rp1 = policy._parser_for("https://example.com/page", fetch_text)
    rp2 = policy._parser_for("https://example.com/other", fetch_text)
    # 第二次命中缓存
    assert rp1 is rp2
    assert len(calls) == 1


def test_robots_policy_parser_fetch_error_falls_back() -> None:
    """fetch_text 抛错时回退到空 robots，allowed 仍返回 True。"""
    policy = RobotsPolicy()

    def fetch_text(url: str) -> str:
        raise ConnectionError("boom")

    rp = policy._parser_for("https://example.com/x", fetch_text)
    assert rp is not None
    # 空 robots 允许所有
    assert policy.allowed("https://example.com/x", fetch_text) is True


def test_robots_policy_allowed_disallow() -> None:
    """robots.txt 禁止的 URL 返回 False。"""
    policy = RobotsPolicy()

    def fetch_text(url: str) -> str:
        return "User-agent: *\nDisallow: /private"

    assert policy.allowed("https://example.com/private/secret", fetch_text) is False
    assert policy.allowed("https://example.com/public", fetch_text) is True


def test_robots_policy_allowed_with_user_agent() -> None:
    """自定义 user-agent 的判断。"""
    policy = RobotsPolicy(user_agent="mybot")

    def fetch_text(url: str) -> str:
        return "User-agent: mybot\nDisallow: /blocked"

    assert policy.allowed("https://example.com/blocked", fetch_text) is False
    assert policy.allowed("https://example.com/ok", fetch_text) is True


def test_robots_policy_different_hosts_cached_separately() -> None:
    """不同 host 的 robots 各自缓存。"""
    policy = RobotsPolicy()
    calls: list[str] = []

    def fetch_text(url: str) -> str:
        calls.append(url)
        return "User-agent: *\nAllow: /"

    policy._parser_for("https://a.example/x", fetch_text)
    policy._parser_for("https://b.example/y", fetch_text)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# fetch_robots_text 测试（标准库默认拉取函数，mock urlopen，无真实网络）
# ---------------------------------------------------------------------------


class _FakeUrlopenResp:
    """模拟 urllib.request.urlopen 返回的上下文管理器响应。"""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> _FakeUrlopenResp:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def test_fetch_robots_text_reads_and_decodes() -> None:
    """fetch_robots_text 用 urlopen 拉取 robots.txt 并按 utf-8 解码返回。"""
    from web_crawler.robots import fetch_robots_text

    body = "User-agent: *\nDisallow: /private\n"
    with patch(
        "web_crawler.robots.urllib.request.urlopen",
        return_value=_FakeUrlopenResp(body.encode("utf-8")),
    ) as mock_urlopen:
        text = fetch_robots_text("https://example.com/robots.txt", timeout=5.0)
    assert text == body
    mock_urlopen.assert_called_once_with("https://example.com/robots.txt", timeout=5.0)


def test_fetch_robots_text_decodes_invalid_bytes_with_replace() -> None:
    """非法 utf-8 字节按 errors=replace 解码，不抛错。"""
    from web_crawler.robots import fetch_robots_text

    with patch(
        "web_crawler.robots.urllib.request.urlopen",
        return_value=_FakeUrlopenResp(b"\xff\xfeUser-agent: *"),
    ):
        text = fetch_robots_text("https://example.com/robots.txt")
    assert "\ufffd" in text
    assert "User-agent: *" in text


# ---------------------------------------------------------------------------
# _ensure_fetcher 测试（覆盖 render=True/False 的懒创建）
# ---------------------------------------------------------------------------


def test_ensure_fetcher_returns_injected_fetcher() -> None:
    """已注入 fetcher 时直接返回，不创建新 fetcher。"""
    fetcher = _FakeFetcher()
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=_DummyExtractor(),
        respect_robots=False,
    )
    assert agent._ensure_fetcher() is fetcher


def test_ensure_fetcher_creates_fetcher_when_no_render() -> None:
    """render=False 时懒创建 Fetcher 并缓存。"""
    agent = AIScrapeAgent(extractor=_DummyExtractor(), render=False)
    fake_fetcher = MagicMock()
    with patch("web_crawler.fetchers.fetcher.Fetcher", return_value=fake_fetcher) as mock_cls:
        result = agent._ensure_fetcher()
        assert result is fake_fetcher
        mock_cls.assert_called_once()
    # 缓存：第二次不再创建
    assert agent._ensure_fetcher() is fake_fetcher


def test_ensure_fetcher_creates_dynamic_fetcher_when_render() -> None:
    """render=True 时懒创建 DynamicFetcher。"""
    agent = AIScrapeAgent(extractor=_DummyExtractor(), render=True)
    fake_fetcher = MagicMock()
    with patch(
        "web_crawler.fetchers.dynamic.DynamicFetcher", return_value=fake_fetcher
    ) as mock_cls:
        result = agent._ensure_fetcher()
        assert result is fake_fetcher
        mock_cls.assert_called_once()


# ---------------------------------------------------------------------------
# _do_fetch / _fetch_text 测试
# ---------------------------------------------------------------------------


def test_do_fetch_prefers_get_method() -> None:
    """fetcher 有 get 方法时优先调用 get。"""
    fetcher = MagicMock()
    fetcher.get.return_value = "got"
    assert AIScrapeAgent._do_fetch(fetcher, "https://x") == "got"
    fetcher.get.assert_called_once_with("https://x")


def test_do_fetch_calls_get_directly() -> None:
    """动词统一后 _do_fetch 直接调用 fetcher.get（DynamicFetcher 亦提供 get 别名）。"""
    fetcher = MagicMock(spec=["get"])
    fetcher.get.return_value = "got"
    assert AIScrapeAgent._do_fetch(fetcher, "https://x") == "got"
    fetcher.get.assert_called_once_with("https://x")


def test_fetch_text_returns_response_text() -> None:
    """_fetch_text 取 resp.text。"""
    resp = _make_response(body=b"hello")
    fetcher = _FakeFetcher(resp)
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=_DummyExtractor(),
        respect_robots=False,
    )
    assert agent._fetch_text("https://x") == "hello"
    assert "https://x" in fetcher.fetched


def test_fetch_text_uses_get_method() -> None:
    """_fetch_text 通过统一的 get 入口取 resp.text。"""
    resp = _make_response(body=b"world")
    fetcher_mock = MagicMock(spec=["get"])
    fetcher_mock.get.return_value = resp
    agent = AIScrapeAgent(
        fetcher=fetcher_mock,
        extractor=_DummyExtractor(),
        respect_robots=False,
    )
    assert agent._fetch_text("https://x") == "world"
    fetcher_mock.get.assert_called_once_with("https://x")


# ---------------------------------------------------------------------------
# _throttle 测试
# ---------------------------------------------------------------------------


def test_throttle_sleeps_when_within_min_delay() -> None:
    """min_delay 未过时调用 time.sleep。"""
    agent = AIScrapeAgent(
        fetcher=_FakeFetcher(),
        extractor=_DummyExtractor(),
        respect_robots=False,
        min_delay=0.5,
    )
    # _last_request_ts 设为最近 → 需要等待
    agent._last_request_ts = time.monotonic()
    with patch("web_crawler.ai.agent.time.sleep") as mock_sleep:
        agent._throttle()
        mock_sleep.assert_called_once()


def test_throttle_no_sleep_when_elapsed_exceeds_delay() -> None:
    """已超过 min_delay 时不 sleep。"""
    agent = AIScrapeAgent(
        fetcher=_FakeFetcher(),
        extractor=_DummyExtractor(),
        respect_robots=False,
        min_delay=0.01,
    )
    # _last_request_ts 很久以前 → 不需要等待
    agent._last_request_ts = time.monotonic() - 10
    with patch("web_crawler.ai.agent.time.sleep") as mock_sleep:
        agent._throttle()
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# _retry_after 测试
# ---------------------------------------------------------------------------


def test_retry_after_uses_retry_after_header() -> None:
    """Retry-After 头（大写）被解析为秒数。"""
    resp = _make_response(headers={"Retry-After": "1.5"})
    assert AIScrapeAgent._retry_after(resp, 0) == 1.5


def test_retry_after_uses_lowercase_header() -> None:
    """retry-after 头（小写）也能解析。"""
    resp = _make_response(headers={"retry-after": "2.0"})
    assert AIScrapeAgent._retry_after(resp, 1) == 2.0


def test_retry_after_invalid_header_falls_back_to_exponential() -> None:
    """非法 Retry-After 值回退到 2**attempt。"""
    resp = _make_response(headers={"Retry-After": "not-a-number"})
    assert AIScrapeAgent._retry_after(resp, 2) == 4.0


def test_retry_after_no_header_uses_exponential_backoff() -> None:
    """无 Retry-After 头时使用指数退避。"""
    resp = _make_response()
    assert AIScrapeAgent._retry_after(resp, 0) == 1.0
    assert AIScrapeAgent._retry_after(resp, 1) == 2.0
    assert AIScrapeAgent._retry_after(resp, 3) == 8.0


def test_retry_after_negative_header_clamped_to_zero() -> None:
    """负值 Retry-After 被 clamp 到 0.0。"""
    resp = _make_response(headers={"Retry-After": "-1.0"})
    assert AIScrapeAgent._retry_after(resp, 0) == 0.0


# ---------------------------------------------------------------------------
# fetch 测试（覆盖 robots 禁止、429/503 重试、达到 max_retries）
# ---------------------------------------------------------------------------


def test_fetch_raises_permission_error_on_robots_disallow() -> None:
    """robots.txt 禁止时抛 PermissionError。"""
    fetcher = _FakeFetcher(_make_response())
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=_DummyExtractor(),
        respect_robots=True,
    )
    with (
        patch.object(agent.robots, "allowed", return_value=False),
        pytest.raises(PermissionError, match="robots.txt disallows"),
    ):
        agent.fetch("https://example.com/page")


def test_fetch_retries_on_429_then_succeeds() -> None:
    """429 触发重试，最终拿到 200。"""
    fail_resp = _make_response(status=429, headers={"Retry-After": "0"})
    ok_resp = _make_response(status=200, body=b"ok")
    fetcher = MagicMock()
    fetcher.get.side_effect = [fail_resp, fail_resp, ok_resp]
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=_DummyExtractor(),
        respect_robots=False,
        max_retries=3,
        min_delay=0.0,
    )
    with patch("web_crawler.ai.agent.time.sleep") as mock_sleep:
        resp = agent.fetch("https://example.com")
        assert resp.status == 200
        assert mock_sleep.call_count == 2  # 两次重试都 sleep


def test_fetch_retries_on_503_then_succeeds() -> None:
    """503 触发重试。"""
    fail_resp = _make_response(status=503)
    ok_resp = _make_response(status=200)
    fetcher = MagicMock()
    fetcher.get.side_effect = [fail_resp, ok_resp]
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=_DummyExtractor(),
        respect_robots=False,
        max_retries=2,
        min_delay=0.0,
    )
    with patch("web_crawler.ai.agent.time.sleep"):
        resp = agent.fetch("https://example.com")
        assert resp.status == 200


def test_fetch_returns_429_after_exhausting_retries() -> None:
    """超过 max_retries 后返回 429 响应（不再重试）。"""
    fail_resp = _make_response(status=429)
    fetcher = MagicMock()
    fetcher.get.return_value = fail_resp
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=_DummyExtractor(),
        respect_robots=False,
        max_retries=2,
        min_delay=0.0,
    )
    with patch("web_crawler.ai.agent.time.sleep"):
        resp = agent.fetch("https://example.com")
        assert resp.status == 429
        # 初始 1 次 + max_retries 2 次 = 3 次
        assert fetcher.get.call_count == 3


def test_fetch_no_retry_on_200() -> None:
    """200 状态码不触发重试。"""
    ok_resp = _make_response(status=200)
    fetcher = MagicMock()
    fetcher.get.return_value = ok_resp
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=_DummyExtractor(),
        respect_robots=False,
        max_retries=3,
        min_delay=0.0,
    )
    resp = agent.fetch("https://example.com")
    assert resp.status == 200
    assert fetcher.get.call_count == 1


def test_fetch_skips_robots_when_disabled() -> None:
    """respect_robots=False 时不检查 robots.txt。"""
    resp = _make_response()
    fetcher = _FakeFetcher(resp)
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=_DummyExtractor(),
        respect_robots=False,
        min_delay=0.0,
    )
    out = agent.fetch("https://example.com")
    assert out.status == 200


def test_fetch_no_retry_on_other_error_status() -> None:
    """非 429/503 的错误状态码不触发重试（直接返回）。"""
    err_resp = _make_response(status=500)
    fetcher = MagicMock()
    fetcher.get.return_value = err_resp
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=_DummyExtractor(),
        respect_robots=False,
        max_retries=3,
        min_delay=0.0,
    )
    resp = agent.fetch("https://example.com")
    assert resp.status == 500
    assert fetcher.get.call_count == 1


# ---------------------------------------------------------------------------
# close / context manager 测试
# ---------------------------------------------------------------------------


def test_close_closes_closeable_fetcher() -> None:
    """close() 调用 fetcher.close()。"""
    fetcher = _FakeFetcher()
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=_DummyExtractor(),
    )
    agent.close()
    assert fetcher.closed is True


def test_close_skips_when_fetcher_has_no_close() -> None:
    """fetcher 无 close 方法时不抛错。"""
    fetcher = MagicMock(spec=["get"])  # 没有 close
    agent = AIScrapeAgent(
        fetcher=fetcher,
        extractor=_DummyExtractor(),
    )
    agent.close()  # 不抛错


def test_close_skips_when_no_fetcher() -> None:
    """未注入 fetcher 时 close 不抛错。"""
    agent = AIScrapeAgent(extractor=_DummyExtractor())
    agent.close()  # _fetcher is None


def test_context_manager_closes_on_normal_exit() -> None:
    """with 块正常结束时 close 被调用。"""
    fetcher = _FakeFetcher()
    with AIScrapeAgent(
        fetcher=fetcher,
        extractor=_DummyExtractor(),
    ) as agent:
        assert agent is not None
    assert fetcher.closed is True


def test_context_manager_closes_on_exception() -> None:
    """with 块抛异常时 close 仍被调用。"""
    fetcher = _FakeFetcher()
    with (
        pytest.raises(RuntimeError, match="boom"),
        AIScrapeAgent(
            fetcher=fetcher,
            extractor=_DummyExtractor(),
        ),
    ):
        raise RuntimeError("boom")
    assert fetcher.closed is True


# ---------------------------------------------------------------------------
# ScrapeResult.ok 测试（覆盖各种判定分支）
# ---------------------------------------------------------------------------


def test_scrape_result_ok_when_status_ok_and_no_missing() -> None:
    """状态 2xx/3xx + 无 missing + needs_human=False → ok=True。"""
    resp = _make_response(status=200)
    result = ScrapeResult(
        url="https://x",
        status=200,
        data={"title": "ok"},
        selectors={"title": "h1"},
        missing=[],
        response=resp,
    )
    assert result.ok is True


def test_scrape_result_not_ok_when_missing_present() -> None:
    """有 missing 字段 → ok=False。"""
    resp = _make_response(status=200)
    result = ScrapeResult(
        url="https://x",
        status=200,
        data={},
        selectors={},
        missing=["title"],
        response=resp,
    )
    assert result.ok is False


def test_scrape_result_not_ok_when_needs_human() -> None:
    """needs_human=True → ok=False。"""
    resp = _make_response(status=200)
    result = ScrapeResult(
        url="https://x",
        status=200,
        data={"title": "ok"},
        selectors={},
        missing=[],
        response=resp,
        needs_human=True,
    )
    assert result.ok is False


def test_scrape_result_not_ok_when_status_error() -> None:
    """状态码 5xx → ok=False。"""
    resp = _make_response(status=500)
    result = ScrapeResult(
        url="https://x",
        status=500,
        data={},
        selectors={},
        missing=[],
        response=resp,
    )
    assert result.ok is False


def test_scrape_result_ok_default_needs_human_false() -> None:
    """needs_human 默认为 False。"""
    resp = _make_response(status=200)
    result = ScrapeResult(
        url="https://x",
        status=200,
        data={"x": "y"},
        selectors={},
        missing=[],
        response=resp,
    )
    assert result.needs_human is False
    assert result.block_reason is None


def test_scrape_result_block_reason_set() -> None:
    """block_reason 字段可被设置。"""
    resp = _make_response(status=403)
    result = ScrapeResult(
        url="https://x",
        status=403,
        data={},
        selectors={},
        missing=["t"],
        response=resp,
        needs_human=True,
        block_reason="http 403",
    )
    assert result.ok is False
    assert result.block_reason == "http 403"


# ---------------------------------------------------------------------------
# detect_block 边界补充（覆盖 _BLOCK_STATUS 与 marker 各分支）
# ---------------------------------------------------------------------------


def test_detect_block_returns_none_on_empty_body() -> None:
    """空 body 不会被误判为反爬。"""
    from web_crawler.ai.agent import detect_block

    resp = _make_response(status=200, body=b"")
    assert detect_block(resp) is None


def test_detect_block_on_401_status() -> None:
    """401 状态码触发反爬检测。"""
    from web_crawler.ai.agent import detect_block

    resp = _make_response(status=401, body=b"unauthorized")
    assert detect_block(resp) == "http 401"


def test_detect_block_on_chinese_marker() -> None:
    """中文反爬标记命中。"""
    from web_crawler.ai.agent import detect_block

    resp = _make_response(status=200, body="<html>请完成验证</html>".encode())
    reason = detect_block(resp)
    assert reason is not None
    assert "请完成验证" in reason


# ===========================================================================
# 回归：robots.txt 轻量拉取（不经重型 fetcher）+ 纳入限速
# ===========================================================================


def test_fetch_robots_uses_lightweight_http_and_throttles() -> None:
    """robots.txt 拉取走轻量 HTTP 且经过 _throttle，不触碰重型 fetcher。"""
    heavy = MagicMock()
    heavy.get.side_effect = AssertionError("robots 拉取不应走重型 fetcher")
    agent = AIScrapeAgent(
        fetcher=heavy,
        extractor=_DummyExtractor(),
        respect_robots=True,
        min_delay=5.0,
    )
    # 置为"刚请求过"，让 _throttle 触发 sleep
    agent._last_request_ts = time.monotonic()
    with (
        patch(
            "web_crawler.ai.agent._http_get_text",
            return_value="User-agent: *\nDisallow: /private",
        ) as mock_get,
        patch("web_crawler.ai.agent.time.sleep") as mock_sleep,
        pytest.raises(PermissionError, match="robots.txt disallows"),
    ):
        agent.fetch("https://example.com/private/x")
    mock_get.assert_called_once()
    heavy.get.assert_not_called()
    # robots 拉取前有一次 _throttle（限速生效）
    assert mock_sleep.call_count >= 1


def test_fetch_robots_http_failure_allows() -> None:
    """robots.txt 拉取失败（网络错误）时回退为允许访问。"""
    heavy = MagicMock()
    heavy.get.return_value = _make_response(status=200, body=b"<html>ok</html>")
    agent = AIScrapeAgent(
        fetcher=heavy,
        extractor=_DummyExtractor(),
        respect_robots=True,
        min_delay=0.0,
    )
    with (
        patch("web_crawler.ai.agent._http_get_text", return_value=""),
        patch("web_crawler.ai.agent.time.sleep"),
    ):
        resp = agent.fetch("https://example.com/page")
    assert resp.status == 200


# ===========================================================================
# 回归：Retry-After / 指数退避上限
# ===========================================================================


def test_retry_after_header_capped() -> None:
    """超大 Retry-After 头应被钳制到 _MAX_RETRY_AFTER。"""
    resp = _make_response(headers={"Retry-After": "999999"})
    assert AIScrapeAgent._retry_after(resp, 0) == 300.0


def test_retry_after_exponential_backoff_capped() -> None:
    """指数退避兜底也应设上限。"""
    resp = _make_response()
    assert AIScrapeAgent._retry_after(resp, 10) == 60.0


# ===========================================================================
# _http_get_text：robots.txt 轻量拉取（stdlib only）
# ===========================================================================


def test_http_get_text_success() -> None:
    """轻量 GET 成功时返回解码后的文本。"""
    fake_resp = MagicMock()
    fake_resp.read.return_value = b"User-agent: *\nDisallow: /private\n"
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=fake_resp):
        text = _http_get_text("https://example.com/robots.txt")
    assert text == "User-agent: *\nDisallow: /private\n"


def test_http_get_text_failure_returns_empty() -> None:
    """轻量 GET 抛异常（网络错误/超时）→ 回退为空串。"""
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        text = _http_get_text("https://example.com/robots.txt")
    assert text == ""

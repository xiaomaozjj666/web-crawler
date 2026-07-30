"""VulnScanner 的补充单元测试：覆盖 httpx 路径、close/上下文管理、LLM 分析、
归一化与 JSON 解析的边角分支。所有 HTTP 调用均被 mock，不发起真实请求。
"""

from __future__ import annotations

import json
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from web_crawler.pentest.vuln_scanner import (
    VulnFinding,
    VulnScanner,
    _inject,
    _normalize,
    _parse_json,
)

# ---------------------------------------------------------------------------
# 测试用假对象
# ---------------------------------------------------------------------------


class _FakeHttpxResponse:
    """模拟 httpx.Response：支持 status_code/text/content/headers.getlist。"""

    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content = content if content is not None else text.encode("utf-8", errors="replace")
        self.headers = headers or {}

    def getlist(self, key: str) -> list[str]:
        return [self.headers[key]] if key in self.headers else []


class _FakeHttpxClient:
    """模拟 httpx.Client 的 get 接口；记录调用与异常。"""

    def __init__(
        self,
        response: _FakeHttpxResponse | None = None,
        responses: Any | None = None,
        *,
        raise_exc: Exception | None = None,
    ) -> None:
        self._response = response
        self._responses = responses
        self._raise_exc = raise_exc
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
        timeout: float = 10.0,
    ) -> _FakeHttpxResponse:
        self.calls.append((url, {"headers": headers, "follow_redirects": follow_redirects}))
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._responses is not None:
            return self._responses(url)
        if self._response is None:
            return _FakeHttpxResponse(text="clean page")
        return self._response

    def close(self) -> None:
        self.closed = True


# 让 type(fetcher).__module__ 返回 "httpx"，从而 _is_httpx=True
_FakeHttpxClient.__module__ = "httpx"


def _make_httpx_module(client: _FakeHttpxClient | None = None) -> types.ModuleType:
    """构造伪 httpx 模块，含 Client 类。"""
    mod = types.ModuleType("httpx")

    def _client_factory(*args: Any, **kwargs: Any) -> _FakeHttpxClient:
        if client is None:
            return _FakeHttpxClient()
        return client

    mod.Client = _client_factory  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# _inject / _parse_json / _normalize 单元测试
# ---------------------------------------------------------------------------


def test_inject_replaces_existing_param() -> None:
    """_inject 应覆盖已存在的同名查询参数。"""
    url = "https://example.com/path?q=orig&lang=en"
    injected = _inject(url, "q", "' OR '1'='1")
    assert "q=%27+OR+%271%27%3D%271" in injected
    assert "lang=en" in injected


def test_inject_adds_missing_param() -> None:
    """_inject 对不存在的参数应新增。"""
    url = "https://example.com/path"
    injected = _inject(url, "id", "1 UNION SELECT NULL--")
    assert "id=" in injected


def test_parse_json_extracts_embedded_object() -> None:
    """_parse_json 从含额外文本的回复中抽取 JSON。"""
    text = '好的，结果为 {"vulnerable": true, "type": "xss"} 已分析'
    parsed = _parse_json(text)
    assert parsed == {"vulnerable": True, "type": "xss"}


def test_parse_json_invalid_returns_empty() -> None:
    """_parse_json 无法解析时返回空 dict。"""
    # 没有 {...} 结构
    assert _parse_json("plain text no json") == {}
    # 有 {...} 结构但 JSON 非法
    assert _parse_json("{not legal json}") == {}


def test_normalize_with_project_response() -> None:
    """_normalize 兼容项目 Response（status 字段 + 普通 dict headers）。"""

    class _ProjResp:
        status = 200
        text = "hello"
        content = b"hello"
        headers = {"Content-Type": "text/html", "Set-Cookie": "a=1"}

    resp = _normalize(_ProjResp())
    assert resp.status == 200
    assert resp.text == "hello"
    assert resp.content == b"hello"
    # dict headers 走 items() 路径
    assert resp.headers["Content-Type"] == ["text/html"]


def test_normalize_content_str_converted_to_bytes() -> None:
    """content 为 str 时应转为 bytes。"""

    class _StrContentResp:
        status_code = 200
        text = "hi"
        content = "hi"  # 故意给 str
        headers: dict[str, str] = {}

    resp = _normalize(_StrContentResp())
    assert resp.content == b"hi"


def test_normalize_content_other_type_converted_to_bytes() -> None:
    """content 为非 str/bytes 类型时应安全转为 bytes。"""

    class _ListContentResp:
        status_code = 200
        text = ""
        content = [1, 2, 3]  # 列表走 bytes() 兜底
        headers: dict[str, str] = {}

    resp = _normalize(_ListContentResp())
    assert isinstance(resp.content, bytes)


def test_normalize_text_none_treated_as_empty() -> None:
    """text 为 None 时归一为空字符串。"""

    class _NoneTextResp:
        status_code = 200
        text = None  # type: ignore[assignment]
        content = b""
        headers: dict[str, str] = {}

    resp = _normalize(_NoneTextResp())
    assert resp.text == ""


# ---------------------------------------------------------------------------
# VulnScanner — httpx 路径与 close / 上下文管理
# ---------------------------------------------------------------------------


def test_scanner_creates_internal_httpx_client_when_fetcher_none() -> None:
    """fetcher=None 时延迟导入 httpx 并创建 Client。"""
    fake_client = _FakeHttpxClient(response=_FakeHttpxResponse(text="clean"))
    fake_module = _make_httpx_module(fake_client)
    with patch.dict("sys.modules", {"httpx": fake_module}):
        scanner = VulnScanner(fetcher=None)
        # _is_httpx 由 type(fetcher).__module__ 派生；伪 Client 模块名为 httpx
        assert scanner._is_httpx is True
        # 内部创建后 close() 应调用 fetcher.close()
        scanner.close()
        assert fake_client.closed is True


def test_scanner_close_swallows_exception_from_external_fetcher() -> None:
    """close() 内部 try/except 吞掉 fetcher.close() 异常。"""

    class _RaisingHttpxClient(_FakeHttpxClient):
        def close(self) -> None:
            raise RuntimeError("boom")

    fake_client = _RaisingHttpxClient()
    fake_module = _make_httpx_module(fake_client)
    with patch.dict("sys.modules", {"httpx": fake_module}):
        scanner = VulnScanner(fetcher=None)
        # 不应抛出
        scanner.close()


def test_scanner_context_manager_closes_internal_client() -> None:
    """with 语法在退出时自动 close 内部 fetcher。"""
    fake_client = _FakeHttpxClient(response=_FakeHttpxResponse(text="clean"))
    fake_module = _make_httpx_module(fake_client)
    with patch.dict("sys.modules", {"httpx": fake_module}):
        with VulnScanner(fetcher=None) as scanner:
            assert scanner is not None
        # 退出 with 后应已 close
        assert fake_client.closed is True


def test_scanner_close_does_not_close_external_fetcher() -> None:
    """外部传入的 fetcher 不由 scanner 关闭（_fetcher_created=False）。"""
    fetcher = _FakeHttpxClient(response=_FakeHttpxResponse(text="clean"))
    # 让 type(fetcher).__module__ 以 httpx 开头，触发 _is_httpx=True
    fetcher.__class__.__module__ = "httpx._client"
    scanner = VulnScanner(fetcher=fetcher)
    scanner.close()
    # 外部 fetcher 不应被关闭
    assert fetcher.closed is False


# ---------------------------------------------------------------------------
# scan_url / _test_payload — httpx 路径与请求异常
# ---------------------------------------------------------------------------


def test_scan_url_uses_httpx_get_path_with_follow_redirects() -> None:
    """httpx 路径应使用 follow_redirects=False 关键字参数。"""
    fake_client = _FakeHttpxClient(response=_FakeHttpxResponse(text="<html>safe</html>"))
    fake_module = _make_httpx_module(fake_client)
    # 用真实 httpx.Client 类创建一个空壳实例，使 _is_httpx 为 True
    # 实际不调用，因为我们替换 sys.modules['httpx']
    with patch.dict("sys.modules", {"httpx": fake_module}):
        scanner = VulnScanner(fetcher=None)
        scanner.scan_url("https://example.com/?q=test")
        # 验证至少一次调用，且 follow_redirects=False
        assert len(fake_client.calls) > 0
        for _, kwargs in fake_client.calls:
            assert kwargs["follow_redirects"] is False


def test_scan_url_returns_empty_when_get_raises() -> None:
    """httpx.get 抛异常时 _get 返回 None，scan_url 收集不到 finding。"""
    fake_client = _FakeHttpxClient(raise_exc=ConnectionError("network down"))
    fake_module = _make_httpx_module(fake_client)
    with patch.dict("sys.modules", {"httpx": fake_module}):
        scanner = VulnScanner(fetcher=None)
        findings = scanner.scan_url("https://example.com/?q=test")
        assert findings == []


def test_scan_url_with_explicit_params_includes_only_those() -> None:
    """传入 params 时仅探测指定参数。"""
    fake_client = _FakeHttpxClient(response=_FakeHttpxResponse(text="clean"))
    fake_module = _make_httpx_module(fake_client)
    with patch.dict("sys.modules", {"httpx": fake_module}):
        scanner = VulnScanner(fetcher=None)
        scanner.scan_url("https://example.com/path", params={"id": ""})
        # 所有调用都应注入 id= 而非 q=
        for url, _ in fake_client.calls:
            assert "id=" in url


def test_scan_url_empty_params_uses_default_q() -> None:
    """params={} 时退化为默认 q 参数。"""
    fake_client = _FakeHttpxClient(response=_FakeHttpxResponse(text="clean"))
    fake_module = _make_httpx_module(fake_client)
    with patch.dict("sys.modules", {"httpx": fake_module}):
        scanner = VulnScanner(fetcher=None)
        scanner.scan_url("https://example.com/", params={})
        # 应有调用且注入 q=
        assert any("q=" in url for url, _ in fake_client.calls)


def test_rule_match_xss_requires_html_chars_in_payload() -> None:
    """单引号 SQL payload 即使回显也不应误判为 XSS。"""
    scanner = VulnScanner(fetcher=_FakeHttpxClient(response=_FakeHttpxResponse(text="'")))
    # _is_httpx=False 因为模块名不是 httpx，走 non-httpx 分支
    # _rule_match 对只含单引号的 payload 不应判 XSS
    finding = scanner._rule_match("q", "'", "reflected: '")
    assert finding is None


def test_rule_match_xss_detected_with_html_chars() -> None:
    """含 <> 的 payload 回显时判定 XSS。"""
    scanner = VulnScanner(fetcher=_FakeHttpxClient())
    payload = "<script>alert(1)</script>"
    finding = scanner._rule_match("q", payload, payload)
    assert finding is not None
    assert finding.type == "xss"
    assert finding.severity == "medium"


def test_rule_match_sql_keyword_case_insensitive() -> None:
    """SQL 错误关键词匹配应大小写不敏感。"""
    scanner = VulnScanner(fetcher=_FakeHttpxClient())
    finding = scanner._rule_match("q", "'", "error: sql SYNTAX issue")
    assert finding is not None
    assert finding.type == "sql_injection"


# ---------------------------------------------------------------------------
# LLM 分析路径
# ---------------------------------------------------------------------------


class _FakeProvider:
    """模拟 LLMProvider.chat，返回预设内容。"""

    def __init__(self, content: str) -> None:
        self._content = content
        self.chat_calls: list[tuple[Any, dict[str, Any]]] = []

    def chat(self, messages: Any, **kwargs: Any) -> Any:
        self.chat_calls.append((messages, kwargs))

        class _R:
            content = self._content

        return _R()


def test_llm_analyze_returns_finding_when_vulnerable() -> None:
    """provider 返回 vulnerable=true 时返回 VulnFinding。"""
    provider = _FakeProvider(
        content=json.dumps(
            {
                "vulnerable": True,
                "type": "xss",
                "evidence": "script tag reflected",
                "severity": "high",
            }
        )
    )
    fetcher = _FakeHttpxClient(response=_FakeHttpxResponse(text="<html>ok</html>"))
    fetcher.__class__.__module__ = "httpx._client"
    scanner = VulnScanner(fetcher=fetcher, provider=provider)
    findings = scanner.scan_url("https://example.com/?q=test")
    # 至少一个 finding 来自 LLM
    assert any(f.type == "xss" and f.severity == "high" for f in findings)
    assert provider.chat_calls  # 调用至少一次


def test_llm_analyze_returns_none_when_not_vulnerable() -> None:
    """provider 返回 vulnerable=false 时无 finding。"""
    provider = _FakeProvider(
        content=json.dumps({"vulnerable": False, "type": "none"})
    )
    fetcher = _FakeHttpxClient(response=_FakeHttpxResponse(text="clean"))
    fetcher.__class__.__module__ = "httpx._client"
    scanner = VulnScanner(fetcher=fetcher, provider=provider)
    findings = scanner.scan_url("https://example.com/?q=test")
    assert findings == []


def test_llm_analyze_returns_none_when_type_none() -> None:
    """vulnerable=true 但 type=none 时仍返回 None。"""
    provider = _FakeProvider(
        content=json.dumps({"vulnerable": True, "type": "none"})
    )
    fetcher = _FakeHttpxClient(response=_FakeHttpxResponse(text="clean"))
    fetcher.__class__.__module__ = "httpx._client"
    scanner = VulnScanner(fetcher=fetcher, provider=provider)
    findings = scanner.scan_url("https://example.com/?q=test")
    assert findings == []


def test_llm_analyze_returns_none_when_type_unknown() -> None:
    """vulnerable=true 但 type 不在白名单时返回 None。"""
    provider = _FakeProvider(
        content=json.dumps({"vulnerable": True, "type": "csrf", "severity": "high"})
    )
    fetcher = _FakeHttpxClient(response=_FakeHttpxResponse(text="clean"))
    fetcher.__class__.__module__ = "httpx._client"
    scanner = VulnScanner(fetcher=fetcher, provider=provider)
    findings = scanner.scan_url("https://example.com/?q=test")
    assert findings == []


def test_llm_analyze_normalizes_invalid_severity_to_medium() -> None:
    """severity 非法时回退为 medium。"""
    provider = _FakeProvider(
        content=json.dumps(
            {
                "vulnerable": True,
                "type": "sql_injection",
                "severity": "BLOCKER",  # 非白名单
                "evidence": "err",
            }
        )
    )
    fetcher = _FakeHttpxClient(response=_FakeHttpxResponse(text="clean"))
    fetcher.__class__.__module__ = "httpx._client"
    scanner = VulnScanner(fetcher=fetcher, provider=provider)
    findings = scanner.scan_url("https://example.com/?q=test")
    assert any(f.severity == "medium" for f in findings)


def test_llm_analyze_provider_raises_returns_none() -> None:
    """provider.chat 抛异常时 _llm_analyze 返回 None。"""

    class _RaisingProvider:
        def chat(self, messages: Any, **kwargs: Any) -> Any:
            raise RuntimeError("llm unavailable")

    fetcher = _FakeHttpxClient(response=_FakeHttpxResponse(text="clean"))
    fetcher.__class__.__module__ = "httpx._client"
    scanner = VulnScanner(fetcher=fetcher, provider=_RaisingProvider())
    findings = scanner.scan_url("https://example.com/?q=test")
    assert findings == []


def test_llm_analyze_default_evidence_when_missing() -> None:
    """evidence 缺失时使用 'llm detected' 兜底。"""
    provider = _FakeProvider(
        content=json.dumps({"vulnerable": True, "type": "xss", "severity": "medium"})
    )
    fetcher = _FakeHttpxClient(response=_FakeHttpxResponse(text="clean"))
    fetcher.__class__.__module__ = "httpx._client"
    scanner = VulnScanner(fetcher=fetcher, provider=provider)
    findings = scanner.scan_url("https://example.com/?q=test")
    assert any(f.evidence == "llm detected" for f in findings)


def test_llm_analyze_invalid_json_returns_none() -> None:
    """provider 返回非 JSON 时 _parse_json 解析为空，无 finding。"""
    provider = _FakeProvider(content="not json at all, no braces")
    fetcher = _FakeHttpxClient(response=_FakeHttpxResponse(text="clean"))
    fetcher.__class__.__module__ = "httpx._client"
    scanner = VulnScanner(fetcher=fetcher, provider=provider)
    findings = scanner.scan_url("https://example.com/?q=test")
    assert findings == []


# ---------------------------------------------------------------------------
# non-httpx fetcher 路径
# ---------------------------------------------------------------------------


def test_scanner_with_non_httpx_fetcher_uses_allow_redirects() -> None:
    """非 httpx fetcher 走 allow_redirects=False 路径。"""
    fetcher = MagicMock()
    fetcher.get.return_value = _FakeHttpxResponse(text="<html>safe</html>")
    # 让 module 名不以 httpx 开头
    fetcher.__class__.__module__ = "web_crawler.fetchers.fetcher"
    scanner = VulnScanner(fetcher=fetcher)
    assert scanner._is_httpx is False
    scanner.scan_url("https://example.com/?q=test")
    # 验证调用 kwargs 包含 allow_redirects
    assert fetcher.get.called
    _, kwargs = fetcher.get.call_args
    assert kwargs.get("allow_redirects") is False


def test_scanner_close_skips_non_httpx_fetcher() -> None:
    """非 httpx fetcher 在 close() 时不调用 close（_is_httpx=False）。"""
    fetcher = MagicMock()
    fetcher.__class__.__module__ = "web_crawler.fetchers.fetcher"
    scanner = VulnScanner(fetcher=fetcher)
    # _fetcher_created=True 但 _is_httpx=False，close 不应调用 fetcher.close
    scanner.close()
    fetcher.close.assert_not_called()


# ---------------------------------------------------------------------------
# VulnFinding.to_dict
# ---------------------------------------------------------------------------


def test_vuln_finding_to_dict_roundtrip() -> None:
    """to_dict 应包含全部字段。"""
    f = VulnFinding(
        type="sql_injection",
        param="id",
        payload="'",
        evidence="SQL syntax",
        severity="high",
    )
    d = f.to_dict()
    assert d == {
        "type": "sql_injection",
        "param": "id",
        "payload": "'",
        "evidence": "SQL syntax",
        "severity": "high",
    }


def test_vuln_finding_to_dict_preserves_path_traversal() -> None:
    f = VulnFinding(
        type="path_traversal",
        param="file",
        payload="../../../etc/passwd",
        evidence="root:x:0:0",
        severity="high",
    )
    d = f.to_dict()
    assert d["type"] == "path_traversal"
    assert d["payload"] == "../../../etc/passwd"


# ---------------------------------------------------------------------------
# 边界：scan_url 唯一性 / 空参数 / 全部 payload 探测
# ---------------------------------------------------------------------------


def test_scan_url_deduplicates_same_param_payload_pairs() -> None:
    """相同 (param, payload) 对仅探测一次（seen 集合生效）。"""
    fake_client = _FakeHttpxClient(response=_FakeHttpxResponse(text="clean"))
    fake_module = _make_httpx_module(fake_client)
    with patch.dict("sys.modules", {"httpx": fake_module}):
        scanner = VulnScanner(fetcher=None)
        # params 含两个参数，但每个参数都会遍历相同 payload 集合
        scanner.scan_url("https://example.com/", params={"q": "", "search": ""})
        urls = [url for url, _ in fake_client.calls]
        # 没有重复 (param, payload) 组合
        seen_combos: set[tuple[str, str]] = set()
        for url in urls:
            # 提取查询参数
            from urllib.parse import parse_qsl, urlsplit

            qs = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
            for k, v in qs.items():
                if k in {"q", "search"}:
                    combo = (k, v)
                    assert combo not in seen_combos, f"dup combo: {combo}"
                    seen_combos.add(combo)


@pytest.mark.parametrize(
    "marker_text,expected_type",
    [
        ("Error: SQL syntax error", "sql_injection"),
        ("Warning: mysql query failed", "sql_injection"),
        ("ORA-00921: unexpected end", "sql_injection"),
        ("PG::Error: bad query", "sql_injection"),
        ("SQLite3::query: malformed", "sql_injection"),
        ("Unclosed quotation mark after", "sql_injection"),
        ("root:x:0:0:root:/root", "path_traversal"),
        ("[extensions]\nfoo=bar", "path_traversal"),
    ],
)
def test_rule_match_sql_and_traversal_markers(marker_text: str, expected_type: str) -> None:
    """验证各类 SQL 错误关键词与穿越文件特征都能命中。"""
    scanner = VulnScanner(fetcher=_FakeHttpxClient())
    finding = scanner._rule_match("q", "payload", marker_text)
    assert finding is not None
    assert finding.type == expected_type
    assert finding.severity == "high"

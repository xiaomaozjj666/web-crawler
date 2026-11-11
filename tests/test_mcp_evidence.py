"""MCP 证据模型、schema 档位与错误恢复建议的单元测试。

覆盖 REA 借鉴项：``_evidence`` 存储/分页、``handle_tool`` 的 meta 收尾、
``inspect_evidence`` / ``list_evidence``、compact schema profile 与错误分类。
所有外部依赖（LLM / 浏览器 / ReverseAgent）均被 mock。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from web_crawler.mcp import _ssrf_gate
from web_crawler.mcp._evidence import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_RECORDS,
    EvidenceStore,
    _env_int,
    evidence_id_for,
    evidence_limits_from_env,
    page_text,
)
from web_crawler.mcp._server_helpers import _error
from web_crawler.mcp._specs import (
    _compact_description,
    build_tool_definitions,
    normalize_schema_profile,
)
from web_crawler.mcp.server import ReverseMCPServer, _to_json


def _make_server(*, agent: Any = None, max_records: int = 16, max_bytes: int = 1_000_000):
    """绕过 __init__ 构造 server，注入 mock 依赖与小型证据存储。"""
    srv: ReverseMCPServer = object.__new__(ReverseMCPServer)
    srv.provider_name = "deepseek"
    srv.model = "deepseek-v4-pro"
    srv.provider = MagicMock(name="provider", model="m", name_="deepseek")
    srv.analyzer = MagicMock(name="analyzer")
    srv.captcha_manager = MagicMock(name="captcha_manager")
    srv.agent = agent
    srv.evidence_store = EvidenceStore(max_records=max_records, max_bytes=max_bytes)
    srv.schema_profile = "full"
    srv._fetcher = None
    srv._browser_lock = threading.Lock()
    srv._progress_sender = None
    srv._progress_lock = threading.Lock()
    srv._closed = False
    return srv


@pytest.fixture(autouse=True)
def _fake_dns_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 URL 门禁的 DNS 解析替换为公网占位 IP，避免真实 DNS 查询。"""
    monkeypatch.setattr(_ssrf_gate, "_resolve_host_ips", lambda host: ["93.184.216.34"])


# -- EvidenceStore ------------------------------------------------------------


def test_evidence_id_is_stable_across_key_order() -> None:
    """规范化 JSON 后同内容不同键顺序得到同一个 evidence_id。"""
    left = evidence_id_for('{"b": 1, "a": 2}')
    right = evidence_id_for('{"a":2,"b":1}')
    assert left == right
    assert left.startswith("ev_")
    assert len(left) == len("ev_") + 16


def test_evidence_id_differs_for_different_content() -> None:
    assert evidence_id_for('{"a": 1}') != evidence_id_for('{"a": 2}')


def test_store_evicts_oldest_when_record_limit_reached() -> None:
    """条数上限触发 FIFO 淘汰，并累计 evicted 计数。"""
    store = EvidenceStore(max_records=2, max_bytes=1_000_000)
    first = store.put("t", '{"i": 1}')
    second = store.put("t", '{"i": 2}')
    third = store.put("t", '{"i": 3}')
    assert first is not None and second is not None and third is not None
    assert store.get(first.evidence_id) is None
    assert store.get(third.evidence_id) is not None
    assert store.stats()["evicted"] == 1
    assert store.stats()["records"] == 2


def test_store_evicts_by_byte_budget() -> None:
    """字节上限触发淘汰：旧记录让位给新记录。"""
    store = EvidenceStore(max_records=10, max_bytes=60)
    first = store.put("t", "x" * 40)
    second = store.put("t", "y" * 40)
    assert first is not None and second is not None
    assert store.get(first.evidence_id) is None
    assert store.get(second.evidence_id) is not None
    assert store.stats()["bytes_used"] == 40


def test_store_rejects_single_record_over_budget() -> None:
    """单条超过总预算时拒收并计数，调用方可内联返回。"""
    store = EvidenceStore(max_records=10, max_bytes=10)
    assert store.put("t", "x" * 20) is None
    assert store.stats()["rejected"] == 1
    assert store.records() == []


def test_store_dedupes_same_content() -> None:
    """同内容重复登记复用 id，不重复占用字节。"""
    store = EvidenceStore(max_records=10, max_bytes=1_000)
    first = store.put("t", '{"a": 1, "b": 2}')
    second = store.put("t2", '{"b":2,"a":1}')
    assert first is not None and second is not None
    assert first.evidence_id == second.evidence_id
    assert len(store.records()) == 1
    assert store.stats()["bytes_used"] == len(second.text.encode())


def test_store_get_refreshes_lru_position() -> None:
    """读取会刷新 LRU，最近读过的记录不会先被淘汰。"""
    store = EvidenceStore(max_records=2, max_bytes=1_000_000)
    first = store.put("t", '{"i": 1}')
    second = store.put("t", '{"i": 2}')
    assert first is not None and second is not None
    store.get(first.evidence_id)
    store.put("t", '{"i": 3}')
    assert store.get(first.evidence_id) is not None
    assert store.get(second.evidence_id) is None


def test_store_invalid_limits_rejected() -> None:
    with pytest.raises(ValueError):
        EvidenceStore(max_records=0)
    with pytest.raises(ValueError):
        EvidenceStore(max_bytes=0)


def test_evidence_limits_from_env_reports_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_CRAWLER_MCP_EVIDENCE_RECORDS", "abc")
    monkeypatch.setenv("WEB_CRAWLER_MCP_EVIDENCE_BYTES", "1")
    records, max_bytes, warnings = evidence_limits_from_env()
    assert records == 16
    assert max_bytes == 1024 * 1024
    assert len(warnings) == 2


def test_evidence_limits_from_env_accepts_values_in_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_CRAWLER_MCP_EVIDENCE_RECORDS", "8")
    monkeypatch.setenv("WEB_CRAWLER_MCP_EVIDENCE_BYTES", "1048576")
    records, max_bytes, warnings = evidence_limits_from_env()
    assert (records, max_bytes, warnings) == (8, 1048576, [])


def test_store_from_env_logs_invalid_values_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("WEB_CRAWLER_MCP_EVIDENCE_RECORDS", "not-a-number")
    monkeypatch.delenv("WEB_CRAWLER_MCP_EVIDENCE_BYTES", raising=False)
    with caplog.at_level(logging.WARNING, logger="web_crawler.mcp._evidence"):
        store = EvidenceStore.from_env()
    assert store.max_records == DEFAULT_MAX_RECORDS
    assert store.max_bytes == DEFAULT_MAX_BYTES
    assert any("不是整数" in record.getMessage() for record in caplog.records)


def test_store_exposes_configured_limits() -> None:
    store = EvidenceStore(max_records=3, max_bytes=4096)
    assert store.max_records == 3
    assert store.max_bytes == 4096


def test_env_int_without_warning_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_CRAWLER_TEST_INT", "abc")
    assert _env_int("WEB_CRAWLER_TEST_INT", 10, 1, 100, None) == 10
    monkeypatch.setenv("WEB_CRAWLER_TEST_INT", "999")
    assert _env_int("WEB_CRAWLER_TEST_INT", 10, 1, 100, None) == 100
    monkeypatch.setenv("WEB_CRAWLER_TEST_INT", "5")
    assert _env_int("WEB_CRAWLER_TEST_INT", 10, 1, 100, None) == 5


def test_page_text_boundaries() -> None:
    page = page_text("abcdef", 2, 2)
    assert page["text"] == "cd"
    assert page["has_more"] is True
    assert page["next_offset"] == 4
    tail = page_text("abcdef", 99, 2)
    assert tail["text"] == ""
    assert tail["offset"] == 6
    assert tail["has_more"] is False


# -- handle_tool 收尾：meta / evidence_id ------------------------------------


def test_handle_tool_adds_meta_and_evidence_id() -> None:
    srv = _make_server()
    srv._tool_deobfuscate_js = lambda args: _to_json({"deobfuscated": "ok"})
    parsed = json.loads(srv.handle_tool("deobfuscate_js", {"code": "x"}))
    assert parsed["deobfuscated"] == "ok"
    meta = parsed["meta"]
    assert meta["tool"] == "deobfuscate_js"
    assert meta["evidence_stored"] is True
    assert parsed["evidence_id"] == meta["evidence_id"]
    record = srv._evidence_store().get(parsed["evidence_id"])
    assert record is not None
    assert "deobfuscated" in record.text


def test_handle_tool_does_not_store_navigation_tools() -> None:
    srv = _make_server()
    parsed = json.loads(srv.handle_tool("list_evidence", {}))
    assert parsed["meta"]["evidence_stored"] is False
    assert parsed["meta"]["evidence_reason"] == "navigation_tool"


def test_handle_tool_evidence_oversize_reports_not_stored() -> None:
    """单条超过存储预算时 meta 如实标记未存储，而不是伪造 id。"""
    srv = _make_server(max_bytes=10)
    srv._tool_deobfuscate_js = lambda args: _to_json({"deobfuscated": "x" * 100})
    parsed = json.loads(srv.handle_tool("deobfuscate_js", {"code": "x"}))
    assert parsed["meta"]["evidence_stored"] is False
    assert parsed["meta"]["evidence_reason"] == "exceeds_store_limit"


# -- inspect_evidence / list_evidence ----------------------------------------


def test_inspect_evidence_pages_and_reports_metadata() -> None:
    srv = _make_server()
    record = srv._evidence_store().put("demo", _to_json({"text": "abcdef"}))
    assert record is not None
    parsed = json.loads(
        srv.handle_tool(
            "inspect_evidence",
            {"evidence_id": record.evidence_id, "offset": 0, "limit": 10},
        )
    )
    assert parsed["tool"] == "demo"
    assert parsed["content_type"] == "application/json"
    assert parsed["total_length"] == len(record.text)
    assert parsed["text"] == record.text[:10]
    assert parsed["has_more"] is True
    assert parsed["next_offset"] == 10


def test_inspect_evidence_unknown_id_returns_recovery() -> None:
    srv = _make_server()
    parsed = json.loads(srv.handle_tool("inspect_evidence", {"evidence_id": "ev_nope"}))
    assert parsed["kind"] == "invalid_request"
    assert "list_evidence" in parsed["recovery"]
    assert parsed["known_evidence_ids"] == []


def test_inspect_evidence_requires_id() -> None:
    srv = _make_server()
    parsed = json.loads(srv.handle_tool("inspect_evidence", {}))
    assert parsed["kind"] == "invalid_request"


def test_inspect_evidence_tool_rejects_blank_id_directly() -> None:
    srv = _make_server()
    parsed = json.loads(srv._tool_inspect_evidence({"evidence_id": "   "}))
    assert parsed["error"] == "evidence_id is required"


def test_list_evidence_reports_records_and_stats() -> None:
    srv = _make_server()
    first = srv._evidence_store().put("demo", '{"a": 1}')
    assert first is not None
    parsed = json.loads(srv.handle_tool("list_evidence", {}))
    assert parsed["count"] == 1
    assert parsed["records"][0]["evidence_id"] == first.evidence_id
    assert parsed["stats"]["max_records"] == 16


def test_evidence_resource_returns_record_list() -> None:
    srv = _make_server()
    record = srv._evidence_store().put("demo", '{"a": 1}')
    assert record is not None
    parsed = json.loads(srv.read_resource("evidence://records"))
    assert parsed["records"][0]["evidence_id"] == record.evidence_id


def test_config_resource_reports_profile_and_stats() -> None:
    srv = _make_server()
    parsed = json.loads(srv.read_resource("config://server"))
    assert parsed["schema_profile"] == "full"
    assert parsed["evidence"]["records"] == 0
    assert parsed["tools"] >= 10


def test_finalize_passes_through_non_json_and_non_dict() -> None:
    srv = _make_server()
    assert srv._finalize_tool_result("demo", "plain text", 1.0) == "plain text"
    assert srv._finalize_tool_result("demo", "[]", 1.0) == "[]"


def test_finalize_keeps_error_payload_without_meta() -> None:
    srv = _make_server()
    parsed = json.loads(
        srv._finalize_tool_result(
            "demo", _to_json({"error": "boom", "kind": "internal", "retryable": False}), 2.0
        )
    )
    assert parsed["error"] == "boom"
    assert parsed["kind"] == "internal"
    assert "meta" not in parsed


# -- 逆向结果：limitations / unknowns / coverage + 截断可恢复 -----------------


@dataclass
class _AgentConfig:
    max_steps: int = 20
    hooks: list[str] | None = None
    headless: bool = False
    target_params: list[str] | None = None


class _FakeAgent:
    def __init__(
        self,
        *,
        config: _AgentConfig,
        provider: Any = None,
        analyzer: Any = None,
        event_bus: Any = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.analyzer = analyzer

    def run(self, url: str, task: str = "") -> dict:
        return {
            "success": True,
            "status": "completed",
            "target_params_found": {"sign": "abc"},
            "analysis": None,
            "hook_data": {"records": [{"url": "https://x", "body": "B" * 5000}], "count": 1},
            "steps": 3,
            "history": [],
        }

    def close(self) -> None:
        pass


def test_reverse_agent_result_has_provenance_and_recoverable_truncation() -> None:
    srv = _make_server(agent=_FakeAgent(config=_AgentConfig()))
    parsed = json.loads(
        srv.handle_tool(
            "reverse_engineer_url",
            {"url": "http://x", "target_params": ["sign", "missing"], "max_text_length": 100},
        )
    )
    assert parsed["coverage"]["mode"] == "agent"
    assert parsed["coverage"]["steps_executed"] == 3
    assert parsed["unknowns"] == ["missing"]
    assert parsed["limitations"]
    assert parsed["truncations"]
    # 截断字段可从完整证据中取回
    record = srv._evidence_store().get(parsed["evidence_id"])
    assert record is not None
    assert "B" * 5000 in record.text
    page = json.loads(
        srv.handle_tool(
            "inspect_evidence",
            {"evidence_id": parsed["evidence_id"], "offset": 0, "limit": 200},
        )
    )
    assert page["has_more"] is True


def test_reverse_agent_non_dict_found_params_are_treated_as_empty() -> None:
    class _WeirdFoundAgent(_FakeAgent):
        def run(self, url: str, task: str = "") -> dict:
            result = super().run(url, task)
            result["target_params_found"] = ["sign"]  # 契约外类型：按「未找到」处理
            return result

    srv = _make_server(agent=_WeirdFoundAgent(config=_AgentConfig()))
    parsed = json.loads(
        srv.handle_tool("reverse_engineer_url", {"url": "http://x", "target_params": ["sign"]})
    )
    assert parsed["coverage"]["target_params_found"] == 0
    assert parsed["unknowns"] == ["sign"]


def test_reverse_fallback_result_declares_unknowns() -> None:
    srv = _make_server(agent=None)
    srv._run_browser_task = MagicMock(  # type: ignore[method-assign]
        return_value={"hook_records": [], "hook_count": 0, "scripts": ["a.js"]}
    )
    parsed = json.loads(
        srv.handle_tool("reverse_engineer_url", {"url": "http://x", "target_params": ["sign"]})
    )
    assert parsed["coverage"]["mode"] == "fallback"
    assert parsed["unknowns"] == ["sign"]
    assert any("没有 JS 反混淆" in item for item in parsed["limitations"])
    assert parsed["meta"]["evidence_stored"] is True


def test_reverse_fallback_oversize_result_reports_not_stored() -> None:
    srv = _make_server(agent=None, max_bytes=1)
    srv._run_browser_task = MagicMock(  # type: ignore[method-assign]
        return_value={"hook_records": [], "hook_count": 0, "scripts": ["a.js"]}
    )
    parsed = json.loads(
        srv.handle_tool("reverse_engineer_url", {"url": "http://x", "target_params": ["sign"]})
    )
    assert "evidence_id" not in parsed
    assert parsed["meta"]["evidence_stored"] is False
    assert parsed["meta"]["evidence_reason"] == "full_result_exceeds_store_limit"


def test_reverse_full_evidence_rejected_does_not_store_truncated_copy() -> None:
    """完整结果超限时，不能把截断后的响应登记成“完整证据”。"""
    srv = _make_server(agent=_FakeAgent(config=_AgentConfig()), max_bytes=1000)
    parsed = json.loads(
        srv.handle_tool(
            "reverse_engineer_url",
            {"url": "http://x", "target_params": ["sign"], "max_text_length": 100},
        )
    )
    assert "evidence_id" not in parsed
    assert "_evidence_unavailable_reason" not in parsed
    assert parsed["truncations"]
    assert parsed["meta"]["evidence_stored"] is False
    assert parsed["meta"]["evidence_reason"] == "full_result_exceeds_store_limit"
    # 截断副本没有偷偷占用证据槽位
    assert srv._evidence_store().stats()["records"] == 0
    assert srv._evidence_store().stats()["rejected"] == 1


def test_finalize_reports_evicted_evidence_instead_of_truncated_copy() -> None:
    """工具声明的证据在收尾前被淘汰时，不降级成截断副本。"""
    srv = _make_server()
    raw = _to_json({"evidence_id": "ev_missing", "result": {}, "truncations": ["result.x"]})
    parsed = json.loads(srv._finalize_tool_result("reverse_engineer_url", raw, 1.0))
    assert "evidence_id" not in parsed
    assert parsed["meta"]["evidence_stored"] is False
    assert parsed["meta"]["evidence_reason"] == "evidence_evicted"
    assert srv._evidence_store().stats()["records"] == 0


# -- schema profile ----------------------------------------------------------


def test_normalize_schema_profile_falls_back_to_full() -> None:
    assert normalize_schema_profile("compact") == "compact"
    assert normalize_schema_profile("COMPACT ") == "compact"
    assert normalize_schema_profile("bogus") == "full"
    assert normalize_schema_profile("") == "full"


def test_compact_description_keeps_short_text() -> None:
    assert _compact_description("很短的一句话") == "很短的一句话"


def test_compact_description_truncates_long_text_without_sentence() -> None:
    preview = _compact_description("x" * 500)
    assert len(preview) == 240
    assert preview.endswith("…")


def test_compact_profile_drops_descriptions_but_keeps_contract() -> None:
    full = {tool["name"]: tool for tool in build_tool_definitions("full")}
    compact = {tool["name"]: tool for tool in build_tool_definitions("compact")}
    assert set(full) == set(compact)
    for name, tool in compact.items():
        assert tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema.get("required", []) == full[name]["inputSchema"].get("required", [])
        for spec in schema.get("properties", {}).values():
            assert "description" not in spec
    # 枚举与默认值保留
    mode = compact["solve_captcha_image"]["inputSchema"]["properties"]["mode"]
    assert mode["enum"] == full["solve_captcha_image"]["inputSchema"]["properties"]["mode"]["enum"]
    assert (
        compact["reverse_engineer_url"]["inputSchema"]["properties"]["max_steps"]["default"] == 20
    )
    # 体积确实下降
    full_bytes = len(json.dumps(list(full.values()), ensure_ascii=False))
    compact_bytes = len(json.dumps(list(compact.values()), ensure_ascii=False))
    assert compact_bytes < full_bytes * 0.8


def test_server_reads_schema_profile_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_CRAWLER_MCP_SCHEMA_PROFILE", "compact")
    srv = _make_server()
    del srv.schema_profile  # 模拟绕过 __init__ 的替身：属性缺失时读环境变量
    assert srv._schema_profile() == "compact"
    assert all(
        "description" not in spec
        for tool in srv.get_tools()
        for spec in tool["inputSchema"].get("properties", {}).values()
    )


# -- 错误分类与恢复建议 ------------------------------------------------------


def test_error_classifies_unsafe_target() -> None:
    parsed = json.loads(_error("unsafe target: private address 127.0.0.1"))
    assert parsed["kind"] == "unsafe_target"
    assert parsed["retryable"] is False
    assert "allow_private" in parsed["recovery"]


def test_error_classifies_authorization_before_invalid_request() -> None:
    parsed = json.loads(_error("authorization required: ...", code=-32602))
    assert parsed["kind"] == "authorization"
    assert parsed["retryable"] is False


def test_error_classifies_dependency_missing() -> None:
    parsed = json.loads(_error("dependency missing", details="No module named 'camoufox'"))
    assert parsed["kind"] == "dependency_missing"
    assert "重启" in parsed["recovery"]


def test_error_classifies_provider_and_timeout() -> None:
    provider = json.loads(_error("LLM call failed", details="api key missing"))
    assert provider["kind"] == "provider_unavailable"
    # provider 自身的 403 仍应按 provider 处理
    provider_403 = json.loads(_error("LLM call failed", details="403 from provider"))
    assert provider_403["kind"] == "provider_unavailable"
    timeout = json.loads(_error("timeout", details="too slow"))
    assert timeout["kind"] == "timeout"
    assert timeout["retryable"] is True


def test_error_classifies_target_http_rejection_not_provider() -> None:
    parsed = json.loads(
        _error("浏览器操作失败", details="Page.goto: 403 Forbidden for https://target.example")
    )
    assert parsed["kind"] == "target_rejected"
    assert "DEEPSEEK" not in parsed["recovery"]
    assert parsed["retryable"] is False


def test_error_classifies_url_gate_messages() -> None:
    scheme = json.loads(_error("scheme not allowed: 'file'"))
    assert scheme["kind"] == "unsafe_target"
    invalid = json.loads(_error("invalid url: missing host in 'http://'"))
    assert invalid["kind"] == "invalid_request"
    userinfo = json.loads(_error("url must not contain userinfo (user:pass@)"))
    assert userinfo["kind"] == "invalid_request"


def test_error_classifies_browser_and_invalid_params() -> None:
    browser = json.loads(_error("浏览器操作失败", details="playwright crash"))
    assert browser["kind"] == "browser_unavailable"
    invalid = json.loads(_error("invalid params: missing required argument 'url'", code=-32602))
    assert invalid["kind"] == "invalid_request"


def test_handler_exception_gets_kind_and_recovery() -> None:
    srv = _make_server()

    def _boom(args: dict) -> str:
        raise TimeoutError("too slow")

    srv._tool_deobfuscate_js = _boom
    parsed = json.loads(srv.handle_tool("deobfuscate_js", {"code": "x"}))
    assert parsed["error"] == "timeout"
    assert parsed["kind"] == "timeout"
    assert parsed["retryable"] is True

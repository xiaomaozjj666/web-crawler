"""证据读取类 MCP 工具（``inspect_evidence`` / ``list_evidence``）。

工具结果由 :class:`web_crawler.mcp.server.ReverseMCPServer` 在
``handle_tool`` 收尾时登记；本模块只负责按 id 分页读取与列出，不重新执行
任何采集/LLM 操作。
"""

from __future__ import annotations

from typing import Any

from ._evidence import page_text
from ._host import HostContract
from ._server_helpers import (
    _DEFAULT_EVIDENCE_PAGE,
    _MAX_EVIDENCE_PAGE,
    _clamp_int,
    _error,
    _to_json,
)


def _content_type(text: str) -> str:
    """粗略判断存储正文类型，帮助客户端决定是否按 JSON 解析。"""
    head = text.lstrip()
    return "application/json" if head.startswith(("{", "[")) else "text/plain"


class EvidenceToolsMixin(HostContract):
    """会话证据读取工具。"""

    def _tool_inspect_evidence(self, args: dict) -> str:
        evidence_id = str(args.get("evidence_id", "")).strip()
        if not evidence_id:
            return _error("evidence_id is required", code=-32602)

        store = self._evidence_store()
        record = store.get(evidence_id)
        if record is None:
            known = [item.evidence_id for item in store.records()[-5:]]
            return _error(
                f"unknown evidence_id: {evidence_id}",
                kind="invalid_request",
                recovery=(
                    "先调用 list_evidence 查看本会话登记的证据；证据不跨进程持久化，"
                    "进程重启后需重新调用原工具生成。"
                ),
                known_evidence_ids=known,
            )

        offset = _clamp_int(args.get("offset"), 0, 0, len(record.text))
        limit = _clamp_int(args.get("limit"), _DEFAULT_EVIDENCE_PAGE, 1, _MAX_EVIDENCE_PAGE)
        page: dict[str, Any] = {
            "evidence_id": record.evidence_id,
            "tool": record.tool,
            "created_at": round(record.created_at, 3),
            "content_type": _content_type(record.text),
        }
        page.update(page_text(record.text, offset, limit))
        if record.metadata:
            page["metadata"] = record.metadata
        return _to_json(page)

    def _tool_list_evidence(self, args: dict) -> str:
        store = self._evidence_store()
        records = store.records()
        return _to_json(
            {
                "records": [
                    {
                        "evidence_id": record.evidence_id,
                        "tool": record.tool,
                        "created_at": round(record.created_at, 3),
                        "bytes": record.size_bytes,
                    }
                    for record in records
                ],
                "count": len(records),
                "stats": store.stats(),
            }
        )

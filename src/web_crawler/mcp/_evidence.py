"""会话级 Evidence 存储（借鉴 REA 的 Evidence 模型）。

REA（``morluto/rea``）把每次工具调用的完整结果登记成带稳定 id 的 Evidence：
调用方先拿摘要 + ``evidence_id`` + limitations/coverage，需要细节时按视图分页
取回，而不是重新执行一次昂贵操作。本项目在本进程内实现同一原则：

- 每次成功调用按规范化 JSON 的 sha256 生成 ``ev_<16hex>`` 稳定 id；
- 完整正文进入有界 LRU（条数 + 字节双上限），响应只带 id 与元数据；
- 工具在 ``max_text_length`` 截断前可自存完整结果，调用方随后可用
  ``inspect_evidence`` 按字符偏移取回，不必重跑浏览器/LLM；
- 淘汰与超限拒收都显式计数，``list_evidence`` 如实报告，绝不静默丢证据。

边界：这是**进程内**存储，不是持久化证据库；进程退出即失效，跨会话需重新
调用原工具。存储内容不签名，不可作为跨进程审计凭证。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

EVIDENCE_ID_PREFIX = "ev_"
DEFAULT_MAX_RECORDS = 16
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
MAX_MAX_RECORDS = 256
MIN_MAX_BYTES = 1024 * 1024
MAX_MAX_BYTES = 1024 * 1024 * 1024

_ENV_MAX_RECORDS = "WEB_CRAWLER_MCP_EVIDENCE_RECORDS"
_ENV_MAX_BYTES = "WEB_CRAWLER_MCP_EVIDENCE_BYTES"


@dataclass(frozen=True)
class EvidenceRecord:
    """一条已登记的工具结果。"""

    evidence_id: str
    tool: str
    created_at: float
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def size_bytes(self) -> int:
        """正文的 UTF-8 字节数（存储配额按此计）。"""
        return len(self.text.encode("utf-8"))


def _canonical_text(text: str) -> str:
    """把 JSON 文本规范化为稳定形式（键排序、紧凑分隔），用于计算 id。

    非 JSON 文本按原样参与哈希——工具结果本应是 JSON，走到这里说明调用方
    传入了自定义文本，稳定 id 仍应可用。
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    return json.dumps(
        parsed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def evidence_id_for(text: str) -> str:
    """返回文本的稳定证据 id：``ev_`` + 规范化 JSON 的 sha256 前 16 位。"""
    digest = hashlib.sha256(_canonical_text(text).encode("utf-8")).hexdigest()
    return f"{EVIDENCE_ID_PREFIX}{digest[:16]}"


def _env_int(name: str, default: int, lo: int, hi: int, warnings: list[str] | None = None) -> int:
    """读取整数环境变量；非法/越界时回退默认并记录原因（供 doctor 展示）。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        if warnings is not None:
            warnings.append(f"{name}={raw!r} 不是整数，已回退默认值 {default}")
        return default
    if value < lo or value > hi:
        if warnings is not None:
            warnings.append(f"{name}={value} 超出 [{lo}, {hi}]，已收敛到边界")
        return max(lo, min(value, hi))
    return value


def evidence_limits_from_env() -> tuple[int, int, list[str]]:
    """解析证据存储上限，返回 ``(max_records, max_bytes, warnings)``。"""
    warnings: list[str] = []
    max_records = _env_int(_ENV_MAX_RECORDS, DEFAULT_MAX_RECORDS, 1, MAX_MAX_RECORDS, warnings)
    max_bytes = _env_int(_ENV_MAX_BYTES, DEFAULT_MAX_BYTES, MIN_MAX_BYTES, MAX_MAX_BYTES, warnings)
    return max_records, max_bytes, warnings


class EvidenceStore:
    """线程安全的会话级证据 LRU。

    同一内容（规范化后相同）重复登记时复用同一 ``evidence_id``，只更新登记
    时间并移到队尾，不重复占用配额；读取也会刷新 LRU 位置。
    """

    def __init__(
        self, *, max_records: int = DEFAULT_MAX_RECORDS, max_bytes: int = DEFAULT_MAX_BYTES
    ):
        if max_records < 1:
            raise ValueError("max_records must be >= 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        self._max_records = max_records
        self._max_bytes = max_bytes
        self._records: OrderedDict[str, EvidenceRecord] = OrderedDict()
        self._total_bytes = 0
        self._evicted = 0
        self._rejected = 0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> EvidenceStore:
        """按环境变量构造；非法值回退默认并写 warning 日志（不阻断服务启动）。"""
        max_records, max_bytes, warnings = evidence_limits_from_env()
        for warning in warnings:
            logger.warning("evidence store: %s", warning)
        return cls(max_records=max_records, max_bytes=max_bytes)

    @property
    def max_records(self) -> int:
        return self._max_records

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def put(
        self, tool: str, text: str, *, metadata: dict[str, Any] | None = None
    ) -> EvidenceRecord | None:
        """登记一条结果；超出总字节上限时拒收并返回 None（调用方应内联返回）。"""
        evidence_id = evidence_id_for(text)
        size = len(text.encode("utf-8"))
        with self._lock:
            if size > self._max_bytes:
                self._rejected += 1
                return None
            existing = self._records.pop(evidence_id, None)
            if existing is not None:
                self._total_bytes -= existing.size_bytes
            while self._records and (
                len(self._records) >= self._max_records
                or self._total_bytes + size > self._max_bytes
            ):
                _, evicted = self._records.popitem(last=False)
                self._total_bytes -= evicted.size_bytes
                self._evicted += 1
            record = EvidenceRecord(
                evidence_id=evidence_id,
                tool=tool,
                created_at=time.time(),
                text=text,
                metadata=dict(metadata or {}),
            )
            self._records[evidence_id] = record
            self._total_bytes += size
            return record

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        """按 id 取回记录并刷新 LRU 位置；不存在返回 None。"""
        with self._lock:
            record = self._records.get(evidence_id)
            if record is not None:
                self._records.move_to_end(evidence_id)
            return record

    def records(self) -> list[EvidenceRecord]:
        """返回当前记录快照（按 LRU 顺序：最久未使用在前，最近使用在后）。"""
        with self._lock:
            return list(self._records.values())

    def stats(self) -> dict[str, Any]:
        """返回存储用量与淘汰计数；``evicted``/``rejected`` 用于如实报告丢失。"""
        with self._lock:
            return {
                "max_records": self._max_records,
                "max_bytes": self._max_bytes,
                "records": len(self._records),
                "bytes_used": self._total_bytes,
                "evicted": self._evicted,
                "rejected": self._rejected,
            }

    def clear(self) -> None:
        """清空全部记录（服务关闭时调用）。"""
        with self._lock:
            self._records.clear()
            self._total_bytes = 0


def page_text(text: str, offset: int, limit: int) -> dict[str, Any]:
    """按字符偏移切页（Python 码点，代理对不会被劈开）。"""
    total = len(text)
    start = max(0, min(offset, total))
    end = min(start + limit, total)
    return {
        "text": text[start:end],
        "total_length": total,
        "offset": start,
        "limit": limit,
        "has_more": end < total,
        "next_offset": end if end < total else None,
    }

"""条件请求缓存（``--cache``）：ETag / Last-Modified 校验器复用。

同一输出目录再次抓取同一 URL 时，带上上次响应的校验器发条件请求
（``If-None-Match`` / ``If-Modified-Since``）；上游答 ``304 Not Modified``
就跳过下载、直接复用上次产物。缓存文件：

    <输出目录>/.cache/validators.json

格式（``saved_path`` 一律是相对输出目录的 POSIX 路径，目录整体搬走仍可用）::

    {
      "version": 1,
      "entries": {
        "https://example.com/logo.png": {
          "etag": "\\"abc123\\"",
          "last_modified": "Wed, 21 Oct 2015 07:28:00 GMT",
          "saved_path": "images/logo.png",
          "content_type": "image/png",
          "bytes": 1234
        }
      }
    }

设计约束都是「缓存永不导致任务失败」的具体化：

- 文件缺失 / JSON 损坏 / 版本不认识 → 当作空缓存并记 warning，退回普通抓取；
- 条目没有任何校验器（服务器不发 ETag/Last-Modified）→ 不发条件请求；
- ``saved_path`` 必须落在输出目录内且文件仍在，否则该条目视为不可用；
- 缓存只影响「要不要下载」，不影响 SSRF / 大小上限 / 重试 / 限速语义。

与 ``--resume-crawl`` 的状态文件（``.crawl_state.json``）互不相干：两者
同目录、不同文件，各写各的，谁也不覆盖谁。``--list-only`` 不下载任何资源，
因此也不会写缓存（无变更时 :meth:`ValidatorCache.save` 直接跳过落盘）。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from web_crawler.app._fsutil import atomic_write_text

_log = logging.getLogger("crawler")

CACHE_DIRNAME = ".cache"
CACHE_FILENAME = "validators.json"
CACHE_VERSION = 1

#: 304 复用行的清单状态词。
#:
#: 保留 ``ok`` 前缀是刻意的：下游（summary / 报告 / 离线重写表）都按
#: ``status.startswith("ok")`` 聚合成功行，``status == "ok"`` 只用于
#: 「本轮真下载的字节数」这类精确统计。写成 ``ok (not modified)`` 既能
#: 如实标记「没重新下载」，又不会把复用行算进失败/跳过，也不会让
#: ``--rewrite-html`` 的离线包漏掉复用产物。同一模式已有先例（
#: ``ok; <播放列表说明>``）。
NOT_MODIFIED_STATUS = "ok (not modified)"


def cache_path(output_dir: Path) -> Path:
    """缓存文件路径：``<输出目录>/.cache/validators.json``。"""
    return Path(output_dir) / CACHE_DIRNAME / CACHE_FILENAME


@dataclass
class CachedArtifact:
    """一条缓存条目：上次响应的校验器 + 产物落盘位置。"""

    etag: str = ""
    last_modified: str = ""
    saved_path: str = ""
    content_type: str = ""
    bytes: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "etag": self.etag,
            "last_modified": self.last_modified,
            "saved_path": self.saved_path,
            "content_type": self.content_type,
            "bytes": self.bytes,
        }

    @classmethod
    def from_dict(cls, raw: object) -> CachedArtifact | None:
        """宽松解析一条条目；形状不对返回 None（该条按不存在处理）。"""
        if not isinstance(raw, dict):
            return None
        byte_count = raw.get("bytes")
        return cls(
            etag=_as_text(raw.get("etag")),
            last_modified=_as_text(raw.get("last_modified")),
            saved_path=_as_text(raw.get("saved_path")),
            content_type=_as_text(raw.get("content_type")),
            bytes=byte_count
            if isinstance(byte_count, int) and not isinstance(byte_count, bool) and byte_count >= 0
            else 0,
        )


def _as_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


class ValidatorCache:
    """内存中的校验器表（线程安全）；``save`` 原子落盘。

    下载阶段跑在 ``ThreadPoolExecutor`` 里，多个 worker 会同时查/写同一条
    表，因此所有读写都在同一把锁下完成；落盘在锁外进行（快照后原子写）。
    """

    def __init__(self, entries: dict[str, CachedArtifact] | None = None) -> None:
        self._entries: dict[str, CachedArtifact] = dict(entries or {})
        self._lock = threading.Lock()
        self._dirty = False

    # -- 载入 / 落盘 ----------------------------------------------------

    @classmethod
    def load(cls, output_dir: Path) -> ValidatorCache:
        """读取缓存；任何损坏/旧格式都降级为空缓存，绝不抛给调用方。"""
        path = cache_path(output_dir)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as exc:
            # ValueError 覆盖 JSONDecodeError 与 UnicodeDecodeError
            _log.warning("ignoring unreadable validator cache %s: %s", path, exc)
            return cls()
        if not isinstance(raw, dict):
            _log.warning("ignoring validator cache with unexpected shape: %s", path)
            return cls()
        version = raw.get("version")
        entries_raw = raw.get("entries")
        if version != CACHE_VERSION or not isinstance(entries_raw, dict):
            # 旧版/未来版的格式一律不猜：丢掉重来，下一次成功下载会重建
            _log.warning("ignoring validator cache with unknown format (version=%r)", version)
            return cls()
        entries: dict[str, CachedArtifact] = {}
        for url, payload in entries_raw.items():
            if not isinstance(url, str) or not url:
                continue
            artifact = CachedArtifact.from_dict(payload)
            if artifact is not None:
                entries[url] = artifact
        return cls(entries)

    def save(self, output_dir: Path) -> bool:
        """脏数据才落盘（无变更返回 False，不给 ``--list-only`` 制造空目录）。"""
        with self._lock:
            if not self._dirty:
                return False
            payload = json.dumps(
                {
                    "version": CACHE_VERSION,
                    "entries": {
                        url: artifact.to_dict() for url, artifact in sorted(self._entries.items())
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            self._dirty = False
        try:
            atomic_write_text(cache_path(output_dir), payload)
        except OSError:
            with self._lock:
                self._dirty = True
            raise
        return True

    # -- 查询 -----------------------------------------------------------

    def conditional_headers(self, url: str, output_dir: Path) -> dict[str, str]:
        """为 ``url`` 生成条件请求头；没有可用校验器/产物时返回空 dict。"""
        with self._lock:
            artifact = self._entries.get(url)
        if artifact is None or (not artifact.etag and not artifact.last_modified):
            return {}
        if self.resolve_saved_path(artifact, output_dir) is None:
            return {}
        headers: dict[str, str] = {}
        if artifact.etag:
            headers["If-None-Match"] = artifact.etag
        if artifact.last_modified:
            headers["If-Modified-Since"] = artifact.last_modified
        return headers

    def reuse(self, url: str, output_dir: Path) -> tuple[Path, str, int] | None:
        """304 命中时返回可复用的 ``(产物路径, content_type, 字节数)``。"""
        with self._lock:
            artifact = self._entries.get(url)
        if artifact is None:
            return None
        path = self.resolve_saved_path(artifact, output_dir)
        if path is None:
            return None
        try:
            size = path.stat().st_size
        except OSError:
            return None
        return path, artifact.content_type, size

    @staticmethod
    def resolve_saved_path(artifact: CachedArtifact, output_dir: Path) -> Path | None:
        """把条目里的相对路径解析成输出目录内的真实文件；非法/缺失返回 None。"""
        if not artifact.saved_path:
            return None
        relative = Path(artifact.saved_path)
        if relative.is_absolute():
            return None
        try:
            root = Path(output_dir).resolve()
            resolved = (root / relative).resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        return resolved

    # -- 更新 -----------------------------------------------------------

    def record(
        self,
        url: str,
        output_dir: Path,
        saved_path: Path,
        *,
        content_type: str = "",
        byte_count: int = 0,
        etag: str = "",
        last_modified: str = "",
    ) -> None:
        """记录一次成功下载：校验器 + 产物相对路径（不在输出目录内则不记）。"""
        try:
            relative = Path(saved_path).resolve().relative_to(Path(output_dir).resolve()).as_posix()
        except (OSError, RuntimeError, ValueError):
            # 产物落在输出目录之外时缓存无从复用（也挡住 ../ 逃逸的写入面）
            return
        artifact = CachedArtifact(
            etag=etag,
            last_modified=last_modified,
            saved_path=relative,
            content_type=content_type,
            bytes=max(0, int(byte_count)),
        )
        with self._lock:
            if self._entries.get(url) != artifact:
                self._entries[url] = artifact
                self._dirty = True

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

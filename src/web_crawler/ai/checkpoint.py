"""任务断点续跑（Checkpoint / Resume）模块。

借鉴 Skyvern / Nanobrowser 的断点持久化能力：在 Agent 主循环每一步结束时
把关键状态序列化为 JSON 文件，进程崩溃或主动中止后，可以从最近一个
checkpoint 恢复运行（重新打开浏览器、跳到上次的 URL、复用已注入的
hooks、保留 target_params_found 等已抽取参数）。

能力清单
--------
- :class:`Checkpoint` — 单步状态快照（dataclass）；
- :class:`CheckpointStore` — 文件系统持久化（原子写入 + 滚动保留）；
- :class:`CheckpointManager` — Agent 主循环接入点：
  * :meth:`save` — 步末保存；
  * :meth:`load_latest` — 启动时加载最新 checkpoint；
  * :meth:`resume` — 在 ReverseAgent 主循环里从指定 step 续跑；
  * :meth:`clear` — 任务完成后清理。

设计要点
--------
- 存储格式：JSON 文件（``checkpoints/<task_id>/step-XXXX.json``）；
- 原子写入：``write_temp + os.replace`` 避免崩溃时半截文件；
- 滚动保留：默认保留最近 5 个 checkpoint，更早的自动清理；
- 反序列化兼容：字段缺失时走默认值，向前兼容旧 checkpoint；
- 不持久化浏览器句柄：``context``/``page``/``fetcher`` 等运行时句柄由
  Agent 启动时重建，只持久化可序列化的状态数据。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 默认 checkpoint 目录（相对 cwd）
_DEFAULT_DIR = ".checkpoints"
# 默认滚动保留数量
_DEFAULT_KEEP = 5


@dataclass
class Checkpoint:
    """单步状态快照。

    Attributes
    ----------
    task_id:
        任务唯一标识（用于隔离不同任务的 checkpoint 目录）。
    step:
        当前已完成的步号（从 1 开始，0 表示尚未开始）。
    url:
        Agent 当前所在 URL（用于 resume 时导航回去）。
    task:
        任务描述（用于 resume 时重建 prompt）。
    target_params_found:
        已抽取的目标参数（dict[param_name, value]）。
    target_params:
        目标参数名列表。
    hooks:
        已注入的 Hook 名称列表（resume 时重新注入）。
    history:
        Agent 历史动作列表（用于上下文）。
    cumulative_summary:
        ContextCompressor 的累积摘要（用于 resume 时跳过压缩流程）。
    created_at:
        创建时间戳（Unix 秒）。
    metadata:
        任意额外元数据。
    """

    task_id: str
    step: int = 0
    url: str = ""
    task: str = ""
    target_params_found: dict[str, Any] = field(default_factory=dict)
    target_params: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    cumulative_summary: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        """容错反序列化：缺失字段走默认值。"""
        return cls(
            task_id=str(data.get("task_id", "")),
            step=int(data.get("step", 0)),
            url=str(data.get("url", "")),
            task=str(data.get("task", "")),
            target_params_found=dict(data.get("target_params_found") or {}),
            target_params=list(data.get("target_params") or []),
            hooks=list(data.get("hooks") or []),
            history=list(data.get("history") or []),
            cumulative_summary=str(data.get("cumulative_summary", "")),
            created_at=float(data.get("created_at", time.time())),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class CheckpointStore:
    """文件系统 checkpoint 存储。

    Parameters
    ----------
    base_dir:
        根目录，所有任务的 checkpoint 都在 ``base_dir/<task_id>/`` 下。
    keep:
        滚动保留数量，超过即清理最早的。
    """

    def __init__(
        self,
        base_dir: str | Path = _DEFAULT_DIR,
        *,
        keep: int = _DEFAULT_KEEP,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.keep = max(1, keep)

    def _task_dir(self, task_id: str) -> Path:
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id)
        return self.base_dir / safe_id

    def save(self, cp: Checkpoint) -> Path:
        """原子写入一个 checkpoint，返回文件路径。"""
        task_dir = self._task_dir(cp.task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        filename = f"step-{cp.step:04d}.json"
        target = task_dir / filename
        tmp = task_dir / f".{filename}.tmp"
        # 原子写入：先写临时文件再 rename
        tmp.write_text(cp.to_json(), encoding="utf-8")
        os.replace(tmp, target)
        self._rotate(cp.task_id)
        return target

    def load_latest(self, task_id: str) -> Checkpoint | None:
        """加载最新 checkpoint，没有则返回 None。"""
        task_dir = self._task_dir(task_id)
        if not task_dir.is_dir():
            return None
        files = sorted(task_dir.glob("step-*.json"))
        if not files:
            return None
        try:
            data = json.loads(files[-1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return Checkpoint.from_dict(data)

    def load_at(self, task_id: str, step: int) -> Checkpoint | None:
        """加载指定 step 的 checkpoint。"""
        task_dir = self._task_dir(task_id)
        target = task_dir / f"step-{step:04d}.json"
        if not target.is_file():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return Checkpoint.from_dict(data)

    def list_checkpoints(self, task_id: str) -> list[Path]:
        """列出某个任务的所有 checkpoint 文件（按 step 升序）。"""
        task_dir = self._task_dir(task_id)
        if not task_dir.is_dir():
            return []
        return sorted(task_dir.glob("step-*.json"))

    def clear(self, task_id: str) -> None:
        """清理某个任务的所有 checkpoint。"""
        task_dir = self._task_dir(task_id)
        if not task_dir.exists():
            return
        for f in task_dir.glob("step-*.json"):
            try:
                f.unlink()
            except OSError:
                pass
        # 空目录也清理掉
        try:
            task_dir.rmdir()
        except OSError:
            pass

    def _rotate(self, task_id: str) -> None:
        """滚动保留最近 N 个 checkpoint。"""
        files = self.list_checkpoints(task_id)
        if len(files) <= self.keep:
            return
        for f in files[: len(files) - self.keep]:
            try:
                f.unlink()
            except OSError:
                pass


class CheckpointManager:
    """Agent 主循环的 checkpoint 接入点。

    Parameters
    ----------
    task_id:
        任务唯一标识。若为 ``None`` 则自动生成（``url + 时间戳`` 的哈希）。
    store:
        :class:`CheckpointStore` 实例，缺省使用默认目录。
    enable:
        总开关，``False`` 时所有方法都变成 no-op，便于条件化使用。
    save_interval:
        每隔多少步保存一次（默认每步保存）。
    """

    def __init__(
        self,
        task_id: str | None = None,
        *,
        store: CheckpointStore | None = None,
        enable: bool = True,
        save_interval: int = 1,
    ) -> None:
        self.store = store or CheckpointStore()
        self.enable = enable
        self.save_interval = max(1, save_interval)
        self.task_id = task_id or ""

    def _ensure_task_id(self, url: str = "", task: str = "") -> str:
        if not self.task_id:
            import hashlib

            raw = f"{url}|{task}|{time.time()}"
            self.task_id = hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]
        return self.task_id

    def save(self, cp: Checkpoint) -> Path | None:
        """保存 checkpoint。``enable=False`` 时返回 None。"""
        if not self.enable:
            return None
        # 仅在 save_interval 倍数步保存
        if cp.step > 0 and cp.step % self.save_interval != 0:
            return None
        self._ensure_task_id(cp.url, cp.task)
        cp.task_id = self.task_id
        return self.store.save(cp)

    def load_latest(self) -> Checkpoint | None:
        """加载最新 checkpoint。"""
        if not self.enable or not self.task_id:
            return None
        return self.store.load_latest(self.task_id)

    def clear(self) -> None:
        """任务完成后清理。"""
        if not self.enable or not self.task_id:
            return
        self.store.clear(self.task_id)

    def build_checkpoint(
        self,
        *,
        step: int,
        url: str,
        task: str,
        target_params_found: dict[str, Any],
        target_params: list[str] | None,
        hooks: list[str] | None,
        history: list[dict[str, Any]],
        cumulative_summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Checkpoint:
        """便捷：从 Agent 主循环收集状态构建 Checkpoint。"""
        return Checkpoint(
            task_id=self.task_id,
            step=step,
            url=url,
            task=task,
            target_params_found=dict(target_params_found),
            target_params=list(target_params or []),
            hooks=list(hooks or []),
            history=list(history),
            cumulative_summary=cumulative_summary,
            metadata=dict(metadata or {}),
        )


__all__ = [
    "Checkpoint",
    "CheckpointManager",
    "CheckpointStore",
]

"""SQLite 持久化层 —— 任务历史与采集结果存储。

使用标准库 sqlite3，零外部依赖。数据库文件默认放在项目根目录
``crawler_data.db``，可通过环境变量 ``CRAWLER_DB_PATH`` 覆盖。
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_DB_PATH = os.environ.get(
    "CRAWLER_DB_PATH",
    str(Path(__file__).resolve().parent.parent / "crawler_data.db"),
)

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """建表（幂等），在 UI 启动时调用一次。"""
    with _lock:
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id          TEXT PRIMARY KEY,
                    url         TEXT NOT NULL,
                    config      TEXT NOT NULL,
                    output_dir  TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'running',
                    exit_code   INTEGER,
                    log         TEXT DEFAULT '',
                    total_resources   INTEGER DEFAULT 0,
                    processed_resources INTEGER DEFAULT 0,
                    pages_scanned     INTEGER DEFAULT 0,
                    current_url       TEXT DEFAULT '',
                    created_at  REAL NOT NULL,
                    finished_at REAL
                );

                CREATE TABLE IF NOT EXISTS results (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id     TEXT NOT NULL,
                    url         TEXT NOT NULL,
                    saved_path  TEXT DEFAULT '',
                    content_type TEXT DEFAULT '',
                    bytes       INTEGER DEFAULT 0,
                    category    TEXT DEFAULT '',
                    found_in    TEXT DEFAULT '',
                    kind        TEXT DEFAULT '',
                    page_url    TEXT DEFAULT '',
                    page_title  TEXT DEFAULT '',
                    sha256      TEXT DEFAULT '',
                    status      TEXT DEFAULT '',
                    diagnostic  TEXT DEFAULT '',
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_results_task_id ON results(task_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
                """
            )
            conn.commit()
        finally:
            conn.close()


def create_task(
    task_id: str,
    url: str,
    config: dict[str, Any],
    output_dir: str,
) -> None:
    """任务启动时写入数据库。"""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO tasks
                   (id, url, config, output_dir, status, created_at)
                   VALUES (?, ?, ?, ?, 'running', ?)""",
                (task_id, url, json.dumps(config, ensure_ascii=False),
                 output_dir, time.time()),
            )
            conn.commit()
        finally:
            conn.close()


def update_task_status(
    task_id: str,
    status: str,
    exit_code: int | None = None,
    log: str | None = None,
    total_resources: int | None = None,
    processed_resources: int | None = None,
    pages_scanned: int | None = None,
    current_url: str | None = None,
) -> None:
    """更新任务状态字段，只更新非 None 的列。"""
    sets: list[str] = []
    vals: list[Any] = []
    if exit_code is not None:
        sets.append("exit_code = ?")
        vals.append(exit_code)
    if log is not None:
        sets.append("log = ?")
        vals.append(log[-80000:])
    if total_resources is not None:
        sets.append("total_resources = ?")
        vals.append(total_resources)
    if processed_resources is not None:
        sets.append("processed_resources = ?")
        vals.append(processed_resources)
    if pages_scanned is not None:
        sets.append("pages_scanned = ?")
        vals.append(pages_scanned)
    if current_url is not None:
        sets.append("current_url = ?")
        vals.append(current_url)
    sets.append("status = ?")
    vals.append(status)
    if status in ("done", "error", "cancelled"):
        sets.append("finished_at = ?")
        vals.append(time.time())
    vals.append(task_id)
    sql = f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?"
    with _lock:
        conn = _connect()
        try:
            conn.execute(sql, vals)
            conn.commit()
        finally:
            conn.close()


def finish_task(task_id: str, status: str, exit_code: int) -> None:  # pragma: no cover
    """任务结束时调用：写入最终状态 + 解析结果清单。"""
    update_task_status(task_id, status, exit_code=exit_code)


def import_results(task_id: str, output_dir: str) -> int:
    """从 resources_manifest.jsonl 导入采集结果到 results 表。

    优先读 jsonl（每行一个 dict），回退到 csv。
    返回导入行数。
    """
    out = Path(output_dir)
    jsonl_path = out / "resources_manifest.jsonl"
    csv_path = out / "resources_manifest.csv"

    rows: list[dict[str, Any]] = []
    if jsonl_path.exists():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    elif csv_path.exists():
        with csv_path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    else:
        return 0

    if not rows:  # pragma: no cover
        return 0

    with _lock:
        conn = _connect()
        try:
            conn.executemany(
                """INSERT INTO results
                   (task_id, url, saved_path, content_type, bytes, category,
                    found_in, kind, page_url, page_title, sha256, status, diagnostic)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        task_id,
                        r.get("url", ""),
                        r.get("saved_path", ""),
                        r.get("content_type", ""),
                        int(r.get("bytes", 0) or 0),
                        r.get("category", ""),
                        r.get("found_in", ""),
                        r.get("kind", ""),
                        r.get("page_url", ""),
                        r.get("page_title", ""),
                        r.get("sha256", ""),
                        r.get("status", ""),
                        r.get("diagnostic", ""),
                    )
                    for r in rows
                ],
            )
            conn.commit()
        finally:
            conn.close()
    return len(rows)


def list_tasks(
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
) -> dict[str, Any]:
    """分页查询任务列表，返回 {tasks, total, page, page_size}。"""
    offset = (page - 1) * page_size
    where = "WHERE status = ?" if status and status != "all" else ""
    params: list[Any] = [status] if status and status != "all" else []

    with _lock:
        conn = _connect()
        try:
            count_sql = f"SELECT COUNT(*) FROM tasks {where}"
            total = conn.execute(count_sql, params).fetchone()[0]

            list_sql = (
                f"SELECT id, url, status, exit_code, output_dir, "
                f"total_resources, processed_resources, pages_scanned, "
                f"created_at, finished_at "
                f"FROM tasks {where} "
                f"ORDER BY created_at DESC LIMIT ? OFFSET ?"
            )
            rows = conn.execute(list_sql, params + [page_size, offset]).fetchall()
        finally:
            conn.close()

    tasks = []
    for r in rows:
        tasks.append({
            "id": r["id"],
            "url": r["url"],
            "status": r["status"],
            "exit_code": r["exit_code"],
            "output_dir": r["output_dir"],
            "total_resources": r["total_resources"],
            "processed_resources": r["processed_resources"],
            "pages_scanned": r["pages_scanned"],
            "created_at": r["created_at"],
            "finished_at": r["finished_at"],
        })
    return {"tasks": tasks, "total": total, "page": page, "page_size": page_size}


def get_task(task_id: str) -> dict[str, Any] | None:
    """查询单个任务详情。"""
    with _lock:
        conn = _connect()
        try:
            r = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
    if not r:
        return None
    return {
        "id": r["id"],
        "url": r["url"],
        "config": json.loads(r["config"]) if r["config"] else {},
        "output_dir": r["output_dir"],
        "status": r["status"],
        "exit_code": r["exit_code"],
        "log": r["log"],
        "total_resources": r["total_resources"],
        "processed_resources": r["processed_resources"],
        "pages_scanned": r["pages_scanned"],
        "current_url": r["current_url"],
        "created_at": r["created_at"],
        "finished_at": r["finished_at"],
    }


def delete_task(task_id: str) -> bool:
    """删除任务及其结果（级联删除）。返回是否删除成功。"""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def get_results(
    task_id: str,
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
) -> dict[str, Any]:
    """分页查询某任务的结果列表。"""
    offset = (page - 1) * page_size
    where = "WHERE task_id = ?"
    params: list[Any] = [task_id]
    if search:
        where += " AND (url LIKE ? OR saved_path LIKE ? OR category LIKE ?)"
        kw = f"%{search}%"
        params += [kw, kw, kw]

    with _lock:
        conn = _connect()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM results {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"""SELECT url, saved_path, content_type, bytes, category,
                          found_in, kind, page_url, page_title, sha256, status
                   FROM results {where}
                   ORDER BY id LIMIT ? OFFSET ?""",
                params + [page_size, offset],
            ).fetchall()
        finally:
            conn.close()

    results = []
    for r in rows:
        results.append({
            "url": r["url"],
            "saved_path": r["saved_path"],
            "content_type": r["content_type"],
            "bytes": r["bytes"],
            "category": r["category"],
            "found_in": r["found_in"],
            "kind": r["kind"],
            "page_url": r["page_url"],
            "page_title": r["page_title"],
            "sha256": r["sha256"],
            "status": r["status"],
        })
    return {"results": results, "total": total, "page": page, "page_size": page_size}

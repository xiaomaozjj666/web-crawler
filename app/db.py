"""SQLite 持久化层 —— 任务历史与采集结果存储。

使用标准库 sqlite3，零外部依赖。数据库文件默认放在项目根目录
``crawler_data.db``，可通过环境变量 ``CRAWLER_DB_PATH`` 覆盖。
"""

from __future__ import annotations

import atexit
import csv
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_DB_PATH = os.environ.get(
    "CRAWLER_DB_PATH",
    str(Path(__file__).resolve().parent.parent / "crawler_data.db"),
)

_local = threading.local()
_write_lock = threading.Lock()

# 全局连接登记表：threading.local 没有析构钩子，线程级连接不会随线程退出
# 自动关闭（会产生 ResourceWarning: unclosed database）。所有连接创建时登记、
# 关闭时移除，进程退出时由 close_all_connections()（atexit）统一关闭。
_all_conns: set[sqlite3.Connection] = set()
_all_conns_lock = threading.Lock()


def _register_conn(conn: sqlite3.Connection) -> None:
    with _all_conns_lock:
        _all_conns.add(conn)


def _unregister_conn(conn: sqlite3.Connection) -> None:
    with _all_conns_lock:
        _all_conns.discard(conn)


def _safe_int(value: object, default: int = 0) -> int:
    """把清单字段安全转为 int；非数字/None 回退默认值（防单行脏数据中断导入）。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


def _get_conn() -> sqlite3.Connection:
    """返回当前线程的持久化 SQLite 连接（线程安全，自动创建）。

    若 _DB_PATH 在两次调用之间被更动（测试夹具常见操作），自动关闭旧连接
    并创建新的。
    """
    current_path = _DB_PATH  # 模块级变量，测试中可能被 monkeypatch
    need_new = (
        not hasattr(_local, "conn")
        or _local.conn is None
        or getattr(_local, "_conn_path", None) != current_path
    )
    if need_new:
        if hasattr(_local, "conn") and _local.conn is not None:
            try:
                _local.conn.close()
            except Exception:
                pass
            _unregister_conn(_local.conn)
        _local.conn = sqlite3.connect(current_path, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
        _local.conn.execute("PRAGMA busy_timeout=5000")
        _local._conn_path = current_path
        _register_conn(_local.conn)
    return _local.conn


def close_thread_connection() -> None:
    """关闭并清理当前线程的连接（线程退出/测试 teardown 时调用，幂等）。"""
    if hasattr(_local, "conn") and _local.conn is not None:
        conn = _local.conn
        _local.conn = None
        try:
            conn.close()
        except Exception:
            pass
        _unregister_conn(conn)


def close_all_connections() -> None:
    """关闭全部已登记连接（进程退出/测试会话结束时调用，幂等）。

    关闭后当前线程的 thread-local 引用一并失效，下次 ``_get_conn()`` 会重建；
    其他线程若在关闭后继续访问数据库属误用（该函数仅用于退出/收尾场景）。
    """
    with _all_conns_lock:
        conns = list(_all_conns)
        _all_conns.clear()
    for conn in conns:
        try:
            conn.close()
        except Exception:
            pass
    if hasattr(_local, "conn") and _local.conn in conns:
        _local.conn = None


atexit.register(close_all_connections)


def init_db() -> None:
    """建表（幂等），在 UI 启动时调用一次。"""
    with _write_lock:
        conn = _get_conn()
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
            pass  # 连接已登记于 _all_conns，进程退出时由 close_all_connections() 统一关闭


def create_task(
    task_id: str,
    url: str,
    config: dict[str, Any],
    output_dir: str,
) -> None:
    """任务启动时写入数据库。"""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO tasks
               (id, url, config, output_dir, status, created_at)
               VALUES (?, ?, ?, ?, 'running', ?)""",
            (task_id, url, json.dumps(config, ensure_ascii=False),
             output_dir, time.time()),
        )
        conn.commit()


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
    with _write_lock:
        conn = _get_conn()
        conn.execute(sql, vals)
        conn.commit()


def import_results(task_id: str, output_dir: str) -> int:
    """从 resources_manifest.jsonl 导入采集结果到 results 表。

    优先读 jsonl（每行一个 dict），回退到 csv。
    返回导入行数。
    """
    out = Path(output_dir)
    jsonl_path = out / "resources_manifest.jsonl"
    csv_path = out / "resources_manifest.csv"

    rows: list[dict[str, Any]] = []
    skipped_bad = 0
    if jsonl_path.exists():
        # 逐行容错：坏行跳过并计数,不因单行损坏中断整体导入
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                skipped_bad += 1
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
            else:
                skipped_bad += 1
    elif csv_path.exists():
        with csv_path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    else:
        return 0

    # JSONL 完全不可读时回退 CSV（例如清单文件被截断损坏）
    if not rows and skipped_bad and csv_path.exists():
        _log.warning(
            "JSONL manifest unreadable (%d bad lines), falling back to CSV", skipped_bad
        )
        with csv_path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        skipped_bad = 0

    if skipped_bad:
        _log.warning("skipped %d malformed manifest line(s) in %s", skipped_bad, jsonl_path)

    if not rows:  # pragma: no cover
        return 0

    with _write_lock:
        conn = _get_conn()
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
                    _safe_int(r.get("bytes", 0)),
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

    conn = _get_conn()
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
    conn = _get_conn()
    r = conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
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
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return cur.rowcount > 0


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

    conn = _get_conn()
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

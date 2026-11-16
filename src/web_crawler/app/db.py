"""SQLite 持久化层 —— 任务历史与采集结果存储。

使用标准库 sqlite3，零外部依赖。数据库文件默认放在当前工作目录
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
import zlib
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .crawler_models import ManifestRow, PageExtraction, Resource

_log = logging.getLogger(__name__)
MAX_PAGE_SAMPLE_BYTES = 2 * 1024 * 1024
MAX_TASK_SAMPLE_BYTES = 16 * 1024 * 1024

# 默认放在启动时的当前工作目录：pip 安装后包目录位于 site-packages，
# 可能无写权限，用户数据写进包内也会随重装/升级丢失。
_DB_PATH = os.environ.get("CRAWLER_DB_PATH", str(Path.cwd() / "crawler_data.db"))

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


def fail_stale_running_tasks() -> int:
    """将上次进程退出遗留的活动任务标记为 error（UI 启动时调用）。

    任务状态由内存中的 JobState 驱动，进程退出后内存态丢失，数据库里的
    running/paused 记录永远无法收敛——启动时统一标记，避免历史列表里
    积累永远不会结束的"僵尸任务"。
    """
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE tasks SET status='error', exit_code=1, finished_at=?, error_message=?, "
            "log = COALESCE(log, '') || ? "
            "WHERE status IN ('running', 'paused', 'cancelling')",
            (
                time.time(),
                "程序退出时任务未完成",
                "\n[启动清理] 程序上次退出时任务未完成，已标记为中断",
            ),
        )
        conn.commit()
        return cur.rowcount


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
                    error_message TEXT DEFAULT '',
                    summary     TEXT NOT NULL DEFAULT '{}',
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
                CREATE INDEX IF NOT EXISTS idx_results_task_page ON results(task_id, page_url);
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);

                CREATE TABLE IF NOT EXISTS page_extractions (
                    task_id     TEXT NOT NULL,
                    page_url    TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    body        TEXT NOT NULL,
                    data        TEXT,
                    rule_result TEXT,
                    sample      BLOB,
                    PRIMARY KEY (task_id, page_url),
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS resource_sources (
                    task_id TEXT NOT NULL,
                    page_url TEXT NOT NULL,
                    resource_url TEXT NOT NULL,
                    PRIMARY KEY (task_id, page_url, resource_url),
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS resource_dependencies (
                    task_id TEXT NOT NULL,
                    parent_url TEXT NOT NULL,
                    child_url TEXT NOT NULL,
                    PRIMARY KEY (task_id, parent_url, child_url),
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                ) WITHOUT ROWID;
                """
            )
            # 轻量迁移：老库没有 error_message 列就补上（不改历史数据）
            try:
                cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
                if "error_message" not in cols:
                    conn.execute("ALTER TABLE tasks ADD COLUMN error_message TEXT DEFAULT ''")
                if "summary" not in cols:
                    conn.execute("ALTER TABLE tasks ADD COLUMN summary TEXT NOT NULL DEFAULT '{}'")
                page_cols = {
                    row["name"] for row in conn.execute("PRAGMA table_info(page_extractions)")
                }
                if "rule_result" not in page_cols:
                    conn.execute("ALTER TABLE page_extractions ADD COLUMN rule_result TEXT")
                if "sample" not in page_cols:
                    conn.execute("ALTER TABLE page_extractions ADD COLUMN sample BLOB")
            except sqlite3.Error:  # pragma: no cover - 迁移失败不阻塞启动
                pass
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
            (task_id, url, json.dumps(config, ensure_ascii=False), output_dir, time.time()),
        )
        conn.commit()


def update_task_status(
    task_id: str,
    status: str,
    exit_code: int | None = None,
    log: str | None = None,
    error_message: str | None = None,
    total_resources: int | None = None,
    processed_resources: int | None = None,
    pages_scanned: int | None = None,
    current_url: str | None = None,
    *,
    result_rows: list[ManifestRow] | None = None,
    resource_sources: Iterable[Resource] = (),
    resource_dependencies: Iterable[tuple[str, str]] = (),
    summary: dict[str, Any] | None = None,
) -> None:
    """更新非空状态字段，可在同一事务中替换本次任务的结果。"""
    sets: list[str] = []
    vals: list[Any] = []
    if exit_code is not None:
        sets.append("exit_code = ?")
        vals.append(exit_code)
    if log is not None:
        sets.append("log = ?")
        vals.append(log[-80000:])
    if error_message is not None:
        sets.append("error_message = ?")
        vals.append(error_message[-4000:])
    if summary is not None:
        sets.append("summary = ?")
        vals.append(json.dumps(summary, ensure_ascii=False))
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
    if status in ("done", "partial", "skipped", "error", "cancelled"):
        sets.append("finished_at = ?")
        vals.append(time.time())
    vals.append(task_id)
    sql = f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?"
    with _write_lock:
        conn = _get_conn()
        with conn:
            updated = conn.execute(sql, vals)
            if result_rows is not None and updated.rowcount:
                conn.execute("DELETE FROM results WHERE task_id = ?", (task_id,))
                _insert_results(conn, task_id, (asdict(row) for row in result_rows))
                conn.execute("DELETE FROM resource_sources WHERE task_id = ?", (task_id,))
                conn.execute("DELETE FROM resource_dependencies WHERE task_id = ?", (task_id,))
                urls = {row.url for row in result_rows}
                conn.executemany(
                    "INSERT OR IGNORE INTO resource_sources VALUES (?, ?, ?)",
                    (
                        (task_id, resource.page_url, resource.url)
                        for resource in resource_sources
                        if resource.page_url and resource.url in urls
                    ),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO resource_dependencies VALUES (?, ?, ?)",
                    (
                        (task_id, parent, child)
                        for parent, child in resource_dependencies
                        if parent in urls and child in urls
                    ),
                )


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
        _log.warning("JSONL manifest unreadable (%d bad lines), falling back to CSV", skipped_bad)
        with csv_path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        skipped_bad = 0

    if skipped_bad:
        _log.warning("skipped %d malformed manifest line(s) in %s", skipped_bad, jsonl_path)

    if not rows:  # pragma: no cover
        return 0

    with _write_lock:
        conn = _get_conn()
        with conn:
            _insert_results(conn, task_id, rows)
    return len(rows)


def _insert_results(conn: sqlite3.Connection, task_id: str, rows: Iterable[dict[str, Any]]) -> None:
    conn.executemany(
        """INSERT INTO results
           (task_id, url, saved_path, content_type, bytes, category,
            found_in, kind, page_url, page_title, sha256, status, diagnostic)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            (
                task_id,
                row.get("url", ""),
                row.get("saved_path", ""),
                row.get("content_type", ""),
                _safe_int(row.get("bytes", 0)),
                row.get("category", ""),
                row.get("found_in", ""),
                row.get("kind", ""),
                row.get("page_url", ""),
                row.get("page_title", ""),
                row.get("sha256", ""),
                row.get("status", ""),
                row.get("diagnostic", ""),
            )
            for row in rows
        ),
    )


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
        f"SELECT id, url, status, exit_code, error_message, output_dir, "
        f"total_resources, processed_resources, pages_scanned, "
        f"created_at, finished_at "
        f"FROM tasks {where} "
        f"ORDER BY created_at DESC LIMIT ? OFFSET ?"
    )
    rows = conn.execute(list_sql, params + [page_size, offset]).fetchall()

    tasks = []
    for r in rows:
        tasks.append(
            {
                "id": r["id"],
                "url": r["url"],
                "status": r["status"],
                "exit_code": r["exit_code"],
                "error_message": r["error_message"],
                "output_dir": r["output_dir"],
                "total_resources": r["total_resources"],
                "processed_resources": r["processed_resources"],
                "pages_scanned": r["pages_scanned"],
                "created_at": r["created_at"],
                "finished_at": r["finished_at"],
            }
        )
    return {"tasks": tasks, "total": total, "page": page, "page_size": page_size}


def get_task(task_id: str) -> dict[str, Any] | None:
    """查询单个任务详情。"""
    conn = _get_conn()
    r = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not r:
        return None
    return {
        "id": r["id"],
        "url": r["url"],
        "config": json.loads(r["config"]) if r["config"] else {},
        "output_dir": r["output_dir"],
        "status": r["status"],
        "exit_code": r["exit_code"],
        "error_message": r["error_message"],
        "summary": json.loads(r["summary"]),
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
    *,
    page_url: str | None = None,
) -> dict[str, Any]:
    """分页查询某任务的结果列表。"""
    offset = (page - 1) * page_size
    dependencies = ""
    where = "WHERE task_id = ?"
    params: list[Any] = [task_id]
    if page_url:
        # UNION 避免样式表循环引用时反复遍历同一资源。
        dependencies = """WITH RECURSIVE page_assets(url) AS (
            SELECT resource_url FROM resource_sources WHERE task_id = ? AND page_url = ?
            UNION
            SELECT child_url FROM resource_dependencies JOIN page_assets ON parent_url = page_assets.url
            WHERE task_id = ?
        ) """
        where += " AND (page_url = ? OR url IN (SELECT url FROM page_assets))"
        params = [task_id, page_url, task_id, task_id, page_url]
    if search:
        where += " AND (url LIKE ? OR saved_path LIKE ? OR content_type LIKE ? OR category LIKE ?)"
        kw = f"%{search}%"
        params += [kw, kw, kw, kw]

    conn = _get_conn()
    total = conn.execute(f"{dependencies}SELECT COUNT(*) FROM results {where}", params).fetchone()[
        0
    ]
    rows = conn.execute(
        f"""{dependencies}SELECT url, saved_path, content_type, bytes, category,
                  found_in, kind, page_url, page_title, sha256, status, diagnostic
           FROM results {where}
           ORDER BY id LIMIT ? OFFSET ?""",
        params + [page_size, offset],
    ).fetchall()

    results = []
    for r in rows:
        results.append(
            {
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
                "diagnostic": r["diagnostic"],
            }
        )
    return {"results": results, "total": total, "page": page, "page_size": page_size}


def save_page_extraction(task_id: str, extraction: PageExtraction) -> None:
    data = json.dumps(extraction.data, ensure_ascii=False) if extraction.data is not None else None
    result = (
        json.dumps(extraction.rule_result, ensure_ascii=False)
        if extraction.rule_result is not None
        else None
    )
    with _write_lock:
        conn = _get_conn()
        with conn:
            conn.execute(
                """INSERT INTO page_extractions (task_id, page_url, title, body, data, rule_result)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task_id, page_url) DO UPDATE SET
                     title = excluded.title, body = excluded.body, data = excluded.data,
                     rule_result = excluded.rule_result""",
                (task_id, extraction.url, extraction.title, extraction.text, data, result),
            )


def list_extracted_pages(task_id: str, page: int, page_size: int) -> dict[str, Any]:
    conn = _get_conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM page_extractions WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    rows = conn.execute(
        """SELECT page_url, title FROM page_extractions WHERE task_id = ?
           ORDER BY page_url LIMIT ? OFFSET ?""",
        (task_id, page_size, (page - 1) * page_size),
    )
    return {"pages": [dict(row) for row in rows], "total": total, "page_size": page_size}


def get_page_extraction(task_id: str, page_url: str) -> dict[str, Any] | None:
    row = (
        _get_conn()
        .execute(
            """SELECT page_url, title, body, data, rule_result, sample IS NOT NULL AS has_sample
               FROM page_extractions WHERE task_id = ? AND page_url = ?""",
            (task_id, page_url),
        )
        .fetchone()
    )
    if row is None:
        return None
    return {
        **dict(row),
        "data": json.loads(row["data"]) if row["data"] is not None else None,
        "rule_result": json.loads(row["rule_result"]) if row["rule_result"] is not None else None,
        "has_sample": bool(row["has_sample"]),
    }


def save_page_sample(task_id: str, page_url: str, title: str, sample: bytes) -> None:
    with _write_lock:
        conn = _get_conn()
        with conn:
            conn.execute(
                """INSERT INTO page_extractions (task_id, page_url, title, body, sample)
                   VALUES (?, ?, ?, '', ?)
                   ON CONFLICT(task_id, page_url) DO UPDATE SET sample = excluded.sample""",
                (task_id, page_url, title, sample),
            )


def get_page_sample(task_id: str, page_url: str) -> str | None:
    row = (
        _get_conn()
        .execute(
            "SELECT sample FROM page_extractions WHERE task_id = ? AND page_url = ?",
            (task_id, page_url),
        )
        .fetchone()
    )
    if row is None or row["sample"] is None:
        return None
    return decode_page_sample(row["sample"])


def page_samples_after(task_id: str, after: str, limit: int) -> list[tuple[str, bytes]]:
    return [
        (row["page_url"], row["sample"])
        for row in _get_conn().execute(
            """SELECT page_url, sample FROM page_extractions
               WHERE task_id = ? AND page_url > ? AND sample IS NOT NULL
               ORDER BY page_url LIMIT ?""",
            (task_id, after, limit),
        )
    ]


def count_page_samples(task_id: str) -> int:
    row = (
        _get_conn()
        .execute(
            "SELECT COUNT(*) FROM page_extractions WHERE task_id = ? AND sample IS NOT NULL",
            (task_id,),
        )
        .fetchone()
    )
    return int(row[0])


def decode_page_sample(sample: bytes) -> str:
    decoder = zlib.decompressobj()
    try:
        html = decoder.decompress(sample, MAX_PAGE_SAMPLE_BYTES + 1)
        if len(html) <= MAX_PAGE_SAMPLE_BYTES and decoder.eof:
            return html.decode("utf-8")
    except (zlib.error, UnicodeError):
        pass
    raise ValueError("页面样本已损坏，请重新采集")

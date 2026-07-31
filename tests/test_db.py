"""app/db.py 单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import db


@pytest.fixture(autouse=True)
def _temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试用临时数据库文件。"""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("CRAWLER_DB_PATH", db_path)
    # 重新初始化内部连接路径
    db._DB_PATH = db_path
    db.init_db()


class TestInitDb:
    def test_init_creates_tables(self) -> None:
        """init_db 应创建 tasks 和 results 表。"""
        import sqlite3

        conn = sqlite3.connect(db._DB_PATH)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "tasks" in tables
        assert "results" in tables

    def test_init_idempotent(self) -> None:
        """多次调用 init_db 不报错。"""
        db.init_db()
        db.init_db()


class TestCreateTask:
    def test_create_task_inserts_row(self) -> None:
        db.create_task("t1", "https://example.com", {"workers": 4}, "/tmp/out")
        task = db.get_task("t1")
        assert task is not None
        assert task["url"] == "https://example.com"
        assert task["status"] == "running"
        assert task["config"]["workers"] == 4

    def test_create_task_with_empty_config(self) -> None:
        db.create_task("t2", "https://test.com", {}, "/tmp")
        task = db.get_task("t2")
        assert task is not None
        assert task["config"] == {}


class TestUpdateTaskStatus:
    def test_update_status_only(self) -> None:
        db.create_task("u1", "https://x.com", {}, "/tmp")
        db.update_task_status("u1", "paused")
        task = db.get_task("u1")
        assert task["status"] == "paused"

    def test_update_with_all_fields(self) -> None:
        db.create_task("u2", "https://x.com", {}, "/tmp")
        db.update_task_status(
            "u2", "done", exit_code=0, log="done\n",
            total_resources=10, processed_resources=10,
            pages_scanned=3, current_url="https://x.com/page2",
        )
        task = db.get_task("u2")
        assert task["status"] == "done"
        assert task["exit_code"] == 0
        assert task["total_resources"] == 10
        assert task["processed_resources"] == 10
        assert task["pages_scanned"] == 3
        assert task["current_url"] == "https://x.com/page2"
        assert task["finished_at"] is not None

    def test_update_nonexistent_task_no_error(self) -> None:
        """更新不存在的任务不应报错。"""
        db.update_task_status("nonexistent", "done", exit_code=0)

    def test_log_truncated(self) -> None:
        """日志超过 80000 字符应被截断。"""
        db.create_task("u3", "https://x.com", {}, "/tmp")
        long_log = "x" * 100000
        db.update_task_status("u3", "done", log=long_log)
        task = db.get_task("u3")
        assert len(task["log"]) == 80000


class TestListTasks:
    def test_list_empty(self) -> None:
        result = db.list_tasks()
        assert result["tasks"] == []
        assert result["total"] == 0

    def test_list_with_tasks(self) -> None:
        for i in range(5):
            db.create_task(f"l{i}", f"https://l{i}.com", {}, "/tmp")
        result = db.list_tasks(page=1, page_size=3)
        assert len(result["tasks"]) == 3
        assert result["total"] == 5

    def test_list_status_filter(self) -> None:
        db.create_task("f1", "https://a.com", {}, "/tmp")
        db.create_task("f2", "https://b.com", {}, "/tmp")
        db.update_task_status("f2", "done", exit_code=0)
        result = db.list_tasks(status="done")
        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["id"] == "f2"

    def test_list_status_all(self) -> None:
        db.create_task("a1", "https://a.com", {}, "/tmp")
        db.update_task_status("a1", "done", exit_code=0)
        db.create_task("a2", "https://b.com", {}, "/tmp")
        result = db.list_tasks(status="all")
        assert result["total"] == 2

    def test_pagination(self) -> None:
        for i in range(10):
            db.create_task(f"p{i}", f"https://p{i}.com", {}, "/tmp")
        page1 = db.list_tasks(page=1, page_size=4)
        page2 = db.list_tasks(page=2, page_size=4)
        page3 = db.list_tasks(page=3, page_size=4)
        assert len(page1["tasks"]) == 4
        assert len(page2["tasks"]) == 4
        assert len(page3["tasks"]) == 2


class TestGetTask:
    def test_get_existing(self) -> None:
        db.create_task("g1", "https://g.com", {"k": "v"}, "/out")
        task = db.get_task("g1")
        assert task is not None
        assert task["url"] == "https://g.com"

    def test_get_nonexistent(self) -> None:
        assert db.get_task("nonexistent") is None


class TestDeleteTask:
    def test_delete_existing(self) -> None:
        db.create_task("d1", "https://d.com", {}, "/tmp")
        assert db.delete_task("d1") is True
        assert db.get_task("d1") is None

    def test_delete_nonexistent(self) -> None:
        assert db.delete_task("nonexistent") is False

    def test_delete_cascades_results(self, tmp_path: Path) -> None:
        # 先创建 manifest 文件
        jsonl = tmp_path / "resources_manifest.jsonl"
        jsonl.write_text(
            json.dumps({"url": "https://d.com/r.png", "bytes": 100, "status": "ok"}) + "\n",
            encoding="utf-8",
        )
        db.create_task("d2", "https://d.com", {}, str(tmp_path))
        db.import_results("d2", str(tmp_path))
        # 确认 results 存在
        r = db.get_results("d2")
        assert r["total"] > 0
        # 删除后 results 也应消失
        db.delete_task("d2")
        r = db.get_results("d2")
        assert r["total"] == 0


class TestImportResults:
    def test_import_from_jsonl(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "resources_manifest.jsonl"
        jsonl.write_text(
            json.dumps({"url": "https://a.com/img.png", "saved_path": "/out/a.png",
                        "content_type": "image/png", "bytes": 1024, "category": "image",
                        "found_in": "img", "kind": "resource", "page_url": "https://a.com",
                        "page_title": "A", "sha256": "abc", "status": "ok", "diagnostic": ""})
            + "\n"
            + json.dumps({"url": "https://a.com/style.css", "saved_path": "/out/a.css",
                          "content_type": "text/css", "bytes": 512, "category": "css",
                          "found_in": "link", "kind": "resource", "page_url": "https://a.com",
                          "page_title": "A", "sha256": "def", "status": "ok", "diagnostic": ""})
            + "\n",
            encoding="utf-8",
        )
        db.create_task("i1", "https://a.com", {}, str(tmp_path))
        count = db.import_results("i1", str(tmp_path))
        assert count == 2
        results = db.get_results("i1")
        assert results["total"] == 2

    def test_import_from_csv(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "resources_manifest.csv"
        csv_file.write_text(
            "url,saved_path,content_type,bytes,category,found_in,kind,page_url,page_title,sha256,status,diagnostic\n"
            "https://b.com/x.js,/out/x.js,application/javascript,2048,script,script,resource,https://b.com,B,ghi,ok,\n",
            encoding="utf-8",
        )
        db.create_task("i2", "https://b.com", {}, str(tmp_path))
        count = db.import_results("i2", str(tmp_path))
        assert count == 1

    def test_import_no_manifest(self, tmp_path: Path) -> None:
        db.create_task("i3", "https://c.com", {}, str(tmp_path))
        count = db.import_results("i3", str(tmp_path))
        assert count == 0


class TestGetResults:
    def test_get_results_empty(self) -> None:
        db.create_task("r1", "https://r.com", {}, "/tmp")
        result = db.get_results("r1")
        assert result["results"] == []
        assert result["total"] == 0

    def test_get_results_with_search(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "resources_manifest.jsonl"
        jsonl.write_text(
            json.dumps({"url": "https://a.com/image.png", "bytes": 100, "status": "ok"})
            + "\n"
            + json.dumps({"url": "https://a.com/script.js", "bytes": 200, "status": "ok"})
            + "\n",
            encoding="utf-8",
        )
        db.create_task("r2", "https://a.com", {}, str(tmp_path))
        db.import_results("r2", str(tmp_path))
        # 搜索 "image"
        result = db.get_results("r2", search="image")
        assert result["total"] == 1
        assert "image" in result["results"][0]["url"]
        # 搜索 "script"
        result = db.get_results("r2", search="script")
        assert result["total"] == 1

    def test_get_results_pagination(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "resources_manifest.jsonl"
        lines = [
            json.dumps({"url": f"https://x.com/r{i}.png", "bytes": i, "status": "ok"})
            for i in range(60)
        ]
        jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
        db.create_task("r3", "https://x.com", {}, str(tmp_path))
        db.import_results("r3", str(tmp_path))
        page1 = db.get_results("r3", page=1, page_size=50)
        page2 = db.get_results("r3", page=2, page_size=50)
        assert len(page1["results"]) == 50
        assert len(page2["results"]) == 10

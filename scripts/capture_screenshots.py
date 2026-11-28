"""Capture fresh README screenshots for the web-crawler local Web UI.

Starts ``web_crawler.app.ui`` against an isolated temporary SQLite database
pre-populated with realistic sample crawl tasks so all 4 tabs (Crawl Console,
Captcha Solver, JS Reverse Agent, and Task History) render cleanly at desktop
resolution (1440x920).

Usage:
    python scripts/capture_screenshots.py
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "docs" / "screenshots"
VIEWPORT = {"width": 1440, "height": 920}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"Web UI did not start in time: {url}")


def _seed_sample_db(db_path: Path) -> None:
    """Populate a temporary SQLite DB with realistic completed tasks."""
    now = time.time()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                config TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                exit_code INTEGER,
                error_message TEXT DEFAULT '',
                summary TEXT NOT NULL DEFAULT '{}',
                log TEXT DEFAULT '',
                total_resources INTEGER DEFAULT 0,
                processed_resources INTEGER DEFAULT 0,
                pages_scanned INTEGER DEFAULT 0,
                current_url TEXT DEFAULT '',
                created_at REAL NOT NULL,
                finished_at REAL
            );
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                url TEXT NOT NULL,
                saved_path TEXT DEFAULT '',
                content_type TEXT DEFAULT '',
                bytes INTEGER DEFAULT 0,
                category TEXT DEFAULT '',
                found_in TEXT DEFAULT '',
                kind TEXT DEFAULT '',
                page_url TEXT DEFAULT '',
                page_title TEXT DEFAULT '',
                sha256 TEXT DEFAULT '',
                status TEXT DEFAULT '',
                diagnostic TEXT DEFAULT ''
            );
            """
        )
        sample_tasks = [
            (
                "demo-docs-01",
                "https://docs.python.org/3/library/asyncio.html",
                json.dumps(
                    {
                        "stealth": True,
                        "extract_markdown": True,
                        "respect_robots": True,
                        "workers": 8,
                    },
                    ensure_ascii=False,
                ),
                "./crawler_output/asyncio-docs",
                "done",
                0,
                "",
                json.dumps(
                    {
                        "pages_scanned": 12,
                        "resources_found": 48,
                        "succeeded": 48,
                        "failed": 0,
                        "bytes": 1_842_910,
                    }
                ),
                "[info] 扫描完成：12 页 · 48 项资源 · 已生成 Markdown 与离线报告",
                48,
                48,
                12,
                "https://docs.python.org/3/library/asyncio.html",
                now - 1800,
                now - 1786,
            ),
            (
                "demo-spa-02",
                "https://quotes.toscrape.com/js/",
                json.dumps(
                    {
                        "render_js": True,
                        "extract_text": True,
                        "rewrite_html": True,
                        "workers": 4,
                    },
                    ensure_ascii=False,
                ),
                "./crawler_output/quotes-spa",
                "done",
                0,
                "",
                json.dumps(
                    {
                        "pages_scanned": 5,
                        "resources_found": 23,
                        "succeeded": 23,
                        "failed": 0,
                        "bytes": 612_400,
                    }
                ),
                "[info] JS 渲染完成：5 页 · 离线重写验收通过",
                23,
                23,
                5,
                "https://quotes.toscrape.com/js/",
                now - 7200,
                now - 7180,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO tasks (
                id, url, config, output_dir, status, exit_code, error_message,
                summary, log, total_resources, processed_resources, pages_scanned,
                current_url, created_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            sample_tasks,
        )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}/"

    with tempfile.TemporaryDirectory(prefix="wc-shots-", ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "crawler_shots.db"
        _seed_sample_db(db_path)

        env = os.environ.copy()
        env["CRAWLER_DB_PATH"] = str(db_path)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        env["PYTHONUTF8"] = "1"
        env.pop("WEB_CRAWLER_POWER_MODE", None)

        srv = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "web_crawler.app.ui",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_http(base_url)
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(channel="msedge")
                except Exception:
                    browser = pw.chromium.launch()
                page = browser.new_page(viewport=VIEWPORT)
                page.goto(base_url, wait_until="networkidle")
                page.wait_for_timeout(700)

                # 1) 网页采集主控制台（双栏桌面布局）
                page.fill("#url", "https://docs.python.org/3/library/asyncio.html")
                page.screenshot(path=str(OUT_DIR / "ui-console.png"))
                print("wrote ui-console.png")

                # 2) JS 逆向 Agent
                page.click("#tabbtn-reverse")
                page.wait_for_timeout(500)
                page.screenshot(path=str(OUT_DIR / "ui-reverse.png"))
                print("wrote ui-reverse.png")

                # 3) 验证码识别
                page.click("#tabbtn-captcha")
                page.wait_for_timeout(500)
                page.screenshot(path=str(OUT_DIR / "ui-captcha.png"))
                print("wrote ui-captcha.png")

                # 4) 任务历史（含预置完成记录）
                page.click("#tabbtn-history")
                page.wait_for_timeout(800)
                page.screenshot(path=str(OUT_DIR / "ui-history.png"))
                print("wrote ui-history.png")

                browser.close()
        finally:
            srv.terminate()
            srv.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

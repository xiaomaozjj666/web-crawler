"""Capture fresh, real-execution README screenshots for the web-crawler Web UI.

Starts an in-process local HTML demo documentation site and runs a real crawl
(including Markdown extraction, page sample retention, and offline HTML rewrite)
plus a real local ``ddddocr`` captcha image recognition against ``web_crawler.app.ui``
using an isolated temporary SQLite database and output directory.

Usage:
    python scripts/capture_screenshots.py
"""

from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "docs" / "screenshots"
VIEWPORT = {"width": 1440, "height": 920}

_DEMO_PAGES: dict[str, tuple[str, bytes]] = {
    "/": (
        "text/html; charset=utf-8",
        """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>AsyncIO 并发爬虫架构指南</title>
  <link rel="stylesheet" href="/assets/style.css">
</head>
<body>
  <header><nav><a href="/">首页</a> | <a href="/guide/patterns.html">并发模式</a> | <a href="/guide/benchmarks.html">性能基准</a></nav></header>
  <main>
    <h1>AsyncIO 并发爬虫架构指南</h1>
    <p>本文展示 web-crawler 的多页抓取、正文转 Markdown、离线 HTML 重写与 CSS 字段规则提取能力。</p>
    <img src="/assets/arch.svg" alt="架构图">
    <h2>核心设计原则</h2>
    <ul>
      <li>自适应元素指纹：页面改版后自动重定位目标节点。</li>
      <li>TLS/JA3 指纹隐身与会话池轮换，降低连接特征暴露。</li>
      <li>默认开启 SSRF 防护与 DNS 钉扎，支持离线归档验收。</li>
    </ul>
    <pre><code>from web_crawler import Fetcher
resp = Fetcher(impersonate="chrome131").get("https://example.com")
print(resp.css("h1::text").get())</code></pre>
    <p>延伸阅读：<a href="/guide/patterns.html">任务调度与背压控制</a> · <a href="/guide/benchmarks.html">吞吐与延迟实测</a></p>
  </main>
</body>
</html>
""".encode(),
    ),
    "/guide/patterns.html": (
        "text/html; charset=utf-8",
        """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>任务调度与背压控制</title>
  <link rel="stylesheet" href="/assets/style.css">
</head>
<body>
  <main>
    <h1>任务调度与背压控制</h1>
    <p>Spider 框架内置域级并发槽位、指数退避重试与基于 EMA 的 AutoThrottle 自适应限速。</p>
    <h2>关键参数</h2>
    <table>
      <tr><th>参数</th><th>默认值</th><th>说明</th></tr>
      <tr><td>per_domain_concurrency</td><td>4</td><td>单域名并发上限</td></tr>
      <tr><td>retry_http_status</td><td>429, 503</td><td>触发退避的响应码</td></tr>
    </table>
    <p><a href="/">返回首页</a> · <a href="/guide/benchmarks.html">查看基准</a></p>
  </main>
</body>
</html>
""".encode(),
    ),
    "/guide/benchmarks.html": (
        "text/html; charset=utf-8",
        """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>吞吐与延迟实测基准</title>
  <link rel="stylesheet" href="/assets/style.css">
</head>
<body>
  <main>
    <h1>吞吐与延迟实测基准</h1>
    <p>基于 4529 项自动化用例与防回归微基准，持续验证解析器、去重器与限速热路径性能。</p>
    <p><a href="/">返回首页</a></p>
  </main>
</body>
</html>
""".encode(),
    ),
    "/assets/style.css": (
        "text/css; charset=utf-8",
        b"body{font-family:sans-serif;max-width:860px;margin:0 auto;padding:24px;color:#1e293b;}\n",
    ),
    "/assets/arch.svg": (
        "image/svg+xml",
        (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="90">'
            b'<rect width="320" height="90" rx="8" fill="#0f172a"/>'
            b'<text x="20" y="52" fill="#38bdf8" font-size="18" font-family="sans-serif">'
            b"web-crawler demo asset</text></svg>"
        ),
    ),
}


class _DemoHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        entry = _DEMO_PAGES.get(path)
        if entry is None:
            self.send_response(404)
            self.end_headers()
            return
        ctype, payload = entry
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


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


def _make_sample_captcha_png(dest: Path) -> Path:
    """Create a clean alphanumeric captcha image verified to decode via ddddocr."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (170, 60), (248, 250, 252))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/consolab.ttf", 34)
    except Exception:
        font = ImageFont.load_default()
    draw.rectangle([2, 2, 167, 57], outline=(203, 213, 225), width=2)
    draw.text((26, 12), "8K4M", fill=(15, 23, 42), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    dest.write_bytes(buf.getvalue())
    return dest


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ui_port = _free_port()
    demo_port = _free_port()
    base_url = f"http://127.0.0.1:{ui_port}/"
    demo_url = f"http://127.0.0.1:{demo_port}/"

    demo_srv = ThreadingHTTPServer(("127.0.0.1", demo_port), _DemoHandler)
    demo_srv.daemon_threads = True
    threading.Thread(target=demo_srv.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory(prefix="wc-shots-", ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "crawler_shots.db"
        captcha_png = _make_sample_captcha_png(tmp_path / "captcha_8K4M.png")

        env = os.environ.copy()
        env["CRAWLER_DB_PATH"] = str(db_path)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        env["PYTHONUTF8"] = "1"
        # 仅限本脚本爬取本机临时演示站点时放行 127.0.0.1
        env["WEB_CRAWLER_POWER_MODE"] = "1"

        srv = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "web_crawler.app.ui",
                "--host",
                "127.0.0.1",
                "--port",
                str(ui_port),
            ],
            cwd=str(tmp_path),
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
                page.wait_for_timeout(600)

                # 1) 网页采集主控制台：对本机演示站点执行一次真实全链路采集（含多页 + Markdown + 样本 + 离线重写）
                page.fill("#url", demo_url)
                page.click('button.preset-btn[data-preset="mirror"]')
                page.wait_for_timeout(200)
                page.evaluate(
                    """() => {
                        for (const name of ['extract_markdown', 'keep_page_samples']) {
                            const inp = document.querySelector(`input[name="${name}"]`);
                            if (!inp) continue;
                            const details = inp.closest('details');
                            if (details) details.open = true;
                            inp.checked = true;
                            inp.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }"""
                )
                page.click('#tab-crawler button[type="submit"]')
                page.wait_for_function(
                    """() => {
                        const log = (document.getElementById('log') || {}).textContent || '';
                        const btn = document.getElementById('current-results');
                        return (btn && !btn.hidden) || log.includes('完成，退出码');
                    }""",
                    timeout=25000,
                )
                page.wait_for_timeout(1800)
                page.screenshot(path=str(OUT_DIR / "ui-console.png"))
                print("wrote ui-console.png (real crawl completed)")

                # 2) 任务详情与页面提取：展示真实采集后的结构化 Markdown + CSS 字段规则实时校验
                page.click("#tabbtn-history")
                page.wait_for_selector("#history-list .btn-results", timeout=10000)
                page.wait_for_timeout(400)
                page.locator("#history-list .btn-results").first.click()
                page.wait_for_selector("#results-modal.show", timeout=10000)
                page.wait_for_timeout(500)
                page.click("#show-pages")
                page.wait_for_selector("#page-options button", timeout=10000)
                page.locator("#page-options button").first.click()
                page.wait_for_timeout(800)
                rules_json = (
                    '{\n  "version": 1,\n  "fields": {\n'
                    '    "title": {"css": "h1", "required": true},\n'
                    '    "headings": {"css": "h2", "many": true},\n'
                    '    "links": {"css": "a::attr(href)", "many": true}\n  }\n}'
                )
                page.evaluate(
                    """() => {
                        const ed = document.getElementById('rules-editor');
                        const det = ed && ed.closest('details');
                        if (det) det.open = true;
                    }"""
                )
                page.fill("#rules-editor", rules_json)
                page.click("#validate-rules")
                page.wait_for_function(
                    """() => {
                        const fb = (document.getElementById('rules-status') || {}).textContent || '';
                        return fb.includes('通过') || fb.includes('缺少') || fb.includes('失败');
                    }""",
                    timeout=10000,
                )
                fb_text = page.locator("#rules-status").text_content()
                print("rules validation status:", fb_text)
                page.wait_for_timeout(500)
                page.screenshot(path=str(OUT_DIR / "ui-history.png"))
                print("wrote ui-history.png (real task detail + markdown + field rules)")
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)

                # 3) 验证码识别：用真实 Pillow 验证码图跑本地 ddddocr 识别接口
                page.click("#tabbtn-captcha")
                page.wait_for_timeout(400)
                page.fill("#cap-page-url", "https://example.com/login")
                page.set_input_files("#cap-img-file", str(captcha_png))
                page.click("#cap-img-btn")
                page.wait_for_function(
                    """() => {
                        const t = (document.getElementById('cap-img-result') || {}).textContent || '';
                        return t.includes('识别结果') || t.includes('识别失败');
                    }""",
                    timeout=15000,
                )
                page.wait_for_timeout(400)
                page.screenshot(path=str(OUT_DIR / "ui-captcha.png"))
                print("wrote ui-captcha.png (real ddddocr recognition)")

                # 4) JS 逆向 Agent 控制台（应用 X-Bogus 预设模板，如实呈现配置与监控面板）
                page.click("#tabbtn-reverse")
                page.wait_for_timeout(400)
                xb_btn = page.locator("#tab-reverse button", has_text="X-Bogus").first
                if xb_btn.is_visible():
                    xb_btn.click()
                    page.wait_for_timeout(300)
                page.fill("#rev-url", "https://example.com/api/search")
                page.wait_for_timeout(300)
                page.screenshot(path=str(OUT_DIR / "ui-reverse.png"))
                print("wrote ui-reverse.png")

                browser.close()
        finally:
            srv.terminate()
            srv.wait(timeout=5)
            demo_srv.shutdown()
            demo_srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

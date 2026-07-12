#!/usr/bin/env python3
"""Local web UI for crawler.py."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.dont_write_bytecode = True

import crawler as web_resource_crawler  # noqa: E402  # 需先设 sys.dont_write_bytecode 再导入

HOST = "127.0.0.1"
PORT = 8765
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = os.path.join(os.getcwd(), "crawler_output")
DEFAULT_BLOCK_KEYWORDS = (
    "ads, adservice, adserver, adclick, doubleclick, googlesyndication, "
    "google-analytics, banner, promo, promotion, popup, popunder, modal, "
    "overlay, interstitial, floating, float-ad, layer-ad, dialog-ad, lightbox, "
    "subscribe, webpush, push-notification, affiliate, tracker, tracking, "
    "analytics, tongji, stat, hm.baidu, cnzz, umeng, "
    "recaptcha, captcha, hcaptcha, turnstile, challenge, verification, "
    "verify, security-check, bot-detect, botdetect"
)
JOBS: dict[str, JobState] = {}
JOBS_LOCK = threading.Lock()
MAX_JOBS = 50


@dataclass
class JobState:
    id: str
    args: object
    output_dir: str
    log: str = ""
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    total_resources: int = 0
    processed_resources: int = 0
    current_url: str = ""
    pages_scanned: int = 0
    exit_code: int | None = None
    pause_event: threading.Event = field(default_factory=threading.Event)
    stop_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.pause_event.set()

    def append(self, text: str) -> None:
        with self.lock:
            self.log += text
            self.log = self.log[-80000:]

    def progress(self, payload: dict[str, object]) -> None:
        with self.lock:
            self.total_resources = int(payload.get("total_resources", self.total_resources) or 0)
            self.processed_resources = int(
                payload.get("processed_resources", self.processed_resources) or 0
            )
            self.current_url = str(payload.get("current_url", self.current_url) or "")
            self.pages_scanned = int(payload.get("pages_scanned", self.pages_scanned) or 0)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            percent = 0
            if self.total_resources:
                percent = min(100, int(self.processed_resources * 100 / self.total_resources))
            return {
                "id": self.id,
                "status": self.status,
                "log": self.log,
                "total_resources": self.total_resources,
                "processed_resources": self.processed_resources,
                "current_url": self.current_url,
                "pages_scanned": self.pages_scanned,
                "percent": percent,
                "output_dir": self.output_dir,
                "exit_code": self.exit_code,
            }


class JobWriter(io.StringIO):
    def __init__(self, job: JobState) -> None:
        super().__init__()
        self.job = job

    def write(self, text: str) -> int:
        self.job.append(text)
        return len(text)

    def flush(self) -> None:
        return


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>网页资源采集器</title>
  <style>
    :root {
      --bg: #f6f7f9; --card: #fff; --border: #e3e7ee; --text: #111827;
      --muted: #5b6472; --input-bg: #fff; --log-bg: #111827; --log-text: #e5e7eb;
      --primary: #2563eb; --primary-hover: #1d4ed8; --danger: #b91c1c;
      --bar-bg: #e5e7eb; --bar-fill: #16a34a;
    }
    [data-theme="dark"] {
      --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #f1f5f9;
      --muted: #94a3b8; --input-bg: #1e293b; --log-bg: #020617; --log-text: #e2e8f0;
      --primary: #3b82f6; --primary-hover: #60a5fa; --danger: #ef4444;
      --bar-bg: #334155; --bar-fill: #22c55e;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-family: "Microsoft YaHei", Arial, sans-serif; }
    main { max-width: 980px; margin: 28px auto; padding: 0 18px; }
    h1 { font-size: 28px; margin: 0 0 4px; display: flex; align-items: center; gap: 10px; }
    .subtitle { color: var(--muted); margin: 0 0 18px; font-size: 14px; }
    .theme-toggle { margin-left: auto; background: none; border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; cursor: pointer; color: var(--text); font-size: 13px; }
    .tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px; }
    .tag { background: var(--card); border: 1px solid var(--border); border-radius: 4px; padding: 2px 8px; font-size: 12px; color: var(--muted); }
    form { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
    label { display: block; font-weight: 700; margin: 14px 0 8px; font-size: 14px; }
    input[type=text], input[type=number], textarea {
      width: 100%; padding: 11px 12px; border: 1px solid var(--border);
      border-radius: 6px; font-size: 15px; background: var(--input-bg); color: var(--text); font-family: inherit;
    }
    textarea { min-height: 72px; resize: vertical; font-family: Consolas, monospace; font-size: 13px; }
    .row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
    .grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px 18px; margin-top: 14px; }
    .check { display: flex; align-items: center; gap: 8px; font-weight: 500; color: var(--text); font-size: 14px; cursor: pointer; }
    .check input { width: 16px; height: 16px; cursor: pointer; }
    .section { margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--border); }
    .buttons { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
    button { border: 0; border-radius: 6px; padding: 11px 16px; font-size: 15px; font-weight: 700; cursor: pointer; }
    .btn-primary { background: var(--primary); color: white; }
    .btn-primary:hover { background: var(--primary-hover); }
    .btn-secondary { background: #475569; color: white; }
    .btn-danger { background: var(--danger); color: white; }
    button:disabled { opacity: .5; cursor: not-allowed; }
    .progress-wrap { margin-top: 18px; background: var(--bar-bg); border-radius: 999px; height: 16px; overflow: hidden; }
    .progress-bar { width: 0%; height: 100%; background: var(--bar-fill); transition: width .25s ease; border-radius: 999px; }
    .status { margin-top: 10px; color: var(--muted); font-size: 14px; overflow-wrap: anywhere; }
    pre { margin-top: 14px; padding: 14px; min-height: 190px; max-height: 500px; overflow: auto; background: var(--log-bg); color: var(--log-text); border-radius: 8px; font-size: 13px; line-height: 1.55; }
    .hint { margin-top: 6px; font-size: 13px; color: var(--muted); }
    @media (max-width: 760px) { .row, .grid { grid-template-columns: 1fr 1fr; } }
    @media (max-width: 480px) { .row, .grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <main>
    <h1>
      网页资源采集器
      <button class="theme-toggle" type="button" onclick="toggleTheme()" title="切换主题">主题</button>
    </h1>
    <p class="subtitle">图片/CSS/JS/视频/字体 · 并发下载 · 自适应限速 · 内容去重</p>
    <div class="tags">
      <span class="tag">并发</span>
      <span class="tag">限速</span>
      <span class="tag">去重</span>
      <span class="tag">断点续传</span>
      <span class="tag">清单</span>
      <span class="tag">离线页面</span>
    </div>

    <form id="crawler-form">
      <label for="url">网页 URL</label>
      <input id="url" name="url" type="text" placeholder="https://example.com" required>

      <label for="out">保存目录</label>
      <input id="out" name="out" type="text" value="">

      <div class="row">
        <div><label for="max_pages">最多扫描页面</label><input id="max_pages" name="max_pages" type="number" min="1" value="1"></div>
        <div><label for="workers">并发线程数</label><input id="workers" name="workers" type="number" min="1" max="64" value="8"></div>
        <div><label for="delay">请求间隔秒数</label><input id="delay" name="delay" type="number" min="0" step="0.1" value="0.3"></div>
        <div><label for="retries">失败重试</label><input id="retries" name="retries" type="number" min="0" value="2"></div>
      </div>
      <div class="row" style="margin-top:14px">
        <div><label for="timeout">超时秒数</label><input id="timeout" name="timeout" type="number" min="1" value="30"></div>
        <div><label for="max_bytes">单文件上限（字节）</label><input id="max_bytes" name="max_bytes" type="number" min="0" value="0"></div>
        <div></div><div></div>
      </div>

      <div class="section">
        <label for="cookie">Cookie</label>
        <textarea id="cookie" name="cookie" placeholder="name=value; name2=value2"></textarea>
        <div class="hint">只填你自己的授权 Cookie。</div>

        <label for="referer">Referer</label>
        <input id="referer" name="referer" type="text" placeholder="留空时默认使用网页 URL">

        <label for="headers">额外请求头（每行一个）</label>
        <textarea id="headers" name="headers" placeholder="Authorization: Bearer ...&#10;Accept-Language: zh-CN,zh;q=0.9"></textarea>

        <label for="block_keywords">过滤关键词 / 域名</label>
        <textarea id="block_keywords" name="block_keywords">{block_keywords}</textarea>
        <div class="hint">URL 含这些关键词则跳过。已内置常见广告和验证服务关键词。</div>
      </div>

      <div class="grid">
        <label class="check"><input type="checkbox" name="same_domain" checked> 只抓同域名</label>
        <label class="check"><input type="checkbox" name="include_css_urls" checked> 抓 CSS 内资源</label>
        <label class="check"><input type="checkbox" name="video_mode"> 生成视频资源清单</label>
        <label class="check"><input type="checkbox" name="video_only"> 只处理视频相关资源</label>
        <label class="check"><input type="checkbox" name="list_only" checked> 只生成清单，不下载</label>
        <label class="check"><input type="checkbox" name="expand_playlists"> 展开播放清单</label>
        <label class="check"><input type="checkbox" name="crawl_pages"> 扫描站内页面</label>
        <label class="check"><input type="checkbox" name="respect_robots" checked> 遵守 robots.txt</label>
        <label class="check"><input type="checkbox" name="resume" checked> 断点续传</label>
        <label class="check"><input type="checkbox" name="organize" checked> 自动分类重命名</label>
        <label class="check"><input type="checkbox" name="dedup" checked> 内容去重（SHA256）</label>
        <label class="check"><input type="checkbox" name="sitemap"> 从 Sitemap 发现页面</label>
        <label class="check"><input type="checkbox" name="strip_overlays" checked> 移除遮挡层</label>
        <label class="check"><input type="checkbox" name="decrypt"> AES 解密</label>
        <label class="check"><input type="checkbox" name="smart_extract"> 智能数据提取</label>
        <label class="check"><input type="checkbox" name="resume_crawl"> 断点续爬</label>
        <label class="check"><input type="checkbox" name="extract_text"> 正文提取</label>
        <label class="check"><input type="checkbox" name="stealth"> 隐身抓取（TLS 指纹）</label>
      </div>

      <div class="buttons">
        <button id="run-button" type="submit" class="btn-primary">开始整理</button>
        <button id="pause-button" type="button" class="btn-secondary" disabled>暂停</button>
        <button id="resume-button" type="button" class="btn-secondary" disabled>继续</button>
        <button id="cancel-button" type="button" class="btn-danger" disabled>取消</button>
        <button id="open-button" type="button" class="btn-secondary">打开输出文件夹</button>
      </div>
    </form>

    <div class="progress-wrap"><div id="progress-bar" class="progress-bar"></div></div>
    <div id="status" class="status">等待开始...</div>
    <pre id="log">等待开始...</pre>
  </main>
  <script>
    (function() {
      var theme = localStorage.getItem('crawler-theme');
      if (theme === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
    })();

    function toggleTheme() {
      var html = document.documentElement;
      var isDark = html.getAttribute('data-theme') === 'dark';
      if (isDark) {
        html.removeAttribute('data-theme');
        localStorage.setItem('crawler-theme', 'light');
      } else {
        html.setAttribute('data-theme', 'dark');
        localStorage.setItem('crawler-theme', 'dark');
      }
    }

    var form = document.getElementById('crawler-form');
    var log = document.getElementById('log');
    var statusEl = document.getElementById('status');
    var bar = document.getElementById('progress-bar');
    var runButton = document.getElementById('run-button');
    var pauseButton = document.getElementById('pause-button');
    var resumeButton = document.getElementById('resume-button');
    var cancelButton = document.getElementById('cancel-button');
    var openButton = document.getElementById('open-button');
    var jobId = null;
    var timer = null;

    async function post(path, data) {
      var response = await fetch(path, { method: 'POST', body: data || new URLSearchParams() });
      return await response.json();
    }
    function setRunning(active) {
      runButton.disabled = active;
      pauseButton.disabled = !active;
      cancelButton.disabled = !active;
    }
    async function poll() {
      if (!jobId) return;
      var response = await fetch('/status?id=' + encodeURIComponent(jobId));
      var result = await response.json();
      log.textContent = result.log || '';
      bar.style.width = (result.percent || 0) + '%';
      statusEl.textContent = (result.status || '') + ' | ' + (result.processed_resources || 0) + '/' + (result.total_resources || 0) + ' | 页面 ' + (result.pages_scanned || 0) + ' | ' + (result.current_url || '');
      if (result.status === 'paused') {
        pauseButton.disabled = true;
        resumeButton.disabled = false;
      } else {
        resumeButton.disabled = true;
      }
      if (['done', 'error', 'cancelled'].indexOf(result.status) >= 0) {
        clearInterval(timer);
        setRunning(false);
        pauseButton.disabled = true;
        resumeButton.disabled = true;
      }
    }
    form.addEventListener('submit', async function(event) {
      event.preventDefault();
      setRunning(true);
      resumeButton.disabled = true;
      log.textContent = '正在启动任务...\\n';
      bar.style.width = '0%';
      var result = await post('/run', new URLSearchParams(new FormData(form)));
      jobId = result.id;
      if (timer) clearInterval(timer);
      timer = setInterval(poll, 1000);
      await poll();
    });
    pauseButton.onclick = async function() { if (jobId) { await post('/pause?id=' + encodeURIComponent(jobId)); await poll(); } };
    resumeButton.onclick = async function() { if (jobId) { await post('/resume?id=' + encodeURIComponent(jobId)); await poll(); } };
    cancelButton.onclick = async function() { if (jobId) { await post('/cancel?id=' + encodeURIComponent(jobId)); await poll(); } };
    openButton.onclick = async function() {
      var data = new URLSearchParams();
      data.set('out', document.getElementById('out').value);
      var result = await post('/open-output', data);
      statusEl.textContent = result.message;
    };
  </script>
</body>
</html>
"""


def output_path(value: str) -> str:
    path = Path(value or DEFAULT_OUTPUT)
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path.resolve())


def header_values(form: dict[str, list[str]]) -> list[str]:
    values: list[str] = []
    cookie = form.get("cookie", [""])[0].strip()
    referer = form.get("referer", [""])[0].strip() or form.get("url", [""])[0].strip()
    extra = form.get("headers", [""])[0]
    if cookie:
        values.append(f"Cookie: {cookie}")
    if referer:
        values.append(f"Referer: {referer}")
    for line in extra.splitlines():
        line = line.strip()
        if line:
            values.append(line)
    return values


def build_args(form: dict[str, list[str]]) -> object:
    def value(name: str, default: str = "") -> str:
        return form.get(name, [default])[0]

    def checked(name: str) -> bool:
        return name in form

    out_val = value("out", "")
    if not out_val:
        out_val = DEFAULT_OUTPUT
    args = web_resource_crawler.build_parser().parse_args(
        [
            "--url",
            value("url"),
            "--out",
            output_path(out_val),
            "--max-pages",
            value("max_pages", "1"),
            "--workers",
            value("workers", "8"),
            "--delay",
            value("delay", "0.5"),
            "--timeout",
            value("timeout", "30"),
            "--retries",
            value("retries", "2"),
            "--max-bytes",
            value("max_bytes", "0"),
            "--block-keyword",
            value("block_keywords", DEFAULT_BLOCK_KEYWORDS),
        ]
    )
    args.header = header_values(form)
    args.same_domain = checked("same_domain")
    args.include_css_urls = checked("include_css_urls")
    args.video_mode = checked("video_mode")
    args.video_only = checked("video_only")
    args.list_only = checked("list_only")
    args.expand_playlists = checked("expand_playlists")
    args.resume = checked("resume")
    args.organize = checked("organize")
    args.dedup = checked("dedup")
    args.sitemap = checked("sitemap")
    args.strip_overlays = checked("strip_overlays")
    args.decrypt = checked("decrypt")
    args.smart_extract = checked("smart_extract")
    args.resume_crawl = checked("resume_crawl")
    args.extract_text = checked("extract_text")
    args.rewrite_html = checked("strip_overlays")
    args.crawl_pages = checked("crawl_pages")
    args.respect_robots = checked("respect_robots")
    args.stealth = checked("stealth")
    args.save_config = ""
    args.load_config = ""
    return args


def run_job(job: JobState) -> None:
    job.args.wait_if_paused = lambda: wait_for_resume(job)
    job.args.should_stop = job.stop_event.is_set
    job.args.progress_callback = job.progress
    writer = JobWriter(job)
    try:
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            code = web_resource_crawler.crawl(job.args)
        job.exit_code = code
        job.status = "cancelled" if job.stop_event.is_set() else "done"
        report_html = Path(job.output_dir) / "run_report.html"
        report_md = Path(job.output_dir) / "run_report.md"
        job.append(f"\n完成，退出码：{code}\n输出目录：{job.output_dir}\n")
        if report_html.exists():
            job.append(f"可视化报告：{report_html}\n")
        if report_md.exists():
            job.append(f"Markdown 报告：{report_md}\n")
    except Exception as exc:
        job.exit_code = 1
        job.status = "error"
        job.append(f"\n任务出错：{exc}\n")


def wait_for_resume(job: JobState) -> None:
    while not job.pause_event.is_set():
        job.status = "paused"
        if job.stop_event.is_set():
            raise RuntimeError("cancelled by user")
        time.sleep(0.2)
    if job.status == "paused":
        job.status = "running"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in {"/", "/index.html"}:
            self.respond(
                200,
                PAGE.replace("{block_keywords}", DEFAULT_BLOCK_KEYWORDS).encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return
        if self.path.startswith("/status"):
            query = parse_qs(urlparse(self.path).query)
            job = JOBS.get(query.get("id", [""])[0])
            if not job:
                self.respond_json({"status": "missing", "log": "任务不存在"})
                return
            self.respond_json(job.snapshot())
            return
        self.send_error(404)

    def do_POST(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        path = urlparse(self.path).path
        if path == "/run":
            form = self.read_form()
            args = build_args(form)
            job_id = uuid.uuid4().hex[:12]
            job = JobState(id=job_id, args=args, output_dir=str(Path(args.out).resolve()))
            with JOBS_LOCK:
                JOBS[job_id] = job
                # 清理已完成的任务，防止内存泄漏
                if len(JOBS) > MAX_JOBS:
                    for jid in list(JOBS.keys()):
                        j = JOBS[jid]
                        if j.status in ("done", "error", "cancelled"):
                            del JOBS[jid]
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
            self.respond_json({"id": job_id, "status": "running"})
            return
        if path in {"/pause", "/resume", "/cancel"}:
            job = JOBS.get(query.get("id", [""])[0])
            if not job:
                self.respond_json({"ok": False, "message": "任务不存在"})
                return
            if path == "/pause":
                job.pause_event.clear()
                job.status = "paused"
            elif path == "/resume":
                job.pause_event.set()
                job.status = "running"
            elif path == "/cancel":
                job.stop_event.set()
                job.pause_event.set()
                job.status = "cancelled"
            self.respond_json({"ok": True})
            return
        if path == "/open-output":
            form = self.read_form()
            out_dir = Path(output_path(form.get("out", [DEFAULT_OUTPUT])[0]))
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.startfile(str(out_dir))
                self.respond_json({"ok": True, "message": f"已打开：{out_dir}"})
            except Exception as exc:
                self.respond_json({"ok": False, "message": f"无法打开：{exc}"})
            return
        self.send_error(404)

    def read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(body)

    def respond_json(self, payload: dict[str, object]) -> None:
        self.respond(
            200,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    import argparse as _ap

    _parser = _ap.ArgumentParser(description="Web Resource Crawler UI")
    _parser.add_argument("--open", action="store_true", help="Automatically open browser")
    _parser.add_argument("--port", type=int, default=PORT, help=f"Server port (default: {PORT})")
    _args = _parser.parse_args()

    port = _args.port
    server = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}"
    if _args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print(f"Web UI started: {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()

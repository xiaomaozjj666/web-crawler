#!/usr/bin/env python3
"""Local web UI for crawler.py."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

sys.dont_write_bytecode = True
# 确保 app 目录在 sys.path 中，无论作为脚本运行还是作为模块导入
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
_log = logging.getLogger(__name__)

import crawler as web_resource_crawler  # 需先设 sys.dont_write_bytecode 再导入

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

# JS 逆向 Agent 任务注册表
REVERSE_JOBS: dict[str, ReverseJobState] = {}
REVERSE_JOBS_LOCK = threading.Lock()
MAX_REVERSE_JOBS = 20


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


@dataclass
class ReverseJobState:
    """JS 逆向 Agent 任务的实时状态。"""

    id: str
    url: str
    task: str
    config: dict  # ReverseAgentConfig 的可序列化形式
    status: str = "running"  # running / done / error / cancelled
    created_at: float = field(default_factory=time.time)
    current_step: int = 0
    max_steps: int = 20

    # 实时事件流（保留最近 200 条，避免内存爆炸）
    events: list[dict] = field(default_factory=list)
    events_lock: threading.Lock = field(default_factory=threading.Lock)

    # 步骤列表（每个 step 一条，含 action_type/reasoning/duration/tokens/confidence）
    steps: list[dict] = field(default_factory=list)

    # 当前观察
    current_observation: dict = field(default_factory=dict)

    # 预算
    budget_used: int = 0
    budget_total: int = 100_000
    budget_per_step: int = 8_000
    budget_step_used: int = 0

    # 置信度
    last_confidence: dict = field(default_factory=dict)  # {score, reasons, action_type}

    # 护栏
    guard_blocks: list[dict] = field(default_factory=list)

    # Hook 捕获
    hook_records: list[dict] = field(default_factory=list)
    hook_count: int = 0
    network_requests: list[dict] = field(default_factory=list)

    # 目标参数发现状态
    target_params: list[str] = field(default_factory=list)
    target_params_found: dict[str, str] = field(default_factory=dict)

    # Checkpoints
    checkpoints: list[dict] = field(default_factory=list)

    # 最终结果
    success: bool = False
    analysis: str = ""
    compiled_script: str = ""
    judge_result: dict = field(default_factory=dict)

    # 控制
    stop_event: threading.Event = field(default_factory=threading.Event)
    exit_code: int | None = None
    error: str = ""

    # 内部：步号 → 起始时间戳，用于计算单步耗时
    _step_starts: dict = field(default_factory=dict)

    def append_event(self, event: dict) -> None:
        """追加一条事件到流，保留最近 200 条。"""
        with self.events_lock:
            self.events.append(event)
            if len(self.events) > 200:
                self.events = self.events[-200:]

    def snapshot(self) -> dict[str, object]:
        """返回可 JSON 序列化的完整状态快照。"""
        with self.events_lock:
            events_copy = list(self.events)
        return {
            "id": self.id,
            "url": self.url,
            "task": self.task,
            "status": self.status,
            "created_at": self.created_at,
            "current_step": self.current_step,
            "max_steps": self.max_steps,
            "events": events_copy,
            "steps": list(self.steps),
            "current_observation": dict(self.current_observation),
            "budget_used": self.budget_used,
            "budget_total": self.budget_total,
            "budget_per_step": self.budget_per_step,
            "budget_step_used": self.budget_step_used,
            "last_confidence": dict(self.last_confidence),
            "guard_blocks": list(self.guard_blocks),
            "hook_records": list(self.hook_records[-50:]),
            "hook_count": self.hook_count,
            "network_requests": list(self.network_requests[-20:]),
            "target_params": list(self.target_params),
            "target_params_found": dict(self.target_params_found),
            "checkpoints": list(self.checkpoints),
            "success": self.success,
            "analysis": self.analysis,
            "compiled_script": self.compiled_script,
            "judge_result": dict(self.judge_result),
            "exit_code": self.exit_code,
            "error": self.error,
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
  <title>Web Crawler 控制台</title>
  <style>
    :root {
      --bg: #f6f7f9; --card: #fff; --border: #e3e7ee; --text: #111827;
      --muted: #5b6472; --input-bg: #fff; --log-bg: #111827; --log-text: #e5e7eb;
      --primary: #2563eb; --primary-hover: #1d4ed8; --danger: #b91c1c;
      --bar-bg: #e5e7eb; --bar-fill: #16a34a;
      --accent-purple: #a855f7; --accent-orange: #f97316; --accent-blue: #3b82f6;
      --accent-green: #22c55e; --accent-yellow: #eab308; --accent-red: #ef4444;
      --shadow: 0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.06);
    }
    [data-theme="dark"] {
      --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #f1f5f9;
      --muted: #94a3b8; --input-bg: #1e293b; --log-bg: #020617; --log-text: #e2e8f0;
      --primary: #3b82f6; --primary-hover: #60a5fa; --danger: #ef4444;
      --bar-bg: #334155; --bar-fill: #22c55e;
      --shadow: 0 1px 3px rgba(0,0,0,.3), 0 1px 2px rgba(0,0,0,.2);
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-family: "Microsoft YaHei", Arial, sans-serif; }
    main { max-width: 1340px; margin: 20px auto; padding: 0 18px; }
    h1 { font-size: 26px; margin: 0 0 6px; }
    .page-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
    .subtitle { color: var(--muted); margin: 0 0 18px; font-size: 14px; }

    /* Tab 栏 */
    .tabs { display: flex; gap: 4px; border-bottom: 2px solid var(--border); margin-bottom: 18px; align-items: flex-end; }
    .tab { background: none; border: 0; border-bottom: 3px solid transparent; padding: 10px 18px; font-size: 15px; font-weight: 600; cursor: pointer; color: var(--muted); border-radius: 0; transition: color .15s, border-color .15s; }
    .tab:hover { color: var(--text); }
    .tab.active { color: var(--primary); border-bottom-color: var(--primary); }
    .theme-toggle { margin-left: auto; background: none; border: 1px solid var(--border); border-radius: 6px; padding: 6px 12px; cursor: pointer; color: var(--text); font-size: 13px; }

    /* Tab 内容 */
    .tab-content { display: none; }
    .tab-content.active { display: block; }

    /* 采集器 Tab 内部限宽 */
    #tab-crawler .crawler-inner { max-width: 980px; }

    .tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px; }
    .tag { background: var(--card); border: 1px solid var(--border); border-radius: 4px; padding: 2px 8px; font-size: 12px; color: var(--muted); }
    form { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
    label { display: block; font-weight: 700; margin: 14px 0 8px; font-size: 14px; }
    input[type=text], input[type=number], textarea, select {
      width: 100%; padding: 10px 12px; border: 1px solid var(--border);
      border-radius: 6px; font-size: 14px; background: var(--input-bg); color: var(--text); font-family: inherit;
    }
    textarea { min-height: 72px; resize: vertical; font-family: Consolas, monospace; font-size: 13px; }
    .row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
    .grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px 18px; margin-top: 14px; }
    .check { display: flex; align-items: center; gap: 8px; font-weight: 500; color: var(--text); font-size: 14px; cursor: pointer; }
    .check input { width: 16px; height: 16px; cursor: pointer; }
    .section { margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--border); }
    .buttons { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
    button { border: 0; border-radius: 6px; padding: 10px 16px; font-size: 14px; font-weight: 700; cursor: pointer; }
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

    /* ========== JS 逆向 Agent 面板 ========== */
    .reverse-panel { display: flex; flex-direction: column; gap: 16px; }
    .reverse-grid { display: grid; grid-template-columns: 320px 1fr 340px; gap: 16px; align-items: start; }
    .reverse-left, .reverse-right { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; box-shadow: var(--shadow); max-height: calc(100vh - 200px); overflow-y: auto; }
    .reverse-center { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; box-shadow: var(--shadow); min-height: 500px; display: flex; flex-direction: column; }
    .reverse-left h3, .reverse-right h3, .reverse-center h3 { font-size: 14px; margin: 16px 0 10px; color: var(--text); border-bottom: 1px solid var(--border); padding-bottom: 6px; }
    .reverse-left h3:first-child, .reverse-right h3:first-child, .reverse-center h3:first-child { margin-top: 0; }
    .reverse-left label { margin: 10px 0 5px; font-size: 13px; }
    .reverse-left input, .reverse-left select, .reverse-left textarea { padding: 8px 10px; font-size: 13px; }
    .reverse-left .row { grid-template-columns: 1fr 1fr; gap: 10px; }
    .reverse-left .buttons { gap: 8px; margin-top: 14px; }
    .reverse-left .buttons button { padding: 9px 14px; font-size: 13px; flex: 1; }

    /* 高级配置折叠 */
    .reverse-left details { margin-top: 12px; border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; }
    .reverse-left details summary { cursor: pointer; font-weight: 600; font-size: 13px; color: var(--muted); }
    .reverse-left fieldset { border: 1px solid var(--border); border-radius: 6px; padding: 10px; margin: 8px 0; }
    .reverse-left legend { font-size: 12px; font-weight: 700; color: var(--primary); padding: 0 6px; }
    .reverse-left fieldset label { font-size: 12px; font-weight: 500; }
    .reverse-left fieldset input[type=number] { padding: 6px 8px; font-size: 12px; }

    /* 状态栏 */
    .reverse-status-bar { display: flex; align-items: center; gap: 16px; padding: 8px 12px; background: var(--bg); border-radius: 6px; margin-bottom: 12px; }
    .step-counter { font-size: 14px; color: var(--muted); }
    .step-counter strong { color: var(--text); font-size: 16px; }
    .status-badge { padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; }
    .status-badge.running { background: rgba(59,130,246,.15); color: var(--accent-blue); }
    .status-badge.done { background: rgba(34,197,94,.15); color: var(--accent-green); }
    .status-badge.error { background: rgba(239,68,68,.15); color: var(--accent-red); }
    .status-badge.cancelled { background: rgba(107,114,128,.15); color: #6b7280; }

    /* 执行轨迹 */
    .trace-list { flex: 1; overflow-y: auto; padding-right: 4px; }
    .trace-empty { color: var(--muted); text-align: center; padding: 40px 0; font-size: 14px; }
    .trace-step { background: var(--bg); border: 1px solid var(--border); border-left: 4px solid #6b7280; border-radius: 6px; padding: 10px 12px; margin-bottom: 8px; }
    .step-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
    .step-num { font-weight: 700; font-size: 13px; color: var(--text); }
    .action-badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; color: white; }
    .step-meta { font-size: 12px; color: var(--muted); }
    .step-conf { font-size: 12px; font-weight: 700; }
    .step-reasoning { font-size: 13px; color: var(--text); line-height: 1.5; overflow-wrap: anywhere; }

    /* Token 预算环形图 */
    .budget-card { display: flex; align-items: center; gap: 14px; }
    .budget-ring { width: 72px; height: 72px; border-radius: 50%; display: flex; align-items: center; justify-content: center; background: conic-gradient(var(--primary) 0%, var(--bar-bg) 0%); transition: background .3s; flex-shrink: 0; }
    .ring-value { width: 56px; height: 56px; border-radius: 50%; background: var(--card); display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 700; color: var(--text); }
    .budget-meta { flex: 1; font-size: 12px; color: var(--muted); line-height: 1.8; }
    .budget-meta strong { color: var(--text); }

    /* 置信度仪表 */
    .confidence-card { display: flex; align-items: center; gap: 14px; }
    .confidence-meter { width: 72px; height: 72px; border-radius: 50%; display: flex; align-items: center; justify-content: center; background: conic-gradient(#6b7280 0deg, var(--bar-bg) 0deg); transition: background .3s; flex-shrink: 0; }
    .meter-value { width: 56px; height: 56px; border-radius: 50%; background: var(--card); display: flex; align-items: center; justify-content: center; font-size: 14px; font-weight: 700; }
    .confidence-reasons { flex: 1; font-size: 11px; color: var(--muted); }
    .reason-item { padding: 2px 0; border-bottom: 1px dashed var(--border); }

    /* 护栏 / Checkpoint / 参数 / Hook 列表 */
    .guard-list, .checkpoint-list, .target-params, .hook-list { font-size: 12px; color: var(--muted); }
    .guard-item { padding: 4px 6px; background: rgba(239,68,68,.08); border-radius: 4px; margin-bottom: 4px; color: var(--text); }
    .checkpoint-item { padding: 3px 0; border-bottom: 1px dashed var(--border); }
    .param-item { padding: 3px 0; }
    .param-item.found { color: var(--accent-green); font-weight: 600; }
    .hook-item { padding: 3px 6px; background: var(--bg); border-radius: 4px; margin-bottom: 3px; font-family: Consolas, monospace; font-size: 11px; overflow-x: auto; white-space: nowrap; }
    .count-badge { display: inline-block; background: var(--primary); color: white; border-radius: 999px; padding: 1px 7px; font-size: 11px; font-weight: 700; }

    /* 底部结果 */
    .reverse-bottom { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; box-shadow: var(--shadow); }
    .result-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin: 10px 0; }
    .result-grid > div { font-size: 14px; color: var(--muted); }
    .result-grid strong { color: var(--text); }
    .reverse-bottom pre { background: var(--log-bg); color: var(--log-text); padding: 12px; border-radius: 6px; font-size: 12px; max-height: 300px; overflow: auto; }

    /* 滚动条美化 */
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--muted); }

    @media (max-width: 1100px) {
      .reverse-grid { grid-template-columns: 1fr; }
      .reverse-left, .reverse-right { max-height: none; }
    }
    @media (max-width: 760px) { .row, .grid { grid-template-columns: 1fr 1fr; } .result-grid { grid-template-columns: 1fr; } }
    @media (max-width: 480px) { .row, .grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <main>
    <div class="page-header">
      <h1>Web Crawler 控制台</h1>
    </div>
    <div class="tabs">
      <button class="tab active" data-tab="crawler">[采集器] 网页资源采集器</button>
      <button class="tab" data-tab="reverse">[逆向 Agent] JS 逆向 Agent</button>
      <button class="theme-toggle" type="button" onclick="toggleTheme()" title="切换主题">主题</button>
    </div>

    <!-- ========== Tab 1: 网页资源采集器 ========== -->
    <div id="tab-crawler" class="tab-content active">
      <div class="crawler-inner">
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
            <label class="check"><input type="checkbox" name="rewrite_html"> 离线重写 HTML</label>
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
      </div>
    </div>

    <!-- ========== Tab 2: JS 逆向 Agent ========== -->
    <div id="tab-reverse" class="tab-content">
      <div class="reverse-panel">
        <div class="reverse-grid">
          <!-- 左栏：任务配置 -->
          <aside class="reverse-left">
            <h3>任务配置</h3>
            <form id="reverse-form">
              <label>目标 URL</label>
              <input name="url" type="text" placeholder="https://example.com" required>

              <label>任务描述</label>
              <input name="task" type="text" placeholder="提取 Anti-Content / sign 加密参数">

              <label>目标参数（逗号分隔）</label>
              <input name="target_params" type="text" placeholder="anti_content, sign, X-Bogus" value="anti_content, sign">

              <div class="row">
                <div><label>最大步数</label><input name="max_steps" type="number" value="20" min="1"></div>
                <div><label>OS 指纹</label>
                  <select name="os_name">
                    <option value="windows">windows</option>
                    <option value="macos">macos</option>
                    <option value="linux">linux</option>
                  </select>
                </div>
              </div>

              <div class="row">
                <div><label>代理</label><input name="proxy" type="text" placeholder="http://u:p@host:port"></div>
                <div><label>headless</label>
                  <select name="headless">
                    <option value="false">否（可见浏览器）</option>
                    <option value="true">是（无头）</option>
                  </select>
                </div>
              </div>

              <details>
                <summary>高级配置（DomPruner / Checkpoint / Budget / Confidence / Guard）</summary>
                <fieldset>
                  <legend>DOM 焦点裁剪</legend>
                  <label class="check"><input type="checkbox" name="dom_prune" value="1"> 启用 DOM 裁剪</label>
                  <label>max_chars</label>
                  <input name="dom_prune_max_chars" type="number" value="4000">
                  <div class="hint">单一模型策略：规则打分，不调用 LLM 重排</div>
                </fieldset>

                <fieldset>
                  <legend>断点续跑 (Checkpoint)</legend>
                  <label class="check"><input type="checkbox" name="enable_checkpoint" value="1" checked> 启用</label>
                  <label>保存间隔（步）</label>
                  <input name="checkpoint_interval" type="number" value="1">
                  <label>保留数量</label>
                  <input name="checkpoint_keep" type="number" value="5">
                </fieldset>

                <fieldset>
                  <legend>Token 预算</legend>
                  <label>全局上限</label>
                  <input name="budget_total" type="number" value="100000">
                  <label>单步上限</label>
                  <input name="budget_per_step" type="number" value="8000">
                </fieldset>

                <fieldset>
                  <legend>动作置信度</legend>
                  <label>最低阈值（0-1）</label>
                  <input name="min_confidence" type="number" value="0.4" step="0.1" min="0" max="1">
                  <div class="hint">单一模型策略：规则评分，不调用 LLM 评分</div>
                </fieldset>

                <fieldset>
                  <legend>危险动作护栏</legend>
                  <label class="check"><input type="checkbox" name="enable_guard" value="1" checked> 启用</label>
                  <label>允许域名白名单（逗号分隔，留空不限制）</label>
                  <input name="allowed_domains" type="text" placeholder="example.com, cdn.example.com">
                </fieldset>
              </details>

              <div class="buttons">
                <button type="submit" class="btn-primary">启动 Agent</button>
                <button type="button" id="reverse-stop" class="btn-danger" disabled>停止</button>
              </div>
            </form>
          </aside>

          <!-- 中栏：实时执行轨迹 -->
          <section class="reverse-center">
            <div class="reverse-status-bar">
              <span class="step-counter">步数 <strong id="rev-current-step">0</strong>/<span id="rev-max-steps">20</span></span>
              <span class="status-badge running" id="rev-status">等待启动</span>
            </div>
            <h3>执行轨迹 (Trace)</h3>
            <div id="rev-trace" class="trace-list">
              <div class="trace-empty">点击"启动 Agent"开始任务</div>
            </div>
          </section>

          <!-- 右栏：Agent 内部状态 -->
          <aside class="reverse-right">
            <h3>Token 预算</h3>
            <div class="budget-card">
              <div class="budget-ring" id="rev-budget-ring">
                <div class="ring-value"><span id="rev-budget-pct">0%</span></div>
              </div>
              <div class="budget-meta">
                <div>已用 <strong id="rev-budget-used">0</strong></div>
                <div>总额 <strong id="rev-budget-total">100000</strong></div>
                <div>单步 <strong id="rev-budget-step">0</strong>/<span id="rev-budget-step-max">8000</span></div>
              </div>
            </div>

            <h3>动作置信度</h3>
            <div class="confidence-card">
              <div class="confidence-meter" id="rev-confidence-meter">
                <div class="meter-value" id="rev-confidence-value">--</div>
              </div>
              <div class="confidence-reasons" id="rev-confidence-reasons">等待数据</div>
            </div>

            <h3>护栏拦截</h3>
            <div id="rev-guard-blocks" class="guard-list">无</div>

            <h3>Checkpoints</h3>
            <div id="rev-checkpoints" class="checkpoint-list">无</div>

            <h3>目标参数</h3>
            <div id="rev-target-params" class="target-params">--</div>

            <h3>Hook 捕获 <span id="rev-hook-count" class="count-badge">0</span></h3>
            <div id="rev-hooks" class="hook-list">无数据</div>
          </aside>
        </div>

        <!-- 底部：结果汇总 -->
        <section class="reverse-bottom" id="rev-result-section" style="display:none">
          <h3>任务结果</h3>
          <div class="result-grid">
            <div><strong>状态</strong>: <span id="rev-result-status">--</span></div>
            <div><strong>Judge 验证</strong>: <span id="rev-judge">--</span></div>
            <div><strong>成功路径脚本</strong>: <button id="rev-download-script" class="btn-secondary">下载 .py</button></div>
          </div>
          <details>
            <summary>完整 Analysis</summary>
            <pre id="rev-analysis">--</pre>
          </details>
        </section>
      </div>
    </div>
  </main>
  <script>
    /* ========== 主题切换 ========== */
    (function() {
      var theme = localStorage.getItem('crawler-theme');
      if (theme === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
    })();
    function toggleTheme() {
      var html = document.documentElement;
      var isDark = html.getAttribute('data-theme') === 'dark';
      if (isDark) { html.removeAttribute('data-theme'); localStorage.setItem('crawler-theme', 'light'); }
      else { html.setAttribute('data-theme', 'dark'); localStorage.setItem('crawler-theme', 'dark'); }
    }

    /* ========== Tab 切换 ========== */
    document.querySelectorAll('.tab').forEach(function(tab) {
      tab.onclick = function() {
        document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
        document.querySelectorAll('.tab-content').forEach(function(c) { c.classList.remove('active'); });
        tab.classList.add('active');
        document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
      };
    });

    /* ========== 采集器逻辑（原有） ========== */
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
      if (result.status === 'paused') { pauseButton.disabled = true; resumeButton.disabled = false; }
      else { resumeButton.disabled = true; }
      if (['done', 'error', 'cancelled'].indexOf(result.status) >= 0) {
        clearInterval(timer); setRunning(false); pauseButton.disabled = true; resumeButton.disabled = true;
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

    /* ========== JS 逆向 Agent 逻辑 ========== */
    var reverseForm = document.getElementById('reverse-form');
    var reverseStopBtn = document.getElementById('reverse-stop');
    var currentReverseJobId = null;
    var reverseTimer = null;

    function escapeHtml(s) {
      return String(s == null ? '' : s).replace(/[<>&"]/g, function(c) {
        return {'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c];
      });
    }

    function renderStep(s) {
      var colorMap = { navigate: '#3b82f6', inject_hook: '#a855f7', analyze_js: '#f97316',
                       wait: '#6b7280', extract: '#22c55e', solve_captcha: '#eab308', done: '#16a34a' };
      var color = colorMap[s.action_type] || '#6b7280';
      var confColor = '#6b7280';
      if (s.confidence != null) {
        confColor = s.confidence >= 0.7 ? '#22c55e' : s.confidence >= 0.4 ? '#eab308' : '#ef4444';
      }
      var confHtml = (s.confidence != null) ? '<span class="step-conf" style="color:' + confColor + '">conf ' + Number(s.confidence).toFixed(2) + '</span>' : '';
      return '<div class="trace-step" style="border-left-color:' + color + '">' +
        '<div class="step-header">' +
        '<span class="step-num">Step ' + s.step + '</span>' +
        '<span class="action-badge" style="background:' + color + '">' + escapeHtml(s.action_type || '--') + '</span>' +
        '<span class="step-meta">' + (s.duration_ms || 0) + 'ms &middot; ' + (s.tokens || 0) + ' tok</span>' +
        confHtml +
        '</div>' +
        '<div class="step-reasoning">' + escapeHtml(s.reasoning || '') + '</div>' +
        '</div>';
    }

    async function pollReverse() {
      if (!currentReverseJobId) return;
      try {
        var response = await fetch('/reverse/status?id=' + encodeURIComponent(currentReverseJobId));
        var data = await response.json();
      } catch(e) { return; }
      if (!data) return;

      document.getElementById('rev-current-step').textContent = data.current_step || 0;
      document.getElementById('rev-max-steps').textContent = data.max_steps || 20;

      var statusBadge = document.getElementById('rev-status');
      statusBadge.textContent = data.status || '--';
      statusBadge.className = 'status-badge ' + (data.status || '');

      var pct = data.budget_total > 0 ? (data.budget_used / data.budget_total * 100) : 0;
      document.getElementById('rev-budget-ring').style.background =
        'conic-gradient(var(--primary) ' + pct + '%, var(--bar-bg) ' + pct + '%)';
      document.getElementById('rev-budget-pct').textContent = Math.round(pct) + '%';
      document.getElementById('rev-budget-used').textContent = data.budget_used || 0;
      document.getElementById('rev-budget-total').textContent = data.budget_total || 0;
      document.getElementById('rev-budget-step').textContent = data.budget_step_used || 0;
      document.getElementById('rev-budget-step-max').textContent = data.budget_per_step || 0;

      if (data.last_confidence && data.last_confidence.score !== undefined) {
        var score = data.last_confidence.score;
        var confColor = score >= 0.7 ? '#22c55e' : score >= 0.4 ? '#eab308' : '#ef4444';
        document.getElementById('rev-confidence-meter').style.background =
          'conic-gradient(' + confColor + ' ' + (score * 360) + 'deg, var(--bar-bg) ' + (score * 360) + 'deg)';
        document.getElementById('rev-confidence-value').textContent = Number(score).toFixed(2);
        document.getElementById('rev-confidence-value').style.color = confColor;
        var reasonsHtml = (data.last_confidence.reasons || []).map(function(r) {
          return '<div class="reason-item">' + escapeHtml(r) + '</div>';
        }).join('');
        document.getElementById('rev-confidence-reasons').innerHTML = reasonsHtml || '无';
      }

      var guardHtml = (data.guard_blocks || []).map(function(g) {
        return '<div class="guard-item">' + escapeHtml(g.rule) + ': ' + escapeHtml(g.detail) + '</div>';
      }).join('');
      document.getElementById('rev-guard-blocks').innerHTML = guardHtml || '无';

      var cpHtml = (data.checkpoints || []).map(function(c) {
        return '<div class="checkpoint-item">Step ' + c.step + ' - ' + escapeHtml(String(c.url || '').slice(0, 50)) + '</div>';
      }).join('');
      document.getElementById('rev-checkpoints').innerHTML = cpHtml || '无';

      var tpHtml = (data.target_params || []).map(function(p) {
        var found = data.target_params_found && data.target_params_found[p];
        return '<div class="param-item ' + (found ? 'found' : '') + '">' + (found ? '[OK] ' : '[..] ') + escapeHtml(p) + (found ? ': ' + escapeHtml(found) : '') + '</div>';
      }).join('');
      document.getElementById('rev-target-params').innerHTML = tpHtml || '--';

      document.getElementById('rev-hook-count').textContent = data.hook_count || 0;
      var hookHtml = (data.hook_records || []).slice(-10).map(function(h) {
        return '<div class="hook-item">' + escapeHtml(JSON.stringify(h)) + '</div>';
      }).join('');
      document.getElementById('rev-hooks').innerHTML = hookHtml || '无数据';

      var traceEl = document.getElementById('rev-trace');
      var stepsHtml = (data.steps || []).map(function(s) { return renderStep(s); }).join('');
      traceEl.innerHTML = stepsHtml || '<div class="trace-empty">等待数据...</div>';
      traceEl.scrollTop = traceEl.scrollHeight;

      if (['done', 'error', 'cancelled'].indexOf(data.status) >= 0) {
        if (reverseTimer) { clearInterval(reverseTimer); reverseTimer = null; }
        reverseForm.querySelector('button[type=submit]').disabled = false;
        reverseStopBtn.disabled = true;
        showReverseResult(data);
      }
    }

    function showReverseResult(data) {
      var section = document.getElementById('rev-result-section');
      section.style.display = 'block';
      document.getElementById('rev-result-status').textContent = data.status || '--';
      if (data.error) { document.getElementById('rev-result-status').textContent += ' (' + data.error + ')'; }
      var judge = data.judge_result || {};
      var judgeText = '--';
      if (judge.verified !== undefined) {
        judgeText = judge.verified ? '通过' : '未通过';
        if (judge.missing && judge.missing.length) { judgeText += ' (缺失: ' + judge.missing.join(', ') + ')'; }
      }
      document.getElementById('rev-judge').textContent = judgeText;
      document.getElementById('rev-analysis').textContent = data.analysis || '--';

      var dlBtn = document.getElementById('rev-download-script');
      dlBtn.onclick = function() {
        if (!data.compiled_script) { alert('无可用脚本'); return; }
        var blob = new Blob([data.compiled_script], { type: 'text/x-python' });
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'reverse_agent_script.py';
        a.click();
        URL.revokeObjectURL(a.href);
      };
    }

    function startReversePolling() {
      if (reverseTimer) clearInterval(reverseTimer);
      reverseTimer = setInterval(pollReverse, 800);
    }

    reverseForm.addEventListener('submit', async function(event) {
      event.preventDefault();
      var submitBtn = reverseForm.querySelector('button[type=submit]');
      submitBtn.disabled = true;
      reverseStopBtn.disabled = false;
      document.getElementById('rev-result-section').style.display = 'none';
      document.getElementById('rev-trace').innerHTML = '<div class="trace-empty">正在启动 Agent...</div>';
      try {
        var result = await fetch('/reverse/run', { method: 'POST', body: new FormData(reverseForm) }).then(function(r) { return r.json(); });
        if (result.id) {
          currentReverseJobId = result.id;
          startReversePolling();
          await pollReverse();
        } else {
          submitBtn.disabled = false;
          reverseStopBtn.disabled = true;
          alert(result.error || '启动失败');
        }
      } catch(e) {
        submitBtn.disabled = false;
        reverseStopBtn.disabled = true;
        alert('请求失败: ' + e.message);
      }
    });

    reverseStopBtn.onclick = async function() {
      if (!currentReverseJobId) return;
      try {
        await fetch('/reverse/stop?id=' + encodeURIComponent(currentReverseJobId), { method: 'POST' });
        await pollReverse();
      } catch(e) {}
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
    args.rewrite_html = checked("rewrite_html")
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


# ---------------------------------------------------------------------------
# JS 逆向 Agent：表单 → ReverseAgentConfig + 子线程运行 + 事件订阅
# ---------------------------------------------------------------------------


def build_reverse_config(form: dict[str, list[str]]) -> dict[str, object]:
    """从表单构造 ReverseAgentConfig 的可序列化字段字典。"""

    def value(name: str, default: str = "") -> str:
        return form.get(name, [default])[0]

    def checked(name: str) -> bool:
        return name in form

    target_params_str = value("target_params", "")
    target_params = [p.strip() for p in target_params_str.split(",") if p.strip()]

    allowed_domains_str = value("allowed_domains", "")
    allowed_domains: list[str] | None = None
    if allowed_domains_str.strip():
        allowed_domains = [d.strip() for d in allowed_domains_str.split(",") if d.strip()]

    # dom_prune 复选框启用时才使用 max_chars，否则为 0（禁用）
    dom_prune_max_chars = (
        int(value("dom_prune_max_chars", "4000") or "0") if checked("dom_prune") else 0
    )

    return {
        "max_steps": int(value("max_steps", "20") or "20"),
        "target_params": target_params,
        "headless": value("headless", "false") == "true",
        "proxy": value("proxy", "") or None,
        "os_name": value("os_name", "windows"),
        "dom_prune_max_chars": dom_prune_max_chars,
        "dom_prune_llm_rank": checked("dom_prune_llm_rank"),
        "enable_checkpoint": checked("enable_checkpoint"),
        "checkpoint_interval": int(value("checkpoint_interval", "1") or "1"),
        "checkpoint_keep": int(value("checkpoint_keep", "5") or "5"),
        "budget_total": int(value("budget_total", "100000") or "100000"),
        "budget_per_step": int(value("budget_per_step", "8000") or "8000"),
        "min_confidence": float(value("min_confidence", "0.4") or "0.4"),
        "confidence_llm_score": checked("confidence_llm_score"),
        "enable_guard": checked("enable_guard"),
        "allowed_domains": allowed_domains,
    }


class ReverseAgentRunner:
    """在子线程中启动 ReverseAgent，订阅 EventBus，把事件推到 ReverseJobState。

    最简停止策略：stop_event 仅标记 UI 状态为 cancelled，不真正中断 agent
    （agent.run 同步阻塞，daemon 线程自然结束）。
    """

    def run_job(self, job: ReverseJobState) -> None:
        """子线程入口：构造 Agent 并同步运行，事件实时推到 job。"""
        # 延迟导入：避免 UI 启动时加载 camoufox 等重依赖
        try:
            from web_crawler.ai.llm import DEFAULT_MODEL, DeepSeekProvider
            from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig
            from web_crawler.ai.watchdog import EventBus

            cfg_dict = job.config
            config = ReverseAgentConfig(
                max_steps=int(cfg_dict.get("max_steps", 20)),
                target_params=cfg_dict.get("target_params") or None,
                headless=bool(cfg_dict.get("headless", False)),
                proxy=cfg_dict.get("proxy") or None,
                os_name=str(cfg_dict.get("os_name", "windows")),
                dom_prune_max_chars=int(cfg_dict.get("dom_prune_max_chars", 0)),
                # 单一模型策略：DomPruner/Confidence 都用规则路径，不调用 LLM 重排/评分
                enable_checkpoint=bool(cfg_dict.get("enable_checkpoint", False)),
                checkpoint_interval=int(cfg_dict.get("checkpoint_interval", 1)),
                checkpoint_keep=int(cfg_dict.get("checkpoint_keep", 5)),
                budget_total=int(cfg_dict.get("budget_total", 100_000)),
                budget_per_step=int(cfg_dict.get("budget_per_step", 8_000)),
                min_confidence=float(cfg_dict.get("min_confidence", 0.4)),
                enable_guard=bool(cfg_dict.get("enable_guard", True)),
                allowed_domains=cfg_dict.get("allowed_domains") or None,
            )

            # 创建独立 EventBus 并订阅
            bus = EventBus()
            provider = DeepSeekProvider(model=DEFAULT_MODEL)
            agent = ReverseAgent(config=config, provider=provider, event_bus=bus)
            bus.subscribe(lambda evt: self._on_event(job, evt, agent))

            # 同步运行（在子线程中阻塞）
            result = agent.run(url=job.url, task=job.task)

            # 写回结果
            job.success = bool(result.get("success", False))
            analysis = result.get("analysis")
            job.analysis = _serialize_analysis(analysis)
            job.compiled_script = str(result.get("compiled_script") or "")
            job.target_params_found = dict(result.get("target_params_found") or {})
            judge = result.get("judge_result")
            job.judge_result = dict(judge) if isinstance(judge, dict) else {}
            # 最终预算快照
            try:
                summary = agent.budget_tracker.summary()
                job.budget_used = int(summary.get("used_total", 0))
            except Exception:
                pass

            if job.stop_event.is_set():
                job.status = "cancelled"
            elif job.success:
                job.status = "done"
            else:
                job.status = "error"
                if not job.error:
                    job.error = "Agent 未成功完成目标参数提取"
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            job.exit_code = 1

    def _on_event(self, job: ReverseJobState, event: object, agent: object) -> None:
        """EventBus 订阅器：把 AgentEvent 推到 ReverseJobState。

        处理异常时不能让单个事件处理失败导致 agent 崩溃（EventBus 本身
        也会捕获订阅者异常，但这里额外做一层保护）。
        """
        try:
            evt_type = getattr(event, "type", "")
            evt_step = getattr(event, "step", 0)
            evt_payload = getattr(event, "payload", {}) or {}
            ts = time.time()

            # 序列化事件并追加到流
            evt_dict = {"type": evt_type, "step": evt_step, "payload": evt_payload, "ts": ts}
            job.append_event(evt_dict)

            # 根据事件类型更新对应字段
            if evt_type == "step.start":
                job.current_step = evt_step
                job._step_starts[evt_step] = ts
            elif evt_type == "step.end":
                self._finalize_step(job, evt_step, agent)
            elif evt_type == "action":
                self._update_step_action(job, evt_step, evt_payload)
            elif evt_type == "observation":
                job.current_observation = {
                    "url": evt_payload.get("url", ""),
                    "hook_count": evt_payload.get("hook_count", 0),
                    "network_count": evt_payload.get("network_count", 0),
                    "script_count": evt_payload.get("script_count", 0),
                }
            elif evt_type == "confidence.low":
                job.last_confidence = {
                    "score": evt_payload.get("score", 0.0),
                    "reasons": list(evt_payload.get("reasons") or []),
                    "action_type": "",
                }
            elif evt_type == "guard.deny":
                rules = list(evt_payload.get("matched_rules") or [])
                details = list(evt_payload.get("details") or [])
                for i, rule in enumerate(rules):
                    detail = details[i] if i < len(details) else ""
                    job.guard_blocks.append({"rule": rule, "detail": detail})
            elif evt_type == "budget.compress" or evt_type == "budget.exceeded":
                job.budget_used = int(evt_payload.get("used_total", 0))
            elif evt_type == "judge.result":
                job.judge_result = {
                    "verified": evt_payload.get("verified", False),
                    "missing": list(evt_payload.get("missing") or []),
                }
            elif evt_type == "checkpoint.resume":
                job.checkpoints.append(
                    {
                        "step": evt_step,
                        "url": evt_payload.get("url", ""),
                        "type": "resume",
                    }
                )
        except Exception:
            # 静默吞掉订阅者异常，不能影响 agent 主循环
            pass

    def _update_step(self, job: ReverseJobState, step: int) -> dict:
        """获取或创建步骤字典（用于累积 action / confidence 等字段）。"""
        for s in job.steps:
            if s.get("step") == step:
                return s
        entry = {
            "step": step,
            "action_type": "",
            "reasoning": "",
            "duration_ms": 0,
            "tokens": 0,
            "confidence": None,
        }
        job.steps.append(entry)
        return entry

    def _update_step_action(self, job: ReverseJobState, step: int, payload: dict) -> None:
        """收到 action 事件时更新步骤的 action_type / reasoning。"""
        entry = self._update_step(job, step)
        entry["action_type"] = str(payload.get("action_type", ""))
        entry["reasoning"] = str(payload.get("reasoning", ""))

    def _finalize_step(self, job: ReverseJobState, step: int, agent: object) -> None:
        """step.end 时计算耗时、token、置信度，完成步骤卡片。"""
        entry = self._update_step(job, step)
        start_ts = job._step_starts.pop(step, None)
        if start_ts is not None:
            entry["duration_ms"] = int((time.time() - start_ts) * 1000)

        # 从 agent 读取最新置信度与预算
        try:
            conf = getattr(agent, "_last_confidence", None)
            if conf is not None:
                score = getattr(conf, "score", None)
                if score is not None:
                    entry["confidence"] = float(score)
                    job.last_confidence = {
                        "score": float(score),
                        "reasons": list(getattr(conf, "reasons", []) or []),
                        "action_type": str(getattr(conf, "action_type", "")),
                    }
        except Exception:
            pass

        try:
            summary: dict[str, Any] = getattr(
                getattr(agent, "budget_tracker", None), "summary", dict
            )()
            used_now = int(summary.get("used_total", 0))
            entry["tokens"] = max(0, used_now - job.budget_used)
            job.budget_used = used_now
            job.budget_step_used = entry["tokens"]
        except Exception:
            pass

        # 从 agent 读取 hook 数据缓存
        try:
            hook_cache = getattr(agent, "_hook_data_cache", {})
            records = hook_cache.get("records", []) if isinstance(hook_cache, dict) else []
            if records:
                job.hook_records = list(records)
                job.hook_count = len(records)
        except Exception:
            pass

        # 从 agent 读取网络日志
        try:
            net_log = getattr(agent, "_network_log", [])
            if net_log:
                job.network_requests = list(net_log)
        except Exception:
            pass


def _serialize_analysis(analysis: object) -> str:
    """把 AnalysisResult dataclass 序列化为可读字符串。"""
    if analysis is None:
        return ""
    if isinstance(analysis, str):
        return analysis
    if hasattr(analysis, "__dataclass_fields__"):
        try:
            from dataclasses import asdict

            return json.dumps(asdict(analysis), ensure_ascii=False, indent=2, default=str)  # type: ignore[call-overload]
        except Exception:
            pass
    return str(analysis)


def run_reverse_job(job: ReverseJobState) -> None:
    """子线程入口：启动 ReverseAgentRunner。"""
    job.status = "running"
    ReverseAgentRunner().run_job(job)


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
        if self.path.startswith("/reverse/status"):
            query = parse_qs(urlparse(self.path).query)
            rjob = REVERSE_JOBS.get(query.get("id", [""])[0])
            if not rjob:
                self.respond_json({"status": "missing", "error": "任务不存在"})
                return
            self.respond_json(rjob.snapshot())
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
        if path == "/reverse/run":
            form = self.read_form()
            url = form.get("url", [""])[0].strip()
            task = form.get("task", [""])[0].strip()
            if not url:
                self.respond_json({"ok": False, "error": "URL 不能为空"})
                return
            config: dict[str, Any] = build_reverse_config(form)
            job_id = uuid.uuid4().hex[:12]
            rjob = ReverseJobState(
                id=job_id,
                url=url,
                task=task,
                config=config,
                max_steps=int(config.get("max_steps", 20)),
                budget_total=int(config.get("budget_total", 100_000)),
                budget_per_step=int(config.get("budget_per_step", 8_000)),
                target_params=list(config.get("target_params") or []),
            )
            with REVERSE_JOBS_LOCK:
                REVERSE_JOBS[job_id] = rjob
                # 清理已完成的任务
                if len(REVERSE_JOBS) > MAX_REVERSE_JOBS:
                    for jid in list(REVERSE_JOBS.keys()):
                        rj = REVERSE_JOBS[jid]
                        if rj.status in ("done", "error", "cancelled"):
                            del REVERSE_JOBS[jid]
            threading.Thread(target=run_reverse_job, args=(rjob,), daemon=True).start()
            self.respond_json({"id": job_id, "status": "running"})
            return
        if path == "/reverse/stop":
            rstop = REVERSE_JOBS.get(query.get("id", [""])[0])
            if not rstop:
                self.respond_json({"ok": False, "message": "任务不存在"})
                return
            rstop.stop_event.set()
            if rstop.status == "running":
                rstop.status = "cancelled"
            self.respond_json({"ok": True})
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
    _log.info("Web UI started: %s", url)
    _log.info("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log.info("Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()

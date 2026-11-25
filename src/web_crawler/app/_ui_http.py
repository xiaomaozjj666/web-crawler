"""Web UI 的 HTTP Handler 与路由（从 ``ui.py`` 拆出）。

``Handler`` 为 ``BaseHTTPRequestHandler`` 子类，承载控制面全部路由：
页面 / 状态查询 / SSE 流 / 任务历史 API / 采集与逆向任务的启动控制。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import re
import threading
import uuid
from contextlib import suppress
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from web_crawler.app import db as database
from web_crawler.parser.rules import MAX_RULES_BYTES, ExtractionRules

from ._scrape import ScrapeError, scrape_page
from ._ui_helpers import (
    DEFAULT_BLOCK_KEYWORDS,
    DEFAULT_OUTPUT,
    PAGE,
    STATIC_DIR,
    _is_loopback_host,
    _normalize_imported_config,
    _open_folder,
    _task_config_for_db,
    build_args,
    build_args_from_task,
    build_reverse_config,
    normalize_crawl_config,
    output_path,
    portable_crawl_config,
)
from ._ui_runner import run_captcha_job, run_job, run_reverse_job
from ._ui_state import (
    CAPTCHA_JOBS,
    CAPTCHA_JOBS_LOCK,
    JOBS,
    JOBS_LOCK,
    MAX_CAPTCHA_JOBS,
    MAX_JOBS,
    MAX_REVERSE_JOBS,
    REVERSE_JOBS,
    REVERSE_JOBS_LOCK,
    CaptchaJobState,
    JobState,
    ReverseJobState,
)

_log = logging.getLogger(__name__)
_PAGE_AUDIT_BATCH_SIZE = 16
# /v1/scrape 的 JSON 请求体上限（只含 url + 布尔开关，正常远小于 1 KiB）
_SCRAPE_MAX_BODY_BYTES = 64 * 1024

# 前端静态资源：页面的 <script src="/static/js/..."> 从这里取（与页面模板同根）。
# 路由用「单段文件名」白名单匹配，天然排除 ../ 之类路径穿越；
# 文件不存在直接 404（新增 js 无需改 Python）。
_STATIC_JS_RE = re.compile(r"^/static/js/([A-Za-z0-9._-]+\.js)$")
_STATIC_JS_CONTENT_TYPE = "application/javascript; charset=utf-8"
# 本地工具 UI 的 js 与 Python 版本同步发布、URL 不带指纹：走 no-cache 强制
# 每次用 ETag 复验（未改动回 304），既不缓存旧脚本也不重复传主体。
_STATIC_CACHE_CONTROL = "no-cache"

# Host 头主机部分的合法形式：括号 IPv6 字面量（[::1] / [fe80::1%12]）
# 或常规 reg-name / IPv4（字母数字点横线）。含 / \ ? # @ : 等保留字符的
# 一律视为畸形，拒绝。
_HOST_LITERAL_RE = re.compile(r"^(?:\[[0-9a-fA-F:.%]+\]|[0-9a-zA-Z._\-]+)$")


def _split_host_port(value: str) -> tuple[str, str]:
    """拆分 Host 头的 host 与 port 部分；支持 ``[::1]:8765`` 括号 IPv6 形式。

    返回 ``(host, port)``；port 为空字符串表示未携带端口。
    畸形形式（未闭合括号、裸端口 ``:8765`` 等）原样返回，
    交由 :func:`_host_header_allowed` 的形式校验统一拒绝。
    """
    if value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            return value, ""
        host = value[: closing + 1]
        rest = value[closing + 1 :]
        if rest and not rest.startswith(":"):
            return "", ""  # 括号后跟杂字符（如 [::1]x），判畸形
        return host, rest[1:] if rest else ""
    host, sep, port = value.rpartition(":")
    if not sep or not host:
        return value, ""
    return host, port


def _host_header_allowed(host_header: str, bind_host: str, bind_port: int) -> bool:
    """校验 Host 头是否为服务绑定地址的预期形式（防 DNS rebinding）。

    DNS rebinding 攻击（attacker.com 先解析到攻击者服务器加载恶意页面，
    TTL 过后再 rebind 到本机绑定地址）下，浏览器发出的 Origin 与 Host
    同为 ``attacker.com:port``，仅比对 ``Origin == Host`` 无法拦截；
    必须校验 Host 是否命中本地地址白名单（Jupyter 同款标准防护）。

    校验策略与取舍（「远程可用性」vs「安全」）：

    - 回环绑定（默认 127.0.0.1:8765）：白名单 = ``{127.0.0.1, localhost,
      [::1]}`` 加绑定地址自身，且端口必须精确匹配绑定端口。rebinding
      域名不在白名单内，天然被拒。
    - ``--allow-remote`` 远程绑定（0.0.0.0 等）：远程访问的主机名（局域网
      IP / NAT 域名 / Docker 宿主机映射名）不可枚举，无法构建白名单。
      此时保留「非空、无 userinfo 混淆、形式合法、端口匹配」下限校验，
      挡住畸形与端口混淆请求；rebinding 域名在该模式下与正常远程访问
      在 Host 上不可区分，安全边界由用户显式 ``--allow-remote`` 时自行
      承担（控制面无鉴权，仅建议可信网络，与 CLI help 声明一致）。
    - Host 缺失（HTTP/1.0 客户端）：fail-closed 一律拒绝。HTTP/1.1
      （RFC 9112 §7.2）强制 Host，现代浏览器与 curl/httpx 默认携带；
      缺失只可能来自畸形或古老客户端，放行等于打开绕过 Host 校验的
      口子，而拒绝的可用性损失极小。
    """
    host_header = host_header.strip()
    if not host_header:
        return False
    if "@" in host_header:  # userinfo 混淆形式（a@b）不在 Host 头合法语法内
        return False

    host_part, port_part = _split_host_port(host_header)
    host_part = host_part.lower()
    if not _HOST_LITERAL_RE.fullmatch(host_part):
        return False

    # 端口必须与绑定端口一致；无端口形式仅在服务真监听 80 端口时合法
    if port_part:
        if port_part != str(bind_port):
            return False
    elif bind_port != 80:
        return False

    if _is_loopback_host(bind_host):
        # 绑定主机为 IPv6 时取括号形式（与 Host 头的规范写法一致）
        bind_norm = f"[{bind_host.lower()}]" if ":" in bind_host else bind_host.lower()
        allowed = {"127.0.0.1", "localhost", "[::1]", bind_norm}
        return host_part in allowed
    # 远程绑定：形式合法 + 端口匹配即放行（取舍见 docstring）
    return True


def _active_task_counts() -> dict[str, int]:
    with JOBS_LOCK:
        crawler = sum(job.is_active() for job in JOBS.values())
    with REVERSE_JOBS_LOCK:
        reverse = sum(job.status == "running" for job in REVERSE_JOBS.values())
    with CAPTCHA_JOBS_LOCK:
        captcha = sum(job.status == "running" for job in CAPTCHA_JOBS.values())
    return {"crawler": crawler, "reverse": reverse, "captcha": captcha}


def _truthy_flag(value: object) -> bool:
    """解析布尔开关：``True`` 或 ``1/true/yes/on`` 视为真（大小写不敏感）。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _pagination(query: dict[str, list[str]], default_size: int) -> tuple[int, int]:
    try:
        page = int(query.get("page", ["1"])[0])
        size = int(query.get("page_size", [str(default_size)])[0])
    except ValueError as exc:
        raise ValueError("页码和每页条数必须为整数") from exc
    if not (1 <= page <= 2**31 - 1 and 1 <= size <= 100):
        raise ValueError("页码必须为正数，每页支持 1–100 条")
    return page, size


class Handler(BaseHTTPRequestHandler):
    def finish(self) -> None:
        try:
            super().finish()
        finally:
            database.close_thread_connection()

    def _start_crawl(self, args: argparse.Namespace) -> None:
        with JOBS_LOCK:
            active = next((job for job in JOBS.values() if job.is_active()), None)
            if active is not None:
                payload: dict[str, object] = {
                    "ok": False,
                    "error": "已有采集任务正在运行，请等待结束",
                    "active_id": active.id,
                }
            else:
                job_id = uuid.uuid4().hex[:12]
                job = JobState(id=job_id, args=args, output_dir=str(Path(args.out).resolve()))
                try:
                    database.create_task(
                        job_id, args.url, _task_config_for_db(args), job.output_dir
                    )
                except Exception as exc:
                    _log.exception("db create_task failed for %s", job_id)
                    payload = {"ok": False, "error": f"无法创建任务记录：{exc}"}
                else:
                    JOBS[job_id] = job
                    while len(JOBS) > MAX_JOBS:
                        JOBS.pop(next(iter(JOBS)))
                    try:
                        threading.Thread(target=run_job, args=(job,), daemon=True).start()
                    except RuntimeError as exc:
                        JOBS.pop(job_id)
                        error = f"无法启动采集线程：{exc}"
                        database.update_task_status(
                            job_id, "error", exit_code=1, error_message=error
                        )
                        payload = {"ok": False, "error": error}
                    else:
                        payload = {"id": job_id, "status": "running"}
        self.respond_json(payload)

    def _control_crawl(self, job: JobState, action: str) -> dict[str, object]:
        with job.lock:
            if action == "/cancel" and job.status == "cancelling":
                return {"ok": True}
            if job.status not in {"running", "paused"}:
                return {"ok": False, "message": "采集正在结束或已结束"}
            if action == "/pause":
                job.pause_event.clear()
                job.status = "paused"
            elif action == "/resume":
                job.pause_event.set()
                job.status = "running"
            else:
                job.stop_event.set()
                job.pause_event.set()
                job.status = "cancelling"
            try:
                database.update_task_status(job.id, job.status)
            except Exception:
                _log.exception("db update_task_status failed for %s", job.id)
        return {"ok": True}

    # ── /v1/scrape（一次调用返回全部产物，对标 Firecrawl）──────────────

    def _read_scrape_request(self) -> tuple[str, bool]:
        """解析 ``POST /v1/scrape``：JSON 请求体（首选）或表单编码。

        JSON 形如 ``{"url": "...", "include_html": false}``；表单为
        ``url=...&include_html=1``。两者都缺省时回落到查询串，兼容
        ``POST /v1/scrape?url=...`` 的脚本写法。
        """
        query = parse_qs(urlparse(self.path).query)
        media_type = (self.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if media_type == "application/json":
            try:
                declared_length = int(self.headers.get("content-length", "0") or "0")
            except ValueError:
                declared_length = 0
            try:
                body = self.read_json_body(_SCRAPE_MAX_BODY_BYTES)
            except ValueError as exc:  # 请求体超限
                raise ScrapeError(400, "invalid_body", str(exc)) from exc
            if body is None:
                if declared_length > 0:
                    raise ScrapeError(400, "invalid_body", "请求体不是合法 JSON")
                body = {}
            if not isinstance(body, dict):
                raise ScrapeError(400, "invalid_body", "请求体必须是 JSON 对象")
            url: object = body.get("url", query.get("url", [""])[0])
            include: object = body.get("include_html", query.get("include_html", [""])[0])
        else:
            form = self.read_form(keep_blank_values=True)
            url = form.get("url", query.get("url", [""]))[0]
            include = form.get("include_html", query.get("include_html", [""]))[0]
        if not isinstance(url, str):
            raise ScrapeError(400, "invalid_url", "url 必须是字符串")
        return url, _truthy_flag(include)

    def _run_scrape(self, url: str, include_html: bool) -> None:
        """执行一次抓取并输出 JSON；结构化错误映射为对应状态码。"""
        try:
            payload = scrape_page(url, include_html=include_html)
        except ScrapeError as exc:
            self._scrape_error(exc, url)
            return
        self.respond_json(payload)

    def _scrape_error(self, exc: ScrapeError, url: str) -> None:
        payload: dict[str, object] = {
            "error": exc.code,
            "message": exc.message,
            "url": url,
        }
        payload.update(exc.details)
        self.respond_json(payload, status=exc.status)

    def do_GET(self) -> None:
        # DNS rebinding 防护：所有端点一视同仁，Host 不在白名单直接 403
        if not self._is_host_allowed():
            self._reject_host()
            return
        path = urlparse(self.path).path
        static_match = _STATIC_JS_RE.fullmatch(path)
        if static_match is not None:
            self._serve_static_js(static_match.group(1))
            return
        if path == "/v1/scrape":
            # GET 也要过同源校验：否则恶意页面可用 <img>/<script> 触发本机
            # 服务去抓任意公网 URL（POST 的 CSRF 防护可被 GET 绕过）。
            if not self._is_same_origin_request():
                self.respond_json({"ok": False, "error": "跨站请求被拒绝"}, status=403)
                return
            query = parse_qs(urlparse(self.path).query)
            self._run_scrape(
                query.get("url", [""])[0],
                _truthy_flag(query.get("include_html", [""])[0]),
            )
            return
        if self.path in {"/", "/index.html"}:
            self.respond(
                200,
                PAGE.replace("{block_keywords}", DEFAULT_BLOCK_KEYWORDS).encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return
        if path == "/status":
            query = parse_qs(urlparse(self.path).query)
            with JOBS_LOCK:
                if query.get("active") == ["1"]:
                    job = next((j for j in JOBS.values() if j.is_active()), None)
                else:
                    job = JOBS.get(query.get("id", [""])[0])
            if not job:
                self.respond_json({"status": "missing", "log": "任务不存在"})
                return
            self.respond_json(job.snapshot())
            return
        if path == "/activity":
            self.respond_json({"active": _active_task_counts()})
            return
        if self.path.startswith("/reverse/status"):
            query = parse_qs(urlparse(self.path).query)
            rjob = REVERSE_JOBS.get(query.get("id", [""])[0])
            if not rjob:
                self.respond_json({"status": "missing", "error": "任务不存在"})
                return
            self.respond_json(rjob.snapshot())
            return
        if self.path.startswith("/reverse/stream"):
            # SSE 实时推送：建立长连接，把事件增量推给前端
            query = parse_qs(urlparse(self.path).query)
            rjob = REVERSE_JOBS.get(query.get("id", [""])[0])
            if not rjob:
                self.respond_json({"error": "任务不存在"})
                return
            self._stream_reverse_sse(rjob)
            return
        if self.path.startswith("/reverse/jobs"):
            # 返回所有历史任务列表（按创建时间倒序）
            with REVERSE_JOBS_LOCK:
                jobs = [rj.job_summary() for rj in REVERSE_JOBS.values()]
            jobs.sort(key=lambda j: cast(float, j.get("created_at", 0.0)), reverse=True)
            self.respond_json({"jobs": jobs, "count": len(jobs)})
            return
        if self.path.startswith("/captcha/status"):
            query = parse_qs(urlparse(self.path).query)
            cjob = CAPTCHA_JOBS.get(query.get("id", [""])[0])
            if not cjob:
                self.respond_json({"status": "missing", "error": "任务不存在"})
                return
            self.respond_json(cjob.snapshot())
            return
        if self.path.startswith("/captcha/jobs"):
            with CAPTCHA_JOBS_LOCK:
                jobs = [cj.job_summary() for cj in CAPTCHA_JOBS.values()]
            jobs.sort(key=lambda j: cast(float, j.get("created_at", 0.0)), reverse=True)
            self.respond_json({"jobs": jobs, "count": len(jobs)})
            return
        if self.path.startswith("/reverse/script"):
            # 下载成功路径脚本：返回 JSON，含 Content-Disposition
            query = parse_qs(urlparse(self.path).query)
            rjob = REVERSE_JOBS.get(query.get("id", [""])[0])
            if not rjob:
                self.respond_json({"error": "任务不存在"})
                return
            script = rjob.compiled_script or ""
            if not script:
                self.respond_json({"error": "无可用脚本"})
                return
            filename = f"reverse_{rjob.id}.py"
            body = script.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/x-python; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("content-disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/reverse/screenshot"):
            # 返回 PNG 截图：?id=...&step=N[&error=1]
            query = parse_qs(urlparse(self.path).query)
            rjob = REVERSE_JOBS.get(query.get("id", [""])[0])
            if not rjob:
                self.send_error(404, "Task not found")
                return  # pragma: no cover - 截图端点防御性 404
            try:
                step_num = int(query.get("step", ["0"])[0])
            except ValueError:
                step_num = 0
            want_error = query.get("error", ["0"])[0] == "1"
            # 在 screenshots 列表中查找匹配的截图路径
            shot_path = ""
            for s in rjob.screenshots:
                if s.get("step") == step_num and bool(s.get("error")) == want_error:
                    shot_path = s.get("path", "")
                    break
            # 回退：取该 step 的任一截图
            if not shot_path:
                for s in rjob.screenshots:
                    if s.get("step") == step_num:
                        shot_path = s.get("path", "")
                        break
            if not shot_path:
                self.send_error(404, "Screenshot not found")
                return  # pragma: no cover - 截图端点防御性 404
            try:
                png = Path(shot_path).read_bytes()
            except OSError:
                self.send_error(404, "Screenshot file lost")
                return  # pragma: no cover - 截图端点防御性 404
            self.respond(200, png, "image/png")
            return
        # ── 任务历史 API ──────────────────────────────────────────
        query = parse_qs(urlparse(self.path).query)
        if path == "/jobs":
            try:
                page, page_size = _pagination(query, 20)
            except ValueError as exc:
                self.respond_json({"error": str(exc)}, status=400)
                return
            status = query.get("status", [None])[0]
            self.respond_json(database.list_tasks(page, page_size, status))
            return
        task_route = re.fullmatch(r"/jobs/([^/]+)(?:/(results|pages))?", path)
        if task_route is not None:
            task_id, view = task_route.groups()
            if view == "pages" and "url" in query:
                extraction = database.get_page_extraction(task_id, query["url"][0])
                self.respond_json(
                    extraction or {"error": "此任务没有该页面的提取结果"},
                    status=200 if extraction is not None else 404,
                )
                return
            if view:
                try:
                    page, page_size = _pagination(query, 20 if view == "pages" else 50)
                except ValueError as exc:
                    self.respond_json({"error": str(exc)}, status=400)
                    return
                if view == "pages":
                    self.respond_json(database.list_extracted_pages(task_id, page, page_size))
                else:
                    self.respond_json(
                        database.get_results(
                            task_id,
                            page,
                            page_size,
                            query.get("q", [None])[0],
                            page_url=query.get("source", [None])[0],
                        )
                    )
                return
            task = database.get_task(task_id)
            if not task:
                self.respond(404, b'{"error":"task not found"}', "application/json; charset=utf-8")
                return
            self.respond_json({**task, "page_sample_count": database.count_page_samples(task_id)})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        # DNS rebinding 防护：所有端点一视同仁，Host 不在白名单直接 403
        if not self._is_host_allowed():
            self._reject_host()
            return
        if not self._is_same_origin_request():
            # 先消费请求体再响应：未读尽的 body + keep-alive 会在 Windows 上
            # 触发连接重置（WinError 10053），客户端收不到 JSON 错误
            self.close_connection = True
            self.read_form()
            self.respond_json({"ok": False, "error": "跨站请求被拒绝"})
            return
        query = parse_qs(urlparse(self.path).query)
        path = urlparse(self.path).path
        if path == "/v1/scrape":
            try:
                url, include_html = self._read_scrape_request()
            except ScrapeError as exc:
                self._scrape_error(exc, "")
                return
            self._run_scrape(url, include_html)
            return
        if path in {"/crawl/config/import", "/crawl/config/export"}:
            try:
                if path.endswith("/import"):
                    config = normalize_crawl_config(self.read_json_body(2 * MAX_RULES_BYTES))
                else:
                    config = portable_crawl_config(
                        build_args(self.read_form(keep_blank_values=True))
                    )
            except ValueError as exc:
                self.respond_json({"error": str(exc)}, status=400)
                return
            self.respond_json({"config": config})
            return
        rules_route = re.fullmatch(r"/jobs/([^/]+)/validate-rules", path)
        if rules_route is not None:
            try:
                rules = ExtractionRules.from_dict(self.read_json_body(MAX_RULES_BYTES))
                page_url = query.get("url", [""])[0]
                sample = database.get_page_sample(rules_route[1], page_url)
                if sample is None:
                    self.respond_json({"error": "没有保留此页的验证样本"}, status=404)
                    return
                result = rules.extract(sample, page_url=page_url)
            except ValueError as exc:
                self.respond_json({"error": str(exc)}, status=400)
                return
            self.respond_json({"rules": rules.to_dict(), "result": result})
            return
        audit_route = re.fullmatch(r"/jobs/([^/]+)/audit-rules", path)
        if audit_route is not None:
            try:
                rules = ExtractionRules.from_dict(self.read_json_body(MAX_RULES_BYTES))
            except ValueError as exc:
                self.respond_json({"error": str(exc)}, status=400)
                return
            task_id = audit_route[1]
            job = JOBS.get(task_id)
            if job is not None and job.is_active():
                self.respond_json({"error": "采集结束后可验证整批样本"}, status=409)
                return
            after = query.get("after", [""])[0]
            sample_count = database.count_page_samples(task_id) if not after else None
            samples = database.page_samples_after(task_id, after, _PAGE_AUDIT_BATCH_SIZE + 1)
            audit = rules.coverage()
            for page_url, compressed in samples[:_PAGE_AUDIT_BATCH_SIZE]:
                try:
                    result = rules.extract(
                        database.decode_page_sample(compressed), page_url=page_url
                    )
                except ValueError as exc:
                    audit.add_error(page_url, exc)
                else:
                    audit.add_result(result)
            next_after = (
                samples[_PAGE_AUDIT_BATCH_SIZE - 1][0]
                if len(samples) > _PAGE_AUDIT_BATCH_SIZE
                else None
            )
            with suppress(ConnectionError):  # 用户可以在浏览器中取消当前批次。
                self.respond_json(
                    {
                        "rules": rules.to_dict(),
                        "coverage": audit.report(),
                        "next_after": next_after,
                        "total_samples": sample_count,
                    }
                )
            return
        if path == "/run":
            form = self.read_form(keep_blank_values=True)
            try:
                args = build_args(form)
            except ValueError as exc:
                self.respond_json({"ok": False, "error": str(exc)})
                return
            self._start_crawl(args)
            return
        if path == "/retry":
            # 用库里的任务记录重跑一次：配置来自入库白名单，不碰 Cookie
            task_id = query.get("id", [""])[0]
            retry_task = database.get_task(task_id)
            if not retry_task:
                self.respond_json({"ok": False, "error": "任务不存在"})
                return
            try:
                args = build_args_from_task(retry_task)
            except ValueError as exc:
                self.respond_json({"ok": False, "error": str(exc)})
                return
            self._start_crawl(args)
            return
        if path in {"/pause", "/resume", "/cancel"}:
            existing = JOBS.get(query.get("id", [""])[0])
            if not existing:
                self.respond_json({"ok": False, "message": "任务不存在"})
                return
            self.respond_json(self._control_crawl(existing, path))
            return
        if path == "/open-output":
            form = self.read_form()
            out_dir = Path(output_path(form.get("out", [DEFAULT_OUTPUT])[0]))
            # 白名单：仅允许打开默认输出目录或已登记任务的输出目录（含子目录），
            # 防止任意路径被 os.startfile 打开/启动（Windows 上可启动可执行文件）
            resolved = out_dir.resolve()
            with JOBS_LOCK:
                allowed_roots = {str(Path(j.output_dir).resolve()) for j in JOBS.values()}
            allowed_roots.add(str(Path(DEFAULT_OUTPUT).resolve()))
            if not any(
                resolved == Path(root).resolve() or Path(root).resolve() in resolved.parents
                for root in allowed_roots
            ):
                self.respond_json({"ok": False, "message": "目录不在允许范围内"})
                return
            try:
                if not out_dir.exists():
                    out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.respond_json({"ok": False, "message": f"无法创建目录：{exc}"})
                return
            if not out_dir.is_dir():
                self.respond_json({"ok": False, "message": "路径不是目录"})
                return
            try:
                _open_folder(str(out_dir))
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
            rev_config: dict[str, Any] = build_reverse_config(form)
            job_id = uuid.uuid4().hex[:12]
            rjob = ReverseJobState(
                id=job_id,
                url=url,
                task=task,
                config=rev_config,
                max_steps=int(rev_config.get("max_steps", 20)),
                target_params=list(rev_config.get("target_params") or []),
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
        if path == "/captcha/run":
            form = self.read_form()
            url = form.get("url", [""])[0].strip()
            headless = form.get("headless", [""])[0] == "1"
            if not url:
                self.respond_json({"ok": False, "error": "URL 不能为空"})
                return
            job_id = uuid.uuid4().hex[:12]
            cjob = CaptchaJobState(id=job_id, url=url, headless=headless)
            with CAPTCHA_JOBS_LOCK:
                CAPTCHA_JOBS[job_id] = cjob
                if len(CAPTCHA_JOBS) > MAX_CAPTCHA_JOBS:
                    for jid in list(CAPTCHA_JOBS.keys()):
                        cj = CAPTCHA_JOBS[jid]
                        if cj.status in ("done", "error"):
                            del CAPTCHA_JOBS[jid]
            threading.Thread(target=run_captcha_job, args=(cjob,), daemon=True).start()
            self.respond_json({"id": job_id, "status": "running"})
            return
        if path == "/captcha/image":
            form = self.read_form()
            image_b64 = form.get("image", [""])[0].strip()
            math_mode = form.get("math", [""])[0] == "1"
            if not image_b64:
                self.respond_json({"ok": False, "error": "缺少图片数据"})
                return
            try:
                from ..ai.image_captcha import ImageCaptchaSolver, ImageSolverConfig
                from ..ai.llm import get_vision_provider

                provider = get_vision_provider(None)
                solver = ImageCaptchaSolver(
                    provider=provider, config=ImageSolverConfig(math_mode=math_mode)
                )
                raw = (
                    base64.b64decode(image_b64.split(",", 1)[1])
                    if image_b64.startswith("data:")
                    else base64.b64decode(image_b64)
                )
                text = solver.solve_text(raw, mime="image/png")
            except Exception as exc:
                self.respond_json({"ok": False, "error": f"识别失败: {exc}"})
                return
            self.respond_json({"ok": bool(text), "text": text, "math": math_mode})
            return
        if path == "/reverse/stop":
            rstop = REVERSE_JOBS.get(query.get("id", [""])[0])
            if not rstop:
                self.respond_json({"ok": False, "message": "任务不存在"})
                return
            # stop_event 由库侧 should_stop 回调消费:Agent 在每步循环顶部
            # 检查该标志,返回 True 立即中断并标记 cancelled
            rstop.stop_event.set()
            self.respond_json(
                {
                    "ok": True,
                    "message": "已请求停止:Agent 将在当前动作完成后立即中断",
                }
            )
            return
        if path == "/reverse/clear":
            # 清空指定任务的运行时数据（保留最终结果）
            rjob_existing = REVERSE_JOBS.get(query.get("id", [""])[0])
            if not rjob_existing:
                self.respond_json({"ok": False, "message": "任务不存在"})
                return
            rjob_existing.clear_runtime()
            self.respond_json({"ok": True, "id": rjob_existing.id})
            return
        if path == "/reverse/config/export":
            # 接收表单，返回 JSON 配置文件下载
            form = self.read_form()
            rev_config = build_reverse_config(form)
            body = json.dumps(rev_config, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("content-disposition", 'attachment; filename="reverse_config.json"')
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/reverse/config/import":
            # 接收 JSON 配置，返回标准化后的 config dict（前端用来回填表单）
            data = self.read_json_body()
            if not isinstance(data, dict):
                self.respond_json({"error": "配置文件必须是 JSON 对象"})
                return
            # 标准化：补全缺失字段，剔除未知字段，类型转换
            normalized = _normalize_imported_config(data)
            self.respond_json({"config": normalized})
            return
        self.send_error(404)

    def do_DELETE(self) -> None:
        # DNS rebinding 防护：所有端点一视同仁，Host 不在白名单直接 403
        if not self._is_host_allowed():
            self._reject_host()
            return
        if not self._is_same_origin_request():
            self.close_connection = True  # 拒绝后关闭连接，避免 keep-alive 状态错乱
            self.respond_json({"ok": False, "error": "跨站请求被拒绝"})
            return
        path = urlparse(self.path).path
        if path.startswith("/jobs/"):
            task_id = path.split("/jobs/")[1]
            job = JOBS.get(task_id)
            if job is not None and job.is_active():
                self.respond_json({"ok": False, "error": "请等待采集结束后再删除记录"})
            else:
                self.respond_json({"ok": database.delete_task(task_id)})
            return
        self.send_error(404)  # pragma: no cover

    def read_form(self, *, keep_blank_values: bool = False) -> dict[str, list[str]]:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(body, keep_blank_values=keep_blank_values)

    def _is_host_allowed(self) -> bool:
        """Host 头白名单校验入口：取 socket 实际绑定地址后调用纯函数判定。

        绑定地址来自 ``self.server.server_address``（bind 后由
        ``getsockname()`` 写回实际端口），Handler 无需感知启动参数。
        """
        # typeshed 将 BaseServer.server_address 标注为宽类型（str/Buffer 也合法），
        # ThreadingHTTPServer 实际恒为 (host, port) 二元组，cast 收窄
        bind_host, bind_port = cast("tuple[str, int]", self.server.server_address)
        host_header = self.headers.get("Host", "")
        return _host_header_allowed(host_header, bind_host, bind_port)

    def _reject_host(self) -> None:
        """Host 白名单校验失败：统一 403 + 简短 JSON 错误体。"""
        # 先消费请求体再响应：未读尽的 body + keep-alive 会在 Windows 上
        # 触发连接重置（WinError 10053），客户端收不到 JSON 错误
        self.close_connection = True
        if self.command == "POST":
            try:
                self.read_form()
            except (ValueError, OSError):  # pragma: no cover - 畸形请求的兜底
                pass
        self.respond_json(
            {"ok": False, "error": "Host 校验失败，请求被拒绝（DNS rebinding 防护）"},
            status=403,
        )

    def _is_same_origin_request(self) -> bool:
        """CSRF 防护：校验 Origin/Referer 与请求 Host 同源。

        无 Origin/Referer 的请求（curl、本机脚本）放行；
        携带跨站 Origin/Referer 的请求（恶意网页表单/图片）一律拒绝。

        与 Host 白名单（``_is_host_allowed``）构成纵深防御，互不可替代：
        Host 白名单拦 DNS rebinding（Origin==Host==attacker.com 的场景
        Host 不在白名单）；本方法拦经典跨站 CSRF（恶意页面直连本机
        地址，Origin≠Host——此时 Host 合法、白名单放行）。
        在 Host 校验加固后仍有保留必要。
        """
        host = self.headers.get("Host", "")
        for header_name in ("Origin", "Referer"):
            value = (self.headers.get(header_name) or "").strip()
            if not value:
                continue
            if value == "null":  # sandboxed iframe 的 Origin 为 null,不可信
                return False
            parsed = urlparse(value)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                if parsed.netloc != host:
                    return False
            else:
                return False
        return True

    def read_json_body(self, max_bytes: int | None = None) -> object:
        """读取请求体并解析为 JSON；解析失败返回 None。"""
        length = int(self.headers.get("content-length", "0"))
        if max_bytes is not None and not 0 <= length <= max_bytes:
            self.close_connection = True
            raise ValueError("请求数据超出大小限制")
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def respond_json(self, payload: dict[str, object], status: int = 200) -> None:
        self.respond(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _stream_reverse_sse(self, rjob: ReverseJobState) -> None:
        """SSE 长连接：把任务事件增量推给前端，直到任务结束。

        端点 ``GET /reverse/stream?id=<job_id>``，响应 ``text/event-stream``。
        每 800ms 检查一次事件流，把 ``ts > since`` 的事件以 SSE 格式推送。
        任务进入终态（done/error/cancelled）后发送一个 final 事件并关闭连接。
        """
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        self.end_headers()

        import time as _time

        last_ts = 0.0
        deadline = _time.time() + 3600  # 单连接最多 1 小时，避免泄漏
        try:
            while _time.time() < deadline:
                # 检查 job 是否已被清理（新任务达 MAX_REVERSE_JOBS 时可能被移除）
                with REVERSE_JOBS_LOCK:
                    if rjob.id not in REVERSE_JOBS:
                        self.wfile.write(
                            b"event: gone\ndata: "
                            + json.dumps(
                                {"status": "removed", "message": "任务已从注册表移除"},
                                ensure_ascii=False,
                            ).encode("utf-8")
                            + b"\n\n"
                        )
                        self.wfile.flush()
                        return
                # 任务终态：先发 final，再退出
                with rjob.state_lock:
                    cur_status = rjob.status
                if cur_status in {"done", "error", "cancelled"}:
                    payload = rjob.snapshot()
                    payload["events_since"] = last_ts
                    self.wfile.write(
                        b"event: final\ndata: "
                        + json.dumps(payload, ensure_ascii=False).encode("utf-8")
                        + b"\n\n"
                    )
                    self.wfile.flush()
                    return
                # 推送增量事件
                with rjob.state_lock:
                    new_events = [e for e in rjob.events if e.get("ts", 0) > last_ts]
                    if new_events:
                        last_ts = new_events[-1].get("ts", last_ts)
                for evt in new_events:
                    self.wfile.write(
                        b"event: step\ndata: "
                        + json.dumps(evt, ensure_ascii=False).encode("utf-8")
                        + b"\n\n"
                    )
                if new_events:
                    self.wfile.flush()
                    # 有新事件时才推送完整快照（前端用于更新统计卡片）
                    snap = rjob.snapshot()
                    self.wfile.write(
                        b"event: snapshot\ndata: "
                        + json.dumps(snap, ensure_ascii=False).encode("utf-8")
                        + b"\n\n"
                    )
                    self.wfile.flush()
                else:
                    # 无新事件时：发送 SSE 保活注释，避免浏览器超时断开
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                _time.sleep(0.8)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - 客户端断开连接时触发
            # 客户端关闭连接
            return

    def _serve_static_js(self, name: str) -> None:
        """响应 ``/static/js/<name>``：ETag 复验，未改动回 304。"""
        try:
            body = (STATIC_DIR / "js" / name).read_bytes()
        except OSError:
            self.send_error(404)
            return
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("etag", etag)
            self.send_header("cache-control", _STATIC_CACHE_CONTROL)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-type", _STATIC_JS_CONTENT_TYPE)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", _STATIC_CACHE_CONTROL)
        self.send_header("etag", etag)
        self.end_headers()
        self.wfile.write(body)

    def respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return

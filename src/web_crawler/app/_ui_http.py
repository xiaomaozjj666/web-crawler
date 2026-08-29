"""Web UI 的 HTTP Handler 与路由（从 ``ui.py`` 拆出）。

``Handler`` 为 ``BaseHTTPRequestHandler`` 子类，承载控制面全部路由：
页面 / 状态查询 / SSE 流 / 任务历史 API / 采集与逆向任务的启动控制。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from web_crawler.app import db as database

from ._ui_helpers import (
    DEFAULT_BLOCK_KEYWORDS,
    DEFAULT_OUTPUT,
    PAGE,
    _normalize_imported_config,
    _open_folder,
    _task_config_for_db,
    build_args,
    build_reverse_config,
    output_path,
)
from ._ui_runner import run_job, run_reverse_job
from ._ui_state import (
    JOBS,
    JOBS_LOCK,
    MAX_JOBS,
    MAX_REVERSE_JOBS,
    REVERSE_JOBS,
    REVERSE_JOBS_LOCK,
    JobState,
    ReverseJobState,
)

_log = logging.getLogger(__name__)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
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
            page = int(query.get("page", ["1"])[0])
            page_size = int(query.get("page_size", ["20"])[0])
            status = query.get("status", [None])[0]
            self.respond_json(database.list_tasks(page, page_size, status))
            return
        if path.startswith("/jobs/") and "/results" not in path:
            task_id = path.split("/jobs/")[1]
            task = database.get_task(task_id)
            if not task:
                self.respond(404, b'{"error":"task not found"}', "application/json; charset=utf-8")
                return
            self.respond_json(task)
            return
        if path.startswith("/jobs/") and "/results" in path:
            task_id = path.split("/jobs/")[1].split("/results")[0]
            page = int(query.get("page", ["1"])[0])
            page_size = int(query.get("page_size", ["50"])[0])
            search = query.get("q", [None])[0]
            self.respond_json(database.get_results(task_id, page, page_size, search))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if not self._is_same_origin_request():
            # 先消费请求体再响应：未读尽的 body + keep-alive 会在 Windows 上
            # 触发连接重置（WinError 10053），客户端收不到 JSON 错误
            self.close_connection = True
            self.read_form()
            self.respond_json({"ok": False, "error": "跨站请求被拒绝"})
            return
        query = parse_qs(urlparse(self.path).query)
        path = urlparse(self.path).path
        if path == "/run":
            form = self.read_form()
            try:
                args = build_args(form)
            except ValueError as exc:
                self.respond_json({"ok": False, "error": str(exc)})
                return
            with JOBS_LOCK:
                # 并发保护：采集任务日志/共享 opener 非并发安全,同一时间只允许一个任务
                if any(j.status in ("running", "paused") for j in JOBS.values()):
                    self.respond_json(
                        {
                            "ok": False,
                            "error": "已有采集任务正在运行，请等待完成或先取消",
                        }
                    )
                    return
                job_id = uuid.uuid4().hex[:12]
                job = JobState(id=job_id, args=args, output_dir=str(Path(args.out).resolve()))
                JOBS[job_id] = job
                # 清理已完成的任务，防止内存泄漏
                if len(JOBS) > MAX_JOBS:
                    for jid in list(JOBS.keys()):
                        j = JOBS[jid]
                        if j.status in ("done", "error", "cancelled"):
                            del JOBS[jid]
            # 写入数据库（剔除 Cookie 等敏感头,不落明文）
            try:
                config = _task_config_for_db(args)
                database.create_task(job_id, args.url, config, job.output_dir)
            except Exception:  # pragma: no cover
                _log.debug("db create_task failed", exc_info=True)
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
            self.respond_json({"id": job_id, "status": "running"})
            return
        if path in {"/pause", "/resume", "/cancel"}:
            existing = JOBS.get(query.get("id", [""])[0])
            if not existing:
                self.respond_json({"ok": False, "message": "任务不存在"})
                return
            if path == "/pause":
                existing.pause_event.clear()
                with existing.lock:
                    existing.status = "paused"
            elif path == "/resume":
                existing.pause_event.set()
                with existing.lock:
                    existing.status = "running"
            elif path == "/cancel":
                existing.stop_event.set()
                existing.pause_event.set()
                with existing.lock:
                    existing.status = "cancelled"
            # 同步状态到数据库：终态不覆盖（避免迟到的控制请求改写已完成任务的状态）
            with existing.lock:
                db_status = existing.status
            if db_status not in ("done", "error", "cancelled", "missing"):
                try:
                    database.update_task_status(existing.id, db_status)
                except Exception:  # pragma: no cover
                    _log.debug("db update_task_status failed", exc_info=True)
            self.respond_json({"ok": True})
            return
        if path == "/open-output":
            form = self.read_form()
            out_dir = Path(output_path(form.get("out", [DEFAULT_OUTPUT])[0]))
            # 白名单：仅允许打开默认输出目录或已登记任务的输出目录（含子目录），
            # 防止任意路径被 os.startfile 打开/启动（Windows 上可启动可执行文件）
            resolved = out_dir.resolve()
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
        if not self._is_same_origin_request():
            self.close_connection = True  # 拒绝后关闭连接，避免 keep-alive 状态错乱
            self.respond_json({"ok": False, "error": "跨站请求被拒绝"})
            return
        path = urlparse(self.path).path
        if path.startswith("/jobs/"):
            task_id = path.split("/jobs/")[1]
            deleted = database.delete_task(task_id)
            self.respond_json({"ok": deleted})
            return
        self.send_error(404)  # pragma: no cover

    def read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(body)

    def _is_same_origin_request(self) -> bool:
        """CSRF 防护：校验 Origin/Referer 与请求 Host 同源。

        无 Origin/Referer 的请求（curl、本机脚本）放行；
        携带跨站 Origin/Referer 的请求（恶意网页表单/图片）一律拒绝。
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

    def read_json_body(self) -> object:
        """读取请求体并解析为 JSON；解析失败返回 None。"""
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def respond_json(self, payload: dict[str, object]) -> None:
        self.respond(
            200,
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

    def respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return

"""Web UI 的 POST 逆向族路由（从 :mod:`web_crawler.app._ui_http` 拆出）。

覆盖 ``/reverse/run``、``/reverse/stop``、``/reverse/clear``、
``/reverse/config/export`` 与 ``/reverse/config/import``。

拆分只做结构搬移：URL 路径、请求/响应字段、状态码、错误文案与分支顺序
逐字保持原样。被测试 ``patch.object(_ui_http, "run_reverse_job")`` 的
模块全局名留在 :mod:`web_crawler.app._ui_http`：本模块只做路由判定与
任务登记，子线程启动经 ``Handler._launch_reverse_job`` 触发，patch
语义与拆分前一致。
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from ._ui_helpers import _normalize_imported_config, build_reverse_config
from ._ui_state import (
    MAX_REVERSE_JOBS,
    REVERSE_JOBS,
    REVERSE_JOBS_LOCK,
    ReverseJobState,
)

if TYPE_CHECKING:  # 仅类型：运行期由 _ui_http 导入本模块，反向导入会成环
    from web_crawler.app._ui_http import Handler


def dispatch(handler: Handler, path: str, query: dict[str, list[str]]) -> bool:
    """命中逆向族路由时处理并返回 ``True``；未命中返回 ``False``。"""
    if path == "/reverse/run":
        _post_reverse_run(handler)
        return True
    if path == "/reverse/stop":
        _post_reverse_stop(handler, query)
        return True
    if path == "/reverse/clear":
        _post_reverse_clear(handler, query)
        return True
    if path == "/reverse/config/export":
        _post_reverse_config_export(handler)
        return True
    if path == "/reverse/config/import":
        _post_reverse_config_import(handler)
        return True
    return False


def _post_reverse_run(handler: Handler) -> None:
    """``POST /reverse/run``：登记逆向任务并启动执行线程。"""
    form = handler.read_form()
    url = form.get("url", [""])[0].strip()
    task = form.get("task", [""])[0].strip()
    if not url:
        handler.respond_json({"ok": False, "error": "URL 不能为空"})
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
    handler._launch_reverse_job(rjob)
    handler.respond_json({"id": job_id, "status": "running"})


def _post_reverse_stop(handler: Handler, query: dict[str, list[str]]) -> None:
    """``POST /reverse/stop``：请求停止（库侧 should_stop 消费 stop_event）。"""
    rstop = REVERSE_JOBS.get(query.get("id", [""])[0])
    if not rstop:
        handler.respond_json({"ok": False, "message": "任务不存在"})
        return
    # stop_event 由库侧 should_stop 回调消费:Agent 在每步循环顶部
    # 检查该标志,返回 True 立即中断并标记 cancelled
    rstop.stop_event.set()
    handler.respond_json(
        {
            "ok": True,
            "message": "已请求停止:Agent 将在当前动作完成后立即中断",
        }
    )


def _post_reverse_clear(handler: Handler, query: dict[str, list[str]]) -> None:
    """``POST /reverse/clear``：清空指定任务的运行时数据（保留最终结果）。"""
    rjob_existing = REVERSE_JOBS.get(query.get("id", [""])[0])
    if not rjob_existing:
        handler.respond_json({"ok": False, "message": "任务不存在"})
        return
    rjob_existing.clear_runtime()
    handler.respond_json({"ok": True, "id": rjob_existing.id})


def _post_reverse_config_export(handler: Handler) -> None:
    """``POST /reverse/config/export``：接收表单，返回 JSON 配置文件下载。"""
    form = handler.read_form()
    rev_config = build_reverse_config(form)
    body = json.dumps(rev_config, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(200)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("content-length", str(len(body)))
    handler.send_header("content-disposition", 'attachment; filename="reverse_config.json"')
    handler.end_headers()
    handler.wfile.write(body)


def _post_reverse_config_import(handler: Handler) -> None:
    """``POST /reverse/config/import``：接收 JSON 配置，返回标准化后的 dict。"""
    data = handler.read_json_body()
    if not isinstance(data, dict):
        handler.respond_json({"error": "配置文件必须是 JSON 对象"})
        return
    # 标准化：补全缺失字段，剔除未知字段，类型转换
    normalized = _normalize_imported_config(data)
    handler.respond_json({"config": normalized})

"""Web UI 的 POST 采集族路由（从 :mod:`web_crawler.app._ui_http` 拆出）。

覆盖 ``/v1/scrape``、``/crawl/config/{import,export}``、
``/jobs/<id>/{validate,audit}-rules``、``/run``、``/retry``、
``/pause|/resume|/cancel`` 与 ``/open-output``。

拆分只做结构搬移：URL 路径、请求/响应字段、状态码、错误文案与分支顺序
逐字保持原样。被测试 ``patch.object(_ui_http, ...)`` 的模块全局名
（``run_job`` / ``_open_folder``）留在 :mod:`web_crawler.app._ui_http`：
本模块只做路由判定与请求解析，启动采集、控制任务、打开目录经
``Handler`` 的私有接缝（``_start_crawl`` / ``_control_crawl`` /
``_open_output_dir``）触发，patch 语义与拆分前一致。

:func:`dispatch` 只匹配本族独占的精确路径与正则，未命中返回 ``False``
交回 ``do_POST`` 继续判定；它与另外两个 POST 族模块的路径集合两两互斥。
"""

from __future__ import annotations

import re
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from web_crawler.app import db as database
from web_crawler.parser.rules import MAX_RULES_BYTES, ExtractionRules

from ._scrape import ScrapeError
from ._ui_helpers import (
    DEFAULT_OUTPUT,
    build_args,
    build_args_from_task,
    normalize_crawl_config,
    output_path,
    portable_crawl_config,
)
from ._ui_state import JOBS, JOBS_LOCK

if TYPE_CHECKING:  # 仅类型：运行期由 _ui_http 导入本模块，反向导入会成环
    from web_crawler.app._ui_http import Handler

# 每次 /audit-rules 请求处理的样本数上限（one-shot 分批，前端带 after 续批）
_PAGE_AUDIT_BATCH_SIZE = 16


def dispatch(handler: Handler, path: str, query: dict[str, list[str]]) -> bool:
    """命中采集族路由时处理并返回 ``True``；未命中返回 ``False``。"""
    if path == "/v1/scrape":
        _post_scrape(handler)
        return True
    if path in {"/crawl/config/import", "/crawl/config/export"}:
        _post_crawl_config(handler, path)
        return True
    rules_route = re.fullmatch(r"/jobs/([^/]+)/validate-rules", path)
    if rules_route is not None:
        _post_validate_rules(handler, query, rules_route[1])
        return True
    audit_route = re.fullmatch(r"/jobs/([^/]+)/audit-rules", path)
    if audit_route is not None:
        _post_audit_rules(handler, query, audit_route[1])
        return True
    if path == "/run":
        _post_run(handler)
        return True
    if path == "/retry":
        _post_retry(handler, query)
        return True
    if path in {"/pause", "/resume", "/cancel"}:
        _post_control(handler, path, query)
        return True
    if path == "/open-output":
        _post_open_output(handler)
        return True
    return False


def _post_scrape(handler: Handler) -> None:
    """``POST /v1/scrape``：请求体解析与执行沿用 Handler 上 GET 共用的方法。"""
    try:
        url, include_html = handler._read_scrape_request()
    except ScrapeError as exc:
        handler._scrape_error(exc, "")
        return
    handler._run_scrape(url, include_html)


def _post_crawl_config(handler: Handler, path: str) -> None:
    """``POST /crawl/config/{import,export}``：配置往返转换。"""
    try:
        if path.endswith("/import"):
            config = normalize_crawl_config(handler.read_json_body(2 * MAX_RULES_BYTES))
        else:
            config = portable_crawl_config(build_args(handler.read_form(keep_blank_values=True)))
    except ValueError as exc:
        handler.respond_json({"error": str(exc)}, status=400)
        return
    handler.respond_json({"config": config})


def _post_validate_rules(handler: Handler, query: dict[str, list[str]], task_id: str) -> None:
    """``POST /jobs/<id>/validate-rules``：用保留样本验证一组字段规则。"""
    try:
        rules = ExtractionRules.from_dict(handler.read_json_body(MAX_RULES_BYTES))
        page_url = query.get("url", [""])[0]
        sample = database.get_page_sample(task_id, page_url)
        if sample is None:
            handler.respond_json({"error": "没有保留此页的验证样本"}, status=404)
            return
        result = rules.extract(sample, page_url=page_url)
    except ValueError as exc:
        handler.respond_json({"error": str(exc)}, status=400)
        return
    handler.respond_json({"rules": rules.to_dict(), "result": result})


def _post_audit_rules(handler: Handler, query: dict[str, list[str]], task_id: str) -> None:
    """``POST /jobs/<id>/audit-rules``：按批在一批样本上统计规则覆盖率。"""
    try:
        rules = ExtractionRules.from_dict(handler.read_json_body(MAX_RULES_BYTES))
    except ValueError as exc:
        handler.respond_json({"error": str(exc)}, status=400)
        return
    job = JOBS.get(task_id)
    if job is not None and job.is_active():
        handler.respond_json({"error": "采集结束后可验证整批样本"}, status=409)
        return
    after = query.get("after", [""])[0]
    sample_count = database.count_page_samples(task_id) if not after else None
    samples = database.page_samples_after(task_id, after, _PAGE_AUDIT_BATCH_SIZE + 1)
    audit = rules.coverage()
    for page_url, compressed in samples[:_PAGE_AUDIT_BATCH_SIZE]:
        try:
            result = rules.extract(database.decode_page_sample(compressed), page_url=page_url)
        except ValueError as exc:
            audit.add_error(page_url, exc)
        else:
            audit.add_result(result)
    next_after = (
        samples[_PAGE_AUDIT_BATCH_SIZE - 1][0] if len(samples) > _PAGE_AUDIT_BATCH_SIZE else None
    )
    with suppress(ConnectionError):  # 用户可以在浏览器中取消当前批次。
        handler.respond_json(
            {
                "rules": rules.to_dict(),
                "coverage": audit.report(),
                "next_after": next_after,
                "total_samples": sample_count,
            }
        )


def _post_run(handler: Handler) -> None:
    """``POST /run``：表单参数 → 采集任务。"""
    form = handler.read_form(keep_blank_values=True)
    try:
        args = build_args(form)
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)})
        return
    handler._start_crawl(args)


def _post_retry(handler: Handler, query: dict[str, list[str]]) -> None:
    """``POST /retry``：用库里的任务记录重跑一次。

    配置来自入库白名单，不碰 Cookie。
    """
    task_id = query.get("id", [""])[0]
    retry_task = database.get_task(task_id)
    if not retry_task:
        handler.respond_json({"ok": False, "error": "任务不存在"})
        return
    try:
        args = build_args_from_task(retry_task)
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)})
        return
    handler._start_crawl(args)


def _post_control(handler: Handler, path: str, query: dict[str, list[str]]) -> None:
    """``POST /pause|/resume|/cancel``：按 id 控制进行中的采集任务。"""
    existing = JOBS.get(query.get("id", [""])[0])
    if not existing:
        handler.respond_json({"ok": False, "message": "任务不存在"})
        return
    handler.respond_json(handler._control_crawl(existing, path))


def _post_open_output(handler: Handler) -> None:
    """``POST /open-output``：在文件管理器中打开白名单内的输出目录。"""
    form = handler.read_form()
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
        handler.respond_json({"ok": False, "message": "目录不在允许范围内"})
        return
    try:
        if not out_dir.exists():
            out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        handler.respond_json({"ok": False, "message": f"无法创建目录：{exc}"})
        return
    if not out_dir.is_dir():
        handler.respond_json({"ok": False, "message": "路径不是目录"})
        return
    try:
        handler._open_output_dir(out_dir)
        handler.respond_json({"ok": True, "message": f"已打开：{out_dir}"})
    except Exception as exc:
        handler.respond_json({"ok": False, "message": f"无法打开：{exc}"})

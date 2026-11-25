"""Web UI 的 POST 验证码族路由（从 :mod:`web_crawler.app._ui_http` 拆出）。

覆盖 ``/captcha/run``（一键识别任务）与 ``/captcha/image``（图片直接识别）。

拆分只做结构搬移：URL 路径、请求/响应字段、状态码、错误文案与分支顺序
逐字保持原样。被测试 ``patch.object(_ui_http, "run_captcha_job")`` 的
模块全局名留在 :mod:`web_crawler.app._ui_http`：本模块只做路由判定与
任务登记，子线程启动经 ``Handler._launch_captcha_job`` 触发，patch
语义与拆分前一致。
"""

from __future__ import annotations

import base64
import uuid
from typing import TYPE_CHECKING

from ._ui_state import (
    CAPTCHA_JOBS,
    CAPTCHA_JOBS_LOCK,
    MAX_CAPTCHA_JOBS,
    CaptchaJobState,
)

if TYPE_CHECKING:  # 仅类型：运行期由 _ui_http 导入本模块，反向导入会成环
    from web_crawler.app._ui_http import Handler


def dispatch(handler: Handler, path: str, query: dict[str, list[str]]) -> bool:
    """命中验证码族路由时处理并返回 ``True``；未命中返回 ``False``。

    签名与另外两个 POST 族模块一致；``query`` 由 ``do_POST`` 统一解析传入，
    本族两个端点都只读表单体，不使用查询串。
    """
    if path == "/captcha/run":
        _post_captcha_run(handler)
        return True
    if path == "/captcha/image":
        _post_captcha_image(handler)
        return True
    return False


def _post_captcha_run(handler: Handler) -> None:
    """``POST /captcha/run``：登记一键识别任务并启动执行线程。"""
    form = handler.read_form()
    url = form.get("url", [""])[0].strip()
    headless = form.get("headless", [""])[0] == "1"
    if not url:
        handler.respond_json({"ok": False, "error": "URL 不能为空"})
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
    handler._launch_captcha_job(cjob)
    handler.respond_json({"id": job_id, "status": "running"})


def _post_captcha_image(handler: Handler) -> None:
    """``POST /captcha/image``：对上传的图片直接做一次识别。"""
    form = handler.read_form()
    image_b64 = form.get("image", [""])[0].strip()
    math_mode = form.get("math", [""])[0] == "1"
    if not image_b64:
        handler.respond_json({"ok": False, "error": "缺少图片数据"})
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
        handler.respond_json({"ok": False, "error": f"识别失败: {exc}"})
        return
    handler.respond_json({"ok": bool(text), "text": text, "math": math_mode})

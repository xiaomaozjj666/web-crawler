"""``_ui_post_*`` 分派入口的最小单测（``do_POST`` 拆分后新增的私有入口）。

只覆盖路由族判定的边界与原文案的保真：各族只认自己的路径、未命中返回
``False``（交回 ``do_POST`` 走 404）、命中返回 ``True`` 并写出与拆分前
逐字相同的载荷。分支内部行为由 ``test_app_ui.py`` 的真实 HTTP 用例覆盖，
这里不启动服务器、不起线程、不写全局注册表。
"""

from __future__ import annotations

import uuid

from web_crawler.app import _ui_post_captcha, _ui_post_crawl, _ui_post_reverse


class _StubHandler:
    """只实现族 dispatch 触达的 Handler 表面方法（不走真实 HTTP 连接）。"""

    def __init__(self, form: dict[str, list[str]] | None = None) -> None:
        self.form = form or {}
        self.payloads: list[dict[str, object]] = []
        self.statuses: list[int] = []

    def read_form(self, *, keep_blank_values: bool = False) -> dict[str, list[str]]:
        return self.form

    def respond_json(self, payload: dict[str, object], status: int = 200) -> None:
        self.payloads.append(payload)
        self.statuses.append(status)


def _missing_id() -> str:
    """构造一个必然不在注册表里的任务 id（不依赖其它测试的清场顺序）。"""
    return f"missing-{uuid.uuid4().hex[:8]}"


def test_unknown_path_is_not_claimed() -> None:
    handler = _StubHandler()
    dispatches = (
        _ui_post_crawl.dispatch,
        _ui_post_reverse.dispatch,
        _ui_post_captcha.dispatch,
    )
    for dispatch in dispatches:
        assert dispatch(handler, "/unknown-path", {}) is False
    assert handler.payloads == []


def test_crawl_family_ignores_other_family_paths() -> None:
    handler = _StubHandler()
    for path in (
        "/reverse/run",
        "/reverse/stop",
        "/reverse/clear",
        "/reverse/config/export",
        "/reverse/config/import",
        "/captcha/run",
        "/captcha/image",
    ):
        assert _ui_post_crawl.dispatch(handler, path, {}) is False
    assert handler.payloads == []


def test_reverse_family_ignores_other_family_paths() -> None:
    handler = _StubHandler()
    for path in (
        "/v1/scrape",
        "/run",
        "/retry",
        "/pause",
        "/resume",
        "/cancel",
        "/open-output",
        "/crawl/config/export",
        "/jobs/abc/validate-rules",
        "/jobs/abc/audit-rules",
        "/captcha/run",
        "/captcha/image",
    ):
        assert _ui_post_reverse.dispatch(handler, path, {}) is False
    assert handler.payloads == []


def test_captcha_family_ignores_other_family_paths() -> None:
    handler = _StubHandler()
    for path in (
        "/v1/scrape",
        "/run",
        "/retry",
        "/open-output",
        "/reverse/run",
        "/reverse/stop",
        "/reverse/clear",
        "/reverse/config/export",
        "/reverse/config/import",
    ):
        assert _ui_post_captcha.dispatch(handler, path, {}) is False
    assert handler.payloads == []


def test_captcha_image_without_payload_keeps_original_error() -> None:
    handler = _StubHandler()
    assert _ui_post_captcha.dispatch(handler, "/captcha/image", {}) is True
    assert handler.payloads == [{"ok": False, "error": "缺少图片数据"}]
    assert handler.statuses == [200]


def test_pause_missing_task_keeps_original_error() -> None:
    handler = _StubHandler()
    assert _ui_post_crawl.dispatch(handler, "/pause", {"id": [_missing_id()]}) is True
    assert handler.payloads == [{"ok": False, "message": "任务不存在"}]


def test_reverse_stop_missing_task_keeps_original_error() -> None:
    handler = _StubHandler()
    assert _ui_post_reverse.dispatch(handler, "/reverse/stop", {"id": [_missing_id()]}) is True
    assert handler.payloads == [{"ok": False, "message": "任务不存在"}]


def test_reverse_clear_missing_task_keeps_original_error() -> None:
    handler = _StubHandler()
    assert _ui_post_reverse.dispatch(handler, "/reverse/clear", {"id": [_missing_id()]}) is True
    assert handler.payloads == [{"ok": False, "message": "任务不存在"}]

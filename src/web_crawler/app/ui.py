#!/usr/bin/env python3
"""crawler.py 的本地 Web UI。

模块拆分（与 ``mcp/server.py`` 同型）：

- :mod:`web_crawler.app._ui_helpers` — 常量与纯函数（表单解析/校验/序列化）
- :mod:`web_crawler.app._ui_state` — 任务状态 dataclass 与进程内注册表
- :mod:`web_crawler.app._ui_runner` — 采集/逆向任务的子线程执行器
- :mod:`web_crawler.app._ui_http` — HTTP Handler 与全部路由

本模块保留 ``main()`` 入口并 re-export 全部历史导入路径，
``python -m web_crawler.app.ui`` 与 ``web_crawler.app.ui.main`` 用法不变。
"""

from __future__ import annotations

import logging
import sys
import threading
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path  # noqa: F401  (历史导入路径,测试 patch.object(ui.Path, ...) 依赖)

sys.dont_write_bytecode = True
_log = logging.getLogger(__name__)

from web_crawler.app import crawler as web_resource_crawler  # noqa: F401
from web_crawler.app import db as database
from web_crawler.app._ui_helpers import (
    _CONFIG_FIELD_SPECS,  # noqa: F401
    _DB_CONFIG_FIELDS,  # noqa: F401
    _PAGE_TEMPLATE_PATH,  # noqa: F401
    BASE_DIR,  # noqa: F401
    DEFAULT_BLOCK_KEYWORDS,  # noqa: F401
    DEFAULT_OUTPUT,  # noqa: F401
    HOST,
    PAGE,  # noqa: F401
    PORT,
    _as_float,  # noqa: F401
    _as_int,  # noqa: F401
    _is_loopback_host,
    _load_page_template,  # noqa: F401
    _normalize_imported_config,  # noqa: F401
    _open_folder,  # noqa: F401
    _serialize_analysis,  # noqa: F401
    _task_config_for_db,  # noqa: F401
    _validate_float_field,  # noqa: F401
    _validate_int_field,  # noqa: F401
    build_args,  # noqa: F401
    build_reverse_config,  # noqa: F401
    header_values,  # noqa: F401
    output_path,  # noqa: F401
)
from web_crawler.app._ui_http import Handler
from web_crawler.app._ui_runner import (
    ReverseAgentRunner,  # noqa: F401
    run_job,  # noqa: F401
    run_reverse_job,  # noqa: F401
    wait_for_resume,  # noqa: F401
)
from web_crawler.app._ui_state import (
    JOBS,  # noqa: F401
    JOBS_LOCK,  # noqa: F401
    MAX_JOBS,  # noqa: F401
    MAX_REVERSE_JOBS,  # noqa: F401
    REVERSE_JOBS,  # noqa: F401
    REVERSE_JOBS_LOCK,  # noqa: F401
    JobLogHandler,  # noqa: F401
    JobState,  # noqa: F401
    JobWriter,  # noqa: F401
    ReverseJobState,  # noqa: F401
)


def main() -> None:
    import argparse as _ap

    database.init_db()
    _stale = database.fail_stale_running_tasks()
    if _stale:
        _log.info("启动清理: %d 个上次未完成的任务已标记为中断", _stale)

    _parser = _ap.ArgumentParser(description="Web Resource Crawler UI")
    _parser.add_argument("--open", action="store_true", help="Automatically open browser")
    _parser.add_argument("--host", default=HOST, help=f"Server host (default: {HOST})")
    _parser.add_argument("--port", type=int, default=PORT, help=f"Server port (default: {PORT})")
    _parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="允许绑定非回环地址（如 0.0.0.0，供 Docker/远程访问）。"
        "注意：控制面无鉴权，仅建议在可信网络使用",
    )
    _args = _parser.parse_args()

    # 安全边界：控制面默认只允许绑定回环地址，禁止 0.0.0.0 暴露到局域网；
    # 容器/远程场景需显式 --allow-remote 放行（Dockerfile CMD 使用）
    if not _is_loopback_host(_args.host) and not _args.allow_remote:
        _parser.error(
            f"--host 只允许回环地址（127.x / ::1 / localhost），收到: {_args.host!r}；"
            "远程绑定请显式加 --allow-remote"
        )

    server = ThreadingHTTPServer((_args.host, _args.port), Handler)
    url = f"http://{_args.host}:{_args.port}"
    if _args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    _log.info("Web UI started: %s", url)
    _log.info("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log.info("Shutting down...")
        server.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()

"""ReverseMCPServer 的运行时契约(Mixin 宿主声明)。

三个 Mixin(:mod:`_tools_reverse` / :mod:`_tools_pentest` / :mod:`_transport`)
通过继承本类获得宿主属性的静态类型;全部成员由 ``server.ReverseMCPServer``
在运行时真正实现/赋值。
"""

from __future__ import annotations

from typing import Any


class HostContract:  # pragma: no cover - 纯类型契约,无运行时路径
    """宿主属性与方法契约(仅声明)。"""

    # -- 运行时属性(__init__ 赋值) -------------------------------------
    provider_name: str
    model: str
    provider: Any
    analyzer: Any
    captcha_manager: Any
    agent: Any
    _fetcher: Any
    _browser_lock: Any
    _progress_sender: Any
    _progress_lock: Any
    _closed: bool

    # -- 生命周期与浏览器 ------------------------------------------------
    def _create_agent(self) -> Any:
        raise NotImplementedError

    def _get_fetcher(self) -> Any:
        raise NotImplementedError

    def _run_browser_task(
        self,
        url: str,
        task_fn: Any,
        hooks: list[str] | None = None,
        wait_time: float = 0.0,
    ) -> Any: ...
    def close(self) -> None:
        raise NotImplementedError

    # -- 工具目录 / prompts / resources ----------------------------------
    def get_tools(self) -> list[dict]:
        raise NotImplementedError

    def get_prompts(self) -> list[dict]:
        raise NotImplementedError

    def render_prompt(self, name: str, arguments: dict) -> str:
        raise NotImplementedError

    def get_resources(self) -> list[dict]:
        raise NotImplementedError

    def read_resource(self, uri: str) -> str:
        raise NotImplementedError

    # -- 进度 / 校验 / 分发 ------------------------------------------------
    def make_progress_token(self, tool_name: str, total: int = 1) -> dict:
        raise NotImplementedError

    def report_progress(
        self, token: str, current: int, total: int, *, message: str = ""
    ) -> None: ...
    def _validate_tool_args(self, name: str, args: dict) -> str | None:
        raise NotImplementedError

    def handle_tool(self, name: str, arguments: dict) -> str:
        raise NotImplementedError

"""robots.txt 闸门的公共实现：按主机缓存、拉取失败保守视为允许。

供 Spider 框架与 AIScrapeAgent 共用；拉取方式经 ``fetch_text`` 注入，
本模块自身不做网络 IO 之外的策略决策。
"""

from __future__ import annotations

import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser


class RobotsPolicy:
    """带按主机缓存的小型 ``robots.txt`` 闸门（仅用标准库）。

    ``fetch_text`` 由调用方注入：接收 robots.txt URL、返回其文本内容；
    拉取失败（网络错误、超时）时按空规则解析，即保守视为全允许。
    """

    def __init__(self, user_agent: str = "*") -> None:
        self.user_agent = user_agent
        self._cache: dict[str, RobotFileParser] = {}

    def _parser_for(self, url: str, fetch_text: Any) -> RobotFileParser | None:
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        if host in self._cache:
            return self._cache[host]
        robots_url = urljoin(host, "/robots.txt")
        rp = RobotFileParser()
        try:
            text = fetch_text(robots_url)
            rp.parse(text.splitlines())
        except Exception:
            rp = RobotFileParser()
            rp.parse([])
        self._cache[host] = rp
        return rp

    def allowed(self, url: str, fetch_text: Any) -> bool:
        rp = self._parser_for(url, fetch_text)
        if rp is None:  # pragma: no cover - _parser_for 始终返回 RobotFileParser
            return True
        return rp.can_fetch(self.user_agent, url)


def fetch_robots_text(url: str, timeout: float = 10.0) -> str:
    """标准库默认拉取函数：Spider 等无自定义限速组件时使用。"""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")

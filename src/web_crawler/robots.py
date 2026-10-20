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

    def crawl_delay(self, url: str, fetch_text: Any = None) -> float | None:
        """返回 ``url`` 所在站点 robots.txt 为本 UA 声明的 Crawl-delay（秒）。

        复用 :meth:`_parser_for` 的按主机缓存（与 :meth:`allowed` 共享，
        同主机至多一次 robots.txt 外呼）。多 UA 匹配语义与 ``can_fetch``
        同源：具体 UA 组按声明顺序优先命中，含 ``*`` 的组作为兜底；
        标准库对非纯数字的 Crawl-delay 行（负数、小数、文本）本身即忽略。

        以下情形均返回 ``None``（调用方据此视为"站点未施加额外延迟要求"）：
        站点未声明 Crawl-delay、声明值无效（负数/非数字，统一经 ``float``
        归一化后钳制）、robots.txt 拉取失败（按空规则解析，保守视为
        允许故同样无延迟）。

        ``fetch_text`` 注入模式与 :meth:`allowed` 一致；缺省时回退模块级
        :func:`fetch_robots_text`（运行时解析，便于测试整体替换）。
        """
        if fetch_text is None:
            fetch_text = fetch_robots_text
        rp = self._parser_for(url, fetch_text)
        if rp is None:  # pragma: no cover - _parser_for 始终返回 RobotFileParser
            return None
        delay = rp.crawl_delay(self.user_agent)
        if delay is None:
            return None
        # typeshed 把 crawl_delay 标注为 str | None（运行时实为 int|None），
        # float() 归一化：任何非数字（含防御性分支）都保守视为未声明
        try:
            value = float(delay)
        except ValueError:
            return None
        if value < 0:
            return None
        return value


def fetch_robots_text(url: str, timeout: float = 10.0) -> str:
    """标准库默认拉取函数：Spider 等无自定义限速组件时使用。"""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")

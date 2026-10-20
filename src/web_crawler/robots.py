"""robots.txt 闸门的公共实现：按主机缓存、拉取失败保守视为允许。

供 Spider 框架与 AIScrapeAgent 共用；拉取方式经 ``fetch_text`` 注入，
本模块自身不做网络 IO 之外的策略决策。
"""

from __future__ import annotations

import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

Group = tuple[list[str], list[tuple[str, str]]]


def _iter_groups(lines: list[str]) -> list[Group]:
    """把 robots.txt 切成 ``(user-agent 列表, 规则行)`` 序列。

    连续的 ``User-agent`` 行属于同一组；规则行之后再出现 ``User-agent``
    即开启新组。注释与空行丢弃，没有 ``User-agent`` 归属的规则行忽略。
    """
    groups: list[Group] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif agents:
            rules.append((key, value))
    if agents:
        groups.append((agents, rules))
    return groups


def _group_delay(rules: list[tuple[str, str]]) -> float | None:
    """该组声明的 Crawl-delay（秒）；未声明或首条声明非法则返回 None。

    与标准库一致：只接受纯数字，「负数 / 小数 / 文本」一律视为未声明，
    因此本组不参与节流判定。
    """
    for key, value in rules:
        if key != "crawl-delay":
            continue
        if not value.isdigit():
            return None
        return float(int(value))
    return None


def _first_crawl_delay(lines: list[str], user_agent: str) -> float | None:
    """取第一个适用组的 Crawl-delay；具体 UA 组优先于 ``*`` 兜底组。

    刻意不使用 ``RobotFileParser.crawl_delay``：标准库自 3.13 起把「多个同名
    组」的选择由「首个」改成「末个」（同时才引入 ``entries``），同一份
    robots.txt 在 3.10–3.12 与 3.13+ 会得出不同的节流值。这里自行扫描，
    让所有受支持版本的结论一致；匹配规则与标准库同源：UA 取 ``/`` 之前的
    token，小写后按子串匹配。
    """
    token = user_agent.split("/")[0].strip().lower()
    specific: float | None = None
    wildcard: float | None = None
    for agents, rules in _iter_groups(lines):
        delay = _group_delay(rules)
        if delay is None:
            continue
        for agent in agents:
            if agent == "*":
                if wildcard is None:
                    wildcard = delay
            elif specific is None and agent in token:
                specific = delay
    return specific if specific is not None else wildcard


class RobotsPolicy:
    """带按主机缓存的小型 ``robots.txt`` 闸门（仅用标准库）。

    ``fetch_text`` 由调用方注入：接收 robots.txt URL、返回其文本内容；
    拉取失败（网络错误、超时）时按空规则解析，即保守视为全允许。
    """

    def __init__(self, user_agent: str = "*") -> None:
        self.user_agent = user_agent
        self._cache: dict[str, RobotFileParser] = {}
        # 原始行与解析器分开存：crawl_delay 需要自行扫描以保证跨版本一致，
        # 而 _cache 的值类型保持与旧版相同（外部预置解析器的用法仍然可用）。
        self._lines: dict[str, list[str]] = {}

    def _parser_for(self, url: str, fetch_text: Any) -> RobotFileParser | None:
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        if host in self._cache:
            return self._cache[host]
        robots_url = urljoin(host, "/robots.txt")
        rp = RobotFileParser()
        try:
            lines = fetch_text(robots_url).splitlines()
            rp.parse(lines)
        except Exception:
            rp = RobotFileParser()
            rp.parse([])
            lines = []
        self._cache[host] = rp
        self._lines[host] = lines
        return rp

    def allowed(self, url: str, fetch_text: Any) -> bool:
        rp = self._parser_for(url, fetch_text)
        if rp is None:  # pragma: no cover - _parser_for 始终返回 RobotFileParser
            return True
        return rp.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str, fetch_text: Any = None) -> float | None:
        """返回 ``url`` 所在站点 robots.txt 为本 UA 声明的 Crawl-delay（秒）。

        复用 :meth:`_parser_for` 的按主机缓存（与 :meth:`allowed` 共享，
        同主机至多一次 robots.txt 外呼）。多 UA 匹配语义见
        :func:`_first_crawl_delay`：具体 UA 组优先命中，含 ``*`` 的组作为
        兜底；纯数字以外的声明（负数、小数、文本）一律忽略。

        以下情形均返回 ``None``（调用方据此视为"站点未施加额外延迟要求"）：
        站点未声明 Crawl-delay、声明值无效、robots.txt 拉取失败（按空规则
        解析，保守视为允许故同样无延迟）。

        ``fetch_text`` 注入模式与 :meth:`allowed` 一致；缺省时回退模块级
        :func:`fetch_robots_text`（运行时解析，便于测试整体替换）。
        """
        if fetch_text is None:
            fetch_text = fetch_robots_text
        rp = self._parser_for(url, fetch_text)
        if rp is None:  # pragma: no cover - _parser_for 始终返回 RobotFileParser
            return None
        parsed = urlparse(url)
        lines = self._lines.get(f"{parsed.scheme}://{parsed.netloc}")
        if lines is not None:
            return _first_crawl_delay(lines, self.user_agent)
        # 解析器由外部直接预置（没有原始行）：退回标准库语义
        delay = rp.crawl_delay(self.user_agent)
        if delay is None:
            return None
        # typeshed 把 crawl_delay 标注为 str | None（运行时实为 int|None）
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

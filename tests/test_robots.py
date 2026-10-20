"""RobotsPolicy 单元测试：``allowed()`` 原有行为零回归 + Crawl-delay 解析。

Crawl-delay 用注入的 ``fetch_text`` 提供受控 robots.txt 文本（无网络），
并通过闭包计数验证按主机缓存语义（同主机至多一次外呼）。多 UA 匹配
语义与标准库 ``can_fetch`` 同源：具体 UA 组按声明顺序优先命中，含
``*`` 的组作为兜底（标准库 ``_add_entry`` 把含 ``*`` 的组放入
``default_entry``）。
"""

from __future__ import annotations

from typing import Any

import pytest

from web_crawler.robots import RobotsPolicy

HOST_A = "https://a.example"
HOST_B = "https://b.example"


def _counting_fetch(text: str) -> tuple[Any, list[str]]:
    """返回 ``(fetch_text 注入函数, 外呼 URL 记录表)``。"""
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return text

    return fetch, calls


def _failing_fetch() -> tuple[Any, list[str]]:
    """返回总是抛网络错误的 fetch_text 注入函数。"""
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        raise OSError("network unreachable")

    return fetch, calls


# ===========================================================================
# Crawl-delay 解析（新增能力）
# ===========================================================================


def test_crawl_delay_basic_value() -> None:
    """通配组声明 Crawl-delay: 5 时返回 5.0 秒。"""
    fetch, _ = _counting_fetch("User-agent: *\nDisallow: /private\nCrawl-delay: 5")
    policy = RobotsPolicy()
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) == pytest.approx(5.0)


def test_crawl_delay_returns_float_type() -> None:
    """返回值是 float（标准库内部存 int，对外统一转 float）。"""
    fetch, _ = _counting_fetch("User-agent: *\nCrawl-delay: 3")
    policy = RobotsPolicy()
    delay = policy.crawl_delay(f"{HOST_A}/x", fetch)
    assert isinstance(delay, float)
    assert delay == pytest.approx(3.0)


def test_crawl_delay_not_declared_returns_none() -> None:
    """站点声明了规则但未写 Crawl-delay 行时返回 None。"""
    fetch, _ = _counting_fetch("User-agent: *\nDisallow: /private")
    policy = RobotsPolicy()
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) is None


def test_crawl_delay_empty_robots_returns_none() -> None:
    """robots.txt 为空文本（404 等）时返回 None。"""
    fetch, _ = _counting_fetch("")
    policy = RobotsPolicy()
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) is None


def test_crawl_delay_wildcard_group_for_star_ua() -> None:
    """UA 为 "*" 时读取 "*" 组的 Crawl-delay。"""
    fetch, _ = _counting_fetch("User-agent: *\nCrawl-delay: 7")
    policy = RobotsPolicy(user_agent="*")
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) == pytest.approx(7.0)


def test_crawl_delay_specific_ua_group() -> None:
    """UA 匹配具体组时读取该组的 Crawl-delay。"""
    fetch, _ = _counting_fetch("User-agent: mybot\nCrawl-delay: 2")
    policy = RobotsPolicy(user_agent="mybot")
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) == pytest.approx(2.0)


def test_crawl_delay_specific_group_preferred_over_wildcard() -> None:
    """具体 UA 组优先于 "*" 兜底组：mybot 查到 2 而非 9。"""
    text = "User-agent: mybot\nCrawl-delay: 2\nUser-agent: *\nCrawl-delay: 9"
    fetch, _ = _counting_fetch(text)
    policy = RobotsPolicy(user_agent="mybot")
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) == pytest.approx(2.0)


def test_crawl_delay_star_ua_reads_wildcard_not_specific() -> None:
    """UA 为 "*" 时具体组不适用，读取 "*" 兜底组的 9 秒。"""
    text = "User-agent: mybot\nCrawl-delay: 2\nUser-agent: *\nCrawl-delay: 9"
    fetch, _ = _counting_fetch(text)
    policy = RobotsPolicy(user_agent="*")
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) == pytest.approx(9.0)


def test_crawl_delay_first_wildcard_group_wins() -> None:
    """多个 "*" 组时第一个胜出（标准库 default_entry 只记首个）。"""
    text = "User-agent: *\nCrawl-delay: 5\nUser-agent: *\nCrawl-delay: 9"
    fetch, _ = _counting_fetch(text)
    policy = RobotsPolicy()
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) == pytest.approx(5.0)


def test_crawl_delay_ua_token_substring_match() -> None:
    """UA 取 "/" 前的 token 匹配："mybot/1.0" 命中 "mybot" 组。"""
    fetch, _ = _counting_fetch("User-agent: mybot\nCrawl-delay: 3")
    policy = RobotsPolicy(user_agent="mybot/1.0")
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) == pytest.approx(3.0)


def test_crawl_delay_unrelated_ua_returns_none() -> None:
    """无 "*" 兜底组且 UA 不匹配任何具体组时返回 None。"""
    fetch, _ = _counting_fetch("User-agent: mybot\nCrawl-delay: 3")
    policy = RobotsPolicy(user_agent="otherbot")
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) is None


def test_crawl_delay_negative_value_ignored() -> None:
    """负数 Crawl-delay 非法：标准库 isdigit 校验直接忽略，返回 None。"""
    fetch, _ = _counting_fetch("User-agent: *\nCrawl-delay: -5")
    policy = RobotsPolicy()
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) is None


def test_crawl_delay_non_numeric_ignored() -> None:
    """非数字 Crawl-delay 行被忽略，返回 None。"""
    fetch, _ = _counting_fetch("User-agent: *\nCrawl-delay: abc")
    policy = RobotsPolicy()
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) is None


def test_crawl_delay_fractional_value_ignored() -> None:
    """小数 Crawl-delay 不被标准库接受（仅整数），返回 None。"""
    fetch, _ = _counting_fetch("User-agent: *\nCrawl-delay: 1.5")
    policy = RobotsPolicy()
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) is None


def test_crawl_delay_zero_is_valid() -> None:
    """Crawl-delay: 0 是合法声明，返回 0.0 而非 None。"""
    fetch, _ = _counting_fetch("User-agent: *\nCrawl-delay: 0")
    policy = RobotsPolicy()
    delay = policy.crawl_delay(f"{HOST_A}/x", fetch)
    assert delay is not None
    assert delay == pytest.approx(0.0)


def test_crawl_delay_large_value() -> None:
    """大值（小时级）不被截断。"""
    fetch, _ = _counting_fetch("User-agent: *\nCrawl-delay: 3600")
    policy = RobotsPolicy()
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) == pytest.approx(3600.0)


def test_crawl_delay_fetch_failure_returns_none() -> None:
    """robots.txt 拉取失败按空规则解析：无 Crawl-delay 要求。"""
    fetch, calls = _failing_fetch()
    policy = RobotsPolicy()
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) is None
    assert len(calls) == 1


def test_crawl_delay_fetch_failure_cached() -> None:
    """拉取失败的结果同样被缓存：同 host 再查不重复外呼。"""
    fetch, calls = _failing_fetch()
    policy = RobotsPolicy()
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) is None
    assert policy.crawl_delay(f"{HOST_A}/y", fetch) is None
    assert len(calls) == 1


def test_crawl_delay_cached_per_host() -> None:
    """同 host 多个 URL 只触发一次 robots.txt 外呼（解析器按 host 缓存）。"""
    fetch, calls = _counting_fetch("User-agent: *\nCrawl-delay: 4")
    policy = RobotsPolicy()
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) == pytest.approx(4.0)
    assert policy.crawl_delay(f"{HOST_A}/y", fetch) == pytest.approx(4.0)
    assert policy.crawl_delay(f"{HOST_A}/z", fetch) == pytest.approx(4.0)
    assert len(calls) == 1
    assert calls[0] == f"{HOST_A}/robots.txt"


def test_crawl_delay_hosts_isolated() -> None:
    """不同 host 各自拉取、各自延迟值，互不污染。"""
    texts = {
        f"{HOST_A}/robots.txt": "User-agent: *\nCrawl-delay: 1",
        f"{HOST_B}/robots.txt": "User-agent: *\nCrawl-delay: 6",
    }
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return texts[url]

    policy = RobotsPolicy()
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) == pytest.approx(1.0)
    assert policy.crawl_delay(f"{HOST_B}/x", fetch) == pytest.approx(6.0)
    assert policy.crawl_delay(f"{HOST_A}/y", fetch) == pytest.approx(1.0)
    assert sorted(calls) == [f"{HOST_A}/robots.txt", f"{HOST_B}/robots.txt"]


def test_crawl_delay_shares_parser_cache_with_allowed() -> None:
    """crawl_delay 与 allowed 共享同一按 host 缓存：先 allowed 再查延迟不重复外呼。"""
    fetch, calls = _counting_fetch("User-agent: *\nDisallow: /private\nCrawl-delay: 2")
    policy = RobotsPolicy()
    assert policy.allowed(f"{HOST_A}/public", fetch) is True
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) == pytest.approx(2.0)
    assert len(calls) == 1


def test_crawl_delay_default_fetch_text_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺省 fetch_text 时回退模块级 fetch_robots_text（运行时解析可替换）。"""
    calls: list[str] = []

    def fake_default_fetch(url: str) -> str:
        calls.append(url)
        return "User-agent: *\nCrawl-delay: 8"

    monkeypatch.setattr("web_crawler.robots.fetch_robots_text", fake_default_fetch)
    policy = RobotsPolicy()
    assert policy.crawl_delay(f"{HOST_A}/x") == pytest.approx(8.0)
    assert calls == [f"{HOST_A}/robots.txt"]


def test_crawl_delay_case_insensitive_directive() -> None:
    """指令名大小写不敏感（CRAWL-DELAY 同样生效）。"""
    fetch, _ = _counting_fetch("USER-AGENT: *\nCRAWL-DELAY: 4")
    policy = RobotsPolicy()
    assert policy.crawl_delay(f"{HOST_A}/x", fetch) == pytest.approx(4.0)


# ===========================================================================
# allowed() 原有行为零回归
# ===========================================================================


def test_allowed_empty_rules_permits_all() -> None:
    """空 robots.txt 视为全允许。"""
    fetch, _ = _counting_fetch("")
    policy = RobotsPolicy()
    assert policy.allowed(f"{HOST_A}/anything", fetch) is True


def test_allowed_disallow_private_path() -> None:
    """Disallow 声明的路径被拒绝。"""
    fetch, _ = _counting_fetch("User-agent: *\nDisallow: /private")
    policy = RobotsPolicy()
    assert policy.allowed(f"{HOST_A}/private/data", fetch) is False


def test_allowed_public_path_allowed() -> None:
    """未 Disallow 的路径放行。"""
    fetch, _ = _counting_fetch("User-agent: *\nDisallow: /private")
    policy = RobotsPolicy()
    assert policy.allowed(f"{HOST_A}/public", fetch) is True


def test_allowed_disallow_everything() -> None:
    """Disallow: / 表示全站拒绝。"""
    fetch, _ = _counting_fetch("User-agent: *\nDisallow: /")
    policy = RobotsPolicy()
    assert policy.allowed(f"{HOST_A}/x", fetch) is False


def test_allowed_allow_rule_first_match_wins() -> None:
    """先声明的 Allow 规则命中即放行（标准库按首条匹配，非最长匹配）。"""
    fetch, _ = _counting_fetch("User-agent: *\nAllow: /public\nDisallow: /")
    policy = RobotsPolicy()
    assert policy.allowed(f"{HOST_A}/public/page", fetch) is True
    assert policy.allowed(f"{HOST_A}/secret", fetch) is False


def test_allowed_specific_ua_disallow() -> None:
    """具体 UA 组的 Disallow 只约束该 UA。"""
    fetch, _ = _counting_fetch("User-agent: mybot\nDisallow: /private")
    policy = RobotsPolicy(user_agent="mybot")
    assert policy.allowed(f"{HOST_A}/private/x", fetch) is False
    assert policy.allowed(f"{HOST_A}/public", fetch) is True


def test_allowed_other_ua_unaffected_by_specific_group() -> None:
    """无 "*" 兜底组时，具体组规则不影响其他 UA（全允许）。"""
    fetch, _ = _counting_fetch("User-agent: mybot\nDisallow: /private")
    policy = RobotsPolicy(user_agent="otherbot")
    assert policy.allowed(f"{HOST_A}/private/x", fetch) is True


def test_allowed_fetch_failure_treated_as_allowed() -> None:
    """拉取失败保守视为允许（不让瞬时故障拦截爬取）。"""
    fetch, _ = _failing_fetch()
    policy = RobotsPolicy()
    assert policy.allowed(f"{HOST_A}/private", fetch) is True


def test_allowed_fetch_failure_cached_single_call() -> None:
    """失败判定被缓存：同 host 两次查询只外呼一次。"""
    fetch, calls = _failing_fetch()
    policy = RobotsPolicy()
    assert policy.allowed(f"{HOST_A}/a", fetch) is True
    assert policy.allowed(f"{HOST_A}/b", fetch) is True
    assert len(calls) == 1


def test_allowed_cached_per_host() -> None:
    """同 host 多 URL 只拉取一次 robots.txt。"""
    fetch, calls = _counting_fetch("User-agent: *\nDisallow: /private")
    policy = RobotsPolicy()
    assert policy.allowed(f"{HOST_A}/private", fetch) is False
    assert policy.allowed(f"{HOST_A}/public", fetch) is True
    assert len(calls) == 1


def test_allowed_hosts_isolated() -> None:
    """一个 host 的规则不影响另一个 host。"""
    texts = {f"{HOST_A}/robots.txt": "User-agent: *\nDisallow: /", f"{HOST_B}/robots.txt": ""}
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return texts[url]

    policy = RobotsPolicy()
    assert policy.allowed(f"{HOST_A}/x", fetch) is False
    assert policy.allowed(f"{HOST_B}/x", fetch) is True
    assert len(calls) == 2


def test_allowed_directives_case_insensitive_with_comments() -> None:
    """指令大写、行内注释均被正确处理。"""
    fetch, _ = _counting_fetch("USER-AGENT: *\nDisallow: /private # keep out")
    policy = RobotsPolicy()
    assert policy.allowed(f"{HOST_A}/private", fetch) is False
    assert policy.allowed(f"{HOST_A}/public", fetch) is True


def test_allowed_default_user_agent_is_wildcard() -> None:
    """默认 user_agent="*"：读取 "*" 组规则。"""
    fetch, _ = _counting_fetch("User-agent: *\nDisallow: /private")
    policy = RobotsPolicy()
    assert policy.user_agent == "*"
    assert policy.allowed(f"{HOST_A}/private", fetch) is False

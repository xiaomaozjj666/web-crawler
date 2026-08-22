"""Crawler 类的单元测试。

覆盖 crawler.py 中通过 mock httpx.AsyncClient 测试的网络相关方法：
``_get_robot_parser`` / ``_can_fetch`` / ``_throttle`` / ``fetch`` / ``crawl``。
不发起任何真实网络请求。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from web_crawler.crawler import Crawler, CrawlResult

# ---------------------------------------------------------------------------
# __init__ 配置组合
# ---------------------------------------------------------------------------


def test_init_default_values() -> None:
    """默认参数应正确赋值。"""
    c = Crawler()
    assert c.max_concurrency == 10
    assert c.delay == 0.2
    assert c.timeout == 15.0
    assert c.user_agent == "web-crawler/1.0"
    assert c.respect_robots is True
    assert c._robot_parsers == {}
    assert c._last_fetch == {}


def test_init_custom_values() -> None:
    """自定义参数应覆盖默认值。"""
    c = Crawler(
        max_concurrency=5,
        delay=1.0,
        timeout=30.0,
        user_agent="MyBot/2.0",
        respect_robots=False,
    )
    assert c.max_concurrency == 5
    assert c.delay == 1.0
    assert c.timeout == 30.0
    assert c.user_agent == "MyBot/2.0"
    assert c.respect_robots is False


# ---------------------------------------------------------------------------
# _get_robot_parser
# ---------------------------------------------------------------------------


async def test_get_robot_parser_success() -> None:
    """robots.txt 返回 200 时应解析并返回 parser。"""
    c = Crawler()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "User-agent: *\nDisallow: /private"

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("web_crawler.crawler.httpx.AsyncClient", return_value=mock_client):
        rp = await c._get_robot_parser("https", "example.com")

    assert rp is not None
    assert rp.can_fetch("web-crawler/1.0", "https://example.com/") is True
    assert rp.can_fetch("web-crawler/1.0", "https://example.com/private") is False


async def test_get_robot_parser_non_200() -> None:
    """robots.txt 非 200 时返回 None。"""
    c = Crawler()
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("web_crawler.crawler.httpx.AsyncClient", return_value=mock_client):
        rp = await c._get_robot_parser("https", "example.com")

    assert rp is None


async def test_get_robot_parser_exception() -> None:
    """获取 robots.txt 抛异常时返回 None。"""
    c = Crawler()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("web_crawler.crawler.httpx.AsyncClient", return_value=mock_client):
        rp = await c._get_robot_parser("https", "example.com")

    assert rp is None


# ---------------------------------------------------------------------------
# _can_fetch
# ---------------------------------------------------------------------------


async def test_can_fetch_respect_robots_disabled() -> None:
    """respect_robots=False 时直接返回 True。"""
    c = Crawler(respect_robots=False)
    assert await c._can_fetch("https://example.com/page") is True


async def test_can_fetch_uses_cached_parser() -> None:
    """同域名第二次调用应复用缓存的 parser，不重复请求。"""
    c = Crawler(respect_robots=True)
    c._robot_parsers["example.com"] = None  # 预置 None 表示无 robots 限制

    with patch.object(c, "_get_robot_parser", new=AsyncMock()) as mock_get:
        result1 = await c._can_fetch("https://example.com/a")
        result2 = await c._can_fetch("https://example.com/b")

    assert result1 is True
    assert result2 is True
    mock_get.assert_not_called()  # 复用缓存，未调用


async def test_can_fetch_with_blocking_parser() -> None:
    """有 parser 且禁止抓取时返回 False。"""
    c = Crawler(respect_robots=True)
    rp = MagicMock()
    rp.can_fetch = MagicMock(return_value=False)
    c._robot_parsers["example.com"] = rp

    assert await c._can_fetch("https://example.com/secret") is False
    rp.can_fetch.assert_called_once_with("web-crawler/1.0", "https://example.com/secret")


async def test_can_fetch_fetches_parser_when_missing() -> None:
    """无缓存 parser 时应调用 _get_robot_parser 获取。"""
    c = Crawler(respect_robots=True)
    rp = MagicMock()
    rp.can_fetch = MagicMock(return_value=True)

    with patch.object(c, "_get_robot_parser", new=AsyncMock(return_value=rp)) as mock_get:
        result = await c._can_fetch("https://example.com/")

    assert result is True
    mock_get.assert_awaited_once_with("https", "example.com")
    assert c._robot_parsers["example.com"] is rp


# ---------------------------------------------------------------------------
# _throttle
# ---------------------------------------------------------------------------


async def test_throttle_no_delay() -> None:
    """delay=0 时立即返回不 sleep。"""
    c = Crawler(delay=0)
    with patch("web_crawler.crawler.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await c._throttle("https://example.com/")
    mock_sleep.assert_not_called()


async def test_throttle_with_delay() -> None:
    """delay>0 且首次请求时不应 sleep（last=0，wait_for<=0 时跳过）。"""
    c = Crawler(delay=0.5)
    with patch("web_crawler.crawler.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await c._throttle("https://example.com/")
    # 首次请求 last=0.0，now-last 通常 >> delay，故 wait_for<=0 不 sleep
    mock_sleep.assert_not_called()


async def test_throttle_sleeps_when_too_soon() -> None:
    """同域名两次请求间隔不足 delay 时应 sleep 补足。"""
    c = Crawler(delay=1.0)
    # 模拟刚刚请求过（last ≈ now）
    import time

    c._last_fetch["example.com"] = time.monotonic()
    with patch("web_crawler.crawler.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await c._throttle("https://example.com/")
    mock_sleep.assert_awaited_once()


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


async def test_fetch_blocked_by_robots() -> None:
    """robots 禁止抓取时返回 403 + 错误信息。"""
    c = Crawler(respect_robots=True)
    with patch.object(c, "_can_fetch", new=AsyncMock(return_value=False)):
        client = AsyncMock()
        result = await c.fetch(client, "https://example.com/secret")
    assert result.status_code == 403
    assert result.error == "Blocked by robots.txt"
    assert result.url == "https://example.com/secret"
    client.get.assert_not_called()


async def test_fetch_success() -> None:
    """成功抓取时应返回 200 和提取的链接。"""
    c = Crawler(respect_robots=False, delay=0)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '<a href="/page2">p2</a><a href="https://example.com/page3">p3</a>'

    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_resp)

    result = await c.fetch(client, "https://example.com/")

    assert result.status_code == 200
    assert result.error is None
    assert "https://example.com/page2" in result.links
    assert "https://example.com/page3" in result.links


async def test_fetch_timeout() -> None:
    """超时应返回 status 0 + timeout 错误。"""
    c = Crawler(respect_robots=False, delay=0)
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.TimeoutException("slow"))

    result = await c.fetch(client, "https://example.com/slow")
    assert result.status_code == 0
    assert result.error == "timeout"


async def test_fetch_http_status_error() -> None:
    """HTTPStatusError 应返回对应状态码和错误字符串。"""
    c = Crawler(respect_robots=False, delay=0)
    mock_response = MagicMock()
    mock_response.status_code = 500
    err = httpx.HTTPStatusError("server error", request=MagicMock(), response=mock_response)

    client = AsyncMock()
    client.get = AsyncMock(side_effect=err)

    result = await c.fetch(client, "https://example.com/err")
    assert result.status_code == 500
    assert "server error" in (result.error or "")


async def test_fetch_generic_exception() -> None:
    """其他异常应返回 status 0 + 异常字符串。"""
    c = Crawler(respect_robots=False, delay=0)
    client = AsyncMock()
    client.get = AsyncMock(side_effect=ConnectionError("boom"))

    result = await c.fetch(client, "https://example.com/boom")
    assert result.status_code == 0
    assert "boom" in (result.error or "")


# ---------------------------------------------------------------------------
# crawl
# ---------------------------------------------------------------------------


async def test_crawl_single_page_no_links() -> None:
    """抓取无链接的页面应只返回一个结果。"""
    c = Crawler(respect_robots=False, delay=0)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><body><h1>No links here</h1></body></html>"

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("web_crawler.crawler.httpx.AsyncClient", return_value=mock_client):
        results = await c.crawl("https://example.com/", max_pages=5)

    assert len(results) == 1
    assert results[0].url == "https://example.com/"
    assert results[0].status_code == 200
    assert results[0].links == []


async def test_crawl_follows_same_domain_links() -> None:
    """BFS 应跟随同域链接并去重。"""
    c = Crawler(respect_robots=False, delay=0, max_concurrency=1)

    page1_html = '<a href="/page2">p2</a><a href="/page3">p3</a>'
    page2_html = '<a href="/page3">p3 again</a>'  # 重复链接应去重
    page3_html = "<html><body>end</body></html>"

    pages = {
        "https://example.com/": page1_html,
        "https://example.com/page2": page2_html,
        "https://example.com/page3": page3_html,
    }

    async def fake_get(url: str, **kwargs: Any) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.text = pages.get(url, "<html></html>")
        return resp

    mock_client = AsyncMock()
    mock_client.get = fake_get
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("web_crawler.crawler.httpx.AsyncClient", return_value=mock_client):
        results = await c.crawl("https://example.com/", max_pages=10)

    urls = {r.url for r in results}
    assert "https://example.com/" in urls
    assert "https://example.com/page2" in urls
    assert "https://example.com/page3" in urls
    # page3 只被抓取一次（去重生效）
    assert len([r for r in results if r.url == "https://example.com/page3"]) == 1


async def test_crawl_respects_max_pages() -> None:
    """max_pages 限制应阻止抓取所有页面。"""
    c = Crawler(respect_robots=False, delay=0)

    # 生成 5 个页面互相链接
    html = "".join(f'<a href="/p{i}">p{i}</a>' for i in range(5))

    async def fake_get(url: str, **kwargs: Any) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.text = html
        return resp

    mock_client = AsyncMock()
    mock_client.get = fake_get
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("web_crawler.crawler.httpx.AsyncClient", return_value=mock_client):
        results = await c.crawl("https://example.com/", max_pages=2)

    assert len(results) <= 2


async def test_crawl_does_not_follow_external_links() -> None:
    """外域链接不应被跟随。"""
    c = Crawler(respect_robots=False, delay=0)
    html = '<a href="/internal">internal</a><a href="https://other.com/external">external</a>'

    async def fake_get(url: str, **kwargs: Any) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        # internal 页面无链接
        resp.text = "<html></html>" if url != "https://example.com/" else html
        return resp

    mock_client = AsyncMock()
    mock_client.get = fake_get
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("web_crawler.crawler.httpx.AsyncClient", return_value=mock_client):
        results = await c.crawl("https://example.com/", max_pages=10)

    urls = {r.url for r in results}
    assert "https://example.com/" in urls
    assert "https://example.com/internal" in urls
    assert all("other.com" not in u for u in urls)


async def test_crawl_error_results_still_appended() -> None:
    """抓取失败的页面也应加入结果列表但不跟随其链接。"""
    c = Crawler(respect_robots=False, delay=0)

    call_count = 0

    async def fake_get(url: str, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # 第一页有链接，但第二页超时
            resp = MagicMock()
            resp.status_code = 200
            resp.text = '<a href="/page2">p2</a>'
            return resp
        raise httpx.TimeoutException("slow")

    mock_client = AsyncMock()
    mock_client.get = fake_get
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("web_crawler.crawler.httpx.AsyncClient", return_value=mock_client):
        results = await c.crawl("https://example.com/", max_pages=10)

    assert len(results) == 2
    error_result = next(r for r in results if r.url == "https://example.com/page2")
    assert error_result.error == "timeout"
    assert error_result.status_code == 0


def test_crawl_result_dataclass() -> None:
    """CrawlResult dataclass 默认值应正确。"""
    r = CrawlResult(url="https://example.com/", status_code=200)
    assert r.links == []
    assert r.error is None

    r2 = CrawlResult(url="https://example.com/", status_code=403, error="blocked")
    assert r2.links == []
    assert r2.error == "blocked"


async def test_can_fetch_robots_single_flight() -> None:
    """同一域名并发首访只拉取一次 robots.txt（single-flight）。"""
    c = Crawler(respect_robots=True)
    rp = MagicMock()
    rp.can_fetch = MagicMock(return_value=True)
    mock_get = AsyncMock(return_value=rp)

    with patch.object(c, "_get_robot_parser", new=mock_get):
        results = await asyncio.gather(
            c._can_fetch("https://example.com/a"),
            c._can_fetch("https://example.com/b"),
        )

    assert results == [True, True]
    mock_get.assert_awaited_once()
    # 拉取完成后缓存的是解析结果而非任务
    assert c._robot_parsers["example.com"] is rp


# ===========================================================================
# 扩展：_normalize_url 缺失 host / _extract_links 基准 URL 非法分支
# ===========================================================================


def test_normalize_url_missing_host_returns_none() -> None:
    """URL 缺 host（如 https:///path）时返回 None。"""
    assert Crawler._normalize_url("https:///path") is None
    assert Crawler._normalize_url("http://?q=1") is None


def test_normalize_url_invalid_port_returns_none() -> None:
    """URL 端口非法（如 http://x.example:99999/）时返回 None。"""
    assert Crawler._normalize_url("http://x.example:99999/") is None


def test_extract_links_invalid_base_url_returns_empty() -> None:
    """base_url 无法规范化（如 ftp://）时 _extract_links 返回空列表。"""
    assert Crawler._extract_links("ftp://example.com/", "<html><a href='/x'>x</a></html>") == []

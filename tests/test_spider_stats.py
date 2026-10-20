"""Spider stats 导出（JSON/JSONL）与三源延迟集成测试。

stats 导出用内存 FakeFetcher + ``tmp_path`` 验证落盘语义（内容一致/
目录创建/覆盖与追加/失败容错/异常结束也导出）；三源延迟
（per_domain_delay / autothrottle / robots Crawl-delay）先以组合矩阵
直接断言 ``_throttle_delay_for`` 的返回值，再用受控假时钟验证
run()/stream() 两条执行路径上的真实节流时序（假 sleep 记录时长并推进
假时钟，不真实等待）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.robotparser
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from web_crawler import Request, Response, Spider, SpiderError
from web_crawler.spider import AutoThrottle

HOST = "shop.example.com"
BASE = f"https://{HOST}"
PAGES = {
    f"{BASE}/": b'<html><body><div class="item">A</div><a href="/page2">next</a></body></html>',
    f"{BASE}/page2": b'<html><body><div class="item">B</div></body></html>',
}


class FakeFetcher:
    """返回按 URL 预置的 HTML，无网络；记录调用顺序。"""

    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> Response:
        self.calls.append(url)
        content = self.pages.get(url, b"<html><body></body></html>")
        status = 200 if url in self.pages else 404
        return Response(url, status, content)

    async def async_get(self, url: str, **kwargs: Any) -> Response:
        return self.get(url, **kwargs)


class ExportSpider(Spider):
    """爬两个页面、每页产出一条 item 的标准导出测试 spider。"""

    name = "stats_export"
    start_urls = [f"{BASE}/"]

    def parse(self, response: Response) -> Any:
        yield {"url": response.url}
        next_link = response.css_first("a")
        if next_link:
            yield Request(response.urljoin(next_link.attr("href")))


def _export_spider(
    stats_output: str | Path | None, pages: dict[str, bytes] | None = None
) -> ExportSpider:
    """构造带 stats_output 的导出测试 spider（pages 缺省用 PAGES）。"""
    fetcher = FakeFetcher(pages if pages is not None else PAGES)
    return ExportSpider(fetcher=fetcher, stats_output=stats_output)


# ===========================================================================
# JSON 导出（单次快照，覆盖写）
# ===========================================================================


def test_json_export_matches_as_dict(tmp_path: Path) -> None:
    """JSON 文件内容与 as_dict() 完全一致。"""
    path = tmp_path / "stats.json"
    spider = _export_spider(path)
    spider.run()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == spider.stats.as_dict()
    assert data["pages_crawled"] == 2
    assert data["items_scraped"] == 2


def test_json_export_creates_missing_parent_dirs(tmp_path: Path) -> None:
    """父目录不存在时自动逐级创建。"""
    path = tmp_path / "a" / "b" / "c" / "stats.json"
    spider = _export_spider(path)
    spider.run()
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["pages_crawled"] == 2


def test_json_export_overwrites_previous_run(tmp_path: Path) -> None:
    """JSON 是单次快照语义：第二次运行覆盖第一次的内容。"""
    path = tmp_path / "stats.json"
    _export_spider(path).run()
    second = _export_spider(path, pages={f"{BASE}/": b"<html></html>"})
    second.run()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == second.stats.as_dict()
    assert data["pages_crawled"] == 1
    # 仍是单个 JSON 对象（覆盖而非追加）
    assert path.read_text(encoding="utf-8").lstrip().startswith("{")


def test_json_export_accepts_str_path(tmp_path: Path) -> None:
    """stats_output 接受 str 路径（不止 Path）。"""
    spider = _export_spider(str(tmp_path / "stats.json"))
    spider.run()
    assert (tmp_path / "stats.json").exists()


def test_json_export_after_async_run(tmp_path: Path) -> None:
    """async_run 路径同样在结束时导出。"""
    path = tmp_path / "stats.json"
    spider = _export_spider(path)
    asyncio.run(spider.async_run())
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pages_crawled"] == 2


def test_json_export_includes_autothrottle_snapshot(tmp_path: Path) -> None:
    """开启 autothrottle 时快照键透传到导出文件。"""
    path = tmp_path / "stats.json"
    spider = _export_spider(path)
    spider.autothrottle = AutoThrottle(alpha=1.0)
    spider.run()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "autothrottle" in data
    assert data["autothrottle"][HOST]["samples"] == 2


# ===========================================================================
# JSONL 导出（每次运行追加一行，可聚合）
# ===========================================================================


def test_jsonl_appends_one_line_per_run(tmp_path: Path) -> None:
    """同一 spider 跑两次：两行记录（每次运行追加一行）。"""
    path = tmp_path / "stats.jsonl"
    spider = _export_spider(path)
    spider.run()
    spider.run()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_jsonl_each_line_is_valid_json(tmp_path: Path) -> None:
    """每一行都能独立 json.loads（JSON Lines 格式硬性要求）。"""
    path = tmp_path / "stats.jsonl"
    spider = _export_spider(path)
    spider.run()
    spider.run()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert all(isinstance(r, dict) for r in records)
    # stats 跨运行累计；第二行是两次运行的总账（第二次因请求去重
    # 只新增 1 页：/page2 的指纹已在第一次运行登记）
    assert records[0]["pages_crawled"] == 2
    assert records[1]["pages_crawled"] == 3


def test_jsonl_line_contains_all_as_dict_fields(tmp_path: Path) -> None:
    """每行含 as_dict() 全部字段（聚合分析不缺列）。"""
    path = tmp_path / "stats.jsonl"
    spider = _export_spider(path)
    spider.run()
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert set(record) >= set(spider.stats.as_dict())
    assert record["items_scraped"] == 2
    assert record["elapsed_seconds"] >= 0


def test_jsonl_line_has_parseable_iso_asctime(tmp_path: Path) -> None:
    """每行含 ISO 格式结束时间戳 asctime（UTC aware，可跨运行比较）。"""
    path = tmp_path / "stats.jsonl"
    spider = _export_spider(path)
    spider.run()
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert "asctime" in record
    parsed = datetime.fromisoformat(record["asctime"])
    assert parsed.tzinfo is not None


def test_jsonl_creates_missing_parent_dirs(tmp_path: Path) -> None:
    """JSONL 父目录不存在时同样自动创建。"""
    path = tmp_path / "nested" / "dir" / "stats.jsonl"
    spider = _export_spider(path)
    spider.run()
    assert path.exists()


def test_jsonl_uppercase_suffix_treated_as_jsonl(tmp_path: Path) -> None:
    """.JSONL 大写后缀同样按 JSON Lines 追加处理。"""
    path = tmp_path / "stats.JSONL"
    spider = _export_spider(path)
    spider.run()
    spider.run()
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_jsonl_two_runs_are_independent_records(tmp_path: Path) -> None:
    """两个 spider 实例先后写入：两行各自完整、可区分（聚合语义）。"""
    path = tmp_path / "stats.jsonl"
    first = _export_spider(path)
    first.run()
    second = _export_spider(path, pages={f"{BASE}/": b"<html></html>"})
    second.run()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    first_rec, second_rec = (json.loads(line) for line in lines)
    assert first_rec["pages_crawled"] == 2
    assert second_rec["pages_crawled"] == 1


# ===========================================================================
# stats_output=None：零行为变化
# ===========================================================================


def test_stats_output_none_writes_nothing(tmp_path: Path) -> None:
    """默认 None 不导出：运行后目录下无任何新文件。"""
    spider = _export_spider(None)
    spider.run()
    assert list(tmp_path.iterdir()) == []


def test_stats_output_none_run_returns_items_normally() -> None:
    """None 不影响运行本身：items 正常返回、stats 正常计数。"""
    spider = _export_spider(None)
    items = spider.run()
    assert len(items) == 2
    assert spider.stats.pages_crawled == 2


# ===========================================================================
# 写入失败容错：不抛出、记 warning（logging 而非 warnings）
# ===========================================================================


def test_json_export_to_directory_logs_warning_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """路径指向已存在目录（写入必败）：run 正常完成并记 warning。"""
    spider = _export_spider(tmp_path)  # tmp_path 本身是目录
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.spider"):
        items = spider.run()
    assert len(items) == 2
    assert any("stats export" in rec.getMessage() for rec in caplog.records)


def test_jsonl_export_to_directory_logs_warning_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """JSONL 追加到目录同样容错：不抛出、记 warning。"""
    spider = _export_spider(tmp_path / "x.jsonl")
    # 先把 x.jsonl 创建为目录，令 open("a") 必败
    (tmp_path / "x.jsonl").mkdir()
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.spider"):
        items = spider.run()
    assert len(items) == 2
    assert any("stats export" in rec.getMessage() for rec in caplog.records)


def test_jsonl_export_degrades_unserializable_field_to_str(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L4：jsonl 分支对不可序列化字段降级为 str 表示而非中断导出。

    monkeypatch 注入含不可序列化对象（object()）的 stats dict——修复前
    ``json.dumps`` 抛 ``TypeError`` 穿透 ``except OSError``，从 finally 链
    抛出并中断 run() 返回已抓取的 items；修复后 jsonl 分支与 JSON 快照
    分支同样以 ``default=str`` 防御（对齐 ``_dump_state``），导出正常写
    出、run 正常返回。
    """
    path = tmp_path / "stats.jsonl"
    spider = _export_spider(path)
    original_as_dict = spider.stats.as_dict

    def patched_as_dict() -> dict[str, Any]:
        data = dict(original_as_dict())
        # 模拟未来扩展出不可序列化字段（default=str 降级为 repr 字符串）
        data["future_field"] = object()
        return data

    monkeypatch.setattr(spider.stats, "as_dict", patched_as_dict)
    items = spider.run()
    assert len(items) == 2  # 收尾导出异常绝不连累已抓取的数据
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["future_field"]  # object() 降级为 "<object object at ...>"
    assert record["items_scraped"] == 2


def test_export_when_parent_mkdir_fails_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """父路径被同名文件占据（mkdir 必败）：同样容错不抛出。"""
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("occupied", encoding="utf-8")
    spider = _export_spider(blocker / "stats.json")
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.spider"):
        items = spider.run()
    assert len(items) == 2
    assert any("stats export" in rec.getMessage() for rec in caplog.records)


# ===========================================================================
# 异常 / 提前中断结束时也导出
# ===========================================================================


def test_json_exported_when_callback_raises(tmp_path: Path) -> None:
    """回调抛 SpiderError：异常向上传播，但 stats 文件仍写出。"""

    class Bad(Spider):
        name = "bad_callback"
        start_urls = [f"{BASE}/"]

        def parse(self, response: Response) -> Any:
            raise RuntimeError("boom")

    spider = Bad(fetcher=FakeFetcher(PAGES), stats_output=tmp_path / "stats.json")
    with pytest.raises(SpiderError, match="boom"):
        spider.run()
    data = json.loads((tmp_path / "stats.json").read_text(encoding="utf-8"))
    assert data["pages_crawled"] == 1


def test_jsonl_exported_when_stream_consumer_breaks(tmp_path: Path) -> None:
    """stream() 消费方提前 break（aclose）：finally 仍完成导出。"""
    path = tmp_path / "stats.jsonl"
    spider = _export_spider(path)

    async def consume() -> None:
        agen = spider.stream()
        async for _item in agen:
            break
        await agen.aclose()

    asyncio.run(consume())
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert "asctime" in record


# ===========================================================================
# 三源延迟组合矩阵：静态下限 / autothrottle / robots Crawl-delay 取大者
# ===========================================================================

# 无 Crawl-delay 声明的 robots（返回 None，不参与节流）
NO_DELAY_ROBOTS = ["User-agent: *", "Disallow: /private"]


def _throttle_spider(
    *,
    per_domain: dict[str, float] | None = None,
    autothrottle: AutoThrottle | None = None,
    robots_lines: list[str] | None = None,
    respect_robots: bool = True,
) -> Spider:
    """构造用于 _throttle_delay_for 断言的 spider（robots 解析器直接注入缓存）。"""

    class S(Spider):
        name = "throttle_matrix"

        def parse(self, response: Response) -> Any:
            return None

    spider = S(fetcher=FakeFetcher({}), autothrottle=autothrottle)
    spider.respect_robots = respect_robots
    if per_domain:
        spider.per_domain_delay = per_domain
    if robots_lines is not None:
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(robots_lines)
        spider._robots_policy._cache[f"https://{HOST}"] = parser
    return spider


def _at_with_delay(delay: float) -> AutoThrottle:
    """构造 delay 恰为指定值的 AutoThrottle（alpha=1 首样本即真值）。"""
    at = AutoThrottle(alpha=1.0, target_concurrency=1.0)
    at.note_response(HOST, delay, True)
    return at


def test_throttle_all_sources_off_returns_zero() -> None:
    """三者全部未开启：零延迟零键（与旧行为逐字一致）。"""
    spider = _throttle_spider(robots_lines=NO_DELAY_ROBOTS, respect_robots=False)
    assert spider._throttle_delay_for(f"{BASE}/x") == (0.0, "")


def test_throttle_static_only() -> None:
    """仅静态下限生效：键为最长命中域（同域子域共享账本的旧行为）。"""
    spider = _throttle_spider(per_domain={"example.com": 0.5}, robots_lines=NO_DELAY_ROBOTS)
    assert spider._throttle_delay_for(f"{BASE}/x") == (0.5, "example.com")


def test_throttle_autothrottle_only() -> None:
    """仅 autothrottle 生效：键为请求 host。"""
    spider = _throttle_spider(autothrottle=_at_with_delay(2.0), robots_lines=NO_DELAY_ROBOTS)
    assert spider._throttle_delay_for(f"{BASE}/x") == (2.0, HOST)


def test_throttle_robots_only() -> None:
    """仅 robots Crawl-delay 生效：键为请求 host。"""
    spider = _throttle_spider(robots_lines=["User-agent: *", "Crawl-delay: 2"])
    assert spider._throttle_delay_for(f"{BASE}/x") == (2.0, HOST)


def test_throttle_static_beats_robots() -> None:
    """静态下限更保守（3.0 > 1.0）：胜出且保留静态域键。"""
    spider = _throttle_spider(
        per_domain={"example.com": 3.0}, robots_lines=["User-agent: *", "Crawl-delay: 1"]
    )
    assert spider._throttle_delay_for(f"{BASE}/x") == (3.0, "example.com")


def test_throttle_robots_beats_static() -> None:
    """robots Crawl-delay 更保守（3.0 > 0.5）：胜出且键取 host。"""
    spider = _throttle_spider(
        per_domain={"example.com": 0.5}, robots_lines=["User-agent: *", "Crawl-delay: 3"]
    )
    assert spider._throttle_delay_for(f"{BASE}/x") == (3.0, HOST)


def test_throttle_static_beats_autothrottle() -> None:
    """静态下限比自适应值更保守：沿用静态域键（既有语义回归）。"""
    spider = _throttle_spider(
        per_domain={"example.com": 0.5},
        autothrottle=_at_with_delay(0.2),
        robots_lines=NO_DELAY_ROBOTS,
    )
    assert spider._throttle_delay_for(f"{BASE}/x") == (0.5, "example.com")


def test_throttle_autothrottle_beats_static() -> None:
    """自适应值比静态下限更保守：键取 host（既有语义回归）。"""
    spider = _throttle_spider(
        per_domain={"example.com": 0.5},
        autothrottle=_at_with_delay(2.0),
        robots_lines=NO_DELAY_ROBOTS,
    )
    assert spider._throttle_delay_for(f"{BASE}/x") == (2.0, HOST)


def test_throttle_autothrottle_beats_robots() -> None:
    """autothrottle 更保守（2.0 > 1.0）：胜出。"""
    spider = _throttle_spider(
        autothrottle=_at_with_delay(2.0),
        robots_lines=["User-agent: *", "Crawl-delay: 1"],
    )
    assert spider._throttle_delay_for(f"{BASE}/x") == (2.0, HOST)


def test_throttle_robots_beats_autothrottle() -> None:
    """robots Crawl-delay 更保守（5.0 > 2.0）：站点声明优先于自适应。"""
    spider = _throttle_spider(
        autothrottle=_at_with_delay(2.0),
        robots_lines=["User-agent: *", "Crawl-delay: 5"],
    )
    assert spider._throttle_delay_for(f"{BASE}/x") == (5.0, HOST)


def test_throttle_max_of_all_three_sources() -> None:
    """三源并存取最大：静态 0.5 < autothrottle 1.0 < robots 3.0 → 3.0。"""
    spider = _throttle_spider(
        per_domain={"example.com": 0.5},
        autothrottle=_at_with_delay(1.0),
        robots_lines=["User-agent: *", "Crawl-delay: 3"],
    )
    assert spider._throttle_delay_for(f"{BASE}/x") == (3.0, HOST)


def test_throttle_max_of_three_static_wins() -> None:
    """三源并存、静态下限最大（4.0 > 2.0 > 1.0）：静态胜出保留域键。"""
    spider = _throttle_spider(
        per_domain={"example.com": 4.0},
        autothrottle=_at_with_delay(2.0),
        robots_lines=["User-agent: *", "Crawl-delay: 1"],
    )
    assert spider._throttle_delay_for(f"{BASE}/x") == (4.0, "example.com")


def test_throttle_max_of_three_autothrottle_wins() -> None:
    """三源并存、autothrottle 最大（3.0 > 1.0 > 0.5）：键取 host。"""
    spider = _throttle_spider(
        per_domain={"example.com": 0.5},
        autothrottle=_at_with_delay(3.0),
        robots_lines=["User-agent: *", "Crawl-delay: 1"],
    )
    assert spider._throttle_delay_for(f"{BASE}/x") == (3.0, HOST)


def test_throttle_robots_ignored_without_respect_robots() -> None:
    """respect_robots=False：即使缓存里声明了 Crawl-delay 也不参与。"""
    spider = _throttle_spider(
        per_domain={"example.com": 0.5},
        robots_lines=["User-agent: *", "Crawl-delay: 9"],
        respect_robots=False,
    )
    assert spider._throttle_delay_for(f"{BASE}/x") == (0.5, "example.com")


def test_throttle_robots_undeclared_falls_back_to_legacy() -> None:
    """robots 未声明 Crawl-delay：回落到既有两源语义，无额外影响。"""
    spider = _throttle_spider(
        per_domain={"example.com": 0.5},
        autothrottle=_at_with_delay(2.0),
        robots_lines=NO_DELAY_ROBOTS,
    )
    assert spider._throttle_delay_for(f"{BASE}/x") == (2.0, HOST)


def test_throttle_off_matches_domain_delay_lookup() -> None:
    """autothrottle=None 且 robots 未声明时与 _domain_delay_for 逐字等价。"""
    spider = _throttle_spider(per_domain={"example.com": 0.5}, robots_lines=NO_DELAY_ROBOTS)
    url = f"{BASE}/x"
    assert spider._throttle_delay_for(url) == spider._domain_delay_for(url)
    spider.per_domain_delay = {}
    assert spider._throttle_delay_for(url) == spider._domain_delay_for(url)


# ===========================================================================
# run()/stream() 执行路径上的 robots Crawl-delay 真实节流（假时钟）
# ===========================================================================


class FakeClock:
    """受控假时钟：monotonic() 读数仅由 advance() 推进。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, dt: float) -> None:
        self._now += dt


class FakeTimedFetcher:
    """下载耗时通过推进假时钟模拟（不真实等待）的 fetcher。"""

    def __init__(self, clock: FakeClock, durations: list[float]) -> None:
        self._clock = clock
        self._durations = durations
        self.calls: list[str] = []

    def _respond(self, url: str) -> Response:
        i = len(self.calls)
        self.calls.append(url)
        dt = self._durations[min(i, len(self._durations) - 1)]
        self._clock.advance(dt)
        return Response(url, 200, b"<html><body>ok</body></html>")

    def get(self, url: str, **kwargs: Any) -> Response:
        return self._respond(url)

    async def async_get(self, url: str, **kwargs: Any) -> Response:
        return self._respond(url)


def _install_fake_time(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> list[float]:
    """把 spider 模块命名空间内的 time 换成假时钟；返回同步 sleep 记录表。"""
    sync_sleeps: list[float] = []

    def fake_sync_sleep(seconds: float) -> None:
        sync_sleeps.append(seconds)
        clock.advance(seconds)

    fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=fake_sync_sleep)
    monkeypatch.setattr("web_crawler.spider.spider.time", fake_time)
    return sync_sleeps


def _install_fake_async_sleep(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> list[float]:
    """把 asyncio.sleep 换成记录 + 推进假时钟的假实现；返回记录表。"""
    async_sleeps: list[float] = []

    async def fake_async_sleep(delay: float) -> None:
        async_sleeps.append(delay)
        clock.advance(delay)

    monkeypatch.setattr("asyncio.sleep", fake_async_sleep)
    return async_sleeps


def _robots_spider(fetcher: Any, robots_lines: list[str] | None, urls: list[str]) -> Spider:
    """构造 respect_robots=True 的 spider；robots_lines 注入缓存（None 不注）。"""

    class S(Spider):
        name = "robots_throttle"
        max_concurrency = 1

        def parse(self, response: Response) -> Any:
            return None

    spider = S(fetcher=fetcher)
    spider.respect_robots = True
    spider.start_urls = urls
    if robots_lines is not None:
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(robots_lines)
        spider._robots_policy._cache[f"https://{HOST}"] = parser
    return spider


def test_run_sleeps_by_robots_crawl_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """sync run()：robots Crawl-delay: 1 生效，补足间隔序列确定。

    推演（假时钟从 0 起）：请求1 无上一时间戳 → sleep(1.0)；下载 0.1 →
    t=1.1；请求2 wait=1.0+1.0-1.1=0.9 → sleep(0.9)。
    """
    clock = FakeClock()
    sync_sleeps = _install_fake_time(monkeypatch, clock)
    fetcher = FakeTimedFetcher(clock, durations=[0.1, 0.1])
    spider = _robots_spider(
        fetcher, ["User-agent: *", "Crawl-delay: 1"], [f"{BASE}/p1", f"{BASE}/p2"]
    )
    spider.run()
    assert sync_sleeps == pytest.approx([1.0, 0.9])
    assert spider.stats.pages_crawled == 2


def test_stream_sleeps_by_robots_crawl_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """async stream()：同一条 _throttle_delay_for 路径，时序与 run() 一致。"""
    clock = FakeClock()
    async_sleeps = _install_fake_async_sleep(monkeypatch, clock)
    _install_fake_time(monkeypatch, clock)
    fetcher = FakeTimedFetcher(clock, durations=[0.1, 0.1])
    spider = _robots_spider(
        fetcher, ["User-agent: *", "Crawl-delay: 1"], [f"{BASE}/p1", f"{BASE}/p2"]
    )

    async def collect() -> list[Any]:
        return [item async for item in spider.stream()]

    asyncio.run(collect())
    assert async_sleeps == pytest.approx([1.0, 0.9])
    assert spider.stats.pages_crawled == 2


def test_run_robots_fetch_failure_does_not_throttle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robots.txt 拉取失败：保守视为无 Crawl-delay，不节流。

    不注入缓存解析器，强制走真实 fetch_text 路径（被替换为抛错）。
    """
    clock = FakeClock()
    sync_sleeps = _install_fake_time(monkeypatch, clock)

    def failing_fetch(url: str) -> str:
        raise OSError("network unreachable")

    monkeypatch.setattr("web_crawler.spider.spider.fetch_robots_text", failing_fetch)
    fetcher = FakeTimedFetcher(clock, durations=[0.1, 0.1])
    spider = _robots_spider(fetcher, None, [f"{BASE}/p1", f"{BASE}/p2"])
    spider.run()
    assert sync_sleeps == []
    assert spider.stats.pages_crawled == 2


# ===========================================================================
# 现有 stats 计数 / as_dict 格式零回归
# ===========================================================================


def test_stats_as_dict_keys_unchanged() -> None:
    """as_dict() 输出格式逐字不变：仍是既有 6 键，无导出相关新键。"""
    spider = _export_spider(None)
    spider.run()
    assert set(spider.stats.as_dict()) == {
        "pages_crawled",
        "items_scraped",
        "requests_scheduled",
        "requests_failed",
        "requests_ignored",
        "elapsed_seconds",
    }


def test_stats_counters_after_run() -> None:
    """既有计数语义零回归：两页两 item、零失败。"""
    spider = _export_spider(None)
    spider.run()
    assert spider.stats.pages_crawled == 2
    assert spider.stats.items_scraped == 2
    assert spider.stats.requests_scheduled == 2
    assert spider.stats.requests_failed == 0
    assert spider.stats.requests_ignored == 0

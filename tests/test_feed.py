"""Feed 导出管道（JSON / JSONL / CSV）、feed_output 便捷参数与 Spider
状态码重试（retry_http_status / 退避 jitter）的测试。

feed 用内存 FakeFetcher + ``tmp_path`` 验证落盘语义（内容正确 / 覆盖与
追加 / 定列容错 / 失败不连累 item / 空运行行为）；重试测试用可编程
mock fetcher（第一次 503、第二次 200）验证状态码命中走与下载异常完全
相同的重试路径，run()/stream() 双路径各覆盖。
"""

from __future__ import annotations

import asyncio
import csv as csv_mod
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from web_crawler import FeedExportPipeline, Request, Response, Spider
from web_crawler.exceptions import ConfigError
from web_crawler.spider import ItemPipeline

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


class FeedSpider(Spider):
    """爬两个页面、每页产出一条 dict item 的标准 feed 测试 spider。"""

    name = "feed_export"
    start_urls = [f"{BASE}/"]

    def parse(self, response: Response) -> Any:
        yield {"url": response.url, "status": response.status}
        next_link = response.css_first("a")
        if next_link:
            yield Request(response.urljoin(next_link.attr("href")))


EXPECTED_ITEMS = [
    {"url": f"{BASE}/", "status": 200},
    {"url": f"{BASE}/page2", "status": 200},
]


class RaggedSpider(Spider):
    """产出列不齐 item 的 spider：第二条缺 status、第三条多 extra 键。"""

    name = "ragged"
    start_urls = [f"{BASE}/"]

    def parse(self, response: Response) -> Any:
        yield {"url": "a", "status": 200}
        yield {"url": "b"}
        yield {"url": "c", "status": 201, "extra": "x"}


class EmptySpider(Spider):
    """不产出任何 item 的空运行 spider。"""

    name = "empty"
    start_urls = [f"{BASE}/"]

    def parse(self, response: Response) -> Any:
        return None


class RerunSpider(Spider):
    """产出单条 item 的 spider：payload 可在两次 run 之间切换。

    用于验证同一 spider 实例二次 run 的 feed 多周期语义（H-2）。
    """

    name = "rerun"
    start_urls = [f"{BASE}/"]
    payload = "first"

    def parse(self, response: Response) -> Any:
        yield {"payload": self.payload}


class ToggleSpider(Spider):
    """emit 开关控制是否产出 item（验证空周期不覆盖既有快照）。"""

    name = "toggle"
    start_urls = [f"{BASE}/"]
    emit = True

    def parse(self, response: Response) -> Any:
        if self.emit:
            yield {"url": response.url}


def _failing_open(self: Path, *args: Any, **kwargs: Any) -> Any:
    """pathlib.Path.open 的替身：任何打开请求都抛 OSError（磁盘满模拟）。"""

    raise OSError("disk full")


# ===========================================================================
# JSON 快照（覆盖写，close 时一次性落盘）
# ===========================================================================


def test_json_feed_snapshot_content_and_indent(tmp_path: Path) -> None:
    """JSON 快照内容与 run 返回的 items 完全一致，且缩进为 2 空格。"""
    path = tmp_path / "feed.json"
    spider = FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=path)
    items = spider.run()
    text = path.read_text(encoding="utf-8")
    assert json.loads(text) == items == EXPECTED_ITEMS
    # indent=2：数组首个元素行以两个空格开头
    assert text.splitlines()[1] == "  {"


def test_json_feed_overwrites_previous_run(tmp_path: Path) -> None:
    """JSON 快照是单次运行语义：第二次运行覆盖第一次的内容。"""
    path = tmp_path / "feed.json"
    FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=path).run()
    FeedSpider(fetcher=FakeFetcher({f"{BASE}/": b"<html></html>"}), feed_output=path).run()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == [{"url": f"{BASE}/", "status": 200}]


def test_json_feed_empty_run_writes_empty_list(tmp_path: Path) -> None:
    """空运行也产出空 JSON 数组快照（close 钩子在空 run 后仍落盘）。"""
    path = tmp_path / "feed.json"
    EmptySpider(fetcher=FakeFetcher(PAGES), feed_output=path).run()
    assert json.loads(path.read_text(encoding="utf-8")) == []


def test_json_feed_second_run_on_same_instance_snapshots_latest(tmp_path: Path) -> None:
    """H-2 回归：同一实例二次 run，快照只含第二次数据（不静默丢）。"""
    path = tmp_path / "rerun.json"
    spider = RerunSpider(fetcher=FakeFetcher(PAGES), feed_output=path)
    spider.run()
    assert json.loads(path.read_text(encoding="utf-8")) == [{"payload": "first"}]
    spider.payload = "second"
    spider.run()
    # 新周期 item 解除 close 守卫：快照反映最新 run（覆盖语义）
    assert json.loads(path.read_text(encoding="utf-8")) == [{"payload": "second"}]


def test_json_feed_second_empty_run_keeps_previous_snapshot(tmp_path: Path) -> None:
    """H-2 边界：同一实例二次空 run 不覆盖既有快照（数据优先于空覆盖）。"""
    path = tmp_path / "toggle.json"
    spider = ToggleSpider(fetcher=FakeFetcher(PAGES), feed_output=path)
    spider.run()
    first = path.read_text(encoding="utf-8")
    assert json.loads(first) == [{"url": f"{BASE}/"}]
    spider.emit = False
    spider.run()
    # 空周期无新 item：守卫未解除，既有快照原样保留
    assert path.read_text(encoding="utf-8") == first


def test_jsonl_feed_second_run_on_same_instance_appends(tmp_path: Path) -> None:
    """H-2 回归：同一实例二次 run，JSONL 追加两行（周期数据都不丢）。"""
    path = tmp_path / "rerun.jsonl"
    spider = RerunSpider(fetcher=FakeFetcher(PAGES), feed_output=path)
    spider.run()
    spider.payload = "second"
    spider.run()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["payload"] for line in lines] == ["first", "second"]


def test_json_feed_creates_missing_parent_dirs(tmp_path: Path) -> None:
    """JSON 父目录不存在时自动逐级创建。"""
    path = tmp_path / "a" / "b" / "c" / "feed.json"
    FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=path).run()
    assert path.exists()


def test_json_feed_after_stream(tmp_path: Path) -> None:
    """stream() 路径同样经 close 钩子在结束时落盘快照。"""
    path = tmp_path / "feed.json"
    spider = FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=path)

    async def collect() -> list[Any]:
        return [item async for item in spider.stream()]

    items = asyncio.run(collect())
    assert json.loads(path.read_text(encoding="utf-8")) == items == EXPECTED_ITEMS


# ===========================================================================
# JSONL 追加（逐条写，断点续爬聚合）
# ===========================================================================


def test_jsonl_feed_line_per_item_and_unicode(tmp_path: Path) -> None:
    """JSONL 每条 item 一行且 ensure_ascii=False（中文原样落盘）。"""

    class CnSpider(Spider):
        name = "cn"
        start_urls = [f"{BASE}/"]

        def parse(self, response: Response) -> Any:
            yield {"文本": "学而时习之", "作者": "孔子"}

    path = tmp_path / "quotes.jsonl"
    CnSpider(fetcher=FakeFetcher(PAGES), feed_output=path).run()
    text = path.read_text(encoding="utf-8")
    # 非 ASCII 不转义为 \uXXXX
    assert "孔子" in text
    assert json.loads(text.strip()) == {"文本": "学而时习之", "作者": "孔子"}


def test_jsonl_feed_resume_appends_without_rewrite(tmp_path: Path) -> None:
    """断点续爬：第二个 spider 实例追加而非重写，既有行原样保留。"""
    path = tmp_path / "feed.jsonl"
    FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=path).run()
    FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=path).run()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4
    records = [json.loads(line) for line in lines]
    assert records[:2] == EXPECTED_ITEMS
    assert records[2:] == EXPECTED_ITEMS


def test_jsonl_feed_creates_missing_parent_dirs(tmp_path: Path) -> None:
    """JSONL 父目录不存在时同样自动逐级创建。"""
    path = tmp_path / "nested" / "dir" / "feed.jsonl"
    FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=path).run()
    assert path.exists()


def test_jsonl_feed_stream_concurrent_writes_complete_lines(tmp_path: Path) -> None:
    """stream() 并发 worker 交错调用 process_item：每条一开一关，行完整。"""

    class Multi(Spider):
        name = "multi"
        start_urls = [f"{BASE}/{i}" for i in range(8)]
        max_concurrency = 8

        def parse(self, response: Response) -> Any:
            yield {"url": response.url, "status": response.status}

    pages = {f"{BASE}/{i}": b"<html></html>" for i in range(8)}
    spider = Multi(fetcher=FakeFetcher(pages), feed_output=tmp_path / "feed.jsonl")

    async def collect() -> list[Any]:
        return [item async for item in spider.stream()]

    items = asyncio.run(collect())
    lines = (tmp_path / "feed.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(items) == 8
    assert {json.loads(line)["url"] for line in lines} == set(pages)


def test_jsonl_feed_write_failure_passes_item_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """JSONL 写盘抛 OSError 不连累 item：run 正常返回全部数据并记 warning。"""
    monkeypatch.setattr("pathlib.Path.open", _failing_open)
    spider = FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=tmp_path / "feed.jsonl")
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.feed"):
        items = spider.run()
    assert items == EXPECTED_ITEMS
    assert any("feed export" in rec.getMessage() for rec in caplog.records)


def test_json_feed_write_failure_passes_item_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """JSON 快照落盘失败（close 阶段 OSError）同样不连累已抓数据。"""
    monkeypatch.setattr("pathlib.Path.open", _failing_open)
    spider = FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=tmp_path / "feed.json")
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.feed"):
        items = spider.run()
    assert items == EXPECTED_ITEMS
    assert any("feed export" in rec.getMessage() for rec in caplog.records)


def test_json_feed_to_directory_logs_warning_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """输出路径指向已存在的同名目录（写入必败）：run 正常完成并记 warning。"""
    target = tmp_path / "as_dir.json"
    target.mkdir()
    spider = FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=target)
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.feed"):
        items = spider.run()
    assert items == EXPECTED_ITEMS
    assert any("feed export" in rec.getMessage() for rec in caplog.records)


# ===========================================================================
# CSV（首条定列，缺失列空串 / 多余列丢弃）
# ===========================================================================


def test_csv_feed_header_from_first_item(tmp_path: Path) -> None:
    """CSV 表头来自首条 item 的键，后续行按首条列对齐。"""
    path = tmp_path / "feed.csv"
    spider = FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=path)
    items = spider.run()
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv_mod.reader(fh))
    assert rows[0] == ["url", "status"]
    assert rows[1] == [f"{BASE}/", "200"]
    assert rows[2] == [f"{BASE}/page2", "200"]
    assert len(rows) == 1 + len(items)


def test_csv_feed_missing_column_empty_extra_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """CSV 缺失列写空串、多余列记 warning 丢弃（首条 item 定列）。"""
    path = tmp_path / "feed.csv"
    spider = RaggedSpider(fetcher=FakeFetcher(PAGES), feed_output=path)
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.feed"):
        items = spider.run()
    assert len(items) == 3
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv_mod.reader(fh))
    assert rows == [
        ["url", "status"],
        ["a", "200"],
        ["b", ""],
        ["c", "201"],
    ]
    assert any("extra" in rec.getMessage() for rec in caplog.records)


def test_csv_feed_none_value_written_as_empty_string(tmp_path: Path) -> None:
    """CSV 列值显式为 None 时写空串（而非字符串 "None"）。"""

    class NoneSpider(Spider):
        name = "none_val"
        start_urls = [f"{BASE}/"]

        def parse(self, response: Response) -> Any:
            yield {"url": "a", "status": None}

    path = tmp_path / "feed.csv"
    NoneSpider(fetcher=FakeFetcher(PAGES), feed_output=path).run()
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv_mod.reader(fh))
    assert rows == [["url", "status"], ["a", ""]]


def test_csv_feed_resume_appends_without_duplicate_header(tmp_path: Path) -> None:
    """CSV 续爬：文件已非空时追加行而不重复写表头。"""
    path = tmp_path / "feed.csv"
    RaggedSpider(fetcher=FakeFetcher(PAGES), feed_output=path).run()
    RaggedSpider(fetcher=FakeFetcher(PAGES), feed_output=path).run()
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv_mod.reader(fh))
    # 两次运行各 3 行 item，表头只出现一次
    assert len(rows) == 7
    assert sum(1 for r in rows if r == ["url", "status"]) == 1


def test_csv_feed_empty_run_creates_no_file(tmp_path: Path) -> None:
    """空运行不创建 CSV 文件（表头推迟到首条 item 到达才写）。"""
    path = tmp_path / "feed.csv"
    EmptySpider(fetcher=FakeFetcher(PAGES), feed_output=path).run()
    assert not path.exists()


def test_csv_feed_non_dict_item_skipped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """CSV + 非 dict item：记 warning 跳过写盘，item 照常通过、其余行完整。"""

    class MixedSpider(Spider):
        name = "mixed"
        start_urls = [f"{BASE}/"]

        def parse(self, response: Response) -> Any:
            yield {"url": "a", "status": 200}
            yield "plain-string-item"
            yield {"url": "c", "status": 201}

    path = tmp_path / "mixed.csv"
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.feed"):
        items = MixedSpider(fetcher=FakeFetcher(PAGES), feed_output=path).run()
    # 被跳过的 item 照常通过管道进入 run 结果（导出失败/跳过绝不连累数据）
    assert items == [{"url": "a", "status": 200}, "plain-string-item", {"url": "c", "status": 201}]
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv_mod.reader(fh))
    # 非 dict item 不写行、不扰动列定义：其余两行完整落盘
    assert rows == [["url", "status"], ["a", "200"], ["c", "201"]]
    assert any("non-mapping" in rec.getMessage() for rec in caplog.records)


def test_csv_feed_write_failure_passes_item_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """CSV 写盘抛 OSError 不连累 item：run 正常返回全部数据并记 warning。"""
    monkeypatch.setattr("pathlib.Path.open", _failing_open)
    spider = FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=tmp_path / "feed.csv")
    with caplog.at_level(logging.WARNING, logger="web_crawler.spider.feed"):
        items = spider.run()
    assert items == EXPECTED_ITEMS
    assert any("feed export" in rec.getMessage() for rec in caplog.records)


# ===========================================================================
# 构造校验与挂载方式
# ===========================================================================


def test_unknown_suffix_raises_config_error(tmp_path: Path) -> None:
    """未知后缀在构造时抛 ConfigError（兼容既有 except ValueError 断言）。"""
    with pytest.raises(ConfigError, match="feed"):
        FeedExportPipeline(tmp_path / "feed.xml")
    # ConfigError ⊂ ValueError：调用方的 ValueError 断言原样通过
    with pytest.raises(ValueError):
        FeedExportPipeline("out.txt")


def test_feed_output_str_path_works(tmp_path: Path) -> None:
    """feed_output 接受 str 路径（不止 Path）。"""
    spider = FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=str(tmp_path / "f.jsonl"))
    spider.run()
    assert (tmp_path / "f.jsonl").exists()


def test_feed_output_attaches_pipeline_at_tail(tmp_path: Path) -> None:
    """feed_output 便捷参数把 FeedExportPipeline 实例追加到管道尾部。"""
    spider = FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=tmp_path / "f.jsonl")
    assert len(spider._item_pipelines) == 1
    assert isinstance(spider._item_pipelines[0], FeedExportPipeline)
    items = spider.run()
    lines = (tmp_path / "f.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line) for line in lines] == items == EXPECTED_ITEMS


class _UpperUrlPipeline(ItemPipeline):
    """把 item 的 url 字段大写的变换管道（验证与 feed 的执行顺序）。"""

    def process_item(self, item: Any, spider: Spider) -> Any:
        if isinstance(item, dict) and "url" in item:
            return {**item, "url": str(item["url"]).upper()}
        return item


def test_feed_output_coexists_with_declared_pipelines(tmp_path: Path) -> None:
    """feed_output 与自定义 item_pipelines 共存：声明管道先变换、feed 最后落盘。"""

    class S(FeedSpider):
        item_pipelines = [_UpperUrlPipeline]

    spider = S(fetcher=FakeFetcher(PAGES), feed_output=tmp_path / "feed.json")
    assert isinstance(spider._item_pipelines[0], _UpperUrlPipeline)
    assert isinstance(spider._item_pipelines[-1], FeedExportPipeline)
    spider.run()
    data = json.loads((tmp_path / "feed.json").read_text(encoding="utf-8"))
    # 落盘的是变换后的 item：feed 位于管道链末位
    assert data == [
        {"url": f"{BASE}/".upper(), "status": 200},
        {"url": f"{BASE}/page2".upper(), "status": 200},
    ]


def test_feed_pipeline_as_declared_instance(tmp_path: Path) -> None:
    """FeedExportPipeline 实例可直接声明在 item_pipelines 上使用。"""
    path = tmp_path / "direct.jsonl"

    class S(FeedSpider):
        item_pipelines = [FeedExportPipeline(path)]

    S(fetcher=FakeFetcher(PAGES)).run()
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_feed_output_none_keeps_pipelines_untouched() -> None:
    """默认 feed_output=None 不挂载任何管道（零行为变化）。"""
    spider = FeedSpider(fetcher=FakeFetcher(PAGES))
    assert spider._item_pipelines == []
    items = spider.run()
    assert items == EXPECTED_ITEMS


def test_feed_close_is_idempotent(tmp_path: Path) -> None:
    """close 幂等：run 收尾后的重复调用不重写快照（内容保持首次落盘结果）。"""
    path = tmp_path / "feed.json"
    spider = FeedSpider(fetcher=FakeFetcher(PAGES), feed_output=path)
    pipeline = spider._item_pipelines[0]
    spider.run()
    first = path.read_text(encoding="utf-8")
    # run() 的 finally 已调用过一次 close：后续手动重复调用不再写盘
    pipeline.close(spider)
    pipeline.close(spider)
    assert path.read_text(encoding="utf-8") == first


# ===========================================================================
# 重试补强：退避 jitter
# ===========================================================================


def test_retry_backoff_jitter_in_range() -> None:
    """退避基数叠加 [0, 0.5) 全抖动：多次采样落在区间内且值有变化。"""
    spider = Spider()
    first = [spider._retry_backoff(1) for _ in range(20)]
    assert all(0.5 <= v < 1.0 for v in first)
    # 抖动真实存在：20 次采样出现至少两个不同值（全同的概率趋近于零）
    assert len(set(first)) > 1
    second = [spider._retry_backoff(2) for _ in range(20)]
    assert all(1.0 <= v < 1.5 for v in second)
    # 基数封顶 8s：抖动叠加后仍不超过 8.5s
    capped = [spider._retry_backoff(10) for _ in range(20)]
    assert all(8.0 <= v < 8.5 for v in capped)


# ===========================================================================
# 重试补强：retry_http_status（run() 路径）
# ===========================================================================


def test_retry_http_status_triggers_retry_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 命中 retry_http_status：走异常同款重试路径，第二次 200 正常产出。"""
    sleeps: list[float] = []
    monkeypatch.setattr("web_crawler.spider.spider.time.sleep", lambda s: sleeps.append(s))

    class FlakyStatusFetcher:
        """第一次返回 503、第二次返回 200 的可编程 mock fetcher。"""

        def __init__(self) -> None:
            self.attempts = 0

        def get(self, url: str, **kwargs: Any) -> Response:
            self.attempts += 1
            if self.attempts == 1:
                return Response(url, 503, b"<p>service unavailable</p>")
            return Response(url, 200, b"<p>ok</p>")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]
        max_retries = 2

        def parse(self, response: Response) -> Any:
            yield {"status": response.status}

    fetcher = FlakyStatusFetcher()
    spider = S(fetcher=fetcher)
    items = spider.run()
    assert items == [{"status": 200}]
    assert fetcher.attempts == 2
    assert spider.stats.pages_crawled == 1
    assert spider.stats.requests_failed == 0
    # 一次状态重试的退避：0.5s 基数 + [0, 0.5) jitter
    assert len(sleeps) == 1
    assert 0.5 <= sleeps[0] < 1.0


def test_retry_http_status_disabled_by_empty_set() -> None:
    """retry_http_status=set() 关闭状态重试：503 直接交回调、不计失败。"""

    class Always503:
        def get(self, url: str, **kwargs: Any) -> Response:
            return Response(url, 503, b"")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]
        retry_http_status = set()
        max_retries = 2

        def parse(self, response: Response) -> Any:
            yield {"status": response.status}

    fetcher = Always503()
    spider = S(fetcher=fetcher)
    items = spider.run()
    # 不重试：回调拿到的是 503 响应本身
    assert items == [{"status": 503}]
    assert spider.stats.pages_crawled == 1
    assert spider.stats.requests_failed == 0


def test_retry_http_status_exhaustion_releases_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """达到 max_retries 后放行 Response 交回调（计入 requests_failed）。"""
    sleeps: list[float] = []
    monkeypatch.setattr("web_crawler.spider.spider.time.sleep", lambda s: sleeps.append(s))

    class Always503:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, url: str, **kwargs: Any) -> Response:
            self.calls += 1
            return Response(url, 503, b"")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]
        max_retries = 1

        def parse(self, response: Response) -> Any:
            yield {"status": response.status}

    fetcher = Always503()
    spider = S(fetcher=fetcher)
    items = spider.run()
    # 初次 + 1 次重试后放行：回调收到 503 响应
    assert items == [{"status": 503}]
    assert fetcher.calls == 2
    assert sleeps  # 发生过一次重试退避
    assert spider.stats.requests_failed == 1
    assert spider.stats.pages_crawled == 1


def test_retry_http_status_zero_max_retries_releases_immediately() -> None:
    """默认 max_retries=0：命中状态不重试、直接放行交回调并计失败。"""

    class Always429:
        def get(self, url: str, **kwargs: Any) -> Response:
            return Response(url, 429, b"")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]

        def parse(self, response: Response) -> Any:
            yield {"status": response.status}

    spider = S(fetcher=Always429())
    items = spider.run()
    assert items == [{"status": 429}]
    assert spider.stats.pages_crawled == 1
    assert spider.stats.requests_failed == 1


def test_retry_http_status_non_member_status_not_retried() -> None:
    """未命中集合的状态码（如 404）不触发状态重试，直接交回调。"""

    class NotFound:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, url: str, **kwargs: Any) -> Response:
            self.calls += 1
            return Response(url, 404, b"<p>gone</p>")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]
        max_retries = 3

        def parse(self, response: Response) -> Any:
            yield {"status": response.status}

    fetcher = NotFound()
    spider = S(fetcher=fetcher)
    items = spider.run()
    assert items == [{"status": 404}]
    assert fetcher.calls == 1
    assert spider.stats.requests_failed == 0


# ===========================================================================
# 重试补强：retry_http_status（stream() 路径）
# ===========================================================================


def test_retry_http_status_stream_triggers_retry_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream() 路径同样接入状态码重试：503 重试后 200 正常产出。"""

    async def mock_sleep(delay: float) -> None:
        sleeps.append(delay)

    sleeps: list[float] = []
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    class FlakyAsyncStatusFetcher:
        def __init__(self) -> None:
            self.attempts = 0

        async def async_get(self, url: str, **kwargs: Any) -> Response:
            self.attempts += 1
            if self.attempts == 1:
                return Response(url, 503, b"")
            return Response(url, 200, b"<p>ok</p>")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]
        max_retries = 1

        def parse(self, response: Response) -> Any:
            yield {"status": response.status}

    fetcher = FlakyAsyncStatusFetcher()
    spider = S(fetcher=fetcher)

    async def collect() -> list[Any]:
        return [item async for item in spider.stream()]

    items = asyncio.run(collect())
    assert items == [{"status": 200}]
    assert fetcher.attempts == 2
    assert spider.stats.requests_failed == 0
    assert len(sleeps) == 1
    assert 0.5 <= sleeps[0] < 1.0


def test_retry_http_status_stream_disabled_by_empty_set() -> None:
    """stream() 路径 retry_http_status=set()：503 直接交回调不重试。"""

    class Always503Async:
        def __init__(self) -> None:
            self.attempts = 0

        async def async_get(self, url: str, **kwargs: Any) -> Response:
            self.attempts += 1
            return Response(url, 503, b"")

    class S(Spider):
        start_urls = ["https://shop.example.com/"]
        retry_http_status = set()
        max_retries = 2

        def parse(self, response: Response) -> Any:
            yield {"status": response.status}

    fetcher = Always503Async()
    spider = S(fetcher=fetcher)

    async def collect() -> list[Any]:
        return [item async for item in spider.stream()]

    items = asyncio.run(collect())
    assert items == [{"status": 503}]
    assert fetcher.attempts == 1
    assert spider.stats.requests_failed == 0

"""CacheMiddleware 的单元与集成测试（磁盘缓存 + TTL + 离线回放）。"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
import zlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from web_crawler import Response, Spider
from web_crawler.spider import CacheMiddleware, IgnoreRequest, Request
from web_crawler.spider.cache import _REPLACE_RETRIES

# 一页固定内容：多数测试共用，避免每个用例重复定义
PAGE = (200, b"<html><body><p>hello cache</p></body></html>", {"Content-Type": "text/html"})


class FakeFetcher:
    """按 URL 返回固定 ``(status, body, headers)`` 并记录调用的假 fetcher（无网络）。"""

    def __init__(self, pages: dict[str, tuple[int, bytes, dict[str, str]]] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> Response:
        self.calls.append(url)
        status, body, headers = self.pages.get(url, (404, b"<p>not found</p>", {}))
        return Response(url, status, body, headers=dict(headers))

    def post(self, url: str, **kwargs: Any) -> Response:
        self.calls.append(url)
        return Response(url, 200, b"<p>posted</p>")

    async def async_get(self, url: str, **kwargs: Any) -> Response:
        return self.get(url, **kwargs)

    async def async_post(self, url: str, **kwargs: Any) -> Response:
        return self.post(url, **kwargs)


class UrllibFetcher:
    """标准库 urllib 实现的最小 fetcher：对 local_server 做真实 HTTP GET。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> Response:
        self.calls.append(url)
        req = urllib.request.Request(url, headers=kwargs.get("headers") or {})
        with urllib.request.urlopen(req) as r:
            body = r.read()
            headers = dict(r.headers.items())
            return Response(url, r.status, body, headers=headers)

    async def async_get(self, url: str, **kwargs: Any) -> Response:
        return self.get(url, **kwargs)


def _spider(fetcher: Any, mw: CacheMiddleware, urls: list[str]) -> Spider:
    """构造把响应快照（url/status/body/text/headers）作为 item 产出的 spider。"""

    class S(Spider):
        start_urls = urls
        middlewares = [mw]  # 实例注入（类属性机制同样支持类或实例）

        def parse(self, response: Response) -> Iterator[dict[str, Any]]:
            yield {
                "url": response.url,
                "status": response.status,
                "body": response.content,
                "text": response.text,
                "headers": dict(response.headers),
            }

    return S(fetcher=fetcher)


# ===========================================================================
# 命中 / 未命中回写
# ===========================================================================


def test_cache_hit_second_request_skips_network(tmp_path: Path) -> None:
    """同 URL 第二次请求不发网络，缓存内容与原响应等价（status/headers/body/text）。"""
    url = "https://shop.example.com/"
    fetcher = FakeFetcher({url: PAGE})
    cache_dir = tmp_path / "cache"

    items1 = _spider(fetcher, CacheMiddleware(cache_dir), [url]).run()
    items2 = _spider(fetcher, CacheMiddleware(cache_dir), [url]).run()

    # 只发了第一次请求：第二次命中缓存短路
    assert fetcher.calls == [url]
    assert len(items1) == len(items2) == 1
    a, b = items1[0], items2[0]
    assert b["status"] == a["status"] == 200
    assert b["body"] == a["body"] == PAGE[1]
    assert b["text"] == a["text"]
    assert b["headers"] == a["headers"] == {"Content-Type": "text/html"}


def test_cache_miss_passes_through_and_stores(tmp_path: Path) -> None:
    """未命中时透传下载，并在 process_response 回写缓存到磁盘。"""
    url = "https://shop.example.com/data"
    fetcher = FakeFetcher({url: (200, b"payload", {})})
    mw = CacheMiddleware(tmp_path / "cache")
    items = _spider(fetcher, mw, [url]).run()

    assert fetcher.calls == [url]  # 透传到了 fetcher
    assert items[0]["body"] == b"payload"
    stats = mw.stats()
    assert stats["misses"] == 1
    assert stats["stores"] == 1
    assert stats["hits"] == 0
    assert stats["entries"] == 1
    # 磁盘上出现 meta + body 两个文件；body 是 zlib 压缩的
    meta_files = list((tmp_path / "cache").glob("*.meta.json"))
    body_files = list((tmp_path / "cache").glob("*.body"))
    assert len(meta_files) == 1
    assert len(body_files) == 1
    assert zlib.decompress(body_files[0].read_bytes()) == b"payload"
    meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
    assert meta["url"] == url
    assert meta["status"] == 200
    assert meta["method"] == "GET"
    assert "stored_at" in meta


def test_cache_hit_does_not_rewrite_store(tmp_path: Path) -> None:
    """命中短路回来的缓存响应流经 process_response 时不重复写盘。"""
    url = "https://shop.example.com/"
    fetcher = FakeFetcher({url: PAGE})
    _spider(fetcher, CacheMiddleware(tmp_path / "cache"), [url]).run()

    mw = CacheMiddleware(tmp_path / "cache")
    _spider(fetcher, mw, [url]).run()

    assert mw.stats() == {"hits": 1, "misses": 0, "stores": 0, "entries": 1}


# ===========================================================================
# TTL
# ===========================================================================


def _rewind_timestamp(cache_dir: Path, seconds: float) -> None:
    """把缓存条目的 stored_at 回拨指定秒数（替代真实 sleep 的确定性手段）。"""
    meta_path = next(iter(cache_dir.glob("*.meta.json")))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["stored_at"] -= seconds
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def test_cache_ttl_expiry_refetches(tmp_path: Path) -> None:
    """TTL 过期后条目视为未命中：删除旧条目并重新抓取回写。"""
    url = "https://shop.example.com/"
    fetcher = FakeFetcher({url: PAGE})
    cache_dir = tmp_path / "cache"
    _spider(fetcher, CacheMiddleware(cache_dir, ttl=3600.0), [url]).run()

    _rewind_timestamp(cache_dir, 3601.0)  # 回拨到 TTL 之外

    mw = CacheMiddleware(cache_dir, ttl=3600.0)
    _spider(fetcher, mw, [url]).run()
    assert len(fetcher.calls) == 2  # 过期后重新抓取
    assert mw.stats()["misses"] == 1
    # 旧条目被删除、新响应回写：仍恰好一个条目
    assert mw.stats()["entries"] == 1


def test_cache_ttl_none_is_permanent(tmp_path: Path) -> None:
    """ttl=None 表示永久缓存：无论条目多旧都命中。"""
    url = "https://shop.example.com/"
    fetcher = FakeFetcher({url: PAGE})
    cache_dir = tmp_path / "cache"
    _spider(fetcher, CacheMiddleware(cache_dir), [url]).run()

    _rewind_timestamp(cache_dir, 10_000_000.0)  # 回拨到"很久以前"

    mw = CacheMiddleware(cache_dir)
    _spider(fetcher, mw, [url]).run()
    assert len(fetcher.calls) == 1  # 仍然命中
    assert mw.stats()["hits"] == 1


# ===========================================================================
# 非 GET / 状态码过滤
# ===========================================================================


def test_non_get_requests_not_cached(tmp_path: Path) -> None:
    """POST 请求直接透传：不读缓存、不写缓存。"""
    cache_dir = tmp_path / "cache"

    class PostSpider(Spider):
        start_urls = ["https://shop.example.com/api"]

        def start_requests(self) -> Iterator[Request]:
            for url in self.start_urls:
                yield Request(url, method="POST", body=b"k=v")

        def parse(self, response: Response) -> Iterator[dict[str, Any]]:
            yield {"status": response.status}

    fetcher = FakeFetcher()
    for _ in range(2):
        spider = PostSpider(fetcher=fetcher)
        spider._middlewares = [CacheMiddleware(cache_dir)]
        items = spider.run()
        assert items == [{"status": 200}]

    assert len(fetcher.calls) == 2  # 两次都真实发到了 fetcher
    assert not cache_dir.exists()  # 从未写盘（目录都未创建）
    assert CacheMiddleware(cache_dir).stats() == {
        "hits": 0,
        "misses": 0,
        "stores": 0,
        "entries": 0,
    }


def test_4xx_not_cached_by_default_but_optin(tmp_path: Path) -> None:
    """404 默认不缓存（每次都发网络）；``cache_4xx=True`` 时缓存并可命中。"""
    gone = "https://shop.example.com/gone"
    boom = "https://shop.example.com/boom"
    fetcher = FakeFetcher({gone: (404, b"not found", {}), boom: (500, b"error", {})})

    # 默认：404 不缓存，两次都发网络
    cache_dir = tmp_path / "c1"
    _spider(fetcher, CacheMiddleware(cache_dir), [gone]).run()
    _spider(fetcher, CacheMiddleware(cache_dir), [gone]).run()
    assert len(fetcher.calls) == 2

    # opt-in：404 被缓存，第二次命中
    cache_dir = tmp_path / "c2"
    _spider(fetcher, CacheMiddleware(cache_dir, cache_4xx=True), [gone]).run()
    mw = CacheMiddleware(cache_dir, cache_4xx=True)
    items = _spider(fetcher, mw, [gone]).run()
    assert len(fetcher.calls) == 3
    assert mw.stats()["hits"] == 1
    assert items[0]["status"] == 404

    # 5xx 即使 cache_4xx=True 也绝不缓存
    _spider(fetcher, CacheMiddleware(cache_dir, cache_4xx=True), [boom]).run()
    _spider(fetcher, CacheMiddleware(cache_dir, cache_4xx=True), [boom]).run()
    assert fetcher.calls.count(boom) == 2


# ===========================================================================
# 离线回放
# ===========================================================================


def test_offline_miss_raises_ignore_request(tmp_path: Path) -> None:
    """离线模式未命中时 process_request 抛 IgnoreRequest（带清晰原因）。"""
    mw = CacheMiddleware(tmp_path / "cache", offline=True)
    req = Request("https://never-cached.example.com/")
    with pytest.raises(IgnoreRequest, match="offline replay"):
        mw.process_request(req, Spider())


def test_offline_takes_precedence_over_ignore_lookup(tmp_path: Path) -> None:
    """offline 与 ignore_lookup 同时设置时 offline 优先：仍查缓存、未命中即忽略。"""
    mw = CacheMiddleware(tmp_path / "cache", offline=True, ignore_lookup=True)
    req = Request("https://never-cached.example.com/")
    with pytest.raises(IgnoreRequest, match="offline replay"):
        mw.process_request(req, Spider())


def test_offline_mode_replays_without_network(tmp_path: Path) -> None:
    """离线回放集成：命中直接返回缓存内容；未命中被 IgnoreRequest 丢弃。"""
    cache_dir = tmp_path / "cache"
    hit_url = "https://shop.example.com/cached"
    miss_url = "https://shop.example.com/absent"

    # 第一轮：正常模式写入缓存
    fetcher = FakeFetcher({hit_url: (200, b"<p>cached page</p>", {})})
    _spider(fetcher, CacheMiddleware(cache_dir), [hit_url]).run()
    assert len(fetcher.calls) == 1

    # 第二轮：离线回放（换一个内容不同的 fetcher，证明零网络、零内容污染）
    offline_fetcher = FakeFetcher({hit_url: (200, b"<p>SHOULD NOT APPEAR</p>", {})})
    mw = CacheMiddleware(cache_dir, offline=True)
    spider = _spider(offline_fetcher, mw, [hit_url, miss_url])
    items = spider.run()

    assert offline_fetcher.calls == []  # 未发任何网络请求
    assert len(items) == 1  # 只有命中那条产出 item
    assert items[0]["body"] == b"<p>cached page</p>"  # 是旧缓存而非新内容
    assert spider.stats.requests_ignored == 1  # 未命中被 IgnoreRequest 丢弃
    assert mw.stats()["hits"] == 1
    assert mw.stats()["misses"] == 1


# ===========================================================================
# 缓存键规范化
# ===========================================================================


def test_cache_key_normalization_rules() -> None:
    """缓存键规范化：query 顺序 / fragment / 大小写 / 默认端口不区分。"""
    key = CacheMiddleware.cache_key
    # query 顺序不同 -> 同键
    assert key("GET", "https://x.example/p?b=2&a=1") == key("GET", "https://x.example/p?a=1&b=2")
    # fragment 差异 -> 同键
    assert key("GET", "https://x.example/p#section") == key("GET", "https://x.example/p")
    # scheme / host 大小写 -> 同键
    assert key("GET", "HTTPS://X.EXAMPLE/p") == key("GET", "https://x.example/p")
    # 显式默认端口 -> 同键；非默认端口 -> 不同键
    assert key("GET", "https://x.example:443/p") == key("GET", "https://x.example/p")
    assert key("GET", "http://x.example:80/p") == key("GET", "http://x.example/p")
    assert key("GET", "https://x.example:8443/p") != key("GET", "https://x.example/p")
    # method / 路径 / 参数值仍区分
    assert key("GET", "https://x.example/p") != key("POST", "https://x.example/p")
    assert key("GET", "https://x.example/p") != key("GET", "https://x.example/q")
    assert key("GET", "https://x.example/p?a=1") != key("GET", "https://x.example/p?a=2")
    # method 大小写不敏感
    assert key("get", "https://x.example/p") == key("GET", "https://x.example/p")


def test_query_order_and_fragment_share_cache_entry(tmp_path: Path) -> None:
    """行为级验证：query 顺序不同 / fragment 差异的请求命中同一条缓存。"""
    u1 = "https://shop.example.com/search?q=py&page=2"
    u2 = "https://shop.example.com/search?page=2&q=py"
    u3 = "https://shop.example.com/search?q=py&page=2#results"
    fetcher = FakeFetcher({u1: PAGE})

    cache_dir = tmp_path / "cache"
    _spider(fetcher, CacheMiddleware(cache_dir), [u1]).run()
    mw = CacheMiddleware(cache_dir)
    _spider(fetcher, mw, [u2, u3]).run()

    assert fetcher.calls == [u1]  # 后两个请求全部命中
    assert mw.stats()["hits"] == 2
    assert mw.stats()["entries"] == 1  # 三个 URL 共享一个条目


# ===========================================================================
# purge / stats / ignore_lookup
# ===========================================================================


def test_purge_clears_disk_and_resets_stats(tmp_path: Path) -> None:
    """purge 清空缓存文件（保留无关文件）并重置统计；之后可重新缓存。"""
    url = "https://shop.example.com/"
    fetcher = FakeFetcher({url: PAGE})
    cache_dir = tmp_path / "cache"
    mw = CacheMiddleware(cache_dir)
    _spider(fetcher, mw, [url]).run()
    assert mw.stats()["entries"] == 1

    # 放一个无关文件，验证 purge 不误删
    keeper = cache_dir / "notes.txt"
    keeper.write_text("keep me", encoding="utf-8")

    mw.purge()
    assert mw.stats() == {"hits": 0, "misses": 0, "stores": 0, "entries": 0}
    assert list(cache_dir.glob("*.meta.json")) == []
    assert list(cache_dir.glob("*.body")) == []
    assert keeper.exists()

    # purge 后再请求：未命中重新抓取
    _spider(fetcher, CacheMiddleware(cache_dir), [url]).run()
    assert len(fetcher.calls) == 2


def test_stats_counts_hits_misses_stores(tmp_path: Path) -> None:
    """stats 准确统计 hits / misses / stores / entries 的完整生命周期。"""
    url_a = "https://shop.example.com/a"
    url_b = "https://shop.example.com/b"
    fetcher = FakeFetcher({url_a: PAGE, url_b: PAGE})
    mw = CacheMiddleware(tmp_path / "cache")

    _spider(fetcher, mw, [url_a, url_b]).run()  # 2 次 miss + 2 次 store
    _spider(fetcher, mw, [url_a]).run()  # 1 次 hit

    stats = mw.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 2
    assert stats["stores"] == 2
    assert stats["entries"] == 2


def test_ignore_lookup_forces_network_but_still_stores(tmp_path: Path) -> None:
    """ignore_lookup=True：每次都发网络（跳过查找），但结果仍回写缓存。"""
    url = "https://shop.example.com/"
    fetcher = FakeFetcher({url: (200, b"fresh", {})})
    cache_dir = tmp_path / "cache"

    mw = CacheMiddleware(cache_dir, ignore_lookup=True)
    _spider(fetcher, mw, [url]).run()
    _spider(fetcher, mw, [url]).run()

    assert len(fetcher.calls) == 2  # 每次都发网络
    # 跳过查找不计 hit/miss；两次下载都回写（同 key 覆盖为一个条目）
    assert mw.stats() == {"hits": 0, "misses": 0, "stores": 2, "entries": 1}

    # 新的普通中间件能命中 -> 证明缓存确实被回写了
    mw2 = CacheMiddleware(cache_dir)
    _spider(fetcher, mw2, [url]).run()
    assert len(fetcher.calls) == 2
    assert mw2.stats()["hits"] == 1


# ===========================================================================
# 健壮性：损坏条目 / 线程安全
# ===========================================================================


def test_corrupt_cache_entry_treated_as_miss(tmp_path: Path) -> None:
    """meta 损坏或 body 缺失时按未命中处理，坏条目被清理后重新回写。"""
    url = "https://shop.example.com/"
    cache_dir = tmp_path / "cache"
    fetcher = FakeFetcher({url: (200, b"good", {})})
    _spider(fetcher, CacheMiddleware(cache_dir), [url]).run()

    # 情形 1：meta JSON 损坏
    meta_path = next(iter(cache_dir.glob("*.meta.json")))
    meta_path.write_text("{broken json", encoding="utf-8")
    mw = CacheMiddleware(cache_dir)
    _spider(fetcher, mw, [url]).run()
    assert len(fetcher.calls) == 2
    assert mw.stats()["misses"] == 1
    assert mw.stats()["stores"] == 1  # 重新回写

    # 情形 2：meta 存在但 body 文件缺失
    for body_file in cache_dir.glob("*.body"):
        body_file.unlink()
    mw2 = CacheMiddleware(cache_dir)
    _spider(fetcher, mw2, [url]).run()
    assert len(fetcher.calls) == 3
    # 孤儿 meta 被清理，完整条目被重写
    assert mw2.stats()["entries"] == 1
    body_files = list(cache_dir.glob("*.body"))
    assert len(body_files) == 1
    assert zlib.decompress(body_files[0].read_bytes()) == b"good"


def test_unsupported_meta_version_treated_as_miss(tmp_path: Path) -> None:
    """元数据版本不匹配（未来格式）时安全地视为未命中。"""
    url = "https://shop.example.com/"
    cache_dir = tmp_path / "cache"
    fetcher = FakeFetcher({url: PAGE})
    _spider(fetcher, CacheMiddleware(cache_dir), [url]).run()

    meta_path = next(iter(cache_dir.glob("*.meta.json")))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["version"] = 99
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    mw = CacheMiddleware(cache_dir)
    _spider(fetcher, mw, [url]).run()
    assert len(fetcher.calls) == 2
    assert mw.stats()["misses"] == 1


def test_concurrent_access_is_thread_safe(tmp_path: Path) -> None:
    """多线程并发读写（含同 key）不崩溃，最终磁盘状态一致可读。"""
    mw = CacheMiddleware(tmp_path / "cache")
    spider = Spider()
    urls = [f"https://shop.example.com/p{i}" for i in range(8)]

    def worker(url: str) -> None:
        req = Request(url)
        mw.process_request(req, spider)  # 查找（miss 或并发写后 hit）
        resp = Response(url, 200, f"body-{url}".encode())
        mw.process_response(resp, req, spider)  # 回写

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, urls * 3))

    # 每个唯一 key 恰好一个完整条目，且内容可正确读回
    stats = mw.stats()
    assert stats["entries"] == 8
    assert stats["stores"] == 24
    for url in urls:
        cached = mw.process_request(Request(url), spider)
        assert cached is not None
        assert cached.content == f"body-{url}".encode()


# ===========================================================================
# 与 Spider 的真实集成（sync / async / local_server）
# ===========================================================================


def test_spider_integration_two_rounds_share_disk_cache(tmp_path: Path) -> None:
    """两个 spider 实例（各自独立中间件）共享磁盘缓存：第二轮零网络。"""
    url = "https://shop.example.com/"
    fetcher = FakeFetcher({url: PAGE})
    cache_dir = tmp_path / "cache"

    items1 = _spider(fetcher, CacheMiddleware(cache_dir), [url]).run()
    items2 = _spider(fetcher, CacheMiddleware(cache_dir), [url]).run()

    assert fetcher.calls == [url]
    assert items1 == items2  # 两轮产出完全一致（内容来自缓存）


def test_cache_hit_in_async_run(tmp_path: Path) -> None:
    """async_run 路径同样短路：worker 内 process_request 命中即不发请求。"""
    url = "https://shop.example.com/"
    fetcher = FakeFetcher({url: (200, b"<p>async</p>", {})})
    cache_dir = tmp_path / "cache"

    asyncio.run(_spider(fetcher, CacheMiddleware(cache_dir), [url]).async_run())
    mw = CacheMiddleware(cache_dir)
    items = asyncio.run(_spider(fetcher, mw, [url]).async_run())

    assert fetcher.calls == [url]
    assert len(items) == 1
    assert items[0]["body"] == b"<p>async</p>"
    assert mw.stats()["hits"] == 1


def test_local_server_roundtrip(tmp_path: Path, local_server: str) -> None:
    """真实 HTTP 集成：首轮抓取落盘，第二轮零网络命中且响应完全等价。"""
    url = f"{local_server}/json"
    fetcher = UrllibFetcher()
    cache_dir = tmp_path / "cache"

    items1 = _spider(fetcher, CacheMiddleware(cache_dir), [url]).run()
    mw = CacheMiddleware(cache_dir)
    items2 = _spider(fetcher, mw, [url]).run()

    assert fetcher.calls == [url]  # 只发了一次真实 HTTP 请求
    assert mw.stats()["hits"] == 1
    a, b = items1[0], items2[0]
    assert b["status"] == a["status"] == 200
    assert b["body"] == a["body"] == b'{"ok": true, "n": 42}'
    assert b["text"] == a["text"]
    # 真实 HTTP 头（Content-Type 等）被完整恢复
    assert b["headers"] == a["headers"]
    assert b["headers"]["Content-Type"] == "application/json"


# ===========================================================================
# 安全加固：zlib bomb 防护（max_body_size）/ 锁外磁盘 IO 的计数正确性
# ===========================================================================


def test_zlib_bomb_entry_removed_and_treated_as_miss(tmp_path: Path) -> None:
    """zlib bomb 防护：解压输出超限的条目被删除并按未命中处理。

    投毒的 ``.body``（几百字节压缩数据可解压出数百 MB）单次命中即内存
    耗尽——``max_body_size`` 限制解压输出累计上限，超限条目就地删除。
    """
    url = "https://shop.example.com/bomb"
    cache_dir = tmp_path / "cache"
    fetcher = FakeFetcher({url: (200, b"good", {})})
    _spider(fetcher, CacheMiddleware(cache_dir), [url]).run()  # 先写正常条目

    mw = CacheMiddleware(cache_dir, max_body_size=64 * 1024)
    key = mw.cache_key("GET", url)
    # 256KB 全 'A' 数据压缩后仅几百字节：压缩文件本身不超限，解压输出
    # 累计超限——zlib bomb 的典型形态（第二层防护）
    (cache_dir / f"{key}.body").write_bytes(zlib.compress(b"A" * (256 * 1024)))

    spider = Spider()
    assert mw.process_request(Request(url), spider) is None  # 按未命中处理
    # 超限条目被就地删除（meta + body），不会反复读到坏数据
    assert not (cache_dir / f"{key}.meta.json").exists()
    assert not (cache_dir / f"{key}.body").exists()
    assert mw.stats()["misses"] == 1


def test_oversized_compressed_body_removed(tmp_path: Path) -> None:
    """压缩文件本身超过 max_body_size 的条目同样被删除并按未命中处理。"""
    url = "https://shop.example.com/huge"
    cache_dir = tmp_path / "cache"
    fetcher = FakeFetcher({url: (200, b"good", {})})
    _spider(fetcher, CacheMiddleware(cache_dir), [url]).run()

    mw = CacheMiddleware(cache_dir, max_body_size=16)
    key = mw.cache_key("GET", url)
    (cache_dir / f"{key}.body").write_bytes(b"0" * 64)  # 压缩文件即超限（第一层防护）

    spider = Spider()
    assert mw.process_request(Request(url), spider) is None
    assert not (cache_dir / f"{key}.meta.json").exists()
    assert not (cache_dir / f"{key}.body").exists()


def test_counters_correct_with_disk_io_outside_lock(tmp_path: Path) -> None:
    """锁外磁盘 IO 后计数器仍线程安全：并发混合 hit/miss/store 精确计数。

    hit / miss / store 三类 URL 互不重叠（miss 永不落盘、store 永不查找），
    各计数不受落盘时序影响，可做精确断言。
    """
    hit_url = "https://shop.example.com/hit"
    miss_url = "https://shop.example.com/miss"
    store_url = "https://shop.example.com/store"
    cache_dir = tmp_path / "cache"
    fetcher = FakeFetcher({hit_url: (200, b"cached", {})})
    _spider(fetcher, CacheMiddleware(cache_dir), [hit_url]).run()  # 预写命中条目

    mw = CacheMiddleware(cache_dir)
    spider = Spider()
    n_threads, n_rounds = 8, 50

    def worker() -> None:
        for _ in range(n_rounds):
            mw.process_request(Request(hit_url), spider)  # 永远命中
            mw.process_request(Request(miss_url), spider)  # 永远未命中（不落盘）
            mw.process_response(  # 只回写（store_url 从不查找）
                Response(store_url, 200, b"s"), Request(store_url), spider
            )

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(lambda _: worker(), range(n_threads)))

    stats = mw.stats()
    assert stats["hits"] == n_threads * n_rounds
    assert stats["misses"] == n_threads * n_rounds
    assert stats["stores"] == n_threads * n_rounds
    assert stats["entries"] == 2  # hit_url 预写 + store_url 回写


def test_disk_io_executes_outside_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """磁盘 IO（_load / _store）在锁外执行：IO 期间锁可被其他线程即时获取。"""
    mw = CacheMiddleware(tmp_path / "cache")
    original_load = mw._load
    original_store = mw._store

    def assert_lock_free() -> None:
        acquired = mw._lock.acquire(blocking=False)
        assert acquired, "磁盘 IO 必须在锁外执行（锁内只保留计数器更新）"
        mw._lock.release()

    def load(key: str) -> Response | None:
        assert_lock_free()
        return original_load(key)

    def store(key: str, request: Request, response: Response) -> None:
        assert_lock_free()
        original_store(key, request, response)

    monkeypatch.setattr(mw, "_load", load)
    monkeypatch.setattr(mw, "_store", store)

    url = "https://shop.example.com/"
    fetcher = FakeFetcher({url: (200, b"x", {})})
    items = _spider(fetcher, mw, [url]).run()  # miss + store 路径
    assert len(items) == 1
    cached = mw.process_request(Request(url), Spider())  # hit 路径
    assert cached is not None
    assert cached.content == b"x"
    assert mw.stats() == {"hits": 1, "misses": 1, "stores": 1, "entries": 1}


# ===========================================================================
# 原子写入失败路径：临时文件清理（内置规则审查 L5）
# ===========================================================================


def test_atomic_write_success_leaves_no_tmp(tmp_path: Path) -> None:
    """对照：成功路径不残留临时文件（tmp 已被原子 rename 为目标）。"""
    target = tmp_path / "ok.body"
    CacheMiddleware._atomic_write(target, b"data")
    assert target.read_bytes() == b"data"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_cleans_tmp_when_replace_exhausts_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L5：os.replace 重试穷尽后异常上抛，且 .tmp 临时文件被 best-effort 清理。

    修复前失败路径残留 ``<name>.<pid>-<tid>.tmp``——purge 的
    ``_CACHE_FILE_RE`` 不匹配 ``.tmp`` 后缀，无人回收，长期运行的反复
    写失败场景（磁盘满 / Windows 文件占用）会累积垃圾文件；修复后异常
    路径清理 tmp 再上抛。
    """
    target = tmp_path / "cache" / "key.meta.json"
    target.parent.mkdir(parents=True)
    calls: list[int] = []

    def failing_replace(src: object, dst: object) -> None:
        calls.append(1)
        raise PermissionError(13, "sharing violation (mock)")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(PermissionError):
        CacheMiddleware._atomic_write(target, b"payload")
    # 重试全部穷尽（而非首次失败即放弃）
    assert len(calls) == _REPLACE_RETRIES
    # tmp 已被清理：缓存目录里没有残留临时文件
    assert list(target.parent.iterdir()) == []

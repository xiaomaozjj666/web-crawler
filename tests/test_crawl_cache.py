"""``--cache`` 条件请求缓存（ETag / Last-Modified / 304 复用）的测试。

所有网络调用都被假 fetcher 接管（patch ``_crawler_core._get_app_fetcher``），
不产生任何公网请求。覆盖：

- 首次抓取写校验器；第二次发送条件头、304 复用产物且清单如实标记；
- 上游 2xx 时更新缓存；缓存损坏 / 旧格式 / 产物缺失一律退回普通抓取；
- 未开启 ``--cache`` 时零行为变化；``--list-only`` 不写缓存；
- 缓存文件与 ``--resume-crawl`` 状态文件互不覆盖。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from web_crawler.app import _crawler_cache as cache_mod
from web_crawler.app import _crawler_context as ctx_mod
from web_crawler.app import _crawler_core as core
from web_crawler.app import crawler as cr
from web_crawler.exceptions import FetchError

PAGE_URL = "https://example.com/"
IMG_URL = "https://example.com/img/logo.png"
PAGE_HTML = b'<html><body><img src="/img/logo.png"></body></html>'
IMG_DATA = b"PNG_DATA_V1"
IMG_DATA_V2 = b"PNG_DATA_V2_LONGER"
ETAG = '"v1"'
ETAG_V2 = '"v2"'
LAST_MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"
# output_path_for_url 的固定落点（无 --organize 时 prefix=assets）
IMG_RELATIVE = "assets/example.com/img/logo.png"


class FakeFetcher:
    """按 URL 回放脚本化响应，并记录每次请求真正发出的请求头。"""

    def __init__(self, script: dict[str, list[dict[str, Any]]]) -> None:
        self.script = {url: list(items) for url, items in script.items()}
        self.get_calls: list[tuple[str, dict[str, str]]] = []
        self.download_calls: list[tuple[str, dict[str, str]]] = []
        # 可在 download 前执行一次（用于模拟「查完缓存后产物被人删掉」）
        self.on_download: Callable[[str], None] | None = None

    def _next(self, url: str) -> dict[str, Any]:
        items = self.script.get(url)
        if not items:
            raise AssertionError(f"unexpected request: {url}")
        return items.pop(0) if len(items) > 1 else items[0]

    def get(self, url: str, headers: dict[str, str] | None = None, **_: Any) -> SimpleNamespace:
        self.get_calls.append((url, dict(headers or {})))
        item = self._next(url)
        return SimpleNamespace(
            status=int(item["status"]),
            content=item.get("content", b""),
            headers=dict(item.get("headers", {})),
        )

    def download(self, url: str, dest: str | Path, **kwargs: Any) -> Path:
        self.download_calls.append((url, dict(kwargs.get("headers") or {})))
        if self.on_download is not None:
            self.on_download(url)
        item = self._next(url)
        status = int(item["status"])
        if status not in (200, 206):
            # 库层 download() 只接受 200/206：其余状态以 FetchError(status=...) 上抛
            exc = FetchError(f"HTTP Error {status}: download failed for {url}")
            exc.status = status  # type: ignore[attr-defined]
            raise exc
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.get("content", b""))
        on_headers = kwargs.get("on_headers")
        if on_headers is not None:
            on_headers(status, dict(item.get("headers", {})))
        return path

    def headers_for(self, calls: list[tuple[str, dict[str, str]]], url: str) -> dict[str, str]:
        matched = [headers for called, headers in calls if called == url]
        assert matched, f"{url} was never requested (calls={calls})"
        return matched[-1]


@pytest.fixture
def install_fake(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[FakeFetcher], FakeFetcher]:
    """接管所有网络出口：假 fetcher + 不做 DNS/私网校验。"""
    monkeypatch.setattr(core, "validate_url_host", lambda *args, **kwargs: None)

    def install(fake: FakeFetcher) -> FakeFetcher:
        monkeypatch.setattr(core, "_get_app_fetcher", lambda *args, **kwargs: fake)
        return fake

    return install


def run_crawl(tmp_path: Path, fake: FakeFetcher, *extra: str) -> int:
    args = cr.build_parser().parse_args(
        [
            "--url",
            PAGE_URL,
            "--out",
            str(tmp_path),
            "--workers",
            "1",
            "--delay",
            "0",
            "--retries",
            "0",
            *extra,
        ]
    )
    return cr.crawl(args)


def read_manifest(tmp_path: Path) -> list[dict[str, Any]]:
    data = json.loads((tmp_path / "resources_manifest.json").read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return data


def page_ok() -> dict[str, Any]:
    return {"status": 200, "content": PAGE_HTML, "headers": {"Content-Type": "text/html"}}


def image_ok(
    data: bytes = IMG_DATA, etag: str = ETAG, last_modified: str = LAST_MODIFIED
) -> dict[str, Any]:
    return {
        "status": 200,
        "content": data,
        "headers": {"Content-Type": "image/png", "ETag": etag, "Last-Modified": last_modified},
    }


def seeded_cache(tmp_path: Path, relative: str = IMG_RELATIVE) -> Path:
    """写一条缓存条目（并可选地创建产物文件）。"""
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(IMG_DATA)
    cache_file = cache_mod.cache_path(tmp_path)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    IMG_URL: {
                        "etag": ETAG,
                        "last_modified": LAST_MODIFIED,
                        "saved_path": relative,
                        "content_type": "image/png",
                        "bytes": len(IMG_DATA),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


# ========== 首次抓取 / 304 复用 ==========


class TestCacheReuse:
    def test_first_run_records_validators(
        self, tmp_path: Path, install_fake: Callable[[FakeFetcher], FakeFetcher]
    ) -> None:
        """首次抓取（无条件头）下载资源并把校验器写进缓存。"""
        fake = install_fake(FakeFetcher({PAGE_URL: [page_ok()], IMG_URL: [image_ok()]}))
        assert run_crawl(tmp_path, fake, "--cache") == 0

        rows = read_manifest(tmp_path)
        assert [row["status"] for row in rows] == ["ok"]
        assert rows[0]["bytes"] == len(IMG_DATA)
        assert (tmp_path / IMG_RELATIVE).read_bytes() == IMG_DATA

        # 第一次请求不可能带条件头
        assert "If-None-Match" not in fake.headers_for(fake.download_calls, IMG_URL)

        payload = json.loads(cache_mod.cache_path(tmp_path).read_text(encoding="utf-8"))
        assert payload["version"] == cache_mod.CACHE_VERSION
        entry = payload["entries"][IMG_URL]
        assert entry["etag"] == ETAG
        assert entry["last_modified"] == LAST_MODIFIED
        assert entry["saved_path"] == IMG_RELATIVE  # 相对路径，目录可整体搬走
        assert entry["content_type"] == "image/png"
        assert entry["bytes"] == len(IMG_DATA)

    def test_second_run_sends_conditional_headers_and_reuses(
        self, tmp_path: Path, install_fake: Callable[[FakeFetcher], FakeFetcher]
    ) -> None:
        """第二次抓取发 If-None-Match/If-Modified-Since；304 时跳过下载并复用产物。"""
        first = install_fake(FakeFetcher({PAGE_URL: [page_ok()], IMG_URL: [image_ok()]}))
        assert run_crawl(tmp_path, first, "--cache") == 0
        cache_file = cache_mod.cache_path(tmp_path)
        cached_before = cache_file.read_bytes()
        mtime_before = cache_file.stat().st_mtime_ns

        second = install_fake(FakeFetcher({PAGE_URL: [page_ok()], IMG_URL: [{"status": 304}]}))
        assert run_crawl(tmp_path, second, "--cache") == 0

        headers = second.headers_for(second.download_calls, IMG_URL)
        assert headers["If-None-Match"] == ETAG
        assert headers["If-Modified-Since"] == LAST_MODIFIED

        rows = read_manifest(tmp_path)
        assert len(rows) == 1
        assert rows[0]["status"] == cache_mod.NOT_MODIFIED_STATUS
        assert rows[0]["saved_path"] == str((tmp_path / IMG_RELATIVE).resolve())
        assert rows[0]["bytes"] == len(IMG_DATA)
        assert rows[0]["content_type"] == "image/png"
        assert "304" in rows[0]["diagnostic"]
        # 复用的是上次产物，内容一个字节都没变
        assert (tmp_path / IMG_RELATIVE).read_bytes() == IMG_DATA
        # 304 不代表失败/跳过：摘要按成功行统计
        summary = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))
        assert summary["resources"]["ok"] == 1
        assert summary["resources"]["failed"] == 0
        assert summary["resources"]["downloaded_bytes"] == 0
        # 全部命中 304 时缓存无变更，不再重写文件
        assert cache_file.read_bytes() == cached_before
        assert cache_file.stat().st_mtime_ns == mtime_before

    def test_memory_path_304_also_reuses(
        self, tmp_path: Path, install_fake: Callable[[FakeFetcher], FakeFetcher]
    ) -> None:
        """需要内存内容（--dedup）时走 get() 路径，304 同样复用而不是写空文件。"""
        first = install_fake(FakeFetcher({PAGE_URL: [page_ok()], IMG_URL: [image_ok()]}))
        assert run_crawl(tmp_path, first, "--cache", "--dedup") == 0

        second = install_fake(FakeFetcher({PAGE_URL: [page_ok()], IMG_URL: [{"status": 304}]}))
        assert run_crawl(tmp_path, second, "--cache", "--dedup") == 0

        headers = second.headers_for(second.get_calls, IMG_URL)
        assert headers["If-None-Match"] == ETAG
        rows = read_manifest(tmp_path)
        assert rows[0]["status"] == cache_mod.NOT_MODIFIED_STATUS
        assert (tmp_path / IMG_RELATIVE).read_bytes() == IMG_DATA

    def test_200_after_conditional_request_updates_cache(
        self, tmp_path: Path, install_fake: Callable[[FakeFetcher], FakeFetcher]
    ) -> None:
        """条件请求后上游回 200（内容变了）：正常下载并刷新校验器。"""
        first = install_fake(FakeFetcher({PAGE_URL: [page_ok()], IMG_URL: [image_ok()]}))
        assert run_crawl(tmp_path, first, "--cache") == 0

        second = install_fake(
            FakeFetcher(
                {
                    PAGE_URL: [page_ok()],
                    IMG_URL: [image_ok(IMG_DATA_V2, ETAG_V2, "Thu, 22 Oct 2015 08:00:00 GMT")],
                }
            )
        )
        assert run_crawl(tmp_path, second, "--cache") == 0

        headers = second.headers_for(second.download_calls, IMG_URL)
        assert headers["If-None-Match"] == ETAG  # 旧校验器发出去了
        rows = read_manifest(tmp_path)
        assert rows[0]["status"] == "ok"
        assert rows[0]["bytes"] == len(IMG_DATA_V2)
        assert (tmp_path / IMG_RELATIVE).read_bytes() == IMG_DATA_V2

        entry = json.loads(cache_mod.cache_path(tmp_path).read_text(encoding="utf-8"))["entries"][
            IMG_URL
        ]
        assert entry["etag"] == ETAG_V2
        assert entry["last_modified"] == "Thu, 22 Oct 2015 08:00:00 GMT"
        assert entry["bytes"] == len(IMG_DATA_V2)


# ========== 降级路径（缓存永不导致任务失败）==========


class TestCacheDegradation:
    def test_corrupt_cache_is_ignored(
        self, tmp_path: Path, install_fake: Callable[[FakeFetcher], FakeFetcher]
    ) -> None:
        """缓存文件是乱码：当作空缓存，任务照常完成并重建缓存。"""
        cache_file = cache_mod.cache_path(tmp_path)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text("{not json", encoding="utf-8")

        fake = install_fake(FakeFetcher({PAGE_URL: [page_ok()], IMG_URL: [image_ok()]}))
        assert run_crawl(tmp_path, fake, "--cache") == 0

        assert "If-None-Match" not in fake.headers_for(fake.download_calls, IMG_URL)
        assert read_manifest(tmp_path)[0]["status"] == "ok"
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        assert payload["version"] == cache_mod.CACHE_VERSION
        assert IMG_URL in payload["entries"]

    def test_old_flat_format_is_ignored(
        self, tmp_path: Path, install_fake: Callable[[FakeFetcher], FakeFetcher]
    ) -> None:
        """没有 version/entries 外壳的旧/未知格式：忽略，不按它发条件请求。"""
        cache_file = cache_mod.cache_path(tmp_path)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({IMG_URL: {"etag": ETAG, "last_modified": LAST_MODIFIED}}),
            encoding="utf-8",
        )

        fake = install_fake(FakeFetcher({PAGE_URL: [page_ok()], IMG_URL: [image_ok()]}))
        assert run_crawl(tmp_path, fake, "--cache") == 0

        assert "If-None-Match" not in fake.headers_for(fake.download_calls, IMG_URL)
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        assert payload["version"] == cache_mod.CACHE_VERSION

    def test_missing_artifact_skips_conditional_request(
        self, tmp_path: Path, install_fake: Callable[[FakeFetcher], FakeFetcher]
    ) -> None:
        """缓存指向的产物已被删除：不发条件请求，正常重新下载。"""
        cache_file = cache_mod.cache_path(tmp_path)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": {
                        IMG_URL: {
                            "etag": ETAG,
                            "last_modified": LAST_MODIFIED,
                            "saved_path": "assets/example.com/img/gone.png",
                            "content_type": "image/png",
                            "bytes": 1,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        fake = install_fake(FakeFetcher({PAGE_URL: [page_ok()], IMG_URL: [image_ok()]}))
        assert run_crawl(tmp_path, fake, "--cache") == 0

        assert "If-None-Match" not in fake.headers_for(fake.download_calls, IMG_URL)
        assert read_manifest(tmp_path)[0]["status"] == "ok"
        assert (tmp_path / IMG_RELATIVE).read_bytes() == IMG_DATA

    def test_304_with_vanished_artifact_falls_back(
        self, tmp_path: Path, install_fake: Callable[[FakeFetcher], FakeFetcher]
    ) -> None:
        """查完缓存、304 回来时产物已消失：退回无条件抓取，不把任务判失败。"""
        artifact = seeded_cache(tmp_path)

        fake = install_fake(
            FakeFetcher(
                {
                    PAGE_URL: [page_ok()],
                    IMG_URL: [{"status": 304}, image_ok(IMG_DATA_V2, ETAG_V2)],
                }
            )
        )
        deleted = [False]

        def vanish(url: str) -> None:
            if url == IMG_URL and not deleted[0]:
                deleted[0] = True
                artifact.unlink()

        fake.on_download = vanish
        assert run_crawl(tmp_path, fake, "--cache") == 0

        rows = read_manifest(tmp_path)
        assert rows[0]["status"] == "ok"
        assert rows[0]["bytes"] == len(IMG_DATA_V2)
        assert (tmp_path / IMG_RELATIVE).read_bytes() == IMG_DATA_V2

        calls = [headers for called, headers in fake.download_calls if called == IMG_URL]
        assert len(calls) == 2
        assert calls[0]["If-None-Match"] == ETAG  # 第一次带条件头（304）
        assert "If-None-Match" not in calls[1]  # 退回复抓时不再带

    def test_network_error_keeps_normal_error_path(
        self, tmp_path: Path, install_fake: Callable[[FakeFetcher], FakeFetcher]
    ) -> None:
        """缓存开启时网络失败仍按原路径记为 error（缓存不吞错）。"""
        fake = install_fake(
            FakeFetcher({PAGE_URL: [page_ok()], IMG_URL: [{"status": 500, "content": b""}]})
        )
        assert run_crawl(tmp_path, fake, "--cache") == 0

        rows = read_manifest(tmp_path)
        assert rows[0]["status"].startswith("error")


# ========== 零行为变化 / list-only ==========


class TestCacheOff:
    def test_no_cache_flag_keeps_original_path(
        self, tmp_path: Path, install_fake: Callable[[FakeFetcher], FakeFetcher]
    ) -> None:
        """未开启 --cache：不建 .cache/、不发条件头，清单状态与老版本一致。"""
        seeded_cache(tmp_path)
        fake = install_fake(FakeFetcher({PAGE_URL: [page_ok()], IMG_URL: [image_ok()]}))
        assert run_crawl(tmp_path, fake) == 0

        rows = read_manifest(tmp_path)
        assert rows[0]["status"] == "ok"
        assert "If-None-Match" not in fake.headers_for(fake.download_calls, IMG_URL)
        assert "If-Modified-Since" not in fake.headers_for(fake.download_calls, IMG_URL)
        # 缓存已有条目也不会被加载/改写
        payload = json.loads(cache_mod.cache_path(tmp_path).read_text(encoding="utf-8"))
        assert payload["entries"][IMG_URL]["etag"] == ETAG

    def test_list_only_does_not_write_cache(
        self, tmp_path: Path, install_fake: Callable[[FakeFetcher], FakeFetcher]
    ) -> None:
        """--list-only 不下载资源，因此不写缓存文件（也不建 .cache 目录）。"""
        fake = install_fake(FakeFetcher({PAGE_URL: [page_ok()]}))
        assert run_crawl(tmp_path, fake, "--cache", "--list-only") == 0

        assert [row["status"] for row in read_manifest(tmp_path)] == ["listed only"]
        assert not cache_mod.cache_path(tmp_path).exists()
        assert not (tmp_path / cache_mod.CACHE_DIRNAME).exists()
        assert fake.download_calls == []


# ========== 与 --resume-crawl 的独立性 ==========


class TestCacheAndResumeState:
    def test_save_does_not_touch_crawl_state(self, tmp_path: Path) -> None:
        cache = cache_mod.ValidatorCache.load(tmp_path)
        artifact = tmp_path / IMG_RELATIVE
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(IMG_DATA)
        cache.record(
            IMG_URL,
            tmp_path,
            artifact,
            content_type="image/png",
            byte_count=len(IMG_DATA),
            etag=ETAG,
        )
        assert cache.save(tmp_path) is True

        state_path = tmp_path / ctx_mod.CRAWL_STATE_FILE
        ctx_mod.save_crawl_state(state_path.parent, seed_url=PAGE_URL, page_queue=[PAGE_URL])
        assert cache_mod.cache_path(tmp_path).exists()
        # 再存一次缓存，状态文件原样保留
        again = cache_mod.ValidatorCache.load(tmp_path)
        again.record(
            IMG_URL, tmp_path, artifact, content_type="image/png", byte_count=1, etag='"v9"'
        )
        assert again.save(tmp_path) is True
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["seed_url"] == PAGE_URL

    def test_resume_crawl_with_cache_smoke(
        self, tmp_path: Path, install_fake: Callable[[FakeFetcher], FakeFetcher]
    ) -> None:
        """--resume-crawl 与 --cache 同时开启可正常跑完，缓存条目保留。"""
        seeded_cache(tmp_path)
        ctx_mod.save_crawl_state(tmp_path, seed_url=PAGE_URL, page_queue=[PAGE_URL], seen_pages=[])

        fake = install_fake(FakeFetcher({PAGE_URL: [page_ok()], IMG_URL: [{"status": 304}]}))
        assert run_crawl(tmp_path, fake, "--cache", "--resume-crawl") == 0

        assert read_manifest(tmp_path)[0]["status"] == cache_mod.NOT_MODIFIED_STATUS
        payload = json.loads(cache_mod.cache_path(tmp_path).read_text(encoding="utf-8"))
        assert payload["entries"][IMG_URL]["etag"] == ETAG


# ========== ValidatorCache 单元测试 ==========


class TestValidatorCacheUnit:
    def test_default_path(self, tmp_path: Path) -> None:
        assert cache_mod.cache_path(tmp_path) == tmp_path / ".cache" / "validators.json"

    def test_save_clean_cache_writes_nothing(self, tmp_path: Path) -> None:
        assert cache_mod.ValidatorCache().save(tmp_path) is False
        assert not cache_mod.cache_path(tmp_path).exists()

    def test_missing_file_loads_empty(self, tmp_path: Path) -> None:
        assert len(cache_mod.ValidatorCache.load(tmp_path)) == 0

    def test_conditional_headers_need_a_validator(self, tmp_path: Path) -> None:
        artifact = tmp_path / IMG_RELATIVE
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(IMG_DATA)
        cache = cache_mod.ValidatorCache.load(tmp_path)
        cache.record(IMG_URL, tmp_path, artifact, content_type="image/png", byte_count=1)
        # 服务器没给 ETag/Last-Modified：无从条件请求
        assert cache.conditional_headers(IMG_URL, tmp_path) == {}

    def test_path_traversal_entry_is_unusable(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside-secret.bin"
        outside.write_bytes(b"secret")
        entry = cache_mod.CachedArtifact(
            etag=ETAG, saved_path="../outside-secret.bin", content_type="text/plain"
        )
        # 绝对路径与 ../ 逃逸都不能被复用（缓存文件可能被篡改）
        assert cache_mod.ValidatorCache.resolve_saved_path(entry, tmp_path) is None
        assert (
            cache_mod.ValidatorCache.resolve_saved_path(
                cache_mod.CachedArtifact(etag=ETAG, saved_path=str(outside)), tmp_path
            )
            is None
        )

    def test_record_outside_output_dir_is_dropped(self, tmp_path: Path) -> None:
        cache = cache_mod.ValidatorCache.load(tmp_path)
        outside = tmp_path.parent / "elsewhere.bin"
        outside.write_bytes(b"x")
        cache.record(IMG_URL, tmp_path, outside, etag=ETAG)
        assert len(cache) == 0
        assert cache.save(tmp_path) is False

    def test_load_partial_entries_skips_bad_rows(self, tmp_path: Path) -> None:
        path = cache_mod.cache_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": {
                        IMG_URL: {"etag": ETAG, "saved_path": IMG_RELATIVE},
                        "https://example.com/bad": "not-a-dict",
                    },
                }
            ),
            encoding="utf-8",
        )
        cache = cache_mod.ValidatorCache.load(tmp_path)
        assert len(cache) == 1

    def test_load_non_dict_payload_ignored(self, tmp_path: Path) -> None:
        path = cache_mod.cache_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert len(cache_mod.ValidatorCache.load(tmp_path)) == 0

    def test_load_skips_empty_url_keys(self, tmp_path: Path) -> None:
        path = cache_mod.cache_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "entries": {"": {"etag": ETAG, "saved_path": IMG_RELATIVE}}}),
            encoding="utf-8",
        )
        assert len(cache_mod.ValidatorCache.load(tmp_path)) == 0

    def test_save_failure_restores_dirty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        artifact = tmp_path / IMG_RELATIVE
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(IMG_DATA)
        cache = cache_mod.ValidatorCache()
        cache.record(IMG_URL, tmp_path, artifact, etag=ETAG, content_type="image/png", byte_count=1)

        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(cache_mod, "atomic_write_text", boom)
        with pytest.raises(OSError):
            cache.save(tmp_path)
        # 写失败后仍保持 dirty：恢复写盘能力后再次保存应成功
        monkeypatch.undo()
        assert cache.save(tmp_path) is True

    def test_conditional_headers_etag_only(self, tmp_path: Path) -> None:
        artifact = tmp_path / IMG_RELATIVE
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(IMG_DATA)
        cache = cache_mod.ValidatorCache()
        cache.record(IMG_URL, tmp_path, artifact, etag=ETAG, content_type="image/png", byte_count=1)
        assert cache.conditional_headers(IMG_URL, tmp_path) == {"If-None-Match": ETAG}

    def test_conditional_headers_last_modified_only(self, tmp_path: Path) -> None:
        artifact = tmp_path / IMG_RELATIVE
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(IMG_DATA)
        cache = cache_mod.ValidatorCache()
        cache.record(
            IMG_URL,
            tmp_path,
            artifact,
            last_modified=LAST_MODIFIED,
            content_type="image/png",
            byte_count=1,
        )
        assert cache.conditional_headers(IMG_URL, tmp_path) == {"If-Modified-Since": LAST_MODIFIED}

    def test_reuse_missing_or_unstattable_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = cache_mod.ValidatorCache()
        assert cache.reuse(IMG_URL, tmp_path) is None

        artifact = tmp_path / IMG_RELATIVE
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(IMG_DATA)
        cache.record(IMG_URL, tmp_path, artifact, etag=ETAG, content_type="image/png", byte_count=1)

        # 越过 resolve_saved_path 的 is_file 检查，专门命中 reuse() 里的 stat 失败分支
        monkeypatch.setattr(
            cache_mod.ValidatorCache,
            "resolve_saved_path",
            staticmethod(lambda _artifact, _output_dir: artifact),
        )

        def broken_stat(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("gone")

        monkeypatch.setattr(Path, "stat", broken_stat)
        assert cache.reuse(IMG_URL, tmp_path) is None

    def test_resolve_empty_or_unresolvable_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert (
            cache_mod.ValidatorCache.resolve_saved_path(
                cache_mod.CachedArtifact(etag=ETAG, saved_path=""), tmp_path
            )
            is None
        )

        original_resolve = Path.resolve
        cache = cache_mod.ValidatorCache()
        artifact = tmp_path / IMG_RELATIVE
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(IMG_DATA)
        cache.record(IMG_URL, tmp_path, artifact, etag=ETAG, content_type="image/png", byte_count=1)

        def broken_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
            raise ValueError("bad path")

        monkeypatch.setattr(Path, "resolve", broken_resolve)
        assert cache.reuse(IMG_URL, tmp_path) is None
        monkeypatch.setattr(Path, "resolve", original_resolve)

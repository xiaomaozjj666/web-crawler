"""抓取状态/配置的原子写：替换失败时旧文件必须保持完整。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from web_crawler.app import _crawler_context as ctx
from web_crawler.app import _fsutil


def test_save_crawl_state_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx.save_crawl_state(tmp_path, pages=["a"], seen=1)
    path = tmp_path / ctx.CRAWL_STATE_FILE
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(_fsutil.os, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        ctx.save_crawl_state(tmp_path, pages=["b"], seen=2)

    assert path.read_bytes() == before
    assert not list(tmp_path.glob(ctx.CRAWL_STATE_FILE + ".*.tmp"))


def test_save_config_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = ctx.argparse.Namespace(
        url="https://example.com",
        out=str(tmp_path / "out"),
        same_domain=False,
        crawl_pages=False,
        max_pages=1,
        include_css_urls=False,
        rewrite_html=False,
        strip_overlays=False,
        decrypt=False,
        video_mode=False,
        video_only=False,
        list_only=False,
        expand_playlists=False,
        respect_robots=False,
        timeout=20,
        retries=1,
        delay=0.5,
        workers=8,
        max_bytes=0,
        encoding=None,
        user_agent="ua",
        header=[],
        block_keyword=[],
        resume=False,
        organize=False,
        dedup=False,
        sitemap=False,
        smart_extract=False,
        resume_crawl=False,
        extract_text=False,
        include_pattern=None,
        exclude_pattern=None,
        proxy=None,
        stealth=False,
        impersonate="chrome131",
        render_js=False,
        download_linked_data=False,
        rules=None,
        field_rules=None,
    )
    target = tmp_path / "config.json"
    ctx.save_config_to_file(args, str(target))
    before = target.read_bytes()

    monkeypatch.setattr(_fsutil.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("busy")))
    args.max_pages = 99
    with pytest.raises(OSError, match="busy"):
        ctx.save_config_to_file(args, str(target))

    assert target.read_bytes() == before
    assert not list(tmp_path.glob("config.json.*.tmp"))


def test_state_roundtrip(tmp_path: Path) -> None:
    ctx.save_crawl_state(tmp_path, pages=["a"], seen=2)
    loaded = ctx.load_crawl_state(tmp_path)
    assert loaded == {"pages": ["a"], "seen": 2}
    assert json.loads((tmp_path / ctx.CRAWL_STATE_FILE).read_text(encoding="utf-8"))["seen"] == 2

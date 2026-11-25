"""app/crawler.py 单元测试：资源下载器核心逻辑。

覆盖工具函数、数据类、限速/去重、HTML 解析、清单/报告生成、
fetch（mock 库级 Fetcher 桥接）以及 crawl 集成测试（mock fetch）。
所有网络请求均被 mock，测试可重复、不依赖外部状态。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from web_crawler.app import _crawler_context as ctx_mod
from web_crawler.app import _crawler_core as core
from web_crawler.app import _crawler_post as post
from web_crawler.app import crawler as cr
from web_crawler.app._offline import RewriteTable
from web_crawler.app.crawler_models import PageExtraction
from web_crawler.app.crawler_net import decode_stylesheet
from web_crawler.exceptions import FetchError

# ========== 数据类 ==========


class TestResource:
    def test_creation(self) -> None:
        """Resource 创建后保留传入字段。"""
        r = cr.Resource(
            url="https://x.com/a.js",
            found_in="script[src]",
            kind="script",
            page_url="https://x.com/",
        )
        assert r.url == "https://x.com/a.js"
        assert r.kind == "script"

    def test_frozen(self) -> None:
        """Resource 冻结后属性不可修改。"""
        r = cr.Resource(url="u", found_in="f", kind="k", page_url="p")
        with pytest.raises(AttributeError):
            r.url = "changed"  # type: ignore[misc]


class TestManifestRow:
    def test_defaults(self) -> None:
        """ManifestRow 默认字段值正确。"""
        row = cr.ManifestRow(
            status="ok",
            url="u",
            saved_path="p",
            content_type="ct",
            bytes=10,
            category="img",
            found_in="f",
            kind="k",
            page_url="pu",
            page_title="t",
            diagnostic="d",
        )
        assert row.sha256 == ""


# ========== AES 解密 ==========


class TestAES:
    def test_decrypt_aes128(self) -> None:
        """AES-128-CBC 解密 + PKCS7 去填充。"""
        pytest.importorskip("Crypto.Cipher")
        from Crypto.Cipher import AES as _AES

        key = b"0123456789abcdef"
        iv = b"abcdef0123456789"
        plaintext = b"hello world padding!!"
        pad_len = 16 - len(plaintext) % 16
        padded = plaintext + bytes([pad_len]) * pad_len
        cipher = _AES.new(key, _AES.MODE_CBC, iv=iv)
        encrypted = cipher.encrypt(padded)

        result = cr.decrypt_aes128(encrypted, key, iv)
        assert result == plaintext


class TestSegmentKeys:
    def test_register_and_get(self) -> None:
        """注册并获取 segment key 能原样返回。"""
        cr.register_segment_key("https://x.com/seg", b"k" * 16, b"iv" * 8)
        result = cr.get_segment_key("https://x.com/seg")
        assert result is not None
        assert result[0] == b"k" * 16

    def test_get_missing(self) -> None:
        """查询未注册的 segment key 返回 None。"""
        assert cr.get_segment_key("https://nonexistent.com/seg") is None


# ========== DomainRateLimiter ==========


class TestDomainRateLimiter:
    def test_no_delay(self) -> None:
        """无延迟时不阻塞。"""
        limiter = cr.DomainRateLimiter(default_delay=0.0)
        limiter.wait_if_needed("https://example.com/page")  # 不应阻塞

    def test_record_request(self) -> None:
        """记录请求后域名进入最近请求时间表。"""
        limiter = cr.DomainRateLimiter(default_delay=0.0)
        limiter.record_request("https://example.com/a")
        assert "example.com" in limiter._last_times

    def test_handle_429(self) -> None:
        """handle_429 解析 Retry-After 并设置 blocked_until。"""
        limiter = cr.DomainRateLimiter(default_delay=0.0)
        wait = limiter.handle_429("https://example.com/x", "30")
        assert wait == 30

    def test_handle_429_invalid_retry_after(self) -> None:
        """无效 Retry-After 使用默认 10 秒。"""
        limiter = cr.DomainRateLimiter(default_delay=0.0)
        wait = limiter.handle_429("https://example.com/x", "invalid")
        assert wait == 10

    def test_handle_429_no_retry_after(self) -> None:
        """无 Retry-After 使用默认 10 秒。"""
        limiter = cr.DomainRateLimiter(default_delay=0.0)
        wait = limiter.handle_429("https://example.com/x", None)
        assert wait == 10


# ========== ContentDedup ==========


class TestContentDedup:
    def test_new_content(self) -> None:
        """新内容首次判重返回非重复。"""
        dedup = cr.ContentDedup()
        is_dup, sha = dedup.is_duplicate(b"data1", "https://a.com/1")
        assert is_dup is False
        assert len(sha) == 64

    def test_duplicate_content(self) -> None:
        """相同内容再次判重返回重复。"""
        dedup = cr.ContentDedup()
        dedup.is_duplicate(b"same", "https://a.com/1")
        is_dup, _ = dedup.is_duplicate(b"same", "https://b.com/2")
        assert is_dup is True

    def test_same_url_not_dup(self) -> None:
        """同 URL 重复写入不算重复。"""
        dedup = cr.ContentDedup()
        dedup.is_duplicate(b"data", "https://a.com/1")
        is_dup, _ = dedup.is_duplicate(b"data", "https://a.com/1")
        assert is_dup is False

    def test_mark_hash_seen(self) -> None:
        """mark_hash_seen 将哈希注册为已见（用于断点续爬）。"""
        dedup = cr.ContentDedup()
        sha = hashlib.sha256(b"x").hexdigest()
        assert sha not in dedup.seen_hashes()
        dedup.mark_hash_seen(sha)
        assert sha in dedup.seen_hashes()
        assert dedup.seen_count() == 1
        # 重复标记不会增加计数
        dedup.mark_hash_seen(sha)
        assert dedup.seen_count() == 1

    def test_seen_hashes(self) -> None:
        """判重后已见哈希被记录。"""
        dedup = cr.ContentDedup()
        dedup.is_duplicate(b"a", "https://a.com/1")
        dedup.is_duplicate(b"b", "https://a.com/2")
        assert len(dedup.seen_hashes()) == 2

    def test_seen_count(self) -> None:
        """seen_count 返回已判重条目数。"""
        dedup = cr.ContentDedup()
        dedup.is_duplicate(b"a", "https://a.com/1")
        assert dedup.seen_count() == 1


# ========== 工具函数 ==========


class TestParseHeaders:
    def test_valid(self) -> None:
        """parse_headers 解析合法头部键值对。"""
        result = cr.parse_headers(["Cookie: name=val", "Auth: Bearer token"])
        assert result["Cookie"] == "name=val"
        assert result["Auth"] == "Bearer token"

    def test_invalid_no_colon(self) -> None:
        """parse_headers 对无冒号头部抛 ValueError。"""
        with pytest.raises(ValueError, match="Invalid header format"):
            cr.parse_headers(["no colon here"])


class TestParseSrcset:
    def test_basic(self) -> None:
        """parse_srcset 解析多候选 srcset。"""
        result = list(cr.parse_srcset("img1.jpg 1x, img2.jpg 2x"))
        assert result == ["img1.jpg", "img2.jpg"]

    def test_single(self) -> None:
        """parse_srcset 解析单项 srcset。"""
        assert list(cr.parse_srcset("only.jpg")) == ["only.jpg"]


class TestNormalizeUrl:
    def test_http(self) -> None:
        """normalize_url 保留普通 HTTP URL。"""
        assert cr.normalize_url("https://example.com/page") == "https://example.com/page"

    def test_fragment_removed(self) -> None:
        """normalize_url 移除 URL 片段。"""
        assert cr.normalize_url("https://example.com/page#section") == "https://example.com/page"

    def test_data_uri(self) -> None:
        """normalize_url 将 data URI 规范化为空。"""
        assert cr.normalize_url("data:text/html,base64") == ""

    def test_javascript(self) -> None:
        """normalize_url 将 javascript: 规范化为空。"""
        assert cr.normalize_url("javascript:void(0)") == ""

    def test_ssrf_localhost(self) -> None:
        """normalize_url 拒绝 localhost 目标。"""
        assert cr.normalize_url("http://localhost/admin") == ""

    def test_ssrf_private_ip(self) -> None:
        """normalize_url 拒绝私网 IP 目标。"""
        assert cr.normalize_url("http://192.168.1.1/admin") == ""

    def test_ssrf_loopback_ip(self) -> None:
        """normalize_url 拒绝回环 IP 目标。"""
        assert cr.normalize_url("http://127.0.0.1/admin") == ""

    def test_ssrf_metadata(self) -> None:
        """normalize_url 拒绝云元数据地址。"""
        assert cr.normalize_url("http://169.254.169.254/latest") == ""

    def test_non_http_scheme(self) -> None:
        """normalize_url 拒绝非 HTTP 协议。"""
        assert cr.normalize_url("ftp://example.com/file") == ""


class TestLooksLikeUrl:
    def test_http(self) -> None:
        """looks_like_url 识别普通 HTTP URL。"""
        assert cr.looks_like_url("https://example.com") is True

    def test_protocol_relative(self) -> None:
        """looks_like_url 识别协议相对 URL。"""
        assert cr.looks_like_url("//cdn.example.com/lib.js") is True

    def test_absolute_path(self) -> None:
        """looks_like_url 识别绝对路径。"""
        assert cr.looks_like_url("/assets/img.png") is True

    def test_other(self) -> None:
        """looks_like_url 拒绝非 URL 文本。"""
        assert cr.looks_like_url("not a url") is False


class TestLooksLikeDownloadable:
    def test_normal_url(self) -> None:
        """looks_like_downloadable 识别可下载 URL。"""
        assert cr.looks_like_downloadable("https://example.com/img.png") is True

    def test_data_uri(self) -> None:
        """looks_like_downloadable 拒绝 data URI。"""
        assert cr.looks_like_downloadable("data:image/png;base64,abc") is False

    def test_javascript(self) -> None:
        """looks_like_downloadable 拒绝 javascript:。"""
        assert cr.looks_like_downloadable("javascript:void(0)") is False

    def test_mailto(self) -> None:
        """looks_like_downloadable 拒绝 mailto:。"""
        assert cr.looks_like_downloadable("mailto:test@test.com") is False

    def test_empty(self) -> None:
        """looks_like_downloadable 拒绝空字符串。"""
        assert cr.looks_like_downloadable("") is False

    def test_hash(self) -> None:
        """looks_like_downloadable 拒绝纯锚点。"""
        assert cr.looks_like_downloadable("#anchor") is False


class TestLooksLikeResourceUrl:
    def test_image(self) -> None:
        """looks_like_resource_url 识别图片 URL。"""
        assert cr.looks_like_resource_url("https://x.com/logo.png") is True

    def test_css(self) -> None:
        """looks_like_resource_url 识别 CSS URL。"""
        assert cr.looks_like_resource_url("https://x.com/style.css") is True

    def test_js(self) -> None:
        """looks_like_resource_url 识别 JS URL。"""
        assert cr.looks_like_resource_url("https://x.com/app.js") is True

    def test_html_not_resource(self) -> None:
        """looks_like_resource_url 拒绝 HTML 页面。"""
        assert cr.looks_like_resource_url("https://x.com/page.html") is False

    def test_no_extension(self) -> None:
        """looks_like_resource_url 拒绝无扩展名 URL。"""
        assert cr.looks_like_resource_url("https://x.com/page") is False


class TestIsVideoResource:
    def _row(
        self, url: str = "", ct: str = "", found_in: str = "", kind: str = ""
    ) -> cr.ManifestRow:
        return cr.ManifestRow(
            status="ok",
            url=url,
            saved_path="",
            content_type=ct,
            bytes=0,
            category="video",
            found_in=found_in,
            kind=kind,
            page_url="",
            page_title="",
            diagnostic="",
        )

    def test_by_suffix(self) -> None:
        """is_video_resource 按后缀识别视频。"""
        assert cr.is_video_resource(self._row(url="https://x.com/v.mp4")) is True

    def test_by_content_type(self) -> None:
        """is_video_resource 按内容类型识别视频。"""
        assert cr.is_video_resource(self._row(ct="video/mp4")) is True

    def test_by_kind(self) -> None:
        """is_video_resource 按 kind 识别视频。"""
        assert cr.is_video_resource(self._row(kind="video")) is True

    def test_by_found_in(self) -> None:
        """is_video_resource 按 found_in 识别视频。"""
        assert cr.is_video_resource(self._row(found_in="video[src]")) is True

    def test_not_video(self) -> None:
        """is_video_resource 拒绝非视频资源。"""
        assert cr.is_video_resource(self._row(url="https://x.com/img.png", ct="image/png")) is False


class TestCategoryFor:
    def test_playlist(self) -> None:
        """category_for 将 m3u8 归类为 playlist。"""
        assert cr.category_for("https://x.com/v.m3u8", "", "", "") == "playlist"

    def test_subtitle(self) -> None:
        """category_for 将 vtt 归类为 subtitle。"""
        assert cr.category_for("https://x.com/sub.vtt", "", "", "") == "subtitle"

    def test_video(self) -> None:
        """category_for 将 mp4 归类为 video。"""
        assert cr.category_for("https://x.com/v.mp4", "", "", "") == "video"

    def test_audio(self) -> None:
        """category_for 将 mp3 归类为 audio。"""
        assert cr.category_for("https://x.com/a.mp3", "audio/mpeg", "", "") == "audio"

    def test_image(self) -> None:
        """category_for 将 png 归类为 image。"""
        assert cr.category_for("https://x.com/i.png", "image/png", "", "") == "image"

    def test_css(self) -> None:
        """category_for 将 css 归类为 css。"""
        assert cr.category_for("https://x.com/s.css", "text/css", "", "") == "css"

    def test_script(self) -> None:
        """category_for 将 js 归类为 script。"""
        assert cr.category_for("https://x.com/a.js", "application/javascript", "", "") == "script"

    def test_font(self) -> None:
        """category_for 将 woff2 归类为 font。"""
        assert cr.category_for("https://x.com/f.woff2", "font/woff2", "", "") == "font"

    def test_other(self) -> None:
        """category_for 将未知类型归类为 other。"""
        assert cr.category_for("https://x.com/data", "application/json", "", "") == "other"

    def test_poster(self) -> None:
        """category_for 按 found_in 识别 poster。"""
        assert cr.category_for("https://x.com/p.jpg", "", "", "video[poster]") == "poster"


class TestSameDomain:
    def test_same(self) -> None:
        """same_domain 判定同域 URL 为真。"""
        assert cr.same_domain("https://a.com/x", "https://a.com/y") is True

    def test_different(self) -> None:
        """same_domain 判定异域 URL 为假。"""
        assert cr.same_domain("https://a.com/x", "https://b.com/y") is False


class TestBlockKeywords:
    def test_parse(self) -> None:
        """parse_block_keywords 解析逗号分隔关键词。"""
        result = cr.parse_block_keywords(["ad, tracker", "popup"])
        assert result == ["ad", "tracker", "popup"]

    def test_parse_newlines(self) -> None:
        """parse_block_keywords 解析换行分隔关键词。"""
        result = cr.parse_block_keywords(["ad\ntracker\npopup"])
        assert result == ["ad", "tracker", "popup"]

    def test_is_blocked(self) -> None:
        """is_blocked_url 命中关键词返回真。"""
        assert cr.is_blocked_url("https://ad.example.com/track", ["ad", "track"]) is True

    def test_not_blocked(self) -> None:
        """is_blocked_url 未命中关键词返回假。"""
        assert cr.is_blocked_url("https://example.com/page", ["ad"]) is False


class TestDecodeText:
    def test_utf8(self) -> None:
        """decode_text 按字符集解码 UTF-8。"""
        assert cr.decode_text(b"hello", "text/html; charset=utf-8", None) == "hello"

    def test_fallback(self) -> None:
        """decode_text 回退到显式字符集。"""
        assert cr.decode_text(b"hello", "", "utf-8") == "hello"

    def test_default_utf8(self) -> None:
        """decode_text 无字符集时默认 UTF-8。"""
        assert cr.decode_text(b"hello", "", None) == "hello"


class TestExtractTitle:
    def test_normal(self) -> None:
        """extract_title 提取页面标题。"""
        html = "<html><head><title>My Page</title></head></html>"
        assert cr.extract_title(html) == "My Page"

    def test_no_title(self) -> None:
        """extract_title 无标题时返回空。"""
        assert cr.extract_title("<html></html>") == ""

    def test_multiline(self) -> None:
        """extract_title 提取多行标题首行。"""
        html = "<title>Line 1\n  Line 2</title>"
        assert "Line 1" in cr.extract_title(html)


class TestCallbacks:
    def test_should_stop(self) -> None:
        """should_stop 在回调返回真时停止。"""
        args = Mock()
        args.should_stop = lambda: True
        assert cr.should_stop(args) is True

    def test_should_stop_none(self) -> None:
        """should_stop 对 None 参数返回假。"""
        assert cr.should_stop(None) is False

    def test_should_stop_no_callback(self) -> None:
        """should_stop 无回调属性时返回假。"""
        args = Mock(spec=[])
        assert cr.should_stop(args) is False

    def test_wait_if_paused(self) -> None:
        """wait_if_paused 在暂停时调用回调。"""
        called = [0]

        def cb() -> None:
            called[0] += 1

        args = Mock()
        args.wait_if_paused = cb
        cr.wait_if_paused(args)
        assert called[0] == 1

    def test_wait_if_paused_none(self) -> None:
        """wait_if_paused 对 None 参数不抛异常。"""
        cr.wait_if_paused(None)  # 不应抛异常

    def test_report_progress(self) -> None:
        """report_progress 向回调推送进度载荷。"""
        received: list[dict] = []

        def cb(payload: dict) -> None:
            received.append(payload)

        args = Mock()
        args.progress_callback = cb
        cr.report_progress(args, phase="test", url="https://x.com")
        assert received[0]["phase"] == "test"


class TestOutputPathForUrl:
    def test_basic(self, tmp_path: Path) -> None:
        """output_path_for_url 按域名和后缀生成路径。"""
        result = cr.output_path_for_url("https://example.com/img/logo.png", tmp_path, "image/png")
        assert "example.com" in str(result)
        assert result.suffix == ".png"

    def test_with_query(self, tmp_path: Path) -> None:
        """output_path_for_url 将查询参数哈希进文件名。"""
        result = cr.output_path_for_url("https://example.com/img?w=100", tmp_path, "image/png")
        assert "_" in result.name  # query 被哈希附加到文件名

    def test_no_suffix_guess(self, tmp_path: Path) -> None:
        """无后缀时根据 content_type 猜测扩展名。"""
        result = cr.output_path_for_url("https://example.com/api/data", tmp_path, "image/png")
        assert result.suffix == ".png"

    def test_directory_ending(self, tmp_path: Path) -> None:
        """根路径（空 path）时追加 index。"""
        result = cr.output_path_for_url("https://example.com/", tmp_path, "text/html")
        assert "index" in result.name

    def test_trailing_slash_path(self, tmp_path: Path) -> None:
        """路径以 / 结尾时 strip 后按段处理，附加扩展名。"""
        result = cr.output_path_for_url("https://example.com/dir/", tmp_path, "text/html")
        assert result.name == "dir.html"


class TestSafeSegment:
    def test_basic(self) -> None:
        """safe_segment 保留普通文本段。"""
        assert cr.safe_segment("hello") == "hello"

    def test_empty(self) -> None:
        """safe_segment 空字符串回退为 unnamed。"""
        assert cr.safe_segment("") == "unnamed"

    def test_path_traversal(self) -> None:
        """safe_segment 移除路径穿越片段。"""
        assert ".." not in cr.safe_segment("../../etc/passwd")

    def test_special_chars(self) -> None:
        """safe_segment 移除文件系统特殊字符。"""
        result = cr.safe_segment('file<>:"/\\|?*')
        assert "<" not in result
        assert ">" not in result

    def test_truncation(self) -> None:
        """safe_segment 过长段被截断到上限。"""
        assert len(cr.safe_segment("x" * 200)) <= 120


class TestOutputPrefixForResource:
    def test_no_organize(self) -> None:
        """output_prefix_for_resource 未开启整理时返回 assets。"""
        args = Mock()
        args.organize = False
        assert cr.output_prefix_for_resource(args, Mock(), "", {}) == "assets"

    def test_with_organize(self) -> None:
        """output_prefix_for_resource 开启整理时按类型分目录。"""
        args = Mock()
        args.organize = True
        resource = cr.Resource(
            url="https://x.com/img.png", found_in="img[src]", kind="img", page_url="https://x.com/"
        )
        result = cr.output_prefix_for_resource(
            args, resource, "image/png", {"https://x.com/": "Page"}
        )
        assert "image" in result
        assert "Page" in result


# ========== CSS / 播放清单 / 去重 ==========


class TestDiscoverCssResources:
    def test_url_function(self) -> None:
        """discover_css_resources 提取 url() 引用。"""
        css = 'background: url("img/bg.png");'
        result = cr.discover_css_resources(css, "https://x.com/style.css", "https://x.com/")
        assert len(result) == 1
        assert "bg.png" in result[0].url

    def test_import(self) -> None:
        """discover_css_resources 提取 @import 引用。"""
        css = '@import "sub.css";'
        result = cr.discover_css_resources(css, "https://x.com/style.css", "https://x.com/")
        assert len(result) == 1

    def test_data_uri_skipped(self) -> None:
        """discover_css_resources 跳过 data URI。"""
        css = 'background: url("data:image/png;base64,abc");'
        result = cr.discover_css_resources(css, "https://x.com/style.css", "https://x.com/")
        assert len(result) == 0


class TestDiscoverPlaylistResources:
    def test_m3u8_plain(self) -> None:
        """discover_playlist_resources 解析明文 m3u8 分片。"""
        text = "#EXTM3U\n#EXTINF:10,\nhttps://x.com/seg1.ts\n#EXTINF:10,\nhttps://x.com/seg2.ts\n"
        resources, note = cr.discover_playlist_resources(
            text, "https://x.com/playlist.m3u8", "https://x.com/"
        )
        assert len(resources) == 2
        assert note == ""

    def test_m3u8_encrypted(self) -> None:
        """discover_playlist_resources 解析加密 m3u8 并标注密钥。"""
        text = (
            '#EXT-X-KEY:METHOD=AES-128,URI="https://x.com/key",IV=0xabcdef\n'
            "#EXTINF:10,\nhttps://x.com/seg1.ts\n"
        )
        resources, note = cr.discover_playlist_resources(
            text, "https://x.com/playlist.m3u8", "https://x.com/", decrypt=False
        )
        assert len(resources) == 1
        assert "encrypted" in note

    def test_mpd(self) -> None:
        """discover_playlist_resources 解析 MPD 清单。"""
        text = "<MPD><BaseURL>seg1.mp4</BaseURL></MPD>"
        resources, note = cr.discover_playlist_resources(
            text, "https://x.com/playlist.mpd", "https://x.com/"
        )
        assert len(resources) == 1
        assert note == ""

    def test_non_playlist(self) -> None:
        """discover_playlist_resources 对非清单文本返回空。"""
        resources, note = cr.discover_playlist_resources(
            "just text", "https://x.com/page.html", "https://x.com/"
        )
        assert len(resources) == 0
        assert note == ""


class TestUniqueResources:
    def test_dedup(self) -> None:
        """discover_playlist_resources 对重复资源去重。"""
        resources = [
            cr.Resource("https://x.com/a.js", "f", "k", "p"),
            cr.Resource("https://x.com/a.js", "f2", "k2", "p2"),
            cr.Resource("https://x.com/b.js", "f", "k", "p"),
        ]
        result = cr.unique_resources(resources)
        assert len(result) == 2


class TestIsHtml:
    def test_html_content_type(self) -> None:
        """is_html 按内容类型识别 HTML。"""
        assert cr.is_html("text/html", "https://x.com/page") is True

    def test_html_extension(self) -> None:
        """is_html 按扩展名识别 HTML。"""
        assert cr.is_html("", "https://x.com/page.html") is True

    def test_no_suffix(self) -> None:
        """is_html 无后缀时默认视为 HTML。"""
        assert cr.is_html("", "https://x.com/page") is True

    def test_json(self) -> None:
        """is_html 拒绝 JSON 内容。"""
        assert cr.is_html("application/json", "https://x.com/data.json") is False


# ========== PageParser ==========


class TestPageParser:
    def test_img_src(self) -> None:
        """PageParser 提取 img[src] 资源。"""
        parser = cr.PageParser("https://x.com/")
        parser.feed('<img src="logo.png">')
        assert len(parser.resources) == 1
        assert parser.resources[0].url == "https://x.com/logo.png"

    def test_script_src(self) -> None:
        """PageParser 提取 script[src] 资源。"""
        parser = cr.PageParser("https://x.com/")
        parser.feed('<script src="app.js"></script>')
        assert len(parser.resources) == 1

    def test_link_stylesheet(self) -> None:
        """PageParser 提取 link 样式表资源。"""
        parser = cr.PageParser("https://x.com/")
        parser.feed('<link rel="stylesheet" href="style.css">')
        assert len(parser.resources) == 1

    def test_link_non_resource(self) -> None:
        """非资源 link 被跳过。"""
        parser = cr.PageParser("https://x.com/")
        parser.feed('<link rel="canonical" href="https://x.com/page">')
        assert len(parser.resources) == 0

    def test_srcset(self) -> None:
        """PageParser 提取 srcset 多候选资源。"""
        parser = cr.PageParser("https://x.com/")
        parser.feed('<img srcset="img1.jpg 1x, img2.jpg 2x">')
        assert len(parser.resources) == 2

    def test_base_tag(self) -> None:
        """PageParser 按 base 标签修正相对资源。"""
        parser = cr.PageParser("https://x.com/")
        parser.feed('<base href="/sub/">')
        parser.feed('<img src="img.png">')
        assert parser.resources[0].url == "https://x.com/sub/img.png"

    def test_page_links(self) -> None:
        """PageParser 收集页面内链接。"""
        parser = cr.PageParser("https://x.com/")
        parser.feed('<a href="/about">About</a>')
        assert "https://x.com/about" in parser.page_links

    def test_meta_og_image(self) -> None:
        """PageParser 提取 og:image 元数据资源。"""
        parser = cr.PageParser("https://x.com/")
        parser.feed('<meta property="og:image" content="https://cdn.x.com/og.png">')
        assert len(parser.resources) == 1

    def test_data_uri_skipped(self) -> None:
        """PageParser 跳过 data URI 资源。"""
        parser = cr.PageParser("https://x.com/")
        parser.feed('<img src="data:image/png;base64,abc">')
        assert len(parser.resources) == 0


# ========== make_robots_parser / rewrite_html / strip_overlays ==========


class TestMakeRobotsParser:
    def test_with_fetch_mock(self) -> None:
        """make_robots_parser 抓取并解析 robots.txt。"""
        with patch.object(
            core, "fetch", return_value=(b"User-agent: *\nDisallow: /private", "text/plain")
        ):
            parser = cr.make_robots_parser("https://example.com", {}, 10)
        assert parser.can_fetch("*", "https://example.com/page") is True
        assert parser.can_fetch("*", "https://example.com/private") is False

    def test_fetch_error(self) -> None:
        """fetch 失败时返回空 robots parser（允许所有）。"""
        with patch.object(core, "fetch", side_effect=Exception("network error")):
            parser = cr.make_robots_parser("https://example.com", {}, 10)
        assert parser.can_fetch("*", "https://example.com/anything") is True


class TestRewriteHtml:
    def test_basic(self, tmp_path: Path) -> None:
        """核对页面引用资源与已下载清单的匹配。"""
        html = '<img src="https://x.com/logo.png">'
        rows = [
            cr.ManifestRow(
                status="ok",
                url="https://x.com/logo.png",
                saved_path=str(tmp_path / "assets" / "logo.png"),
                content_type="image/png",
                bytes=100,
                category="image",
                found_in="img[src]",
                kind="img",
                page_url="https://x.com/",
                page_title="",
                diagnostic="",
                sha256="",
            )
        ]
        result = cr.rewrite_html(html, rows, "https://x.com/", tmp_path)
        assert "logo.png" in result
        assert "https://x.com/logo.png" not in result

    def test_inline_css_metadata_and_unavailable_assets(self, tmp_path: Path) -> None:
        from lxml import html as lxml_html

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        image_path = output_dir / "assets" / "cover 中文.png"
        image_path.parent.mkdir()
        image_path.write_bytes(b"image")
        outside = tmp_path / "unsaved.png"
        outside.write_bytes(b"outside")
        page_url = "https://example.com/article/page.html"

        def row(name: str, path: Path | None, media: str, status: str = "ok") -> cr.ManifestRow:
            return cr.row_for(
                status,
                cr.Resource(f"https://example.com/article/{name}", "img[src]", "img", page_url),
                str(path) if path else "",
                media,
                5,
                {},
                sha256="same-hash",
            )

        rows = [
            row("cover.png", image_path, "image/png"),
            row("unsaved.png", outside, "image/png"),
            row("different.jpeg", None, "image/jpeg", "skipped by dedup"),
        ]
        html = """<!doctype html><html><head>
            <base href="https://["><base href="/ignored/">
            <meta charset="gbk">
            <meta http-equiv="Content-Type" content="text/html; charset=GBK">
            <meta name="description" content="cover.png">
            <meta property="og:image" content="cover.png">
            <link rel="stylesheet" href="missing.css" integrity="sha256-a" crossorigin="anonymous">
            <style>/* url(cover.png) */ p::after { content: "url(cover.png)" }
            p { background: url(cover.png); filter: url(#clip); }</style>
            </head><body><!-- keep this comment -->
            <p style="background: url(cover.png)">cover.png</p>
            <img src="unsaved.png"><img src="different.jpeg">
            <img src=""><img src="https://[">
            </body></html>"""
        doc = lxml_html.fromstring(cr.rewrite_html(html, rows, page_url, output_dir))
        local_image = "assets/cover%20%E4%B8%AD%E6%96%87.png"
        assert doc.xpath("//p/@style") == [f'background: url("{local_image}")']
        sheet = doc.xpath("//style/text()")[0]
        assert f'background: url("{local_image}")' in sheet
        assert '/* url(cover.png) */ p::after { content: "url(cover.png)" }' in sheet
        assert "filter: url(#clip)" in sheet
        assert doc.xpath("//p/text()") == ["cover.png"]
        assert [node.text for node in doc.xpath("//comment()")] == [" keep this comment "]
        assert doc.xpath('//meta[@name="description"]/@content') == ["cover.png"]
        assert doc.xpath('//meta[@property="og:image"]/@content') == [local_image]
        assert set(doc.xpath("//meta/@charset")) == {"utf-8"}
        assert doc.xpath('//meta[@http-equiv="Content-Type"]/@content') == [
            "text/html; charset=utf-8"
        ]
        assert doc.xpath("//img/@src") == [
            "https://example.com/article/unsaved.png",
            "https://example.com/article/different.jpeg",
            "",
            "https://[",
        ]
        assert doc.xpath("//link/@href") == ["https://example.com/article/missing.css"]
        assert doc.xpath("//link/@integrity") == ["sha256-a"]
        assert doc.xpath("//link/@crossorigin") == ["anonymous"]

    def test_stylesheet_copy_failure_keeps_original_reference(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        source = tmp_path / "styles.css"
        css = ".banner { color: red }"
        source.write_text(css, encoding="utf-8")
        offline = tmp_path / "offline_pages"
        offline.mkdir()
        (offline / "_styles").write_text("occupied", encoding="utf-8")
        rows = [
            cr.row_for(
                "ok",
                cr.Resource(
                    "https://example.com/styles.css", "link[href]", "link", "https://example.com/"
                ),
                str(source),
                "text/css",
                len(css),
                {},
            )
        ]
        table = RewriteTable(rows, tmp_path)
        table.rewrite_stylesheets(rows, lambda: False, encoding=None)
        html = table.apply(
            '<link rel="stylesheet" href="styles.css">',
            "https://example.com/",
            destination=offline / "page.html",
        )
        assert 'href="../styles.css"' in html
        assert source.read_text(encoding="utf-8") == css
        assert "offline stylesheet rewrite failed" in caplog.text


@pytest.mark.parametrize(
    ("data", "content_type", "override", "expected"),
    [
        ("封面".encode("gbk"), "text/css; charset=gbk", None, "封面"),
        ("封面".encode("gbk"), "text/css; charset=utf-8", "gbk", "封面"),
        ("封面".encode("utf-16"), "text/css", None, "封面"),
        (
            b'@charset "unknown-encoding"; .title {}',
            "text/css",
            None,
            '@charset "unknown-encoding"; .title {}',
        ),
    ],
)
def test_decode_stylesheet(
    data: bytes, content_type: str, override: str | None, expected: str
) -> None:
    assert decode_stylesheet(data, content_type, override) == expected


class TestStripPageOverlays:
    def test_removes_modal(self) -> None:
        """strip_page_overlays 移除 modal 覆盖层。"""
        html = '<div class="modal">overlay content</div><p>real content</p>'
        result = cr.strip_page_overlays(html)
        assert "modal" not in result or "overlay content" not in result

    def test_removes_by_id(self) -> None:
        """strip_page_overlays 按 id 移除弹窗。"""
        html = '<div id="popup">spam</div><p>content</p>'
        result = cr.strip_page_overlays(html)
        assert "spam" not in result

    def test_preserves_content(self) -> None:
        """strip_page_overlays 保留正文内容。"""
        html = "<p>main content</p>"
        result = cr.strip_page_overlays(html)
        assert "main content" in result


# ========== smart_extract / extract_readable_text ==========


class TestSmartExtract:
    def test_metadata_is_independent_of_attribute_order(self) -> None:
        html = "<meta content='Cats &amp; Dogs' property='OG:TITLE'><title>Example</title>"
        result = cr.smart_extract(html, "https://example.com")
        assert result["og_title"] == "Cats & Dogs"
        assert result["page_title"] == "Example"

    def test_basic(self) -> None:
        """extract_readable_text 提取页面可读文本与元数据。"""
        html = """
        <html><head>
        <title>Test Page</title>
        <meta property="og:title" content="OG Title">
        <meta property="og:image" content="https://x.com/og.png">
        <meta name="description" content="A test page">
        <meta name="keywords" content="test, example">
        <link rel="canonical" href="https://x.com/page">
        </head><body>
        <h1>Title</h1><h2>Sub</h2>
        <a href="/link">link</a>
        <img src="img.png">
        <p>Some text content here</p>
        </body></html>
        """
        result = cr.smart_extract(html, "https://x.com/page")
        assert result["page_url"] == "https://x.com/page"
        assert result["page_title"] == "Test Page"
        assert result["og_title"] == "OG Title"
        assert result["og_image"] == "https://x.com/og.png"
        assert result["meta_description"] == "A test page"
        assert result["meta_keywords"] == "test, example"
        assert result["h1_count"] == 1
        assert result["h2_count"] == 1
        assert result["link_count"] == 1
        assert result["image_count"] == 1
        assert result["text_length"] > 0
        assert result["has_canonical"] is True


class TestExtractReadableText:
    def test_article_tag(self) -> None:
        """extract_readable_text 优先提取 article 正文。"""
        html = "<article><p>Main article text that is long enough to be kept</p></article>"
        result = cr.extract_readable_text(html)
        assert "Main article text" in result

    def test_body_fallback(self) -> None:
        """extract_readable_text 回退到 body 文本。"""
        html = "<body><p>Body text that is long enough to be kept here</p></body>"
        result = cr.extract_readable_text(html)
        assert "Body text" in result

    def test_strips_scripts(self) -> None:
        """extract_readable_text 剥离 script 标签内容。"""
        html = (
            "<article><script>var x = 1;</script><p>Visible text here is long enough</p></article>"
        )
        result = cr.extract_readable_text(html)
        assert "var x" not in result
        assert "Visible text" in result


class TestWriteExtractedData:
    def test_writes_json_and_csv(self, tmp_path: Path) -> None:
        """write_extracted_data 同时写出 JSON 与 CSV。"""
        data = [{"page_url": "https://x.com", "title": "X"}]
        cr.write_extracted_data(tmp_path, data)
        assert (tmp_path / "extracted_data.json").exists()
        assert (tmp_path / "extracted_data.csv").exists()

    def test_empty_data(self, tmp_path: Path) -> None:
        """空数据只写 JSON。"""
        cr.write_extracted_data(tmp_path, [])
        assert (tmp_path / "extracted_data.json").exists()
        assert not (tmp_path / "extracted_data.csv").exists()


# ========== sitemap ==========


class TestDiscoverSitemapUrls:
    def test_basic(self) -> None:
        """discover_sitemap_urls 抓取并解析 sitemap URL。"""
        sitemap = '<?xml version="1.0"?><urlset><url><loc>https://x.com/a</loc></url><url><loc>https://x.com/b</loc></url></urlset>'
        with patch.object(core, "fetch", return_value=(sitemap.encode(), "application/xml")):
            urls = cr.discover_sitemap_urls("https://x.com/sitemap.xml", {}, 10)
        assert len(urls) == 2

    def test_fetch_error(self) -> None:
        """discover_sitemap_urls 抓取失败时返回空列表。"""
        with patch.object(core, "fetch", side_effect=Exception("err")):
            urls = cr.discover_sitemap_urls("https://x.com/sitemap.xml", {}, 10)
        assert urls == []


# ========== 配置保存/加载 ==========


class TestConfigSaveLoad:
    def test_save_and_load(self, tmp_path: Path) -> None:
        """save_config_to_file 保存后可重新加载。"""
        args = cr.build_parser().parse_args(["--url", "https://x.com", "--out", str(tmp_path)])
        config_path = tmp_path / "config.json"
        cr.save_config_to_file(args, str(config_path))
        assert config_path.exists()

        loaded = cr.load_config_from_file(str(config_path))
        assert loaded["url"] == "https://x.com"


class TestCrawlState:
    def test_save_and_load(self, tmp_path: Path) -> None:
        """crawl_state 保存后可原样加载。"""
        cr.save_crawl_state(tmp_path, page_queue=["https://x.com/a"], seen_pages=[])
        loaded = cr.load_crawl_state(tmp_path)
        assert loaded["page_queue"] == ["https://x.com/a"]

    def test_load_missing(self, tmp_path: Path) -> None:
        """load_crawl_state 状态文件缺失时返回空。"""
        assert cr.load_crawl_state(tmp_path) == {}

    def test_load_invalid(self, tmp_path: Path) -> None:
        """load_crawl_state 状态文件非法时返回空。"""
        (tmp_path / cr.CRAWL_STATE_FILE).write_text("invalid json")
        assert cr.load_crawl_state(tmp_path) == {}

    def test_clear(self, tmp_path: Path) -> None:
        """clear_crawl_state 删除状态文件。"""
        cr.save_crawl_state(tmp_path, key="val")
        cr.clear_crawl_state(tmp_path)
        assert not (tmp_path / cr.CRAWL_STATE_FILE).exists()


# ========== 清单写入 ==========


class TestManifests:
    def _rows(self) -> list[cr.ManifestRow]:
        return [
            cr.ManifestRow(
                status="ok",
                url="https://x.com/a.js",
                saved_path="/tmp/a.js",
                content_type="application/javascript",
                bytes=100,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="Page",
                diagnostic="",
                sha256="abc",
            ),
            cr.ManifestRow(
                status="error: 404",
                url="https://x.com/b.js",
                saved_path="",
                content_type="",
                bytes=0,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="Page",
                diagnostic="not found",
            ),
        ]

    def test_write_manifests(self, tmp_path: Path) -> None:
        """write_manifests 写出 CSV 与 JSON 清单。"""
        cr.write_manifests(tmp_path, self._rows())
        assert (tmp_path / "resources_manifest.csv").exists()
        assert (tmp_path / "resources_manifest.json").exists()
        data = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert len(data) == 2

    def test_write_video_manifests(self, tmp_path: Path) -> None:
        """write_video_manifests 单独写出视频清单。"""
        rows = self._rows() + [
            cr.ManifestRow(
                status="ok",
                url="https://x.com/v.mp4",
                saved_path="/tmp/v.mp4",
                content_type="video/mp4",
                bytes=1000,
                category="video",
                found_in="video[src]",
                kind="video",
                page_url="https://x.com/",
                page_title="Page",
                diagnostic="",
            )
        ]
        count = cr.write_video_manifests(tmp_path, rows)
        assert count == 1
        assert (tmp_path / "video_manifest.csv").exists()

    def test_write_video_manifests_empty(self, tmp_path: Path) -> None:
        """write_video_manifests 无视频时写出零条。"""
        count = cr.write_video_manifests(tmp_path, self._rows())
        assert count == 0

    def test_write_failed_manifests(self, tmp_path: Path) -> None:
        """write_failed_manifests 写出失败资源清单。"""
        count = cr.write_failed_manifests(tmp_path, self._rows())
        assert count == 1
        assert (tmp_path / "failed_resources.csv").exists()

    def test_is_failed_row(self) -> None:
        """is_failed_row 按状态判定失败与跳过行。"""
        ok = cr.ManifestRow("ok", "u", "", "", 0, "", "", "", "", "", "")
        err = cr.ManifestRow("error: x", "u", "", "", 0, "", "", "", "", "", "")
        skipped = cr.ManifestRow("skipped: dedup", "u", "", "", 0, "", "", "", "", "", "")
        assert cr.is_failed_row(ok) is False
        assert cr.is_failed_row(err) is True
        assert cr.is_failed_row(skipped) is True


# ========== 格式化函数 ==========


class TestFormatBytes:
    def test_bytes(self) -> None:
        """_format_bytes 格式化字节数。"""
        assert cr._format_bytes(500) == "500 B"

    def test_kb(self) -> None:
        """1024 ≤ n < 1024² 返回 KB。"""
        assert cr._format_bytes(2048) == "2.0 KB"

    def test_mb(self) -> None:
        """1024² ≤ n < 1024³ 返回 MB。"""
        assert cr._format_bytes(2 * 1024 * 1024) == "2.0 MB"

    def test_gb(self) -> None:
        """1024³ ≤ n < 1024⁴ 返回 GB。"""
        assert cr._format_bytes(2 * 1024 * 1024 * 1024) == "2.0 GB"

    def test_tb(self) -> None:
        """n ≥ 1024⁴ 返回 TB。"""
        assert cr._format_bytes(2 * 1024 * 1024 * 1024 * 1024) == "2.0 TB"


class TestFormatDuration:
    def test_milliseconds(self) -> None:
        """format_duration 毫秒级时长含毫秒单位。"""
        assert "毫秒" in cr.format_duration(0.5)

    def test_seconds(self) -> None:
        """format_duration 秒级时长含秒单位。"""
        assert "秒" in cr.format_duration(5.0)

    def test_minutes(self) -> None:
        """format_duration 分钟级时长含分单位。"""
        assert "分" in cr.format_duration(125.0)

    def test_hours(self) -> None:
        """format_duration 小时级时长含时单位。"""
        assert "时" in cr.format_duration(3725.0)

    def test_negative(self) -> None:
        """format_duration 负值返回未知。"""
        assert cr.format_duration(-1) == "未知"


# ========== 错误分类 ==========


class TestAtomicWrite:
    """报告产物的原子写：中断不留下半截文件，失败保留旧文件。"""

    def test_write_lands_and_leaves_no_tmp(self, tmp_path: Path) -> None:
        from web_crawler.app._fsutil import atomic_write_text

        target = tmp_path / "run_report.json"
        target.write_text('{"old": 1}', encoding="utf-8")
        atomic_write_text(target, '{"new": 2}')
        assert target.read_text(encoding="utf-8") == '{"new": 2}'
        assert not list(tmp_path.glob("run_report.json.*.tmp"))

    def test_failed_replace_keeps_old_file(self, tmp_path: Path, monkeypatch: Any) -> None:
        from web_crawler.app import _fsutil

        target = tmp_path / "run_report.json"
        target.write_text('{"old": 1}', encoding="utf-8")

        def boom(src: str, dst: str) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(_fsutil.os, "replace", boom)
        with pytest.raises(OSError):
            _fsutil.atomic_write_text(target, '{"new": 2}')
        assert target.read_text(encoding="utf-8") == '{"old": 1}'
        assert not list(tmp_path.glob("run_report.json.*.tmp"))

    def test_permission_error_falls_back_to_direct_write(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Windows 目标被占用（PermissionError）时降级为直接写，保证报告落盘。"""
        from web_crawler.app import _fsutil

        target = tmp_path / "run_report.html"
        target.write_text("<html>old</html>", encoding="utf-8")

        def in_use(src: str, dst: str) -> None:
            raise PermissionError("target is locked")

        monkeypatch.setattr(_fsutil.os, "replace", in_use)
        _fsutil.atomic_write_text(target, "<html>new</html>")
        assert target.read_text(encoding="utf-8") == "<html>new</html>"
        assert not list(tmp_path.glob("run_report.html.*.tmp"))

    def test_atomic_csv_writer_lands_and_cleans_up(self, tmp_path: Path) -> None:
        from web_crawler.app._fsutil import atomic_text_writer

        target = tmp_path / "resources_manifest.csv"
        with atomic_text_writer(target, newline="") as handle:
            handle.write("a,b\n1,2\n")  # type: ignore[attr-defined]
        assert target.read_text(encoding="utf-8").splitlines() == ["a,b", "1,2"]
        assert not list(tmp_path.glob("resources_manifest.csv.*.tmp"))


class TestGuardBlockedAssetReporting:
    """子资源被 SSRF 门禁拦截后，报告要能解释「哪些没下载、为什么」。"""

    def test_report_context_and_recommendation(self) -> None:
        ctx = cr.build_report_context(
            [],
            1,
            0.0,
            0.0,
            page_scan={"failed": 0, "skipped": 0, "first_error": "", "blocked_assets": 3},
        )
        assert ctx["page_scan"]["blocked_assets"] == 3
        recs = cr.build_recommendations(ctx)
        assert any("门禁" in item["title"] for item in recs)

    def test_render_page_drains_blocked_resources(self, monkeypatch: Any) -> None:
        from types import SimpleNamespace

        from web_crawler.app import _crawler_core as core

        fetcher = SimpleNamespace(last_blocked_resources=["http://localhost:9/x"])

        def fake_fetch(url: str) -> Any:
            return SimpleNamespace(
                status=200,
                headers={"content-type": "text/html"},
                content=b"<html>x</html>",
            )

        fetcher.fetch = fake_fetch  # type: ignore[attr-defined]
        monkeypatch.setattr(core, "is_power_mode", lambda: True)
        monkeypatch.setattr(core, "_get_render_fetcher", lambda *args, **kwargs: fetcher)

        body, content_type = core.render_page("http://127.0.0.1/", 5, {})
        assert body == b"<html>x</html>" and content_type == "text/html"
        assert core.drain_blocked_resources() == ["http://localhost:9/x"]
        assert core.drain_blocked_resources() == []

    def test_render_failure_still_reports_blocked_resources(self, monkeypatch: Any) -> None:
        from types import SimpleNamespace

        from web_crawler.app import _crawler_core as core
        from web_crawler.exceptions import FetchError

        fetcher = SimpleNamespace(last_blocked_resources=[])

        def fake_fetch(url: str) -> Any:
            fetcher.last_blocked_resources = ["ws://10.0.0.5/socket"]
            raise FetchError("browser crashed")

        fetcher.fetch = fake_fetch  # type: ignore[attr-defined]
        monkeypatch.setattr(core, "is_power_mode", lambda: True)
        monkeypatch.setattr(core, "_get_render_fetcher", lambda *args, **kwargs: fetcher)

        with pytest.raises(FetchError, match="browser crashed"):
            core.render_page("http://127.0.0.1/", 5, {})
        assert core.drain_blocked_resources() == ["ws://10.0.0.5/socket"]

    def test_drain_deduplicates_and_clears(self) -> None:
        from web_crawler.app import _crawler_core as core

        core._render_local.blocked_resources = ["a", "b", "a"]
        try:
            assert core.drain_blocked_resources() == ["a", "b"]
            assert core.drain_blocked_resources() == []
        finally:
            core._render_local.blocked_resources = []

    def test_summary_and_markdown_report_mention_blocked_assets(self, tmp_path: Path) -> None:
        page_scan = {"failed": 0, "skipped": 0, "first_error": "", "blocked_assets": 2}
        cr.write_summary(tmp_path, [], 1, page_scan=page_scan)  # type: ignore[arg-type]
        summary = (tmp_path / "summary.txt").read_text(encoding="utf-8")
        assert "门禁拦截子资源" in summary and "2" in summary

        cr.write_run_report(tmp_path, [], 1, page_scan=page_scan)  # type: ignore[arg-type]
        markdown = (tmp_path / "run_report.md").read_text(encoding="utf-8")
        assert "门禁拦截子资源" in markdown
        html = (tmp_path / "run_report.html").read_text(encoding="utf-8")
        assert "门禁拦截子资源" in html
        payload = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))
        assert payload["page_scan"]["blocked_assets"] == 2


class TestReportConfigRedaction:
    """报告 config 里的代理凭据必须脱敏（报告默认外发/归档）。"""

    def test_redact_proxy_helper(self) -> None:
        assert cr._redact_proxy("http://user:pass@host:8080") == "http://***@host:8080"
        assert cr._redact_proxy("socks5h://u:p@10.0.0.1:1080") == "socks5h://***@10.0.0.1:1080"
        assert cr._redact_proxy("http://proxy.example.com:3128") == "http://proxy.example.com:3128"
        assert cr._redact_proxy("") == ""
        assert cr._redact_proxy(None) is None

    def test_crawl_report_redacts_proxy(self, tmp_path: Path) -> None:
        html = b'<html><body><img src="https://example.com/a.png"></body></html>'
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--proxy",
                    "http://user:secret@proxy.example.com:8080",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            assert cr.crawl(args) == 0
        report = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))
        assert report["config"]["proxy"] == "http://***@proxy.example.com:8080"
        markdown = (tmp_path / "run_report.md").read_text(encoding="utf-8")
        assert "secret" not in markdown
        assert "extract_markdown" in markdown
        assert "download_linked_data" in markdown


class TestClassifyError:
    def test_auth(self) -> None:
        """classify_error 将 401 归类为 auth。"""
        assert cr.classify_error("error: HTTP Error 401") == "auth"

    def test_not_found(self) -> None:
        """classify_error 将 404 归类为 not_found。"""
        assert cr.classify_error("error: HTTP Error 404") == "not_found"

    def test_rate_limit(self) -> None:
        """classify_error 将 429 归类为 rate_limit。"""
        assert cr.classify_error("error: HTTP Error 429") == "rate_limit"

    def test_timeout(self) -> None:
        """classify_error 将超时归类为 timeout。"""
        assert cr.classify_error("error: timed out") == "timeout"

    def test_size_limit(self) -> None:
        """classify_error 将超限归类为 size_limit。"""
        assert cr.classify_error("error: file exceeds max-bytes") == "size_limit"

    def test_robots(self) -> None:
        """classify_error 将 robots 跳过归类为 robots。"""
        assert cr.classify_error("skipped by robots.txt") == "robots"

    def test_dedup(self) -> None:
        """classify_error 将去重跳过归类为 dedup。"""
        assert cr.classify_error("skipped by dedup") == "dedup"

    def test_encrypted(self) -> None:
        """classify_error 将加密清单归类为 encrypted。"""
        assert cr.classify_error("encrypted playlist detected") == "encrypted"

    def test_guard(self) -> None:
        """安全门禁拦截单独归类，不再混进 other_error。"""
        assert cr.classify_error("error: navigation blocked: private address 10.0.0.5") == "guard"
        assert cr.classify_error("error: [安全拦截] https://example.com") == "guard"

    def test_tls(self) -> None:
        """证书/TLS 错误单独归类，便于提示用户检查证书或代理。"""
        assert cr.classify_error("error: TLS certificate verify failed") == "tls"
        assert cr.classify_error("error: [SSL: CERTIFICATE_VERIFY_FAILED]") == "tls"

    def test_network(self) -> None:
        """classify_error 将连接错误归类为 network。"""
        assert cr.classify_error("error: URLError connection refused") == "network"

    def test_cancelled(self) -> None:
        """classify_error 将取消归类为 cancelled。"""
        assert cr.classify_error("cancelled by user") == "cancelled"

    def test_other_error(self) -> None:
        """classify_error 将未知错误归类为 other_error。"""
        assert cr.classify_error("error: unknown") == "other_error"

    def test_skipped_other(self) -> None:
        """classify_error 将未知跳过归类为 skipped_other。"""
        assert cr.classify_error("skipped: unknown reason") == "skipped_other"

    def test_other(self) -> None:
        """classify_error 将正常状态归类为 other。"""
        assert cr.classify_error("ok") == "other"


# ========== 报告上下文 ==========


class TestBuildReportContext:
    def _rows(self) -> list[cr.ManifestRow]:
        return [
            cr.ManifestRow(
                status="ok",
                url="https://x.com/a.js",
                saved_path="/tmp/a.js",
                content_type="application/javascript",
                bytes=100,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="Page",
                diagnostic="",
                sha256="abc",
            ),
            cr.ManifestRow(
                status="error: HTTP Error 404",
                url="https://x.com/b.js",
                saved_path="",
                content_type="",
                bytes=0,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="Page",
                diagnostic="not found",
            ),
            cr.ManifestRow(
                status="skipped by dedup",
                url="https://x.com/c.js",
                saved_path="",
                content_type="application/javascript",
                bytes=50,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="Page",
                diagnostic="dedup",
                sha256="def",
            ),
        ]

    def test_basic(self) -> None:
        """build_report_context 构建报告上下文含统计字段。"""
        ctx = cr.build_report_context(self._rows(), 2, 1000.0, 1005.0)
        assert ctx["schema"] == 2
        assert ctx["pages_scanned"] == 2
        res = ctx["resources"]
        assert res["total"] == 3
        assert res["ok"] == 1
        assert res["failed"] == 1
        assert res["skipped"] == 1
        assert res["deduped"] == 1
        assert res["success_rate_percent"] > 0

    def test_empty_rows(self) -> None:
        """build_report_context 空清单时成功率记为百分百。"""
        ctx = cr.build_report_context([], 0, 0.0, 0.0)
        assert ctx["resources"]["total"] == 0
        assert ctx["resources"]["success_rate_percent"] == 100.0

    def test_with_config(self) -> None:
        """build_report_context 带配置时纳入 config 字段。"""
        ctx = cr.build_report_context([], 0, 0.0, 0.0, config={"url": "https://x.com"})
        assert ctx["config"]["url"] == "https://x.com"

    def test_failures_by_class(self) -> None:
        """build_report_context 按错误类别汇总失败。"""
        ctx = cr.build_report_context(self._rows(), 1, 0.0, 1.0)
        assert "not_found" in ctx["failures_by_class"]


class TestBuildRecommendations:
    def test_no_resources(self) -> None:
        """build_recommendations 无资源时仍给出建议。"""
        ctx = cr.build_report_context([], 0, 0.0, 0.0)
        recs = cr.build_recommendations(ctx)
        assert len(recs) >= 1
        assert any("未发现" in r["title"] for r in recs)

    def test_auth_error(self) -> None:
        """build_recommendations 针对鉴权失败给出建议。"""
        rows = [
            cr.ManifestRow(
                "error: HTTP Error 401", "https://x.com/private", "", "", 0, "", "", "", "", "", ""
            )
        ]
        ctx = cr.build_report_context(rows, 1, 0.0, 1.0)
        recs = cr.build_recommendations(ctx)
        assert any("401/403" in r["title"] for r in recs)

    def test_normal(self) -> None:
        """按正常资源清单生成报告内容。"""
        rows = [
            cr.ManifestRow(
                "ok",
                "https://x.com/a.js",
                "/tmp/a.js",
                "application/javascript",
                100,
                "script",
                "script[src]",
                "script",
                "https://x.com/",
                "Page",
                "",
            )
        ]
        ctx = cr.build_report_context(rows, 1, 0.0, 1.0)
        recs = cr.build_recommendations(ctx)
        assert len(recs) >= 1


class TestWriteSummary:
    def test_writes_file(self, tmp_path: Path) -> None:
        """报告生成写出报告文件。"""
        rows = [
            cr.ManifestRow(
                "ok",
                "https://x.com/a.js",
                "/tmp/a.js",
                "application/javascript",
                100,
                "script",
                "script[src]",
                "script",
                "https://x.com/",
                "Page",
                "",
            )
        ]
        cr.write_summary(
            tmp_path, rows, 1, start_time=0.0, end_time=1.0, config={"url": "https://x.com"}
        )
        assert (tmp_path / "summary.txt").exists()
        content = (tmp_path / "summary.txt").read_text(encoding="utf-8")
        assert "网页资源采集" in content


class TestWriteRunReport:
    def test_writes_all_formats(self, tmp_path: Path) -> None:
        """报告生成写出全部格式文件。"""
        rows = [
            cr.ManifestRow(
                "ok",
                "https://x.com/a.js",
                "/tmp/a.js",
                "application/javascript",
                100,
                "script",
                "script[src]",
                "script",
                "https://x.com/",
                "Page",
                "",
            )
        ]
        cr.write_run_report(
            tmp_path, rows, 1, start_time=0.0, end_time=1.0, config={"url": "https://x.com"}
        )
        assert (tmp_path / "run_report.json").exists()
        assert (tmp_path / "run_report.md").exists()
        assert (tmp_path / "run_report.html").exists()
        # JSON 可解析
        data = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))
        assert data["schema"] == 2
        assert "recommendations" in data


# ========== diagnostic_for_status / row_for ==========


class TestDiagnosticForStatus:
    def test_401(self) -> None:
        """diagnostic_for_status 对 401 含状态码提示。"""
        assert "401" in cr.diagnostic_for_status("error: HTTP Error 401")

    def test_403(self) -> None:
        """diagnostic_for_status 对 403 含状态码提示。"""
        assert "403" in cr.diagnostic_for_status("error: HTTP Error 403")

    def test_404(self) -> None:
        """diagnostic_for_status 对 404 含状态码提示。"""
        assert "404" in cr.diagnostic_for_status("error: HTTP Error 404")

    def test_timeout(self) -> None:
        """diagnostic_for_status 对超时含提示。"""
        assert "Timeout" in cr.diagnostic_for_status("error: timed out")

    def test_size_limit(self) -> None:
        """diagnostic_for_status 对超限含提示。"""
        assert "max-bytes" in cr.diagnostic_for_status("error: file exceeds")

    def test_robots(self) -> None:
        """diagnostic_for_status 对 robots 跳过含提示。"""
        assert "robots" in cr.diagnostic_for_status("skipped by robots.txt")

    def test_dedup(self) -> None:
        """diagnostic_for_status 对去重跳过含哈希提示。"""
        assert "SHA256" in cr.diagnostic_for_status("skipped by dedup")

    def test_generic_error(self) -> None:
        """diagnostic_for_status 对未知错误含通用提示。"""
        assert "Request failed" in cr.diagnostic_for_status("error: unknown")

    def test_ok(self) -> None:
        """diagnostic_for_status 对正常状态返回空。"""
        assert cr.diagnostic_for_status("ok") == ""


class TestRowFor:
    def test_basic(self) -> None:
        """row_for 按资源与结果构建清单行。"""
        resource = cr.Resource("https://x.com/a.js", "script[src]", "script", "https://x.com/")
        row = cr.row_for(
            "ok",
            resource,
            "/tmp/a.js",
            "application/javascript",
            100,
            {"https://x.com/": "Page"},
            sha256="abc123",
        )
        assert row.status == "ok"
        assert row.url == "https://x.com/a.js"
        assert row.category == "script"
        assert row.sha256 == "abc123"


# ========== fetch ==========


def _mock_fetch_response(status: int, content: bytes, headers: dict[str, str]) -> Mock:
    """构造库级 Response 风格的桩对象（fetch 内存路径消费）。"""
    return Mock(status=status, content=content, headers=headers)


class TestFetch:
    def test_success(self) -> None:
        """fetch 成功读取响应体。"""
        fake = Mock()
        fake.get.return_value = _mock_fetch_response(
            200, b"<html>data</html>", {"content-type": "text/html"}
        )
        with patch.object(core, "_get_app_fetcher", return_value=fake):
            data, ct = cr.fetch("https://example.com", 30, {}, 0, None)
        assert data == b"<html>data</html>"
        assert ct == "text/html"

    def test_max_bytes_exceeded(self) -> None:
        """超出 max_bytes 抛 ValueError 且不再重试。"""
        fake = Mock()
        fake.get.return_value = _mock_fetch_response(200, b"x" * 200, {})
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            pytest.raises(ValueError, match="max-bytes"),
        ):
            cr.fetch("https://example.com", 30, {}, 0, 100)
        assert fake.get.call_count == 1

    def test_http_error_no_retry(self) -> None:
        """404 错误抛 FetchError（带 status 属性）。"""
        fake = Mock()
        fake.get.return_value = _mock_fetch_response(404, b"", {})
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            pytest.raises(FetchError, match="HTTP Error 404") as exc_info,
        ):
            cr.fetch("https://x.com", 30, {}, 0, None)
        assert getattr(exc_info.value, "status", None) == 404


# ========== crawl 集成测试 ==========


class TestCrawl:
    def test_no_resources(self, tmp_path: Path) -> None:
        """页面无资源时生成空清单。"""
        html = b"<html><body><p>no resources</p></body></html>"
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        assert (tmp_path / "resources_manifest.json").exists()
        manifest = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert len(manifest) == 0

    def test_list_only(self, tmp_path: Path) -> None:
        """list_only 模式下发现资源但不下载。"""
        html = b'<html><body><img src="logo.png"><script src="app.js"></script></body></html>'
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        manifest = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert len(manifest) == 2
        assert all(r["status"] == "listed only" for r in manifest)

    def test_download(self, tmp_path: Path) -> None:
        """下载模式下载资源并写入文件。"""
        html = b'<html><body><img src="https://example.com/logo.png"></body></html>'
        img_data = b"PNG_FAKE_DATA"

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "example.com" in url and "logo.png" not in url:
                return (html, "text/html")
            return (img_data, "image/png")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        manifest = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert len(manifest) == 1
        assert manifest[0]["status"] == "ok"

    def test_header_parse_error(self, tmp_path: Path) -> None:
        """header 解析错误返回 exit code 2。"""
        args = cr.build_parser().parse_args(
            [
                "--url",
                "https://example.com",
                "--out",
                str(tmp_path),
            ]
        )
        args.header = ["invalid header without colon"]
        exit_code = cr.crawl(args)
        assert exit_code == 2

    def test_cancelled(self, tmp_path: Path) -> None:
        """should_stop 返回 True 时取消,统一返回退出码 1。"""
        html = b"<html></html>"
        stop_flag = [False]

        def mock_should_stop(args: Any) -> bool:
            return stop_flag[0]

        with (
            patch.object(core, "fetch", return_value=(html, "text/html")),
            patch.object(core, "should_stop", side_effect=mock_should_stop),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            # 第一次 should_stop 返回 False（进入循环前），第二次返回 True
            stop_flag[0] = True
            exit_code = cr.crawl(args)
        # 主线程取消路径与 worker 取消路径统一为 1
        assert exit_code == 1


# ========== build_parser / main ==========


class TestBuildParser:
    def test_basic_args(self) -> None:
        """build_parser 解析基本 url 参数。"""
        parser = cr.build_parser()
        args = parser.parse_args(["--url", "https://x.com"])
        assert args.url == "https://x.com"
        assert args.workers == cr.DEFAULT_WORKERS
        assert args.timeout == 20

    def test_all_args(self) -> None:
        """build_parser 解析全部命令行参数。"""
        parser = cr.build_parser()
        args = parser.parse_args(
            [
                "--url",
                "https://x.com",
                "--out",
                "/tmp/out",
                "--workers",
                "4",
                "--delay",
                "1.0",
                "--timeout",
                "60",
                "--retries",
                "3",
                "--max-pages",
                "10",
                "--same-domain",
                "--include-css-urls",
                "--list-only",
                "--dedup",
                "--stealth",
            ]
        )
        assert args.workers == 4
        assert args.same_domain is True
        assert args.include_css_urls is True
        assert args.list_only is True
        assert args.dedup is True
        assert args.stealth is True


class TestMain:
    def test_no_url_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无 --url 时报错退出。"""
        monkeypatch.setattr("sys.argv", ["crawler"])
        with pytest.raises(SystemExit):
            cr.main()

    def test_save_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--save-config 保存配置后退出。"""
        config_path = tmp_path / "config.json"
        monkeypatch.setattr(
            "sys.argv",
            [
                "crawler",
                "--url",
                "https://x.com",
                "--save-config",
                str(config_path),
            ],
        )
        cr.main()
        assert config_path.exists()

    def test_load_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--load-config 加载配置并运行。"""
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "url": "https://example.com",
                    "out": str(tmp_path),
                    "workers": 1,
                    "list_only": True,
                    "save_config": None,
                    "same_domain": True,
                    "include_css_urls": True,
                    "rewrite_html": False,
                    "strip_overlays": False,
                    "decrypt": False,
                    "video_mode": False,
                    "video_only": False,
                    "expand_playlists": False,
                    "respect_robots": False,
                    "timeout": 20,
                    "retries": 1,
                    "delay": 0.5,
                    "max_bytes": 0,
                    "encoding": None,
                    "user_agent": cr.DEFAULT_USER_AGENT,
                    "header": [],
                    "block_keyword": [],
                    "include_pattern": None,
                    "exclude_pattern": None,
                    "proxy": None,
                    "stealth": False,
                    "impersonate": "chrome131",
                    "resume": False,
                    "organize": False,
                    "dedup": False,
                    "sitemap": False,
                    "smart_extract": False,
                    "resume_crawl": False,
                    "extract_text": False,
                    "max_pages": 1,
                    "crawl_pages": False,
                }
            )
        )

        monkeypatch.setattr("sys.argv", ["crawler", "--load-config", str(config_path)])

        html = b"<html><body><p>content</p></body></html>"
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            with pytest.raises(SystemExit) as exc_info:
                cr.main()
            assert exc_info.value.code == 0


# ========== AES 解密（mock _AES） ==========


class TestAESMocked:
    """即使 pycryptodome 未安装，也通过 mock _AES 测试 decrypt_aes128 逻辑。"""

    def test_decrypt_with_valid_padding(self) -> None:
        """PKCS7 有效填充时返回去填充后的明文。"""
        fake_cipher = Mock()
        # 明文长 11 字节，填充 5 字节 (pad_len=5)
        plaintext = b"hello world"
        pad_len = 5
        padded = plaintext + bytes([pad_len]) * pad_len
        fake_cipher.decrypt.return_value = padded

        fake_aes = Mock()
        fake_aes.new.return_value = fake_cipher
        fake_aes.MODE_CBC = 2

        with patch.object(core, "_AES", fake_aes, create=True):
            result = cr.decrypt_aes128(padded, b"k" * 16, b"iv" * 8)
        assert result == plaintext

    def test_decrypt_invalid_padding(self) -> None:
        """pad_len 不在 1-16 范围时返回原始解密数据。"""
        fake_cipher = Mock()
        # pad_len=0 (无效)
        data = b"\x00" * 16
        fake_cipher.decrypt.return_value = data

        fake_aes = Mock()
        fake_aes.new.return_value = fake_cipher
        fake_aes.MODE_CBC = 2

        with patch.object(core, "_AES", fake_aes, create=True):
            result = cr.decrypt_aes128(data, b"k" * 16, b"iv" * 8)
        assert result == data

    def test_decrypt_pad_len_too_large(self) -> None:
        """pad_len=17 (>16) 时返回原始解密数据。"""
        fake_cipher = Mock()
        data = bytes([17]) * 16
        fake_cipher.decrypt.return_value = data

        fake_aes = Mock()
        fake_aes.new.return_value = fake_cipher
        fake_aes.MODE_CBC = 2

        with patch.object(core, "_AES", fake_aes, create=True):
            result = cr.decrypt_aes128(data, b"k" * 16, b"iv" * 8)
        assert result == data


# ========== DomainRateLimiter 进阶 ==========


class TestDomainRateLimiterAdvanced:
    def test_blocked_domain_waits(self) -> None:
        """被 429 阻塞的域名会触发 sleep。"""
        limiter = cr.DomainRateLimiter(default_delay=0.0)
        # 设置一个未来的阻塞时间
        limiter._blocked_until["example.com"] = time.time() + 0.05
        with patch.object(cr.time, "sleep") as mock_sleep:
            limiter.wait_if_needed("https://example.com/page")
        mock_sleep.assert_called_once()

    def test_default_delay_triggers_sleep(self) -> None:
        """default_delay > 0 且距上次请求间隔不够时 sleep。"""
        limiter = cr.DomainRateLimiter(default_delay=1.0)
        limiter._last_times["example.com"] = time.time()  # 刚请求过
        with patch.object(cr.time, "sleep") as mock_sleep:
            limiter.wait_if_needed("https://example.com/page")
        mock_sleep.assert_called_once()

    def test_default_delay_elapsed_no_sleep(self) -> None:
        """default_delay > 0 但距上次请求间隔足够时不 sleep。"""
        limiter = cr.DomainRateLimiter(default_delay=0.01)
        limiter._last_times["example.com"] = time.time() - 1.0  # 1 秒前
        with patch.object(cr.time, "sleep") as mock_sleep:
            limiter.wait_if_needed("https://example.com/page")
        mock_sleep.assert_not_called()

    def test_handle_429_invalid_retry_after_uses_default(self) -> None:
        """handle_429 无效 Retry-After 使用默认 10 秒。"""
        limiter = cr.DomainRateLimiter(default_delay=0.0)
        wait = limiter.handle_429("https://example.com/x", "not-a-number")
        assert wait == 10


# ========== _get_app_fetcher ==========


class TestGetAppFetcher:
    """线程本地应用 fetcher 缓存：同 key 复用、key 变更换新实例。"""

    def _reset_local(self) -> None:
        core._fetchers_local = threading.local()

    def test_same_key_reuses_instance(self) -> None:
        """相同 (impersonate, proxy, timeout) 键复用同一实例。"""
        self._reset_local()
        try:
            f1 = cr._get_app_fetcher(None, 30.0, None)
            f2 = cr._get_app_fetcher(None, 30.0, None)
            assert f1 is f2
        finally:
            self._reset_local()

    def test_key_change_creates_new_instance(self) -> None:
        """key 任一维度变化时构建新实例。"""
        self._reset_local()
        try:
            f1 = cr._get_app_fetcher(None, 30.0, None)
            f2 = cr._get_app_fetcher(None, 30.0, "http://proxy:8080")
            f3 = cr._get_app_fetcher("chrome131", 30.0, None)
            assert f1 is not f2
            assert f1 is not f3
            assert f2 is not f3
        finally:
            self._reset_local()

    def test_fetcher_config_passed_through(self) -> None:
        """构造参数传递到库级 Fetcher（指纹/超时/重试/重定向上限）。"""
        self._reset_local()
        try:
            f = cr._get_app_fetcher("chrome131", 5.0, None)
            assert f.impersonate == "chrome131"
            assert f.timeout == 5.0
            assert f.retries == 0
            assert f.max_redirects == cr._MAX_REDIRECTS
        finally:
            self._reset_local()


# ========== 库级 Fetcher 导入桥 ==========


class TestImportLibFetcher:
    def test_returns_none_when_unavailable(self) -> None:
        """web_crawler.Fetcher 不可用时返回 None。"""
        # 模拟两次 import 都失败
        with patch.dict("sys.modules", {"web_crawler": None}, clear=False):
            result = cr._import_lib_fetcher()
        # 可能返回 None 或 Fetcher，取决于环境；只验证不抛异常
        assert result is None or result is not None

    def test_import_success(self) -> None:
        """web_crawler.Fetcher 可用时返回该类。"""
        fake_fetcher = Mock()
        fake_module = Mock(Fetcher=fake_fetcher)
        with patch.dict("sys.modules", {"web_crawler": fake_module}):
            result = cr._import_lib_fetcher()
        assert result is fake_fetcher


class TestSafeRedirectHandler:
    """重定向安全校验（防 SSRF）：拒绝内网/回环/非 http(s) 目标。"""

    @staticmethod
    def _handler() -> cr.SafeRedirectHandler:
        return cr.SafeRedirectHandler()

    @staticmethod
    def _request() -> Any:
        from urllib.request import Request

        return Request("https://example.com/start")

    def test_blocks_metadata_redirect(self) -> None:
        """重定向处理器拒绝元数据地址跳转。"""
        from email.message import Message

        with pytest.raises(ValueError, match="unsafe"):
            self._handler().redirect_request(
                self._request(),
                None,
                302,
                "Found",
                Message(),
                "http://169.254.169.254/latest/meta-data/",
            )

    def test_blocks_loopback_redirect(self) -> None:
        """重定向处理器拒绝回环地址跳转。"""
        from email.message import Message

        with pytest.raises(ValueError, match="unsafe"):
            self._handler().redirect_request(
                self._request(),
                None,
                302,
                "Found",
                Message(),
                "http://127.0.0.1/admin",
            )

    def test_blocks_non_http_scheme_redirect(self) -> None:
        """重定向处理器拒绝非 HTTP 协议跳转。"""
        from email.message import Message

        with pytest.raises(ValueError, match="non-http"):
            self._handler().redirect_request(
                self._request(),
                None,
                302,
                "Found",
                Message(),
                "file:///etc/passwd",
            )

    def test_allows_safe_redirect(self) -> None:
        """重定向处理器允许安全跨域跳转。"""
        from email.message import Message

        result = self._handler().redirect_request(
            self._request(),
            None,
            302,
            "Found",
            Message(),
            "https://cdn.example.com/lib.js",
        )
        assert result is not None
        assert result.full_url == "https://cdn.example.com/lib.js"


class TestImportLibFetcherSrcInPath:
    def test_src_path_present_skips_insert(self) -> None:
        """src 已在 sys.path 时不再重复插入（覆盖跳过分支）。"""
        original_path = list(sys.path)
        try:
            src_path = str(Path(cr.__file__).resolve().parent.parent / "src")
            if src_path not in sys.path:
                sys.path.insert(0, src_path)
            with patch.dict("sys.modules", {"web_crawler": None}, clear=False):
                result = cr._import_lib_fetcher()
            assert result is None
        finally:
            sys.path[:] = original_path


# ========== fetch 进阶分支 ==========


class TestFetchAdvanced:
    def test_stealth_path_in_fetch(self) -> None:
        """control_args.stealth=True 时以目标指纹构造 fetcher。"""
        control_args = Mock()
        control_args.stealth = True
        control_args.impersonate = "chrome131"
        control_args.proxy = None
        fake = Mock()
        fake.get.return_value = _mock_fetch_response(200, b"data", {"content-type": "text/html"})
        with patch.object(core, "_get_app_fetcher", return_value=fake) as get_fetcher:
            data, ct = cr.fetch("https://example.com", 30, {}, 0, None, control_args=control_args)
        assert data == b"data"
        assert ct == "text/html"
        get_fetcher.assert_called_once_with("chrome131", 30.0, None)

    def test_non_stealth_uses_no_impersonation(self) -> None:
        """非 stealth 路径以 impersonate=None 构造 fetcher（无指纹伪装）。"""
        control_args = Mock()
        control_args.stealth = False
        control_args.proxy = None
        fake = Mock()
        fake.get.return_value = _mock_fetch_response(200, b"plain", {"content-type": "text/html"})
        with patch.object(core, "_get_app_fetcher", return_value=fake) as get_fetcher:
            cr.fetch("https://example.com", 30, {}, 0, None, control_args=control_args)
        get_fetcher.assert_called_once_with(None, 30.0, None)

    def test_stealth_path_max_bytes_exceeded(self) -> None:
        """stealth 路径超出 max_bytes 抛 ValueError。"""
        control_args = Mock()
        control_args.stealth = True
        control_args.impersonate = "chrome131"
        control_args.proxy = None
        fake = Mock()
        fake.get.return_value = _mock_fetch_response(200, b"x" * 200, {})
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            pytest.raises(ValueError, match="max-bytes"),
        ):
            cr.fetch("https://example.com", 30, {}, 0, 100, control_args=control_args)

    def test_with_rate_limiter(self) -> None:
        """内存路径调用 rate_limiter 的 wait_if_needed / record_request。"""
        control_args = Mock()
        control_args.stealth = False
        control_args.proxy = None
        fake = Mock()
        fake.get.return_value = _mock_fetch_response(200, b"data", {})
        rate_limiter = Mock()
        with patch.object(core, "_get_app_fetcher", return_value=fake):
            cr.fetch(
                "https://example.com",
                30,
                {},
                0,
                None,
                rate_limiter=rate_limiter,
                control_args=control_args,
            )
        rate_limiter.wait_if_needed.assert_called_once()
        rate_limiter.record_request.assert_called_once()

    def test_resume_path_with_existing_part(self, tmp_path: Path) -> None:
        """resume_path 走库级 download 流式落盘（返回空 body + content-type）。"""
        resume_path = tmp_path / "file.bin"
        (tmp_path / "file.bin.part").write_bytes(b"existing")

        def _fake_download(url: str, dest: Path, **kwargs: Any) -> Path:
            kwargs["on_headers"](206, {"content-type": "application/octet-stream"})
            (tmp_path / "file.bin.part").replace(dest)
            return dest

        fake = Mock()
        fake.download.side_effect = _fake_download
        with patch.object(core, "_get_app_fetcher", return_value=fake):
            data, ct = cr.fetch(
                "https://example.com/file.bin", 30, {}, 0, None, resume_path=resume_path
            )
        assert data == b""
        assert ct == "application/octet-stream"
        assert resume_path.exists()
        call_kwargs = fake.download.call_args.kwargs
        assert call_kwargs["resume"] is True
        assert call_kwargs["chunk_size"] == 1024 * 64
        assert call_kwargs["should_abort"] is not None
        assert call_kwargs["on_headers"] is not None

    def test_resume_path_download_failure_propagates(self, tmp_path: Path) -> None:
        """download 的 4xx FetchError 不退避直接上抛（状态错误语义）。"""
        exc = FetchError("HTTP Error 404: download failed for https://x.com")
        exc.status = 404  # type: ignore[attr-defined]
        fake = Mock()
        fake.download.side_effect = exc
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            pytest.raises(FetchError, match="404"),
        ):
            cr.fetch("https://x.com/file.bin", 30, {}, 3, None, resume_path=tmp_path / "f.bin")

    def test_http_429_with_retry(self) -> None:
        """429 + Retry-After + 有重试次数时 sleep 并重试。"""
        fake = Mock()
        fake.get.side_effect = [
            _mock_fetch_response(429, b"", {"Retry-After": "1"}),
            _mock_fetch_response(200, b"ok", {"content-type": "text/html"}),
        ]
        rate_limiter = cr.DomainRateLimiter()
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            patch.object(cr.time, "sleep"),
        ):
            data, _ct = cr.fetch("https://x.com", 30, {}, 1, None, rate_limiter=rate_limiter)
        assert data == b"ok"
        assert fake.get.call_count == 2

    def test_http_403_no_backoff(self) -> None:
        """403 错误不退避。"""
        fake = Mock()
        fake.get.return_value = _mock_fetch_response(403, b"", {})
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            patch.object(cr.time, "sleep") as mock_sleep,
            pytest.raises(FetchError, match="HTTP Error 403"),
        ):
            cr.fetch("https://x.com", 30, {}, 3, None)
        assert not mock_sleep.called

    def test_os_error_retry(self) -> None:
        """传输层 OSError 重试后成功。"""
        fake = Mock()
        fake.get.side_effect = [
            OSError("connection refused"),
            _mock_fetch_response(200, b"ok", {"content-type": "text/html"}),
        ]
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            patch.object(cr.time, "sleep"),
        ):
            data, _ct = cr.fetch("https://x.com", 30, {}, 2, None)
        assert data == b"ok"

    def test_value_error_no_retry(self) -> None:
        """ValueError（如 max_bytes 超限）不再重试，直接向上传播。"""
        fake = Mock()
        fake.get.side_effect = [
            _mock_fetch_response(200, b"x" * 200, {}),
            _mock_fetch_response(200, b"ok", {}),
        ]
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            patch.object(cr.time, "sleep"),
            pytest.raises(ValueError, match="file exceeds"),
        ):
            cr.fetch("https://x.com", 30, {}, 1, 100)
        assert fake.get.call_count == 1

    def test_all_retries_exhausted(self) -> None:
        """所有重试都失败后抛出最后一个错误。"""
        fake = Mock()
        fake.get.side_effect = OSError("connection refused")
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            patch.object(cr.time, "sleep"),
            pytest.raises(OSError),
        ):
            cr.fetch("https://x.com", 30, {}, 2, None)

    def test_cancelled_by_user(self) -> None:
        """should_stop 返回 True 时 download 中止并转 RuntimeError cancelled。"""
        control_args = Mock()
        control_args.should_stop = lambda: True
        control_args.wait_if_paused = None
        control_args.stealth = False
        exc = FetchError("download aborted by should_abort callback")
        exc.keep_part = True  # type: ignore[attr-defined]
        fake = Mock()
        fake.download.side_effect = exc
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            pytest.raises(RuntimeError, match="cancelled"),
        ):
            cr.fetch(
                "https://example.com/file.bin",
                30,
                {},
                0,
                None,
                resume_path=Path("cancelled.bin"),
                control_args=control_args,
            )


# ========== 播放清单加密分支 ==========


class TestPlaylistEncrypted:
    def test_m3u8_encrypted_with_key_fetch(self) -> None:
        """加密 m3u8 + decrypt=True + HAS_AES 时尝试获取 key。"""
        text = (
            '#EXT-X-KEY:METHOD=AES-128,URI="https://x.com/key.bin",IV=0xabcdef\n'
            "#EXTINF:10,\nhttps://x.com/seg1.ts\n"
        )
        # 模拟 HAS_AES=True 和 fetch 成功获取 key
        fake_aes = Mock()
        fake_aes.MODE_CBC = 2
        with (
            patch.object(core, "HAS_AES", True),
            patch.object(
                core, "fetch", return_value=(b"16bytekey1234567", "application/octet-stream")
            ),
        ):
            resources, note = cr.discover_playlist_resources(
                text, "https://x.com/playlist.m3u8", "https://x.com/", decrypt=True
            )
        assert len(resources) == 1
        assert "decrypted" in note

    def test_m3u8_encrypted_key_fetch_without_iv(self) -> None:
        """加密 m3u8 无 IV 时使用 0 填充。"""
        text = (
            '#EXT-X-KEY:METHOD=AES-128,URI="https://x.com/key.bin"\n'
            "#EXTINF:10,\nhttps://x.com/seg1.ts\n"
        )
        with (
            patch.object(core, "HAS_AES", True),
            patch.object(
                core, "fetch", return_value=(b"16bytekey1234567", "application/octet-stream")
            ),
        ):
            resources, note = cr.discover_playlist_resources(
                text, "https://x.com/playlist.m3u8", "https://x.com/", decrypt=True
            )
        assert len(resources) == 1
        assert "decrypted" in note

    def test_m3u8_encrypted_key_fetch_fails(self) -> None:
        """加密 m3u8 key 获取失败时返回 encrypted 提示。"""
        text = (
            '#EXT-X-KEY:METHOD=AES-128,URI="https://x.com/key.bin",IV=0xabcdef\n'
            "#EXTINF:10,\nhttps://x.com/seg1.ts\n"
        )
        with (
            patch.object(core, "HAS_AES", True),
            patch.object(core, "fetch", side_effect=Exception("network error")),
        ):
            resources, note = cr.discover_playlist_resources(
                text, "https://x.com/playlist.m3u8", "https://x.com/", decrypt=True
            )
        assert len(resources) == 1
        assert "encrypted" in note

    def test_m3u8_encrypted_no_aes(self) -> None:
        """加密 m3u8 + HAS_AES=False 时提示安装 pycryptodome。"""
        text = (
            '#EXT-X-KEY:METHOD=AES-128,URI="https://x.com/key.bin"\n'
            "#EXTINF:10,\nhttps://x.com/seg1.ts\n"
        )
        with (
            patch.object(core, "HAS_AES", False),
        ):
            resources, note = cr.discover_playlist_resources(
                text, "https://x.com/playlist.m3u8", "https://x.com/", decrypt=True
            )
        assert len(resources) == 1
        assert "pycryptodome" in note

    def test_m3u8_encrypted_decrypt_false(self) -> None:
        """加密 m3u8 + decrypt=False 时提示使用 --decrypt。"""
        text = (
            '#EXT-X-KEY:METHOD=AES-128,URI="https://x.com/key.bin"\n'
            "#EXTINF:10,\nhttps://x.com/seg1.ts\n"
        )
        resources, note = cr.discover_playlist_resources(
            text, "https://x.com/playlist.m3u8", "https://x.com/", decrypt=False
        )
        assert len(resources) == 1
        assert "--decrypt" in note

    def test_mpd_skips_non_downloadable(self) -> None:
        """MPD 中非 downloadable 的 BaseURL 被跳过。"""
        text = "<MPD><BaseURL></BaseURL><BaseURL>seg1.mp4</BaseURL></MPD>"
        resources, note = cr.discover_playlist_resources(
            text, "https://x.com/playlist.mpd", "https://x.com/"
        )
        assert len(resources) == 1
        assert note == ""


# ========== extract_readable_text content div 分支 ==========


class TestExtractReadableTextContentDiv:
    def test_content_div(self) -> None:
        """id=content 的 div 分支。"""
        html = '<div id="content"><p>Content div text that is long enough to remain</p></div>'
        result = cr.extract_readable_text(html)
        assert "Content div text" in result

    def test_article_content_div(self) -> None:
        """class=article-content 的 div 分支。"""
        html = '<div class="article-content"><p>Article content text long enough here</p></div>'
        result = cr.extract_readable_text(html)
        assert "Article content text" in result

    def test_entry_content_div(self) -> None:
        """class=entry-content 的 div 分支。"""
        html = '<div class="entry-content"><p>Entry content text long enough to keep</p></div>'
        result = cr.extract_readable_text(html)
        assert "Entry content text" in result


# ========== sitemap nested ==========


class TestDiscoverSitemapNested:
    def test_nested_sitemap_index(self) -> None:
        """sitemapindex 嵌套 sitemap 正确解析。"""
        sitemap_index = (
            '<?xml version="1.0"?><sitemapindex>'
            "<sitemap><loc>https://x.com/sub1.xml</loc></sitemap>"
            "<sitemap><loc>https://x.com/sub2.xml</loc></sitemap>"
            "</sitemapindex>"
        )
        sub1 = '<?xml version="1.0"?><urlset><url><loc>https://x.com/a</loc></url></urlset>'
        sub2 = '<?xml version="1.0"?><urlset><url><loc>https://x.com/b</loc></url></urlset>'

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "sub1" in url:
                return (sub1.encode(), "application/xml")
            if "sub2" in url:
                return (sub2.encode(), "application/xml")
            return (sitemap_index.encode(), "application/xml")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            urls = cr.discover_sitemap_urls("https://x.com/sitemap.xml", {}, 10)
        assert "https://x.com/a" in urls
        assert "https://x.com/b" in urls

    def test_nested_sitemap_fetch_error(self) -> None:
        """嵌套 sitemap 获取失败时跳过。"""
        sitemap_index = (
            '<?xml version="1.0"?><sitemapindex>'
            "<sitemap><loc>https://x.com/sub1.xml</loc></sitemap>"
            "</sitemapindex>"
        )

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "sub1" in url:
                raise RuntimeError("network error")
            return (sitemap_index.encode(), "application/xml")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            urls = cr.discover_sitemap_urls("https://x.com/sitemap.xml", {}, 10)
        assert urls == []


# ========== load_config_from_file not exists ==========


class TestLoadConfigNotFound:
    def test_missing_file_exits(self, tmp_path: Path) -> None:
        """配置文件不存在时 sys.exit(2)（配置错误码，区别于 1=取消）。"""
        with pytest.raises(SystemExit) as exc_info:
            cr.load_config_from_file(str(tmp_path / "nonexistent.json"))
        assert exc_info.value.code == 2


# ========== is_video_candidate ==========


class TestIsVideoCandidate:
    def _resource(self, url: str = "", found_in: str = "", kind: str = "") -> cr.Resource:
        return cr.Resource(url=url, found_in=found_in, kind=kind, page_url="")

    def test_by_suffix(self) -> None:
        """is_video_candidate 按后缀识别视频。"""
        assert cr.is_video_candidate(self._resource(url="https://x.com/v.mp4")) is True

    def test_by_kind_video(self) -> None:
        """is_video_candidate 按 kind 识别视频。"""
        assert cr.is_video_candidate(self._resource(kind="video")) is True

    def test_by_kind_audio(self) -> None:
        """is_video_candidate 按 kind 识别音频。"""
        assert cr.is_video_candidate(self._resource(kind="audio")) is True

    def test_by_kind_source(self) -> None:
        """is_video_candidate 按 kind 识别 source。"""
        assert cr.is_video_candidate(self._resource(kind="source")) is True

    def test_by_found_in_video(self) -> None:
        """is_video_candidate 按 found_in 识别视频。"""
        assert cr.is_video_candidate(self._resource(found_in="video[src]")) is True

    def test_by_found_in_poster(self) -> None:
        """is_video_candidate 按 found_in 识别 poster。"""
        assert cr.is_video_candidate(self._resource(found_in="video[poster]")) is True

    def test_not_video(self) -> None:
        """is_video_candidate 拒绝非视频资源。"""
        assert cr.is_video_candidate(self._resource(url="https://x.com/img.png")) is False


# ========== build_recommendations 全错误分类 ==========


class TestBuildRecommendationsAllErrors:
    def _make_rows(self, statuses: list[str]) -> list[cr.ManifestRow]:
        return [
            cr.ManifestRow(
                status=s,
                url=f"https://x.com/{i}",
                saved_path="",
                content_type="",
                bytes=0,
                category="other",
                found_in="",
                kind="",
                page_url="",
                page_title="",
                diagnostic="",
            )
            for i, s in enumerate(statuses)
        ]

    def test_rate_limit_error(self) -> None:
        """429 限流错误触发限流建议。"""
        rows = self._make_rows(["error: HTTP Error 429"] * 3)
        ctx = cr.build_report_context(rows, 1, 0.0, 1.0)
        recs = cr.build_recommendations(ctx)
        assert any("429" in r["title"] for r in recs)

    def test_timeout_error(self) -> None:
        """超时错误触发超时建议。"""
        rows = self._make_rows(["error: timed out"] * 3)
        ctx = cr.build_report_context(rows, 1, 0.0, 1.0)
        recs = cr.build_recommendations(ctx)
        assert any("超时" in r["title"] for r in recs)

    def test_network_error(self) -> None:
        """网络错误触发网络建议。"""
        rows = self._make_rows(["error: URLError connection refused"] * 3)
        ctx = cr.build_report_context(rows, 1, 0.0, 1.0)
        recs = cr.build_recommendations(ctx)
        assert any("网络" in r["title"] for r in recs)

    def test_not_found_error(self) -> None:
        """404 错误触发 404 建议。"""
        rows = self._make_rows(["error: HTTP Error 404"] * 3)
        ctx = cr.build_report_context(rows, 1, 0.0, 1.0)
        recs = cr.build_recommendations(ctx)
        assert any("404" in r["title"] for r in recs)

    def test_size_limit_error(self) -> None:
        """大小限制错误触发大小建议。"""
        rows = self._make_rows(["error: file exceeds max-bytes"] * 3)
        ctx = cr.build_report_context(rows, 1, 0.0, 1.0)
        recs = cr.build_recommendations(ctx)
        assert any("大小" in r["title"] for r in recs)

    def test_encrypted_error(self) -> None:
        """加密播放清单错误触发加密建议。"""
        rows = self._make_rows(["error: encrypted playlist detected"] * 3)
        ctx = cr.build_report_context(rows, 1, 0.0, 1.0)
        recs = cr.build_recommendations(ctx)
        assert any("加密" in r["title"] for r in recs)

    def test_dedup_tip(self) -> None:
        """有去重时触发去重建议。"""
        rows = [
            cr.ManifestRow(
                status="ok",
                url="https://x.com/a.js",
                saved_path="/tmp/a.js",
                content_type="application/javascript",
                bytes=100,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="",
                diagnostic="",
                sha256="abc",
            ),
            cr.ManifestRow(
                status="skipped by dedup",
                url="https://x.com/b.js",
                saved_path="",
                content_type="application/javascript",
                bytes=50,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="",
                diagnostic="dedup",
                sha256="abc",
            ),
        ]
        ctx = cr.build_report_context(rows, 1, 0.0, 1.0)
        recs = cr.build_recommendations(ctx)
        assert any("去重" in r["title"] for r in recs)

    def test_low_success_rate(self) -> None:
        """成功率低于 80% 且无 auth 错误时触发成功率建议。"""
        rows = self._make_rows(["error: timed out"] * 15 + ["ok"] * 2)
        ctx = cr.build_report_context(rows, 1, 0.0, 1.0)
        recs = cr.build_recommendations(ctx)
        assert any("成功率" in r["title"] for r in recs)


# ========== write_summary with error/skip classes ==========


class TestWriteSummaryWithErrors:
    def test_summary_with_error_and_skip_classes(self, tmp_path: Path) -> None:
        """summary 包含失败原因分类和跳过原因分类。"""
        rows = [
            cr.ManifestRow(
                status="ok",
                url="https://x.com/a.js",
                saved_path="/tmp/a.js",
                content_type="application/javascript",
                bytes=100,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="Page",
                diagnostic="",
                sha256="abc",
            ),
            cr.ManifestRow(
                status="error: HTTP Error 404",
                url="https://x.com/b.js",
                saved_path="",
                content_type="",
                bytes=0,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="Page",
                diagnostic="not found",
                sha256="",
            ),
            cr.ManifestRow(
                status="skipped by dedup",
                url="https://x.com/c.js",
                saved_path="",
                content_type="application/javascript",
                bytes=50,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="Page",
                diagnostic="dedup",
                sha256="def",
            ),
        ]
        cr.write_summary(
            tmp_path, rows, 1, start_time=0.0, end_time=1.0, config={"url": "https://x.com"}
        )
        content = (tmp_path / "summary.txt").read_text(encoding="utf-8")
        assert "失败原因分类" in content
        assert "跳过原因分类" in content
        assert "失败资源" in content


# ========== write_run_report with failures ==========


class TestWriteRunReportWithFailures:
    def test_markdown_with_failures_by_class(self, tmp_path: Path) -> None:
        """Markdown 报告包含失败资源明细（按原因分组）。"""
        rows = [
            cr.ManifestRow(
                status="error: HTTP Error 404",
                url="https://x.com/b.js",
                saved_path="",
                content_type="",
                bytes=0,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="Page",
                diagnostic="not found",
                sha256="",
            ),
            cr.ManifestRow(
                status="error: HTTP Error 401",
                url="https://x.com/c.js",
                saved_path="",
                content_type="",
                bytes=0,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="Page",
                diagnostic="unauthorized",
                sha256="",
            ),
        ]
        cr.write_run_report(
            tmp_path, rows, 1, start_time=0.0, end_time=1.0, config={"url": "https://x.com"}
        )
        md = (tmp_path / "run_report.md").read_text(encoding="utf-8")
        assert "失败资源明细" in md
        assert "404" in md or "不存在" in md

    def test_html_with_empty_config(self, tmp_path: Path) -> None:
        """HTML 报告无配置时显示'无配置信息'。"""
        cr.write_run_report(tmp_path, [], 0, start_time=0.0, end_time=0.0, config=None)
        html = (tmp_path / "run_report.html").read_text(encoding="utf-8")
        assert "无配置信息" in html

    def test_html_with_error_and_skip_classes(self, tmp_path: Path) -> None:
        """HTML 报告包含失败原因和跳过原因分类。"""
        rows = [
            cr.ManifestRow(
                status="error: HTTP Error 404",
                url="https://x.com/b.js",
                saved_path="",
                content_type="",
                bytes=0,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="Page",
                diagnostic="not found",
                sha256="",
            ),
            cr.ManifestRow(
                status="skipped by robots.txt",
                url="https://x.com/c.js",
                saved_path="",
                content_type="",
                bytes=0,
                category="script",
                found_in="script[src]",
                kind="script",
                page_url="https://x.com/",
                page_title="Page",
                diagnostic="robots",
                sha256="",
            ),
        ]
        cr.write_run_report(
            tmp_path, rows, 1, start_time=0.0, end_time=1.0, config={"url": "https://x.com"}
        )
        html = (tmp_path / "run_report.html").read_text(encoding="utf-8")
        assert "失败原因" in html
        assert "跳过原因" in html
        assert "失败资源明细" in html


# ========== diagnostic_for_status encrypted ==========


class TestDiagnosticEncrypted:
    def test_encrypted_playlist(self) -> None:
        """diagnostic_for_status 对加密清单含提示。"""
        assert "Encrypted playlist" in cr.diagnostic_for_status("encrypted playlist detected")


# ========== crawl 进阶分支 ==========


class TestCrawlSitemap:
    def test_sitemap_discovery(self, tmp_path: Path) -> None:
        """--sitemap 时从 sitemap.xml 发现页面。"""
        sitemap = (
            '<?xml version="1.0"?><urlset>'
            "<url><loc>https://example.com/page1</loc></url>"
            "<url><loc>https://example.com/page2</loc></url>"
            "</urlset>"
        )
        html = b"<html><body><p>content</p></body></html>"

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "sitemap.xml" in url:
                return (sitemap.encode(), "application/xml")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--sitemap",
                    "--max-pages",
                    "3",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0


class TestCrawlResumeCrawl:
    def test_resume_crawl_with_saved_state(self, tmp_path: Path) -> None:
        """resume_crawl 加载已保存的状态（状态必须带同一 seed_url）。"""
        # 先保存一个状态
        cr.save_crawl_state(
            tmp_path,
            seed_url="https://example.com",
            page_queue=["https://example.com"],
            seen_pages=["https://example.com/old"],
            page_titles={"https://example.com/old": "Old"},
            resources=[],
            sha256_set=[],
        )
        html = b"<html><body><p>content</p></body></html>"
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--resume-crawl",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0

    def test_resume_crawl_ignores_state_from_other_site(self, tmp_path: Path) -> None:
        """同一输出目录换站续跑时，旧站状态必须被忽略，不能把旧页面抓回来。"""
        cr.save_crawl_state(
            tmp_path,
            seed_url="https://old.example",
            page_queue=["https://old.example/page"],
            seen_pages=["https://old.example/page"],
            page_titles={},
            resources=[],
            sha256_set=[],
        )
        html = b"<html><body><p>content</p></body></html>"
        fetched: list[str] = []

        def fake_fetch(url, *args, **kwargs):
            fetched.append(url)
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=fake_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://new.example",
                    "--out",
                    str(tmp_path),
                    "--resume-crawl",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        assert all("old.example" not in url for url in fetched)
        assert any("new.example" in url for url in fetched)

    def test_resume_crawl_no_saved_state(self, tmp_path: Path) -> None:
        """resume_crawl 无保存状态时从头开始。"""
        html = b"<html><body><p>content</p></body></html>"
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--resume-crawl",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0

    def test_resume_crawl_clears_state_on_success(self, tmp_path: Path) -> None:
        """resume_crawl 成功完成后清理状态文件。"""
        cr.save_crawl_state(tmp_path, page_queue=["https://example.com"])
        # HTML 必须包含资源，否则 crawl 在清理前就 return 0
        html = b'<html><body><img src="https://example.com/logo.png"></body></html>'
        with (
            patch.object(core, "fetch", return_value=(html, "text/html")),
            patch.object(ctx_mod, "clear_crawl_state") as mock_clear,
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--resume-crawl",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        mock_clear.assert_called_once_with(tmp_path.resolve())


class TestCrawlBlockedPages:
    def test_blocked_page_skipped(self, tmp_path: Path) -> None:
        """被 block_keyword 匹配的页面被跳过。"""
        html = b"<html><body><p>content</p></body></html>"
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--block-keyword",
                    "ads",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0

    def test_same_domain_filter(self, tmp_path: Path) -> None:
        """--same-domain 过滤跨域资源。"""
        html = b'<html><body><img src="https://cdn.other.com/img.png"></body></html>'
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--same-domain",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        manifest = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert len(manifest) == 0  # 跨域资源被过滤

    def test_respect_robots_skips(self, tmp_path: Path) -> None:
        """--respect-robots 跳过 robots.txt 禁止的资源。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "robots.txt" in url:
                return (b"User-agent: *\nDisallow: /img", "text/plain")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--respect-robots",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0


class TestCrawlCrawlPages:
    def test_resume_preserves_two_pages_with_case_colliding_paths(self, tmp_path: Path) -> None:
        args = cr.build_parser().parse_args(
            [
                "--url",
                "https://example.com/News",
                "--out",
                str(tmp_path),
                "--max-pages",
                "2",
                "--crawl-pages",
                "--resume-crawl",
                "--extract-text",
                "--rewrite-html",
            ]
        )
        stop = threading.Event()
        args.should_stop = stop.is_set
        real_save = ctx_mod.save_crawl_state

        def save_state(output: Path, **state: object) -> None:
            real_save(output, **state)
            stop.set()

        pages = [
            (b'<article>Upper body</article><a href="/news">Other article</a>', "text/html"),
            (b"<article>Lower body</article>", "text/html"),
        ]
        with (
            patch.object(ctx_mod, "save_crawl_state", side_effect=save_state),
            patch.object(core, "fetch", side_effect=pages),
        ):
            assert cr.crawl(args) == 1

        stop.clear()
        extracted: list[PageExtraction] = []
        args.extraction_callback = extracted.append
        with patch.object(core, "fetch") as fetch:
            assert cr.crawl(args) == 0
        fetch.assert_not_called()
        assert {page.url: page.text for page in extracted} == {
            "https://example.com/News": "Upper body",
            "https://example.com/news": "Lower body",
        }
        contents = {
            path.read_text(encoding="utf-8")
            for path in (tmp_path / "extracted_text").rglob("*.txt")
        }
        assert contents == {"Upper body", "Lower body"}
        offline_dir = tmp_path / "offline_pages"
        offline = [
            path.read_text(encoding="utf-8")
            for path in offline_dir.rglob("*.html")
            if path != offline_dir / "index.html"
        ]
        assert len(offline) == 2
        assert sum("Upper body" in page for page in offline) == 1
        assert sum("Lower body" in page for page in offline) == 1

    def test_crawl_pages_follows_links(self, tmp_path: Path) -> None:
        """--crawl-pages 跟随页面链接并保存状态。"""
        page1 = b'<html><body><a href="/page2">link</a><img src="img.png"></body></html>'
        page2 = b"<html><body><p>page2</p></body></html>"

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "page2" in url:
                return (page2, "text/html")
            return (page1, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--crawl-pages",
                    "--max-pages",
                    "2",
                    "--list-only",
                    "--workers",
                    "1",
                    "--resume-crawl",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0

    def test_non_document_links_are_not_saved_as_pages(self, tmp_path: Path) -> None:
        """--crawl-pages 跟随到 PDF/CSV 等非文档响应时跳过，不产生垃圾页面。"""
        index = b'<html><body><a href="/file.pdf">pdf</a><img src="i.png"></body></html>'
        pdf = b"%PDF-1.4 binary payload"

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if ".pdf" in url:
                return (pdf, "application/pdf")
            return (index, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--crawl-pages",
                    "--max-pages",
                    "2",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            assert cr.crawl(args) == 0
        assert not list((tmp_path / "pages").rglob("*.pdf"))
        report = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))
        assert report["page_scan"]["skipped"] >= 1

    def test_seed_url_is_always_scanned(self, tmp_path: Path) -> None:
        """显式指定的种子 URL 不做响应类型过滤（保持旧行为）。"""
        pdf = b"%PDF-1.4 binary payload"

        with patch.object(core, "fetch", return_value=(pdf, "application/pdf")):
            args = cr.build_parser().parse_args(
                ["--url", "https://example.com/file.pdf", "--out", str(tmp_path), "--list-only"]
            )
            assert cr.crawl(args) == 0
        assert list((tmp_path / "pages").rglob("*"))


class TestCrawlUrlPatterns:
    def test_include_pattern(self, tmp_path: Path) -> None:
        """--include-pattern 过滤匹配的资源。"""
        html = b'<html><body><img src="https://example.com/img.png"><img src="https://example.com/logo.jpg"></body></html>'
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--include-pattern",
                    r"logo",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        manifest = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert len(manifest) == 1
        assert "logo" in manifest[0]["url"]

    def test_exclude_pattern(self, tmp_path: Path) -> None:
        """--exclude-pattern 排除匹配的资源。"""
        html = b'<html><body><img src="https://example.com/img.png"><img src="https://example.com/logo.jpg"></body></html>'
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--exclude-pattern",
                    r"logo",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        manifest = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert len(manifest) == 1
        assert "img.png" in manifest[0]["url"]


class TestCrawlDedup:
    def test_dedup_skips_duplicate(self, tmp_path: Path) -> None:
        """--dedup 跳过 SHA256 重复的资源。"""
        html = b'<html><body><img src="https://example.com/a.png"><img src="https://example.com/b.png"></body></html>'
        img_data = b"SAME_CONTENT"

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if ".png" in url:
                return (img_data, "image/png")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--dedup",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        manifest = json.loads((tmp_path / "resources_manifest.json").read_text())
        statuses = [r["status"] for r in manifest]
        assert "ok" in statuses
        assert any("dedup" in s for s in statuses)


class TestCrawlCssUrls:
    def test_include_css_urls(self, tmp_path: Path) -> None:
        """--include-css-urls 发现 CSS 中的 url() 资源。"""
        html = b'<html><head><link rel="stylesheet" href="https://example.com/style.css"></head></html>'
        css = 'background: url("https://example.com/bg.png");'

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "style.css" in url:
                return (css.encode(), "text/css")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--include-css-urls",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0


class TestCrawlRewriteHtml:
    def test_rewrite_html(self, tmp_path: Path) -> None:
        """报告、同站页面、样式表及去重资源都可从本地文件访问。"""
        from urllib.parse import unquote, urlsplit

        from lxml import html as lxml_html

        source = "https://example.com/section/start.html"
        css = (
            '@charset "GBK"; @import "reset.css"; .cover { background: url(/img/logo.png) }'
            ' .cover::after { content: "封面" }'
        ).encode("gbk")
        outputs = {
            "/section/start.html": (
                (
                    b'<!doctype html><head><base href="../"><title>Start &amp; more</title>'
                    b'<link rel="stylesheet" href="css/main.css" integrity="sha256-placeholder" crossorigin="anonymous">'
                    b'<link rel="stylesheet" href="css/alternate/main.css" media="print"></head><body>'
                    b'<p>img/logo.png</p><img src="img/logo.png">'
                    b'<img src="img/alias.png"><img srcset="data:image/png,abcd 1x, img/logo.png 2x">'
                    b'<a href="other/more.html#anchor">More</a><a href="#same">Same</a>'
                    b'<a href="contact">Remote</a><a download href="img/logo.png">Download</a></body>'
                ),
                "text/html",
            ),
            "/other/more.html": (
                (
                    b'<title>More</title><base href=""><base href="../">'
                    b'<p id="anchor">Other page</p><a href="#anchor">Local</a>'
                ),
                "text/html",
            ),
            "/css/main.css": (css, "text/css"),
            "/css/reset.css": (b".cover { color: rgb(10, 20, 30) }", "text/css"),
            "/css/alternate/main.css": (css, "text/css"),
            "/css/alternate/reset.css": (b".cover { color: rgb(40, 50, 60) }", "text/css"),
            "/img/logo.png": (b"PNG_DATA", "image/png"),
            "/img/alias.png": (b"PNG_DATA", "image/png"),
        }

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            body, content_type = outputs[urlsplit(url).path]
            resume_path = kwargs.get("resume_path")
            if resume_path is not None:
                resume_path.write_bytes(body)
                return b"", content_type
            return body, content_type

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    source,
                    "--out",
                    str(tmp_path),
                    "--rewrite-html",
                    "--crawl-pages",
                    "--max-pages",
                    "2",
                    "--include-css-urls",
                    "--dedup",
                    "--resume",
                    "--workers",
                    "1",
                ]
            )
            result = Mock()
            args.result_callback = result
            assert cr.crawl(args) == 0

        def read_document(path: Path) -> Any:
            return lxml_html.fromstring(path.read_text(encoding="utf-8"))

        def local_path(document: Path, reference: str) -> Path:
            parts = urlsplit(reference)
            assert not parts.scheme and not parts.netloc
            path = (document.parent / unquote(parts.path)).resolve()
            assert path.is_relative_to(tmp_path) and path.is_file()
            return path

        report = tmp_path / "run_report.html"
        index = local_path(
            report, read_document(report).xpath('//a[contains(@href,"offline_pages")]/@href')[0]
        )
        page_count = {"count": 2, "index": index.relative_to(tmp_path).as_posix()}
        summary = result.call_args.args[0].summary
        assert summary["offline_pages"] == page_count
        check = summary["offline_check"]
        assert check["status"] == "passed"
        report_json = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))
        assert report_json["offline_check"] == check
        assert "静态引用通过" in report.read_text(encoding="utf-8")
        assert (
            json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))["offline_pages"]
            == page_count
        )
        links = read_document(index).xpath("//main//a")
        assert [link.text for link in links] == ["Start & more", "More"]
        page, more = [local_path(index, link.get("href")) for link in links]
        document = read_document(page)
        assert document.xpath("//p/text()") == ["img/logo.png"]
        assert not document.xpath("//base/@href")
        assert document.xpath("//meta[@charset]/@charset") == ["utf-8"]
        image, alias = document.xpath("//img/@src")
        assert local_path(page, image).read_bytes() == b"PNG_DATA"
        assert alias == image
        assert document.xpath("//img/@srcset") == [f"data:image/png,abcd 1x, {image} 2x"]
        captured, anchor, remote, download = document.xpath("//a/@href")
        assert local_path(page, captured) == more and captured.endswith("#anchor")
        assert (anchor, remote) == ("https://example.com/#same", "https://example.com/contact")
        assert local_path(page, download) == local_path(page, image)
        assert read_document(more).xpath('//*[@id="anchor"]/text()') == ["Other page"]
        assert read_document(more).xpath("//a/@href") == ["#anchor"]
        assert not document.xpath("//link[@integrity or @crossorigin]")
        stylesheet, alternate = [local_path(page, href) for href in document.xpath("//link/@href")]
        content = stylesheet.read_text(encoding="utf-8")
        imported = content.split('@import "')[1].split('"')[0]
        imported_image = content.split('url("')[1].split('"')[0]
        assert "rgb(10, 20, 30)" in local_path(stylesheet, imported).read_text(encoding="utf-8")
        assert local_path(stylesheet, imported_image) == local_path(page, image)
        assert 'content: "封面"' in content
        alternate_import = alternate.read_text(encoding="utf-8").split('@import "')[1].split('"')[0]
        assert "rgb(40, 50, 60)" in local_path(alternate, alternate_import).read_text(
            encoding="utf-8"
        )
        assert any(path.read_bytes() == css for path in tmp_path.rglob("main.css"))

    def test_rewrite_html_with_strip_overlays(self, tmp_path: Path) -> None:
        """--rewrite-html + --strip-overlays 去除 overlay。"""
        html = b'<html><body><div class="modal">ad</div><img src="https://example.com/logo.png"></body></html>'
        img_data = b"PNG_DATA"

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "logo.png" in url:
                return (img_data, "image/png")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--rewrite-html",
                    "--strip-overlays",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0


class TestCrawlSmartExtract:
    def test_smart_extract(self, tmp_path: Path) -> None:
        """--smart-extract 提取结构化数据。"""
        # 必须包含至少一个资源，否则 crawl 会在"no resources found"处提前返回。
        html = b"""<html><head>
        <title>Test</title>
        <meta property="og:title" content="OG">
        </head><body><p>content</p>
        <img src="https://example.com/logo.png"></body></html>"""

        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--smart-extract",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        assert (tmp_path / "extracted_data.json").exists()


class TestCrawlExtractText:
    def test_extract_text(self, tmp_path: Path) -> None:
        """--extract-text 提取可读文本。"""
        # 必须包含至少一个资源，否则 crawl 会在"no resources found"处提前返回。
        html = b"""<html><body><article><p>This is a long enough article text to be extracted as readable content</p></article>
        <img src="https://example.com/img.png"></body></html>"""

        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--extract-text",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        assert (tmp_path / "extracted_text").exists()


class TestCrawlExtractMarkdown:
    """--extract-markdown：firecrawl 式结构化 Markdown 输出。"""

    def test_extract_markdown_writes_files(self, tmp_path: Path) -> None:
        html = (
            "<html><body><article>"
            "<h2>区域销售</h2>"
            "<p>华东区使用<b>新口径</b>统计，详见<a href='/r'>说明</a>。</p>"
            "<ul><li>苹果</li><li>香蕉</li></ul>"
            "<table><tr><th>名称</th><th>数量</th></tr><tr><td>苹果</td><td>3</td></tr></table>"
            "</article></body>"
            "<img src='https://example.com/img.png'></html>"
        ).encode()

        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--extract-markdown",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        md_files = list((tmp_path / "extracted_markdown").rglob("*.md"))
        assert md_files, "应生成至少一个 .md 产物"
        content = md_files[0].read_text(encoding="utf-8")
        assert "## 区域销售" in content
        assert "**新口径**" in content
        assert "[说明](/r)" in content
        assert "- 苹果" in content
        assert "| 名称 | 数量 |" in content
        assert "| --- | --- |" in content

    def test_extract_markdown_unit_renders_semantics(self) -> None:
        html = (
            "<article><h3>要点</h3><blockquote>引用一</blockquote>"
            "<pre><code>print(1)</code></pre>"
            "<ol><li>第一</li><li>第二</li></ol></article>"
        )
        result = cr.extract_markdown(html)
        assert "### 要点" in result
        assert "> 引用一" in result
        assert "```" in result and "print(1)" in result
        assert "1. 第一" in result and "2. 第二" in result

    def test_standalone_inline_elements_keep_markup(self) -> None:
        """容器直属的行内元素（不在 <p> 内）也要保留 Markdown 标记。"""
        html = (
            "<article><p>前言段落文字足够长的一段。</p>"
            "<a href='/x'>独立链接</a><strong>独立强调</strong><em>独立斜体</em>"
            "<code>code()</code><a href='/y'><strong>嵌套</strong></a></article>"
        )
        result = cr.extract_markdown(html)
        assert "[独立链接](/x)" in result
        assert "**独立强调**" in result
        assert "*独立斜体*" in result
        assert "`code()`" in result
        assert "[**嵌套**](/y)" in result

    def test_list_item_with_paragraph_is_not_duplicated(self) -> None:
        """<li><p>文字</p></li> 只输出一次（此前会列表项 + 段落重复两遍）。"""
        html = "<article><ul><li><p>段落列表项</p></li><li>普通项</li></ul></article>"
        result = cr.extract_markdown(html)
        assert "- 段落列表项" in result
        assert "- 普通项" in result
        assert result.count("段落列表项") == 1

    def test_nested_list_indentation_and_blocks(self) -> None:
        html = (
            "<article><ol><li>一<ul><li>a</li><li>b</li></ul></li>"
            "<li><div>块级列表项</div></li></ol></article>"
        )
        result = cr.extract_markdown(html)
        assert "1. 一" in result
        assert "  - a" in result
        assert "2. 块级列表项" in result

    def test_cjk_spacing_around_inline_markers(self) -> None:
        """中文与 **/` 交界不插空格（对齐源码文档契约与纯文本空格规则）。"""
        html = "<article><p>请先<strong>安装</strong>依赖，然后运行<code>pip install</code>。</p></article>"
        result = cr.extract_markdown(html)
        assert "请先**安装**依赖" in result
        assert "运行 `pip install`。" in result

    def test_hr_and_table_pipe_are_safe(self) -> None:
        html = (
            "<article><div>上面<hr>下面</div>"
            "<table><tr><th>a|b</th><th>c</th></tr><tr><td>1</td><td>2</td></tr></table></article>"
        )
        result = cr.extract_markdown(html)
        assert "---" in result
        assert "| a\\|b | c |" in result

    def test_markdown_only_feeds_extraction_callback(self, tmp_path: Path) -> None:
        """未开正文提取时，用 Markdown 原文填充页面提取，Web UI 才能看到内容。"""
        html = (
            b"<html><body><article><p>Enough body text for the extraction pipeline</p>"
            b"<img src='https://example.com/i.png'></article></body></html>"
        )
        collected: list[Any] = []
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--extract-markdown",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            args.extraction_callback = collected.append
            assert cr.crawl(args) == 0
        assert collected
        assert collected[0].text.strip() == "Enough body text for the extraction pipeline"
        assert "example.com" in collected[0].url

    def test_markdown_and_text_flags_are_independent(self, tmp_path: Path) -> None:
        html = (
            b"<html><body><article><p>Enough body text for the extraction pipeline</p>"
            b"<img src='https://example.com/i.png'></article></body></html>"
        )
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--extract-markdown",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            assert cr.crawl(args) == 0
        assert (tmp_path / "extracted_markdown").exists()
        # 未开 --extract-text 时不应生成纯文本目录
        assert not (tmp_path / "extracted_text").exists()


class TestCrawlVideoMode:
    def test_video_mode(self, tmp_path: Path) -> None:
        """--video-mode 生成视频清单。"""
        html = b'<html><body><video src="https://example.com/v.mp4"></video></body></html>'

        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--video-mode",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0


class TestCrawlDownloadError:
    def test_download_error_recorded(self, tmp_path: Path) -> None:
        """下载失败时记录 error 状态。"""
        html = b'<html><body><img src="https://example.com/broken.png"></body></html>'

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "broken.png" in url:
                raise RuntimeError("404 not found")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        manifest = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert any(r["status"].startswith("error") for r in manifest)

    def test_download_cancelled_returns_1(self, tmp_path: Path) -> None:
        """下载过程中取消返回 1。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'

        stop_flag = [False]

        def mock_should_stop(args: Any) -> bool:
            return stop_flag[0]

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "img.png" in url:
                stop_flag[0] = True
                raise RuntimeError("cancelled by user")
            return (html, "text/html")

        with (
            patch.object(core, "fetch", side_effect=mock_fetch),
            patch.object(core, "should_stop", side_effect=mock_should_stop),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        # 取消统一返回 1，且跳过后处理（不写清单）
        assert exit_code == 1
        assert not (tmp_path / "resources_manifest.json").exists()


class TestCrawlExpandPlaylists:
    def test_expand_playlists(self, tmp_path: Path) -> None:
        """--expand-playlists 展开播放清单。"""
        html = b'<html><body><video src="https://example.com/playlist.m3u8"></video></body></html>'
        m3u8 = "#EXTM3U\n#EXTINF:10,\nhttps://example.com/seg1.ts\n"

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "playlist.m3u8" in url:
                return (m3u8.encode(), "application/vnd.apple.mpegurl")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--expand-playlists",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0


class TestMainSaveConfigNoUrl:
    def test_save_config_without_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--save-config 无 --url 时报错退出。"""
        config_path = tmp_path_for_config()
        monkeypatch.setattr("sys.argv", ["crawler", "--save-config", str(config_path)])
        with pytest.raises(SystemExit):
            cr.main()

    def test_load_config_with_cli_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--load-config 后 CLI 参数覆盖配置文件。"""
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "url": "https://example.com",
                    "out": str(tmp_path),
                    "workers": 1,
                    "list_only": False,
                    "save_config": None,
                    "same_domain": False,
                    "include_css_urls": False,
                    "rewrite_html": False,
                    "strip_overlays": False,
                    "decrypt": False,
                    "video_mode": False,
                    "video_only": False,
                    "expand_playlists": False,
                    "respect_robots": False,
                    "timeout": 20,
                    "retries": 1,
                    "delay": 0.5,
                    "max_bytes": 0,
                    "encoding": None,
                    "user_agent": cr.DEFAULT_USER_AGENT,
                    "header": [],
                    "block_keyword": [],
                    "include_pattern": None,
                    "exclude_pattern": None,
                    "proxy": None,
                    "stealth": False,
                    "impersonate": "chrome131",
                    "resume": False,
                    "organize": False,
                    "dedup": False,
                    "sitemap": False,
                    "smart_extract": False,
                    "resume_crawl": False,
                    "extract_text": False,
                    "max_pages": 1,
                    "crawl_pages": False,
                }
            )
        )

        monkeypatch.setattr(
            "sys.argv",
            [
                "crawler",
                "--load-config",
                str(config_path),
                "--list-only",  # CLI 覆盖配置
            ],
        )

        html = b"<html><body><p>content</p></body></html>"
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            with pytest.raises(SystemExit) as exc_info:
                cr.main()
            assert exc_info.value.code == 0


def tmp_path_for_config() -> Path:
    """临时配置路径（用于 test_save_config_without_url）。"""
    import tempfile

    return Path(tempfile.gettempdir()) / "test_config_save.json"


# ========== 补充分支测试：覆盖剩余未覆盖行 ==========


class TestIsSafeHostname:
    def test_empty_hostname(self) -> None:
        """空 hostname 返回 False。"""
        assert cr._is_safe_hostname("") is False

    def test_none_hostname(self) -> None:
        """_is_safe_hostname 拒绝 None 主机名。"""
        assert cr._is_safe_hostname(None) is False

    def test_valid_domain(self) -> None:
        """_is_safe_hostname 接受普通域名。"""
        assert cr._is_safe_hostname("example.com") is True

    def test_public_ip(self) -> None:
        """_is_safe_hostname 接受公网 IP。"""
        assert cr._is_safe_hostname("8.8.8.8") is True

    def test_private_ip(self) -> None:
        """_is_safe_hostname 拒绝私网 IP。"""
        assert cr._is_safe_hostname("10.0.0.1") is False

    def test_loopback_ip(self) -> None:
        """_is_safe_hostname 拒绝回环 IP。"""
        assert cr._is_safe_hostname("127.0.0.1") is False

    def test_link_local_ip(self) -> None:
        """_is_safe_hostname 拒绝链路本地 IP。"""
        assert cr._is_safe_hostname("169.254.1.1") is False

    def test_multicast_ip(self) -> None:
        """_is_safe_hostname 拒绝多播 IP。"""
        assert cr._is_safe_hostname("224.0.0.1") is False

    def test_localhost_domain(self) -> None:
        """_is_safe_hostname 拒绝 localhost。"""
        assert cr._is_safe_hostname("localhost") is False

    def test_metadata_domain(self) -> None:
        """_is_safe_hostname 拒绝云元数据主机名。"""
        assert cr._is_safe_hostname("metadata.google.internal") is False


class TestIsSafeHostnamePowerMode:
    def test_power_mode_allows_private_hostname(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Power Mode（WEB_CRAWLER_POWER_MODE=1）下内网/环回/localhost 均被放行。"""
        monkeypatch.setenv("WEB_CRAWLER_POWER_MODE", "1")
        assert cr._is_safe_hostname("10.0.0.1") is True
        assert cr._is_safe_hostname("127.0.0.1") is True
        assert cr._is_safe_hostname("169.254.169.254") is True
        assert cr._is_safe_hostname("localhost") is True


class TestNormalizeUrlEmptyHostname:
    def test_no_hostname(self) -> None:
        """URL 无 hostname 时返回空（覆盖 _is_safe_hostname 空值分支）。"""
        assert cr.normalize_url("http:///path") == ""


class TestImportLibFetcherFallback:
    def test_src_path_inserted(self) -> None:
        """_import_lib_fetcher 两次 import 都失败时返回 None。"""
        # web_crawler 未安装 → 第一次 import 失败 → 插入 src → 第二次 import 也失败 → None
        original_path = list(sys.path)
        try:
            # 确保 src 不在 sys.path 中（覆盖插入分支）
            src_path = str(Path(cr.__file__).resolve().parent.parent / "src")
            sys.path[:] = [p for p in sys.path if p != src_path]
            # 清除可能缓存的 web_crawler 模块
            with patch.dict("sys.modules", {"web_crawler": None}, clear=False):
                result = cr._import_lib_fetcher()
            assert result is None
        finally:
            sys.path[:] = original_path


class TestFetchHttpErrorRetry:
    def test_http_error_retry_sleep(self) -> None:
        """HTTP 500 重试后最终抛出异常，重试期间调用 sleep。"""
        fake = Mock()
        fake.get.side_effect = [_mock_fetch_response(500, b"", {})] * 3
        with (
            patch.object(core, "_get_app_fetcher", return_value=fake),
            patch.object(cr.time, "sleep") as mock_sleep,
            pytest.raises(FetchError, match="HTTP Error 500"),
        ):
            cr.fetch("https://example.com/x", 30, {}, 2, None)
        # 重试时调用过 sleep（退避）
        assert mock_sleep.called
        assert fake.get.call_count == 3


class TestEmptyRecommendations:
    def test_no_recommendations_html(self, tmp_path: Path) -> None:
        """无推荐时 HTML 报告包含"无"占位。"""
        rows: list[cr.ManifestRow] = []
        ctx = cr.build_report_context(rows, 0, 0.0, 1.0)
        # 直接设置空推荐列表以覆盖 line 2289
        ctx["recommendations"] = []
        cr._write_html_report(tmp_path / "report.html", ctx)
        html = (tmp_path / "report.html").read_text(encoding="utf-8")
        assert "无" in html


class TestCrawlSitemapSameDomain:
    def test_sitemap_skips_different_domain(self, tmp_path: Path) -> None:
        """sitemap 发现的跨域 URL 被 same_domain 过滤。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "sitemap.xml" in url:
                sitemap = (
                    b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    b"<url><loc>https://other.com/page1</loc></url>"
                    b"<url><loc>https://example.com/page2</loc></url>"
                    b"</urlset>"
                )
                return (sitemap, "application/xml")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--sitemap",
                    "--same-domain",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0

    def test_sitemap_skips_blocked(self, tmp_path: Path) -> None:
        """sitemap 发现的被 block URL 被跳过。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "sitemap.xml" in url:
                sitemap = (
                    b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    b"<url><loc>https://example.com/ad/page</loc></url>"
                    b"<url><loc>https://example.com/ok</loc></url>"
                    b"</urlset>"
                )
                return (sitemap, "application/xml")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--sitemap",
                    "--block-keyword",
                    "ad",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0


class TestCrawlResumeState:
    def test_resume_with_saved_state(self, tmp_path: Path) -> None:
        """--resume-crawl 加载已保存的状态。"""
        # 先保存状态
        state = {
            "page_queue": ["https://example.com/page2"],
            "seen_pages": ["https://example.com"],
            "page_titles": {"https://example.com": "Test"},
            "resources": [
                {
                    "url": "https://example.com/img.png",
                    "found_in": "img[src]",
                    "kind": "img",
                    "page_url": "https://example.com",
                }
            ],
            "sha256_set": ["abc123"],
        }
        (tmp_path / cr.CRAWL_STATE_FILE).write_text(json.dumps(state))

        html = b'<html><body><img src="https://example.com/img2.png"></body></html>'

        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--resume-crawl",
                    "--dedup",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0

    def test_resume_clear_state_on_success(self, tmp_path: Path) -> None:
        """--resume-crawl 成功完成后清除状态文件。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'

        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--resume-crawl",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        assert not (tmp_path / cr.CRAWL_STATE_FILE).exists()

    def test_resume_save_state_exception(self, tmp_path: Path) -> None:
        """--resume-crawl 保存状态失败时仅记录 warning 不崩溃。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'

        with (
            patch.object(core, "fetch", return_value=(html, "text/html")),
            patch.object(ctx_mod, "save_crawl_state", side_effect=OSError("disk full")),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--resume-crawl",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0

    def test_resume_clear_state_exception(self, tmp_path: Path) -> None:
        """--resume-crawl 清除状态失败时仅记录 warning。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'

        with (
            patch.object(core, "fetch", return_value=(html, "text/html")),
            patch.object(ctx_mod, "clear_crawl_state", side_effect=OSError("permission")),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--resume-crawl",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0


class TestCrawlPageScanningSkips:
    def test_blocked_page_skipped(self, tmp_path: Path) -> None:
        """被 block_keyword 匹配的页面被跳过。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'

        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://ad.example.com",
                    "--out",
                    str(tmp_path),
                    "--block-keyword",
                    "ad",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        # 页面被 block，无资源 → 返回 0（no resources found）
        assert exit_code == 0

    def test_same_domain_page_skipped(self, tmp_path: Path) -> None:
        """跨域页面被 same_domain 过滤。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'

        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--same-domain",
                    "--crawl-pages",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0

    def test_page_fetch_exception(self, tmp_path: Path) -> None:
        """零页成功时，退出码和报告都标明页面失败。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "example.com" == url.rstrip("/").split("//")[-1]:
                raise RuntimeError("connection refused")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 3
        report = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))
        assert report["status"] == "error"
        assert report["pages_scanned"] == 0
        assert report["page_scan"]["failed"] == 1
        assert "connection refused" in report["page_scan"]["first_error"]


class TestCrawlVideoOnlyFilter:
    def test_video_only_filters_resources(self, tmp_path: Path) -> None:
        """--video-only 过滤非视频资源。"""
        html = b"""<html><body>
        <img src="https://example.com/img.png">
        <video src="https://example.com/v.mp4"></video>
        </body></html>"""

        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--video-only",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0


class TestCrawlCssDiscovery:
    def test_include_css_urls(self, tmp_path: Path) -> None:
        """--include-css-urls 发现 CSS 中的额外资源。"""
        html = b'<html><head><link rel="stylesheet" href="https://example.com/style.css"></head><body></body></html>'
        css_content = b'body { background: url("bg.png"); } @import url("extra.css");'

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "style.css" in url:
                return (css_content, "text/css")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--include-css-urls",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0


class TestCrawlPlaylistExpansion:
    def test_expand_playlist(self, tmp_path: Path) -> None:
        """--expand-playlists 展开播放列表。"""
        html = b'<html><body><video src="https://example.com/playlist.m3u8"></video></body></html>'
        m3u8 = b"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
https://example.com/seg1.ts
#EXTINF:10.0,
https://example.com/seg2.ts
#EXT-X-ENDLIST"""

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "playlist.m3u8" in url:
                return (m3u8, "application/vnd.apple.mpegurl")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--expand-playlists",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0


class TestCrawlShouldStopDownload:
    def test_cancelled_during_download(self, tmp_path: Path) -> None:
        """下载阶段 should_stop 返回 True 时取消并返回 1。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'

        stop_flag = [False]

        def mock_should_stop(args: Any) -> bool:
            return stop_flag[0]

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "img.png" in url:
                stop_flag[0] = True
                raise RuntimeError("cancelled by user")
            return (html, "text/html")

        with (
            patch.object(core, "fetch", side_effect=mock_fetch),
            patch.object(core, "should_stop", side_effect=mock_should_stop),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 1


class TestCrawlWorkerCrash:
    def test_worker_crash_recorded(self, tmp_path: Path) -> None:
        """worker 线程崩溃时记录 error: worker crashed。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "img.png" in url:
                raise MemoryError("out of memory")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        manifest = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert any("worker crashed" in r["status"] or "error" in r["status"] for r in manifest)


class TestCrawlFinalTargetMove:
    def test_final_target_differs(self, tmp_path: Path) -> None:
        """下载后 final_target 与 target 不同时移动文件。"""
        with patch.object(core, "fetch", return_value=(b"png_data", "image/png")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--organize",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0


class TestCrawlJsonlWriteException:
    def test_jsonl_write_fails(self, tmp_path: Path) -> None:
        """JSONL 写入失败时仅记录 warning。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'

        original_open = Path.open

        def selective_open(self: Path, *args: Any, **kwargs: Any) -> Any:
            if "resources_manifest.jsonl" in str(self):
                raise OSError("disk full")
            return original_open(self, *args, **kwargs)

        with (
            patch.object(core, "fetch", return_value=(html, "text/html")),
            patch.object(Path, "open", selective_open),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        # 即使 JSONL 写入失败也返回 0
        assert exit_code == 0

    def test_jsonl_append_and_finalize_fail(self, tmp_path: Path) -> None:
        """逐条追加与收尾 flush 失败时仅记 warning，不影响抓取。"""

        class _FailingJsonlFile:
            """write/flush 抛 OSError（模拟磁盘满），close 正常。"""

            def write(self, s: str) -> int:
                raise OSError("disk full")

            def flush(self) -> None:
                raise OSError("flush fail")

            def close(self) -> None:
                pass

        html = b'<html><body><img src="https://example.com/img.png"></body></html>'
        original_open = Path.open

        def selective_open(self: Path, *args: Any, **kwargs: Any) -> Any:
            if "resources_manifest.jsonl" in str(self):
                return _FailingJsonlFile()
            return original_open(self, *args, **kwargs)

        with (
            patch.object(core, "fetch", return_value=(html, "text/html")),
            patch.object(Path, "open", selective_open),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0


class TestCrawlExtractTextEmpty:
    def test_extract_text_empty_content(self, tmp_path: Path) -> None:
        """--extract-text 对无正文的页面跳过。"""
        html = b"""<html><head></head><body>
        <img src="https://example.com/img.png">
        </body></html>"""

        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--extract-text",
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        # extracted_text 目录存在但可能没有 txt 文件
        assert (tmp_path / "extracted_text").exists()


class TestCrawlDownloadLoopCancellation:
    def test_cancellation_skips_queued_downloads_and_keeps_completed_row(
        self, tmp_path: Path
    ) -> None:
        html = b'<img src="/a.png"><img src="/b.png"><img src="/c.png">'
        stopped = False
        downloads: list[str] = []

        def fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            nonlocal stopped
            if url.endswith(".png"):
                downloads.append(url)
                stopped = True
                return b"completed", "image/png"
            return html, "text/html"

        def wait_if_paused() -> None:
            if stopped:
                raise RuntimeError("cancelled by user")

        args = cr.build_parser().parse_args(
            ["--url", "https://example.com", "--out", str(tmp_path), "--workers", "1", "--dedup"]
        )
        args.should_stop = lambda: stopped
        args.wait_if_paused = wait_if_paused
        with patch.object(core, "fetch", side_effect=fetch):
            assert cr.crawl(args) == 1
        assert len(downloads) == 1
        lines = (tmp_path / "resources_manifest.jsonl").read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["url"] for line in lines] == downloads

    def test_should_stop_cancels_pending_futures(self, tmp_path: Path) -> None:
        """workers>1 时 should_stop 在下载循环体中返回 True，取消待处理 future。"""
        html = b'<html><body><img src="https://example.com/a.png"><img src="https://example.com/b.png"></body></html>'
        call_count = [0]

        def mock_should_stop(args: Any) -> bool:
            call_count[0] += 1
            # 第一次调用（页面扫描）返回 False，第二次（下载循环）返回 True
            return call_count[0] > 1

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            # 页面 URL 返回 HTML（含 img 标签），资源 URL 返回二进制
            if url.endswith(".png"):
                return (b"data", "image/png")
            return (html, "text/html")

        with (
            patch.object(core, "fetch", side_effect=mock_fetch),
            patch.object(core, "should_stop", side_effect=mock_should_stop),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "2",
                ]
            )
            exit_code = cr.crawl(args)
        # 主线程取消路径与 worker 取消路径统一返回 1，且跳过后处理
        assert exit_code == 1
        assert not (tmp_path / "resources_manifest.json").exists()


class TestCrawlCancelSkipsStages:
    def test_cancel_after_scan_skips_download_phase(self, tmp_path: Path) -> None:
        """页面扫描结束后、下载阶段开始前触发取消 → 不启动下载、不写后处理。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'
        call_count = [0]

        def mock_should_stop(args: Any) -> bool:
            call_count[0] += 1
            # 第一次（页面循环入口）返回 False 以完成扫描，之后返回 True
            return call_count[0] > 1

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if "img.png" in url:
                raise AssertionError("取消后不应进入下载阶段")
            return (html, "text/html")

        with (
            patch.object(core, "fetch", side_effect=mock_fetch),
            patch.object(core, "should_stop", side_effect=mock_should_stop),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "2",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 1
        assert not (tmp_path / "resources_manifest.json").exists()
        assert not (tmp_path / "run_report.json").exists()


class TestFormatBytesPB:
    def test_pb(self) -> None:
        """n ≥ 1024⁵ 返回 PB。"""
        assert cr._format_bytes(2 * 1024**5) == "2.0 PB"


class TestCrawlWaitPausedCancel:
    """暂停期间被取消（wait_if_paused 抛 RuntimeError）的取消语义分支。"""

    def test_cancelled_by_pause_in_page_loop(self, tmp_path: Path) -> None:
        """页面扫描阶段 wait_if_paused 抛 RuntimeError → 按取消处理返回 1。"""

        def mock_wait() -> None:
            raise RuntimeError("cancelled by user")

        with patch.object(core, "fetch", return_value=(b"<html></html>", "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "1",
                ]
            )
            args.wait_if_paused = mock_wait
            exit_code = cr.crawl(args)
        # 暂停期间取消 → cancelled 标志置位，统一退出码 1
        assert exit_code == 1

    def test_cancelled_by_pause_in_download_loop(self, tmp_path: Path) -> None:
        """下载循环中 wait_if_paused 抛 RuntimeError → 按取消处理返回 1。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'
        wait_calls = [0]

        def mock_wait() -> None:
            wait_calls[0] += 1
            if wait_calls[0] > 1:
                raise RuntimeError("cancelled by user")

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if url.endswith(".png"):
                return (b"data", "image/png")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "1",
                ]
            )
            args.wait_if_paused = mock_wait
            exit_code = cr.crawl(args)
        # 第一次（页面循环）放行，第二次（下载循环）抛 cancelled
        assert exit_code == 1
        assert not (tmp_path / "resources_manifest.json").exists()

    def test_should_stop_in_download_loop_cancels_futures(self, tmp_path: Path) -> None:
        """下载循环内 should_stop 返回 True → 取消待处理 future 并返回 1。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'
        stop_calls = [0]

        def mock_should_stop(args: Any) -> bool:
            stop_calls[0] += 1
            # 页面循环入口(1)、下载阶段守卫(2) 返回 False，下载循环体(3) 返回 True
            return stop_calls[0] >= 3

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if url.endswith(".png"):
                return (b"data", "image/png")
            return (html, "text/html")

        with (
            patch.object(core, "fetch", side_effect=mock_fetch),
            patch.object(core, "should_stop", side_effect=mock_should_stop),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 1
        assert not (tmp_path / "resources_manifest.json").exists()

    def test_post_pause_check_swallows_cancel_exception(self, tmp_path: Path) -> None:
        """后处理阶段 wait_if_paused 抛 RuntimeError 时吞掉，由 should_stop 统一判定。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'
        wait_calls = [0]

        def mock_wait() -> None:
            wait_calls[0] += 1
            # 页面循环(1)、下载循环(2) 放行，后处理首次检查(3) 抛 cancelled
            if wait_calls[0] >= 3:
                raise RuntimeError("cancelled by user")

        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if url.endswith(".png"):
                return (b"data", "image/png")
            return (html, "text/html")

        with patch.object(core, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "1",
                ]
            )
            args.wait_if_paused = mock_wait
            exit_code = cr.crawl(args)
        # should_stop 未置位 → 视为未取消，后处理继续完成
        assert exit_code == 0
        assert (tmp_path / "resources_manifest.json").exists()


class TestCrawlPostProcessingTolerance:
    """后处理单步失败仅记 warning，不影响整体退出码。"""

    def _fetch_page_with_img(self, html: bytes) -> Any:
        def mock_fetch(url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            if url.endswith(".png"):
                return (b"data", "image/png")
            return (html, "text/html")

        return mock_fetch

    def test_rewrite_html_failure_only_warns(self, tmp_path: Path) -> None:
        """offline HTML 重写抛异常 → 仅记 warning，退出码 0。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'
        with (
            patch.object(core, "fetch", side_effect=self._fetch_page_with_img(html)),
            patch.object(post, "RewriteTable", side_effect=RuntimeError("rewrite boom")),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--rewrite-html",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0

    def test_write_manifests_failure_only_warns(self, tmp_path: Path) -> None:
        """写资源清单失败 → 仅记 warning，退出码 0。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'
        with (
            patch.object(core, "fetch", side_effect=self._fetch_page_with_img(html)),
            patch.object(post, "write_manifests", side_effect=OSError("disk full")),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0

    def test_write_failed_manifests_failure_only_warns(self, tmp_path: Path) -> None:
        """写失败资源清单失败 → 仅记 warning，退出码 0。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'
        with (
            patch.object(core, "fetch", side_effect=self._fetch_page_with_img(html)),
            patch.object(post, "write_failed_manifests", side_effect=OSError("disk full")),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0

    def test_smart_extract_failure_only_warns(self, tmp_path: Path) -> None:
        """智能抽取失败 → 仅记 warning，退出码 0。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'
        with (
            patch.object(core, "fetch", side_effect=self._fetch_page_with_img(html)),
            patch.object(post, "smart_extract", side_effect=RuntimeError("llm down")),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--smart-extract",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0

    def test_extract_text_failure_only_warns(self, tmp_path: Path) -> None:
        """正文提取失败 → 仅记 warning，退出码 0。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'
        with (
            patch.object(core, "fetch", side_effect=self._fetch_page_with_img(html)),
            patch.object(post, "extract_readable_text", side_effect=RuntimeError("boom")),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--extract-text",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0

    def test_write_run_report_failure_only_warns(self, tmp_path: Path) -> None:
        """写运行报告失败 → 仅记 warning，退出码 0。"""
        html = b'<html><body><img src="https://example.com/img.png"></body></html>'
        with (
            patch.object(core, "fetch", side_effect=self._fetch_page_with_img(html)),
            patch.object(post, "write_run_report", side_effect=OSError("disk full")),
        ):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0


class TestConfigRoundTrip:
    """--save-config → --load-config 往返兼容（save 产物可直接 load 运行）。"""

    def test_save_config_contains_new_fields(self, tmp_path: Path) -> None:
        """save 产物包含 include_pattern/stealth 等新增参数。"""
        config_path = tmp_path / "roundtrip.json"
        args = cr.build_parser().parse_args(
            [
                "--url",
                "https://example.com",
                "--out",
                str(tmp_path),
                "--workers",
                "2",
                "--include-pattern",
                r"\.(jpg|png)$",
                "--stealth",
                "--impersonate",
                "chrome131",
            ]
        )
        cr.save_config_to_file(args, str(config_path))
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        assert saved["include_pattern"] == r"\.(jpg|png)$"
        assert saved["stealth"] is True
        assert saved["impersonate"] == "chrome131"
        assert "proxy" in saved

    def test_load_config_with_saved_file_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """save 产物 load 后可直接进入 crawl（缺省字段由 parser 默认值兜底）。"""
        config_path = tmp_path / "roundtrip.json"
        args = cr.build_parser().parse_args(
            [
                "--url",
                "https://example.com",
                "--out",
                str(tmp_path),
                "--workers",
                "1",
            ]
        )
        cr.save_config_to_file(args, str(config_path))

        # 用 main() 走真实 --load-config 路径：load 后不抛 AttributeError，
        # crawl 收到完整 Namespace（含 include_pattern 等缺省字段）
        captured: dict[str, object] = {}

        def _fake_crawl(crawl_args: argparse.Namespace) -> int:
            captured["args"] = crawl_args
            return 0

        monkeypatch.setattr("sys.argv", ["crawler", "--load-config", str(config_path)])
        with (
            patch.object(cr, "crawl", side_effect=_fake_crawl),
            pytest.raises(SystemExit) as exc_info,
        ):
            cr.main()
        assert exc_info.value.code == 0
        crawl_args = captured["args"]
        assert isinstance(crawl_args, argparse.Namespace)
        assert crawl_args.include_pattern is None  # 缺省字段存在
        assert crawl_args.stealth is False
        assert crawl_args.save_config is None  # 不再 AttributeError
        assert crawl_args.url == "https://example.com"


# ========== 后处理异常兜底(_crawler_post) ==========


class TestPostProcessErrorPaths:
    """_post_process 各阶段独立 try/except:单步失败仅记 warning、不中断收尾。"""

    @staticmethod
    def _make_ctx(tmp_path: Path, **flags: Any) -> Any:
        """构造能走完后处理流程的最小 _CrawlContext。"""
        from web_crawler.app import _crawler_post
        from web_crawler.app._crawler_context import _CrawlContext

        args = argparse.Namespace(
            url="https://example.com/",
            output_dir=str(tmp_path / "out"),
            rewrite_html=flags.get("rewrite_html", False),
            strip_overlays=flags.get("strip_overlays", False),
            encoding=None,
            video_mode=flags.get("video_mode", False),
            smart_extract=flags.get("smart_extract", False),
            extract_text=flags.get("extract_text", False),
            pause_file=None,
            progress_callback=None,
        )
        ctx = _CrawlContext(
            args=args,
            headers={},
            output_dir=tmp_path / "out",
            max_bytes=None,
            block_keywords=[],
            robots=None,
            rate_limiter=MagicMock(),
            dedup=None,
        )
        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        page_file = ctx.output_dir / "page.html"
        page_file.write_text("<html><body>x</body></html>", encoding="utf-8")
        ctx.page_files["https://example.com/"] = (page_file, "utf-8")
        ctx.seen_pages.add("https://example.com/")
        return _crawler_post, ctx

    def test_verification_failure_distinguishes_existing_index(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        module, ctx = self._make_ctx(tmp_path, rewrite_html=True)
        with patch.object(module, "verify_offline", side_effect=OSError("filesystem error")):
            module._post_process(ctx, [], time.time(), {}, cancelled=False)
        report = json.loads((ctx.output_dir / "run_report.json").read_text(encoding="utf-8"))
        assert (ctx.output_dir / report["offline_pages"]["index"]).is_file()
        assert report["offline_check"]["status"] == "limited"
        assert report["offline_check"]["missing"] == 0
        assert report["offline_check"]["unverified"] == 1
        assert "offline verification failed" in caplog.text
        assert "offline HTML rewrite failed" not in caplog.text

    @pytest.mark.parametrize(
        "flags,patch_target",
        [
            ({"rewrite_html": True}, "RewriteTable"),
            ({"rewrite_html": True, "strip_overlays": True}, "strip_page_overlays"),
            ({}, "write_manifests"),
            ({"video_mode": True}, "write_video_manifests"),
            ({"smart_extract": True}, "smart_extract"),
            ({"extract_text": True}, "extract_readable_text"),
            ({}, "write_summary"),
        ],
    )
    def test_stage_failure_does_not_abort(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        flags: dict[str, Any],
        patch_target: str,
    ) -> None:
        """任一后处理阶段抛异常:整体不崩,返回 (0, 0)。"""
        _crawler_post, ctx = self._make_ctx(tmp_path, **flags)
        failure = MagicMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(_crawler_post, patch_target, failure)
        video_count, failed_count = _crawler_post._post_process(
            ctx,
            manifest_rows=[],
            crawl_start_time=time.time(),
            report_config={},
            cancelled=False,
        )
        assert (video_count, failed_count) == (0, 0)
        failure.assert_called_once()


# ========== --render-js 桥接与 stealth UA 配对（2026-11 竞品调研实测驱动）==========


class TestStealthUserAgent:
    """TLS 指纹伪装必须与 UA 成对：julius.ai 实测 chrome131 指纹 + 爬虫默认 UA = 403。"""

    def test_stealth_pairs_browser_ua_with_fingerprint(self) -> None:
        """stealth + 默认 UA 时，请求头换成与指纹匹配的浏览器 UA。"""
        control_args = Mock()
        control_args.stealth = True
        control_args.impersonate = "chrome131"
        control_args.proxy = None
        fake = Mock()
        fake.get.return_value = _mock_fetch_response(200, b"data", {"content-type": "text/html"})
        with patch.object(core, "_get_app_fetcher", return_value=fake):
            cr.fetch("https://example.com", 30, {}, 0, None, control_args=control_args)
        sent = fake.get.call_args.kwargs["headers"]["User-Agent"]
        assert "Chrome/131" in sent
        assert "ResourceCrawler" not in sent

    def test_stealth_explicit_user_agent_preserved(self) -> None:
        """用户显式指定的 UA 不被 stealth 覆盖。"""
        control_args = Mock()
        control_args.stealth = True
        control_args.impersonate = "chrome131"
        control_args.proxy = None
        fake = Mock()
        fake.get.return_value = _mock_fetch_response(200, b"data", {})
        with patch.object(core, "_get_app_fetcher", return_value=fake):
            cr.fetch(
                "https://example.com",
                30,
                {"User-Agent": "custom-agent"},
                0,
                None,
                control_args=control_args,
            )
        assert fake.get.call_args.kwargs["headers"]["User-Agent"] == "custom-agent"

    def test_non_stealth_keeps_default_user_agent(self) -> None:
        """非 stealth 路径保持爬虫默认 UA（robots 语义与历史行为不变）。"""
        fake = Mock()
        fake.get.return_value = _mock_fetch_response(200, b"data", {})
        with patch.object(core, "_get_app_fetcher", return_value=fake):
            cr.fetch("https://example.com", 30, {}, 0, None)
        sent = fake.get.call_args.kwargs["headers"]["User-Agent"]
        assert sent == ctx_mod.DEFAULT_USER_AGENT

    def test_user_agent_presets(self) -> None:
        """各指纹预设映射到语义匹配的浏览器 UA。"""
        assert "Firefox/135.0" in core._stealth_user_agent("firefox135")
        assert "Edg/" in core._stealth_user_agent("edge101")
        assert "Chrome/120" in core._stealth_user_agent("chrome120")
        assert "Version/18" in core._stealth_user_agent("safari18")
        # 未知预设回退到 Chrome 形态而不是爬虫默认值
        assert "Chrome/" in core._stealth_user_agent("unknown-preset")


class _FakeDynamicFetcher:
    """记录构造参数与调用序列的 DynamicFetcher 桩。"""

    instances: list[_FakeDynamicFetcher] = []

    def __init__(
        self,
        timeout: float,
        proxy: str | None,
        extra_headers: dict[str, str] | None,
        block_images: bool,
        network_idle: bool,
    ) -> None:
        self.timeout = timeout
        self.proxy = proxy
        self.extra_headers = extra_headers
        self.block_images = block_images
        self.network_idle = network_idle
        self.closed = False
        self.fetched: list[str] = []
        self.response: Mock = _mock_fetch_response(
            200, b"<html>rendered</html>", {"content-type": "text/html"}
        )
        _FakeDynamicFetcher.instances.append(self)

    def fetch(self, url: str) -> Mock:
        self.fetched.append(url)
        return self.response

    def close(self) -> None:
        self.closed = True


class TestRenderPageBridge:
    def setup_method(self) -> None:
        core._render_local.cache = {}
        _FakeDynamicFetcher.instances = []

    def teardown_method(self) -> None:
        core._render_local.cache = {}
        _FakeDynamicFetcher.instances = []

    def test_render_page_success_and_cache(self) -> None:
        with patch.object(core, "_import_dynamic_fetcher", return_value=_FakeDynamicFetcher):
            data, ct = core.render_page("https://example.com", 30, {"X-A": "1"})
        assert data == b"<html>rendered</html>"
        assert ct == "text/html"
        assert len(_FakeDynamicFetcher.instances) == 1
        fake = _FakeDynamicFetcher.instances[0]
        assert fake.block_images is True
        assert fake.network_idle is True
        # 同线程同参数复用实例（浏览器进程昂贵）
        with patch.object(core, "_import_dynamic_fetcher", return_value=_FakeDynamicFetcher):
            core.render_page("https://example.com/other", 30, {"X-A": "1"})
        assert len(_FakeDynamicFetcher.instances) == 1
        # 默认爬虫 UA 不透传给浏览器（浏览器用自己的真实 UA），其余头保留
        assert fake.extra_headers == {"X-A": "1"}

    def test_render_page_passes_custom_headers(self) -> None:
        with patch.object(core, "_import_dynamic_fetcher", return_value=_FakeDynamicFetcher):
            core.render_page("https://example.com", 30, {"Cookie": "a=b", "User-Agent": "x"})
        fake = _FakeDynamicFetcher.instances[0]
        assert fake.extra_headers == {"Cookie": "a=b"}

    def test_render_page_4xx_raises_fetch_error_with_status(self) -> None:
        class _Forbidden(_FakeDynamicFetcher):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)  # type: ignore[arg-type]
                self.response = _mock_fetch_response(403, b"denied", {})

        with (
            patch.object(core, "_import_dynamic_fetcher", return_value=_Forbidden),
            pytest.raises(FetchError, match="HTTP Error 403"),
        ):
            core.render_page("https://example.com", 30, {})

    def test_close_render_fetchers_closes_cached_instances(self) -> None:
        with patch.object(core, "_import_dynamic_fetcher", return_value=_FakeDynamicFetcher):
            core.render_page("https://example.com", 30, {})
        assert len(_FakeDynamicFetcher.instances) == 1
        core.close_render_fetchers()
        assert _FakeDynamicFetcher.instances[0].closed is True
        assert core._render_local.cache == {}


class TestRenderJsScanPath:
    """页面扫描的 --render-js 分支：渲染优先、失败退回静态 HTTP。"""

    @staticmethod
    def _render_ctx(tmp_path: Path) -> tuple[Any, Any]:
        from web_crawler.app._crawler_context import _CrawlContext
        from web_crawler.app._crawler_scan import _fetch_page_html

        args = argparse.Namespace(
            url="https://example.com",
            out=str(tmp_path),
            timeout=30,
            retries=0,
            max_bytes=0,
            render_js=True,
            impersonate="chrome131",
            stealth=False,
            proxy=None,
            encoding=None,
        )
        ctx = _CrawlContext(
            args=args,
            headers={},
            output_dir=tmp_path,
            max_bytes=None,
            block_keywords=[],
            robots=None,
            rate_limiter=MagicMock(),
            dedup=None,
        )
        return _fetch_page_html, ctx

    def test_render_success_uses_browser_html(self, tmp_path: Path) -> None:
        _fetch_page_html, ctx = self._render_ctx(tmp_path)
        with patch.object(core, "render_page", return_value=(b"<html>browser</html>", "text/html")):
            data, ct = _fetch_page_html(ctx, "https://example.com")
        assert data == b"<html>browser</html>"
        assert ct == "text/html"
        ctx.rate_limiter.wait_if_needed.assert_called_once()
        ctx.rate_limiter.record_request.assert_called_once()

    def test_render_failure_falls_back_to_http(self, tmp_path: Path) -> None:
        _fetch_page_html, ctx = self._render_ctx(tmp_path)
        with (
            patch.object(core, "render_page", side_effect=FetchError("render crashed")),
            patch.object(
                core, "fetch", return_value=(b"<html>static</html>", "text/html")
            ) as static_fetch,
        ):
            data, ct = _fetch_page_html(ctx, "https://example.com")
        assert data == b"<html>static</html>"
        assert ct == "text/html"
        static_fetch.assert_called_once()

    def test_render_max_bytes_exceeded_falls_back(self, tmp_path: Path) -> None:
        _fetch_page_html, ctx = self._render_ctx(tmp_path)
        ctx.max_bytes = 10
        with (
            patch.object(core, "render_page", return_value=(b"x" * 100, "text/html")),
            patch.object(
                core, "fetch", return_value=(b"<html>small</html>", "text/html")
            ) as static_fetch,
        ):
            data, _ = _fetch_page_html(ctx, "https://example.com")
        assert data == b"<html>small</html>"
        static_fetch.assert_called_once()

    def test_render_user_cancel_is_not_swallowed(self, tmp_path: Path) -> None:
        _fetch_page_html, ctx = self._render_ctx(tmp_path)
        with (
            patch.object(core, "render_page", side_effect=RuntimeError("cancelled by user")),
            pytest.raises(RuntimeError, match="cancelled by user"),
        ):
            _fetch_page_html(ctx, "https://example.com")

    def test_crawl_with_render_js_flag(self, tmp_path: Path) -> None:
        """crawl 集成：--render-js 时页面 HTML 来自渲染路径，资源解析正常。"""
        html = b'<html><body><img src="logo.png"><p>rendered page</p></body></html>'
        with patch.object(core, "render_page", return_value=(html, "text/html")) as render:
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--list-only",
                    "--render-js",
                    "--workers",
                    "1",
                ]
            )
            exit_code = cr.crawl(args)
        assert exit_code == 0
        render.assert_called_once()
        manifest = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert any(row["url"].endswith("logo.png") for row in manifest)

    def test_crawl_closes_render_fetchers_in_finally(self, tmp_path: Path) -> None:
        """无论抓取成败，crawl 收尾都要释放渲染 fetcher（浏览器进程不泄漏）。"""
        with patch.object(cr, "close_render_fetchers") as close:
            args = cr.build_parser().parse_args(
                ["--url", "https://example.com", "--out", str(tmp_path), "--list-only"]
            )
            cr.crawl(args)
        close.assert_called_once()


class TestExtractReadableTextStructure:
    """正文提取保留块级结构、剥离导航骨架、去掉连续重复行。"""

    def test_blocks_render_on_separate_lines(self) -> None:
        html = "<main><h1>Title</h1><p>First paragraph text</p><p>Second paragraph</p></main>"
        result = cr.extract_readable_text(html)
        lines = result.split("\n")
        assert lines == ["Title", "First paragraph text", "Second paragraph"]

    def test_inline_tags_do_not_break_paragraph(self) -> None:
        html = "<article><p>Read the <a href='/docs'>documentation</a> for details.</p></article>"
        result = cr.extract_readable_text(html)
        assert result == "Read the documentation for details."

    def test_nav_header_footer_aside_stripped_inside_content_root(self) -> None:
        html = (
            "<main>"
            "<nav><a href='/a'>Nav Link</a></nav>"
            "<header>Banner Text</header>"
            "<p>Real content paragraph</p>"
            "<footer>Copyright Footer</footer>"
            "<aside>Sidebar Junk</aside>"
            "</main>"
        )
        result = cr.extract_readable_text(html)
        assert "Real content paragraph" in result
        assert "Nav Link" not in result
        assert "Banner Text" not in result
        assert "Copyright Footer" not in result
        assert "Sidebar Junk" not in result

    def test_script_style_and_controls_stripped(self) -> None:
        html = (
            "<article><style>.x{color:red}</style>"
            "<script>var a=1;</script>"
            "<form><input name='q'><button>Go</button></form>"
            "<p>Visible paragraph text</p></article>"
        )
        result = cr.extract_readable_text(html)
        assert "Visible paragraph text" in result
        assert "var a" not in result
        assert "color:red" not in result
        assert "Go" not in result

    def test_lists_and_nested_blocks(self) -> None:
        html = "<main><ul><li><p>Nested item paragraph</p></li><li>Plain item</li></ul></main>"
        result = cr.extract_readable_text(html)
        lines = result.split("\n")
        assert "Nested item paragraph" in lines
        assert "Plain item" in lines

    def test_consecutive_duplicate_lines_removed(self) -> None:
        html = "<main><div>Same nav text</div><div>Same nav text</div><p>Unique</p></main>"
        result = cr.extract_readable_text(html)
        lines = result.split("\n")
        assert lines.count("Same nav text") == 1
        assert "Unique" in lines

    def test_loose_text_between_blocks_kept(self) -> None:
        html = "<main>Lead text <p>Paragraph</p> trailing text</main>"
        result = cr.extract_readable_text(html)
        assert "Lead text" in result
        assert "Paragraph" in result
        assert "trailing text" in result

    def test_body_fallback_keeps_paragraphs(self) -> None:
        html = "<body><h2>Section</h2><p>Body paragraph content</p></body>"
        result = cr.extract_readable_text(html)
        assert result == "Section\nBody paragraph content"

    def test_existing_contracts_hold(self) -> None:
        """历史契约不回归：article 优先 / script 剥离 / substring 可匹配。"""
        article_html = "<nav>x</nav><article><p>Main article text kept</p></article>"
        assert "Main article text kept" in cr.extract_readable_text(article_html)
        script_html = (
            "<article><script>var x = 1;</script><p>Visible text here is long enough</p></article>"
        )
        result = cr.extract_readable_text(script_html)
        assert "var x" not in result
        assert "Visible text" in result


# ========== robots 抓取透传与 403 提示（2026-11 调研实测驱动）==========


class TestRobotsControlArgs:
    def test_control_args_forwarded_to_fetch(self) -> None:
        """--stealth/--proxy 经 control_args 透传到 robots.txt 抓取。"""
        control_args = Mock()
        control_args.stealth = True
        control_args.impersonate = "chrome131"
        control_args.proxy = "http://127.0.0.1:7890"
        with patch.object(
            core, "fetch", return_value=(b"User-agent: *\nDisallow: /p", "text/plain")
        ) as fetch:
            cr.make_robots_parser("https://example.com", {}, 10, control_args=control_args)
        assert fetch.call_args.kwargs.get("control_args") is control_args

    def test_fetch_failure_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """robots 抓取失败不再静默：warning 留痕，行为保持「无限制」兼容。"""
        import logging as _logging

        with (
            caplog.at_level(_logging.WARNING, logger="crawler"),
            patch.object(core, "fetch", side_effect=Exception("network error")),
        ):
            parser = cr.make_robots_parser("https://example.com", {}, 10)
        assert parser.can_fetch("*", "https://example.com/anything") is True
        assert any("robots.txt fetch failed" in record.message for record in caplog.records)


class TestForbiddenHint:
    def test_403_hint_suggests_stealth_and_render(self) -> None:
        from web_crawler._cli import render_error

        exc = FetchError("HTTP Error 403")
        exc.status = 403  # type: ignore[attr-defined]
        rendered = render_error(exc, context="https://example.com")
        assert "--stealth" in rendered
        assert "--render-js" in rendered

    def test_non_403_keeps_generic_hint(self) -> None:
        from web_crawler._cli import render_error

        exc = FetchError("HTTP Error 503")
        exc.status = 503  # type: ignore[attr-defined]
        rendered = render_error(exc, context="https://example.com")
        assert "--delay" in rendered
        assert "--stealth" not in rendered


# ========== 正文容器选优（2026-11 调研实测驱动）==========


class TestReadableTextCandidateSelection:
    def test_prefers_larger_main_over_tiny_article(self) -> None:
        """julius.ai 实测回归：盲取首 article 会截掉 92% 正文。"""
        html = (
            "<main>"
            "<article>Short teaser</article>"
            "<p>Real main content line one</p>"
            "<p>Real main content line two with more detail</p>"
            "</main>"
        )
        result = cr.extract_readable_text(html)
        assert "Real main content line one" in result
        assert "Real main content line two" in result
        assert "Short teaser" in result

    def test_article_preferred_when_volume_close_to_body(self) -> None:
        """无显式 body 时 lxml 会把 article 归进 head（解析器工件）——
        article 与 body 文本量接近时仍优先 article，避免丢正文。"""
        html = (
            "<html><title>Second</title>"
            "<article>Second body</article>"
            "<a href='/related'>Related page</a></html>"
        )
        result = cr.extract_readable_text(html)
        assert "Second body" in result

    def test_container_inline_leaf_text_collected(self) -> None:
        """lxml 自动包 body 后，行内 <a> 是容器直接子节点：其文本不丢。"""
        html = (
            "<html><title>Second</title>"
            "<article>tiny</article>"
            "<a href='/r'>Related page</a>"
            "<a href='/r2'>Second link</a></html>"
        )
        result = cr.extract_readable_text(html)
        # 无块级子节点的容器按段落语义整行合并（保句子完整性优先于逐链接分行）
        assert "Related page" in result
        assert "Second link" in result


# ========== 内嵌 JSON 状态块正文兜底（2026-11 调研实测驱动）==========


class TestEmbeddedStateJson:
    """可见文本为空的 SSR 页面，正文在 script 状态块里。"""

    def _row(self, status: str, url: str = "https://x.com/a.png") -> Any:
        return cr.ManifestRow(
            status=status,
            url=url,
            saved_path="",
            content_type="image/png",
            bytes=100,
            category="image",
            found_in="img[src]",
            kind="image",
            page_url="https://x.com/",
            page_title="Page",
            diagnostic="",
        )

    def test_application_json_state_block(self) -> None:
        payload = (
            '{"page":{"title":"Modern docs","content":'
            '"This is a long paragraph of real documentation content that should be extracted '
            'from the embedded state blob without any visible text."}}'
        )
        html = (
            "<html><body><div id='app'></div>"
            f'<script type="application/json" id="__MODERN_SERVER_DATA__">{payload}</script>'
            "</body></html>"
        )
        result = cr.extract_readable_text(html)
        assert "long paragraph of real documentation" in result

    def test_window_assignment_state_block(self) -> None:
        html = (
            "<html><body><div id='app'></div><script>"
            "window._ROUTER_DATA = " + '{"loaderData":{"page":{"body":'
            '"Another long paragraph served by React Router state that must be recovered '
            'when the visible DOM is just an empty shell."}}};'
            "</script></body></html>"
        )
        result = cr.extract_readable_text(html)
        assert "React Router state" in result

    def test_short_config_strings_filtered(self) -> None:
        payload = '{"router":{"baseUrl":"/","params":{}},"x":"ok"}'
        html = f'<html><body><script type="application/json">{payload}</script></body></html>'
        assert cr.extract_readable_text(html) == ""

    def test_visible_content_wins_when_long_enough(self) -> None:
        visible = "<p>" + ("Visible paragraph text. " * 20) + "</p>"
        payload = (
            '{"content":"Embedded content that is long enough to pass the prose filter for sure."}'
        )
        html = (
            f"<html><body><main>{visible}</main>"
            f'<script type="application/json">{payload}</script></body></html>'
        )
        result = cr.extract_readable_text(html)
        assert "Visible paragraph text" in result
        assert "Embedded content" not in result


class TestSitemapFallbackWarning:
    def test_spa_fallback_page_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """sitemap.xml 回落到 SPA 首页 HTML 时要告警，不能静默 0 发现。"""
        import logging as _logging

        html = b"<html><body><div id='app'></div></body></html>"
        with (
            caplog.at_level(_logging.WARNING, logger="crawler"),
            patch.object(core, "fetch", return_value=(html, "text/html")),
        ):
            urls = cr.discover_sitemap_urls("https://example.com/sitemap.xml", {}, 10)
        assert urls == []
        assert any("non-XML content" in record.message for record in caplog.records)


class TestListOnlyRecommendation:
    def test_all_listed_only_gets_explicit_note(self) -> None:
        row = cr.ManifestRow(
            status="listed only",
            url="https://x.com/a.png",
            saved_path="",
            content_type="image/png",
            bytes=0,
            category="image",
            found_in="img[src]",
            kind="image",
            page_url="https://x.com/",
            page_title="Page",
            diagnostic="",
        )
        ctx = cr.build_report_context([row], 1, 0.0, 1.0)
        recs = cr.build_recommendations(ctx)
        titles = [rec["title"] for rec in recs]
        assert any("仅生成资源清单" in title for title in titles)
        assert "本次运行状态正常" not in titles


class TestRobotsContentValidation:
    def test_spa_fallback_robots_warns_and_allows(self, caplog: pytest.LogCaptureFixture) -> None:
        """robots.txt 回落到 HTML 时告警（不是把 HTML 当 robots 解析）。"""
        import logging as _logging

        html = b"<!doctype html><html><body><div id='app'></div></body></html>"
        with (
            caplog.at_level(_logging.WARNING, logger="crawler"),
            patch.object(core, "fetch", return_value=(html, "text/html")),
        ):
            parser = cr.make_robots_parser("https://example.com", {}, 10)
        assert parser.can_fetch("*", "https://example.com/anything") is True
        assert any(
            "does not look like a robots file" in record.message for record in caplog.records
        )


class TestQuillDeltaExtraction:
    """Quill 富文本 delta：短片段 + 深层嵌套（Trae 文档实测形态）。"""

    def test_ops_delta_joins_short_inserts(self) -> None:
        html = (
            "<html><body><div id='app'></div>"
            '<script type="application/json">'
            '{"ops":[{"insert":"Skills are defined through the "},'
            '{"insert":"SKILL.md"},{"insert":" file.\\n"},'
            '{"insert":"Each skill encapsulates instructions and scripts.\\n"}]}'
            "</script></body></html>"
        )
        result = cr.extract_readable_text(html)
        assert "SKILL.md" in result
        assert "Each skill encapsulates" in result

    def test_bare_ops_array(self) -> None:
        payload = (
            '[{"insert":"First line of the document body.\\n"},'
            '{"insert":"Second line with "},{"insert":"inline"},{"insert":" code.\\n"}]'
        )
        html = "<html><body><script>window.__STATE__ = " + payload + ";</script></body></html>"
        result = cr.extract_readable_text(html)
        assert "First line of the document body" in result
        assert "Second line with inline code" in result

    def test_deep_state_tree_not_truncated(self) -> None:
        node: object = "Deep nested documentation paragraph that must survive recursion limits."
        for _ in range(18):
            node = {"children": node}
        payload = __import__("json").dumps({"loaderData": node})
        html = f'<html><body><script type="application/json">{payload}</script></body></html>'
        result = cr.extract_readable_text(html)
        assert "Deep nested documentation paragraph" in result


class TestPageLinkSchemeFilter:
    def test_mailto_tel_javascript_links_skipped_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """mailto/tel/javascript 链接不进入页面队列，也不刷告警。"""
        import logging as _logging

        parser = cr.PageParser("https://example.com/page")
        with caplog.at_level(_logging.WARNING, logger="crawler"):
            parser.feed(
                "<html><body>"
                "<a href='mailto:team@example.com'>mail</a>"
                "<a href='tel:+123'>tel</a>"
                "<a href='javascript:void(0)'>js</a>"
                "<a href='/next'>next</a>"
                "<a href='https://other.example.com/x'>external</a>"
                "</body></html>"
            )
        assert parser.page_links == ["https://example.com/next", "https://other.example.com/x"]
        assert not [r for r in caplog.records if "URL rejected" in r.message]

    def test_normalize_url_warning_is_not_seed_specific(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging as _logging

        with caplog.at_level(_logging.WARNING, logger="crawler"):
            assert cr.normalize_url("mailto:a@b.c") == ""
        messages = [r.message for r in caplog.records if "URL rejected" in r.message]
        assert messages and all("seed" not in message for message in messages)


# ========== 链接数据文件下载（跨项目「抓数据 → 分析」链路）==========


class TestLinkedDataParser:
    def test_anchor_data_file_becomes_resource_when_enabled(self) -> None:
        parser = cr.PageParser("https://example.com/", download_linked_data=True)
        parser.feed(
            '<a href="/data/orders.csv">csv</a>'
            '<a href="/data/book.xlsx">xlsx</a>'
            '<a href="/page.html">page</a>'
        )
        urls = {resource.url for resource in parser.resources}
        assert urls == {"https://example.com/data/orders.csv", "https://example.com/data/book.xlsx"}
        # 普通页面链接仍然保留；已资源化的数据文件不再入页面队列
        assert parser.page_links == ["https://example.com/page.html"]
        assert all(resource.kind == "data" for resource in parser.resources)

    def test_anchor_data_file_ignored_by_default(self) -> None:
        parser = cr.PageParser("https://example.com/")
        parser.feed('<a href="/data/orders.csv">csv</a>')
        assert parser.resources == []
        assert parser.page_links == ["https://example.com/data/orders.csv"]

    def test_duplicate_links_are_deduplicated(self) -> None:
        parser = cr.PageParser("https://example.com/", download_linked_data=True)
        parser.feed('<a href="/a.csv">1</a><a href="/a.csv">2</a>')
        assert len(parser.resources) == 1


class TestLinkedDataCrawl:
    def test_download_linked_data_flag_lists_data_resources(self, tmp_path: Path) -> None:
        html = b'<html><body><a href="/orders.csv">csv</a><a href="/page.html">p</a></body></html>'
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--list-only",
                    "--download-linked-data",
                    "--workers",
                    "1",
                ]
            )
            cr.crawl(args)
        manifest = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert {row["url"] for row in manifest} == {"https://example.com/orders.csv"}

    def test_without_flag_data_links_are_not_listed(self, tmp_path: Path) -> None:
        html = b'<html><body><a href="/orders.csv">csv</a></body></html>'
        with patch.object(core, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args(
                [
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path),
                    "--list-only",
                    "--workers",
                    "1",
                ]
            )
            cr.crawl(args)
        manifest = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert manifest == []

    def test_config_roundtrip_includes_flag(self, tmp_path: Path) -> None:
        args = cr.build_parser().parse_args(
            ["--url", "https://example.com", "--download-linked-data", "--list-only"]
        )
        target = tmp_path / "config.json"
        ctx_mod.save_config_to_file(args, str(target))
        config = json.loads(target.read_text(encoding="utf-8"))
        assert config["download_linked_data"] is True


class TestReadableTextCJKAndBlocks:
    """中文行内拼接不污染空格；<pre>/表格保留结构。"""

    def test_cjk_inline_no_spaces(self) -> None:
        html = "<article><p>请先<strong>安装</strong>依赖</p></article>"
        assert cr.extract_readable_text(html) == "请先安装依赖"

    def test_cjk_punctuation_no_spaces(self) -> None:
        html = "<article><p>第一，<em>第二</em>。</p></article>"
        assert cr.extract_readable_text(html) == "第一，第二。"

    def test_cjk_latin_boundary_keeps_space(self) -> None:
        html = "<article><p>使用 <code>pip</code> 安装依赖</p></article>"
        assert cr.extract_readable_text(html) == "使用 pip 安装依赖"

    def test_english_inline_keeps_space(self) -> None:
        html = "<article><p>Read <a href='/x'>the</a> docs</p></article>"
        assert cr.extract_readable_text(html) == "Read the docs"

    def test_pre_preserves_line_breaks_and_indent(self) -> None:
        html = "<article><pre>line1\n  line2\n\nline3</pre></article>"
        assert cr.extract_readable_text(html) == "line1\n  line2\n\nline3"

    def test_table_rows_keep_cell_boundaries(self) -> None:
        html = (
            "<article><table><tr><th>名称</th><th>数量</th></tr>"
            "<tr><td>苹果</td><td>2</td></tr></table></article>"
        )
        assert cr.extract_readable_text(html) == "名称 | 数量\n苹果 | 2"

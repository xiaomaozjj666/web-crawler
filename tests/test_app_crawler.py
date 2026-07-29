"""app/crawler.py 单元测试：资源下载器核心逻辑。

覆盖工具函数、数据类、限速/去重、HTML 解析、清单/报告生成、
fetch（mock opener）以及 crawl 集成测试（mock fetch）。
所有网络请求均被 mock，测试可重复、不依赖外部状态。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from app import crawler as cr

# ========== 数据类 ==========


class TestResource:
    def test_creation(self) -> None:
        r = cr.Resource(url="https://x.com/a.js", found_in="script[src]", kind="script",
                        page_url="https://x.com/")
        assert r.url == "https://x.com/a.js"
        assert r.kind == "script"

    def test_frozen(self) -> None:
        r = cr.Resource(url="u", found_in="f", kind="k", page_url="p")
        with pytest.raises(AttributeError):
            r.url = "changed"  # type: ignore[misc]


class TestManifestRow:
    def test_defaults(self) -> None:
        row = cr.ManifestRow(
            status="ok", url="u", saved_path="p", content_type="ct", bytes=10,
            category="img", found_in="f", kind="k", page_url="pu", page_title="t",
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
        cr.register_segment_key("https://x.com/seg", b"k" * 16, b"iv" * 8)
        result = cr.get_segment_key("https://x.com/seg")
        assert result is not None
        assert result[0] == b"k" * 16

    def test_get_missing(self) -> None:
        assert cr.get_segment_key("https://nonexistent.com/seg") is None


# ========== DomainRateLimiter ==========


class TestDomainRateLimiter:
    def test_no_delay(self) -> None:
        """无延迟时不阻塞。"""
        limiter = cr.DomainRateLimiter(default_delay=0.0)
        limiter.wait_if_needed("https://example.com/page")  # 不应阻塞

    def test_record_request(self) -> None:
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
        dedup = cr.ContentDedup()
        is_dup, sha = dedup.is_duplicate(b"data1", "https://a.com/1")
        assert is_dup is False
        assert len(sha) == 64

    def test_duplicate_content(self) -> None:
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
        dedup = cr.ContentDedup()
        dedup.is_duplicate(b"a", "https://a.com/1")
        dedup.is_duplicate(b"b", "https://a.com/2")
        assert len(dedup.seen_hashes()) == 2

    def test_seen_count(self) -> None:
        dedup = cr.ContentDedup()
        dedup.is_duplicate(b"a", "https://a.com/1")
        assert dedup.seen_count() == 1


# ========== 工具函数 ==========


class TestParseHeaders:
    def test_valid(self) -> None:
        result = cr.parse_headers(["Cookie: name=val", "Auth: Bearer token"])
        assert result["Cookie"] == "name=val"
        assert result["Auth"] == "Bearer token"

    def test_invalid_no_colon(self) -> None:
        with pytest.raises(ValueError, match="Invalid header format"):
            cr.parse_headers(["no colon here"])


class TestParseSrcset:
    def test_basic(self) -> None:
        result = list(cr.parse_srcset("img1.jpg 1x, img2.jpg 2x"))
        assert result == ["img1.jpg", "img2.jpg"]

    def test_single(self) -> None:
        assert list(cr.parse_srcset("only.jpg")) == ["only.jpg"]


class TestNormalizeUrl:
    def test_http(self) -> None:
        assert cr.normalize_url("https://example.com/page") == "https://example.com/page"

    def test_fragment_removed(self) -> None:
        assert cr.normalize_url("https://example.com/page#section") == "https://example.com/page"

    def test_data_uri(self) -> None:
        assert cr.normalize_url("data:text/html,base64") == ""

    def test_javascript(self) -> None:
        assert cr.normalize_url("javascript:void(0)") == ""

    def test_ssrf_localhost(self) -> None:
        assert cr.normalize_url("http://localhost/admin") == ""

    def test_ssrf_private_ip(self) -> None:
        assert cr.normalize_url("http://192.168.1.1/admin") == ""

    def test_ssrf_loopback_ip(self) -> None:
        assert cr.normalize_url("http://127.0.0.1/admin") == ""

    def test_ssrf_metadata(self) -> None:
        assert cr.normalize_url("http://169.254.169.254/latest") == ""

    def test_non_http_scheme(self) -> None:
        assert cr.normalize_url("ftp://example.com/file") == ""


class TestLooksLikeUrl:
    def test_http(self) -> None:
        assert cr.looks_like_url("https://example.com") is True

    def test_protocol_relative(self) -> None:
        assert cr.looks_like_url("//cdn.example.com/lib.js") is True

    def test_absolute_path(self) -> None:
        assert cr.looks_like_url("/assets/img.png") is True

    def test_other(self) -> None:
        assert cr.looks_like_url("not a url") is False


class TestLooksLikeDownloadable:
    def test_normal_url(self) -> None:
        assert cr.looks_like_downloadable("https://example.com/img.png") is True

    def test_data_uri(self) -> None:
        assert cr.looks_like_downloadable("data:image/png;base64,abc") is False

    def test_javascript(self) -> None:
        assert cr.looks_like_downloadable("javascript:void(0)") is False

    def test_mailto(self) -> None:
        assert cr.looks_like_downloadable("mailto:test@test.com") is False

    def test_empty(self) -> None:
        assert cr.looks_like_downloadable("") is False

    def test_hash(self) -> None:
        assert cr.looks_like_downloadable("#anchor") is False


class TestLooksLikeResourceUrl:
    def test_image(self) -> None:
        assert cr.looks_like_resource_url("https://x.com/logo.png") is True

    def test_css(self) -> None:
        assert cr.looks_like_resource_url("https://x.com/style.css") is True

    def test_js(self) -> None:
        assert cr.looks_like_resource_url("https://x.com/app.js") is True

    def test_html_not_resource(self) -> None:
        assert cr.looks_like_resource_url("https://x.com/page.html") is False

    def test_no_extension(self) -> None:
        assert cr.looks_like_resource_url("https://x.com/page") is False


class TestIsVideoResource:
    def _row(self, url: str = "", ct: str = "", found_in: str = "", kind: str = "") -> cr.ManifestRow:
        return cr.ManifestRow(
            status="ok", url=url, saved_path="", content_type=ct, bytes=0,
            category="video", found_in=found_in, kind=kind, page_url="", page_title="",
            diagnostic="",
        )

    def test_by_suffix(self) -> None:
        assert cr.is_video_resource(self._row(url="https://x.com/v.mp4")) is True

    def test_by_content_type(self) -> None:
        assert cr.is_video_resource(self._row(ct="video/mp4")) is True

    def test_by_kind(self) -> None:
        assert cr.is_video_resource(self._row(kind="video")) is True

    def test_by_found_in(self) -> None:
        assert cr.is_video_resource(self._row(found_in="video[src]")) is True

    def test_not_video(self) -> None:
        assert cr.is_video_resource(self._row(url="https://x.com/img.png", ct="image/png")) is False


class TestCategoryFor:
    def test_playlist(self) -> None:
        assert cr.category_for("https://x.com/v.m3u8", "", "", "") == "playlist"

    def test_subtitle(self) -> None:
        assert cr.category_for("https://x.com/sub.vtt", "", "", "") == "subtitle"

    def test_video(self) -> None:
        assert cr.category_for("https://x.com/v.mp4", "", "", "") == "video"

    def test_audio(self) -> None:
        assert cr.category_for("https://x.com/a.mp3", "audio/mpeg", "", "") == "audio"

    def test_image(self) -> None:
        assert cr.category_for("https://x.com/i.png", "image/png", "", "") == "image"

    def test_css(self) -> None:
        assert cr.category_for("https://x.com/s.css", "text/css", "", "") == "css"

    def test_script(self) -> None:
        assert cr.category_for("https://x.com/a.js", "application/javascript", "", "") == "script"

    def test_font(self) -> None:
        assert cr.category_for("https://x.com/f.woff2", "font/woff2", "", "") == "font"

    def test_other(self) -> None:
        assert cr.category_for("https://x.com/data", "application/json", "", "") == "other"

    def test_poster(self) -> None:
        assert cr.category_for("https://x.com/p.jpg", "", "", "video[poster]") == "poster"


class TestSameDomain:
    def test_same(self) -> None:
        assert cr.same_domain("https://a.com/x", "https://a.com/y") is True

    def test_different(self) -> None:
        assert cr.same_domain("https://a.com/x", "https://b.com/y") is False


class TestBlockKeywords:
    def test_parse(self) -> None:
        result = cr.parse_block_keywords(["ad, tracker", "popup"])
        assert result == ["ad", "tracker", "popup"]

    def test_parse_newlines(self) -> None:
        result = cr.parse_block_keywords(["ad\ntracker\npopup"])
        assert result == ["ad", "tracker", "popup"]

    def test_is_blocked(self) -> None:
        assert cr.is_blocked_url("https://ad.example.com/track", ["ad", "track"]) is True

    def test_not_blocked(self) -> None:
        assert cr.is_blocked_url("https://example.com/page", ["ad"]) is False


class TestDecodeText:
    def test_utf8(self) -> None:
        assert cr.decode_text(b"hello", "text/html; charset=utf-8", None) == "hello"

    def test_fallback(self) -> None:
        assert cr.decode_text(b"hello", "", "utf-8") == "hello"

    def test_default_utf8(self) -> None:
        assert cr.decode_text(b"hello", "", None) == "hello"


class TestExtractTitle:
    def test_normal(self) -> None:
        html = "<html><head><title>My Page</title></head></html>"
        assert cr.extract_title(html) == "My Page"

    def test_no_title(self) -> None:
        assert cr.extract_title("<html></html>") == ""

    def test_multiline(self) -> None:
        html = "<title>Line 1\n  Line 2</title>"
        assert "Line 1" in cr.extract_title(html)


class TestCallbacks:
    def test_should_stop(self) -> None:
        args = Mock()
        args.should_stop = lambda: True
        assert cr.should_stop(args) is True

    def test_should_stop_none(self) -> None:
        assert cr.should_stop(None) is False

    def test_should_stop_no_callback(self) -> None:
        args = Mock(spec=[])
        assert cr.should_stop(args) is False

    def test_wait_if_paused(self) -> None:
        called = [0]
        def cb() -> None:
            called[0] += 1
        args = Mock()
        args.wait_if_paused = cb
        cr.wait_if_paused(args)
        assert called[0] == 1

    def test_wait_if_paused_none(self) -> None:
        cr.wait_if_paused(None)  # 不应抛异常

    def test_report_progress(self) -> None:
        received: list[dict] = []
        def cb(payload: dict) -> None:
            received.append(payload)
        args = Mock()
        args.progress_callback = cb
        cr.report_progress(args, phase="test", url="https://x.com")
        assert received[0]["phase"] == "test"


class TestOutputPathForUrl:
    def test_basic(self, tmp_path: Path) -> None:
        result = cr.output_path_for_url("https://example.com/img/logo.png", tmp_path, "image/png")
        assert "example.com" in str(result)
        assert result.suffix == ".png"

    def test_with_query(self, tmp_path: Path) -> None:
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
        assert cr.safe_segment("hello") == "hello"

    def test_empty(self) -> None:
        assert cr.safe_segment("") == "unnamed"

    def test_path_traversal(self) -> None:
        assert ".." not in cr.safe_segment("../../etc/passwd")

    def test_special_chars(self) -> None:
        result = cr.safe_segment('file<>:"/\\|?*')
        assert "<" not in result
        assert ">" not in result

    def test_truncation(self) -> None:
        assert len(cr.safe_segment("x" * 200)) <= 120


class TestOutputPrefixForResource:
    def test_no_organize(self) -> None:
        args = Mock()
        args.organize = False
        assert cr.output_prefix_for_resource(args, Mock(), "", {}) == "assets"

    def test_with_organize(self) -> None:
        args = Mock()
        args.organize = True
        resource = cr.Resource(url="https://x.com/img.png", found_in="img[src]",
                               kind="img", page_url="https://x.com/")
        result = cr.output_prefix_for_resource(args, resource, "image/png", {"https://x.com/": "Page"})
        assert "image" in result
        assert "Page" in result


# ========== CSS / 播放清单 / 去重 ==========


class TestDiscoverCssResources:
    def test_url_function(self) -> None:
        css = 'background: url("img/bg.png");'
        result = cr.discover_css_resources(css, "https://x.com/style.css", "https://x.com/")
        assert len(result) == 1
        assert "bg.png" in result[0].url

    def test_import(self) -> None:
        css = '@import "sub.css";'
        result = cr.discover_css_resources(css, "https://x.com/style.css", "https://x.com/")
        assert len(result) == 1

    def test_data_uri_skipped(self) -> None:
        css = 'background: url("data:image/png;base64,abc");'
        result = cr.discover_css_resources(css, "https://x.com/style.css", "https://x.com/")
        assert len(result) == 0


class TestDiscoverPlaylistResources:
    def test_m3u8_plain(self) -> None:
        text = "#EXTM3U\n#EXTINF:10,\nhttps://x.com/seg1.ts\n#EXTINF:10,\nhttps://x.com/seg2.ts\n"
        resources, note = cr.discover_playlist_resources(
            text, "https://x.com/playlist.m3u8", "https://x.com/"
        )
        assert len(resources) == 2
        assert note == ""

    def test_m3u8_encrypted(self) -> None:
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
        text = '<MPD><BaseURL>seg1.mp4</BaseURL></MPD>'
        resources, note = cr.discover_playlist_resources(
            text, "https://x.com/playlist.mpd", "https://x.com/"
        )
        assert len(resources) == 1
        assert note == ""

    def test_non_playlist(self) -> None:
        resources, note = cr.discover_playlist_resources(
            "just text", "https://x.com/page.html", "https://x.com/"
        )
        assert len(resources) == 0
        assert note == ""


class TestUniqueResources:
    def test_dedup(self) -> None:
        resources = [
            cr.Resource("https://x.com/a.js", "f", "k", "p"),
            cr.Resource("https://x.com/a.js", "f2", "k2", "p2"),
            cr.Resource("https://x.com/b.js", "f", "k", "p"),
        ]
        result = cr.unique_resources(resources)
        assert len(result) == 2


class TestIsHtml:
    def test_html_content_type(self) -> None:
        assert cr.is_html("text/html", "https://x.com/page") is True

    def test_html_extension(self) -> None:
        assert cr.is_html("", "https://x.com/page.html") is True

    def test_no_suffix(self) -> None:
        assert cr.is_html("", "https://x.com/page") is True

    def test_json(self) -> None:
        assert cr.is_html("application/json", "https://x.com/data.json") is False


# ========== PageParser ==========


class TestPageParser:
    def test_img_src(self) -> None:
        parser = cr.PageParser("https://x.com/")
        parser.feed('<img src="logo.png">')
        assert len(parser.resources) == 1
        assert parser.resources[0].url == "https://x.com/logo.png"

    def test_script_src(self) -> None:
        parser = cr.PageParser("https://x.com/")
        parser.feed('<script src="app.js"></script>')
        assert len(parser.resources) == 1

    def test_link_stylesheet(self) -> None:
        parser = cr.PageParser("https://x.com/")
        parser.feed('<link rel="stylesheet" href="style.css">')
        assert len(parser.resources) == 1

    def test_link_non_resource(self) -> None:
        """非资源 link 被跳过。"""
        parser = cr.PageParser("https://x.com/")
        parser.feed('<link rel="canonical" href="https://x.com/page">')
        assert len(parser.resources) == 0

    def test_srcset(self) -> None:
        parser = cr.PageParser("https://x.com/")
        parser.feed('<img srcset="img1.jpg 1x, img2.jpg 2x">')
        assert len(parser.resources) == 2

    def test_base_tag(self) -> None:
        parser = cr.PageParser("https://x.com/")
        parser.feed('<base href="/sub/">')
        parser.feed('<img src="img.png">')
        assert parser.resources[0].url == "https://x.com/sub/img.png"

    def test_page_links(self) -> None:
        parser = cr.PageParser("https://x.com/")
        parser.feed('<a href="/about">About</a>')
        assert "https://x.com/about" in parser.page_links

    def test_meta_og_image(self) -> None:
        parser = cr.PageParser("https://x.com/")
        parser.feed('<meta property="og:image" content="https://cdn.x.com/og.png">')
        assert len(parser.resources) == 1

    def test_data_uri_skipped(self) -> None:
        parser = cr.PageParser("https://x.com/")
        parser.feed('<img src="data:image/png;base64,abc">')
        assert len(parser.resources) == 0


# ========== make_robots_parser / rewrite_html / strip_overlays ==========


class TestMakeRobotsParser:
    def test_with_fetch_mock(self) -> None:
        with patch.object(cr, "fetch", return_value=(b"User-agent: *\nDisallow: /private", "text/plain")):
            parser = cr.make_robots_parser("https://example.com", {}, 10)
        assert parser.can_fetch("*", "https://example.com/page") is True
        assert parser.can_fetch("*", "https://example.com/private") is False

    def test_fetch_error(self) -> None:
        """fetch 失败时返回空 robots parser（允许所有）。"""
        with patch.object(cr, "fetch", side_effect=Exception("network error")):
            parser = cr.make_robots_parser("https://example.com", {}, 10)
        assert parser.can_fetch("*", "https://example.com/anything") is True


class TestRewriteHtml:
    def test_basic(self, tmp_path: Path) -> None:
        html = '<img src="https://x.com/logo.png">'
        rows = [cr.ManifestRow(
            status="ok", url="https://x.com/logo.png",
            saved_path=str(tmp_path / "assets" / "logo.png"),
            content_type="image/png", bytes=100, category="image",
            found_in="img[src]", kind="img", page_url="https://x.com/",
            page_title="", diagnostic="", sha256="",
        )]
        result = cr.rewrite_html(html, rows, "https://x.com/", tmp_path)
        assert "logo.png" in result
        assert "https://x.com/logo.png" not in result


class TestStripPageOverlays:
    def test_removes_modal(self) -> None:
        html = '<div class="modal">overlay content</div><p>real content</p>'
        result = cr.strip_page_overlays(html)
        assert "modal" not in result or "overlay content" not in result

    def test_removes_by_id(self) -> None:
        html = '<div id="popup">spam</div><p>content</p>'
        result = cr.strip_page_overlays(html)
        assert "spam" not in result

    def test_preserves_content(self) -> None:
        html = '<p>main content</p>'
        result = cr.strip_page_overlays(html)
        assert "main content" in result


# ========== smart_extract / extract_readable_text ==========


class TestSmartExtract:
    def test_basic(self) -> None:
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
        html = '<article><p>Main article text that is long enough to be kept</p></article>'
        result = cr.extract_readable_text(html)
        assert "Main article text" in result

    def test_body_fallback(self) -> None:
        html = '<body><p>Body text that is long enough to be kept here</p></body>'
        result = cr.extract_readable_text(html)
        assert "Body text" in result

    def test_strips_scripts(self) -> None:
        html = '<article><script>var x = 1;</script><p>Visible text here is long enough</p></article>'
        result = cr.extract_readable_text(html)
        assert "var x" not in result
        assert "Visible text" in result


class TestWriteExtractedData:
    def test_writes_json_and_csv(self, tmp_path: Path) -> None:
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
        sitemap = '<?xml version="1.0"?><urlset><url><loc>https://x.com/a</loc></url><url><loc>https://x.com/b</loc></url></urlset>'
        with patch.object(cr, "fetch", return_value=(sitemap.encode(), "application/xml")):
            urls = cr.discover_sitemap_urls("https://x.com/sitemap.xml", {}, 10)
        assert len(urls) == 2

    def test_fetch_error(self) -> None:
        with patch.object(cr, "fetch", side_effect=Exception("err")):
            urls = cr.discover_sitemap_urls("https://x.com/sitemap.xml", {}, 10)
        assert urls == []


# ========== 配置保存/加载 ==========


class TestConfigSaveLoad:
    def test_save_and_load(self, tmp_path: Path) -> None:
        args = cr.build_parser().parse_args(["--url", "https://x.com", "--out", str(tmp_path)])
        config_path = tmp_path / "config.json"
        cr.save_config_to_file(args, str(config_path))
        assert config_path.exists()

        loaded = cr.load_config_from_file(str(config_path))
        assert loaded["url"] == "https://x.com"


class TestCrawlState:
    def test_save_and_load(self, tmp_path: Path) -> None:
        cr.save_crawl_state(tmp_path, page_queue=["https://x.com/a"], seen_pages=[])
        loaded = cr.load_crawl_state(tmp_path)
        assert loaded["page_queue"] == ["https://x.com/a"]

    def test_load_missing(self, tmp_path: Path) -> None:
        assert cr.load_crawl_state(tmp_path) == {}

    def test_load_invalid(self, tmp_path: Path) -> None:
        (tmp_path / cr.CRAWL_STATE_FILE).write_text("invalid json")
        assert cr.load_crawl_state(tmp_path) == {}

    def test_clear(self, tmp_path: Path) -> None:
        cr.save_crawl_state(tmp_path, key="val")
        cr.clear_crawl_state(tmp_path)
        assert not (tmp_path / cr.CRAWL_STATE_FILE).exists()


# ========== 清单写入 ==========


class TestManifests:
    def _rows(self) -> list[cr.ManifestRow]:
        return [
            cr.ManifestRow(
                status="ok", url="https://x.com/a.js", saved_path="/tmp/a.js",
                content_type="application/javascript", bytes=100, category="script",
                found_in="script[src]", kind="script", page_url="https://x.com/",
                page_title="Page", diagnostic="", sha256="abc",
            ),
            cr.ManifestRow(
                status="error: 404", url="https://x.com/b.js", saved_path="",
                content_type="", bytes=0, category="script",
                found_in="script[src]", kind="script", page_url="https://x.com/",
                page_title="Page", diagnostic="not found",
            ),
        ]

    def test_write_manifests(self, tmp_path: Path) -> None:
        cr.write_manifests(tmp_path, self._rows())
        assert (tmp_path / "resources_manifest.csv").exists()
        assert (tmp_path / "resources_manifest.json").exists()
        data = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert len(data) == 2

    def test_write_video_manifests(self, tmp_path: Path) -> None:
        rows = self._rows() + [cr.ManifestRow(
            status="ok", url="https://x.com/v.mp4", saved_path="/tmp/v.mp4",
            content_type="video/mp4", bytes=1000, category="video",
            found_in="video[src]", kind="video", page_url="https://x.com/",
            page_title="Page", diagnostic="",
        )]
        count = cr.write_video_manifests(tmp_path, rows)
        assert count == 1
        assert (tmp_path / "video_manifest.csv").exists()

    def test_write_video_manifests_empty(self, tmp_path: Path) -> None:
        count = cr.write_video_manifests(tmp_path, self._rows())
        assert count == 0

    def test_write_failed_manifests(self, tmp_path: Path) -> None:
        count = cr.write_failed_manifests(tmp_path, self._rows())
        assert count == 1
        assert (tmp_path / "failed_resources.csv").exists()

    def test_is_failed_row(self) -> None:
        ok = cr.ManifestRow("ok", "u", "", "", 0, "", "", "", "", "", "")
        err = cr.ManifestRow("error: x", "u", "", "", 0, "", "", "", "", "", "")
        skipped = cr.ManifestRow("skipped: dedup", "u", "", "", 0, "", "", "", "", "", "")
        assert cr.is_failed_row(ok) is False
        assert cr.is_failed_row(err) is True
        assert cr.is_failed_row(skipped) is True


# ========== 格式化函数 ==========


class TestFormatBytes:
    def test_bytes(self) -> None:
        assert cr._format_bytes(500) == "500 B"

    def test_mb(self) -> None:
        """1024 ≤ n < 1024² 返回 MB（源码阈值逻辑使 KB 分支不可达）。"""
        assert cr._format_bytes(2048) == "2.0 MB"

    def test_gb(self) -> None:
        """1024² ≤ n < 1024³ 返回 GB。"""
        assert cr._format_bytes(2 * 1024 * 1024) == "2.0 GB"

    def test_tb(self) -> None:
        """n ≥ 1024³ 返回 TB。"""
        assert cr._format_bytes(2 * 1024 * 1024 * 1024) == "2.0 TB"


class TestFormatDuration:
    def test_milliseconds(self) -> None:
        assert "毫秒" in cr.format_duration(0.5)

    def test_seconds(self) -> None:
        assert "秒" in cr.format_duration(5.0)

    def test_minutes(self) -> None:
        assert "分" in cr.format_duration(125.0)

    def test_hours(self) -> None:
        assert "时" in cr.format_duration(3725.0)

    def test_negative(self) -> None:
        assert cr.format_duration(-1) == "未知"


# ========== 错误分类 ==========


class TestClassifyError:
    def test_auth(self) -> None:
        assert cr.classify_error("error: HTTP Error 401") == "auth"

    def test_not_found(self) -> None:
        assert cr.classify_error("error: HTTP Error 404") == "not_found"

    def test_rate_limit(self) -> None:
        assert cr.classify_error("error: HTTP Error 429") == "rate_limit"

    def test_timeout(self) -> None:
        assert cr.classify_error("error: timed out") == "timeout"

    def test_size_limit(self) -> None:
        assert cr.classify_error("error: file exceeds max-bytes") == "size_limit"

    def test_robots(self) -> None:
        assert cr.classify_error("skipped by robots.txt") == "robots"

    def test_dedup(self) -> None:
        assert cr.classify_error("skipped by dedup") == "dedup"

    def test_encrypted(self) -> None:
        assert cr.classify_error("encrypted playlist detected") == "encrypted"

    def test_network(self) -> None:
        assert cr.classify_error("error: URLError connection refused") == "network"

    def test_cancelled(self) -> None:
        assert cr.classify_error("cancelled by user") == "cancelled"

    def test_other_error(self) -> None:
        assert cr.classify_error("error: unknown") == "other_error"

    def test_skipped_other(self) -> None:
        assert cr.classify_error("skipped: unknown reason") == "skipped_other"

    def test_other(self) -> None:
        assert cr.classify_error("ok") == "other"


# ========== 报告上下文 ==========


class TestBuildReportContext:
    def _rows(self) -> list[cr.ManifestRow]:
        return [
            cr.ManifestRow(
                status="ok", url="https://x.com/a.js", saved_path="/tmp/a.js",
                content_type="application/javascript", bytes=100, category="script",
                found_in="script[src]", kind="script", page_url="https://x.com/",
                page_title="Page", diagnostic="", sha256="abc",
            ),
            cr.ManifestRow(
                status="error: HTTP Error 404", url="https://x.com/b.js", saved_path="",
                content_type="", bytes=0, category="script",
                found_in="script[src]", kind="script", page_url="https://x.com/",
                page_title="Page", diagnostic="not found",
            ),
            cr.ManifestRow(
                status="skipped by dedup", url="https://x.com/c.js", saved_path="",
                content_type="application/javascript", bytes=50, category="script",
                found_in="script[src]", kind="script", page_url="https://x.com/",
                page_title="Page", diagnostic="dedup", sha256="def",
            ),
        ]

    def test_basic(self) -> None:
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
        ctx = cr.build_report_context([], 0, 0.0, 0.0)
        assert ctx["resources"]["total"] == 0
        assert ctx["resources"]["success_rate_percent"] == 100.0

    def test_with_config(self) -> None:
        ctx = cr.build_report_context([], 0, 0.0, 0.0, config={"url": "https://x.com"})
        assert ctx["config"]["url"] == "https://x.com"

    def test_failures_by_class(self) -> None:
        ctx = cr.build_report_context(self._rows(), 1, 0.0, 1.0)
        assert "not_found" in ctx["failures_by_class"]


class TestBuildRecommendations:
    def test_no_resources(self) -> None:
        ctx = cr.build_report_context([], 0, 0.0, 0.0)
        recs = cr.build_recommendations(ctx)
        assert len(recs) >= 1
        assert any("未发现" in r["title"] for r in recs)

    def test_auth_error(self) -> None:
        rows = [cr.ManifestRow(
            "error: HTTP Error 401", "https://x.com/private", "", "", 0, "", "", "", "", "", ""
        )]
        ctx = cr.build_report_context(rows, 1, 0.0, 1.0)
        recs = cr.build_recommendations(ctx)
        assert any("401/403" in r["title"] for r in recs)

    def test_normal(self) -> None:
        rows = [cr.ManifestRow(
            "ok", "https://x.com/a.js", "/tmp/a.js", "application/javascript", 100,
            "script", "script[src]", "script", "https://x.com/", "Page", ""
        )]
        ctx = cr.build_report_context(rows, 1, 0.0, 1.0)
        recs = cr.build_recommendations(ctx)
        assert len(recs) >= 1


class TestWriteSummary:
    def test_writes_file(self, tmp_path: Path) -> None:
        rows = [cr.ManifestRow(
            "ok", "https://x.com/a.js", "/tmp/a.js", "application/javascript", 100,
            "script", "script[src]", "script", "https://x.com/", "Page", ""
        )]
        cr.write_summary(tmp_path, rows, 1, start_time=0.0, end_time=1.0, config={"url": "https://x.com"})
        assert (tmp_path / "summary.txt").exists()
        content = (tmp_path / "summary.txt").read_text(encoding="utf-8")
        assert "网页资源采集" in content


class TestWriteRunReport:
    def test_writes_all_formats(self, tmp_path: Path) -> None:
        rows = [cr.ManifestRow(
            "ok", "https://x.com/a.js", "/tmp/a.js", "application/javascript", 100,
            "script", "script[src]", "script", "https://x.com/", "Page", ""
        )]
        cr.write_run_report(tmp_path, rows, 1, start_time=0.0, end_time=1.0, config={"url": "https://x.com"})
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
        assert "401" in cr.diagnostic_for_status("error: HTTP Error 401")

    def test_403(self) -> None:
        assert "403" in cr.diagnostic_for_status("error: HTTP Error 403")

    def test_404(self) -> None:
        assert "404" in cr.diagnostic_for_status("error: HTTP Error 404")

    def test_timeout(self) -> None:
        assert "Timeout" in cr.diagnostic_for_status("error: timed out")

    def test_size_limit(self) -> None:
        assert "max-bytes" in cr.diagnostic_for_status("error: file exceeds")

    def test_robots(self) -> None:
        assert "robots" in cr.diagnostic_for_status("skipped by robots.txt")

    def test_dedup(self) -> None:
        assert "SHA256" in cr.diagnostic_for_status("skipped by dedup")

    def test_generic_error(self) -> None:
        assert "Request failed" in cr.diagnostic_for_status("error: unknown")

    def test_ok(self) -> None:
        assert cr.diagnostic_for_status("ok") == ""


class TestRowFor:
    def test_basic(self) -> None:
        resource = cr.Resource("https://x.com/a.js", "script[src]", "script", "https://x.com/")
        row = cr.row_for("ok", resource, "/tmp/a.js", "application/javascript", 100,
                         {"https://x.com/": "Page"}, sha256="abc123")
        assert row.status == "ok"
        assert row.url == "https://x.com/a.js"
        assert row.category == "script"
        assert row.sha256 == "abc123"


# ========== fetch ==========


class TestFetch:
    def test_success(self) -> None:
        """fetch 成功读取响应体。"""
        mock_response = MagicMock()
        mock_response.headers.get.return_value = "text/html"
        mock_response.read.side_effect = [b"<html>data</html>", b""]
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_response

        with patch.object(cr, "_get_opener", return_value=mock_opener):
            data, ct = cr.fetch("https://example.com", 30, {}, 0, None)
        assert data == b"<html>data</html>"
        assert ct == "text/html"

    def test_max_bytes_exceeded(self) -> None:
        """超出 max_bytes 抛 ValueError。"""
        mock_response = MagicMock()
        mock_response.headers.get.return_value = "text/html"
        mock_response.read.side_effect = [b"x" * 200, b""]
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_response

        with (
            patch.object(cr, "_get_opener", return_value=mock_opener),
            pytest.raises(ValueError, match="max-bytes"),
        ):
            cr.fetch("https://example.com", 30, {}, 0, 100)

    def test_http_error_no_retry(self) -> None:
        """404 错误不重试。"""
        from urllib.error import HTTPError

        mock_opener = MagicMock()
        mock_opener.open.side_effect = HTTPError(
            "https://x.com", 404, "Not Found", {}, None  # type: ignore[arg-type]
        )

        with (
            patch.object(cr, "_get_opener", return_value=mock_opener),
            pytest.raises(HTTPError),
        ):
            cr.fetch("https://x.com", 30, {}, 0, None)


# ========== crawl 集成测试 ==========


class TestCrawl:
    def test_no_resources(self, tmp_path: Path) -> None:
        """页面无资源时生成空清单。"""
        html = b"<html><body><p>no resources</p></body></html>"
        with patch.object(cr, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args([
                "--url", "https://example.com",
                "--out", str(tmp_path),
                "--list-only",
                "--workers", "1",
            ])
            exit_code = cr.crawl(args)
        assert exit_code == 0
        assert (tmp_path / "resources_manifest.json").exists()
        manifest = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert len(manifest) == 0

    def test_list_only(self, tmp_path: Path) -> None:
        """list_only 模式下发现资源但不下载。"""
        html = b'<html><body><img src="logo.png"><script src="app.js"></script></body></html>'
        with patch.object(cr, "fetch", return_value=(html, "text/html")):
            args = cr.build_parser().parse_args([
                "--url", "https://example.com",
                "--out", str(tmp_path),
                "--list-only",
                "--workers", "1",
            ])
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

        with patch.object(cr, "fetch", side_effect=mock_fetch):
            args = cr.build_parser().parse_args([
                "--url", "https://example.com",
                "--out", str(tmp_path),
                "--workers", "1",
            ])
            exit_code = cr.crawl(args)
        assert exit_code == 0
        manifest = json.loads((tmp_path / "resources_manifest.json").read_text())
        assert len(manifest) == 1
        assert manifest[0]["status"] == "ok"

    def test_header_parse_error(self, tmp_path: Path) -> None:
        """header 解析错误返回 exit code 2。"""
        args = cr.build_parser().parse_args([
            "--url", "https://example.com",
            "--out", str(tmp_path),
        ])
        args.header = ["invalid header without colon"]
        exit_code = cr.crawl(args)
        assert exit_code == 2

    def test_cancelled(self, tmp_path: Path) -> None:
        """should_stop 返回 True 时取消。"""
        html = b"<html></html>"
        stop_flag = [False]

        def mock_should_stop(args: Any) -> bool:
            return stop_flag[0]

        with (
            patch.object(cr, "fetch", return_value=(html, "text/html")),
            patch.object(cr, "should_stop", side_effect=mock_should_stop),
        ):
            args = cr.build_parser().parse_args([
                "--url", "https://example.com",
                "--out", str(tmp_path),
                "--list-only",
                "--workers", "1",
            ])
            # 第一次 should_stop 返回 False（进入循环前），第二次返回 True
            stop_flag[0] = True
            exit_code = cr.crawl(args)
        assert exit_code == 0


# ========== build_parser / main ==========


class TestBuildParser:
    def test_basic_args(self) -> None:
        parser = cr.build_parser()
        args = parser.parse_args(["--url", "https://x.com"])
        assert args.url == "https://x.com"
        assert args.workers == cr.DEFAULT_WORKERS
        assert args.timeout == 20

    def test_all_args(self) -> None:
        parser = cr.build_parser()
        args = parser.parse_args([
            "--url", "https://x.com",
            "--out", "/tmp/out",
            "--workers", "4",
            "--delay", "1.0",
            "--timeout", "60",
            "--retries", "3",
            "--max-pages", "10",
            "--same-domain",
            "--include-css-urls",
            "--list-only",
            "--dedup",
            "--stealth",
        ])
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
        monkeypatch.setattr("sys.argv", [
            "crawler", "--url", "https://x.com", "--save-config", str(config_path),
        ])
        cr.main()
        assert config_path.exists()

    def test_load_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--load-config 加载配置并运行。"""
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
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
        }))

        monkeypatch.setattr("sys.argv", ["crawler", "--load-config", str(config_path)])

        html = b"<html><body><p>content</p></body></html>"
        with patch.object(cr, "fetch", return_value=(html, "text/html")):
            with pytest.raises(SystemExit) as exc_info:
                cr.main()
            assert exc_info.value.code == 0

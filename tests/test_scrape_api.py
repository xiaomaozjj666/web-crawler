"""``/v1/scrape`` 接口测试：一次调用返回全部产物。

全部走真实 ``ThreadingHTTPServer`` + ``http.client``，但上游 fetch 一律
mock，不依赖公网；DNS/SSRF 解析在正常路径里被替换为 no-op（SSRF 拒绝
用例保留真实校验，目标是环回地址，不需要 DNS）。
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import pytest

from web_crawler.app import _crawler_core as core
from web_crawler.app import _scrape
from web_crawler.app._ui_http import Handler
from web_crawler.exceptions import FetchError

PAGE = (
    "<!DOCTYPE html>\n"
    '<html lang="zh-CN"><head>'
    "<title>示例页面</title>"
    '<meta name="description" content="  用于测试的   描述  ">'
    '<base href="https://cdn.example.com/assets/">'
    "</head><body><main><h1>标题一</h1>"
    '<p>正文 <a href="/next">下一页</a>，重复链接：<a href="/next">再点一次</a>。</p>'
    "</main></body></html>"
)


@pytest.fixture(scope="module")
def server_url() -> str:
    """启动真实 HTTP 服务（临时端口），整个模块复用。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def _request(
    base: str,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    parsed = urlparse(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        return response.status, payload
    finally:
        conn.close()


def _patch_fetch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: bytes = PAGE.encode("utf-8"),
    content_type: str = "text/html; charset=utf-8",
    status: int = 200,
) -> list[str]:
    """把 fetch 换成返回固定页面的假实现，并跳过 DNS 解析。"""
    calls: list[str] = []

    def fake_fetch(
        url: str,
        timeout: int,
        headers: dict[str, str],
        retries: int,
        max_bytes: int | None,
        *,
        meta: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> tuple[bytes, str]:
        calls.append(url)
        if meta is not None:
            meta["status"] = status
            meta["content_type"] = content_type
        return body, content_type

    monkeypatch.setattr(core, "fetch", fake_fetch)
    monkeypatch.setattr(_scrape, "validate_url_host", lambda *_a, **_k: None)
    return calls


class TestScrapeSuccess:
    def test_get_returns_all_artifacts(
        self, server_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_fetch(monkeypatch)
        status, payload = _request(
            server_url, "GET", "/v1/scrape?url=https%3A%2F%2Fexample.com%2Fpage"
        )
        assert status == 200
        assert payload["url"] == "https://example.com/page"
        assert payload["status_code"] == 200
        assert payload["content_type"].startswith("text/html")
        assert payload["title"] == "示例页面"
        assert payload["html"] is None  # 默认不返回 HTML
        assert "标题一" in payload["markdown"]
        assert "下一页" in payload["markdown"]
        # <base> 生效 + 绝对化 + 去重（重复的 /next 只出现一次）
        assert payload["links"] == ["https://cdn.example.com/next"]
        assert payload["metadata"] == {"description": "用于测试的 描述", "lang": "zh-CN"}

    def test_post_json_include_html(self, server_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_fetch(monkeypatch)
        body = json.dumps(
            {"url": "https://example.com/", "include_html": True}, ensure_ascii=False
        ).encode("utf-8")
        status, payload = _request(
            server_url,
            "POST",
            "/v1/scrape",
            body=body,
            headers={"content-type": "application/json"},
        )
        assert status == 200
        assert payload["html"] == PAGE
        assert payload["links"] == ["https://cdn.example.com/next"]

    def test_post_form_encoded(self, server_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_fetch(monkeypatch)
        status, payload = _request(
            server_url,
            "POST",
            "/v1/scrape",
            body=b"url=https%3A%2F%2Fexample.com%2F&include_html=1",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert status == 200
        assert payload["html"] == PAGE

    def test_get_include_html_flag(self, server_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_fetch(monkeypatch)
        status, payload = _request(
            server_url, "GET", "/v1/scrape?url=https%3A%2F%2Fexample.com%2F&include_html=1"
        )
        assert status == 200
        assert payload["html"] is not None
        assert payload["url"] == "https://example.com/"

    def test_unknown_charset_falls_back_to_utf8(
        self, server_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_fetch(monkeypatch, content_type="text/html; charset=x-unknown")
        status, payload = _request(server_url, "GET", "/v1/scrape?url=https%3A%2F%2Fexample.com%2F")
        assert status == 200
        assert payload["title"] == "示例页面"

    def test_empty_body_is_not_an_error(
        self, server_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_fetch(monkeypatch, body=b"", content_type="text/html")
        status, payload = _request(server_url, "GET", "/v1/scrape?url=https%3A%2F%2Fexample.com%2F")
        assert status == 200
        assert payload["title"] == ""
        assert payload["markdown"] == ""
        assert payload["links"] == []
        assert payload["metadata"] == {"description": "", "lang": ""}


class TestScrapeErrors:
    def test_invalid_scheme_rejected_without_fetch(
        self, server_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_fetch(monkeypatch)
        status, payload = _request(
            server_url, "GET", "/v1/scrape?url=ftp%3A%2F%2Fexample.com%2Ffile"
        )
        assert status == 400
        assert payload["error"] == "invalid_url"
        assert calls == []

    @pytest.mark.parametrize("url", ["not-a-url", "http://", "https:///path"])
    def test_malformed_url(
        self, server_url: str, monkeypatch: pytest.MonkeyPatch, url: str
    ) -> None:
        calls = _patch_fetch(monkeypatch)
        status, payload = _request(
            server_url, "GET", f"/v1/scrape?url={url.replace(':', '%3A').replace('/', '%2F')}"
        )
        assert status == 400
        assert payload["error"] in {"invalid_url", "missing_url"}
        assert calls == []

    def test_missing_url(self, server_url: str) -> None:
        status, payload = _request(server_url, "GET", "/v1/scrape")
        assert status == 400
        assert payload["error"] == "missing_url"

    def test_private_host_blocked(self, server_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        # 不 patch validate_url_host：真实 SSRF 校验应拒绝环回目标（无需 DNS）
        calls: list[str] = []

        def fake_fetch(*_args: object, **_kwargs: object) -> tuple[bytes, str]:
            calls.append("called")
            return b"", "text/html"

        monkeypatch.setattr(core, "fetch", fake_fetch)
        status, payload = _request(
            server_url, "GET", "/v1/scrape?url=http%3A%2F%2F127.0.0.1%2Fadmin"
        )
        assert status == 400
        assert payload["error"] == "blocked_url"
        assert calls == []

    def test_non_html_rejected(self, server_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_fetch(monkeypatch, body=b"%PDF-1.4 binary", content_type="application/pdf")
        status, payload = _request(
            server_url, "GET", "/v1/scrape?url=https%3A%2F%2Fexample.com%2Ffile.pdf"
        )
        assert status == 415
        assert payload["error"] == "not_html"
        assert payload["content_type"] == "application/pdf"

    def test_upstream_http_error(self, server_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_fetch(monkeypatch)

        def failing_fetch(*_args: object, **_kwargs: object) -> tuple[bytes, str]:
            exc = FetchError("HTTP Error 503")
            exc.status = 503  # type: ignore[attr-defined]
            raise exc

        monkeypatch.setattr(core, "fetch", failing_fetch)
        status, payload = _request(server_url, "GET", "/v1/scrape?url=https%3A%2F%2Fexample.com%2F")
        assert status == 502
        assert payload["error"] == "upstream_error"
        assert payload["upstream_status"] == 503

    def test_transport_error(self, server_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_fetch(monkeypatch)

        def failing_fetch(*_args: object, **_kwargs: object) -> tuple[bytes, str]:
            raise OSError("connection reset")

        monkeypatch.setattr(core, "fetch", failing_fetch)
        status, payload = _request(server_url, "GET", "/v1/scrape?url=https%3A%2F%2Fexample.com%2F")
        assert status == 502
        assert payload["error"] == "fetch_failed"

    @pytest.mark.parametrize("body", [b"[1, 2, 3]", b"not json", b"\xff\xfe"])
    def test_post_invalid_json(
        self, server_url: str, monkeypatch: pytest.MonkeyPatch, body: bytes
    ) -> None:
        _patch_fetch(monkeypatch)
        status, payload = _request(
            server_url,
            "POST",
            "/v1/scrape",
            body=body,
            headers={"content-type": "application/json"},
        )
        assert status == 400
        assert payload["error"] == "invalid_body"

    def test_post_json_url_not_string(
        self, server_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_fetch(monkeypatch)
        status, payload = _request(
            server_url,
            "POST",
            "/v1/scrape",
            body=json.dumps({"url": 42}).encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        assert status == 400
        assert payload["error"] == "invalid_url"


class TestScrapeCsrf:
    def test_cross_site_get_rejected(
        self, server_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_fetch(monkeypatch)
        status, payload = _request(
            server_url,
            "GET",
            "/v1/scrape?url=https%3A%2F%2Fexample.com%2F",
            headers={"Referer": "https://evil.example/steal"},
        )
        assert status == 403
        assert payload["ok"] is False
        assert calls == []

    def test_cross_site_post_rejected(
        self, server_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_fetch(monkeypatch)
        status, payload = _request(
            server_url,
            "POST",
            "/v1/scrape",
            body=json.dumps({"url": "https://example.com/"}).encode("utf-8"),
            headers={"content-type": "application/json", "Origin": "https://evil.example"},
        )
        assert status == 200  # 与既有 POST 路由一致：ok=false 的 200 响应
        assert payload["ok"] is False
        assert calls == []

    def test_host_mismatch_rejected(self, server_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _patch_fetch(monkeypatch)
        status, payload = _request(
            server_url,
            "GET",
            "/v1/scrape?url=https%3A%2F%2Fexample.com%2F",
            headers={"Host": "evil.example"},
        )
        assert status == 403
        assert payload["ok"] is False
        assert calls == []

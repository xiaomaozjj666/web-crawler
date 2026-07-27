"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import sys
import threading
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# curl_cffi's async backend needs the selector loop on Windows (Proactor lacks
# add_reader). Switch policy once for the whole session before tests import it.
# The policy setter is deprecated for removal in Python 3.16 but still required
# for curl_cffi on Windows today; suppress the deprecation noise in test runs.
if sys.platform == "win32":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class _Handler(BaseHTTPRequestHandler):
    """Serves a small canned HTML page and a JSON endpoint for fetcher tests."""

    def log_message(self, *args: object) -> None:  # silence test noise
        pass

    def do_GET(self) -> None:
        if self.path == "/":
            body = b"""<!DOCTYPE html>
<html><head><title>Home</title></head>
<body>
  <div id="main">
    <h1>Welcome</h1>
    <a class="link" href="/page2">page 2</a>
    <a class="link" href="https://other.example/external">external</a>
    <ul id="items">
      <li class="item"><span class="name">Apple</span><span class="price">1.0</span></li>
      <li class="item"><span class="name">Banana</span><span class="price">2.0</span></li>
      <li class="item"><span class="name">Cherry</span><span class="price">3.0</span></li>
    </ul>
  </div>
</body></html>"""
            self._send(200, body, "text/html; charset=utf-8")
        elif self.path == "/page2":
            self._send(
                200, b"<html><body><h1>Page 2</h1></body></html>", "text/html; charset=utf-8"
            )
        elif self.path == "/json":
            self._send(200, b'{"ok": true, "n": 42}', "application/json")
        elif self.path == "/404":
            self._send(404, b"not found", "text/plain")
        else:
            self._send(404, b"unknown path", "text/plain")

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="session")
def local_server() -> str:
    """Start a local HTTP server on an ephemeral port; yield its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture
def tmp_storage(tmp_path: Path):
    """Return an AdaptiveStorage backed by a temp sqlite file."""
    from web_crawler.parser.adaptive import AdaptiveStorage

    store = AdaptiveStorage(tmp_path / "adaptive.sqlite3")
    yield store
    store.close()

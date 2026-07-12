# web-crawler

A Scrapling-aligned stealth web scraping library for Python: **adaptive selectors**, **TLS-fingerprint stealth HTTP**, **JS rendering**, and a **callback-driven spider framework** — plus an application layer that downloads web resources.

## Highlights

- **Adaptive parser** — `Selector` over `lxml` with element fingerprinting and structural-similarity relocation. When a site's markup changes, stored fingerprints re-find the element automatically (Scrapling's signature feature). Public `save`/`retrieve`/`relocate` API lets you manage fingerprints explicitly. Includes `find_by_regex`, `re`/`re_first`, `get_all_text`, `prettify`, full DOM traversal (`parent`/`children`/`siblings`/`next`/`previous`/`path`), and `ResultList` batch helpers (`css`/`xpath`/`get`/`getall`/`.first`/`.last`).
- **Stealth HTTP** — `Fetcher` uses `curl_cffi` to replay a real browser's TLS/JA3 fingerprint and HTTP/2 frame ordering, so requests are indistinguishable from Chrome at the network layer. Degrades to `httpx` (with a warning) when `curl_cffi` is absent. `AsyncFetcher` provides a pure async-only API surface.
- **Lazy imports** — `import web_crawler` never forces `playwright` or `curl_cffi` to load; heavy submodules resolve on first access (Scrapling-style).
- **JS rendering** — `DynamicFetcher` drives Playwright/Chromium to render dynamic pages, block resources for speed, and wait on selectors.
- **Anti-bot** — `StealthyFetcher` injects fingerprint-patching JS, humanizes mouse/scroll, and best-effort solves Cloudflare challenges.
- **Proxy rotation** — `ProxyPool` with round-robin/random strategies and per-proxy failure cooldown.
- **Spider framework** — `Spider`/`Request` with callback dispatch, priority scheduling, domain filtering, dedup, and JSON-based pause/resume.
- **Unified `Response`** — every fetcher returns the same `Response` with `.css()` / `.xpath()` / `.json()` helpers.

## Requirements

- Python 3.10+
- `lxml`, `cssselect`, `httpx`, `beautifulsoup4`
- `curl_cffi` (stealth HTTP) — optional but recommended
- `playwright` (JS rendering) — optional; run `playwright install chromium` after installing

## Installation

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
playwright install chromium      # only needed for DynamicFetcher / StealthyFetcher
```

Run with `PYTHONPATH=src` so the `src/web_crawler/` package is importable.

## Architecture

```
src/web_crawler/          # Scrapling-aligned core library
  _types.py               # TextHandler / Attrs / ResultList
  compat.py               # optional-dependency detection (graceful degradation)
  response.py             # unified Response (selector helpers, meta, urljoin)
  parser/
    selector.py           # Selector + Adaptors (CSS/XPath/text/similarity/adaptive)
    adaptive.py           # compute_fingerprint + similarity_score + best_match
    storage.py            # AdaptiveStorage (thread-safe SQLite for fingerprints)
  fetchers/
    _base.py              # BaseFetcher (shared config + response building)
    fetcher.py            # Fetcher + AsyncFetcher (curl_cffi TLS stealth, httpx fallback)
    dynamic.py            # DynamicFetcher (Playwright JS rendering)
    stealthy.py           # StealthyFetcher (anti-bot / Cloudflare)
    proxy.py              # ProxyPool (rotation + cooldown)
  spider/
    spider.py             # Spider + Request + SpiderStats (pause/resume)
  py.typed                # PEP 561 type marker
app/                      # application layer
  crawler.py              # resource downloader (concurrent, resume, dedup, UI-driven)
  ui.py                   # local web UI
tests/                    # pytest suite (167 tests)
benchmarks.py             # parser/fetcher micro-benchmarks
CHANGELOG.md              # version history
```

## Quick start

### Stealth HTTP fetch

```python
from web_crawler import Fetcher

with Fetcher(impersonate="chrome131", timeout=30.0) as f:
    resp = f.get("https://example.com")
    print(resp.status, resp.css_first("h1").text)
```

### Adaptive parsing (survives markup changes)

```python
from web_crawler import Selector, AdaptiveStorage

storage = AdaptiveStorage()  # ~/.web_crawler/adaptive.sqlite3

# First run: save the element fingerprint
page = Selector(html_v1, url="https://shop.example.com", adaptive=True, storage=storage)
title = page.css_first("#product-title", auto_save=True)

# Later, after the site redesigns and the id is gone...
page2 = Selector(html_v2, url="https://shop.example.com", adaptive=True, storage=storage)
relocated = page2.css_first("#product-title", adaptive=True)  # found by similarity
```

### Spider framework

```python
from web_crawler import Spider, Request, Fetcher

class QuotesSpider(Spider):
    start_urls = ["https://quotes.toscrape.com/"]
    allowed_domains = ["quotes.toscrape.com"]

    def parse(self, response):
        for quote in response.css("div.quote"):
            yield {"text": quote.css_first(".text").text,
                   "author": quote.css_first(".author").text}
        nxt = response.css_first("li.next > a")
        if nxt:
            yield Request(response.urljoin(nxt.attr("href")))

items = QuotesSpider(fetcher=Fetcher(impersonate="chrome131")).run()
# Pause mid-run, resume later:
# spider.run(state_file="state.json")          # pauses -> state saved
# QuotesSpider(fetcher=Fetcher()).run(state_file="state.json", resume=True)
```

### JS rendering & anti-bot

```python
from web_crawler import DynamicFetcher, StealthyFetcher

# Render a JS-heavy page
with DynamicFetcher(headless=True, wait_selector="div.content") as f:
    resp = f.fetch("https://spa.example.com")

# Stealth mode: humanized input + Cloudflare-aware
with StealthyFetcher(google_search=True, humanize=True) as f:
    resp = f.fetch("https://protected.example.com")
```

### Proxy rotation

```python
from web_crawler import Fetcher, ProxyPool

pool = ProxyPool(["http://u:p@proxy1:8080", "http://proxy2:3128"],
                 strategy="round_robin", max_failures=3, cooldown=60)
with Fetcher(proxy=pool, impersonate="chrome131") as f:
    resp = f.get("https://example.com")
```

## Application: resource downloader

`app/crawler.py` is a concurrent web resource downloader with resume, dedup,
sitemap discovery, and a local web UI (`app/ui.py`).

```bash
python app/crawler.py --url https://example.com --out ./out --workers 8
python app/crawler.py --url https://example.com --stealth --impersonate chrome131
python app/ui.py --open     # launch the local web UI
```

`--stealth` routes page-HTML and non-resumable fetches through the library's
`Fetcher` (curl_cffi TLS fingerprinting) to bypass JA3/JA4 fingerprint blocking.
Large/resumable downloads keep the streaming urllib path.

## Development

```bash
ruff check .          # lint
ruff format .         # format
mypy src              # type-check
pytest --cov=web_crawler   # tests + coverage
python benchmarks.py       # parser/adaptive micro-benchmarks
```

CI (`.gitlab-ci.yml`) runs lint, type-check, and tests with coverage on every push.

## License

MIT. See [LICENSE](LICENSE).

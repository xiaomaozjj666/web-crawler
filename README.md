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
- **AI-assisted scraping** — `AIExtractor` turns a plain-language field schema into validated CSS selectors; `AIScrapeAgent` orchestrates fetch + extract with robots.txt respect, 429/503 back-off (`Retry-After` honored), and "stuck → hand-off to human" semantics.
- **JS reverse-engineering agent** — `ReverseAgent` runs an observe→think→act loop over a target URL using `CamoufoxFetcher` + DeepSeek-V4-Pro: injects JS hooks (fetch / XHR / cookie / `crypto.subtle` / webpack / console), captures network traffic, splits webpack bundles, then asks the LLM to deobfuscate and reimplement signing algorithms in Python. Exposed via both an MCP server (`web-crawler-mcp`) and a CLI (`web-crawler-reverse`).

## Requirements

- Python 3.10+
- `lxml`, `cssselect`, `httpx`, `beautifulsoup4`
- `curl_cffi` (stealth HTTP) — optional but recommended
- `playwright` (JS rendering) — optional; run `playwright install chromium` after installing

## Installation

Recommended: editable install via the packaged entry points.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"          # parser + tests + lint/types
# Optional extras (combine as needed):
pip install -e ".[all]"          # + curl_cffi TLS stealth + Playwright JS rendering
pip install -e ".[camoufox]"     # + Camoufox fingerprint-resistant Firefox
pip install -e ".[mcp]"           # + MCP server / CLI (implies camoufox)
playwright install chromium       # only needed for DynamicFetcher / StealthyFetcher
```

This installs the `web_crawler` package plus the `web-crawler`, `crawler-ui`,
`web-crawler-mcp`, and `web-crawler-reverse` console commands, so no
`PYTHONPATH` tweaking is needed.

Alternatively, run from the source tree without installing:

```bash
pip install httpx beautifulsoup4 lxml cssselect pytest pytest-asyncio ruff mypy
PYTHONPATH=src pytest
```

## Architecture

```
src/web_crawler/          # Scrapling-aligned core library
  _types.py               # TextHandler / Attrs / ResultList
  compat.py               # optional-dependency detection (graceful degradation)
  response.py             # unified Response (selector helpers, meta, urljoin)
  crawler.py              # async same-domain crawler (robots.txt-aware)
  parser/
    selector.py           # Selector + Adaptors (CSS/XPath/text/similarity/adaptive)
    adaptive.py           # compute_fingerprint + similarity_score + best_match
    storage.py            # AdaptiveStorage (thread-safe SQLite for fingerprints)
    visual.py             # VisualExtractor (PixelRAG-style screenshot-tile VLM)
  fetchers/
    _base.py              # BaseFetcher (shared config + response building)
    fetcher.py            # Fetcher + AsyncFetcher (curl_cffi TLS stealth, httpx fallback)
    dynamic.py            # DynamicFetcher (Playwright JS rendering)
    stealthy.py           # StealthyFetcher (anti-bot / Cloudflare)
    camoufox.py           # CamoufoxFetcher (fingerprint-resistant Firefox)
    proxy.py              # ProxyPool (rotation + cooldown)
  spider/
    spider.py             # Spider + Request + SpiderStats (pause/resume)
  ai/                     # AI-assisted scraping + JS reverse-engineering suite
    llm.py                # LLMProvider (OpenAI-compatible, DeepSeek default)
    extractor.py          # AIExtractor (CSS selector generation + self-heal)
    agent.py              # AIScrapeAgent (robots-aware polite crawler)
    hooks.py              # JS Hook library (fetch/XHR/cookie/crypto/webpack/console)
    analyzer.py           # JSAnalyzer (webpack module extraction + AI deobfuscation)
    captcha.py            # CaptchaManager (detect + humanize-only solve)
    reverse_agent.py      # ReverseAgent (observe→think→act loop)
    vision.py             # VisionObserver (Vision-LLM 截图感知双模态)
    planner.py            # Planner/Actor 双脑分离 + 周期重规划
    loop.py               # LoopDetector + ContextCompressor (循环检测 + 历史压缩)
    judge.py               # TaskJudge (done 二次验证，防止 LLM 幻觉)
    watchdog.py           # EventBus + Heartbeat + CrashRecovery (崩溃自愈)
    recorder.py           # RunRecorder (成功路径编译为确定性脚本)
    schema.py             # SchemaValidator (结构化抽取 Pydantic 校验)
    dom_pruner.py         # DomPruner (DOM 焦点裁剪，Skyvern/browser-use 风格)
    checkpoint.py         # CheckpointManager (断点续跑 + 状态持久化)
    budget.py             # BudgetTracker (Token 预算管理，单步/全局/单次)
    confidence.py         # ConfidenceScorer (动作置信度评分，规则 + LLM 双路径)
    guardrails.py         # ActionGuard (危险动作护栏，白名单 + 跨域拦截)
  mcp/                    # MCP server + CLI exposing the reverse-agent tools
    server.py             # ReverseMCPServer (JSON-RPC over stdio)
    cli.py                # web-crawler-reverse command-line interface
  py.typed                # PEP 561 type marker
app/                      # application layer
  crawler.py              # resource downloader (concurrent, resume, dedup, UI-driven)
  ui.py                   # local web UI
tests/                    # pytest suite
benchmarks.py             # parser/fetcher micro-benchmarks
demo.py / demo.bat        # interactive usage demo
start.bat                 # launch the local web UI
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
            yield {"text": quote.css_first(".text").text, "author": quote.css_first(".author").text}
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

pool = ProxyPool(
    ["http://u:p@proxy1:8080", "http://proxy2:3128"],
    strategy="round_robin",
    max_failures=3,
    cooldown=60,
)
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

## JS reverse-engineering agent

The `web_crawler.ai.reverse_agent.ReverseAgent` orchestrates a Camoufox-driven
browser session through an **observe → think → act** loop, calling DeepSeek-V4-Pro
(or any OpenAI-compatible LLM) at each step. It injects JS hooks, captures network
traffic, splits webpack bundles, then asks the LLM to deobfuscate and reimplement
the signing algorithm in Python.

Two entry points expose the same capabilities:

### MCP server (for Claude Desktop / Cursor / etc.)

```bash
pip install -e ".[mcp]"
export DEEPSEEK_API_KEY=sk-...        # Windows: set DEEPSEEK_API_KEY=...
web-crawler-mcp                       # speaks JSON-RPC over stdio
```

Register in your AI client's MCP config:

```json
{
  "mcpServers": {
    "js-reverse": {
      "command": "web-crawler-mcp",
      "env": {"DEEPSEEK_API_KEY": "sk-..."}
    }
  }
}
```

### CLI (for shell scripts and interactive REPL)

```bash
web-crawler-reverse https://example.com --target-params anti_content sign
web-crawler-reverse analyze script.js            # deobfuscate a JS snippet
web-crawler-reverse webpack bundle.js            # extract webpack modules
web-crawler-reverse reimplement algo.js --lang python
web-crawler-reverse capture https://example.com --wait 8
web-crawler-reverse interactive                  # REPL: type `tools` to list
```

**Compliance note** — the agent only simulates normal user interaction; it does
not crack image-based captchas, forge login credentials, or bypass paywalls.
When a page is blocked (401/403) or shows a captcha it cannot solve, it stops
and returns a "hand-off to human" status.

### Mainstream-agent alignment

Beyond the base observe→think→act loop, `ReverseAgent` integrates the
following capabilities commonly seen in production agent frameworks
(browser-use / Skyvern / PentAGI / LangGraph):

| Capability | Module | Purpose |
| --- | --- | --- |
| DOM focus pruning | `ai.dom_pruner.DomPruner` | Rule + LLM rerank, keeps only encryption-related elements (≈80% token cut) |
| Checkpoint / resume | `ai.checkpoint.CheckpointManager` | Step-end state persisted; resume after crash or interrupt |
| Token budget | `ai.budget.BudgetTracker` | Per-step / per-call / global caps with COMPRESS/DOWNGRADE/STOP policies |
| Action confidence | `ai.confidence.ConfidenceScorer` | Rule + LLM dual-path scoring; low-confidence actions trigger fallback |
| Action guardrails | `ai.guardrails.ActionGuard` | Domain whitelist, blocks localhost/non-HTTPS/cross-origin/dangerous scripts |
| Planner / Actor split | `ai.planner.Planner` | High-level sub-goal planning + periodic replanning |
| Loop detection | `ai.loop.LoopDetector` | Page-state fingerprint; auto-replan on repeated states |
| Context compression | `ai.loop.ContextCompressor` | Rolling summary of history; `force_compress` on budget overflow |
| Task judge | `ai.judge.TaskJudge` | Independent LLM verifies `done` to prevent hallucinated success |
| Success recorder | `ai.recorder.RunRecorder` | Compile a successful trace into a deterministic Python script |
| Event bus + watchdog | `ai.watchdog` | Pub/sub events, heartbeat stall detection, browser crash auto-recovery |
| Structured schema | `ai.schema.SchemaValidator` | Pydantic-based result validation with auto-repair hints |
| Vision observer | `ai.vision.VisionObserver` | Vision-LLM screenshots for DOM-obfuscated / Canvas-rendered pages |

All capabilities are optional and individually toggleable via
`ReverseAgentConfig` fields (e.g. `enable_checkpoint=True`,
`budget_total=100_000`, `min_confidence=0.4`, `enable_guard=True`).


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

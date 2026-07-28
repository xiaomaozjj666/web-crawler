# web-crawler

A Scrapling-aligned stealth web scraping library for Python: **adaptive selectors**, **TLS-fingerprint stealth HTTP**, **JS rendering**, and a **callback-driven spider framework** — plus an application layer that downloads web resources.

## Highlights

- **Adaptive parser** — `Selector` over `lxml` with element fingerprinting and structural-similarity relocation. When a site's markup changes, stored fingerprints re-find the element automatically (Scrapling's signature feature). Public `save`/`retrieve`/`relocate` API lets you manage fingerprints explicitly. Includes `find_by_regex`, `re`/`re_first`, `get_all_text`, `prettify`, full DOM traversal (`parent`/`children`/`siblings`/`next`/`previous`/`path`), and `ResultList` batch helpers (`css`/`xpath`/`get`/`getall`/`.first`/`.last`).
- **Stealth HTTP** — `Fetcher` uses `curl_cffi` to replay a real browser's TLS/JA3 fingerprint and HTTP/2 frame ordering, so requests are indistinguishable from Chrome at the network layer. Supports `ja4_fingerprint` for fine-grained TLS extension customization (passed through to `curl_cffi`'s `ja3` parameter, overriding the `impersonate` preset). Degrades to `httpx` (with a warning) when `curl_cffi` is absent. `AsyncFetcher` provides a pure async-only API surface.
- **Lazy imports** — `import web_crawler` never forces `playwright` or `curl_cffi` to load; heavy submodules resolve on first access (Scrapling-style).
- **JS rendering** — `DynamicFetcher` drives Playwright/Chromium to render dynamic pages, block resources for speed, and wait on selectors.
- **Anti-bot** — `StealthyFetcher` injects fingerprint-patching JS, humanizes mouse/scroll, and best-effort solves Cloudflare challenges.
- **Proxy rotation** — `ProxyPool` with round-robin/random strategies and per-proxy failure cooldown.
- **Spider framework** — `Spider`/`Request` with callback dispatch, priority scheduling, domain filtering, dedup, and JSON-based pause/resume.
- **Unified `Response`** — every fetcher returns the same `Response` with `.css()` / `.xpath()` / `.json()` helpers.
- **AI-assisted scraping** — `AIExtractor` turns a plain-language field schema into validated CSS selectors; `AIScrapeAgent` orchestrates fetch + extract with robots.txt respect, 429/503 back-off (`Retry-After` honored), and "stuck → hand-off to human" semantics.
- **JS reverse-engineering agent** — `ReverseAgent` runs an observe→think→act loop over a target URL using `CamoufoxFetcher` + DeepSeek-V4-Pro: injects JS hooks (fetch / XHR / cookie / `crypto.subtle` / webpack / console), captures network traffic, splits webpack bundles, then asks the LLM to deobfuscate and reimplement signing algorithms in Python. Supports 6 real browser interaction actions (`click` / `type` / `scroll` / `press` / `hover` / `select_option`) via Playwright, plus 3 multi-tab actions (`new_tab` / `switch_tab` / `close_tab`) and humanized input trajectories (hover-before-click, per-keystroke random delay). Dangerous-click guardrails and selector-injection blocking built in. Exposed via both an MCP server (`web-crawler-mcp`) and a CLI (`web-crawler-reverse`). Web UI uses SSE real-time push (`/reverse/stream`) for live step events.
- **Image captcha solver** — `ImageCaptchaSolver` recognizes three image-challenge families without a browser: text OCR (4–8 char alphanumeric), slider gap localization (Pillow + numpy template matching), and click-order coordinate recognition (Vision-LLM). Falls back across backends: local `ddddocr` → `numpy` template match → LLM Vision (`gpt-4o` / Claude / Qwen-VL / DeepSeek-vision). `CaptchaSolver` injects the image solver automatically when `ReverseAgentConfig.enable_image_captcha=True` (default); hCaptcha / reCAPTCHA v2 image challenges and GeeTest puzzles are then auto-solved end-to-end. Exposed as the `solve_captcha_image` MCP tool and `captcha-image` CLI subcommand.
- **Lightweight pentest toolkit** — `web_crawler.pentest` subpackage provides pure-Python, dependency-free reconnaissance utilities inspired by PentAGI's tool-integration approach: `PortScanner` (TCP connect + TOP-100), `DirBruter` (60+ common paths), `SubdomainEnumerator` (80+ prefix dictionary), `VulnScanner` (SQL injection / XSS / path traversal detection, rule-based with optional LLM analysis), `HeaderChecker` (8 security headers + A–F grade), aggregated by `PentestReport`. Exposed as the `pentest_recon` MCP tool and `pentest` CLI subcommand. Compliance notice: authorized testing only.

## Requirements

- Python 3.10+
- `lxml`, `cssselect`, `httpx`, `beautifulsoup4`
- `curl_cffi` (stealth HTTP) — optional but recommended
- `playwright` (JS rendering) — optional; run `playwright install chromium` after installing

## Installation

Install from PyPI:

```bash
pip install web-crawler
```

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

## Docker

```bash
# 构建镜像
docker build -t web-crawler .

# 运行容器
docker run -p 8765:8765 -e DEEPSEEK_API_KEY=your-key web-crawler

# 或用 docker-compose
docker-compose up -d
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
    captcha.py            # CaptchaManager (detect + humanize + image_solver injection)
    image_captcha.py     # ImageCaptchaSolver (OCR / slider gap / click coord, LLM Vision + ddddocr/Pillow fallback)
    reverse_agent.py      # ReverseAgent (observe→think→act loop)
    planner.py            # Planner/Actor 双脑分离 + 周期重规划
    loop.py               # LoopDetector + ContextCompressor (循环检测 + 历史压缩)
    judge.py               # TaskJudge (done 二次验证，防止 LLM 幻觉)
    watchdog.py           # EventBus + Heartbeat + CrashRecovery (崩溃自愈)
    recorder.py           # RunRecorder (成功路径编译为确定性脚本)
    schema.py             # SchemaValidator (结构化抽取 Pydantic 校验)
    dom_pruner.py         # DomPruner (DOM 焦点裁剪，Skyvern/browser-use 风格)
    checkpoint.py         # CheckpointManager (断点续跑 + 状态持久化)
    confidence.py         # ConfidenceScorer (动作置信度评分，规则 + LLM 双路径)
    guardrails.py         # ActionGuard (危险动作护栏，白名单 + 跨域拦截)
  pentest/                # 轻量渗透辅助工具集（纯 Python，无外部命令依赖）
    port_scanner.py       # PortScanner（TCP connect + TOP-100 + 服务名映射）
    dir_bruter.py         # DirBruter（目录/文件路径爆破 + 标题提取）
    subdomain.py          # SubdomainEnumerator（80+ 子域前缀字典枚举）
    vuln_scanner.py       # VulnScanner（SQL 注入 / XSS / 路径穿越规则检测）
    header_check.py       # HeaderChecker（8 项安全头检测 + A–F 评级）
    report.py             # PentestReport（聚合所有结果 + summary/to_dict/to_json）
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

The `reverse_engineer_url` tool returns the full runtime state in one call:
`analysis`, `compiled_script`, `judge_result`, plus `budget_summary`,
`last_confidence`, `checkpoints`, `screenshots`, and `error_screenshot` — so
upstream AI clients can render progress / step lists / screenshot galleries
without polling a second endpoint.

### CLI (for shell scripts and interactive REPL)

```bash
web-crawler-reverse https://example.com --target-params anti_content sign
web-crawler-reverse analyze script.js            # deobfuscate a JS snippet
web-crawler-reverse webpack bundle.js            # extract webpack modules
web-crawler-reverse reimplement algo.js --lang python
web-crawler-reverse capture https://example.com --wait 8
web-crawler-reverse interactive                  # REPL: type `tools` to list
```

### CLI `run` subcommand (full agent, no MCP)

`run` constructs a `ReverseAgent` directly and calls `run()`, bypassing the MCP
transport. It exposes the full set of guard / checkpoint / screenshot
flags, can save the success-path script to a file, and emits a complete JSON
result (`last_confidence`, `checkpoints`, `screenshots`,
`error_screenshot`) to stdout or `--output`:

```bash
# Headless run, save the compiled script, write JSON result to result.json
web-crawler-reverse run \
  --url https://target.example.com \
  --task "提取 Anti-Content 签名参数" \
  --target-params anti_content,sign \
  --max-steps 20 --headless \
  --enable-checkpoint \
  --min-confidence 0.4 \
  --enable-screenshot \
  --save-script ./out/sign.py \
  --output ./out/result.json

# Quick foreground run with visible browser, stdout JSON
web-crawler-reverse run --url https://example.com
```

`--enable-screenshot` (default on) saves a PNG per observation step and on every
error path to `reverse_screenshots/<task_id>_step<N>[_error].png`; failures are
swallowed so the main loop never crashes on a screenshot error.

**Compliance note** — the agent only simulates normal user interaction; it does
not forge login credentials or bypass paywalls. Image-based captchas (text OCR,
slider gap, click-order) are auto-solved via `ImageCaptchaSolver` when
`enable_image_captcha=True` (default); for unsolvable challenges the agent
stops and returns a "hand-off to human" status.

### Mainstream-agent alignment

Beyond the base observe→think→act loop, `ReverseAgent` integrates the
following capabilities commonly seen in production agent frameworks
(browser-use / Skyvern / PentAGI / LangGraph):

| Capability | Module | Purpose |
| --- | --- | --- |
| DOM focus pruning | `ai.dom_pruner.DomPruner` | Rule + LLM rerank, keeps only encryption-related elements (≈80% token cut) |
| Checkpoint / resume | `ai.checkpoint.CheckpointManager` | Step-end state persisted; resume after crash or interrupt |
| Action confidence | `ai.confidence.ConfidenceScorer` | Rule + LLM dual-path scoring; low-confidence actions trigger fallback |
| Action guardrails | `ai.guardrails.ActionGuard` | Domain whitelist, blocks localhost/non-HTTPS/cross-origin/dangerous scripts |
| Planner / Actor split | `ai.planner.Planner` | High-level sub-goal planning + periodic replanning |
| Loop detection | `ai.loop.LoopDetector` | Page-state fingerprint; auto-replan on repeated states |
| Context compression | `ai.loop.ContextCompressor` | Rolling summary of history; `force_compress` on budget overflow |
| Task judge | `ai.judge.TaskJudge` | Independent LLM verifies `done` to prevent hallucinated success |
| Success recorder | `ai.recorder.RunRecorder` | Compile a successful trace into a deterministic Python script |
| Event bus + watchdog | `ai.watchdog` | Pub/sub events, heartbeat stall detection, browser crash auto-recovery |
| Structured schema | `ai.schema.SchemaValidator` | Pydantic-based result validation with auto-repair hints |

All capabilities are optional and individually toggleable via
`ReverseAgentConfig` fields (e.g. `enable_checkpoint=True`,
`min_confidence=0.4`, `enable_guard=True`).

### Multi-tab management & humanized input

`ReverseAgent` supports 3 multi-tab actions on top of the 6 browser
interaction actions:

| Action | Params | Behavior |
| --- | --- | --- |
| `new_tab` | `url`, `name` (optional) | Open a new tab, navigate, switch `self._page` to it; main page registered as `"main"` |
| `switch_tab` | `name` **or** `index` | Switch active page by name or insertion-order index; calls `bring_to_front` |
| `close_tab` | `name` | Close the tab; if it was active, `self._page` falls back to `main` |

`ReverseAgentConfig.humanize_input=True` (default) enables trajectory
simulation to evade anti-bot detection:

- **`click`** — `hover(selector)` moves the cursor first, then a random
  50–200 ms delay, then `click`
- **`type`** — `focus(selector)`, a 100–300 ms "thinking" pause, then
  `type(text, delay=30–150ms)` for per-keystroke rhythm

Both sync (`run`) and async (`arun`) paths implement the humanized variants;
mock objects that don't accept `delay=` auto-degrade via `TypeError` fallback.

### JA4 fingerprint customization

`Fetcher(ja4_fingerprint=...)` passes a JA3/JA4 TLS extension string through
to `curl_cffi`'s `ja3` parameter, overriding the `impersonate` preset's
default TLS fingerprint. This enables fine-grained customization of the
TLS ClientHello (cipher order, extensions, supported groups) beyond the
built-in browser presets:

```python
from web_crawler import Fetcher

# Use Chrome 131's HTTP/2 frame ordering but a custom JA4 TLS fingerprint
with Fetcher(
    impersonate="chrome131",
    ja4_fingerprint="t13d1516h2_8daaf6152771_b0da82dd1658",
) as f:
    resp = f.get("https://example.com")
```

When `curl_cffi` is not installed (httpx fallback), `ja4_fingerprint` is
silently ignored (httpx has no TLS fingerprint capability). Both sync
`Fetcher` and `AsyncFetcher` honor the parameter.

### Single-model strategy

`ReverseAgent` uses a **single DeepSeek V4 Pro** instance shared by every
sub-component — Planner, Actor, Judge, DomPruner, ConfidenceScorer,
JSAnalyzer all reuse the same `DeepSeekProvider(model="deepseek-v4-pro")`.
There is no per-component model routing, no LLM-as-judge rerank, no
capability-based provider selection. Override only if you bring your own multi-model
setup; the defaults assume DeepSeek V4 Pro everywhere.


## Documentation

An API documentation site is built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/)
+ [mkdocstrings](https://mkdocstrings.github.io/). Source lives in `docs/`,
config in `mkdocs.yml`.

```bash
pip install -e ".[docs]"
mkdocs serve          # http://127.0.0.1:8000
mkdocs build          # static site in site/
```

The site covers:

- **Home / quick start** — install + minimal examples
- **Architecture** — module tree + per-layer responsibilities + data flow
- **JS reverse agent** — full usage guide, all actions, all config fields
- **API reference** — auto-generated from docstrings via mkdocstrings


## Development

```bash
ruff check .          # lint
ruff format .         # format
mypy src              # type-check
pytest --cov=web_crawler   # tests + coverage
python benchmarks.py       # parser/adaptive micro-benchmarks
python benchmarks.py --check-regression   # CI: fail on >20% regression vs built-in baseline
```

CI (`.gitlab-ci.yml`) runs lint, type-check, tests with coverage, a
benchmark regression check, and a docs build on every push. Slow tests
(marked `@pytest.mark.slow`, e.g. the Camoufox end-to-end suite) are
excluded from the default CI test run via `-m "not slow"`.

## License

MIT. See [LICENSE](LICENSE).

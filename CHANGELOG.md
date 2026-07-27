# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **JS reverse-engineering agent** (`web_crawler.ai.reverse_agent.ReverseAgent`):
  Camoufox-driven browser observe→think→act loop powered by DeepSeek-V4-Pro.
  Injects JS hooks (fetch / XHR / cookie / `crypto.subtle` / webpack / console),
  captures network traffic, splits webpack bundles, then asks the LLM to
  deobfuscate and reimplement signing algorithms in Python.
- **JS Hook library** (`web_crawler.ai.hooks.HookLibrary`) with 6 hooks and a
  combined-script generator; `collect_hook_data` sync/async helpers.
- **JSAnalyzer** (`web_crawler.ai.analyzer`) — webpack module extraction,
  signing-flow tracing, AI deobfuscation and algorithm reimplementation.
- **CaptchaManager** (`web_crawler.ai.captcha`) — detects Turnstile / hCaptcha /
  reCAPTCHA v2&v3 / 极验 GeeTest; only simulates normal user interaction (入口
  click / token-wait / humanized drag). Image challenges hand off to human.
- **CamoufoxFetcher** (`web_crawler.fetchers.camoufox`) — fingerprint-resistant
  Firefox via Camoufox + Playwright, with hook-injection and network-capture
  helpers.
- **MCP server** (`web_crawler.mcp.server.ReverseMCPServer`) — exposes 9 tools
  over JSON-RPC/stdio for AI clients (Claude Desktop / Cursor). Falls back to a
  manual stdio implementation when the `mcp` SDK is missing.
- **CLI** (`web-crawler-reverse`) with 10 subcommands plus an interactive REPL.
- New `[project.optional-dependencies]`: `camoufox`, `mcp`.
- New entry points: `web-crawler-mcp`, `web-crawler-reverse`.

### Changed
- `pyproject.toml`: bumped `[tool.mypy].python_version` to `"3.12"` (modern
  stubs like numpy 2.5 use PEP 695 `type` statements). `requires-python` still
  `>=3.10`; runtime compat is verified by the test suite.
- Removed `requirements.txt` / `requirements-dev.txt` — install via
  `pip install -e ".[dev]"` (or `.[mcp,dev]`, `.[all,dev]`). CI updated.
- README: refreshed architecture tree, updated test count to 224, added the JS
  reverse-agent / MCP / CLI sections, documented all installation extras.

## [0.2.1] — 2026-07-12

### Fixed
- UI: `rewrite_html` now has its own checkbox (was incorrectly wired to `strip_overlays`)
- `app/crawler.py`: `print()` → `_log.error()` for header parse errors
- `app/ui.py`: startup messages now use `logging` instead of `print()`

### Changed
- `playwright` and `curl_cffi` moved to `[project.optional-dependencies] all` — no longer forced dependencies
- Added `[project.scripts]` entry points: `web-crawler` and `crawler-ui`
- Updated LICENSE copyright to `xiaomaozjj666`
- Expanded `.gitignore` to cover runtime-generated files

## [0.2.0] — 2026-06-27

### Added
- **Lazy imports**: `import web_crawler` no longer forces `playwright` or
  `curl_cffi` to load. Public symbols are resolved via `__getattr__` on first
  access (Scrapling-style), so parser-only users pay no optional-dependency
  import cost.
- **`AsyncFetcher`** — a dedicated async-only fetcher class (Scrapling
  `AsyncFetcher` parity). Shares all config/retry/stealth logic with `Fetcher`
  but exposes only async methods (`get`/`post`/`request` are coroutines); no
  sync session is ever created.
- **Adaptive public API on `Selector`**:
  - `save(element, identifier)` — explicitly persist a fingerprint.
  - `retrieve(identifier)` — fetch a stored fingerprint record.
  - `relocate(element, threshold=)` — manually relocate an element by
    structural similarity against the current document.
- **DOM traversal** (Scrapling parity): `siblings`, `next`, `previous`,
  `path` (root-to-self ancestor chain).
- **Selector methods** (Scrapling parity): `find_by_regex`, `re`, `re_first`,
  `get_all_text`, `prettify`.
- **`ResultList` batch helpers** (Scrapling `Selectors` parity): `get`,
  `getall`, `css`, `xpath` (batch), `.length`, slice-safe typing, and
  property-style `.first` / `.last`.
- **Backward-compat aliases**: `Adaptor = Selector`, `Adaptors = Selectors`.
- **PEP 561**: `py.typed` marker shipped with the package.
- `pyproject.toml` metadata aligned with Scrapling: full `classifiers`,
  `keywords`, `authors`, `[project.urls]`.
- `CHANGELOG.md` introduced.

### Changed
- `Fetcher` backend imports (`curl_cffi`, `httpx`) are now deferred to session
  construction time instead of module import time.
- `first` / `last` on `ResultList` changed from callables to properties
  (Scrapling `Selectors.first` / `.last` parity). `css_first` / `xpath_first`
  still accept a `default` argument.
- `fetchers/__init__.py` lazily imports `DynamicFetcher` / `StealthyFetcher`
  (Playwright) so `from web_crawler.fetchers import Fetcher` stays cheap.

## [0.1.0] — initial release

- Adaptive `Selector` over `lxml` with fingerprinting + similarity relocation.
- `Fetcher` (curl_cffi TLS stealth, httpx fallback), `DynamicFetcher`
  (Playwright), `StealthyFetcher` (anti-bot / Cloudflare).
- `ProxyPool` with round-robin/random rotation and cooldown.
- `Spider` framework with callback dispatch, priority scheduling, dedup,
  domain filtering, and JSON pause/resume.
- Unified `Response` with `.css()` / `.xpath()` / `.json()` helpers.

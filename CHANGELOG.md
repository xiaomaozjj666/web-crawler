# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Browser interaction actions**: `ReverseAgent` now supports 6 real
  Playwright browser actions — `click`, `type`, `scroll`, `press`, `hover`,
  `select_option` — in both sync `run` and async `arun` paths. All actions
  use 10s timeout, auto-screenshot on failure, and emit `browser.action`
  events for UI. LLM prompt extended with action descriptions.
- **ActionGuard** gains 2 new rules: `no-dangerous-click` (blocks clicks on
  删除/delete/logout/退出/withdraw/提现/支付 buttons) and
  `no-selector-injection` (blocks JS injection in CSS selectors).
- **ConfidenceScorer** `_VALID_ACTIONS` extended with 6 browser action types
  and their required-parameter validation.
- **RunRecorder** compile method extended to compile 6 browser actions into
  replay scripts; also fixes a pre-existing indentation bug that prevented
  multi-step paths from generating valid Python.
- **SSE real-time push** (`GET /reverse/stream?id=...`): Server-Sent Events
  endpoint streams `snapshot` / `step` / `final` events to the browser,
  replacing 800ms HTTP polling. Frontend auto-degrades to polling on SSE
  error.
- **MCP progress token**: `reverse_engineer_url` now constructs a progress
  token, subscribes to ReverseAgent's EventBus `step.end` events, and
  actually calls `report_progress` to push progress notifications to MCP
  clients (Cursor / Claude Desktop).
- **Docker deployment**: `Dockerfile` (python:3.12-slim + Firefox ESR +
  Playwright deps), `.dockerignore`, `docker-compose.yml` (port 8765 +
  volume mounts + DEEPSEEK_API_KEY env), `.github/workflows/release.yml`
  (push `v*` tag triggers build + PyPI publish + GitHub Release).
- **`--host` CLI flag** for `app/ui.py` (default 127.0.0.1; use 0.0.0.0 in
  Docker).
- **26 new tests**: browser action execution (sync + async), confidence
  scoring, guard blocking, recorder compilation, Action parsing, prompt
  content. Test count: 260 → 286.
- **Single-model strategy**: `ReverseAgentConfig.budget_total` and
  `budget_per_step` default to `None` (budget disabled by default). UI
  removes "LLM rerank" / "LLM scoring" toggles. `BudgetPolicy.DOWNGRADE`
  documented as no-op under single-model strategy.

### Changed
- `pyproject.toml` `[tool.setuptools.packages.find]` extended to include
  `app` package.
- Web UI right panel replaced "Token budget" ring with "Task statistics"
  card (steps / avg step time / elapsed / hook count / network count).
- `app/ui.py` polling logic refactored: `pollReverse` split into
  `updateReverseUI` (reusable by SSE) + `appendReverseEvent` (incremental).

### Removed
- Web UI "Token budget" panel, budget fieldset, budget JS update logic,
  `ReverseJobState.budget_*` fields, budget event handlers.

### Added (previous)
- **Screenshot capture** (`ReverseAgent.enable_screenshot`): every observation
  step and every think/act error path now saves a PNG to
  `reverse_screenshots/<task_id>_step<N>[_error].png`. Failures are swallowed
  (empty path returned) so the main loop never crashes on a screenshot error.
  Both sync `run` and async `arun` paths covered; `_screenshots` list and
  `_last_error_screenshot` are exposed in the result dict.
- **CLI `run` subcommand** (`web-crawler-reverse run`): one-shot agent
  execution that bypasses MCP and calls `ReverseAgent.run` directly. Supports
  `--enable-screenshot` / `--no-enable-screenshot`, `--save-script`,
  `--output`, and all budget/guard/checkpoint flags.
- **MCP `reverse_engineer_url`** now returns `budget_summary`,
  `last_confidence`, `checkpoints`, `screenshots`, and `error_screenshot`
  fields so upstream AI clients get full runtime state in one call.
- **Web UI enhancements** (app/ui.py):
  - Historical task list panel (`GET /reverse/jobs`) with click-to-load.
  - Screenshot gallery with click-to-zoom modal (`GET /reverse/screenshot`).
  - Task template buttons (Anti-Content / X-Bogus / _signature / generic).
  - Config import/export (`POST /reverse/config/export`,
    `POST /reverse/config/import`) with field normalization.
  - Clear-runtime-data button (`POST /reverse/clear`).
  - Adaptive polling: 800 ms while running, 3 s when idle.
  - Statistics cards (steps, avg step time, token rate, hook count, param
    hit rate).
  - Incremental event query (`GET /reverse/events?since=TS`).
  - Script download with Content-Disposition (`GET /reverse/script`).
  - `ReverseJobState` extended with `screenshots`, `error_screenshot`,
    `step_durations`, `events_since()`, `clear_runtime()`, `job_summary()`.
- **10 new tests**: screenshot success/disabled/error/failure/none-page/async
  (mock page.screenshot), CLI run arg parsing, defaults, mocked execution,
  save-script-to-file.

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
- **Mainstream-agent alignment modules** (browser-use / Skyvern / PentAGI
  feature parity), all individually toggleable via `ReverseAgentConfig`:
  - `web_crawler.ai.dom_pruner.DomPruner` — DOM focus pruning (rule + LLM
    rerank, ≈80% token cut, Skyvern/browser-use style).
  - `web_crawler.ai.checkpoint.CheckpointManager` — step-end state
    persisted; resume after crash or interrupt (`CheckpointStore` keeps the
    last N snapshots, atomic writes).
  - `web_crawler.ai.budget.BudgetTracker` — per-step / per-call / global
    token caps with COMPRESS / DOWNGRADE / STOP policies; raises
    `BudgetExceeded` when hard-stop is configured.
  - `web_crawler.ai.confidence.ConfidenceScorer` — rule + LLM dual-path
    scoring; low-confidence actions trigger fallback.
  - `web_crawler.ai.guardrails.ActionGuard` — domain whitelist, blocks
    localhost / non-HTTPS / cross-origin / dangerous scripts; supports
    custom `GuardrailRule` and CONFIRM callbacks.
  - `ReverseAgent` now wires DomPruner / Checkpoint / Budget / Confidence /
    Guard into both sync `run` and async `arun` loops; `ContextCompressor`
    gains `force_compress` / `force_compress_async` for budget-driven compaction.

### Changed
- `pyproject.toml`: bumped `[tool.mypy].python_version` to `"3.12"` (modern
  stubs like numpy 2.5 use PEP 695 `type` statements). `requires-python` still
  `>=3.10`; runtime compat is verified by the test suite.
- Removed `requirements.txt` / `requirements-dev.txt` — install via
  `pip install -e ".[dev]"` (or `.[mcp,dev]`, `.[all,dev]`). CI updated.
- README: refreshed architecture tree, added a "Mainstream-agent alignment"
  capability matrix, documented all installation extras.
- `web_crawler.ai.__init__` now lazily exports 15 alignment symbols
  (`DomPruner`, `PrunedDom`, `Checkpoint`, `CheckpointManager`,
  `CheckpointStore`, `BudgetTracker`, `TokenBudget`, `BudgetPolicy`,
  `BudgetExceeded`, `ConfidenceScorer`, `ConfidenceResult`, `ActionGuard`,
  `GuardrailResult`, `GuardrailAction`, `GuardrailRule`).
- **Single-model strategy**: `ReverseAgent` and every sub-component
  (Planner / Actor / Judge / DomPruner / ConfidenceScorer / JSAnalyzer)
  share the same `DeepSeekProvider(model="deepseek-v4-pro")` instance.
  The Web UI no longer exposes "LLM rerank" / "LLM scoring" toggles (they
  would imply a second model and contradict the single-model policy).
  `BudgetPolicy.DOWNGRADE` is documented as a no-op under this strategy;
  use `COMPRESS` (default) or `STOP` for budget overruns.


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

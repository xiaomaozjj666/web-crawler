# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

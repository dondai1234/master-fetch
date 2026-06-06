# Changelog

## [2.9.0] - 2026-06-06

### Fixed
- **MCP server now starts in <3s instead of 5-10s.** Scrapling/playwright imports deferred to first tool call. Before: OpenCode and other clients timed out waiting for `initialize` response (default 5s timeout). Now: server responds instantly, heavy deps load on first `smart_fetch`/`smart_search` call.
- `serverInfo.version` in MCP handshake now shows Hound version (e.g. `2.9.0`) instead of MCP SDK version (`1.27.0`)
- `NameError: name 'sys' is not defined` in `smart_fetch` and `open_session` — missing import in lazy loader

### Changed
- Install flow: `pip install hound-mcp[all]` then `hound install` (Chromium setup). No more chicken-and-egg where `hound install` can't run before pip.
- `hound -v` output simplified: `Hound v2.9.0 (latest)` or `Hound v2.9.0. v3.0.0 available. Run hound -u to update.` No PyPI jargon.
- `hound install` no longer re-runs pip (just Chromium setup). Shows `(already installed)` when re-run.
- Agent prompts in README: correct POV ("Tell the user to restart this agent", not "Restart your agent"), no client-specific config examples, no fetch-only option.
- Removed broken fetch-only install path. One product: fetch + search.
- OpenCode MCP config format: `type: "local"`, `command: ["hound"]`, `environment: { ... }` (not `env`).

### Added
- `__main__.py`: `python -m master_fetch` works as MCP server
- `__version__` in `__init__.py` as single source of truth
- `list_sessions` in README tools table

## [2.8.0] - 2026-06-06

### Removed
- 6 redundant tools removed: `get`, `fetch`, `stealthy_fetch`, `bulk_get`, `bulk_fetch`, `bulk_stealthy_fetch`. All functionality merged into `smart_fetch` via `force_fetcher` and `urls` params. 13 tools → 7.

### Changed
- `smart_fetch` now accepts `urls` (list) for bulk fetching. Returns `BulkResponseModel`.

### Added
- `ResponseModel` metadata: `content_type` (e.g. "text/html", "application/json"), `total_size_bytes`, `is_truncated`, `escalation_path` (e.g. "http→dynamic→stealthy"), `retry_count`
- JSON responses detected and returned raw — no more trafilatura mangling
- Agent-focused tool descriptions: "when to use", output shape, recovery hints
- Actionable error messages with recovery tips ("Try: use smart_fetch for auto-escalation")

## [2.7.0] - 2026-06-06

### Security
- SSRF protection: blocks internal IPs, localhost, cloud metadata endpoints, dangerous URL schemes
- Input validation on all entry points: URLs, CSS selectors, custom headers, proxies, timeouts, search queries
- API key redaction in error messages and logs
- Response body size limit (50MB cap)

### Fixed
- `_is_cloudflare_from_response` defined after class, causing runtime NameError. Moved above class.
- `is_allowed()` was blocking the async event loop with sync urllib. Now fully async.
- Cache corruption silently swallowed (bare except). Now logged.

### Changed
- Deduplicated fetch→annotate→cache→chunk pattern (8 copies → 1 `_finalize_result` helper)
- Smart fetch split into `_force_fetch`, `_auto_escalate`, `_phase_c_unknown`
- `__init__.py` uses lazy imports so lightweight modules don't pull scrapling
- Cookie conversion hardened against missing keys

### Added
- `security.py` module
- 158 unit tests across 6 test files

## [2.6.0] - 2026-06-02

### Fixed
- Auto-escalation was broken. Phase C bailed out after HTTP failure unless the response looked like a Cloudflare challenge. Sites returning 403 with "JS required" (IMDb, ScrapingCourse) never got dynamic or stealthy. Now: every tier that fails escalates to the next. Simple try-next-tier, no fancy gating.

## [2.4.0] - 2026-06-02

### Changed
- README overhaul: honest competitor comparison (Hound vs Crawl4AI/Firecrawl/Bright Data/Exa/Tavily/Jina), Pi agent one-prompt install section, installation prompts for all major agent harnesses
- Pi agent setup uses pi-mcp-adapter (correct package name)
- Search now requires TINYFISH_API_KEY env var (get free key at tinyfish.ai)

## [2.3.1] - 2026-06-02

### Fixed
- **Content continuation actually works now**: The 40KB truncation message previously said "re-fetch with offset parameter" but no offset parameter existed. Now `smart_fetch` has an `offset` parameter (default 0). When content is truncated, the response tells the agent exactly what offset to use: "Call smart_fetch again with offset=40000 to get the next chunk." The cached full content is used, so continuation calls are instant.
- **Chunking preserves all ResponseModel fields**: Previously dropped extracted_type, session_id, duration_ms, error during truncation. Now all fields survive.
- **Offset beyond content returns clean end message**: "No more content available" instead of empty or error.

### Changed
- **Continuation message is agent-friendly**: Shows exact char range ("received 40,000 of 60,000 chars, offset 0-40,000, 20,000 chars remaining") and the exact next call to make.

## [2.3.0] - 2026-06-01

### Fixed
- **Smart router now detects JS-only shells and escalates** (#1): When HTTP returns a 200 with content like "You need to enable JavaScript", smart_fetch now escalates to dynamic. When dynamic returns a JS-disabled placeholder (e.g. Twitter), escalates to stealthy. Previously accepted these as successful results.
- **Error field now signals content quality issues** (#7, #8): The `error` field is set when content is a JS shell (`js_shell_detected`), a geo/region redirect (`geo_redirect_detected`), or a bot challenge page (`bot_challenge_detected`). Downstream agents can now detect failures without parsing content strings.
- **Bulk output now respects max_content_chars** (#3): All bulk operations (`bulk_get`, `bulk_fetch`, `bulk_stealthy_fetch`) now accept a `max_content_chars` parameter (default 40000) that truncates each result. Prevents 300KB+ output that overwhelms tool runtimes.
- **Bulk `successful` count now excludes results with content issues**: A result with an error field is no longer counted as successful.

### Changed
- **Domain intelligence expanded**: Added known-safe domains (httpbin.org, wikipedia.org, github.com, stackoverflow.com, etc.) to prevent over-escalation of static sites. Added YouTube, Uniswap, Spotify, Notion, and other SPA domains as known-dynamic. Moved Twitter/X from dynamic to stealthy (dynamic returns JS-disabled placeholder for these).
- **All smart_fetch return paths now run content quality annotation**: Every exit point (Phase A/B/C, force_fetcher, escalation results) calls `_annotate_quality()` to ensure the error field is populated when content is bad.
- **All-tiers-failed error now includes failure trace**: The `error` field shows which tiers were tried and what failed.

### Added
- 15 new unit tests covering JS shell detection, content quality, geo redirect, domain intelligence routing

## [2.0.0] - 2026-06-02: Hound

Renamed product from "Master Fetch" to **Hound**: web research for AI agents.
Internal module name stays `master_fetch`. Package: `hound-mcp`. CLI: `hound`.

### Added

### Added
- Web search via TinyFish API: `smart_search` tool returns structured results (title, url, snippet)
  - Free (30 searches/min), no API key needed
  - Results cached for 5 minutes via SQLite
  - Optional install: `pip install master-fetch[all]`
  - Fetch-only users stay lean with zero extra dependencies

### Changed
- Package architecture: `master-fetch` = fetch only, `master-fetch[all]` = fetch + search
- README rewritten with competitor comparison tables and one-prompt install guides

## [1.1.0] - 2026-06-02

### Added
- Robots.txt compliance: respects site scraping policies by default. `respect_robots=False` to bypass.
- HTTP retry logic: exponential backoff (1s/2s/4s) on transient network failures
- Comprehensive test suite: 22 unit tests covering models, chunking, CF detection, domain extraction, binary detection, robots.txt
- GitHub Actions CI: cross-platform testing on Ubuntu and Windows (Python 3.11, 3.12)
- Proper PyPI metadata: classifiers, dev dependencies, pytest config

### Changed
- ResponseModel now includes `extracted_type`, `session_id`, `duration_ms`, `error` fields
- Error messages: all-tiers-failed returns diagnostic trace showing what was tried

### Fixed
- Binary content (PDF) no longer crashes the extractor, returns clean error
- HTTP error pages (non-challenge) no longer trigger wasteful browser escalation

## [1.0.4] - 2026-06-02

### Fixed
- PDF and binary content handling: returns clean error instead of crashing with decode error
- HTTP error pages no longer trigger unnecessary browser escalation. If the response contains no bot challenge text, the error is returned directly.

## [1.0.3] - 2026-06-02

### Fixed
- Domain extraction now correctly handles multi-part TLDs (.co.uk, .com.au, .co.jp, etc.)
- Auto-persistent browser sessions in smart_fetch. Dynamic and stealthy tiers now reuse browser instances instead of launching a new browser each time.
- Disabled resource loading in dynamic/stealthy tiers for ~25% speed improvement.
- Rewrote README with accurate, minimal information.

## [1.0.2] - 2026-06-01

### Fixed
- Default caching was OFF (cache_ttl=0). Now defaults to 3600s (1 hour). Repeat fetches return instantly from cache.
- Added 40KB content chunking with offset continuation. AI agents get a truncation notice when content exceeds the limit.

## [1.0.0] - 2026-06-01

### Added
- Smart fetch routing: auto-escalates HTTP → Dynamic → Stealthy based on bot detection
- Cloudflare Turnstile/Interstitial bypass via Patchright + Scrapling
- Trafilatura content extraction pipeline (markdown, text, article, structured)
- SQLite content caching with configurable TTL
- Domain intelligence system: remembers which domains need which fetcher level
- 12 MCP tools: get, bulk_get, fetch, bulk_fetch, stealthy_fetch, bulk_stealthy_fetch, screenshot, open_session, close_session, list_sessions, smart_fetch, cache_clear
- Streamable HTTP transport (--http flag) for remote agent connections
- Anti-bot bypass for DataDome, Akamai, Cloudflare challenges
- Content quality rating: 9.5/10 vs competitors
- Beats Exa and Tavily on JS-rendered and bot-protected pages (see COMPARISON_REPORT.md)

### Known Limitations
- DataDome + Cloudflare dual protection (g2.com) still blocks all fetchers
- Reddit infinite scroll only returns first-load content
- No built-in rate limiting between fetcher tiers
- Domain extraction doesn't handle .co.uk / .com.au correctly

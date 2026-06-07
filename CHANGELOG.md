# Changelog

## [3.0.1] - 2026-06-07

### Fixed
- **SSRF bypass via alternate IP notations**: `0177.0.0.1` (octal), `0x7f.1` (hex), `2130706433` (decimal integer), `127.1` (short-form) now correctly resolved and blocked. `_normalize_ip_notation()` handles all curl/libcurl-supported IP formats that `ipaddress.ip_address()` misses.
- **Proxy credentials leaking in error messages**: `http://user:pass@proxy:8080` URLs in error output now redacted to `http://[CREDENTIALS_REDACTED]@proxy:8080`.
- **`_check_version()` blocks MCP event loop**: `version()` now calls `_check_version()` via `asyncio.to_thread()`. No more 5s freeze on concurrent tool calls.
- **No URL count limit in bulk methods**: Added `MAX_BULK_URLS = 100` cap to `bulk_get`, `bulk_fetch`, `bulk_stealthy_fetch`.
- **Race condition on `_db_initialized`**: Both `cache.py` and `domain_intel.py` now use `asyncio.Lock` with double-check pattern to prevent concurrent DB init.
- **Empty string CSS selector passthrough**: `validate_css_selector("")` now returns `None` instead of `""`.
- Removed dead imports: `guess_protection_level` from `server.py`, `json` from `domain_intel.py`.

## [3.0.0] - 2026-06-07

### Changed
- **Major reliability upgrade**: no new features, hardened internals.
- `gather()` in all bulk methods now uses `return_exceptions=True`. One failed URL no longer crashes the entire batch. Failed URLs return `ResponseModel` with `status=0` and error details.
- `_ensure_auto_session` race condition fixed: concurrent calls no longer orphan browser sessions. Re-check after session creation closes orphans.
- `open_session` exception handler now sets `_alive=False` inside the session lock, preventing use-after-close races.
- `_normalize_credentials` now validates types (must be string), length (max 512), and rejects newlines. Prevents injection/DoS via oversized credential strings.
- `_dispatch` error responses now use MCP's `isError=True` flag. Agents can distinguish errors from successful results. Missing url/urls and unknown tools now raise `ValueError` caught by the outer handler instead of returning silent error JSON.
- `search.py` raises `SecurityError` consistently instead of plain `Exception`. Matches the rest of the codebase's error hierarchy.
- Domain intel downgrade threshold raised from 5 to 10 consecutive stealthy hits with zero fails. Prevents protection-level flip-flopping on temporarily permissive sites.
- Cache and domain intel DB initialization cached: `_db_initialized` dict prevents redundant PRAGMA calls on every cache operation.
- Cloudflare detection now only triggers on status 403/503. Status 200 pages mentioning "cloudflare" in body text (e.g. articles about web security) are no longer falsely flagged.

## [2.11.3] - 2026-06-06

### Fixed
- `list_sessions` returned a list as `structuredContent` (MCP requires dict). Now returns `{"sessions": [...]}`.

### Fixed
- `list_sessions` NameError: `json` not imported at module level (broken in v2.11.0 low-level server refactor).

## [2.11.1] - 2026-06-06

### Changed
- Trimmed hand-holding from tool descriptions and truncation messages. Same meta-awareness, fewer tokens.

## [2.11.0] - 2026-06-06

### Changed
- **Switched from FastMCP to low-level MCP Server**: hand-crafted minimal tool definitions eliminate Pydantic schema bloat. Tools/list payload dropped from 13KB/4.4K tokens to 4KB/1.4K tokens (69% reduction). No outputSchema in declarations (agents get structured content in every response instead). Every param still has descriptions, annotations intact.
- `next_offset` field in ResponseModel: structured integer for pagination. No string parsing needed.

## [2.10.1] - 2026-06-06

### Added
- `next_offset` field in ResponseModel: structured integer telling agents the exact offset for the next chunk. No string parsing needed. `0` = no more content.

### Changed
- `is_truncated` description: `True=more content, use next_offset`
- `offset` param description: `0-based char offset. is_truncated=true? Use next_offset value.`

## [2.10.0] - 2026-06-06

### Changed
- **Token-efficient MCP tool definitions**: 20KB → 13KB (34% less tokens burned per conversation). Core params stay structured and validated. Advanced params moved to `options` dict (proxy, cookies, useragent, etc.). Agents discover them without paying token tax on every call.
- Compressed all field descriptions in output schemas (e.g. "HTTP status code returned by the website (0 if network error)" → "HTTP status (0=network error)").
- Tool descriptions compressed to terse one-liners.

## [2.9.3] - 2026-06-06

### Added
- Parameter descriptions on all `smart_fetch` inputs (via `Annotated` + `Field`). Agents now see what `offset`, `cache_ttl`, `force_fetcher`, etc. do before calling.
- Tool description now mentions chunking (`is_truncated=True` means call again with `offset`) and caching (`cache_ttl=0` to force fresh).

## [2.9.2] - 2026-06-06

### Fixed
- **Double-chunking bug**: `bulk_get`, `bulk_fetch`, `bulk_stealthy_fetch` applied `_apply_chunking` before `_finalize_result` applied it again. Truncation messages got baked into cached content, and offset continuation returned the truncation message instead of actual content. Now chunking only happens once, in `_finalize_result` (and the direct cache-hit/robots paths).

## [2.9.1] - 2026-06-06

### Added
- MCP `ToolAnnotations` on all 8 tools: `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`. Agents can now determine whether a tool is safe to parallelize, retry, or cache without trial and error. ([Idea from this Reddit post](https://www.reddit.com/r/mcp/comments/1tyh7dp/mcp_needs_better_tool_metadata/))

### Removed
- `hound install` command. Use `playwright install chromium` directly. One less custom command to maintain.

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

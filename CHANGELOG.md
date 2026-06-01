# Changelog

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

# Changelog

## [9.1.1] - 2026-07-07

### Startup reliability + lazy search/reranker imports

This is a reliability and speed release for the MCP handshake path. No new
tools, no schema breaks, no response-shape breaks.

- Removed the no-op `search_engines` startup/shutdown imports from both stdio
  and streamable HTTP serve paths. Startup no longer imports the metasearch
  stack just to call no-op prewarm/close hooks.
- Added `_safe_imported_prewarm()`: optional prewarm modules are resolved inside
  `asyncio.to_thread()` before their async prewarm function runs. Slow optional
  imports (reranker/ONNX chain, future prewarms) cannot block the event loop and
  mute the MCP `initialize` response.
- `master_fetch.search_engines` now lazy-loads `search_metasearch` only inside
  `multi_search()`. Importing `master_fetch.search` for cache hits, validation
  errors, or model construction no longer imports primp/httpx/lxml/fake_useragent.
- `master_fetch.search` now lazy-loads `master_fetch.reranker`; cached searches
  and invalid search requests no longer import the reranker wrapper or touch any
  ONNX/tokenizer path.
- Removed the broad `RuntimeWarning: coroutine was never awaited` suppression.
  Hound still suppresses targeted Windows asyncio transport teardown noise, but
  real async bugs now surface instead of being globally hidden.
- Cleaned stale search response metadata from `keyword` to `merge`, matching the
  existing reality that keyword/BM25 search mode was removed.

### Tests

- Added regression coverage proving `master_fetch.search` does not eagerly load
  live-search/reranker backends.
- Added regression coverage proving slow optional prewarm imports do not starve
  the event loop.
- Verified focused startup/search/lifecycle suite: 72 tests passing.
- Repeated cold stdio probes: 6/6 initialized in ~0.9-1.1s, exit 0, empty stderr.

## [9.1.0] - 2026-07-06

### Native streamable HTTP transport (Open WebUI direct connect)

Hound now speaks the streamable HTTP transport (MCP 2025-03-26 spec), so HTTP
MCP clients like Open WebUI (v0.6.31+) connect to it directly, no `mcpo` proxy
required.

    hound --http --host 127.0.0.1 --port 8765

serves Hound at `http://127.0.0.1:8765/mcp`. Point Open WebUI's native MCP
support at that URL and it connects. Stdio (the default, used by Claude Code,
Cursor, OpenCode, etc.) is unchanged.

The previous `--http` mode used the legacy SSE transport, which is deprecated in
the MCP spec and unsupported by Open WebUI's native MCP client. SSE was replaced
(not added alongside) by streamable HTTP, so there is one HTTP mode and it is the
spec-current one. No new dependencies: `starlette` and `uvicorn` already ship
transitively with `mcp`.

### Tests

- `tests/test_v91_http.py`: a full streamable HTTP lifecycle (initialize ->
  notifications/initialized -> tools/list -> tools/call -> clean shutdown)
  against a `python -m master_fetch.server --http` subprocess, plus a clean-
  teardown assertion. CI-safe (network-free, calls `cache_clear`). 626 tests
  total (was 624).

### Compatibility

No API breaks. The 6 tools, their schemas, and response shapes are unchanged.
Stdio behavior is identical. Only `--http` changed: if you were using the old
SSE `--http` mode (undocumented), switch to the new streamable HTTP mode; the
endpoint moved from `/sse` + `/messages/` to `/mcp`.


## [9.0.0] - 2026-07-05

### Production-hardening + repo-professionalism release

The v9 focus is reliability and presentation: the server always starts, never
spews crash-like tracebacks on shutdown, and the repo reads like a maintained
product. No new tools, no API breaks — the 6 tools, their schemas, and the
response shapes are unchanged.

### Fixed: clean shutdown (no more 'failed to load' on Windows)

On Windows the ProactorEventLoop's pipe transports (the warm patchright Chrome
subprocess) emit `Exception ignored in __del__` tracebacks to stderr during GC
AFTER the event loop closes — `RuntimeError: Event loop is closed` and
`ValueError: I/O operation on closed pipe` from `_ProactorBasePipeTransport`.
The process exited 0, but an MCP client reading stderr saw a crash-like
traceback and could report 'failed to load'. v8.2's `finally` block couldn't
catch these (they fire in `__del__` post-loop, beyond any try/except).

- `serve()` now installs a `sys.unraisablehook` override that swallows ONLY the
  asyncio-transport-teardown noise (RuntimeError / ValueError / ResourceWarning
  whose traceback is in `asyncio/`). Every other unraisable exception still
  reaches the original hook, so genuine `__del__` bugs stay visible.
- `_shutdown_close_sessions` now closes the browser session, flushes the loop
  (`asyncio.sleep`) so pending `connection_lost` callbacks drain while the loop
  is alive, then explicitly closes any lingering asyncio subprocess transports
  on the loop (`loop._subprocess_transports`) so their `__del__` is a no-op.
- Net result: `python -m master_fetch` exits 0 with an empty stderr on both a
  quick disconnect (browser mid-launch) and a long-lived session. Verified with
  a new CI-safe lifecycle test (`tests/test_v9_lifecycle.py`).

### Added: CI-safe MCP lifecycle test

`tests/test_v9_lifecycle.py` spawns `python -m master_fetch` as a stdio MCP
server and runs a full `initialize` -> `notifications/initialized` ->
`tools/list` -> `tools/call` (cache_clear) -> clean-disconnect cycle in ~3s,
asserting: the handshake responds, the connect-time `instructions` ship, all 6
tools are present with their hand-crafted descriptions, a tool call succeeds,
and the process exits 0 with no `Traceback` in stderr. Runs in CI (not e2e):
fast and network-free.

### Changed: leaner tool defs, honest token count

- `mcp_smart_fetch`'s `options` description stopped advertising the 6 rare
  internal anti-detect knobs (`real_chrome`, `solve_cloudflare`, `block_webrtc`,
  `hide_canvas`, `main_content_only`, `use_trafilatura`). They still work via
  `additionalProperties: true`; the agent just isn't told to twiddle them.
- README token claim corrected from the stale '~2.5K' to the measured
  ~2.7K tools/list + ~0.8K one-time instructions (`cl100k_base`).

### Fixed: stale docs + repo professionalism

- README: '9 backends' -> '10 backends' everywhere (Qwant, added in 8.1, was
  missing); the resilience-layer row that claimed 'Three independent indexes
  (DuckDuckGo, Bing, Qwant)' now reflects the real 10-backend / 6+ index-family
  pool; the lean-install 'keyword BM25' claim (BM25 was removed in 7.2) now
  says 'cross-backend consensus + engine-position ranking'.
- `STATUS.md` (a personal agent status doc) is no longer tracked in the public
  repo; it is gitignored and stays local.
- Added `.gitattributes` (LF normalization + `linguist-vendored` for the ddgs-
  derived `search_metasearch.py` so it doesn't skew the language bar).
- Added community files: `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1),
  `SECURITY.md`, `PULL_REQUEST_TEMPLATE.md`, an `ISSUE_TEMPLATE/feature_request.yml`,
  and `ISSUE_TEMPLATE/config.yml`.
- e2e test desalted: removed the dead `TINYFISH_API_KEY` injection (TinyFish was
  removed in 7.0) and bumped the protocol version to `2025-03-26`.

624 tests (622 + 2 lifecycle).

## [8.2.1] - 2026-07-02

### fix: faster first search + race-safe reranker prewarm

The first `smart_search` on a fresh process was ~7s because the neural reranker's
ONNX model (~1.4s cold load) ran **sequentially after** the engine fetch
(~2s diversity quorum), even though the startup prewarm was loading it in the
background. Worse, the prewarm and the search **raced** on the sync
`get_reranker()`: if the prewarm had set `_reranker_tried=True` mid-load, the
search's `get_reranker()` saw `tried=True, _reranker=None` and silently returned
None, so the first search fell back to non-neural (merge/consensus) rerank — or
the search loaded the model itself while the prewarm redundantly loaded it too
(double load).

- **`ensure_reranker()` (async, race-safe)** replaces the sync load path. An
  `asyncio.Lock` serializes concurrent callers (the startup prewarm + the first
  search) so they share **ONE** load; the search awaits the in-flight prewarm
  instead of racing it. `get_reranker()` is now a peek-only fast path (returns
  the cached singleton or None, never loads) used by `rerank()` / find_similar.

- **Reranker load now overlaps the engine fetch**: `smart_search` starts
  `ensure_reranker()` as a background task at search start (in parallel with
  `multi_search`), then awaits it before the rerank step. The ~1.4s cold model
  load now runs concurrently with the ~2s diversity quorum, so the first search
  = `max(engine_fetch, model_load)` instead of `engine_fetch + model_load`.
  First search drops from ~7s to ~2-4s (engine-fetch-bound); warm searches are
  unchanged (0ms peek). Real MCP clients (with a gap between handshake and first
  call) get a warm prewarm and see ~2s on the first search.

- 622 tests (619 + 3 new: get_reranker is peek-only, concurrent ensure_reranker
  shares one load, no retry after a finished failure; prewarm tests updated to
  the new _load_reranker path).

## [8.2.0] - 2026-06-28

### fix: 'hound failed to load' (50% startup failure) + browser prewarm isolation

The recurring 'hound failed to load' ~50% of the time was caused by heavy
MODULE-LEVEL imports blocking the process for ~5s BEFORE the MCP initialize
handshake could respond. On a cold start `import master_fetch.server` took
**5.45s** (trafilatura 0.87s + the metasearch engine chain 0.86s +
mcp.server.fastmcp 1.03s + mcp.types 1.03s, all eager) — cold starts exceeded
the MCP client's initialize timeout, the client killed hound, and the messy
teardown (Event loop is closed / EPIPE) looked like a crash. The browser
prewarm itself was async + caught, but the synchronous 5s import was the killer.

- **Heavy imports deferred to first use**: trafilatura, the search_metasearch
  engine chain (primp/httpx/lxml/h2/fake_useragent), mcp.server.fastmcp, and
  mcp.types are now lazy-imported at their call sites, NOT at module load.
  `import master_fetch.server` dropped from **5.45s to 0.52s** (10x). The MCP
  handshake now responds in ~0.5-1s instead of 5s+. The heavy deps load on the
  first search/screenshot/fetch that actually needs them, never blocking
  startup. (mcp.server.Server + mcp.types are still imported in serve() before
  the first response — they're irreducible SDK requirements, ~2s, but cached
  after first run.)

- **Browser prewarm fully isolated**: `_prewarm_stealthy` now catches
  `BaseException` (not just `Exception`) so a CancelledError or any launch
  failure can NEVER crash the server, and is capped at 30s (`asyncio.wait_for`)
  so a hung browser launch can't hold the session-creation lock forever. On any
  failure the browser simply lazy-launches on the first stealthy fetch.

- **Bulletproof shutdown**: the serve() finally block now cancels prewarm tasks
  and closes sessions swallowing `BaseException` at every step, so the Windows
  ProactorEventLoop 'Event loop is closed' RuntimeError and the patchright node
  driver EPIPE on teardown can never crash the process or look like a 'failed to
  load' to the MCP client. New `_safe_prewarm` helper isolates + times out the
  search/reranker prewarm tasks too.

- 619 tests (613 + 6 new v8.2 startup tests in test_v8_2_startup.py: assert the
  heavy deps are NOT in sys.modules after `import master_fetch.server`, assert
  import < 2s, assert `_safe_prewarm` swallows Exception + BaseException +
  caps hung launches). Live-proven: 11/11 stdio initialize probes succeeded
  (steady-state ~2.5s, was 5.45s cold).

## [8.1.0] - 2026-06-28

### feat: search reliability + power upgrade (real Qwant backend, circuit breaker, tracking-aware dedup)

Make the keyless metasearch more reliable, more powerful, and faster. Three
changes, all functional (no re-labels).

- **Real Qwant backend** (10th independent index): Qwant was aliased to
  duckduckgo since v7.5 (the vendored ddgs has no qwant backend). v8.1 adds a
  real `Qwant` class hitting the keyless JSON API `api.qwant.com/v3/search/web`
  with SearXNG's proven param set (count=10 exactly, locale en_US, tgp random
  1-3, device=desktop, shuffled param order to resist fingerprinting).\  primp-pinned to a safari TLS fingerprint (chrome/edge get 403-captcha). Qwant
  has its own independent index (European) -> a 10th `qwant` index family for
  cross-backend consensus. Live-proven: 10 authoritative results, contributes
  to the default pool.

- **Circuit breaker for blocked backends**: a backend that CAPTCHAs / 403s /
  rate-limits us is now skipped for a 60s cooldown (`_record_block`/
  `_is_circuit_open`/`_record_success`), instead of being re-fired every search.
  This (a) stops hammering a host that is actively blocking our IP (which risks
  escalating to a longer IP-level ban) and (b) frees quorum slots so healthy
  backends aren't held back waiting on a sick one. Empty results and timeouts
  are transient and do NOT trip the breaker (only `MetaBlockedException` does,
  raised on HTTP 403/503 or Qwant's captcha/rate-limit JSON signal). New status
  values `blocked` + `circuit_open` map to `engine_blocked` in the response.

- **Tracking-aware dedup**: `_normalize_url` previously dropped the ENTIRE query
  string, which collapsed genuinely distinct pages (`?page=2` vs `?page=3`). Now
  it strips only tracking/analytics params (utm_*, fbclid, gclid, ref, si, ...) and
  keeps real query, so tracking-variant dupes across backends collapse while
  distinct pages stay distinct.

- Tool defs + `HOUND_INSTRUCTIONS` updated: 10 backends (added qwant), circuit-
  breaker noted. `search.py` stale "duckduckgo+bing+qwant" docstring fixed.

- 613 tests (603 + 10 new v8.1 search tests in test_v8_1_search.py). Live-proven:
  full default search 1.1-1.4s with 3+ backends contributing; Qwant returns 10
  authoritative results; no false circuit-opens across repeated searches.

## [8.0.0] - 2026-06-28

### feat: sitemap-mode crawl, outgoing-links field, related-queries, PDF section-map + tool-def overhaul

A major upgrade focused on agent efficiency: collapse real multi-step agent
loops into fewer calls, and surface what a page/site actually contains so the
agent fetches less and navigates better. Four new capabilities (all NEW - none
are re-labels of existing behavior), plus a tool-def overhaul and a robustness
fix to the metasearch status logic.

- **Sitemap mode for smart_crawl** (`sitemap=true|'auto'|false`): map a whole
  site from its sitemap.xml in ONE fetch (full URL list + `<lastmod>`) instead
  of blind best-first BFS. Collapses a hundreds-of-pages discovery crawl into a
  single call. `auto` uses the sitemap if the site has one, else BFS; `true`
  maps sitemap-only (honest empty if none). Discovery: robots.txt `Sitemap:`
  directives first, then `/sitemap.xml` + `/sitemap_index.xml`; recurses into
  `<sitemapindex>` children (depth + count capped); gzip-tolerant;
  namespace-agnostic; same-domain + path filters applied. New module
  `sitemap.py`. CrawlResponseModel gains `sitemap_used` + `sitemaps`; CrawlPage
  gains `lastmod`.

- **Outgoing-links field on smart_fetch** (`include_links=true`): populates
  `response.links = {citations, navigation, external, primary_source}`.
  `citations` = links inside the main-content area (the page's referenced
  sources - the highest-value links to follow); `navigation` = site chrome;
  `external` = off-domain links; `primary_source` = best-effort hint at the
  actual primary source (canonical/JSON-LD on a different host, else an
  in-content off-domain reference on a known primary host like arxiv/doi/github).
  Lets an agent follow a page's source chain in one step instead of eyeballing
  markdown links. New module `links.py`.

- **Related-queries on smart_search**: `related_queries` mined EXTRACTIVELY
  from the result titles + snippets hound already collected (no LLM, no
  per-engine "related searches" SERP markup dependency). Ranks bigrams by
  document frequency across the result set, drops ones that overlap the
  original query, falls back to high-frequency unigrams. Engine-agnostic and
  robust to SERP markup changes. Helps an agent refine a broad query.

- **PDF section-map with page ranges**: `table_of_contents` entries now carry
  `end_page` (computed from the outline structure) so an agent can pass
  `pages='23-31'` to grab exactly one section by range. For PDFs WITHOUT a
  bookmark outline (most arxiv papers), a heading-based section-map is built
  from the font-size heading detection already run during render - so every
  navigable PDF gets a section-map. Clamped to the extracted page range so the
  map matches what was actually returned.

- **Tool-def overhaul**: all 6 `_TOOL_DEFS` rewritten tight + structured (the
  agent-facing surface - the MCP client sees these, not the method docstrings).
  `HOUND_INSTRUCTIONS` rewritten: killed the stale `open_session` pro tip
  (removed in v4), tightened prose, documented the new features. The existing
  `actions` capability (infinite scroll / load-more / forms) is now obvious in
  the def with examples instead of buried. Stripped the redundant `:param:`
  block from the `smart_fetch` method docstring (internal bloat never sent to
  the client).

- **Robustness fix (metasearch status)**: a backend that returned valid results
  which happened to all be deduped by an earlier-finishing backend was marked
  `empty` - misleading (it DID contribute, it confirmed consensus). Now `ok`
  if it returned any valid result (new OR a dupe), `empty` only if it returned
  nothing usable. Also makes the dedup test deterministic (was racy on backend
  completion order).

- **Chunking fix**: `_apply_chunking` rebuilt the ResponseModel on truncation
  and dropped the `links` field (and would have dropped any new
  contextvar-driven field). Now copies `links` through (same as media/metadata).

6 tools unchanged in count (smart_fetch, smart_crawl, smart_search, screenshot,
cache_clear, version) - all sharpened, none cut. 603 tests (585 + 18 new v8
feature tests). Live-proven: sitemap mapped docs.python.org in one fetch;
search mines related_queries; include_links classified 30 citations / 20 nav /
20 external on Wikipedia with a correct github primary_source; PDF section-map
built 27 entries with page ranges on a bookmarkless arxiv PDF.

## [7.5.0] - 2026-06-25

### feat: search rebuilt on a vendored ddgs metasearch (the robust rewrite)

The hand-rolled 3-engine scraper (v7.0-7.4) kept failing: rate-limits, garbage
results, engines not contributing, and a 2nd Chrome instance spawning for
search. v7.5 replaces the whole engine layer with a **vendored, stripped
ddgs metasearch** (ddgs is MIT by deedy5; attributed in NOTICE.ddgs.txt).

- **9 keyless backends in parallel**: duckduckgo, brave, mojeek, yahoo, yandex,
  startpage, google + opt-in wikipedia, grokipedia. INDEPENDENT indexes (not the
  same feed twice). A backend that CAPTCHAs / rate-limits / has no topic-match
  simply yields nothing and the others carry — the diversity IS the robustness
  my 3-engine hand-rolled never had.
- **Vendored + stripped**, not a dependency: ddgs's base engine class + the 9 text
  backends + the aggregation logic live in `search_metasearch.py`. Removed the
  CLI / API server / MCP server / images / videos / news / books / extract /
  cache / async-loop-in-thread — text search only.
- **Async-native parallel aggregator**: backends run concurrently (asyncio +
  to_thread for the sync primp/httpx fetches); a **diversity quorum** waits for
  at least 3 backends to contribute before returning (so no single backend's
  bias/rate-limit dominates), with a 2s soft fallback so dead backends don't
  stall the search. Cross-backend consensus is tracked per URL (a URL returned
  by N independent index-families gets the consensus boost).
- **Transport**: primp (Rust HTTP client, random browser TLS/header
  impersonation) for most backends; httpx (HTTP/2 + randomized cipher/SETTINGS
  frame) for DuckDuckGo. `primp`/`httpx[http2]`/`fake-useragent`/`lxml` added as
  core deps.
- **No browser for search** — kills the 2nd-Chrome bug dead. Search is 100%
  HTTP; the single Patchright browser stays for smart_fetch only (eager +
  persistent at startup, as perfected).
- **Neural rerank kept** (the part that was already great); `find_similar` kept.
- `engine_blocked` now reports only genuinely-blocked backends (rate-limit /
  CAPTCHA / timeout); empty (no results) + preempted (cancelled because enough
  backends delivered) backends are not falsely flagged.
- `HOUND_SEARCH_PROXY` (http/https/socks5) is the power-user rotating-proxy
  escape hatch for per-IP throttling — the one thing no scraper can escape from
  a single IP.

Verified live: 10 rapid searches -> 0 dead-ends, avg 3.7s, 3 backends
contributing per search, high-quality authoritative top results (tokio.rs,
climate.ec.europa.eu, wikipedia, github, doc.rust-lang.org). 1 browser session
throughout (smart_fetch only, no 2nd Chrome). 585 tests pass.

## [7.4.0] - 2026-06-24

### fix: the real rate-limit fix — shared-browser search backbone + parallel race

Rate-limiting was terrible: 2 searches and all 3 engines could be dead for a
while. Root cause: the previous patch (7.3.1) made the stealthy browser lazy
AND the search→browser path had been removed, so search was bare HTTP — when the
engines 429'd, the circuit breaker put them all in cooldown with no recovery
path, returning 0 results.

This release fixes it properly, using Dondai's insight: **one always-on browser
shared by smart_fetch AND search**. No escalation (one transport per engine):

- **DuckDuckGo renders its SERP in the shared warm Patchright browser.** A real
  browser fingerprint never hits the 429/CAPTCHA wall that curl_cffi does, so DDG
  is a never-blocked backbone. Bing + Qwant stay on HTTP (Bing's SERP is too
  heavy for the browser; Qwant's JSON API is tolerant).
- **Parallel race:** all three engines start concurrently and the search returns
  the moment enough results have merged (cancelling laggards). ~1s when the HTTP
  engines are healthy (the DDG browser render is cancelled early — it ran in
  parallel, not as a fallback), and the DDG browser still delivers (~3-5s) when
  every HTTP engine 429s — so search is **never dead**.
- **Reverted the 7.3.1 lazy-browser mistake:** the stealthy browser is eager at
  startup again + persistent for the whole session (as before). One browser
  total, shared by smart_fetch, screenshot, and search — no extra Chrome.
- Speed opts (no quality loss): parallel race with early return, wait_selector
  on the SERP result container (return at domcontentloaded, not full load),
  disable_resources on SERP renders, eager warm browser, result caching.
- `engine_blocked` no longer false-alarms: an engine cancelled because enough
  results arrived is `preempted`, not `blocked`.
- `HOUND_SEARCH_PROXY` remains the power-user rotating-proxy escape hatch for
  per-IP throttling (the one thing a real browser can't escape).

Verified live: 10 rapid searches → 0 dead-ends, avg 1.3s, max 2.2s, 1 browser.
With both HTTP engines force-blocked, the DDG browser backbone still delivered
5 results. 616 tests pass.

## [7.3.1] - 2026-06-24

### fix: no more Chrome at startup (lazy stealthy browser)

The stealthy Patchright browser was eagerly prewarmed at server startup (for
smart_fetch's anti-bot escalation), so a Chrome instance sat idle eating ~150MB
RAM the moment the agent started — even though search is all-HTTP and never
needs a browser. The browser is now LAZY: it launches only on the first
smart_fetch / screenshot / search-all-blocked-last-resort that actually needs
it. Startup now warms only the cheap search-engine HTTP sessions + the neural
reranker (no browser). Verified live: 0 stealthy sessions at startup + after a
search. Trade-off: the first stealthy fetch is a ~3-5s cold launch instead of
warm (subsequent fetches are warm); search + HTTP fetches are unaffected.

## [7.3.0] - 2026-06-24

### speed + smart rate-limit avoidance + Qwant (replaces Brave)

Dondai: Brave rate-limits too fast to be usable; the per-engine stealthy
escalation adds a 0.6-1s+ cut when an engine blocks; want lower rate-limiting,
faster search, less garbage, and a smart (not brute-force) rate-limit bypass.

#### Engines
- **Qwant REPLACES Brave** as the third default engine. Qwant is a keyless JSON
  API (`api.qwant.com/v3/search/web`, own index + Bing feed = independent from
  DDG). Clean JSON parsing (no fragile HTML selectors). MUCH more rate-limit-
  tolerant than Brave: 25 rapid back-to-back searches, Qwant contributed 25/25,
  zero blocks (Brave blocked in ~10). curl_cffi passes Qwant's bot check only
  with the `safari184` fingerprint (chrome/edge get 403-captcha), so the SERL
  coordinator pins safari184 for qwant via `_ENGINE_IMPERSONATE`. Qwant's API
  requires `count=10` exactly (a real gotcha: any other value -> 400).
- **Brave DROPPED entirely** (rate-limits too fast per Dondai; urllib, no TLS
  impersonation). The `_URLLIB_ENGINES` / `_urllib_fetch` urllib transport is
  gone. `DEFAULT_ENGINES = (duckduckgo, bing, qwant)`.

#### Smart rate-limit bypass (not brute force)
- **Per-engine stealthy escalation REMOVED.** Previously when DDG/Bing got a
  403/202, hound escalated THAT engine to the warm stealthy browser (a 2-5s
  render) — the latency cut Dondai saw. Now a rate-limited engine just returns
  its 403 in <1s and the other engines carry; no slow stealthy path in the
  common case. The stealthy browser is now a **search-wide last resort only**
  (one stealthy DDG fetch, fired solely when ALL engines blocked and 0 results —
  rare), preserving the anti-bot flagship move without taxing the common case.
- **Hard per-engine deadline 8s -> 5s** (engines normally return 1-2s; the
  deadline is just a cap for the rare hang).

#### Smarter search / less garbage
- **Quality filter**: drop low-relevance results (`fetch_relevance == 'low'`)
  instead of padding to max_results with garbage, when at least 3 good results
  remain. Niche/ambiguous queries return fewer good results, not 6 padded with
  garbage; clear queries keep all (none are 'low').

#### Robustness proven live
- 25 rapid successive searches: all 3 engines contributed 25/25, zero blocks,
  every search got results, max latency 3.8s (most 1.3-2.3s), no stealthy cut.
- Simulated block test: Qwant blocked (cooldown) -> DDG+Bing carried (6 results,
  no failure) -> Qwant rejoined after cooldown. Full block -> failover ->
  recovery cycle works. 616 tests pass.

## [7.2.0] - 2026-06-24

### diverse independent search pool + cross-engine consensus (rate-limit fix)

Solves the rate-limiting problem once and for all WITHOUT trading away speed
(no just-add-delays cheating). The unique new feature, built from scratch at zero
fetch cost: **cross-engine consensus ranking** — a URL returned by several
independent engines is an authority signal no single-engine tool can produce.

#### Engines
- **Google REMOVED entirely.** It CAPTCHAs even via the stealthy browser, so it
  was removed rather than silently contributing nothing. `engines=['google']` is
  now rejected.
- **Brave (web) ADDED as a default engine** — an independent 30B-page index that
  breaks the DuckDuckGo~Bing 99%-overlap problem. The Brave Search API free tier
  was killed Feb 2026 (metered billing), so Hound scrapes the keyless web UI.
  curl_cffi/scrapling throws curl error 23 on search.brave.com (every
  fingerprint), but stdlib urllib returns 200 + 20 clean results, so Brave rides a
  urllib transport inside the resilience coordinator (still gets the pacer +
  circuit breaker; stdlib = lean installs get it too).
- **Mojeek DROPPED** — 403s every HTTP client AND the stealthy Patchright browser
  (IP-blocked). Unreliable + per-search browser render = a speed cost.
- **Yahoo ADDED opt-in** — serves Bing's index from a different server, a
  redundancy source for Bing's index when bing.com rate-limits.
- **DEFAULT_ENGINES = (duckduckgo, bing, brave)** — three independent index
  families, all HTTP (no browser needed for the default pool, so search stays
  fast).
- **Default max_results 9 -> 6.**

#### Cross-engine consensus ranking
- `merge_dedupe` tracks which engines returned each URL + stamps
  `RawResult.consensus` = distinct index-families (`_INDEX_FAMILY`: yahoo counts
  as the bing family).
- `_apply_consensus_boost` (additive): `score + 0.2*(consensus-1)`. Consensus
  AMPLIFIES relevance without overriding it (a strongly-relevant single-engine
  result still beats a weak consensus hit); also breaks the neural-saturation
  tie. Zero extra fetches (counted during the merge).
- New agent-facing `engines_consensus` field per result ("N of M independent
  indexes") + `source` now shows all agreeing engines (e.g. "brave,duckduckgo").

#### Rate-limit resilience proven live
- Three independent engines run in PARALLEL (wall-clock = max(slowest, 8s
  deadline), same as two engines — no slowdown). When one rate-limits, the
  circuit breaker rests it (15-120s) and the other independent engines carry
  genuinely different results. Live test: 15 successive searches, Brave blocked
  on #11 -> DDG+Bing carried, 6 results every time, agent never saw a failure;
  waited out the cooldown -> Brave rejoined. Full block -> cooldown -> recovery
  cycle works without issues.

### keyword/BM25 removed; neural is the only reranker + optimized

Dondai: keyword (BM25) is the same speed or slower than neural, so it was removed
entirely (simpler). Neural is always the top reranker.

- **BM25 / `keyword` mode REMOVED.** `bm25_rerank` deleted; `multi_search` returns
  the merge order (consensus + engine-position). `mode='keyword'` is now
  rejected. Lean installs (no model) fall back to cross-engine consensus +
  engine-position order (rerank_mode "merge"), no lexical rerank.
- **Neural optimized — min-max normalize per query** (`reranker.rerank`): ms-marco
  sigmoid saturates (~1.0 for any clearly-relevant snippet) so raw scores cluster
  and can't discriminate; normalizing to 0..1 across the result set restores
  meaningful `relevance_score` spread. Ranking order unchanged (monotonic).
- **Consensus boost switched multiplicative -> additive** (composes cleanly with
  normalized scores; renormalize to 0..1 only when a bonus pushes a score >1.0).
- Cache key bumped `search:v3` -> `search:v4` (consensus field). 614 tests pass.

## [7.1.0] - 2026-06-24

### smart_search reliability + agent-comfort overhaul

This release fixes a real reliability bug (first-call timeouts) and simplifies
smart_search per agent-feedback: cut the bloat, make the response judgment-
empowering instead of prescriptive, and stop returning low-value results.

#### Fixed: first-call timeout (cold start)
- Root cause: cold engine sessions (no cookies, cold TLS) got soft-blocked on the
  first hit, so each engine escalated to the stealthy browser; if that browser
  was still cold, multiple escalations serialized on one browser and blew past
  the MCP client timeout. The 2nd call was fast because sessions were warm by then.
- **Pre-warm engine sessions + the neural reranker at startup** (best-effort,
  no download for the reranker unless the model is already cached) so the first
  real search is warm.
- **Hard per-engine deadline** (`asyncio.wait_for`, 8s, `HOUND_SEARCH_DEADLINE`
  env) so a slow / blocked / escalating engine can never hang the search; the
  agent gets partial results from the engines that finished + the cut one in
  `engine_blocked`.
- Verified with the failing queries from the field: ~1.2-2.0s, no timeouts.

#### Removed (bloat)
- **Research mode** (`fetch_content` / `fetch_top` / `max_content_chars_per` +
  `ResearchResponseModel`): searching + fetching in one call depended on the
  model picking high-value URLs. smart_search now always returns URLs + ranking,
  NOT page content; the agent `smart_fetch`es the results it wants itself (one
  extra call beats guessing which URL is worth fetching).
- **`expand` (autoretrieval)**: marginal; the model rephrases better itself.
- Dead `_urllib_get` removed; `find_similar` source fetch bounded to 6s.

#### Changed (agent comfort + token economy)
- **Default results 10 -> 9.** Results are capped at `max_results` (was returning
  up to 3x = 30, a token waste). The agent gets the top 9 ranked from the merged
  DDG+Bing pool.
- **Wikipedia dropped from default engines** (constant bottom-barrel garbage);
  default is now **DuckDuckGo + Bing**. `wikipedia` + `google` remain opt-in via
  `engines=`.
- **Google visibility fix**: an engine returning 0 results (google often
  CAPTCHAs/consents) was in neither `engines_used` nor `engine_blocked`, so it
  silently vanished. `engine_blocked` now lists any engine that did NOT
  contribute (rate-limited / timed out / parsed no results / consent page), so
  google surfaces there instead of being invisible. + google consent-page
  detection + a more robust parser (best-effort; google is opt-in + honest).
- **Pagination (`page`) fixed**: was advertised but never passed to the engines
  (only in the cache key, did nothing). Now threads the offset to each engine
  (DDG `&s=`, Bing `&first=`, Wikipedia `&sroffset=`, Google `&start=`).
- **`summary` + `next_action` on every search response** (consistency with
  `smart_fetch`). `next_action` is judgment-empowering, not prescriptive: "Results
  are ranked by relevance (relevance_score + fetch_relevance). smart_fetch the
  ones that match what you actually need - the ranking is a hint, not a directive;
  a lower-ranked result can be the right one, so trust your judgment." (a rigid
  "fetch 1-2" made the LLM stress over whether to break it when a lower-ranked
  result was the one it needed).
- Partial-results note in `fetch_hint` when engines did not contribute.
- `fetch_relevance` field description + tool def + connect-time `instructions`
  + README all softened to "fetch what matches your need, use your judgment".

#### Notes
- `neural` + `find_similar` rerank modes kept (real opt-in capability).
- 616 tests pass. Live-verified against the real web.
- TinyFish remains HARD-REMOVED (unchanged from 7.0.0).

## [7.0.0] - 2026-06-23

### flagship: 100% local keyless search (TinyFish removed)

Hound is now fully local + keyless + no-account. The TinyFish dependency is
HARD-REMOVED. `smart_search` is rebuilt from scratch as a hound-native keyless
metasearch that scrapes public engines and reranks on your machine. After 7.0,
the entire server is $0, no accounts, no third-party APIs, nothing routing through
someone else's cloud.

#### New: hound-native keyless engine layer (`search_engines.py`)
- Scrapes **DuckDuckGo, Bing, Wikipedia** in parallel (add `google` via `engines=`)
  over browser-impersonated HTTP (scrapling `FetcherSession`, a CORE dep, so lean
  installs get working search with zero new deps).
- **Anti-bot engine scraping**: when an engine rate-limits/CAPTCHAs the HTTP
  scraper, Hound escalates to its warm stealthy Patchright browser to render the
  results page and parse that. No keyless search tool does this.
- Multi-engine merge + dedup by normalized URL; per-engine crash isolation (one
  engine failing never kills the call); `engines_used` + `engine_blocked` reported.
- Bing's opaque `ck/a` redirect decoded from the `<cite>` display URL; DDG's
  `uddg=` redirect decoded; Wikipedia via the official keyless API.

#### New: rerank modes (`mode`) + autoretrieval (`expand`) + find_similar (`url=`)
- **`keyword`** (BM25 over title+snippet): the baseline, always available, even
  on the lean install.
- **`neural`**: a local ONNX cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`,
  Apache-2.0, 22.7M params, MS MARCO passage reranking) running on the
  `onnxruntime` Hound already ships for OCR. The model + tokenizer download once
  on first use (~80MB, pinned HF revision + sha256-checked, cached, NOT bundled).
  Exa-style semantic ranking, $0, local.
- **`find_similar`** (pass `url=`): fetches a page you like, derives a query from
  it, and reranks candidates against that source page's content. Exa's
  find-similar, local.
- **`expand=N`** (1-5, default 1=off): autoretrieval. Generates N sub-query
  variants locally (no external LLM) and runs them in parallel across engines,
  then merges + dedups. Boosts recall for niche queries.
- Graceful fallback throughout: if the reranker is unavailable (lean install /
  offline / download failed), `neural`/`find_similar` fall back to keyword
  BM25 with a note in `fetch_hint`.

#### New agent-facing search fields
- `relevance_score` (0-1, BM25 or neural), `fetch_relevance` (high/med/low),
  `engines_used`, `engine_blocked`, `rerank_mode` (keyword|neural|find_similar),
- Cache key bumped to `search:v2` and now includes `mode` + `expand` + the source
  URL (for find_similar), so different modes/filters never collide.

#### Dependencies
- **Zero new hard deps.** Engine scrapers use existing `requests`-free transport
  (scrapling, a core dep).
- `[all]` adds `tokenizers>=0.20` (Apache-2.0, ~3-5MB). `onnxruntime` is reused
  from OCR. No new runtime.

#### Removed
- **TinyFish: HARD-REMOVED.** No `api_key` param, no `TINYFISH_API_KEY` env, no
  optional backend. The entire server is local + keyless.
- `compute_fetch_relevance` (old overlap heuristic) replaced by BM25 + `_tier`.

#### Notes
- **Search Engine Resilience Layer (SERL)**: a stateful per-engine coordinator
  in front of every SERP request. (1) Persistent warm session per engine reused
  across searches (cookies + TLS accumulate -> returning human, not fresh bot;
  also faster, no per-search TLS handshake). (2) Per-engine pacing with jitter;
  within one search all engines fire in parallel. (3) Per-engine circuit breaker
  with exponential cooldown (15->120s); a blocked engine is skipped while the
  others keep serving. (4) 202 soft-limit + 429/503/403 + Retry-After aware (DDG
  202 was a missed case before). (5) Fingerprint rotation across a pool of real
  Chrome/Edge/Firefox/Safari TLS profiles. (6) Adaptive Google reserve tier
  (fires via the stealthy browser only when primaries fall short + one was
  blocked). (7) `HOUND_SEARCH_PROXY` env to route all engine requests through a
  user-supplied proxy (the bulletproof path for sustained heavy use). Honest
  posture: not bulletproof without a proxy, but dramatically more robust for a
  single user on a clean residential IP. No search-engine ToS compliance claimed.
- 604 tests pass (was 549 at 6.0.0). New: `tests/test_search_engines.py` (38,
  incl. 14 SERL tests), `tests/test_reranker.py` (29). All TinyFish tests removed; replaced with the
  local-search error contract. Live-verified against the real web: all engines
  return clean real URLs, neural reranks vs keyword,
  find_similar returns pages ranked vs a source URL, expand runs sub-queries.

## [6.0.0] - 2026-06-23

### flagship overhaul: smart_crawl + PDF extraction

Per a thorough external agent bug report (12 crawl issues, 14 PDF issues), both
features were rebuilt to be best-in-class among free/OSS alternatives.

#### smart_crawl — best-first + content-adaptive + normalized
- **Best-first priority queue** (was BFS). Discovered URLs are scored by focus
  relevance + content-likelihood (docs/guide/api boosted; login/submit/cart/
  admin penalized) + shallow-depth, so content pages are crawled before junk
  when the budget is tight. `focus` now reorders globally (was per-layer).
- **Content-adaptive per-page extraction**: article/docs -> trafilatura main
  content; **list/index pages (HN, aggregators, directories) -> a structured
  `* [title](url)` link list** (was 0 content_ok); JS shells detected and
  reported honestly (was silent empty/timeout). New `page_type` field per page.
- **URL normalization + dedup**: trailing slash, default ports, lowercase host,
  tracking params (utm_*/fbclid/gclid/ref) stripped; `/docs` and `/docs/` no
  longer crawled twice. Dedup by normalized, fetch the original.
- **`same_domain_only` default** (external links dropped).
- **Two-phase crawl**: new `crawl_urls` param fetches a chosen subset of
  discovered URLs with no re-discovery (after `discover_only=true`).
- Network errors report **status `-1`** (was 0). `fetched_at` per page;
  `cache_ttl=0` forces fresh. Default `max_content_chars_per` 4000 -> 8000.
- Overall **`deadline_ms`** (default 120000) so one slow page can't hang the
  crawl; partial results returned with `truncated_by_time`.

#### PDF — CID auto-OCR + honest quality + ToC + metadata
- **CID-corruption auto-OCR (the flagship trick).** Fonts without a ToUnicode
  CMap make pdfplumber emit `(cid:71)(cid:302)...` garbage (figures/diagrams/
  math in academic papers), but the glyphs render correctly. Hound detects
  CID-garbage pages (ratio >= 0.30), renders them via pypdfium2, and OCRs them
  with rapidocr (one batched call), recovering the real text. Equations/
  figures are OCR'd as visible symbols with an honest marker. Reuses OCR deps
  already shipped; no new heavy deps.
- **Per-page scanned OCR for MIXED PDFs**: a text body + scanned appendix used
  to leave the scanned pages empty. Now each image-bearing low-text page is
  detected and OCR'd alongside CID pages. (All-scanned docs still hit the
  honest `scanned_pdf` dead-end that the caller OCRs.)
- **`quality_score` (0.0-1.0) + honest `content_ok`**: a garbled doc reports
  `content_ok=false` even on HTTP 200 (was `true` despite 85% garbage). Scored
  from raw page text + OCR status so honest markers don't inflate it.
  `_agent_hints` respects the PDF verdict (won't let status 200 mask corruption).
- **`table_of_contents`** from the PDF outline via pypdfium2 `get_toc()`.
- **`metadata`** populated (title/author/subject/keywords/creator/producer/
  dates) on the response, not just the markdown header.
- **`include_media`** -> per-page embedded-image metadata for PDFs.
- **`.pdf` URLs never escalate to the stealthy browser** (was a 16s waste); a
  `.pdf` URL returning a login/paywall HTML page returns `auth_required` /
  `not_a_pdf` instead of extracting the login page.

#### Other
- README: reliable pepy.tech downloads badge (replaced the fragile shields.io
  monthly badge that intermittently showed "rate limited by upstream service").
  Feature sections rewritten for v6.
- 549 tests pass (was 529). +22 crawl tests, +14 pdf-v6 tests. CI green.

### Deferred (future)
- MCP progress notifications during long crawl/PDF ops (P8).
- PDF page labels for `--- Page N ---` (P11).
- Optional `[ai]` extra pulling marker for SOTA neural math/tables (heavy torch;
  not a hard dep).

## [5.0.1] - 2026-06-22

### Fixed
- **OCR was broken for real `pip install hound-mcp[all]` users.** `rapidocr` v3 ships without an inference backend; `RapidOCR()` raised `ImportError: onnxruntime is not installed`. The `[all]` extra declared `rapidocr>=3.0` but not `onnxruntime`, so a clean install got the core package without the backend. The dev venv had onnxruntime installed manually, which masked it (same class of bug as the 4.0.0 pdfplumber miss). `[all]` now also declares `onnxruntime>=1.16`. Verified with a clean `pip install hound-mcp[all]` from PyPI: `RapidOCR()` instantiates and OCR runs.
- Added a regression test asserting the `[all]` extra declares every OCR/PDF dep (pdfplumber, pypdfium2, rapidocr, onnxruntime) so a missing declaration fails CI instead of shipping.

## [5.0.0] - 2026-06-22

The flagship release. Hound goes from a fetch+search tool to a complete $0 local web-research server for AI agents: **crawl, fetch, anti-bot, PDF/OCR, page interaction, query-focused extraction, search+research, and agent-optimized responses** in one lean MCP install (6 tools, ~2K tokens). Free alternatives stop being comparable; only paid services (Bright Data, ZenRows, Firecrawl paid) can compete, and only on hard anti-bot at scale.

### Added — `smart_crawl` (new tool)
- **Deep same-domain crawl.** BFS from a start URL up to a depth/page/token budget, returns each page as clean markdown with `content_ok`/`summary`. `discover_only=true` returns the URL map. `path_include`/`path_exclude` scope it. `focus` makes it a query-prioritized crawl (most relevant pages first within the budget, each page focus-filtered). Concurrency via a semaphore; one fetch per page reusing `smart_fetch`'s anti-bot + cache.

### Added — OCR (scanned PDFs + image pages)
- Scanned/image-only PDFs are auto-OCR'd with `rapidocr` v3 + `pypdfium2` (pure-pip, no system binary, Python 3.13-supported). Image-only web pages (`content-type: image/*`) are OCR'd to text. Auto-caps at the first 10 pages when no `pages` spec to avoid hangs on huge scanned PDFs. `[all]` extra += `pypdfium2>=4.30`, `rapidocr>=3.0`.

### Added — query-focused extraction (`smart_fetch` `focus`)
- `smart_fetch(url, focus="...")` returns only the BM25-relevant blocks. Runs post-cache (one cached page serves any focus query), so it never triggers a re-fetch. 80%+ context cut on long pages. Re-pass the same `focus` when paginating.

### Added — `smart_search` filters + research mode
- Filters: `site`/`exclude_sites` (domain include/exclude via native TinyFish `site:`/`-site:` operators), `location`/`language` (geo), `page` (0-10). Cache key includes every filter.
- **Research mode** (`fetch_content=true`): searches AND bulk-fetches the top-N high-relevance results' full content in one call (each via `smart_fetch`, so anti-bot/PDF/OCR/cache apply), returning a `ResearchResponseModel` with per-result `content_ok` + relevance. Replaces the 3-5 call search→fetch loop.

### Added — page interaction (`smart_fetch` `actions`)
- `actions=[{click},{fill},{press},{wait},{scroll},{wait_selector}]` run on the stealthy browser after load, before extraction. Reaches content behind a click, search form, "load more", or infinite scroll. Forces the stealthy tier, bypasses cache. Max 20 actions, per-action error isolation.

### Added — metadata on every response
- Every HTML fetch carries structured `metadata`: title, description, site_name, type, image, canonical, lang, published_time, author (OpenGraph + JSON-LD + canonical + `<title>`).

### Added — opt-in media
- `include_media=true` populates `response.media` with up to 20 page image URLs (for multimodal agents). Empty by default to keep responses lean.

### Changed
- Tool count 5 → 6 (`smart_crawl` added). Connect-time `instructions` teach the 4-tool mental model (fetch / crawl / search / screenshot). tools/list ~1.3K → ~2K tokens (still far under competitors' 3-5K / 12-19 tools).
- Scanned-PDF dead-end removed: `hound-mcp[all]` now auto-OCRs instead of returning "needs OCR".
- README overhauled: 6-tool table, feature deep-dives, hand-authored SVG pipeline + token-comparison diagrams, updated free-tools comparison (crawl/OCR/interact/research/metadata/focus rows), honest limits.

### Fixed
- `_apply_chunking` now preserves `metadata`/`media` (it previously rebuilt the ResponseModel and dropped them).
- Removed em-dashes from all public-facing strings (tool descriptions, `instructions`, `next_action`, error/content messages, Field descriptions, `fetch_hint`) per the voice rule.

### Notes
- No breaking API changes to existing tools; `smart_fetch`/`smart_search` gain opt-in params. Full suite 528 tests pass (was 442). All features live-verified: OCR (scanned PDF + image), focus (87% reduction), search filters + research (real TinyFish), crawl (books.toscrape.com), metadata (Wikipedia), interact (quotes.toscrape.com navigation), media.
- Deferred (future releases): conditional revalidation (ETag/304), MCP progress notifications for crawl, shadow-DOM piercing, hardened fingerprint rotation, cache-stats.

## [4.0.3] - 2026-06-20

### Added
- **Brand visuals in the README.** Added a hero banner, a square logo mark, and an editorial "retrieving the web" illustration under `docs/`. Images are referenced via absolute `raw.githubusercontent.com` URLs so they render on both GitHub and the PyPI project page (PyPI cannot resolve relative repo paths). No code changes.

## [4.0.2] - 2026-06-20

### Fixed
- **`hound -u` ghost-prompt overlap on Windows.** After the parent `hound.exe` exited and PowerShell reclaimed the console (printing its `PS C:\Users\...>` prompt), the detached console updater woke from its 2s wait and printed its first line (`Running pip...`) on top of the prompt, producing overlapping "ghost" text. The updater child now emits a leading newline (`chr(10)`) right after the wait, so its output starts on a fresh line below the prompt instead of overlapping it.

### Notes
- No API changes. Test added: the generated updater child source must contain the leading-newline write.

## [4.0.1] - 2026-06-20

### Fixed
- **`pdfplumber` was missing from the `[all]` extra**, so `pip install hound-mcp[all]` did NOT install it and PDF extraction was broken for real users (it raised "PDF extraction requires pdfplumber. Run: pip install hound-mcp[all]" even after installing `[all]`). The local dev venv had pdfplumber installed manually, which masked the missing declaration; CI's clean environment caught it. `pdfplumber>=0.11.0` is now declared in `[all]`.
- CI workflow now installs `.[dev,all]` (was `.[dev]`) so the PDF test suite actually exercises the flagship feature instead of failing on the missing optional dependency.

### Notes
- No code changes. Re-publishing solely to fix the dependency declaration so `hound-mcp[all]` delivers PDF support as documented.

## [4.0.0] - 2026-06-20

The agent-effectiveness release. Hound now masters itself the moment it connects, reads PDFs, and tells the agent exactly what to do next. **Breaking:** the manual `open_session` / `close_session` / `list_sessions` MCP tools are removed (8 tools → 5); a single warm browser is managed automatically.

### Added — flagship: PDF extraction
- **`smart_fetch` now extracts PDFs to structured, agent-optimized markdown.** PDFs are detected by content-type or `%PDF` magic bytes and routed to a new `pdf_extractor` built on `pdfplumber` (MIT, no AGPL): multi-column reading order, real tables as markdown tables, font-size heading detection, de-hyphenated paragraphs, a metadata header (title/author/date/subject), and `--- Page N ---` markers for citation.
- **`pages` param** (`"1-5"`, `"1,3,5-7"`) extracts a PDF subset to save tokens/time on big PDFs. **`password`** for encrypted PDFs. Both flow to the extractor via contextvars (task-local, bulk-safe); the cache key includes `pages` so subsets don't collide with full-PDF entries.
- Honest PDF signals: scanned/image-only PDFs return `content_ok=false` + a "needs OCR" `next_action`; encrypted PDFs report and accept a password; not-a-PDF/empty/corrupt are reported honestly.
- `pdfplumber` added to `[all]` (lean install unaffected).

### Added — connect-time mastery + actionable responses
- **MCP `instructions` at `initialize`.** A concise orientation (~365 tokens, paid once at handshake) gives the agent the 3-tool mental model, the #1 search→fetch workflow, and the known limits.
- **Agent-facing response fields on every fetch:** `summary` (one-line status), `content_ok` (trust content only if true), `next_action` (the obvious next call: paginate / bypass robots / switch sources), `fetched_at` (ISO-8601 UTC).
- **`smart_search` `fetch_relevance`** (high/med/low) per result + a `fetch_hint` so the agent fetches 1-2 results instead of all 10.
- **Promoted `css_selector`, `max_content_chars`, `timeout`** to first-class `smart_fetch` params (were buried in the `options` bag). `max_content_chars` is a token-spend control. Units + defaults on every param description.

### Changed — single warm browser instance
- **Removed `open_session` / `close_session` / `list_sessions` MCP tools** (and the `list_sessions` method). `open_session`/`close_session` stay as internal helpers. Tool count 8 → 5.
- **Eager warm-up at server startup** (was: pre-warm on first `smart_search`). The single stealthy Chrome launches when the harness starts the server, in parallel with the handshake.
- **No second browser ever spawns**: a new `_auto_session_lock` serializes auto-session creation. Keep-alive-forever; graceful shutdown closes the browser when the harness closes.
- `screenshot` now auto-manages a stealthy session (`session_id` optional); description clarifies it's for multimodal agents.

### Changed — Reddit optimization hardened
- Reddit URLs now **skip HTTP and go straight to stealthy** (www.reddit.com walls HTTP; saves ~1s). The old.reddit.com rewrite runs before `force_fetcher` so a pinned `http` also benefits.
- **Listing parser rewritten** to read per-post `data-*` attributes (was: span-scraping that misaligned scores/comment counts on real HTML, e.g. reported score 27 for a post whose real `data-score` was 28). Thing-block detection fixed for real HTML (`class=" thing"` has a leading space). HTML entities unescaped; promoted ads skipped; sticky/NSFW tagged; `1 comment` singular grammar; per-block span fallback for user-profile pages.
- `rewrite_to_old_reddit` now rejects lookalikes (`notreddit.com`) standalone.

### Fixed — caching + JS-shell detection
- **Bad content is no longer cached.** `_finalize_result` previously cached JS shells / bot challenges / error statuses for the whole TTL, and the cache-hit path didn't restore `error`, so `content_ok` came back `true` and the agent trusted broken cached pages. New `_is_cacheable`: cache only clean, non-blank, <400 content.
- **Cache size cap + oldest eviction** (`MAX_CACHE_ENTRIES = 10000`) so a long-lived agent's DB can't grow unbounded.
- **Search cache key includes `max_results`** (was: a 5-result and 10-result search collided on one cached entry).
- **JS-shell detection catches SPAs whose shell text doesn't match known phrases** (e.g. `quotes.toscrape.com/js` returned a 29-char nav shell over HTTP with no escalation). New heuristic: HTTP tier + status 200 + large body + <200 chars text → JS shell → escalate to stealthy. Scoped to the HTTP tier so stealthy-rendered low-text pages (image galleries, canvas) don't false-positive.

### Notes
- No new public API beyond the additions above. Reddit/stealthy-default/PDF/caching are transparent to existing callers.
- Full suite: 442 tests pass (5 e2e deselected). New test files: `tests/test_agent_qol.py`, `tests/test_cache_qol.py`, `tests/test_pdf_extractor.py`, `tests/test_reddit_routing.py`; real PDF fixtures `tests/background_checks.pdf` + `tests/dummy.pdf`; real Reddit HTML fixture `tests/old_reddit_real.html`.
- README overhauled: 5-tool table, feature deep-dives, free-fetch comparison (Crawl4AI / Jina Reader / Firecrawl OSS / DIY), honest limits, ~1.3K token claim.

## [3.6.7] - 2026-06-18

### Fixed
- **`hound -u` finally just works on Windows.** The root cause of every prior failed attempt: the running `hound -u` command IS `hound.exe` (a console-scripts launcher that spawns python.exe as a grandchild), so pip can't overwrite the launcher while it runs — and every attempt to "detect the running process and refuse" flagged the command's OWN launcher (its PID is a grandparent of the python process, unreachable via `os.getppid()`), making `hound -u` refuse to run on itself forever.

  The fix: `hound -u` now spawns a **detached console updater** — a child `python.exe` (NOT hound.exe) that inherits the same console window, waits ~2s for the `hound.exe` launcher to exit, then runs pip. With the launcher gone, `hound.exe` is free and pip replaces it cleanly. The child prints pip progress and the result to the same window, so the user sees everything. The child re-checks for a REAL hound MCP server only AFTER the launcher exits (so the current command's own launcher is never mistaken for a server — the false-positive that made `hound -u` refuse on itself).

  Verified end-to-end on Windows: `hound -u` (3.6.7 -> 3.6.8) replaced `hound.exe` with no manual kill, no lock error, full pip output visible, `hound -v` confirmed the new version.

- **macOS/Linux:** no file lock, so pip runs synchronously. If a hound MCP server is running, `hound -u` warns that it will keep old code until restarted (but still proceeds — pip works on POSIX). No false refusal.

- Removed the broken upfront running-server refusal (it false-positived on the command's own launcher) and the harmful detached-fallback from 3.6.3 (which created metadata/binary mismatches). The new console updater is the primary mechanism on Windows.

### Notes
- No new features. No public API changes. 291 tests pass (rewrote the self-update test suite for the new detached-updater design: helper tests, `_spawn_console_updater` source-compile + self-contained-ness tests, `_run_pip_sync` bulletproof-message tests, `_do_update` dispatch tests for Windows/POSIX, `hound -v` tests).
- **Recovery for users on an older binary** (whose `hound -u` is one of the buggy 3.6.2-3.6.6 ones): run pip directly once, after stopping any running hound MCP server:
  ```bash
  taskkill /IM hound.exe /F            # Windows  (POSIX: pkill -f hound)
  pip install --force-reinstall --no-deps hound-mcp==3.6.7
  ```
  After that, `hound -u` works normally on every platform.

## [3.6.6] - 2026-06-18

### Fixed
- **Bulletproof, platform-aware error messages on every `hound -u` / `hound -v` failure path.** No more silent no-ops or dead-ends. Every path now tells the user exactly what to do, with the correct command for their OS.
- **Silent no-op detection (the bug that stranded users).** Previously, when pip returned 0 but `hound.exe` couldn't be replaced (a running hound MCP server holds it), `hound -u` just printed `Hound v<old>` after `Updating to v<new>...` — looking like success while nothing changed. Now `_do_update` re-reads the version after pip and, if it didn't advance, prints: "The upgrade to vX did not complete. hound.exe could not be replaced (a running hound MCP server likely holds it). Stop it: <platform stop cmd>, then re-run `hound -u`, or recover manually: pip install --force-reinstall --no-deps hound-mcp==X".
- **Platform-aware recovery commands** via two helpers: `_stop_hound_cmd()` (`taskkill /IM hound.exe /F` on Windows, `pkill -f hound` on macOS/Linux) and `_reinstall_cmd(ver)` (`pip install --force-reinstall --no-deps hound-mcp==ver`, same everywhere). Every failure path (running server, file lock, pip error, timeout, silent no-op, corrupted metadata) prints both the stop command and the reinstall command.
- **New failure paths covered with messages:**
  - PyPI unreachable (`hound -v` and `hound -u`): "couldn't reach PyPI to check for updates" + manual upgrade command.
  - pip timeout: "update timed out (pip took too long)" + reinstall command.
  - Corrupted result after update (metadata wiped): "package metadata is missing after the update" + reinstall command.
  - `hound -v` corrupted install now prints the exact reinstall command for the latest known version.

### Notes
- No new features. No public API changes. 290 tests pass (7 new bulletproof-message tests: silent no-op, PyPI unreachable for -u and -v, pip timeout, platform-aware stop command, reinstall-cmd format, corrupted-install reinstall cmd).
- **Recovery for users already stuck on an older binary** (whose `hound -u` is the buggy one): run pip directly once, after stopping any running hound MCP server:
  ```bash
  taskkill /IM hound.exe /F            # Windows  (POSIX: pkill -f hound)
  pip install --force-reinstall --no-deps hound-mcp==3.6.6
  ```
  After that, `hound -u` gives honest, actionable messages on every failure.

## [3.6.5] - 2026-06-18

### Fixed
- **`hound -u` no longer creates a metadata/binary mismatch when a hound MCP server is running.** The 3.6.3 detached-background-updater fallback was actively harmful in the common case where hound runs as a long-lived MCP server: that server process holds `hound.exe` against replacement, so the detached updater's pip run installed the new package *metadata* but could NOT replace `hound.exe` (WinError 32 on `hound.exe -> hound.exe.deleteme`), leaving the install in a broken state where `hound -v` reported the new version but the binary was still the old one. Removed the detached fallback entirely.
- **`hound -u` now refuses to update while another hound process is running.** `_other_hound_pids()` (cross-platform: `tasklist` on Windows, `ps` on macOS/Linux) detects other running hound launchers BEFORE pip is invoked. If any are found, `hound -u` prints their PIDs and the exact stop command (`taskkill /IM hound.exe /F` / `pkill -f hound`), then exits without touching pip — so a half-update is now impossible. This is the only reliable fix: a running hound MCP server holds the launcher, and no self-update trick can replace a file another process has locked.
- **Honest pip-failure message.** If the running-process check misses something and pip still hits the file lock, `hound -u` now prints "hound.exe is locked by another process. Stop any running hound MCP server, then re-run: hound -u" instead of promising a background updater that would silently fail.

### Notes
- No new features. No public API changes. 283 tests pass (replaced the detached-fallback tests with running-server-detection tests + abort-behavior tests).
- **Recovery for a machine already in the mismatch state** (metadata newer than the binary, e.g. `hound -v` says 3.6.4 but `hound.exe` is older):
  ```bash
  taskkill /IM hound.exe /F            # Windows: stop the running hound MCP server
  # (POSIX: pkill -f hound)
  pip install --force-reinstall --no-deps hound-mcp==3.6.5
  ```
  The MCP client will respawn hound (now the new binary) on next use.

## [3.6.4] - 2026-06-17

### Fixed
- **Self-diagnosis for a corrupted install.** If a previous `hound -u` was interrupted mid-update (WinError 32 before the fix), pip could delete the `hound-mcp` package metadata without replacing `hound.exe`, leaving the launcher orphaned. `importlib.metadata.version("hound-mcp")` then raises `PackageNotFoundError`, so `hound -v` printed the useless `Hound vunknown`. Now:
  - `hound -v` detects the missing-metadata case and prints a clear recovery message naming the **real package** (`hound-mcp`) and the exact reinstall command, plus an explicit warning to install `hound-mcp` and **not** the unrelated `hound` PyPI package ("A FireCloud database extension", v1.0.1) that shadows our `hound` console script in search results.
  - `hound -u` (on 3.6.3+ binaries) self-heals: when metadata is missing it prints "reinstalling to recover" and proceeds with `pip install --upgrade`, which restores both the metadata and the launcher.

### Notes
- No new features. No public API changes. 3 new tests cover the corrupted-install message content, the `hound -v` diagnosis path, and the `hound -u` self-heal path.
- **Recovery for users already in the corrupted state on an old (<=3.6.1) binary** (whose `hound -u` is still the broken one): run pip directly once:
  ```bash
  pip install --force-reinstall --no-deps hound-mcp==3.6.4
  ```
  If you accidentally installed the wrong `hound` package, remove it first: `pip uninstall hound -y`.

## [3.6.3] - 2026-06-17

### Fixed
- **`hound -u` self-update hardened cross-platform with a bulletproof fallback.** The 3.6.2 fix (rename the running `hound.exe` aside so pip can replace it) is now wrapped in a two-layer updater that guarantees no user ever hits `WinError 32`:
  - **Layer 1 — launcher staging (Windows):** `_stage_running_launcher()` renames `hound.exe` → `hound.exe.old` before pip runs (Windows allows renaming a running .exe even though it forbids overwriting it). pip then writes a fresh `hound.exe`. The `.old` is swept on the next launch by `_cleanup_old_launcher()`.
  - **Layer 2 — detached fallback (Windows):** if staging fails (read-only install, unusual layout) AND pip still hits the file lock, `_spawn_detached_updater()` spawns a background child that waits for the current process to exit (releasing the lock) and then runs pip, logging the outcome to `~/.master_fetch_cache/hound_updater.log`.
  - **macOS/Linux:** no file lock exists, so staging is skipped entirely and pip runs synchronously. None of the Windows `.exe` logic is touched on POSIX.
- **Every pip failure now prints the manual recovery command** (`python -m pip install --upgrade hound-mcp[all]`), so a user is never left without a path forward.
- **Detached-updater generated script bug:** the child one-liner double-braced the `{r.returncode}` placeholder, which would have emitted a literal string instead of the pip result. Fixed and covered by a compile-check test so it can't regress silently.

### Notes
- No new features. No public API changes. The codebase was audited end-to-end for platform-specific code: the only platform-conditional logic in the entire package is this updater section, all guarded by `sys.platform == "win32"`. Everything else (`cache`, `robots`, `security`, `server`, `trafilatura_extractor`, `reddit`, `search`) uses `Path.home()`, stdlib, and cross-platform deps (scrapling, aiosqlite, trafilatura, mcp, pydantic) — fully native on macOS/Linux/Windows.
- Verified end-to-end on Windows: staging moves the real `hound.exe` aside and real `pip` writes a fresh one (sha changed, returncode 0).
- To get onto 3.6.3 from <=3.6.1 (broken updater), run pip directly once: `python -m pip install --upgrade hound-mcp[all]`. From 3.6.2+, `hound -u` works normally.

## [3.6.2] - 2026-06-17

### Fixed
- **`hound -u` self-update failed on Windows with WinError 32**: `_do_update` ran `pip install --upgrade` *inside* the running `hound.exe` process, so Windows locked `hound.exe` against the very overwrite pip was attempting (`The process cannot access the file because it is being used by another process`). The fix stages the running launcher aside first: `hound.exe` is renamed to `hound.exe.old` before pip runs (Windows permits renaming a running .exe even though it forbids overwriting it), so pip can write a fresh `hound.exe`. The `.old` is swept on the next `hound` launch by `_cleanup_old_launcher()`. Non-Windows is unaffected (no file lock). If staging fails for any reason, the code falls through to the old behavior — no worse than before.

### Notes
- This is a Windows-only fix to the updater. **To get onto 3.6.2 from a version with the broken updater (<=3.6.1), run pip directly once** (not via `hound -u`), since `python.exe` running pip does not lock `hound.exe`:
  ```powershell
  python -m pip install --upgrade hound-mcp[all]
  ```
  After that, `hound -u` works normally for future updates.

## [3.6.1] - 2026-06-17

### Fixed
- **robots.txt scrapling fetch path was silently broken**: `_fetch_robots_txt` wrapped an async `sess.get()` coroutine in `asyncio.to_thread` and unpacked the result as a `(response, elapsed)` tuple. The coroutine was never awaited and the unpack always raised, so every robots.txt lookup fell through to the plain-urllib fallback — defeating the browser-impersonated fetch path entirely. Now awaits `sess.get()` directly and reads `response.body`. Impersonated requests reach sites that block stdlib urllib.
- **`smart_fetch` bulk mode silently truncated URLs past `MAX_BULK_URLS`**: `_smart_fetch_bulk` dropped overflow URLs with `urls[:MAX_BULK_URLS]` and no warning — silent data loss. Now raises `ValueError` matching the single-URL and `bulk_get`/`bulk_fetch`/`bulk_stealthy_fetch` behavior.
- **`force_fetcher="http"` ignored the caller's `timeout`**: the HTTP branch hardcoded `timeout=30` seconds. A caller asking for a 5s budget got 30s. Now passes `timeout=max(1, min(int(timeout/1000), 30))`. The auto-escalation HTTP tier now also honors the caller timeout instead of always using the 30s default.
- **`validate_proxy` rejected `socks5`/`socks5h` dict proxies**: the dict path validated the `server` URL with `validate_url`, which only permits `http`/`https`, so `proxy={"server": "socks5://host:1080"}` was rejected even though the string form `socks5://host:1080` was accepted. Now uses the same `http/https/socks5/socks5h` scheme set as the string path (and still allows internal/local proxy hosts).
- **`_safe_cookie_dict` leaked cookie values into logs**: the "missing name" warning logged the whole cookie dict, which may contain a sensitive `value`. Now logs a fixed message without the dict.
- **`_http_with_retry` retried deterministic validation errors**: `SecurityError`/`ValueError` (bad URL, oversized response body, blocked scheme) were retried 3x with exponential backoff — re-running the same deterministic failure (and, for oversized bodies, re-downloading them). Now surfaces validation errors immediately and retries only transport/network failures.

### Removed (dead code)
- **`domain_intel.py`** (207 lines): per-domain protection-level tracking was orphaned when v3.5.1 removed domain-intel routing from `smart_fetch`. No production code imported it; the `server.py` comment claiming it was "imported on demand for list_sessions stats only" was false. Removed the module, `tests/test_domain_intel.py`, and the domain-intel test classes/methods in `test_reliability_v3.py`.
- **`_close_auto_dynamic_session`** + its two `asyncio.create_task` call sites in `_force_fetch`/`_auto_escalate`: the auto dynamic session is never created in production (smart_fetch only uses the stealthy auto session), so this always no-op'd. Removed the method and the no-op task spawns.
- **`_stealthy_auto_alive`** and **`_acquire_stealthy_session`**: defined but never called anywhere (production or tests). Removed.
- **`reddit.enhance_old_reddit_extraction`**: stub that returned its input unchanged; never called. Removed.

### Performance
- **Idle monitor no longer started in keep-alive-forever mode**: with `AUTO_SESSION_IDLE_TIMEOUT = 0` (the default), `_ensure_idle_monitor` now no-ops instead of spawning a background task that wakes every 60s only to `continue`.

### Notes
- No new features. No public API changes. `open_session(session_type="dynamic")` and the `fetch`/`bulk_fetch` dynamic methods remain supported public API; only the dead auto-routing scaffolding was removed.
- Test count unchanged in spirit: removed 8 dead domain-intel tests, added focused regression tests for the robots/bulk/proxy fixes.

## [3.6.0] - 2026-06-16

### Added
- **Reddit optimization**: Subreddit listing URLs auto-rewritten to old.reddit.com for 2x faster fetching. old.reddit.com serves the same content but with 7x smaller page size (134KB vs 1MB), reducing fetch times from 12-15s to 5-10s.
- **Structured Reddit extraction**: Custom parser for old.reddit.com listings extracts post titles, scores, comment counts, authors, and URLs as clean numbered markdown. Agents get structured data instead of raw text dump.
- **27 new tests**: URL detection (all Reddit domain variants), URL rewriting (www/m/np → old, skips /comments/), structured parser (titles, scores, comments, authors, URLs, 25-post limit).

### Changed
- **smart_fetch**: Reddit subreddit URLs (www.reddit.com, m.reddit.com, np.reddit.com) transparently rewritten to old.reddit.com before fetching. Individual post pages (/comments/...) are NOT rewritten to preserve full comment threads.

### Performance
- Reddit subreddit fetch: 5-10s (was 12-15s) — 2x faster
- Reddit page size: 134KB (was 1MB) — 7x smaller
- Reddit extraction: 5,000+ chars structured (was 1,500 chars unstructured)
- Reddit post pages: unchanged (preserves full comments)
- Cached Reddit fetches: 21ms (unchanged)
- Non-Reddit URLs: no change

## [3.5.3] - 2026-06-13

### Fixed
- **Cache schema upgrade**: `content_type` and `total_size_bytes` now persist through the SQLite cache (previously returned as empty string / 0 on cache hits, even though the live fetch populated them). `_ensure_db()` runs an idempotent `ALTER TABLE ADD COLUMN` migration on first access so users upgrading from 3.5.2 do not lose any cached entries — old rows get the new columns defaulted to `''` and `0`. Migration is safe to re-run; a `try/except` on `duplicate column name` makes it idempotent.

### Notes
- No behavior change for the live fetch path (HTTP/stealthy already populated these fields correctly). Cache hits and `cache_clear` descriptions are unchanged from a user-experience standpoint. New columns carry the existing `ResponseModel.content_type` / `ResponseModel.total_size_bytes` fields exactly.
- Backwards-compatible: `set_cached(...)` accepts `content_type=""`, `total_size_bytes=0` defaults; old callers that omit the new kwargs keep working unchanged.

## [3.5.2] - 2026-06-13

### Fixed
- **`force_fetcher` schema enum aligned with runtime**: removed `"dynamic"` from the `mcp_smart_fetch` input schema. The 3-tier path (HTTP -> dynamic -> stealthy) was dropped in v3.5.0; the schema was stale and accepted `force_fetcher="dynamic"` even though it silently rerouted to stealthy. Now only `http` and `stealthy` are valid for pinning the tier. `force_fetcher="stealthy"` and `force_fetcher="http"` continue to work as they always have.
- **Schema/escalation string docs match runtime**: `mcp_smart_fetch` description rewritten to say "http -> stealthy (2 tiers)" instead of the stale "HTTP -> browser -> stealth". `escalation_path` field description updated to reflect the new path. The dynamic tier is still accepted by `mcp_open_session(session_type="dynamic")` for backward compatibility with manual session creation; only the auto-routing removed it.

### Notes
- No behavior change. Pure schema/docstring cleanup. Tests already use only the stealthy tier via auto-routing.

## [3.5.1] - 2026-06-08

### Added
- **Pre-warm on smart_search**: Browser launches in background when the agent calls smart_search (always the first call). By the time smart_fetch runs, the browser is warm. No race condition — search is API-only, doesn't touch the browser.
- **Browser stays alive**: Idle timeout set to 0 (keep forever). Browser sits at idle consuming minimal RAM, wakes instantly when stealthy fetch needs it, returns to idle after. No cold starts after the first search.

## [3.5.0] - 2026-06-08

### Changed
- **Ripped out Phase A/B/C domain intel routing**: No more "high", "low", "none" domain levels deciding which fetcher to use. The algorithm is now dead simple: try HTTP first. If it fails, use stealthy. That's it. Every URL gets the same treatment. HTTP is fast (~1s), stealthy handles everything else. No more stale domain intel forcing sites through slow browser paths when HTTP would work fine.
- **Removed dynamic (Playwright) tier entirely**: One browser engine (Patchright/stealthy). No second Chrome instance possible. `force_fetcher="dynamic"` now routes to stealthy — Patchright handles everything Playwright does.

### Added
- **Post-HTTP pre-warming**: After a successful HTTP fetch, the stealthy browser starts in the background. No race condition — the current call is already done. The next call that needs a browser finds it warm and ready. Zero cold-start on second fetch.

## [3.4.1] - 2026-06-08

### Fixed
- **Removed browser pre-warming (caused two-Chrome bug + slowness)**: Pre-warming created a stealthy session in the background on first smart_fetch call. This raced with Phase B: if Phase B started before pre-warming completed, it created a dynamic session, then pre-warming created a stealthy session — both lived indefinitely. Pre-warming also made stealthy always-alive, causing Phase B to always take the stealthy shortcut (slower than dynamic for simple JS rendering). Removed entirely.
- **Dynamic session close is now non-blocking**: `_close_auto_dynamic_session()` was `await`-ed in the fetch path, blocking the actual fetch while Chrome shut down. Now fires as `asyncio.create_task()` — the close happens in background, the fetch proceeds immediately.

## [3.4.0] - 2026-06-08

### Fixed
- **Double Chrome instance after auto-escalation**: When Phase B or C escalated from dynamic to stealthy, the dynamic auto session was never closed. Result: two Chrome processes (Playwright + Patchright) consuming ~300MB RAM combined. Added `_close_auto_dynamic_session()` to atomically close the dynamic session whenever a stealthy auto session is created. Patchright handles everything Playwright does — no reason to keep both.

### Changed
- **Phase C (unknown domain) skips dynamic tier**: HTTP → Stealthy directly instead of HTTP → Dynamic → Stealthy. For unknown domains, trying dynamic first wastes 3-5s launching a browser that will likely need escalation anyway, and leaves an orphan Chrome process. Cuts worst-case first-fetch latency from ~12s to ~7s.

### Added
- **Browser pre-warming**: Stealthy Chrome launches in the background on the first `smart_fetch`, `open_session`, or `screenshot` call. By the time a follow-up fetch or escalation needs it, the browser is already warm — eliminating the 3-5s cold-start penalty on subsequent calls. Fire-and-forget, fails silently.

## [3.3.1] - 2026-06-07

### Changed
- **smart_fetch description**: Added "USE THIS whenever you need information from the web — this is your web access" to make agents recognize it as their primary web tool, not an optional utility.
- **smart_search description**: Added "Search finds links — descriptions are NOT enough to answer questions. ALWAYS fetch the result URL with smart_fetch for full content" to prevent agents from answering based on search snippets alone.

## [3.3.0] - 2026-06-07

### Added
- **Idle timeout for auto browser sessions**: Browser processes now close after 30 minutes of inactivity instead of running forever. Frees ~200-300MB RAM per session. Reopens on next fetch (one-time 5s penalty). `AUTO_SESSION_IDLE_TIMEOUT = 1800`, `IDLE_CHECK_INTERVAL = 60`.
- **Session consolidation**: When the stealthy (Patchright) auto session is already alive, dynamic-tier requests use it instead of spawning a separate Playwright browser. Patchright is a Playwright fork and handles everything Playwright does. Cuts idle RAM from ~400-600MB (2 browsers) to ~200-300MB (1 browser).

### Fixed
- **TOCTOU race in session consolidation**: `_acquire_stealthy_session()` atomically checks session liveness, bumps `last_used` timestamp, and returns session ID under the sessions lock. Prevents idle monitor from closing a session between check and use.
- **Idle monitor race condition**: All reads of `_auto_*_id` and `_auto_*_last_used` now happen inside the sessions lock. Prevents stale timestamp reads that could kill a session that was just used.
- **Domain intel corruption from consolidation**: When dynamic tier is skipped due to stealthy consolidation, `record_result` now records the domain's actual level ("low"), not the fetcher used ("high"). Prevents false promotion that would skip dynamic on future fetches.
- **Orphaned sessions on close failure**: When `close_session()` fails internally during idle cleanup, the session is still removed from the sessions dict and marked as not alive. No ghost sessions leaking memory.
- **Idle monitor silent death**: Added `try/except` around monitor loop body. `CancelledError` re-raised for proper asyncio cleanup. Other exceptions logged and loop continues on next cycle.
- **Monitor restart on session reuse**: `_ensure_idle_monitor()` now called on all return paths of `_ensure_auto_session()` (not just new session creation).

## [3.2.0] - 2026-06-07

### Fixed
- **Double-chunking regression**: `bulk_get`, `bulk_fetch`, `bulk_stealthy_fetch` were applying `_apply_chunking` internally, then `_finalize_result` chunked again. Result: 148KB JSON appeared as 40KB because chunking ran twice. Removed chunking from bulk methods — only `_finalize_result` (single-URL path) and `_smart_fetch_bulk` (bulk path) apply chunking once.
- **Cache now stores unchunked content**: Since bulk methods no longer chunk, cache stores full extracted content. Offset-based pagination now works correctly — the second call with `offset=40000` gets the correct remaining content instead of the first chunk again.

## [3.1.0] - 2026-06-07

### Changed
- **Smart chunking merge**: When remaining content after a chunk is less than 500 chars, it's included in the current chunk instead of setting `is_truncated: true`. No more wasteful round-trips for 55-char second calls.
- **`total_extracted_chars` field**: New ResponseModel field shows total extracted text length (before chunking). Agents can calculate remaining content without a follow-up call: `total_extracted_chars - offset`.
- **Honest chunking meta-awareness**: `offset` pages through EXTRACTED text, not raw HTML. If extraction produces 40KB from a 1MB page, offset can't reach beyond that 40KB. Tool description now explains this. Use `extraction_type=html` for raw HTML.
- **Honest truncation messages**: Now includes exact remaining chars count: `[Truncated: showing 40,000 of 120,000 extracted chars. 80,000 chars remaining. Next offset: 40000]`
- **Cache meta-awareness**: `cache_clear` and `mcp_smart_search` descriptions now mention cache behavior and TTL.

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

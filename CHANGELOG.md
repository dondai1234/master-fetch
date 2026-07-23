# Changelog

## [Unreleased]

### Fixed

- Forward the documented `focus` and `actions` arguments from the MCP tool dispatcher to `smart_fetch`.

## [11.2.0] - 2026-07-23

### Added: Six-signal search ranking

Hound's search reranking uses a six-signal composite score, all zero-latency
(operate on already-fetched snippets, titles, URLs - no extra HTTP calls):

- **Neural relevance** (existing): ONNX cross-encoder scores (query, title+snippet) topical relevance.
- **Cross-engine consensus** (existing): URL returned by N distinct index-families gets +0.2 * (N-1).
- **Domain reputation**: Known authoritative domains get +0.15 for technical queries.
  Tech domains: github.com, arxiv.org, stackoverflow.com, huggingface.co, paperswithcode.com,
  pytorch.org, openai.com, anthropic.com, deepmind.google, and 20+ more. Reference domains
  (wikipedia.org, britannica.com) get +0.05 for any query. Not a blocklist: Medium/Substack
  are simply not boosted, not penalized. Subdomain matching (en.wikipedia.org matches wikipedia.org).
- **Answer-signal scoring**: Detects if the snippet CONTAINS the answer vs just
  DISCUSSES the topic. Queries asking for "dimension/size/parameters" boost snippets with
  3+ digit numbers (+0.15). Queries with "table/comparison" boost snippets with table markers
  (+0.15). Queries with "vs/compare" boost snippets with comparison structure (+0.10). Queries
  with "code/api/function" boost snippets with code markers (+0.10). Capped at +0.30.
- **Title relevance** (new): Query terms appearing in the result title get up to +0.10 boost.
  A result titled "GPT-3 Architecture: d_model=12288" outranks "The Evolution of AI" for a
  query about "d_model", even with similar neural scores.
- **URL relevance** (new): Query terms appearing in the URL path get up to +0.08 boost.
  /docs/api/models/gpt-3 is more relevant than /blog/random-thoughts for a "gpt-3 api" query.

### Added: Source type detection

Each search result now has a `source_type` field classifying the source by URL pattern:
`docs`, `paper`, `repo`, `blog`, `forum`, `reference`, `news`, or `other`. Helps the agent
pick the right source: docs for API documentation, paper for research, repo for code,
forum for Q&A, reference for encyclopedic. Surfaced in `fetch_hint` as a type breakdown
(e.g. "Source types: repo:2, paper:1, blog:3").

### Added: Snippet merging from multiple engines

When the same URL appears in multiple search engines, their snippets are now merged
(capped at 600 chars). Different engines often extract different sentences from the
same page, so merged snippets give the agent richer information per result without
fetching the page. Zero-latency (no extra HTTP calls).

### Added: Smarter fetch_relevance tiers

The `fetch_relevance` tier (high/med/low) now incorporates consensus, domain reputation,
and answer signals, not just neural score. A GitHub repo with 3-engine consensus and
answer signals gets "high" tier even with a medium neural score. This prevents a blog post
from getting "high" while a technical reference with the actual data gets "med".

### Added: Heading-aware BM25 for focus_content

The `focus_content()` function (used by smart_fetch `focus=`) now uses three-pass block
selection:

1. **BM25 scoring** (existing): k1=1.5, b=0.75 with always-positive IDF.
2. **Heading-aware boosting** (new): When a markdown heading contains query terms, ALL blocks
   under that heading get 1.5x score boost. Content under "## Model Architecture" is relevant
   to a query about "architecture" even if individual paragraphs don't contain the exact word.
3. **Table/code preservation** (new): Tables and code blocks containing ANY query term are
   ALWAYS kept, regardless of BM25 score. Tables get BM25-underweighted (sparse tokens) but
   are high-value for factual queries.

### Added: Neural rerank pre-filter (41% latency reduction)

The neural cross-encoder previously scored ALL results from all engines (often 30+ URLs).
Now it pre-filters to 2x max_results (12) before neural rerank, since the top results by
engine rank order are already the best candidates. This cuts rerank time from ~3s to ~0.5s,
reducing total search latency by 41% (5.8s -> 3.4s with warm reranker).

### Added: Faster search deadline

The per-engine search deadline has been reduced from 8s to 5s. Engines that haven't responded
in 5s are cancelled (they're almost certainly blocked/rate-limited, not just slow). The soft
deadline (2s) with early-return-on-quorum means most searches still return in 2-3s.

### Removed: include_content feature

The `include_content` parameter has been removed entirely from smart_search. It was a failed
experiment: instead of reducing tool calls, it added 6-10 seconds of latency per search (fetching
6 pages with 10s timeout each), added ~33k tokens of content damage per multi-search session,
and the content quality was too low (BM25-filtered snippets from blog posts, not the actual
data tables the agent needed). The efficient workflow is: search (fast, returns URLs + snippets,
no page fetches) + smart_fetch with focus= (accurate, gets exactly the content needed).

### Tests

62 new adversarial tests in `test_search_quality.py` covering domain boost, answer signals,
title/URL relevance, source type detection, smarter tier calculation, snippet merging, heading-
aware BM25, and table/code preservation. Total: 466 tests.

## [11.1.10] - 2026-07-23

### Added: Docker support (PR #12 by @imonlinux)

Production-ready Docker setup for running hound as an HTTP MCP server in a container:

- **Dockerfile**: Multi-stage build from `python:3.12-slim-bookworm`. Installs both
  Playwright and Patchright browser binaries with `--with-deps` (Chromium system
  libraries). Non-root user for Chromium sandbox. Optional `INSTALL_CHROME=true`
  build arg to bake in real Google Chrome for maximum stealth. TCP healthcheck.
  Serves streamable-HTTP MCP at `http://<host>:8765/mcp`.
- **docker-compose.yml**: `shm_size: 1gb` (prevents Chromium crashes on default
  64MB /dev/shm), `init: true` (reaps zombie processes), DNS set to 1.1.1.1 /
  9.9.9.9, cache volume, 3g memory limit, `restart: unless-stopped`.
- **.dockerignore**: Excludes .git, __pycache__, .venv, tests, docs from image.
- **docker-ci.yml**: CI workflow that builds the image, starts the container,
  runs healthcheck, and verifies the MCP endpoint. Only triggers on Docker file
  changes.
- **README**: Docker quick-start section with `docker-compose up -d`, manual
  `docker build`, environment variables table (including `HOUND_SEARCH_PROXY`),
  and HTTP mode connection instructions.

### Fixed: MCP schema for `actions` array (PR #12 / #13 by @imonlinux)

The `actions` parameter in `smart_crawl` was declared as `"type": "array"` without
an `"items"` property. Strict MCP clients that validate JSON Schema may reject the
parameter. Added `"items": {"type": "object", "additionalProperties": true}` so
the array schema is complete. This was the only array parameter missing `items`
(`urls` and `crawl_urls` already had `"items": {"type": "string"}`).

### Fixed: docker-ci.yml only triggers on Docker file changes

The original `docker-ci.yml` ran on every push to master (no `paths` filter on
the `push` trigger). Fixed to only run when Docker files change. Also fixed the
healthcheck to wait for `healthy` (not `healthy|starting`) and made the MCP
endpoint check tolerate non-GET responses (MCP requires POST).

### Fixed: README Docker env vars

Removed `HOUND_HTTP_PORT` (not actually used, port is hardcoded in Dockerfile).
Added `HOUND_SEARCH_PROXY` and `HOUND_SEARCH_DEADLINE` to the env var table.

## [11.1.9] - 2026-07-22

### Fixed: HOUND_SEARCH_PROXY reliability (3 bugs)

**Bug 1: Proxy not stripped.** `HOUND_SEARCH_PROXY=" http://proxy:8080 "` (whitespace
around a valid URL) crashed the DuckDuckGo backend (httpx) at construction
while other backends (primp) tolerated it. Whitespace-only (`"   "`) crashed
every backend. Fix: `_PROXY` is now `.strip()`-ed and validated at import time.

**Bug 2: socks5 silently broken for DDG.** `pyproject.toml` declared
`httpx[http2]` but not `httpx[socks]`. The `socksio` package (needed for socks5
proxy support in httpx) was not a dependency. A fresh `pip install hound-mcp`
would silently skip the DuckDuckGo backend when a socks5 proxy was configured.
Fix: changed to `httpx[http2,socks]>=0.27`.

**Bug 3: Bad proxy = silent 0 results.** When all engines failed to construct
(bad proxy, missing deps), `metasearch()` returned `(0, {})` with no error.
The user got 0 search results and no explanation. Fix: raises
`MetaSearchException` with the proxy value and per-engine status when no engines
can start.

Also added proxy scheme validation: only `http`, `https`, `socks5`, `socks5h`
are accepted. Invalid schemes are rejected with a warning and fall back to
direct connection.

6 new tests in `TestSearchProxyValidation` verify all cases.

## [11.1.8] - 2026-07-22

### Fixed: MCP server fails to start with -32001 REQUEST_TIMEOUT (issue #11)

The MCP server intermittently failed to respond to the `initialize` handshake,
 causing the client to time out with -32001 REQUEST_TIMEOUT. The server process
 stayed alive but never replied.

**Root cause:** `_prewarm_stealthy()` called `_browser_deps_available()` on the
 asyncio event loop before any thread offload. This triggered `import patchright`
 synchronously, blocking the single-threaded event loop for 1-3 seconds. Since
 `server.run()` shares the same event loop, the MCP `initialize` handshake never
 got processed, and the client timed out.

The code already had a comment explaining this exact problem and an
 `asyncio.to_thread()` call for the import, but the `_browser_deps_available()`
 check at the top of `_warm()` was missed - it did the import on the event loop
 before the thread offload.

**Fix (three layers of defense):**

1. **Moved browser check into the thread.** `_prewarm_stealthy()` now runs
   `check_browser_available()` inside `asyncio.to_thread()`. The synchronous
   `import patchright` never touches the event loop.

2. **Made `_browser_deps_available()` non-blocking.** Previously, it called
   `check_browser_available()` which triggers `import patchright` on first call.
   Now it reads only the cache populated by the prewarm thread. If the cache is
   not yet populated (prewarm hasn't finished), it returns True (optimistic).
   The actual browser operation catches ImportError gracefully if patchright
   isn't installed. This protects all 8 call sites, not just the prewarm.

3. **Added `is_browser_available_cached()` to browser.py.** A cache-only reader
   that returns None if not yet checked, True/False if cached. Never triggers
   an import. Safe to call on the event loop at any time.

Also renamed `_scrapling_available` -> `_browser_deps_available` and
`_scrapling_import_error` -> `_browser_import_error` throughout (the `scrapling`
name was a leftover from v10.x; scrapling was removed in v11.0.0).

6 new tests in `TestBrowserDepsNonBlocking` verify: non-blocking when cache
unset, cached True/False returned instantly, `is_browser_available_cached`
reads without import, `check_browser_available` caches result, prewarm uses
`asyncio.to_thread` (not the event loop) for the browser check.

## [11.1.7] - 2026-07-22

### Changed: agent-optimized tool descriptions

- Removed `HOUND_INSTRUCTIONS` (MCP `instructions` field). It was injected once
  at connect time, gone after any context compact, and duplicated tool defs.
  All guidance now lives in the tool descriptions, which are re-injected every
  turn (tools/list) and survive compaction. Net token savings: -476 tokens
  (-17.5%) vs the instructions+tools approach.
- `mcp_smart_fetch` description now includes: structured POWER FEATURES section
  (focus, pages, urls), DECISION GUIDE (have URL+question? focus=; have PDF+
  know page? pages=; have PDF+don't know page? focus= finds it), RESPONSE
  SIGNALS section (content_ok, next_action, page_type, is_truncated, content_age,
  quality_score), css_selector vs focus distinction, bulk urls= guidance.
- `mcp_smart_search` description now includes: WORKFLOW section (search ->
  fetch with focus=), ANTI-PATTERN (don't search when you have a URL), FILTERS
  section (site, exclude_sites, freshness with usage examples), RESULT FIELDS
  section (relevance_score, fetch_relevance, engines_consensus, related_queries).
- `mcp_smart_crawl` description now includes: WHEN TO USE guidance, TWO-PHASE
  CRAWL pattern (sitemap -> crawl_urls), focus= for large doc sites.
- `_agent_hints` next_action for truncated pages now suggests `focus=` first,
  then `offset=`. Large PDFs (>20K chars) now get a next_action suggesting
  `focus=` or `pages=`.
- Pi extension descriptions mirrored to match new MCP defs. Sync verified.

### Fixed

- Search `site` and `exclude_sites` filters now compare normalized hostnames by
  exact domain or subdomain boundary. This rejects lookalike hosts such as
  `evilgithub.com` and `github.com.evil.test` while preserving real subdomains.
- GitHub repository case variants now deduplicate in `smart_search`. Owner and
  repo name are treated as case-insensitive for `github.com` URLs, while branch
  and file-path casing remains unchanged. Credential-bearing URLs skip the
  GitHub-specific folding.
- `detect_page_type()` no longer classifies a page as a paywall solely because
  raw HTML contains the bare token `paywall`. Concrete visible subscription
  prompts and active, exact paywall data attributes remain signals; false-like
  values, foreign attribute names, and examples inside metadata, scripts,
  styles, fallbacks, templates, or comments are ignored.
- GitHub system routes (topics, settings, explore, dashboard, etc.) are now
  excluded from case-folding in `smart_search` dedup. Only actual repository
  owner/name segments are case-folded; system route paths retain their casing.

### Changed: test suite rewrite

- Wiped 833 legacy tests (36 files, 9,663 lines) and wrote 376 new
  behavior-focused adversarial tests (10 files, 2,219 lines). Organized by
  capability, not version. Tests real code against real inputs. Covers SSRF
  bypass vectors, paywall false positives, CF challenge detection, canvas
  noise, hostname boundary filtering, GitHub case-folding, cache TTL +
  eviction, BM25 focus filtering, chunking, stealth fingerprint profiles.

## [11.1.6] - 2026-07-21

### Fixed: smart_crawl does not bypass Cloudflare on protected sites

`smart_crawl` uses `extraction_type=html` internally to extract links from
pages. When a CF-protected site returns a 200 challenge page, the raw HTML is
large (40KB+ of CF Turnstile scripts), so `_is_js_shell()` did not detect it
as a JS shell. The crawl never escalated to the stealthy browser and returned
the CF challenge page as content.

Fix: add CF challenge page markers (`cf-turnstile`, `challenges.cloudflare.com/turnstile`,
`cf_chl_opt`, `__cf_chl`, `cf-browser-verification`, `challenge-platform`,
`cf-mitigated`) to the JS shell detection. When any of these appear in the
HTTP response content, the page is detected as a CF challenge and escalation
to the stealthy browser is triggered.

Verified: `smart_crawl` on nowsecure.nl (CF challenge on every page) now uses
`http->stealthy` escalation instead of returning the challenge page as content.

806 tests (1 new: `test_is_js_shell_detects_cf_turnstile`).

## [11.1.5] - 2026-07-21

### Fixed: Response.css() no longer silently returns [] on errors

Follow-up to the cssselect dependency fix (#3). `Response.css()` had a broad
`except Exception: return []` that silently swallowed ALL errors, including
`ImportError` when `cssselect` was missing and `SelectorSyntaxError` for invalid
selectors. This meant broken CSS selectors produced empty results instead of
an error, making debugging impossible.

Fix: removed the broad except. Errors now propagate to the caller. The action
layer validates selectors upstream via `validate_css_selector()`, so invalid
selectors are caught before reaching `css()`. The `cssselect>=1.2` dependency
added in v11.1.4 ensures the import never fails.

805 tests (1 new: `test_response_css_invalid_selector_raises`).

## [11.1.4] - 2026-07-21

### Fixed: missing cssselect dependency breaks Response.css()

`lxml` does NOT list `cssselect` as a dependency. On environments where
`cssselect` isn't installed by other packages, `lxml.cssselect.CSSSelector`
import fails, and `Response.css()` silently returns `[]`.

Fix: add `cssselect>=1.2` as an explicit core dependency. Also switched HTML
parsing from `lxml.html.fromstring()` to `lxml.html.parse()` from BytesIO for
cross-platform consistency.

804 tests. No behavior change.

## [11.1.0] - 2026-07-21

### Stealth fetch: next-generation anti-detection

Six novel innovations that make hound's stealthy browser significantly harder
to detect, based on 2026 anti-detection research (31-target benchmark,
CDP protocol analysis, Cloudflare v9 behavioral scoring).

**1. System Chrome auto-detection (channel=chrome)**

The biggest single win from the benchmark. Hound now defaults to using the
system-installed Google Chrome (channel='chrome') instead of bundled
Chromium. System Chrome has a real TLS fingerprint (JA4) that matches what
real users have. Bundled Chromium's fingerprint differs and is detectable.
Falls back to bundled Chromium if Chrome isn't installed.

`_detect_chrome_channel()` auto-detects Chrome on Windows (PROGRAMFILES
paths) and POSIX (google-chrome in PATH). Cached.

**2. Coherent fingerprint profiles + init script**

4 internally consistent fingerprint identities (Win32+NVIDIA/Intel/AMD,
MacIntel+Apple). Each profile has matching platform, WebGL renderer,
languages, plugins, hardware specs. No contradictions that detectors
cross-check for.

`_build_stealth_init_script()` generates a JS init script that patches
JS-layer signals patchright does NOT handle. Injected per-page via CDP
`Page.addScriptToEvaluateOnNewDocument` (not context.add_init_script,
which uses Routes and breaks DNS with patchright).

Essential patches (always applied, even with system Chrome):
- navigator.webdriver → undefined (patchright sets false)
- navigator.userAgent: removes 'HeadlessChrome' (dead giveaway)
- navigator.languages: adds 'en' fallback for consistency
- Canvas fingerprint: per-session deterministic noise (seeded PRNG)
- Permissions API: consistent responses

Full patches (only for bundled Chromium, where defaults are wrong):
- WebGL vendor/renderer: replaces SwiftShader with real GPU strings
- navigator.plugins: populates from 0 to 5 entries
- navigator.hardwareConcurrency / deviceMemory
- navigator.platform
- window.chrome runtime object

**3. Human behavior simulation**

Cloudflare v9 ML model weights behavioral telemetry (mouse path entropy,
time-on-page, scroll velocity). `_simulate_human_behavior()` adds:
- Randomized dwell time (1-2.5s before interaction)
- Bezier curve mouse movement (15-30 steps, ease-in-out, overshoot+correction)
- Smooth scroll (variable speed, slight pauses)

~1.5-2.5s total, only for stealthy fetch, configurable via `humanize=True`.

**4. Cloudflare Turnstile solver with human mouse movement**

The CF solver now moves the mouse to the checkbox via a bezier curve
before clicking, instead of a straight-line click. Passes behavioral
scoring that checks mouse trajectory entropy.

**5. HeadlessChrome UA fix (both JS and HTTP headers)**

`_get_chrome_ua()` reads the system Chrome version (PowerShell on Windows,
`chrome --version` on POSIX) and constructs a UA with 'Chrome' instead of
'HeadlessChrome'. Applied to both `navigator.userAgent` (via init script)
and HTTP headers (via context user_agent option). The UA matches the
real Chrome TLS fingerprint from channel=chrome.

**6. Removed contradicting overrides for system Chrome**

When using channel=chrome, hound no longer overrides:
- User-Agent (uses real Chrome UA with HeadlessChrome fix)
- device_scale_factor (was hardcoded to 2, a Mac Retina giveaway)

System Chrome already has correct WebGL, plugins, platform, window.chrome.
Overriding them with profile values creates contradictions that detectors
specifically look for.

794 tests (43 new in test_stealth_v11.py).

**Post-testing fixes:**

- Canvas noise was not working: `_noisePixels` only intercepted `toDataURL`
  (detectors like sannysoft/creepjs use `getImageData` directly). Fixed to
  intercept BOTH `toDataURL` and `getImageData`.
- Canvas noise size guard (`<= 16`) blocked noise on large reads (200x50).
  Fixed: noise applies to ALL reads, limited to first 16 pixels for perf.
- `const _seed` bug crashed the entire init script (Assignment to constant
  variable). Changed to `let _seed`. This was preventing ALL JS-layer patches
  from running.
- CDP `Page.addScriptToEvaluateOnNewDocument` does NOT work with patchright
  (it patches `Runtime.enable`). `context.add_init_script` uses Routes which
  breaks DNS. Fixed: inject via `page.goto(wait_until='commit')` + immediate
  `page.evaluate()`, which runs BEFORE the page's own JS.
- CDP session leak: each fetch created a CDP session that was never detached.
  Fixed: CDP session created for memory cleanup only, detached after use.

**Live verification (real-world results):**

- bot.sannysoft.com: ALL checks pass (HEADCHR_UA, WebDriver, Chrome,
  Plugins, PHANTOM_*, SELENIUM, CHR_DEBUG_TOOLS, etc.)
- navigator.webdriver: undefined (patched from false)
- navigator.userAgent: Chrome/150 (no HeadlessChrome)
- Canvas noise: different hashes per session (verified)
- CanadianInsider (CF Turnstile, hardest 31-site benchmark target): 200 OK
- Medium (CF interstitial): 200 OK
- StackOverflow (CF): 200 OK
- NowSecure (CF challenge): 200 OK
- Glassdoor (DataDome): 200 OK
- Memory: RSS decreased over 5 sequential fetches (no leak, pressure
  notification working)

## [11.0.3] - 2026-07-21

### Reliability: self-healing entry point + stale process cleanup

Three reliability fixes that make hound brick-proof across major version upgrades.

**1. Self-healing CLI entry point (cli.py)**

New `master_fetch/cli.py` module replaces `master_fetch.server:main` as the pip
entry point. It is deliberately lightweight (stdlib only, no heavy imports at
module level). When `hound` runs, it tries `from master_fetch.server import
main` inside a try/except. If the import fails (missing dep, corrupted
install, half-failed update), it:
- Detects the ImportError
- Checks if `~/.hound/repair.py` exists
- If yes: runs it automatically (kills hound + force-reinstalls)
- If no: writes an inline repair script and runs it
- Prints a clean one-line error (not a traceback)

Users never see a raw `ModuleNotFoundError` traceback again. A broken install
auto-recovers on the next `hound` command.

**2. Stale process cleanup during updates**

The detached Windows helper (spawned by `hound -u`) now kills ALL hound.exe
processes before running pip, not just in the `--reinstall` path. This
prevents stale hound servers (e.g. a Pi extension's singleton subprocess)
from surviving an update and running old code. The POSIX path also kills
stale servers before pip.

**3. Doctor detects stale processes**

`hound --doctor` now includes a "no stale servers" check. If hound.exe
processes are running, it reports their PIDs and suggests the kill command.

**4. Doctor browser deps check fixed**

Removed `curl_cffi` from the browser deps check (no longer a dependency
since v11.0.0 removed scrapling). Now checks `playwright` + `patchright` only.

761 tests (8 new in test_self_heal.py).

## [11.0.2] - 2026-07-21

### Fixed: smart_fetch and smart_crawl broken (follow_redirects type error)

`smart_fetch` and `smart_crawl` were completely broken after the scrapling
removal in v11.0.0. The root cause: `follow_redirects` defaults to the
string `"safe"` (scrapling's API accepted strings: "safe", "always",
"never"). Hound's new `HTTPSession` passed this string directly to
`primp.Client.get(follow_redirects="safe")`, but primp expects a `bool`.

**Fix**: `HTTPSession.get()` now coerces `follow_redirects` from string to
bool before passing to primp. `"safe"` and `"always"` become `True`,
`"never"` becomes `False`, and actual `bool` values pass through unchanged.

Also fixed: stale `"chrome131"` default impersonate value in `bulk_get()`
that triggered a primp warning on every fetch.

## [11.0.1] - 2026-07-21

### Fixed: self-update installs core deps (not --no-deps)

The v10.x self-update used `--no-deps` to avoid pulling the heavy `[all]`
extras (onnxruntime, tokenizers, rapidocr). This was correct for v10.x where
the core dep tree didn't change between minor versions.

v11.0.0 added `markdownify` as a new core dep. The `--no-deps` flag meant
pip skipped it. The old v10.4.1 install didn't have markdownify (it came
via scrapling's transitive tree). After `--no-deps` update, `import
master_fetch` crashed on `import markdownify` with `ModuleNotFoundError`.

**Fix**: Drop `--no-deps` from the primary update pass. `pip install
hound-mcp==TARGET` (no `--no-deps`, no `[all]`) installs hound-mcp + any
missing core deps, but does NOT pull the heavy `[all]` extras. Existing
deps already satisfied are left alone by pip. The self-heal pass also
drops `--no-deps` for the same reason.

The `reinstall` command (`hound --reinstall`) still uses `--no-deps` with
`[all]` because its purpose is a full reinstall where the [all] extras are
the point, and `--no-deps` prevents breaking transitive dep compatibility
(pydantic vs pydantic-core).

## [11.0.0] - 2026-07-21

### Removed scrapling dependency entirely

Hound no longer depends on scrapling. All scrapling functionality is replaced
with hound's own modules using the underlying libraries directly (primp,
patchright, browserforge, trafilatura, lxml, markdownify).

**New modules:**
- `fetcher.py` (18KB): Response class + HTTPSession (primp-based HTTP fetch
  with TLS impersonation). Replaces scrapling's FetcherSession (curl_cffi).
- `browser.py` (29KB): StealthyBrowser + DynamicBrowser (patchright-based).
  Includes Cloudflare Turnstile solver, anti-detection stealth args,
  fingerprint generation via browserforge, resource blocking. Replaces
  scrapling's AsyncStealthySession + AsyncDynamicSession.
- `extractor.py` (5KB): Content extraction using trafilatura (primary) +
  markdownify (fallback). Replaces scrapling's Convertor._extract_content.

**Dependency changes:**
- `scrapling>=0.4.7` removed from core deps entirely
- `curl_cffi`, `msgspec`, `protego`, `click`, `apify-fingerprint-datapoints`
  removed from [all] extra (were scrapling transitive deps)
- `markdownify>=1.2.0` added to core deps (HTML->markdown fallback)
- `patchright>=1.50`, `playwright>=1.50`, `browserforge>=1.2.4` remain in [all]

**What this means:**
- No more scrapling dependency surprises (API changes, broken transitive deps)
- Drop scrapling + ~15 transitive deps from the install
- Full control over the fetch pipeline (can fix bugs without waiting for
  scrapling releases)
- Faster cold start (no scrapling import chain)
- Smaller, cleaner dependency tree
- The v10.5.0 graceful degradation code is now the primary path, not a fallback

**Response class** mimics scrapling's interface: .status, .url, .headers,
.body (bytes), .encoding, .content (decoded), .css() (CSS selector via lxml),
.reason, .cookies. ElementWrapper provides ._root (lxml node) for
trafilatura's CSS-selector narrowing path.

**Cloudflare solver** ported from scrapling's StealthySessionMixin (~80 lines):
detects challenge type (non-interactive, managed, interactive, embedded),
calculates Turnstile checkbox coordinates from iframe bounding box, clicks
with random delay, waits for challenge to clear. Standard Playwright page
manipulation, no secret sauce.

753 tests pass (29 new tests for the replacement modules).

## [10.5.0] - 2026-07-21

### Fixed: Termux/Android install + HTTP-only graceful degradation

The lean install (`pip install hound-mcp`) pulled `scrapling[ai]`, which
transitively activates `scrapling[fetchers]`, which pins `playwright==X.Y.Z`
exactly. Playwright has no wheels for Termux/aarch64/Python 3.13, so the
entire install failed. Even if the pin were loosened, playwright itself
returns "no matching distribution" on Termux.

**Root cause**: `scrapling[ai]` was a core dependency. It dragged in
`scrapling[fetchers]` (playwright, patchright, curl_cffi, browserforge) at
install time, and `scrapling.fetchers` imports `playwright` at module level
(via `engines.toolbelt.convertor`), so even HTTP-only fetch paths were
blocked.

**Fix (two parts)**:

1. **Dependency restructure**: `scrapling[ai]>=0.4.7` replaced with
   `scrapling>=0.4.7` (core parsing only, no extras). Browser deps
   (playwright>=1.50, patchright>=1.50, curl_cffi>=0.15, browserforge,
   apify-fingerprint-datapoints, msgspec, anyio, protego, markdownify, click)
   moved to the `[all]` extra with loose pins (>=, not ==). Now
   `pip install hound-mcp` installs only scrapling core + httpx + trafilatura
   + primp + mcp, all of which have universal wheels.

2. **Graceful degradation**: when scrapling's browser deps can't be imported,
   hound falls back to HTTP-only mode:
   - `_get_scrapling()` catches `ImportError` and returns None instead of
     crashing.
   - `_FallbackResponse` class mimics the scrapling Response interface for
     `_translate_response` and trafilatura extraction.
   - `_fallback_http_get()` fetches URLs via httpx directly (no scrapling
     FetcherSession needed).
   - `_translate_response` uses trafilatura for extraction when scrapling's
     Convertor is unavailable, with a raw HTML/text fallback.
   - `_auto_escalate` skips the stealthy browser tier when unavailable and
     returns the HTTP result with a `browser_unavailable` error.
   - `screenshot`, `stealthy_fetch`, `open_session`, `bulk_fetch` raise clear
     `RuntimeError` with install instructions.
   - `_prewarm_stealthy` is a silent no-op.
   - `trafilatura_extractor._fallback_extract` handles missing scrapling.

**What works in HTTP-only mode**: `web_fetch` (HTTP tier only, no stealthy
escalation), `web_search` (primp/httpx, no scrapling needed), `web_crawl`
(HTTP fetch only), `cache_clear`, `hound_version`.

**What's disabled**: stealthy browser escalation (403/429/503 pages won't
get a browser retry), `web_screenshot`, persistent browser sessions.

**Doctor**: added a `browser deps` check (non-blocking, magenta `!` marker).
Checks `playwright`, `patchright`, `curl_cffi`. If missing, reports
"HTTP-only mode" and suggests `pip install hound-mcp[all]` if the platform
supports playwright.

18 new tests in `test_graceful_degradation.py` covering: pyproject dep
structure, _FallbackResponse, _fallback_http_get, browser method errors,
_auto_escalate skip, _prewarm no-op, _translate_response fallback paths.

742 tests pass, 0 fail.

### Changed

- `pyproject.toml`: `scrapling[ai]>=0.4.7` -> `scrapling>=0.4.7` (core only)
- `pyproject.toml`: browser deps moved from core to `[all]` extra
- `pyproject.toml`: browser dep pins loosened from `==X.Y.Z` to `>=1.50`
- `server.py`: `_get_scrapling()` catches ImportError, sets
  `_scrapling_import_error`, logs warning
- `server.py`: `_scrapling_available()`, `_FallbackResponse`,
  `_fallback_http_get()` added
- `server.py`: all browser methods raise RuntimeError when unavailable
- `server.py`: `_auto_escalate` skips browser tier when unavailable
- `server.py`: `_prewarm_stealthy` no-op when unavailable
- `server.py`: `_translate_response` trafilatura + raw fallback paths
- `trafilatura_extractor.py`: `_fallback_extract` handles missing scrapling
- `updater.py`: doctor adds `browser deps` check (non-blocking)

## [10.4.1] - 2026-07-20

### Removed: Internet Archive fallback

The archive fallback feature (auto-recover dead pages from the Wayback Machine)
was removed. It was slow, unreliable in practice, and added latency to every
hard-blocked fetch. Hard-blocks now return clean errors immediately instead of
spending time querying archive.org.

Removed:
- `archive.py` module (213 lines)
- `archive_fallback` parameter from smart_fetch
- `archive_fallback` from tool defs + options schema + dispatch allowlist
- `_ARCHIVE_FALLBACK` contextvar + `_ARCHIVE_ENABLED` env var (`HOUND_ARCHIVE_FALLBACK`)
- `_is_archive_worthy` function
- Archive.org branch in `_agent_hints` next_action logic
- `test_v10_archive.py` (entire test file)
- Archive-specific tests from `test_error_detection.py` and `test_v10_envelope.py`

Kept: `source` and `archived_at` fields on ResponseModel (always default to
`"live"` and `""` now; harmless, no behavior change for existing clients).

### Added: hound --reinstall command

Full reinstall with all deps + [all] extras, pinned to the latest PyPI version.
Uses `--force-reinstall --no-deps` to avoid breaking transitive deps
(pydantic vs pydantic-core version conflict).

### Fixed: doctor [all] extras check

Was checking `rapidocr_onnxruntime` (old v1 package name) instead of `rapidocr`
(v3 import name). Now checks: onnxruntime, tokenizers, rapidocr.

## [10.4.0] - 2026-07-20

### Universal error detection: 4xx/5xx no longer treated as real content

**The problem:** A 404 error page (or any 4xx/5xx response) was returned with
`error=""` and `content_ok=false`, but the error page HTML was still in the
`content` field. AI agents would see the error page content and mistake it for
real data. Worse, some error statuses (429, 500, 502) didn't trigger stealthy
browser escalation, so the error page was the final result with no retry.

**The fix (3 layers):**

1. `_detect_content_issue`: any 4xx/5xx status now sets `result.error` to
   `http_error_{status}: server returned error status`. The error field is the
   universal signal agents check. Before, only JS shells, geo redirects, and
   403/503 bot challenges set it.

2. `_auto_escalate`: stealthy browser escalation broadened from 403/503 to also
   include 429 (rate limited) and 500/502 (server error). The stealthy browser
   has a different fingerprint and may avoid rate limits or intermittent server
   errors. 404/410/451 don't escalate (page is gone, stealthy gets the same
   result).

3. Pi extension (`hound.ts`): when `error` is set AND `content_ok` is false AND
   source is not `archive.org`, shows `Fetch failed: {error}` instead of dumping
   the error page content as if it were real content. Archive-recovered content
   still shows normally.

22 new tests (`test_error_detection.py`). 727 total.

### Pi extension v10.4.0 (hardening)

- `HOUND_EXE` re-resolved lazily (was stale forever if hound installed after
  extension load)
- `AbortSignal` support in `call()` (Esc cancellation now propagates to hound)
- Kill existing process before respawn (prevent orphaned subprocesses)
- Extension version read from `package.json` (no hardcoded version strings)
- `session_start` notification when hound not found or failed to start
- Best-effort version sync check at session start (warns on major mismatch)
- Fixed screenshot `promptSnippet` (`options.full_page`, not top-level)
- `hound_version` formatted nicely + fallback to `hound --version`
- Root `package.json` for git install (was missing, git install discovered
  nothing)
- `scripts/check_pi_extension_sync.py` dev tool (catches description drift)

## [10.3.0] - 2026-07-20

### Token-optimized tool definitions + official Pi agent extension

Two major improvements in this release:

**1. Tool-definition token compression (29.6% reduction)**

Compressed the MCP `tools/list` + `instructions` from 3,900 to 2,746 tokens
(-1,154). Same information, tighter LLM language. Every functional fact the
model needs to use the tools is preserved; the 27 test-required keywords all
pass.

- `instructions`: 878 -> 634 tokens (-28%)
- `tools/list`: 3,022 -> 2,112 tokens (-30%)
- Per-tool: web_fetch -31%, web_crawl -33%, web_search -40%

**2. Fixed two v10 bugs in the client-visible tool descriptions**

- The v10 envelope signals (page_type, content_age_days/is_stale, source_type,
  is_official, source/archived_at) were in the internal method docstrings but
  NOT in the client-visible `_TOOL_DEFS` descriptions (the low-level MCP server
  sends `_TOOL_DEFS`, not docstrings). Now surfaced.
- `archive_fallback` was advertised as an opt-out in the instructions but was
  NOT wired through `_dispatch` — the method param always defaulted True, no
  MCP client could disable archive fallback. Now wired through the dispatch
  allowlist + documented in the options schema.

**3. Official Pi agent extension**

New `pi-extension/` directory ships a distributable Pi package that gives Pi
users all 6 Hound tools as native Pi tools with TUI rendering. Install:

```
pip install hound-mcp[all]
pi install git:github.com/dondai1234/master-fetch@v10.3.0
```

The extension spawns Hound as a singleton MCP subprocess (prewarmed at session
start, zero re-launch cost per call). No generic MCP adapter needed.

705 tests. No tool behavior, schema, or response-shape change.

## [10.2.1] - 2026-07-18

### Tool-description awareness (no behavior change)

The `smart_fetch` tool docstring (the description every MCP client shows in the
 tool list) was stale: its "Response contains" list predated the v10
research-grade envelope and never mentioned the Internet Archive fallback. A
model leaning on the tool description alone would miss `content_ok`,
`next_action`, `page_type`, `content_age_days`/`is_stale`, `source_type`/`is_official`,
`source`/`archived_at`, and the archive auto-recovery.

- `smart_fetch` docstring now lists the envelope signals to branch on, plus the
  archive fallback (`source='archive.org'`, `archived_at`, `archive_fallback=false`
  to opt out).
- The connect-time instructions' "Known unbypassable" line clarifies that
  `smart_fetch` already auto-recovers hard-blocks from the Internet Archive
  before telling you to switch sources (so the model doesn't abandon a URL that
  smart_fetch can still recover).

No tool behavior, schema, or response-shape change. The `archive_fallback`
parameter and the `source`/`archived_at` field descriptions were already correct;
this surfaces them in the one place a model is guaranteed to look - the tool
description. 705 tests.

## [10.2.0] - 2026-07-18

### Reimagined, brick-proof self-update (+ `hound doctor`, `hound --rollback`)

The old `hound -u` could **brick the install**, and once bricked `hound -u`
itself was dead so the tool could not self-heal. Two root causes:

1. `hound -u` ran `pip install --upgrade hound-mcp[all]`. The `[all]` extra pulls
   `onnxruntime` (~100 MB), `tokenizers`, `rapidocr`, `pdfplumber` - slow and
   fragile. When it failed mid-install, pip had already deleted `master_fetch`
   but could not replace `hound.exe` (Windows locks a running .exe), so every
   `hound` command crashed with `ModuleNotFoundError`, including `hound -u`.
2. The recovery messages told users to run a bare `pip install --force-reinstall`
   while a hound server held the launcher - the exact command that bricks it.

This release rebuilds the whole update lifecycle in a new `updater.py` module.

**`--no-deps`, no extras.** The self-update only touches `hound-mcp` itself.
Dependencies already installed are left alone; a user's `[all]` extra is
preserved. Fast, deterministic, cannot fail on a heavy dep.

**Windows: a detached helper runs pip after the launcher exits.** The running
`hound -u` command IS `hound.exe`, which Windows locks against overwrite. The
helper is a standalone `python -c` (no `master_fetch` dependency, so it survives
the package being replaced) that waits for the parent launcher to exit, frees
the launcher via the **rename trick** (Windows permits renaming a running .exe,
just not overwriting it), then runs pip with the launcher free. A still-running
hound server keeps the old code in memory until restarted - no need to stop it
first; a stale locked `.old` is cleared by stopping that server. It never
refuses and never bricks.

**Self-heal.** If pip's first pass leaves the version unchanged or broken, a
`--force-reinstall --no-deps` pass runs (the launcher is free by then) and
re-verifies. Catches a half-failed install automatically.

**Surviving repair.** `~/.hound/repair.py` (pure stdlib, outside site-packages)
is written on every update. If hound is ever bricked (e.g. a manual pip while a
server held the launcher), `python ~/.hound/repair.py` stops hound and
force-reinstalls. It survives because it is not part of the hound-mcp package,
so a failed pip uninstall of `hound-mcp` never removes it.

**Safe messages.** Every failure prints ONE clean error plus the safe recovery
(`python ~/.hound/repair.py`), never a bare destructive pip command.

**`hound doctor`** - proactive health check: launcher resolves, package imports,
metadata version matches the module, no stale locked `.old`, core dependencies
present, PyPI reachable, repair script ready. Catches a half-broken install
before it bricks, and prints the right fix (update / repair / reinstall deps).

**`hound --rollback`** - undo a bad update by reinstalling the version recorded
before the last update (`~/.hound/last_version`).

### Compatibility

No new tools, no MCP schema changes, no response-shape changes, no new
runtime dependencies. The CLI gained `--doctor` and `--rollback`; `hound -u`
and `hound -v` keep their flags. The update command switched from `hound-mcp[all]`
to `--no-deps hound-mcp` (faster, safer; your `[all]` extras are preserved).

### Tests

- `tests/test_optimizations_v361.py` rewritten for the new contract: helper
  source (self-contained, --no-deps, self-heal, rename-trick, wait-parent,
  repair.py fallback), `do_update` (already-latest, PyPI unreachable, POSIX
  inline success, POSIX self-heal on no-op, failure points at repair not a bare
  pip, Windows spawns helper, spawn-fail fallback, corrupted install proceeds),
  `hound -v` (corrupted points at repair, PyPI unreachable, update available),
  `hound doctor` (reports every check, writes repair script, flags missing dep),
  `hound --rollback` (nothing to roll back, already-at-previous, reinstalls
  previous), `~/.hound/repair.py` (compiles, standalone, has the stop +
  force-reinstall, does not raise on unwritable home).
- Full suite: 705 passed (was 693; +12 updater tests), 0 failed.

## [10.1.0] - 2026-07-18

### Polished, professional CLI (zero new dependencies)

The `hound` CLI commands were plain `print()` text  -  a single line for `-v`, a
wall of pip output for `-u`, generic argparse `--help`. v10.1 gives every
command a clean, GitHub-grade look that works on any machine (Linux / Windows /
macOS) without adding a dependency.

New module `cli_ui.py` (stdlib only  -  no rich) renders:

- `hound -v`: a compact bordered version panel  -  magenta wordmark, cyan
  version, right-aligned status (green ✓ up to date / magenta "vX available"),
  plus an `→ update with hound -u` hint when an update exists. A clean error
  panel with the exact recovery command when the install is corrupted.
- `hound -u`: a branded one-line progress flow (`Hound  v9.2.0 → v10.0.0 …
  updating`) with **quiet pip** (no progress-bar wall) and a clean
  `Hound  v10.0.0  ✓ updated` result. Every failure path prints one clean
  error line + the platform-aware recovery command.
- `hound --help`: a styled description (wordmark + tagline) + concise options +
  a command cheat-sheet + docs link.
- `hound --http`: a one-line `Hound  serving HTTP  http://host:port/mcp`
  banner (stdio mode stays silent  -  never corrupts the MCP protocol).

Cross-platform reliability (the renderer degrades gracefully, never breaks):

- Color only when stdout is a TTY; respects `NO_COLOR` (any value) and
  `FORCE_COLOR`. Enables Windows VT processing (Win10+). When piped, output is
  clean plain text  -  `hound -v | grep` never sees ANSI escapes.
- Unicode box borders (╭─│╰) with an **automatic ASCII fallback** (+-|) when
  stdout isn't UTF-8 (legacy consoles), and status glyphs (✓ → ✗) fall back to
  ASCII too. No mojibake on any machine.
- Visible-length math excludes ANSI codes, so the right border always aligns.

Palette: magenta + cyan-teal accents, dim gray secondary, red errors (no
amber/gold, no forest green). Minimal  -  one panel for `-v`, one-liners
elsewhere.

### Code hygiene

- Removed two dead functions (`_print_pip_failure` was never called;
  `_corrupted_install_message` lost its caller when `-v` moved to the panel).
- The Windows self-update child process prints clean one-line status (no pip
  wall) and the recovery command on every failure path.

### Tests

- `tests/test_v10_1_cli.py` (6 tests): panel rows equal visible width
  (borders align), NO_COLOR strips ANSI, `ver_transition` stamps both versions
  with `v` (the v10.1 bug where `ver(f"{a} → {b}")` gave one missing `v`),
  glyph ASCII fallback, `lr` right-alignment.
- `tests/test_optimizations_v361.py`: the `-v`/`-u`/`_run_pip_sync` tests
  updated to assert on stable visible substrings (work whether color/borders
  are on or off).
- Full suite: 693 passed (was 687; +6 cli_ui tests), 0 failed.

### Compatibility

No new tools, no schema breaks, no response-shape breaks, no new dependencies.
The CLI output format changed (that's the point); the 6 tools and the MCP
protocol are unchanged.

## [10.0.0] - 2026-07-18

### The web research tool that never gives up, and tells your agent what to do next

v10 is a flagship release built around two ideas: (1) a fetch should stop
returning dead-ends, and (2) every response should carry the trust, currency,
and next-step signals an agent needs without a second call. No new tools (still
6). One new feature, one major upgrade of the existing response envelope, and
code-quality hardening that makes the internals professional-grade.

### Internet Archive recovery (new) 

When `smart_fetch`'s live tiers hard-fail (404 / 451 / 500 / network error /
bot-block-after-escalation / auth-required), hound now automatically recovers
the page from the Internet Archive's closest snapshot and returns it  - 
honestly marked `source='archive.org'` + `archived_at` (the snapshot date) so the
agent always knows it got a dated snapshot, not the live page.

Reliability-first (the agent must never get bloat or dumb info from the archive):

- Triggers ONLY on a genuine hard-fail (see `_is_archive_worthy`). Never on
  success, soft errors, or cases archive can't fix (bad request, encrypted PDF).
- Requires the snapshot's own `status == 200`  -  rejects snapshots that archived
  a 404/error page (archive would just re-serve the failure).
- Fetches the snapshot via the Wayback `id_` identity marker, which serves the
  RAW archived HTML with NO toolbar/wrapper and ORIGINAL links intact. The
  agent gets clean content, not Wayback chrome or rewritten `/web/<ts>/` links.
  No manual HTML stripping needed.
- Validates the snapshot actually yields real content (status<400, no error,
  non-empty, not a JS shell). If it doesn't, falls through to the original error
   -  never worse than today.
- Retries the flaky availability API (transient ~1-in-10 errors, confirmed
  2026) with backoff. Caps the whole fallback at ~12s.
- Caches archive results under a separate key (`source='archive'`) so a repeat
  fetch of a blocked URL is instant, and a page that later unblocks isn't
  served a stale archive snapshot.
- Opt-out: `archive_fallback=False` per-call, or `HOUND_ARCHIVE_FALLBACK=0` env.

The only other tool doing Wayback fallback (TadMSTR/searxng-mcp) needs a
Firecrawl API key + Crawl4AI + self-hosted SearXNG + Ollama. Hound = one
`pip install`. That is the moat. Honest claim: the only free, keyless,
zero-setup fetch tool with automatic Internet Archive recovery.

### Research-grade response envelope (major upgrade)

Every `smart_fetch` response now carries trust + currency + a concrete
next step, computed from the page itself (dependency-free, ~26us/response):

- `page_type`: structural class  -  `article|docs|list|forum|qa|pdf|js_shell|
  auth_wall|paywall|redirect|image|json|unknown`. Detected from raw HTML
  (forum/qa/docs framework markers, list-page link density, `<article>`,
  paywall phrases, meta-refresh/JS redirects) with error/content-type signals
  overriding the structural guess.
- `content_age_days` + `is_stale`: from the page's own published/modified date
  (OpenGraph/JSON-LD/PDF). Prefers modified over published (a page updated
  last week is current even if first published in 2014). -1 = no date
  recoverable; future-dated = untrustworthy. `is_stale` = age > 365d.
- `source_type` + `is_official`: domain authority class
  (gov/edu/github/docs-site/qa/forum/blog/news/ecommerce/unknown) with a
  conservative `is_official` (True only on a strong canonical-owner signal).
- `next_action` got a brain: it now consumes the envelope. A list page points
  to its top 3 links (or suggests `smart_crawl`); a stale article suggests a
  dated `smart_search`; an auth wall / paywall suggests the archive or another
  source; a redirect points to the canonical URL; an archive result notes the
  snapshot date. Precedence: archive-source > page structure > freshness.

### Cache now round-trips the full envelope (fixes a silent field-loss bug)

Pre-v10, cache hits rebuilt `ResponseModel` with only content/status/
content_type/size  -  silently dropping `metadata`, `links`, `quality_score`,
`table_of_contents`, `media`, and the new envelope fields. v10 adds an
`envelope` column to the cache DB and round-trips all of them, so a repeat
fetch returns the SAME rich response as the first (and `source` separates
live vs archive cache entries).

### Code-quality hardening ("professional, not a side project")

- **Killed the vanish-on-truncation bug class.** `_apply_chunking` rebuilt
  `ResponseModel` by hand at two sites, listing fields explicitly  -  any field
  not listed (metadata, links, every envelope field) silently vanished on
  truncated responses. Collapsed both sites to `result.model_copy(update=...)`
  so fields survive by construction. Add a field to `ResponseModel` and it
  survives chunking free.
- **Killed the stale-wheel test trap.** The dev venv ships a BUILT wheel (not
  editable); `pytest` silently ran the stale installed copy and `src/` edits
  went untested (bit the project 4+ times). Added `pythonpath = ["src"]` to
  `[tool.pytest.ini_options]` so pytest always imports from src. Pinned with a
  test asserting `master_fetch.__file__` is under `src/`.
- New modules `envelope.py` (page-type/freshness/authority) and `archive.py`
  (Wayback fallback) are dependency-free (stdlib only) and fully type-annotated.
- `archive.py` imports httpx lazily (only on a fallback) so it adds zero
  startup cost.

### Performance (profiled)

`scripts/profile_hotpath.py` measures the new per-response cost: the full
envelope (`_with_agent_hints`) is **~26us/response**; `detect_page_type` on a
50KB page is ~0.7ms (once per fetch, <0.1% of fetch time); `classify_source`
  and `compute_freshness` are ~3us each. Cache hits are ~5ms (pre-existing
SQLite connection cost, still ~200x faster than a live fetch) and touch NO
browser and run NO extraction. No v10-introduced waste to cut.

### Tests

- `tests/test_v10_archive.py` (20 tests): worthiness, the `id_` marker
  transform, availability retry-then-success, rejection of archived error
  pages / empty / JS-shell snapshots, honest marking, archive cache hit, and
  the `_finalize_result` integration (fires on hard-fail, not on success,
  per-call + env opt-out).
- `tests/test_v10_envelope.py` (32 tests): source classification (gov/edu/
  github/docs/qa/forum/blog/news), freshness (recent/stale/no-date/future/
  modified-preferred/compact-timestamp), page-type detection (every class +
  the article-with-many-links precision guard), and every smart-next_action
  branch + precedence.
- `tests/test_v10_construction.py` (3 tests): truncation + no-more-content +
  short-content all preserve every field (the model_copy regression).
- `tests/test_v10_devworkflow.py`: pins the pytest `pythonpath=["src"]` fix.
- `tests/test_cache_qol.py`: the caching class gets an autouse fixture
  disabling archive so it tests caching in isolation (archive is tested
  separately).
- `scripts/live_archive_check.py`: recovers a real page from the Internet
  Archive (real network, not mocks)  -  proves both the module and the
  `_finalize_result` integration end-to-end.
- Full suite: 687 passed (was 635; +52 v10 tests), 0 failed.

### Compatibility

v10.0.0 is a MAJOR bump because archive fallback is default-on (a behavior
change on the failure path: was error-only, now archive content). All new
`ResponseModel` fields are additive with defaults (non-breaking). The 6 tools,
their schemas, and all existing response fields are unchanged. Opt-out exists
for the one behavior change.

## [9.2.0] - 2026-07-08

### Idle browser close frees RAM (Issue #1)

Hound keeps a single warm Patchright Chrome for `smart_fetch` and `screenshot`.
Until now it stayed alive for the whole process, which was the right call for
fetch latency but meant a hound left running in the background held Chrome's
RAM forever. v9.2 closes the warm browser entirely after a period of idleness
and relaunches it on the next fetch.

This is the mechanism that already existed in the codebase (`_start_idle_monitor`,
race-safe, cross-platform, no new dependencies) but was disabled with
`AUTO_SESSION_IDLE_TIMEOUT = 0`. v9.2 enables it by default and makes it tunable. The
browser process actually exits, so the OS reclaims all its RAM.

- `HOUND_BROWSER_IDLE_TIMEOUT` env var (default 300s, i.e. 5 min). Set to `0` to
  keep Chrome alive forever (the old behavior, for when RAM is not a concern).
- 300s was chosen so an agent actively working (30-90s think-pauses between
  fetches) keeps Chrome warm, while a background-idle hound actually frees its
  RAM. A fetch that pays the ~2s cold relaunch only happens after 5 min of true
  idleness.
- The idle monitor checks every 60s and only closes a session whose
  `_auto_*_last_used` is older than the threshold, under `_sessions_lock`.
- The next fetch's `_ensure_auto_session` cold-launches a fresh session, the
  same path used for the startup prewarm.

### Tests

- `tests/test_v92_idle.py` (6 tests): idle monitor reaps the warm session after
  the timeout, is a no-op at `0`, the next fetch relaunches a fresh session, a
  freshly-used session is not reaped, and the env default is 300 (not 0).
- `tests/test_optimizations_v361.py`: the old "monitor never starts" test pinned
  the disabled default; rewritten to pin the opt-out (`0`) and assert the monitor
  starts when enabled.
- `scripts/live_idle_check.py`: a real-browser live check (not a pytest test).
  Launches Chrome via a real fetch, counts Chrome procs, waits for the idle
  reap, asserts the count drops to 0, fetches again, asserts Chrome relaunches.
  Verified: 7 procs after first fetch, 0 after idle, 8 after relaunch,
  `content_ok=True` both fetches.
- 635 tests pass, no regressions.

### Compatibility

The default behavior changes: a hound left idle for more than 5 min now closes
Chrome and pays a ~2s relaunch on the next fetch. Set `HOUND_BROWSER_IDLE_TIMEOUT=0`
to restore the keep-alive-forever behavior. The 6 tools, their schemas, and
response shapes are unchanged.


## [9.1.2] - 2026-07-07

### Packaging hardening on top of 9.1.1 startup work

9.1.1 shipped the startup/lazy-import reliability work, then fresh-install
verification caught a packaging hygiene bug: Hound imports `mcp` and `pydantic`
directly but relied on Scrapling's transitive dependency chain to install them.
That is wrong for a console entrypoint package.

- Declared `mcp>=1.27.0` as a direct runtime dependency.
- Declared `pydantic>=2.0` as a direct runtime dependency.
- Kept all 9.1.1 startup reliability changes: lazy metasearch import, lazy
  reranker import, off-event-loop optional prewarm imports, removal of broad
  unawaited-coroutine warning suppression, and `merge` search metadata cleanup.

No new tools, no schema breaks, no response-shape breaks.

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
product. No new tools, no API breaks  -  the 6 tools, their schemas, and the
response shapes are unchanged.

### Fixed: clean shutdown (no more 'failed to load' on Windows)

On Windows the ProactorEventLoop's pipe transports (the warm patchright Chrome
subprocess) emit `Exception ignored in __del__` tracebacks to stderr during GC
AFTER the event loop closes  -  `RuntimeError: Event loop is closed` and
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
None, so the first search fell back to non-neural (merge/consensus) rerank  -  or
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
mcp.server.fastmcp 1.03s + mcp.types 1.03s, all eager)  -  cold starts exceeded
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
  the first response  -  they're irreducible SDK requirements, ~2s, but cached
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
  simply yields nothing and the others carry  -  the diversity IS the robustness
  my 3-engine hand-rolled never had.
- **Vendored + stripped**, not a dependency: ddgs's base engine class + the 9 text
  backends + the aggregation logic live in `search_metasearch.py`. Removed the
  CLI / API server / MCP server / images / videos / news / books / extract /
  cache / async-loop-in-thread  -  text search only.
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
- **No browser for search**  -  kills the 2nd-Chrome bug dead. Search is 100%
  HTTP; the single Patchright browser stays for smart_fetch only (eager +
  persistent at startup, as perfected).
- **Neural rerank kept** (the part that was already great); `find_similar` kept.
- `engine_blocked` now reports only genuinely-blocked backends (rate-limit /
  CAPTCHA / timeout); empty (no results) + preempted (cancelled because enough
  backends delivered) backends are not falsely flagged.
- `HOUND_SEARCH_PROXY` (http/https/socks5) is the power-user rotating-proxy
  escape hatch for per-IP throttling  -  the one thing no scraper can escape from
  a single IP.

Verified live: 10 rapid searches -> 0 dead-ends, avg 3.7s, 3 backends
contributing per search, high-quality authoritative top results (tokio.rs,
climate.ec.europa.eu, wikipedia, github, doc.rust-lang.org). 1 browser session
throughout (smart_fetch only, no 2nd Chrome). 585 tests pass.

## [7.4.0] - 2026-06-24

### fix: the real rate-limit fix  -  shared-browser search backbone + parallel race

Rate-limiting was terrible: 2 searches and all 3 engines could be dead for a
while. Root cause: the previous patch (7.3.1) made the stealthy browser lazy
AND the search→browser path had been removed, so search was bare HTTP  -  when the
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
  engines are healthy (the DDG browser render is cancelled early  -  it ran in
  parallel, not as a fallback), and the DDG browser still delivers (~3-5s) when
  every HTTP engine 429s  -  so search is **never dead**.
- **Reverted the 7.3.1 lazy-browser mistake:** the stealthy browser is eager at
  startup again + persistent for the whole session (as before). One browser
  total, shared by smart_fetch, screenshot, and search  -  no extra Chrome.
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
RAM the moment the agent started  -  even though search is all-HTTP and never
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
  render)  -  the latency cut Dondai saw. Now a rate-limited engine just returns
  its 403 in <1s and the other engines carry; no slow stealthy path in the
  common case. The stealthy browser is now a **search-wide last resort only**
  (one stealthy DDG fetch, fired solely when ALL engines blocked and 0 results  - 
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
fetch cost: **cross-engine consensus ranking**  -  a URL returned by several
independent engines is an authority signal no single-engine tool can produce.

#### Engines
- **Google REMOVED entirely.** It CAPTCHAs even via the stealthy browser, so it
  was removed rather than silently contributing nothing. `engines=['google']` is
  now rejected.
- **Brave (web) ADDED as a default engine**  -  an independent 30B-page index that
  breaks the DuckDuckGo~Bing 99%-overlap problem. The Brave Search API free tier
  was killed Feb 2026 (metered billing), so Hound scrapes the keyless web UI.
  curl_cffi/scrapling throws curl error 23 on search.brave.com (every
  fingerprint), but stdlib urllib returns 200 + 20 clean results, so Brave rides a
  urllib transport inside the resilience coordinator (still gets the pacer +
  circuit breaker; stdlib = lean installs get it too).
- **Mojeek DROPPED**  -  403s every HTTP client AND the stealthy Patchright browser
  (IP-blocked). Unreliable + per-search browser render = a speed cost.
- **Yahoo ADDED opt-in**  -  serves Bing's index from a different server, a
  redundancy source for Bing's index when bing.com rate-limits.
- **DEFAULT_ENGINES = (duckduckgo, bing, brave)**  -  three independent index
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
  deadline), same as two engines  -  no slowdown). When one rate-limits, the
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
- **Neural optimized  -  min-max normalize per query** (`reranker.rerank`): ms-marco
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

#### smart_crawl  -  best-first + content-adaptive + normalized
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

#### PDF  -  CID auto-OCR + honest quality + ToC + metadata
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

### Added  -  `smart_crawl` (new tool)
- **Deep same-domain crawl.** BFS from a start URL up to a depth/page/token budget, returns each page as clean markdown with `content_ok`/`summary`. `discover_only=true` returns the URL map. `path_include`/`path_exclude` scope it. `focus` makes it a query-prioritized crawl (most relevant pages first within the budget, each page focus-filtered). Concurrency via a semaphore; one fetch per page reusing `smart_fetch`'s anti-bot + cache.

### Added  -  OCR (scanned PDFs + image pages)
- Scanned/image-only PDFs are auto-OCR'd with `rapidocr` v3 + `pypdfium2` (pure-pip, no system binary, Python 3.13-supported). Image-only web pages (`content-type: image/*`) are OCR'd to text. Auto-caps at the first 10 pages when no `pages` spec to avoid hangs on huge scanned PDFs. `[all]` extra += `pypdfium2>=4.30`, `rapidocr>=3.0`.

### Added  -  query-focused extraction (`smart_fetch` `focus`)
- `smart_fetch(url, focus="...")` returns only the BM25-relevant blocks. Runs post-cache (one cached page serves any focus query), so it never triggers a re-fetch. 80%+ context cut on long pages. Re-pass the same `focus` when paginating.

### Added  -  `smart_search` filters + research mode
- Filters: `site`/`exclude_sites` (domain include/exclude via native TinyFish `site:`/`-site:` operators), `location`/`language` (geo), `page` (0-10). Cache key includes every filter.
- **Research mode** (`fetch_content=true`): searches AND bulk-fetches the top-N high-relevance results' full content in one call (each via `smart_fetch`, so anti-bot/PDF/OCR/cache apply), returning a `ResearchResponseModel` with per-result `content_ok` + relevance. Replaces the 3-5 call search→fetch loop.

### Added  -  page interaction (`smart_fetch` `actions`)
- `actions=[{click},{fill},{press},{wait},{scroll},{wait_selector}]` run on the stealthy browser after load, before extraction. Reaches content behind a click, search form, "load more", or infinite scroll. Forces the stealthy tier, bypasses cache. Max 20 actions, per-action error isolation.

### Added  -  metadata on every response
- Every HTML fetch carries structured `metadata`: title, description, site_name, type, image, canonical, lang, published_time, author (OpenGraph + JSON-LD + canonical + `<title>`).

### Added  -  opt-in media
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

### Added  -  flagship: PDF extraction
- **`smart_fetch` now extracts PDFs to structured, agent-optimized markdown.** PDFs are detected by content-type or `%PDF` magic bytes and routed to a new `pdf_extractor` built on `pdfplumber` (MIT, no AGPL): multi-column reading order, real tables as markdown tables, font-size heading detection, de-hyphenated paragraphs, a metadata header (title/author/date/subject), and `--- Page N ---` markers for citation.
- **`pages` param** (`"1-5"`, `"1,3,5-7"`) extracts a PDF subset to save tokens/time on big PDFs. **`password`** for encrypted PDFs. Both flow to the extractor via contextvars (task-local, bulk-safe); the cache key includes `pages` so subsets don't collide with full-PDF entries.
- Honest PDF signals: scanned/image-only PDFs return `content_ok=false` + a "needs OCR" `next_action`; encrypted PDFs report and accept a password; not-a-PDF/empty/corrupt are reported honestly.
- `pdfplumber` added to `[all]` (lean install unaffected).

### Added  -  connect-time mastery + actionable responses
- **MCP `instructions` at `initialize`.** A concise orientation (~365 tokens, paid once at handshake) gives the agent the 3-tool mental model, the #1 search→fetch workflow, and the known limits.
- **Agent-facing response fields on every fetch:** `summary` (one-line status), `content_ok` (trust content only if true), `next_action` (the obvious next call: paginate / bypass robots / switch sources), `fetched_at` (ISO-8601 UTC).
- **`smart_search` `fetch_relevance`** (high/med/low) per result + a `fetch_hint` so the agent fetches 1-2 results instead of all 10.
- **Promoted `css_selector`, `max_content_chars`, `timeout`** to first-class `smart_fetch` params (were buried in the `options` bag). `max_content_chars` is a token-spend control. Units + defaults on every param description.

### Changed  -  single warm browser instance
- **Removed `open_session` / `close_session` / `list_sessions` MCP tools** (and the `list_sessions` method). `open_session`/`close_session` stay as internal helpers. Tool count 8 → 5.
- **Eager warm-up at server startup** (was: pre-warm on first `smart_search`). The single stealthy Chrome launches when the harness starts the server, in parallel with the handshake.
- **No second browser ever spawns**: a new `_auto_session_lock` serializes auto-session creation. Keep-alive-forever; graceful shutdown closes the browser when the harness closes.
- `screenshot` now auto-manages a stealthy session (`session_id` optional); description clarifies it's for multimodal agents.

### Changed  -  Reddit optimization hardened
- Reddit URLs now **skip HTTP and go straight to stealthy** (www.reddit.com walls HTTP; saves ~1s). The old.reddit.com rewrite runs before `force_fetcher` so a pinned `http` also benefits.
- **Listing parser rewritten** to read per-post `data-*` attributes (was: span-scraping that misaligned scores/comment counts on real HTML, e.g. reported score 27 for a post whose real `data-score` was 28). Thing-block detection fixed for real HTML (`class=" thing"` has a leading space). HTML entities unescaped; promoted ads skipped; sticky/NSFW tagged; `1 comment` singular grammar; per-block span fallback for user-profile pages.
- `rewrite_to_old_reddit` now rejects lookalikes (`notreddit.com`) standalone.

### Fixed  -  caching + JS-shell detection
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
- **`hound -u` finally just works on Windows.** The root cause of every prior failed attempt: the running `hound -u` command IS `hound.exe` (a console-scripts launcher that spawns python.exe as a grandchild), so pip can't overwrite the launcher while it runs  -  and every attempt to "detect the running process and refuse" flagged the command's OWN launcher (its PID is a grandparent of the python process, unreachable via `os.getppid()`), making `hound -u` refuse to run on itself forever.

  The fix: `hound -u` now spawns a **detached console updater**  -  a child `python.exe` (NOT hound.exe) that inherits the same console window, waits ~2s for the `hound.exe` launcher to exit, then runs pip. With the launcher gone, `hound.exe` is free and pip replaces it cleanly. The child prints pip progress and the result to the same window, so the user sees everything. The child re-checks for a REAL hound MCP server only AFTER the launcher exits (so the current command's own launcher is never mistaken for a server  -  the false-positive that made `hound -u` refuse on itself).

  Verified end-to-end on Windows: `hound -u` (3.6.7 -> 3.6.8) replaced `hound.exe` with no manual kill, no lock error, full pip output visible, `hound -v` confirmed the new version.

- **macOS/Linux:** no file lock, so pip runs synchronously. If a hound MCP server is running, `hound -u` warns that it will keep old code until restarted (but still proceeds  -  pip works on POSIX). No false refusal.

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
- **Silent no-op detection (the bug that stranded users).** Previously, when pip returned 0 but `hound.exe` couldn't be replaced (a running hound MCP server holds it), `hound -u` just printed `Hound v<old>` after `Updating to v<new>...`  -  looking like success while nothing changed. Now `_do_update` re-reads the version after pip and, if it didn't advance, prints: "The upgrade to vX did not complete. hound.exe could not be replaced (a running hound MCP server likely holds it). Stop it: <platform stop cmd>, then re-run `hound -u`, or recover manually: pip install --force-reinstall --no-deps hound-mcp==X".
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
- **`hound -u` now refuses to update while another hound process is running.** `_other_hound_pids()` (cross-platform: `tasklist` on Windows, `ps` on macOS/Linux) detects other running hound launchers BEFORE pip is invoked. If any are found, `hound -u` prints their PIDs and the exact stop command (`taskkill /IM hound.exe /F` / `pkill -f hound`), then exits without touching pip  -  so a half-update is now impossible. This is the only reliable fix: a running hound MCP server holds the launcher, and no self-update trick can replace a file another process has locked.
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
  - **Layer 1  -  launcher staging (Windows):** `_stage_running_launcher()` renames `hound.exe` → `hound.exe.old` before pip runs (Windows allows renaming a running .exe even though it forbids overwriting it). pip then writes a fresh `hound.exe`. The `.old` is swept on the next launch by `_cleanup_old_launcher()`.
  - **Layer 2  -  detached fallback (Windows):** if staging fails (read-only install, unusual layout) AND pip still hits the file lock, `_spawn_detached_updater()` spawns a background child that waits for the current process to exit (releasing the lock) and then runs pip, logging the outcome to `~/.master_fetch_cache/hound_updater.log`.
  - **macOS/Linux:** no file lock exists, so staging is skipped entirely and pip runs synchronously. None of the Windows `.exe` logic is touched on POSIX.
- **Every pip failure now prints the manual recovery command** (`python -m pip install --upgrade hound-mcp[all]`), so a user is never left without a path forward.
- **Detached-updater generated script bug:** the child one-liner double-braced the `{r.returncode}` placeholder, which would have emitted a literal string instead of the pip result. Fixed and covered by a compile-check test so it can't regress silently.

### Notes
- No new features. No public API changes. The codebase was audited end-to-end for platform-specific code: the only platform-conditional logic in the entire package is this updater section, all guarded by `sys.platform == "win32"`. Everything else (`cache`, `robots`, `security`, `server`, `trafilatura_extractor`, `reddit`, `search`) uses `Path.home()`, stdlib, and cross-platform deps (scrapling, aiosqlite, trafilatura, mcp, pydantic)  -  fully native on macOS/Linux/Windows.
- Verified end-to-end on Windows: staging moves the real `hound.exe` aside and real `pip` writes a fresh one (sha changed, returncode 0).
- To get onto 3.6.3 from <=3.6.1 (broken updater), run pip directly once: `python -m pip install --upgrade hound-mcp[all]`. From 3.6.2+, `hound -u` works normally.

## [3.6.2] - 2026-06-17

### Fixed
- **`hound -u` self-update failed on Windows with WinError 32**: `_do_update` ran `pip install --upgrade` *inside* the running `hound.exe` process, so Windows locked `hound.exe` against the very overwrite pip was attempting (`The process cannot access the file because it is being used by another process`). The fix stages the running launcher aside first: `hound.exe` is renamed to `hound.exe.old` before pip runs (Windows permits renaming a running .exe even though it forbids overwriting it), so pip can write a fresh `hound.exe`. The `.old` is swept on the next `hound` launch by `_cleanup_old_launcher()`. Non-Windows is unaffected (no file lock). If staging fails for any reason, the code falls through to the old behavior  -  no worse than before.

### Notes
- This is a Windows-only fix to the updater. **To get onto 3.6.2 from a version with the broken updater (<=3.6.1), run pip directly once** (not via `hound -u`), since `python.exe` running pip does not lock `hound.exe`:
  ```powershell
  python -m pip install --upgrade hound-mcp[all]
  ```
  After that, `hound -u` works normally for future updates.

## [3.6.1] - 2026-06-17

### Fixed
- **robots.txt scrapling fetch path was silently broken**: `_fetch_robots_txt` wrapped an async `sess.get()` coroutine in `asyncio.to_thread` and unpacked the result as a `(response, elapsed)` tuple. The coroutine was never awaited and the unpack always raised, so every robots.txt lookup fell through to the plain-urllib fallback  -  defeating the browser-impersonated fetch path entirely. Now awaits `sess.get()` directly and reads `response.body`. Impersonated requests reach sites that block stdlib urllib.
- **`smart_fetch` bulk mode silently truncated URLs past `MAX_BULK_URLS`**: `_smart_fetch_bulk` dropped overflow URLs with `urls[:MAX_BULK_URLS]` and no warning  -  silent data loss. Now raises `ValueError` matching the single-URL and `bulk_get`/`bulk_fetch`/`bulk_stealthy_fetch` behavior.
- **`force_fetcher="http"` ignored the caller's `timeout`**: the HTTP branch hardcoded `timeout=30` seconds. A caller asking for a 5s budget got 30s. Now passes `timeout=max(1, min(int(timeout/1000), 30))`. The auto-escalation HTTP tier now also honors the caller timeout instead of always using the 30s default.
- **`validate_proxy` rejected `socks5`/`socks5h` dict proxies**: the dict path validated the `server` URL with `validate_url`, which only permits `http`/`https`, so `proxy={"server": "socks5://host:1080"}` was rejected even though the string form `socks5://host:1080` was accepted. Now uses the same `http/https/socks5/socks5h` scheme set as the string path (and still allows internal/local proxy hosts).
- **`_safe_cookie_dict` leaked cookie values into logs**: the "missing name" warning logged the whole cookie dict, which may contain a sensitive `value`. Now logs a fixed message without the dict.
- **`_http_with_retry` retried deterministic validation errors**: `SecurityError`/`ValueError` (bad URL, oversized response body, blocked scheme) were retried 3x with exponential backoff  -  re-running the same deterministic failure (and, for oversized bodies, re-downloading them). Now surfaces validation errors immediately and retries only transport/network failures.

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
- Reddit subreddit fetch: 5-10s (was 12-15s)  -  2x faster
- Reddit page size: 134KB (was 1MB)  -  7x smaller
- Reddit extraction: 5,000+ chars structured (was 1,500 chars unstructured)
- Reddit post pages: unchanged (preserves full comments)
- Cached Reddit fetches: 21ms (unchanged)
- Non-Reddit URLs: no change

## [3.5.3] - 2026-06-13

### Fixed
- **Cache schema upgrade**: `content_type` and `total_size_bytes` now persist through the SQLite cache (previously returned as empty string / 0 on cache hits, even though the live fetch populated them). `_ensure_db()` runs an idempotent `ALTER TABLE ADD COLUMN` migration on first access so users upgrading from 3.5.2 do not lose any cached entries  -  old rows get the new columns defaulted to `''` and `0`. Migration is safe to re-run; a `try/except` on `duplicate column name` makes it idempotent.

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
- **Pre-warm on smart_search**: Browser launches in background when the agent calls smart_search (always the first call). By the time smart_fetch runs, the browser is warm. No race condition  -  search is API-only, doesn't touch the browser.
- **Browser stays alive**: Idle timeout set to 0 (keep forever). Browser sits at idle consuming minimal RAM, wakes instantly when stealthy fetch needs it, returns to idle after. No cold starts after the first search.

## [3.5.0] - 2026-06-08

### Changed
- **Ripped out Phase A/B/C domain intel routing**: No more "high", "low", "none" domain levels deciding which fetcher to use. The algorithm is now dead simple: try HTTP first. If it fails, use stealthy. That's it. Every URL gets the same treatment. HTTP is fast (~1s), stealthy handles everything else. No more stale domain intel forcing sites through slow browser paths when HTTP would work fine.
- **Removed dynamic (Playwright) tier entirely**: One browser engine (Patchright/stealthy). No second Chrome instance possible. `force_fetcher="dynamic"` now routes to stealthy  -  Patchright handles everything Playwright does.

### Added
- **Post-HTTP pre-warming**: After a successful HTTP fetch, the stealthy browser starts in the background. No race condition  -  the current call is already done. The next call that needs a browser finds it warm and ready. Zero cold-start on second fetch.

## [3.4.1] - 2026-06-08

### Fixed
- **Removed browser pre-warming (caused two-Chrome bug + slowness)**: Pre-warming created a stealthy session in the background on first smart_fetch call. This raced with Phase B: if Phase B started before pre-warming completed, it created a dynamic session, then pre-warming created a stealthy session  -  both lived indefinitely. Pre-warming also made stealthy always-alive, causing Phase B to always take the stealthy shortcut (slower than dynamic for simple JS rendering). Removed entirely.
- **Dynamic session close is now non-blocking**: `_close_auto_dynamic_session()` was `await`-ed in the fetch path, blocking the actual fetch while Chrome shut down. Now fires as `asyncio.create_task()`  -  the close happens in background, the fetch proceeds immediately.

## [3.4.0] - 2026-06-08

### Fixed
- **Double Chrome instance after auto-escalation**: When Phase B or C escalated from dynamic to stealthy, the dynamic auto session was never closed. Result: two Chrome processes (Playwright + Patchright) consuming ~300MB RAM combined. Added `_close_auto_dynamic_session()` to atomically close the dynamic session whenever a stealthy auto session is created. Patchright handles everything Playwright does  -  no reason to keep both.

### Changed
- **Phase C (unknown domain) skips dynamic tier**: HTTP → Stealthy directly instead of HTTP → Dynamic → Stealthy. For unknown domains, trying dynamic first wastes 3-5s launching a browser that will likely need escalation anyway, and leaves an orphan Chrome process. Cuts worst-case first-fetch latency from ~12s to ~7s.

### Added
- **Browser pre-warming**: Stealthy Chrome launches in the background on the first `smart_fetch`, `open_session`, or `screenshot` call. By the time a follow-up fetch or escalation needs it, the browser is already warm  -  eliminating the 3-5s cold-start penalty on subsequent calls. Fire-and-forget, fails silently.

## [3.3.1] - 2026-06-07

### Changed
- **smart_fetch description**: Added "USE THIS whenever you need information from the web  -  this is your web access" to make agents recognize it as their primary web tool, not an optional utility.
- **smart_search description**: Added "Search finds links  -  descriptions are NOT enough to answer questions. ALWAYS fetch the result URL with smart_fetch for full content" to prevent agents from answering based on search snippets alone.

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
- **Double-chunking regression**: `bulk_get`, `bulk_fetch`, `bulk_stealthy_fetch` were applying `_apply_chunking` internally, then `_finalize_result` chunked again. Result: 148KB JSON appeared as 40KB because chunking ran twice. Removed chunking from bulk methods  -  only `_finalize_result` (single-URL path) and `_smart_fetch_bulk` (bulk path) apply chunking once.
- **Cache now stores unchunked content**: Since bulk methods no longer chunk, cache stores full extracted content. Offset-based pagination now works correctly  -  the second call with `offset=40000` gets the correct remaining content instead of the first chunk again.

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
- `NameError: name 'sys' is not defined` in `smart_fetch` and `open_session`  -  missing import in lazy loader

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
- JSON responses detected and returned raw  -  no more trafilatura mangling
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

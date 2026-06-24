# master-fetch (hound-mcp) — Status

Workspace status doc for the hound-mcp OSS tool (web fetch+search MCP). Build
log + milestones live in the repo history; this file holds the dev-notes section
for project-specific quirks that must survive context compaction.

## Shipped in 4.0.0 / 4.0.1 / 4.0.2 / 4.0.3 (2026-06-20, published to PyPI + GitHub)

- **4.0.0** LIVE: https://pypi.org/project/hound-mcp/4.0.0/ — tag v4.0.0.
- **4.0.1** LIVE: https://pypi.org/project/hound-mcp/4.0.1/ — tag v4.0.1.
- **4.0.2** LIVE: https://pypi.org/project/hound-mcp/4.0.2/ — tag v4.0.2.
- **4.0.3** LIVE: https://pypi.org/project/hound-mcp/4.0.3/ — tag v4.0.3. Docs/branding:
  added brand visuals to README (hero banner + square logo mark + editorial
  "retrieving the web" scene, committed under `docs/`, referenced via absolute
  `raw.githubusercontent.com` URLs so they render on GitHub AND PyPI). **Real
  fix found while doing this: pyproject had NO `readme` field, so every release
  through 4.0.2 shipped a wheel whose METADATA had NO long description — the
  PyPI project page showed no README at all (silent).** Added `readme =
  "README.md"`; wheel METADATA now embeds the full README (Description-
  Content-Type: text/markdown). Verified via PyPI JSON API: description 14014
  chars, all 3 images present. Image prep: lossless PNG for hero/scene (780/
  738KB; 256-color palette quantization bands the soft gradients — not premium),
  quantized PNG-8 for the flat logo mark (22KB, clean). No code changes.
- **4.0.2** detail: `hound -u` ghost-prompt overlap on Windows — the detached
  console updater child now emits a leading newline (`chr(10)`) after its 2s
  wait so its output starts below the PowerShell prompt instead of on top of
  it. (Tool quirk that bit this fix: the edit tool halves backslashes; used
  `chr(10)` instead of `"\n"` in the f-string-generated child source.)
- **4.0.1** detail: 4.0.0 shipped WITHOUT `pdfplumber` declared in the `[all]` extra (the
  edit was in the same rejected edit call as a bad description edit and never
  re-applied). So `pip install hound-mcp[all]` did not install pdfplumber and
  PDF extraction was broken for real users (circular "install hound-mcp[all]"
  error). The local dev venv had pdfplumber installed manually (`pip install
  pdfplumber` during dev), which MASKED the missing declaration — local suite
  passed (442) while CI's clean env failed. CI caught it. 4.0.1 declares
  `pdfplumber>=0.11.0` in `[all]`. **Lesson: after adding an optional dep,
  grep the BUILT wheel METADATA for `Requires-Dist: ...; extra == 'X'` to
  confirm it's declared; a locally-installed dep can mask a missing
  declaration.**
- CI: `.[dev]` → `.[dev,all]` so the PDF suite runs against the real
  optional deps; `actions/checkout@v4` → `v5` (Node 20 deprecation).
- README comparison re-researched and corrected (no false info): Jina Reader
  = optional free key (20 RPM w/o, 200 RPM w/) + natively supports PDFs
  (was wrongly "none" / "no"); Crawl4AI PDF = partial (PDFCrawlerStrategy
  but reported buggy); Firecrawl PDF = yes (cloud + OCR); Crawl4AI anti-bot
  = "limited (stealth mode)".
- CI green on 4.0.1: all 6 jobs (ubuntu+windows, 3.11/3.12/3.13) pass, 442
  tests. The notes below are the record of what 4.0.0/4.0.1 contained.

### Flagship: PDF extraction (the 4.0.0 flagship feature)
Agents hit PDFs constantly (papers, docs, reports, manuals) and hound used to
return a useless "[Binary content detected]" dead-end. Now PDFs are
auto-extracted to structured, agent-optimized markdown.

- **Engine: `pdfplumber`** (MIT, built on pdfminer.six — both MIT, NO AGPL).
  Chosen over PyMuPDF/PyMuPDF4LLM (AGPL — a real adoption blocker for an MIT
  project per research) and over pypdf (weak tables/layout). Added to `[all]`
  deps (lean install unaffected; lean users get a clear "pip install
  hound-mcp[all]" error on PDFs). Lazy-imported.
- **New module `src/master_fetch/pdf_extractor.py`** — emulates PyMuPDF4LLM's
  markdown output shape without the license risk:
  - Metadata header (`# Title` + `> Author · Date · Subject · Keywords` + a
    `> PDF · pages X–Y of N` scope line) so the agent can judge relevance
    before reading the body.
  - Multi-column reading order via pdfminer layout analysis.
  - **Word spacing fixed**: pdfplumber's default `x_tolerance=3` jams words on
    tightly-spaced PDFs (academic papers). Tuned to `x_tolerance=1.5` — verified
    on the arxiv "Attention Is All You Need" paper ("Provided proper
    attribution is provided, Google hereby grants permission...").
  - **Tables → markdown tables**, merged into the page by y-position (not
    double-extracted as messy text — non-table chars are filtered out of table
    bboxes first). `text_x_tolerance` tuned too.
  - **Font-size heading detection** (h1/h2/h3 from char-size ratios + bold),
    conservative (short, non-sentence-ending lines only) to avoid false
    positives. Verified: "## Attention Is All You Need".
  - **Paragraph segmentation** by vertical gap (median line height × 1.5), with
    **de-hyphenation** of soft line-break hyphens (only when the next line
    starts lowercase — leaves real hyphenated terms intact).
  - `--- Page N ---` markers for citation.
  - **Honest limits**: scanned/image-only PDFs detected (avg < 20 chars/page) →
    `content_ok=False` + `error=scanned_pdf` + `next_action` telling the agent
    to use a vision tool. Encrypted PDFs → `encrypted_pdf` + `next_action`
    (pass a password). not-a-pdf / corrupt / empty → honest errors. PDFs always
    return markdown-structured content regardless of extraction_type.
- **smart_fetch integration**: PDFs detected in `_translate_response` by
  `content-type: application/pdf` OR `%PDF` magic bytes (many servers serve
  PDFs as `application/octet-stream`, so the magic-byte check is the reliable
  detector — fixed a `== b'%PDF'` 5-vs-4-byte bug found during live testing).
  Routed to `_extract_pdf_response` → `extract_pdf`. The markdown then flows
  through the existing `_apply_chunking` (offset pagination for huge PDFs) +
  agent hints + (clean-content-only) caching.
- **Two new first-class smart_fetch params**: `pages` (e.g. "1-5", "1,3,5-7")
  to extract a subset — the biggest agent token/time saver on 500-page PDFs;
  and `password` for encrypted PDFs. They flow down to `_translate_response`
  via **contextvars** (task-local, safe under concurrent bulk fetches) instead
  of threading two params through every fetcher signature. **Cache key includes
  `pages`** so a subset extraction doesn't collide with a full-PDF cache entry.
- **`_agent_hints` next_action cases** added for scanned_pdf / encrypted_pdf /
  pdf_deps_missing / not_a_pdf / pdf_open_failed / pdf_extract_failed.
- Live end-to-end verified on real PDFs: arxiv 1706.03762 (2-column, headings,
  equations — clean text + `## Attention Is All You Need` heading + paragraph
  segmentation); pdfplumber background-checks.pdf (table → markdown table);
  w3 dummy.pdf (scanned → correct `scanned_pdf` error + next_action).
- Tests: `tests/test_pdf_extractor.py` (50 tests) — `_parse_pages` (ranges,
  lists, clamping, dedup, invalid, out-of-range), `_dehyphenate_join` (soft /
  real hyphen / normal), `_table_to_markdown` (markdown/text/ragged/None/empty),
  `_heading_level` (h1-h3/body/bold/sentence-end/length/zero),
  `_format_metadata` (full/missing/date-cleaning), `extract_pdf` on two real
  fixtures (text+table, scanned) + pages subset + not-a-pdf + empty + text_mode
  + encrypted (mocked) + missing-deps, `_extract_pdf_response` integration,
  `_dispatch` pages/password threading + options fallback, cache-key-includes-
  pages, tool-def schema. Full suite 438 passed.
- Fixtures committed: `tests/background_checks.pdf` (text+table),
  `tests/dummy.pdf` (scanned).

### Caching hardening (bug fix + size cap + search key)
- **Bug fix: bad content is no longer cached.** `_finalize_result` previously
  cached whenever `status > 0`, so status-200 **JS shells**, bot-challenge
  pages, geo redirects, and all_tiers_failed results were stored for the whole
  TTL. The cache-hit path doesn't restore the `error` field, so `content_ok`
  came back True and the agent TRUSTED the broken cached page for an hour.
  New `_is_cacheable(result)` helper: cache only when `0 < status < 400`, no
  `error`, and non-blank content. 4xx/5xx/JS-shells/bot-challenges/empty now
  re-fetch instead of being frozen in cache.
- **Size cap + oldest eviction** (`MAX_CACHE_ENTRIES = 10000` in cache.py):
  `set_cached` now bounds the DB — when over the cap it evicts the oldest rows
  by `fetched_at` down to 90% of the cap (batch delete, amortized cost). Fixes
  unbounded growth on long-lived agents.
- **Search cache key includes `max_results`** — was `"search:v1"`, now
  `f"search:v1:{max_results}"`. A max_results=5 and max_results=10 search no
  longer collide on one cached result set (whichever ran first used to win).
- Tests: `tests/test_cache_qol.py` (16 tests). Full suite 388 passed.
- Deferred: conditional revalidation (ETag/Last-Modified→304) — bigger HTTP
  feature; cache stats tool — low agent-actionable value, would add a tool.
  Existing smart bits (WAL, MIN(stored_ttl, requested_ttl), url+type+selector
  keying, idempotent schema migration) kept as-is.

### Single warm browser instance (replaces manual session tools + pre-warm-on-search)
- **Removed the `open_session` / `close_session` / `list_sessions` MCP tools**
  (and the `list_sessions` method). `open_session`/`close_session` stay as
  INTERNAL helpers used by `_ensure_auto_session`. Tool count 8 → 5.
- **Removed pre-warm-on-smart_search** (`_prewarm_triggered` flag + the
  `asyncio.create_task(self._prewarm_stealthy())` block in the mcp_smart_search
  dispatch branch).
- **Eager warm-up at server startup**: `serve()` schedules
  `_prewarm_stealthy()` as a background task in `_run()` (stdio) and via
  Starlette `on_startup` (HTTP/SSE). The single stealthy Chrome launches the
  moment the agent harness starts the server, in parallel with the initialize
  handshake (handshake stays instant). Best-effort: if chromium is missing it
  fails silently and the browser launches on first fetch instead.
- **No 2nd browser ever spawns**: `_ensure_auto_session` creation is now
  serialized by `self._auto_session_lock` (new asyncio.Lock). A concurrent fetch
  during warm-up waits on the lock, then reuses the in-progress session instead
  of launching a 2nd Chrome (the old close-the-orphan race can no longer happen
  in production; the final re-check guard still defends out-of-band setters).
- **Keep-alive-forever** (AUTO_SESSION_IDLE_TIMEOUT=0 unchanged): the single
  session is never closed by the idle monitor. It stays warm for the whole
  process.
- **Graceful shutdown**: `serve()`'s finally block calls new
  `_shutdown_close_sessions()` which closes every live session (best-effort)
  when the harness closes the server (stdin closed / process exit) — instead of
  relying on OS child reaping (which can orphan Chrome on Windows).
- **Idle memory is already minimal by architecture**: scrapling closes each page
  after every fetch (`page.close()` in `_page_generator`'s finally), so idle
  pages don't accumulate heap — verified via `get_pool_stats`: 0 total_pages
  when idle. Combined with `disable_resources=True` on stealthy fetches (drops
  font/image/media/css), idle memory is just the browser-context baseline
  (~100-150MB), which is unavoidable while warm. No about:blank hack needed
  (there are no idle pages to blank).
- Live end-to-end verified: warm-up 1.2s (one launch), two real stealthy
  fetches reuse the single instance (1.23s, 0.79s — no ~3-5s cold start),
  exactly 1 session throughout, graceful shutdown clears it.
- Tests: `tests/test_agent_qol.py::TestSingleBrowserInstance` (7 tests) — tools
  removed + dispatch-as-unknown, no `_prewarm_triggered` + has
  `_auto_session_lock`, creation lock → N concurrent calls = 1 `open_session`,
  `_shutdown_close_sessions` closes all, `_prewarm_stealthy` idempotent +
  swallows creation failure. Updated `test_every_known_tool_dispatches`
  (dropped the 3 removed tools); removed obsolete `test_open_session_enum_clarified`.
  Existing `TestEnsureAutoSessionRace` still passes (creation-lock rewrite
  preserves the close-orphan guard it tests).

### Agent effectiveness + QoL batch (connect-time mastery)
Goal: the moment hound connects, the agent masters the tool with 100%
effectiveness. All driven by "what would I want as an agent using this?".

- **MCP `instructions` in the initialize handshake** (`HOUND_INSTRUCTIONS`
  module constant, wired via `server.instructions` in `serve()`). Clients that
  support it inject ~365 tokens of orientation ONCE on connect: the 3-tool
  mental model, the #1 workflow (search → fetch high-relevance results →
  synthesize), known unbypassable protections, when to use screenshot. Not
  per-turn. Also set `server.website_url`.
- **ResponseModel agent-facing fields**: `summary` (one-line status like
  `200 OK · 12.4KB markdown · http · truncated`), `content_ok` (bool — trust
  content only if true; false on error status / JS shell / login wall / empty),
  `next_action` (structured next-call hint: paginate / bypass robots /
  auto-escalates / switch sources; empty = done), `fetched_at` (ISO-8601 UTC).
  Set by `_with_agent_hints` inside `_apply_chunking` (the universal final
  wrapper) so every fetch response — live, cached, robots-blocked, bulk-error —
  carries them. Agents branch on structured fields far more reliably than on
  `error` text.
- **`max_content_chars` token-spend control** — new first-class smart_fetch
  param (min 500, max 200000, default 40000). Threaded through smart_fetch →
  _force_fetch/_auto_escalate → _finalize_result → _apply_chunking. Lower it
  to load less context per call; rest paginated via offset/next_offset.
- **Promoted `css_selector` + `timeout` out of the `options` bag** to
  first-class smart_fetch params. css_selector is the #1 content-narrowing /
  token-saving lever and was previously invisible. `_dispatch` reads them
  top-level with `options` fallback for backward compat.
- **Units + defaults on every param description** in `_TOOL_DEFS` (timeout ms,
  wait ms, cache_ttl seconds, max_results 1-50, etc.). Agents were previously
  guessing units → wrong values / hangs.
- **Screenshot auto-manages a stealthy session** (`session_id` now optional;
  omitted → `_ensure_auto_session('stealthy')`). `mcp_screenshot` no longer
  requires session_id. Description rewritten: multimodal agents only (canvas /
  image-of-text / visual layout); text agents use smart_fetch.
- **`open_session` enum clarified**: `stealthy` (Patchright anti-detect,
  recommended) vs `dynamic` (plain Playwright). Both still work for manual
  sessions; only auto-routing dropped dynamic in v3.5.
- **smart_search `fetch_relevance` (high|med|low) per result** + response
  `fetch_hint` ("N high, M med, K low — smart_fetch 'high' first…"). Heuristic:
  query-term overlap with title (weighted) + snippet + position bonus. Option
  (b) per the plan: agent still fetches (keeps source provenance + token
  control), but now knows WHICH results to fetch instead of fetching all 10.
  `compute_fetch_relevance` / `compute_fetch_hint` in search.py.
- Tests: `tests/test_agent_qol.py` (38 tests) — hints, max_content_chars
  threading, dispatch promoted-params + options fallback, screenshot auto /
  explicit session, fetch_relevance tiers + fetch_hint, instructions wiring,
  tool-def schema assertions.
- Token budget: tools/list JSON now ~5.0KB ≈ 1.25K tokens (after the 3 session
  tools were removed in the single-instance batch below — the QoL schema growth
  was nearly offset). Still well under competitors' 3-5K. The ~365-token
  instructions are one-shot at connect. README's "~1K" / "8 tools" need updating
  at publish (~1.25K / 5 tools).

### Reddit optimization hardened + stealth-default routing
- **`smart_fetch` now defaults Reddit URLs straight to the stealthy tier,
  skipping HTTP.** www.reddit.com JS-walls/blocks plain HTTP ~100% of the time,
  so the HTTP tier was ~1s of wasted time before escalation. Listings AND post
  pages skip HTTP. An explicit `force_fetcher="http"|"stealthy"` still wins.
- **Reddit rewrite moved BEFORE `force_fetcher` dispatch** so an explicit
  `force_fetcher="http"` on a listing benefits from the old.reddit.com rewrite
  too (previously the rewrite ran after force_fetcher returned, so pinned-http
  hit the heavy www.reddit.com).
- **Listing parser rewritten to read per-block `data-*` attrs** (`data-score`,
  `data-comments-count`, `data-author`, `data-url`, `data-domain`,
  `data-subreddit`, `data-promoted`, `data-nsfw`). The old span-scraping regex
  picked up 3 score spans per post (dislikes/unvoted/likes) and silently
  misaligned scores + comment counts on REAL html (it reported score 27 for a
  post whose real `data-score` was 28, and post 2's score as post 1's `unvoted`
  value). Now every field is sourced from the SAME thing block — no alignment
  possible.
- **Thing-block detection fixed for real HTML**: real old.reddit.com writes
  `class=" thing id-t3_..."` (LEADING SPACE). The old `class="thing` check
  never matched real HTML and only proceeded via a loose `'reddit.com' in html`
  fallback. New pattern: `class="[^"]*\bthing\b[^"]*"`.
- **Parser returns `Optional[str]`** (`None` when no posts parsed). Caller no
  longer uses the `len(parsed) > 500` heuristic, which previously dumped raw
  HTML to the agent when the page wasn't actually a listing (login wall /
  error / post page).
- **Per-block span fallback** for user-profile pages whose thing blocks lack
  `data-score`/`data-comments-count`: the `score unvoted` span title + the
  `N comments` / `full comments (N)` link recover them, scoped to one block
  (alignment-safe). Stickied `score-hidden` comments honestly show `?`.
- **HTML entities unescaped** in titles/authors (`html.unescape`); promoted ads
  skipped; sticky/NSFW posts tagged `[sticky]`/`[NSFW]`; `1 comment` singular
  grammar; relative URLs absolutized to `https://old.reddit.com`.
- `rewrite_to_old_reddit` now rejects lookalikes standalone (`notreddit.com`,
  `reddit-clone.com` — previously `"reddit.com" in host` matched `notreddit.com`).
- Tests: `tests/test_reddit.py` rewritten (realistic fixture with 3 score spans
  + data-attrs + sticky + promoted + NSFW + entities; self-consistent real-HTML
  regression via `tests/old_reddit_real.html`, skip-gated so CI without the
  fixture passes). `tests/test_reddit_routing.py` new — locks reddit→stealth,
  non-reddit→auto-escalate, rewrite-before-force_fetcher, lookalike rejection.

## v5 plan (personal scope, NOT committed; public v5 info goes in CHANGELOG + README)

Goal: make hound the flagship $0 local MIT MCP web-research server; destroy free
alternatives; compete with paid where $0 allows. Grounded in research (Jun 2026):
Crawl4AI = Apache-2.0 (attribution required), heavy dep; Firecrawl = freemium, 12
MCP tools, cloud needs account+Redis+Docker, partial anti-bot (fails Cloudflare
per ZenRows); Tavily = paid, rich filters + /research; ZenRows = paid, 1 tool,
strong anti-bot; Jina = freemium, 19 tools, NO anti-bot. Industry insight: lean
tool count wins. MIT (no attribution) beats Crawl4AI's Apache-2.0 for embedding.

CUT from plan (Dondai's call):
- Residential proxy rotation: can't test (no proxies), risky to ship blind.
- LLM extraction via user endpoint (HOUND_LLM_*): "solving nothing" when search+fetch
  already get content; hound GETS content, the agent's own model extracts.
- CSS-schema extraction as a feature/tool: same reasoning, cut. (HTML tables as
  markdown STAYS, as a content-quality improvement, not a separate tool.)
- Captcha-solving hook (CapSolver key): untestable without a key, same risk as proxy.
- Diff/monitor: niche, not "used most times".

KEPT v5 scope:
Tier 1 (flagship): smart_crawl (deep crawl + map/discover mode + token budget +
  per-page agent hints + progress notifications); OCR via rapidocr-onnxruntime
  (scanned PDFs + images, pure-pip no system binary, Apache-2.0-compatible); smart_search
  filters (domain include/exclude, topic general/news, time range, depth) + research
  mode (one-call: search + bulk-fetch top-N full content as a structured bundle);
  query-focused fit markdown (smart_fetch `focus` param, BM25/pruning over extracted
  text, token-saving on long pages).
Tier 2: metadata + citations on every response (OpenGraph + JSON-LD + canonical +
  published-date + author + numbered link references); interact (smart_fetch `actions`
  click/type/wait/scroll/press on the warm stealthy session, tested vs real fixture
  sites e.g. quotes.toscrape.com/login, scrapethissite.com); shadow-DOM flattening +
  infinite-scroll expansion; HTML tables -> markdown tables.
Tier 3 (polish): hardened stealth (fingerprint rotation, header/TLS hygiene, undetected
  toggle); MCP progress notifications during crawl/research; conditional revalidation
  (ETag/304); cache-stats; token-tightening pass; media (image URLs) field.

Tool surface: 6 tools (smart_fetch, smart_crawl, smart_search, screenshot, cache_clear,
version). Target ~2K tokens tools/list (still far under Firecrawl 12 / Jina 19).
README rewritten at v5 release to highlight new features + "destroys all free
alternatives" (every claim verified vs research). Honest limits kept: DataDome/Akamai/
Cloudflare Turnstile-interactive at scale need paid residential proxies (NOT claimed
for $0); login/auth flows out of scope (safety).

Phasing: propose v5.0 (flagship Tier 1), v5.1 (power Tier 2), v5.2 (polish Tier 3).
Pending Dondai confirm. RapidOCR + MCP progress notifications confirmed available
(RapidOCR pure-pip 10.8M downloads; MCP progress spec 2025-11-25).

## v5 build (SHIPPED 5.0.0 / 5.0.1, 2026-06-22) — all 3 tiers done

**5.0.0** LIVE: https://pypi.org/project/hound-mcp/5.0.0/ — tag v5.0.0.
**5.0.1** LIVE: https://pypi.org/project/hound-mcp/5.0.1/ — tag v5.0.1. Patch:
  OCR was broken for real `pip install hound-mcp[all]` users. rapidocr v3 ships
  WITHOUT an inference backend; `RapidOCR()` raised `ImportError: onnxruntime
  is not installed`. The [all] extra declared `rapidocr>=3.0` but not onnxruntime,
  so a clean install got the core without the backend. Dev venv had onnxruntime
  installed manually, MASKING it (same class as the 4.0.0 pdfplumber miss). Fix:
  `[all]` += `onnxruntime>=1.16`. Regression test added asserting [all] declares
  every OCR/PDF dep (pdfplumber/pypdfium2/rapidocr/onnxruntime). Verified in a
  clean venv from PyPI: onnxruntime installs, RapidOCR() instantiates, OCR runs.
  CI green on 5.0.1 (5.0.0 CI was red on the OCR tests — the fix). **Lesson:
  rapidocr v3 needs onnxruntime installed separately; declare it. General lesson
  (already in memory from 4.0.1): after adding an optional dep, verify in a
  CLEAN venv that `pip install pkg[all]` actually delivers it AND that the dep
  can be INSTANTIATED (not just imported) — import-only checks miss backend
  deps loaded at __init__.**

Built staged per Dondai: each feature fully tested + verified, no push, tell him
when all 3 done. All work is local commits on master (not pushed). 528 tests
pass (was 442 at v4.0.3). Tool surface: 6 tools (smart_fetch, smart_crawl,
smart_search, screenshot, cache_clear, version). tools/list ~2.1K tokens for 6
tools (still lean vs Firecrawl 12 / Jina 19). Instructions ~640 tokens one-shot.

### Tier 1 (flagship) — DONE, live-verified
- **OCR** (`master_fetch/ocr.py`): scanned PDFs + image pages via rapidocr v3 +
  pypdfium2 (pure-pip, no system binary, py3.13-supported; rapidocr-onnxruntime v1
  is a runtime fallback). Auto-OCR fallback in _extract_pdf_response when a PDF is
  scanned; image/* pages OCR'd in _translate_response. Auto-cap: first 10 pages
  when no `pages` spec (avoids hangs on huge scanned PDFs). [all] extra +=
  pypdfium2>=4.30, rapidocr>=3.0. Live-verified: arxiv text PDF still text-extracts
  (no regression); placehold.co image-with-text OCRs correctly.
- **Query-focused fit markdown** (`master_fetch/focus.py`): smart_fetch `focus`
  param, BM25 block filter, post-cache (one cache entry serves any focus).
  Live-verified: Wikipedia 26501 -> 3432 chars (87% reduction).
- **smart_search filters + research mode** (`search.py`): site/exclude_sites
  (site:/-site: operators), location/language (geo), page (0-10) — all NATIVE
  TinyFish (honest: TinyFish has NO date/news params, so none claimed). Research
  mode (fetch_content=true) bulk-fetches top-N high-relevance results' content in
  one call (ResearchResponseModel). Cache key includes every filter. 18 mocked
  tests. **Live-verified against the real TinyFish API (key in keys.md): basic
  search (relevance tiers + fetch_hint), site: filter (all results on
  docs.python.org), exclude+geo, and research mode (searched + fetched top-2 in
  one call, both content_ok).**
- **smart_crawl** (`master_fetch/crawl.py` + mcp_smart_crawl tool): BFS same-domain
  deep crawl, discover_only (map mode), focus (query-prioritized), max_pages/
  max_depth/max_total_chars caps, path_include/exclude, concurrency, per-page
  agent hints. One fetch/page (html), links + markdown derived from same body.
  Live-verified on books.toscrape.com: 5 crawled / 74 discovered, truncation +
  next_action correct, same-domain enforced, focus crawl works.

### Tier 2 — DONE (shadow DOM deferred)
- **Metadata enrichment** (`master_fetch/metadata.py` + ResponseModel.metadata):
  OpenGraph + JSON-LD + canonical + <title> + published_time + author on every
  HTML fetch. _apply_chunking preserves it (cache hits return metadata={}).
  Live-verified on Wikipedia.
- **Interact / actions** (`master_fetch/actions.py` + smart_fetch `actions` param):
  click/fill/press/wait/scroll/wait_selector via scrapling's awaited `page_action`
  hook on the stealthy browser. Forces stealthy, bypasses cache, max 20 actions,
  per-action error isolation. Live-verified: clicking 'Next' on quotes.toscrape.com
  navigated to /page/2/.
- **HTML tables -> markdown**: already handled by trafilatura include_tables=True
  (verified on Wikipedia Nepal, no new code).
- **Shadow DOM flattening**: DEFERRED. scroll is covered by the `actions` scroll
  step; shadow-DOM piercing needs reaching into scrapling/Playwright internals
  (niche, fragile). 

### Tier 3 — DONE (media + token review); bigger items deferred
- **Media (image URLs)**: ResponseModel.media + smart_fetch include_media flag
  (opt-in, default false, up to 20 URLs). Live-verified on Wikipedia.
- **Token review**: 6 tools ~2.1K tokens (acceptable; trimming would hurt the
  agent-guidance descriptions). README headline updates 1.3K -> ~2K at release.
- **Conditional revalidation (ETag/304)**: DEFERRED. Needs a cache schema change +
  304 handling through scrapling (uncertain). Real value (instant re-fetches of
  unchanged pages) but medium-high effort; future release.
- **MCP progress notifications (crawl)**: DEFERRED. Needs the MCP request progress
  token wired into the tool call (protocol plumbing). Crawl works without it;
  summary/next_action guide the agent. Future release.
- **Hardened stealth (fingerprint rotation etc.)**: DEFERRED. Risky to change the
  working anti-bot path; hard to test improvement. 
- **cache-stats tool/view**: DEFERRED (low value, would add a tool).

### Release tasks (after Dondai OK)
- Get a TinyFish key from Dondai; live-verify search filters + research mode. **DONE (key in keys.md, live-verified).**
- Bump 4.0.3 -> 5.0.0 (pyproject + __init__). **DONE (5.0.0 shipped, then 5.0.1 patch).**
- Rewrite README: 6-tool table, feature deep-dives, "destroys all free alternatives" framing, ~2K token headline, honest limits. **DONE + 2 hand-authored SVGs (docs/flow.svg pipeline, docs/tokens.svg tool-count chart) rendered via absolute raw URLs.**
- Build, run full suite, push, GitHub release, PyPI upload, verify + CI. **DONE. CI green on 5.0.1.**

## v6 build (SHIPPED 6.0.0, 2026-06-23) — flagship crawl + PDF overhaul

Per the opencode agent bug report (S1-S12 crawl, P1-P14 PDF). Both features
rebuilt to be best-in-class among free/OSS alternatives. Driven by 2026
research: Crawl4AI BestFirstCrawlingStrategy + scorers/filters is the proven
crawl design; marker/docling/MinerU beat us on math only via heavy torch (2-4GB,
slow, hard on Windows/CI) — not appropriate for a lean MIT MCP tool. PyMuPDF4LLM
is AGPL (blocked). So hound's flagship angle = agent-usable output with HONEST
quality signals + a CID-OCR trick no lightweight tool does.

**SHIPPED: https://pypi.org/project/hound-mcp/6.0.0/ — tag v6.0.0. CI green. 549 tests pass.**

### smart_crawl flagship
- Best-first priority queue (heapq) scored by focus relevance + content-likelihood
  (boost docs/guide/api/reference; penalize login/submit/cart/admin) + shallow-depth.
  Content pages crawled before junk when budget tight (S5). focus reorders globally (S4).
- Content-adaptive per-page extraction (`_classify_and_extract`): article -> trafilatura;
  list/index (link-density >= 0.5) -> structured `* [title](url)` link list (S2: HN);
  js_shell -> honest empty report (S1/S8).
- `normalize_url`: trailing slash (non-root), default ports, lowercase host, tracking
  params (utm_*/fbclid/gclid/ref/_ga) stripped. Dedup by normalized, FETCH original
  (so /docs/ still fetches with slash). Fixes S3/S7.
- same_domain_only default (external dropped, S10); off-domain redirect guard.
- Two-phase: `crawl_urls=[...]` selective second-phase (no re-discovery, max_depth=0), S9.
- Network error -> status -1 (S11). fetched_at per page + cache_ttl:0 (S6). Default
  max_content_chars_per 4000->8000 (S12). Overall deadline_ms (default 120000) so
  one slow page can't hang the crawl.
- Live-verified: HN (list extraction, 8000 chars, was 0/10); opencode.ai/docs (0 dupes,
  0 external, focus prioritizes /docs/config).

### PDF flagship
- **CID-garbage auto-OCR fallback (P1, the killer feature)**: fonts without a
  ToUnicode CMap make pdfplumber emit `(cid:71)(cid:302)...`, but the glyphs RENDER
  correctly visually. So when a page's CID-garbage ratio >= 0.30, hound renders it
  via pypdfium2 + OCRs with rapidocr (batched: one ocr_pdf call for all CID pages),
  recovering the real text. Equations/figures OCR'd as visible symbols + honest marker
  (P2). Reuses OCR deps already shipped. Live-verified: ToT p2 (46% CID -> 0 cid
  remaining, real text recovered).
- `quality_score` (0-1 readable-char ratio) + honest `content_ok` (P3): scored from
  RAW page text + OCR status so honest markers don't inflate it. Garbled doc ->
  content_ok=false even on HTTP 200. `_agent_hints` respects the PDF verdict when
  quality_score > 0 (won't let status-200 mask corruption).
- `table_of_contents` from PDF outline via pypdfium2 `get_toc()` (P13). Live: ToT 14 entries.
- `metadata` populated (title/author/subject/keywords/creator/producer/dates) on
  ResponseModel (P7), not just the markdown header.
- `include_media` -> per-page embedded-image metadata for PDFs (P4).
- `.pdf` URLs never escalate to stealthy (P9: was 16s waste); .pdf URL returning HTML
  (login/paywall) -> auth_required/not_a_pdf instead of extracting the login page (P6/P14).
- `_apply_chunking` carries quality_score + table_of_contents + content_ok through.

### Deferred (future)
- P8 MCP progress notifications (protocol plumbing).
- P11 PDF page labels for `--- Page N ---` (minor).
- P10/P12 doc-only (extraction-ratio note, password behavior docs).
- Optional `[ai]` extra pulling marker for SOTA neural math/tables (Dondai's call;
  ~2-4GB torch, slow, hard to test on Windows/CI — not recommended as a hard dep).

## v7 build (IN PROGRESS, 2026-06-23) — local search flagship

Goal: remove TinyFish. Make hound 100% local + keyless + no-account. Build a
web search that exists for ONE job: feed smart_fetch. Hand-rolled hound-native
engine scrapers (no ddgs dep). TinyFish HARD-REMOVED. Spec in V7-SEARCH-PLAN.md.

Phases (each tested + live-verified, no push until all done + Dondai approves):
- [x] **Phase 1 — engine scrapers + keyword BM25 (DONE, live-verified).**
  - New module `src/master_fetch/search_engines.py`: hound-native keyless
    scrapers for DuckDuckGo (html.duckduckgo.com/html), Bing, Google,
    Wikipedia (official API). Transport = scrapling `FetcherSession` (browser-
    impersonated curl_cffi, a CORE dep, so LEAN installs get working search — no
    `requests` needed) + urllib fallback. Stealthy-escalation hook
    `_stealthy_html(server, url)` falls back to hound's warm Patchright browser
    when an engine blocks (the flagship anti-bot move; wired for Google + as a
    fallback for any blocked engine). Multi-engine `multi_search` runs engines
    in parallel (asyncio.gather, return_exceptions=True so one crash never kills
    the call), merges via `normalize_url` dedup, applies site/exclude filters,
    BM25-reranks over (title+snippet).
  - Rewritten `src/master_fetch/search.py`: TinyFish gone. New agent-facing
    fields `relevance_score` (BM25, 0-1), `engines_used`, `engine_blocked`,
    `rerank_mode`. `fetch_relevance` tiers derived from score+rank (top result
    never low). Research mode preserved (bulk-fetches the RERANKED top-N). Cache
    key bumped to `search:v2` (includes engines + freshness + region). 
    `compute_fetch_relevance` (old overlap heuristic) removed; replaced by `_tier`.
  - `server.py` wired: `smart_search` method + tool def + dispatch +
    HOUND_INSTRUCTIONS updated (api_key gone; engines/freshness/region added).
  - Tests: new `tests/test_search_engines.py` (24: DDG uddg decode, Bing
    <cite> breadcrumb reconstruction, Google /url?q= decode, Wikipedia JSON +
    region-suffix lang, merge/dedup/site filters, BM25 ranking + zero-overlap
    order-preserving tiebreak, multi_search parallel + crash isolation). Rewrote
    `tests/test_search_v5.py` (stub multi_search; relevance_score/tiers,
    filter+engine+freshness wiring, engine_blocked error surface, research mode,
    cache-key collision, cache-hit skip). Updated `tests/test_agent_qol.py`,
    `tests/test_cache_qol.py`, `tests/test_reliability_v3.py` (removed all
    _tinyfish_search / TinyFish-key tests; replaced with the local-search error
    contract). Full suite 573 passed (was 549).
  - LIVE-VERIFIED against the real web: DDG + Bing + Wikipedia all return clean
    real URLs, merged + BM25-ranked meaningfully (tokio-specific results high,
    generic Rust low), full server.smart_search path works, engines_used/
    engine_blocked/fetch_hint correct. Bing title spacing fixed (h2 has nested
    spans -> use get_text(" ", strip=True)).
- [x] **Phase 2 — neural rerank (DONE, live-verified).** New module
      `src/master_fetch/reranker.py`: ONNX `cross-encoder/ms-marco-MiniLM-L-6-v2`
      (Apache-2.0, 22.7M params, MS MARCO passage reranking) on the onnxruntime
      we ALREADY ship for OCR. Model + tokenizer downloaded ONCE on first neural
      search into `~/.master_fetch_cache/models/msmarco-minilm-l6-v2/` (pinned to
      HF rev `c5ee24cb16...`, sha256 sidecar, ~80MB, NOT bundled in the wheel).
      `tokenizers>=0.20` added to `[all]` (the only new optional dep; onnxruntime
      reused). `get_reranker()` is a warm singleton that NEVER raises — returns
      None if onnxruntime/tokenizers missing (lean install) or download/load
      fails, and the caller falls back to BM25. `smart_search` gained a `mode`
      param (auto|keyword|neural; deep/find_similar rejected until their phases).
      `mode=auto` uses neural if available else keyword; `mode=neural` falls back
      to keyword with a note in `fetch_hint` if unavailable. New helper `_rank`.
      Cache key includes `mode` (neural vs keyword don't collide). `rerank_mode`
      on the response reflects what ran. Tests: `tests/test_reranker.py` (11:
      mode validation, neural-when-available, graceful fallback + note, keyword-
      never-calls-neural, auto-detect, mode-aware cache key, reranker contract +
      deps-missing). `[all]` regression test extended to `tokenizers`. 584 pass.
      LIVE-VERIFIED: model downloaded + cached, ONNX session loads, `mode=neural`
      reorders results vs keyword end-to-end through `server.smart_search`.
      Note: ms-marco-MiniLM sigmoid saturates (~1.0) for clearly-relevant
      snippets, so fine discrimination among top snippet-results is weak —
      Phase 3 deep mode reranks on full page content (richer signal) where it
      discriminates better.
- [x] **Phase 3 — deep content-aware rerank: BUILT then CUT per Dondai review.**
      Built `mode=deep` (peek real page content + rerank on it + `peek` field), but
      Dondai correctly judged it over-built: for modern LLMs, snippet + neural
      rerank + smart_fetch is enough; deep's content-peek cost 6-11s + N fetches
      for marginal gain, and the model fetches the real pages anyway. CUT: removed
      `deep` from modes, removed `deep_rerank` from reranker.py, removed
      `peek_content`/`peek_many` from search_engines.py, removed the `peek` field,
      removed the research-mode auto-deep (research now uses neural). The real
      flagship is neural (local keyless semantic rerank reusing onnxruntime) +
      the keyless multi-engine scraping itself. Tests: -9 deep tests; 590 pass.
- [x] **Phase 4 — find_similar + autoretrieval + niche (DONE, live-verified).**
      - `mode=find_similar` (pass `url=`): fetches the source page
        (`fetch_source_for_similar` in search_engines.py: title + trafilatura body),
        derives a query from the title, runs the engines, then reranks candidates
        against the SOURCE page content with the cross-encoder (Exa find-similar,
        local). Response `query` = the source URL; `fetch_hint` notes the derived
        query. Falls back to keyword BM25 on the derived query (with a note) when
        the reranker is unavailable. Returns a clear error if no url / source
        unfetchable.
      - `expand=N` (1-5, default 1=off): autoretrieval. `_expand_query` generates
        N sub-query variants locally (intent suffixes + prefixes, NO external LLM),
        `_gather` runs all variants × engines in parallel, merges + dedups. Boosts
        recall for niche queries (the 'Exa for niche' angle). Ignored for
        find_similar.
      - `engines_used`/`engine_blocked` deduped (expand ran engines N times →
        dedup to a clean list).
      - New params on `smart_search`: `expand`, `url`. `_IMPLEMENTED_MODES` +=
        `find_similar`. Cache key += `expand` + `cache_query` (find_similar keyed
        on the source URL). Tests: +8 (validate_expand, _expand_query variants,
        expand runs subqueries + merges, expand=1 no-op, find_similar fetches+
        reranks vs source, find_similar requires url, source unfetchable, keyword
        fallback). 599 pass. LIVE-VERIFIED: find_similar on a Wikipedia page
        returned genuinely similar pages ranked vs its content (7.4s incl source
        fetch); expand=3 ran 3 sub-queries across all engines in parallel.
- [x] **Phase 5 — integration + docs + verify (DONE; NOT shipped — awaiting
      Dondai approval before publish).** Version bumped 6.0.0 -> 7.0.0
      (pyproject + __init__). CHANGELOG 7.0.0 entry. README rewritten: search is
      now keyless local (TinyFish removed from every section + install blocks +
      comparison table + the referral disclosure); new Local keyless search
      feature section (modes keyword/neural/deep/find_similar, expand, anti-bot
      engine scraping, honest engine-rate-limit posture); honest limits updated
      (search engine rate-limits, neural/deep need [all]); install comments fixed
      (lean install now HAS keyless keyword search). Clean-venv verify PASSED
      (the v4.0.0/v5.0.0 regression class): built the 7.0.0 wheel, fresh venv,
      `pip install hound-mcp[all]` delivered tokenizers-0.23.1 + onnxruntime-1.27.0
      + rapidocr-3.9.0 + pypdfium2-5.10.1 + pdfplumber-0.11.10; `RapidOCR()`
      instantiates; `get_reranker()` READY (model loads from the shared cache);
      reranker score sanity = 1.0 (asyncio doc) vs 0.0 (pizza) for "what is
      asyncio" (perfect discrimination); live keyword `smart_search` works in the
      clean venv (engines DDG+Bing+Wikipedia, no dev-venv leakage). 599 tests
      pass. **NOT pushed, NOT uploaded to PyPI, NO GitHub release — Dondai
      approves before shipping.**

### v7 status: SHIPPED 2026-06-24 (v7.0.0 on PyPI + GitHub, CI green)

Pushed (fast-forward, no rebase needed — Dondai had not touched remote README
since v6), tagged v7.0.0, GitHub release (wheel + sdist attached), PyPI upload.
Fresh-venv verify PASSED: `pip install hound-mcp[all]==7.0.0` -> ver 7.0.0, neural
search works (9 results, all 3 engines, no blocks), reranker ready, import points
at site-packages (clean, no dev-venv leakage). CI green on the README commit.
README rewritten from scratch (clean/organized/star-worthy, no TinyFish; TinyFish
lives only in CHANGELOG). Reddit post at C:/Users/Dondai/hound-reddit-post.md
still OUTDATED (says "search takes a free TinyFish key") — UPDATE before posting.

### v7 post-7.0.0 UNRELEASED fixes (accumulating, ship all at once — Dondai's call)

SHIPPED as v7.1.0 on 2026-06-24 (PyPI + GitHub release, CI green, fresh-venv
verify passed: 7.1.0 installs clean, default 9 results ddg+bing neural, reranker
ready, MCP dispatch gracefully drops removed fetch_content/expand params).

Local commits in this batch (now shipped in 7.1.0):
- **Cold-start timeout fix**: prewarm engine sessions + reranker at startup; hard
  per-engine deadline (asyncio.wait_for 8s, HOUND_SEARCH_DEADLINE env) so a
  slow/blocked/escalating engine can never hang the search. Root cause was cold
  sessions soft-blocked -> stealthy escalation stacking on a cold browser ->
  >30s -> MCP timeout. Verified: opencode's failing queries now 1.2-2.0s.
- **Agent QoL**: summary + next_action on every response; results capped at
  max_results (was returning up to 3x); partial-results note; pagination (page)
  fixed (was advertised but did nothing — now threads offset to each engine).
- **Cut research mode** (fetch_content/fetch_top) + **expand** (autoretrieval):
  bloat. smart_search now always returns SearchResponseModel (URLs + ranking,
  NOT page content; the agent fetches the ones it wants).
- **Default max_results 10 -> 9.**
- **Wikipedia dropped from default engines** (constant bottom-barrel garbage);
  default = DDG + Bing. Wikipedia + Google are opt-in (engines=).
- **Google visibility fix**: an engine returning 0 results was in neither
  engines_used nor engine_blocked (silently vanished). engine_blocked now = any
  engine that did NOT contribute, so google surfaces there. + google consent-page
  detection + more robust parser (best-effort; google is opt-in + honest).
- next_action says "fetch the top 1-2" not "fetch the N high" (neural saturates so
  all 9 can be high; agent should fetch top 1-2).
- Removed dead _urllib_get; find_similar source fetch bounded to 6s.
- 616 tests pass. Live-verified.

#### Search Engine Resilience Layer (SERL) — built 2026-06-24
Dondai challenged that local keyless scraping exposes the user IP to rate-limits
/ blocks. Designed + built a stateful per-engine coordinator in
`search_engines.py` (`_EngineCoordinator` + module singleton `_ENGINES_COORD`):
(1) persistent warm session per engine (cookies+TLS reuse, also faster); (2)
per-engine pacer w/ jitter (parallel within a search, paces same-engine bursts);
(3) circuit breaker w/ exponential cooldown 15->120s (blocked engine skipped,
others carry); (4) 202 soft-limit + 429/503/403 + Retry-After aware (DDG 202 was
a real missed-case bug, now fixed); (5) fingerprint rotation via scrapling
`impersonate` list; (6) adaptive Google reserve tier (fires via stealthy only
when primaries <3 results + one blocked + server present); (7) `HOUND_SEARCH_PROXY`
env escape hatch for power users w/ a rotating proxy. New `_engine_get` seam
(replaces `_impersonated_get` for SERPs). `close_search_engines()` wired into
`server._shutdown_close_sessions`. 14 new SERL tests; 604 pass. Live-verified:
12-15 results from all 3 engines, warm sessions created+reused, no blocks on a
clean IP, cooldown idle. Honest line: not bulletproof without a proxy, but
dramatically more robust for single-user real agent work. tools/list ~2.6K tokens.

### v7.2 batch (UNRELEASED, built 2026-06-24) — diverse independent pool + cross-engine consensus

SHIPPED as v7.2.0 on 2026-06-24 (PyPI + GitHub release, CI green, fresh-venv
verify passed: 7.2.0 installs clean, 6 results from ddg+bing+brave, neural
rerank, top result is a consensus hit (consensus=2 of 3, src=brave,duckduckgo),
keyword rejected, reranker ready).

Dondai's ask: Google is hopeless (remove it); DDG+Bing are ~99% the same quality
but still rate-limit; default 9 -> 6 (5 usually enough); solve rate-limiting once
and for all WITHOUT trading away speed (no just-add-delays cheating); find other
free engines. Research found: Brave Search API free tier KILLED Feb 2026 (now
metered billing + card + attribution) BUT the Brave WEB UI is keyless + an
independent 30B-page index; Mojeek is an independent index too. Live-tested both:

- **Brave (web) ADDED as a default engine.** Independent index (breaks the
  DDG≈Bing 99% overlap). BUT curl_cffi/scrapling returns curl error 23 on
  search.brave.com (every fingerprint) while stdlib urllib returns 200 + 20
  parseable results (data-type="web" snippets, direct URLs). So Brave rides a
  URLLIB transport inside the SERL coordinator (`_URLLIB_ENGINES`, `_urllib_fetch`,
  `asyncio.to_thread`) — still gets the pacer + circuit breaker. urllib is stdlib,
  so lean installs get Brave too. Selectors: `div.snippet[data-type="web"]` ->
  `a[href]` (direct), `.snippet-title`/`.title`, `.generic-snippet`. Freshness
  `&tf=`, pagination `&offset=`, `&safesearch=off`.
- **Google REMOVED entirely** (engine, reserve tier, advertised option). It
  CAPTCHAs even via the stealthy browser. `engines=['google']` now rejected with
  the valid-set error.
- **Mojeek DROPPED** (was tried as a default then opt-in). 403s ALL HTTP
  (scrapling + plain requests + Mozilla UA) AND 403s the stealthy Patchright
  browser (`<title>403 - Forbidden</title>`) — IP-blocked in this environment.
  Only a real browser on a clean IP would work (the searxng-cli ref used pydoll
  Chrome at 4 req/min). Unreliable + per-search browser render = speed cost ->
  cut entirely rather than ship a broken opt-in.
- **Yahoo ADDED as opt-in** (`engines=['yahoo']`): serves Bing's index from
  Yahoo's servers (different IP/rate bucket) = redundancy for Bing's index when
  bing.com rate-limits. RU= redirect decoding. Not a default (same index as bing).
- **DEFAULT_ENGINES = (duckduckgo, bing, brave)** — 3 independent index families,
  all HTTP (no browser needed for the default pool -> speed preserved).
- **Default max_results 9 -> 6.**
- **Cross-engine consensus ranking (the unique strong feature, zero speed cost):**
  merge_dedupe now tracks which engines returned each URL + stamps
  `RawResult.consensus` = number of DISTINCT index-families (`_INDEX_FAMILY`:
  yahoo counts as the bing family). `_apply_consensus_boost` in search.py:
  `score *= (1 + 0.25*(consensus-1))` (consensus AMPLIFIES relevance, doesn't
  override; a consensus-but-irrelevant result still ranks low). Also breaks the
  neural-saturation tie (ms-marco gives ~1.0 for any clearly-relevant snippet;
  consensus is a discrete 1..N discriminator). New agent-facing field
  `engines_consensus` ("N of M independent indexes") + `source` now shows all
  agreeing engines (e.g. "brave,duckduckgo"). Re-normalizes to 0..1 only when a
  boost pushes a score >1.0 (preserves raw neural scores otherwise).
- **Rate-limiting solved without speed cost:** 3 independent engines run in
  PARALLEL (wall-clock = max(slowest, 8s deadline) ≈ same as 2 engines). When one
  rate-limits, the circuit breaker rests it (15-120s) + the other 2 INDEPENDENT
  indexes carry genuinely different results (not the 99%-overlap useless
  redundancy of ddg+bing-only). Live-verified: 5 rapid searches, Brave blocked on
  query 4 -> ddg+bing carried, 6 results every time, agent never saw a failure.
- Cache key bumped `search:v3` -> `search:v4` (consensus field). 617 tests pass.
- Live-verified: rust/pizza/asyncio/reverse-list queries all return 6 results
  from ddg+bing+brave with consensus hits surfacing authoritative URLs (tokio.rs,
  github.com/tokio-rs/tokio) to rank #1-2.

### v7.2 add-on (built 2026-06-24) — keyword/BM25 removed; neural-only + optimized

Dondai: keyword (BM25) is the same speed or slower than neural, so remove it
entirely (simpler); neural is always the top reranker; optimize neural for better
accuracy; test the rate-limiting hard.

- **BM25 / `keyword` mode REMOVED entirely.** Deleted `bm25_rerank` + `_tokenize`
  from search_engines.py; `multi_search` now returns the merge order (consensus +
  engine-position, already sorted by `merge_dedupe`) instead of BM25-ranking.
  `_IMPLEMENTED_MODES = ("auto", "neural", "find_similar")`. `mode='keyword'` is
  now REJECTED ("Invalid mode"). Lean installs (no onnxruntime/model) fall back to
  cross-engine consensus + engine-position order (no lexical rerank) — rerank_mode
  "merge"; `mode='neural'` surfaces a note when unavailable, `auto` is silent.
- **Neural optimization — min-max normalize per query** (`reranker.rerank`):
  ms-marco sigmoid saturates (~1.0 for any clearly-relevant snippet) so raw scores
  cluster + can't discriminate. Min-max normalize across the result set to 0..1
  (top=1.0, worst=0.0) restores meaningful `relevance_score` spread. Ranking ORDER
  unchanged (monotonic); only the score field + tier derivation get real signal.
- **Consensus boost switched multiplicative -> ADDITIVE** (`_apply_consensus_boost`):
  `score + 0.2*(consensus-1)` (each agreeing index-family +0.2). Additive reliably
  surfaces consensus authority WITHOUT overriding strong neural relevance (a
  strongly-relevant single-engine result still beats a weak consensus hit), and
  composes cleanly with normalized neural scores. Renormalize to 0..1 only when
  the additive bonus pushes a score >1.0.
- 614 tests pass (was 617; -3 removed BM25 tests, +keyword-rejected test).
- **Rate-limit failover + recovery LIVE-PROVEN (the hard test Dondai asked for):**
  15 successive distinct searches → Brave blocked on #11 (cooldown 13s) → DDG+Bing
  carried, 6 results every time, agent never saw a failure. Then waited out the
  cooldown → Brave REJOINED on the next search (`brave back: True`). Full
  block -> cooldown -> recovery cycle works without issues. Score spread live:
  'jaguar' (ambiguous) 0.7-1.0, 'rust tokio runtime' (clear) 0.833-1.0 (narrow is
  honest - all results relevant); consensus surfaces tokio.rs + github to #1-2.

### v7.3 (SHIPPED 2026-06-24 as v7.3.0) — speed + smart rate-limit avoidance + Qwant

Dondai: Brave rate-limits too fast; per-engine stealthy escalation adds a
0.6-1s+ cut on block; want lower rate-limiting, faster search, less garbage,
smart (not brute-force) rate-limit bypass. Researched engine landscape: Qwant
(keyless JSON API, own index, tolerant), Yandex (aggressive captcha - dead),
Ecosia (own index now - future option).

- **Qwant REPLACES Brave** as the third default. Keyless JSON API
  (`api.qwant.com/v3/search/web`), scrapling with safari184 PINNED (chrome/edge
  get 403-captcha, safari passes) via `_ENGINE_IMPERSONATE`. MUCH more tolerant:
  25 rapid searches, 25/25, 0 blocks (Brave blocked ~10). Clean JSON parsing.
  GOTCHA: Qwant API requires `count=10` exactly (any other value -> 400 "count
  must be equal to 10") - fetch 10, slice to max_results in the parser.
- **Brave DROPPED entirely** + the `_URLLIB_ENGINES`/`_urllib_fetch` urllib
  transport removed. `DEFAULT_ENGINES = (duckduckgo, bing, qwant)`.
- **Per-engine stealthy escalation REMOVED** (was the 0.6-1s+ cut: a blocked
  engine triggered a 2-5s browser render). Now a blocked engine 403s in <1s +
  others carry; stealthy is a search-wide LAST RESORT only (one stealthy DDG fetch
  when ALL engines blocked + 0 results, rare).
- **Hard deadline 8s -> 5s.** **Quality filter**: drop low-relevance results
  (fetch_relevance=='low') when >=3 good remain (less garbage; niche queries
  return fewer good results, not 6 padded).
- ROBUSTNESS LIVE-PROVEN: 25 rapid searches all-3-engines 25/25 0-blocks
  all-got-results max 3.8s; simulated Qwant block -> ddg+bing carried (6 res, no
  failure) -> Qwant rejoined after cooldown. 616 tests pass. Cache key v4 (unchanged).

## Dev notes / API quirks

- **v7.2/v7.3 engine transport quirks (search_engines.py):**
  - **Qwant** (api.qwant.com/v3/search/web) is a keyless JSON API (own index +
    Bing feed). curl_cffi passes its bot check (captcha-delivery) ONLY with the
    `safari184` fingerprint (chrome/edge -> 403-captcha); pinned via
    `_ENGINE_IMPERSONATE = {"qwant": ["safari184"]}`. The coordinator's
    `_make_session(name)` uses the per-engine pin. JSON parse:
    `data.result.items.mainline[]` -> items with title/url/desc (skip type!=web
    + ads). **REQUIRES count=10 exactly** (else 400 "count must be equal to 10").
    locale: region 'us-en' -> 'en_US' (lang_COUNTRY). Much more rate-limit-
    tolerant than Brave (25/25 rapid, 0 blocks) but burst-sensitive: rapid
    hammering can temporarily flag the IP (lifts after a pause); the circuit
    breaker handles it (cooldown + retry + failover to ddg+bing).
  - **Brave REMOVED** (v7.3): rate-limited too fast (urllib, no TLS
    impersonation). The urllib transport (`_URLLIB_ENGINES`/`_urllib_fetch`) is
    gone. (v7.2 note: Brave failed under curl_cffi with curl error 23 on every
    fingerprint while urllib worked - but urllib rate-limited fast, so dropped.)
  - **Brave Search API free tier was KILLED Feb 2026** (metered billing + card +
    attribution); Bing Search API killed by Microsoft summer 2025. Yandex is
    aggressively captcha'd (dead). Ecosia now has its own index (Staan) - future
    option. Mojeek 403s all HTTP + the stealthy browser (dead).

- **Dev venv ships a BUILT WHEEL, not an editable install**: master-fetch's dev
  venv (`.venv/`) contains a BUILT WHEEL in `site-packages/`, NOT an editable
  install. `pytest` therefore runs the STALE installed copy, not `src/`. Before
  testing any `src/` edit: `.venv/Scripts/pip install -e . --no-deps`. Verify the
  import points at src/, not site-packages:
  `python -c "import master_fetch; print(master_fetch.__file__)"` → must show
  `src/master_fetch/...`. If it shows `.venv/Lib/site-packages/...`, the editable
  install didn't take and you're testing stale code.
- **old.reddit.com thing-block class has a LEADING SPACE**: real HTML is
  `class=" thing id-t3_..."`, never `class="thing...`. Any regex matching post
  blocks must allow `class="[^"]*\bthing\b`. Verified Jun 2026 on r/Python,
  u/spez, post pages.
- **OCR test fixture must be detector-friendly, not just human-readable**: `tests/dummy.pdf` was one tiny line of text ("Dummy PDF file") on a blank A4 page. Visually trivial to OCR, but RapidOCR's text DETECTOR (region-proposal) did not fire on a text box occupying <2% of the canvas, so on CI's ubuntu/3.13 runner OCR returned "[No text detected on this page.]" while tests asserted the exact string. Local OCR (Windows) caught it; CI did not. Flaky across OS/version. Fix: regenerated dummy.pdf as a realistic scan (48pt bold multi-line text filling the page) so the detector reliably fires everywhere. Still single-page + no-text-layer (pdf_extractor still classifies scanned_pdf). **General lesson: never assert exact OCR/probabilistic-engine output in CI. Assert the path ran (header marker) + a meaningful guard (no "No text detected" marker) instead of an exact recognized string.** Hit on the README-only commit (CI red on a doc change = the giveaway that it was flaky, not a real regression).
- **CID font corruption + the OCR-recovery trick**: PDFs with embedded font subsets lacking a ToUnicode CMap make pdfplumber/pdfminer emit `(cid:71)(cid:302)...` garbage for those fonts (architecture diagrams, figures, math in academic papers). But the glyphs RENDER correctly visually — only the text-to-unicode map is broken. So rendering the page (pypdfium2) + OCR (rapidocr) recovers the REAL text. This is hound's PDF flagship: detect CID-garbage ratio >= 0.30 per page -> batch-OCR those pages -> replace garbage with recovered text + an honest marker. Verified on Tree of Thoughts p2 (46% CID -> 0 remaining). If OCR extras absent, keep garbage + low quality_score + content_ok=false + marker telling the agent to install [all] or use vision.
- **pypdfium2 ToC/bookmark API**: `pdf.get_toc()` returns a GENERATOR (materialize with list()). Each `PdfBookmark` has `.level` (0-based int) + `.get_title()` (str) + `.get_dest()` -> `PdfDest` with `.get_index()` (0-based page index; page = idx+1). get_dest() may return None for URI-action bookmarks — wrap in try/except. CLIP/GPT-3: GPT-3 has 32 entries, CLIP has 0 (no outline). Most arxiv papers lack a ToC; books/reports have one.
- **PDF quality_score must exclude honest markers**: computing quality_score from the FINAL assembled markdown (which includes clean-English scaffolding like "[Page N: 64% CID garbage...]") inflates the score and masks corruption. Compute it from RAW rendered page text + OCR status (OCR-recovered pages = 1.0, unrecovered CID pages = _quality_score(raw)) BEFORE adding markers.
- **v7 local search — engine scraping quirks (search_engines.py):**
  - **Transport**: use scrapling `FetcherSession` (`async with FetcherSession() as sess: await sess.get(url, timeout=...)`) for browser-impersonated HTTP (curl_cffi TLS). It is a CORE dep (already used by robots.py), so lean installs get working keyless search with NO `requests` dependency. `sess.get` is a coroutine returning a Response directly (NOT a tuple) — the v3.6.1 robots fix documents the old `asyncio.to_thread(coroutine)` bug; do not re-wrap. Response: `.body` (bytes), `.status`, `.encoding`. urllib fallback (`_urllib_get`) only if scrapling's static engine is unavailable.
  - **DuckDuckGo** html endpoint `https://html.duckduckgo.com/html/?q=...&kl=us-en[&df=d/w/m/y]`: result blocks `.result` with `.result__a` (title, href is a redirect `//duckduckgo.com/l/?uddg=ENCODED&rut=...`), `.result__snippet` (an `<a>`). Decode the real URL from the `uddg=` query param (`_ddg_real_url`). DF freshness = `&df={d|w|m|y}`.
  - **Bing** `https://www.bing.com/search?q=...&count=N&setlang=en[&filters=ex1:"ez5_{d|w|y}1"]`: the `h2 a` href is an OPAQUE `bing.com/ck/a?!&&p=<token>` redirect with NO recoverable real URL (no `u=` param). The real URL is in the `<cite>` display element (e.g. `https://www.programiz.com › python-programming › online-compiler`). `_bing_real_url` replaces the `›` (U+203A) breadcrumb separators with `/` and ensures a scheme. Results with no `<cite>` are DROPPED (a junk redirect URL is worse than no URL). Bing `<h2>` titles have nested spans -> use `get_text(" ", strip=True)` or words glue together ("RustProgramming Language").
  - **Google** `https://www.google.com/search?q=...&hl=en&num=N[&tbs=qdr:d|w|m|y]`: almost always CAPTCHAs plain impersonated HTTP under load -> `_stealthy_html` escalation (warm Patchright browser) is the primary path for Google. Parse `div.g` / `div[data-ved]` -> `h3` + parent `<a>`; href may be `/url?q=REAL&sa=U&ved=...` (decode `q=`) or direct. Dedup within-engine by URL.
  - **Wikipedia** official keyless API `https://{lang}.wikipedia.org/w/api.php?action=query&list=search&srsearch=...&srlimit=N&srprop=snippet&format=json&utf8=1`. `{lang}` = the LAST segment of the region (`us-en` -> `en`), NOT the country prefix (`us.wikipedia.org` does not exist — real bug hit in live testing). Snippet comes as HTML -> strip with BeautifulSoup `get_text(" ", strip=True)`. URL = `https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ','_'))}`.
  - **BM25 rerank** (`bm25_rerank`): k1=1.5, b=0.75 over (title+snippet). Score normalized to 0..1 by MAX so the top result is always 1.0. CRITICAL tiebreak: when there is zero query-term overlap (purely semantic query), all scores are 0 -> must NOT randomly shuffle the merged dict order; sort key is `(-score, engine_position, original_order)` so zero-overlap preserves a stable, sensible order. `relevance_score` is recomputed in `smart_search` via `bm25_rerank(query, ranked)` (idempotent) because `multi_search` returns ranked RawResults without the scores attached.
  - **`_tier`** (fetch_relevance): high if score>=0.5 OR rank==1 (top result never low); med if score>=0.15 OR rank<=total/3; else low.
  - **Search cache key** is `search:v2:{max_results}:{site}:{exclude_sites}:{location}:{language}:{page}:{engines}:{freshness}` (was v1 with TinyFish). Bumped to v2 so old TinyFish cache entries don't deserialize into the new schema.
  - **Stealthy SERP escalation** (`_stealthy_html`): calls `server.stealthy_fetch(url, extraction_type="html", main_content_only=False, use_trafilatura=False, google_search=False, disable_resources=True)`. If the html extraction strips SERP structure, fall back to reading page.body via a dedicated helper (TODO if live testing shows the SERP result blocks are missing from the extracted html). Only fires when `server` is passed (production); unit tests pass `server=None` so no browser spawns.
- **edit tool + apostrophes (reaffirmed v7):** replacing long blocks containing straight apostrophes (e.g. the 7 TinyFish error tests in test_reliability_v3.py with "API key invalid"/"lacks permission") fails the edit tool's exact match. Workaround that worked here: splice the file programmatically with a small Python script (read lines, replace the line range, write back) instead of the edit tool.
- **old.reddit.com listing vs user-profile thing blocks differ**: subreddit
  listing thing blocks carry `data-score`/`data-comments-count`/`data-url`/
  `data-domain`. User-profile thing blocks (comments) often lack them (comments
  use `score-hidden`); the parser falls back to per-block `score unvoted` span
  + `full comments (N)` link, else honest `?`.

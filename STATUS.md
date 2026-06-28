# master-fetch (hound-mcp) — Status

Workspace status doc for the hound-mcp OSS tool (web fetch+search MCP). This file
holds (1) the current-shipped-version detail and (2) the dev-notes / API-quirks
section that must survive context compaction. Earlier shipped versions are
summarized one-line each below; their full build logs live in the repo history
and CHANGELOG.md.

Project: github.com/dondai1234/master-fetch, PyPI package `hound-mcp`, MIT.
Tool surface: 6 tools (smart_fetch, smart_crawl, smart_search, screenshot,
cache_clear, version). Single warm Patchright browser ("Google Chrome for
Testing", real_chrome=False) for smart_fetch/screenshot only, eager at startup.

---

## Current shipped version: 8.0.0 (2026-06-28)

Major upgrade focused on agent efficiency: collapse real multi-step agent loops
into fewer calls, and surface what a page/site actually contains so the agent
fetches less + navigates better. Four NEW capabilities (none are re-labels of
existing behavior), a tool-def overhaul, and a robustness fix.

- **Sitemap mode for smart_crawl** (`options sitemap=true|'auto'|false`): map a
  whole site from sitemap.xml in ONE fetch (full URL list + lastmod) instead of
  blind BFS. `auto` uses sitemap if present else BFS; `true` = sitemap-only.
  Discovery: robots.txt `Sitemap:` first, then /sitemap.xml +
  /sitemap_index.xml; recurses <sitemapindex> (capped); gzip-tolerant;
  namespace-agnostic; same-domain + path filters. New module `sitemap.py`.
  CrawlResponseModel += `sitemap_used` + `sitemaps`; CrawlPage += `lastmod`.
  Live-proven: docs.python.org mapped in one fetch.
- **Outgoing-links field on smart_fetch** (`options include_links=true`):
  `response.links = {citations, navigation, external, primary_source}`.
  citations = main-content-area links (the page's referenced sources);
  navigation = site chrome; external = off-domain; primary_source = best-effort
  hint (canonical/JSON-LD on a different host, else an in-content off-domain
  reference on a known primary host: arxiv/doi/github/wikimedia/etc). New module
  `links.py`. Live-proven on Wikipedia (30 cit / 20 nav / 20 ext, github
  primary_source).
- **Related-queries on smart_search**: `related_queries` mined EXTRACTIVELY
  from result titles+snippets (no LLM, no per-engine SERP-markup dependency).
  Bigram doc-frequency ranking, query-overlap filtered, unigram fallback.
  Engine-agnostic + robust to SERP markup changes.
- **PDF section-map with page ranges**: `table_of_contents` entries now carry
  `end_page` (from the outline structure) so the agent passes `pages='23-31'`
  for one section. For PDFs WITHOUT bookmarks (most arxiv papers), a heading-
  based section-map is built from the font-size heading detection already run
  during render (`_render_page` collects headings with page_num; `_heading_
  outline` builds the fallback ToC clamped to the extracted page range). Every
  navigable PDF now gets a section-map. Live-proven on a bookmarkless arxiv PDF
  (27 entries with page ranges).
- **Tool-def overhaul**: all 6 `_TOOL_DEFS` rewritten tight + structured (the
  agent-facing surface; the MCP client sees these, NOT the method docstrings).
  `HOUND_INSTRUCTIONS` rewritten: killed the stale `open_session` pro tip
  (removed in v4), tightened, documented new features. `actions` (infinite
  scroll / load-more / forms) now obvious in the def with examples. Stripped
  the redundant `:param:` block from the `smart_fetch` method docstring.
- **Robustness fix (metasearch status)**: a backend returning valid results
  that all got deduped by an earlier-finishing backend was marked `empty`
  (misleading; it DID contribute = confirmed consensus). Now `ok` if it
  returned any valid result (new OR dupe), `empty` only if nothing usable. Also
  makes the dedup test deterministic (was racy on completion order).
- **Chunking fix**: `_apply_chunking` rebuilt the ResponseModel on truncation
  and dropped `links` (would drop any new contextvar-driven field). Now copies
  `links` through (same as media/metadata/toc).
- 6 tools unchanged in count, all sharpened, none cut. 603 tests (585 + 18 new
  v8 feature tests in test_v8_features.py).

---

## Version history (summarized; full detail in CHANGELOG.md + git history)

- **4.0.0 / 4.0.1 / 4.0.2 / 4.0.3 (2026-06-20)** — PDF extraction flagship
  (pdfplumber MIT, multi-column, tables→markdown, font-size headings, de-hyphen,
  pages/password via contextvars, scanned/encrypted honest errors; detected by
  content-type OR `%PDF` magic bytes); caching hardening (bad content no longer
  cached, MAX_CACHE_ENTRIES=10000 oldest-eviction, search key includes
  max_results); single warm browser instance (removed open/close/list_session
  MCP tools 8→5, eager prewarm at startup, _auto_session_lock kills the 2nd-
  browser race, graceful shutdown); agent QoL batch (MCP instructions in
  initialize handshake, summary/content_ok/next_action/fetched_at, max_content_chars,
  promoted css_selector+timeout, screenshot auto-session, fetch_relevance tiers +
  fetch_hint); reddit stealth-default routing + old.reddit data-attr parser
  rewrite. 4.0.1 = pdfplumber declared in [all] (was missing); 4.0.2 = updater
  newline overlap; 4.0.3 = README + brand visuals (hero/logo/scene + flow.svg +
  tokens.svg) + `readme="README.md"` field added (every release through 4.0.2
  shipped a wheel with NO long description — silent). 442 tests.
- **5.0.0 / 5.0.1 (2026-06-22)** — all 3 v5 tiers. Tier1 flagship: OCR
  (rapidocr v3 + onnxruntime + pypdfium2, scanned PDFs + images, auto-cap 10
  pages), query-focused `focus` markdown (BM25 block filter, 87% reduction live),
  smart_search filters (site/exclude/location/language/page) + research mode
  (one-call search + bulk-fetch top-N), smart_crawl BFS deep/discover/focus
  with caps + per-page hints. Tier2: metadata enrichment (OG + JSON-LD +
  canonical + dates), interact/actions (click/fill/press/wait/scroll),
  HTML tables via trafilatura. Tier3: media URLs, token review (~2.1K for 6
  tools). 5.0.1 = onnxruntime declared in [all] (rapidocr v3 ships without a
  backend; same class as the 4.0.0 pdfplumber miss — dev venv masked it).
  **Search was TinyFish-keyed at this point (removed in v7).** 528 tests.
- **6.0.0 (2026-06-23)** — crawl flagship (best-first priority queue scored by
  focus + content-likelihood + shallow-depth; content-adaptive _classify_and_
  extract (article→trafilatura, list→link index, js_shell→honest empty);
  normalize_url dedup; same-domain default; two-phase crawl_urls; per-page
  status/hints; overall deadline) + PDF flagship (CID-garbage auto-OCR fallback
  — fonts without ToUnicode CMap render fine, so render+OCR recovers text;
  quality_score; table_of_contents via pypdfium2 get_toc(); metadata on
  ResponseModel; .pdf never escalates to stealthy; .pdf-returning-HTML =
  auth_required). 549 tests.
- **7.0.0 (2026-06-24)** — local keyless search flagship. TinyFish HARD-REMOVED.
  Hand-rolled hound-native engine scrapers (DDG html, Bing, Google, Wikipedia
  API) via scrapling FetcherSession + BM25 rerank; neural rerank
  (cross-encoder/ms-marco-MiniLM-L-6-v2 on the onnxruntime already shipped for
  OCR, model cached ~80MB, tokenizers added to [all]); `mode` param
  (auto/keyword/neural); find_similar (fetch source, rerank candidates vs source
  content) + expand (autoretrieval, N local sub-query variants). 599 tests.
- **7.1.0 (2026-06-24)** — cold-start timeout fix (prewarm engines + reranker at
  startup; 8s HOUND_SEARCH_DEADLINE so a slow engine can't hang); agent QoL
  (summary + next_action on every response, results capped at max_results,
  pagination page actually threads offset); CUT research mode + expand as bloat
  (smart_search returns URLs, agent fetches the ones it wants); default 9
  results; Wikipedia dropped from defaults (garbage); Google visibility fix
  (engine_blocked = any engine that did not contribute); SERL resilience layer
  (per-engine warm session, pacer + jitter, circuit breaker 15→120s, 202/429/
  503/403 + Retry-After aware, impersonate rotation, adaptive Google reserve
  tier via stealthy, HOUND_SEARCH_PROXY). 616 tests.
- **7.2.0 (2026-06-24)** — diverse independent pool. Brave (web UI, urllib
  transport — curl_cffi error 23 on brave) added; Google removed entirely
  (CAPTCHAs even via stealthy); Mojeek dropped (403s all HTTP + stealthy);
  Yahoo opt-in (Bing-index from different server). Defaults = DDG+Bing+Brave
  (3 independent index families). Cross-engine consensus ranking (consensus =
  distinct _INDEX_FAMILY; multiplicative boost). BM25/keyword removed entirely
  (neural-only); neural min-max normalized per query; consensus boost switched
  additive (score + 0.2*(consensus-1)). Default 6 results. 614 tests.
- **7.3.0 (2026-06-24)** — Qwant replaces Brave (keyless JSON API
  api.qwant.com/v3/search/web, safari184 PINNED — chrome/edge get 403-captcha;
  count=10 exactly or 400); per-engine stealthy escalation removed (was the
  0.6-1s cut); 5s deadline; quality filter (drop low-relevance when >=3 good
  remain). Defaults = DDG+Bing+Qwant. 616 tests.
- **7.3.1 (SUPERSEDED — was a mistake)** — made smart_fetch's stealthy browser
  LAZY at startup. Dondai only reported a SEARCH problem; I touched smart_fetch's
  browser without authorization. Lesson recorded in USER.md (scope discipline).
  Reverted in 7.4.
- **7.4.0 (SUPERSEDED by 7.5)** — shared-browser search backbone (DDG SERP
  rendered in the warm Patchright browser via _browser_html, Bing/Qwant HTTP) +
  parallel race (asyncio.wait FIRST_COMPLETED, return at max_results, cancel
  laggards, EngineReport.preempted). Dondai judged it garbage: sometimes only
  Bing (the parallel race returned whatever finished first = garbage), engines
  not contributing, constant rate-limits. Replaced by 7.5.
- **7.5.0 (2026-06-25)** — vendored ddgs metasearch, 9 backends in parallel, diversity quorum, 100% HTTP search (no browser). 585 tests.
- **8.0.0 (2026-06-28)** — CURRENT. Sitemap-mode crawl, outgoing-links field,
  related-queries mining, PDF section-map with page ranges, tool-def overhaul,
  metasearch status robustness fix. 603 tests.

### Key decisions / cut scope (still valid, do not re-propose)
- Residential proxy rotation: cut (can't test without proxies, risky blind).
- LLM extraction via user endpoint (HOUND_LLM_*): cut ("solving nothing" —
  hound gets content, the agent's own model extracts).
- CSS-schema extraction as a feature/tool: cut (same reasoning).
- Captcha-solving hook (CapSolver key): cut (untestable without a key).
- Diff/monitor tool: cut (niche, not "used most times").
- Deep content-aware rerank (mode=deep): built then cut per Dondai — for modern
  LLMs snippet + neural + smart_fetch is enough; deep's 6-11s content-peek cost
  wasn't worth it, and the model fetches the real pages anyway.
- No free keyless general web search API without rate-limits exists in 2026:
  Bing Search API killed summer 2025, Brave Search API free tier killed Feb
  2026. Only non-profit APIs (Wikipedia, Internet Archive, OpenStreetMap,
  OpenAlex) are truly free. General web search = scraping (rate-limited per-IP)
  or paid API. That's why hound vendors a multi-backend metasearch.
- Parallel-race early-return sacrifices quality (returns whatever finishes
  first, often the weakest engine) — the v7.5 diversity quorum (wait for >=3
  backends) is the deliberate correction.

---

## Dev notes / API quirks

Project-specific quirks that must survive context compaction. NOT cross-project
(those go in MEMORY.md). Keep only CURRENT quirks — superseded engine layers
(v7.0-7.4 hand-rolled, SERL coordinator, browser-rendered DDG) are deleted since
the code no longer exists.

- **v8 _apply_chunking field-copy gotcha**: `_apply_chunking` REBUILDS the
  ResponseModel on truncation (two construction sites: the offset-past-end
  branch + the main chunk branch). It must copy EVERY contextvar-driven field
  (metadata, media, links, quality_score, table_of_contents, content_ok) or they
  silently vanish on truncated responses. When adding a new contextvar-driven
  response field, add it to BOTH constructors here or it'll work on short pages
  and disappear on long ones. The `links` field hit this in v8.

- **v8 contextvar propagation through the fetch path is fine**: `_INCLUDE_LINKS`/
  `_INCLUDE_MEDIA` set in `smart_fetch` (the method) are visible inside
  `_translate_response` even though the http tier runs the fetch via
  `bulk_get`'s `gather(*timed_tasks)` — because `_translate_response` is called
  AFTER `await gather(...)` in the main task, not inside the gathered tasks. So
  no need to pass flags down the call chain; contextvars work. (If a future
  refactor moves `_translate_response` INSIDE a gathered task, it still works —
  gather copies the current context at task-creation time.)

- **v8 metasearch status: 'ok' vs 'empty'**: a backend that returned valid
  results which all got deduped by an earlier-finishing backend must be 'ok'
  (it contributed = confirmed consensus), NOT 'empty'. The `touched` flag tracks
  dupe-matches; `status = 'ok' if (added or touched) else 'empty'`. The dedup
  test was racy on backend completion order before this fix.

- **v8 sitemap transport is injected**: `sitemap.discover_sitemap` takes an
  `http_get` callable (url -> (status, body) | None) so it has no hard dep on a
  specific HTTP client and is unit-testable with a fake. crawl._sitemap_map
  builds one with primp (impersonate='random') + a stdlib urllib fallback (some
  hosts reject primp fingerprints, accept urllib). sitemaps_used records EVERY
  sitemap fetched+parsed (index + leaves), not just leaf-yielding ones.

- **v8 PDF section-map**: bookmark ToC gets `end_page` via `_add_end_pages`
  (next entry at same/shallower level - 1, else total). PDFs without bookmarks
  get a heading-based fallback: `_render_page(page, body_size, text_mode,
  page_num=n, headings=heading_outline)` appends {level,title,page} per detected
  heading; `_heading_outline` dedupes, caps at 60, keeps only headings on
  extracted pages, clamps end_page to max(page_nums). `_extract_toc` reads
  `len(pdf)` for total pages (guarded for fakes without __len__).

- **v8 links classification**: anchors classified by container (ancestor in
  nav/header/footer/aside/role=navigation -> navigation for same-domain / dropped
  for external in_nav) vs main-content (article/main/section/p/li -> citation
  for same-domain, or content_external for off-domain). primary_source =
  canonical/JSON-LD on a DIFFERENT host, else first OFF-DOMAIN in-content link
  on a known primary host (arxiv/doi/github/wikimedia/etc). Same-domain links are
  never a primary source. Off-domain links in nav chrome (e.g. Wikipedia's
  donate.wikimedia sidebar link) are NOT primary_source candidates.

- **v8 related_queries is extractive, not SERP-scraped**: mined from result
  titles+snippets via bigram doc-frequency (no LLM, no per-engine 'related
  searches' markup dependency — that markup is fragile and changes often).
  Filter: drop bigrams where every token is in the query; drop df<2 bigrams;
  fall back to unigrams. Engine-agnostic = robust to backend SERP changes.

- **v7.5 search transport quirks (search_metasearch.py — vendored ddgs, MIT):**
  - 9 text backends: duckduckgo (httpx transport, POST, html.duckduckgo.com/html,
    XPath //div[contains(@class,'body')]; post_extract filters y.js ad links),
    brave (primp, search.brave.com, //div[@data-type='web'], cookies for
    region/safesearch), google (primp, Android UA + CONSENT cookie,
    //div[@data-hveid][.//h3]; often CAPTCHAs under load but carried by others),
    startpage (primp, POST, needs an `sc` token fetched from the homepage first;
    Google-index privacy frontend), grokipedia (primp, JSON api/typeahead,
    priority 1.9, encyclopedic), wikipedia (primp, opensearch API + extracts,
    priority 2.0, topic-match only), yahoo (primp, Bing-index from diff server,
    //div[contains(@class,'relsrch')], /RU= redirect decode), mojeek (primp, own
    index, //ul[results]/li), yandex (primp, yandex.com/search/site). **Bing
    disabled** (DDG+Yahoo serve it).
  - **Transport**: primp.Client(impersonate='random', impersonate_os='random')
    for most; httpx.Client(http2=True, _random_ssl_context cipher shuffle +
    _H2Patch randomizes HTTP/2 SETTINGS frame) for DDG. Both sync -> metasearch
    wraps engine.search in asyncio.to_thread.
  - **metasearch() async aggregator**: asyncio.wait(FIRST_COMPLETED) loop;
    min_engines=min(3,len(instances)) + soft_deadline=2.0 + quorum_results=
    max_results+4; early-return cancels laggards (await with `except BaseException`
    — CancelledError is BaseException py3.11+). Dedup by _normalize_url, tracks
    ALL backends per URL (seen[key]['backends'] set) for consensus. status per
    backend: ok/empty/error:X/timeout/preempted.
  - **_HOUND_TO_BACKEND**: bing->yahoo, qwant->duckduckgo (no ddgs qwant).
    _INDEX_FAMILY by ddgs provider: duckduckgo+yahoo='bing', google+startpage=
    'google', brave/grokipedia/wikipedia/mojeek/yandex each own. consensus =
    distinct families.
  - `HOUND_SEARCH_PROXY` env -> primp/httpx proxy param (per-IP throttle escape).
  - **search_engines.py adapter**: EngineReport.preempted = cancelled-because-
    enough (NOT blocked); engine_blocked in search.py = r.blocked only.
    fetch_source_for_similar (find_similar) uses a one-off scrapling
    FetcherSession(impersonate=[chrome131/136/142, edge, safari184, firefox147],
    stealthy_headers=True, retries=1) — a single arbitrary page, not a repeated
    engine hit, so metasearch backend rotation does not apply.

- **Dev venv ships a BUILT WHEEL, not an editable install**: master-fetch's dev
  venv (`.venv/`) contains a BUILT WHEEL in `site-packages/`, NOT an editable
  install. `pytest` therefore runs the STALE installed copy, not `src/`. Before
  testing any `src/` edit: `.venv/Scripts/pip install -e . --no-deps`. Verify the
  import points at src/, not site-packages:
  `python -c "import master_fetch; print(master_fetch.__file__)"` → must show
  `src/master_fetch/...`. If it shows `.venv/Lib/site-packages/...`, the editable
  install didn't take and you're testing stale code.

- **Optional-dep masking (recurring class)**: a dev venv with a dep installed
  manually MASKS a missing pyproject declaration. Happened on 4.0.0 (pdfplumber),
  5.0.0 (onnxruntime for rapidocr v3). After adding an optional dep, verify in a
  CLEAN venv that `pip install pkg[all]` actually delivers it AND that the dep
  can be INSTANTIATED (not just imported) — import-only checks miss backend deps
  loaded at __init__ (rapidocr v3 ships without onnxruntime; import works,
  RapidOCR() raises). Grep the BUILT wheel METADATA for
  `Requires-Dist: ...; extra == 'all'` to confirm declaration.

- **old.reddit.com thing-block class has a LEADING SPACE**: real HTML is
  `class=" thing id-t3_..."`, never `class="thing...`. Any regex matching post
  blocks must allow `class="[^"]*\bthing\b`. Subreddit listing thing blocks carry
  `data-score`/`data-comments-count`/`data-url`/`data-domain`; user-profile
  thing blocks (comments) often lack them (use `score-hidden`); the parser
  falls back to per-block `score unvoted` span + `full comments (N)` link, else
  honest `?`. Verified Jun 2026 on r/Python, u/spez, post pages.

- **CID font corruption + the OCR-recovery trick**: PDFs with embedded font
  subsets lacking a ToUnicode CMap make pdfplumber/pdfminer emit
  `(cid:71)(cid:302)...` garbage for those fonts (architecture diagrams, figures,
  math in academic papers). But the glyphs RENDER correctly visually — only the
  text-to-unicode map is broken. So rendering the page (pypdfium2) + OCR
  (rapidocr) recovers the REAL text. This is hound's PDF flagship: detect
  CID-garbage ratio >= 0.30 per page -> batch-OCR those pages -> replace garbage
  with recovered text + an honest marker. Verified on Tree of Thoughts p2
  (46% CID -> 0 remaining). If OCR extras absent, keep garbage + low
  quality_score + content_ok=false + marker telling the agent to install [all]
  or use vision.

- **pypdfium2 ToC/bookmark API**: `pdf.get_toc()` returns a GENERATOR
  (materialize with list()). Each `PdfBookmark` has `.level` (0-based int) +
  `.get_title()` (str) + `.get_dest()` -> `PdfDest` with `.get_index()` (0-based
  page index; page = idx+1). get_dest() may return None for URI-action bookmarks
  — wrap in try/except. CLIP/GPT-3: GPT-3 has 32 entries, CLIP has 0 (no
  outline). Most arxiv papers lack a ToC; books/reports have one.

- **PDF quality_score must exclude honest markers**: computing quality_score
  from the FINAL assembled markdown (which includes clean-English scaffolding
  like "[Page N: 64% CID garbage...]") inflates the score and masks corruption.
  Compute it from RAW rendered page text + OCR status (OCR-recovered pages =
  1.0, unrecovered CID pages = _quality_score(raw)) BEFORE adding markers.

- **OCR test fixture must be detector-friendly, not just human-readable**:
  `tests/dummy.pdf` was one tiny line of text on a blank A4 page. Visually
  trivial to OCR, but RapidOCR's text DETECTOR (region-proposal) did not fire on
  a text box occupying <2% of the canvas, so on CI's ubuntu/3.13 runner OCR
  returned "[No text detected on this page.]" while tests asserted the exact
  string. Local OCR (Windows) caught it; CI did not. Flaky across OS/version.
  Fix: regenerated dummy.pdf as a realistic scan (48pt bold multi-line text
  filling the page) so the detector reliably fires everywhere. **General lesson:
  never assert exact OCR/probabilistic-engine output in CI. Assert the path ran
  (header marker) + a meaningful guard (no "No text detected" marker) instead of
  an exact recognized string.**

- **edit tool + apostrophes (reaffirmed v7):** replacing long blocks containing
  straight apostrophes fails the edit tool's exact match. Workaround: splice the
  file programmatically with a small Python script (read lines, replace the line
  range, write back) instead of the edit tool.

- **Single warm browser — confirmed not the source of phantom Chrome**: hound
  only ever launches the ms-playwright chromium ("Google Chrome for Testing",
  real_chrome=False everywhere; scrapling uses channel="chromium" when
  real_chrome is falsy). Verified mid-stealthy-fetch: the only chrome.exe
  ExecutablePath is `...\ms-playwright\chromium-XXXX\chrome-win64\chrome.exe`.
  Any plain "Google Chrome" (Program Files) is another tool/MCP server, NOT
  hound. Don't chase it in hound again.

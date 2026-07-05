<div align="center">

<img src="https://raw.githubusercontent.com/dondai1234/master-fetch/master/docs/hound-logo.png" alt="Hound logo" width="128">

# 🐕 Hound

**Give your AI agent the web. $0. Two commands. No keys.**

Fetch · crawl · bypass bot walls · read PDFs (even scanned) · search the web
One MCP server · one warm browser · zero accounts · runs on your machine

[![PyPI](https://img.shields.io/pypi/v/hound-mcp.svg?label=pypi)](https://pypi.org/project/hound-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/hound-mcp.svg)](https://pypi.org/project/hound-mcp/)
[![License: MIT](https://img.shields.io/pypi/l/hound-mcp.svg)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/dondai1234/master-fetch/ci.yml?label=CI)](https://github.com/dondai1234/master-fetch/actions/workflows/ci.yml)
[![Downloads](https://static.pepy.tech/badge/hound-mcp)](https://pepy.tech/project/hound-mcp)
[![GitHub stars](https://img.shields.io/github/stars/dondai1234/master-fetch?style=social)](https://github.com/dondai1234/master-fetch/stargazers)

```bash
pip install hound-mcp[all] && playwright install chromium
```

[Install](#-install) · [The 6 tools](#-the-6-tools) · [Search](#-local-keyless-search) · [Comparison](#-comparison-free-tools) · [Honest limits](#-honest-limits)

</div>

<br>

<div align="center">
<img src="https://raw.githubusercontent.com/dondai1234/master-fetch/master/docs/hound-hero.png" alt="Hound gives your AI agent the web" width="860">
</div>

---

## 🎬 Demo

Same prompt, three tools. Hound does the whole thing on its own, search + fetch + crawl, locally. The others get stuck on the parts they don't do.

<video src="https://github.com/user-attachments/assets/172212ac-0e50-4030-8d23-878ee1db30ed" controls muted width="860"></video>

Prefer the full-resolution file? [Download the 60s demo (mp4)](https://github.com/dondai1234/master-fetch/releases/download/v9.0.0/Hound.demo.mp4).

---

## ✨ New in 9.0.0

**Production-hardening release.** The MCP server now starts in under 1 second and exits cleanly on Windows with an empty stderr: no more asyncio `__del__` tracebacks that a client can mistake for a crash (the old "failed to load"). The reranker prewarm is race-safe, and a CI lifecycle test proves the full connect → list → call → disconnect cycle stays clean. No new tools, no API breaks. [Release notes →](https://github.com/dondai1234/master-fetch/releases/tag/v9.0.0)

---

## Why developers pick Hound

Hound is one [MCP](https://modelcontextprotocol.io) server that gives any agent (Claude Code, Cursor, OpenCode, Hermes, Pi, anything that speaks MCP) full web research from a single local process.

- 🆓 **$0 forever, MIT**: no keys, no accounts, no per-request billing, no data routed to a third-party scraper. Search is keyless and local.
- 🧠 **Mastered on connect**: a one-time `instructions` block hands the agent the mental model, the #1 workflow, and the known limits. Effective on turn one.
- 📐 **~2.7K tokens, 6 tools**: hand-crafted tool defs, no Pydantic schema bloat. More capability than tools shipping 5K+.
- 🎯 **Every response is actionable**: `content_ok`, `next_action`, `summary`, `relevance_score`, `fetch_relevance`. Agents branch on structured fields, not error text.
- 🛡️ **Production-safe startup + shutdown**: cold start under 1s so the MCP handshake never times out; exits 0 with clean stderr, no crash-like teardown noise.

> Hound is for the agent itself. You install it once; the agent calls it whenever it needs the web.

---

## 🚀 Quick start

```bash
pip install hound-mcp[all]          # fetch + crawl + keyless search + PDF + OCR + neural rerank
playwright install chromium         # the anti-detect browser engine
```

Then point any MCP client at the `hound` command. No arguments, no keys, no env vars. See **[Install](#-install)** for the lean option and **[Tell your agent to install it](#-tell-your-agent-to-install-it)** for a copy-paste prompt.

```bash
hound -v        # version + update status
hound -u        # update to latest
```

---

## 🧰 The 6 tools

| Tool | One-liner |
|------|-----------|
| [`smart_fetch`](#-fetch--anti-bot) | Fetch any URL. HTTP first, auto-escalates to the anti-detect browser if blocked. Bulk, PDFs (with OCR + quality score), `css_selector`, `focus`, `actions`, pagination. |
| [`smart_crawl`](#-crawl) | Best-first same-domain crawl. Each page as markdown with `content_ok` + `page_type` (article / list / js_shell). `discover_only`, `crawl_urls`, `focus`, sitemap mode, time + token caps. |
| [`smart_search`](#-local-keyless-search) | Local keyless web search. 10 backends in parallel, merges + ranks with neural rerank + cross-backend consensus. `relevance_score` + `engines_consensus` per result. |
| [`screenshot`](#-screenshot) | Capture a page as an image. For multimodal agents (canvas, image-of-text, visual layout). |
| `cache_clear` | Clear the fetch cache. `all=true` wipes everything. |
| `version` | Installed version + update status. |

---

## 🔎 Local keyless search

<div align="center">
<img src="https://raw.githubusercontent.com/dondai1234/master-fetch/master/docs/hound-scene.png" alt="Hound fetches the web and brings it back to your agent" width="820">
</div>

No API key, no account, no third-party service. `smart_search` runs **10 keyless backends in parallel** on your machine, merges, dedups, and ranks. It returns URLs + ranking, **not page content**: the agent `smart_fetch`es whichever results match what it needs (the ranking is a hint, not a directive).

- 🌐 **10 independent backends**: duckduckgo, brave, mojeek, yahoo, yandex, startpage, google, qwant, plus opt-in wikipedia + grokipedia. Six+ independent index families, not the same feed twice.
- 🧠 **Neural rerank**: a local ONNX cross-encoder (`ms-marco-MiniLM-L-6-v2`, Apache-2.0) running on the `onnxruntime` Hound already ships for OCR. Exa-style semantic ranking, $0, on your machine. Model downloads once (~80MB, cached, not bundled). Lean installs fall back to cross-engine consensus + engine-position order.
- 🎯 **Cross-backend consensus**: a URL returned by several independent indexes gets a consensus boost: a free authority signal from merging, no extra fetches. Every result carries `relevance_score` (0–1), `fetch_relevance` (high/med/low), and `engines_consensus`.
- 🔍 **`find_similar`**: pass `url=`; Hound fetches a page you like, derives a query, and reranks candidates against that source page. Exa's find-similar, local.
- 🛡️ **Never dead**: a diversity quorum waits for at least 3 backends to contribute before returning, so a single backend's bias or rate-limit can't dominate. A backend that CAPTCHAs or rate-limits is circuit-broken for 60s and carried by the others. `engine_blocked` in the response reports which ones cooled down.
- 📊 **Filters**: `site` / `exclude_sites` (domain include/exclude), `location` / `language` / `region` (geo), `page` (0–10), `freshness` (day | week | month | year). Default 6 results. A quality filter drops low-relevance results instead of padding to the max with garbage.
- 📈 **`related_queries`**: follow-up queries mined from result titles + snippets (no LLM). Search one to refine a broad query.

Search is **100% HTTP**: it never touches the browser (the single Patchright browser is `smart_fetch`'s alone).

<details>
<summary><b>🔧 Search Engine Resilience Layer</b></summary>

Scraping public engines from your IP can be rate-limited or CAPTCHA'd. No keyless local tool is bulletproof against sustained blocking without a proxy: Hound is honest about that, then makes the no-proxy case as reliable as possible for a single user:

| # | Mechanism | What it does |
|---|-----------|--------------|
| 1 | **Persistent warm session per engine** | One long-lived session reused across searches: cookies + TLS accumulate, so the engine sees a returning human, not a fresh bot. Also faster. |
| 2 | **Per-engine pacing + jitter** | Within one search all engines fire in parallel (free); only same-engine bursts across searches get a small jittered delay. |
| 3 | **Circuit breaker + cooldown** | A blocked engine auto-cools (60s) while the others keep serving. |
| 4 | **202 / 429 / 503 / 403 + Retry-After** | DDG's HTTP 202 soft rate-limit is detected; `Retry-After` honored. |
| 5 | **Fingerprint rotation** | A pool of real Chrome / Edge / Firefox / Safari TLS profiles, picked per request. |
| 6 | **Diverse pool + consensus** | 10 backends across 6+ index families run in parallel: no single engine is a bottleneck, and agreement across independent indexes is a free authority signal. |
| 7 | **`HOUND_SEARCH_PROXY`** | Route all engine requests through your own rotating / residential proxy: the bulletproof path for heavy use. |

Same gray-area posture as SearXNG / ddgs; no search-engine ToS compliance is claimed.
</details>

---

## 🌐 Fetch & anti-bot

`smart_fetch` tries plain HTTP first (~1s). If the site blocks HTTP or serves a JS shell, it auto-escalates to a **Patchright** anti-detect browser with Cloudflare challenge solving. Two tiers, nothing to configure.

- 🛡️ **Built-in Cloudflare bypass**: a single stealthy Chrome warms at startup and stays alive for the whole session. Pages close after each fetch, idle memory stays near baseline. One browser total.
- 🎯 **Query-focused extraction**: `smart_fetch(url, focus="...")` returns only the BM25-relevant blocks. Cuts context 80%+ on long pages, no re-fetch (runs post-cache). Re-pass the same `focus` when paginating.
- 🖱️ **Page interaction**: `actions=[{click:'button.load-more'},{fill:{selector:'#q',text:'x'}},{press:'Enter'},{wait:500},{scroll:3},{wait_selector:'.item'}]` for load-more, search forms, pagination, infinite scroll. Forces stealthy + bypasses cache.
- 🏷️ **Metadata on every response**: title, description, site name, type, image, canonical URL, language, published time, author (OpenGraph + JSON-LD + canonical).
- 🔗 **Outgoing links**: `include_links=true` populates `response.links` classified as `citations` (main-content references, the ones worth following) / `navigation` / `external` + a `primary_source` hint. Follow a page's source chain in one step.
- 🐕 **Reddit, optimized**: Reddit URLs auto-rewrite to old.reddit.com (7× smaller) and skip to the stealthy browser. Subreddit listings parse into structured posts with promoted ads filtered out.
- 💾 **Smart caching**: SQLite (WAL mode), keyed by URL + extraction type + `css_selector` + `pages`. Bad content is never cached; a size cap evicts the oldest so a long-lived agent's cache can't grow unbounded. `cache_ttl=0` forces fresh.
- 📐 **Pagination**: content over 40KB is chunked; the response gives `next_offset` so the agent pages through with one more call (served instantly from cache).

---

## 🕷️ Crawl

`smart_crawl` walks same-domain links in **best-first** order: discovered URLs are scored by focus relevance + content-likelihood (docs/guide/api boosted, login/submit/cart penalized) + shallow depth, so content pages are crawled before junk when the budget is tight.

- 🎯 **Content-adaptive extraction**: article/docs → trafilatura main content; list/index pages (HN, aggregators, directories) → a structured `* [title](url)` link list; JS shells → detected and reported honestly.
- 🗺️ **Sitemap mode**: `options sitemap=true` maps the whole site from `sitemap.xml` in ONE fetch (full URL list + lastmod, no BFS). `sitemap='auto'` uses it if the site has one, else falls back to BFS. Collapses a hundreds-of-pages discovery crawl into one call.
- 📍 **`discover_only=true`**: URL map only (BFS-based). For big sites prefer `sitemap=true` instead.
- 🎯 **`focus='query'`**: prioritizes relevant pages within the budget AND focus-filters each page's content.
- 📋 **`crawl_urls=[...]`**: second-phase selective crawl of a chosen subset (no re-discovery).
- 🛡️ **Dedup + scoping**: URLs normalized so `/docs` and `/docs/` are never crawled twice. Same-domain only by default; `path_include` / `path_exclude` to scope.
- ⏱️ **Caps**: `max_pages` (default 10), `max_depth` (default 2), `max_total_chars` (token budget), `deadline_ms` (overall time, default 120000). Each page carries `content_ok` + `status` + `fetched_at`; `next_action` tells you if the crawl stopped early.

---

## 📄 PDF + scanned-PDF OCR

`smart_fetch` detects a PDF (by content-type **or** `%PDF` magic bytes) and extracts it to **structured markdown** with `pdfplumber` (MIT): multi-column reading order, real **tables as markdown tables**, font-size headings, de-hyphenated paragraphs, a metadata header, and `--- Page N ---` markers.

- 📑 **`table_of_contents`**: the PDF outline as `[{level, title, page, end_page}]`. PDFs without bookmarks get a heading-based fallback map. Pass `pages='23-31'` to grab one section by range and save tokens.
- 🔍 **CID-corruption auto-OCR (the flagship trick)**: academic papers embed font subsets without a Unicode map, so extractors emit `(cid:71)(cid:302)...` garbage for figures/diagrams/math. But the glyphs render correctly. Hound detects CID-garbage pages, renders them via `pypdfium2`, and OCRs them with `rapidocr`, recovering the real text automatically.
- 🖼️ **Scanned / image-only PDFs** (and image-only web pages) are auto-OCR'd too. Pure-pip, no system binary, with `[all]`.
- 📊 **`quality_score`** (0.0–1.0) + honest `content_ok`: trust PDF content more the closer the score is to 1.0.
- 📎 **`password`** for encrypted PDFs; `include_media=true` for per-page image metadata; a `.pdf` URL that returns a login/paywall is reported as `auth_required`.

---

## 📸 Screenshot

`screenshot` captures a page as an image. For **multimodal agents only**: use when content is rendered as images / canvas / image-of-text or you need visual layout. Text-only agents should use `smart_fetch` instead. A stealthy browser session is auto-managed.

---

## 📊 Comparison: free tools

Most free web tools for agents do one thing and miss the rest. Hound is the only one that bolts all of it onto a single local MCP server for $0, no keys.

| | **Hound** | Crawl4AI | Parallel Search | Jina Reader | Firecrawl (OSS/free) |
|---|---|---|---|---|---|
| **Price** | $0 forever | $0 (self-host) | free, rate-limited | free, rate-limited | $0 self-host / 1K free |
| **Runs locally** | yes | yes | no (their servers) | no (their API) | self-host: yes (Redis + Docker) |
| **Web search** | yes (keyless local, 10 backends) | **no** | yes (remote) | yes | **no** |
| **Deep crawl** | yes (best-first, sitemap, budget) | yes | **no** | no | yes (cloud) |
| **Anti-bot / Cloudflare** | built-in (Patchright) | limited | yes (their infra) | none | not by default |
| **PDF → structured markdown** | yes (tables, ToC, subset) | partial | no | yes (native) | yes (cloud + OCR) |
| **Scanned-PDF / image OCR** | yes (rapidocr, pure-pip) | **no** | no | no | yes (cloud paid) |
| **Page interaction** | yes (`actions`) | hooks (code) | no | no | yes (cloud) |
| **Query-focused extraction** | yes (`focus`, BM25) | yes (BM25 filter) | no | no | no |
| **Agent signals** | yes (`content_ok`/`next_action`/`summary`/`relevance_score`) | no | no | no | no |
| **Connect-time `instructions`** | yes | no | no | no | no |
| **MCP server** | yes (official) | community | yes (official) | yes (official) | build it |
| **Token cost (tools/list)** | ~2.7K (6 tools) | varies | n/a | n/a | varies (12 tools) |

**The short version:** Crawl4AI crawls well but has no search and trips on Cloudflare. Parallel Search is remote search-only, no crawl, and runs on their servers. Jina fetches but rate-limits and routes through Jina. Firecrawl keeps the good stuff behind the paid cloud. Hound is the only free tool that combines keyless local search, built-in Cloudflare bypass, best-first crawl, scanned-PDF OCR, page interaction, and query-focused extraction in one local MIT server: $0, no accounts, no keys.


<details>
<summary><b>When a paid service makes sense</b></summary>

Paid scrapers (Bright Data, ZenRows, Firecrawl paid, Spider.cloud) can beat free tools on the hardest anti-bot (DataDome, Akamai, Cloudflare Turnstile) and on massive scale, because they run large residential-proxy networks. Paid search APIs (Exa, Tavily) offer hosted neural search. They cost $16 to $500+/month, require accounts + API keys, and send your queries + content through their servers. Use Hound for $0 local web research with no accounts and no keys; reach for a paid service only for enterprise scale, sites Hound explicitly can't crack, or hosted neural search at scale.
</details>

---

## 📦 Install

```bash
pip install hound-mcp[all]          # recommended: fetch + crawl + keyless search + PDF + OCR + neural rerank
playwright install chromium
```

<details>
<summary><b>Lean install (no neural rerank, no PDF/OCR)</b></summary>

```bash
pip install hound-mcp               # fetch + crawl + keyless search (consensus + engine-position ranking)
playwright install chromium
```

The lean install is fully functional: multi-engine keyless search with cross-backend consensus, anti-bot, crawl, fetch, caching. `[all]` adds the ONNX neural reranker, PDF extraction, and OCR (scanned PDFs + CID-recovery + image pages) on the same `onnxruntime`.
</details>

<details>
<summary><b>Optional environment variables</b></summary>

| Variable | Purpose |
|----------|---------|
| `HOUND_SEARCH_PROXY` | Route all search-engine requests through your own proxy (`http://host:port`, `socks5://...`, or `user:pass@host:port`). For sustained heavy search use with a rotating / residential proxy. Not required for normal single-user use. |
| `HOUND_SEARCH_MIN_INTERVAL` | Override the per-engine pacing floor (seconds, float). `0` = use the built-in defaults (DDG 1.2s, Bing 1.5s, Wikipedia 0.3s). Power-user tuning. |

No API keys or accounts are needed for anything: search is keyless and local.
</details>

---

## 🤖 Tell your agent to install it

Paste this into your agent:

```
Install the Hound MCP server on this machine. Follow every step. Do not skip any.

1. Figure out which agent harness you are running on (OpenCode, Hermes, Pi, etc). Then find: (a) where the MCP config file lives, and (b) what format it expects for adding a local MCP server. Read the harness docs if needed. Do not guess.

2. Run: pip install hound-mcp[all]
   Then run: playwright install chromium (But only if it isnt installed already, verify first about its existence)
   If either fails, stop and tell the user.

3. Find the MCP config file from step 1 and back it up before editing. Add a new MCP server named "hound" with command "hound", no arguments, in the format your harness requires. No API keys or environment variables are needed (search is keyless and local).

4. Save the file. Tell the user to restart the agent. After restart, smart_fetch, smart_crawl, smart_search, screenshot, cache_clear and version should be available.
```

<details>
<summary><b>For Pi agent users</b></summary>

```
Install the Hound MCP server. Follow every step. Do not skip any.

1. Run: pip install hound-mcp[all]
   Then run: playwright install chromium (But only if it isnt installed already, verify first about its existence)
   If either fails, stop and tell the user.

2. Check pi-mcp-adapter: pi list. If not installed: pi install npm:pi-mcp-adapter

3. Backup ~/.pi/agent/mcp.json, then add this inside mcpServers:
   "hound": { "command": "hound", "transport": "stdio", "lifecycle": "eager" }
   No API keys or environment variables are needed (search is keyless and local).

4. Tell the user: "Run /reload, then /mcp to verify. smart_fetch, smart_crawl and smart_search should be available."
```
</details>

---

## ⚠️ Honest limits

No free tool can do everything. Hound is upfront about what it can't:

| Limit | What happens instead |
|-------|----------------------|
| **DataDome / Akamai / Cloudflare Turnstile (interactive)** | Not bypassed. `next_action` tells the agent to switch sources instead of retrying. |
| **Search rate-limits / CAPTCHAs** | Solved by diversity: 10 keyless backends run in parallel; a backend that rate-limits/CAPTCHAs is carried by the others, and a diversity quorum waits for 3 to contribute so no single backend dominates. Search is never dead. `engine_blocked` reports cooled-down backends; `HOUND_SEARCH_PROXY` is a power-user rotating-proxy escape hatch for per-IP throttling (the one thing no scraper can escape from one IP). |
| **Neural / find_similar search** | Need `hound-mcp[all]` (the ONNX reranker runs on the same `onnxruntime` as OCR; model downloads once). Lean installs get cross-backend consensus + engine-position ranking. |
| **Sites requiring login** | Out of scope (Hound does page interaction, not authenticated sessions). |
| **Deep shadow-DOM / hard SPAs** | `actions` (scroll, click, `wait_selector`) reach most of it; deep shadow-DOM piercing not yet wired. |
| **YouTube** | Minimal text. |

When a fetch or search fails, the response says exactly why and what to try next, so the agent doesn't waste calls guessing.

---

## 🪙 Token cost

<div align="center">
<img src="https://raw.githubusercontent.com/dondai1234/master-fetch/master/docs/tokens.svg" alt="MCP tool count comparison: Hound 6 tools vs Firecrawl 12, Jina 19, Bright Data 60+" width="760">
</div>

Most MCP servers cost 3–5K tokens just to exist. Hound's 6 tools cost **~2.7K tokens** at `tools/list` (measured with `cl100k_base`); the connect-time `instructions` (~0.8K, the orientation doc) are injected ONCE at handshake, not repeated every turn. Your context window is expensive; Hound respects it.

---

<div align="center">

### If Hound saves you time, ⭐ the repo: it helps others find it.

[![GitHub stars](https://img.shields.io/github/stars/dondai1234/master-fetch?style=social)](https://github.com/dondai1234/master-fetch/stargazers)

**MIT** · [Changelog](CHANGELOG.md) · [Issues](https://github.com/dondai1234/master-fetch/issues) · [PyPI](https://pypi.org/project/hound-mcp/)

</div>

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

<div align="center"><b>OpenCode + Hound</b></div>

<video src="https://github.com/user-attachments/assets/4e69e476-9c46-40cd-9bdf-f1b45882ac3e" controls muted width="860"></video>

<div align="center"><b>Pi + Hound</b></div>

<video src="https://github.com/user-attachments/assets/18b502c0-1e7b-4cb3-a730-074a9fbd0993" controls muted width="860"></video>


---

## ✨ New in 12.1.2

**BYOK search · intent-aware multi-query fan-out · six-signal ranking · next-gen stealth engine.**

- 🔑 **Bring Your Own Key (BYOK) search**: add your own API keys for Serper, Tavily, Exa, Firecrawl, or TinyFish via `hound keys add`. Keys become the primary search source with **key stacking** (multiple keys per provider, auto-rotation on rate limit), automatic fallback to Hound's keyless local engines when all keys are exhausted. CLI key management: `hound keys add/list/test/remove/clear`.
- 🧠 **Intent-aware multi-query fan-out**: Hound detects query intent (comparison, howto, research, code, reference, news, factual) and generates expanded query variants distributed across diversity engines. Same parallel request count, zero added latency, wider recall. Cross-variant consensus: a URL surfaced by different queries from different engines is a stronger authority signal.
- 📊 **Six-signal ranking**: cross-variant consensus + domain reputation + answer-signal scoring + title relevance + URL relevance + result diversity (max 2 per domain in top results). Source-type detection tags every result as docs/paper/repo/blog/forum/reference/news.
- 🧬 **Next-gen stealth engine**: system Chrome auto-detection, 4 coherent fingerprint profiles, JS-layer patches (HeadlessChrome UA fix, `navigator.webdriver=undefined`, canvas noise, permissions API), human behavior simulation (Bezier mouse curves, natural scroll), CF Turnstile solver with human-like mouse movement. Passes bot.sannysoft.com, bypasses Cloudflare Turnstile on CanadianInsider (hardest in a 31-site benchmark), Medium, StackOverflow, NowSecure, Glassdoor (DataDome). See the [stealth benchmark](#-stealth-benchmark) below.
- 🐍 **No more scrapling.** All scrapling functionality replaced with hound's own modules: `fetcher.py` (primp-based HTTP), `browser.py` (patchright-based browser), `extractor.py` (trafilatura + markdownify). Smaller install, fewer transitive deps, faster cold start.
- 📱 **Works everywhere.** Lean install `pip install hound-mcp` pulls no browser deps. On platforms without playwright (Termux, aarch64), hound runs in HTTP-only mode with graceful degradation.
- 🔒 **Reliability fixes**: universal error detection (error pages no longer look like success), dead Internet Archive fallback removed, self-healing CLI + stale process cleanup, CSS selector errors propagate instead of silently returning `[]`, browser startup failure cleans partial sessions, `hound --rollback` works for pinned older versions, `focus` and `actions` now forwarded from MCP dispatcher to `smart_fetch`.
- 🐳 **Docker support**: multi-stage Dockerfile, docker-compose with shm_size 1gb, non-root user, healthcheck. By @imonlinux.
- 🧪 **673 tests**.


---

## Why you should pick Hound

Hound is one [MCP](https://modelcontextprotocol.io) server that gives any agent (Claude Code, Cursor, OpenCode, Hermes, Pi, anything that speaks MCP) full web research from a single local process.

- 🆓 **$0 forever, MIT**: no keys, no accounts, no per-request billing, no data routed to a third-party scraper. Search is keyless and local.
- 🧠 **Mastered on connect**: a one-time `instructions` block hands the agent the mental model, the #1 workflow, and the known limits. Effective on turn one.
- 📐 **~2.9K tokens, 6 tools**: hand-crafted tool defs, no Pydantic schema bloat. More capability than tools shipping 5K+.
- 🎯 **Every response is actionable**: `content_ok`, `next_action`, `summary`, `page_type`, `content_age_days`/`is_stale`, `source_type`/`is_official`, `relevance_score`, `fetch_relevance`. Agents branch on structured fields, not error text. Hard-blocks (404/bot/auth) return clean errors, not fake content.
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
hound -v          # version + update status
hound -u          # update to latest (brick-proof, self-healing)
hound --doctor    # health check + fix advice
hound --rollback  # undo the last update
```

If `hound` ever breaks (a failed update, a locked launcher), recover with `python ~/.hound/repair.py`, or run `hound --doctor` to diagnose.

---

## 🐳 Docker

Hound can run in Docker with HTTP mode, ideal for:
- Running on a server
- Isolated deployment

### Quick start with Docker Compose

```bash
git clone https://github.com/dondai1234/master-fetch.git
cd master-fetch
docker-compose up -d
```

Hound will be available at `http://<your-host-ip>:8765/mcp` as an HTTP MCP server (use `localhost` if running on the same machine).

### Manual Docker build

```bash
docker build -t hound-mcp .
docker run -p 8765:8765 hound-mcp
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HOUND_BROWSER_IDLE_TIMEOUT` | 300 | Seconds before browser closes (0 = never) |
| `HOUND_SEARCH_PROXY` | - | Proxy for all search backends (http/https/socks5/socks5h) |
| `HOUND_SEARCH_DEADLINE` | 8 | Search deadline in seconds |
| `HTTP_PROXY` | - | Proxy for HTTP fetch requests |
| `HTTPS_PROXY` | - | Proxy for HTTPS fetch requests |

### Connecting to HTTP mode

For MCP clients that support HTTP (Claude Code, Open WebUI), use your host's LAN IP:

```
http://<your-host-ip>:8765/mcp
```

(Or `localhost:8765/mcp` if running on the same machine.)

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
- 📊 **Filters**: `site` / `exclude_sites` (domain include/exclusion), `location` / `language` / `region` (geo), `page` (0–10), `freshness` (day | week | month | year). Default 6 results. A quality filter drops low-relevance results instead of padding to the max with garbage.
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
| 7 | **`HOUND_SEARCH_PROXY` + rotation pool** | Route engine requests through one or more proxies. Add up to 20 and Hound rotates per search call: the bulletproof path for heavy use. |

Same gray-area posture as SearXNG / ddgs; no search-engine ToS compliance is claimed.
</details>

#### Smart proxy rotation

Local keyless search scrapes public engines from your IP. Sustained use can get rate-limited. Hound's proxy rotation lets you add multiple proxies and cycles through them automatically: each search call uses the next proxy, spreading traffic across all IPs. Unhealthy proxies (connection errors) are auto-cooled for 60s and skipped. If all proxies are down, Hound falls back to direct connection so search never fails.

**Setup in 30 seconds:**

```bash
# Add proxies (supports http, https, socks5, socks5h + auth)
hound proxy add "http://user:pass@31.59.20.176:6754"
hound proxy add "socks5://1.2.3.4:1080"
hound proxy add "http://1.2.3.4:8080" "socks5://5.6.7.8:1080"  # bulk add

# List configured proxies (credentials redacted)
hound proxy list

# Remove by index or clear all
hound proxy remove 0
hound proxy clear
```

Or set via env var (comma-separated):
```bash
export HOUND_SEARCH_PROXY="http://p1:8080,socks5://p2:1080,http://user:pass@p3:3128"
```

Max 20 proxies. Config persists in `~/.hound/search_proxies.json`. Rotation is per-search-call (not per-engine), so all engines in one search share one IP, and the next search rotates to the next proxy. `hound doctor` shows your proxy pool status.

**Free proxy sources tested with Hound:**

| Platform | Signup | Tested result | Recommended |
|---|---|---|---|
| **[Webshare](https://www.webshare.io/)** | Free account (no card) | 10 dedicated proxies, **100% success rate**, 6 countries | **Highly recommended** |
| **[ProxyScrape](https://proxyscrape.com/free-proxy-list)** | None | 2,000+ proxies, ~10% work, refreshed every minute | Quick start, no account |

Webshare's 10 free proxies are dedicated (yours alone, not shared with other scrapers), which is why they achieve 100% success. ProxyScrape's public list is shared and short-lived, but requires no signup. SOCKS5 outperforms HTTP for search engines because it tunnels HTTPS reliably.

```bash
# ProxyScrape: grab working SOCKS5 proxies (no signup needed)
curl -sL "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text" | grep "^socks5://" | head -5

# Add them to Hound's rotation pool
# (paste each one: hound proxy add "socks5://ip:port")
```


### 🔑 Bring Your Own Key (BYOK)

Hound's local keyless search works with zero configuration and never goes away. But some users have API keys from search providers, either from free tiers or paid plans. Hound respects that: bring your keys, and Hound makes them first-class citizens with the same reliability guarantees as its own keyless engines.

When keys are configured, those providers become the **primary search source** and Hound's local keyless engines are **completely shut off**. This is the entire point of BYOK: avoid hitting public search engines from your IP. The local engines run only as a last-resort fallback when every API key is exhausted or rate-limited, so the search never fails.

Hound uses **one provider per search**. If you have keys for multiple providers, Hound picks the first available one, and only switches to the next provider when the first is exhausted. This means your search capacity scales with how many keys you configure for a single provider, not with how many providers you stack.

#### Supported providers

| Provider | Free tier | Auth | Strength |
|----------|-----------|------|----------|
| [Serper](https://serper.dev) | 2,500 credits one-time | `X-API-KEY` | Google SERP, fast |
| [Tavily](https://tavily.com) | 1,000 credits/month | `Bearer` | AI-ranked results |
| [Exa](https://exa.ai) | 1,000 searches/month | `x-api-key` | Neural search |
| [Firecrawl](https://firecrawl.dev) | 1,000 credits/month | `Bearer` | Web search + crawl |
| [TinyFish](https://tinyfish.ai) | 30 req/min (~43K/month) | `X-API-Key` | High throughput |

Mix and match. Use one provider, use all five, switch providers anytime. Hound treats them as parallel engines in the same ranking pipeline.

#### Key stacking

Add **multiple keys per provider**. Hound stacks them in a rotation pool per provider:

- 🔁 **Auto-rotation**: when a key hits a rate limit (HTTP 429), Hound switches to the next key for the **same provider** within milliseconds. The search completes without the agent ever knowing a key was rate-limited.
- ⏳ **Per-key cooldown**: a rate-limited key enters a 60-second cooldown. An invalid key (401/403) enters a 300-second cooldown. Hound keeps trying the remaining keys in the pool.
- 🛡️ **Graceful exhaustion**: only when **all keys for all providers** are exhausted does Hound fall back to its keyless local engines. The transition is seamless: the agent gets results either way.
- 📊 **Live key testing**: `hound keys test` makes a real API call per key and reports which are valid, rate-limited, or invalid. Know before you search.

This means your search capacity scales with how many keys you configure, not with a single key's rate limit.

#### CLI key management

```bash
# Add a key (stack multiple keys for the same provider)
hound keys add serper YOUR_SERPER_KEY
hound keys add serper ANOTHER_SERPER_KEY   # stacked, auto-rotated
hound keys add tavily YOUR_TAVILY_KEY

# List all configured keys (redacted for safety)
hound keys list

# Test all keys (live API call per key)
hound keys test

# Test a specific provider
hound keys test serper

# Remove a specific key by index (0-based)
hound keys remove serper 0

# Remove all keys for a provider
hound keys remove serper

# Remove all keys across all providers
hound keys clear
```

Keys are stored at `~/.hound/search_keys.json` with redaction on display. Key rotation state is in-memory only (resets on restart).

#### Environment variables

For CI/CD, Docker, or ephemeral environments, set env vars instead of the config file. Comma-separated for multiple keys:

```bash
export HOUND_SEARCH_SERPER_KEYS=key1,key2,key3
export HOUND_SEARCH_TAVILY_KEYS=key1
export HOUND_SEARCH_EXA_KEYS=key1
export HOUND_SEARCH_FIRECRAWL_KEYS=key1
export HOUND_SEARCH_TINYFISH_KEYS=key1
```

Env vars override the config file for any provider that has env vars set. Providers without env vars fall back to the config file. Mix both: some providers in the config file, others via env vars.

#### `hound doctor` integration

`hound --doctor` reports your BYOK configuration status: which providers have keys, how many keys per provider, and whether any are in cooldown. One command to see the full picture.

---

## 🌐 Fetch & anti-bot

`smart_fetch` tries plain HTTP first (~1s). If the site blocks HTTP or serves a JS shell, it auto-escalates to a **Patchright** anti-detect browser with Cloudflare challenge solving. Two tiers, nothing to configure.

- 🛡️ **Built-in Cloudflare bypass**: a single stealthy Chrome warms at startup. It closes after 5 min of idleness to free RAM (`HOUND_BROWSER_IDLE_TIMEOUT`, set `0` to keep it alive forever) and relaunches in ~2s on the next fetch. Pages close after each fetch, idle memory stays near baseline. One browser total.
- 🧬 **Stealth engine (v11.1+)**: system Chrome auto-detection (`channel=chrome` for real TLS fingerprint), coherent fingerprint profiles, JS-layer patches (HeadlessChrome UA fix, `navigator.webdriver=undefined`, canvas noise via `getImageData`+`toDataURL` interception, permissions API), human behavior simulation (Bezier mouse curves, natural scroll, dwell time), and a Cloudflare Turnstile solver with human-like mouse movement. See the [stealth benchmark](#-stealth-benchmark) below.
- 🎯 **Query-focused extraction**: `smart_fetch(url, focus="...")` returns only the BM25-relevant blocks. Cuts context 80%+ on long pages, no re-fetch (runs post-cache). Re-pass the same `focus` when paginating.
- 🖱️ **Page interaction**: `actions=[{click:'button.load-more'},{fill:{selector:'#q',text:'x'}},{press:'Enter'},{wait:500},{scroll:3},{wait_selector:'.item'}]` for load-more, search forms, pagination, infinite scroll. Forces stealthy + bypasses cache.
- 🏷️ **Metadata on every response**: title, description, site name, type, image, canonical URL, language, published time, author (OpenGraph + JSON-LD + canonical).
- 🔗 **Outgoing links**: `include_links=true` populates `response.links` classified as `citations` (main-content references, the ones worth following) / `navigation` / `external` + a `primary_source` hint. Follow a page's source chain in one step.
- 🐕 **Reddit, optimized**: Reddit URLs auto-rewrite to old.reddit.com (7× smaller) and skip to the stealthy browser. Subreddit listings parse into structured posts with promoted ads filtered out.
- 💾 **Smart caching**: SQLite (WAL mode), keyed by URL + extraction type + `css_selector` + `pages`. Bad content is never cached; a size cap evicts the oldest so a long-lived agent's cache can't grow unbounded. `cache_ttl=0` forces fresh.
- 📐 **Pagination**: content over 40KB is chunked; the response gives `next_offset` so the agent pages through with one more call (served instantly from cache).

### 🧬 Stealth benchmark

Real-world results from v11.1.0, tested against hard anti-bot targets.

**Detection test sites (all checks pass):**

| Site | What it tests | Result |
|---|---|---|
| bot.sannysoft.com | HeadlessChrome UA, webdriver, plugins, WebGL, permissions, Selenium | ALL PASS |
| CreepJS | Canvas hash, audio fingerprint, lie detection | 200 OK |
| BrowserScan | CDP detection, fingerprint analysis | 200 OK |
| Pixelscan | Headless detection, fingerprint consistency | 200 OK |

**Anti-bot protected sites (content extracted):**

| Site | Protection | Status | Content |
|---|---|---|---|
| CanadianInsider | Cloudflare Turnstile (hardest in 31-site benchmark) | 200 | 78 KB, title: "Canadian Insider" |
| Medium | Cloudflare interstitial | 200 | 93 KB, title: "Medium" |
| StackOverflow | Cloudflare | 200 | 1.1 MB, full question page |
| NowSecure | Cloudflare challenge | 200 | 180 KB, title: "nowsecure.nl" |
| Glassdoor | DataDome | 200 | 849 KB |
| Reddit | Cloudflare lite | 200 | 1 MB |
| Hacker News | None (baseline) | 200 | 35 KB, title: "Hacker News" |
| GitHub | None (baseline) | 200 | 523 KB |

> **Note:** Google Search returns 429 (rate limited) as it uses its own bot detection independent of Cloudflare. This is expected.

**Stealth signals verified:**

| Signal | Value | Detection status |
|---|---|---|
| `navigator.webdriver` | `undefined` (patched from `false`) | Not detected |
| `navigator.userAgent` | `Chrome/150` (HeadlessChrome removed) | Not detected |
| `navigator.platform` | `Win32` (real system Chrome) | Not detected |
| `navigator.plugins.length` | 5 (real system Chrome) | Not detected |
| `window.chrome` | `object` (present) | Not detected |
| WebGL vendor/renderer | Real GPU (Intel UHD, Direct3D11) | Not detected |
| Canvas fingerprint | Per-session noise (different each session) | Not detected |
| TLS fingerprint | Real Chrome 150 JA4 (system Chrome) | Not detected |

**Memory efficiency (5 sequential fetches):**

RSS decreased by 3.5 MB over 5 fetches. No RAM creep. The `Memory.simulatePressureNotification` CDP command triggers Chrome's internal GC + cache drop after each fetch (~5ms, non-disruptive).

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
<summary><b>Lean install (HTTP-only mode, no browser/OCR)</b></summary>

```bash
pip install hound-mcp               # fetch + crawl + keyless search (HTTP-only, no stealthy browser)
```

The lean install works on all platforms (including Termux/Android). It gives you
multi-engine keyless search, HTTP fetch with auto-escalation (HTTP tier only, no
stealthy browser), crawl, and caching. Stealthy browser escalation and screenshot
require browser deps from the `[all]` extra.
</details>

<details>
<summary><b>Optional environment variables</b></summary>

| Variable | Purpose |
|----------|---------|
| `HOUND_SEARCH_PROXY` | Route all search-engine requests through your own proxy (`http://host:port`, `socks5://...`, or `user:pass@host:port`). For sustained heavy search use with a rotating / residential proxy. Not required for normal single-user use. |
| `HOUND_SEARCH_MIN_INTERVAL` | Override the per-engine pacing floor (seconds, float). `0` = use the built-in defaults (DDG 1.2s, Bing 1.5s, Wikipedia 0.3s). Power-user tuning. |
| `HOUND_BROWSER_IDLE_TIMEOUT` | Seconds of browser idleness before the warm Chrome is closed entirely to free RAM (default 300, i.e. 5 min). The next fetch relaunches it in ~2s. Set to `0` to keep Chrome alive forever (old behavior). |

No API keys or accounts are needed for anything: search is keyless and local.
</details>

<details>
<summary><b>Updating, rolling back, and repairing</b></summary>

```bash
hound -u          # update to latest (brick-proof: --no-deps, detached helper, self-heal)
hound --doctor    # health check: launcher, imports, metadata, deps, PyPI, repair script
hound --rollback  # reinstall the version from before the last update
```

`hound -u` is designed to never brick the install. It updates with `--no-deps` (no heavy extras that fail mid-install); on Windows it runs pip in a detached helper after the launcher exits (Windows can't overwrite a running .exe), freeing the launcher via the rename trick. If a pip pass leaves the version unchanged, it self-heals with a `--force-reinstall --no-deps` pass.

If `hound` is ever broken (a failed manual pip while a server held the launcher, a half-finished update), the safety net is a standalone script written outside site-packages on every update:

```bash
python ~/.hound/repair.py   # stops hound, force-reinstalls hound-mcp from PyPI, verifies
```

It survives because it is not part of the `hound-mcp` package, so a failed `pip uninstall` never removes it. `hound --doctor` diagnoses the install and tells you the right fix.
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

Install the Hound MCP server, then the Pi extension:

```bash
pip install hound-mcp[all]
pi install npm:@houndmcp/hound-mcp-pi
```

No API keys, no config file, no MCP adapter needed. The extension spawns `hound` as a singleton subprocess and registers all 6 tools (`web_fetch`, `web_search`, `web_crawl`, `web_screenshot`, `cache_clear`, `hound_version`) as native Pi tools. Prewarmed at session start. Run `/reload` to activate.

Updating:

```bash
hound -u                              # update the MCP server
pi update npm:@houndmcp/hound-mcp-pi  # update the extension
```

The extension checks version sync at session start and warns if the extension and hound diverge by a major version.

</details>

<details>
<summary><b>For Open WebUI (HTTP) users</b></summary>

Open WebUI v0.6.31+ speaks the streamable HTTP transport natively. Run Hound in HTTP mode and point Open WebUI at it, no `mcpo` proxy needed:

```bash
hound --http --host 127.0.0.1 --port 8765
```

Then in Open WebUI add an MCP server with URL `http://127.0.0.1:8765/mcp`. Stdio clients (Claude Code, Cursor, OpenCode, Pi, etc.) just use `hound` with no flag.

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
</div>

Most MCP servers cost 3–5K tokens just to exist. Hound's 6 tools cost **~2.7K tokens** at `tools/list` (measured with `cl100k_base`); the connect-time `instructions` (~0.8K, the orientation doc) are injected ONCE at handshake, not repeated every turn. Your context window is expensive; Hound respects it.

---

<div align="center">

### If Hound saves you time, ⭐ the repo: it helps others find it.

[![GitHub stars](https://img.shields.io/github/stars/dondai1234/master-fetch?style=social)](https://github.com/dondai1234/master-fetch/stargazers)

**MIT** · [Changelog](CHANGELOG.md) · [Issues](https://github.com/dondai1234/master-fetch/issues) · [PyPI](https://pypi.org/project/hound-mcp/)

</div>

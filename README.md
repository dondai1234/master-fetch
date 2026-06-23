<div align="center">

# 🐕 Hound

<img src="https://raw.githubusercontent.com/dondai1234/master-fetch/master/docs/hound-hero.png" alt="Hound - web fetch, anti-bot, PDF, crawl and search for AI agents" width="896">

**Give your AI agent the web. $0. Two commands. ~2K tokens.**

Fetch, crawl, bypass bot walls, read PDFs (even scanned), interact with pages, search the web.
No accounts. No Docker. No credit card. Runs on your machine.

[![PyPI](https://img.shields.io/pypi/v/hound-mcp.svg)](https://pypi.org/project/hound-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/hound-mcp.svg)](https://pypi.org/project/hound-mcp/)
[![License: MIT](https://img.shields.io/pypi/l/hound-mcp.svg)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/dondai1234/master-fetch/ci.yml?label=CI)](https://github.com/dondai1234/master-fetch/actions/workflows/ci.yml)
[![Downloads](https://static.pepy.tech/badge/hound-mcp)](https://pepy.tech/project/hound-mcp)

```bash
pip install hound-mcp[all] && playwright install chromium
```

[Install](#install) · [Tools](#tools) · [Features](#features) · [How it works](#how-it-works) · [Comparison](#comparison-free-tools) · [Honest limits](#honest-limits)

</div>

---

## What it is

Hound is an [MCP](https://modelcontextprotocol.io) server that gives any AI agent (Claude Code, Cursor, OpenCode, Hermes, Pi, anything that speaks MCP) full web research from a single local process: **fetch + crawl + anti-bot + PDF/OCR + page interaction + search**. It is built so the agent masters it the moment it connects, and so every response tells the agent exactly what to do next.

It is **$0 forever**, MIT-licensed, and runs locally. No API keys for fetching, no per-request billing, no data sent to a third-party scraper (search uses a free key from TinyFish).

> Hound is for the agent itself. You install it once; the agent calls it whenever it needs the web.

---

## Why agents love it

Most MCP servers dump 3 to 5K tokens of tool schema into the agent's context just to exist (some ship 12 to 60+ tools), and return raw blobs the agent has to interpret. Hound is different:

- **Connect-time mastery.** On `initialize`, Hound sends a concise `instructions` block (paid once, not per turn) that gives the agent the 4-tool mental model, the #1 workflow (search → fetch the high-relevance results → synthesize), and the known limits. The agent is effective on turn one.
- **Every response is actionable.** Each fetch returns `content_ok` (trust the content only if true), `next_action` (the obvious next call: paginate, bypass robots, switch sources), `summary` (a one-line status), `fetched_at`, and structured `metadata` (title, author, date, canonical). Agents branch on structured fields instead of guessing from error text.
- **~2K tokens for all 6 tools.** Hand-crafted tool definitions, no Pydantic schema bloat. More capability than tools shipping 5K+ tokens.

---

## Tools

| Tool | What it does |
|------|--------------|
| `smart_fetch` | Fetch any URL. HTTP first, auto-escalates to the anti-detect browser if blocked. Bulk via `urls`. PDFs auto-extracted (scanned PDFs auto-OCR). Narrow with `css_selector`. Focus to a query with `focus`. Interact with `actions` (click/fill/scroll). Paginate with `offset`. |
| `smart_crawl` | Deep-crawl a site: BFS same-domain from a URL, returns each page as markdown with `content_ok`. `discover_only=true` for a URL map. `focus` prioritizes relevant pages. Caps on pages/depth/token budget. |
| `smart_search` | Web search via TinyFish (free key). Domain/geo filters. `fetch_relevance` (high/med/low) per result. Research mode (`fetch_content=true`) auto-fetches the top results' full content in one call. |
| `screenshot` | Capture a page as an image. For multimodal agents (canvas, image-of-text, visual layout). Session auto-managed. |
| `cache_clear` | Clear the fetch cache. `all=true` wipes everything. |
| `version` | Installed version + update status. |

---

## Features

### 🕷️ Crawl a whole site (`smart_crawl`)
Read all the docs on a site, or scrape a section, in one call. `smart_crawl` walks same-domain links breadth-first from a start URL up to a depth/page/token budget and returns each page as clean markdown with the same `content_ok` / `summary` signals `smart_fetch` produces. `discover_only=true` returns just the URL map. `path_include`/`path_exclude` scope it (e.g. `["/docs"]`). `focus="query"` turns it into a query-prioritized crawl: the most relevant pages are crawled first within the budget, and each page is focus-filtered. One fetch per page, reusing `smart_fetch`'s anti-bot escalation and cache.

### 🛡️ Anti-bot, built in
`smart_fetch` tries plain HTTP first (fast, ~1s). If the site blocks HTTP or serves a JavaScript shell, it auto-escalates to a **Patchright** anti-detect browser with Cloudflare challenge solving. Two tiers, nothing to configure. JS-only SPAs that return an empty shell over HTTP are detected and escalated automatically.

### 📄 PDF + scanned-PDF OCR
`smart_fetch` detects a PDF (by content-type **or** `%PDF` magic bytes) and extracts it to **structured markdown** using `pdfplumber` (MIT, no AGPL baggage): multi-column reading order, real **tables as markdown tables**, font-size heading detection, de-hyphenated paragraphs, a metadata header, and `--- Page N ---` markers. Pass `pages="1-5"` to extract a subset. **Scanned/image-only PDFs are auto-OCR'd** with `rapidocr` (pure-pip, no system binary) when you install `[all]`; image-only web pages are OCR'd too. Encrypted PDFs accept a `password`.

### 🎯 Query-focused extraction (`focus`)
`smart_fetch(url, focus="...")` returns only the BM25-relevant blocks (paragraphs, headings, tables) for your query. On a long page this cuts context 80%+ with no re-fetch (it runs post-cache, so one cached page serves any focus query). Re-pass the same `focus` when paginating with `offset`.

### 🖱️ Page interaction (`actions`)
Content behind a click, a search form, a "load more" button, or infinite scroll: `smart_fetch(url, actions=[{click:"button.load-more"}, {fill:{selector:"#q", text:"x"}}, {press:"Enter"}, {wait:500}, {scroll:3}, {wait_selector:".item"}])`. Actions run on the stealthy browser after load, before extraction. Forces the stealthy tier and bypasses cache.

### 🔎 Search + research mode
`smart_search` returns results with a `fetch_relevance` tier (high/med/low) and a `fetch_hint` so the agent fetches 1 to 2 results instead of all 10. Filters: `site`/`exclude_sites` (domain include/exclude via `site:` operators), `location`/`language` (geo), `page`. **Research mode** (`fetch_content=true`) searches and bulk-fetches the top-N results' full content in this one call, each with `content_ok` and relevance, replacing the 3 to 5 call search → fetch loop with one call.

### 🏷️ Metadata on every response
Every HTML fetch carries structured `metadata` for citation and relevance: title, description, site name, type, image, canonical URL, language, published time, author (from OpenGraph, JSON-LD, the canonical link, and `<title>`).

### 🐕 Reddit, optimized
Reddit URLs are auto-rewritten to old.reddit.com (7x smaller pages) and skip straight to the stealthy browser (www.reddit.com walls HTTP). Subreddit listings are parsed into structured posts (title, score, comments, author, domain) from canonical per-post data attributes, with promoted ads filtered out and sticky/NSFW posts tagged.

### ⚡ One warm browser, always ready
A single stealthy Chrome warms up when the server starts and stays alive for the whole session, so stealthy fetches, crawls, and screenshots skip the 3 to 5s cold start. Only one browser instance ever runs; it closes cleanly when the agent harness closes. Pages are closed after each fetch and resource loading is dropped, so idle memory stays near the browser baseline.

### 💾 Smart caching
SQLite cache keyed by URL + extraction type + css_selector + pages. Uses the lesser of the stored and requested TTL. WAL mode for concurrent access. **Bad content is never cached** (JS shells, bot challenges, error statuses re-fetch instead of freezing broken pages). A size cap evicts the oldest entries so a long-lived agent's cache can't grow unbounded.

---

## Install

```bash
pip install hound-mcp[all]      # fetch + crawl + search + PDF + OCR (recommended)
playwright install chromium     # the anti-detect browser engine
```

Lean install (fetch only, no search/PDF/OCR):

```bash
pip install hound-mcp
playwright install chromium
```

Verify:

```bash
hound -v        # version + update status
hound -u        # update to latest
```

---

## Tell your agent to install it

Paste this into your agent:

```
Install the Hound MCP server on this machine. Follow every step. Do not skip any.

1. Figure out which agent harness you are running on (OpenCode, Hermes, Pi, etc). Then find: (a) where the MCP config file lives, and (b) what format it expects for adding a local MCP server. Read the harness docs if needed. Do not guess.

2. Run: pip install hound-mcp[all]
   Then run: playwright install chromium (But only if it isnt installed already, verify first about its existence)
   If either fails, stop and tell the user.

3. Find the MCP config file from step 1 and back it up before editing. Add a new MCP server named "hound" with command "hound", no arguments, in the format your harness requires. Include an environment variable TINYFISH_API_KEY (leave the value empty for now).

4. Ask the user: "Open this link to get a free TinyFish API key (no credit card): https://agent.tinyfish.ai/sign-up?ref=v1.dXNlcl8zRGVtcWxOT25nSU5aSkRzU0NLQXNtT1BVUXk.VU_44hCD2lp3YXGzJWIy3mwwsHQ8xr1TbnmQQrUNDj4 and paste the key here." Wait for the key.

5. Fill in the TINYFISH_API_KEY value in the MCP config. Save the file.

6. Tell the user to restart the agent. After restart, smart_fetch, smart_crawl, smart_search, screenshot, cache_clear and version should be available.
```

### For Pi agent users only

```
Install the Hound MCP server. Follow every step. Do not skip any.

1. Run: pip install hound-mcp[all]
   Then run: playwright install chromium (But only if it isnt installed already, verify first about its existence)
   If either fails, stop and tell the user.

2. Check pi-mcp-adapter: pi list. If not installed: pi install npm:pi-mcp-adapter

3. Backup ~/.pi/agent/mcp.json, then add this inside mcpServers:
   "hound": { "command": "hound", "transport": "stdio", "lifecycle": "eager", "env": { "TINYFISH_API_KEY": "" } }
   Leave the TINYFISH_API_KEY value empty for now (filled in step 5).

4. Ask the user: "Open this link to get a free TinyFish API key (no credit card): https://agent.tinyfish.ai/sign-up?ref=v1.dXNlcl8zRGVtcWxOT25nSU5aSkRzU0NLQXNtT1BVUXk.VU_44hCD2lp3YXGzJWIy3mwwsHQ8xr1TbnmQQrUNDj4 and paste the key here." Wait for the key.

5. Fill in the TINYFISH_API_KEY value. Save the file.

6. Tell the user: "Run /reload, then /mcp to verify. smart_fetch, smart_crawl and smart_search should be available."
```

---

## How it works

<div align="center">
<img src="https://raw.githubusercontent.com/dondai1234/master-fetch/master/docs/flow.svg" alt="smart_fetch pipeline: HTTP to stealthy escalation, extract, agent-optimized response; PDF and crawl branches" width="760">
</div>

- `smart_fetch` checks cache + robots, tries HTTP, escalates to the stealthy Patchright browser on a block/JS-shell/403/503, then extracts and enriches. PDFs branch to pdfplumber (OCR if scanned). `smart_crawl` reuses the same pipeline across a same-domain BFS walk.
- The stealthy browser is pre-warmed at startup and reused, so escalation is fast.
- Content over 40KB is chunked; the response gives `next_offset` so the agent pages through with one more call (served instantly from cache).

---

## Comparison: free tools

Hound is compared only to other **free** ways to give an agent web research. The free alternatives stop being comparable: none combine crawl + anti-bot + PDF/OCR + interaction + search + agent-optimized responses in one $0 local install. Only paid services (Bright Data, ZenRows, Firecrawl paid) can compete on hard anti-bot at scale, and they cost money, require accounts, and route your data through their servers.

| | **Hound** | Crawl4AI | Jina Reader | Firecrawl (OSS / free tier) | DIY Playwright |
|---|---|---|---|---|---|
| **Price** | $0 forever | $0 (self-host) | free, rate-limited | $0 self-host / 1K pages free | $0 (your time) |
| **License** | MIT (no attribution) | Apache-2.0 (attribution required) | proprietary | AGPL-ish / cloud | n/a |
| **Account / API key** | none (search: optional free key) | none | optional free key (20 RPM w/o, 200 RPM w/) | cloud needs account + key | none |
| **Runs locally** | yes | yes | no (their API) | self-host: yes (Redis + Docker) | yes |
| **Anti-bot / Cloudflare** | built-in (Patchright) | limited (stealth mode) | none | not by default | none |
| **Deep crawl (site walk)** | yes (BFS, depth/budget, map mode) | yes (deep crawl) | no | yes (cloud) | build it |
| **PDF → structured markdown** | yes (tables, headings, pages subset) | partial (buggy on some PDFs) | yes (native) | yes (cloud: + OCR) | build it |
| **Scanned-PDF / image OCR** | yes (rapidocr, pure-pip) | no | no | yes (cloud paid) | build it |
| **Page interaction (click/fill/scroll)** | yes (`actions`) | hooks (code) | no | yes (cloud `/interact`) | build it |
| **Query-focused extraction** | yes (`focus`, BM25) | yes (BM25 filter) | no | no | build it |
| **Web search** | yes (free key + research mode) | no | yes | no | no |
| **Metadata (OG/JSON-LD/date/author)** | yes | partial | partial | yes | build it |
| **Agent signals (`content_ok`/`next_action`/`summary`)** | yes | no | no | no | no |
| **Connect-time `instructions`** | yes | no | no | no | no |
| **MCP server** | yes (official) | community | yes (official) | yes (official) | build it |
| **Token cost (tools/list)** | ~2K (6 tools) | varies | n/a | varies (12 tools) | n/a |

**The takeaway:** Crawl4AI is the closest free competitor (self-hosted Python, local, no key, has crawl + BM25), but its anti-bot is basic stealth, it has no web search, no scanned-PDF OCR, no page interaction, and no agent-optimized response signals, and it's Apache-2.0 (attribution required) where Hound is MIT. Jina Reader is the easiest (prefix a URL) and handles PDFs, but has **no anti-bot, no crawl, no interaction**, routes everything through Jina's servers, and is rate-limited. Firecrawl's open-source version has no anti-bot by default and the generous features (crawl, interact, OCR, extract) live in the paid cloud. **Hound is the only free tool that combines crawl, built-in Cloudflare bypass, scanned-PDF OCR, page interaction, query-focused extraction, web search, and a response shape designed for agents**, all local, MIT, $0.

### When a paid service makes sense

Paid scrapers (Bright Data, ZenRows, Firecrawl paid, Spider.cloud) can beat free tools on the hardest anti-bot (DataDome, Akamai, Cloudflare Turnstile) and on massive scale, because they run large residential-proxy networks. They cost $16 to $500+/month, require accounts and API keys, and send your URLs and content through their servers. Use Hound when you want $0 local web research for an agent; reach for a paid service only for enterprise scale or sites Hound explicitly can't crack.

---

## Token cost

<div align="center">
<img src="https://raw.githubusercontent.com/dondai1234/master-fetch/master/docs/tokens.svg" alt="MCP tool count comparison: Hound 6 tools vs Firecrawl 12, Jina 19, Bright Data 60+" width="760">
</div>

Most MCP servers cost 3 to 5K tokens just to exist. Hound's 6 tools cost **~2K tokens**, and the connect-time `instructions` are injected once at handshake, not repeated every turn. Your context window is expensive; Hound respects it.

---

## Honest limits

Hound is honest about what it can't do (no free tool can):

- **DataDome, Akamai, Cloudflare Turnstile (interactive)**: not bypassed. If `smart_fetch` fails on one, the `next_action` tells the agent to switch sources instead of retrying.
- **Sites requiring login**: out of scope (Hound does page interaction, not authenticated sessions).
- **Shadow-DOM / hard SPAs**: `actions` (scroll, click, wait_selector) reach most of it; deep shadow-DOM piercing is not yet wired.
- **YouTube**: minimal text.

When a fetch fails, the response says exactly why and what to try next, so the agent doesn't waste calls guessing.

---

## For Pi agent users

See the Pi-specific install block above. After `/reload` and `/mcp`, `smart_fetch`, `smart_crawl`, and `smart_search` should be available.

---

<div align="center">

**MIT** · [GitHub](https://github.com/dondai1234/master-fetch) · [Changelog](CHANGELOG.md) · [Issues](https://github.com/dondai1234/master-fetch/issues)

*TinyFish links are referral links. I get a small credit when you sign up. Costs you nothing and helps me keep Hound free.*

</div>

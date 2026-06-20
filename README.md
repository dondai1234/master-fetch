<div align="center">

# <img src="https://raw.githubusercontent.com/dondai1234/master-fetch/master/docs/hound-logo.png" alt="hound" width="44"> Hound

<img src="https://raw.githubusercontent.com/dondai1234/master-fetch/master/docs/hound-hero.png" alt="Hound - web fetch, anti-bot, PDF and search for AI agents" width="896">

**Give your AI agent the web. $0. Two commands. ~1.3K tokens.**

Fetch any page, bypass bot walls, read PDFs, search the web.
No accounts. No Docker. No credit card. Runs on your machine.

[![PyPI](https://img.shields.io/pypi/v/hound-mcp.svg)](https://pypi.org/project/hound-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/hound-mcp.svg)](https://pypi.org/project/hound-mcp/)
[![License: MIT](https://img.shields.io/pypi/l/hound-mcp.svg)](LICENSE)
[![Downloads](https://img.shields.io/pypi/dm/hound-mcp.svg)](https://pypi.org/project/hound-mcp/)

```bash
pip install hound-mcp[all] && playwright install chromium
```

[Install](#install) · [Tools](#tools) · [Features](#features) · [How it works](#how-it-works) · [Comparison](#comparison-free-fetches) · [Honest limits](#honest-limits)

</div>

---

## What it is

Hound is an [MCP](https://modelcontextprotocol.io) server that gives any AI agent (Claude Code, Cursor, OpenCode, Hermes, Pi, anything that speaks MCP) full web access from a single local process: **fetch + anti-bot + PDF + search**. It is built so the agent masters it the moment it connects, and so every response tells the agent exactly what to do next.

It is **$0 forever**, MIT-licensed, and runs locally. No API keys for fetching, no per-request billing, no data sent to a third-party scraper (search uses a free key from TinyFish).

> Hound is for the agent itself. You install it once; the agent calls it whenever it needs the web.

---

## Why agents love it

Most MCP servers dump 3 to 5K tokens of tool schema into the agent's context just to exist, and return raw blobs the agent has to interpret. Hound is different:

- **Connect-time mastery.** On `initialize`, Hound sends a concise `instructions` block (paid once, not per turn) that gives the agent the 3-tool mental model, the #1 workflow (search → fetch the high-relevance results → synthesize), and the known limits. The agent is effective on turn one.
- **Every response is actionable.** Each fetch returns `content_ok` (trust the content only if true), `next_action` (the obvious next call: paginate, bypass robots, switch sources), `summary` (a one-line status), and `fetched_at`. Agents branch on structured fields instead of guessing from error text.
- **~1.3K tokens for all 5 tools.** Hand-crafted tool definitions, no Pydantic schema bloat.

---

## Tools

| Tool | What it does |
|------|--------------|
| `smart_fetch` | Fetch any URL. HTTP first, auto-escalates to the anti-detect browser if blocked. Bulk via `urls`. PDFs auto-extracted to structured markdown. Narrow with `css_selector`. Paginate with `offset`. |
| `smart_search` | Web search via TinyFish (free key). Each result carries `fetch_relevance` (high/med/low) so the agent knows which to fetch. |
| `screenshot` | Capture a page as an image. For multimodal agents (canvas, image-of-text, visual layout). Session auto-managed. |
| `cache_clear` | Clear the fetch cache. `all=true` wipes everything. |
| `version` | Installed version + update status. |

---

## Features

### 🛡️ Anti-bot, built in
`smart_fetch` tries plain HTTP first (fast, ~1s). If the site blocks HTTP or serves a JavaScript shell, it auto-escalates to a **Patchright** anti-detect browser with Cloudflare challenge solving. Two tiers, nothing to configure. JS-only SPAs that return an empty shell over HTTP are detected and escalated automatically.

### 📄 PDF extraction (flagship)
PDFs used to be a dead end. Now `smart_fetch` detects a PDF (by content-type **or** `%PDF` magic bytes) and extracts it to **structured markdown** using `pdfplumber` (MIT, no AGPL baggage):
- Multi-column reading order, real **tables as markdown tables**, **font-size heading detection**, de-hyphenated paragraphs.
- A metadata header (title, author, date, subject) so the agent can judge relevance before reading the body.
- `--- Page N ---` markers for citation.
- Pass `pages="1-5"` or `"1,3,5-7"` to extract a subset and save tokens on 500-page PDFs.
- Honest signals: scanned/image-only PDFs return `content_ok=false` with a clear "needs OCR" hint; encrypted PDFs accept a `password`.

### 🔎 Search that tells the agent what to fetch
`smart_search` returns results with a `fetch_relevance` tier (high/med/low) computed from query-term overlap and rank, plus a `fetch_hint` (`"2 high, 3 med, 5 low - fetch the 'high' results first"`). The agent fetches 1 to 2 results instead of all 10, saving tokens and time. Snippets are never enough on their own; the description tells the agent to always fetch the URL.

### 🐕 Reddit, optimized
Reddit URLs are auto-rewritten to old.reddit.com (7x smaller pages) and skip straight to the stealthy browser (www.reddit.com walls HTTP). Subreddit listings are parsed into structured posts (title, score, comments, author, domain) from canonical per-post data attributes, with promoted ads filtered out and sticky/NSFW posts tagged.

### ⚡ One warm browser, always ready
A single stealthy Chrome warms up when the server starts and stays alive for the whole session, so stealthy fetches and screenshots skip the 3 to 5s cold start. Only one browser instance ever runs; it closes cleanly when the agent harness closes. Pages are closed after each fetch and resource loading is dropped, so idle memory stays near the browser baseline.

### 💾 Smart caching
SQLite cache keyed by URL + extraction type + css_selector + pages. Uses the lesser of the stored and requested TTL (request a fresher window and you get a miss, not stale data). WAL mode for concurrent access. **Bad content is never cached** (JS shells, bot challenges, error statuses re-fetch instead of freezing broken pages in cache). A size cap evicts the oldest entries so a long-lived agent's cache can't grow unbounded.

---

## Install

```bash
pip install hound-mcp[all]      # fetch + search + PDF (recommended)
playwright install chromium     # the anti-detect browser engine
```

Lean install (fetch only, no search/PDF):

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

6. Tell the user to restart the agent. After restart, smart_fetch, smart_search, screenshot, cache_clear and version should be available.
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

6. Tell the user: "Run /reload, then /mcp to verify. smart_fetch and smart_search should be available."
```

---

## How it works

<div align="center">
<img src="https://raw.githubusercontent.com/dondai1234/master-fetch/master/docs/hound-scene.png" alt="Hound retrieving a page from the web" width="768">
</div>

```
smart_fetch(url)
   │
   ├─ robots.txt check (optional, off by default)
   ├─ cache check (SQLite, TTL-bounded)
   ├─ Reddit?  → rewrite to old.reddit.com, go straight to stealthy
   ├─ force_fetcher pinned? → that tier only
   └─ else auto-escalate:
         HTTP (curl_cffi, ~1s)  ──ok──►  extract → cache → chunk → respond
            │
            blocked / JS shell / 403 / 503
            ▼
         Stealthy (Patchright, Cloudflare solve)  ──ok──►  extract → cache → chunk → respond
```

- The stealthy browser is pre-warmed at startup and reused, so escalation is fast.
- Content over 40KB is chunked; the response gives `next_offset` so the agent pages through with one more call (served instantly from cache).
- PDFs branch off before the HTML pipeline and go through the pdfplumber extractor.

---

## Comparison: free fetches

Hound is compared only to other **free** ways to fetch a page for an agent. Free fetches get annihilated. Only paid services (Bright Data, ZenRows, Context.dev, Firecrawl paid) can compete on hard anti-bot at scale, and they cost money, require accounts, and route your data through their servers.

| | **Hound** | Crawl4AI | Jina Reader | Firecrawl (OSS / free tier) | DIY Playwright |
|---|---|---|---|---|---|
| **Price** | $0 forever | $0 (self-host) | free, rate-limited | $0 self-host / 1K pages free | $0 (your time) |
| **Account / API key** | none (search: optional free key) | none | optional free key (20 RPM w/o, 200 RPM w/) | cloud needs account + key | none |
| **Runs locally** | yes | yes | no (their API) | self-host: yes | yes |
| **Anti-bot / Cloudflare** | built-in (Patchright) | limited (stealth mode) | none | not by default | none |
| **PDF → structured markdown** | yes (tables, headings, pages subset) | partial (buggy on some PDFs) | yes (native) | yes (cloud: + OCR) | build it yourself |
| **Web search** | yes (free key) | no | yes | no | no |
| **Agent-optimized responses** (`content_ok`, `next_action`, `summary`) | yes | no | no | no | no |
| **Connect-time `instructions`** | yes | no | no | no | no |
| **MCP server** | yes (official) | community | yes (official) | yes (official) | build it |
| **Token cost (tools/list)** | ~1.3K | varies | n/a | varies | n/a |
| **Languages** | any (MCP) | Python only | any HTTP | Python/JS/Go/Rust/Ruby | Python |

**The takeaway:** Crawl4AI is the closest free competitor (self-hosted Python, local, no key), but its anti-bot is basic stealth (not robust Cloudflare bypass), it has no web search, and no agent-optimized response signals. Jina Reader is the easiest (prefix a URL) and does handle PDFs, but it has **no anti-bot**, routes everything through Jina's servers, and is single-page with a tight rate limit unless you grab a free key. Firecrawl's open-source version does not include anti-bot by default (you hit Cloudflare at scale) and the generous features live in the paid cloud. Hound is the only free option that combines **built-in Cloudflare bypass, local execution, no API key, PDF + search, and a response shape designed for agents** (every fetch returns `content_ok` / `next_action` / `summary`, and the server orients the agent at connect time).

### When a paid service makes sense

Paid scrapers (Bright Data, ZenRows, Context.dev, Firecrawl paid, Spider.cloud) can beat free tools on the hardest anti-bot (DataDome, Akamai, Cloudflare Turnstile) and on massive scale, because they run large residential-proxy networks. They cost $16 to $500+/month, require accounts and API keys, and send your URLs and content through their servers. Use Hound when you want $0 local web access for an agent; reach for a paid service only for enterprise scale or sites Hound explicitly can't crack.

---

## Token cost

Most MCP servers cost 3 to 5K tokens just to exist. Hound's 5 tools cost **~1.3K tokens**, and the connect-time `instructions` (~365 tokens) are injected once at handshake, not repeated every turn. Your context window is expensive; Hound respects it.

---

## Honest limits

Hound is honest about what it can't do (no free tool can):

- **DataDome, Akamai, Cloudflare Turnstile (interactive)**: not bypassed. If `smart_fetch` fails on one, the `next_action` tells the agent to switch sources instead of retrying.
- **Scanned / image-only PDFs**: detected and reported (needs OCR, which Hound doesn't do).
- **Sites requiring login**: out of scope.
- **YouTube**: minimal text.

When a fetch fails, the response says exactly why and what to try next, so the agent doesn't waste calls guessing.

---

## For Pi agent users

See the Pi-specific install block above. After `/reload` and `/mcp`, `smart_fetch` and `smart_search` should be available.

---

<div align="center">

**MIT** · [GitHub](https://github.com/dondai1234/master-fetch) · [Changelog](CHANGELOG.md) · [Issues](https://github.com/dondai1234/master-fetch/issues)

*TinyFish links are referral links. I get a small credit when you sign up. Costs you nothing and helps me keep Hound free.*

</div>

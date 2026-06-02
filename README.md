<div align="center">

# 🐕 Hound

**Give your AI agent the web. In 2 minutes. For $0.**

<p>
  <img src="https://img.shields.io/badge/cost-$0_forever-brightgreen" alt="$0 forever">
  <img src="https://img.shields.io/badge/bypass-Cloudflare-blue" alt="Cloudflare bypass">
  <img src="https://img.shields.io/badge/search-free_with_key-orange" alt="Free search with key">
  <img src="https://img.shields.io/badge/MCP-stdio-purple" alt="MCP stdio">
  <img src="https://img.shields.io/github/license/dondai1234/master-fetch" alt="MIT">
</p>

*Free MCP server. Fetch any page, search the web, bypass bot protection.*<br>
*No accounts. No Docker. No credit card.*

[Install now](#install) · [How it works](#how-it-works) · [Comparison](#comparison) · [Pi Agent setup](#for-pi-agent-users)

</div>

---

## What is this?

Hound is an MCP server that gives any AI agent web capabilities: fetch pages, bypass bot protection, search the web. You install it, tell your agent about it, and you're done. Two commands, one config entry.

It's built for people new to AI agents who just want their agent to read websites without paying for Exa or Tavily. If you've been copying URLs into chat boxes, this replaces that entirely.

**What you get:**
- Fetch any URL. If the site blocks simple HTTP, Hound automatically retries with a real browser. If that's blocked too, it switches to anti-bot stealth mode. You don't pick. It figures it out.
- Search the web. Free TinyFish API key required (sign up at [tinyfish.ai](https://tinyfish.ai), no credit card). Returns title, URL, and snippet for each result.
- Clean markdown output. Navigation, ads, and footers stripped automatically.
- Content caching. Same URL within an hour? Instant. No re-fetch.
- 40KB smart chunking. Large pages get truncated with a continuation offset so your agent can read the rest.
- Persistent browser sessions. The first dynamic/stealthy fetch takes a few seconds to launch the browser. After that, the browser stays open and every subsequent fetch on that session is fast.

**What you don't get (honest limits):**
- DataDome, Akamai Bot Manager, and Cloudflare Turnstile (interactive captcha): not bypassed. No free tool bypasses these. The next step up is a paid proxy service ($50+/mo).
- Reddit: first page of posts only. No infinite scroll.
- YouTube: minimal text content (it's a video platform, this is expected).

---

## Install

### Option A: Tell your AI agent to do it

Copy this prompt, paste it into your agent (OpenClaw, Hermes Agent, OpenCode, etc.):

**Full package (fetch + search):**
```
Install the Hound MCP server on this machine. Steps:

1. pip install hound-mcp[all]
2. playwright install chromium
3. Add to MCP config. Server name: "hound". Command: "hound". No arguments. Back up the config before editing.
4. Set up search: go to https://tinyfish.ai, click Get API Key, sign up (free, no credit card). Copy the key.
5. Add the TinyFish key as env var TINYFISH_API_KEY in the hound server's MCP config entry.
6. Done? Tell me to restart my agent.
```

**Fetch only (no search, no API key needed):**
```
Install the Hound MCP server (fetch only) on this machine. Steps:

1. pip install hound-mcp
2. playwright install chromium
3. Add to MCP config. Server name: "hound". Command: "hound". No arguments. Back up the config before editing.
4. Done? Tell me to restart my agent.
```

That's it. 2 commands + 1 config entry. Your agent handles the rest.

### Option B: Manual

```bash
# Install
pip install hound-mcp[all]        # fetch + search (requires TinyFish API key)
# or
pip install hound-mcp              # fetch only

# Browser (required for JS/anti-bot pages)
playwright install chromium
```

Add to your MCP config file:

```json
{
  "mcpServers": {
    "hound": {
      "command": "hound",
      "env": {
        "TINYFISH_API_KEY": "sk-tinyfish-..."
      }
    }
  }
}
```

Get your free TinyFish API key at [tinyfish.ai](https://tinyfish.ai). No credit card needed. Search will not work without this key.

Fetch-only users can omit the `env` block entirely.

### Updating

```bash
pip install --upgrade hound-mcp[all]
```

Restart your agent. MCP servers launch fresh each session, so the new version loads automatically.

---

## How it works

### Smart fetch: one tool, three tiers

`smart_fetch` tries the fastest method first. If blocked, it escalates automatically:

| Tier | Engine | Speed | When it's used |
|------|--------|-------|----------------|
| HTTP | curl_cffi (Chrome TLS fingerprint) | 1-3s | Most websites. Works for anything static. |
| Dynamic | Playwright + Chromium | 3-8s | JS-heavy pages (SPAs, React/Angular sites). |
| Stealthy | Patchright + Cloudflare solver | 5-13s | Pages behind bot protection. |

**How escalation works:**
1. Try HTTP. If the page returns a JS-only shell like "You need to enable JavaScript", escalate to dynamic.
2. Try dynamic. If the page returns a bot challenge or JS-disabled placeholder, escalate to stealthy.
3. Try stealthy. Full anti-bot bypass with fingerprint spoofing and Cloudflare solving.
4. If all tiers fail, return a clear error telling you what was tried.

**First fetch is slower, then it's fast:**
The dynamic and stealthy tiers need to launch a real browser. The first fetch on a fresh session takes 3-13 seconds to start Chromium. After that, the browser stays open and reused, so subsequent fetches are fast. Hound manages this automatically.

**What it remembers:**
- Domain intelligence: after fetching a domain, Hound remembers which tier works. Next time, it skips straight there.
- Content cache: SQLite, 1 hour TTL. Repeat fetches are instant.
- The `error` field signals content quality: `js_shell_detected`, `geo_redirect_detected`, `bot_challenge_detected`. Your agent knows the fetch failed without parsing the content.

### Smart search: web search via TinyFish

`smart_search` searches the web via TinyFish API. Requires a free API key set as `TINYFISH_API_KEY`. Returns title, URL, and snippet for each result. Results cached for 5 minutes.

### Content chunking

Large pages get truncated at 40KB with a continuation notice:

```
[Content truncated: received 40,000 of 60,000 chars (offset 0-40,000).
20,000 chars remaining. Call smart_fetch again with offset=40000 to get the next chunk.]
```

Your agent calls `smart_fetch` with `offset=40000` and gets the next chunk instantly from cache. No re-fetch needed.

---

## Tools

| Tool | What it does |
|------|-------------|
| `smart_fetch` | Fetch any URL. Auto-routes to the right tier. Start here. |
| `smart_search` | Search the web. Requires TINYFISH_API_KEY. |
| `get` / `bulk_get` | HTTP-only fetch. Fast, for known-static sites. |
| `fetch` / `bulk_fetch` | Dynamic fetch. Playwright browser, JS rendering. |
| `stealthy_fetch` / `bulk_stealthy_fetch` | Anti-bot fetch. Patchright + Cloudflare solver. |
| `screenshot` | Full-page screenshot of any URL (needs open session). |
| `open_session` / `close_session` / `list_sessions` | Persistent browser sessions. |
| `cache_clear` | Clear the content cache. |

---

## Comparison

### Hound vs paid tools (fetch capabilities)

| | Hound | Exa | Tavily | Bright Data MCP | Firecrawl |
|---|---|---|---|---|---|
| **Cost** | $0 forever | 1K/mo free, then $7/1K | 1K/mo free, then $8/credit | 5K/mo free, then paid | Self-hosted free, cloud from $19/mo |
| **Cloudflare bypass** | Built-in | No | No | Yes (proxy infra) | No |
| **Auto-escalation** | HTTP -> Browser -> Stealth | No | No | No | No |
| **Runs locally** | Yes | No (cloud API) | No (cloud API) | No (cloud API) | Yes (self-hosted) or cloud |
| **MCP native** | Yes (stdio) | No | No (remote MCP) | Yes (remote MCP) | Yes (remote MCP) |
| **Content caching** | SQLite, instant hits | No | No | No | No |
| **Domain memory** | Learns per-domain | No | No | No | No |
| **No account/signup** | Yes (fetch), key for search | API key required | API key required | API key required | API key or self-host |

The key difference: Hound runs on your machine with real browsers. Exa, Tavily, and Bright Data are cloud APIs. When they can't fetch a page, you're stuck. When Hound's HTTP tier fails, it opens a real Chromium browser on your hardware and tries again. Firecrawl can also run locally (self-hosted Docker) but has no auto-escalation or anti-bot bypass.

**Where paid tools win:** Bright Data has residential proxy infrastructure that bypasses DataDome and Akamai, which Hound cannot. Exa and Tavily have better search quality and scale at high volume. Firecrawl has advanced crawling (deep crawl, sitemap, map) that Hound doesn't. If you need 10K+ searches/day, enterprise anti-bot, or multi-page crawling, those are the right tools. For straightforward page fetching and web search, Hound covers it at $0.

### Hound vs free OSS alternatives

| | Hound | Crawl4AI | Jina Reader |
|---|---|---|---|
| **Anti-bot bypass** | Built-in (3-tier auto) | Stealth mode (basic, needs external proxies for protected sites) | No |
| **Cloudflare bypass** | Yes | Partial (needs proxy config) | No |
| **Web search** | Yes (TinyFish, free key) | No | No |
| **MCP native** | Yes (stdio, 0 config) | Docker MCP only | No |
| **All-in-one install** | `pip install` + one config entry | `pip install` + Docker for MCP | API call, no install |
| **Smart routing** | Auto (HTTP -> Browser -> Stealth) | Manual tier selection | Single HTTP fetch |
| **Content caching** | SQLite | In-memory only | No |
| **GitHub stars** | New | 50K+ | 10K+ |

Crawl4AI has way more features (LLM extraction, deep crawling, sitemap parsing, Docker dashboard, etc.). It's a power tool. Hound is a plug-and-play MCP server. If you need Crawl4AI's depth, use it. If you want your agent to just fetch pages and search the web without reading docs, Hound does that in 2 minutes.

---

## For Pi Agent Users

If you're running the [Pi coding agent](https://pi.dev), adding Hound is one step after the pip install.

### Setup

1. Install Hound:
```bash
pip install hound-mcp[all]
playwright install chromium
```

2. Install the MCP extension if you haven't:
```bash
pi install npm:pi-mcp-extension
```

3. Add Hound to `~/.pi/agent/mcp.json`:
```json
{
  "mcpServers": {
    "hound": {
      "command": "hound",
      "transport": "stdio",
      "lifecycle": "eager",
      "env": {
        "TINYFISH_API_KEY": "sk-tinyfish-..."
      }
    }
  }
}
```

Get your free TinyFish key at [tinyfish.ai](https://tinyfish.ai). Omit the `env` block if you only need fetch.

4. Run `/reload` in Pi. Check with `/mcp` -- you should see `hound` as connected.

### Prompt to give your agent

If your Pi agent doesn't have web capabilities yet, give it this prompt so it knows how to use Hound:

```
You now have Hound MCP tools for web access. Use them like this:

- To fetch any webpage: use mcp_hound_smart_fetch with the URL. It handles JS rendering and bot protection automatically. No need to pick a fetcher.
- To search the web: use mcp_hound_smart_search with your query. Returns titles, URLs, and snippets.
- If content is truncated, call smart_fetch again with the offset value from the truncation message to get the next chunk.
- For screenshots: open a session with mcp_hound_open_session (session_type="stealthy"), then use mcp_hound_screenshot with the session_id.

You do not need to ask me before fetching URLs or searching. Do it proactively when it would help answer my question.
```

---

## Requirements

- Python 3.11+
- Chromium (installed via `playwright install chromium`)
- Search: `pip install hound-mcp[all]` + free TinyFish API key

## License

MIT

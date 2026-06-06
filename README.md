# 🐕 Hound

**Give your AI agent the web. $0. One command.**

```
hound install
```

Fetch any page, search the web, bypass bot protection. No accounts, no Docker, no credit card.

[Commands](#commands) · [Tools](#tools) · [How it works](#how-it-works) · [Comparison](#comparison) · [Pi Agent](#for-pi-agent-users)

---

## Commands

```bash
hound install    # install everything: hound + Chromium
hound -v         # version + update status
hound -u         # update to latest
```

---

## Tell your agent to do it

Paste this into your agent (Claude Desktop, Pi, OpenCode, etc.):

**Fetch + search:**
```
Install the Hound MCP server on this machine:

1. Run: hound install
2. Add Hound to the MCP config. Server name: "hound", command: "hound", no args. Backup the config file first.
3. Ask me: "Get a free API key at tinyfish.ai (no credit card) and paste it here."
4. Add the key as TINYFISH_API_KEY env var in the hound MCP config.
5. Done. Restart your agent. smart_fetch and smart_search should be available.
```

**Fetch only (no API key):**
```
Install the Hound MCP server (fetch only) on this machine:

1. Run: hound install
2. Add Hound to the MCP config. Server name: "hound", command: "hound", no args. Backup the config file first.
3. Done. Restart your agent. smart_fetch should be available.
```

### MCP config reference

```json
{
  "mcpServers": {
    "hound": {
      "command": "hound",
      "env": { "TINYFISH_API_KEY": "sk-tinyfish-..." }
    }
  }
}
```

---

## Tools

| Tool | Does |
|------|------|
| `smart_fetch` | Fetch any URL. Auto HTTP → browser → stealth escalation. Use `urls` for bulk. |
| `smart_search` | Web search via TinyFish. Free key required. |
| `screenshot` | Full-page screenshot via open session. |
| `open_session` / `close_session` | Browser session management for screenshot reuse. |
| `cache_clear` | Clear fetch cache. Set `all=true` for full clear. |
| `version` | Check installed version + availability of updates. |

---

## How it works

`smart_fetch` tries the fastest method. Escalates if blocked:

| Tier | Engine | Speed | Used for |
|------|--------|-------|----------|
| HTTP | curl_cffi (Chrome TLS) | 1-3s | Most sites |
| Dynamic | Playwright + Chromium | 3-8s | JS-heavy pages |
| Stealthy | Patchright + Cloudflare solver | 5-13s | Bot protection |

First browser launch takes a few seconds. After that the session stays open and subsequent fetches are fast. Hound remembers which tier works per domain and caches results (SQLite, 1hr TTL).

Content over 40KB gets chunked with a continuation offset. Your agent can call again to get the rest, instantly from cache.

**Honest limits:** DataDome, Akamai, Cloudflare Turnstile (interactive): no free tool bypasses these. Reddit new design: first page only. YouTube: minimal text.

---

## Comparison

### Runs on your machine (install once, $0 forever)

| | Fetch pages | Anti-bot | Search | Account needed |
|---|---|---|---|---|
| **Hound** | 3-tier auto | Cloudflare + bot walls | TinyFish (free key) | No (fetch), key for search |
| **Crawl4AI** | Playwright | Stealth mode (basic) | No | No |
| **Firecrawl** (self) | HTTP + browser | No | Yes | API key |

### Cloud APIs (free tiers, then pay)

| | Fetch pages | Anti-bot | Search | Free limit |
|---|---|---|---|---|
| **Bright Data** | Scrape + unlocker | Proxy infra | Yes | 5K req/mo |
| **Exa** | /contents | No | Yes | 1K req/mo |
| **Tavily** | /extract | No | Yes | 1K credits/mo |
| **Firecrawl** (cloud) | HTTP + browser | Yes | Yes | 1K pages/mo |
| **Jina Reader** | URL→markdown | No | No | Free tier |

---

## For Pi Agent Users

Paste this into Pi:

```
Install the Hound MCP server:

1. Run: hound install
2. Check pi-mcp-adapter: pi list. If not installed: pi install npm:pi-mcp-adapter
3. Add to ~/.pi/agent/mcp.json inside mcpServers:

   "hound": { "command": "hound", "transport": "stdio", "lifecycle": "eager" }

   Backup the file first.
4. For search: get a free key at tinyfish.ai (no credit card), then add as env:

   "hound": { "command": "hound", "transport": "stdio", "lifecycle": "eager", "env": { "TINYFISH_API_KEY": "<key>" } }

5. Done. Run /reload, then /mcp. smart_fetch and smart_search should be available.
```

---

MIT

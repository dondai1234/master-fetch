# 🐕 Hound

**Give your AI agent the web. 2 steps. $0.**

```
pip install hound-mcp[all]
playwright install chromium
```

Fetch any page, search the web, bypass bot protection. No accounts, no Docker, no credit card.

[Install](#install) · [Tools](#tools) · [How it works](#how-it-works) · [Comparison](#comparison) · [Pi Agent](#for-pi-agent-users)

---

## Install

*Just want it to work? Copy the AI prompt below, give it to your agent, done.*

### Tell your agent to do it

Paste this into your agent (OpenClaw, Hermes Agent, Pi, OpenCode, etc.):

**Fetch + search:**
```
Install the Hound MCP server on this machine. Steps:

1. Run: pip install hound-mcp[all]
2. Run: playwright install chromium
3. Add Hound to the MCP config on this machine. The server name is "hound". The command is "hound" with no arguments. Create a backup of the config file before editing.
4. After setup, tell me: "Go to https://tinyfish.ai, click Get API Key, sign up (free, no credit card needed). Copy the key and paste it here."
5. Wait for me to paste the key. Do not proceed until I provide it.
6. Once I give you the key, add it as TINYFISH_API_KEY env var in the hound server's MCP config.
7. Tell me: "Done. Restart your agent. You should see smart_fetch and smart_search in your tools."
```

**Fetch only (no API key):**
```
Install the Hound MCP server (fetch only) on this machine. Steps:

1. Run: pip install hound-mcp
2. Run: playwright install chromium
3. Add Hound to the MCP config on this machine. The server name is "hound". The command is "hound" with no arguments. Create a backup of the config file before editing.
4. Tell me: "Done. Restart your agent. You should see smart_fetch in your tools."
```

### Manual install

```bash
pip install hound-mcp[all]        # fetch + search (needs TinyFish key)
pip install hound-mcp              # fetch only
playwright install chromium        # browser for JS/anti-bot pages
```

MCP config:
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

Get a free TinyFish key at [tinyfish.ai](https://tinyfish.ai) (no credit card). Skip the `env` block for fetch-only.

### Update
```bash
pip install --upgrade hound-mcp[all]
```

---

## Tools

| Tool | Does |
|------|------|
| `smart_fetch` | Fetch any URL. Auto HTTP → browser → stealth escalation. Start here. |
| `smart_search` | Web search. Free TinyFish key required. |
| `get` / `fetch` / `stealthy_fetch` | Manual tier selection. `bulk_*` variants for parallel. |
| `screenshot` | Full-page screenshot via open session. |
| `open_session` / `close_session` | Persistent browser sessions. |

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

Paste this into Pi to install Hound:

```
Install the Hound MCP server for web fetching and search. Steps:

1. Run: pip install hound-mcp[all]
2. Run: playwright install chromium
3. Check if pi-mcp-adapter is installed: pi list. If not: pi install npm:pi-mcp-adapter
4. Add to ~/.pi/agent/mcp.json inside mcpServers:

   "hound": { "command": "hound", "transport": "stdio", "lifecycle": "eager" }

   Back up the file first. If mcpServers doesn't exist, create the full structure:
   { "mcpServers": { "hound": { "command": "hound", "transport": "stdio", "lifecycle": "eager" } } }

5. Tell me: "Go to https://tinyfish.ai, click Get API Key, sign up (free, no credit card). Copy the key and paste it here."
6. Wait for me to paste the TinyFish key.
7. Add the key as env in the hound entry:

   "hound": { "command": "hound", "transport": "stdio", "lifecycle": "eager", "env": { "TINYFISH_API_KEY": "<the key>" } }

8. Tell me: "Done. Run /reload, then /mcp. You should see smart_fetch and smart_search."
```

Fetch only:
```
Install the Hound MCP server (fetch only). Steps:

1. Run: pip install hound-mcp
2. Run: playwright install chromium
3. Check pi-mcp-adapter: pi list. If not: pi install npm:pi-mcp-adapter
4. Add to ~/.pi/agent/mcp.json inside mcpServers:

   "hound": { "command": "hound", "transport": "stdio", "lifecycle": "eager" }

   Back up the file first.
5. Tell me: "Done. Run /reload, then /mcp. smart_fetch should be available."
```

---

## Requirements

Python 3.11+ · Chromium (`playwright install chromium`) · Search: `pip install hound-mcp[all]` + free TinyFish key

MIT

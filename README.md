# 🐕 Hound

**Give your AI agent the web. $0. Two commands.**

```
pip install hound-mcp[all] && hound install
```

Fetch any page, search the web, bypass bot protection. No accounts, no Docker, no credit card.

[Commands](#commands) · [Tools](#tools) · [How it works](#how-it-works) · [Comparison](#comparison) · [Pi Agent](#for-pi-agent-users)

---

## Commands

```bash
pip install hound-mcp[all] && hound install    # full setup (package + Chromium)
hound -v         # version + update status
hound -u         # update to latest
```

---

## Tell your agent to do it

Paste this into your agent:

```
Install the Hound MCP server on this machine. Follow every step. Do not skip any.

1. Run: pip install hound-mcp[all]
   Then run: hound install
   If either fails, stop and tell the user.

2. Find your MCP config file and back it up before editing.
   Add a new MCP server named "hound" with command "hound", no arguments.
   Include an environment variable TINYFISH_API_KEY (leave value empty for now).

3. Ask the user: "Open this link to get a free TinyFish API key (no credit card): https://agent.tinyfish.ai/sign-up?ref=v1.dXNlcl8zRGVtcWxON25nSU5aSkRzU0NLQXNtT1BVUXk.VU_44hCD2lp3YXGzJWIy3mwwsHQ8xr1TbnmQQrUNDj4 and paste the key here."
   Wait for the user to respond with the key.

4. Fill in the TINYFISH_API_KEY value in the MCP config with the key the user gave you. Save the file.

5. Tell the user to restart this agent. After restart, smart_fetch and smart_search should be available.
```

---

## Tools

| Tool | Does |
|------|------|
| `smart_fetch` | Fetch any URL. Auto HTTP → browser → stealth escalation. Use `urls` for bulk. |
| `smart_search` | Web search via TinyFish. Free key required. |
| `screenshot` | Full-page screenshot via open session. |
| `open_session` / `close_session` / `list_sessions` | Browser session management. |
| `cache_clear` | Clear fetch cache. Set `all=true` for full clear. |
| `version` | Check installed version and whether an update is available. |

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

| | Fetch pages | Anti-bot | Search | Free? |
|---|---|---|---|---|
| **Hound** | 3-tier auto | Cloudflare + bot walls | Yes (TinyFish) | Yes |
| **Crawl4AI** | Playwright | Stealth mode (basic) | No | Yes |
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

Only for Pi agent. If you use a different agent, use the generic instructions above.

Paste this into Pi:

```
Install the Hound MCP server. Follow every step. Do not skip any.

1. Run: pip install hound-mcp[all]
   Then run: hound install
   If either fails, stop and tell the user.

2. Check pi-mcp-adapter: pi list. If not installed: pi install npm:pi-mcp-adapter

3. Backup ~/.pi/agent/mcp.json, then add this inside mcpServers:
   "hound": { "command": "hound", "transport": "stdio", "lifecycle": "eager", "env": { "TINYFISH_API_KEY": "" } }
   Leave the TINYFISH_API_KEY value empty for now (filled in step 5).

4. Ask the user: "Open this link to get a free TinyFish API key (no credit card): https://agent.tinyfish.ai/sign-up?ref=v1.dXNlcl8zRGVtcWxON25nSU5aSkRzU0NLQXNtT1BVUXk.VU_44hCD2lp3YXGzJWIy3mwwsHQ8xr1TbnmQQrUNDj4 and paste the key here."
   Wait for the user to respond with the key.

5. Fill in the TINYFISH_API_KEY value in the MCP config with the key the user gave you. Save the file.

6. Tell the user: "Run /reload, then /mcp to verify. smart_fetch and smart_search should be available."
```

---

*TinyFish links are referral links. I get a small credit when you sign up. Costs you nothing.*

MIT

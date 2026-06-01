# Master Fetch

[![PyPI version](https://img.shields.io/pypi/v/master-fetch)](https://pypi.org/project/master-fetch/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An MCP server that actually fetches web pages. Bypasses Cloudflare, DataDome, and Akamai. Extracts clean content. Runs locally. Costs nothing.

If you've ever tried to fetch a URL from an AI agent and got a blank page, a captcha, or an error — this is for you.

## Quick Start

```bash
pip install master-fetch
master-fetch --http --port 8000
```

Your agent can now fetch pages behind bot protection. No API keys. No accounts. No credit card.

## Why This Exists

I built this because every web fetch tool for AI agents has the same two problems:

1. They can't handle sites with bot protection (Cloudflare, DataDome, etc.)
2. They cost money (Exa: $100/mo, Tavily: limited free tier, Firecrawl: $19/mo)

Master Fetch is free, self-hosted, and handles sites that paid tools can't touch.

## Comparison

| Site | Protection | Master Fetch | Exa | Tavily |
|------|-----------|-------------|-----|--------|
| example.com | None | ✅ | ✅ | ✅ |
| Fandom (JS-heavy) | None | ✅ 42K chars | ❌ | ❌ |
| Reddit | JS-rendered | ✅ | ❌ | ❌ |
| BBC News | Medium | ✅ | ❌ | ❌ |
| Cloudflare-protected | Turnstile | ✅ | ❌ | ❌ |

Full comparison in [COMPARISON_REPORT.md](TEST/COMPARISON_REPORT.md).

## How It Works

Smart routing. Master Fetch tries the fastest method first, then escalates:

1. **HTTP** (curl_cffi, TLS fingerprint impersonation). Fastest. Good for most sites.
2. **Dynamic** (Playwright/Chromium). For JavaScript-rendered content and SPAs.
3. **Stealthy** (Patchright, Cloudflare solver). For sites with anti-bot protection.

Pages get extracted through Trafilatura — clean markdown, no nav bars, no cookie banners, no ads. Results are cached in SQLite so repeated requests are instant.

## MCP Tools

| Tool | What it does |
|------|-------------|
| `smart_fetch` | Auto-routed fetch. Tries HTTP first, escalates if needed. |
| `fetch` | HTTP-level fetch with browser TLS impersonation |
| `stealthy_fetch` | Full stealth fetch with Cloudflare bypass |
| `bulk_fetch` | Fetch multiple URLs in parallel |
| `bulk_stealthy_fetch` | Stealthy parallel fetch |
| `screenshot` | Take a screenshot of any page |
| `open_session` | Open a persistent browser session |
| `close_session` | Close a session |
| `list_sessions` | List active sessions |
| `cache_clear` | Clear the content cache |

## Connecting Your Agent

### With Pi Agent / Hermes Agent

```json
{
  "mcpServers": {
    "master-fetch": {
      "command": "master-fetch",
      "args": ["--http", "--port", "8000"]
    }
  }
}
```

### With Claude Code

```json
{
  "mcpServers": {
    "master-fetch": {
      "command": "master-fetch"
    }
  }
}
```

### With OpenClaw / Codex

Same pattern — point your agent's MCP config at the `master-fetch` command.

## Requirements

- Python 3.11 or newer
- Chromium or Chrome browser (auto-installed on first run)
- That's it. No API keys. No Docker. No accounts.

## Limits

This is what it CAN'T do (and neither can anything else at this price):

- DataDome + Cloudflare dual protection (g2.com blocks everything)
- Reddit infinite scroll (only gets first-load content)
- Domain extraction for .co.uk / .com.au (simple heuristic, works for most)

If you need those, you're looking at paid residential proxy rotation — which nobody offers for free.

## Why Not Just Use Scrapling Directly?

Scrapling is the engine. Master Fetch wraps it with:

- Smart auto-escalation (you don't pick the fetcher, the tool does)
- Domain intelligence (remembers which sites need which protection level)
- Trafilatura extraction (cleaner output than Scrapling's built-in converter)
- SQLite caching (repeat requests are instant)
- Ready-to-use MCP server (no code needed)

If you want full control, use Scrapling directly. If you want "fetch this URL and give me clean content," use Master Fetch.

## License

MIT. Do whatever you want with it.

## Author

Built by [Bishesh Bhandari](https://github.com/dondai1234). I use this daily with my own AI agents. If it breaks, I feel it too.

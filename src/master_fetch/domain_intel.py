"""Domain intelligence:

Stores per-domain protection level so smart_fetch can auto-escalate.
Levels: "none" (plain HTTP works), "low" (dynamic needed), "high" (stealthy needed).
"""
import json
import time
from pathlib import Path

import aiosqlite

_CACHE_DIR = Path.home() / ".master_fetch_cache"
_DB_NAME = "domains.db"

PROTECTION_LEVELS = ("none", "low", "high")
# Domains known to be static (HTTP works fine). Prevents over-escalation.
_KNOWN_SAFE_DOMAINS = {
    "httpbin.org", "example.com", "wikipedia.org", "wikimedia.org",
    "github.com", "gist.github.com", "pypi.org", "npmjs.com",
    "stackoverflow.com", "serverfault.com", "superuser.com",
    "docs.python.org", "docs.rs", "pkg.go.dev",
    "jsonplaceholder.typicode.com", "httpstat.us",
    "news.ycombinator.com", "lobste.rs",
    "raw.githubusercontent.com",
}
# Domains known to need stealth (anti-bot blocks even dynamic/Playwright)
_KNOWN_STEALTHY_DOMAINS = {
    "cloudflare.com", "nowsecure.nl", "nopecha.com",
    "bot.sannysoft.com", "abrahamjuliot.github.io",
    "datadome.co", "kasada.io", "arkoselabs.com",
    "perimeterx.com", "shape.com", "f5.com",
    "twitter.com", "x.com",
}
# Domains known to need dynamic (JS rendering) but not full stealth
_KNOWN_DYNAMIC_DOMAINS = {
    "reddit.com", "facebook.com", "instagram.com",
    "linkedin.com", "tiktok.com",
    "youtube.com", "youtu.be",
    "uniswap.org", "app.uniswap.org",
    "opensea.io", "dune.com",
    "notion.so", "notion.site",
    "figma.com", "airtable.com",
    "vercel.app", "netlify.app",
    "spotify.com", "open.spotify.com",
}


# Two-part TLDs where we need 3 domain parts, not 2
_MULTI_PART_TLDS = {
    "co.uk", "com.au", "net.au", "org.au", "co.nz", "net.nz", "org.nz",
    "co.jp", "ac.uk", "gov.uk", "org.uk", "me.uk", "net.uk", "sch.uk",
    "co.za", "web.za", "co.in", "net.in", "org.in", "firm.in", "gen.in",
    "com.br", "org.br", "net.br", "gov.br", "com.cn", "net.cn", "org.cn",
}


def _extract_domain(url: str) -> str:
    """Extract registered domain from URL, handling multi-part TLDs."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        parts = host.split(".")
        if len(parts) <= 1:
            return host
        # Check for two-part TLDs like .co.uk or .com.au
        if len(parts) >= 3:
            tld_candidate = ".".join(parts[-2:])
            if tld_candidate in _MULTI_PART_TLDS:
                return ".".join(parts[-3:])
        return ".".join(parts[-2:])
    except Exception:
        return url


async def _ensure_db() -> Path:
    d = _CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    db_path = d / _DB_NAME

    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS domain_intel (
                domain TEXT PRIMARY KEY,
                protection_level TEXT NOT NULL DEFAULT 'none',
                avg_response_ms REAL,
                hit_count INTEGER NOT NULL DEFAULT 0,
                fail_count INTEGER NOT NULL DEFAULT 0,
                last_seen REAL NOT NULL
            )
        """)
        await db.commit()

    return db_path


def guess_protection_level(url: str) -> str:
    """Guess protection level from known domain lists."""
    domain = _extract_domain(url)
    if domain in _KNOWN_SAFE_DOMAINS:
        return "none"
    if domain in _KNOWN_STEALTHY_DOMAINS:
        return "high"
    if domain in _KNOWN_DYNAMIC_DOMAINS:
        return "low"
    return "none"


async def get_domain_level(url: str) -> str:
    """Get stored protection level for a URL's domain, or guess.

    Known domains (safe, stealthy, dynamic) always take priority over
    learned DB records. Only unknown domains use the learned behavior.
    """
    domain = _extract_domain(url)

    # Known domains always win — no amount of learning overrides them
    if domain in _KNOWN_SAFE_DOMAINS:
        return "none"
    if domain in _KNOWN_STEALTHY_DOMAINS:
        return "high"
    if domain in _KNOWN_DYNAMIC_DOMAINS:
        return "low"

    # Unknown domain: check if we've learned anything
    db_path = await _ensure_db()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT protection_level FROM domain_intel WHERE domain = ?", (domain,)
        )
        row = await cursor.fetchone()
        if row:
            return row["protection_level"]

    return "none"


async def record_result(url: str, level: str, success: bool, response_ms: float = 0) -> None:
    """Record fetch result for future smart routing decisions."""
    domain = _extract_domain(url)
    db_path = await _ensure_db()

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        # Check if exists
        cursor = await db.execute(
            "SELECT hit_count, fail_count, avg_response_ms FROM domain_intel WHERE domain = ?",
            (domain,),
        )
        row = await cursor.fetchone()

        if row is None:
            await db.execute(
                """INSERT INTO domain_intel (domain, protection_level, avg_response_ms, hit_count, fail_count, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (domain, level, response_ms, 1 if success else 0, 0 if success else 1, time.time()),
            )
        else:
            hits = row["hit_count"] + (1 if success else 0)
            fails = row["fail_count"] + (0 if success else 1)
            old_avg = row["avg_response_ms"] or 0
            new_avg = (old_avg * row["hit_count"] + response_ms) / max(hits, 1) if response_ms else old_avg
            # If we failed with current level, upgrade protection
            new_level = level
            if not success and level == "none":
                new_level = "low"
            elif not success and level == "low":
                new_level = "high"
            # If we succeeded with stealthy many times, consider downgrading
            elif success and level == "high" and hits > 5 and fails == 0:
                new_level = "low"

            await db.execute(
                """UPDATE domain_intel SET protection_level=?, avg_response_ms=?, hit_count=?, fail_count=?, last_seen=?
                   WHERE domain=?""",
                (new_level, new_avg, hits, fails, time.time(), domain),
            )
        await db.commit()

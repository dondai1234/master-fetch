"""Domain intelligence — track which domains need which fetcher level.

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
# Domains known to need stealth
_KNOWN_STEALTHY_DOMAINS = {
    "cloudflare.com", "nowsecure.nl", "nopecha.com",
    "bot.sannysoft.com", "abrahamjuliot.github.io",
    "datadome.co", "kasada.io", "arkoselabs.com",
    "perimeterx.com", "shape.com", "f5.com",
}
# Domains known to need dynamic (JS rendering) but not full stealth
_KNOWN_DYNAMIC_DOMAINS = {
    "twitter.com", "x.com", "reddit.com", "facebook.com",
    "instagram.com", "linkedin.com", "tiktok.com",
}


def _extract_domain(url: str) -> str:
    """Extract registered domain from URL."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        # Simple: take last two parts for most domains
        parts = host.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return host
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
    if domain in _KNOWN_STEALTHY_DOMAINS:
        return "high"
    if domain in _KNOWN_DYNAMIC_DOMAINS:
        return "low"
    return "none"


async def get_domain_level(url: str) -> str:
    """Get stored protection level for a URL's domain, or guess."""
    domain = _extract_domain(url)
    db_path = await _ensure_db()

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT protection_level FROM domain_intel WHERE domain = ?", (domain,)
        )
        row = await cursor.fetchone()
        if row:
            return row["protection_level"]

    # No record — guess from known lists
    return guess_protection_level(url)


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

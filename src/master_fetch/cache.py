"""SQLite-based content cache with TTL support.

Stores fetched content keyed by URL+params hash. Auto-expires entries past TTL.
"""
import hashlib
import json
import time
from pathlib import Path

import aiosqlite

# Default cache dir: next to the project
_CACHE_DIR = Path.home() / ".master_fetch_cache"
_DB_NAME = "cache.db"

DEFAULT_TTL = 3600  # 1 hour


def _cache_key(url: str, extraction_type: str, css_selector: str | None = None) -> str:
    """Deterministic cache key from fetch params."""
    raw = f"{url}|{extraction_type}|{css_selector or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


async def _ensure_db(cache_dir: Path | None = None) -> Path:
    """Ensure the DB and table exist. Returns DB path."""
    d = cache_dir or _CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    db_path = d / _DB_NAME

    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                extraction_type TEXT NOT NULL,
                content TEXT NOT NULL,
                status INTEGER NOT NULL,
                fetched_at REAL NOT NULL,
                ttl INTEGER NOT NULL DEFAULT 3600
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_fetched_at ON cache(fetched_at)")
        await db.commit()

    return db_path


async def get_cached(
    url: str,
    extraction_type: str,
    css_selector: str | None = None,
    ttl: int = DEFAULT_TTL,
    cache_dir: Path | None = None,
) -> dict | None:
    """Return cached response if fresh, else None."""
    key = _cache_key(url, extraction_type, css_selector)
    db_path = await _ensure_db(cache_dir)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM cache WHERE key = ? AND fetched_at + ttl > ?",
            (key, time.time()),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "status": row["status"],
            "content": json.loads(row["content"]),
            "url": row["url"],
        }


async def set_cached(
    url: str,
    extraction_type: str,
    content: list[str],
    status: int,
    css_selector: str | None = None,
    ttl: int = DEFAULT_TTL,
    cache_dir: Path | None = None,
) -> None:
    """Store a response in cache."""
    key = _cache_key(url, extraction_type, css_selector)
    db_path = await _ensure_db(cache_dir)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT OR REPLACE INTO cache (key, url, extraction_type, content, status, fetched_at, ttl)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (key, url, extraction_type, json.dumps(content), status, time.time(), ttl),
        )
        await db.commit()


async def clear_cache(cache_dir: Path | None = None) -> int:
    """Clear all expired entries. Returns count of purged rows."""
    db_path = await _ensure_db(cache_dir)
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "DELETE FROM cache WHERE fetched_at + ttl <= ?", (time.time(),)
        )
        await db.commit()
        return cursor.rowcount


async def clear_all_cache(cache_dir: Path | None = None) -> int:
    """Nuke the entire cache. Returns count of purged rows."""
    db_path = await _ensure_db(cache_dir)
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("DELETE FROM cache")
        await db.commit()
        return cursor.rowcount

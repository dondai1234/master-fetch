"""SQLite-based content cache with TTL support.

Stores fetched content keyed by URL+params hash. Auto-expires entries past TTL.
Uses a shared DB connection pool for efficiency instead of opening a new
connection per operation.
"""
import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import aiosqlite

# Default cache dir: next to the project
_CACHE_DIR = Path.home() / ".master_fetch_cache"
_DB_NAME = "cache.db"

DEFAULT_TTL = 3600  # 1 hour
MAX_CACHE_ENTRIES = 10000  # hard cap so a long-lived agent's cache DB can't grow unbounded

# Shared DB path cache — avoids re-running PRAGMA on every operation
_db_initialized: dict[Path, bool] = {}
_db_init_lock = asyncio.Lock()


def _cache_key(url: str, extraction_type: str, css_selector: str | None = None, pages: str | None = None) -> str:
    """Deterministic cache key from fetch params."""
    raw = f"{url}|{extraction_type}|{css_selector or ''}|{pages or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


async def _ensure_db(cache_dir: Path | None = None) -> Path:
    """Ensure the DB and table exist. Returns DB path.

    Caches initialization status to avoid redundant PRAGMA calls.
    WAL journal mode for better concurrent read/write performance.
    Busy timeout to handle lock contention gracefully.
    Lock-protected to prevent races during concurrent first-access.
    """
    d = cache_dir or _CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    db_path = d / _DB_NAME

    # Fast path: already initialized, no lock needed
    if _db_initialized.get(db_path):
        return db_path

    async with _db_init_lock:
        # Re-check after acquiring lock (another task may have initialized)
        if _db_initialized.get(db_path):
            return db_path

        async with aiosqlite.connect(db_path) as db:
            # WAL mode: readers don't block writers, writers don't block readers.
            await db.execute("PRAGMA journal_mode=WAL")
            # Wait up to 5s if DB is locked by another connection.
            await db.execute("PRAGMA busy_timeout=5000")
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

            # v3.5.3 schema upgrade: add content_type and total_size_bytes columns.
            # Idempotent — ALTER TABLE raises "duplicate column" if already added.
            # SQLite reuses the same error class as aiosqlite wraps; we catch via sqlite3.
            for ddl in (
                "ALTER TABLE cache ADD COLUMN content_type TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE cache ADD COLUMN total_size_bytes INTEGER NOT NULL DEFAULT 0",
            ):
                try:
                    await db.execute(ddl)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc):
                        raise

            await db.commit()

        _db_initialized[db_path] = True
        return db_path


async def get_cached(
    url: str,
    extraction_type: str,
    css_selector: str | None = None,
    ttl: int = DEFAULT_TTL,
    cache_dir: Path | None = None,
    pages: str | None = None,
) -> dict | None:
    """Return cached response if fresh, else None.

    Uses the *lesser* of the stored TTL and the caller-requested TTL.
    This prevents serving stale cache when caller wants a fresher window.
    """
    key = _cache_key(url, extraction_type, css_selector, pages)
    db_path = await _ensure_db(cache_dir)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        # Use MIN(stored_ttl, requested_ttl) so caller can request fresher data
        cursor = await db.execute(
            "SELECT * FROM cache WHERE key = ? AND fetched_at + MIN(ttl, ?) > ?",
            (key, ttl, time.time()),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "status": row["status"],
            "content": json.loads(row["content"]),
            "url": row["url"],
            "content_type": row["content_type"],
            "total_size_bytes": row["total_size_bytes"],
        }


async def set_cached(
    url: str,
    extraction_type: str,
    content: list[str],
    status: int,
    css_selector: str | None = None,
    ttl: int = DEFAULT_TTL,
    cache_dir: Path | None = None,
    content_type: str = "",
    total_size_bytes: int = 0,
    pages: str | None = None,
) -> None:
    """Store a response in cache.

    v3.5.3+: content_type and total_size_bytes round-trip through cache so
    agents preserve MIME info on hits instead of always seeing empty/0.
    """
    key = _cache_key(url, extraction_type, css_selector, pages)
    db_path = await _ensure_db(cache_dir)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT OR REPLACE INTO cache
               (key, url, extraction_type, content, status, fetched_at, ttl,
                content_type, total_size_bytes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, url, extraction_type, json.dumps(content), status,
             time.time(), ttl, content_type, total_size_bytes),
        )
        # Bound the cache: if over MAX_CACHE_ENTRIES, evict the oldest rows by
        # fetched_at down to 90% of the cap. Cheaper than per-insert single
        # evictions and amortizes the cost (only runs when the cap is exceeded).
        count_cursor = await db.execute("SELECT COUNT(*) FROM cache")
        (count,) = await count_cursor.fetchone()
        if count > MAX_CACHE_ENTRIES:
            excess = count - int(MAX_CACHE_ENTRIES * 0.9)
            await db.execute(
                "DELETE FROM cache WHERE key IN "
                "(SELECT key FROM cache ORDER BY fetched_at ASC LIMIT ?)",
                (excess,),
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

"""Web search for Master Fetch via TinyFish API (free, 30/min).

No API key needed. Structured JSON results. Cached for 5 minutes.
"""

import asyncio
import json
import logging
import os
from time import time
from urllib.parse import quote

from pydantic import BaseModel, Field

from master_fetch.cache import get_cached, set_cached

logger = logging.getLogger("master-fetch.search")

TINYFISH_API = "https://api.search.tinyfish.ai"
TINYFISH_TIMEOUT = 12
SEARCH_CACHE_TTL = 300  # 5 minutes


class SearchResult(BaseModel):
    title: str = Field(description="Result title.")
    url: str = Field(description="Result URL.")
    snippet: str = Field(default="", description="Result snippet/description.")
    source: str = Field(default="tinyfish", description="Source name.")
    position: int = Field(default=0, description="Position in results (1-indexed).")


class SearchResponseModel(BaseModel):
    query: str = Field(description="The search query.")
    results: list[SearchResult] = Field(description="Search results.")
    total_results: int = Field(default=0, description="Total number of results found.")
    cached: bool = Field(default=False, description="Whether served from cache.")
    duration_ms: float = Field(default=0, description="Search duration in ms.")
    error: str = Field(default="", description="Error message if search failed.")


def _get_requests():
    """Lazy import requests (optional dependency)."""
    try:
        import requests as _requests
        return _requests
    except ImportError:
        raise ImportError("requests not installed. Run: pip install master-fetch[all]")


async def _tinyfish_search(query: str, max_results: int = 10, api_key: str = "") -> list[SearchResult]:
    """Query TinyFish search API. Returns list of SearchResult or raises on failure."""
    requests = _get_requests()
    key = api_key or os.environ.get("TINYFISH_API_KEY", "")
    if not key:
        raise Exception("TinyFish API key required for search. Get a free key at tinyfish.ai and set TINYFISH_API_KEY env var in your MCP config.")
    url = f"{TINYFISH_API}?query={quote(query)}&location=US&language=en"
    try:
        resp = await asyncio.to_thread(
            lambda: requests.get(url, headers={"X-API-Key": key}, timeout=TINYFISH_TIMEOUT)
        )
        if resp.status_code == 429:
            raise Exception("TinyFish rate limited (30/min free tier). Wait a moment and retry.")
        if resp.status_code == 401:
            raise Exception("TinyFish API key invalid. Get a free key at tinyfish.ai")
        if not resp.ok:
            raise Exception(f"TinyFish returned HTTP {resp.status_code}")
        data = resp.json()
        results_raw = data.get("results", [])[:max_results]
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("snippet", ""),
                source="tinyfish",
                position=i + 1,
            )
            for i, r in enumerate(results_raw)
            if r.get("url") and r.get("title")
        ]
    except Exception as e:
        if isinstance(e, ImportError):
            raise
        if "TinyFish" in str(e) or "rate limited" in str(e) or "API key" in str(e):
            raise
        raise Exception(f"TinyFish request failed: {e}")


async def smart_search(
    server,
    query: str,
    max_results: int = 10,
    cache_ttl: int = SEARCH_CACHE_TTL,
    api_key: str = "",
) -> SearchResponseModel:
    """Search the web via TinyFish API and return structured results.

    Args:
        server: MasterFetchServer instance (for cache access).
        query: Search query string.
        max_results: Max results to return (1-50, default 10).
        cache_ttl: Cache TTL in seconds (default 300 = 5 min, 0 = no cache).
        api_key: Optional TinyFish API key (uses built-in key if empty).
    """
    t0 = time()
    query = query.strip()

    if not query:
        return SearchResponseModel(
            query="", results=[], duration_ms=0, error="Empty search query",
        )
    max_results = max(1, min(max_results, 50))

    # Check cache
    if cache_ttl > 0:
        cache_key = f"search:v1"
        cached = await get_cached(cache_key, query, None, ttl=cache_ttl)
        if cached and cached.get("content"):
            try:
                data = json.loads(cached["content"][0])
                results = [SearchResult(**r) for r in data.get("results", [])]
                return SearchResponseModel(
                    query=query, results=results,
                    total_results=len(results), cached=True,
                    duration_ms=(time() - t0) * 1000,
                )
            except Exception:
                pass  # Corrupt cache, fall through to live search

    # Live search
    error = ""
    results: list[SearchResult] = []
    try:
        results = await _tinyfish_search(query, max_results, api_key)
    except ImportError as e:
        error = f"Search dependencies not installed. Run: pip install master-fetch[all]\n({e})"
    except Exception as e:
        error = str(e)

    # Cache successful results
    if cache_ttl > 0 and results:
        cache_key = f"search:v1"
        cache_data = json.dumps({"results": [r.model_dump() for r in results]})
        await set_cached(query, cache_key, [cache_data], 200, None, cache_ttl)

    return SearchResponseModel(
        query=query, results=results, total_results=len(results),
        duration_ms=(time() - t0) * 1000, error=error,
    )

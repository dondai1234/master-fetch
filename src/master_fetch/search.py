"""Web search for Hound via TinyFish API.

Requires TINYFISH_API_KEY env var (free key at tinyfish.ai, no credit card).
Structured JSON results. Cached for 5 minutes.
"""

import asyncio
import json
import logging
import os
from time import time
from urllib.parse import quote

from pydantic import BaseModel, Field

from master_fetch.cache import get_cached, set_cached
from master_fetch.security import validate_search_query, redact_api_key, SecurityError

logger = logging.getLogger("master-fetch.search")

TINYFISH_API = "https://api.search.tinyfish.ai"
TINYFISH_TIMEOUT = 12
SEARCH_CACHE_TTL = 300  # 5 minutes


class SearchResult(BaseModel):
    title: str = Field(description="Result title")
    url: str = Field(description="Result URL")
    snippet: str = Field(default="", description="Result snippet")
    source: str = Field(default="tinyfish", description="Source name")
    position: int = Field(default=0, description="1-indexed position")
    fetch_relevance: str = Field(default="", description="high|med|low — how likely this result answers the query. Fetch 'high' first, 'med' if needed, skip 'low'.")


class SearchResponseModel(BaseModel):
    query: str = Field(description="Search query")
    results: list[SearchResult] = Field(description="Search results")
    total_results: int = Field(default=0, description="Total results found")
    cached: bool = Field(default=False, description="Served from cache?")
    duration_ms: float = Field(default=0, description="Duration ms")
    error: str = Field(default="", description="Error message")
    fetch_hint: str = Field(default="", description="How many high/med/low results + which to smart_fetch first")


def _query_terms(query: str) -> set[str]:
    """Lowercased significant query terms for relevance matching."""
    terms = {w.lower() for w in (query or "").split() if len(w) >= 3}
    if not terms:
        terms = {w.lower() for w in (query or "").split() if w}
    return terms


def compute_fetch_relevance(query: str, title: str, snippet: str, position: int) -> str:
    """Heuristic high|med|low for how likely a result answers the query.

    Combines query-term overlap with title (weighted highest) and snippet, plus a
    small position bonus (search engines already rank by relevance, so the top
    results get a lift). Designed to help an agent pick 1-2 results to fetch
    instead of fetching all N or guessing.
    """
    terms = _query_terms(query)
    if not terms:
        # No usable terms (e.g. pure stopwords) — fall back to position only.
        return "high" if position <= 2 else ("med" if position <= 4 else "low")
    title_l = (title or "").lower()
    snippet_l = (snippet or "").lower()
    title_overlap = sum(1 for t in terms if t in title_l) / len(terms)
    snippet_overlap = sum(1 for t in terms if t in snippet_l) / len(terms)
    if position <= 2 and title_overlap >= 0.5:
        return "high"
    if position == 1 and title_overlap >= 0.25:
        return "high"
    if title_overlap >= 0.25 or snippet_overlap >= 0.5 or position <= 3:
        return "med"
    return "low"


def compute_fetch_hint(results: list[SearchResult]) -> str:
    """One-line nudge telling the agent how many of each relevance tier exist."""
    if not results:
        return ""
    high = sum(1 for r in results if r.fetch_relevance == "high")
    med = sum(1 for r in results if r.fetch_relevance == "med")
    low = sum(1 for r in results if r.fetch_relevance == "low")
    return (f"{high} high, {med} med, {low} low — smart_fetch the 'high' results "
            f"first (then 'med' if needed). Skip 'low' unless nothing else helps.")


def _get_requests():
    """Lazy import requests (optional dependency)."""
    try:
        import requests as _requests
        return _requests
    except ImportError:
        raise ImportError(
            "requests not installed. Run: pip install hound-mcp[all]"
        )


def _validate_api_key(api_key: str) -> str:
    """Get API key from param or env var, with basic format validation."""
    key = (api_key or "").strip()
    if not key:
        key = os.environ.get("TINYFISH_API_KEY", "").strip()
    if not key:
        raise SecurityError(
            "TinyFish API key required for search. Get a free key at tinyfish.ai "
            "and set TINYFISH_API_KEY env var in your MCP config."
        )
    # Basic format check: TinyFish keys start with sk-tinyfish-
    if not key.startswith("sk-tinyfish-"):
        logger.warning(
            "API key doesn't match expected TinyFish format (sk-tinyfish-...). "
            "Proceeding anyway."
        )
    return key


async def _tinyfish_search(
    query: str, max_results: int = 10, api_key: str = "",
) -> list[SearchResult]:
    """Query TinyFish search API. Returns list of SearchResult or raises on failure."""
    requests = _get_requests()
    key = _validate_api_key(api_key)

    url = f"{TINYFISH_API}?query={quote(query)}&location=US&language=en"

    def _do_request():
        return requests.get(
            url,
            headers={"X-API-Key": key},
            timeout=TINYFISH_TIMEOUT,
        )

    try:
        resp = await asyncio.to_thread(_do_request)
    except Exception as e:
        raise SecurityError(f"TinyFish request failed: {e}")

    if resp.status_code == 429:
        raise SecurityError(
            "TinyFish rate limited (30/min free tier). Wait a moment and retry."
        )
    if resp.status_code == 401:
        raise SecurityError("TinyFish API key invalid. Get a free key at tinyfish.ai")
    if resp.status_code == 403:
        raise SecurityError("TinyFish API key lacks permission. Check your key at tinyfish.ai")
    if not resp.ok:
        raise SecurityError(f"TinyFish returned HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError:
        raise SecurityError("TinyFish returned invalid JSON response")

    results_raw = data.get("results", [])
    if not results_raw:
        return []

    results: list[SearchResult] = []
    for i, r in enumerate(results_raw[:max_results]):
        url_val = r.get("url", "").strip()
        title = r.get("title", "").strip()
        if url_val and title:
            snippet = r.get("snippet", "").strip()
            results.append(SearchResult(
                title=title,
                url=url_val,
                snippet=snippet,
                source="tinyfish",
                position=i + 1,
                fetch_relevance=compute_fetch_relevance(query, title, snippet, i + 1),
            ))

    return results


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
        api_key: TinyFish API key. Uses TINYFISH_API_KEY env var if empty.
    """
    t0 = time()

    try:
        query = validate_search_query(query)
    except Exception as e:
        return SearchResponseModel(
            query="", results=[], total_results=0,
            duration_ms=0, error=str(e),
        )

    max_results = max(1, min(max_results, 50))

    # Cache key includes max_results so a max_results=5 and max_results=10 search
    # don't collide on one cached result set. get_cached args: (url, extraction_type).
    cache_type = f"search:v1:{max_results}"
    if cache_ttl > 0:
        cached = await get_cached(query, cache_type, None, ttl=cache_ttl)
        if cached and cached.get("content"):
            try:
                data = json.loads(cached["content"][0])
                results_list = [SearchResult(**r) for r in data.get("results", [])]
                return SearchResponseModel(
                    query=query, results=results_list,
                    total_results=len(results_list), cached=True,
                    duration_ms=(time() - t0) * 1000,
                    fetch_hint=compute_fetch_hint(results_list),
                )
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"Corrupt search cache for '{query[:50]}': {e}")
                # Corrupt cache entry — fall through to live search

    # Live search
    error = ""
    results: list[SearchResult] = []
    try:
        results = await _tinyfish_search(query, max_results, api_key)
    except ImportError as e:
        error = (
            f"Search dependencies not installed. "
            f"Run: pip install hound-mcp[all] ({e})"
        )
    except Exception as e:
        error = redact_api_key(str(e)[:200])

    # Cache successful results
    if cache_ttl > 0 and results:
        cache_data = json.dumps({"results": [r.model_dump() for r in results]})
        await set_cached(query, cache_type, [cache_data], 200, None, cache_ttl)

    return SearchResponseModel(
        query=query, results=results, total_results=len(results),
        duration_ms=(time() - t0) * 1000, error=error,
        fetch_hint=compute_fetch_hint(results),
    )

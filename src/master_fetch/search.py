"""Web search for Hound via TinyFish API.

Requires TINYFISH_API_KEY env var (free key at tinyfish.ai, no credit card).
Structured JSON results. Cached for 5 minutes.
"""

import asyncio
import json
import logging
import os
from time import time
from typing import Optional, Union
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
    fetch_relevance: str = Field(default="", description="high|med|low - how likely this result answers the query. Fetch 'high' first, 'med' if needed, skip 'low'.")


class SearchResponseModel(BaseModel):
    query: str = Field(description="Search query")
    results: list[SearchResult] = Field(description="Search results")
    total_results: int = Field(default=0, description="Total results found")
    cached: bool = Field(default=False, description="Served from cache?")
    duration_ms: float = Field(default=0, description="Duration ms")
    error: str = Field(default="", description="Error message")
    fetch_hint: str = Field(default="", description="How many high/med/low results + which to smart_fetch first")


class ResearchResult(SearchResult):
    """A search result with its full fetched content attached (research mode)."""
    content: list[str] = Field(default=[], description="Fetched page content (truncated per max_content_chars_per)")
    content_ok: bool = Field(default=False, description="True = the fetched content is real (trust it). False = fetch failed/blocked/empty.")
    fetched_summary: str = Field(default="", description="One-line status of the fetch for this result")
    is_truncated: bool = Field(default=False, description="True = more content for this URL; smart_fetch it with offset=next_offset.")
    next_offset: int = Field(default=0, description="Next offset if is_truncated; 0 = no more.")
    fetch_error: str = Field(default="", description="Error from the fetch, if content_ok is False.")


class ResearchResponseModel(BaseModel):
    """Response from research mode: search + auto-fetched top results in one call."""
    query: str = Field(description="Search query")
    results: list[ResearchResult] = Field(description="Top results with full content attached")
    total_results: int = Field(default=0, description="Total search results found (only the top fetch_top were fetched)")
    fetched_count: int = Field(default=0, description="How many results were fetched")
    cached: bool = Field(default=False, description="Search served from cache?")
    duration_ms: float = Field(default=0, description="Duration ms")
    error: str = Field(default="", description="Error message")
    fetch_hint: str = Field(default="", description="High/med/low breakdown of the full search")
    summary: str = Field(default="", description="One-line status of the research call")


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
    return (f"{high} high, {med} med, {low} low - smart_fetch the 'high' results "
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


def _append_site_operators(query: str, site: Optional[str], exclude_sites: Optional[list[str]]) -> str:
    """Append TinyFish search operators: `site:domain` to restrict, `-site:d` to exclude."""
    q = (query or "").strip()
    if site:
        q = f"{q} site:{site}"
    for d in exclude_sites or []:
        if d:
            q = f"{q} -site:{d}"
    return q


def _validate_filters(site, exclude_sites, location, language, page):
    """Validate search filter params. Raises SecurityError on bad input."""
    import re
    _domain_re = re.compile(r"^(?!-)[A-Za-z0-9.-]{1,253}(?<!-)$")
    if site is not None:
        if not isinstance(site, str) or not _domain_re.match(site) or "." not in site:
            raise SecurityError(f"Invalid site filter: {site!r} (must be a domain like 'docs.python.org')")
    if exclude_sites is not None:
        if not isinstance(exclude_sites, list) or len(exclude_sites) > 20:
            raise SecurityError("exclude_sites must be a list of <= 20 domains")
        for d in exclude_sites:
            if not isinstance(d, str) or not _domain_re.match(d) or "." not in d:
                raise SecurityError(f"Invalid exclude_sites entry: {d!r}")
    if location is not None:
        if not isinstance(location, str) or not re.match(r"^[A-Z]{2}$", location):
            raise SecurityError(f"Invalid location: {location!r} (2-letter country code, e.g. 'US')")
    if language is not None:
        if not isinstance(language, str) or not re.match(r"^[a-z]{2}$", language):
            raise SecurityError(f"Invalid language: {language!r} (2-letter language code, e.g. 'en')")
    if page is not None:
        if isinstance(page, bool) or not isinstance(page, int) or page < 0 or page > 10:
            raise SecurityError(f"Invalid page: {page!r} (0-10)")


async def _tinyfish_search(
    query: str, max_results: int = 10, api_key: str = "",
    site: Optional[str] = None, exclude_sites: Optional[list[str]] = None,
    location: Optional[str] = None, language: Optional[str] = None, page: int = 0,
) -> list[SearchResult]:
    """Query TinyFish search API. Returns list of SearchResult or raises on failure.

    Filters: site/exclude_sites are appended as `site:`/`-site:` operators to the
    query (native TinyFish). location/language/page are passed as API params.
    """
    requests = _get_requests()
    key = _validate_api_key(api_key)

    q = _append_site_operators(query, site, exclude_sites)
    from urllib.parse import urlencode
    params = {"query": q, "location": location or "US", "language": language or "en"}
    if page:
        params["page"] = str(int(page))
    url = f"{TINYFISH_API}?{urlencode(params)}"

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
    site: Optional[str] = None,
    exclude_sites: Optional[list[str]] = None,
    location: Optional[str] = None,
    language: Optional[str] = None,
    page: int = 0,
    fetch_content: bool = False,
    fetch_top: int = 3,
    max_content_chars_per: int = 8000,
) -> Union[SearchResponseModel, ResearchResponseModel]:
    """Search the web via TinyFish API and return structured results.

    Filters (native TinyFish): site/exclude_sites append `site:`/`-site:` operators;
    location/language/page are API params. Research mode (fetch_content=True)
    auto-fetches the top-N high-relevance results' full content in this same call
    (each via server.smart_fetch, so anti-bot + PDF + OCR + the fetch cache all
    apply) and returns a ResearchResponseModel.
    """
    t0 = time()

    try:
        query = validate_search_query(query)
        _validate_filters(site, exclude_sites, location, language, page)
    except Exception as e:
        return SearchResponseModel(
            query=query, results=[], total_results=0,
            duration_ms=0, error=str(e),
        )

    max_results = max(1, min(max_results, 50))
    fetch_top = max(1, min(int(fetch_top) if not isinstance(fetch_top, bool) else 3, 5))
    max_content_chars_per = max(1000, min(int(max_content_chars_per), 50000))

    # Cache key includes max_results AND every filter so a filtered search and an
    # unfiltered one (or two different filters) don't collide on one cached set.
    cache_type = f"search:v1:{max_results}:{site or ''}:{','.join(exclude_sites or [])}:{location or ''}:{language or ''}:{page or 0}"
    if cache_ttl > 0:
        cached = await get_cached(query, cache_type, None, ttl=cache_ttl)
        if cached and cached.get("content"):
            try:
                data = json.loads(cached["content"][0])
                results_list = [SearchResult(**r) for r in data.get("results", [])]
                if fetch_content:
                    return await _research_fetch(
                        server, query, results_list, fetch_top, max_content_chars_per,
                        cache_ttl, cached=True, t0=t0, error="",
                    )
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
        results = await _tinyfish_search(
            query, max_results, api_key, site=site, exclude_sites=exclude_sites,
            location=location, language=language, page=page,
        )
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

    if fetch_content and results:
        return await _research_fetch(
            server, query, results, fetch_top, max_content_chars_per,
            cache_ttl, cached=False, t0=t0, error=error,
        )

    return SearchResponseModel(
        query=query, results=results, total_results=len(results),
        duration_ms=(time() - t0) * 1000, error=error,
        fetch_hint=compute_fetch_hint(results),
    )


async def _research_fetch(
    server, query: str, results: list[SearchResult],
    fetch_top: int, max_content_chars_per: int, cache_ttl: int,
    cached: bool, t0: float, error: str,
) -> ResearchResponseModel:
    """Research mode: bulk-fetch the top-N high-relevance results' content."""
    rank = {"high": 3, "med": 2, "low": 1, "": 0}
    ranked = sorted(results, key=lambda r: (rank.get(r.fetch_relevance, 0), -r.position), reverse=True)
    top = ranked[:fetch_top]

    async def _fetch_one(r: SearchResult) -> ResearchResult:
        try:
            res = await server.smart_fetch(
                url=r.url, cache_ttl=max(cache_ttl, 3600),
                max_content_chars=max_content_chars_per,
            )
            return ResearchResult(
                title=r.title, url=r.url, snippet=r.snippet, source=r.source,
                position=r.position, fetch_relevance=r.fetch_relevance,
                content=res.content, content_ok=res.content_ok,
                fetched_summary=res.summary, is_truncated=res.is_truncated,
                next_offset=res.next_offset, fetch_error=res.error,
            )
        except Exception as e:
            return ResearchResult(
                title=r.title, url=r.url, snippet=r.snippet, source=r.source,
                position=r.position, fetch_relevance=r.fetch_relevance,
                content=[f"[Fetch failed: {redact_api_key(str(e)[:160])}]"],
                content_ok=False, fetch_error=redact_api_key(str(e)[:200]),
            )

    fetched = await asyncio.gather(*[_fetch_one(r) for r in top])
    ok = sum(1 for r in fetched if r.content_ok)
    summary = (
        f"searched {query!r} -> {len(results)} results; fetched {len(fetched)} top "
        f"({ok} content_ok). Paginate any is_truncated result via smart_fetch with offset."
    )
    return ResearchResponseModel(
        query=query, results=fetched, total_results=len(results),
        fetched_count=len(fetched), cached=cached,
        duration_ms=(time() - t0) * 1000, error=error,
        fetch_hint=compute_fetch_hint(results), summary=summary,
    )

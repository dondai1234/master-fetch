"""Hound local web search (v7 flagship: keyless, no-account, fully local).

Scrapes public search engines (DuckDuckGo, Bing, Google, Wikipedia) via the
hound-native engine layer in search_engines.py - no third-party API, no key, no
account. Results are merged across engines, deduped by normalized URL, and
ranked. Every result carries a relevance_score and a fetch_relevance tier so the
agent fetches the right 1-2 URLs via smart_fetch itself (search returns URLs +
ranking, NOT page content - the agent decides what to fetch).

Rerank modes: keyword (BM25, always available, even on the lean install), neural
(local ONNX cross-encoder on snippets, needs [all]), find_similar (pass url=,
find pages similar to it).
"""

from __future__ import annotations

import asyncio
import json
import logging
from time import time
from typing import Optional

from pydantic import BaseModel, Field

from master_fetch.cache import get_cached, set_cached
from master_fetch.security import validate_search_query, validate_url, redact_api_key, SecurityError
from master_fetch.search_engines import (
    RawResult, multi_search, EngineReport, DEFAULT_ENGINES, bm25_rerank,
    fetch_source_for_similar,
)
from master_fetch.reranker import (
    rerank as neural_rerank, unavailable_reason, get_reranker,
)

logger = logging.getLogger("master-fetch.search")

SEARCH_CACHE_TTL = 300  # 5 minutes


# ─── response model ──────────────────────────────────────────────────────────

class SearchResult(BaseModel):
    title: str = Field(description="Result title")
    url: str = Field(description="Result URL")
    snippet: str = Field(default="", description="Result snippet from the engine")
    source: str = Field(default="", description="Engine that returned this result (duckduckgo/bing/google/wikipedia)")
    position: int = Field(default=0, description="1-indexed rank after merge + rerank")
    relevance_score: float = Field(default=0.0, description="0.0-1.0 relevance to the query (BM25 over title+snippet, or neural cross-encoder score in neural mode). 1.0 = most relevant in this set.")
    fetch_relevance: str = Field(default="", description="high|med|low - relative relevance hint. smart_fetch what matches your need; the tiers rank results but a lower tier can be the right one - use your judgment.")


class SearchResponseModel(BaseModel):
    query: str = Field(description="Search query")
    results: list[SearchResult] = Field(description="Ranked search results (URLs + ranking, not page content)")
    total_results: int = Field(default=0, description="Results returned")
    engines_used: list[str] = Field(default=[], description="Engines that returned results")
    engine_blocked: list[str] = Field(default=[], description="Engines that did NOT contribute (rate-limited/CAPTCHA'd/timed out/parsed no results). Results still came from engines_used; retry shortly for more recall.")
    rerank_mode: str = Field(default="keyword", description="Rerank used: keyword|neural|find_similar.")
    cached: bool = Field(default=False, description="Served from cache?")
    duration_ms: float = Field(default=0, description="Duration ms")
    error: str = Field(default="", description="Error message (empty = ok)")
    fetch_hint: str = Field(default="", description="How many high/med/low results + which to smart_fetch first")
    summary: str = Field(default="", description="One-line status of the search (counts + engines + rerank).")
    next_action: str = Field(default="", description="The obvious next call: fetch the high results, rephrase, retry, etc. Empty = nothing more to do.")


# ─── tier derivation + hint ──────────────────────────────────────────────────

def _tier(score: float, rank: int, total: int) -> str:
    """Derive high|med|low from BM25 score + rank. Top result is never 'low'."""
    if score >= 0.5 or rank == 1:
        return "high"
    if score >= 0.15:
        return "med"
    if rank <= max(2, total // 3):
        return "med"
    return "low"


def compute_fetch_hint(results: list[SearchResult]) -> str:
    if not results:
        return ""
    high = sum(1 for r in results if r.fetch_relevance == "high")
    med = sum(1 for r in results if r.fetch_relevance == "med")
    low = sum(1 for r in results if r.fetch_relevance == "low")
    return (f"{high} high, {med} med, {low} low. Ranked by relevance_score; "
            f"smart_fetch what fits your need (high first, but a lower tier can be the right call).")


def _search_summary(query: str, results: list[SearchResult], engines_used: list[str],
                    rerank_mode: str) -> str:
    """One-line status for the agent (counts + engines + rerank mode)."""
    high = sum(1 for r in results if r.fetch_relevance == "high")
    med = sum(1 for r in results if r.fetch_relevance == "med")
    low = sum(1 for r in results if r.fetch_relevance == "low")
    eng = ",".join(engines_used) if engines_used else "none"
    return (f"Searched {query[:60]!r} -> {len(results)} results "
            f"({high} high, {med} med, {low} low) from {eng}; rerank={rerank_mode}.")


def _search_next_action(results: list[SearchResult], engine_blocked: list[str],
                         error: str) -> str:
    """A judgment-empowering nudge, not a rigid directive. The ranking is a HINT:
    the agent may legitimately need a lower-ranked result, so we point it at the
    signals (relevance_score + fetch_relevance) and trust it to pick, instead of
    prescribing 'fetch N'. This avoids the LLM stressing over whether to 'break'
    the instruction when a lower-ranked result is the one it actually needs."""
    if not results:
        if error and ("rate-limited" in error.lower() or "timed out" in error.lower() or engine_blocked):
            return ("No results (engines rate-limited/timed out). Retry in a moment, "
                    "or set HOUND_SEARCH_PROXY for sustained heavy use.")
        return "No results. Rephrase (more specific / different terms) or try mode=neural for semantic matching."
    high = [r for r in results if r.fetch_relevance == "high"]
    base = ("Results are ranked by relevance (relevance_score + fetch_relevance). "
            "smart_fetch the ones that match what you actually need - the ranking is a hint, "
            "not a directive; a lower-ranked result can be the right one, so trust your judgment.")
    if not high:
        base += " No 'high' matches - if none of these fit, rephrase (more specific) or try mode=neural."
    if engine_blocked:
        base += " Some engines didn't contribute; retry shortly for more recall."
    return base


# ─── filter validation (site/exclude/location/language/page) ─────────────────

def _validate_filters(site, exclude_sites, location, language, page):
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
        if not isinstance(location, str) or not re.match(r"^[A-Za-z]{2}(-[A-Za-z]{2})?$", location):
            raise SecurityError(f"Invalid location: {location!r} (e.g. 'US' or 'us-en')")
    if language is not None:
        if not isinstance(language, str) or not re.match(r"^[a-z]{2}$", language):
            raise SecurityError(f"Invalid language: {language!r} (2-letter code, e.g. 'en')")
    if page is not None:
        if isinstance(page, bool) or not isinstance(page, int) or page < 0 or page > 10:
            raise SecurityError(f"Invalid page: {page!r} (0-10)")


def _validate_engines(engines):
    if engines is None:
        return None
    if not isinstance(engines, list) or not engines:
        raise SecurityError("engines must be a non-empty list")
    if len(engines) > 6:
        raise SecurityError("engines list too long (max 6)")
    valid = set(DEFAULT_ENGINES) | {"google", "wikipedia"}
    for e in engines:
        if not isinstance(e, str) or e.lower() not in valid:
            raise SecurityError(f"Invalid engine: {e!r} (one of {sorted(valid)})")
    return [e.lower() for e in engines]


def _validate_freshness(freshness):
    if freshness is None:
        return None
    if freshness not in ("day", "week", "month", "year"):
        raise SecurityError(f"Invalid freshness: {freshness!r} (day|week|month|year)")
    return freshness


# Implemented rerank modes (find_similar = URL->similar). Unknown modes are
# rejected so the schema does not advertise a mode that is not wired.
_IMPLEMENTED_MODES = ("auto", "keyword", "neural", "find_similar")


def _validate_mode(mode):
    if mode is None:
        return "auto"
    if not isinstance(mode, str) or mode.lower() not in _IMPLEMENTED_MODES:
        raise SecurityError(f"Invalid mode: {mode!r} (auto|keyword|neural|find_similar)")
    return mode.lower()


def _rank(query: str, ranked: list[RawResult], mode: str):
    """Apply the chosen rerank. Returns (ranked_list, scores, mode_used, note).

    mode='auto' uses neural if the reranker is available (hound-mcp[all] + model
    downloaded), else keyword BM25. mode='neural' tries neural and falls back to
    keyword with a note if unavailable. mode='keyword' is always BM25.
    """
    note = ""
    if mode in ("neural", "auto"):
        pairs = neural_rerank(query, ranked)
        if pairs is not None:
            return [r for r, _ in pairs], [s for _, s in pairs], "neural", note
        if mode == "neural":
            note = ("neural rerank unavailable - used keyword. " +
                    (unavailable_reason() or "install hound-mcp[all] and retry"))
    scored = bm25_rerank(query, ranked)
    return [r for r, _ in scored], [s for _, s in scored], "keyword", note


def _build_results(query: str, ranked: list[RawResult], scores: Optional[list[float]] = None
                   ) -> list[SearchResult]:
    """Convert RawResults (already ranked) into SearchResults with tiers."""
    total = len(ranked)
    out: list[SearchResult] = []
    for i, r in enumerate(ranked):
        score = scores[i] if scores and i < len(scores) else 0.0
        out.append(SearchResult(
            title=r.title, url=r.url, snippet=r.snippet, source=r.source,
            position=i + 1, relevance_score=round(score, 4),
            fetch_relevance=_tier(score, i + 1, total),
        ))
    return out


# ─── main entry ───────────────────────────────────────────────────────────────

async def smart_search(
    server,
    query: str,
    max_results: int = 9,
    cache_ttl: int = SEARCH_CACHE_TTL,
    mode: str = "auto",
    engines: Optional[list[str]] = None,
    url: Optional[str] = None,
    site: Optional[str] = None,
    exclude_sites: Optional[list[str]] = None,
    location: Optional[str] = None,
    language: Optional[str] = None,
    region: Optional[str] = None,
    page: int = 0,
    freshness: Optional[str] = None,
) -> SearchResponseModel:
    """Local keyless web search (no API key, no account). Engines (default
    duckduckgo+bing; add 'google' or 'wikipedia') are scraped in parallel, merged,
    deduped, and ranked. Returns URLs + ranking (NOT page content) so the agent
    smart_fetches the right 1-2 itself.

    mode: auto (neural if [all]+model present else keyword), keyword (BM25),
    neural (cross-encoder on snippets; better for semantic/ambiguous queries),
    find_similar (pass url=; fetches the source page, derives a query, and reranks
    candidates against the source content - Exa find-similar, local).
    """
    t0 = time()

    try:
        query = validate_search_query(query)
        _validate_filters(site, exclude_sites, location, language, page)
        engines = _validate_engines(engines)
        freshness = _validate_freshness(freshness)
        mode = _validate_mode(mode)
    except Exception as e:
        return SearchResponseModel(
            query=query, results=[], total_results=0,
            duration_ms=0, error=str(e),
        )

    max_results = max(1, min(max_results, 50))

    # find_similar: the target is a URL, not a query. Derive it early so the cache
    # key is keyed on the source URL.
    find_sim_url = ""
    if mode == "find_similar":
        cand = (url or "").strip() or (query if query.startswith("http") else "")
        try:
            find_sim_url = validate_url(cand) if cand else ""
        except Exception:
            find_sim_url = ""
        if not find_sim_url:
            return SearchResponseModel(
                query=query, results=[], total_results=0,
                duration_ms=(time() - t0) * 1000,
                error="find_similar requires a url (pass url=, or the URL as query).",
                next_action="Pass url= with a page URL to find pages similar to it (or pass the URL as the query).")

    # region derives from location/language if not given (e.g. "US" -> "us-en").
    if region is None:
        loc = (location or "US").lower()
        lang = (language or "en").lower()
        region = f"{loc}-{lang}" if len(loc) == 2 else "us-en"

    cache_query = find_sim_url or query
    cache_type = (f"search:v3:{max_results}:{site or ''}:{','.join(exclude_sites or [])}:"f"{location or ''}:{language or ''}:{page or 0}:{','.join(engines or [])}:"f"{freshness or ''}:{mode}:{cache_query}")
    if cache_ttl > 0:
        cached = await get_cached(cache_query, cache_type, None, ttl=cache_ttl)
        if cached and cached.get("content"):
            try:
                data = json.loads(cached["content"][0])
                results_list = [SearchResult(**r) for r in data.get("results", [])]
                _eu = data.get("engines_used", [])
                _eb = data.get("engine_blocked", [])
                _rm = data.get("rerank_mode", "keyword")
                return SearchResponseModel(
                    query=cache_query, results=results_list,
                    total_results=len(results_list), cached=True,
                    engines_used=_eu,
                    engine_blocked=_eb,
                    rerank_mode=_rm,
                    duration_ms=(time() - t0) * 1000,
                    fetch_hint=compute_fetch_hint(results_list),
                    summary=_search_summary(cache_query, results_list, _eu, _rm),
                    next_action=_search_next_action(results_list, _eb, ""),
                )
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"Corrupt search cache for '{cache_query[:50]}': {e}")

    # Live local search
    error = ""
    ranked: list[RawResult] = []
    reports: list[EngineReport] = []
    rerank_used = "keyword"
    rerank_note = ""

    if mode == "find_similar":
        src_title, src_text = await fetch_source_for_similar(find_sim_url, timeout=6)
        if not src_text:
            return SearchResponseModel(
                query=find_sim_url, results=[], total_results=0,
                duration_ms=(time() - t0) * 1000,
                error="could not fetch the source URL for find_similar (blocked or offline).",
                next_action="Retry, or smart_fetch the source URL first to confirm it is reachable, then call smart_search with mode=find_similar.")
        derived_query = src_title or " ".join(src_text.split()[:8]) or query
        try:
            ranked, reports = await multi_search(
                derived_query, max_results, engines=engines, site=site,
                exclude_sites=exclude_sites, region=region, freshness=freshness,
                page=page, server=server,
            )
        except Exception as e:
            error = redact_api_key(str(e)[:200])
        # Rerank candidates against the SOURCE page content (Exa find-similar,
        # local: the cross-encoder scores (source_content, candidate)).
        rer = get_reranker()
        if rer is not None and ranked:
            docs = [f"{r.title} {r.snippet}" for r in ranked]
            try:
                scores = rer.score(src_text[:2000], docs)
                pairs = sorted(zip(ranked, scores), key=lambda rs: (-rs[1], rs[0].position))
                ranked_list = [r for r, _ in pairs]
                scores = [s for _, s in pairs]
                rerank_used = "find_similar"
            except Exception:
                ranked_list, scores, _, _ = _rank(derived_query, ranked, "keyword")
                rerank_used = "find_similar"
        else:
            ranked_list, scores, _, _ = _rank(derived_query, ranked, "keyword")
            rerank_used = "find_similar"
            if ranked and get_reranker() is None:
                rerank_note = ("find_similar used keyword BM25 (neural unavailable). " +
                               (unavailable_reason() or "install hound-mcp[all]"))
        ranked_list, scores = ranked_list[:max_results], scores[:max_results]
        results_list = _build_results(cache_query, ranked_list, scores)
        sim_note = f"find_similar to {find_sim_url} (searched: {derived_query[:60]!r})"
        fetch_hint = compute_fetch_hint(results_list)
        fetch_hint = (fetch_hint + " | " + sim_note) if fetch_hint else sim_note
        if rerank_note:
            fetch_hint = (fetch_hint + " | " + rerank_note) if fetch_hint else rerank_note
    else:
        try:
            ranked, reports = await multi_search(
                query, max_results, engines=engines, site=site,
                exclude_sites=exclude_sites, region=region, freshness=freshness,
                page=page, server=server,
            )
        except Exception as e:
            error = redact_api_key(str(e)[:200])

        if not ranked and not error:
            blocked_any = bool([r for r in reports if r.blocked])
            error = (
                "No results from any engine. " +
                ("Engines were rate-limited/CAPTCHA'd; retry in a moment, rephrase, or set HOUND_SEARCH_PROXY for sustained heavy use. "
                 if blocked_any else "Try rephrasing the query.")
            )

        ranked_list, scores, rerank_used, rerank_note = _rank(query, ranked, mode)
        ranked_list, scores = ranked_list[:max_results], scores[:max_results]
        results_list = _build_results(query, ranked_list, scores)
        fetch_hint = compute_fetch_hint(results_list)
        if rerank_note:
            fetch_hint = (fetch_hint + " | " + rerank_note) if fetch_hint else rerank_note

    # engines_used = contributed; engine_blocked = did NOT contribute (blocked /
    # timed out / parsed no results / consent page). Surfacing non-contributing
    # engines means an opt-in engine like google that CAPTCHAs is visible to the
    # agent (in engine_blocked), not silently absent from both lists.
    engines_used = list(dict.fromkeys(r.name for r in reports if r.ok))
    engine_blocked = list(dict.fromkeys(r.name for r in reports if not r.ok))

    # Agent QoL: when some engines didn't contribute but results came back from
    # the rest, say so plainly so the agent knows the results are partial + a
    # retry may add recall (instead of looking like a failure).
    if engine_blocked and results_list:
        _blk_note = (f"Engines {', '.join(engine_blocked)} didn't contribute (rate-limited/timed out/no results); "
                     f"results are from the rest - retry shortly for more recall.")
        fetch_hint = (fetch_hint + " | " + _blk_note) if fetch_hint else _blk_note

    # Cache successful results (+ engine metadata for cache hits)
    if cache_ttl > 0 and results_list:
        cache_data = json.dumps({
            "results": [r.model_dump() for r in results_list],
            "engines_used": engines_used,
            "engine_blocked": engine_blocked,
            "rerank_mode": rerank_used,
        })
        await set_cached(cache_query, cache_type, [cache_data], 200, None, cache_ttl)

    return SearchResponseModel(
        query=cache_query, results=results_list, total_results=len(results_list),
        engines_used=engines_used, engine_blocked=engine_blocked,
        rerank_mode=rerank_used,
        duration_ms=(time() - t0) * 1000, error=error,
        fetch_hint=fetch_hint,
        summary=_search_summary(cache_query, results_list, engines_used, rerank_used),
        next_action=_search_next_action(results_list, engine_blocked, error),
    )

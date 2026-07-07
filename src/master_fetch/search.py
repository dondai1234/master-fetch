"""Hound local web search (v7 flagship: keyless, no-account, fully local).

Scrapes public search engines (DuckDuckGo, Bing, Qwant, Wikipedia) via the
hound-native engine layer in search_engines.py - no third-party API, no key, no
account. Results are merged across engines, deduped by normalized URL, and
ranked. Merging INDEPENDENT indexes gives a free authority signal: a URL
returned by several engines is a consensus hit (engines_consensus field) and
gets a ranking boost. Every result also carries a relevance_score and a
fetch_relevance tier so the agent fetches the right URLs via smart_fetch itself
(search returns URLs + ranking, NOT page content - the agent decides what to fetch).

Rerank: neural (a local ONNX cross-encoder on snippets, needs [all]; the ONLY
reranker - BM25 was removed as redundant since neural matches its speed and
ranks better), find_similar (pass url=, find pages similar to it). Lean installs
without the model fall back to cross-engine consensus + engine-position order.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from time import time
from typing import Optional

from pydantic import BaseModel, Field

from master_fetch.cache import get_cached, set_cached
from master_fetch.security import validate_search_query, validate_url, redact_api_key, SecurityError
from master_fetch.search_engines import (
    RawResult, multi_search, EngineReport, DEFAULT_ENGINES,
    fetch_source_for_similar, _INDEX_FAMILY,
)

logger = logging.getLogger("master-fetch.search")


def neural_rerank(query: str, ranked: list[RawResult]):
    from master_fetch.reranker import rerank
    return rerank(query, ranked)


def unavailable_reason() -> str:
    from master_fetch.reranker import unavailable_reason as _unavailable_reason
    return _unavailable_reason()


def get_reranker():
    from master_fetch.reranker import get_reranker as _get_reranker
    return _get_reranker()


async def ensure_reranker(*, download: bool = True):
    from master_fetch.reranker import ensure_reranker as _ensure_reranker
    return await _ensure_reranker(download=download)


SEARCH_CACHE_TTL = 300  # 5 minutes


# ─── related-query mining (extractive, no LLM) ──────────────────────────────
# Mine follow-up queries from the result titles + snippets hound already has.
# Engine-agnostic and robust: no dependence on fragile per-engine "related
# searches" SERP markup (which changes often). Ranks bigrams by document
# frequency across the result set, drops ones that overlap the original query,
# and returns the top N as suggested refinements.

_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were",
    "has", "have", "had", "you", "your", "its", "our", "not", "but", "can",
    "will", "into", "via", "using", "use", "how", "what", "when", "why", "who",
    "which", "about", "also", "more", "most", "than", "then", "them", "they",
    "their", "there", "here", "such", "each", "other", "some", "any", "all",
    "one", "two", "new", "get", "got", "may", "might", "could", "should",
    "would", "does", "did", "done", "been", "being", "very", "just", "like",
}
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'+-]{2,}")


def _query_tokens(query: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(query or "") if w.lower() not in _STOPWORDS}


def _related_queries(query: str, results: list["SearchResult"], *, n: int = 6) -> list[str]:
    """Mine follow-up queries from result titles + snippets.

    Ranks bigrams by document frequency (in how many results they appear),
    drops bigrams that overlap the original query or duplicate each other, and
    falls back to high-frequency unigrams if too few bigrams. Returns up to n
    phrases. Robust + cheap; never raises.
    """
    if not results:
        return []
    q_tokens = _query_tokens(query)
    docs: list[list[str]] = []
    for r in results:
        text = (f"{r.title} {r.snippet}").lower()
        words = [w for w in _WORD_RE.findall(text) if w not in _STOPWORDS]
        docs.append(words)
    if not docs:
        return []

    bigram_docfreq: Counter[str] = Counter()
    unigram_docfreq: Counter[str] = Counter()
    for words in docs:
        uniq_bi = set()
        for i in range(len(words) - 1):
            a, b = words[i], words[i + 1]
            if len(a) < 3 or len(b) < 3:
                continue
            uniq_bi.add(f"{a} {b}")
        for bi in uniq_bi:
            bigram_docfreq[bi] += 1
        for w in set(words):
            if len(w) >= 3:
                unigram_docfreq[w] += 1

    def _overlaps_query(phrase: str) -> bool:
        toks = phrase.split()
        if not toks:
            return True
        if not [t for t in toks if t not in q_tokens]:  # every token already in query
            return True
        pq, pphrase = query.lower(), phrase
        if pphrase in pq or pq in pphrase:
            return True
        return False

    scored = []
    for bi, df in bigram_docfreq.items():
        if df < 2:  # appears in only one result -> not a pattern
            continue
        if _overlaps_query(bi):
            continue
        scored.append((df, bi))
    scored.sort(key=lambda x: (-x[0], x[1]))

    out: list[str] = []
    seen_words: set[str] = set()
    for _df, bi in scored:
        toks = bi.split()
        if len(set(toks) & seen_words) >= 2:  # near-dup of a kept suggestion
            continue
        out.append(bi)
        seen_words.update(toks)
        if len(out) >= n:
            break

    if len(out) < n:  # fall back to unigrams
        for w, df in unigram_docfreq.most_common():
            if df < 2 or w in q_tokens or w in seen_words:
                continue
            out.append(w)
            seen_words.add(w)
            if len(out) >= n:
                break
    return out[:n]


# ─── response model ──────────────────────────────────────────────────────────

class SearchResult(BaseModel):
    title: str = Field(description="Result title")
    url: str = Field(description="Result URL")
    snippet: str = Field(default="", description="Result snippet from the engine")
    source: str = Field(default="", description="Backend(s) that returned this result (duckduckgo/brave/mojeek/yahoo/yandex/startpage/google/wikipedia/grokipedia). Multiple = cross-backend consensus.")
    position: int = Field(default=0, description="1-indexed rank after merge + rerank")
    relevance_score: float = Field(default=0.0, description="0.0-1.0 relevance to the query (neural cross-encoder score in neural mode, min-max normalized), boosted by cross-backend consensus. 1.0 = most relevant in this set.")
    fetch_relevance: str = Field(default="", description="high|med|low - relative relevance hint. smart_fetch what matches your need; the tiers rank results but a lower tier can be the right one - use your judgment.")
    engines_consensus: str = Field(default="", description="How many independent indexes returned this URL (e.g. '3 of 4'). A free authority signal: a URL returned by several independent engines is more likely authoritative.")


class SearchResponseModel(BaseModel):
    query: str = Field(description="Search query")
    results: list[SearchResult] = Field(description="Ranked search results (URLs + ranking, not page content)")
    total_results: int = Field(default=0, description="Results returned")
    engines_used: list[str] = Field(default=[], description="Engines that returned results")
    engine_blocked: list[str] = Field(default=[], description="Engines that did NOT contribute (rate-limited/CAPTCHA'd/timed out/parsed no results). Results still came from engines_used; retry shortly for more recall.")
    rerank_mode: str = Field(default="merge", description="Rerank used: merge|neural|find_similar.")
    cached: bool = Field(default=False, description="Served from cache?")
    duration_ms: float = Field(default=0, description="Duration ms")
    error: str = Field(default="", description="Error message (empty = ok)")
    fetch_hint: str = Field(default="", description="How many high/med/low results + which to smart_fetch first")
    related_queries: list[str] = Field(default=[], description="Follow-up queries worth searching next, mined extractively from the result titles+snippets (no LLM). Empty if none derived. Use to refine a broad query.")
    summary: str = Field(default="", description="One-line status of the search (counts + engines + rerank).")
    next_action: str = Field(default="", description="The obvious next call: fetch the high results, rephrase, retry, etc. Empty = nothing more to do.")


# ─── tier derivation + hint ──────────────────────────────────────────────────

def _tier(score: float, rank: int, total: int) -> str:
    """Derive high|med|low from relevance score + rank. Top result is never 'low'."""
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
    base = ("Results are ranked by relevance + cross-engine consensus (engines_consensus = how many independent indexes agree). "
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
    if len(engines) > 9:
        raise SecurityError("engines list too long (max 9)")
    valid = set(DEFAULT_ENGINES) | {"wikipedia", "grokipedia", "yahoo", "bing", "qwant"}
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
_IMPLEMENTED_MODES = ("auto", "neural", "find_similar")


def _validate_mode(mode):
    if mode is None:
        return "auto"
    if not isinstance(mode, str) or mode.lower() not in _IMPLEMENTED_MODES:
        raise SecurityError(f"Invalid mode: {mode!r} (auto|neural|find_similar)")
    return mode.lower()


def _rank(query: str, ranked: list[RawResult], mode: str):
    """Apply neural rerank (the ONLY reranker; BM25 was removed as redundant -
    neural matches its speed and ranks better). Returns (ranked_list, scores,
    mode_used, note).

    mode='auto'/'neural': use the local ONNX cross-encoder if available
    (hound-mcp[all] + model cached), else fall back to cross-engine consensus +
    engine-position order (no lexical rerank). 'neural' surfaces a note when
    unavailable; 'auto' is silent (expected on lean installs).
    """
    note = ""
    if mode in ("neural", "auto"):
        pairs = neural_rerank(query, ranked)
        if pairs is not None:
            return [r for r, _ in pairs], [s for _, s in pairs], "neural", note
        if mode == "neural":
            note = ("neural rerank unavailable - using consensus + engine-position order. " +
                    (unavailable_reason() or "install hound-mcp[all] and retry"))
    # Fallback (lean install / model missing): no lexical rerank. Score by position
    # so tiers derive sensibly; the caller's consensus boost adds the authority
    # signal on top.
    n = len(ranked)
    scores = [1.0 - (i / max(n, 1)) for i in range(n)]
    return list(ranked), scores, "merge", note


def _build_results(query: str, ranked: list[RawResult], scores: Optional[list[float]] = None,
                   total_families: int = 1) -> list[SearchResult]:
    """Convert RawResults (already ranked) into SearchResults with tiers + consensus."""
    total = len(ranked)
    out: list[SearchResult] = []
    for i, r in enumerate(ranked):
        score = scores[i] if scores and i < len(scores) else 0.0
        src = ",".join(r.sources) if r.sources else (r.source or "")
        consensus = f"{max(1, getattr(r, 'consensus', 1))} of {max(1, total_families)}"
        out.append(SearchResult(
            title=r.title, url=r.url, snippet=r.snippet, source=src,
            position=i + 1, relevance_score=round(score, 4),
            fetch_relevance=_tier(score, i + 1, total),
            engines_consensus=consensus,
        ))
    return out


def _quality_filter(results: list[SearchResult], min_keep: int = 3) -> list[SearchResult]:
    """Drop low-relevance results instead of padding to max_results with garbage.
    A result is 'low' if fetch_relevance == 'low'. If dropping all low leaves at
    least min_keep results, drop them; otherwise keep everything (don't go below
    min_keep). Re-numbers positions 1..N after the drop. Niche/ambiguous queries
    thus return fewer good results instead of 6 padded with garbage; clear queries
    keep all (none are 'low'). No quality sacrifice - only garbage is dropped."""
    if len(results) <= min_keep:
        return results
    kept = [r for r in results if r.fetch_relevance != "low"]
    if len(kept) < min_keep:
        return results  # not enough good ones -> keep all rather than go below min_keep
    for i, r in enumerate(kept):
        r.position = i + 1
    return kept


def _apply_consensus_boost(ranked: list[RawResult], scores: list[float]
                           ) -> tuple[list[RawResult], list[float]]:
    """Boost results returned by multiple independent engines (consensus). A free
    authority signal from merging independent indexes: a URL returned by N
    distinct index-families gets score * (1 + 0.25*(N-1)). Consensus AMPLIFIES
    relevance rather than overriding it (a consensus-but-irrelevant result still
    ranks low). Also breaks the neural-saturation tie (ms-marco gives ~1.0 for any
    clearly-relevant snippet; consensus is a discrete 1..N discriminator). Costs
    zero extra fetches (consensus is stamped during merge). Re-sorts by boosted
    score and renormalizes to 0..1 (top = 1.0)."""
    if not ranked:
        return ranked, scores
    boosted = []
    for r, s in zip(ranked, scores):
        c = max(1, getattr(r, "consensus", 1))
        boosted.append((r, s + 0.2 * (c - 1)))   # ADDITIVE: each agreeing family +0.2
    order = {id(r): i for i, (r, _) in enumerate(boosted)}
    boosted.sort(key=lambda rs: (-rs[1], -getattr(rs[0], "consensus", 1), rs[0].position, order[id(rs[0])]))
    # Keep the field in 0..1: renormalize only when an additive consensus bonus
    # pushed a score above 1.0. Otherwise preserve the ranker's scores.
    mx = max((s for _, s in boosted), default=0.0)
    if mx > 1.0:
        boosted = [(r, round(s / mx, 4)) for r, s in boosted]
    else:
        boosted = [(r, round(s, 4)) for r, s in boosted]
    return [r for r, _ in boosted], [s for _, s in boosted]


# ─── main entry ───────────────────────────────────────────────────────────────

async def smart_search(
    server,
    query: str,
    max_results: int = 6,
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
    """Local keyless web search (no API key, no account). The default pool
    (duckduckgo, brave, mojeek, yahoo, yandex, startpage, google, qwant - eight
    independent indexes, all HTTP, no browser; add 'wikipedia' or 'grokipedia')
    is scraped in parallel, merged, deduped, and ranked. A URL returned
    by several independent engines is a consensus hit (engines_consensus field) and
    gets a ranking boost - a free authority signal. Returns URLs + ranking (NOT
    page content) so the agent smart_fetches the ones it wants itself.

    mode: auto (neural rerank if [all]+model present, else consensus + engine-
    position order), neural (same, explicit - surfaces a note if unavailable),
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
    cache_type = (f"search:v5:{max_results}:{site or ''}:{','.join(exclude_sites or [])}:"f"{location or ''}:{language or ''}:{page or 0}:{','.join(engines or [])}:"f"{freshness or ''}:{mode}:{cache_query}")
    if cache_ttl > 0:
        cached = await get_cached(cache_query, cache_type, None, ttl=cache_ttl)
        if cached and cached.get("content"):
            try:
                data = json.loads(cached["content"][0])
                results_list = [SearchResult(**r) for r in data.get("results", [])]
                _eu = data.get("engines_used", [])
                _eb = data.get("engine_blocked", [])
                _rm = data.get("rerank_mode", "merge")
                _rq = data.get("related_queries", [])
                return SearchResponseModel(
                    query=cache_query, results=results_list,
                    total_results=len(results_list), cached=True,
                    engines_used=_eu,
                    engine_blocked=_eb,
                    rerank_mode=_rm,
                    related_queries=_rq,
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
    rerank_used = "merge"
    rerank_note = ""

    # Start the reranker load in parallel with the engine fetch so the cold ONNX
    # model load (~1-2s) overlaps the ~2s diversity quorum instead of stacking
    # AFTER it (the old path paid engine_fetch + model_load sequentially = ~6-7s
    # on the first search). Race-safe via ensure_reranker's lock — shares ONE
    # load with the startup prewarm; awaited below before the rerank step so the
    # result is warm by then (usually already done, loaded during the fetch).
    _rerank_task = asyncio.create_task(ensure_reranker()) if mode in ("neural", "auto", "find_similar") else None

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
        if _rerank_task:
            try:
                await _rerank_task
            except Exception:
                pass
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
                ranked_list, scores, _, _ = _rank(derived_query, ranked, "auto")
                rerank_used = "find_similar"
        else:
            ranked_list, scores, _, _ = _rank(derived_query, ranked, "auto")
            rerank_used = "find_similar"
            if ranked and get_reranker() is None:
                rerank_note = ("find_similar used consensus + position order (neural unavailable). " +
                               (unavailable_reason() or "install hound-mcp[all]"))
        _efams = {_INDEX_FAMILY.get(r.name, r.name) for r in reports if r.ok}
        total_families = len(_efams) or 1
        ranked_list, scores = _apply_consensus_boost(ranked_list, scores)
        ranked_list, scores = ranked_list[:max_results], scores[:max_results]
        results_list = _build_results(cache_query, ranked_list, scores, total_families)
        results_list = _quality_filter(results_list)
        sim_note = f"find_similar to {find_sim_url} (searched: {derived_query[:60]!r})"
        fetch_hint = compute_fetch_hint(results_list)
        fetch_hint = (fetch_hint + " | " + sim_note) if fetch_hint else sim_note
        if rerank_note:
            fetch_hint = (fetch_hint + " | " + rerank_note) if fetch_hint else rerank_note
        sim_related = _related_queries(derived_query, results_list)
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

        if _rerank_task:
            try:
                await _rerank_task
            except Exception:
                pass
        ranked_list, scores, rerank_used, rerank_note = _rank(query, ranked, mode)
        _efams = {_INDEX_FAMILY.get(r.name, r.name) for r in reports if r.ok}
        total_families = len(_efams) or 1
        ranked_list, scores = _apply_consensus_boost(ranked_list, scores)
        ranked_list, scores = ranked_list[:max_results], scores[:max_results]
        results_list = _build_results(query, ranked_list, scores, total_families)
        results_list = _quality_filter(results_list)
        fetch_hint = compute_fetch_hint(results_list)
        if rerank_note:
            fetch_hint = (fetch_hint + " | " + rerank_note) if fetch_hint else rerank_note
        main_related = _related_queries(query, results_list)

    # engines_used = contributed; engine_blocked = did NOT contribute (blocked /
    # timed out / parsed no results / consent page). Surfacing non-contributing
    # engines means an opt-in engine like google that CAPTCHAs is visible to the
    # agent (in engine_blocked), not silently absent from both lists.
    engines_used = list(dict.fromkeys(r.name for r in reports if r.ok))
    engine_blocked = list(dict.fromkeys(r.name for r in reports if r.blocked))

    # Agent QoL: when some engines didn't contribute but results came back from
    # the rest, say so plainly so the agent knows the results are partial + a
    # retry may add recall (instead of looking like a failure).
    if engine_blocked and results_list:
        _blk_note = (f"Engines {', '.join(engine_blocked)} didn't contribute (rate-limited/timed out/no results); "
                     f"results are from the rest - retry shortly for more recall.")
        fetch_hint = (fetch_hint + " | " + _blk_note) if fetch_hint else _blk_note

    # Cache successful results (+ engine metadata + related queries for cache hits)
    if cache_ttl > 0 and results_list:
        _rq_cache = sim_related if mode == "find_similar" else main_related
        cache_data = json.dumps({
            "results": [r.model_dump() for r in results_list],
            "engines_used": engines_used,
            "engine_blocked": engine_blocked,
            "rerank_mode": rerank_used,
            "related_queries": _rq_cache,
        })
        await set_cached(cache_query, cache_type, [cache_data], 200, None, cache_ttl)

    return SearchResponseModel(
        query=cache_query, results=results_list, total_results=len(results_list),
        engines_used=engines_used, engine_blocked=engine_blocked,
        rerank_mode=rerank_used,
        related_queries=(sim_related if mode == "find_similar" else main_related),
        duration_ms=(time() - t0) * 1000, error=error,
        fetch_hint=fetch_hint,
        summary=_search_summary(cache_query, results_list, engines_used, rerank_used),
        next_action=_search_next_action(results_list, engine_blocked, error),
    )

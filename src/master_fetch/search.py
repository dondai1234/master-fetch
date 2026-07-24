"""Hound local web search (v8: multi-signal ranking, zero-latency quality signals).

Scrapes public search engines (DuckDuckGo, Brave, Mojeek, Yahoo, Yandex,
Startpage, Google, Qwant + opt-in Wikipedia, Grokipedia) + auto-fired
specialized JSON-API backends (Semantic Scholar, GitHub, Hacker News) via the
hound-native
engine layer in search_engines.py - no third-party API, no key, no account.
Results are merged across engines, deduped by normalized URL, and ranked by a
six-signal composite: neural cross-encoder (topical relevance) + cross-engine
consensus (authority) + domain reputation (source quality for query intent) +
answer-signal scoring (snippet contains actual answer data vs just discussing
the topic) + title relevance (query terms in the result title) + URL relevance
(query terms in the result URL path). All signals are zero-latency (operate on
already-fetched snippets, titles, and URLs - no extra HTTP calls).

Snippets from multiple engines are MERGED when the same URL appears in several
backends, giving the agent richer information per result without fetching pages.

Returns URLs + ranking + snippets, NOT page content. The agent smart_fetches
the 1-2 best results with focus= to get page content. This is the efficient
workflow: one search (fast, no page fetches) + 1-2 targeted fetches (accurate,
get exactly the content needed).

Rerank: neural (local ONNX cross-encoder on snippets, needs [all]), find_similar
(pass url=, find pages similar to it). Lean installs without the model fall back
to cross-engine consensus + domain + answer-signal scoring (no neural).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from datetime import datetime
from time import time
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from master_fetch.cache import get_cached, set_cached
from master_fetch.security import validate_search_query, validate_url, redact_api_key, SecurityError
from master_fetch.search_engines import (
    RawResult, multi_search, EngineReport, DEFAULT_ENGINES,
    fetch_source_for_similar, _INDEX_FAMILY,
)

logger = logging.getLogger("master-fetch.search")


# ─── domain reputation + answer-signal scoring (zero-latency quality signals) ─
# These run AFTER neural rerank, operating on already-fetched snippets. They
# address the "discusses vs contains" gap: a Medium blog post titled "The
# Evolution of GPT" scores high on topical relevance but contains zero data.
# A GitHub repo with "d_model=12288, layers=96" in the snippet scores similarly
# on neural but CONTAINS the answer. Domain reputation + answer signals break
# that tie toward the result that actually has the data.

_TECH_DOMAINS = frozenset({
    "github.com", "arxiv.org", "stackoverflow.com", "docs.python.org",
    "huggingface.co", "paperswithcode.com", "pytorch.org", "tensorflow.org",
    "developer.mozilla.org", "kaggle.com", "pypi.org", "npmjs.com",
    "raw.githubusercontent.com", "dl.acm.org", "acm.org", "ieee.org",
    "openreview.net", "semanticscholar.org", "scholar.google.com",
    "docs.anthropic.com", "docs.openai.com", "platform.openai.com",
    "openai.com", "anthropic.com", "ai.meta.com", "research.google.com",
    "deepmind.google", "mistral.ai", "x.ai", "grok.com", "deepseek.com",
})
_REFERENCE_DOMAINS = frozenset({
    "wikipedia.org", "wikimedia.org", "britannica.com",
})

# Keywords that indicate a technical/factual query (not an opinion/news query).
_TECH_QUERY_SIGNALS = frozenset({
    "model", "architecture", "d_model", "embedding", "dimension", "api",
    "code", "implement", "benchmark", "paper", "arxiv", "github",
    "algorithm", "neural", "transformer", "layer", "parameter",
    "config", "specification", "spec", "schema", "table", "comparison",
    "matrix", "tensor", "precision", "throughput", "latency",
    "inference", "training", "fine-tun", "quantiz", "attention",
    "tokeniz", "vocab", "hidden", "encoder", "decoder", "dense",
})

# Answer-signal patterns: detect if the snippet CONTAINS the answer, not just
# discusses the topic. Each pattern is gated on a query-type check so it only
# fires when the query asks for that type of answer.
_DIGIT_RE = re.compile(r"\d{3,}")  # 3+ digits = likely a real value, not a year
_TABLE_MARKERS = ("table", "|", "column", "row", "\t")
_CODE_MARKERS = ("def ", "func ", "class ", "import ", "const ", "```", "=> ", "fn ")
_COMPARISON_MARKERS = ("vs", "compared", "while", "however", "whereas", "better", "faster", "larger")


def _domain_boost(url: str, query: str, is_technical: bool) -> float:
    """Boost authoritative domains for the query type. Returns 0.0-0.15.
    Not a blocklist: Medium/Substack are simply not boosted, not penalized.
    Checks subdomain matching (en.wikipedia.org matches wikipedia.org)."""
    try:
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
    except Exception:
        return 0.0
    if not host:
        return 0.0
    # Check exact match or subdomain match (e.g. en.wikipedia.org matches wikipedia.org)
    def _matches_domain_set(domain_set: frozenset[str]) -> bool:
        if host in domain_set:
            return True
        for d in domain_set:
            if host.endswith(f".{d}"):
                return True
        return False
    if _matches_domain_set(_TECH_DOMAINS) and is_technical:
        return 0.15
    if _matches_domain_set(_REFERENCE_DOMAINS):
        return 0.05
    return 0.0


def _answer_signal_score(query: str, snippet: str, is_technical: bool) -> float:
    """Detect if the snippet contains actual answer data vs just discussing the topic.
    Returns 0.0-0.3 additive boost. Zero-latency (regex on already-fetched text)."""
    if not snippet:
        return 0.0
    boost = 0.0
    q_lower = query.lower()
    s_lower = snippet.lower()

    # Query asks for numbers/sizes/dimensions -> boost snippets with real digits
    if is_technical or any(w in q_lower for w in (
        "dimension", "size", "parameters", "count", "number", "how many",
        "value", "d_model", "embedding", "layers", "heads",
    )):
        if _DIGIT_RE.search(snippet):
            boost += 0.15

    # Query asks for a table -> boost snippets with table markers
    if "table" in q_lower or "comparison" in q_lower:
        if any(w in s_lower for w in _TABLE_MARKERS):
            boost += 0.15

    # Query asks for comparison -> boost snippets with comparison structure
    if any(w in q_lower for w in ("vs", "compare", "comparison", "difference", "better")):
        if any(w in s_lower for w in _COMPARISON_MARKERS):
            boost += 0.10

    # Query mentions code/API -> boost snippets with code markers
    if any(w in q_lower for w in ("code", "api", "function", "method", "implement", "example", "snippet")):
        if any(w in s_lower for w in _CODE_MARKERS):
            boost += 0.10

    return min(boost, 0.30)


def _is_technical_query(query: str) -> bool:
    """Detect if the query is technical/factual (vs opinion/news/how-to)."""
    q_lower = query.lower()
    return any(sig in q_lower for sig in _TECH_QUERY_SIGNALS)


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


# ─── query intelligence: intent detection + multi-query fan-out (v12) ─────────
# The core v12 innovation: instead of sending one query verbatim to all 8
# engines, detect the query intent and generate an expanded query variant that
# is more likely to surface primary sources. Different engines get different
# query variants (per-engine query_map), so the total request count stays the
# same (8 requests, not 16) but recall increases dramatically because different
# queries surface different pages. Zero-latency: all engines run in parallel,
# same deadline. Cross-variant consensus: a URL surfaced by different queries
# from different engines is a STRONGER authority signal than one from a single
# query.

_INTENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Ambiguous standalone words removed (e.g., "code" → "area code", "paper" →
    # "toilet paper", "update" → "database update"). Replaced with compounds that
    # are unambiguous in search context. False negatives (missed intent) are cheap
    # (same as v11, no expansion). False positives are expensive (dilute diversity
    # engines with irrelevant expansion terms).
    ("comparison", re.compile(r"\b(?:vs\.?|versus|compare|comparison|difference\s+between|pros\s+and\s+cons|which\s+is\s+better)\b", re.I)),
    ("howto", re.compile(r"\b(?:how\s+to|how\s+do\s+i|guide|tutorial|step\s+by\s+step|walkthrough)\b", re.I)),
    ("research", re.compile(r"\b(?:arxiv|research|benchmark|literature|state\s+of\s+the\s+art|case\s+study|white\s+paper)\b", re.I)),
    ("code", re.compile(r"\b(?:implement|function|api|method|class|snippet|script|debug|error|exception|source\s+code|code\s+example)\b", re.I)),
    ("reference", re.compile(r"\b(?:what\s+is|definition|explain|meaning|overview|introduction|understand)\b", re.I)),
    ("news", re.compile(r"\b(?:latest|newest|recent|announcement|breaking|changelog|release\s+notes|new\s+release|just\s+released)\b", re.I)),
]

_INTENT_EXPANSIONS: dict[str, str] = {
    "research": " paper arxiv benchmark results",
    "factual": " specifications table data parameters",
    # Other intents (code, news, howto, comparison, reference, general) do NOT
    # get expansion. Testing showed expanded terms for these intents returned
    # tutorial spam and beginner guides instead of primary sources. For research/
    # factual, the expansion terms ("paper arxiv", "specifications table") actually
    # help diversity engines surface primary sources the original query missed.
    "comparison": "",
    "howto": "",
    "code": "",
    "reference": "",
    "news": "",
    "general": "",
}

# Expanded set of data/spec signal words for factual intent detection. Catches
# queries asking for concrete numbers/config, not just general tech discussion.
_FACTUAL_DATA_WORDS = frozenset({
    "dimension", "size", "parameters", "count", "value", "spec",
    "specification", "d_model", "architecture", "layer",
    "config", "hidden", "precision", "vocab", "vocabulary",
    "encoder", "decoder", "context", "window", "throughput",
    "latency", "memory", "token", "batch", "sequence", "flops",
    "compute", "gpu", "head", "heads", "embedding", "optimizer",
})


def _detect_intent(query: str) -> str:
    """Detect query intent for multi-query fan-out. Returns one of:
    comparison, howto, research, code, reference, news, factual, general.
    Rule-based pattern matching (no LLM needed). Priority order: comparison >
    howto > research > code > reference > news > factual > general."""
    q_lower = query.lower()
    for intent, pattern in _INTENT_PATTERNS:
        if pattern.search(q_lower):
            return intent
    # Factual: technical query with data/spec signals (not caught by patterns)
    if _is_technical_query(query) and any(
        w in q_lower for w in _FACTUAL_DATA_WORDS
    ):
        return "factual"
    return "general"


def _expand_query(query: str, intent: str) -> str:
    """Generate an expanded query variant by appending intent-specific terms.
    Only appends terms NOT already in the query. Returns the original query
    unchanged if no expansion applies (general intent, all terms present, or
    query too long). The expanded query surfaces pages that contain primary-
    source data (tables, specs, code examples) rather than blog posts that
    merely discuss the topic."""
    expansion = _INTENT_EXPANSIONS.get(intent, "")
    if not expansion:
        return query
    # Don't expand very long queries — expansion terms could exceed engine
    # query-length limits (most engines cap at ~256-2048 chars).
    if len(query.split()) >= 15:
        return query
    # Dynamic year for news intent (avoid hardcoded stale year).
    if intent == "news":
        expansion = expansion.replace("{year}", str(datetime.now().year))
    q_lower = query.lower()
    new_terms = [t for t in expansion.split() if t.lower() not in q_lower]
    if not new_terms:
        return query
    return query + " " + " ".join(new_terms)


# Specialized JSON-API backends that fire when the query intent matches.
# Each searches an authoritative index directly, complementing the 8 general
# HTML-scraping engines. They run in parallel (zero added latency) and merge
# into the same ranking pipeline. Only added when engines is None (default pool)
# so explicit engine selections are respected.
_INTENT_BACKENDS: dict[str, list[str]] = {
    "research": ["semantic_scholar"],
    "factual": ["semantic_scholar"],
    "code": ["github_api"],
    "news": ["hackernews"],
    "howto": ["hackernews"],
    "reference": ["wikipedia"],
    "comparison": [],
    "general": [],
}


def _intent_backends(intent: str) -> list[str]:
    """Return specialized backend names for the given intent. Empty for
    comparison/general (8 general engines suffice)."""
    return _INTENT_BACKENDS.get(intent, [])


def _generate_query_map(query: str, intent: str, engines: list[str]) -> dict[str, str]:
    """Assign per-engine query variants for multi-query fan-out.

    Core engines (DDG, Brave, Mojeek, Yahoo) get the original query (most
    reliable for general matching). Diversity engines (Yandex, Startpage,
    Google, Qwant) get the expanded query (different indexes surface different
    results with the expanded terms, surfacing primary sources the original
    query missed). Returns {} if no expansion applies (all engines get the
    same original query = backward-compatible with v11)."""
    expanded = _expand_query(query, intent)
    if expanded == query:
        return {}
    core = {"duckduckgo", "brave", "mojeek", "yahoo"}
    query_map: dict[str, str] = {}
    for eng in engines:
        query_map[eng] = query if eng in core else expanded
    return query_map


# ─── result diversity: prevent same-domain domination ─────────────────────
# After quality boost, if 3+ results are from the same domain (e.g., all
# medium.com), defer the excess to the bottom so top results are from diverse
# sources. The agent gets a broader view instead of 4 variations of the same
# blog post. Only applied when no site: filter is set (user didn't restrict
# to one domain).

def _get_domain(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def _diversify(ranked: list[RawResult], scores: list[float],
                max_per_domain: int = 2) -> tuple[list[RawResult], list[float]]:
    """Limit same-domain results in top positions. Excess results from an
    already-represented domain are deferred to the bottom (not dropped).
    Returns re-ordered (ranked, scores) with same objects (id() preserved for
    quality_signals mapping)."""
    if not ranked:
        return ranked, scores
    domain_counts: Counter = Counter()
    kept: list[RawResult] = []
    kept_scores: list[float] = []
    deferred: list[RawResult] = []
    deferred_scores: list[float] = []
    for r, s in zip(ranked, scores):
        domain = _get_domain(r.url)
        if domain_counts[domain] < max_per_domain:
            kept.append(r)
            kept_scores.append(s)
            domain_counts[domain] += 1
        else:
            deferred.append(r)
            deferred_scores.append(s)
    return kept + deferred, kept_scores + deferred_scores


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


def _related_queries(query: str, results: list["SearchResult"], *, n: int = 5) -> list[str]:
    """Mine follow-up queries from result titles + snippets.

    Ranks bigrams by document frequency (in how many results they appear),
    drops bigrams that overlap the original query or duplicate each other.
    Only returns multi-word phrases (bigrams+) — single-word unigrams are almost
    never useful as search queries ("function", "descent") and agents waste calls
    searching them. Requires bigrams to appear in 3+ results (not just 2) to
    surface genuine patterns, not coincidental word adjacency.
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
    for words in docs:
        uniq_bi = set()
        for i in range(len(words) - 1):
            a, b = words[i], words[i + 1]
            if len(a) < 3 or len(b) < 3:
                continue
            uniq_bi.add(f"{a} {b}")
        for bi in uniq_bi:
            bigram_docfreq[bi] += 1

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
        if df < 3:  # appears in fewer than 3 results -> not a strong pattern
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

    return out[:n]


# ─── response model ──────────────────────────────────────────────────────────

class SearchResult(BaseModel):
    title: str = Field(description="Result title")
    url: str = Field(description="Result URL")
    snippet: str = Field(default="", description="Result snippet from the engine")
    source: str = Field(default="", description="Backend(s) that returned this result (duckduckgo/brave/mojeek/yahoo/yandex/startpage/google/wikipedia/grokipedia). Multiple = cross-backend consensus.")
    position: int = Field(default=0, description="1-indexed rank after merge + rerank")
    relevance_score: float = Field(default=0.0, description="0.0-1.0 relevance to the query (neural cross-encoder score in neural mode, min-max normalized), boosted by cross-backend consensus + domain reputation + answer-signal scoring. 1.0 = most relevant in this set.")
    fetch_relevance: str = Field(default="", description="high|med|low - relative relevance hint incorporating neural score + consensus + domain reputation + answer signals. smart_fetch what matches your need; the tiers rank results but a lower tier can be the right one - use your judgment.")
    engines_consensus: str = Field(default="", description="How many independent indexes returned this URL (e.g. '3 of 4'). A free authority signal: a URL returned by several independent engines is more likely authoritative.")
    source_type: str = Field(default="", description="Source type from URL pattern: docs|paper|repo|blog|forum|reference|news|other. Helps pick the right source: docs for API docs, paper for research, repo for code, forum for Q&A, reference for encyclopedic.")


class SearchResponseModel(BaseModel):
    query: str = Field(description="Search query")
    results: list[SearchResult] = Field(description="Ranked search results (URLs + ranking + snippets, NOT page content). smart_fetch the best results to get content.")
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


# ─── source type detection (zero-latency URL pattern matching) ──────────────
# Classifies results by URL pattern so the agent can pick the right type of
# source: docs for API docs, paper for research, repo for code, forum for Q&A,
# reference for encyclopedic. Helps the agent skip blog posts when it needs
# primary sources, or skip forums when it needs official docs.

_DOCS_DOMAINS = frozenset({
    "docs.python.org", "docs.anthropic.com", "docs.openai.com", "platform.openai.com",
    "pytorch.org", "tensorflow.org", "developer.mozilla.org", "developers.google.com",
    "docs.github.com", "docs.microsoft.com", "learn.microsoft.com", "docs.rs",
    "go.dev", "doc.rust-lang.org", "kotlinlang.org", "dart.dev", "flutter.dev",
    "nodejs.org", "react.dev", "vuejs.org", "angular.io", "svelte.dev",
})
_PAPER_DOMAINS = frozenset({
    "arxiv.org", "dl.acm.org", "acm.org", "ieee.org", "ieeexplore.ieee.org",
    "openreview.net", "semanticscholar.org", "scholar.google.com",
    "paperswithcode.com", "papers.ssrn.com", "biorxiv.org", "medrxiv.org",
})
_REPO_DOMAINS = frozenset({
    "github.com", "gitlab.com", "bitbucket.org", "codeberg.org",
    "sourceforge.net", "raw.githubusercontent.com",
})
_FORUM_DOMAINS = frozenset({
    "stackoverflow.com", "serverfault.com", "superuser.com", "askubuntu.com",
    "reddit.com", "news.ycombinator.com", "discourse.org", "discuss.python.org",
    "forum.vuejs.org", "groups.google.com", "community.openai.com",
})
_BLOG_DOMAINS = frozenset({
    "medium.com", "substack.com", "dev.to", "hashnode.com", "blog.google",
    "towardsdatascience.com", "freecodecamp.org",
})
_REFERENCE_DOMAINS_SET = frozenset({
    "wikipedia.org", "wikimedia.org", "britannica.com", "en.wikipedia.org",
})
_NEWS_DOMAINS = frozenset({
    "reuters.com", "bloomberg.com", "techcrunch.com", "theverge.com",
    "arstechnica.com", "wired.com", "cnet.com", "zdnet.com",
})


def _source_type(url: str) -> str:
    """Classify a URL by domain pattern. Returns one of:
    docs, paper, repo, forum, reference, blog, news, other."""
    try:
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
    except Exception:
        return "other"
    if not host:
        return "other"
    def _matches(domain_set: frozenset[str]) -> bool:
        if host in domain_set:
            return True
        for d in domain_set:
            if host.endswith(f".{d}"):
                return True
        return False
    if _matches(_DOCS_DOMAINS):
        return "docs"
    if _matches(_PAPER_DOMAINS):
        return "paper"
    if _matches(_REPO_DOMAINS):
        return "repo"
    if _matches(_FORUM_DOMAINS):
        return "forum"
    if _matches(_REFERENCE_DOMAINS_SET):
        return "reference"
    if _matches(_NEWS_DOMAINS):
        return "news"
    if _matches(_BLOG_DOMAINS):
        return "blog"
    # Path-based heuristics for unlisted domains
    try:
        path = (urlparse(url).path or "").lower()
    except Exception:
        path = ""
    if "/docs/" in path or "/documentation/" in path or "/api/" in path:
        return "docs"
    if "/blog/" in path or "/post/" in path or "/article/" in path:
        return "blog"
    if "/forum/" in path or "/thread/" in path or "/discussion/" in path:
        return "forum"
    return "other"


# ─── title + URL relevance scoring (zero-latency lexical signals) ────────────
# Boost results whose TITLE or URL PATH contains query terms. Neural rerank
# scores (query, title+snippet) together, but title relevance is an independent
# strong signal: a result titled "GPT-3 Architecture: d_model=12288" is more
# relevant than "The Evolution of AI" for a query about "d_model". URL relevance
# catches path-based relevance: /docs/api/models/gpt-3 > /blog/2023/my-thoughts.


def _query_terms(query: str) -> set[str]:
    """Extract meaningful query terms (words >= 3 chars, not stopwords)."""
    return {w.lower() for w in _WORD_RE.findall(query or "") if w.lower() not in _STOPWORDS and len(w) >= 3}


def _title_relevance(query: str, title: str) -> float:
    """Boost for query terms appearing in the title. Up to +0.10.
    More query terms in title = stronger signal."""
    if not title:
        return 0.0
    q_terms = _query_terms(query)
    if not q_terms:
        return 0.0
    title_lower = title.lower()
    hits = sum(1 for t in q_terms if t in title_lower)
    if hits == 0:
        return 0.0
    return min(hits / len(q_terms), 1.0) * 0.10


def _url_relevance(query: str, url: str) -> float:
    """Boost for query terms appearing in the URL path. Up to +0.08.
    Path-based relevance: /docs/api/models/gpt-3 is more relevant than /blog/random."""
    if not url:
        return 0.0
    q_terms = _query_terms(query)
    if not q_terms:
        return 0.0
    try:
        path = (urlparse(url).path or "").lower()
    except Exception:
        return 0.0
    if not path or path == "/":
        return 0.0
    hits = sum(1 for t in q_terms if t in path)
    if hits == 0:
        return 0.0
    return min(hits / len(q_terms), 1.0) * 0.08


# ─── tier derivation + hint ──────────────────────────────────────────────────

def _tier(score: float, rank: int, total: int, *, consensus: int = 1,
           domain_boosted: bool = False, answer_signals: float = 0.0) -> str:
    """Derive high|med|low from relevance score + rank + quality signals.
    Top result is never 'low'. High consensus (3+ engines), domain reputation,
    or strong answer signals can promote a result to 'high' even with a medium
    neural score - this prevents a blog post from getting 'high' while a GitHub
    repo with the actual data gets 'med'."""
    if score >= 0.5 or rank == 1:
        return "high"
    # Quality-signal promotion: consensus 3+ OR domain-boosted OR strong answer
    # signals (>= 0.15) bump a medium-score result up to 'high'.
    if (consensus >= 3 or domain_boosted or answer_signals >= 0.15) and score >= 0.25:
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
    # Source type breakdown: show the agent what types of sources it got
    type_counts: Counter = Counter()
    for r in results:
        if r.source_type:
            type_counts[r.source_type] += 1
    type_str = ", ".join(f"{t}:{c}" for t, c in type_counts.most_common()) if type_counts else ""
    # Concrete fetch count: give the agent a specific number, not "what fits"
    if high >= 3:
        fetch_rec = "smart_fetch #1-2 (high-quality, diverse sources)"
    elif high >= 1:
        fetch_rec = f"smart_fetch #1 (high), consider #2 if more detail needed"
    elif med >= 2:
        fetch_rec = "smart_fetch #1-2 (med quality), rephrase if thin"
    else:
        fetch_rec = "smart_fetch #1, rephrase if results are thin"
    hint = f"{high} high, {med} med, {low} low. {fetch_rec}."
    if type_str:
        hint += f" Source types: {type_str}."
    return hint


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
                         error: str, query: str = "") -> str:
    """An intent-aware, source-type-aware nudge that points the agent at specific
    results instead of a generic 'fetch 1-2'. The agent reads this field every
    turn, so it must be concrete and actionable — not wishy-washy.

    Strategy:
    - Detect intent (reference, code, comparison, factual, howto, research, general)
    - Point to specific result positions (#1, #1-2, #1-3)
    - Include a focus= suggestion when the query is specific enough
    - Note snippet sufficiency: if the top snippet is long and the query is
      simple (reference/factual), tell the agent to check the snippet first
    - Recommend source types that match the intent
    """
    if not results:
        if error and ("rate-limited" in error.lower() or "timed out" in error.lower() or engine_blocked):
            return ("No results (engines rate-limited/timed out). Retry in a moment, "
                    "or set HOUND_SEARCH_PROXY for sustained heavy use.")
        return "No results. Rephrase (more specific / different terms) or try mode=neural for semantic matching."
    high = [r for r in results if r.fetch_relevance == "high"]
    # Detect intent for tailored guidance
    intent = _detect_intent(query) if query else "general"
    top = results[0]
    top_domain = _get_domain(top.url)
    # Snippet sufficiency: long snippet from a reference/known domain on a
    # simple query likely already has the answer. Tells the agent to check
    # before committing to a fetch.
    snippet_sufficient = (
        len(top.snippet) > 200
        and intent in ("reference", "factual", "general")
        and top.source_type in ("reference", "docs")
    )
    # Build a focus= suggestion from the query (shortened to key terms)
    focus_terms = query.split()[:5]  # first 5 words is a good focus hint
    focus_hint = " ".join(focus_terms) if focus_terms else ""
    if intent == "comparison":
        base = f"Fetch #1-2 from different sources for a balanced comparison."
        if snippet_sufficient:
            base = f"#1 snippet may contain key data — check it first. {base}"
    elif intent == "code":
        # Prefer repo/docs sources for code queries
        code_sources = [r for r in results if r.source_type in ("repo", "docs")]
        if code_sources:
            pos = results.index(code_sources[0]) + 1
            base = f"smart_fetch #{pos} ({top_domain if pos == 1 else _get_domain(code_sources[0].url)}, {code_sources[0].source_type})"
            if focus_hint:
                base += f" with focus='{focus_hint}'"
            base += ". Prefer repo/docs for code."
        else:
            base = f"smart_fetch #1 ({top_domain}, {top.source_type})"
            if focus_hint:
                base += f" with focus='{focus_hint}'"
    elif intent == "research":
        paper_sources = [r for r in results if r.source_type in ("paper", "repo")]
        if paper_sources:
            pos = results.index(paper_sources[0]) + 1
            base = f"smart_fetch #{pos} ({_get_domain(paper_sources[0].url)}, {paper_sources[0].source_type}) for primary source."
        else:
            base = "smart_fetch #1-2. Prefer paper/repo sources for research."
    elif intent in ("reference", "factual"):
        if snippet_sufficient:
            base = f"#1 snippet ({top_domain}, {top.source_type}) may already have the answer — check it first. smart_fetch #1 if you need more detail."
        else:
            base = f"smart_fetch #1 ({top_domain}, {top.source_type})"
            if focus_hint:
                base += f" with focus='{focus_hint}'"
    elif intent == "howto":
        base = "smart_fetch #1-2. Prefer docs/forum sources for how-to guides."
        if focus_hint:
            base += f" Use focus='{focus_hint}' to extract only the relevant steps."
    else:  # general
        n_high = len(high)
        if n_high >= 3:
            base = f"smart_fetch #1-2 (top results are high-quality)."
        elif n_high >= 1:
            base = f"smart_fetch #1 (high). Fetch #2 if #1 is insufficient."
        else:
            base = "No 'high' matches — rephrase (more specific) or try mode=neural."
    if engine_blocked:
        base += " Some engines didn't contribute; retry shortly for more recall."
    return base


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
    if len(engines) > 12:
        raise SecurityError("engines list too long (max 12)")
    valid = set(DEFAULT_ENGINES) | {"wikipedia", "grokipedia", "yahoo", "bing", "qwant",
                                   "semantic_scholar", "github_api", "hackernews"}
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
                   total_families: int = 1, quality_signals: Optional[dict] = None
                   ) -> list[SearchResult]:
    """Convert RawResults (already ranked) into SearchResults with tiers + consensus + source_type."""
    total = len(ranked)
    out: list[SearchResult] = []
    for i, r in enumerate(ranked):
        score = scores[i] if scores and i < len(scores) else 0.0
        src = ",".join(r.sources) if r.sources else (r.source or "")
        consensus_n = max(1, getattr(r, "consensus", 1))
        consensus = f"{consensus_n} of {max(1, total_families)}"
        stype = _source_type(r.url)
        # Pull per-result quality signals for smarter tier calculation
        signals = (quality_signals or {}).get(id(r), {})
        out.append(SearchResult(
            title=r.title, url=r.url, snippet=r.snippet, source=src,
            position=i + 1, relevance_score=round(score, 4),
            fetch_relevance=_tier(
                score, i + 1, total,
                consensus=consensus_n,
                domain_boosted=signals.get("domain_boosted", False),
                answer_signals=signals.get("answer_signals", 0.0),
            ),
            engines_consensus=consensus,
            source_type=stype,
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


def _apply_quality_boost(ranked: list[RawResult], scores: list[float],
                           query: str
                           ) -> tuple[list[RawResult], list[float]]:
    """Apply consensus + domain reputation + answer-signal boosts in ONE pass,
    then renormalize to 0..1. A free authority signal from merging independent
    indexes: a URL returned by N distinct index-families gets +0.2 * (N-1).
    Domain reputation adds +0.15 for known authoritative tech domains on technical
    queries, +0.05 for reference domains. Answer-signal scoring adds up to +0.30
    for snippets that contain actual answer data (numbers, tables, code) vs just
    discussing the topic. Title relevance adds up to +0.10 for query terms in
    the result title. URL relevance adds up to +0.08 for query terms in the URL
    path. All additive; consensus AMPLIFIES relevance rather than overriding it
    (a consensus-but-irrelevant result still ranks low). Re-sorts by boosted
    score and renormalizes to 0..1 (top = 1.0). Costs zero extra fetches.
    Returns (ranked, scores, quality_signals) where quality_signals maps id(result)
    -> {domain_boosted, answer_signals} for smarter tier calculation."""
    if not ranked:
        return ranked, scores, {}
    is_tech = _is_technical_query(query)
    boosted = []
    quality_signals: dict = {}
    for r, s in zip(ranked, scores):
        c = max(1, getattr(r, "consensus", 1))
        consensus_boost = 0.2 * (c - 1)
        domain_b = _domain_boost(r.url, query, is_tech)
        signal_b = _answer_signal_score(query, r.snippet, is_tech)
        title_b = _title_relevance(query, r.title)
        url_b = _url_relevance(query, r.url)
        total_boost = consensus_boost + domain_b + signal_b + title_b + url_b
        boosted.append((r, s + total_boost))
        quality_signals[id(r)] = {
            "domain_boosted": domain_b > 0,
            "answer_signals": signal_b,
        }
    order = {id(r): i for i, (r, _) in enumerate(boosted)}
    boosted.sort(key=lambda rs: (-rs[1], -getattr(rs[0], "consensus", 1), rs[0].position, order[id(rs[0])]))
    # Renormalize to 0..1 only when a boost pushed a score above 1.0.
    mx = max((s for _, s in boosted), default=0.0)
    if mx > 1.0:
        boosted = [(r, round(s / mx, 4)) for r, s in boosted]
    else:
        boosted = [(r, round(s, 4)) for r, s in boosted]
    return [r for r, _ in boosted], [s for _, s in boosted], quality_signals


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
    gets a ranking boost - a free authority signal. Returns URLs + ranking + snippets
    (NOT page content) so the agent smart_fetches the ones it wants itself.

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

    # Preserve an explicit region for cache identity before deriving the
    # location/language default used by search engines.
    cache_region = str(region).strip().lower() if region is not None else None
    # region derives from location/language if not given (e.g. "US" -> "us-en").
    if region is None:
        loc = (location or "US").lower()
        lang = (language or "en").lower()
        region = f"{loc}-{lang}" if len(loc) == 2 else "us-en"

    cache_query = find_sim_url or query
    cache_type = (f"search:v12:{max_results}:{site or ''}:{','.join(exclude_sites or [])}:"
                  f"{location or ''}:{language or ''}:{page or 0}:{','.join(engines or [])}:"
                  f"{freshness or ''}:{mode}:{cache_query}")
    # An explicit region is passed through to the engines, so keep it in the
    # cache identity. Omitted regions retain the existing cache key behavior.
    if cache_region is not None:
        cache_type = f"{cache_type}:region={cache_region}"
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
                    next_action=_search_next_action(results_list, _eb, "", cache_query),
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
        _fs_intent = _detect_intent(derived_query)
        _fs_engines = list(engines) if engines else list(DEFAULT_ENGINES)
        _fs_specialized: list[str] = []
        _fs_byok_names: list[str] = []
        if engines is None:
            _fs_specialized = _intent_backends(_fs_intent)
            try:
                from master_fetch.search_api_keys import get_byok_engines
                _fs_byok_names = list(get_byok_engines().keys())
            except Exception:
                pass
            if _fs_byok_names:
                _fs_engines = [_fs_byok_names[0]] + _fs_specialized
            else:
                for b in _fs_specialized:
                    if b not in _fs_engines:
                        _fs_engines.append(b)
        _fs_qmap = _generate_query_map(derived_query, _fs_intent, _fs_engines)
        for b in _fs_specialized:
            _fs_qmap[b] = derived_query
        try:
            ranked, reports = await multi_search(
                derived_query, max_results, engines=_fs_engines, site=site,
                exclude_sites=exclude_sites, region=region, freshness=freshness,
                page=page, server=server,
                query_map=_fs_qmap if _fs_qmap else None,
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
        ranked_list, scores, qsig = _apply_quality_boost(ranked_list, scores, derived_query)
        ranked_list, scores = ranked_list[:max_results], scores[:max_results]
        results_list = _build_results(cache_query, ranked_list, scores, total_families, quality_signals=qsig)
        results_list = _quality_filter(results_list)
        sim_note = f"find_similar to {find_sim_url} (searched: {derived_query[:60]!r})"
        fetch_hint = compute_fetch_hint(results_list)
        fetch_hint = (fetch_hint + " | " + sim_note) if fetch_hint else sim_note
        if rerank_note:
            fetch_hint = (fetch_hint + " | " + rerank_note) if fetch_hint else rerank_note
        sim_related = _related_queries(derived_query, results_list)
    else:
        _intent = _detect_intent(query)
        _specialized: list[str] = []
        _byok_names: list[str] = []
        if engines is None:
            _specialized = _intent_backends(_intent)
            # BYOK: when user has configured API keys, use ONLY the first BYOK
            # provider + specialized backends. NO local keyless engines.
            # The whole point of BYOK is to avoid IP rate limiting from scraping
            # public search engines. Local engines run only as fallback when
            # all BYOK providers are exhausted.
            try:
                from master_fetch.search_api_keys import get_byok_engines
                _byok_names = list(get_byok_engines().keys())
            except Exception:
                pass
            if _byok_names:
                _effective_engines = [_byok_names[0]] + _specialized
            else:
                _effective_engines = list(DEFAULT_ENGINES)
                for b in _specialized:
                    if b not in _effective_engines:
                        _effective_engines.append(b)
        else:
            _effective_engines = list(engines)
        # No multi-query fan-out in BYOK mode: the BYOK provider is a single
        # high-quality source, query expansion is unnecessary.
        _qmap = _generate_query_map(query, _intent, _effective_engines) if not _byok_names else {}
        for b in _specialized:
            _qmap[b] = query
        try:
            ranked, reports = await multi_search(
                query, max_results, engines=_effective_engines, site=site,
                exclude_sites=exclude_sites, region=region, freshness=freshness,
                page=page, server=server,
                query_map=_qmap if _qmap else None,
            )
        except Exception as e:
            error = redact_api_key(str(e)[:200])

        # BYOK fallback: if the first BYOK provider returned nothing, try the
        # remaining providers. If all BYOK providers are exhausted, fall back
        # to local keyless engines so the search never fails.
        if _byok_names and not ranked:
            error = ""
            for next_byok in _byok_names[1:]:
                try:
                    ranked, reports = await multi_search(
                        query, max_results, engines=[next_byok] + _specialized, site=site,
                        exclude_sites=exclude_sites, region=region, freshness=freshness,
                        page=page, server=server,
                    )
                    if ranked:
                        break
                except Exception as e:
                    error = redact_api_key(str(e)[:200])
                    continue
            if not ranked:
                try:
                    ranked, reports = await multi_search(
                        query, max_results, engines=list(DEFAULT_ENGINES), site=site,
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
        # Pre-filter to 2x max_results before neural rerank. The metasearch returns
        # up to 34 unique URLs across engines; neural reranking ALL of them takes
        # ~3s. We only keep max_results, so reranking 2x is plenty (top results by
        # engine rank order are the best candidates). Cuts rerank time ~60%.
        _prerank = ranked[:max(2 * max_results, 12)]
        ranked_list, scores, rerank_used, rerank_note = _rank(query, _prerank, mode)
        _efams = {_INDEX_FAMILY.get(r.name, r.name) for r in reports if r.ok}
        total_families = len(_efams) or 1
        ranked_list, scores, qsig = _apply_quality_boost(ranked_list, scores, query)
        if not site:
            ranked_list, scores = _diversify(ranked_list, scores, max_per_domain=2)
        ranked_list, scores = ranked_list[:max_results], scores[:max_results]
        results_list = _build_results(query, ranked_list, scores, total_families, quality_signals=qsig)
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
        next_action=_search_next_action(results_list, engine_blocked, error, cache_query),
    )

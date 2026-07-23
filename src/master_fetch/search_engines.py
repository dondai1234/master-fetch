"""Hound search engine layer (v7.5: vendored ddgs metasearch backbone).

No API key, no account, no third-party service at runtime. The actual multi-
backend scraping + parsing + rotation lives in `search_metasearch.py` (vendored
+ stripped from ddgs, MIT, attributed in NOTICE.ddgs.txt). This module is the
thin hound-side adapter: maps hound's smart_search params (engines, freshness,
site, region, page) onto the metasearch, maps results back to RawResult with
cross-backend consensus, and builds the per-engine reports.

Backends (all keyless): duckduckgo, brave, google, startpage, grokipedia,
wikipedia, yahoo, mojeek, yandex. Bing is disabled (DDG + Yahoo already serve
its index). They run in PARALLEL; a backend that CAPTCHAs / rate-limits / has
no topic-match just yields nothing and the others carry. Search is 100% HTTP
(no browser) - the single Patchright browser stays for smart_fetch only.

HOUND_SEARCH_PROXY (http/https/socks5) is the power-user rotating-proxy escape
hatch for per-IP throttling - the one thing no scraper can escape from one IP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

_metasearch = None


def _get_metasearch():
    """Load the heavy scraper backend only for live searches.

    Importing search_metasearch pulls in primp/httpx/lxml/fake_useragent. Keep
    that cost off cached searches, validation failures, and server startup.
    Tests may monkeypatch _metasearch directly; this helper respects that.
    """
    global _metasearch
    if _metasearch is None:
        from master_fetch.search_metasearch import metasearch as loaded
        _metasearch = loaded
    return _metasearch


# Public default engine pool (the full keyless backend set; order = rough
# preference). `engines=None` in smart_search uses this via the metasearch.
DEFAULT_ENGINES = ("duckduckgo", "brave", "mojeek", "yahoo", "yandex",
                   "startpage", "google", "qwant")

# Index family per backend (by the underlying index/provider, for consensus).
# A URL returned by duckduckgo AND yahoo is ONE family (both Bing's index);
# returned by duckduckgo AND brave is TWO families = a stronger authority signal.
_INDEX_FAMILY = {
    "duckduckgo": "bing", "yahoo": "bing", "bing": "bing",
    "google": "google", "startpage": "google",
    "brave": "brave", "grokipedia": "grokipedia", "wikipedia": "wikipedia",
    "mojeek": "mojeek", "yandex": "yandex", "qwant": "qwant",
    # Specialized JSON-API backends (each is an independent index for consensus).
    "semantic_scholar": "semantic_scholar", "github_api": "github_api",
    "hackernews": "hackernews",
    # BYOK (Bring Your Own Key) search API backends (each is an independent index).
    "serper": "serper", "tavily": "tavily", "exa": "exa",
    "firecrawl": "firecrawl", "tinyfish": "tinyfish",
}

_FRESHNESS_TO_TIMELIMIT = {"day": "d", "week": "w", "month": "m", "year": "y"}


@dataclass
class RawResult:
    title: str
    url: str
    snippet: str
    source: str              # backend that won the dedup (first to return the URL)
    position: int = 0        # 1-indexed within the merged order
    consensus: int = 1       # distinct independent index-families that returned this URL
    sources: tuple = ()      # all backends that returned this URL (set by multi_search)


@dataclass
class EngineReport:
    name: str
    ok: bool = False        # parsed >=1 result
    blocked: bool = False   # rate-limited / CAPTCHA'd / refused / timed out / errored
    preempted: bool = False # cancelled because enough backends delivered (NOT blocked)
    error: str = ""


def normalize_url(url: str) -> str:
    """Light URL normalization for dedup (lowercase host, strip non-root trailing slash)."""
    if not url:
        return ""
    u = url.strip()
    if u.startswith("//"):
        u = "https:" + u
    p = urlparse(u)
    scheme = (p.scheme or "https").lower()
    host = p.netloc.lower()
    path = p.path.rstrip("/") if len(p.path) > 1 else p.path
    return f"{scheme}://{host}{path}"


def _strip_tags(s: str) -> str:
    if not s:
        return ""
    from bs4 import BeautifulSoup
    return BeautifulSoup(s, "lxml").get_text(" ", strip=True)


def _normalize_domain(value: str) -> str:
    """Return a comparable hostname without a cosmetic leading ``www.``."""
    value = value.strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value if "://" in value or value.startswith("//") else f"//{value}")
        host = parsed.hostname or ""
    except ValueError:
        return ""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _is_domain_or_subdomain(host: str, domain: str) -> bool:
    return bool(domain) and (host == domain or host.endswith(f".{domain}"))


def _passes_site_filter(url: str, site: Optional[str], exclude_sites: Optional[list[str]]) -> bool:
    try:
        host = _normalize_domain(urlparse(url).hostname or "")
    except ValueError:
        return False
    if site and not _is_domain_or_subdomain(host, _normalize_domain(site)):
        return False
    for ex in exclude_sites or []:
        if _is_domain_or_subdomain(host, _normalize_domain(ex)):
            return False
    return True


async def fetch_source_for_similar(url: str, *, timeout: int = 10, max_chars: int = 4000
                                    ) -> tuple[str, str]:
    """Fetch a URL for find_similar: returns (title, body_text). Uses a one-off
    impersonated HTTP fetch (primp) - a single arbitrary page, not a repeated
    engine hit, so the metasearch's backend rotation does not apply."""
    try:
        from master_fetch.fetcher import HTTPSession
        from master_fetch.search_metasearch import _PROXY as _p
        from urllib.parse import urlparse as _up
        from bs4 import BeautifulSoup
        # rotation pool kept here (not imported from the removed SERL module)
        _pool = ["chrome", "safari", "firefox", "edge"]
        async with HTTPSession(impersonate=_pool, proxy=_p,
                               stealthy_headers=True, retries=1) as sess:
            resp = await sess.get(url, timeout=timeout)
            text = (getattr(resp, "body", None) or b"").decode(
                getattr(resp, "encoding", None) or "utf-8", errors="replace")
            status = getattr(resp, "status", 0) or 0
    except Exception:
        return "", ""
    if not text:
        return "", ""
    # light blocked-page heuristic
    low = text[:4000].lower()
    if any(m in low for m in ("access denied", "are you a robot", "captcha", "403 forbidden")):
        return "", ""
    title = ""
    try:
        soup = BeautifulSoup(text[:60000], "lxml")
        t = soup.find("title")
        if t:
            title = t.get_text(" ", strip=True)
    except Exception:
        pass
    try:
        import trafilatura
        body = (trafilatura.extract(text[:60000], include_comments=False,
                                    include_tables=False) or "")
    except Exception:
        body = ""
    return title, body[:max_chars]


async def multi_search(
    query: str,
    max_results: int = 10,
    *,
    engines: Optional[list[str]] = None,
    site: Optional[str] = None,
    exclude_sites: Optional[list[str]] = None,
    region: str = "us-en",
    freshness: Optional[str] = None,
    page: int = 0,
    server=None,  # accepted for signature compat; search is 100% HTTP (no browser)
    query_map: dict[str, str] | None = None,
) -> tuple[list[RawResult], list[EngineReport]]:
    """Run the keyless metasearch backends in parallel; return (ranked, reports).

    `engines` selects backends (None = the full default pool). `freshness` maps
    to the engines' time filter. `site` / `exclude_sites` are applied both as a
    query prefix (so backends that honor site: filter upstream) and on the final
    URL (a safety net for backends that do not). `page` is 0-indexed (hound API)
    -> 1-indexed for the backends. `server` is unused (kept for call-site compat;
    search never touches the browser).
    """
    # Build the query with site:/-site: prefixes (best-effort upstream filter).
    def _apply_site(q: str) -> str:
        if site:
            q = f"site:{site} {q}"
        for ex in exclude_sites or []:
            q = f"-site:{ex} {q}"
        return q

    q = _apply_site(query)
    # Apply site: filters to each per-engine query in the query_map (v12).
    if query_map:
        query_map = {eng: _apply_site(qq) for eng, qq in query_map.items()}
    timelimit = _FRESHNESS_TO_TIMELIMIT.get(freshness) if freshness else None
    backend_page = page + 1  # hound 0-indexed -> backends 1-indexed

    # Map hound engine names -> metasearch backends (it handles 'auto'/None/legacy).
    mapped = list(engines) if engines else None

    metasearch = _get_metasearch()
    results_dicts, status = await metasearch(
        q, max_results, region=region, timelimit=timelimit,
        page=backend_page, engines=mapped,
        query_map=query_map,
        site=site, exclude_sites=exclude_sites,
    )

    # Map to RawResult with cross-backend consensus + apply the final site filter.
    ranked: list[RawResult] = []
    for i, d in enumerate(results_dicts, start=1):
        url = d.get("href", "")
        if not _passes_site_filter(url, site, exclude_sites):
            continue
        backends = d.get("backends") or [d.get("backend", "")]
        families = {_INDEX_FAMILY.get(b, b) for b in backends}
        ranked.append(RawResult(
            title=d.get("title", ""),
            url=url,
            snippet=d.get("body", ""),
            source=d.get("backend", backends[0] if backends else ""),
            position=i,
            consensus=len(families),
            sources=tuple(backends),
        ))

    # Per-backend reports from the metasearch status.
    reports: list[EngineReport] = []
    for name, st in status.items():
        if st == "ok":
            reports.append(EngineReport(name=name, ok=True))
        elif st == "preempted":
            reports.append(EngineReport(name=name, preempted=True,
                                        error="preempted (enough backends delivered)"))
        elif st == "blocked":
            reports.append(EngineReport(name=name, blocked=True,
                                        error="blocked/captcha (circuit opened)"))
        elif st == "circuit_open":
            reports.append(EngineReport(name=name, blocked=True,
                                        error="circuit open (recently blocked; skipped)"))
        elif st == "timeout":
            reports.append(EngineReport(name=name, blocked=True, error="timed out"))
        elif st.startswith("error"):
            reports.append(EngineReport(name=name, blocked=True, error=st))
        else:  # "empty"
            reports.append(EngineReport(name=name, error="no results"))

    return ranked, reports


async def prewarm_search_engines(engines: Optional[list[str]] = None) -> None:
    """No-op: the metasearch backends are cheap one-shot HTTP clients (primp/httpx)
    constructed per search; there is no warm session pool to prewarm. Kept for the
    startup path so callers do not break."""
    return None


async def close_search_engines() -> None:
    """No-op: nothing to close (no persistent sessions). Kept for shutdown path."""
    return None

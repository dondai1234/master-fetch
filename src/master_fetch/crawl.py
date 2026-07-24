"""smart_crawl — flagship same-domain deep crawl for Hound, optimized for agents.

Walks a website from a start URL and returns each page as agent-usable markdown
with the same honest signals smart_fetch produces (content_ok / summary / error /
fetched_at). Designed to beat free OSS crawlers (Crawl4AI, Firecrawl free tier,
Jina) on agent-usability: honest per-page quality signals, content-adaptive
extraction, and next_action guidance instead of a dumb page dump.

Flagship design (v6):
  * **Best-first priority queue** (not just BFS). Discovered URLs are scored and
    the highest-value page is crawled next. The scorer blends: focus-query
    relevance (anchor text + URL path tokens), content-likelihood (boost
    docs/guide/api/reference/article/blog, penalize login/submit/register/cart/
    admin/account), and a shallow-depth preference. With no `focus` this still
    beats raw BFS by skipping junk URLs (login/submit) before content pages.
  * **Content-adaptive per-page extraction.** A page is classified from its HTML
    and extracted the right way:
      - article/docs  -> trafilatura main content (markdown).
      - list/index    -> a structured `* [title](url)` link list (HN, aggregators,
                         directory pages where trafilatura's main-content filter
                         returns nothing because the page IS a list of links).
      - js-shell      -> the fetch already auto-escalated HTTP->stealthy render;
                         if even the rendered HTML is empty, content_ok=false
                         with an actionable error (use screenshot / vision).
      - fallback      -> cleaned visible text.
    This fixes the "0 content_ok on Hacker News" and "JS SPA timeout" class of
    bugs at the root: list pages now return their link list as content, and JS
    shells are detected and reported honestly instead of silently empty.
  * **URL normalization + dedup.** Trailing slashes, default ports, lowercase
    host, and tracking query params (utm_*, fbclid, gclid, ref, _) are stripped
    before dedup, so `/docs` and `/docs/` are no longer crawled twice.
  * **Two-phase crawl.** `discover_only=true` returns the URL map (prefetch);
    pass `crawl_urls=[...]` to fetch a chosen subset in a second phase without
    re-discovering (selective deep crawl).
  * **same_domain_only=true** default. External links are dropped (not crawled).
  * **Honest status.** Network failures report status -1 (documented) so
    downstream logic can distinguish them from a real HTTP 0 / no response.
  * **Freshness.** Each page carries `fetched_at`; `cache_ttl=0` forces fresh.
  * **Overall deadline.** One slow page can't hang the crawl; when the deadline
    is hit, partial results are returned with `truncated_by_time=true`.
  * One fetch per page (extraction_type='html'), reusing smart_fetch's anti-bot
    escalation + fetch cache. Links + markdown are derived from the same body.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import re
from time import time
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

from pydantic import BaseModel, Field

logger = logging.getLogger("master-fetch.crawl")

# Match <a ... href="..."> and capture the href + the anchor's visible text.
_LINK_RE = re.compile(
    r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
# Skip these asset extensions when discovering links (they aren't pages).
_ASSET_RE = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|css|js|ico|pdf|zip|mp[34]|woff2?|gz|tar|exe|dmg)(\?|$)",
    re.IGNORECASE,
)
_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "#")

# Tracking / analytics query params that don't change page content -> stripped
# during normalization so two URLs differing only in these don't get crawled twice.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "ref_src", "source", "_ga", "mc_cid", "mc_eid",
}

# Content-likelihood path tokens. Boost content pages, penalize app/admin noise
# so the priority queue crawls docs before login/submit/cart.
_CONTENT_BOOST = ("doc", "docs", "guide", "tutorial", "api", "reference",
                  "article", "blog", "post", "learn", "manual", "help", "spec")
_JUNK_PENALTY = ("login", "signin", "sign-in", "signup", "sign-up", "register",
                 "submit", "cart", "checkout", "account", "admin", "logout",
                 "auth", "password", "settings", "preferences")


class CrawlPage(BaseModel):
    url: str = Field(description="Page URL (final, after redirects). Normalized.")
    depth: int = Field(default=0, description="Hop depth from the start URL (0 = start).")
    status: int = Field(default=0, description="HTTP status. -1 = network error (no response / connection failed). 0 = no response yet.")
    content_ok: bool = Field(default=False, description="True = real content retrieved AND extracted. Trust content only if true. HTTP 200 alone does NOT set this.")
    fetcher_used: str = Field(default="", description="http/stealthy/cache/none")
    title: str = Field(default="", description="Page <title>")
    page_type: str = Field(default="", description="How the page was extracted: article / list / js_shell / fallback / discover_only. Tells the agent what kind of content it got.")
    content: list[str] = Field(default=[], description="Page markdown (empty in discover_only mode). For list pages, a structured link list.")
    content_chars: int = Field(default=0, description="Chars of markdown returned for this page.")
    is_truncated: bool = Field(default=False, description="True = this page has more content; smart_fetch it with offset=next_offset.")
    next_offset: int = Field(default=0, description="Next offset if is_truncated; 0 = no more.")
    fetched_at: str = Field(default="", description="ISO-8601 UTC when this page was fetched (may show cache age).")
    lastmod: str = Field(default="", description="<lastmod> from the site's sitemap.xml for this URL (sitemap mode only). Empty otherwise.")
    summary: str = Field(default="", description="One-line status for this page.")
    error: str = Field(default="", description="Error for this page, if content_ok is False.")


class CrawlResponseModel(BaseModel):
    start_url: str = Field(description="The URL crawl started from (normalized).")
    pages: list[CrawlPage] = Field(description="Crawled pages (one CrawlPage each). In discover_only, content is empty and pages hold the discovered URL map.")
    pages_crawled: int = Field(default=0, description="Pages actually fetched.")
    pages_discovered: int = Field(default=0, description="Total unique same-domain URLs found (including crawled).")
    discover_only: bool = Field(default=False, description="True = map mode (URLs only, no content).")
    truncated_by_budget: bool = Field(default=False, description="True = stopped early because the total-char budget was reached.")
    truncated_by_max_pages: bool = Field(default=False, description="True = stopped early because max_pages was reached.")
    truncated_by_time: bool = Field(default=False, description="True = stopped early because the overall deadline (ms) was reached.")
    sitemap_used: bool = Field(default=False, description="True = the URL map came from the site's sitemap.xml (one fetch), not best-first BFS discovery.")
    sitemaps: list[str] = Field(default=[], description="Sitemap.xml URLs that were fetched + parsed (sitemap mode only).")
    duration_ms: float = Field(default=0, description="Duration ms.")
    error: str = Field(default="", description="Error message (crawl-level).")
    summary: str = Field(default="", description="One-line crawl status.")
    next_action: str = Field(default="", description="Obvious next call when one exists (continue crawl / fetch a page).")


def _same_root(start_url: str) -> str:
    """Normalized root (scheme://netloc) for same-domain filtering."""
    p = urlparse(start_url)
    return f"{p.scheme or 'https'}://{p.netloc}"


def normalize_url(u: str) -> str:
    """Canonicalize a URL for dedup: lowercase host, drop default ports, strip
    tracking query params, drop the fragment, and collapse a trailing slash on
    non-root paths so `/docs` and `/docs/` compare equal. Preserves real query
    params (e.g. pagination `?page=2`)."""
    try:
        p = urlparse(u)
    except Exception:
        return u
    scheme = (p.scheme or "https").lower()
    host = (p.netloc or "").lower()
    # Strip default ports.
    if host.endswith(":80") and scheme == "http":
        host = host[:-3]
    elif host.endswith(":443") and scheme == "https":
        host = host[:-4]
    path = p.path or "/"
    # Collapse trailing slash on non-root paths ("/docs/" -> "/docs").
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    # Drop tracking query params; keep the rest (order preserved).
    if p.query:
        kept = [kv for kv in p.query.split("&")
                if kv and kv.split("=", 1)[0].lower() not in _TRACKING_PARAMS]
        query = "&".join(kept)
    else:
        query = ""
    return urlunparse((scheme, host, path, p.params, query, ""))


def _link_list_markdown(html: str, base_url: str, start_url: str, max_items: int = 200) -> str:
    """Render a list/index page as a structured markdown link list.

    Used when trafilatura's main-content filter returns nothing because the page
    IS a directory of links (Hacker News, aggregators, section index pages).
    Returns the same-domain links as `* [anchor text](url)` so the agent gets
    the page's actual content (the list of items) instead of an empty page.
    """
    pairs = extract_same_domain_links(html, base_url, start_url)
    if not pairs:
        return ""
    lines = []
    for href, text in pairs[:max_items]:
        text = (text or "").strip() or "(no title)"
        if len(text) > 160:
            text = text[:157] + "..."
        lines.append(f"* [{text}]({href})")
    return "\n".join(lines)


def _visible_text(html: str) -> str:
    """Cheap visible-text extraction: strip script/style blocks + tags, collapse
    whitespace. Used as a last-resort fallback and for page-type signals."""
    if not html:
        return ""
    no_ss = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", no_ss)
    return re.sub(r"\s+", " ", text).strip()


def _page_signals(html: str) -> tuple[int, int, int, bool, int]:
    """Cheap signals for page classification: (visible_text_len, script_count,
    link_count, has_framework_root, link_text_len)."""
    if not html:
        return (0, 0, 0, False, 0)
    text_len = len(_visible_text(html))
    script_count = len(re.findall(r"<script\b", html, re.IGNORECASE))
    link_text_len = 0
    link_count = 0
    for m in _LINK_RE.finditer(html):
        link_count += 1
        link_text_len += len(_TAG_RE.sub(" ", m.group(2) or "").strip())
    has_fw = bool(re.search(
        r'id="root"|id="__next"|__NUXT__|__NEXT_DATA__|data-reactroot|<div id="app"',
        html, re.IGNORECASE))
    return (text_len, script_count, link_count, has_fw, link_text_len)


def _classify_and_extract(html: str, url: str, start_url: str, focus: Optional[str],
                          max_chars: int) -> tuple[str, str, bool]:
    """Content-adaptive extraction. Returns (markdown, page_type, content_ok).

    page_type is one of: article / list / js_shell / fallback. content_ok is
    False only when we genuinely got nothing usable (js_shell that didn't
    render, or an empty/error page).
    """
    from master_fetch.trafilatura_extractor import extract_content_from_html
    from master_fetch.focus import focus_content

    md = ""
    try:
        md = extract_content_from_html(html, url, "markdown") or ""
    except Exception:
        md = ""

    text_len, script_count, link_count, has_fw, link_text_len = _page_signals(html)

    # 1) List / index page: dominated by links (most visible text is anchor
    #    text). Takes priority over 'article' because trafilatura returns the
    #    link texts as 'content' for HN/aggregator pages, but the page IS a list.
    #    Render the same-domain links as a structured `* [title](url)` list so
    #    the agent gets the page's actual content (the items).
    link_density = (link_text_len / text_len) if text_len else (1.0 if link_count else 0.0)
    if link_count >= 10 and link_density >= 0.5:
        list_md = _link_list_markdown(html, url, start_url)
        if list_md:
            md = list_md
            kind = "list"
        else:
            kind = "fallback"
    # 2) Article / docs: trafilatura found real main content (prose, not links).
    elif md and len(md) >= 200:
        if focus:
            try:
                md = focus_content(md, focus)
            except Exception:
                pass
        kind = "article"
    # 3) List page with fewer/looser links but trafilatura still empty.
    elif link_count >= 10 and (len(md) < 200):
        list_md = _link_list_markdown(html, url, start_url)
        if list_md:
            md = list_md
            kind = "list"
        else:
            kind = "fallback"
    # 4) JS shell: little visible text + heavy scripts + framework root, and
    #    trafilatura got nothing. The fetch already tried stealthy render; if
    #    we still see a shell, the page didn't render -> honest failure.
    elif text_len < 400 and (script_count >= 5 or has_fw) and not md:
        kind = "js_shell"
        md = ""
    # 5) Fallback: cleaned visible text (better than nothing).
    else:
        md = md or _visible_text(html)
        kind = "fallback"

    content_ok = bool(md and len(md) >= 50 and kind != "js_shell")

    # Truncate to the per-page budget.
    if md and len(md) > max_chars:
        md = md[:max_chars]
    return md, kind, content_ok


def extract_same_domain_links(
    html: str,
    base_url: str,
    start_url: str,
    path_include: Optional[list[str]] = None,
    path_exclude: Optional[list[str]] = None,
) -> list[tuple[str, str]]:
    """Extract absolute same-domain (anchor URL, anchor text) pairs from HTML.

    Resolves relative URLs against base_url, keeps only links on the start URL's
    netloc, drops fragments/assets/non-http schemes, applies path include/exclude
    prefixes, and dedupes by the NORMALIZED URL (so `/docs` and `/docs/` collapse
    to one). Order-preserving.
    """
    root_netloc = urlparse(start_url).netloc
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    if not html:
        return out
    for m in _LINK_RE.finditer(html):
        href = (m.group(1) or "").strip()
        if not href or href.lower().startswith(_SKIP_SCHEMES):
            continue
        try:
            absu = urljoin(base_url, href)
        except Exception:
            continue
        parsed = urlparse(absu)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc != root_netloc:
            continue  # external -> dropped (same_domain_only default)
        path = parsed.path or "/"
        if path_include and not any(path.startswith(p) for p in path_include):
            continue
        if path_exclude and any(path.startswith(p) for p in path_exclude):
            continue
        if _ASSET_RE.search(path):
            continue
        clean = normalize_url(absu)
        if clean in seen:
            continue
        seen.add(clean)
        text = _TAG_RE.sub(" ", m.group(2) or "").strip()
        out.append((absu, text))  # original absolute URL (for fetching); dedup by `clean`
    return out


def _query_terms(query: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z0-9]+", query or "") if len(w) >= 2}


def score_link(url: str, text: str, focus: str) -> float:
    """Priority score for a discovered URL. Higher = crawl first.

    Blends:
      * focus relevance (anchor-text overlap * 2 + URL-path token overlap),
      * content-likelihood (path tokens: docs/guide/api boosted,
        login/submit/cart penalized),
      * a shallow-depth preference is applied by the caller via a depth term.
    """
    terms = _query_terms(focus)
    path_l = (urlparse(url).path or "").lower().replace("/", " ").replace("-", " ").replace("_", " ")
    text_l = (text or "").lower()
    focus_score = 0.0
    if terms:
        text_hit = sum(1 for t in terms if t in text_l) / len(terms)
        path_hit = sum(1 for t in terms if t in path_l) / len(terms)
        focus_score = text_hit * 2.0 + path_hit
    path_tokens = path_l.split()
    content_score = 0.0
    for tok in _CONTENT_BOOST:
        if tok in path_tokens:
            content_score += 0.5
    for tok in _JUNK_PENALTY:
        if tok in path_tokens:
            content_score -= 1.0
    return focus_score + content_score


def _error_status(resp) -> int:
    """Honest status: -1 for network errors (no HTTP response), else the real
    HTTP status. Lets downstream logic distinguish 'server returned 500' from
    'connection died'."""
    if resp is None:
        return -1
    status = getattr(resp, "status", 0) or 0
    if status == 0 and getattr(resp, "error", ""):
        return -1
    return status


def _sitemap_passes_filters(path: str, path_include: Optional[list[str]],
                            path_exclude: Optional[list[str]]) -> bool:
    if path_include and not any(path.startswith(p) for p in path_include):
        return False
    if path_exclude and any(path.startswith(p) for p in path_exclude):
        return False
    return True


async def _sitemap_map(url: str, path_include: Optional[list[str]],
                       path_exclude: Optional[list[str]], *,
                       max_pages: int, deadline_t: float) -> Optional[CrawlResponseModel]:
    """Try to map the site via sitemap.xml. Returns a CrawlResponseModel on
    success (sitemap found + parsed), or None if no sitemap was reachable so the
    caller can fall back to BFS ('auto'). Same-domain + path filters applied.
    Caps the returned URL map at max(1000, max_pages*10)."""
    from master_fetch.sitemap import discover_sitemap, SitemapURL

    def _make_http_get():
        from master_fetch.search_proxy import get_next_proxy
        _sitemap_proxy = get_next_proxy()
        try:
            import primp  # type: ignore
            client = primp.Client(proxy=_sitemap_proxy, timeout=15, impersonate="random",
                                  impersonate_os="random", verify=True)
        except Exception:
            client = None
        import urllib.request as _urllib_req

        def _get(u: str):
            if client is not None:
                try:
                    r = client.get(u)
                    if r.status_code == 200 and r.content:
                        return (int(r.status_code), bytes(r.content))
                    return None
                except Exception:
                    pass  # fall back to urllib
            # stdlib fallback (some hosts reject primp fingerprints, accept urllib)
            try:
                req = _urllib_req.Request(u, headers={"User-Agent": "Hound-Sitemap/8.0"})
                with _urllib_req.urlopen(req, timeout=15) as resp:  # noqa: S310
                    body = resp.read()
                    if body:
                        return (int(resp.status), body)
            except Exception:
                return None
            return None
        return _get

    t0 = time()
    root_netloc = urlparse(url).netloc
    cap = min(5000, max(1000, max_pages * 10))
    try:
        result = await asyncio.to_thread(
            discover_sitemap, url, http_get=_make_http_get(), max_urls=cap,
        )
    except Exception:
        return None
    if not result.urls or not result.sitemaps_used:
        return None

    pages: list[CrawlPage] = []
    seen: set[str] = set()
    for su in result.urls:
        if time() > deadline_t:
            break
        try:
            parsed = urlparse(su.url)
        except Exception:
            continue
        if parsed.scheme not in ("http", "https") or parsed.netloc != root_netloc:
            continue  # sitemaps can list other hosts; keep same-domain only
        path = parsed.path or "/"
        if not _sitemap_passes_filters(path, path_include, path_exclude):
            continue
        norm = normalize_url(su.url)
        if norm in seen:
            continue
        seen.add(norm)
        pages.append(CrawlPage(
            url=norm, depth=0, status=0, content_ok=True, fetcher_used="sitemap",
            page_type="sitemap", content=[], content_chars=0, lastmod=su.lastmod,
            summary="sitemap entry",
        ))
        if len(pages) >= cap:
            break

    if not pages:
        return None

    ok = len(pages)
    summary = (f"mapped {ok} URL(s) at {url} from sitemap.xml "
               f"(via {result.via}; {len(result.sitemaps_used)} sitemap file(s)) - one fetch, no BFS")
    next_action = (
        f"{ok} URLs mapped from the sitemap. smart_fetch the ones you need, or re-run "
        f"smart_crawl with crawl_urls=[...] (or discover_only=false) to fetch content "
        f"for a chosen subset. Use path_include/path_exclude to scope."
    )
    return CrawlResponseModel(
        start_url=normalize_url(url), pages=pages, pages_crawled=0,
        pages_discovered=ok, discover_only=True, sitemap_used=True,
        sitemaps=list(result.sitemaps_used),
        duration_ms=(time() - t0) * 1000, summary=summary, next_action=next_action,
    )


async def smart_crawl(
    server,
    url: str,
    max_pages: int = 10,
    max_depth: int = 2,
    path_include: Optional[list[str]] = None,
    path_exclude: Optional[list[str]] = None,
    discover_only: bool = False,
    focus: Optional[str] = None,
    crawl_urls: Optional[list[str]] = None,
    max_content_chars_per: int = 8000,
    max_total_chars: Optional[int] = None,
    concurrency: int = 3,
    cache_ttl: int = 3600,
    respect_robots: bool = False,
    force_fetcher: Optional[str] = None,
    timeout: int = 30000,
    deadline_ms: int = 120000,
    sitemap: str | bool = False,
) -> CrawlResponseModel:
    """Best-first same-domain crawl. See module docstring.

    sitemap: True = map the site from its sitemap.xml only (one fetch; returns
    the full URL list + lastmod, no BFS, no content). 'auto' = use the sitemap if
    the site has one, else fall back to BFS. False (default) = BFS only. The
    sitemap path collapses big-site discovery (hundreds of pages) into one call.
    """
    from master_fetch.security import validate_url, SecurityError
    from master_fetch.trafilatura_extractor import extract_html_title
    from master_fetch.robots import is_allowed

    t0 = time()
    deadline_t = t0 + (deadline_ms / 1000.0)

    def _err(msg: str, start: str = "") -> CrawlResponseModel:
        return CrawlResponseModel(start_url=start or url, pages=[], error=msg[:200],
                                  duration_ms=(time() - t0) * 1000,
                                  summary=f"invalid start URL: {msg[:120]}")

    try:
        url = validate_url(url)
    except (SecurityError, ValueError) as e:
        return _err(str(e))
    start_norm = normalize_url(url)  # canonical form for dedup + the response field

    max_pages = max(1, min(int(max_pages), 100))
    max_depth = max(0, min(int(max_depth), 5))
    concurrency = max(1, min(int(concurrency), 5))
    max_content_chars_per = max(500, min(int(max_content_chars_per), 50000))
    if max_total_chars is None:
        max_total_chars = max_pages * max_content_chars_per
    max_total_chars = max(max_content_chars_per, min(int(max_total_chars), 500000))
    focus = focus.strip() if isinstance(focus, str) and focus.strip() else None
    selective = bool(crawl_urls)

    # Normalize the sitemap flag: True/'auto'/False. 'auto' = use sitemap if
    # present, else BFS. True = sitemap only (return empty if none). Strings are
    # case-insensitive.
    sm_mode = "off"
    if isinstance(sitemap, str):
        s = sitemap.strip().lower()
        sm_mode = "auto" if s == "auto" else ("on" if s in ("true", "1", "on", "yes") else "off")
    elif sitemap is True:
        sm_mode = "on"

    # ── Sitemap mode: map the site from sitemap.xml in one fetch ───────────
    # Runs before BFS. 'auto' uses the sitemap if found, else falls through to
    # BFS. 'on' returns the sitemap map (or an honest empty if none found).
    if sm_mode in ("on", "auto") and not selective:
        sm_result = await _sitemap_map(url, path_include, path_exclude,
                                       max_pages=max_pages, deadline_t=deadline_t)
        if sm_result is not None:
            return sm_result  # sitemap found + mapped (auto/on success)
        # auto: no sitemap -> fall through to BFS. on: no sitemap -> honest empty.
        if sm_mode == "on":
            return CrawlResponseModel(
                start_url=start_norm, pages=[], pages_crawled=0,
                pages_discovered=0, discover_only=True, sitemap_used=False,
                duration_ms=(time() - t0) * 1000,
                summary=f"no sitemap.xml found at {root} (robots.txt had no Sitemap directive and /sitemap.xml returned nothing)",
                next_action=("No sitemap found. Re-run smart_crawl with sitemap=false (or omit it) "
                             "to use best-first BFS discovery instead."),
            )

    # Two-phase selective crawl: a caller-supplied URL subset is fetched with no
    # further discovery (max_depth=0). URLs are normalized + same-domain-checked.
    selective = bool(crawl_urls)
    if selective:
        root_netloc = urlparse(url).netloc
        culled: list[str] = []
        seen0: set[str] = set()
        for raw in crawl_urls:
            try:
                joined = urljoin(url, raw)
            except Exception:
                continue
            jnorm = normalize_url(joined)
            if urlparse(joined).netloc != root_netloc:
                continue
            if jnorm not in seen0:
                seen0.add(jnorm)
                culled.append(joined)  # original for fetching; dedup by jnorm
        crawl_urls = culled[:max_pages]

    root = _same_root(url)
    visited: set[str] = set()
    discovered: set[str] = {start_norm}
    pages: list[CrawlPage] = []
    total_chars = 0
    truncated_budget = False
    truncated_maxpages = False
    truncated_time = False

    sem = asyncio.Semaphore(concurrency)

    async def fetch_one(u: str) -> tuple[str, "object", str]:
        """Fetch one page as HTML. Returns (url, ResponseModel, html_str)."""
        async with sem:
            # Rotate through proxy pool so crawl pages don't hammer one IP.
            from master_fetch.search_proxy import get_next_proxy, get_proxy_pool
            _crawl_proxy = get_next_proxy()
            try:
                resp = await server.smart_fetch(
                    url=u, extraction_type="html", cache_ttl=cache_ttl,
                    max_content_chars=200000, force_fetcher=force_fetcher,
                    respect_robots=respect_robots, timeout=timeout,
                    proxy=_crawl_proxy,
                )
                # Health tracking: success marks proxy healthy.
                if _crawl_proxy:
                    pool = get_proxy_pool()
                    if pool is not None and resp.status > 0:
                        pool.mark_success(_crawl_proxy)
            except Exception as e:
                from master_fetch.server import ResponseModel
                resp = ResponseModel(url=u, status=-1, content=[""],
                                     fetcher_used="none", error=str(e)[:200])
                # Health tracking: connection error cools the proxy.
                if _crawl_proxy:
                    pool = get_proxy_pool()
                    if pool is not None:
                        pool.mark_failed(_crawl_proxy)
        html = resp.content[0] if resp.content else ""
        return u, resp, html

    # ---- Priority queue (best-first) -------------------------------------
    # Heap entries: (score, depth, seq, url, anchor_text). `seq` breaks ties so
    # heapq never tries to order dicts/strings on a tie. Lower score = popped
    # first, so we negate the real score.
    heap: list[tuple[float, int, int, str, str]] = []
    seq = 0

    def push(u: str, depth: int, text: str):
        nonlocal seq
        s = score_link(u, text, focus or "")
        # Shallow-depth preference: prefer shallower pages (small penalty/depth).
        s -= 0.15 * depth
        heapq.heappush(heap, (-s, depth, seq, u, text))
        seq += 1

    if selective:
        for u in crawl_urls:
            push(u, 0, "")
        max_depth = 0  # don't expand from a selective crawl
    else:
        push(url, 0, "")

    # Fetch a batch of up to `concurrency` best URLs from the heap concurrently.
    while heap and len(pages) < max_pages and not truncated_budget:
        if time() > deadline_t:
            truncated_time = True
            break
        remaining = max_pages - len(pages)
        batch: list[tuple[float, int, int, str, str]] = []
        while heap and len(batch) < min(concurrency, remaining):
            batch.append(heapq.heappop(heap))
        results = await asyncio.gather(*[fetch_one(e[3]) for e in batch])

        for (neg_score, depth, _seq, u, _text), (ru, resp, html) in zip(batch, results):
            if len(pages) >= max_pages:
                truncated_maxpages = True
                break
            if time() > deadline_t:
                truncated_time = True
                break
            u_norm = normalize_url(resp.url or u)
            # Guard: if a redirect took us off-domain, skip it (don't crawl external).
            if urlparse(u_norm).netloc != urlparse(url).netloc:
                continue
            if u_norm in visited:
                continue
            visited.add(u_norm)

            title = ""
            try:
                title = extract_html_title(html) if html else ""
            except Exception:
                pass

            content_md = ""
            page_type = "discover_only" if discover_only else ""
            is_trunc = False
            next_off = 0
            content_ok = bool(resp.content_ok)
            if not discover_only and html:
                if resp.content_ok:
                    try:
                        md, kind, ok = _classify_and_extract(
                            html, resp.url or u, url, focus, max_content_chars_per)
                    except Exception:
                        md, kind, ok = "", "fallback", False
                    page_type = kind
                    content_ok = ok and bool(md)
                    if md:
                        if len(md) > max_content_chars_per:
                            is_trunc = True
                            next_off = max_content_chars_per
                            md = md[:max_content_chars_per]
                        content_md = md
                else:
                    # Fetch reported not content_ok (JS shell / bot wall / error).
                    page_type = "js_shell" if resp.error else "fallback"
            elif discover_only:
                content_ok = True  # map mode: the URL itself is the result

            page = CrawlPage(
                url=u_norm, depth=depth, status=_error_status(resp),
                content_ok=content_ok, fetcher_used=resp.fetcher_used,
                title=title, page_type=page_type,
                content=[content_md] if content_md else [],
                content_chars=len(content_md), is_truncated=is_trunc,
                next_offset=next_off, fetched_at=getattr(resp, "fetched_at", ""),
                summary=resp.summary, error=resp.error,
            )
            pages.append(page)
            total_chars += page.content_chars
            if total_chars >= max_total_chars and not discover_only:
                truncated_budget = True

            # Discover links for deeper layers (skip in selective mode).
            if not selective and depth < max_depth and html:
                try:
                    links = extract_same_domain_links(
                        html, resp.url or u, url, path_include, path_exclude)
                except Exception:
                    links = []
                for link_url, link_text in links:
                    link_norm = normalize_url(link_url)
                    if link_norm not in discovered and link_norm not in visited:
                        discovered.add(link_norm)
                        push(link_url, depth + 1, link_text)  # fetch original
            if truncated_budget:
                break

    pages_crawled = len(pages)
    # In selective mode, "discovered" is just the chosen subset.
    if selective:
        pages_discovered = len(crawl_urls)
    else:
        # Count any URLs still in the heap as discovered (they were found but not crawled).
        for _neg, _d, _s, hu, _t in heap:
            discovered.add(normalize_url(hu))
        pages_discovered = len(discovered)

    if pages_crawled >= max_pages and pages_discovered > pages_crawled:
        truncated_maxpages = True

    # Build a concise summary + next_action.
    ok = sum(1 for p in pages if p.content_ok)
    by_type: dict[str, int] = {}
    for p in pages:
        if p.page_type:
            by_type[p.page_type] = by_type.get(p.page_type, 0) + 1
    type_bits = ", ".join(f"{k}:{v}" for k, v in sorted(by_type.items()) if k != "discover_only") or ""
    bits = [f"crawled {pages_crawled} page(s) at {root} (depth <= {max_depth})",
            f"{ok} content_ok"]
    if type_bits:
        bits.append(type_bits)
    if discover_only:
        bits[0] = f"mapped {pages_discovered} URL(s) at {root} (depth <= {max_depth})"
    if truncated_budget:
        bits.append("stopped: token budget reached")
    if truncated_maxpages:
        bits.append("stopped: max_pages reached")
    if truncated_time:
        bits.append("stopped: time deadline reached")
    summary = "; ".join(bits)

    next_action = ""
    if truncated_budget or truncated_maxpages or truncated_time:
        why = ("time deadline" if truncated_time else
               "token budget" if truncated_budget else "max_pages")
        next_action = (
            f"crawl stopped early ({why}); re-run smart_crawl with a higher "
            f"max_pages / max_total_chars / deadline_ms, or scope with "
            f"path_include. {pages_discovered - pages_crawled} URL(s) were "
            f"discovered but not fetched."
        )
    elif discover_only and pages_discovered > pages_crawled:
        next_action = (
            f"{pages_discovered} URLs mapped. smart_fetch the ones you need, or "
            f"re-run smart_crawl with discover_only=false (or crawl_urls=[...]) "
            f"to fetch content for a chosen subset."
        )

    return CrawlResponseModel(
        start_url=start_norm, pages=pages, pages_crawled=pages_crawled,
        pages_discovered=pages_discovered, discover_only=discover_only,
        truncated_by_budget=truncated_budget, truncated_by_max_pages=truncated_maxpages,
        truncated_by_time=truncated_time, duration_ms=(time() - t0) * 1000,
        summary=summary, next_action=next_action,
    )

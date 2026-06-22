"""smart_crawl — recursive same-domain deep crawl for Hound.

Walks a website from a start URL, following same-domain links breadth-first up
to a depth/page/token budget, and returns each page as clean markdown with the
same agent-facing signals smart_fetch produces (content_ok / summary / error).
A `discover_only` mode returns the URL map without page content (competes with
Firecrawl /map + Crawl4AI's URL seeder). A `focus` query turns it into a
query-prioritized crawl: discovered links are scored against the focus and the
most relevant pages are crawled first within the budget.

Design:
  * One fetch per page (extraction_type='html'), reusing smart_fetch's anti-bot
    escalation (HTTP -> stealthy) and the fetch cache. Links are extracted from
    the raw HTML; markdown content is derived from the same HTML via trafilatura
    (no second fetch).
  * Same-domain only (by the start URL's netloc) so a crawl can't wander off the
    site. Path include/exclude prefixes scope it further (e.g. ['/docs']).
  * Hard caps: max_pages, max_depth, and a total-char token budget
    (max_total_chars, default = max_pages * max_content_chars_per). When the
    budget or page cap stops the crawl early, next_action tells the agent how to
    continue (raise the caps / crawl a specific path).
  * Concurrency via an asyncio.Semaphore (default 3, capped at 5) so a layer of
    links fetches in parallel without hammering the server.
  * Per-page agent hints (content_ok/summary) so the agent can trust/filter the
    crawled pages the same way it filters smart_fetch results.
"""

from __future__ import annotations

import asyncio
import logging
import re
from time import time
from typing import Optional
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel, Field

logger = logging.getLogger("master-fetch.crawl")

# Match <a ... href="..."> and capture the href + the anchor's visible text.
_LINK_RE = re.compile(
    r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
# Skip these asset extensions when discovering links (they aren't pages).
_ASSET_RE = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|css|js|ico|pdf|zip|mp[34]|woff2?|gz|tar|exe|dmg)(\?|$)",
    re.IGNORECASE,
)
_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "#")


class CrawlPage(BaseModel):
    url: str = Field(description="Page URL (final, after redirects)")
    depth: int = Field(default=0, description="Hop depth from the start URL (0 = start)")
    status: int = Field(default=0, description="HTTP status (0 = network error)")
    content_ok: bool = Field(default=False, description="True = real content retrieved. Trust content only if true.")
    fetcher_used: str = Field(default="", description="http/stealthy/cache/none")
    title: str = Field(default="", description="Page <title>")
    content: list[str] = Field(default=[], description="Page markdown (empty in discover_only mode)")
    content_chars: int = Field(default=0, description="Chars of markdown returned for this page")
    is_truncated: bool = Field(default=False, description="True = this page has more content; smart_fetch it with offset=next_offset")
    next_offset: int = Field(default=0, description="Next offset if is_truncated; 0 = no more")
    summary: str = Field(default="", description="One-line status for this page")
    error: str = Field(default="", description="Error for this page, if content_ok is False")


class CrawlResponseModel(BaseModel):
    start_url: str = Field(description="The URL crawl started from")
    pages: list[CrawlPage] = Field(description="Crawled pages (one CrawlPage each). In discover_only, content is empty and pages hold the discovered URL map.")
    pages_crawled: int = Field(default=0, description="Pages actually fetched")
    pages_discovered: int = Field(default=0, description="Total unique same-domain URLs found (including crawled)")
    discover_only: bool = Field(default=False, description="True = map mode (URLs only, no content)")
    truncated_by_budget: bool = Field(default=False, description="True = stopped early because the total-char budget was reached")
    truncated_by_max_pages: bool = Field(default=False, description="True = stopped early because max_pages was reached")
    duration_ms: float = Field(default=0, description="Duration ms")
    error: str = Field(default="", description="Error message (crawl-level)")
    summary: str = Field(default="", description="One-line crawl status")
    next_action: str = Field(default="", description="Obvious next call when one exists (continue crawl / fetch a page)")


def _same_root(start_url: str) -> str:
    """Normalized root (scheme://netloc) for same-domain filtering."""
    p = urlparse(start_url)
    return f"{p.scheme or 'https'}://{p.netloc}"


def extract_same_domain_links(
    html: str,
    base_url: str,
    start_url: str,
    path_include: Optional[list[str]] = None,
    path_exclude: Optional[list[str]] = None,
) -> list[tuple[str, str]]:
    """Extract absolute same-domain (anchor URL, anchor text) pairs from HTML.

    Resolves relative URLs against base_url, keeps only links on the start URL's
    netloc, drops fragments/assets/non-http schemes, and applies path
    include/exclude prefixes. Deduped, order-preserving.
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
            continue
        path = parsed.path or "/"
        if path_include and not any(path.startswith(p) for p in path_include):
            continue
        if path_exclude and any(path.startswith(p) for p in path_exclude):
            continue
        if _ASSET_RE.search(path):
            continue
        clean = parsed._replace(fragment="").geturl()
        if clean in seen:
            continue
        seen.add(clean)
        text = _TAG_RE.sub(" ", m.group(2) or "").strip()
        out.append((clean, text))
    return out


def _query_terms(query: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z0-9]+", query or "") if len(w) >= 2}


def score_link(url: str, text: str, focus: str) -> float:
    """Cheap relevance score of a discovered link to the focus query:
    query-term overlap with the anchor text (weighted) + the URL path tokens."""
    terms = _query_terms(focus)
    if not terms:
        return 0.0
    text_l = (text or "").lower()
    path_l = (urlparse(url).path or "").lower().replace("/", " ").replace("-", " ")
    text_hit = sum(1 for t in terms if t in text_l) / len(terms)
    path_hit = sum(1 for t in terms if t in path_l) / len(terms)
    return text_hit * 2.0 + path_hit


async def smart_crawl(
    server,
    url: str,
    max_pages: int = 10,
    max_depth: int = 2,
    path_include: Optional[list[str]] = None,
    path_exclude: Optional[list[str]] = None,
    discover_only: bool = False,
    focus: Optional[str] = None,
    max_content_chars_per: int = 4000,
    max_total_chars: Optional[int] = None,
    concurrency: int = 3,
    cache_ttl: int = 3600,
    respect_robots: bool = False,
    force_fetcher: Optional[str] = None,
    timeout: int = 30000,
) -> CrawlResponseModel:
    """BFS same-domain crawl. See module docstring."""
    from master_fetch.security import validate_url, SecurityError
    from master_fetch.trafilatura_extractor import extract_content_from_html, extract_html_title
    from master_fetch.focus import focus_content
    from master_fetch.robots import is_allowed

    t0 = time()
    try:
        url = validate_url(url)
    except (SecurityError, ValueError) as e:
        return CrawlResponseModel(start_url=url, pages=[], error=str(e)[:200],
                                  duration_ms=(time() - t0) * 1000,
                                  summary=f"invalid start URL: {str(e)[:120]}")

    max_pages = max(1, min(int(max_pages), 100))
    max_depth = max(0, min(int(max_depth), 5))
    concurrency = max(1, min(int(concurrency), 5))
    max_content_chars_per = max(500, min(int(max_content_chars_per), 50000))
    if max_total_chars is None:
        max_total_chars = max_pages * max_content_chars_per
    max_total_chars = max(max_content_chars_per, min(int(max_total_chars), 500000))
    focus = focus.strip() if isinstance(focus, str) and focus.strip() else None

    root = _same_root(url)
    visited: set[str] = set()
    discovered: set[str] = {url}
    pages: list[CrawlPage] = []
    total_chars = 0
    truncated_budget = False
    truncated_maxpages = False

    sem = asyncio.Semaphore(concurrency)

    async def fetch_one(u: str) -> tuple[str, "object", str]:
        """Fetch one page as HTML. Returns (url, ResponseModel, html_str)."""
        async with sem:
            try:
                resp = await server.smart_fetch(
                    url=u, extraction_type="html", cache_ttl=cache_ttl,
                    max_content_chars=200000, force_fetcher=force_fetcher,
                    respect_robots=respect_robots, timeout=timeout,
                )
            except Exception as e:
                from master_fetch.server import ResponseModel
                resp = ResponseModel(url=u, status=0, content=[""],
                                     fetcher_used="none", error=str(e)[:200])
        html = resp.content[0] if resp.content else ""
        return u, resp, html

    current_layer: list[str] = [url]
    depth = 0
    while current_layer and depth <= max_depth and len(pages) < max_pages and not truncated_budget:
        # Cap this layer to the remaining page budget.
        remaining = max_pages - len(pages)
        current_layer = current_layer[:remaining]
        results = await asyncio.gather(*[fetch_one(u) for u in current_layer])

        next_layer_pairs: list[tuple[str, str]] = []
        for u, resp, html in results:
            if len(pages) >= max_pages:
                truncated_maxpages = True
                break
            title = ""
            try:
                title = extract_html_title(html) if html else ""
            except Exception:
                pass
            content_md = ""
            is_trunc = False
            next_off = 0
            if not discover_only and html and resp.content_ok:
                try:
                    md = extract_content_from_html(html, resp.url, "markdown") or ""
                except Exception:
                    md = ""
                if focus and md:
                    try:
                        md = focus_content(md, focus)
                    except Exception:
                        pass
                if md:
                    if len(md) > max_content_chars_per:
                        is_trunc = True
                        next_off = max_content_chars_per
                        md = md[:max_content_chars_per]
                    content_md = md
            page = CrawlPage(
                url=resp.url or u, depth=depth, status=resp.status,
                content_ok=resp.content_ok and bool(content_md or discover_only),
                fetcher_used=resp.fetcher_used, title=title,
                content=[content_md] if content_md else [],
                content_chars=len(content_md), is_truncated=is_trunc,
                next_offset=next_off, summary=resp.summary, error=resp.error,
            )
            pages.append(page)
            total_chars += page.content_chars
            if total_chars >= max_total_chars and not discover_only:
                truncated_budget = True
            # Discover links for the next layer.
            if depth < max_depth and html:
                try:
                    links = extract_same_domain_links(
                        html, resp.url or u, url, path_include, path_exclude,
                    )
                except Exception:
                    links = []
                for link_url, link_text in links:
                    if link_url not in discovered:
                        discovered.add(link_url)
                        next_layer_pairs.append((link_url, link_text))
            if truncated_budget:
                break

        # Focus-prioritize the next layer (most relevant links first).
        if focus and next_layer_pairs:
            next_layer_pairs.sort(key=lambda lt: score_link(lt[0], lt[1], focus), reverse=True)
        current_layer = [lu for lu, _ in next_layer_pairs]
        depth += 1

    pages_crawled = len(pages)
    pages_discovered = len(discovered)

    # The crawl stopped early by the page cap if there are still discovered
    # URLs we didn't get to (covers the case where max_depth is also binding,
    # so the next-layer queue is empty but URLs remain at the current depth).
    if pages_crawled >= max_pages and pages_discovered > pages_crawled:
        truncated_maxpages = True
    elif pages_crawled < max_pages:
        truncated_maxpages = False

    # Build a concise summary + next_action.
    ok = sum(1 for p in pages if p.content_ok)
    bits = [f"crawled {pages_crawled} page(s) at {root} (depth <= {max_depth})",
            f"{ok} content_ok"]
    if discover_only:
        bits[0] = f"mapped {pages_discovered} URL(s) at {root} (depth <= {max_depth})"
    if truncated_budget:
        bits.append("stopped: token budget reached")
    if truncated_maxpages:
        bits.append("stopped: max_pages reached")
    summary = "; ".join(bits)

    next_action = ""
    if truncated_budget or truncated_maxpages:
        next_action = (
            f"crawl stopped early; re-run smart_crawl with a higher max_pages / "
            f"max_total_chars, or scope with path_include. {pages_discovered - pages_crawled} "
            f"URL(s) were discovered but not fetched."
        )
    elif discover_only and pages_discovered > pages_crawled:
        next_action = (
            f"{pages_discovered} URLs mapped. smart_fetch the ones you need, or "
            f"re-run smart_crawl with discover_only=false to fetch content."
        )

    return CrawlResponseModel(
        start_url=url, pages=pages, pages_crawled=pages_crawled,
        pages_discovered=pages_discovered, discover_only=discover_only,
        truncated_by_budget=truncated_budget, truncated_by_max_pages=truncated_maxpages,
        duration_ms=(time() - t0) * 1000, summary=summary, next_action=next_action,
    )

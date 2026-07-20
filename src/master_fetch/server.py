"""Hound MCP Server.

Forks Scrapling's built-in MCP server and adds:
- Trafilatura article extraction (cleaner than markdownify)
- Smart fetch routing (auto-escalate HTTP -> Stealthy). 2 tiers: HTTP first, then Patchright stealth browser.
- SQLite content cache with TTL
- smart_fetch umbrella tool (single entry point that routes automatically)
- extract_article and extract_structured modes
- Input validation with SSRF protection

Note: the dynamic (Playwright) browser tier was removed in v3.5.0. open_session
still accepts session_type="dynamic" for backward compatibility with manual
session creation, but smart_fetch auto-routing only uses http -> stealthy.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from uuid import uuid4
import asyncio
import contextvars
from asyncio import gather, Lock, sleep as asyncio_sleep, to_thread as asyncio_to_thread
from datetime import datetime, timezone
from time import time as now
from dataclasses import dataclass, field
from typing import Annotated, Mapping, Sequence, Optional, Literal, Union, Dict, List, Any, TYPE_CHECKING
import warnings as _warnings
import sys as _sys
import traceback as _traceback

# Production cleanliness: on Windows the ProactorEventLoop's stdio/subprocess
# pipe transports can emit 'unclosed transport' ResourceWarnings from __del__
# during interpreter teardown (after the loop is closed). These print a
# traceback to stderr that an MCP client can mistake for a crash. The real
# fix is closing the transports while the loop is alive (see
# _shutdown_close_sessions); these two guards suppress any residual noise so
# stderr stays clean. (Real __del__ exceptions still surface.)
_warnings.filterwarnings("ignore", message="unclosed .*transport", category=ResourceWarning)

# CPython prints 'Exception ignored in __del__' via sys.unraisablehook. The
# asyncio transport teardown on a closed ProactorEventLoop raises RuntimeError
# ('Event loop is closed') and ValueError ('I/O operation on closed pipe') from
# __del__ during GC — after the loop is gone, so they can't be caught. Override
# the hook to swallow ONLY that benign asyncio-transport teardown noise; every
# other unraisable exception still goes to the original hook (real bugs stay
# visible). This is what keeps `python -m master_fetch` stderr clean on exit.
_ORIG_UNRAISABLEHOOK = getattr(_sys, "unraisablehook", None)

def _quiet_asyncio_del_hook(args):
    etype = getattr(args, "exc_type", None)
    try:
        tb = getattr(args, "exc_traceback", None)
        # extract_tb yields FrameSummary objects (.filename, not .f_code).
        filenames = " ".join(
            (getattr(fr, "filename", "") or "") for fr in _traceback.extract_tb(tb)
        ) if tb is not None else ""
    except Exception:
        filenames = ""
    is_asyncio_teardown = (
        "asyncio" in filenames
        and etype in (RuntimeError, ValueError, ResourceWarning)
    )
    if is_asyncio_teardown:
        return  # benign transport teardown on a closed loop — swallow
    if _ORIG_UNRAISABLEHOOK is not None:
        try:
            _ORIG_UNRAISABLEHOOK(args)
        except Exception:
            pass

try:
    _sys.unraisablehook = _quiet_asyncio_del_hook
except Exception:
    pass

logger = logging.getLogger("master-fetch.server")



from master_fetch import __version__
from pydantic import BaseModel, Field

# Lazy imports: scrapling pulls in playwright (~5s load). Defer until first use
# so the MCP server responds to initialize immediately.
_scrapling = None

# Module-level type placeholders — needed because FastMCP evaluates string
# annotations at tool registration time. Set to actual types on first fetch.
SetCookieParam: Any = None  # type: ignore[valid-type]
SelectorWaitStates: Any = None
FollowRedirects: Any = None
ImpersonateType: Any = None


def _get_scrapling():
    """Import scrapling on first call. Cached for subsequent calls."""
    global _scrapling, SetCookieParam, SelectorWaitStates, FollowRedirects, ImpersonateType
    if _scrapling is None:
        from scrapling.core.shell import Convertor
        from scrapling.engines.toolbelt.custom import Response as _SResponse
        from scrapling.engines.static import ImpersonateType as _Imp
        from scrapling.fetchers import FetcherSession, AsyncDynamicSession, AsyncStealthySession
        from scrapling.core._types import SetCookieParam as _SCP, SelectorWaitStates as _SWS, FollowRedirects as _FR
        from types import SimpleNamespace
        _scrapling = SimpleNamespace()
        _scrapling.Convertor = Convertor
        _scrapling.Response = _SResponse
        _scrapling.ImpersonateType = _Imp
        _scrapling.FetcherSession = FetcherSession
        _scrapling.AsyncDynamicSession = AsyncDynamicSession
        _scrapling.AsyncStealthySession = AsyncStealthySession
        _scrapling.SetCookieParam = _SCP
        _scrapling.SelectorWaitStates = _SWS
        _scrapling.FollowRedirects = _FR
        # Also set module-level placeholders so function signature evaluation works
        SetCookieParam = _SCP  # type: ignore[assignment]
        SelectorWaitStates = _SWS
        FollowRedirects = _FR
        ImpersonateType = _Imp
    return _scrapling

if TYPE_CHECKING:
    from scrapling.engines.toolbelt.custom import Response as _ScraplingResponse
    from scrapling.fetchers import FetcherSession, AsyncDynamicSession, AsyncStealthySession
    from master_fetch.search import SearchResponseModel
    from mcp.server.fastmcp import Image
    from mcp.types import ImageContent, TextContent

from master_fetch.cache import get_cached, set_cached, clear_cache, clear_all_cache, DEFAULT_TTL
from master_fetch.robots import is_allowed, clear_robots_cache
from master_fetch.reddit import is_reddit_url, rewrite_to_old_reddit, parse_old_reddit_listing
from master_fetch.envelope import (
    classify_source, compute_freshness, detect_page_type, page_type_from_error,
)
from master_fetch.security import (
    validate_url,
    validate_css_selector,
    validate_headers,
    validate_proxy,
    validate_timeout,
    validate_search_query,
    redact_api_key,
    SecurityError,
)

# Extended extraction types (beyond Scrapling's markdown/html/text)
ExtendedExtractionType = Literal["markdown", "html", "text", "article", "structured"]
SessionType = Literal["dynamic", "stealthy"]
ScreenshotType = Literal["png", "jpeg"]

MAX_CONTENT_CHARS = 40000
MIN_CHUNK_CHARS = 500  # if remaining < this, merge into current chunk (avoids wasteful round-trips)
MAX_RESPONSE_BYTES = 50 * 1024 * 1024  # 50MB hard cap for response bodies
MAX_BULK_URLS = 100  # hard cap to prevent DoS via unbounded parallel requests
def _env_int(name: str, default: int) -> int:
    """Read an integer env var, falling back to default on missing/invalid."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default

# Idle browser close: after this many seconds with no smart_fetch/screenshot in
# flight, the warm Patchright Chrome is closed entirely (process exits, OS reclaims
# all its RAM). The next fetch relaunches it (~2s cold start). Tuned via the
# HOUND_BROWSER_IDLE_TIMEOUT env var. Default 300s (5 min) so an agent actively
# working (30-90s think-pauses between fetches) keeps Chrome warm, while a hound
# left running in the background actually frees its RAM. Set to 0 to keep the
# browser alive forever (the old behavior, useful when RAM is not a concern).
AUTO_SESSION_IDLE_TIMEOUT = _env_int("HOUND_BROWSER_IDLE_TIMEOUT", 300)
IDLE_CHECK_INTERVAL = 60  # How often to check for idle sessions (seconds)

# v10 archive fallback: when a live fetch hard-fails (404/451/500/network/
# bot-block/auth), recover the page from the Internet Archive's closest snapshot.
# Default ON (the headline resilience feature). Set HOUND_ARCHIVE_FALLBACK=0 to
# disable globally. Per-call opt-out via the smart_fetch `archive_fallback` option.
_ARCHIVE_ENABLED = os.environ.get("HOUND_ARCHIVE_FALLBACK", "1").strip().lower() not in ("0", "false", "no", "off", "")

# MCP initialize `instructions` — injected into the agent's context ONCE on
# connect by clients that support it. This is the connect-time mastery doc:
# the #1 workflow, the gotchas, and when to use each tool. Kept tight (~300
# tokens) since it is paid once, not per-turn-per-tool.
HOUND_INSTRUCTIONS = (
    "Hound = web access. 4 tools (+ cache_clear, version).\n"
    "\n"
    "smart_fetch(url) - get any page. Auto anti-bot (HTTP -> stealthy). Text + metadata + signals (content_ok, next_action, summary, page_type, content_age_days/is_stale, source_type/is_official, source/archived_at). Hard-block (404/bot/auth) -> auto-recover from Internet Archive (source=archive.org + archived_at); archive_fallback=false to opt out.\n"
    "  Narrow: css_selector. Long page: offset/next_offset to paginate, or focus='query' for only relevant blocks (post-cache; re-pass when paginating). actions=[{click:..},{scroll:N},{fill:{selector,text}},{press:Enter},{wait:ms},{wait_selector:..}] for click/form/scroll (forces stealthy, bypasses cache). PDFs -> structured markdown + table_of_contents section-map [{level,title,page,end_page}] -> pass pages='23-31' for one section. Scanned/CID -> auto-OCR with [all] (quality_score 0-1). include_links=true -> response.links = {citations,navigation,external,primary_source}. Bulk: urls=[...]. cache_ttl=0 forces fresh.\n"
    "smart_search(query) - keyless web search (no API key). 10 backends in parallel (ddg,brave,mojeek,yahoo,yandex,startpage,google,qwant + opt-in wikipedia,grokipedia), neural-reranked + cross-backend consensus. Returns URLs + ranking, NOT content -> smart_fetch the matches. Each result: relevance_score + fetch_relevance (high/med/low) + engines_consensus. related_queries from result titles+snippets. Blocked backends circuit-broken 60s. NEVER answer from snippets alone. Filters in options: site/exclude_sites, location/language/region, page (0-10), freshness (day|week|month|year).\n"
    "smart_crawl(url) - deep-crawl a site. Best-first same-domain walk; each page: markdown + content_ok + page_type. List pages -> structured link list. BIG SITES: options sitemap=true maps whole site from sitemap.xml in ONE fetch (URL list + lastmod); sitemap='auto' = use if present else BFS. discover_only=true = URL map only. focus='query' crawls relevant pages first + focus-filters each. crawl_urls=[...] fetches a chosen subset. Caps: max_pages (10), max_depth (2), max_total_chars, deadline_ms.\n"
    "screenshot(url) - image capture. Multimodal agents only (content as images/canvas/visual layout). Text agents: use smart_fetch.\n"
    "\n"
    "#1 workflow: smart_search -> smart_fetch matching results -> synthesize with URLs.\n"
    "\n"
    "Unbypassable live (no free tool beats): DataDome, Akamai, Cloudflare Turnstile. smart_fetch already auto-recovers hard-blocks from the Internet Archive; if it still fails (no snapshot), switch sources - don't retry same URL.\n"
)

class ResponseModel(BaseModel):
    """Request's response information structure."""
    status: int = Field(description="HTTP status (0=network error)")
    content: list[str] = Field(description="Extracted text (truncated if is_truncated)")
    url: str = Field(description="Final URL")
    cached: bool = Field(default=False, description="From cache")
    fetcher_used: str = Field(default="", description="http/dynamic/stealthy/cache/none")
    extracted_type: str = Field(default="markdown", description="markdown|html|text|article|structured")
    session_id: str = Field(default="", description="Browser session ID")
    duration_ms: float = Field(default=0, description="Duration ms")
    error: str = Field(default="", description="Error + recovery hints")
    content_type: str = Field(default="", description="e.g. text/html, application/json")
    total_size_bytes: int = Field(default=0, description="Raw body bytes")
    total_extracted_chars: int = Field(default=0, description="Total chars of extracted text (before chunking). Use to gauge how much remains: total_extracted_chars - offset")
    is_truncated: bool = Field(default=False, description="True=more extracted content. Use next_offset. Check total_extracted_chars to see how much remains.")
    next_offset: int = Field(default=0, description="Next offset when is_truncated. 0=no more")
    escalation_path: str = Field(default="", description="e.g. http→stealthy. Pre-v3.5 logs may contain http→dynamic→stealthy entries from the old 3-tier path.")
    retry_count: int = Field(default=0, description="Retries")
    # Agent-facing signals (set by _with_agent_hints on every finalized response).
    summary: str = Field(default="", description="One-line status for quick reasoning, e.g. '200 OK · 12.4KB markdown · http · truncated'")
    content_ok: bool = Field(default=False, description="True = real content retrieved (status<400, no error, not a JS shell/login wall). Check this before trusting content.")
    next_action: str = Field(default="", description="Suggested next call when one is obvious (paginate/retry/switch source). Empty = nothing to do.")
    fetched_at: str = Field(default="", description="ISO-8601 UTC timestamp this response was generated. For cached responses, content age is bounded by cache_ttl.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Page metadata for citation/relevance: title, description, site_name, type, image, canonical, lang, published_time, author (OpenGraph + JSON-LD + canonical). For PDFs: title, author, subject, keywords, creator, producer, creation_date, mod_date. Empty for non-HTML/non-PDF.")
    media: List[str] = Field(default_factory=list, description="Image URLs on the page (only populated when include_media=true). Multimodal agents can fetch/screenshot these. For PDFs: per-page embedded-image metadata (count + dimensions).")
    links: Dict[str, Any] = Field(default_factory=dict, description="Outgoing links classified by context (only populated when include_links=true): {citations:[{url,text}], navigation:[{url,text}], external:[{url,text}], primary_source:url}. citations = links inside the main-content area (the page's referenced sources - the highest-value links to follow); navigation = site chrome; external = off-domain links; primary_source = best-effort hint at the actual primary source (canonical/JSON-LD or a citation on arxiv/doi/github/etc). Use to follow a page's source chain in one step.")
    quality_score: float = Field(default=0.0, description="PDF extraction quality 0.0-1.0 (readable-char ratio; 1.0 = clean, low = garbled/CID corruption). 0.0 for non-PDF. Trust PDF content more the closer this is to 1.0.")
    table_of_contents: list = Field(default_factory=list, description="PDF section-map: outline/bookmarks as [{level, title, page, end_page}] when the PDF has a ToC; for PDFs without bookmarks, a heading-based map is built from font-size detection. page+end_page give a range per section so you can pass pages='X-Y' to grab one section. Empty for non-PDF or PDFs with no detectable headings.")
    # ─── v10 research-grade envelope (additive; all default-valued) ───
    # page_type: structural class of the page, computed from raw HTML. Drives
    # next_action (list pages point to their links, auth walls suggest archive).
    page_type: str = Field(default="unknown", description="Structural class: article|docs|list|forum|qa|pdf|js_shell|auth_wall|paywall|redirect|image|json|unknown. Drives next_action. 'list' = a page whose main content is links to other pages (fetch those or smart_crawl). 'auth_wall'/'paywall' = content behind login/payment.")
    # source_type + is_official: domain-based authority signal so the agent can
    # weigh trust without a separate lookup. Conservative: is_official is True
    # only on a strong signal (vendor's own docs domain, gov, edu, github).
    source_type: str = Field(default="unknown", description="Domain authority class: vendor-docs|official-docs|news|blog|forum|qa|gov|edu|github|docs-site|ecommerce|unknown. Helps weigh source trust.")
    is_official: bool = Field(default=False, description="True only on a strong signal that this is the canonical/official source for its subject (vendor docs, gov, edu, github, the org's own domain). Conservative default False.")
    # Freshness: content_age_days from the page's own published/modified date
    # (OpenGraph/JSON-LD/PDF). -1 = no date recoverable. is_stale = age > 365d.
    content_age_days: int = Field(default=-1, description="Age in days from the page's published/modified date (OpenGraph/JSON-LD/PDF creation_date). -1 = no date recoverable. Pair with is_stale to judge currency.")
    is_stale: bool = Field(default=False, description="True when content_age_days > 365 (info may be outdated). For news/current-state questions, seek a newer source.")
    # source + archived_at: set ONLY when this content came from the Internet
    # Archive (auto-fallback after a live hard-block). 'live' (default) = the
    # real page. Honest marking so the agent knows it may be a dated snapshot.
    source: str = Field(default="live", description="'live' (default) = fetched from the real URL. 'archive.org' = the live site hard-blocked and this content was recovered from the Internet Archive's closest snapshot (see archived_at for the snapshot date).")
    archived_at: str = Field(default="", description="ISO date of the archive.org snapshot when source='archive.org'. Empty when source='live'. The content reflects the page as it was on this date.")


class BulkResponseModel(BaseModel):
    """Response from bulk fetch operations, one result per URL."""
    results: list[ResponseModel] = Field(description="Per-URL results")
    total: int = Field(description="Total URLs")
    successful: int = Field(description="Fetches with status<400 + no error")


class ArticleModel(BaseModel):
    """Structured article data extracted by Trafilatura."""
    title: str = Field(description="Article title")
    author: str = Field(description="Article author")
    date: str = Field(description="Publication date")
    body: str = Field(description="Main article text")
    description: str = Field(description="Article summary")
    url: str = Field(description="Source URL")
    categories: list[str] = Field(default=[], description="Categories")
    tags: list[str] = Field(default=[], description="Tags")


class SessionInfo(BaseModel):
    """Information about an open browser session."""
    session_id: str = Field(description="Session ID")
    session_type: SessionType = Field(description="dynamic|stealthy")
    created_at: str = Field(description="ISO timestamp")
    is_alive: bool = Field(description="Session alive?")


class SessionCreatedModel(SessionInfo):
    """Response returned when a new session is created."""
    message: str = Field(description="Confirmation message")


class SessionClosedModel(BaseModel):
    """Response returned when a session is closed."""
    session_id: str = Field(description="Closed session ID")
    message: str = Field(description="Confirmation message")


class CacheInfoModel(BaseModel):
    """Response from cache management operations."""
    message: str = Field(description="Result message")
    purged: int = Field(default=0, description="Entries purged")


class VersionInfoModel(BaseModel):
    """Hound version and update status."""
    version: str = Field(description="Installed version")
    latest: str = Field(default="", description="Latest PyPI version")
    up_to_date: bool = Field(default=True, description="Installed >= latest?")
    update_command: str = Field(default="hound -u", description="Update command")


@dataclass
class _SessionEntry:
    session: Any  # AsyncDynamicSession | AsyncStealthySession
    session_type: SessionType
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    _alive: bool = True


# ─── Content quality detection (module-level, used by the class) ─────

# Heuristic thresholds for catching JS-rendered SPAs whose HTTP shell text
# doesn't match the known signal phrases (e.g. quotes.toscrape.com/js returns
# a nav-only shell). Scoped to the HTTP tier so stealthy-rendered low-text
# pages (image galleries, canvas apps) don't false-positive.
_JS_SHELL_MIN_BODY_BYTES = 3000   # raw HTML body must be at least this large
_JS_SHELL_MIN_TEXT_CHARS = 200    # ...while extracted text is below this

_JS_SHELL_SIGNALS = [
    "enable javascript", "you need to enable javascript",
    "javascript is required", "javascript is disabled",
    "javascript to run this app", "javascript must be enabled",
    "please enable javascript", "requires javascript",
    "we've detected that javascript is disabled",
    "javascript is disabled in this browser",
    "enable javascript to run this app",
]

_GEO_REDIRECT_SIGNALS = [
    "choose a country", "select your country", "select your region",
    "shopping in the u.s.", "choose your country",
    "country selector", "region selector",
]


def _is_cloudflare_from_response(result: ResponseModel) -> bool:
    """Check if a ResponseModel indicates a bot challenge page.

    Detects common bot challenge signatures in page content including embedded
    Cloudflare challenges, generic CAPTCHA pages, and verification prompts.
    Does NOT distinguish DataDome/Turnstile from ordinary bot checks.

    IMPORTANT: Only meaningful on error status codes (403, 503). A status-200
    page about web security that mentions "cloudflare" is not a bot challenge.
    """
    # Guard: only check on error status codes where bot challenges make sense
    if result.status not in (403, 503):
        return False
    content_str = " ".join(result.content).lower()
    cf_signals = ["cloudflare", "cf-browser", "challenge-platform", "cf_chl_opt", "ray id"]
    dd_signals = ["captcha-delivery.com", "datadome", "dd="]
    generic_signals = ["please verify you are a human", "are you a robot", "checking your browser"]
    all_signals = cf_signals + dd_signals + generic_signals
    return any(signal in content_str for signal in all_signals)


def _is_js_shell(result: ResponseModel) -> bool:
    """Check if a response contains only a JS-only placeholder, not real content.

    Used by smart_fetch to decide whether to escalate from HTTP to stealthy.
    Pre-v3.5 callers may have passed through dynamic as an intermediate step.
    """
    content_str = " ".join(result.content).lower().strip()
    if not content_str:
        return True  # Empty content after extraction = JS shell or blank page
    if any(signal in content_str for signal in _JS_SHELL_SIGNALS):
        return True
    # Heuristic: the HTTP tier returned a 200 with a large HTML body but almost
    # no extractable text -> the page is JS-rendered and HTTP got the empty shell
    # (e.g. a SPA whose nav-only shell doesn't match the known signal phrases).
    # Scoped to the HTTP tier: a stealthy result with little text from a large
    # page is a genuinely low-text page (image gallery / canvas), not a shell.
    if result.fetcher_used == "http" and result.status == 200 \
            and result.total_size_bytes > _JS_SHELL_MIN_BODY_BYTES:
        text_len = sum(len(c) for c in result.content)
        if text_len < _JS_SHELL_MIN_TEXT_CHARS:
            return True
    return False


def _detect_content_issue(result: ResponseModel) -> str:
    """Detect content quality issues in a response. Returns error string or ''.

    Called on the final result to give the caller a signal that content may be unusable,
    even when HTTP status is 200. Sets the error field so AI agents can detect failures
    without having to parse content strings themselves.

    Note: Bot challenge detection is only applied to 403/503 responses. Legitimate
    articles about web security may contain "cloudflare" in body text with status 200.
    """
    content_str = " ".join(result.content).lower().strip()

    if _is_js_shell(result):
        return "js_shell_detected: page requires JavaScript rendering but fetcher returned placeholder"

    if any(signal in content_str for signal in _GEO_REDIRECT_SIGNALS):
        return "geo_redirect_detected: page returned region/country selector instead of content"

    # Only check for bot challenge on error status codes. Legitimate pages
    # (status 200) that mention "cloudflare" in body text are not bot challenges.
    if result.status in (403, 503) and _is_cloudflare_from_response(result):
        return "bot_challenge_detected: page returned bot challenge/verification page"

    # Universal: any 4xx/5xx is an error, even if the server returned an HTML
    # error page as content. Without this, a 404 page gets treated as real
    # content (error="" -> agent trusts it). Set the error field so agents,
    # cache, and archive fallback all see it as a failure.
    if result.status >= 400:
        return f"http_error_{result.status}: server returned error status"
    if result.status == 0:
        return "network_error: request failed (DNS/timeout/connection refused)"

    return ""


def _annotate_quality(result: ResponseModel) -> ResponseModel:
    """Check content quality and set error field if issues detected. Returns same result."""
    if not result.error:
        issue = _detect_content_issue(result)
        if issue:
            result.error = issue
    return result


def _is_cacheable(result: ResponseModel) -> bool:
    """True only for clean, usable content worth caching.

    Excludes: error statuses (4xx/5xx), JS shells, bot-challenge pages, geo
    redirects, all_tiers_failed, and blank/empty extractions. Caching any of
    those would serve broken pages from cache for the whole TTL.
    """
    if not (0 < result.status < 400):
        return False
    if result.error:
        return False
    return bool(result.content) and any(c.strip() for c in result.content)


def _is_archive_worthy(result: ResponseModel) -> bool:
    """True when a live fetch hard-failed in a way the Internet Archive can rescue.

    Conservative: never on success, soft errors, or cases archive can't fix
    (bad request, encrypted PDF). The caller also guards `result.source !=
    'archive'` to prevent re-entry when finalizing an archive result.
    """
    if result.source != "live":  # only live results are archive-worthy; never recurse on a recovered source
        return False
    err = (result.error or "").lower()
    if result.status == 404:      # page gone/deleted — archive goldmine
        return True
    if result.status == 451:      # legal block
        return True
    if result.status == 0:        # network/DNS/timeout — archive may have it
        return True
    if result.status >= 500:      # server error
        return True
    if result.status in (403, 503) and "bot_challenge" in err:
        return True
    if err.startswith("all_tiers_failed"):
        return True
    if err.startswith("auth_required"):
        return True
    return False


def _format_size(n: int) -> str:
    """Human-readable byte size for the summary line."""
    if not n:
        return "0B"
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def _agent_hints(result: ResponseModel) -> tuple[str, str, bool]:
    """Build (summary, next_action, content_ok) for a finalized fetch result.

    summary       — one-line status agents can pattern-match on at a glance.
    next_action   — the obvious next call, if any (paginate / bypass robots /
                    switch sources). Empty when there is nothing to do.
    content_ok    — True only when real content was retrieved (status<400, no
                    error, not a JS shell / login wall / empty page). Agents should
                    check this before trusting content.
    """
    has_content = bool(result.content) and any(c.strip() for c in result.content)
    # PDFs carry a quality-based content_ok verdict from the extractor (CID
    # garbage / corruption -> False even on HTTP 200). Respect it instead of
    # letting status-200 + has-content mask corruption (the P3 bug).
    if result.quality_score > 0:
        content_ok = result.content_ok and result.status > 0 and not result.error and has_content
    else:
        content_ok = (
            result.status > 0 and result.status < 400
            and not result.error
            and has_content
        )

    size = result.total_size_bytes or sum(len(c) for c in result.content)
    parts: list[str] = []
    if result.status == 0:
        parts.append("network error")
    else:
        parts.append(f"{int(result.status)} {'OK' if result.status < 400 else 'ERR'}")
    parts.append(f"{_format_size(size)} {result.extracted_type or 'markdown'}")
    if result.fetcher_used:
        parts.append(result.fetcher_used)
    if result.cached:
        parts.append("cached")
    if result.is_truncated:
        parts.append("truncated")
    summary = " · ".join(parts)

    next_action = ""
    err = result.error or ""
    if result.is_truncated and result.next_offset:
        next_action = f"paginate: call smart_fetch with offset={result.next_offset}"
    elif err == "robots_txt_disallowed":
        next_action = "blocked by robots.txt: set options.respect_robots=false to bypass"
    elif err.startswith("js_shell_detected"):
        next_action = "page is a JS shell; re-fetch auto-escalates to the stealthy browser"
    elif err.startswith("bot_challenge_detected"):
        next_action = "bot challenge page; re-fetch auto-escalates to the stealthy browser"
    elif err.startswith("geo_redirect_detected"):
        next_action = "geo redirect: try a different regional URL or a proxy"
    elif err.startswith("scanned_pdf"):
        next_action = "scanned/image-only PDF - install hound-mcp[all] to auto-OCR, or use a vision-capable tool / another source"
    elif (not result.content_ok) and result.quality_score > 0 and result.quality_score < 0.7 and not err:
        next_action = "low-quality PDF extraction (CID font corruption / garbled text) - install hound-mcp[all] for auto-OCR, or use a vision tool / screenshot on the flagged pages"
    elif err.startswith("encrypted_pdf"):
        next_action = "encrypted PDF - pass a password via the 'password' option"
    elif err.startswith("pdf_deps_missing"):
        next_action = "PDF support not installed - run: pip install hound-mcp[all]"
    elif err.startswith("not_a_pdf") or err.startswith("pdf_open_failed") or err.startswith("pdf_extract_failed"):
        next_action = "PDF could not be parsed - see error field"
    elif "all_tiers_failed" in err:
        next_action = "all fetchers failed; site may use unbypassable protection (DataDome/Akamai/Turnstile) - switch sources"
    elif result.status == 0 or result.status >= 400:
        next_action = "fetch failed - see error field"

    # v10 envelope-driven next actions: fire ONLY when the fetch succeeded with
    # real content and no error-driven next_action already fired. Turn the
    # envelope (source/page_type/freshness) into a concrete next step so the
    # agent doesn't have to re-derive it. Precedence: archive-source > page
    # structure (list/auth/paywall/redirect) > freshness.
    if not next_action and content_ok:
        if result.source == "archive.org":
            next_action = (
                f"archive.org snapshot from {result.archived_at or 'unknown date'}; "
                "the live site is blocking automated fetches — retry later or "
                "use a different source for current data"
            )
        elif result.page_type == "list":
            cits = (result.links or {}).get("citations") or []
            top = [c.get("url", "") for c in cits[:3] if isinstance(c, dict) and c.get("url")]
            if top:
                next_action = (
                    "this is a list page; the content you want is likely behind its "
                    f"links. Top targets: {', '.join(top)}. Or call smart_crawl on this URL."
                )
            else:
                next_action = (
                    "this is a list page (links to other pages); call smart_crawl on "
                    "this URL, or fetch the linked pages directly"
                )
        elif result.page_type == "auth_wall":
            next_action = (
                "content behind login/authentication; the Internet Archive may have a "
                "snapshot, or switch sources"
            )
        elif result.page_type == "paywall":
            next_action = "paywalled content; try the Internet Archive or a different source"
        elif result.page_type == "redirect":
            canon = (result.metadata or {}).get("canonical") or ""
            next_action = (
                f"page redirected; the real URL is {canon}" if canon
                else "page redirected; check the final URL field"
            )
        elif result.is_stale and result.page_type in ("article", "docs", "unknown"):
            next_action = (
                f"content is {result.content_age_days} days old (may be outdated); for "
                "current info, smart_search a recent query (e.g. add the current year)"
            )
    return summary, next_action, content_ok


def _apply_envelope(result: ResponseModel) -> None:
    """Compute the v10 research-grade envelope fields on a result in place.

    page_type: definitive error/content_type signals override the structural
    value set in _translate_response (js_shell/auth_wall/redirect from error;
    pdf/json/image from content_type). source_type/is_official from the URL
    (cheap heuristic). content_age_days/is_stale from metadata dates + fetched_at.
    Recomputed on every return (incl. cache hits) since it is near-free and the
    inputs (url, metadata, fetched_at) are always present.
    """
    # page_type override: definitive signals win over the structural guess.
    err_type = page_type_from_error(result.error)
    if err_type:
        result.page_type = err_type
    else:
        ct = (result.content_type or "").lower()
        if ct.startswith("application/pdf"):
            result.page_type = "pdf"
        elif ct.startswith("application/json") or ct.startswith("text/json"):
            result.page_type = "json"
        elif ct.startswith("image/"):
            result.page_type = "image"
        # else: keep the structural page_type from _translate_response
        # (forum/qa/list/docs/article/paywall/redirect/unknown).
    # Source authority: cheap URL heuristic, recomputed always (not cached).
    st, off = classify_source(result.url)
    result.source_type = st
    result.is_official = off
    # Freshness: from metadata dates vs this response's fetched_at. Recomputed
    # always (metadata may be cache-restored; age is relative to now).
    age, stale = compute_freshness(result.metadata, result.fetched_at)
    result.content_age_days = age
    result.is_stale = stale


def _with_agent_hints(result: ResponseModel) -> ResponseModel:
    """Stamp the v10 envelope + agent-facing hints on a result.

    This is the universal final wrapper (called by _apply_chunking on every
    return: live fetches, cache hits, robots blocks, archive fallback), so the
    envelope appears on every response an agent ever sees.
    """
    result.fetched_at = datetime.now(timezone.utc).isoformat()
    _apply_envelope(result)
    summary, next_action, content_ok = _agent_hints(result)
    result.summary = summary
    result.content_ok = content_ok
    result.next_action = next_action
    return result


def _apply_chunking(result: ResponseModel, max_chars: int = MAX_CONTENT_CHARS, offset: int = 0) -> ResponseModel:
    """Truncate content if it exceeds max_chars, starting from offset.

    Smart merge: if remaining content after a chunk is less than MIN_CHUNK_CHARS,
    include it all in the current chunk. This prevents wasteful round-trips where
    an agent calls again just to get 55 chars.

    Always sets total_extracted_chars so agents can gauge remaining content
    without making a follow-up call. Stamps agent-facing hints (summary,
    content_ok, next_action, fetched_at) on every returned result.
    """
    full_text = "\n".join(result.content)
    # Query-focused filter (post-cache): if the caller passed `focus`, keep only
    # the BM25-relevant blocks so the agent loads less context on long pages.
    # Only applies to text-like extractions (not raw html). Runs before chunking
    # so offset/next_offset page through the FOCUSED content.
    focus_q = _FOCUS.get()
    if focus_q and result.extracted_type in ("markdown", "text", "article", "structured"):
        try:
            from master_fetch.focus import focus_content
            full_text = focus_content(full_text, focus_q)
        except Exception as e:
            logger.debug("focus filter failed: %s", e)
    total_len = len(full_text)

    if offset >= total_len:
        # model_copy(update=...) preserves EVERY field by construction
        # (metadata/links/page_type/source_type/quality_score/toc/...). The
        # old hand-written constructor dropped any field not listed, so every
        # new envelope field silently vanished on the no-more-content branch.
        return _with_agent_hints(result.model_copy(update={
            "content": ["[No more content.]"],
            "total_extracted_chars": total_len,
            "is_truncated": False,
            "next_offset": 0,
        }))

    chunk = full_text[offset:offset + max_chars]
    chunk_len = len(chunk)
    remaining = total_len - offset - chunk_len

    # Smart merge: if remaining is small, include it all in this chunk.
    # Avoids wasteful round-trip where agent calls again for 55 chars.
    truncated = False
    next_off = 0
    if remaining > MIN_CHUNK_CHARS:
        truncated = True
        next_off = offset + chunk_len
        remaining_hint = total_len - next_off
        chunk += (
            f"\n\n[Truncated: showing {chunk_len:,} of {total_len:,} extracted chars. "
            f"{remaining_hint:,} chars remaining. Next offset: {next_off}]"
        )
    elif remaining > 0:
        # Remaining is small — include it all, no truncation flag
        chunk = full_text[offset:]

    # model_copy(update=...) preserves every field by construction — no more
    # hand-maintained field list that silently dropped envelope fields on
    # truncation. Add a field to ResponseModel and it survives chunking free.
    return _with_agent_hints(result.model_copy(update={
        "content": [chunk],
        "total_extracted_chars": total_len,
        "is_truncated": truncated,
        "next_offset": next_off,
    }))


# ─── Response translation helpers ──────────────────────────────────

# PDF extraction options flow from smart_fetch down to _translate_response via
# contextvars (task-local, safe under concurrent bulk fetches) instead of
# threading two new params through every fetcher signature.
_PDF_PAGES: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_pdf_pages", default=None)
_PDF_PASSWORD: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_pdf_password", default=None)
# Query-focused content filter (smart_fetch `focus` param). Applied POST-cache
# inside _apply_chunking: the full extracted text is cached once, and different
# focus queries are just different BM25 views over the same cached content.
_FOCUS: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_focus", default=None)
# Opt-in: populate ResponseModel.media with the page's image URLs (multimodal).
_INCLUDE_MEDIA: contextvars.ContextVar[bool] = contextvars.ContextVar("_include_media", default=False)
_INCLUDE_LINKS: contextvars.ContextVar[bool] = contextvars.ContextVar("_include_links", default=False)
# v10 archive fallback opt-out (per-call). Default True. Set False to get the
# raw live failure instead of an archive.org snapshot when the live site blocks.
_ARCHIVE_FALLBACK: contextvars.ContextVar[bool] = contextvars.ContextVar("_archive_fallback", default=True)


def _extract_pdf_response(body: bytes, raw_ct: str, total_size: int, url: str,
                          extraction_type: str, fetcher_used: str, duration_ms: float) -> ResponseModel:
    """Build a ResponseModel from a PDF body using the flagship extractor."""
    pages = _PDF_PAGES.get()
    password = _PDF_PASSWORD.get()
    include_media = _INCLUDE_MEDIA.get()
    try:
        from master_fetch.pdf_extractor import extract_pdf, PdfResult
        result: PdfResult = extract_pdf(body, extraction_type=extraction_type,
                                        pages=pages, password=password,
                                        include_media=include_media)
    except ImportError as e:
        return ResponseModel(
            status=200, content=[f"[PDF extraction requires hound-mcp[all]. {e}]"],
            url=url, fetcher_used=fetcher_used, duration_ms=duration_ms,
            content_type=raw_ct, total_size_bytes=total_size,
            extracted_type="markdown", error=f"pdf_deps_missing: {e}",
        )
    except Exception as e:
        return ResponseModel(
            status=200, content=[f"[PDF extraction failed: {str(e)[:200]}]"],
            url=url, fetcher_used=fetcher_used, duration_ms=duration_ms,
            content_type=raw_ct, total_size_bytes=total_size,
            extracted_type="markdown", error=f"pdf_extract_failed: {str(e)[:200]}",
        )
    # Preserve the structured fields from the text pass; the scanned-OCR swap
    # below only replaces content + the scanned flag, not metadata/toc/quality.
    base_meta = result.metadata
    base_toc = result.table_of_contents
    base_media = result.media
    # Scanned / image-only PDF: fall back to OCR if the OCR extras are installed.
    if result.scanned and not result.encrypted:
        try:
            from master_fetch.ocr import ocr_pdf, ocr_available
            if ocr_available():
                ocr_result = ocr_pdf(body, pages=pages, password=password)
                if ocr_result.content and not ocr_result.error:
                    # Swap content but keep the text pass's metadata/toc.
                    result.content = ocr_result.content
                    result.error = ""
                    result.scanned = False
                    result.ocr_fallback_used = True
                    result.quality_score = max(result.quality_score, 0.9)
                    result.content_ok = True
                elif ocr_result.error and ocr_result.encrypted:
                    result = ocr_result  # encrypted surfaced by OCR path too
                elif ocr_result.error:
                    result.content = [f"[Scanned PDF - OCR attempted but failed: {ocr_result.error[:160]}]"]
                    result.error = f"ocr_failed: {ocr_result.error[:160]}"
            # else: OCR extras not installed -> keep the scanned dead-end below
        except ImportError:
            pass
        except Exception as e:
            logger.debug("OCR fallback failed for %s: %s", url, e)
    return ResponseModel(
        status=200, content=result.content, url=url,
        fetcher_used=fetcher_used, duration_ms=duration_ms,
        content_type=raw_ct, total_size_bytes=total_size,
        extracted_type="markdown", error=result.error,
        content_ok=result.content_ok, metadata=base_meta or result.metadata,
        table_of_contents=base_toc or result.table_of_contents,
        quality_score=result.quality_score, media=base_media or result.media,
    )


def _translate_response(
    page: _ScraplingResponse,
    extraction_type: str,
    css_selector: Optional[str],
    main_content_only: bool,
    use_trafilatura: bool = False,
    fetcher_used: str = "",
    duration_ms: float = 0,
) -> ResponseModel:
    """Extract content from a response and translate it to a ResponseModel.

    When use_trafilatura=True, ALL non-HTML extraction types go through
    Trafilatura first. Trafilatura has its own robust fallback chain internally,
    so we only fall back to Scrapling if Trafilatura completely fails.

    For JSON responses (content-type: application/json), extraction is skipped
    and the raw JSON is returned directly to avoid mangling by HTML extractors.
    """
    # Enforce response size limit before any processing
    _check_response_size(page)

    # Extract metadata from raw response
    resp_headers = getattr(page, 'headers', {}) or {}
    raw_ct = resp_headers.get('content-type', '') if isinstance(resp_headers, dict) else ''
    raw_body = getattr(page, 'body', None)
    total_size = len(raw_body) if isinstance(raw_body, bytes) else 0

    # Detect JSON responses. Return raw JSON without extraction.
    is_json = raw_ct.startswith('application/json') or raw_ct.startswith('text/json')
    if is_json and raw_body:
        try:
            json_text = raw_body.decode(page.encoding or 'utf-8', errors='replace')
            return ResponseModel(
                status=page.status, content=[json_text], url=page.url,
                fetcher_used=fetcher_used, duration_ms=duration_ms,
                content_type=raw_ct, total_size_bytes=total_size,
            )
        except Exception:
            pass  # Fall through to normal extraction if JSON decode fails

    # Detect PDF responses. Route to the flagship PDF extractor instead of the
    # HTML/text pipeline (which would return a useless "binary content" error).
    # Many servers serve PDFs as application/octet-stream, so the %PDF magic-byte
    # check is the reliable detector.
    is_pdf = raw_ct.startswith('application/pdf') or (bool(raw_body) and raw_body[:5].startswith(b'%PDF'))
    if is_pdf and raw_body:
        return _extract_pdf_response(raw_body, raw_ct, total_size, page.url,
                                     extraction_type, fetcher_used, duration_ms)
    # PDF-intent URL (.pdf) that returned HTML, not a PDF: a login/paywall/error
    # redirect. Don't extract the login HTML as if it were content (P6/P14).
    _url_path = (page.url or '').lower().split('?')[0]
    if _url_path.endswith('.pdf') and raw_body and not raw_body[:5].startswith(b'%PDF'):
        try:
            head = raw_body[:4096].decode(getattr(page, 'encoding', None) or 'utf-8', errors='ignore').lower()
        except Exception:
            head = ''
        if any(m in head for m in ('sign in', 'log in', 'login', 'password',
                                   'subscribe', 'paywall', 'access denied', 'authenticate')):
            err = ("auth_required: URL ends in .pdf but returned a login/paywall page, "
                   "not the PDF. The content is behind authentication.")
        else:
            err = ("not_a_pdf: URL ends in .pdf but the response is HTML, not a PDF "
                   "(possibly a redirect/error page). Try the direct PDF link.")
        return ResponseModel(status=getattr(page, 'status', 200), content=[f"[{err}]"],
                             url=page.url, fetcher_used=fetcher_used,
                             duration_ms=duration_ms, content_type=raw_ct,
                             total_size_bytes=total_size, extracted_type="markdown",
                             error=err, content_ok=False)

    # Image-only page (content-type image/*): OCR it to text if the OCR extras
    # are installed. Many pages are just a PNG/JPEG (screenshots, scans, memes,
    # image-of-text); without OCR the agent gets nothing useful.
    is_image = raw_ct.startswith('image/') and bool(raw_body)
    if is_image and raw_body:
        try:
            from master_fetch.ocr import ocr_image_bytes, ocr_available
            if ocr_available():
                text = ocr_image_bytes(raw_body)
                if text:
                    return ResponseModel(
                        status=page.status, content=[text], url=page.url,
                        fetcher_used=fetcher_used, duration_ms=duration_ms,
                        content_type=raw_ct, total_size_bytes=total_size,
                        extracted_type="text",
                    )
                return ResponseModel(
                    status=page.status,
                    content=["[Image page - OCR detected no extractable text.]"],
                    url=page.url, fetcher_used=fetcher_used, duration_ms=duration_ms,
                    content_type=raw_ct, total_size_bytes=total_size,
                    extracted_type="text", error="image_ocr_empty",
                )
            return ResponseModel(
                status=page.status,
                content=["[Image page (content-type image/*). Install hound-mcp[all] for OCR text extraction.]"],
                url=page.url, fetcher_used=fetcher_used, duration_ms=duration_ms,
                content_type=raw_ct, total_size_bytes=total_size,
                extracted_type="text", error="image_ocr_unavailable",
            )
        except Exception as e:
            return ResponseModel(
                status=page.status,
                content=[f"[Image page - OCR failed: {str(e)[:160]}]"],
                url=page.url, fetcher_used=fetcher_used, duration_ms=duration_ms,
                content_type=raw_ct, total_size_bytes=total_size,
                extracted_type="text", error=f"image_ocr_failed: {str(e)[:160]}",
            )

    s = _get_scrapling()
    content: list[str]
    
    # Reddit optimization: use custom parser for old.reddit.com listings
    page_url = getattr(page, 'url', '') or ''
    is_old_reddit_listing = (
        'old.reddit.com' in page_url
        and '/comments/' not in page_url  # Not a post page
        and extraction_type in ("markdown", "text")
    )
    
    if is_old_reddit_listing and raw_body:
        try:
            html_text = raw_body.decode(page.encoding or 'utf-8', errors='replace')
            parsed = parse_old_reddit_listing(html_text)
            if parsed:  # parser found real posts -> use structured markdown
                content = [parsed]
            else:
                # Fallback to normal extraction
                content = list(
                    s.Convertor._extract_content(
                        page,
                        css_selector=css_selector,
                        extraction_type=extraction_type if extraction_type in ("markdown", "html", "text") else "markdown",
                        main_content_only=main_content_only,
                    )
                )
        except Exception:
            # Fallback to normal extraction
            content = list(
                s.Convertor._extract_content(
                    page,
                    css_selector=css_selector,
                    extraction_type=extraction_type if extraction_type in ("markdown", "html", "text") else "markdown",
                    main_content_only=main_content_only,
                )
            )
    elif use_trafilatura and extraction_type in ("markdown", "text", "article", "structured"):
        from master_fetch.trafilatura_extractor import extract_with_trafilatura
        content = extract_with_trafilatura(page, extraction_type=extraction_type, css_selector=css_selector)
        if not content or content == [""] or content == ["\n"]:
            content = list(
                s.Convertor._extract_content(
                    page,
                    css_selector=css_selector,
                    extraction_type=extraction_type if extraction_type in ("markdown", "html", "text") else "markdown",
                    main_content_only=main_content_only,
                )
            )
    else:
        content = list(
            s.Convertor._extract_content(
                page,
                css_selector=css_selector,
                extraction_type=extraction_type if extraction_type in ("markdown", "html", "text") else "markdown",
                main_content_only=main_content_only,
            )
        )

    if page.status == 503 and fetcher_used == "stealthy":
        note = "[503 via stealthy fetcher. The target server may block headless browser fingerprints. Try smart_fetch or http/dynamic fetcher instead.]"
        content = [note]

    # Metadata enrichment (OpenGraph + JSON-LD + canonical + <title>) for HTML
    # pages. JSON/PDF/image responses return earlier with metadata={}. Cheap
    # regex pass over the raw body; never blocks the response.
    page_metadata: Dict[str, Any] = {}
    page_media: List[str] = []
    page_links: Dict[str, Any] = {}
    page_html: str = ""  # raw HTML for page_type detection (v10 envelope)
    if raw_body and isinstance(raw_body, (bytes, bytearray)):
        try:
            _html = raw_body.decode(getattr(page, 'encoding', None) or 'utf-8', errors='replace')
            page_html = _html
            from master_fetch.metadata import extract_metadata, extract_image_urls
            page_metadata = extract_metadata(_html, page_url)
            if _INCLUDE_MEDIA.get():
                page_media = extract_image_urls(_html, page_url)
            if _INCLUDE_LINKS.get():
                try:
                    from master_fetch.links import extract_links
                    page_links = extract_links(_html, page_url, page_metadata)
                except Exception as e:
                    logger.debug("links extraction failed for %s: %s", page_url, e)
                    page_links = {}
            else:
                page_links = {}
        except Exception as e:
            logger.debug("metadata/media extraction failed for %s: %s", page_url, e)

    # v10 page_type: structural class from raw HTML (forum/qa/list/docs/article/
    # paywall/redirect). pdf/json/image/js_shell/auth_wall are filled later in
    # _with_agent_hints from content_type/error (definitive signals override
    # this structural guess).
    _page_type = detect_page_type(
        page_html, page_url, raw_ct, sum(len(c) for c in content),
    )

    return ResponseModel(
        status=page.status, content=content, url=page.url,
        fetcher_used=fetcher_used, duration_ms=duration_ms,
        content_type=raw_ct, total_size_bytes=total_size, metadata=page_metadata,
        media=page_media, links=page_links, page_type=_page_type,
    )


def _check_response_size(page: _ScraplingResponse) -> None:
    """Raise if response body exceeds safety limit."""
    body = getattr(page, 'body', None)
    if body and isinstance(body, bytes) and len(body) > MAX_RESPONSE_BYTES:
        raise ValueError(
            f"Response body too large ({len(body):,} bytes, max {MAX_RESPONSE_BYTES:,} bytes)"
        )


async def _timed(coro):
    """Run a coroutine and return (result, elapsed_ms)."""
    t0 = now()
    result = await coro
    elapsed = (now() - t0) * 1000
    return result, elapsed


async def _safe_prewarm(coro_fn, timeout: float = 20.0) -> None:
    """Run a prewarm callable in the background, fully isolated.

    `coro_fn` is a zero-arg callable returning a coroutine. Catches
    BaseException (a hung/crashing prewarm NEVER takes down the server, not even
    CancelledError) and caps it at `timeout` so a stuck launch can't linger.
    Prewarm is best-effort by design.
    """
    try:
        await asyncio.wait_for(coro_fn(), timeout=timeout)
    except BaseException:
        pass


async def _safe_imported_prewarm(module_name: str, attr: str, timeout: float = 20.0) -> None:
    """Import and run an optional async prewarm without touching the event loop.

    Startup prewarms are best-effort. Their imports can be surprisingly heavy
    (Scrapling/Playwright/ONNX chains), so resolve the callable in a worker
    thread first; then run the coroutine with the same isolation as
    _safe_prewarm. Import failure, timeout, cancellation, and bad callables are
    all non-fatal.
    """
    def _resolve():
        import importlib
        module = importlib.import_module(module_name)
        return getattr(module, attr)

    try:
        coro_fn = await asyncio.to_thread(_resolve)
    except BaseException:
        return
    await _safe_prewarm(coro_fn, timeout=timeout)


def _normalize_credentials(credentials: Optional[Dict[str, str]]) -> Optional[tuple]:
    """Convert a credentials dictionary to a tuple accepted by fetchers.

    Returns None if credentials is None or empty.
    Validates types and lengths to prevent injection/DoS.
    """
    if not credentials:
        return None
    username = credentials.get("username")
    password = credentials.get("password")
    if username is None or password is None:
        raise ValueError("Credentials dictionary must contain both 'username' and 'password' keys")
    if not isinstance(username, str) or not isinstance(password, str):
        raise SecurityError("Credential username and password must be strings")
    if len(username) > 512 or len(password) > 512:
        raise SecurityError("Credential values exceed maximum length of 512 characters")
    if "\n" in username or "\r" in username or "\n" in password or "\r" in password:
        raise SecurityError("Credential values must not contain newline characters")
    return username, password


def _safe_cookie_dict(cookies: Sequence[SetCookieParam] | None) -> Optional[Dict[str, str]]:
    """Safely convert MCP cookie param list to {name: value} dict.

    Handles missing keys gracefully and logs warnings.
    Returns None for empty/None input.
    """
    if not cookies:
        return None
    result: Dict[str, str] = {}
    for c in cookies:
        if isinstance(c, dict):
            name = c.get("name", "")
            value = c.get("value", "")
            if name:
                result[name] = value
            else:
                # Don't log the dict — it may contain a sensitive cookie value.
                logger.warning("Cookie dict missing 'name' key, skipping")
    return result or None


# ─── Main server class ─────────────────────────────────────────────

class MasterFetchServer:
    """Enhanced MCP server built on Scrapling with smart routing, caching, and Trafilatura."""

    def __init__(self, cache_ttl: int = DEFAULT_TTL, use_trafilatura: bool = True):
        self._sessions: Dict[str, _SessionEntry] = {}
        self._sessions_lock: Lock = Lock()
        self._cache_ttl = cache_ttl
        self._use_trafilatura = use_trafilatura
        self._auto_dynamic_id: Optional[str] = None
        self._auto_stealthy_id: Optional[str] = None
        self._auto_dynamic_last_used: float = 0  # timestamp of last auto dynamic session use
        self._auto_stealthy_last_used: float = 0  # timestamp of last auto stealthy session use
        self._idle_monitor_task: Optional[Any] = None  # asyncio.Task for idle session cleanup
        self._auto_session_lock: Lock = Lock()  # serializes auto-session creation so the startup warm-up + a concurrent fetch never spawn a 2nd browser

    # ─── Core helpers ─────────────────────────────────────────────

    async def _get_session(self, session_id: str, expected_type: Optional[SessionType]) -> _SessionEntry:
        """Look up a session by ID, optionally validating its type.

        Holds the session lock to prevent races with close_session.
        Returns the entry with validation — the caller MUST NOT close
        the session concurrently while using the returned entry.
        """
        async with self._sessions_lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                raise ValueError(
                    f"Session '{session_id}' not found. Use list_sessions to see active sessions."
                )
            if not entry.session._is_alive:
                raise ValueError(
                    f"Session '{session_id}' is no longer alive. Open a new session."
                )
            if expected_type is not None and entry.session_type != expected_type:
                raise ValueError(
                    f"Session '{session_id}' is a '{entry.session_type}' session, but this tool "
                    f"requires a '{expected_type}' session. Use the matching fetch tool for your "
                    f"session type."
                )
            return entry

    async def _ensure_auto_session(self, session_type: SessionType) -> str:
        """Get or create an auto-persistent browser session. Avoids browser startup on every fetch.

        Race-safe: if two concurrent calls both pass the initial check,
        the second one closes its orphaned session and reuses the first.

        Idle timeout: when AUTO_SESSION_IDLE_TIMEOUT > 0, auto sessions close
        after that many seconds of inactivity. When it is 0 (default), the
        browser is kept alive forever and no idle monitor is started.
        """
        attr = "_auto_dynamic_id" if session_type == "dynamic" else "_auto_stealthy_id"
        ts_attr = "_auto_dynamic_last_used" if session_type == "dynamic" else "_auto_stealthy_last_used"

        # Fast path: reuse an existing alive session.
        async with self._sessions_lock:
            existing_id = getattr(self, attr)
            if existing_id and existing_id in self._sessions and self._sessions[existing_id].session._is_alive:
                setattr(self, ts_attr, now())
                self._ensure_idle_monitor()
                return existing_id

        # Serialize creation: the startup warm-up and a concurrent fetch share ONE
        # creation. A second caller waits on the lock, then reuses — never a 2nd
        # browser instance. (The previous close-the-orphan race can no longer
        # happen in production, but the final guard below still defends against
        # any path that sets the attr out-of-band.)
        async with self._auto_session_lock:
            # Re-check: another creator may have finished while we waited.
            async with self._sessions_lock:
                existing_id = getattr(self, attr)
                if existing_id and existing_id in self._sessions and self._sessions[existing_id].session._is_alive:
                    setattr(self, ts_attr, now())
                    self._ensure_idle_monitor()
                    return existing_id
            # Create outside the sessions lock (expensive — browser launch) but
            # inside the creation lock (no concurrent 2nd launch).
            sid = await self.open_session(session_type=session_type, headless=True)
            async with self._sessions_lock:
                existing_id = getattr(self, attr)
                if existing_id and existing_id in self._sessions and self._sessions[existing_id].session._is_alive:
                    try:
                        await self.close_session(sid.session_id)
                    except Exception:
                        pass  # Best effort cleanup
                    setattr(self, ts_attr, now())
                    self._ensure_idle_monitor()
                    return existing_id
                setattr(self, attr, sid.session_id)
                setattr(self, ts_attr, now())

        self._ensure_idle_monitor()
        return sid.session_id

    async def _prewarm_stealthy(self) -> None:
        """Warm the single stealthy browser at startup (background, best-effort).

        Scheduled when the MCP server starts so the browser is warm by the time
        the agent first needs a stealthy fetch or screenshot, skipping the
        ~3-5s cold start. Closes after HOUND_BROWSER_IDLE_TIMEOUT of inactivity,
        then relaunches on the next fetch. Idempotent: _ensure_auto_session
        reuses any existing session.

        Robustness (v8.2): fully isolated — catches BaseException (so a
        CancelledError or any launch failure can NEVER crash the server) and is
        capped at 30s so a hung browser launch can't hold the session-creation
        lock forever (a later real fetch can then take the lock and retry). On
        any failure the browser simply lazy-launches on the first stealthy fetch.
        """
        async def _warm():
            # Run the (synchronous, ~5s) scrapling/playwright import in a WORKER
            # THREAD, not on the event loop. A bare `_get_scrapling()` call here
            # freezes the single-threaded asyncio loop for the whole import, which
            # starves `server.run` so the `initialize` handshake never gets its
            # reply out — the MCP client sees a silent dead hang and reports
            # "failed to start". asyncio.wait_for cannot interrupt a synchronous
            # blocking call, so this matters even with the 30s cap below.
            await asyncio.to_thread(_get_scrapling)
            await self._ensure_auto_session("stealthy")
        try:
            await asyncio.wait_for(_warm(), timeout=30.0)
            logger.debug("Stealthy browser warmed at startup")
        except BaseException as e:
            logger.debug(f"Startup warm-up failed/skipped (will launch on first fetch): {e!r}")

    async def _start_idle_monitor(self) -> None:
        """Background task: close auto browser sessions after AUTO_SESSION_IDLE_TIMEOUT
        of inactivity.

        All reads of _auto_*_id and _auto_*_last_used happen inside the sessions lock
        to prevent races with _ensure_auto_session.
        Session closing happens outside the lock to avoid blocking other operations.
        """
        while True:
            await asyncio_sleep(IDLE_CHECK_INTERVAL)
            try:
                # If AUTO_SESSION_IDLE_TIMEOUT = 0, keep browser alive forever
                if AUTO_SESSION_IDLE_TIMEOUT == 0:
                    continue
                now_ts = now()
                async with self._sessions_lock:
                    # Check dynamic auto session (all reads under lock)
                    if self._auto_dynamic_id and now_ts - self._auto_dynamic_last_used > AUTO_SESSION_IDLE_TIMEOUT:
                        close_dynamic = self._auto_dynamic_id
                        self._auto_dynamic_id = None
                    else:
                        close_dynamic = None
                    # Check stealthy auto session (all reads under lock)
                    if self._auto_stealthy_id and now_ts - self._auto_stealthy_last_used > AUTO_SESSION_IDLE_TIMEOUT:
                        close_stealthy = self._auto_stealthy_id
                        self._auto_stealthy_id = None
                    else:
                        close_stealthy = None
                # Close sessions outside the lock to avoid blocking
                if close_dynamic:
                    try:
                        await self.close_session(close_dynamic)
                    except Exception as e:
                        logger.warning(f"Idle monitor failed to close dynamic session {close_dynamic}: {e}")
                        # Pop from sessions dict even if close() failed — don't orphan
                        async with self._sessions_lock:
                            entry = self._sessions.pop(close_dynamic, None)
                            if entry:
                                entry._alive = False
                if close_stealthy:
                    try:
                        await self.close_session(close_stealthy)
                    except Exception as e:
                        logger.warning(f"Idle monitor failed to close stealthy session {close_stealthy}: {e}")
                        # Pop from sessions dict even if close() failed — don't orphan
                        async with self._sessions_lock:
                            entry = self._sessions.pop(close_stealthy, None)
                            if entry:
                                entry._alive = False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Idle monitor check failed, will retry on next cycle")

    def _ensure_idle_monitor(self) -> None:
        """Start the idle monitor background task if not already running.

        Idempotent: safe to call from any code path (session creation, reuse, etc.).
        If the monitor task crashed, the next call restarts it.

        No-op when AUTO_SESSION_IDLE_TIMEOUT == 0 (keep-alive-forever mode) —
        avoids a perpetual background task that wakes every IDLE_CHECK_INTERVAL
        only to `continue`.
        """
        if AUTO_SESSION_IDLE_TIMEOUT == 0:
            return
        if self._idle_monitor_task is None or self._idle_monitor_task.done():
            self._idle_monitor_task = asyncio.create_task(self._start_idle_monitor())

    async def _shutdown_close_sessions(self) -> None:
        """Gracefully close all browser sessions when the server is stopping.

        Called from serve()'s finally block so the single warm Chrome instance
        is torn down cleanly when the agent harness closes the MCP server
        (stdin closed / process exit), rather than relying on OS child reaping.
        Best-effort: never raises.

        The critical detail on Windows: the patchright Chrome subprocess is an
        asyncio BaseSubprocessTransport. Closing the session schedules
        connection_lost via loop.call_soon; if the event loop exits before that
        callback runs, the transport's __del__ fires during GC AFTER the loop is
        closed and prints 'Exception ignored in __del__' tracebacks to stderr
        (RuntimeError: Event loop is closed / ValueError: I/O operation on closed
        pipe). An MCP client reading stderr sees a crash-like traceback even
        though the process exited 0. So we close the sessions, then EXPLICITLY
        flush the loop with a short sleep so pending callbacks drain while the
        loop is alive, then close any lingering asyncio subprocess transports
        so their __del__ is a no-op (they're already closing).
        """
        async with self._sessions_lock:
            entries = list(self._sessions.items())
            self._sessions.clear()
            self._auto_stealthy_id = None
            self._auto_dynamic_id = None
        for sid, entry in entries:
            try:
                await entry.session.close()
            except BaseException:
                pass
            entry._alive = False
        # Drain pending loop callbacks (the Chrome subprocess transport's
        # connection_lost) so the transports fully close while the loop is
        # alive. Without this, their __del__ warns/errors after loop close.
        try:
            await asyncio.sleep(0.15)
        except BaseException:
            pass
        # Belt-and-suspenders: explicitly close any asyncio subprocess
        # transports (the Chrome driver pipes) still holding the loop. This is
        # the only reliable way to silence the 'unclosed transport' ResourceWarning
        # + 'Event loop is closed' RuntimeError noise on Windows teardown.
        try:
            self._close_all_subprocess_transports()
        except BaseException:
            pass
        try:
            await asyncio.sleep(0.05)
        except BaseException:
            pass

    @staticmethod
    def _close_all_subprocess_transports() -> None:
        """Close every asyncio subprocess transport still alive on the current
        loop so their __del__ is a no-op (no 'unclosed transport' / 'Event loop
        is closed' noise on Windows teardown). Best-effort; never raises."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if loop is None:
            return
        try:
            procs = list(getattr(loop, "_subprocess_transports", {}).values())
        except Exception:
            procs = []
        for transport in procs:
            try:
                if not transport.is_closing():
                    transport.close()
            except BaseException:
                pass

    async def _finalize_result(
        self,
        result: ResponseModel,
        url: str,
        extraction_type: str,
        css_selector: Optional[str],
        cache_ttl: int,
        offset: int = 0,
        max_chars: int = MAX_CONTENT_CHARS,
    ) -> ResponseModel:
        """Apply content quality annotation, cache, and chunking to a fetch result.

        Centralizes the repetitive 'annotate -> cache -> chunk' pattern
        that was duplicated 8+ times across smart_fetch.
        """
        result = _annotate_quality(result)
        # v10: archive fallback for hard-fails. When the live tiers hard-blocked
        # the page, recover it from the Internet Archive's closest snapshot
        # (honestly marked source='archive.org'). Disabled by env or per-call
        # opt-out. _is_archive_worthy guards re-entry (source='archive' -> False).
        if (_ARCHIVE_ENABLED and _ARCHIVE_FALLBACK.get()
                and result.source == "live" and _is_archive_worthy(result)):
            from master_fetch.archive import try_archive_fallback
            archived = await try_archive_fallback(
                self, url, extraction_type, css_selector, cache_ttl,
                pages=_PDF_PAGES.get(), max_chars=max_chars,
            )
            if archived is not None:
                # Finalize the archive result: cache under the archive key +
                # chunk + envelope. _is_archive_worthy(archived) is False
                # (source='archive'), so this recursion terminates immediately.
                return await self._finalize_result(
                    archived, url, extraction_type, css_selector, cache_ttl,
                    offset, max_chars,
                )
        # Only cache CLEAN content. Caching JS shells / bot challenges / geo
        # redirects / error statuses would serve broken pages from cache for
        # the whole TTL (and the cache-hit path doesn't restore the error field,
        # so content_ok would come back True — the agent would trust garbage).
        if cache_ttl > 0 and _is_cacheable(result):
            await set_cached(
                url, extraction_type, result.content, result.status,
                css_selector, cache_ttl,
                content_type=result.content_type,
                total_size_bytes=result.total_size_bytes,
                pages=_PDF_PAGES.get(),
                source=result.source,
                envelope={
                    "metadata": result.metadata,
                    "media": result.media,
                    "links": result.links,
                    "quality_score": result.quality_score,
                    "table_of_contents": result.table_of_contents,
                    "page_type": result.page_type,
                    "source": result.source,
                    "archived_at": result.archived_at,
                },
            )
        return _apply_chunking(result, max_chars=max_chars, offset=offset)

    def _validate_smart_fetch_params(
        self,
        url: str,
        extraction_type: str,
        css_selector: Optional[str],
        extra_headers: Optional[Dict[str, str]],
        timeout: int | float,
        proxy: Optional[str | Dict[str, str]],
        useragent: Optional[str],
    ) -> tuple:
        """Validate and sanitize inputs for smart_fetch and related tools.

        Returns (validated_url, validated_css_selector, validated_headers,
                 validated_timeout, validated_proxy, validated_useragent).
        """
        url = validate_url(url)
        css_selector = validate_css_selector(css_selector)
        extra_headers = validate_headers(extra_headers)
        proxy = validate_proxy(proxy)

        # Timeout validation: browser uses ms, HTTP uses seconds.
        # Since smart_fetch can use both, validate as milliseconds (max 120s).
        timeout = validate_timeout(timeout)

        # User agent sanitization
        if useragent is not None:
            if not isinstance(useragent, str):
                raise SecurityError("User agent must be a string")
            useragent = useragent.strip()
            if "\n" in useragent or "\r" in useragent:
                raise SecurityError("User agent contains newline characters")

        return url, css_selector, extra_headers, timeout, proxy, useragent

    # ─── Session Management ──────────────────────────────────────

    async def open_session(
        self,
        session_type: SessionType,
        session_id: Optional[str] = None,
        headless: bool = True,
        google_search: bool = True,
        real_chrome: bool = False,
        wait: int | float = 0,
        proxy: Optional[str | Dict[str, str]] = None,
        timezone_id: str | None = None,
        locale: str | None = None,
        extra_headers: Optional[Dict[str, str]] = None,
        useragent: Optional[str] = None,
        cdp_url: Optional[str] = None,
        timeout: int | float = 30000,
        disable_resources: bool = False,
        wait_selector: Optional[str] = None,
        cookies: Sequence[SetCookieParam] | None = None,
        network_idle: bool = False,
        wait_selector_state: SelectorWaitStates = "attached",
        max_pages: int = 5,
        hide_canvas: bool = False,
        block_webrtc: bool = False,
        allow_webgl: bool = True,
        solve_cloudflare: bool = False,
        additional_args: Optional[Dict] = None,
    ) -> SessionCreatedModel:
        """Open a persistent browser session that can be reused across multiple fetch calls.

        This avoids the overhead of launching a new browser for each request.
        Internal helper — used by _ensure_auto_session to create the single
        warm stealthy session. Not exposed as an MCP tool.

        :param session_type: "dynamic" for standard Playwright, or "stealthy" for anti-bot bypass.
        :param session_id: Optional custom session ID (random 12-char hex if not provided).
        :param headless: Run browser headless (default True).
        :param google_search: Set Google referer header (default True).
        :param real_chrome: Use installed Chrome instead of Chromium.
        :param wait: Milliseconds to wait after everything finishes.
        :param proxy: Proxy string or dict with 'server', 'username', 'password'.
        :param timezone_id: Change browser timezone.
        :param locale: User locale, e.g., 'en-GB'.
        :param extra_headers: Extra headers to add to requests.
        :param useragent: Custom user agent string.
        :param cdp_url: Connect via CDP URL instead of launching a new browser.
        :param timeout: Timeout in milliseconds (default 30000).
        :param disable_resources: Drop font/image/media/stylesheet requests for speed.
        :param wait_selector: CSS selector to wait for before proceeding.
        :param cookies: Cookies for the session.
        :param network_idle: Wait until no network connections for 500ms.
        :param wait_selector_state: 'attached', 'detached', 'visible', or 'hidden'.
        :param max_pages: Max concurrent browser tabs (default 5).
        :param hide_canvas: (Stealthy) Random canvas noise for anti-fingerprinting.
        :param block_webrtc: (Stealthy) Prevent IP leak via WebRTC.
        :param allow_webgl: (Stealthy) Keep WebGL enabled (default True; WAFs check for it).
        :param solve_cloudflare: (Stealthy) Auto-solve Cloudflare challenges.
        :param additional_args: (Stealthy) Extra Playwright context args.
        """
        session_id = session_id or uuid4().hex[:12]
        async with self._sessions_lock:
            if session_id in self._sessions:
                raise ValueError(
                    f"Session '{session_id}' already exists. Use a different ID or close "
                    f"the existing one."
                )

        # Validate inputs
        validate_proxy(proxy)
        validate_headers(extra_headers)
        validate_css_selector(wait_selector)

        common_kwargs: Dict[str, Any] = dict(
            wait=wait, proxy=proxy, locale=locale, timeout=timeout, cookies=cookies,
            cdp_url=cdp_url, headless=headless, block_ads=True, max_pages=max_pages,
            useragent=useragent, timezone_id=timezone_id, real_chrome=real_chrome,
            network_idle=network_idle, wait_selector=wait_selector, google_search=google_search,
            extra_headers=extra_headers, disable_resources=disable_resources,
            wait_selector_state=wait_selector_state,
        )

        s = _get_scrapling()
        session: Union[s.AsyncDynamicSession, s.AsyncStealthySession]
        if session_type == "stealthy":
            session = s.AsyncStealthySession(
                **common_kwargs, hide_canvas=hide_canvas, block_webrtc=block_webrtc,
                allow_webgl=allow_webgl, solve_cloudflare=solve_cloudflare,
                additional_args=additional_args,
            )
        else:
            session = s.AsyncDynamicSession(**common_kwargs)

        entry = _SessionEntry(session=session, session_type=session_type)
        async with self._sessions_lock:
            self._sessions[session_id] = entry
        try:
            await session.start()
        except Exception:
            async with self._sessions_lock:
                entry._alive = False
                self._sessions.pop(session_id, None)
            raise

        return SessionCreatedModel(
            session_id=session_id, session_type=session_type,
            created_at=entry.created_at, is_alive=True,
            message=f"Session '{session_id}' ({session_type}) created successfully.",
        )

    async def close_session(self, session_id: Annotated[str, Field(description="Session ID to close")]) -> SessionClosedModel:
        """Close a persistent browser session and free its resources.

        :param session_id: The unique identifier of the session to close.
        """
        async with self._sessions_lock:
            entry = self._sessions.pop(session_id, None)
        if entry is None:
            raise ValueError(f"Session '{session_id}' not found.")
        await entry.session.close()
        return SessionClosedModel(
            session_id=session_id,
            message=f"Session '{session_id}' closed successfully.",
        )

    # ─── Screenshot ───────────────────────────────────────────────

    async def screenshot(
        self,
        url: str,
        session_id: Optional[str] = None,
        image_type: ScreenshotType = "png",
        full_page: bool = False,
        quality: Optional[int] = None,
        wait: int | float = 0,
        wait_selector: Optional[str] = None,
        wait_selector_state: SelectorWaitStates = "attached",
        network_idle: bool = False,
        timeout: int | float = 30000,
    ) -> List[ImageContent | TextContent]:
        """Capture a screenshot of a web page.

        If session_id is omitted, a stealthy browser session is auto-managed
        (reused across calls, so no cold-start after the first screenshot).
        Pass session_id only to reuse a specific session from open_session.

        :param url: The URL to navigate to and capture.
        :param session_id: Optional ID of an open browser session. If omitted, a stealthy session is auto-managed.
        :param image_type: Image format: "png" (default) or "jpeg".
        :param full_page: Capture full scrollable page instead of viewport.
        :param quality: JPEG quality (0-100), only for jpeg.
        :param wait: Milliseconds to wait after page load.
        :param wait_selector: CSS selector to wait for.
        :param wait_selector_state: State to wait for.
        :param network_idle: Wait for no network connections for 500ms.
        :param timeout: Timeout in milliseconds (default 30000).
        """
        url = validate_url(url)
        validate_css_selector(wait_selector)

        if quality is not None and image_type != "jpeg":
            raise ValueError("'quality' is only valid when 'image_type' is 'jpeg'.")

        # Auto-manage a stealthy session when none is provided (mirrors smart_fetch).
        if session_id:
            ssid = session_id
        else:
            ssid = await self._ensure_auto_session("stealthy")
        entry = await self._get_session(ssid, expected_type=None)
        screenshot_kwargs: Dict[str, Any] = {"type": image_type, "full_page": full_page}
        if quality is not None:
            screenshot_kwargs["quality"] = quality

        captured: Dict[str, Any] = {}

        async def _capture(page: Any) -> None:
            try:
                captured["bytes"] = await page.screenshot(**screenshot_kwargs)
                captured["url"] = page.url
            except Exception as exc:
                captured["error"] = exc

        await entry.session.fetch(
            url, wait=wait, timeout=timeout, network_idle=network_idle,
            wait_selector=wait_selector, wait_selector_state=wait_selector_state,
            page_action=_capture,
        )

        if "error" in captured:
            raise captured["error"]
        if "bytes" not in captured:
            raise RuntimeError(f"Failed to capture screenshot for {url}")

        from mcp.server.fastmcp import Image  # lazy: fastmcp is ~1s to import, only needed for screenshots
        from mcp.types import TextContent  # lazy: mcp.types is ~1s; only needed for screenshot output
        image = Image(data=captured["bytes"], format=image_type).to_image_content()
        return [image, TextContent(type="text", text=captured["url"])]

    # ─── HTTP Fetcher (curl_cffi) ─────────────────────────────────

    @staticmethod
    async def get(
        url: str,
        impersonate: ImpersonateType = "chrome",
        extraction_type: ExtendedExtractionType = "markdown",
        css_selector: Optional[str] = None,
        main_content_only: bool = True,
        use_trafilatura: bool = True,
        params: Optional[Dict] = None,
        headers: Optional[Mapping[str, Optional[str]]] = None,
        cookies: Optional[Dict[str, str]] = None,
        timeout: Optional[int | float] = 30,
        follow_redirects: FollowRedirects = "safe",
        max_redirects: int = 30,
        retries: Optional[int] = 3,
        retry_delay: Optional[int] = 1,
        proxy: Optional[str] = None,
        proxy_auth: Optional[Dict[str, str]] = None,
        auth: Optional[Dict[str, str]] = None,
        verify: Optional[bool] = True,
        http3: Optional[bool] = False,
        stealthy_headers: Optional[bool] = True,
    ) -> ResponseModel:
        """Make GET HTTP request with browser fingerprint impersonation.
        Fast, but only works for low-protection sites. For protected sites, use
        smart_fetch or stealthy_fetch.

        :param url: The URL to request.
        :param impersonate: Browser to impersonate (default 'chrome').
        :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
        :param css_selector: CSS selector to narrow content before extraction.
        :param main_content_only: Strip nav/ads/footers (default True).
        :param use_trafilatura: Use Trafilatura for article extraction (default True).
        :param params: Query string parameters.
        :param headers: Request headers.
        :param cookies: Request cookies.
        :param timeout: Timeout in seconds (default 30).
        :param follow_redirects: Redirect policy: 'safe', True, or False.
        :param max_redirects: Max redirects (default 30).
        :param retries: Retry attempts (default 3).
        :param retry_delay: Seconds between retries (default 1).
        :param proxy: Proxy URL.
        :param proxy_auth: Proxy auth dict with 'username' and 'password'.
        :param auth: HTTP basic auth dict with 'username' and 'password'.
        :param verify: Verify HTTPS certificates (default True).
        :param http3: Use HTTP/3 (default False).
        :param stealthy_headers: Generate real browser headers (default True).
        """
        url = validate_url(url)
        validate_css_selector(css_selector)
        validate_proxy(proxy)

        t0 = now()
        bulk = await MasterFetchServer.bulk_get(
            urls=[url], impersonate=impersonate, extraction_type=extraction_type,
            css_selector=css_selector, main_content_only=main_content_only,
            use_trafilatura=use_trafilatura, params=params, headers=headers,
            cookies=cookies, timeout=timeout, follow_redirects=follow_redirects,
            max_redirects=max_redirects, retries=retries, retry_delay=retry_delay,
            proxy=proxy, proxy_auth=proxy_auth, auth=auth, verify=verify,
            http3=http3, stealthy_headers=stealthy_headers,
        )
        result = bulk.results[0]
        result.duration_ms = (now() - t0) * 1000
        return result

    @staticmethod
    async def bulk_get(
        urls: List[str],
        impersonate: ImpersonateType = "chrome",
        extraction_type: ExtendedExtractionType = "markdown",
        css_selector: Optional[str] = None,
        main_content_only: bool = True,
        use_trafilatura: bool = True,
        params: Optional[Dict] = None,
        headers: Optional[Mapping[str, Optional[str]]] = None,
        cookies: Optional[Dict[str, str]] = None,
        timeout: Optional[int | float] = 30,
        follow_redirects: FollowRedirects = "safe",
        max_redirects: int = 30,
        retries: Optional[int] = 3,
        retry_delay: Optional[int] = 1,
        proxy: Optional[str] = None,
        proxy_auth: Optional[Dict[str, str]] = None,
        auth: Optional[Dict[str, str]] = None,
        verify: Optional[bool] = True,
        http3: Optional[bool] = False,
        stealthy_headers: Optional[bool] = True,
    ) -> BulkResponseModel:
        """Async parallel GET requests with browser fingerprint impersonation.
        Fast, but only works for low-protection sites.

        :param urls: List of URLs to request.
        :param impersonate: Browser to impersonate (default 'chrome').
        :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
        :param css_selector: CSS selector to narrow content.
        :param main_content_only: Strip nav/ads/footers (default True).
        :param use_trafilatura: Use Trafilatura for article extraction (default True).
        :param params: Query parameters.
        :param headers: Request headers.
        :param cookies: Request cookies.
        :param timeout: Timeout in seconds (default 30).
        :param follow_redirects: Redirect policy.
        :param max_redirects: Max redirects (default 30).
        :param retries: Retry attempts (default 3).
        :param retry_delay: Seconds between retries (default 1).
        :param proxy: Proxy URL.
        :param proxy_auth: Proxy auth dict.
        :param auth: HTTP basic auth dict.
        :param verify: Verify HTTPS certificates (default True).
        :param http3: Use HTTP/3 (default False).
        :param stealthy_headers: Generate real browser headers (default True).
        """
        # Validate all URLs
        urls = [validate_url(u) for u in urls]
        if len(urls) > MAX_BULK_URLS:
            raise ValueError(f"Too many URLs ({len(urls)}). Maximum is {MAX_BULK_URLS} per call.")
        validate_css_selector(css_selector)
        validate_proxy(proxy)

        normalized_proxy_auth = _normalize_credentials(proxy_auth)
        normalized_auth = _normalize_credentials(auth)
        use_tf = use_trafilatura and extraction_type in ("markdown", "text", "article", "structured")

        s = _get_scrapling()
        async with s.FetcherSession() as session:
            timed_tasks = [
                _timed(session.get(
                    url, auth=normalized_auth, proxy=proxy, http3=http3, verify=verify,
                    params=params, headers=headers, cookies=cookies, timeout=timeout,
                    retries=retries, proxy_auth=normalized_proxy_auth, retry_delay=retry_delay,
                    impersonate=impersonate, max_redirects=max_redirects,
                    follow_redirects=follow_redirects, stealthy_headers=stealthy_headers,
                ))
                for url in urls
            ]
            timed_responses = await gather(*timed_tasks, return_exceptions=True)
            results = []
            for i, resp in enumerate(timed_responses):
                if isinstance(resp, BaseException):
                    results.append(ResponseModel(
                        url=urls[i], status=0,
                        content=[f"[Fetch error: {redact_api_key(str(resp)[:200])}]"],
                        fetcher_used="http", error=redact_api_key(str(resp)[:200]),
                    ))
                else:
                    page, elapsed = resp
                    results.append(_annotate_quality(
                            _translate_response(
                                page, extraction_type, css_selector, main_content_only, use_tf, "http", elapsed,
                            )
                        ))
            successful = sum(1 for r in results if r.status < 400 and not r.error)
            return BulkResponseModel(results=results, total=len(results), successful=successful)

    # ─── Dynamic Fetcher (Playwright) ──────────────────────────────

    async def fetch(
        self,
        url: str,
        extraction_type: ExtendedExtractionType = "markdown",
        css_selector: Optional[str] = None,
        main_content_only: bool = True,
        use_trafilatura: bool = True,
        headless: bool = True,
        google_search: bool = True,
        real_chrome: bool = False,
        wait: int | float = 0,
        proxy: Optional[str | Dict[str, str]] = None,
        timezone_id: str | None = None,
        locale: str | None = None,
        extra_headers: Optional[Dict[str, str]] = None,
        useragent: Optional[str] = None,
        cdp_url: Optional[str] = None,
        timeout: int | float = 30000,
        disable_resources: bool = False,
        wait_selector: Optional[str] = None,
        cookies: Sequence[SetCookieParam] | None = None,
        network_idle: bool = False,
        wait_selector_state: SelectorWaitStates = "attached",
        session_id: Optional[str] = None,
    ) -> ResponseModel:
        """Dynamic content via Playwright browser. Handles JS-rendered pages, low-mid protection.
        For high protection / Cloudflare, use stealthy_fetch or smart_fetch instead.

        :param url: The URL to fetch.
        :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
        :param css_selector: CSS selector to narrow content.
        :param main_content_only: Strip nav/ads/footers (default True).
        :param use_trafilatura: Use Trafilatura for article extraction (default True).
        :param headless: Run browser in headless mode (default True).
        :param google_search: Set Google referer header (default True).
        :param real_chrome: Use installed Chrome instead of Chromium.
        :param wait: Milliseconds to wait after page load.
        :param proxy: Proxy to use.
        :param timezone_id: Browser timezone.
        :param locale: Browser locale, e.g., 'en-GB'.
        :param extra_headers: Extra request headers.
        :param useragent: Custom user agent.
        :param cdp_url: Connect via CDP URL.
        :param timeout: Timeout in milliseconds (default 30000).
        :param disable_resources: Drop font/image/media/stylesheet requests.
        :param wait_selector: CSS selector to wait for.
        :param cookies: Cookies to set.
        :param network_idle: Wait for no network connections for 500ms.
        :param wait_selector_state: Selector wait state.
        :param session_id: Reuse existing browser session.
        """
        url = validate_url(url)
        validate_css_selector(css_selector)
        validate_headers(extra_headers)
        validate_proxy(proxy)

        t0 = now()
        bulk = await self.bulk_fetch(
            urls=[url], extraction_type=extraction_type, css_selector=css_selector,
            main_content_only=main_content_only, use_trafilatura=use_trafilatura,
            headless=headless, google_search=google_search, real_chrome=real_chrome,
            wait=wait, proxy=proxy, timezone_id=timezone_id, locale=locale,
            extra_headers=extra_headers, useragent=useragent, cdp_url=cdp_url,
            timeout=timeout, disable_resources=disable_resources,
            wait_selector=wait_selector, cookies=cookies, network_idle=network_idle,
            wait_selector_state=wait_selector_state, session_id=session_id,
        )
        result = bulk.results[0]
        result.duration_ms = (now() - t0) * 1000
        return result

    async def bulk_fetch(
        self,
        urls: List[str],
        extraction_type: ExtendedExtractionType = "markdown",
        css_selector: Optional[str] = None,
        main_content_only: bool = True,
        use_trafilatura: bool = True,
        headless: bool = True,
        google_search: bool = True,
        real_chrome: bool = False,
        wait: int | float = 0,
        proxy: Optional[str | Dict[str, str]] = None,
        timezone_id: str | None = None,
        locale: str | None = None,
        extra_headers: Optional[Dict[str, str]] = None,
        useragent: Optional[str] = None,
        cdp_url: Optional[str] = None,
        timeout: int | float = 30000,
        disable_resources: bool = False,
        wait_selector: Optional[str] = None,
        cookies: Sequence[SetCookieParam] | None = None,
        network_idle: bool = False,
        wait_selector_state: SelectorWaitStates = "attached",
        session_id: Optional[str] = None,
    ) -> BulkResponseModel:
        """Async parallel dynamic fetch via Playwright. Handles JS-rendered pages.

        :param urls: List of URLs to fetch.
        :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
        :param css_selector: CSS selector to narrow content.
        :param main_content_only: Strip nav/ads/footers (default True).
        :param use_trafilatura: Use Trafilatura for article extraction (default True).
        :param headless: Run browser in headless mode (default True).
        :param google_search: Set Google referer header (default True).
        :param real_chrome: Use installed Chrome instead of Chromium.
        :param wait: Milliseconds to wait after page load.
        :param proxy: Proxy to use.
        :param timezone_id: Browser timezone.
        :param locale: Browser locale.
        :param extra_headers: Extra request headers.
        :param useragent: Custom user agent.
        :param cdp_url: Connect via CDP URL.
        :param timeout: Timeout in milliseconds (default 30000).
        :param disable_resources: Drop unnecessary resource requests.
        :param wait_selector: CSS selector to wait for.
        :param cookies: Cookies to set.
        :param network_idle: Wait for no network connections for 500ms.
        :param wait_selector_state: Selector wait state.
        :param session_id: Reuse existing browser session.
        """
        urls = [validate_url(u) for u in urls]
        if len(urls) > MAX_BULK_URLS:
            raise ValueError(f"Too many URLs ({len(urls)}). Maximum is {MAX_BULK_URLS} per call.")
        validate_css_selector(css_selector)
        validate_headers(extra_headers)
        validate_proxy(proxy)
        validate_css_selector(wait_selector)

        use_tf = use_trafilatura and extraction_type in ("markdown", "text", "article", "structured")

        if session_id:
            entry = await self._get_session(session_id, "dynamic")
            timed_tasks = [
                _timed(entry.session.fetch(
                    url, wait=wait, timeout=timeout, google_search=google_search,
                    extra_headers=extra_headers, disable_resources=disable_resources,
                    wait_selector=wait_selector, wait_selector_state=wait_selector_state,
                    network_idle=network_idle, proxy=proxy,
                ))
                for url in urls
            ]
            timed_responses = await gather(*timed_tasks, return_exceptions=True)
        else:
            s = _get_scrapling()
            async with s.AsyncDynamicSession(
                wait=wait, proxy=proxy, locale=locale, timeout=timeout,
                cookies=cookies, cdp_url=cdp_url, headless=headless,
                block_ads=True, max_pages=len(urls), useragent=useragent,
                timezone_id=timezone_id, real_chrome=real_chrome,
                network_idle=network_idle, wait_selector=wait_selector,
                google_search=google_search, extra_headers=extra_headers,
                disable_resources=disable_resources,
                wait_selector_state=wait_selector_state,
            ) as session:
                timed_tasks = [_timed(session.fetch(url)) for url in urls]
                timed_responses = await gather(*timed_tasks, return_exceptions=True)

        results = []
        for i, resp in enumerate(timed_responses):
            if isinstance(resp, BaseException):
                results.append(ResponseModel(
                    url=urls[i], status=0,
                    content=[f"[Fetch error: {redact_api_key(str(resp)[:200])}]"],
                    fetcher_used="dynamic", error=redact_api_key(str(resp)[:200]),
                ))
            else:
                page, elapsed = resp
                results.append(_annotate_quality(
                        _translate_response(
                            page, extraction_type, css_selector, main_content_only, use_tf, "dynamic", elapsed,
                        )
                    ))
        successful = sum(1 for r in results if r.status < 400 and not r.error)
        return BulkResponseModel(results=results, total=len(results), successful=successful)

    # ─── Stealthy Fetcher (Patchright) ─────────────────────────────

    async def stealthy_fetch(
        self,
        url: str,
        extraction_type: ExtendedExtractionType = "markdown",
        css_selector: Optional[str] = None,
        main_content_only: bool = True,
        use_trafilatura: bool = True,
        headless: bool = True,
        google_search: bool = True,
        real_chrome: bool = False,
        wait: int | float = 0,
        proxy: Optional[str | Dict[str, str]] = None,
        timezone_id: str | None = None,
        locale: str | None = None,
        extra_headers: Optional[Dict[str, str]] = None,
        useragent: Optional[str] = None,
        hide_canvas: bool = False,
        cdp_url: Optional[str] = None,
        timeout: int | float = 30000,
        disable_resources: bool = False,
        wait_selector: Optional[str] = None,
        cookies: Sequence[SetCookieParam] | None = None,
        network_idle: bool = False,
        wait_selector_state: SelectorWaitStates = "attached",
        block_webrtc: bool = False,
        allow_webgl: bool = True,
        solve_cloudflare: bool = False,
        additional_args: Optional[Dict] = None,
        session_id: Optional[str] = None,
        page_action=None,
    ) -> ResponseModel:
        """Stealthy fetcher with anti-bot bypass via Patchright (rebrowser-playwright fork).

        Uses browser fingerprint randomization to evade detection by:
        - Cloudflare embedded challenge pages (not Turnstile CAPTCHA)
        - Basic bot-detection scripts that check navigator/webdriver properties

        Does NOT bypass:
        - Cloudflare Turnstile (interactive CAPTCHA widget — requires human)
        - DataDome (behavioral analysis — detects headless browsers via timing)
        - Akamai Bot Manager (advanced fingerprinting beyond Patchright's scope)

        For the 3-tier auto-escalation that tries HTTP→dynamic→stealthy, use smart_fetch instead.

        :param url: The URL to fetch.
        :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
        :param css_selector: CSS selector to narrow content.
        :param main_content_only: Strip nav/ads/footers (default True).
        :param use_trafilatura: Use Trafilatura for article extraction (default True).
        :param headless: Run browser in headless mode (default True).
        :param solve_cloudflare: Auto-solve Cloudflare embedded challenges.
        :param block_webrtc: Prevent IP leak via WebRTC.
        :param hide_canvas: Random canvas noise.
        :param allow_webgl: Keep WebGL enabled (default True; WAFs check for it).
        :param real_chrome: Use installed Chrome.
        :param wait: Milliseconds to wait after page load.
        :param proxy: Proxy to use.
        :param timezone_id: Browser timezone.
        :param locale: Browser locale.
        :param extra_headers: Extra request headers.
        :param useragent: Custom user agent.
        :param cdp_url: Connect via CDP URL.
        :param timeout: Timeout in milliseconds (default 30000).
        :param disable_resources: Drop unnecessary resource requests.
        :param wait_selector: CSS selector to wait for.
        :param cookies: Cookies to set.
        :param network_idle: Wait for no network connections for 500ms.
        :param wait_selector_state: Selector wait state.
        :param additional_args: Extra Playwright context args.
        :param session_id: Reuse existing browser session.
        """
        url = validate_url(url)
        validate_css_selector(css_selector)
        validate_headers(extra_headers)
        validate_proxy(proxy)

        t0 = now()
        bulk = await self.bulk_stealthy_fetch(
            urls=[url], extraction_type=extraction_type, css_selector=css_selector,
            main_content_only=main_content_only, use_trafilatura=use_trafilatura,
            headless=headless, google_search=google_search, real_chrome=real_chrome,
            wait=wait, proxy=proxy, timezone_id=timezone_id, locale=locale,
            extra_headers=extra_headers, useragent=useragent, hide_canvas=hide_canvas,
            cdp_url=cdp_url, timeout=timeout, disable_resources=disable_resources,
            wait_selector=wait_selector, cookies=cookies, network_idle=network_idle,
            wait_selector_state=wait_selector_state, block_webrtc=block_webrtc,
            allow_webgl=allow_webgl, solve_cloudflare=solve_cloudflare,
            additional_args=additional_args, session_id=session_id,
            page_action=page_action,
        )
        result = bulk.results[0]
        result.duration_ms = (now() - t0) * 1000
        return result

    async def bulk_stealthy_fetch(
        self,
        urls: List[str],
        extraction_type: ExtendedExtractionType = "markdown",
        css_selector: Optional[str] = None,
        main_content_only: bool = True,
        use_trafilatura: bool = True,
        headless: bool = True,
        google_search: bool = True,
        real_chrome: bool = False,
        wait: int | float = 0,
        proxy: Optional[str | Dict[str, str]] = None,
        timezone_id: str | None = None,
        locale: str | None = None,
        extra_headers: Optional[Dict[str, str]] = None,
        useragent: Optional[str] = None,
        hide_canvas: bool = False,
        cdp_url: Optional[str] = None,
        timeout: int | float = 30000,
        disable_resources: bool = False,
        wait_selector: Optional[str] = None,
        cookies: Sequence[SetCookieParam] | None = None,
        network_idle: bool = False,
        wait_selector_state: SelectorWaitStates = "attached",
        block_webrtc: bool = False,
        allow_webgl: bool = True,
        solve_cloudflare: bool = False,
        additional_args: Optional[Dict] = None,
        session_id: Optional[str] = None,
        page_action=None,
    ) -> BulkResponseModel:
        """Async parallel stealthy fetch with browser fingerprint randomization.

        :param urls: List of URLs to fetch.
        :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
        :param css_selector: CSS selector to narrow content.
        :param main_content_only: Strip nav/ads/footers (default True).
        :param use_trafilatura: Use Trafilatura for article extraction (default True).
        :param headless: Run browser in headless mode (default True).
        :param solve_cloudflare: Auto-solve Cloudflare challenges.
        :param block_webrtc: Prevent IP leak via WebRTC.
        :param hide_canvas: Random canvas noise.
        :param allow_webgl: Keep WebGL enabled (default True).
        :param real_chrome: Use installed Chrome.
        :param wait: Milliseconds to wait after page load.
        :param proxy: Proxy to use.
        :param timezone_id: Browser timezone.
        :param locale: Browser locale.
        :param extra_headers: Extra request headers.
        :param useragent: Custom user agent.
        :param cdp_url: Connect via CDP URL.
        :param timeout: Timeout in milliseconds (default 30000).
        :param disable_resources: Drop unnecessary resource requests.
        :param wait_selector: CSS selector to wait for.
        :param cookies: Cookies to set.
        :param network_idle: Wait for no network connections for 500ms.
        :param wait_selector_state: Selector wait state.
        :param additional_args: Extra Playwright context args.
        :param session_id: Reuse existing browser session.
        """
        urls = [validate_url(u) for u in urls]
        if len(urls) > MAX_BULK_URLS:
            raise ValueError(f"Too many URLs ({len(urls)}). Maximum is {MAX_BULK_URLS} per call.")
        validate_css_selector(css_selector)
        validate_headers(extra_headers)
        validate_proxy(proxy)
        validate_css_selector(wait_selector)

        use_tf = use_trafilatura and extraction_type in ("markdown", "text", "article", "structured")

        if session_id:
            entry = await self._get_session(session_id, "stealthy")
            timed_tasks = [
                _timed(entry.session.fetch(
                    url, wait=wait, timeout=timeout, google_search=google_search,
                    extra_headers=extra_headers, disable_resources=disable_resources,
                    wait_selector=wait_selector, wait_selector_state=wait_selector_state,
                    network_idle=network_idle, proxy=proxy, solve_cloudflare=solve_cloudflare,
                    page_action=page_action,
                ))
                for url in urls
            ]
            timed_responses = await gather(*timed_tasks, return_exceptions=True)
        else:
            s = _get_scrapling()
            async with s.AsyncStealthySession(
                wait=wait, proxy=proxy, locale=locale, cdp_url=cdp_url,
                timeout=timeout, cookies=cookies, headless=headless,
                block_ads=True, useragent=useragent, timezone_id=timezone_id,
                real_chrome=real_chrome, hide_canvas=hide_canvas,
                allow_webgl=allow_webgl, network_idle=network_idle,
                block_webrtc=block_webrtc, wait_selector=wait_selector,
                google_search=google_search, extra_headers=extra_headers,
                additional_args=additional_args, solve_cloudflare=solve_cloudflare,
                disable_resources=disable_resources,
                wait_selector_state=wait_selector_state,
            ) as session:
                timed_tasks = [_timed(session.fetch(url, page_action=page_action)) for url in urls]
                timed_responses = await gather(*timed_tasks, return_exceptions=True)

        results = []
        for i, resp in enumerate(timed_responses):
            if isinstance(resp, BaseException):
                results.append(ResponseModel(
                    url=urls[i], status=0,
                    content=[f"[Fetch error: {redact_api_key(str(resp)[:200])}]"],
                    fetcher_used="stealthy", error=redact_api_key(str(resp)[:200]),
                ))
            else:
                page, elapsed = resp
                results.append(_annotate_quality(
                        _translate_response(
                            page, extraction_type, css_selector, main_content_only, use_tf, "stealthy", elapsed,
                        )
                    ))
        successful = sum(1 for r in results if r.status < 400 and not r.error)
        return BulkResponseModel(results=results, total=len(results), successful=successful)

    # ─── SMART FETCH (The One Tool To Rule Them All) ────────────────

    async def _http_with_retry(self, url: str, **kwargs) -> ResponseModel:
        """HTTP fetch with retry logic for transient network failures.

        Does NOT retry on validation errors (SecurityError/ValueError) — those
        are deterministic (bad URL, oversized response, blocked scheme) and
        retrying just re-downloads the same failure. Only network/transport
        errors are retried with exponential backoff.
        """
        max_retries = 3
        base_delay = 1.0
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                return await self.get(url, **kwargs)
            except (SecurityError, ValueError):
                # Deterministic failure — surface immediately, no retry.
                raise
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        f"HTTP fetch attempt {attempt + 1} failed for {url}: "
                        f"{redact_api_key(str(e)[:200])}. Retrying in {delay:.0f}s..."
                    )
                    await asyncio_sleep(delay)
                else:
                    logger.error(
                        f"HTTP fetch failed after {max_retries + 1} attempts for "
                        f"{url}: {redact_api_key(str(e)[:200])}"
                    )
        return ResponseModel(
            url=url,
            content=[
                f"[Network error] Failed to fetch {url} after {max_retries + 1} "
                f"attempts.\n"
                f"Error: {redact_api_key(str(last_error)[:500])}\n"
                f"\n"
                f"Tips:\n"
                f"- Check that the URL is publicly accessible.\n"
                f"- If the site requires JavaScript, smart_fetch will auto-escalate to a browser.\n"
                f"- If behind Cloudflare, smart_fetch will try stealthy mode with the Cloudflare solver."
            ],
            status=0, fetcher_used="none", cached=False,
            extracted_type=kwargs.get("extraction_type", "markdown"),
            session_id="", duration_ms=0,
            error=redact_api_key(str(last_error)[:200]),
            retry_count=max_retries + 1,
        )

    async def smart_fetch(
        self,
        url: Annotated[str, Field(description="Single URL to fetch.")],
        urls: Annotated[Optional[List[str]], Field(description="Multiple URLs to fetch in parallel. Returns bulk results. Use instead of calling smart_fetch multiple times.")] = None,
        extraction_type: Annotated[ExtendedExtractionType, Field(description="Content format: 'markdown' (default), 'html', 'text', 'article', 'structured'.")] = "markdown",
        css_selector: Annotated[Optional[str], Field(description="CSS selector to narrow extracted content (e.g. 'article', '.main-content').")] = None,
        main_content_only: Annotated[bool, Field(description="Strip nav, ads, footers (default True).")] = True,
        use_trafilatura: Annotated[bool, Field(description="Use Trafilatura for cleaner article extraction (default True).")] = True,
        cache_ttl: Annotated[int, Field(description="Cache duration in seconds. Default 3600 (1 hour). Set 0 to skip cache and force a fresh fetch.")] = DEFAULT_TTL,
        force_fetcher: Annotated[Optional[Literal["http", "dynamic", "stealthy"]], Field(description="Lock to one fetcher tier. 'http' = fast HTTP-only, 'dynamic' = Playwright JS rendering, 'stealthy' = Cloudflare bypass. Skips auto-escalation.")] = None,
        respect_robots: Annotated[bool, Field(description="Check robots.txt before fetching (default False).")] = False,
        headless: Annotated[bool, Field(description="Run browser without visible window (default True).")] = True,
        real_chrome: Annotated[bool, Field(description="Use installed Chrome instead of bundled browser.")] = False,
        wait: Annotated[int | float, Field(description="Extra milliseconds to wait after page load for JS rendering.")] = 0,
        proxy: Annotated[Optional[str | Dict[str, str]], Field(description="Proxy URL or dict with server/username/password.")] = None,
        timeout: Annotated[int | float, Field(description="Max request time in milliseconds (default 30000).")] = 30000,
        network_idle: Annotated[bool, Field(description="Wait until network is idle for 500ms before capturing (good for SPAs).")] = False,
        solve_cloudflare: Annotated[bool, Field(description="Attempt Cloudflare bypass in stealthy mode (default True).")] = True,
        block_webrtc: Annotated[bool, Field(description="Prevent WebRTC IP leak in stealthy mode (default True).")] = True,
        hide_canvas: Annotated[bool, Field(description="Randomize canvas fingerprint in stealthy mode (default True).")] = True,
        extra_headers: Annotated[Optional[Dict[str, str]], Field(description="Additional HTTP headers as {name: value} dict.")] = None,
        useragent: Annotated[Optional[str], Field(description="Override browser user agent string.")] = None,
        cookies: Annotated[Sequence[SetCookieParam] | None, Field(description="Cookies as list of {name, value, domain} dicts.")] = None,
        offset: Annotated[int, Field(description="Resume from this character offset when content was truncated. The response tells you the next offset to use.")] = 0,
        max_content_chars: Annotated[Optional[int], Field(description="Max chars of extracted content to return (default 40000). Lower this to save context tokens on big pages; the rest is paginated via offset/next_offset.")] = None,
        pages: Annotated[Optional[str], Field(description="PDF only: page spec like '1-5' or '1,3,5-7' to extract a subset of pages (saves tokens/time on big PDFs). None = all pages.")] = None,
        password: Annotated[Optional[str], Field(description="PDF only: password for an encrypted PDF.")] = None,
        focus: Annotated[Optional[str], Field(description="Query-focused extraction: pass a query and only the BM25-relevant blocks (paragraphs/headings/tables) are returned, saving context on long pages. Works post-cache, so it never triggers a re-fetch. Re-pass the same focus when paginating with offset. Empty = full page.")] = None,
        actions: Annotated[Optional[List[Dict[str, Any]]], Field(description="Page interactions run on the stealthy browser AFTER load, BEFORE extraction: [{click:'button.load-more'}, {fill:{selector:'#q', text:'x'}}, {press:'Enter'}, {wait:500}, {scroll:3}, {wait_selector:'.item'}]. Forces the stealthy tier; bypasses cache. Reaches content behind a click/form/infinite scroll.")] = None,
        include_media: Annotated[bool, Field(description="If true, populate the response .media field with up to 20 image URLs found on the page (for multimodal agents). Default false (keeps responses lean).")] = False,
        include_links: Annotated[bool, Field(description="If true, populate the response .links field with the page's outgoing links classified as citations/navigation/external + a primary_source hint. Default false. Use when you want to follow a page's referenced sources in one step.")] = False,
        archive_fallback: Annotated[bool, Field(description="If true (default), when the live site hard-blocks the fetch (404/451/500/bot-block/auth), recover the page from the Internet Archive's closest snapshot, honestly marked with source='archive.org' + archived_at. Set false to get the raw live failure instead.")] = True,
    ) -> ResponseModel:
        """Fetch a URL (or multiple URLs) with automatic anti-bot escalation.

        Use this for ALL web page fetching. It auto-selects the best method:
        HTTP (fast, curl_cffi) → Dynamic (Playwright, JS rendering) → Stealthy (Cloudflare bypass).

        When to use:
        - Fetching any web page for content extraction
        - Sites that might have anti-bot protection (Cloudflare embedded challenges, JS-required pages)
        - Fetching multiple URLs at once (use urls parameter)
        - When you don't know which fetcher to use. This tool decides for you.

        When NOT to use:
        - Taking screenshots: use the screenshot tool instead
        - Web search: use smart_search instead
        - You specifically need HTTP-only without escalation: set force_fetcher="http"

        Response: url, status, content (extracted text), content_type, total_size_bytes,
        is_truncated (+ next_offset to paginate), escalation_path, duration_ms, error.
        Signals to branch on: content_ok (real content, not a login/bot wall?), next_action
        (suggested next call), summary, page_type (article/docs/list/forum/auth_wall/paywall/...),
        content_age_days + is_stale, source_type + is_official, source + archived_at. If the
        live site hard-blocks (404/bot-wall/auth), content is auto-recovered from the Internet
        Archive (source='archive.org', archived_at = snapshot date); archive_fallback=false
        to opt out and get the raw live failure instead.
        """
        # Bulk mode: fetch multiple URLs in parallel
        if urls is not None:
            if actions:
                raise ValueError("actions are not supported in bulk mode; call smart_fetch once per URL")
            return await self._smart_fetch_bulk(
                urls, extraction_type, css_selector, main_content_only,
                use_trafilatura, cache_ttl, force_fetcher, respect_robots,
                headless, real_chrome, wait, proxy, timeout, network_idle,
                solve_cloudflare, block_webrtc, hide_canvas, extra_headers,
                useragent, cookies, max_content_chars, include_media, include_links,
            )

        # Validate all inputs
        url, css_selector, extra_headers, timeout, proxy, useragent = \
            self._validate_smart_fetch_params(
                url, extraction_type, css_selector, extra_headers, timeout, proxy, useragent,
            )

        # max_content_chars: token-spend control. Lower = less context per call,
        # the rest is paginated via offset/next_offset.
        if max_content_chars is not None:
            if isinstance(max_content_chars, bool) or not isinstance(max_content_chars, int) \
                    or max_content_chars < 500:
                raise ValueError("max_content_chars must be an int >= 500")
            max_content_chars = min(max_content_chars, 200000)
        mc = max_content_chars if isinstance(max_content_chars, int) else MAX_CONTENT_CHARS

        # PDF options flow down to _translate_response via contextvars (task-local,
        # safe under concurrent bulk fetches) and into the cache key so a
        # pages-subset extraction doesn't collide with a full-PDF cache entry.
        _PDF_PAGES.set(pages if isinstance(pages, str) else None)
        _PDF_PASSWORD.set(password if isinstance(password, str) else None)
        # focus: set only when provided (truthy) so bulk-mode inner calls inherit
        # the parent's focus via the gather context copy instead of resetting it.
        if isinstance(focus, str) and focus.strip():
            _FOCUS.set(focus)
        _INCLUDE_MEDIA.set(bool(include_media))
        _INCLUDE_LINKS.set(bool(include_links))
        _ARCHIVE_FALLBACK.set(bool(archive_fallback))
        # actions produce post-interaction content unique to the action sequence;
        # bypass the cache so a plain (pre-action) cached copy is never served.
        if actions:
            cache_ttl = 0

        # 1. Check robots.txt compliance
        if respect_robots and not await is_allowed(url):
            disallowed = ResponseModel(
                url=url,
                content=[
                    f"[Blocked by robots.txt] The URL '{url}' is disallowed by the "
                    f"site's robots.txt policy. Set respect_robots=False to bypass."
                ],
                status=403, fetcher_used="none", cached=False,
                extracted_type=extraction_type, session_id="",
                duration_ms=0, error="robots_txt_disallowed",
            )
            return _apply_chunking(disallowed, max_chars=mc)

        # 2. Check cache
        if cache_ttl > 0:
            cached = await get_cached(url, extraction_type, css_selector, ttl=cache_ttl, pages=pages if isinstance(pages, str) else None)
            if cached is not None:
                env = cached.get("envelope") or {}
                return _apply_chunking(ResponseModel(
                    url=cached["url"], status=cached["status"], content=cached["content"],
                    cached=True, fetcher_used="cache", duration_ms=0,
                    extracted_type=extraction_type,
                    content_type=cached.get("content_type", ""),
                    total_size_bytes=cached.get("total_size_bytes", 0),
                    # v10: restore the envelope so cache hits keep metadata/links/
                    # quality_score/toc/page_type/source/archived_at (previously lost).
                    metadata=env.get("metadata", {}) or {},
                    media=env.get("media", []) or [],
                    links=env.get("links", {}) or {},
                    quality_score=env.get("quality_score", 0.0) or 0.0,
                    table_of_contents=env.get("table_of_contents", []) or [],
                    page_type=env.get("page_type", "unknown") or "unknown",
                    source=env.get("source", "live") or "live",
                    archived_at=env.get("archived_at", "") or "",
                ), max_chars=mc, offset=offset)

        # 3. Reddit optimization: rewrite listings to old.reddit.com (7x smaller,
        #    2x faster). Done BEFORE force_fetcher so even an explicit
        #    force_fetcher="http" benefits from the old.reddit.com rewrite.
        #    Post pages (/comments/...) stay on www.reddit.com (old.reddit.com
        #    shows the sidebar instead of full comments) — handled inside
        #    rewrite_to_old_reddit.
        is_reddit = is_reddit_url(url)
        if is_reddit:
            url = rewrite_to_old_reddit(url)

        # 3.5. actions: page interactions (click/fill/press/wait/scroll) require
        # the stealthy browser tier. Force it, bypass cache (post-action content
        # is unique to the action sequence), and pass a page_action callable.
        if actions:
            if force_fetcher == "http":
                raise ValueError("actions require the browser tier; use force_fetcher='stealthy' or omit it")
            from master_fetch.actions import build_page_action
            page_action = build_page_action(actions)  # validates; raises on bad input
            if page_action is None:
                raise ValueError("actions must be a non-empty list of action dicts")
            return await self._force_fetch(
                url, "stealthy", extraction_type, css_selector, main_content_only,
                use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
                proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
                hide_canvas, extra_headers, useragent, cookies, mc,
                page_action=page_action,
            )

        # 4. Force specific fetcher (explicit pin wins; uses rewritten url)
        if force_fetcher:
            return await self._force_fetch(
                url, force_fetcher, extraction_type, css_selector, main_content_only,
                use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
                proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
                hide_canvas, extra_headers, useragent, cookies, mc,
            )

        # 5. Reddit default: skip HTTP, go straight to stealthy. www.reddit.com
        #    JS-walls/blocks plain HTTP ~100% of the time, so the HTTP tier is
        #    ~1s of wasted time before it escalates anyway. old.reddit.com
        #    listings render fine in the stealthy browser. Saves ~1s per fetch.
        #    (An explicit force_fetcher above already returned, so this only
        #    applies to the unpinned/default case.)
        if is_reddit:
            return await self._force_fetch(
                url, "stealthy", extraction_type, css_selector, main_content_only,
                use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
                proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
                hide_canvas, extra_headers, useragent, cookies, mc,
            )

        # 6. Auto-escalation (HTTP -> stealthy) for everything else
        return await self._auto_escalate(
            url, extraction_type, css_selector, main_content_only,
            use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
            proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
            hide_canvas, extra_headers, useragent, cookies, mc,
        )

    async def _smart_fetch_bulk(
        self, urls, extraction_type, css_selector, main_content_only,
        use_trafilatura, cache_ttl, force_fetcher, respect_robots,
        headless, real_chrome, wait, proxy, timeout, network_idle,
        solve_cloudflare, block_webrtc, hide_canvas, extra_headers,
        useragent, cookies, max_chars: int = MAX_CONTENT_CHARS,
        include_media: bool = False, include_links: bool = False,
    ) -> BulkResponseModel:
        """Fetch multiple URLs in parallel through the smart fetch pipeline."""
        if len(urls) > MAX_BULK_URLS:
            raise ValueError(
                f"Too many URLs ({len(urls)}). Maximum is {MAX_BULK_URLS} per call."
            )

        async def _fetch_one(u: str) -> ResponseModel:
            try:
                return await self.smart_fetch(
                    url=u, extraction_type=extraction_type,
                    css_selector=css_selector, main_content_only=main_content_only,
                    use_trafilatura=use_trafilatura, cache_ttl=cache_ttl,
                    force_fetcher=force_fetcher, respect_robots=respect_robots,
                    headless=headless, real_chrome=real_chrome, wait=wait,
                    proxy=proxy, timeout=timeout, network_idle=network_idle,
                    solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc,
                    hide_canvas=hide_canvas, extra_headers=extra_headers,
                    useragent=useragent, cookies=cookies,
                    max_content_chars=max_chars,
                    include_media=include_media, include_links=include_links,
                )
            except Exception as e:
                return _with_agent_hints(ResponseModel(
                    url=u, status=0, content=[f"[Error: {redact_api_key(str(e)[:200])}]"],
                    fetcher_used="none", error=redact_api_key(str(e)[:200]),
                ))

        # Small delay between URL batches to avoid hammering the same server
        results = []
        batch_size = 10
        for i in range(0, len(urls), batch_size):
            batch = urls[i:i + batch_size]
            batch_results = await gather(*[_fetch_one(u) for u in batch])
            results.extend(batch_results)
            if i + batch_size < len(urls):
                await asyncio_sleep(0.5)

        successful = sum(1 for r in results if r.status > 0 and r.status < 400 and not r.error)
        return BulkResponseModel(results=results, total=len(results), successful=successful)

    async def _force_fetch(
        self, url, force_fetcher, extraction_type, css_selector,
        main_content_only, use_trafilatura, cache_ttl, offset,
        headless, real_chrome, wait, proxy, timeout, network_idle,
        solve_cloudflare, block_webrtc, hide_canvas, extra_headers,
        useragent, cookies, max_chars: int = MAX_CONTENT_CHARS,
        page_action=None,
    ) -> ResponseModel:
        """Execute a forced fetcher tier and finalize the result."""
        # HTTP fetcher takes seconds; browser timeout is ms. Cap at 30s.
        http_timeout = max(1, min(int(timeout / 1000), 30))
        if force_fetcher == "http":
            http_cookies = _safe_cookie_dict(cookies)
            result = await self.get(
                url, extraction_type=extraction_type, css_selector=css_selector,
                main_content_only=main_content_only, use_trafilatura=use_trafilatura,
                proxy=proxy if isinstance(proxy, str) else None,
                headers=extra_headers, cookies=http_cookies, timeout=http_timeout,
                stealthy_headers=True,
            )
            result.escalation_path = "direct:http"
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)

        else:  # stealthy ("dynamic" also routes here — Patchright handles everything)
            ssid = await self._ensure_auto_session("stealthy")
            result = await self.stealthy_fetch(
                url, extraction_type=extraction_type,
                css_selector=css_selector, main_content_only=main_content_only,
                use_trafilatura=use_trafilatura, headless=headless,
                real_chrome=real_chrome, wait=wait, proxy=proxy,
                timeout=timeout, network_idle=network_idle,
                disable_resources=True,
                solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc,
                hide_canvas=hide_canvas, extra_headers=extra_headers,
                useragent=useragent, cookies=cookies,
                session_id=ssid,
                page_action=page_action,
            )
            result.escalation_path = "direct:stealthy"
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)

    async def _auto_escalate(
        self, url, extraction_type, css_selector, main_content_only,
        use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
        proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
        hide_canvas, extra_headers, useragent, cookies, max_chars: int = MAX_CONTENT_CHARS,
    ) -> ResponseModel:
        """Auto-escalation: try HTTP first, fall back to stealthy if it fails.

        Two tiers. No domain intel routing. No dynamic tier.
        HTTP is fast (~1s). Stealthy (Patchright) handles everything else.
        If HTTP succeeds, fire background pre-warm so stealthy is ready
        for the next call that needs it.
        """
        start_time = now()
        errors = []
        http_cookies = _safe_cookie_dict(cookies)
        # HTTP fetcher takes seconds; browser timeout is ms. Cap at 30s.
        http_timeout = max(1, min(int(timeout / 1000), 30))

        # Tier 1: HTTP (always try first — it's fast)
        result = await self._http_with_retry(
            url, extraction_type=extraction_type,
            css_selector=css_selector, main_content_only=main_content_only,
            use_trafilatura=use_trafilatura,
            proxy=proxy if isinstance(proxy, str) else None,
            headers=extra_headers, cookies=http_cookies, stealthy_headers=True,
            timeout=http_timeout,
        )
        elapsed = (now() - start_time) * 1000
        result.duration_ms = elapsed

        # PDF-intent URLs (.pdf) are binary; never escalate to a JS browser
        # (a stealthy render of a PDF URL is always wasted, and the body is
        # either %PDF or a login/error redirect handled in _translate_response).
        if url.lower().split('?')[0].endswith('.pdf'):
            result.escalation_path = "direct:http"
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)

        # Accept if status is OK and content is real (not a JS shell).
        if result.status < 400 and not _is_js_shell(result):
            result.escalation_path = "direct:http"
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)

        # Should we escalate? Stealthy browser can genuinely help for:
        # 1. Status 200 with JS shell -> page needs a real browser
        # 2. Status 403 or 503 -> explicit bot block / bot challenge
        # 3. Status 429 -> rate limited; stealthy has a different fingerprint
        # 4. Status 500/502 -> server error; may be intermittent or bot-related
        # NOT for 401/407 (auth needed, not bot), 404/410 (page gone, stealthy
        # gets the same 404), 451 (legal block), 400 (bad request).
        should_escalate = (
            (result.status < 400 and _is_js_shell(result))
            or result.status in (403, 429, 500, 502, 503)
        )
        if not should_escalate:
            result.duration_ms = elapsed
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)

        # Tier 2: Stealthy
        errors.append(f"HTTP failed (status {result.status})")
        remaining = max(timeout - int((now() - start_time) * 1000), 5000)
        ssid = await self._ensure_auto_session("stealthy")
        result = await self.stealthy_fetch(
            url, extraction_type=extraction_type,
            css_selector=css_selector, main_content_only=main_content_only,
            use_trafilatura=use_trafilatura, headless=headless,
            real_chrome=real_chrome, wait=wait, proxy=proxy,
            timeout=remaining, network_idle=network_idle,
            disable_resources=True,
            solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc,
            hide_canvas=hide_canvas, extra_headers=extra_headers,
            useragent=useragent, cookies=cookies,
            session_id=ssid,
        )
        elapsed = (now() - start_time) * 1000
        result.duration_ms = elapsed

        if result.status < 400 and not _is_js_shell(result):
            result.escalation_path = "http→stealthy"
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)

        # All tiers failed
        errors.append(f"Stealthy failed (status {result.status})")
        result.content = [
            f"[All fetch tiers failed for {url}]\n"
            f"Attempted: HTTP → Stealthy\n"
            f"Failures: {'; '.join(errors)}\n"
            f"Final status: {result.status}\n"
            f"\n"
            f"Tips:\n"
            f"- If the site uses Cloudflare Turnstile or DataDome, no free tool can bypass it.\n"
            f"- Try a different URL on the same domain (some paths have lower protection).\n"
            f"- Set solve_cloudflare=True (already tried).\n"
            f"- Try with a proxy via the proxy parameter."
        ]
        result.escalation_path = "http→stealthy(all_failed)"
        result.retry_count = 2
        result.duration_ms = elapsed
        result.error = f"all_tiers_failed: HTTP status {result.status}"
        return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)

    # ─── Cache Management ──────────────────────────────────────────

    async def cache_clear(self, all: Annotated[bool, Field(description="True=wipe all, False=expired only")] = False) -> CacheInfoModel:
        """Clear expired cache entries, or all entries if 'all' is True.

        :param all: If True, clear ALL cache entries. If False (default), only expired ones.
        """
        if all:
            count = await clear_all_cache()
            return CacheInfoModel(message=f"Cleared all {count} cache entries.", purged=count)
        else:
            count = await clear_cache()
            await clear_robots_cache()
            return CacheInfoModel(
                message=f"Cleared {count} expired cache entries.", purged=count,
            )

    # ─── Version ──────────────────────────────────────────────────

    async def version(self) -> VersionInfoModel:
        """Check installed Hound version and whether an update is available.

        Returns installed version, latest PyPI version, and whether Hound is up to date.
        Call this to check if you should tell the user to run: hound -u
        """
        from master_fetch import updater
        installed, latest, is_current = await asyncio_to_thread(updater.check_version)
        # up_to_date: True if at or ahead of PyPI (no update needed)
        up_to_date = is_current if is_current is not None else True
        if not up_to_date and latest:
            try:
                if updater.pad_version(installed) > updater.pad_version(latest):
                    up_to_date = True
            except (ValueError, IndexError):
                pass
        return VersionInfoModel(
            version=installed,
            latest=latest or "",
            up_to_date=up_to_date,
            update_command="hound -u",
        )

    # ─── Search ────────────────────────────────────────────────────

    async def smart_search(
        self,
        query: str,
        max_results: int = 6,
        cache_ttl: int = 300,
        mode: str = "auto",
        engines: Optional[List[str]] = None,
        url: Optional[str] = None,
        site: Optional[str] = None,
        exclude_sites: Optional[List[str]] = None,
        location: Optional[str] = None,
        language: Optional[str] = None,
        region: Optional[str] = None,
        page: int = 0,
        freshness: Optional[str] = None,
    ) -> SearchResponseModel:
        """Local keyless web search (no API key, no account, no third-party service).

        Runs 9 keyless backends in parallel (duckduckgo, brave, mojeek, yahoo,
        yandex, startpage, google + opt-in wikipedia, grokipedia; engines= to
        choose), merges + dedups + ranks by neural relevance + cross-backend
        consensus (a URL returned by several independent indexes is an authority
        signal). Returns URLs + ranking, not page content - smart_fetch the
        results you want. Each result has
        relevance_score + fetch_relevance + engines_consensus. Filters:
        site/exclude_sites (domain include/exclude on the final URL),
        location/language/region (geo), page (0-10), freshness
        (day|week|month|year). Results cached 5min.
        """
        from master_fetch.search import SearchResponseModel  # lazy: search.py pulls the metasearch engine chain
        try:
            query = validate_search_query(query)
        except SecurityError as e:
            return SearchResponseModel(
                query=query, results=[], total_results=0,
                duration_ms=0, error=str(e),
            )

        try:
            from master_fetch.search import smart_search as _smart_search
            return await _smart_search(
                self, query, max_results, cache_ttl,
                mode=mode, engines=engines, url=url,
                site=site, exclude_sites=exclude_sites,
                location=location, language=language, region=region,
                page=page, freshness=freshness,
            )
        except Exception as e:
            return SearchResponseModel(
                query=query, results=[], total_results=0,
                error=redact_api_key(str(e)[:200]),
            )

    # ─── Crawl ─────────────────────────────────────────────────────

    async def smart_crawl(
        self,
        url: str,
        max_pages: int = 10,
        max_depth: int = 2,
        path_include: Optional[List[str]] = None,
        path_exclude: Optional[List[str]] = None,
        discover_only: bool = False,
        focus: Optional[str] = None,
        crawl_urls: Optional[List[str]] = None,
        max_content_chars_per: int = 8000,
        max_total_chars: Optional[int] = None,
        concurrency: int = 3,
        cache_ttl: int = DEFAULT_TTL,
        respect_robots: bool = False,
        force_fetcher: Optional[str] = None,
        timeout: int = 30000,
        deadline_ms: int = 120000,
        sitemap: str | bool = False,
    ) -> "CrawlResponseModel":
        """Deep-crawl a site: best-first same-domain from `url`, returning each
        page as markdown with content_ok/summary/page_type. discover_only=true
        returns the URL map only. `focus` prioritizes relevant pages AND
        focus-filters each page's content. crawl_urls=[...] fetches a chosen
        subset (second-phase selective crawl, no re-discovery). Content-adaptive:
        article pages -> main content, list/index pages -> structured link list,
        JS shells -> detected + reported honestly. Caps: max_pages, max_depth,
        max_total_chars (token budget), deadline_ms (overall time). Reuses
        smart_fetch anti-bot escalation + cache.
        """
        try:
            from master_fetch.crawl import smart_crawl as _smart_crawl, CrawlResponseModel as _CRM
            return await _smart_crawl(
                self, url, max_pages=max_pages, max_depth=max_depth,
                path_include=path_include, path_exclude=path_exclude,
                discover_only=discover_only, focus=focus, crawl_urls=crawl_urls,
                max_content_chars_per=max_content_chars_per,
                max_total_chars=max_total_chars, concurrency=concurrency,
                cache_ttl=cache_ttl, respect_robots=respect_robots,
                force_fetcher=force_fetcher, timeout=timeout,
                deadline_ms=deadline_ms, sitemap=sitemap,
            )
        except Exception as e:
            from master_fetch.crawl import CrawlResponseModel as _CRM
            return _CRM(start_url=url, pages=[], error=redact_api_key(str(e)[:200]))

    # ─── Serve ─────────────────────────────────────────────────────

    # Minimal hand-crafted tool definitions — no Pydantic schema bloat.
    # Saves ~69% tokens vs FastMCP auto-generated schemas.
    _TOOL_DEFS: list[dict] = [
        {
            "name": "mcp_smart_fetch",
            "description": "Fetch a URL (or urls=[...] for parallel bulk). Auto HTTP -> stealthy escalation. Returns extracted text + metadata + signals: content_ok, next_action, summary, page_type, content_age_days/is_stale, source_type/is_official, source/archived_at. Hard-block (404/bot/auth) -> auto-recover from Internet Archive (source=archive.org, archived_at=snapshot date; archive_fallback=false in options to opt out). PDFs -> structured markdown + ToC + page ranges + auto-OCR. Long pages: paginate with offset/next_offset or focus='query' for only relevant blocks. actions=[...] for click/form/scroll. include_links/include_media via options.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "urls": {"type": "array", "items": {"type": "string"}, "description": "Multiple URLs (parallel; returns per-URL results)"},
                    "extraction_type": {"type": "string", "enum": ["markdown", "html", "text", "article", "structured"], "description": "Content format (default markdown). html = raw HTML."},
                    "css_selector": {"type": "string", "description": "CSS selector to narrow extracted content (e.g. 'article', '.main'). Token saver."},
                    "max_content_chars": {"type": "integer", "description": "Max chars of extracted content (default 40000, min 500). Lower = less context; rest paginated via offset/next_offset."},
                    "timeout": {"type": "integer", "description": "Max request time in ms (default 30000)."},
                    "cache_ttl": {"type": "integer", "description": "Cache seconds (default 3600). 0 = force fresh."},
                    "force_fetcher": {"type": "string", "enum": ["http", "stealthy"], "description": "Pin to one tier, skip auto-escalation. 'http' = fast HTTP-only (fails on JS/bot walls). 'stealthy' = anti-detect browser. Default = auto."},
                    "offset": {"type": "integer", "description": "Char offset into extracted text to resume a truncated page. Use next_offset from previous response."},
                    "pages": {"type": "string", "description": "PDF only: page spec like '1-5' or '1,3,5-7'. Use table_of_contents page/end_page ranges to pick. None = all pages."},
                    "password": {"type": "string", "description": "PDF only: password for an encrypted PDF."},
                    "focus": {"type": "string", "description": "Query-focused extraction: only BM25-relevant blocks returned. Context saver on long pages. Post-cache (no re-fetch). Re-pass same focus when paginating."},
                    "actions": {"type": "array", "description": "Page interactions on stealthy browser AFTER load, BEFORE extraction. Forces stealthy + bypasses cache. Each item: {click:'css'}, {fill:{selector:'css',text:'x'}}, {press:'Enter'}, {wait:500}, {scroll:3}, {wait_selector:'css'}. Use for load-more, search forms, pagination, infinite scroll."},
                    "options": {"type": "object", "description": "include_links (bool,false: response.links=citations/navigation/external+primary_source), include_media (bool,false: up to 20 page image URLs), archive_fallback (bool,true: recover from Internet Archive on hard-block; false=raw failure), proxy (str|dict), cookies (list), extra_headers (dict), useragent (str), wait (ms,0), network_idle (bool,SPAs), headless (bool,true), respect_robots (bool,false), real_chrome/solve_cloudflare/block_webrtc/hide_canvas/main_content_only/use_trafilatura (anti-detect tuning, good defaults, rarely needed).", "additionalProperties": True},
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        },
        {
            "name": "mcp_smart_crawl",
            "description": "Deep-crawl a site: best-first same-domain walk, each page as markdown + content_ok + page_type. List pages -> structured link list. sitemap=true (in options) maps whole site from sitemap.xml in one fetch; sitemap='auto' = use if present else BFS. discover_only=true = URL map only. focus='query' prioritizes relevant pages + focus-filters each. crawl_urls=[...] fetches a chosen subset. Caps: max_pages (10), max_depth (2), max_total_chars, deadline_ms. Reuses smart_fetch anti-bot + cache.",
            "inputSchema": {
                "type": "object", "required": ["url"],
                "properties": {
                    "url": {"type": "string", "description": "Start URL (crawl stays on this domain)"},
                    "discover_only": {"type": "boolean", "description": "true = return URL map only, no page content. For big sites prefer options sitemap=true (one-fetch map)."},
                    "focus": {"type": "string", "description": "Query: prioritize crawling links relevant to this + focus-filter each page. Token saver on doc sites."},
                    "crawl_urls": {"type": "array", "items": {"type": "string"}, "description": "Chosen subset of URLs to fetch (second-phase selective crawl, no re-discovery). Use after sitemap=true or discover_only=true."},
                    "options": {"type": "object", "description": "sitemap (true|'auto'|false,false: true=map from sitemap.xml in one fetch; 'auto'=use if present else BFS), max_pages (1-100,10), max_depth (0-5,2), path_include (list of path prefixes), path_exclude (list to skip), max_content_chars_per (8000), max_total_chars (token budget), concurrency (1-5,3), cache_ttl (3600;0=fresh), respect_robots (false), force_fetcher ('http'|'stealthy'), timeout (ms,30000), deadline_ms (120000).", "additionalProperties": True},
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        },
        {
            "name": "mcp_screenshot",
            "description": "Screenshot a URL as an image. Multimodal agents only (content as images/canvas/visual layout). Text agents: use smart_fetch. Stealthy browser auto-managed.",
            "inputSchema": {
                "type": "object", "required": ["url"],
                "properties": {
                    "url": {"type": "string", "description": "URL to screenshot"},
                    "session_id": {"type": "string", "description": "Optional: reuse a specific open browser session. Omit to auto-manage."},
                    "options": {"type": "object", "description": "full_page (bool,false), image_type (png|jpeg,png), quality (0-100,jpeg), wait (ms), wait_selector (css), network_idle (bool), timeout (ms,30000).", "additionalProperties": True},
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        },
        {
            "name": "mcp_smart_search",
            "description": "Keyless web search (no API key). 10 backends in parallel (ddg,brave,mojeek,yahoo,yandex,startpage,google,qwant + opt-in wikipedia,grokipedia), neural-reranked + cross-backend consensus. Returns URLs + ranking, NOT content -> smart_fetch the ones that match. Each result: relevance_score + fetch_relevance (high/med/low) + engines_consensus. related_queries from result titles+snippets. Blocked backends circuit-broken 60s. NEVER answer from snippets alone. Filters in options: site, exclude_sites, location, language, region, page, freshness.",
            "inputSchema": {
                "type": "object", "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "options": {"type": "object", "description": "max_results (1-50,6), cache_ttl (300), mode (auto|neural|find_similar; auto=neural if [all]+model else consensus; find_similar needs url=), engines (list, default: ddg,brave,mojeek,yahoo,yandex,startpage,google,qwant; add 'wikipedia'/'grokipedia'), site (domain restrict), exclude_sites (list), location, language (2-letter), region, page (0-10), freshness (day|week|month|year), url (for find_similar).", "additionalProperties": True},
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        },
        {
            "name": "cache_clear",
            "description": "Clear fetch cache. all=true wipes all (default: expired only). To re-fetch one URL fresh, pass cache_ttl=0 to smart_fetch/smart_crawl instead. Cache stores extracted text per URL+extraction_type+css_selector+pages (+ per query+filters for search); default TTL 1hr.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "all": {"type": "boolean", "description": "Wipe all (default: expired only)"},
                },
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
        },
        {
            "name": "version",
            "description": "Hound version + update status.",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        },
    ]

    def serve(self, http: bool = False, host: str = "127.0.0.1", port: int = 8765):
        """Start the MCP server using low-level Server for minimal token overhead.

        When ``http`` is False (default) the server runs over stdio, which is what
        Claude Code, Cursor, OpenCode, and other local MCP clients expect. When
        True it exposes the **streamable HTTP** transport (MCP 2025-03-26 spec)
        at ``http://host:port/mcp``, which is what Open WebUI (v0.6.31+) and
        other HTTP MCP clients connect to directly, no proxy needed. The legacy
        SSE transport was removed (deprecated in the spec)."""
        from mcp.server import Server
        from mcp.types import Tool, TextContent

        server = Server(name="Hound", version=__version__)
        # Connect-time orientation: clients inject this into the agent context
        # once on initialize (the MCP `instructions` field).
        server.instructions = HOUND_INSTRUCTIONS
        server.website_url = "https://github.com/dondai1234/master-fetch"

        # ── list_tools: return hand-crafted minimal definitions ──────
        @server.list_tools()
        async def list_tools():
            return [Tool(**td) for td in self._TOOL_DEFS]

        # ── call_tool: dispatch to existing methods ─────────────────
        @server.call_tool(validate_input=False)
        async def call_tool(name: str, arguments: dict):
            from mcp.types import CallToolResult
            try:
                result = await self._dispatch(name, arguments)
                # _dispatch returns (content_list, structured_dict) or just content_list
                if isinstance(result, tuple):
                    content_list, structured = result
                    return CallToolResult(content=content_list, structuredContent=structured)
                return CallToolResult(content=result)
            except Exception as e:
                error_text = json.dumps({"error": redact_api_key(str(e)[:300])})
                return CallToolResult(
                    content=[TextContent(type="text", text=error_text)],
                    isError=True,
                )

        if not http:
            import anyio
            from mcp.server.stdio import stdio_server

            async def _run():
                # Warm the single stealthy browser at startup so it's ready before
                # the agent's first stealthy fetch/screenshot. It stays alive until
                # the idle monitor closes it after HOUND_BROWSER_IDLE_TIMEOUT of
                # inactivity (default 300s), then relaunches on the next fetch.
                # Best-effort, runs in the background while the server handles the
                # initialize handshake.
                warm = asyncio.create_task(self._prewarm_stealthy())
                warm_reranker = asyncio.create_task(
                    _safe_imported_prewarm("master_fetch.reranker", "prewarm_reranker")
                )
                try:
                    async with stdio_server() as (read, write):
                        await server.run(read, write, server.create_initialization_options())
                finally:
                    # Bulletproof teardown: cancel prewarm tasks + close sessions,
                    # swallowing EVERYTHING (including 'Event loop is closed' and
                    # BaseException) so the process always exits cleanly. A noisy
                    # teardown traceback must never look like a server crash to the
                    # MCP client (which reports it as 'failed to load').
                    for _t in (warm, warm_reranker):
                        try:
                            _t.cancel()
                        except BaseException:
                            pass
                    for _t in (warm, warm_reranker):
                        try:
                            await _t
                        except BaseException:
                            pass
                    try:
                        await self._shutdown_close_sessions()
                    except BaseException:
                        pass

            anyio.run(_run)
        else:
            # Streamable HTTP transport (MCP 2025-03-26 spec). This is the
            # transport Open WebUI (v0.6.31+) and other modern HTTP MCP clients
            # connect to directly, no mcpo proxy needed. Endpoint: http://host:port/mcp
            from contextlib import asynccontextmanager
            from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
            from starlette.applications import Starlette
            from starlette.routing import Route
            import uvicorn

            manager = StreamableHTTPSessionManager(app=server)

            class _StreamableHTTPASGIApp:
                async def __call__(self, scope, receive, send):
                    await manager.handle_request(scope, receive, send)

            @asynccontextmanager
            async def lifespan(app):
                warm = asyncio.create_task(self._prewarm_stealthy())
                warm_reranker = asyncio.create_task(
                    _safe_imported_prewarm("master_fetch.reranker", "prewarm_reranker")
                )
                try:
                    async with manager.run():
                        yield
                finally:
                    for _t in (warm, warm_reranker):
                        try:
                            _t.cancel()
                        except BaseException:
                            pass
                    for _t in (warm, warm_reranker):
                        try:
                            await _t
                        except BaseException:
                            pass
                    try:
                        await self._shutdown_close_sessions()
                    except BaseException:
                        pass

            app = Starlette(routes=[Route("/mcp", endpoint=_StreamableHTTPASGIApp())], lifespan=lifespan)
            uvicorn.run(app, host=host, port=port)

    async def _dispatch(self, name: str, args: dict) -> list | tuple:
        """Route MCP tool calls to internal methods and format responses.

        Returns either:
        - (content_list, structured_dict) for tools with structured output
        - content_list for tools with mixed content (e.g. screenshot with ImageContent)
        """
        from mcp.types import TextContent, ImageContent

        options = args.get("options") or {}

        if name == "mcp_smart_fetch":
            url = args.get("url", "")
            urls = args.get("urls")
            if not url and not urls:
                raise ValueError("Either 'url' or 'urls' must be provided")
            # Promoted first-class params: top-level takes precedence over the
            # options bag (backward compat: options still accepted as fallback).
            css_selector = args.get("css_selector") if args.get("css_selector") is not None else options.get("css_selector")
            max_content_chars = args.get("max_content_chars") if args.get("max_content_chars") is not None else options.get("max_content_chars")
            timeout = args.get("timeout") if args.get("timeout") is not None else options.get("timeout")
            pages = args.get("pages") if args.get("pages") is not None else options.get("pages")
            password = args.get("password") if args.get("password") is not None else options.get("password")
            kw = {k: v for k, v in options.items() if k in (
                "proxy", "cookies", "extra_headers", "useragent",
                "wait", "network_idle", "headless", "real_chrome", "respect_robots",
                "main_content_only", "use_trafilatura", "solve_cloudflare", "block_webrtc", "hide_canvas",
                "include_media", "include_links", "archive_fallback",
            )}
            result = await self.smart_fetch(
                url=url, urls=urls,
                extraction_type=args.get("extraction_type", "markdown"),
                css_selector=css_selector,
                max_content_chars=max_content_chars,
                timeout=timeout if timeout is not None else 30000,
                pages=pages,
                password=password,
                cache_ttl=args.get("cache_ttl", DEFAULT_TTL),
                force_fetcher=args.get("force_fetcher"),
                offset=args.get("offset", 0), **kw,
            )
            return [TextContent(type="text", text=result.model_dump_json())], result.model_dump()

        elif name == "mcp_smart_crawl":
            kw = {k: v for k, v in options.items() if k in (
                "max_pages", "max_depth", "path_include", "path_exclude",
                "max_content_chars_per", "max_total_chars", "concurrency",
                "cache_ttl", "respect_robots", "force_fetcher", "timeout",
                "deadline_ms", "sitemap",
            )}
            result = await self.smart_crawl(
                url=args["url"], discover_only=args.get("discover_only", False),
                focus=args.get("focus"), crawl_urls=args.get("crawl_urls"), **kw,
            )
            return [TextContent(type="text", text=result.model_dump_json())], result.model_dump()

        elif name == "mcp_screenshot":
            kw = {k: v for k, v in options.items() if k in (
                "full_page", "image_type", "quality", "wait", "wait_selector", "network_idle", "timeout",
            )}
            result = await self.screenshot(url=args["url"], session_id=args.get("session_id"), **kw)
            return result  # already list[ImageContent|TextContent]

        elif name == "mcp_smart_search":
            kw = {k: v for k, v in options.items() if k in (
                "max_results", "cache_ttl", "mode", "engines", "url",
                "site", "exclude_sites", "location", "language", "region", "page",
                "freshness",
            )}
            result = await self.smart_search(query=args["query"], **kw)
            return [TextContent(type="text", text=result.model_dump_json())], result.model_dump()

        elif name == "cache_clear":
            result = await self.cache_clear(all=args.get("all", False))
            return [TextContent(type="text", text=result.model_dump_json())], result.model_dump()

        elif name == "version":
            result = await self.version()
            return [TextContent(type="text", text=result.model_dump_json())], result.model_dump()

        else:
            raise ValueError(f"Unknown tool: {name}")



def _help_epilog() -> str:
    """Styled epilog for `hound --help`: the command cheat-sheet + docs link."""
    from master_fetch import cli_ui as ui
    return "\n".join([
        ui.dim("commands:"),
        f"  {ui.cyan('hound')}            {ui.dim('serve · stdio MCP (Claude Code, Cursor, OpenCode, Pi)')}",
        f"  {ui.cyan('hound --http')}     {ui.dim('serve · streamable HTTP (Open WebUI), use --host/--port')}",
        f"  {ui.cyan('hound -v')}         {ui.dim('version + update check')}",
        f"  {ui.cyan('hound -u')}         {ui.dim('update to the latest version')}",
        f"  {ui.cyan('hound --doctor')}   {ui.dim('health check + fix advice')}",
        f"  {ui.cyan('hound --rollback')} {ui.dim('undo the last update')}",
        "",
        ui.dim("docs:") + "  " + ui.cyan("https://github.com/dondai1234/master-fetch"),
    ])


def main():
    """Entry point for the hound CLI."""
    from master_fetch import cli_ui as ui
    from master_fetch import updater
    import argparse
    parser = argparse.ArgumentParser(
        prog="hound",
        description=ui.branded(ui.dim("web research for AI agents · $0 · no keys"), ""),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_help_epilog(),
    )
    parser.add_argument("--http", action="store_true",
                        help="serve over streamable HTTP (MCP 2025-03-26) at http://host:port/mcp")
    parser.add_argument("--host", default="127.0.0.1",
                        help="host for HTTP transport (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765,
                        help="port for HTTP transport (default 8765)")
    parser.add_argument("--cache-ttl", type=int, default=3600,
                        help="default cache TTL in seconds (default 3600)")
    parser.add_argument("-v", "--version", action="store_true",
                        help="show version + update status")
    parser.add_argument("-u", "--update", action="store_true",
                        help="update hound to the latest version")
    parser.add_argument("--doctor", action="store_true",
                        help="diagnose the install and suggest fixes")
    parser.add_argument("--rollback", action="store_true",
                        help="reinstall the version from before the last update")
    args = parser.parse_args()

    # Sweep a stale launcher left by a previous `hound -u` (Windows only).
    updater.cleanup_old_launcher()

    if args.update:
        updater.do_update()
        return
    if args.rollback:
        updater.rollback()
        return
    if args.doctor:
        updater.doctor()
        return
    if args.version:
        updater.print_version()
        return

    # HTTP mode: stdout is free (not an MCP stdio pipe), so a one-line banner is
    # safe. uvicorn follows with its own URL line. Stdio mode stays silent - any
    # stdout/stderr noise corrupts the MCP protocol or reads as a crash.
    if args.http:
        print(ui.branded(ui.cyan("serving HTTP"), ui.dim(f"http://{args.host}:{args.port}/mcp")))

    srv = MasterFetchServer(cache_ttl=args.cache_ttl)
    srv.serve(http=args.http, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

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

logger = logging.getLogger("master-fetch.server")

from mcp.server.fastmcp import Image
from mcp.types import ImageContent, TextContent

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

from master_fetch.cache import get_cached, set_cached, clear_cache, clear_all_cache, DEFAULT_TTL
from master_fetch.trafilatura_extractor import extract_with_trafilatura
from master_fetch.robots import is_allowed, clear_robots_cache
from master_fetch.search import SearchResponseModel
from master_fetch.reddit import is_reddit_url, rewrite_to_old_reddit, parse_old_reddit_listing
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
AUTO_SESSION_IDLE_TIMEOUT = 0  # 0 = keep browser alive forever. Pre-warmed on smart_search, stays idle until needed.
IDLE_CHECK_INTERVAL = 60  # How often to check for idle sessions (seconds)

# MCP initialize `instructions` — injected into the agent's context ONCE on
# connect by clients that support it. This is the connect-time mastery doc:
# the #1 workflow, the gotchas, and when to use each tool. Kept tight (~300
# tokens) since it is paid once, not per-turn-per-tool.
HOUND_INSTRUCTIONS = (
    "Hound = web access for this agent. 4 tools cover 95% of web work.\n"
    "\n"
    "• smart_fetch(url) - get any page. Auto-handles anti-bot (HTTP → stealthy Patchright browser). Returns extracted text + metadata.\n"
    "  - One section only? pass css_selector (e.g. 'article', '.main').\n"
    "  - Long page? response has is_truncated + next_offset → call again with offset=next_offset to page through.\n"
    "  - Seems wrong/empty? check response.content_ok and response.next_action - they tell you what to do.\n"
    "  - Many URLs? pass urls=['a','b'] (parallel bulk).\n"
    "  - Raw HTML? extraction_type='html'.\n"
    "  - PDFs: auto-extracted to structured markdown (tables/headings/metadata). Pass pages='1-5' to extract a subset and save tokens on big PDFs.\n"
    "  - Cache: cache_ttl=0 forces a fresh fetch (default 1hr).\n"
    "  - Long page, one topic? pass focus='...' to get only the BM25-relevant blocks (post-cache, no re-fetch; re-pass it when paginating with offset).\n"
    "• smart_search(query) - find pages. NEVER answer from snippets alone. Each result has fetch_relevance (high/med/low): smart_fetch the 'high' ones first (1-2), then 'med' if needed. Skip 'low'.\n"
    "  - Research mode: options={fetch_content:true} auto-fetches the top 3 results' full content in the same call (one call instead of 4). Good for quick factual answers.\n"
    "  - Filters: options={site:'docs.python.org', exclude_sites:['pinterest.com'], location:'US', language:'en', page:0}.\n"
    "• smart_crawl(url) - read a whole site/section. BFS same-domain links, returns each page as markdown with content_ok. options: max_pages (default 10), max_depth (default 2), path_include (scope to ['/docs']), discover_only=true (URL map only), focus='query' (crawl relevant pages first + focus-filter). Check next_action if it stopped early.\n"
    "• screenshot(url) - image capture. Multimodal agents only (content rendered as images/canvas/visual layout). Text agents: use smart_fetch instead. Session is auto-managed.\n"
    "\n"
    "#1 workflow (answer a factual question): smart_search → smart_fetch the 2 most relevant (fetch_relevance=high) results → synthesize, citing URLs.\n"
    "\n"
    "Known unbypassable (no free tool beats these): DataDome, Akamai, Cloudflare Turnstile (interactive). If smart_fetch fails on one, switch sources - do not retry the same URL.\n"
    "\n"
    "Pro tip: open_session once and reuse it for many fetches to skip browser cold-starts (power users only; smart_fetch auto-manages sessions otherwise)."
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
        next_action = "scanned/image-only PDF - OCR is not supported; use a vision-capable tool or another source"
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
    return summary, next_action, content_ok


def _with_agent_hints(result: ResponseModel) -> ResponseModel:
    """Stamp agent-facing summary/content_ok/next_action/fetched_at on a result."""
    summary, next_action, content_ok = _agent_hints(result)
    result.summary = summary
    result.content_ok = content_ok
    result.next_action = next_action
    result.fetched_at = datetime.now(timezone.utc).isoformat()
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
        return _with_agent_hints(ResponseModel(
            status=result.status, content=["[No more content.]"],
            url=result.url, cached=result.cached, fetcher_used=result.fetcher_used,
            extracted_type=result.extracted_type, session_id=result.session_id,
            duration_ms=result.duration_ms, error=result.error,
            content_type=result.content_type, total_size_bytes=result.total_size_bytes,
            total_extracted_chars=total_len,
            escalation_path=result.escalation_path, retry_count=result.retry_count,
            next_offset=0,
        ))

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

    return _with_agent_hints(ResponseModel(
        status=result.status, content=[chunk], url=result.url,
        cached=result.cached, fetcher_used=result.fetcher_used,
        extracted_type=result.extracted_type, session_id=result.session_id,
        duration_ms=result.duration_ms, error=result.error,
        content_type=result.content_type, total_size_bytes=result.total_size_bytes,
        total_extracted_chars=total_len,
        is_truncated=truncated, next_offset=next_off,
        escalation_path=result.escalation_path, retry_count=result.retry_count,
    ))


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


def _extract_pdf_response(body: bytes, raw_ct: str, total_size: int, url: str,
                          extraction_type: str, fetcher_used: str, duration_ms: float) -> ResponseModel:
    """Build a ResponseModel from a PDF body using the flagship extractor."""
    pages = _PDF_PAGES.get()
    password = _PDF_PASSWORD.get()
    try:
        from master_fetch.pdf_extractor import extract_pdf, PdfResult
        result: PdfResult = extract_pdf(body, extraction_type=extraction_type,
                                        pages=pages, password=password)
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
    # Scanned / image-only PDF: fall back to OCR if the OCR extras are
    # installed, so the agent gets the text instead of a dead-end. Auto-OCR
    # (no new param); an explicit `pages` spec is honored, otherwise OCR caps
    # at the first OCR_DEFAULT_PAGES pages to avoid a multi-minute hang.
    if result.scanned and not result.encrypted:
        try:
            from master_fetch.ocr import ocr_pdf, ocr_available
            if ocr_available():
                ocr_result = ocr_pdf(body, pages=pages, password=password)
                if ocr_result.content and not ocr_result.error:
                    result = ocr_result  # replace with OCR'd content
                elif ocr_result.error and ocr_result.encrypted:
                    result = ocr_result  # encrypted surfaced by OCR path too
                elif ocr_result.error:
                    # OCR attempted but failed — surface it honestly.
                    result.content = [f"[Scanned PDF - OCR attempted but failed: {ocr_result.error[:160]}]"]
                    result.error = f"ocr_failed: {ocr_result.error[:160]}"
            # else: OCR extras not installed -> keep the scanned dead-end below
        except ImportError:
            pass  # OCR extras not installed
        except Exception as e:
            logger.debug("OCR fallback failed for %s: %s", url, e)
    return ResponseModel(
        status=200, content=result.content, url=url,
        fetcher_used=fetcher_used, duration_ms=duration_ms,
        content_type=raw_ct, total_size_bytes=total_size,
        extracted_type="markdown", error=result.error,
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

    return ResponseModel(
        status=page.status, content=content, url=page.url,
        fetcher_used=fetcher_used, duration_ms=duration_ms,
        content_type=raw_ct, total_size_bytes=total_size,
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
        the agent first needs a stealthy fetch or screenshot — skipping the
        ~3-5s cold start. Idempotent: _ensure_auto_session reuses any existing
        session. Fails silently (e.g. chromium not installed); in that case the
        browser launches on first fetch instead.
        """
        try:
            _get_scrapling()  # Lazy-import scrapling (playwright) if not done yet
            await self._ensure_auto_session("stealthy")
            logger.debug("Stealthy browser warmed at startup")
        except Exception as e:
            logger.debug(f"Startup warm-up failed (will launch on first fetch): {e}")

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
        """
        async with self._sessions_lock:
            entries = list(self._sessions.items())
            self._sessions.clear()
            self._auto_stealthy_id = None
            self._auto_dynamic_id = None
        for sid, entry in entries:
            try:
                await entry.session.close()
            except Exception:
                pass
            entry._alive = False

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
                timed_tasks = [_timed(session.fetch(url)) for url in urls]
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

        Response contains: url, status, content (extracted text), content_type (e.g. 'text/html',
        'application/json'), total_size_bytes, is_truncated (if content was too long),
        escalation_path (e.g. 'http→dynamic'), duration_ms, error (with recovery hints).

        :param url: Single URL to fetch.
        :param urls: Multiple URLs to fetch in parallel. Returns BulkResponseModel instead.
            Use this instead of calling smart_fetch multiple times sequentially.
        :param extraction_type: Content format: 'markdown' (default), 'html', 'text', 'article', 'structured'.
        :param css_selector: CSS selector to narrow extracted content (e.g. 'article', '.main-content').
        :param main_content_only: Strip nav, ads, and footers (default True).
        :param use_trafilatura: Use Trafilatura for cleaner article extraction (default True).
        :param cache_ttl: Cache duration in seconds. Default 3600 (1 hour). Set 0 to skip cache.
        :param force_fetcher: Lock to one fetcher: 'http', 'dynamic', or 'stealthy'. Skips auto-escalation.
        :param respect_robots: Check robots.txt before fetching (default False).
        :param headless: Run browser without a visible window (default True).
        :param real_chrome: Use installed Chrome/Chromium instead of bundled browser.
        :param wait: Extra milliseconds to wait after page load for JS to render.
        :param proxy: Proxy URL (e.g. 'http://user:pass@host:8080') or dict with 'server', 'username', 'password'.
        :param timeout: Maximum request time in milliseconds (default 30000 = 30s). Browser fetcher uses ms; HTTP fetcher capped at 30s.
        :param network_idle: Wait until network is idle for 500ms before capturing (good for SPAs).
        :param solve_cloudflare: Attempt Cloudflare bypass in stealthy mode (default True).
        :param block_webrtc: Prevent WebRTC IP leak in stealthy mode (default True).
        :param hide_canvas: Randomize canvas fingerprint in stealthy mode (default True).
        :param extra_headers: Additional HTTP headers as {name: value} dict.
        :param useragent: Override browser user agent string.
        :param cookies: Cookies as list of {name, value, domain} dicts.
        :param offset: Resume from a specific character offset for truncated content.
        """
        # Bulk mode: fetch multiple URLs in parallel
        if urls is not None:
            return await self._smart_fetch_bulk(
                urls, extraction_type, css_selector, main_content_only,
                use_trafilatura, cache_ttl, force_fetcher, respect_robots,
                headless, real_chrome, wait, proxy, timeout, network_idle,
                solve_cloudflare, block_webrtc, hide_canvas, extra_headers,
                useragent, cookies, max_content_chars,
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
                return _apply_chunking(ResponseModel(
                    url=cached["url"], status=cached["status"], content=cached["content"],
                    cached=True, fetcher_used="cache", duration_ms=0,
                    extracted_type=extraction_type,
                    content_type=cached.get("content_type", ""),
                    total_size_bytes=cached.get("total_size_bytes", 0),
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

        # Accept if status is OK and content is real (not a JS shell).
        if result.status < 400 and not _is_js_shell(result):
            result.escalation_path = "direct:http"
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)

        # Should we escalate? Two reasons:
        # 1. Status 200 with JS shell -> page needs a real browser
        # 2. Status 403 or 503 -> explicit bot block
        should_escalate = (result.status < 400 and _is_js_shell(result)) or result.status in (403, 503)
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
        installed, latest, is_current = await asyncio_to_thread(_check_version)
        # up_to_date: True if at or ahead of PyPI (no update needed)
        up_to_date = is_current if is_current is not None else True
        if not up_to_date and latest:
            try:
                if _pad_version(installed) > _pad_version(latest):
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
        max_results: int = 10,
        cache_ttl: int = 300,
        api_key: str = "",
        site: Optional[str] = None,
        exclude_sites: Optional[List[str]] = None,
        location: Optional[str] = None,
        language: Optional[str] = None,
        page: int = 0,
        fetch_content: bool = False,
        fetch_top: int = 3,
        max_content_chars_per: int = 8000,
    ) -> Union[SearchResponseModel, "ResearchResponseModel"]:
        """Search the web via TinyFish API and return structured results.

        Filters: site/exclude_sites (domain include/exclude via site: operators),
        location/language (geo), page (0-10). Research mode (fetch_content=True)
        auto-fetches the top-N high-relevance results' full content in this call.
        Requires TINYFISH_API_KEY env var (free key at tinyfish.ai).
        """
        try:
            query = validate_search_query(query)
        except SecurityError as e:
            return SearchResponseModel(
                query=query, results=[], total_results=0,
                duration_ms=0, error=str(e),
            )

        # Redact API key from loggable context
        safe_api_key = api_key.strip() if api_key and isinstance(api_key, str) else ""

        try:
            from master_fetch.search import smart_search as _smart_search
            return await _smart_search(
                self, query, max_results, cache_ttl, safe_api_key,
                site=site, exclude_sites=exclude_sites, location=location,
                language=language, page=page, fetch_content=fetch_content,
                fetch_top=fetch_top, max_content_chars_per=max_content_chars_per,
            )
        except ImportError as e:
            return SearchResponseModel(
                query=query, results=[], total_results=0,
                error=(
                    f"Search dependencies not installed. "
                    f"Run: pip install hound-mcp[all] ({e})"
                ),
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
        max_content_chars_per: int = 4000,
        max_total_chars: Optional[int] = None,
        concurrency: int = 3,
        cache_ttl: int = DEFAULT_TTL,
        respect_robots: bool = False,
        force_fetcher: Optional[str] = None,
        timeout: int = 30000,
    ) -> "CrawlResponseModel":
        """Deep-crawl a site: BFS same-domain from `url`, returning each page as
        markdown with content_ok/summary. discover_only=true returns the URL map
        only. `focus` prioritizes relevant pages within the budget. Caps:
        max_pages, max_depth, max_total_chars (token budget). Reuses smart_fetch's
        anti-bot escalation + cache.
        """
        try:
            from master_fetch.crawl import smart_crawl as _smart_crawl, CrawlResponseModel as _CRM
            return await _smart_crawl(
                self, url, max_pages=max_pages, max_depth=max_depth,
                path_include=path_include, path_exclude=path_exclude,
                discover_only=discover_only, focus=focus,
                max_content_chars_per=max_content_chars_per,
                max_total_chars=max_total_chars, concurrency=concurrency,
                cache_ttl=cache_ttl, respect_robots=respect_robots,
                force_fetcher=force_fetcher, timeout=timeout,
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
            "description": "Fetch any URL with full content extraction. USE THIS whenever you need information from the web: this is your web access. Auto http -> stealthy escalation (plain HTTP first, then Patchright anti-detect browser if blocked). Bulk: pass urls. Narrow to one section with css_selector. PDFs: auto-extracted to structured markdown (tables, headings, metadata); pass pages='1-5' to extract a subset and save tokens; scanned PDFs auto-OCR with [all]. Long pages: paginate with offset (pages through EXTRACTED text; use extraction_type=html for raw HTML). focus='query' returns only the BM25-relevant blocks (token saver on long pages; re-pass it when paginating). Response signals: content_ok (trust content only if true), next_action (do this next if non-empty), summary (one-line status), is_truncated+next_offset (more content available). cache_ttl=0 bypasses cache.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "urls": {"type": "array", "items": {"type": "string"}, "description": "Multiple URLs (fetched in parallel; returns per-URL results)"},
                    "extraction_type": {"type": "string", "enum": ["markdown", "html", "text", "article", "structured"], "description": "Content format (default markdown). html = raw HTML."},
                    "css_selector": {"type": "string", "description": "CSS selector to narrow extracted content (e.g. 'article', '.main-content', '#post'). Big token/context saver."},
                    "max_content_chars": {"type": "integer", "description": "Max chars of extracted content to return (default 40000; min 500). Lower = less context per call; the rest is paginated via offset/next_offset."},
                    "timeout": {"type": "integer", "description": "Max request time in ms (default 30000). HTTP tier capped at 30s."},
                    "cache_ttl": {"type": "integer", "description": "Cache seconds (default 3600). 0 = force fresh."},
                    "force_fetcher": {"type": "string", "enum": ["http", "stealthy"], "description": "Pin to one tier and skip auto-escalation. 'http' = fast HTTP-only (fails on JS/bot walls). 'stealthy' = anti-detect browser. Default = auto http->stealthy."},
                    "offset": {"type": "integer", "description": "Char offset into EXTRACTED text to resume a truncated page. Use next_offset from the previous response."},
                    "pages": {"type": "string", "description": "PDF only: page spec like '1-5' or '1,3,5-7' to extract a subset (saves tokens/time on big PDFs). None = all pages."},
                    "password": {"type": "string", "description": "PDF only: password for an encrypted PDF."},
                    "focus": {"type": "string", "description": "Query-focused extraction: pass a query and only the BM25-relevant blocks (paragraphs/headings/tables) are returned, a big context saver on long pages. Runs post-cache (no re-fetch). Re-pass the same focus when paginating with offset."},
                    "options": {"type": "object", "description": "proxy (str|dict), cookies (list), extra_headers (dict), useragent (str), wait (ms, default 0), network_idle (bool, for SPAs), headless (bool, default true), real_chrome (bool), respect_robots (bool, default false), main_content_only (bool, default true), use_trafilatura (bool, default true), solve_cloudflare (bool, default true), block_webrtc (bool, default true), hide_canvas (bool, default true)", "additionalProperties": True},
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        },
        {
            "name": "mcp_smart_crawl",
            "description": "Deep-crawl a site from a start URL: walks same-domain links breadth-first and returns each page as clean markdown with content_ok. Use for 'read all the docs on this site' / 'scrape this whole section'. discover_only=true returns just the URL map (no content). focus='query' prioritizes relevant pages within the budget AND focus-filters each page. Caps: max_pages (default 10), max_depth (default 2), max_total_chars (token budget). Each page carries content_ok + summary; check them. next_action tells you if the crawl stopped early (raise the caps or scope with path_include). Reuses smart_fetch anti-bot + cache.",
            "inputSchema": {
                "type": "object", "required": ["url"],
                "properties": {
                    "url": {"type": "string", "description": "Start URL (crawl stays on this domain)"},
                    "discover_only": {"type": "boolean", "description": "true = return the URL map only, no page content (map mode). Default false."},
                    "focus": {"type": "string", "description": "Query: prioritize crawling links relevant to this, and focus-filter each page's content. Big token saver on doc sites."},
                    "options": {"type": "object", "description": "max_pages (1-100, default 10), max_depth (0-5, default 2), path_include (list of path prefixes to crawl, e.g. ['/docs']), path_exclude (list to skip), max_content_chars_per (default 4000), max_total_chars (token budget, default max_pages*per), concurrency (1-5, default 3), cache_ttl (seconds, default 3600), respect_robots (bool, default false), force_fetcher ('http'|'stealthy'), timeout (ms, default 30000)", "additionalProperties": True},
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        },
        {
            "name": "mcp_screenshot",
            "description": "Capture a screenshot of a URL as an image. For MULTIMODAL agents only: use when content is rendered as images/canvas/image-of-text that text extraction can't read, or you need visual layout. Text-only agents: prefer smart_fetch. A stealthy browser session is auto-managed (pass session_id only to reuse a specific open_session).",
            "inputSchema": {
                "type": "object", "required": ["url"],
                "properties": {
                    "url": {"type": "string", "description": "URL to screenshot"},
                    "session_id": {"type": "string", "description": "Optional: a session from open_session to reuse. Omit to auto-manage."},
                    "options": {"type": "object", "description": "full_page (bool, default false), image_type (png|jpeg, default png), quality (0-100, jpeg only), wait (ms), wait_selector (css), network_idle (bool), timeout (ms, default 30000)", "additionalProperties": True},
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        },
        {
            "name": "mcp_smart_search",
            "description": "Web search via TinyFish (free key). Returns URLs with titles + snippets; each result has fetch_relevance (high/med/low). NEVER answer from snippets alone: either smart_fetch the 'high' results, OR set fetch_content=true (research mode) to auto-fetch the top-N results' full content in THIS one call (saves round-trips). Filters in options: site/exclude_sites (domain include/exclude), location/language (geo), page (0-10). Results cached 5min.",
            "inputSchema": {
                "type": "object", "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "options": {"type": "object", "description": "max_results (1-50, default 10), cache_ttl (seconds, default 300), api_key, site (domain to restrict, e.g. 'docs.python.org'), exclude_sites (list of domains to exclude), location (2-letter country code, e.g. 'US'), language (2-letter code, e.g. 'en'), page (0-10, pagination), fetch_content (bool, default false: research mode, auto-fetch top results' full content in this call), fetch_top (1-5, default 3: how many to fetch in research mode), max_content_chars_per (default 8000: per-result content cap in research mode)", "additionalProperties": True},
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        },
        {
            "name": "cache_clear",
            "description": "Clear fetch cache. all=true wipes all (default: expired only). Cache stores extracted text per URL+extraction_type+css_selector; default TTL 1hr.",
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
        """Start the MCP server using low-level Server for minimal token overhead."""
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
                # the agent's first stealthy fetch/screenshot. Best-effort, runs in
                # the background while the server handles the initialize handshake.
                warm = asyncio.create_task(self._prewarm_stealthy())
                try:
                    async with stdio_server() as (read, write):
                        await server.run(read, write, server.create_initialization_options())
                finally:
                    warm.cancel()
                    try:
                        await warm
                    except Exception:
                        pass
                    await self._shutdown_close_sessions()

            anyio.run(_run)
        else:
            from mcp.server.sse import SseServerTransport
            from starlette.applications import Starlette
            from starlette.routing import Route

            sse = SseServerTransport("/messages/")

            async def handle_sse(request):
                async with sse.connect_sse(request.scope, request.receive, request._send) as (read, write):
                    await server.run(read, write, server.create_initialization_options())

            async def _startup():
                asyncio.create_task(self._prewarm_stealthy())

            app = Starlette(
                routes=[Route("/sse", endpoint=handle_sse), Route("/messages/", endpoint=sse.handle_post_message)],
                on_startup=[_startup],
                on_shutdown=[self._shutdown_close_sessions],
            )
            import uvicorn
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
            )}
            result = await self.smart_crawl(
                url=args["url"], discover_only=args.get("discover_only", False),
                focus=args.get("focus"), **kw,
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
                "max_results", "cache_ttl", "api_key",
                "site", "exclude_sites", "location", "language", "page",
                "fetch_content", "fetch_top", "max_content_chars_per",
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

def _check_version():
    """Check installed version and compare with PyPI latest. Returns (installed, latest, is_current).

    Note: Uses synchronous urllib (blocking). Acceptable because this is called
    from the version tool which runs infrequently and is not latency-sensitive.
    """
    from importlib.metadata import version as _get_version
    try:
        installed = _get_version("hound-mcp")
    except Exception:
        installed = "unknown"

    latest = None
    try:
        import json
        from urllib.request import urlopen, Request
        req = Request(
            "https://pypi.org/pypi/hound-mcp/json",
            headers={"User-Agent": "Hound/" + installed},
        )
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            latest = data.get("info", {}).get("version")
    except Exception:
        pass

    return installed, latest, (latest == installed if latest else None)


def _pad_version(v: str) -> tuple:
    parts = v.split(".")
    return tuple(int(p) for p in parts[:3])


def _hound_launcher_path() -> str | None:
    """Locate the installed hound launcher executable.

    Cross-platform: returns the path to the launcher that `hound` resolves to
    (hound.exe on Windows, a Python script named `hound` on macOS/Linux), or
    None if it can't be found. Only meaningfully used on Windows for launcher
    staging; on POSIX there is no file-lock so staging is unnecessary.
    """
    import shutil
    # Primary: whatever 'hound' on PATH resolves to (what the user just ran).
    candidate = shutil.which("hound")
    if candidate and os.path.exists(candidate):
        return candidate
    # Windows fallback: the Scripts dir next to the running interpreter.
    scripts_dir = os.path.join(os.path.dirname(sys.executable), "Scripts")
    for name in ("hound.exe", "hound"):
        fallback = os.path.join(scripts_dir, name)
        if os.path.exists(fallback):
            return fallback
    # POSIX fallback: the bin dir next to the running interpreter.
    posix_bin = os.path.dirname(sys.executable)
    posix_fallback = os.path.join(posix_bin, "hound")
    if os.path.exists(posix_fallback):
        return posix_fallback
    return None


def _stage_running_launcher() -> str | None:
    """Stage the running Windows launcher aside so pip can replace it.

    Windows locks a running .exe against overwrite but permits renaming it.
    Rename hound.exe -> hound.exe.old so `pip install --upgrade` can write a
    fresh hound.exe. Returns the .old path on success, None otherwise.

    No-op on non-Windows (POSIX has no file lock) and when the launcher
    can't be located or renamed (read-only install). The .old is swept on
    the next launch by _cleanup_old_launcher().
    """
    if sys.platform != "win32":
        return None
    exe = _hound_launcher_path()
    if not exe or not exe.lower().endswith(".exe"):
        return None
    old = exe + ".old"
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        pass
    try:
        os.rename(exe, old)
        return old
    except OSError:
        return None


def _cleanup_old_launcher() -> None:
    """Remove a stale hound.exe.old left by a previous self-update.

    Windows locks the running .exe against deletion, so _stage_running_launcher
    renames the live launcher to hound.exe.old before letting pip write a fresh
    one. The .old can only be deleted once the process running from it has
    exited, so we sweep it on the next launch instead. No-op on non-Windows.
    """
    if sys.platform != "win32":
        return
    exe = _hound_launcher_path()
    if not exe:
        return
    old = exe + ".old"
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        # Still locked (another hound -u running concurrently) — leave it.
        pass


def _looks_like_file_lock_error(stderr: str) -> bool:
    """Detect pip failure caused by a locked running launcher (WinError 32)."""
    if not stderr:
        return False
    s = stderr.lower()
    return ("winerror 32" in s or "being used by another process" in s
            or "permission denied" in s and "hound" in s)


def _other_hound_pids() -> list[int]:
    """PIDs of OTHER running hound launcher processes (excludes this one).

    Cross-platform. Detects a long-running hound MCP server that holds the
    launcher (hound.exe on Windows, `hound` on POSIX) against an in-place
    upgrade. Returns [] on detection failure (we never block the update just
    because we couldn't enumerate processes — the pip-failure path handles it).
    """
    import subprocess
    my_pid = os.getpid()
    pids: list[int] = []
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq hound.exe", "/FO", "CSV", "/NH"],
                text=True, timeout=10, creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            for line in out.splitlines():
                # Line: "hound.exe","PID","Session","SessionNum","Mem"
                parts = [p.strip().strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[0].lower() == "hound.exe":
                    try:
                        pid = int(parts[1])
                    except ValueError:
                        continue
                    if pid != my_pid:
                        pids.append(pid)
        else:
            out = subprocess.check_output(["ps", "-eo", "pid=,comm="], text=True, timeout=10)
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    pid_s, comm = line.split(None, 1)
                    pid = int(pid_s)
                except ValueError:
                    continue
                if os.path.basename(comm.strip()) == "hound" and pid != my_pid:
                    pids.append(pid)
    except Exception:
        return []
    return pids


def _stop_hound_cmd() -> str:
    """Platform command to stop all running hound launcher processes."""
    if sys.platform == "win32":
        return "taskkill /IM hound.exe /F"
    return "pkill -f hound"


def _reinstall_cmd(ver: str) -> str:
    """Platform pip command to force-reinstall a specific hound-mcp version."""
    return f"pip install --force-reinstall --no-deps hound-mcp=={ver}"


def _corrupted_install_message() -> str:
    """Message shown when hound-mcp package metadata is missing.

    This happens when a previous `hound -u` (or a manual pip operation) was
    interrupted mid-uninstall: pip deleted the dist-info but could not
    overwrite hound.exe (WinError 32), leaving the launcher orphaned from its
    package metadata. importlib.metadata then can't find the version, so
    `hound -v` prints 'vunknown'. Tell the user exactly how to recover.
    """
    return (
        "Hound install is corrupted: package metadata is missing (a previous\n"
        "update was interrupted before it could finish). The launcher still\n"
        "works, but pip no longer knows which version is installed. Recover with:\n"
        "  pip install --force-reinstall --no-deps hound-mcp==<latest>\n"
        "(find <latest> at https://pypi.org/project/hound-mcp/).\n"
        "Also make sure you install 'hound-mcp' (the package), NOT 'hound'\n"
        "(an unrelated PyPI package)."
    )


def _print_pip_failure(result) -> None:
    """Print the most useful line from a failed pip run. Callers add recovery."""
    err = (result.stderr or "").strip() if result is not None else ""
    printed = False
    for line in err.split("\n"):
        if "ERROR" in line or "error" in line.lower():
            print(f"  {line.strip()}")
            printed = True
            break
    if not printed:
        print(f"  {err.split(chr(10))[-1] if err else 'update failed'}")


def _spawn_console_updater(pip_cmd: list, target_ver: str) -> bool:
    """Windows: spawn a child python.exe (NOT hound.exe) that inherits this
    console, waits for the hound.exe launcher to exit, then runs pip.

    This is the reliable fix for the self-update lock: the running `hound -u`
    command IS hound.exe, so pip can't overwrite it while it runs. The child is
    a separate python.exe that doesn't lock hound.exe; once the parent launcher
    exits, hound.exe is free and pip replaces it. The child prints pip progress
    and the result to the inherited console, so the user sees everything.

    The child re-checks for a REAL running hound MCP server AFTER the parent
    exits (by then the current command's launcher is gone, so no false positive
    — unlike an upfront check, which can't distinguish the current launcher
    from a server because the launcher is a grandparent of the python process).
    Returns True if the child was spawned.
    """
    import subprocess
    stop_cmd = _stop_hound_cmd()
    reinstall = _reinstall_cmd(target_ver)
    pip_repr = ", ".join(repr(c) for c in pip_cmd)
    # Generated as source so the child has NO dependency on the about-to-be-
    # replaced master_fetch package. Uses chr(34) for embedded quotes.
    child_src = f'''import time, subprocess, sys, os
time.sleep(2)  # let the parent hound.exe launcher exit and release the file

# By now the parent has exited and the shell has reclaimed this console,
# printing its prompt (e.g. the PowerShell prompt). Move to a fresh line below
# it BEFORE we print, so our output does not overlap the prompt line (the
# "ghost prompt" bug where "Running pip..." landed on top of the prompt).
try:
    sys.stdout.write(chr(10)); sys.stdout.flush()
except Exception:
    pass

def _hound_pids():
    my = os.getpid()
    try:
        o = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq hound.exe", "/FO", "CSV", "/NH"],
            text=True, timeout=10, creationflags=0x08000000)
    except Exception:
        return []
    out = []
    for ln in o.splitlines():
        ps = [x.strip().strip(chr(34)) for x in ln.split(chr(34) + "," + chr(34))]
        if len(ps) >= 2 and ps[0].lower() == "hound.exe":
            try:
                pid = int(ps[1])
            except ValueError:
                continue
            if pid != my:
                out.append(pid)
    return out

others = _hound_pids()
if others:
    print("Cannot update: a hound MCP server is still running and holds the")
    print("launcher against replacement:")
    for p in others:
        print("  PID %d" % p)
    print("Stop it first:")
    print("  {stop_cmd}")
    print("then re-run:  hound -u")
    print("or recover manually:  {reinstall}")
    sys.exit(1)

print("Running pip...")
r = subprocess.run([{pip_repr}])
if r.returncode != 0:
    print("Update failed (pip returned %d)." % r.returncode)
    print("Stop any running hound MCP server:  {stop_cmd}")
    print("or recover manually:  {reinstall}")
    sys.exit(1)

from importlib.metadata import version as _v
try:
    new_ver = _v("hound-mcp")
except Exception:
    new_ver = "unknown"
target = {target_ver!r}
def _pad(v):
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except Exception:
        return None
np_, tp_ = _pad(new_ver), _pad(target)
if new_ver == "unknown" or (np_ and tp_ and np_ < tp_):
    print("The upgrade to v%s did not complete. hound.exe could not be" % target)
    print("replaced (a running hound MCP server likely holds it). Stop it:")
    print("  {stop_cmd}")
    print("then re-run:  hound -u")
    print("or recover manually:  {reinstall}")
    sys.exit(1)
print("Hound v" + new_ver)
'''
    try:
        # Inherit the parent console (no DETACHED / CREATE_NEW_PROCESS_GROUP)
        # so the child's output appears in the same window after the parent exits.
        subprocess.Popen([sys.executable, "-c", child_src])
        return True
    except Exception:
        return False


def _run_pip_sync(pip_cmd: list, target_ver: str) -> None:
    """Run pip synchronously with bulletproof, platform-aware messaging.

    Used on POSIX (no file lock) and as a Windows fallback if the detached
    console updater can't be spawned. Detects a silent no-op (pip returns 0
    but the version didn't advance) and every failure path.
    """
    import subprocess
    try:
        result = subprocess.run(pip_cmd, timeout=300)
        rc = result.returncode
    except subprocess.TimeoutExpired:
        print("  update timed out (pip took too long). Re-run:  hound -u")
        print(f"  or recover manually:  {_reinstall_cmd(target_ver)}")
        sys.exit(1)
    except Exception as e:
        print(f"  update failed: {e}")
        print(f"  Recover manually:  {_reinstall_cmd(target_ver)}")
        sys.exit(1)
    if rc != 0:
        print(f"  Update failed (pip returned {rc}).")
        print(f"  Stop any running hound MCP server:  {_stop_hound_cmd()}")
        print(f"  or recover manually:  {_reinstall_cmd(target_ver)}")
        sys.exit(1)
    new_ver = _check_version()[0]
    if new_ver == "unknown":
        print("  Upgrade failed: package metadata is missing after the update.")
        print(f"  Recover manually:  {_reinstall_cmd(target_ver)}")
        sys.exit(1)
    try:
        advanced = _pad_version(new_ver) >= _pad_version(target_ver)
    except (ValueError, IndexError):
        advanced = (new_ver == target_ver)
    if not advanced:
        print(f"  The upgrade to v{target_ver} did not complete. hound.exe could not be")
        print("  replaced (a running hound MCP server likely holds it). Stop it:")
        print(f"    {_stop_hound_cmd()}")
        print("  then re-run:  hound -u")
        print(f"  or recover manually:  {_reinstall_cmd(target_ver)}")
        sys.exit(1)
    print(f"Hound v{new_ver}")


def _do_update():
    """Update hound-mcp via pip. Cross-platform; just works on Windows.

    On Windows the running `hound -u` command IS hound.exe, so pip can't
    overwrite the launcher while it runs. We spawn a child python.exe (not
    hound.exe) that inherits the console, waits for the launcher to exit, then
    runs pip — at which point hound.exe is free and pip replaces it. The child
    prints pip progress and the result to the same window. It re-checks for a
    real hound MCP server AFTER the launcher exits (so the current command's
    own launcher is never mistaken for a server — the bug that made `hound -u`
    refuse to run on itself).

    On macOS/Linux there's no file lock, so pip runs synchronously. Other
    hound processes don't block the file replace, but a running MCP server
    keeps the old code in memory until restarted — we warn so the user knows.
    Every failure path prints an actionable, platform-aware recovery command.
    """
    installed, latest, is_current = _check_version()

    if installed == "unknown":
        print("Hound install metadata is missing; reinstalling to recover...")

    if not latest:
        print(f"Hound v{installed}: couldn't reach PyPI to check for updates.")
        print("  Check your internet connection, then re-run:  hound -u")
        print("  or upgrade manually:  pip install --upgrade hound-mcp[all]")
        return

    try:
        if _pad_version(installed) >= _pad_version(latest):
            print(f"Hound v{installed} (latest)")
            return
    except (ValueError, IndexError):
        # installed is "unknown" or unparseable — fall through and reinstall.
        pass

    # Verbose pip (no -qq) so the user sees progress in the console.
    pip_cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "hound-mcp[all]",
               "--no-cache-dir", "--disable-pip-version-check",
               "--no-python-version-warning"]

    if sys.platform == "win32":
        if _spawn_console_updater(pip_cmd, latest):
            print(f"Updating v{installed} to v{latest}...")
            print("  (the upgrade finishes in this window once this command exits)")
            return
        # Spawn failed — best-effort synchronous attempt with honest messaging.
        print(f"Updating v{installed} to v{latest}...")
        _run_pip_sync(pip_cmd, latest)
        return

    # POSIX: no file lock. Run pip synchronously. Warn about running servers
    # (they'll keep old code until restarted) but don't refuse — pip works.
    others = _other_hound_pids()
    if others:
        print("Note: other hound processes are running. They will keep using the")
        print("old code until restarted:")
        for p in others:
            print(f"  PID {p}")
        print(f"  After the update, restart them:  {_stop_hound_cmd()}  (then start hound again)")
        print()
    print(f"Updating v{installed} to v{latest}...")
    _run_pip_sync(pip_cmd, latest)


def main():
    """Entry point for the hound CLI."""
    import argparse
    parser = argparse.ArgumentParser(description="Hound MCP Server")
    parser.add_argument("--http", action="store_true",
                        help="Use HTTP transport instead of stdio")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Host for HTTP transport")
    parser.add_argument("--port", type=int, default=8765,
                        help="Port for HTTP transport")
    parser.add_argument("--cache-ttl", type=int, default=3600,
                        help="Default cache TTL in seconds")
    parser.add_argument("-v", "--version", action="store_true",
                        help="Check installed version + update status")
    parser.add_argument("-u", "--update", action="store_true",
                        help="Update Hound to latest version")
    args = parser.parse_args()

    # Sweep a stale launcher left by a previous `hound -u` (Windows only).
    _cleanup_old_launcher()

    if args.update:
        _do_update()
        return

    if args.version:
        installed, latest, is_current = _check_version()
        if installed == "unknown":
            print(_corrupted_install_message())
            if latest:
                print(f"  Latest known version: v{latest}")
                print(f"  Recover with:  {_reinstall_cmd(latest)}")
            return
        if latest is None:
            print(f"Hound v{installed} (couldn't reach PyPI to check for updates).")
            print("  Check your internet, then re-run:  hound -v")
            return
        try:
            up_to_date = _pad_version(installed) >= _pad_version(latest)
        except (ValueError, IndexError):
            up_to_date = bool(is_current)
        if up_to_date:
            print(f"Hound v{installed} (latest)")
        else:
            print(f"Hound v{installed}. v{latest} available. Run `hound -u` to update.")
        return

    srv = MasterFetchServer(cache_ttl=args.cache_ttl)
    srv.serve(http=args.http, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

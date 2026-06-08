"""Hound MCP Server.

Forks Scrapling's built-in MCP server and adds:
- Trafilatura article extraction (cleaner than markdownify)
- Smart fetch routing (auto-escalate HTTP -> Dynamic -> Stealthy)
- SQLite content cache with TTL
- Domain intelligence (remember which sites need stealth)
- smart_fetch umbrella tool (single entry point that routes automatically)
- extract_article and extract_structured modes
- Input validation with SSRF protection
"""

from __future__ import annotations

import json
import logging
import sys
from uuid import uuid4
import asyncio
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
from master_fetch.domain_intel import get_domain_level, record_result
from master_fetch.trafilatura_extractor import extract_with_trafilatura
from master_fetch.robots import is_allowed, clear_robots_cache
from master_fetch.search import SearchResponseModel
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
AUTO_SESSION_IDLE_TIMEOUT = 1800  # Close auto browser sessions after 30 min idle (seconds)
IDLE_CHECK_INTERVAL = 60  # How often to check for idle sessions (seconds)


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
    escalation_path: str = Field(default="", description="e.g. http→dynamic→stealthy")
    retry_count: int = Field(default=0, description="Retries")


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

    Used by smart_fetch to decide whether to escalate from HTTP->dynamic or dynamic->stealthy.
    """
    content_str = " ".join(result.content).lower().strip()
    if not content_str:
        return True  # Empty content after extraction = JS shell or blank page
    return any(signal in content_str for signal in _JS_SHELL_SIGNALS)


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


def _apply_chunking(result: ResponseModel, max_chars: int = MAX_CONTENT_CHARS, offset: int = 0) -> ResponseModel:
    """Truncate content if it exceeds max_chars, starting from offset.

    Smart merge: if remaining content after a chunk is less than MIN_CHUNK_CHARS,
    include it all in the current chunk. This prevents wasteful round-trips where
    an agent calls again just to get 55 chars.

    Always sets total_extracted_chars so agents can gauge remaining content
    without making a follow-up call.
    """
    full_text = "\n".join(result.content)
    total_len = len(full_text)

    if offset >= total_len:
        return ResponseModel(
            status=result.status, content=["[No more content.]"],
            url=result.url, cached=result.cached, fetcher_used=result.fetcher_used,
            extracted_type=result.extracted_type, session_id=result.session_id,
            duration_ms=result.duration_ms, error=result.error,
            content_type=result.content_type, total_size_bytes=result.total_size_bytes,
            total_extracted_chars=total_len,
            escalation_path=result.escalation_path, retry_count=result.retry_count,
            next_offset=0,
        )

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

    return ResponseModel(
        status=result.status, content=[chunk], url=result.url,
        cached=result.cached, fetcher_used=result.fetcher_used,
        extracted_type=result.extracted_type, session_id=result.session_id,
        duration_ms=result.duration_ms, error=result.error,
        content_type=result.content_type, total_size_bytes=result.total_size_bytes,
        total_extracted_chars=total_len,
        is_truncated=truncated, next_offset=next_off,
        escalation_path=result.escalation_path, retry_count=result.retry_count,
    )


# ─── Response translation helpers ──────────────────────────────────

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

    s = _get_scrapling()
    content: list[str]
    if use_trafilatura and extraction_type in ("markdown", "text", "article", "structured"):
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
                logger.warning(f"Cookie dict missing 'name' key, skipping: {c}")
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

        Idle timeout: auto sessions close after AUTO_SESSION_IDLE_TIMEOUT (30 min)
        of inactivity. Reopened on next fetch (one-time startup penalty).
        """
        attr = "_auto_dynamic_id" if session_type == "dynamic" else "_auto_stealthy_id"
        ts_attr = "_auto_dynamic_last_used" if session_type == "dynamic" else "_auto_stealthy_last_used"
        async with self._sessions_lock:
            existing_id = getattr(self, attr)
            if existing_id and existing_id in self._sessions and self._sessions[existing_id].session._is_alive:
                setattr(self, ts_attr, now())
                # Ensure monitor is alive (may have crashed since last check)
                self._ensure_idle_monitor()
                return existing_id

        # Create session outside lock (expensive — browser launch)
        sid = await self.open_session(session_type=session_type, headless=True)

        async with self._sessions_lock:
            # Re-check: another call may have created a session while we were busy
            existing_id = getattr(self, attr)
            if existing_id and existing_id in self._sessions and self._sessions[existing_id].session._is_alive:
                # We lost the race — close ours, use theirs
                try:
                    await self.close_session(sid.session_id)
                except Exception:
                    pass  # Best effort cleanup
                setattr(self, ts_attr, now())
                self._ensure_idle_monitor()
                return existing_id
            setattr(self, attr, sid.session_id)
            setattr(self, ts_attr, now())

        # Start idle monitor if not running
        self._ensure_idle_monitor()

        return sid.session_id

    async def _close_auto_dynamic_session(self) -> None:
        """Close the auto dynamic browser session if it exists.

        Called when a stealthy auto session is created — Patchright handles
        everything Playwright can, so keeping both is wasted memory (~100-180MB).

        Safe to call even if no dynamic session exists (no-op).
        """
        async with self._sessions_lock:
            dsid = self._auto_dynamic_id
            if dsid is None:
                return
            self._auto_dynamic_id = None
        # Close outside lock to avoid blocking
        try:
            await self.close_session(dsid)
        except Exception as e:
            logger.warning(f"Failed to close dynamic auto session during consolidation: {e}")
            async with self._sessions_lock:
                entry = self._sessions.pop(dsid, None)
                if entry:
                    entry._alive = False

    def _stealthy_auto_alive(self) -> bool:
        """Check if the auto stealthy session is alive (Patchright browser running).

        INFORMATIONAL ONLY. Does not acquire the sessions lock, so the result may
        be stale by the time the caller acts on it. Safe for tier-skip decisions
        (Phase C unknown domain), but NOT safe for reading the session ID directly.
        For atomic check-and-use, see _acquire_stealthy_session().
        """
        sid = self._auto_stealthy_id
        return bool(sid and sid in self._sessions and self._sessions[sid]._alive)

    async def _acquire_stealthy_session(self) -> Optional[str]:
        """Atomically check if the auto stealthy session is alive, bump its
        last-used timestamp, and return its session ID.

        Returns None if not alive. Uses sessions_lock to prevent TOCTOU races
        with the idle monitor and other concurrent callers.
        """
        async with self._sessions_lock:
            sid = self._auto_stealthy_id
            if sid and sid in self._sessions and self._sessions[sid]._alive:
                self._auto_stealthy_last_used = now()
                return sid
        return None

    async def _start_idle_monitor(self) -> None:
        """Background task: close auto browser sessions after AUTO_SESSION_IDLE_TIMEOUT
        of inactivity.

        All reads of _auto_*_id and _auto_*_last_used happen inside the sessions lock
        to prevent races with _ensure_auto_session and _acquire_stealthy_session.
        Session closing happens outside the lock to avoid blocking other operations.
        """
        while True:
            await asyncio_sleep(IDLE_CHECK_INTERVAL)
            try:
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
        """
        if self._idle_monitor_task is None or self._idle_monitor_task.done():
            self._idle_monitor_task = asyncio.create_task(self._start_idle_monitor())

    async def _finalize_result(
        self,
        result: ResponseModel,
        url: str,
        extraction_type: str,
        css_selector: Optional[str],
        cache_ttl: int,
        offset: int = 0,
    ) -> ResponseModel:
        """Apply content quality annotation, cache, and chunking to a fetch result.

        Centralizes the repetitive 'annotate -> cache -> chunk' pattern
        that was duplicated 8+ times across smart_fetch.
        """
        result = _annotate_quality(result)
        if cache_ttl > 0 and result.status > 0:
            await set_cached(url, extraction_type, result.content, result.status, css_selector, cache_ttl)
        return _apply_chunking(result, offset=offset)

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
        Use close_session to close the session when done, and list_sessions to see all
        active sessions.

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

    async def list_sessions(self) -> List[SessionInfo]:
        """List all active browser sessions with their details."""
        async with self._sessions_lock:
            return [
                SessionInfo(
                    session_id=sid, session_type=entry.session_type,
                    created_at=entry.created_at, is_alive=entry._alive,
                )
                for sid, entry in self._sessions.items()
            ]

    # ─── Screenshot ───────────────────────────────────────────────

    async def screenshot(
        self,
        url: str,
        session_id: str,
        image_type: ScreenshotType = "png",
        full_page: bool = False,
        quality: Optional[int] = None,
        wait: int | float = 0,
        wait_selector: Optional[str] = None,
        wait_selector_state: SelectorWaitStates = "attached",
        network_idle: bool = False,
        timeout: int | float = 30000,
    ) -> List[ImageContent | TextContent]:
        """Capture a screenshot of a web page using an existing browser session.
        A browser session must be opened first with `open_session`.

        :param url: The URL to navigate to and capture.
        :param session_id: ID of an open browser session.
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

        entry = await self._get_session(session_id, expected_type=None)
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
        """HTTP fetch with retry logic for transient network failures."""
        max_retries = 3
        base_delay = 1.0
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                return await self.get(url, **kwargs)
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
                useragent, cookies,
            )

        # Validate all inputs
        url, css_selector, extra_headers, timeout, proxy, useragent = \
            self._validate_smart_fetch_params(
                url, extraction_type, css_selector, extra_headers, timeout, proxy, useragent,
            )

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
            return _apply_chunking(disallowed)

        # 2. Check cache
        if cache_ttl > 0:
            cached = await get_cached(url, extraction_type, css_selector, ttl=cache_ttl)
            if cached is not None:
                return _apply_chunking(ResponseModel(
                    url=cached["url"], status=cached["status"], content=cached["content"],
                    cached=True, fetcher_used="cache", duration_ms=0,
                    extracted_type=extraction_type,
                ), offset=offset)

        # 3. Force specific fetcher
        if force_fetcher:
            return await self._force_fetch(
                url, force_fetcher, extraction_type, css_selector, main_content_only,
                use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
                proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
                hide_canvas, extra_headers, useragent, cookies,
            )

        # 4. Auto-escalation
        return await self._auto_escalate(
            url, extraction_type, css_selector, main_content_only,
            use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
            proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
            hide_canvas, extra_headers, useragent, cookies,
        )

    async def _smart_fetch_bulk(
        self, urls, extraction_type, css_selector, main_content_only,
        use_trafilatura, cache_ttl, force_fetcher, respect_robots,
        headless, real_chrome, wait, proxy, timeout, network_idle,
        solve_cloudflare, block_webrtc, hide_canvas, extra_headers,
        useragent, cookies,
    ) -> BulkResponseModel:
        """Fetch multiple URLs in parallel through the smart fetch pipeline."""
        if len(urls) > MAX_BULK_URLS:
            urls = urls[:MAX_BULK_URLS]

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
                )
            except Exception as e:
                return ResponseModel(
                    url=u, status=0, content=[f"[Error: {redact_api_key(str(e)[:200])}]"],
                    fetcher_used="none", error=redact_api_key(str(e)[:200]),
                )

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
        useragent, cookies,
    ) -> ResponseModel:
        """Execute a forced fetcher tier and finalize the result."""
        if force_fetcher == "http":
            http_cookies = _safe_cookie_dict(cookies)
            result = await self.get(
                url, extraction_type=extraction_type, css_selector=css_selector,
                main_content_only=main_content_only, use_trafilatura=use_trafilatura,
                proxy=proxy if isinstance(proxy, str) else None,
                headers=extra_headers, cookies=http_cookies, timeout=30,
                stealthy_headers=True,
            )
            result.escalation_path = "direct:http"
            await record_result(url, "none", result.status < 400, result.duration_ms)
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset)

        elif force_fetcher == "dynamic":
            # If stealthy is already alive, use it instead — Patchright handles
            # everything Playwright does. _acquire_stealthy_session atomically
            # checks, bumps the timestamp, and returns the session ID.
            ssid = await self._acquire_stealthy_session()
            if ssid:
                result = await self.stealthy_fetch(
                    url, extraction_type=extraction_type, css_selector=css_selector,
                    main_content_only=main_content_only, use_trafilatura=use_trafilatura,
                    headless=headless, real_chrome=real_chrome, wait=wait,
                    proxy=proxy, timeout=timeout, network_idle=network_idle,
                    disable_resources=True,
                    solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc,
                    hide_canvas=hide_canvas, extra_headers=extra_headers,
                    useragent=useragent, cookies=cookies,
                    session_id=ssid,
                )
                result.escalation_path = "direct:stealthy(consolidated)"
                # force_fetcher overrides auto-escalation; record what was used
                await record_result(url, "high", result.status < 400, result.duration_ms)
                return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset)
            dsid = await self._ensure_auto_session("dynamic")
            result = await self.fetch(
                url, extraction_type=extraction_type, css_selector=css_selector,
                main_content_only=main_content_only, use_trafilatura=use_trafilatura,
                headless=headless, real_chrome=real_chrome, wait=wait,
                proxy=proxy, timeout=timeout, network_idle=network_idle,
                disable_resources=True,
                extra_headers=extra_headers, useragent=useragent, cookies=cookies,
                session_id=dsid,
            )
            result.escalation_path = "direct:dynamic"
            await record_result(url, "low", result.status < 400, result.duration_ms)
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset)

        else:  # stealthy
            ssid = await self._ensure_auto_session("stealthy")
            # Stealthy handles everything dynamic does — close the dynamic
            # auto session to free memory (~100-180MB of Chrome RAM).
            # Stealthy handles everything dynamic does — close the dynamic
            # auto session in background to free ~100-180MB RAM. Fire-and-forget
            # so the fetch isn't blocked waiting for Chrome to shut down.
            asyncio.create_task(self._close_auto_dynamic_session())
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
            await record_result(url, "high", result.status < 400, result.duration_ms)
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset)

    async def _auto_escalate(
        self, url, extraction_type, css_selector, main_content_only,
        use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
        proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
        hide_canvas, extra_headers, useragent, cookies,
    ) -> ResponseModel:
        """Auto-escalation routing: try HTTP -> dynamic -> stealthy based on domain intel."""
        domain_level = await get_domain_level(url)
        start_time = now()

        # Phase A: Domain known to need stealthy. Skip straight to it.
        if domain_level == "high":
            ssid = await self._ensure_auto_session("stealthy")
            # Stealthy handles everything dynamic does — close any leftover
            # dynamic auto session in background to free ~100-180MB RAM.
            # Fire-and-forget so the fetch isn't blocked.
            asyncio.create_task(self._close_auto_dynamic_session())
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
            result.escalation_path = "direct:stealthy(auto)"
            elapsed = (now() - start_time) * 1000
            result.duration_ms = elapsed
            await record_result(url, "high", result.status < 400, elapsed)
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset)

        # Phase B: Domain needs dynamic. Try dynamic, escalate to stealthy if blocked.
        if domain_level == "low":
            # If stealthy already alive, skip dynamic — Patchright handles everything
            # Playwright does. _acquire_stealthy_session atomically checks, bumps
            # timestamp, and returns the session ID (no TOCTOU).
            ssid = await self._acquire_stealthy_session()
            if ssid:
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
                result.escalation_path = "stealthy(auto,consolidated)"
                elapsed = (now() - start_time) * 1000
                result.duration_ms = elapsed
                # Record "low" — skipped dynamic for resource consolidation,
                # not because it failed. The domain still belongs at "low".
                await record_result(url, "low", result.status < 400, elapsed)
                return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset)

            remaining = max(timeout - int((now() - start_time) * 1000), 5000)
            dsid = await self._ensure_auto_session("dynamic")
            result = await self.fetch(
                url, extraction_type=extraction_type,
                css_selector=css_selector, main_content_only=main_content_only,
                use_trafilatura=use_trafilatura, headless=headless,
                real_chrome=real_chrome, wait=wait, proxy=proxy,
                timeout=remaining, network_idle=network_idle,
                disable_resources=True,
                extra_headers=extra_headers, useragent=useragent, cookies=cookies,
                session_id=dsid,
            )
            elapsed = (now() - start_time) * 1000
            result.duration_ms = elapsed

            if result.status < 400 and not _is_js_shell(result):
                result.escalation_path = "direct:dynamic(auto)"
                await record_result(url, "low", True, elapsed)
                return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset)

            # Dynamic failed. Escalate to stealthy.
            remaining = max(timeout - int((now() - start_time) * 1000), 5000)
            ssid = await self._ensure_auto_session("stealthy")
            # Stealthy handles everything dynamic does — close the dynamic
            # auto session in background to free ~100-180MB RAM.
            # Fire-and-forget so the fetch isn't blocked.
            asyncio.create_task(self._close_auto_dynamic_session())
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
            result.escalation_path = "dynamic→stealthy"
            elapsed = (now() - start_time) * 1000
            result.duration_ms = elapsed
            await record_result(url, "high", result.status < 400, elapsed)
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset)

        # Phase C: Unknown domain. Try HTTP first; if it fails, jump straight
        # to stealthy. No reason to try dynamic in between — stealthy handles
        # everything dynamic does, and trying both leaves an orphan Chrome
        # process eating memory (~100-180MB).
        return await self._phase_c_unknown(
            url, extraction_type, css_selector, main_content_only,
            use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
            proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
            hide_canvas, extra_headers, useragent, cookies, start_time,
        )

    async def _phase_c_unknown(
        self, url, extraction_type, css_selector, main_content_only,
        use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
        proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
        hide_canvas, extra_headers, useragent, cookies, start_time,
    ) -> ResponseModel:
        """Phase C: Unknown domain. Try HTTP, escalate directly to stealthy.

        Skip dynamic (Playwright) entirely for unknown domains. Patchright handles
        everything Playwright can, and trying both sequentially leaves an orphan
        Chrome process eating memory. Two tiers: HTTP → Stealthy.
        """
        errors = []
        http_cookies = _safe_cookie_dict(cookies)

        # Tier 1: HTTP
        result = await self._http_with_retry(
            url, extraction_type=extraction_type,
            css_selector=css_selector, main_content_only=main_content_only,
            use_trafilatura=use_trafilatura,
            proxy=proxy if isinstance(proxy, str) else None,
            headers=extra_headers, cookies=http_cookies, stealthy_headers=True,
        )
        elapsed = (now() - start_time) * 1000
        result.duration_ms = elapsed

        # Accept if status is OK and content is real (not a JS shell).
        # Do NOT check _is_cloudflare_from_response on status < 400 — legitimate
        # articles about web security can contain "cloudflare" in body text.
        if result.status < 400 and not _is_js_shell(result):
            result.escalation_path = "direct:http"
            await record_result(url, "none", True, elapsed)
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset)

        # Decide whether to escalate. Two reasons:
        # 1. Status 200 with JS shell -> page needs a real browser
        # 2. Status 403 or 503 -> explicit bot block
        should_escalate = (result.status < 400 and _is_js_shell(result)) or result.status in (403, 503)
        if not should_escalate:
            await record_result(url, "none", False, elapsed)
            result.duration_ms = elapsed
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset)

        # Tier 2: Stealthy (skip dynamic — Patchright handles everything Playwright does,
        # and trying both sequentially wastes memory + time)
        errors.append(f"HTTP failed (status {result.status})")
        remaining = max(timeout - int((now() - start_time) * 1000), 5000)
        ssid = await self._ensure_auto_session("stealthy")
        # If a dynamic auto session exists from a prior call (e.g. Phase B),
        # close it in background — stealthy handles everything. Saves ~100-180MB RAM.
        # Fire-and-forget so the fetch isn't blocked.
        asyncio.create_task(self._close_auto_dynamic_session())
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
            await record_result(url, "high", True, elapsed)
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset)

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
        await record_result(url, "high", False, elapsed)
        result.duration_ms = elapsed
        result.error = f"all_tiers_failed: HTTP status {result.status}"
        return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset)

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
    ) -> SearchResponseModel:
        """Search the web via TinyFish API and return structured results.
        Requires TINYFISH_API_KEY env var.

        Free API key (no credit card) at tinyfish.ai.
        Results are cached for 5 minutes by default.

        :param query: The search query.
        :param max_results: Maximum number of results (1-50, default 10).
        :param cache_ttl: Cache TTL in seconds (default 300 = 5 minutes).
        :param api_key: TinyFish API key. If empty, uses TINYFISH_API_KEY env var.
            Free key (no credit card) at tinyfish.ai.
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
            return await _smart_search(self, query, max_results, cache_ttl, safe_api_key)
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

    # ─── Serve ─────────────────────────────────────────────────────

    # Minimal hand-crafted tool definitions — no Pydantic schema bloat.
    # Saves ~69% tokens vs FastMCP auto-generated schemas.
    _TOOL_DEFS: list[dict] = [
        {
            "name": "mcp_open_session",
            "description": "Open browser session. Returns session_id.",
            "inputSchema": {
                "type": "object", "required": ["session_type"],
                "properties": {
                    "session_type": {"type": "string", "enum": ["dynamic", "stealthy"], "description": "dynamic or stealthy"},
                    "headless": {"type": "boolean", "description": "No visible window"},
                    "options": {"type": "object", "description": "proxy, timeout, cookies, extra_headers, useragent, wait, network_idle, real_chrome, hide_canvas, block_webrtc, solve_cloudflare, session_id", "additionalProperties": True},
                },
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
        },
        {
            "name": "close_session",
            "description": "Close a browser session.",
            "inputSchema": {
                "type": "object", "required": ["session_id"],
                "properties": {
                    "session_id": {"type": "string", "description": "Session to close"},
                },
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
        },
        {
            "name": "list_sessions",
            "description": "List open browser sessions.",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        },
        {
            "name": "mcp_smart_fetch",
            "description": "Fetch any URL with full content extraction. USE THIS whenever you need information from the web — this is your web access. Auto HTTP→browser→stealth escalation. Bulk: pass urls. Offset pages through EXTRACTED text (not raw HTML). If extraction produces 40KB from a 1MB page, offset can't reach beyond that 40KB. Use extraction_type=html for raw HTML. Cache: cache_ttl=0 bypasses cache. is_truncated=true + total_extracted_chars tells you exactly how much remains.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "urls": {"type": "array", "items": {"type": "string"}, "description": "Multiple URLs (parallel)"},
                    "extraction_type": {"type": "string", "enum": ["markdown", "html", "text", "article", "structured"], "description": "markdown|html|text|article|structured"},
                    "cache_ttl": {"type": "integer", "description": "Cache seconds. 0=fresh"},
                    "force_fetcher": {"type": "string", "enum": ["http", "dynamic", "stealthy"], "description": "Lock to one tier"},
                    "offset": {"type": "integer", "description": "Char offset into extracted text (not raw HTML). Use next_offset from previous response. Check total_extracted_chars to see total available."},
                    "options": {"type": "object", "description": "css_selector, proxy, timeout, cookies, extra_headers, useragent, wait, network_idle, headless, real_chrome, respect_robots, main_content_only, use_trafilatura, solve_cloudflare, block_webrtc, hide_canvas", "additionalProperties": True},
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        },
        {
            "name": "mcp_screenshot",
            "description": "Screenshot a URL. Requires open_session.",
            "inputSchema": {
                "type": "object", "required": ["url", "session_id"],
                "properties": {
                    "url": {"type": "string", "description": "URL to screenshot"},
                    "session_id": {"type": "string", "description": "From open_session"},
                    "options": {"type": "object", "description": "full_page, image_type, quality, wait, wait_selector, network_idle, timeout", "additionalProperties": True},
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        },
        {
            "name": "mcp_smart_search",
            "description": "Web search via TinyFish. Returns URLs with short descriptions. Search finds links — descriptions are NOT enough to answer questions. ALWAYS fetch the result URL with smart_fetch for full content. Free key at tinyfish.ai. Results cached 5min (cache_ttl to adjust).",
            "inputSchema": {
                "type": "object", "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "options": {"type": "object", "description": "max_results, cache_ttl, api_key", "additionalProperties": True},
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        },
        {
            "name": "cache_clear",
            "description": "Clear fetch cache. all=true wipes all. Cache stores extracted text per URL+extraction_type. Default TTL: 1hr.",
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
                async with stdio_server() as (read, write):
                    await server.run(read, write, server.create_initialization_options())

            anyio.run(_run)
        else:
            from mcp.server.sse import SseServerTransport
            from starlette.applications import Starlette
            from starlette.routing import Route

            sse = SseServerTransport("/messages/")

            async def handle_sse(request):
                async with sse.connect_sse(request.scope, request.receive, request._send) as (read, write):
                    await server.run(read, write, server.create_initialization_options())

            app = Starlette(routes=[Route("/sse", endpoint=handle_sse), Route("/messages/", endpoint=sse.handle_post_message)])
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
            kw = {k: v for k, v in options.items() if k in (
                "css_selector", "proxy", "timeout", "cookies", "extra_headers", "useragent",
                "wait", "network_idle", "headless", "real_chrome", "respect_robots",
                "main_content_only", "use_trafilatura", "solve_cloudflare", "block_webrtc", "hide_canvas",
            )}
            result = await self.smart_fetch(
                url=url, urls=urls,
                extraction_type=args.get("extraction_type", "markdown"),
                cache_ttl=args.get("cache_ttl", DEFAULT_TTL),
                force_fetcher=args.get("force_fetcher"),
                offset=args.get("offset", 0), **kw,
            )
            return [TextContent(type="text", text=result.model_dump_json())], result.model_dump()

        elif name == "mcp_open_session":
            kw = {k: v for k, v in options.items() if k in (
                "proxy", "timeout", "cookies", "extra_headers", "useragent",
                "wait", "network_idle", "real_chrome", "hide_canvas", "block_webrtc",
                "solve_cloudflare", "session_id",
            )}
            result = await self.open_session(
                session_type=args["session_type"], headless=args.get("headless", True), **kw,
            )
            return [TextContent(type="text", text=result.model_dump_json())], result.model_dump()

        elif name == "close_session":
            result = await self.close_session(session_id=args["session_id"])
            return [TextContent(type="text", text=result.model_dump_json())], result.model_dump()

        elif name == "list_sessions":
            result = await self.list_sessions()
            data = {"sessions": [r.model_dump() for r in result]}
            return [TextContent(type="text", text=json.dumps(data))], data

        elif name == "mcp_screenshot":
            kw = {k: v for k, v in options.items() if k in (
                "full_page", "image_type", "quality", "wait", "wait_selector", "network_idle", "timeout",
            )}
            result = await self.screenshot(url=args["url"], session_id=args["session_id"], **kw)
            return result  # already list[ImageContent|TextContent]

        elif name == "mcp_smart_search":
            kw = {k: v for k, v in options.items() if k in ("max_results", "cache_ttl", "api_key")}
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


def _do_update():
    """Update hound-mcp via pip with clean output."""
    import subprocess
    installed, latest, is_current = _check_version()

    if not latest:
        print(f"Hound v{installed}: can't check for updates.")
        return

    try:
        if _pad_version(installed) >= _pad_version(latest):
            print(f"Hound v{installed} (latest)")
            return
    except (ValueError, IndexError):
        pass

    print(f"Updating v{installed} to v{latest}...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "hound-mcp[all]",
         "-qq", "--no-cache-dir", "--disable-pip-version-check", "--no-python-version-warning"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        for line in err.split("\n"):
            if "ERROR" in line or "error" in line.lower():
                print(f"  {line.strip()}")
                break
        else:
            print(f"  {err.split(chr(10))[-1] if err else 'failed'}")
        sys.exit(1)

    new_ver = _check_version()[0]
    print(f"Hound v{new_ver}")


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

    if args.update:
        _do_update()
        return

    if args.version:
        installed, latest, is_current = _check_version()
        if latest and is_current:
            print(f"Hound v{installed} (latest)")
        elif latest:
            try:
                if _pad_version(installed) >= _pad_version(latest):
                    print(f"Hound v{installed} (latest)")
                else:
                    print(f"Hound v{installed}. v{latest} available. Run hound -u to update.")
            except (ValueError, IndexError):
                print(f"Hound v{installed}")
        else:
            print(f"Hound v{installed}")
        return

    srv = MasterFetchServer(cache_ttl=args.cache_ttl)
    srv.serve(http=args.http, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

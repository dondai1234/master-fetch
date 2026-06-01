"""Master Fetch MCP Server.

Forks Scrapling's built-in MCP server and adds:
- Trafilatura article extraction (cleaner than markdownify)
- Smart fetch routing (auto-escalate HTTP → Dynamic → Stealthy)
- SQLite content cache with TTL
- Domain intelligence (remember which sites need stealth)
- smart_fetch umbrella tool (single entry point that routes automatically)
- extract_article and extract_structured modes
"""
import logging
from uuid import uuid4
from asyncio import gather, sleep as asyncio_sleep
from datetime import datetime, timezone
from time import time as now
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger("master-fetch.server")

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ImageContent, TextContent
from pydantic import BaseModel, Field

from scrapling.core.shell import Convertor
from scrapling.engines.toolbelt.custom import Response as _ScraplingResponse
from scrapling.engines.static import ImpersonateType
from scrapling.fetchers import (
    FetcherSession,
    AsyncDynamicSession,
    AsyncStealthySession,
)
from scrapling.core._types import (
    Optional, Literal, Union, Tuple, Mapping,
    Dict, List, Any, SetCookieParam,
    extraction_types, SelectorWaitStates, FollowRedirects,
)

from master_fetch.cache import get_cached, set_cached, clear_cache, clear_all_cache, DEFAULT_TTL
from master_fetch.domain_intel import get_domain_level, record_result, guess_protection_level
from master_fetch.trafilatura_extractor import extract_with_trafilatura
from master_fetch.robots import is_allowed, clear_robots_cache
from master_fetch.search import SearchResponseModel

# Extended extraction types (beyond Scrapling's markdown/html/text)
ExtendedExtractionType = Literal["markdown", "html", "text", "article", "structured"]
SessionType = Literal["dynamic", "stealthy"]
ScreenshotType = Literal["png", "jpeg"]

MAX_CONTENT_CHARS = 40000


class ResponseModel(BaseModel):
    """Request's response information structure."""
    status: int = Field(description="The status code returned by the website.")
    content: list[str] = Field(description="The content as Markdown/HTML/text/article/structured JSON.")
    url: str = Field(description="The URL given by the user that resulted in this response.")
    cached: bool = Field(default=False, description="Whether this response was served from cache.")
    fetcher_used: str = Field(default="", description="Which fetcher was used: 'http', 'dynamic', or 'stealthy'.")
    extracted_type: str = Field(default="markdown", description="The extraction type used.")
    session_id: str = Field(default="", description="Browser session ID used for this request.")
    duration_ms: float = Field(default=0, description="Request duration in milliseconds.")
    error: str = Field(default="", description="Error message if the request failed.")


class BulkResponseModel(BaseModel):
    """Response from bulk fetch operations, one result per URL."""
    results: list[ResponseModel] = Field(description="Individual results for each URL.")
    total: int = Field(description="Total number of URLs fetched.")
    successful: int = Field(description="Number of successful fetches (status < 400).")


class ArticleModel(BaseModel):
    """Structured article data extracted by Trafilatura."""
    title: str = Field(description="Article title.")
    author: str = Field(description="Article author.")
    date: str = Field(description="Publication date.")
    body: str = Field(description="Main article text content.")
    description: str = Field(description="Article description/summary.")
    url: str = Field(description="Source URL.")
    categories: list[str] = Field(default=[], description="Categories.")
    tags: list[str] = Field(default=[], description="Tags.")


class SessionInfo(BaseModel):
    """Information about an open browser session."""
    session_id: str = Field(description="The unique identifier of the session.")
    session_type: SessionType = Field(description="The type of the session: 'dynamic' or 'stealthy'.")
    created_at: str = Field(description="ISO timestamp of when the session was created.")
    is_alive: bool = Field(description="Whether the session is still alive and usable.")


class SessionCreatedModel(SessionInfo):
    """Response returned when a new session is created."""
    message: str = Field(description="A confirmation message.")


class SessionClosedModel(BaseModel):
    """Response returned when a session is closed."""
    session_id: str = Field(description="The unique identifier of the closed session.")
    message: str = Field(description="A confirmation message.")


class CacheInfoModel(BaseModel):
    """Response from cache management operations."""
    message: str = Field(description="Result message.")
    purged: int = Field(default=0, description="Number of entries purged.")


@dataclass
class _SessionEntry:
    session: Any  # AsyncDynamicSession | AsyncStealthySession
    session_type: SessionType
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    _alive: bool = True  # True from creation; set False only on explicit close or crash


def _annotate_quality(result: ResponseModel) -> ResponseModel:
    """Check content quality and set error field if issues detected. Returns same result."""
    if not result.error:  # Don't overwrite existing errors
        issue = _detect_content_issue(result)
        if issue:
            result.error = issue
    return result


def _apply_chunking(result: ResponseModel, max_chars: int = MAX_CONTENT_CHARS) -> ResponseModel:
    """Truncate content if it exceeds max_chars. Adds continuation info."""
    total = sum(len(c) for c in result.content)
    if total <= max_chars:
        return result
    truncated = []
    budget = max_chars
    for c in result.content:
        if budget <= 0:
            break
        truncated.append(c[:budget])
        budget -= len(truncated[-1])
    if truncated:
        remaining = total - max_chars
        truncated[-1] += f"\n\n[Content truncated: {remaining:,} more chars available. Re-fetch with offset parameter to continue.]"
    return ResponseModel(
        status=result.status, content=truncated, url=result.url,
        cached=result.cached, fetcher_used=result.fetcher_used,
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
    """
    content: list[str]
    if use_trafilatura and extraction_type in ("markdown", "text", "article", "structured"):
        content = extract_with_trafilatura(page, extraction_type=extraction_type, css_selector=css_selector)
        # If Trafilatura returned empty content, fall back to Scrapling
        if not content or content == [""] or content == ["\n"]:
            content = list(
                Convertor._extract_content(
                    page,
                    css_selector=css_selector,
                    extraction_type=extraction_type if extraction_type in ("markdown", "html", "text") else "markdown",
                    main_content_only=main_content_only,
                )
            )
    else:
        content = list(
            Convertor._extract_content(
                page,
                css_selector=css_selector,
                extraction_type=extraction_type if extraction_type in ("markdown", "html", "text") else "markdown",
                main_content_only=main_content_only,
            )
        )
    # Add user-friendly note for stealthy 503 (simple test servers block headless browsers)
    if page.status == 503 and fetcher_used == "stealthy":
        note = "[503 via stealthy fetcher. The target server may block headless browser fingerprints. Try smart_fetch or http/dynamic fetcher instead.]"
        content = [note]
    return ResponseModel(status=page.status, content=content, url=page.url, fetcher_used=fetcher_used, duration_ms=duration_ms)


async def _timed(coro):
    """Run a coroutine and return (result, elapsed_ms)."""
    t0 = now()
    result = await coro
    elapsed = (now() - t0) * 1000
    return result, elapsed


def _normalize_credentials(credentials: Optional[Dict[str, str]]) -> Optional[Tuple[str, str]]:
    """Convert a credentials dictionary to a tuple accepted by fetchers."""
    if not credentials:
        return None
    username = credentials.get("username")
    password = credentials.get("password")
    if username is None or password is None:
        raise ValueError("Credentials dictionary must contain both 'username' and 'password' keys")
    return username, password


def _is_cloudflare_challenge(page: _ScraplingResponse) -> bool:
    """Detect if a response is a bot challenge page (Cloudflare, DataDome, etc.)."""
    if page.status in (403, 503):
        try:
            body = page.body.decode(page.encoding or 'utf-8', errors='replace').lower()
            signals = [
                'cloudflare', 'cf-browser', 'challenge-platform', 'cf_chl_opt',
                'captcha-delivery.com', 'datadome', 'dd=',
                'please verify you are a human', 'are you a robot',
            ]
            return any(s in body for s in signals)
        except Exception:
            pass
    return False


class MasterFetchServer:
    """Enhanced MCP server built on Scrapling with smart routing, caching, and Trafilatura."""

    def __init__(self, cache_ttl: int = DEFAULT_TTL, use_trafilatura: bool = True):
        self._sessions: Dict[str, _SessionEntry] = {}
        self._cache_ttl = cache_ttl
        self._use_trafilatura = use_trafilatura
        self._auto_dynamic_id: Optional[str] = None
        self._auto_stealthy_id: Optional[str] = None

    def _get_session(self, session_id: str, expected_type: Optional[SessionType]) -> _SessionEntry:
        """Look up a session by ID, optionally validating its type."""
        entry = self._sessions.get(session_id)
        if entry is None:
            raise ValueError(f"Session '{session_id}' not found. Use list_sessions to see active sessions.")
        if not entry.session._is_alive:
            raise ValueError(f"Session '{session_id}' is no longer alive. Open a new session.")
        if expected_type is not None and entry.session_type != expected_type:
            raise ValueError(
                f"Session '{session_id}' is a '{entry.session_type}' session, but this tool requires a "
                f"'{expected_type}' session. Use the matching fetch tool for your session type."
            )
        return entry

    async def _ensure_auto_session(self, session_type: SessionType) -> str:
        """Get or create an auto-persistent browser session. Avoids browser startup on every fetch."""
        attr = "_auto_dynamic_id" if session_type == "dynamic" else "_auto_stealthy_id"
        existing_id = getattr(self, attr)
        if existing_id and existing_id in self._sessions and self._sessions[existing_id].session._is_alive:
            return existing_id
        sid = await self.open_session(session_type=session_type, headless=True)
        setattr(self, attr, sid.session_id)
        return sid.session_id

    # ─── Session Management ──────────────────────────────────────────

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
        Use close_session to close the session when done, and list_sessions to see all active sessions.

        :param session_type: The type of session to open. Use "dynamic" for standard Playwright browser, or "stealthy" for anti-bot bypass with fingerprint spoofing.
        :param session_id: Optional custom session ID. If not provided, a random 12-character hex ID will be generated.
        :param headless: Run the browser in headless/hidden (default), or headful/visible mode.
        :param google_search: Enabled by default, Scrapling will set a Google referer header.
        :param real_chrome: If you have a Chrome browser installed, enable this to use your real browser.
        :param wait: Time (milliseconds) to wait after everything finishes.
        :param proxy: Proxy to use: string or dict with 'server', 'username', 'password'.
        :param timezone_id: Change browser timezone.
        :param locale: User locale, e.g., 'en-GB'.
        :param extra_headers: Extra headers to add to requests.
        :param useragent: Custom user agent string.
        :param cdp_url: Connect to browser via CDP URL instead of launching new.
        :param timeout: Timeout in milliseconds (default 30000).
        :param disable_resources: Drop font/image/media/stylesheet requests for speed.
        :param wait_selector: CSS selector to wait for before proceeding.
        :param cookies: Set cookies for the session.
        :param network_idle: Wait until no network connections for 500ms.
        :param wait_selector_state: State to wait for: 'attached', 'detached', 'visible', 'hidden'.
        :param max_pages: Max concurrent browser tabs (default 5).
        :param hide_canvas: (Stealthy only) Random noise on canvas to prevent fingerprinting.
        :param block_webrtc: (Stealthy only) Prevent IP leak via WebRTC.
        :param allow_webgl: (Stealthy only) Keep WebGL enabled (default True; WAFs check for it).
        :param solve_cloudflare: (Stealthy only) Auto-solve Cloudflare challenges.
        :param additional_args: (Stealthy only) Extra Playwright context args.
        """
        session_id = session_id or uuid4().hex[:12]
        if session_id in self._sessions:
            raise ValueError(f"Session '{session_id}' already exists. Use a different ID or close the existing one.")

        common_kwargs: Dict[str, Any] = dict(
            wait=wait, proxy=proxy, locale=locale, timeout=timeout, cookies=cookies,
            cdp_url=cdp_url, headless=headless, block_ads=True, max_pages=max_pages,
            useragent=useragent, timezone_id=timezone_id, real_chrome=real_chrome,
            network_idle=network_idle, wait_selector=wait_selector, google_search=google_search,
            extra_headers=extra_headers, disable_resources=disable_resources,
            wait_selector_state=wait_selector_state,
        )

        session: Union[AsyncDynamicSession, AsyncStealthySession]
        if session_type == "stealthy":
            session = AsyncStealthySession(
                **common_kwargs, hide_canvas=hide_canvas, block_webrtc=block_webrtc,
                allow_webgl=allow_webgl, solve_cloudflare=solve_cloudflare,
                additional_args=additional_args,
            )
        else:
            session = AsyncDynamicSession(**common_kwargs)

        # Add entry to sessions dict BEFORE start() so it's visible immediately
        # (fixes race: list_sessions called right after open_session)
        entry = _SessionEntry(session=session, session_type=session_type)
        self._sessions[session_id] = entry
        try:
            await session.start()
        except Exception:
            entry._alive = False
            self._sessions.pop(session_id, None)  # Clean up on failure
            raise

        return SessionCreatedModel(
            session_id=session_id, session_type=session_type,
            created_at=entry.created_at, is_alive=True,
            message=f"Session '{session_id}' ({session_type}) created successfully.",
        )

    async def close_session(self, session_id: str) -> SessionClosedModel:
        """Close a persistent browser session and free its resources.

        :param session_id: The unique identifier of the session to close.
        """
        entry = self._sessions.pop(session_id, None)
        if entry is None:
            raise ValueError(f"Session '{session_id}' not found.")
        await entry.session.close()
        return SessionClosedModel(session_id=session_id, message=f"Session '{session_id}' closed successfully.")

    async def list_sessions(self) -> List[SessionInfo]:
        """List all active browser sessions with their details."""
        return [
            SessionInfo(
                session_id=sid, session_type=entry.session_type,
                created_at=entry.created_at, is_alive=entry._alive,
            )
            for sid, entry in self._sessions.items()
        ]

    # ─── Screenshot ──────────────────────────────────────────────────

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
        if quality is not None and image_type != "jpeg":
            raise ValueError("'quality' is only valid when 'image_type' is 'jpeg'.")
        entry = self._get_session(session_id, expected_type=None)
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

    # ─── HTTP Fetcher (curl_cffi) ────────────────────────────────────

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
        Fast, but only works for low-protection sites. For protected sites, use smart_fetch or stealthy_fetch.

        :param url: The URL to request.
        :param impersonate: Browser to impersonate (default 'chrome').
        :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
        :param css_selector: CSS selector to narrow content before extraction.
        :param main_content_only: Strip nav/ads/footers (default True).
        :param use_trafilatura: Use Trafilatura for article extraction (default True, cleaner output).
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
        max_content_chars: int = MAX_CONTENT_CHARS,
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
        :param max_content_chars: Max chars per result before truncation (default 40000). Prevents huge bulk output.
        """
        normalized_proxy_auth = _normalize_credentials(proxy_auth)
        normalized_auth = _normalize_credentials(auth)
        use_tf = use_trafilatura and extraction_type in ("markdown", "text", "article", "structured")

        async with FetcherSession() as session:
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
            timed_responses = await gather(*timed_tasks)
            results = [
                _apply_chunking(
                    _annotate_quality(
                        _translate_response(page, extraction_type, css_selector, main_content_only, use_tf, "http", elapsed)
                    ),
                    max_chars=max_content_chars,
                )
                for page, elapsed in timed_responses
            ]
            successful = sum(1 for r in results if r.status < 400 and not r.error)
            return BulkResponseModel(results=results, total=len(results), successful=successful)

    # ─── Dynamic Fetcher (Playwright) ────────────────────────────────

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
        max_content_chars: int = MAX_CONTENT_CHARS,
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
        :param max_content_chars: Max chars per result before truncation (default 40000). Prevents huge bulk output.
        """
        use_tf = use_trafilatura and extraction_type in ("markdown", "text", "article", "structured")

        if session_id:
            entry = self._get_session(session_id, "dynamic")
            timed_tasks = [
                _timed(entry.session.fetch(
                    url, wait=wait, timeout=timeout, google_search=google_search,
                    extra_headers=extra_headers, disable_resources=disable_resources,
                    wait_selector=wait_selector, wait_selector_state=wait_selector_state,
                    network_idle=network_idle, proxy=proxy,
                ))
                for url in urls
            ]
            timed_responses = await gather(*timed_tasks)
        else:
            async with AsyncDynamicSession(
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
                timed_responses = await gather(*timed_tasks)

        results = [_apply_chunking(_annotate_quality(_translate_response(page, extraction_type, css_selector, main_content_only, use_tf, "dynamic", elapsed)), max_chars=max_content_chars) for page, elapsed in timed_responses]
        successful = sum(1 for r in results if r.status < 400 and not r.error)
        return BulkResponseModel(results=results, total=len(results), successful=successful)

    # ─── Stealthy Fetcher (Patchright) ───────────────────────────────

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
        """Stealthy fetcher with anti-bot bypass. Handles Cloudflare, DataDome, Akamai, etc.
        THE tool for high-protection sites. Auto-solves Cloudflare challenges.

        :param url: The URL to fetch.
        :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
        :param css_selector: CSS selector to narrow content.
        :param main_content_only: Strip nav/ads/footers (default True).
        :param use_trafilatura: Use Trafilatura for article extraction (default True).
        :param headless: Run browser in headless mode (default True).
        :param solve_cloudflare: Auto-solve Cloudflare challenges (default False; enable for CF sites).
        :param block_webrtc: Prevent IP leak via WebRTC.
        :param hide_canvas: Random noise on canvas to prevent fingerprinting.
        :param allow_webgl: Keep WebGL enabled (default True; WAFs check for it).
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
        :param additional_args: Extra Playwright context args.
        :param session_id: Reuse existing browser session.
        """
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
        max_content_chars: int = MAX_CONTENT_CHARS,
    ) -> BulkResponseModel:
        """Async parallel stealthy fetch with anti-bot bypass.

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
        :param max_content_chars: Max chars per result before truncation (default 40000). Prevents huge bulk output.
        """
        use_tf = use_trafilatura and extraction_type in ("markdown", "text", "article", "structured")

        if session_id:
            entry = self._get_session(session_id, "stealthy")
            timed_tasks = [
                _timed(entry.session.fetch(
                    url, wait=wait, timeout=timeout, google_search=google_search,
                    extra_headers=extra_headers, disable_resources=disable_resources,
                    wait_selector=wait_selector, wait_selector_state=wait_selector_state,
                    network_idle=network_idle, proxy=proxy, solve_cloudflare=solve_cloudflare,
                ))
                for url in urls
            ]
            timed_responses = await gather(*timed_tasks)
        else:
            async with AsyncStealthySession(
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
                timed_responses = await gather(*timed_tasks)

        results = [_apply_chunking(_annotate_quality(_translate_response(page, extraction_type, css_selector, main_content_only, use_tf, "stealthy", elapsed)), max_chars=max_content_chars) for page, elapsed in timed_responses]
        successful = sum(1 for r in results if r.status < 400 and not r.error)
        return BulkResponseModel(results=results, total=len(results), successful=successful)

    # ─── SMART FETCH (The One Tool To Rule Them All) ──────────────────

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
                    logger.warning(f"HTTP fetch attempt {attempt + 1} failed for {url}: {e}. Retrying in {delay:.0f}s...")
                    await asyncio_sleep(delay)
                else:
                    logger.error(f"HTTP fetch failed after {max_retries + 1} attempts for {url}: {e}")
        return ResponseModel(
            url=url,
            content=[f"[Network error] Failed to fetch {url} after {max_retries + 1} attempts: {last_error}"],
            status=0, fetcher_used="none", cached=False,
            extracted_type=kwargs.get("extraction_type", "markdown"),
            session_id="", duration_ms=0, error=str(last_error)
        )

    async def smart_fetch(
        self,
        url: str,
        extraction_type: ExtendedExtractionType = "markdown",
        css_selector: Optional[str] = None,
        main_content_only: bool = True,
        use_trafilatura: bool = True,
        cache_ttl: int = DEFAULT_TTL,
        force_fetcher: Optional[Literal["http", "dynamic", "stealthy"]] = None,
        respect_robots: bool = False,
        headless: bool = True,
        real_chrome: bool = False,
        wait: int | float = 0,
        proxy: Optional[str | Dict[str, str]] = None,
        timeout: int | float = 30000,
        network_idle: bool = False,
        solve_cloudflare: bool = True,
        block_webrtc: bool = True,
        hide_canvas: bool = True,
        extra_headers: Optional[Dict[str, str]] = None,
        useragent: Optional[str] = None,
        cookies: Sequence[SetCookieParam] | None = None,
    ) -> ResponseModel:
        """THE ONE TOOL TO RULE THEM ALL. Automatically picks the best fetcher for the URL.

        How it works:
        1. Check cache: if URL was fetched within cache_ttl seconds, return cached result
        2. Check domain intelligence: do we know this domain needs stealth?
        3. If force_fetcher is set, use that fetcher directly
        4. Otherwise, try HTTP first (fastest). If blocked/403/503, escalate to dynamic.
        5. If dynamic also fails, escalate to stealthy (full anti-bot bypass).
        6. Cache successful results and record domain intelligence for future routing.

        This is the tool you want 95% of the time. Use the specific fetchers only when you
        need fine-grained control over browser settings.

        :param url: The URL to fetch.
        :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
        :param css_selector: CSS selector to narrow content.
        :param main_content_only: Strip nav/ads/footers (default True).
        :param use_trafilatura: Use Trafilatura for cleaner article extraction (default True).
        :param cache_ttl: Cache TTL in seconds. Default 3600 (1 hour). Set to 0 to disable.
        :param force_fetcher: Force a specific fetcher: 'http', 'dynamic', or 'stealthy'. Skip auto-routing.
        :param headless: Run browser in headless mode (default True).
        :param real_chrome: Use installed Chrome instead of Chromium.
        :param wait: Milliseconds to wait after page load.
        :param proxy: Proxy to use.
        :param timeout: Timeout in milliseconds (default 30000) for browser fetchers, seconds for HTTP.
        :param network_idle: Wait for no network connections for 500ms.
        :param solve_cloudflare: Auto-solve Cloudflare when escalating to stealthy (default True).
        :param block_webrtc: Block WebRTC IP leak in stealthy mode (default True).
        :param hide_canvas: Hide canvas fingerprint in stealthy mode (default True).
        :param extra_headers: Extra request headers.
        :param useragent: Custom user agent.
        :param cookies: Cookies to set.
        """
        # 1. Check robots.txt compliance
        if respect_robots and not is_allowed(url):
            disallowed = ResponseModel(
                url=url,
                content=[f"[Blocked by robots.txt] The URL '{url}' is disallowed by the site's robots.txt policy. Set respect_robots=False to bypass."],
                status=403, fetcher_used="none", cached=False,
                extracted_type=extraction_type, session_id="",
                duration_ms=0, error="robots_txt_disallowed"
            )
            return _apply_chunking(disallowed)

        # 2. Check cache
        if cache_ttl > 0:
            cached = await get_cached(url, extraction_type, css_selector, ttl=cache_ttl)
            if cached is not None:
                return _apply_chunking(ResponseModel(
                    url=cached["url"], status=cached["status"], content=cached["content"],
                    cached=True, fetcher_used="cache", duration_ms=0,
                ))

        # 2. Force specific fetcher
        if force_fetcher == "http":
            _http_cookies = {c["name"]: c["value"] for c in cookies} if cookies else None
            result = await self.get(url, extraction_type=extraction_type, css_selector=css_selector,
                main_content_only=main_content_only, use_trafilatura=use_trafilatura,
                proxy=proxy if isinstance(proxy, str) else None,
                headers=extra_headers, cookies=_http_cookies, timeout=30,
                stealthy_headers=True)
            await record_result(url, "none", True)
            result = _annotate_quality(result)
            if cache_ttl > 0:
                await set_cached(url, extraction_type, result.content, result.status, css_selector, cache_ttl)
            return _apply_chunking(result)

        if force_fetcher == "dynamic":
            dsid = await self._ensure_auto_session("dynamic")
            result = await self.fetch(url, extraction_type=extraction_type, css_selector=css_selector,
                main_content_only=main_content_only, use_trafilatura=use_trafilatura,
                headless=headless, real_chrome=real_chrome, wait=wait,
                proxy=proxy, timeout=timeout, network_idle=network_idle,
                disable_resources=True,
                extra_headers=extra_headers, useragent=useragent, cookies=cookies,
                session_id=dsid)
            await record_result(url, "low", True)
            result = _annotate_quality(result)
            if cache_ttl > 0:
                await set_cached(url, extraction_type, result.content, result.status, css_selector, cache_ttl)
            return _apply_chunking(result)

        if force_fetcher == "stealthy":
            ssid = await self._ensure_auto_session("stealthy")
            result = await self.stealthy_fetch(url, extraction_type=extraction_type,
                css_selector=css_selector, main_content_only=main_content_only,
                use_trafilatura=use_trafilatura, headless=headless,
                real_chrome=real_chrome, wait=wait, proxy=proxy,
                timeout=timeout, network_idle=network_idle,
                disable_resources=True,
                solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc,
                hide_canvas=hide_canvas, extra_headers=extra_headers,
                useragent=useragent, cookies=cookies,
                session_id=ssid)
            await record_result(url, "high", True)
            result = _annotate_quality(result)
            if cache_ttl > 0:
                await set_cached(url, extraction_type, result.content, result.status, css_selector, cache_ttl)
            return _apply_chunking(result)

        # 3. Auto-escalation logic
        domain_level = await get_domain_level(url)
        start_time = now()

        # Phase A: If domain is known to need stealthy, skip to stealthy
        if domain_level == "high":
            ssid = await self._ensure_auto_session("stealthy")
            result = await self.stealthy_fetch(url, extraction_type=extraction_type,
                css_selector=css_selector, main_content_only=main_content_only,
                use_trafilatura=use_trafilatura, headless=headless,
                real_chrome=real_chrome, wait=wait, proxy=proxy,
                timeout=timeout, network_idle=network_idle,
                disable_resources=True,
                solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc,
                hide_canvas=hide_canvas, extra_headers=extra_headers,
                useragent=useragent, cookies=cookies,
                session_id=ssid)
            elapsed = (now() - start_time) * 1000
            result.duration_ms = elapsed
            await record_result(url, "high", result.status < 400, elapsed)
            result = _annotate_quality(result)
            if cache_ttl > 0:
                await set_cached(url, extraction_type, result.content, result.status, css_selector, cache_ttl)
            return _apply_chunking(result)

        # Phase B: If domain needs dynamic, try dynamic then escalate
        if domain_level == "low":
            dsid = await self._ensure_auto_session("dynamic")
            result = await self.fetch(url, extraction_type=extraction_type,
                css_selector=css_selector, main_content_only=main_content_only,
                use_trafilatura=use_trafilatura, headless=headless,
                real_chrome=real_chrome, wait=wait, proxy=proxy,
                timeout=timeout, network_idle=network_idle,
                disable_resources=True,
                extra_headers=extra_headers, useragent=useragent, cookies=cookies,
                session_id=dsid)
            if result.status < 400 and not _is_cloudflare_from_response(result):
                if _is_js_shell(result):
                    # Dynamic returned JS-disabled placeholder: escalate to stealthy
                    logger.info(f"Dynamic returned JS shell for {url}, escalating to stealthy")
                else:
                    elapsed = (now() - start_time) * 1000
                    result.duration_ms = elapsed
                    await record_result(url, "low", True, elapsed)
                    result = _annotate_quality(result)
                    if cache_ttl > 0:
                        await set_cached(url, extraction_type, result.content, result.status, css_selector, cache_ttl)
                    return _apply_chunking(result)
            # Escalate to stealthy
            ssid = await self._ensure_auto_session("stealthy")
            result = await self.stealthy_fetch(url, extraction_type=extraction_type,
                css_selector=css_selector, main_content_only=main_content_only,
                use_trafilatura=use_trafilatura, headless=headless,
                real_chrome=real_chrome, wait=wait, proxy=proxy,
                timeout=timeout, network_idle=network_idle,
                disable_resources=True,
                solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc,
                hide_canvas=hide_canvas, extra_headers=extra_headers,
                useragent=useragent, cookies=cookies,
                session_id=ssid)
            elapsed = (now() - start_time) * 1000
            result.duration_ms = elapsed
            await record_result(url, "high", result.status < 400, elapsed)
            result = _annotate_quality(result)
            if cache_ttl > 0:
                await set_cached(url, extraction_type, result.content, result.status, css_selector, cache_ttl)
            return _apply_chunking(result)

        # Phase C: Unknown domain: try HTTP first (fastest), then escalate
        errors = []  # Collect errors for better diagnostics
        _http_cookies = {c["name"]: c["value"] for c in cookies} if cookies else None

        result = await self._http_with_retry(url, extraction_type=extraction_type,
            css_selector=css_selector, main_content_only=main_content_only,
            use_trafilatura=use_trafilatura,
            proxy=proxy if isinstance(proxy, str) else None,
            headers=extra_headers, cookies=_http_cookies, stealthy_headers=True)
        elapsed = (now() - start_time) * 1000
        result.duration_ms = elapsed

        # Success with HTTP: check if content is a JS-only shell
        if result.status < 400:
            if _is_js_shell(result):
                # JS-only placeholder: escalate to dynamic fetcher
                logger.info(f"HTTP returned JS shell for {url}, escalating to dynamic")
            else:
                # Real content: done, fastest path
                await record_result(url, "none", True, elapsed)
                result = _annotate_quality(result)
                if cache_ttl > 0:
                    await set_cached(url, extraction_type, result.content, result.status, css_selector, cache_ttl)
                return _apply_chunking(result)

        # Failed with HTTP: check if it's a bot challenge or just an error page
        if not _is_cloudflare_from_response(result):
            # Not a bot challenge. Don't waste time escalating. Return the error.
            await record_result(url, "none", False, elapsed)
            result.duration_ms = elapsed
            result = _annotate_quality(result)
            if cache_ttl > 0:
                await set_cached(url, extraction_type, result.content, result.status, css_selector, cache_ttl)
            return _apply_chunking(result)

        # Bot challenge detected: try dynamic
        errors.append(f"HTTP tier blocked (status {result.status})")
        dsid = await self._ensure_auto_session("dynamic")
        result = await self.fetch(url, extraction_type=extraction_type,
            css_selector=css_selector, main_content_only=main_content_only,
            use_trafilatura=use_trafilatura, headless=headless,
            real_chrome=real_chrome, wait=wait, proxy=proxy,
            timeout=timeout, network_idle=network_idle,
            disable_resources=True,
            extra_headers=extra_headers, useragent=useragent, cookies=cookies,
            session_id=dsid)
        elapsed = (now() - start_time) * 1000
        result.duration_ms = elapsed

        if result.status < 400 and not _is_cloudflare_from_response(result):
            if _is_js_shell(result):
                # Dynamic returned JS-disabled placeholder: escalate to stealthy
                logger.info(f"Dynamic returned JS shell for {url}, escalating to stealthy")
            else:
                await record_result(url, "low", True, elapsed)
                result = _annotate_quality(result)
                if cache_ttl > 0:
                    await set_cached(url, extraction_type, result.content, result.status, css_selector, cache_ttl)
                return _apply_chunking(result)

        # Failed with dynamic: escalate to stealthy
        errors.append(f"Dynamic tier failed (status {result.status})")
        ssid = await self._ensure_auto_session("stealthy")
        result = await self.stealthy_fetch(url, extraction_type=extraction_type,
            css_selector=css_selector, main_content_only=main_content_only,
            use_trafilatura=use_trafilatura, headless=headless,
            real_chrome=real_chrome, wait=wait, proxy=proxy,
            timeout=timeout, network_idle=network_idle,
            disable_resources=True,
            solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc,
            hide_canvas=hide_canvas, extra_headers=extra_headers,
            useragent=useragent, cookies=cookies,
            session_id=ssid)
        elapsed = (now() - start_time) * 1000
        result.duration_ms = elapsed

        if result.status < 400 and not _is_cloudflare_from_response(result):
            await record_result(url, "high", True, elapsed)
            result = _annotate_quality(result)
            if cache_ttl > 0:
                await set_cached(url, extraction_type, result.content, result.status, css_selector, cache_ttl)
            return _apply_chunking(result)

        # All tiers failed: return with diagnostic info
        errors.append(f"Stealthy tier also failed (status {result.status})")
        result.content = [
            f"[All fetch tiers failed for {url}]\n"
            f"Tried: HTTP (curl_cffi with Chrome impersonation) → Dynamic (Playwright browser) → Stealthy (Cloudflare solver)\n"
            f"Failures: {'; '.join(errors)}\n"
            f"Final status: {result.status}"
        ]
        await record_result(url, "high", False, elapsed)
        result.duration_ms = elapsed
        result.error = f"all_tiers_failed: {('; '.join(errors))}"
        if cache_ttl > 0:
            await set_cached(url, extraction_type, result.content, result.status, css_selector, cache_ttl)
        return _apply_chunking(result)

    # ─── Cache Management Tools ───────────────────────────────────────

    async def cache_clear(self, all: bool = False) -> CacheInfoModel:
        """Clear expired cache entries, or all entries if 'all' is True.

        :param all: If True, clear ALL cache entries. If False (default), only expired ones.
        """
        if all:
            count = await clear_all_cache()
            return CacheInfoModel(message=f"Cleared all {count} cache entries.", purged=count)
        else:
            count = await clear_cache()
            clear_robots_cache()
            return CacheInfoModel(message=f"Cleared {count} expired cache entries.", purged=count)

    # ─── Search ──────────────────────────────────────────────────────

    async def smart_search(
        self,
        query: str,
        max_results: int = 10,
        cache_ttl: int = 300,
        api_key: str = "",
    ) -> SearchResponseModel:
        """Search the web via TinyFish API (free, 30/min) and return structured results.

        No API key needed. Returns title, URL, and snippet for each result.

        :param query: The search query.
        :param max_results: Maximum number of results (1-50, default 10).
        :param cache_ttl: Cache TTL in seconds (default 300 = 5 minutes).
        :param api_key: Optional TinyFish API key (uses built-in key if empty).
        """
        try:
            from master_fetch.search import smart_search as _smart_search
            return await _smart_search(self, query, max_results, cache_ttl, api_key)
        except ImportError as e:
            return SearchResponseModel(
                query=query, results=[], total_results=0,
                error=f"Search dependencies not installed. Run: pip install master-fetch[all] ({e})",
            )
        except Exception as e:
            return SearchResponseModel(
                query=query, results=[], total_results=0,
                error=str(e)[:200],
            )

    # ─── Serve ────────────────────────────────────────────────────────

    def serve(self, http: bool = False, host: str = "127.0.0.1", port: int = 8765):
        """Start the MCP server."""
        server = FastMCP(name="MasterFetch", host=host, port=port)

        # Session management
        server.add_tool(self.open_session, title="open_session", structured_output=True)
        server.add_tool(self.close_session, title="close_session", structured_output=True)
        server.add_tool(self.list_sessions, title="list_sessions", structured_output=True)

        # HTTP fetcher
        server.add_tool(self.get, title="get", description=self.get.__doc__, structured_output=True)
        server.add_tool(self.bulk_get, title="bulk_get", description=self.bulk_get.__doc__, structured_output=True)

        # Dynamic fetcher
        server.add_tool(self.fetch, title="fetch", description=self.fetch.__doc__, structured_output=True)
        server.add_tool(self.bulk_fetch, title="bulk_fetch", description=self.bulk_fetch.__doc__, structured_output=True)

        # Stealthy fetcher
        server.add_tool(self.stealthy_fetch, title="stealthy_fetch", description=self.stealthy_fetch.__doc__, structured_output=True)
        server.add_tool(self.bulk_stealthy_fetch, title="bulk_stealthy_fetch", description=self.bulk_stealthy_fetch.__doc__, structured_output=True)

        # Screenshot
        server.add_tool(self.screenshot, title="screenshot", description=self.screenshot.__doc__)

        # THE ONE TOOL TO RULE THEM ALL
        server.add_tool(self.smart_fetch, title="smart_fetch", description=self.smart_fetch.__doc__, structured_output=True)

        # Web Search
        server.add_tool(self.smart_search, title="smart_search", description=self.smart_search.__doc__, structured_output=True)

        # Cache management
        server.add_tool(self.cache_clear, title="cache_clear", description=self.cache_clear.__doc__, structured_output=True)

        server.run(transport="stdio" if not http else "streamable-http")


# Signals that the fetched content is a JS-only placeholder, not real content.
_JS_SHELL_SIGNALS = [
    "enable javascript", "you need to enable javascript",
    "javascript is required", "javascript is disabled",
    "javascript to run this app", "javascript must be enabled",
    "please enable javascript", "requires javascript",
    "we've detected that javascript is disabled",
    "javascript is disabled in this browser",
    "enable javascript to run this app",
]

# Signals that the fetched content is a geo/region redirect page.
_GEO_REDIRECT_SIGNALS = [
    "choose a country", "select your country", "select your region",
    "shopping in the u.s.", "choose your country",
    "country selector", "region selector",
]


def _is_cloudflare_from_response(result: ResponseModel) -> bool:
    """Check if a ResponseModel indicates a bot challenge page (Cloudflare, DataDome, etc.)."""
    content_str = " ".join(result.content).lower()
    # Cloudflare indicators
    cf_signals = ["cloudflare", "cf-browser", "challenge-platform", "cf_chl_opt", "ray id"]
    # DataDome indicators
    dd_signals = ["captcha-delivery.com", "datadome", "dd="]
    # Generic bot challenge indicators
    generic_signals = ["please verify you are a human", "are you a robot", "checking your browser"]
    all_signals = cf_signals + dd_signals + generic_signals
    return any(signal in content_str for signal in all_signals)


def _is_js_shell(result: ResponseModel) -> bool:
    """Check if a response contains only a JS-only placeholder, not real content.

    Used by smart_fetch to decide whether to escalate from HTTP→dynamic or dynamic→stealthy.
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
    """
    content_str = " ".join(result.content).lower().strip()

    if _is_js_shell(result):
        return "js_shell_detected: page requires JavaScript rendering but fetcher returned placeholder"

    if any(signal in content_str for signal in _GEO_REDIRECT_SIGNALS):
        return "geo_redirect_detected: page returned region/country selector instead of content"

    if _is_cloudflare_from_response(result):
        return "bot_challenge_detected: page returned bot challenge/verification page"

    return ""


def main():
    """Entry point for the master-fetch CLI."""
    import argparse
    parser = argparse.ArgumentParser(description="Master Fetch MCP Server")
    parser.add_argument("--http", action="store_true", help="Use Streamable HTTP transport instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="Host for HTTP transport (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Port for HTTP transport (default: 8765)")
    parser.add_argument("--cache-ttl", type=int, default=3600, help="Default cache TTL in seconds (default: 3600)")
    args = parser.parse_args()

    srv = MasterFetchServer(cache_ttl=args.cache_ttl)
    srv.serve(http=args.http, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

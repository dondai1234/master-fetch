"""Hound's own HTTP fetcher and Response class.

Replaces scrapling's FetcherSession (which wraps curl_cffi) with a direct
primp-based implementation. primp provides the same TLS impersonation
(JA3/JA4 fingerprinting, HTTP/2 settings randomization) as curl_cffi but
with a cleaner API and no C dependency beyond what's already installed.

The Response class mimics scrapling's Response interface: .status, .url,
.headers, .body (bytes), .encoding, .content (decoded HTML), .css() (CSS
selector via lxml), .reason, .cookies.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

import primp

logger = logging.getLogger("master_fetch.fetcher")


# ─── Response ────────────────────────────────────────────────────────────────

class ElementWrapper:
    """Wraps an lxml element to mimic scrapling's Selector element interface.

    Exposes ._root (the lxml node) so trafilatura_extractor's
    CSS-selector narrowing path (tostring(el._root)) keeps working.
    """

    __slots__ = ("_root", "_url")

    def __init__(self, root, url: str = ""):
        self._root = root
        self._url = url

    @property
    def url(self) -> str:
        return self._url

    def css(self, selector: str) -> List["ElementWrapper"]:
        """CSS selector query on this element's subtree."""
        from lxml.cssselect import CSSSelector
        try:
            sel = CSSSelector(selector)
            matches = sel(self._root)
            return [ElementWrapper(m, self._url) for m in matches]
        except Exception:
            return []

    def text_content(self) -> str:
        """Get all text content from this element."""
        return self._root.text or ""


class Response:
    """HTTP response with CSS selector support.

    Mimics scrapling's Response interface used throughout server.py:
        .status (int)          - HTTP status code
        .url (str)             - final URL after redirects
        .headers (dict)        - response headers
        .body (bytes)          - raw response body
        .encoding (str)        - detected encoding
        .content (str)         - decoded body (lazy)
        .css(selector) -> list - CSS selector query
        .reason (str)          - status text
        .cookies (dict)        - response cookies
    """

    __slots__ = (
        "_status", "_url", "_headers", "_body", "_encoding",
        "_reason", "_cookies", "_root", "_content_cached",
    )

    def __init__(
        self,
        url: str,
        body: bytes,
        status: int,
        headers: Optional[Dict[str, str]] = None,
        encoding: str = "utf-8",
        reason: str = "",
        cookies: Optional[Dict[str, str]] = None,
    ):
        self._url = url
        self._body = body if isinstance(body, bytes) else (body or b"")
        self._status = status
        self._headers = headers or {}
        self._encoding = encoding or "utf-8"
        self._reason = reason or ""
        self._cookies = cookies or {}
        self._root: Any = None  # lazy lxml tree
        self._content_cached: Optional[str] = None

    # ── Properties matching scrapling's interface ──────────────────

    @property
    def status(self) -> int:
        return self._status

    @property
    def url(self) -> str:
        return self._url

    @property
    def headers(self) -> Dict[str, str]:
        return self._headers

    @property
    def body(self) -> bytes:
        return self._body

    @property
    def encoding(self) -> str:
        return self._encoding

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def cookies(self) -> Dict[str, str]:
        return self._cookies

    @property
    def content(self) -> str:
        """Decoded body (lazy, cached)."""
        if self._content_cached is None:
            self._content_cached = self._body.decode(
                self._encoding, errors="replace"
            )
        return self._content_cached

    @property
    def html_content(self) -> str:
        """Alias for .content (scrapling compat)."""
        return self.content

    # ── CSS selector support ───────────────────────────────────────

    def _ensure_parsed(self):
        """Lazily parse the body into an lxml tree."""
        if self._root is not None:
            return
        if not self._body:
            from lxml import etree
            self._root = etree.fromstring(b"<html></html>")
            return
        try:
            from lxml import html as lxml_html
            # Use html_fromstring which handles malformed HTML gracefully
            self._root = lxml_html.fromstring(self.content)
        except Exception:
            try:
                from lxml import etree
                parser = etree.HTMLParser(recover=True)
                self._root = etree.fromstring(
                    self._body, parser=parser
                )
            except Exception:
                # Last resort: empty tree
                from lxml import etree
                self._root = etree.fromstring(b"<html></html>")

    def css(self, selector: str) -> List[ElementWrapper]:
        """CSS selector query. Returns list of ElementWrapper objects.

        Each ElementWrapper has ._root (lxml element) so callers can do
        tostring(el._root, encoding='unicode') to get the HTML.
        """
        self._ensure_parsed()
        if self._root is None:
            return []
        try:
            from lxml.cssselect import CSSSelector
            sel = CSSSelector(selector)
            matches = sel(self._root)
            return [ElementWrapper(m, self._url) for m in matches]
        except Exception:
            return []

    @property
    def first(self) -> "Response":
        """Scrapling compat: .css('body').first returns self-like or empty."""
        return self

    def get_all_text(self, strip=False, ignore_tags=()) -> str:
        """Get all text content, optionally stripping whitespace and ignoring tags."""
        self._ensure_parsed()
        if self._root is None:
            return ""
        try:
            # Collect text from all elements, skipping ignored tags
            tags_to_skip = set(ignore_tags) if ignore_tags else set()
            texts = []
            for el in self._root.iter():
                tag = el.tag if isinstance(el.tag, str) else ""
                if tag in tags_to_skip:
                    continue
                if el.text:
                    texts.append(el.text)
                if el.tail:
                    texts.append(el.tail)
            result = " ".join(texts)
            if strip:
                result = result.strip()
            return result
        except Exception:
            return self.content


# ─── Browser response builder ─────────────────────────────────────────────────

async def response_from_browser_page(
    page: Any,
    first_response: Any,
    final_response: Optional[Any],
) -> Response:
    """Build a Response from a patchright/playwright page + response objects.

    Mirrors scrapling's ResponseFactory.from_async_playwright_response but
    without the Selector/parser overhead. Gets the page content (with the
    Windows page.content() retry workaround), response headers, status, etc.
    """
    # Get page content with Windows retry workaround
    # (Playwright has a known issue with page.content() on Windows:
    #  https://github.com/microsoft/playwright/issues/16108)
    page_content = b""
    for _ in range(20):
        try:
            html_str = await page.content()
            if html_str:
                page_content = html_str.encode("utf-8")
                break
        except Exception:
            await page.wait_for_timeout(500)

    # Determine the final response (fall back to first if no final)
    resp = final_response if final_response else first_response
    if resp is None:
        return Response(
            url=page.url if page else "",
            body=page_content,
            status=0,
            headers={},
            encoding="utf-8",
        )

    # Extract headers
    try:
        headers = await resp.all_headers()
    except Exception:
        headers = {}

    # Extract encoding from content-type
    ct = headers.get("content-type", "")
    encoding = _extract_encoding(ct)

    # Extract status
    status = resp.status

    # Extract cookies from context
    cookies: Dict[str, str] = {}
    try:
        cookie_list = await page.context.cookies()
        for c in cookie_list:
            cookies[c.get("name", "")] = c.get("value", "")
    except Exception:
        pass

    return Response(
        url=page.url if page else (resp.url if hasattr(resp, "url") else ""),
        body=page_content,
        status=status,
        headers=headers,
        encoding=encoding,
        cookies=cookies,
    )


def _extract_encoding(content_type: str) -> str:
    """Extract charset from a content-type header."""
    if not content_type:
        return "utf-8"
    for part in content_type.split(";"):
        part = part.strip().lower()
        if part.startswith("charset="):
            return part.split("=", 1)[1].strip().strip('"').strip("'")
    return "utf-8"


# ─── HTTP fetcher (primp-based) ────────────────────────────────────────────────

# Default impersonation targets for the primp client
# primp supports: chrome, safari, firefox, edge, random
# (primp falls back to 'random' for unknown targets)
_IMPERSONATE_POOL = [
    "chrome",
    "safari",
    "firefox",
    "edge",
]


class HTTPSession:
    """Async HTTP fetch session using primp for TLS impersonation.

    Replaces scrapling's FetcherSession. Provides:
    - TLS fingerprint impersonation (Chrome, Firefox, Safari, Edge)
    - Proxy support (http, https, socks5)
    - Retry with backoff
    - Stealthy headers (Google referer, realistic User-Agent)

    Usage:
        async with HTTPSession(impersonate="chrome") as session:
            response = await session.get("https://example.com")
    """

    def __init__(
        self,
        impersonate: Union[str, List[str]] = "chrome",
        proxy: Optional[str] = None,
        stealthy_headers: bool = True,
        retries: int = 1,
        retry_delay: float = 1.0,
        timeout: int = 30,
    ):
        self._impersonate = impersonate
        self._proxy = proxy
        self._stealthy_headers = stealthy_headers
        self._retries = retries
        self._retry_delay = retry_delay
        self._timeout = timeout
        self._client: Optional[primp.Client] = None

    async def __aenter__(self) -> "HTTPSession":
        await self._init_client()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _init_client(self) -> None:
        """Initialize the primp client in a worker thread (import is ~1s)."""
        impersonate = self._impersonate
        if isinstance(impersonate, list):
            # primp doesn't support rotation pools like scrapling's curl_cffi.
            # Pick a random one from the pool.
            import random
            impersonate = random.choice(impersonate)

        def _create():
            kwargs: Dict[str, Any] = {
                "impersonate": impersonate,
                "impersonate_os": "random",
            }
            if self._proxy:
                kwargs["proxy"] = self._proxy
            return primp.Client(**kwargs)

        self._client = await asyncio.to_thread(_create)

    async def close(self) -> None:
        """Close the underlying client."""
        # primp.Client doesn't have an explicit close, but we drop the reference
        self._client = None

    def _build_headers(
        self, headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """Build request headers with stealthy defaults."""
        final_headers: Dict[str, str] = {}
        if self._stealthy_headers:
            final_headers["referer"] = "https://www.google.com/"
            try:
                from browserforge.headers import HeaderGenerator
                hg = HeaderGenerator()
                generated = hg.generate()
                # Merge browserforge headers (lowercase keys)
                for k, v in generated.items():
                    if k.lower() not in ("referer",):
                        final_headers.setdefault(k.lower(), v)
            except Exception:
                pass
        # User-supplied headers override defaults
        if headers:
            for k, v in headers.items():
                final_headers[k] = v
        return final_headers

    async def get(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        retries: Optional[int] = None,
        proxy: Optional[str] = None,
        follow_redirects: bool = True,
        max_redirects: int = 5,
        params: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Response:
        """Fetch a URL via HTTP with TLS impersonation.

        Returns a Response object.
        """
        if self._client is None:
            await self._init_client()

        client = self._client
        if proxy:
            # Override proxy for this request: create a new client
            def _create_proxy_client():
                impersonate = self._impersonate
                if isinstance(impersonate, list):
                    import random
                    impersonate = random.choice(impersonate)
                return primp.Client(impersonate=impersonate, proxy=proxy)
            client = await asyncio.to_thread(_create_proxy_client)

        final_headers = self._build_headers(headers)
        if cookies:
            final_headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        actual_timeout = timeout or self._timeout
        actual_retries = retries if retries is not None else self._retries

        last_error: Optional[Exception] = None
        for attempt in range(actual_retries + 1):
            try:
                # primp's get() is synchronous, wrap in to_thread
                def _do_get():
                    # Build query params
                    req_url = url
                    if params:
                        from urllib.parse import urlencode
                        separator = "&" if "?" in req_url else "?"
                        req_url = f"{req_url}{separator}{urlencode(params)}"
                    return client.get(
                        req_url,
                        headers=final_headers,
                        timeout=actual_timeout,
                        follow_redirects=follow_redirects,
                    )

                resp = await asyncio.to_thread(_do_get)

                # Parse encoding from content-type
                resp_headers = dict(resp.headers) if hasattr(resp, "headers") else {}
                ct = ""
                for k, v in resp_headers.items():
                    if k.lower() == "content-type":
                        ct = v
                        break
                encoding = _extract_encoding(ct)

                # Parse cookies
                resp_cookies: Dict[str, str] = {}
                if hasattr(resp, "cookies"):
                    try:
                        for c in resp.cookies:
                            if isinstance(c, dict):
                                resp_cookies[c.get("name", "")] = c.get("value", "")
                            elif hasattr(c, "name"):
                                resp_cookies[c.name] = c.value
                    except Exception:
                        pass

                return Response(
                    url=str(resp.url) if hasattr(resp, "url") else url,
                    body=resp.content if hasattr(resp, "content") else b"",
                    status=resp.status_code if hasattr(resp, "status_code") else 0,
                    headers=resp_headers,
                    encoding=encoding,
                    reason=resp.reason if hasattr(resp, "reason") else "",
                    cookies=resp_cookies,
                )

            except Exception as e:
                last_error = e
                if attempt < actual_retries:
                    logger.warning(
                        f"HTTP fetch attempt {attempt + 1} failed for {url}: {str(e)[:200]}. "
                        f"Retrying in {self._retry_delay}s..."
                    )
                    await asyncio.sleep(self._retry_delay)
                else:
                    raise

        # Should not reach here, but just in case
        raise last_error or RuntimeError(f"Failed to fetch {url}")


# ─── Convenience function ─────────────────────────────────────────────────────

async def http_get(
    url: str,
    *,
    impersonate: Union[str, List[str]] = "chrome",
    proxy: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    cookies: Optional[Dict[str, str]] = None,
    timeout: int = 30,
    stealthy_headers: bool = True,
    retries: int = 1,
) -> Response:
    """One-off async HTTP fetch with TLS impersonation.

    Usage:
        response = await http_get("https://example.com")
    """
    async with HTTPSession(
        impersonate=impersonate,
        proxy=proxy,
        stealthy_headers=stealthy_headers,
        retries=retries,
        timeout=timeout,
    ) as session:
        return await session.get(
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
        )


def _proxy_for_env() -> Optional[str]:
    """Read proxy from environment, if set."""
    import os
    return os.environ.get("HOUND_SEARCH_PROXY")


def search_proxy() -> Optional[str]:
    """Get the search proxy, if configured."""
    return _proxy_for_env()

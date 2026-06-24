"""Hound-native keyless search engine scrapers (v7 local search flagship).

No API key, no account, no third-party service. Scrapes public search engines
(DuckDuckGo, Bing, Google, Wikipedia) over browser-impersonated HTTP (scrapling
FetcherSession, a CORE dependency, so lean installs get working search), with
escalation to hound's warm stealthy Patchright browser when an engine blocks.

Built for one job: feed smart_fetch.

Anti rate-limit / IP-block: the Search Engine Resilience Layer (SERL), a stateful
per-engine coordinator (_EngineCoordinator) in front of every SERP request:
  1. Persistent warm session per engine (cookies + TLS reuse across searches) so
     the engine sees a returning human, not a fresh bot each call. Also faster
     (no per-search TLS handshake).
  2. Per-engine pacer with jitter: within one search all engines fire in parallel
     (free); across searches, only same-engine bursts get a small jittered delay.
  3. Per-engine circuit breaker + exponential cooldown: a blocked engine is
     skipped for 15->30->60->120s (capped) while the other engines carry the load.
  4. 202 soft-limit + 429/503/403 + Retry-After aware (DDG returns 202 as a soft
     rate-limit; this was a missed case before).
  5. Fingerprint rotation: scrapling impersonate list picks a real Chrome/Edge/
     Firefox/Safari TLS fingerprint per request.
  6. Adaptive reserve tier: Google (most CAPTCHA-prone) is held in reserve and
     only fires via the stealthy browser when the primary engines fall short AND
     one was blocked.
  7. HOUND_SEARCH_PROXY env: route all engine requests through a user-supplied
     proxy (residential/rotating) for near-unblockable heavy use. Not bundled.

Honest posture (same as SearXNG/ddgs): no keyless local tool is bulletproof
against sustained engine blocking without a proxy. For a single user on a clean
residential IP doing real agent work, the seven mechanisms above keep it working;
HOUND_SEARCH_PROXY is the bulletproof path for those who bring one. No
search-engine ToS compliance is claimed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass, asdict
from time import time
from typing import Optional
from urllib.parse import quote, urlparse, parse_qs, unquote, urljoin

from bs4 import BeautifulSoup

from master_fetch.crawl import normalize_url

logger = logging.getLogger("master-fetch.search_engines")

DEFAULT_ENGINES = ("duckduckgo", "bing", "wikipedia")
ENGINE_TIMEOUT = 12  # seconds per engine request
CAPTCHA_MARKERS = ("captcha", "unusual traffic", "are you a robot", "sorry/image", "ddg-captcha", "blocked", "access denied")

# Per-engine pacing: min seconds between successive requests to the SAME engine.
# Within a single search all engines fire in parallel (each engine sees 1 req),
# so this only spreads same-engine bursts across successive searches.
_PACE = {"duckduckgo": 1.2, "bing": 1.5, "wikipedia": 0.3, "google": 2.0}
_COOLDOWN_BASE = 15.0   # seconds; doubles each consecutive block
_COOLDOWN_CAP = 120.0   # max cooldown
_RECREATE_EVERY = 3     # recreate the persistent session every N consecutive blocks (burned session)

# Hard per-engine deadline: a slow / blocked / escalating engine can never hang
# the whole search. Engines that don't finish in time are reported as blocked
# (timed out) and the agent gets results from the engines that did. Bounds total
# search latency so it stays under typical MCP client timeouts. Tunable via the
# HOUND_SEARCH_DEADLINE env var (seconds).
try:
    SEARCH_ENGINE_DEADLINE = float(os.environ.get("HOUND_SEARCH_DEADLINE", "8") or "8")
except ValueError:
    SEARCH_ENGINE_DEADLINE = 8.0

# Power-user env knobs (all optional).
_PROXY = os.environ.get("HOUND_SEARCH_PROXY") or None
try:
    _PACE_OVERRIDE = float(os.environ.get("HOUND_SEARCH_MIN_INTERVAL", "0") or 0)
except ValueError:
    _PACE_OVERRIDE = 0.0
# Current, realistic TLS fingerprints scrapling can impersonate. Passing a LIST
# makes scrapling pick one at random per request -> fingerprint rotation.
_IMPERSONATE_POOL = ["chrome131", "chrome136", "chrome142", "edge", "safari184", "firefox147"]


@dataclass
class RawResult:
    title: str
    url: str
    snippet: str
    source: str  # engine name
    position: int = 0  # 1-indexed within its engine


@dataclass
class EngineReport:
    name: str
    ok: bool = False        # parsed >=1 result
    blocked: bool = False   # rate-limited / CAPTCHA'd / refused / cooling down
    error: str = ""


# ─── transport ──────────────────────────────────────────────────────────────

def _is_blocked(status: int, body_text: str) -> bool:
    # 202 = DuckDuckGo soft rate-limit (it accepts the request but will not
    # answer). 429/503/403 are hard rate-limit / refusal codes.
    if status in (429, 503, 403, 202):
        return True
    low = (body_text or "").lower()
    if any(m in low for m in CAPTCHA_MARKERS):
        # Only treat as blocked if the body is short (a real SERP is large and
        # may legitimately contain the word "blocked" in a result snippet).
        return len(low) < 6000
    return False


def _retry_after(resp) -> float:
    """Read a Retry-After header (seconds) from a scrapling Response, else 0."""
    try:
        headers = getattr(resp, "headers", None) or {}
        ra = headers.get("Retry-After") or headers.get("retry-after")
        if ra:
            return float(int(ra))
    except Exception:
        pass
    return 0.0


async def _stealthy_html(server, url: str, timeout: int = ENGINE_TIMEOUT) -> Optional[str]:
    """Escalation: fetch a SERP via hound's warm stealthy browser, return raw HTML.

    This is the flagship anti-bot move: when an engine blocks the HTTP scraper,
    hound renders the SERP in the warm Patchright browser and parses that instead.
    No keyless search lib does this. Returns None if the stealthy path is
    unavailable or yields no HTML.
    """
    if server is None:
        return None
    try:
        res = await server.stealthy_fetch(
            url, extraction_type="html", main_content_only=False,
            use_trafilatura=False, google_search=False,
            disable_resources=True, timeout=int(timeout * 1000),
        )
        # content is a list of text chunks; for html extraction it is the page HTML.
        if res and res.content and not res.error:
            html = "".join(res.content)
            if html and len(html) > 200:
                return html
    except Exception as e:
        logger.debug(f"stealthy SERP escalation failed for {url[:80]}: {e}")
    return None


# ─── Search Engine Resilience Layer (SERL) ───────────────────────────────────
#
# A stateful per-engine coordinator: persistent warm session + pacer + circuit
# breaker. One module singleton (_ENGINES_COORD) lives for the server lifetime.

class _EngineState:
    __slots__ = ("name", "cm", "sess", "lock", "last_req",
                 "cooldown_until", "consecutive_blocks", "created", "recreate")

    def __init__(self, name: str):
        self.name = name
        self.cm = None            # FetcherSession async context manager (held open)
        self.sess = None          # the live session object returned by __aenter__
        self.lock = asyncio.Lock()  # serializes same-engine requests (pacing)
        self.last_req = 0.0
        self.cooldown_until = 0.0
        self.consecutive_blocks = 0
        self.created = False
        self.recreate = False


class _EngineCoordinator:
    """Per-engine persistent warm session + pacer + circuit breaker."""

    def __init__(self):
        self.states: dict[str, _EngineState] = {}

    def state(self, name: str) -> _EngineState:
        st = self.states.get(name)
        if st is None:
            st = _EngineState(name)
            self.states[name] = st
        return st

    def cooldown_left(self, name: str) -> float:
        return max(0.0, self.state(name).cooldown_until - time())

    def reset(self, name: str) -> None:
        """Clear the circuit breaker for an engine (e.g. stealthy escalation
        succeeded, so the engine is not actually blocked)."""
        st = self.state(name)
        st.cooldown_until = 0.0
        st.consecutive_blocks = 0

    async def warmup(self, name: str, url: str, timeout: float = 6.0) -> None:
        """Best-effort pre-warm: ensure the persistent session + do one throwaway
        GET so TLS + cookies are established before the first real search. Does
        NOT touch the circuit breaker or the pacer (so a warmup cannot trigger a
        cooldown, and the first real search is not paced because of it). Called at
        server startup; errors are swallowed silently."""
        st = self.state(name)
        async with st.lock:
            await self._ensure_session(st)
            if st.sess is None:
                return
            try:
                await st.sess.get(url, timeout=timeout)
            except Exception:
                pass  # warming is best-effort; a failure here is not a block

    def _make_session(self):
        from scrapling.engines.static import FetcherSession
        # impersonate list -> random fingerprint per request (rotation).
        # retries=2 handles transient connection errors; scrapling does NOT retry
        # on HTTP 429 (a 429 is a successful response), so the circuit breaker
        # owns backoff for rate-limits.
        return FetcherSession(
            impersonate=_IMPERSONATE_POOL, proxy=_PROXY,
            stealthy_headers=True, retries=2, retry_delay=1,
            follow_redirects="safe", timeout=ENGINE_TIMEOUT,
        )

    async def _ensure_session(self, st: _EngineState):
        """Lazily create (or recreate) the persistent warm session for an engine."""
        if st.recreate and st.cm is not None:
            try:
                await st.cm.__aexit__(None, None, None)
            except Exception:
                pass
            st.cm = None
            st.sess = None
            st.created = False
            st.recreate = False
        if not st.created or st.sess is None:
            try:
                st.cm = self._make_session()
                st.sess = await st.cm.__aenter__()
                st.created = True
            except Exception as e:
                logger.debug(f"persistent session for {st.name} failed: {e}; per-request fallback")
                st.cm = None
                st.sess = None
                st.created = False

    async def get(self, name: str, url: str, *,
                  method: str = "GET", form: Optional[dict] = None,
                  timeout: int = ENGINE_TIMEOUT) -> tuple[Optional[str], int, bool, bool]:
        """Paced, circuit-broken, persistent-session request to an engine SERP.

        Returns (text, status, blocked, cooling). When cooling is True the engine
        is in cooldown and NO request was made (load-shedding); text is None,
        blocked is True. Transport errors return blocked=False (not a rate-limit).
        """
        st = self.state(name)
        async with st.lock:  # serialize same-engine requests so pacing is accurate
            now = time()
            # Circuit breaker: skip entirely while cooling down.
            if now < st.cooldown_until:
                return None, 0, True, True
            # Pacer: enforce a min interval (with jitter) between successive
            # same-engine hits so a burst of searches does not hammer one engine.
            pace = _PACE_OVERRIDE if _PACE_OVERRIDE > 0 else _PACE.get(name, 1.0)
            elapsed = now - st.last_req
            if elapsed < pace:
                await asyncio.sleep(pace - elapsed + random.uniform(0, 0.35))
            await self._ensure_session(st)
            st.last_req = time()

            text: Optional[str] = None
            status = 0
            try:
                if st.sess is not None:
                    if method == "POST" and form is not None:
                        resp = await st.sess.post(url, data=form, timeout=timeout)
                    else:
                        resp = await st.sess.get(url, timeout=timeout)
                else:
                    # Per-request fallback if the persistent session would not create.
                    cm = self._make_session()
                    s2 = await cm.__aenter__()
                    try:
                        if method == "POST" and form is not None:
                            resp = await s2.post(url, data=form, timeout=timeout)
                        else:
                            resp = await s2.get(url, timeout=timeout)
                    finally:
                        await cm.__aexit__(None, None, None)
                status = getattr(resp, "status", 0) or 0
                body = getattr(resp, "body", None)
                if body:
                    text = body.decode(getattr(resp, "encoding", None) or "utf-8", errors="replace")
                blocked = _is_blocked(status, text or "")
                ra = _retry_after(resp)
            except Exception as e:
                logger.debug(f"engine {name} GET failed: {e}")
                return None, 0, False, False  # transport error, not a rate-limit

            # Update the circuit breaker.
            if blocked:
                st.consecutive_blocks += 1
                cd = min(_COOLDOWN_BASE * (2 ** (st.consecutive_blocks - 1)), _COOLDOWN_CAP)
                if ra > 0:
                    cd = max(cd, ra)
                st.cooldown_until = time() + cd
                # If this session keeps getting blocked, it may be burned: force a
                # fresh session (new cookies + TLS) on the next acquire.
                if st.consecutive_blocks % _RECREATE_EVERY == 0:
                    st.recreate = True
            else:
                st.consecutive_blocks = 0
                st.cooldown_until = 0.0
            return text, status, blocked, False

    async def close_all(self) -> None:
        """Close every persistent session (called at server shutdown)."""
        for st in self.states.values():
            if st.cm is not None:
                try:
                    await st.cm.__aexit__(None, None, None)
                except Exception:
                    pass
            st.cm = None
            st.sess = None
            st.created = False


_ENGINES_COORD = _EngineCoordinator()


async def _engine_get(name: str, url: str, *, method: str = "GET",
                      form: Optional[dict] = None,
                      timeout: int = ENGINE_TIMEOUT) -> tuple[Optional[str], int, bool, bool]:
    """SERP transport via the resilience coordinator. Returns
    (text, status, blocked, cooling). Patchable seam for tests."""
    return await _ENGINES_COORD.get(name, url, method=method, form=form, timeout=timeout)


async def close_search_engines() -> None:
    """Shutdown hook: close all persistent warm engine sessions."""
    await _ENGINES_COORD.close_all()


# Throwaway warmup URLs that mirror each engine's real search path (same host +
# endpoint) so the first real search reuses a warm TLS session + cookies.
_WARMUP_URLS = {
    "duckduckgo": "https://html.duckduckgo.com/html/?q=test",
    "bing": "https://www.bing.com/search?q=test",
    "wikipedia": "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=test&srlimit=1&srprop=snippet&format=json&utf8=1",
}


async def prewarm_search_engines(engines: Optional[list[str]] = None) -> None:
    """Pre-warm the default engine sessions at startup so the agent's first
    smart_search is fast (warm TLS + cookies, fewer first-hit blocks, fewer
    stealthy escalations). Best-effort, fire-and-forget; never raises."""
    names = [e for e in (engines or list(DEFAULT_ENGINES)) if e in _WARMUP_URLS]
    if not names:
        return
    await asyncio.gather(
        *[_ENGINES_COORD.warmup(n, _WARMUP_URLS[n]) for n in names],
        return_exceptions=True,
    )


# ─── DuckDuckGo (html endpoint) ─────────────────────────────────────────────

def _ddg_real_url(href: str) -> str:
    """Decode a DDG redirect link (//duckduckgo.com/l/?uddg=ENCODED&rut=...) to the real URL."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        qs = parse_qs(urlparse(href).query)
        u = qs.get("uddg", [""])[0]
        if u:
            return unquote(u)
    return href


def _parse_ddg(html: str) -> list[RawResult]:
    soup = BeautifulSoup(html, "lxml")
    out: list[RawResult] = []
    for i, block in enumerate(soup.select(".result, .web-result")):
        a = block.select_one(".result__a")
        if not a:
            continue
        href = a.get("href", "") or ""
        url = _ddg_real_url(href)
        title = a.get_text(" ", strip=True)
        if not url or not title:
            continue
        snip_el = block.select_one(".result__snippet")
        snippet = snip_el.get_text(" ", strip=True) if snip_el else ""
        out.append(RawResult(title=title, url=url, snippet=snippet, source="duckduckgo", position=i + 1))
    return out


async def search_ddg(query: str, max_results: int, *, region: str = "us-en",
                     freshness: Optional[str] = None, page: int = 0,
                     server=None) -> tuple[list[RawResult], EngineReport]:
    q = query
    # DDG html endpoint supports a time filter via the `df` param (d/w/m/y) and
    # pagination via `s` (result start offset).
    params = f"q={quote(q)}&kl={quote(region)}"
    if freshness in ("day", "week", "month", "year"):
        params += f"&df={freshness[0]}"
    if page > 0:
        params += f"&s={page * max_results}"
    url = f"https://html.duckduckgo.com/html/?{params}"
    text, status, blocked, cooling = await _engine_get("duckduckgo", url)
    rep = EngineReport(name="duckduckgo")
    if blocked and not cooling and server is not None:
        escalated = await _stealthy_html(server, url)
        if escalated:
            text, blocked = escalated, False
            _ENGINES_COORD.reset("duckduckgo")
    if not text:
        rep.blocked = blocked
        rep.error = (f"cooling down (rate-limited, ~{int(_ENGINES_COORD.cooldown_left('duckduckgo'))}s left)"
                     if cooling else f"no response (status {status})")
        return [], rep
    results = _parse_ddg(text)[:max_results]
    rep.ok = bool(results)
    rep.blocked = (not results) and blocked
    if not results:
        rep.error = "no results parsed"
    return results, rep


# ─── Bing ────────────────────────────────────────────────────────────────────

def _parse_bing(html: str) -> list[RawResult]:
    soup = BeautifulSoup(html, "lxml")
    out: list[RawResult] = []
    for i, li in enumerate(soup.select("li.b_algo")):
        a = li.select_one("h2 a")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        # Bing wraps the main link in an opaque bing.com/ck/a redirect that has
        # NO recoverable real URL in the href. The real URL is shown in the <cite>
        # display element (sometimes with '>>' breadcrumb separators).
        cite = li.select_one(".b_attribution cite, .t_tgk cite, cite")
        cite_text = cite.get_text(" ", strip=True) if cite else ""
        url = _bing_real_url(cite_text)
        if not url:
            continue  # can't recover the real URL; skip the junk redirect
        snip = li.select_one(".b_caption p, p.b_paractr, .b_lineclamp4, .b_caption .b_paractr")
        snippet = snip.get_text(" ", strip=True) if snip else ""
        out.append(RawResult(title=title, url=url, snippet=snippet, source="bing", position=i + 1))
    return out


def _bing_real_url(cite_text: str) -> str:
    """Reconstruct a real URL from Bing's <cite> display text.

    Bing shows the result URL as e.g. 'https://www.programiz.com >> python-programming >> online-compiler'.
    Replace the '>' breadcrumb separators with '/' and ensure a scheme.
    """
    if not cite_text:
        return ""
    parts = [p.strip() for p in cite_text.split("\u203a") if p.strip()]
    url = "/".join(parts) if len(parts) > 1 else cite_text.strip()
    if url.startswith(("http://", "https://")):
        return url
    first = url.split("/", 1)[0]
    if url.startswith("www.") or ("." in first and " " not in first):
        return "https://" + url
    return ""


async def search_bing(query: str, max_results: int, *, region: str = "us-en",
                      freshness: Optional[str] = None, page: int = 0,
                      server=None) -> tuple[list[RawResult], EngineReport]:
    params = f"q={quote(query)}&count={max(min(max_results * 2, 50), 10)}&setlang=en"
    if freshness in ("day", "week", "month"):
        params += f"&filters=ex1%3a%22ez5_{freshness[0]}1%22"
    elif freshness == "year":
        params += "&filters=ex1%3a%22ez5_y1%22"
    if page > 0:
        params += f"&first={page * max_results + 1}"
    url = f"https://www.bing.com/search?{params}"
    text, status, blocked, cooling = await _engine_get("bing", url)
    rep = EngineReport(name="bing")
    if blocked and not cooling and server is not None:
        escalated = await _stealthy_html(server, url)
        if escalated:
            text, blocked = escalated, False
            _ENGINES_COORD.reset("bing")
    if not text:
        rep.blocked = blocked
        rep.error = (f"cooling down (rate-limited, ~{int(_ENGINES_COORD.cooldown_left('bing'))}s left)"
                     if cooling else f"no response (status {status})")
        return [], rep
    results = _parse_bing(text)[:max_results]
    rep.ok = bool(results)
    rep.blocked = (not results) and blocked
    if not results:
        rep.error = "no results parsed"
    return results, rep


# ─── Google (stealthy-friendly; impersonated HTTP often CAPTCHAs) ────────────

def _parse_google(html: str) -> list[RawResult]:
    soup = BeautifulSoup(html, "lxml")
    out: list[RawResult] = []
    seen: set[str] = set()
    # Modern Google: div.g / div.MjjYud > div[data-ved] with h3 + a + snippet span.
    for block in soup.select("div.g, div._kno, div[data-ved]"):
        a = block.select_one("a:has(h3)") or block.select_one("h3 a, a h3")
        # fallback: any <a> with an <h3> inside
        if not a:
            h3 = block.select_one("h3")
            if h3:
                a = h3.find_parent("a")
        if not a:
            continue
        href = a.get("href", "") or ""
        if href.startswith("/url?"):
            qs = parse_qs(urlparse(href).query)
            href = qs.get("q", [""])[0] or href
        if not href.startswith("http"):
            continue
        h3 = block.select_one("h3")
        title = h3.get_text(" ", strip=True) if h3 else a.get_text(" ", strip=True)
        if not title:
            continue
        if href in seen:
            continue
        seen.add(href)
        snip = block.select_one(".VwiC3b, [data-sncf], span.aCOpRe, div.IsZvec span")
        snippet = snip.get_text(" ", strip=True) if snip else ""
        out.append(RawResult(title=title, url=href, snippet=snippet, source="google", position=len(out) + 1))
        if len(out) >= 30:
            break
    return out


async def search_google(query: str, max_results: int, *, region: str = "us-en",
                        freshness: Optional[str] = None, page: int = 0,
                        server=None) -> tuple[list[RawResult], EngineReport]:
    params = f"q={quote(query)}&hl=en&num={max(min(max_results * 2, 50), 10)}"
    if freshness in ("day", "week", "month", "year"):
        params += f"&tbs=qdr:{freshness[0]}"
    if page > 0:
        params += f"&start={page * max_results + 1}"
    url = f"https://www.google.com/search?{params}"
    text, status, blocked, cooling = await _engine_get("google", url)
    rep = EngineReport(name="google")
    # Google almost always CAPTCHAs plain impersonated HTTP under any load; if
    # blocked or empty, escalate to the warm stealthy browser (the flagship move).
    if (blocked or not text) and not cooling and server is not None:
        escalated = await _stealthy_html(server, url)
        if escalated:
            text, blocked = escalated, False
            _ENGINES_COORD.reset("google")
    if not text:
        rep.blocked = blocked or (status in (429, 503, 403, 202))
        rep.error = (f"cooling down (rate-limited, ~{int(_ENGINES_COORD.cooldown_left('google'))}s left)"
                     if cooling else f"no response (status {status})")
        return [], rep
    results = _parse_google(text)[:max_results]
    rep.ok = bool(results)
    rep.blocked = (not results) and blocked
    if not results:
        rep.error = "no results parsed (likely CAPTCHA)"
    return results, rep


# ─── Wikipedia (official API, keyless, always works) ─────────────────────────

async def search_wikipedia(query: str, max_results: int, *, region: str = "us-en",
                           freshness: Optional[str] = None, page: int = 0,
                           server=None
                           ) -> tuple[list[RawResult], EngineReport]:
    lang = "en"
    # region is like 'us-en' (country-language); the Wikipedia host language is
    # the LAST segment (the language), not the country prefix.
    if region and "-" in region:
        lang = region.split("-")[-1]
    elif region:
        lang = region
    sroffset = f"&sroffset={page * max_results}" if page > 0 else ""
    url = (f"https://{lang}.wikipedia.org/w/api.php?action=query&list=search"
           f"&srsearch={quote(query)}&srlimit={max(min(max_results, 20), 1)}"
           f"&srprop=snippet&format=json&utf8=1{sroffset}")
    text, status, blocked, cooling = await _engine_get("wikipedia", url)
    rep = EngineReport(name="wikipedia")
    if not text:
        rep.blocked = blocked
        rep.error = (f"cooling down (rate-limited, ~{int(_ENGINES_COORD.cooldown_left('wikipedia'))}s left)"
                     if cooling else f"no response (status {status})")
        return [], rep
    out: list[RawResult] = []
    try:
        data = __import__("json").loads(text)
        for i, item in enumerate(data.get("query", {}).get("search", [])[:max_results]):
            title = item.get("title", "").strip()
            if not title:
                continue
            snip_html = item.get("snippet", "")
            snippet = BeautifulSoup(snip_html, "lxml").get_text(" ", strip=True)
            page_url = f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
            out.append(RawResult(title=title, url=page_url, snippet=snippet, source="wikipedia", position=i + 1))
    except Exception as e:
        rep.error = f"parse error: {e}"
        return [], rep
    rep.ok = bool(out)
    return out, rep


# ─── orchestrator ─────────────────────────────────────────────────────────────

_ENGINES = {
    "duckduckgo": search_ddg,
    "bing": search_bing,
    "google": search_google,
    "wikipedia": search_wikipedia,
}


def _strip_tags(s: str) -> str:
    if not s:
        return ""
    return BeautifulSoup(s, "lxml").get_text(" ", strip=True)


def merge_dedupe(per_engine: list[tuple[list[RawResult], EngineReport]], max_results: int,
                 site: Optional[str] = None, exclude_sites: Optional[list[str]] = None,
                 ) -> list[RawResult]:
    """Merge results across engines, dedup by normalized URL, apply site filters.

    Same-domain `site:` filter and `-site:` exclusions are applied here (on the
    final URL) so they work regardless of which engine returned the result.
    """
    seen: dict[str, RawResult] = {}
    for results, _rep in per_engine:
        for r in results:
            try:
                host = (urlparse(r.url).netloc or "").lower()
            except Exception:
                continue
            if site and site.lower() not in host:
                continue
            if any(d and d.lower() in host for d in (exclude_sites or [])):
                continue
            key = normalize_url(r.url)
            if key in seen:
                # Keep the higher-ranked / earlier copy; prefer a non-empty snippet.
                prev = seen[key]
                if (not prev.snippet and r.snippet) or r.position < prev.position:
                    seen[key] = r
                continue
            seen[key] = r
    return list(seen.values())[:max(max_results * 3, max_results)]


# ─── BM25 keyword rerank (Phase 1 baseline; neural comes in Phase 2) ──────────

def _tokenize(text: str) -> list[str]:
    import re
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1]


def bm25_rerank(query: str, results: list[RawResult], *, k1: float = 1.5, b: float = 0.75
                ) -> list[tuple[RawResult, float]]:
    """Rank the merged set by BM25 over (title + snippet). Returns (result, score)
    sorted desc. Score normalized to 0..1 by the max. When scores are all 0
    (no term overlap, e.g. a purely semantic query), preserves engine order via a
    position-based tiebreak so results are never randomly shuffled."""
    q_terms = _tokenize(query)
    docs = [f"{r.title} {r.snippet}" for r in results]
    doc_tokens = [_tokenize(d) for d in docs]
    N = len(doc_tokens)
    if N == 0 or not q_terms:
        return [(r, 0.0) for r in results]
    avgdl = (sum(len(t) for t in doc_tokens) / N) or 1.0
    df: dict[str, int] = {}
    for toks in doc_tokens:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    scored: list[tuple[RawResult, float]] = []
    for r, toks in zip(results, doc_tokens):
        if not toks:
            scored.append((r, 0.0))
            continue
        dl = len(toks)
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for q in q_terms:
            if q not in tf:
                continue
            n_q = df.get(q, 0)
            idf = max(0.0, ((N - n_q + 0.5) / (n_q + 0.5)) + 1.0)
            f = tf[q]
            score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        scored.append((r, score))
    max_s = max((s for _, s in scored), default=0.0)
    # Tiebreak: engine position (lower = better) then original order, so zero-overlap
    # queries do not get randomly reordered by the merge dict.
    order = {id(r): i for i, (r, _) in enumerate(scored)}
    scored.sort(key=lambda rs: (-rs[1], rs[0].position, order[id(rs[0])]))
    if max_s > 0:
        scored = [(r, s / max_s) for r, s in scored]
    return scored


async def fetch_source_for_similar(url: str, *, timeout: int = 10, max_chars: int = 4000
                                    ) -> tuple[str, str]:
    """Fetch a URL for find_similar: returns (title, body_text). Uses a one-off
    impersonated HTTP fetch (not the per-engine SERL coordinator: this is a single
    arbitrary page, not a repeated engine hit, so pacing/cookies do not apply)."""
    try:
        from scrapling.engines.static import FetcherSession
        async with FetcherSession(impersonate=_IMPERSONATE_POOL, proxy=_PROXY,
                                  stealthy_headers=True, retries=1) as sess:
            resp = await sess.get(url, timeout=timeout)
            text = (getattr(resp, "body", None) or b"").decode(
                getattr(resp, "encoding", None) or "utf-8", errors="replace")
            status = getattr(resp, "status", 0) or 0
    except Exception:
        return "", ""
    if not text or _is_blocked(status, text):
        return "", ""
    title = ""
    try:
        soup = BeautifulSoup(text[:60000], "lxml")
        title_el = soup.find("title")
        if title_el:
            title = title_el.get_text(" ", strip=True)
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
    server=None,
) -> tuple[list[RawResult], list[EngineReport]]:
    """Run the chosen engines in parallel, merge, dedup, BM25-rerank.

    Returns (ranked_results, engine_reports). engines defaults to
    (duckduckgo, bing, wikipedia). Unknown engine names are ignored.

    Adaptive reserve tier: if the primary engines returned few results AND one was
    blocked, fan out to Google via the stealthy browser (Google is CAPTCHA-prone,
    so it is held in reserve, not hit by default). Skipped when google was already
    requested or no server is available (the reserve uses the stealthy browser).
    """
    names = [e for e in (engines or list(DEFAULT_ENGINES)) if e in _ENGINES]
    if not names:
        names = list(DEFAULT_ENGINES)

    async def _run_engine(name: str):
        # Hard per-engine deadline: a slow / blocked / stealthy-escalating engine
        # is cut off so it can never hang the whole search. The agent gets results
        # from the engines that finished; the cut one is reported as timed out.
        try:
            return await asyncio.wait_for(
                _ENGINES[name](query, max_results, region=region, freshness=freshness,
                               page=page, server=server),
                timeout=SEARCH_ENGINE_DEADLINE,
            )
        except asyncio.TimeoutError:
            return ([], EngineReport(name=name, blocked=True,
                                     error=f"timed out ({int(SEARCH_ENGINE_DEADLINE)}s)"))

    per_engine = await asyncio.gather(*[_run_engine(n) for n in names], return_exceptions=True)
    reports: list[EngineReport] = []
    cleaned: list[tuple[list[RawResult], EngineReport]] = []
    for name, res in zip(names, per_engine):
        if isinstance(res, BaseException):
            rep = EngineReport(name=name, error=redact(str(res)[:120]))
            reports.append(rep)
            cleaned.append(([], rep))
            logger.warning(f"engine {name} crashed: {res}")
        else:
            results, rep = res
            reports.append(rep)
            cleaned.append((results, rep))
    merged = merge_dedupe(cleaned, max_results, site=site, exclude_sites=exclude_sites)

    # Adaptive reserve tier (Google) when primaries fell short + one was blocked.
    if (server is not None and "google" not in names
            and len(merged) < 3 and any(r.blocked for _, r in cleaned)):
        try:
            g_res, g_rep = await asyncio.wait_for(
                search_google(query, max_results, region=region,
                              freshness=freshness, page=page, server=server),
                timeout=SEARCH_ENGINE_DEADLINE,
            )
            if g_res:
                cleaned.append((g_res, g_rep))
                reports.append(g_rep)
                merged = merge_dedupe(cleaned, max_results, site=site, exclude_sites=exclude_sites)
        except asyncio.TimeoutError:
            reports.append(EngineReport(name="google", blocked=True,
                                        error=f"timed out ({int(SEARCH_ENGINE_DEADLINE)}s)"))
        except Exception as e:
            logger.debug(f"reserve google fan-out failed: {e}")

    ranked = bm25_rerank(query, merged)
    return [r for r, _ in ranked], reports


def redact(s: str) -> str:
    """Light redaction for engine error strings surfaced to the agent."""
    return s

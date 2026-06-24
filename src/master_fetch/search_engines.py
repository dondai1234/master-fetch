"""Hound-native keyless search engine scrapers (v7 local search flagship).

No API key, no account, no third-party service. Scrapes public search engines
(DuckDuckGo, Bing, Qwant, Wikipedia) with a split transport: DDG + Bing render
their SERP in hound's single shared warm Patchright browser (the same one
smart_fetch uses) so a real browser fingerprint never hits the 429 wall that
kills curl_cffi under sustained use; Qwant (tolerant keyless JSON API) +
Wikipedia use browser-impersonated HTTP (scrapling FetcherSession, a CORE dep,
so lean installs get working search). ONE browser total, shared with smart_fetch
(eager + persistent at startup). No escalation - one transport per engine.
Google is NOT scraped: it CAPTCHAs even via the stealthy browser.

Diversity + consensus (the rate-limit fix that costs zero speed): the default
pool is three INDEPENDENT indexes (DuckDuckGo, Bing, Qwant) run in
parallel. They rarely all rate-limit at once (different clocks), so if 1-2 block
the others carry genuinely different results. Merging independent indexes also
yields a free authority signal: a URL returned by several engines is a CONSENSUS
hit. merge_dedupe counts the distinct index-families per URL (RawResult.consensus)
so the ranker can boost consensus hits at zero extra fetch cost.

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
  6. Diverse independent pool + cross-engine consensus: 4 independent indexes
     run in parallel (no single engine is a bottleneck); a URL returned by N
     distinct index-families gets a consensus boost (free authority signal).
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

DEFAULT_ENGINES = ("duckduckgo", "bing", "qwant")
ENGINE_TIMEOUT = 12  # seconds per engine request
CAPTCHA_MARKERS = ("captcha", "unusual traffic", "are you a robot", "sorry/image", "ddg-captcha", "blocked", "access denied", "to continue, please agree")

# Per-engine pacing: min seconds between successive requests to the SAME engine.
# Within a single search all engines fire in parallel (each engine sees 1 req),
# so this only spreads same-engine bursts across successive searches.
_PACE = {"duckduckgo": 1.2, "bing": 1.5, "qwant": 1.0, "wikipedia": 0.3, "yahoo": 1.5}
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

# Engines that curl_cffi/scrapling CANNOT reach (transport error) but stdlib
# urllib fetches fine (no TLS impersonation needed). Routed through urllib inside
# the SERL coordinator so they still get the pacer + circuit breaker. Brave is the
# key one: independent 30B-page index, but curl_cffi returns curl error 23 on
# search.brave.com while urllib returns 200 + parseable HTML.
# Per-engine TLS fingerprint pin. Qwant's bot check (captcha-delivery) only
# accepts the safari184 fingerprint under curl_cffi - chrome/edge get 403-captcha.
# Other engines use the rotation pool (_IMPERSONATE_POOL). A pinned engine still
# gets the pacer + circuit breaker; if the fingerprint stops working the circuit
# breaker marks it blocked and the other engines carry (no failure).
_ENGINE_IMPERSONATE = {"qwant": ["safari184"]}


@dataclass
class RawResult:
    title: str
    url: str
    snippet: str
    source: str  # engine name (the copy that won the dedup)
    position: int = 0  # 1-indexed within its engine
    consensus: int = 1       # distinct independent index-families that returned this URL
    sources: tuple = ()      # all engine names that returned this URL (set by merge_dedupe)


@dataclass
class EngineReport:
    name: str
    ok: bool = False        # parsed >=1 result
    blocked: bool = False   # rate-limited / CAPTCHA'd / refused / cooling down / timed out
    preempted: bool = False # cancelled because enough results arrived from other engines (NOT blocked)
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

    def _make_session(self, name: str = ""):
        from scrapling.engines.static import FetcherSession
        # Per-engine fingerprint pin (see _ENGINE_IMPERSONATE); default = rotation pool.
        imp = _ENGINE_IMPERSONATE.get(name, _IMPERSONATE_POOL)
        return FetcherSession(
            impersonate=imp, proxy=_PROXY,
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
                st.cm = self._make_session(st.name)
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
            st.last_req = time()

            text: Optional[str] = None
            status = 0
            try:
                await self._ensure_session(st)
                if st.sess is not None:
                    if method == "POST" and form is not None:
                        resp = await st.sess.post(url, data=form, timeout=timeout)
                    else:
                        resp = await st.sess.get(url, timeout=timeout)
                else:
                    # Per-request fallback if the persistent session would not create.
                    cm = self._make_session(name)
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
    "qwant": "https://api.qwant.com/v3/search/web?q=test&count=10&locale=en_US&offset=0&device=desktop&safesearch=0&tgp=1",
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


async def _browser_html(server, url: str, wait_sel: str, *, timeout: int = 7000) -> Optional[str]:
    """Render a SERP URL in hound's single shared warm Patchright browser (the
    same one smart_fetch uses) and return the page HTML.

    This is the search anti-bot transport for rate-limit-prone engines: a real
    browser fingerprint does not get rate-limited the way curl_cffi does, so
    DDG/Bing scraped this way never hit the 429 wall that kills the HTTP path
    under sustained use. wait_sel = the SERP result-container CSS selector; we
    return the moment it attaches (server-rendered SERPs are ready at
    domcontentloaded, no need to wait for full load / networkidle).
    disable_resources drops images/fonts/css/media for a fast render. Returns
    None if the browser is unavailable or yields no usable HTML.
    """
    if server is None:
        return None
    try:
        res = await server.stealthy_fetch(
            url, extraction_type="html", main_content_only=False,
            use_trafilatura=False, google_search=False, disable_resources=True,
            network_idle=False, wait_selector=wait_sel, wait_selector_state="attached",
            timeout=timeout,
        )
        if res and res.content and not res.error:
            html = "".join(res.content)
            if html and len(html) > 200:
                return html
    except Exception as e:
        logger.debug(f"browser SERP render failed for {url[:80]}: {e}")
    return None


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
    rep = EngineReport(name="duckduckgo")
    # Production (server provided): render the SERP in the shared warm browser.
    # A real browser fingerprint is not rate-limited like curl_cffi, so DDG
    # scraped this way never hits the 429 wall that kills the HTTP path under
    # sustained use. server=None (unit tests, no browser) uses the HTTP seam below.
    if server is not None:
        html = await _browser_html(server, url, ".result")
        if html:
            results = _parse_ddg(html)[:max_results]
            if results:
                rep.ok = True
                return results, rep
        rep.blocked = True
        rep.error = "browser render failed"
        return [], rep
    # HTTP seam (tests / no browser available): SERL-coordinated curl_cffi.
    if _ENGINES_COORD.cooldown_left("duckduckgo") > 0:
        rep.blocked = True
        rep.error = (f"cooling down (rate-limited, ~{int(_ENGINES_COORD.cooldown_left('duckduckgo'))}s left)")
        return [], rep
    text, status, blocked, cooling = await _engine_get("duckduckgo", url)
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
    rep = EngineReport(name="bing")
    # Bing's SERP is heavy (102KB + lots of JS) -> a browser render takes ~11s,
    # too slow. Bing stays on HTTP (fast ~1s); when it rate-limits, the circuit
    # breaker rests it and the DDG browser backbone + Qwant carry. server is
    # accepted but unused (Bing is HTTP-primary, not browser).
    if _ENGINES_COORD.cooldown_left("bing") > 0:
        rep.blocked = True
        rep.error = (f"cooling down (rate-limited, ~{int(_ENGINES_COORD.cooldown_left('bing'))}s left)")
        return [], rep
    text, status, blocked, cooling = await _engine_get("bing", url)
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


# ─── Qwant (independent index, keyless JSON API) ─────────────────────────────

def _qwant_locale(region: str) -> str:
    # region is like 'us-en' (country-lang); Qwant wants 'en_US' (lang_COUNTRY).
    if region and "-" in region:
        parts = region.split("-")
        if len(parts) >= 2:
            return f"{parts[-1]}_{parts[0]}".upper()
    return "EN_US"


def _parse_qwant_json(text: str, max_results: int) -> list[RawResult]:
    """Parse Qwant's JSON API response: data.result.items.mainline[] -> web items."""
    import json
    out: list[RawResult] = []
    try:
        data = json.loads(text)
    except Exception:
        return out
    if data.get("status") != "success":
        return out  # error_code 24 (rate-limit) / captcha / 403 -> caller treats as blocked
    mainline = data.get("data", {}).get("result", {}).get("items", {}).get("mainline", [])
    for row in mainline:
        if row.get("type") != "web":
            continue  # skip ads / images / videos / news rows
        for item in row.get("items", []):
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            if not title or not url or not url.startswith("http"):
                continue
            snippet = (item.get("desc") or "").strip()
            out.append(RawResult(title=title, url=url, snippet=snippet, source="qwant", position=len(out) + 1))
            if len(out) >= max_results:
                return out
    return out


async def search_qwant(query: str, max_results: int, *, region: str = "us-en",
                       freshness: Optional[str] = None, page: int = 0,
                       server=None) -> tuple[list[RawResult], EngineReport]:
    # Qwant: keyless JSON API (api.qwant.com/v3/search/web). Own index + Bing feed
    # = independent from DDG. Much more rate-limit-tolerant than HTML-scraping
    # engines (10/10 rapid calls, 0 blocks in live testing). curl_cffi passes Qwant's bot check ONLY with
    # the safari184 fingerprint, so the coordinator pins it via _ENGINE_IMPERSONATE.
    # Clean JSON parsing (no fragile HTML selectors). No native freshness param;
    # pagination via &offset=. No per-engine stealthy escalation (keeps search fast;
    # the search-wide last-resort was removed: search is 100% HTTP, never touches
    # the stealthy browser).
    locale = _qwant_locale(region)
    # Qwant's API REQUIRES count=10 exactly (any other value -> 400 "count must be
    # equal to 10"). Fetch 10, then _parse_qwant_json slices to max_results.
    args = (f"q={quote(query)}&count=10&locale={locale}"
            f"&offset={page * 10}&device=desktop&safesearch=0&tgp=1")
    url = f"https://api.qwant.com/v3/search/web?{args}"
    text, status, blocked, cooling = await _engine_get("qwant", url)
    rep = EngineReport(name="qwant")
    if not text:
        rep.blocked = blocked
        rep.error = (f"cooling down (rate-limited, ~{int(_ENGINES_COORD.cooldown_left('qwant'))}s left)"
                     if cooling else f"no response (status {status})")
        return [], rep
    results = _parse_qwant_json(text, max_results)
    rep.ok = bool(results)
    if not results:
        # Qwant sometimes returns HTTP 200 with a non-success JSON body on
        # rate-limit/captcha (error_code 24 / captchaUrl). Detect + mark blocked.
        low = text[:600].lower()
        if ("captcha" in low or '"status":"error"' in low or '"error_code"' in low
                or status in (403, 429, 202)):
            rep.blocked = True
            rep.error = "qwant rate-limit/captcha (no results)"
        else:
            rep.error = "no results parsed"
    return results, rep


# ─── Yahoo (Bing-feed index; opt-in redundancy for when Bing rate-limits) ────

def _yahoo_real_url(href: str) -> str:
    """Decode a Yahoo redirect (r.search.yahoo.com/.../RU=ENCODED/RK=...) to the real URL."""
    if not href:
        return ""
    if "/RU=" in href:
        raw = href.split("/RU=", 1)[1].split("/RK=", 1)[0].split("/RS=", 1)[0]
        decoded = unquote(raw)
        if decoded.startswith("http"):
            return decoded
    return href if href.startswith("http") else ""


def _parse_yahoo(html: str) -> list[RawResult]:
    soup = BeautifulSoup(html, "lxml")
    out: list[RawResult] = []
    for i, el in enumerate(soup.select("div.algo-sr, div.algo")):
        a = el.select_one("a[href]")
        if not a:
            continue
        href = _yahoo_real_url(a.get("href", "") or "")
        if not href:
            continue
        title_el = el.select_one("h3.title") or el.select_one(".compTitle a") or a
        title = title_el.get_text(" ", strip=True)
        if not title:
            continue
        snip = el.select_one(".compText") or el.select_one("p")
        snippet = snip.get_text(" ", strip=True) if snip else ""
        out.append(RawResult(title=title, url=href, snippet=snippet, source="yahoo", position=i + 1))
    return out


async def search_yahoo(query: str, max_results: int, *, region: str = "us-en",
                       freshness: Optional[str] = None, page: int = 0,
                       server=None) -> tuple[list[RawResult], EngineReport]:
    # Yahoo serves Bing's index from Yahoo's own servers (a different IP/rate
    # bucket than bing.com): a redundancy source for Bing's index when bing.com
    # rate-limits. Not a default (same index as bing -> no diversity); opt-in.
    params = f"p={quote(query)}&n={max(min(max_results * 2, 50), 10)}"
    if page > 0:
        params += f"&b={(page * max_results) + 1}"
    url = f"https://search.yahoo.com/search?{params}"
    text, status, blocked, cooling = await _engine_get("yahoo", url)
    rep = EngineReport(name="yahoo")
    if not text:
        rep.blocked = blocked
        rep.error = (f"cooling down (rate-limited, ~{int(_ENGINES_COORD.cooldown_left('yahoo'))}s left)"
                     if cooling else f"no response (status {status})")
        return [], rep
    results = _parse_yahoo(text)[:max_results]
    rep.ok = bool(results)
    rep.blocked = (not results) and blocked
    if not results:
        rep.error = "no results parsed"
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

# Each engine's independent index "family". Consensus counts DISTINCT families
# that returned a URL (a free authority signal): bing+yahoo agreeing = 1 family
# (correlated Bing feed, weak); bing+qwant+wikipedia = 3 families (independent, strong).
_INDEX_FAMILY = {"duckduckgo": "duckduckgo", "bing": "bing", "yahoo": "bing",
"qwant": "qwant", "wikipedia": "wikipedia"}

_ENGINES = {
    "duckduckgo": search_ddg,
    "bing": search_bing,
    "qwant": search_qwant,
    "yahoo": search_yahoo,
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

    Cross-engine consensus: tracks which engines returned each URL and stamps
    RawResult.consensus = number of DISTINCT index-families that returned it (a
    free authority signal for the ranker). Results are pre-sorted by consensus
    then engine position so consensus hits surface even on zero-overlap queries.
    """
    seen: dict[str, RawResult] = {}
    sources_by_key: dict[str, set[str]] = {}
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
            sources_by_key.setdefault(key, set()).add(r.source)
            if key in seen:
                prev = seen[key]
                if (not prev.snippet and r.snippet) or r.position < prev.position:
                    seen[key] = r
                continue
            seen[key] = r
    out: list[RawResult] = []
    for key, r in seen.items():
        srcs = sources_by_key.get(key, {r.source})
        fams = {_INDEX_FAMILY.get(s, s) for s in srcs}
        r.consensus = len(fams)
        r.sources = tuple(sorted(srcs))
        out.append(r)
    out.sort(key=lambda r: (-r.consensus, r.position))
    return out[:max(max_results * 3, max_results)]


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
    """Run the chosen engines in a parallel race, merge, dedup.

    Returns (ranked_results, engine_reports). engines defaults to the three
    independent indexes (duckduckgo, bing, qwant). Unknown engine names are
    ignored. DDG renders its SERP in the shared warm browser (never-429 backbone);
    Bing/Qwant/Wikipedia/Yahoo run over HTTP. All start concurrently at t=0 and
    the search returns the moment enough results have merged (cancelling
    laggards) - fast when HTTP is healthy, reliable (via the DDG browser) when
    every HTTP engine 429s. A URL returned by several engines carries a consensus
    boost (see merge_dedupe) - a free authority signal from merging independent
    indexes.
    """
    names = [e for e in (engines or list(DEFAULT_ENGINES)) if e in _ENGINES]
    if not names:
        names = list(DEFAULT_ENGINES)

    # Parallel race (NOT escalation - no fallback order): every engine starts
    # concurrently at t=0. DDG renders its SERP in the shared warm browser (real
    # browser = no 429 wall, the never-blocked backbone); Bing/Qwant/Wikipedia/
    # Yahoo run over HTTP. We return the MOMENT enough results have merged from
    # the engines that finished, cancelling the laggards - so when the HTTP
    # engines are healthy the search returns in ~1s (the DDG browser render is
    # cancelled early, it ran in parallel not as a fallback), and when every HTTP
    # engine 429s the DDG browser still delivers (~3-5s) so the search is never
    # dead. A hard overall deadline bounds a fully-throttled search.
    tasks = {asyncio.ensure_future(
        _ENGINES[n](query, max_results, region=region, freshness=freshness,
                    page=page, server=server)): n for n in names}
    reports: list[EngineReport] = []
    cleaned: list[tuple[list[RawResult], EngineReport]] = []
    pending = set(tasks)
    deadline = time() + SEARCH_ENGINE_DEADLINE

    async def _cancel_laggards(*, blocked: bool, error: str) -> None:
        for t in pending:
            t.cancel()
        for t in list(pending):
            try:
                await t
            except BaseException:
                pass
            reports.append(EngineReport(name=tasks[t], blocked=blocked,
                                        preempted=(not blocked), error=error))

    while pending and time() < deadline:
        timeout = max(0.1, deadline - time())
        try:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED,
                                                timeout=timeout)
        except Exception:
            break
        if not done:
            break  # nothing finished this window; stop waiting
        for t in done:
            name = tasks[t]
            try:
                results, rep = t.result()
            except BaseException as e:
                rep = EngineReport(name=name, error=redact(str(e)[:120]))
                results = []
                logger.warning(f"engine {name} crashed: {e}")
            reports.append(rep)
            cleaned.append((results, rep))
        merged = merge_dedupe(cleaned, max_results, site=site, exclude_sites=exclude_sites)
        if len(merged) >= max_results:
            # Enough good results - cancel the laggards (e.g. the still-rendering
            # DDG browser page) and return now. Awaiting the cancels lets scrapling
            # close the cancelled page promptly (no browser leak under load).
            await _cancel_laggards(blocked=False, error="preempted (enough results from other engines)")
            return merged, reports
    await _cancel_laggards(blocked=True, error=f"timed out ({int(SEARCH_ENGINE_DEADLINE)}s)")
    ranked = merge_dedupe(cleaned, max_results, site=site, exclude_sites=exclude_sites)
    return ranked, reports


def redact(s: str) -> str:
    """Light redaction for engine error strings surfaced to the agent."""
    return s

"""Bring Your Own Key (BYOK) search API engines for Hound.

Five API-backed search engines that use user-provided API keys. When keys are
configured (via env vars or ~/.hound/search_keys.json), these engines become
the PRIMARY search sources, with hound's keyless local engines as fallback.

Each provider supports multiple keys (key rotation): if one key hits a rate
limit (429), the engine automatically switches to the next key for the same
provider. If all keys are exhausted, the engine raises MetaBlockedException
(circuit breaker opens, local engines carry the search).

Providers (all require user-provided API keys):
  - serper:     Google SERP API, X-API-KEY header, 2,500 free credits
  - tavily:     AI search API, Bearer auth, 1,000 credits/month
  - exa:        Neural search API, x-api-key header, 1,000 searches/month
  - firecrawl:  Web search API, Bearer auth, 1,000 credits/month
  - tinyfish:   Web search API, X-API-Key header, 30 req/min free

Transport: httpx (already a core dep). No TLS fingerprint impersonation needed
for JSON APIs. Key rotation state persists across searches (module-level pools).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import quote_plus

import httpx

from master_fetch.byok_config import load_byok_keys, BYOK_PROVIDERS
from master_fetch.search_metasearch import (
    BaseSearchEngine,
    TextResult,
    MetaBlockedException,
    MetaTimeoutException,
    MetaSearchException,
    _PROXY,
)
from master_fetch.api_backends import _SimpleHttpxClient, _strip_site_prefixes

logger = logging.getLogger("master-fetch.search_api_keys")

# Default per-engine result count. Enough to contribute to ranking without
# drowning out other engines.
_API_LIMIT = 10


# ─── key rotation pool ──────────────────────────────────────────────────────

class KeyPool:
    """Manages multiple API keys for a single provider with rotation.

    Round-robin selection with rate-limit tracking. When a key gets 429,
    it's marked as rate-limited for a cooldown (60s). The next available
    key is used. If all keys are rate-limited, MetaBlockedException is raised
    so the circuit breaker opens for this engine.

    State is in-memory only (not persisted). Resets on restart.
    Thread-safe enough for the metasearch's asyncio.to_thread usage:
    search() runs in a thread but each engine has its own pool, and the
    metasearch creates a new engine instance per search but shares the
    module-level pool.
    """

    # Rate-limit cooldown: a key that gets 429 is skipped for this long.
    RATE_LIMIT_COOLDOWN = 60.0
    # Invalid key cooldown: 401/403 suggests the key is wrong/expired.
    # Longer cooldown since retrying a bad key is wasteful.
    INVALID_KEY_COOLDOWN = 300.0

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("KeyPool requires at least one key")
        self._keys = list(keys)
        # Per-key state: {"rate_limited_until": float, "error": str}
        self._state: dict[str, dict[str, Any]] = {k: {} for k in self._keys}
        self._idx = 0  # round-robin pointer

    @property
    def size(self) -> int:
        return len(self._keys)

    def get_key(self) -> str:
        """Return the next available (non-rate-limited) key.

        Raises MetaBlockedException if all keys are rate-limited.
        """
        now = time.time()
        for i in range(len(self._keys)):
            key = self._keys[(self._idx + i) % len(self._keys)]
            state = self._state.get(key, {})
            until = state.get("rate_limited_until", 0)
            if until < now:
                # This key is available.
                self._idx = (self._idx + i + 1) % len(self._keys)
                return key
        # All keys rate-limited.
        available_at = min(
            self._state.get(k, {}).get("rate_limited_until", 0)
            for k in self._keys
        )
        wait = max(0, available_at - now)
        raise MetaBlockedException(
            f"All {len(self._keys)} keys rate-limited; next available in {wait:.0f}s"
        )

    def mark_rate_limited(self, key: str) -> None:
        """Mark a key as rate-limited (429 response)."""
        self._state.setdefault(key, {})["rate_limited_until"] = (
            time.time() + self.RATE_LIMIT_COOLDOWN
        )
        self._state[key]["error"] = "rate_limited"
        logger.debug("BYOK key %s rate-limited for %ss", key[:8] + "...", self.RATE_LIMIT_COOLDOWN)

    def mark_invalid(self, key: str) -> None:
        """Mark a key as invalid (401/403 response). Longer cooldown."""
        self._state.setdefault(key, {})["rate_limited_until"] = (
            time.time() + self.INVALID_KEY_COOLDOWN
        )
        self._state[key]["error"] = "invalid"
        logger.debug("BYOK key %s marked invalid for %ss", key[:8] + "...", self.INVALID_KEY_COOLDOWN)

    def mark_success(self, key: str) -> None:
        """Clear any rate-limit state for a key that just succeeded."""
        self._state.setdefault(key, {}).pop("rate_limited_until", None)
        self._state[key].pop("error", None)

    def status(self) -> list[dict[str, Any]]:
        """Return per-key status for diagnostics."""
        now = time.time()
        result = []
        for k in self._keys:
            state = self._state.get(k, {})
            until = state.get("rate_limited_until", 0)
            if until > now:
                result.append({
                    "key": k[:8] + "..." + k[-4:] if len(k) > 12 else "***",
                    "status": state.get("error", "rate_limited"),
                    "cooldown_remaining": round(until - now, 1),
                })
            else:
                result.append({"key": k[:8] + "..." + k[-4:] if len(k) > 12 else "***", "status": "active"})
        return result


# Module-level singleton pools: {provider: KeyPool}
# Created on first use, shared across all engine instances.
_POOLS: dict[str, KeyPool] = {}


def _get_pool(provider: str) -> KeyPool | None:
    """Get or create the KeyPool for a provider. Returns None if no keys."""
    if provider in _POOLS:
        return _POOLS[provider]
    keys = load_byok_keys().get(provider, [])
    if not keys:
        return None
    pool = KeyPool(keys)
    _POOLS[provider] = pool
    return pool


def _refresh_pools() -> None:
    """Reload keys from config and reconcile pools with the configured keys.

    Called lazily by _register_byok_backends() so key additions, removals, and
    reordered keys from the CLI are picked up without restarting the server.
    """
    all_keys = load_byok_keys()
    for provider in BYOK_PROVIDERS:
        keys = all_keys.get(provider, [])
        if keys:
            if provider in _POOLS:
                # Keep state for retained keys, but make both pool membership and
                # order exactly match the current configuration. Preserve the next
                # surviving key in the old rotation where possible.
                old_pool = _POOLS[provider]
                old_keys = old_pool._keys
                next_key = None
                if old_keys:
                    for offset in range(len(old_keys)):
                        candidate = old_keys[(old_pool._idx + offset) % len(old_keys)]
                        if candidate in keys:
                            next_key = candidate
                            break
                old_state = old_pool._state
                old_pool._keys = list(keys)
                old_pool._state = {key: old_state.get(key, {}) for key in keys}
                old_pool._idx = old_pool._keys.index(next_key) if next_key else 0
            else:
                _POOLS[provider] = KeyPool(keys)
        else:
            _POOLS.pop(provider, None)


def _byok_pool_status() -> dict[str, Any]:
    """Return status of all BYOK key pools for diagnostics."""
    _refresh_pools()
    return {provider: pool.status() for provider, pool in _POOLS.items()}


def _reset_byok_pools() -> None:
    """Test hook: clear all BYOK pools."""
    _POOLS.clear()


# ─── base BYOK engine ───────────────────────────────────────────────────────

class BaseBYOKEngine(BaseSearchEngine):
    """Base class for BYOK search engines. Subclasses implement
    _build_request() and _parse_results(). Handles key rotation automatically."""

    provider_name: str = ""  # e.g. "serper", "tavily"
    search_url: str = ""
    search_method: str = "POST"  # GET or POST

    def __init__(self, proxy: str | None = None, timeout: int | None = None,
                 *, verify: bool = True) -> None:
        self.http_client = _SimpleHttpxClient(
            proxy=proxy or _PROXY,
            timeout=timeout or 8,
        )
        self.results: list[Any] = []

    @property
    def result_type(self) -> type:
        return TextResult

    def _build_request(self, query: str, key: str, *,
        site: str | None = None,
        exclude_sites: list[str] | None = None,
        timelimit: str | None = None,
        region: str = "us-en",
        page: int = 1,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Build (body/params, headers) for the API request using this key.

        Subclasses map hound's generic params (site, exclude_sites, timelimit,
        region, page) to each provider's native API parameters.
        """
        raise NotImplementedError

    def _parse_results(self, data: dict[str, Any]) -> list[TextResult]:
        """Parse the JSON response into TextResult objects."""
        raise NotImplementedError

    def search(self, query: str, region: str = "us-en", safesearch: str = "moderate",
               timelimit: str | None = None, page: int = 1, **kwargs: str
               ) -> list[TextResult] | None:
        """Make the API request with key rotation. Strips site: prefixes."""
        pool = _get_pool(self.provider_name)
        if pool is None:
            return None

        clean_query = _strip_site_prefixes(query)
        if not clean_query.strip():
            return None

        # Try each key in the pool until one works or all are exhausted.
        tried_keys: set[str] = set()
        last_error: str = ""

        while True:
            try:
                key = pool.get_key()
            except MetaBlockedException:
                # All keys rate-limited.
                if last_error:
                    raise MetaBlockedException(f"All keys exhausted: {last_error}")
                raise

            if key in tried_keys:
                # We've already tried all available keys in this call.
                raise MetaBlockedException(f"All keys failed: {last_error}")
            tried_keys.add(key)

            try:
                data, headers = self._build_request(
                    clean_query, key,
                    site=kwargs.get("site"),
                    exclude_sites=kwargs.get("exclude_sites"),
                    timelimit=timelimit,
                    region=region,
                    page=page,
                )
            except Exception as ex:
                last_error = f"{type(ex).__name__}: {ex}"
                pool.mark_invalid(key)
                continue

            try:
                if self.search_method == "GET":
                    resp = self.http_client.request("GET", self.search_url, params=data, headers=headers)
                else:
                    resp = self.http_client.request("POST", self.search_url, json=data, headers=headers)
            except (MetaTimeoutException, MetaSearchException) as ex:
                # Network error — don't mark the key as bad, just fail.
                logger.debug("%s request failed: %r", self.provider_name, ex)
                return None

            # Check status codes.
            if resp.status_code == 429:
                pool.mark_rate_limited(key)
                last_error = "rate_limited (429)"
                continue  # try next key
            if resp.status_code in (401, 403):
                pool.mark_invalid(key)
                last_error = f"unauthorized ({resp.status_code})"
                continue  # try next key
            if resp.status_code in (502, 503):
                # Server error — not the key's fault. Don't mark the key.
                logger.debug("%s server error: %d", self.provider_name, resp.status_code)
                return None
            if resp.status_code != 200:
                logger.debug("%s returned HTTP %d", self.provider_name, resp.status_code)
                return None

            # Success — parse the response.
            pool.mark_success(key)
            try:
                resp_data = json.loads(resp.text)
            except (json.JSONDecodeError, ValueError) as ex:
                logger.debug("%s JSON parse failed: %r", self.provider_name, ex)
                return None

            if not isinstance(resp_data, dict):
                return None

            try:
                results = self._parse_results(resp_data)
            except Exception as ex:
                logger.debug("%s parse error: %r", self.provider_name, ex)
                return None

            self.results = results
            return results if results else None


# ─── Serper (Google SERP API) ───────────────────────────────────────────────

class SerperEngine(BaseBYOKEngine):
    """Serper.dev - Google SERP API.

    Maps hound params to Serper's native API:
    - timelimit (d/w/m/y) -> tbs (qdr:d/qdr:w/qdr:m/qdr:y)
    - page -> page (pagination, 10 results per page)
    - gl/hl already sent
    """
    name = "serper"
    provider = "serper"
    provider_name = "serper"
    search_url = "https://google.serper.dev/search"
    search_method = "POST"

    _TBS_MAP = {"d": "qdr:d", "w": "qdr:w", "m": "qdr:m", "y": "qdr:y"}

    def _build_request(self, query: str, key: str, *,
        site: str | None = None,
        exclude_sites: list[str] | None = None,
        timelimit: str | None = None,
        region: str = "us-en",
        page: int = 1,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        body: dict[str, Any] = {"q": query, "num": _API_LIMIT}
        # Parse region (format: "us-en" -> gl="us", hl="en")
        parts = region.split("-", 1)
        body["gl"] = parts[0] if parts else "us"
        body["hl"] = parts[1] if len(parts) > 1 else "en"
        if page > 1:
            body["page"] = page
        if timelimit and timelimit in self._TBS_MAP:
            body["tbs"] = self._TBS_MAP[timelimit]
        # Serper supports domain filtering via query operator, not native params.
        # The site:/-site: prefixes are already in the query (applied by multi_search)
        # and stripped by BaseBYOKEngine.search(). For Serper, we re-apply them
        # since Serper's API passes the query directly to Google.
        if site:
            body["q"] = f"site:{site} {body['q']}"
        for ex in exclude_sites or []:
            body["q"] = f"-site:{ex} {body['q']}"
        return (
            body,
            {"X-API-KEY": key, "Content-Type": "application/json"},
        )

    def _parse_results(self, data: dict[str, Any]) -> list[TextResult]:
        organic = data.get("organic") or []
        results: list[TextResult] = []
        for r in organic:
            title = r.get("title", "")
            link = r.get("link", "")
            snippet = r.get("snippet", "")
            if not title or not link:
                continue
            # Rich snippet from date + source if available.
            date = r.get("date", "")
            if date and snippet:
                snippet = f"{date} - {snippet}"
            elif date:
                snippet = date
            results.append(TextResult(title=title, href=link, body=snippet))
        return results


# ─── Tavily (AI search API) ─────────────────────────────────────────────────

class TavilyEngine(BaseBYOKEngine):
    """Tavily - AI search API optimized for LLM consumption.

    Maps hound params to Tavily's native API:
    - timelimit (d/w/m/y) -> time_range (day/week/month/year)
    - site -> include_domains
    - exclude_sites -> exclude_domains
    - Uses search_depth="advanced" for higher quality results
    """
    name = "tavily"
    provider = "tavily"
    provider_name = "tavily"
    search_url = "https://api.tavily.com/search"
    search_method = "POST"

    _TIME_RANGE_MAP = {"d": "day", "w": "week", "m": "month", "y": "year"}

    def _build_request(self, query: str, key: str, *,
        site: str | None = None,
        exclude_sites: list[str] | None = None,
        timelimit: str | None = None,
        region: str = "us-en",
        page: int = 1,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        body: dict[str, Any] = {
            "query": query,
            "max_results": _API_LIMIT,
            "search_depth": "advanced",
            "topic": "general",
        }
        if site:
            body["include_domains"] = [site]
        if exclude_sites:
            body["exclude_domains"] = list(exclude_sites)
        if timelimit and timelimit in self._TIME_RANGE_MAP:
            body["time_range"] = self._TIME_RANGE_MAP[timelimit]
        return (
            body,
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )

    def _parse_results(self, data: dict[str, Any]) -> list[TextResult]:
        results_list = data.get("results") or []
        results: list[TextResult] = []
        for r in results_list:
            title = r.get("title", "")
            url = r.get("url", "")
            content = r.get("content", "")
            if not url:
                continue
            if not title:
                title = url[:60]
            # Tavily's content is a substantial snippet (not full page text).
            # Cap it to keep results compact for ranking.
            if content and len(content) > 400:
                content = content[:400] + "..."
            results.append(TextResult(title=title, href=url, body=content))
        return results


# ─── Exa (Neural search API) ─────────────────────────────────────────────────

class ExaEngine(BaseBYOKEngine):
    """Exa - Neural/semantic search API.

    Maps hound params to Exa's native API:
    - site -> includeDomains
    - exclude_sites -> excludeDomains
    - timelimit (d/w/m/y) -> startPublishedDate (ISO 8601 date)
    - contents: {highlights: true} requests snippet text (CRITICAL - without
      this, Exa returns only title+url with no snippet/body)
    """
    name = "exa"
    provider = "exa"
    provider_name = "exa"
    search_url = "https://api.exa.ai/search"
    search_method = "POST"

    # Days per timelimit unit.
    _DAYS_MAP = {"d": 1, "w": 7, "m": 30, "y": 365}

    def _build_request(self, query: str, key: str, *,
        site: str | None = None,
        exclude_sites: list[str] | None = None,
        timelimit: str | None = None,
        region: str = "us-en",
        page: int = 1,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        body: dict[str, Any] = {
            "query": query,
            "numResults": _API_LIMIT,
            "type": "auto",
            # CRITICAL: without contents.highlights, Exa returns no snippets.
            # Highlights are 10x more token-efficient than full text.
            "contents": {"highlights": True},
        }
        if site:
            body["includeDomains"] = [site]
        if exclude_sites:
            body["excludeDomains"] = list(exclude_sites)
        if timelimit and timelimit in self._DAYS_MAP:
            from datetime import datetime, timedelta, timezone
            cutoff = datetime.now(timezone.utc) - timedelta(days=self._DAYS_MAP[timelimit])
            body["startPublishedDate"] = cutoff.strftime("%Y-%m-%d")
        return (
            body,
            {"x-api-key": key, "Content-Type": "application/json"},
        )

    def _parse_results(self, data: dict[str, Any]) -> list[TextResult]:
        results_list = data.get("results") or []
        results: list[TextResult] = []
        for r in results_list:
            title = r.get("title", "")
            url = r.get("url", "")
            if not url:
                continue
            if not title:
                try:
                    from urllib.parse import urlparse
                    title = urlparse(url).hostname or url[:60]
                except Exception:
                    title = url[:60]
            # Build rich snippet from highlights + metadata.
            # Exa returns highlights at the TOP LEVEL of each result (not
            # nested under contents), as a list of relevant text snippets.
            highlights = r.get("highlights") or []
            parts: list[str] = []
            if isinstance(highlights, list) and highlights:
                # Join first 2 highlights for a compact snippet.
                joined = " ... ".join(str(h)[:200] for h in highlights[:2])
                if joined:
                    parts.append(joined[:400])
            published = r.get("publishedDate", "")
            if published:
                parts.append(f"Published: {published[:10]}")
            author = r.get("author", "")
            if author:
                parts.append(f"Author: {author}")
            snippet = " | ".join(parts) if parts else title
            results.append(TextResult(title=title, href=url, body=snippet))
        return results


# ─── Firecrawl (Web search API) ──────────────────────────────────────────────

class FirecrawlEngine(BaseBYOKEngine):
    """Firecrawl - Web search + scrape API.

    Maps hound params to Firecrawl v2's native API:
    - site -> includeDomains
    - exclude_sites -> excludeDomains
    - timelimit (d/w/m/y) -> tbs (qdr:d/qdr:w/qdr:m/qdr:y)
    """
    name = "firecrawl"
    provider = "firecrawl"
    provider_name = "firecrawl"
    search_url = "https://api.firecrawl.dev/v2/search"
    search_method = "POST"

    _TBS_MAP = {"d": "qdr:d", "w": "qdr:w", "m": "qdr:m", "y": "qdr:y"}

    def _build_request(self, query: str, key: str, *,
        site: str | None = None,
        exclude_sites: list[str] | None = None,
        timelimit: str | None = None,
        region: str = "us-en",
        page: int = 1,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        body: dict[str, Any] = {"query": query, "limit": _API_LIMIT}
        if site:
            body["includeDomains"] = [site]
        if exclude_sites:
            body["excludeDomains"] = list(exclude_sites)
        if timelimit and timelimit in self._TBS_MAP:
            body["tbs"] = self._TBS_MAP[timelimit]
        return (
            body,
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )

    def _parse_results(self, data: dict[str, Any]) -> list[TextResult]:
        # Firecrawl v2 response: {success: true, data: {web: [...]}}
        # Or fallback: {data: [...]}
        raw_data = data.get("data", data)
        results_list: list[dict[str, Any]] = []
        if isinstance(raw_data, dict):
            results_list = raw_data.get("web") or raw_data.get("results") or []
        elif isinstance(raw_data, list):
            results_list = raw_data
        results: list[TextResult] = []
        for r in results_list:
            title = r.get("title", "")
            url = r.get("url", "")
            description = r.get("description", "")
            highlights = r.get("highlights", [])
            if not url:
                continue
            if not title:
                title = url[:60]
            parts: list[str] = []
            if description:
                parts.append(description[:300])
            if isinstance(highlights, list) and highlights:
                joined = " ".join(str(h) for h in highlights[:3])
                if joined:
                    parts.append(joined[:200])
            snippet = " | ".join(parts) if parts else title
            results.append(TextResult(title=title, href=url, body=snippet))
        return results


# ─── TinyFish (Web search API) ───────────────────────────────────────────────

class TinyFishEngine(BaseBYOKEngine):
    """TinyFish - Web search API for AI agents.

    Maps hound params to TinyFish's native API:
    - timelimit (d/w/m/y) -> after_date (ISO 8601 date)
    - region -> location
    """
    name = "tinyfish"
    provider = "tinyfish"
    provider_name = "tinyfish"
    search_url = "https://api.search.tinyfish.ai"
    search_method = "GET"

    _DAYS_MAP = {"d": 1, "w": 7, "m": 30, "y": 365}

    def _build_request(self, query: str, key: str, *,
        site: str | None = None,
        exclude_sites: list[str] | None = None,
        timelimit: str | None = None,
        region: str = "us-en",
        page: int = 1,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        params: dict[str, Any] = {"query": query, "limit": _API_LIMIT}
        if timelimit and timelimit in self._DAYS_MAP:
            from datetime import datetime, timedelta, timezone
            cutoff = datetime.now(timezone.utc) - timedelta(days=self._DAYS_MAP[timelimit])
            params["after_date"] = cutoff.strftime("%Y-%m-%d")
        # TinyFish doesn't have native include/exclude domain params via GET,
        # so re-apply site:/-site: operators in the query.
        if site:
            params["query"] = f"site:{site} {params['query']}"
        for ex in exclude_sites or []:
            params["query"] = f"-site:{ex} {params['query']}"
        return (
            params,
            {"X-API-Key": key},
        )

    def _parse_results(self, data: dict[str, Any]) -> list[TextResult]:
        results_list = data.get("results") or []
        results: list[TextResult] = []
        for r in results_list:
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("snippet", "")
            if not url:
                continue
            if not title:
                title = url[:60]
            results.append(TextResult(title=title, href=url, body=snippet))
        return results


# ─── engine registration ─────────────────────────────────────────────────────

# Map provider name -> engine class.
_BYOK_ENGINES: dict[str, type[BaseBYOKEngine]] = {
    "serper": SerperEngine,
    "tavily": TavilyEngine,
    "exa": ExaEngine,
    "firecrawl": FirecrawlEngine,
    "tinyfish": TinyFishEngine,
}


def get_byok_engines() -> dict[str, type[BaseBYOKEngine]]:
    """Return {engine_name: engine_class} for all providers with keys configured.

    Refreshes pools first so newly-added keys are picked up.
    """
    _refresh_pools()
    result: dict[str, type[BaseBYOKEngine]] = {}
    for provider, cls in _BYOK_ENGINES.items():
        if provider in _POOLS:
            result[cls.name] = cls
    return result

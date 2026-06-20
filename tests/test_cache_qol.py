"""Tests for the v3.7 caching hardening:

  * Bad content is NOT cached (JS shells / 4xx-5xx / bot challenges / empty) —
    fixes the bug where broken pages were served from cache for the whole TTL.
  * Cache size cap evicts the oldest entries so a long-lived agent's DB can't
    grow unbounded.
  * Search cache key includes max_results so different result-set sizes don't
    collide on one cached entry.
"""

import pytest
from unittest.mock import AsyncMock
from pathlib import Path

from master_fetch.server import MasterFetchServer, ResponseModel, _is_cacheable
from master_fetch.cache import set_cached, get_cached, MAX_CACHE_ENTRIES
import master_fetch.cache as cache_mod
import master_fetch.server as srv_mod
import master_fetch.search as search_mod


# ─── _is_cacheable ────────────────────────────────────────────────────────

class TestIsCacheable:
    def _r(self, status=200, content=None, error=""):
        return ResponseModel(
            status=status, content=content or ["real content"],
            url="https://x.com", fetcher_used="http", error=error,
        )

    def test_clean_content_cacheable(self):
        assert _is_cacheable(self._r(status=200, content=["real content"])) is True

    def test_js_shell_not_cacheable(self):
        assert _is_cacheable(self._r(error="js_shell_detected: needs JS")) is False

    def test_bot_challenge_not_cacheable(self):
        assert _is_cacheable(self._r(status=503, error="bot_challenge_detected: cf")) is False

    def test_404_not_cacheable(self):
        assert _is_cacheable(self._r(status=404, content=["Not Found"])) is False

    def test_500_not_cacheable(self):
        assert _is_cacheable(self._r(status=500)) is False

    def test_network_error_not_cacheable(self):
        assert _is_cacheable(self._r(status=0, content=[""])) is False

    def test_empty_content_not_cacheable(self):
        assert _is_cacheable(self._r(status=200, content=["  "])) is False

    def test_blank_list_not_cacheable(self):
        assert _is_cacheable(self._r(status=200, content=[""])) is False


# ─── _finalize_result caching condition ───────────────────────────────────

class TestFinalizeResultCaching:
    """_finalize_result must only persist clean content to the cache."""

    @pytest.fixture
    def mocked_set_cached(self):
        orig = srv_mod.set_cached
        m = AsyncMock(return_value=None)
        srv_mod.set_cached = m
        try:
            yield m
        finally:
            srv_mod.set_cached = orig

    @pytest.mark.asyncio
    async def test_clean_content_cached(self, mocked_set_cached):
        srv = MasterFetchServer()
        r = ResponseModel(status=200, content=["real article body"], url="https://x.com",
                          fetcher_used="http", extracted_type="markdown")
        await srv._finalize_result(r, "https://x.com", "markdown", None, 3600, 0)
        assert mocked_set_cached.await_count == 1

    @pytest.mark.asyncio
    async def test_js_shell_not_cached(self, mocked_set_cached):
        srv = MasterFetchServer()
        r = ResponseModel(status=200, content=["Please enable JavaScript to view this page."],
                          url="https://x.com", fetcher_used="http", extracted_type="markdown")
        # _annotate_quality will flag this as a JS shell -> not cached
        await srv._finalize_result(r, "https://x.com", "markdown", None, 3600, 0)
        assert mocked_set_cached.await_count == 0

    @pytest.mark.asyncio
    async def test_404_not_cached(self, mocked_set_cached):
        srv = MasterFetchServer()
        r = ResponseModel(status=404, content=["Not Found"], url="https://x.com",
                          fetcher_used="http", extracted_type="markdown")
        await srv._finalize_result(r, "https://x.com", "markdown", None, 3600, 0)
        assert mocked_set_cached.await_count == 0

    @pytest.mark.asyncio
    async def test_all_tiers_failed_not_cached(self, mocked_set_cached):
        srv = MasterFetchServer()
        r = ResponseModel(status=403, content=["[All fetch tiers failed...]"],
                          url="https://x.com", fetcher_used="stealthy",
                          extracted_type="markdown", error="all_tiers_failed: HTTP status 403")
        await srv._finalize_result(r, "https://x.com", "markdown", None, 3600, 0)
        assert mocked_set_cached.await_count == 0

    @pytest.mark.asyncio
    async def test_cache_ttl_zero_skips_cache(self, mocked_set_cached):
        srv = MasterFetchServer()
        r = ResponseModel(status=200, content=["real content"], url="https://x.com",
                          fetcher_used="http", extracted_type="markdown")
        await srv._finalize_result(r, "https://x.com", "markdown", None, 0, 0)
        assert mocked_set_cached.await_count == 0


# ─── Size cap + oldest eviction ───────────────────────────────────────────

class TestCacheSizeCap:
    @pytest.fixture
    def cache_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    @pytest.mark.asyncio
    async def test_cap_evicts_oldest_keeps_newest(self, cache_dir):
        import aiosqlite
        orig_cap = cache_mod.MAX_CACHE_ENTRIES
        cache_mod.MAX_CACHE_ENTRIES = 5
        try:
            for i in range(20):
                await set_cached(f"https://x{i}.com", "markdown", [f"c{i}"], 200,
                                 cache_dir=cache_dir)
            async with aiosqlite.connect(cache_dir / "cache.db") as db:
                cur = await db.execute("SELECT COUNT(*) FROM cache")
                (count,) = await cur.fetchone()
                cur = await db.execute("SELECT url FROM cache WHERE url='https://x0.com'")
                oldest = await cur.fetchone()
                cur = await db.execute("SELECT url FROM cache WHERE url='https://x19.com'")
                newest = await cur.fetchone()
            assert count <= 5, f"cache should be capped at 5, got {count}"
            assert oldest is None, "oldest entry should have been evicted"
            assert newest is not None, "newest entry should be kept"
        finally:
            cache_mod.MAX_CACHE_ENTRIES = orig_cap

    @pytest.mark.asyncio
    async def test_under_cap_no_eviction(self, cache_dir):
        import aiosqlite
        for i in range(3):
            await set_cached(f"https://y{i}.com", "markdown", [f"c{i}"], 200,
                             cache_dir=cache_dir)
        async with aiosqlite.connect(cache_dir / "cache.db") as db:
            cur = await db.execute("SELECT COUNT(*) FROM cache")
            (count,) = await cur.fetchone()
        assert count == 3  # nothing evicted when under the cap


# ─── Search cache key includes max_results ────────────────────────────────

class TestSearchCacheKey:
    @pytest.fixture
    def dict_cache(self, monkeypatch):
        """Dict-backed mock cache so the test never touches the real SQLite DB."""
        store: dict[tuple, str] = {}

        async def mock_get(url, etype, css=None, ttl=300, cache_dir=None):
            k = (url, etype)
            if k in store:
                return {"status": 200, "content": [store[k]], "url": url,
                        "content_type": "", "total_size_bytes": 0}
            return None

        async def mock_set(url, etype, content, status, css=None, ttl=300,
                           cache_dir=None, **kw):
            store[(url, etype)] = content[0]

        monkeypatch.setattr(search_mod, "get_cached", mock_get)
        monkeypatch.setattr(search_mod, "set_cached", mock_set)
        return store

    @pytest.fixture(autouse=True)
    def _api_key(self, monkeypatch):
        monkeypatch.setenv("TINYFISH_API_KEY", "sk-tinyfish-test")

    @pytest.mark.asyncio
    async def test_different_max_results_do_not_collide(self, dict_cache):
        """max_results=2 and max_results=3 must use separate cache keys."""
        from master_fetch.search import smart_search as _ss, SearchResult
        calls = []

        async def fake_tinyfish(query, max_results=10, api_key=""):
            calls.append(max_results)
            return [
                SearchResult(title=f"t{i}", url=f"u{i}", snippet="s", source="tinyfish",
                             position=i + 1, fetch_relevance="high")
                for i in range(max_results)
            ]

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(search_mod, "_tinyfish_search", fake_tinyfish)
        srv = MasterFetchServer()
        try:
            await _ss(srv, "python", max_results=2, cache_ttl=60)   # live -> cache key max=2
            await _ss(srv, "python", max_results=3, cache_ttl=60)   # different key -> live
            await _ss(srv, "python", max_results=2, cache_ttl=60)   # same key -> cache hit
        finally:
            monkeypatch.undo()
        # fake_tinyfish called for the first max=2 and the max=3, NOT the second max=2
        assert calls == [2, 3], f"expected [2, 3], got {calls}"
        # Both keys stored separately
        assert ("python", "search:v1:2") in dict_cache
        assert ("python", "search:v1:3") in dict_cache

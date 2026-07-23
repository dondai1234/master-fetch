"""Search tests: site/exclude_sites hostname filtering (PR #7), GitHub
case-folding dedup (PR #8), URL normalization, multi_search mapping logic,
RawResult consensus, EngineReport status mapping.

The metasearch backend is mocked (no network), but the filtering, dedup,
consensus, and mapping logic being tested is the REAL code that runs on
real search results.
"""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from master_fetch import search as search
from master_fetch import search_engines as se
from master_fetch.search_engines import (
    _passes_site_filter, _normalize_domain, _is_domain_or_subdomain,
    RawResult, EngineReport, multi_search, normalize_url,
    DEFAULT_ENGINES, _INDEX_FAMILY,
)


@pytest.fixture
def smart_search_cache(monkeypatch):
    """In-memory cache and deterministic search backend for cache-key tests."""
    cache = {}
    live_regions = []

    async def fake_get_cached(query, cache_type, css_selector, **kwargs):
        return cache.get((query, cache_type))

    async def fake_set_cached(query, cache_type, content, status, css_selector, ttl):
        cache[(query, cache_type)] = {"content": content}

    async def fake_multi_search(query, max_results, **kwargs):
        region = kwargs["region"]
        live_regions.append(region)
        return [RawResult(
            title=f"Result for {region}",
            url=f"https://{region}.example.test/",
            snippet="regional result",
            source="brave",
            position=1,
        )], [EngineReport(name="brave", ok=True)]

    async def fake_ensure_reranker():
        return None

    monkeypatch.setattr(search, "get_cached", fake_get_cached)
    monkeypatch.setattr(search, "set_cached", fake_set_cached)
    monkeypatch.setattr(search, "multi_search", fake_multi_search)
    monkeypatch.setattr(search, "ensure_reranker", fake_ensure_reranker)
    monkeypatch.setattr(
        search, "_rank",
        lambda query, ranked, mode: (ranked, [1.0] * len(ranked), "merge", ""),
    )
    return cache, live_regions


class TestSmartSearchRegionCache:

    @pytest.mark.asyncio
    async def test_explicit_regions_use_distinct_cache_entries(self, smart_search_cache):
        cache, live_regions = smart_search_cache

        us = await search.smart_search(
            None, "regional cache test", engines=["brave"], region="us-en",
        )
        gb = await search.smart_search(
            None, "regional cache test", engines=["brave"], region="gb-en",
        )

        assert us.cached is False
        assert gb.cached is False
        assert [us.results[0].title, gb.results[0].title] == [
            "Result for us-en", "Result for gb-en",
        ]
        assert live_regions == ["us-en", "gb-en"]
        assert len(cache) == 2

    @pytest.mark.asyncio
    async def test_same_normalized_explicit_region_uses_cached_result(self, smart_search_cache):
        cache, live_regions = smart_search_cache

        first = await search.smart_search(
            None, "regional cache test", engines=["brave"], region="GB-EN",
        )
        second = await search.smart_search(
            None, "regional cache test", engines=["brave"], region="gb-en",
        )

        assert first.cached is False
        assert second.cached is True
        assert second.results[0].title == "Result for GB-EN"
        assert live_regions == ["GB-EN"]
        assert len(cache) == 1

    @pytest.mark.asyncio
    async def test_omitted_region_keeps_existing_cache_key(self, smart_search_cache):
        cache, _ = smart_search_cache

        await search.smart_search(
            None, "regional cache test", engines=["brave"],
        )

        assert list(cache) == [
            ("regional cache test", "search:v12:6:::::0:brave::auto:regional cache test"),
        ]


# ─── Hostname boundary filtering (PR #7) ──────────────────────────

class TestSiteFilter:

    def test_exact_domain_passes_site_filter(self):
        assert _passes_site_filter("https://github.com/repo", "github.com", None) is True

    def test_subdomain_passes_site_filter(self):
        assert _passes_site_filter("https://docs.github.com/en", "github.com", None) is True

    def test_evil_prefix_rejected(self):
        assert _passes_site_filter("https://evilgithub.com/repo", "github.com", None) is False

    def test_evil_suffix_rejected(self):
        assert _passes_site_filter("https://github.com.evil.test/repo", "github.com", None) is False

    def test_similar_domain_rejected(self):
        assert _passes_site_filter("https://notgithub.com/repo", "github.com", None) is False

    def test_port_stripped_for_comparison(self):
        assert _passes_site_filter("https://github.com:8443/repo", "github.com", None) is True

    def test_www_prefix_stripped_correctly(self):
        # www.github.com as site filter should match www.github.com
        assert _passes_site_filter("https://www.github.com/repo", "www.github.com", None) is True
        # But github.com as site should also match www.github.com (subdomain)
        assert _passes_site_filter("https://www.github.com/repo", "github.com", None) is True

    def test_ww_prefix_not_stripped_as_www(self):
        # "wwgithub.com" must not be stripped as if it started with "www."
        assert _passes_site_filter("https://github.com/repo", "wwgithub.com", None) is False


class TestExcludeSitesFilter:

    def test_exclude_exact_domain(self):
        assert _passes_site_filter("https://github.com/repo", None, ["github.com"]) is False

    def test_exclude_subdomain(self):
        assert _passes_site_filter("https://docs.github.com/repo", None, ["github.com"]) is False

    def test_exclude_does_not_block_evil_prefix(self):
        assert _passes_site_filter("https://evilgithub.com/repo", None, ["github.com"]) is True

    def test_exclude_does_not_block_evil_suffix(self):
        assert _passes_site_filter("https://github.com.evil.test/repo", None, ["github.com"]) is True

    def test_exclude_does_not_block_similar(self):
        assert _passes_site_filter("https://notgithub.com/repo", None, ["github.com"]) is True


class TestNormalizeDomain:

    def test_strips_www_prefix(self):
        assert _normalize_domain("www.example.com") == "example.com"

    def test_does_not_strip_ww(self):
        assert _normalize_domain("wwexample.com") == "wwexample.com"

    def test_strips_trailing_dot(self):
        assert _normalize_domain("example.com.") == "example.com"

    def test_lowercases(self):
        assert _normalize_domain("Example.COM") == "example.com"

    def test_empty_returns_empty(self):
        assert _normalize_domain("") == ""

    def test_handles_url_with_scheme(self):
        assert _normalize_domain("https://www.example.com/path") == "example.com"

    def test_handles_url_without_scheme(self):
        assert _normalize_domain("www.example.com") == "example.com"


# ─── GitHub case-folding dedup (PR #8) ────────────────────────────

class TestGitHubCaseFolding:

    def test_owner_repo_casefolded(self):
        from master_fetch.search_metasearch import _normalize_url
        a = _normalize_url("https://github.com/nousresearch/hermes-agent")
        b = _normalize_url("https://github.com/NousResearch/hermes-agent")
        assert a == b

    def test_branch_case_preserved(self):
        from master_fetch.search_metasearch import _normalize_url
        a = _normalize_url("https://github.com/NousResearch/Hermes-Agent/tree/Main")
        b = _normalize_url("https://github.com/nousresearch/hermes-agent/tree/main")
        assert a != b

    def test_non_github_paths_case_sensitive(self):
        from master_fetch.search_metasearch import _normalize_url
        a = _normalize_url("https://example.com/Docs/Readme")
        b = _normalize_url("https://example.com/docs/readme")
        assert a != b

    def test_credential_urls_skip_folding(self):
        from master_fetch.search_metasearch import _normalize_url
        a = _normalize_url("https://User:Secret@github.com/NousResearch/Hermes-Agent")
        b = _normalize_url("https://user:secret@github.com/nousresearch/hermes-agent")
        assert a != b


class TestGitHubReservedRoutes:
    """PR #10: GitHub system routes (topics, settings, explore, etc.) should
    NOT have their path segments case-folded, because they are not repositories
    and case can carry meaning (e.g. /topics/Python vs /topics/python)."""

    def test_reserved_route_not_folded(self):
        from master_fetch.search_metasearch import _normalize_url
        a = _normalize_url("https://github.com/Settings/Keys")
        b = _normalize_url("https://github.com/settings/keys")
        assert a != b

    def test_topics_route_case_preserved(self):
        from master_fetch.search_metasearch import _normalize_url
        a = _normalize_url("https://github.com/topics/Python")
        b = _normalize_url("https://github.com/topics/python")
        assert a != b

    def test_explore_route_case_preserved(self):
        from master_fetch.search_metasearch import _normalize_url
        a = _normalize_url("https://github.com/Explore/Rust")
        b = _normalize_url("https://github.com/explore/rust")
        assert a != b

    def test_repo_still_folded_after_fix(self):
        from master_fetch.search_metasearch import _normalize_url
        a = _normalize_url("https://github.com/NousResearch/Hermes-Agent")
        b = _normalize_url("https://github.com/nousresearch/hermes-agent")
        assert a == b

    def test_reserved_route_lowercased_unchanged(self):
        """Already-lowercase reserved routes should be unchanged."""
        from master_fetch.search_metasearch import _normalize_url
        assert _normalize_url("https://github.com/topics/python") == \
               "https://github.com/topics/python"

    def test_multiple_reserved_routes(self):
        from master_fetch.search_metasearch import _normalize_url
        for route in ["settings", "topics", "explore", "dashboard", "notifications",
                      "marketplace", "sponsors", "collections", "trending", "search"]:
            a = _normalize_url(f"https://github.com/{route.title()}/Sub")
            b = _normalize_url(f"https://github.com/{route}/sub")
            assert a != b, f"Route '{route}' should not be case-folded"


# ─── URL normalization ─────────────────────────────────────────────

class TestNormalizeUrl:

    def test_lowercases_scheme_and_host(self):
        assert normalize_url("HTTPS://Example.COM/Path") == "https://example.com/Path"

    def test_strips_trailing_slash_on_non_root(self):
        assert normalize_url("https://example.com/page/") == "https://example.com/page"

    def test_preserves_root_slash(self):
        assert normalize_url("https://example.com/") == "https://example.com/"

    def test_handles_protocol_relative(self):
        result = normalize_url("//example.com/path")
        assert result.startswith("https://")

    def test_empty_returns_empty(self):
        assert normalize_url("") == ""


# ─── multi_search mapping logic ────────────────────────────────────

class TestMultiSearchMapping:
    """Test the mapping from metasearch dicts to RawResult + EngineReport.
    The metasearch backend is mocked, but the mapping/filtering is real."""

    @pytest.mark.asyncio
    async def test_site_filter_applied_on_results(self, monkeypatch):
        fake_results = [
            {"title": "A", "href": "https://github.com/repo", "body": "b", "backend": "brave", "backends": ["brave"]},
            {"title": "B", "href": "https://evilgithub.com/repo", "body": "b", "backend": "brave", "backends": ["brave"]},
        ]
        async def fake_metasearch(q, n, **kw):
            return fake_results, {"brave": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        ranked, reports = await multi_search("test", 6, site="github.com")
        assert len(ranked) == 1
        assert ranked[0].url == "https://github.com/repo"

    @pytest.mark.asyncio
    async def test_exclude_sites_filter_applied(self, monkeypatch):
        fake_results = [
            {"title": "A", "href": "https://github.com/repo", "body": "b", "backend": "brave", "backends": ["brave"]},
            {"title": "B", "href": "https://example.com/repo", "body": "b", "backend": "brave", "backends": ["brave"]},
        ]
        async def fake_metasearch(q, n, **kw):
            return fake_results, {"brave": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        ranked, _ = await multi_search("test", 6, exclude_sites=["github.com"])
        assert len(ranked) == 1
        assert ranked[0].url == "https://example.com/repo"

    @pytest.mark.asyncio
    async def test_consensus_from_multiple_backends(self, monkeypatch):
        fake_results = [
            {"title": "A", "href": "https://github.com/repo", "body": "b",
             "backend": "brave", "backends": ["brave", "duckduckgo"]},
        ]
        async def fake_metasearch(q, n, **kw):
            return fake_results, {"brave": "ok", "duckduckgo": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        ranked, _ = await multi_search("test", 6)
        assert len(ranked) == 1
        # brave and duckduckgo are different index families
        assert ranked[0].consensus == 2

    @pytest.mark.asyncio
    async def test_consensus_same_index_family(self, monkeypatch):
        # DDG and Yahoo both use Bing's index -> consensus = 1
        fake_results = [
            {"title": "A", "href": "https://example.com", "body": "b",
             "backend": "duckduckgo", "backends": ["duckduckgo", "yahoo"]},
        ]
        async def fake_metasearch(q, n, **kw):
            return fake_results, {"duckduckgo": "ok", "yahoo": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        ranked, _ = await multi_search("test", 6)
        assert ranked[0].consensus == 1  # same index family (Bing)

    @pytest.mark.asyncio
    async def test_engine_reports_mapped(self, monkeypatch):
        async def fake_metasearch(q, n, **kw):
            return [], {"brave": "ok", "google": "blocked", "yahoo": "empty", "mojeek": "timeout"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        _, reports = await multi_search("test", 6)
        report_map = {r.name: r for r in reports}
        assert report_map["brave"].ok is True
        assert report_map["google"].blocked is True
        assert report_map["yahoo"].ok is False
        assert report_map["mojeek"].blocked is True

    @pytest.mark.asyncio
    async def test_freshness_maps_to_timelimit(self, monkeypatch):
        captured = {}
        async def fake_metasearch(q, n, **kw):
            captured["timelimit"] = kw.get("timelimit")
            return [], {"brave": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        await multi_search("test", 6, freshness="week")
        assert captured["timelimit"] == "w"

    @pytest.mark.asyncio
    async def test_page_zero_indexed_to_one(self, monkeypatch):
        captured = {}
        async def fake_metasearch(q, n, **kw):
            captured["page"] = kw.get("page")
            return [], {"brave": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        await multi_search("test", 6, page=0)
        assert captured["page"] == 1

    @pytest.mark.asyncio
    async def test_site_prefix_added_to_query(self, monkeypatch):
        captured = {}
        async def fake_metasearch(q, n, **kw):
            captured["query"] = q
            return [], {"brave": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        await multi_search("test query", 6, site="github.com")
        assert "site:github.com" in captured["query"]

    @pytest.mark.asyncio
    async def test_exclude_prefix_added_to_query(self, monkeypatch):
        captured = {}
        async def fake_metasearch(q, n, **kw):
            captured["query"] = q
            return [], {"brave": "ok"}
        monkeypatch.setattr(se, "_metasearch", fake_metasearch)

        await multi_search("test", 6, exclude_sites=["pinterest.com"])
        assert "-site:pinterest.com" in captured["query"]


# ─── DEFAULT_ENGINES and index family ─────────────────────────────

class TestEngineConfig:

    def test_default_engines_has_eight(self):
        assert len(DEFAULT_ENGINES) == 8

    def test_default_engines_contains_key_backends(self):
        for engine in ("duckduckgo", "brave", "google", "mojeek", "yandex"):
            assert engine in DEFAULT_ENGINES

    def test_index_family_mapping(self):
        # DDG and Yahoo share Bing's index
        assert _INDEX_FAMILY["duckduckgo"] == _INDEX_FAMILY["yahoo"] == "bing"
        # Google and Startpage share Google's index
        assert _INDEX_FAMILY["google"] == _INDEX_FAMILY["startpage"] == "google"
        # Brave has its own independent index
        assert _INDEX_FAMILY["brave"] == "brave"
        # Mojeek has its own independent index
        assert _INDEX_FAMILY["mojeek"] == "mojeek"


# ─── Proxy validation (HOUND_SEARCH_PROXY) ─────────────────────────────────
class TestSearchProxyValidation:
    """Verify _PROXY is stripped and validated at import time.

    A bad proxy used to silently kill every search backend: httpx crashed at
    construction (ValueError), primp crashed at construction (BuilderError), and
    metasearch() returned 0 results with no explanation. Now the proxy is
    stripped + scheme-validated, and if all engines fail to construct, a
    MetaSearchException is raised.
    """

    def test_whitespace_stripped(self):
        """Leading/trailing whitespace is stripped so httpx doesn't crash."""
        import os, importlib
        os.environ["HOUND_SEARCH_PROXY"] = " http://proxy:8080 "
        import master_fetch.search_metasearch as m
        importlib.reload(m)
        try:
            assert m._PROXY == "http://proxy:8080"
        finally:
            os.environ.pop("HOUND_SEARCH_PROXY", None)
            importlib.reload(m)
            # Reload api_backends so MetaBlockedException references
            # stay in sync after search_metasearch reload.
            try:
                import master_fetch.api_backends as _ab
                importlib.reload(_ab)
            except Exception:
                pass

    def test_whitespace_only_nulled(self):
        """Whitespace-only proxy becomes None, not a crash-inducing string."""
        import os, importlib
        os.environ["HOUND_SEARCH_PROXY"] = "   "
        import master_fetch.search_metasearch as m
        importlib.reload(m)
        try:
            assert m._PROXY is None
        finally:
            os.environ.pop("HOUND_SEARCH_PROXY", None)
            importlib.reload(m)
            # Reload api_backends so MetaBlockedException references
            # stay in sync after search_metasearch reload.
            try:
                import master_fetch.api_backends as _ab
                importlib.reload(_ab)
            except Exception:
                pass

    def test_invalid_scheme_rejected(self):
        """Unknown scheme (not http/https/socks5/socks5h) is rejected."""
        import os, importlib
        os.environ["HOUND_SEARCH_PROXY"] = "garbage://proxy"
        import master_fetch.search_metasearch as m
        importlib.reload(m)
        try:
            assert m._PROXY is None
        finally:
            os.environ.pop("HOUND_SEARCH_PROXY", None)
            importlib.reload(m)
            # Reload api_backends so MetaBlockedException references
            # stay in sync after search_metasearch reload.
            try:
                import master_fetch.api_backends as _ab
                importlib.reload(_ab)
            except Exception:
                pass

    def test_valid_socks5_accepted(self):
        """socks5 scheme is accepted (primp supports it natively)."""
        import os, importlib
        os.environ["HOUND_SEARCH_PROXY"] = "socks5://192.0.2.1:1080"
        import master_fetch.search_metasearch as m
        importlib.reload(m)
        try:
            assert m._PROXY == "socks5://192.0.2.1:1080"
        finally:
            os.environ.pop("HOUND_SEARCH_PROXY", None)
            importlib.reload(m)
            # Reload api_backends so MetaBlockedException references
            # stay in sync after search_metasearch reload.
            try:
                import master_fetch.api_backends as _ab
                importlib.reload(_ab)
            except Exception:
                pass

    def test_no_proxy_env(self):
        """Unset env var -> None (direct connection)."""
        import os, importlib
        os.environ.pop("HOUND_SEARCH_PROXY", None)
        import master_fetch.search_metasearch as m
        importlib.reload(m)
        assert m._PROXY is None
        # Reload api_backends to sync class references after reload.
        try:
            import importlib as _il, master_fetch.api_backends as _ab
            _il.reload(_ab)
        except Exception:
            pass

    def test_all_engines_construction_failure_raises(self):
        """If every engine fails to construct (bad deps, etc), raise an error
        instead of silently returning 0 results."""
        import asyncio
        import master_fetch.search_metasearch as m

        class BrokenEngine:
            disabled = False
            def __init__(self, **kwargs):
                raise RuntimeError("simulated construction failure")

        original = dict(m._TEXT_ENGINES)
        # Ensure lazy-registered backends (API, BYOK) are in the dict before replacing.
        m._register_api_backends()
        m._register_byok_backends()
        original = dict(m._TEXT_ENGINES)
        for name in m._TEXT_ENGINES:
            m._TEXT_ENGINES[name] = type(
                f"Broken{name}", (BrokenEngine,),
                {"name": name, "disabled": False, "priority": 1.0}
            )
        try:
            with pytest.raises(m.MetaSearchException, match="No search engines could start"):
                asyncio.run(m.metasearch("test", max_results=3))
        finally:
            m._TEXT_ENGINES.clear()
            m._TEXT_ENGINES.update(original)

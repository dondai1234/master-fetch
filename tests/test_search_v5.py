"""Tests for v7 local search: filter wiring, engine selection, research mode,
cache keys, and the new agent-facing fields (relevance_score, engines_used,
engine_blocked). The network layer (multi_search) is stubbed so no real engine
is hit; server.smart_fetch is stubbed for research mode."""

import asyncio

import pytest

import master_fetch.search as search_mod
from master_fetch.search import (
    SearchResult, SearchResponseModel,
    _validate_filters, _validate_engines, _validate_freshness,
    smart_search as _ss,
)
from master_fetch.search_engines import RawResult, EngineReport
from master_fetch.security import SecurityError


def _raw(title, url, src="duckduckgo", pos=1, snip="python asyncio"):
    return RawResult(title=title, url=url, snippet=snip, source=src, position=pos)


# ─── validation ──────────────────────────────────────────────────────────────

def test_validate_filters_accepts_valid():
    _validate_filters("docs.python.org", ["x.com"], "US", "en", 0)
    _validate_filters(None, None, None, None, None)
    _validate_filters("docs.python.org", ["x.com"], "us-en", "en", 0)  # region-style location


@pytest.mark.parametrize("bad", [
    {"site": "no dot"},
    {"site": "a b.com"},
    {"location": "usa"},
    {"language": "EN"},
    {"page": -1}, {"page": 11}, {"page": "x"},
    {"exclude_sites": "notalist"},
])
def test_validate_filters_rejects_bad(bad):
    with pytest.raises(SecurityError):
        _validate_filters(bad.get("site"), bad.get("exclude_sites"),
                          bad.get("location"), bad.get("language"), bad.get("page"))


def test_validate_engines_lowercases_and_accepts():
    assert _validate_engines(["DuckDuckGo", "Bing"]) == ["duckduckgo", "bing"]
    assert _validate_engines(None) is None


def test_validate_engines_rejects_unknown():
    with pytest.raises(SecurityError):
        _validate_engines(["altavista"])
    with pytest.raises(SecurityError):
        _validate_engines([])


def test_validate_freshness_accepts():
    assert _validate_freshness("day") == "day"
    assert _validate_freshness(None) is None


def test_validate_freshness_rejects():
    with pytest.raises(SecurityError):
        _validate_freshness("hour")


# ─── smart_search builds agent-facing fields ─────────────────────────────────

def _stub_multi(monkeypatch, results, reports):
    captured = {}

    async def fake_multi(query, max_results, *, engines, site, exclude_sites,
                         region, freshness, page=0, server=None):
        captured.update(engines=engines, site=site, exclude_sites=exclude_sites,
                        region=region, freshness=freshness)
        return results, reports

    monkeypatch.setattr(search_mod, "multi_search", fake_multi)
    # Default the reranker path to 'unavailable' so mode=auto deterministically
    # falls back to keyword BM25 (no real model load / network in tests). Tests
    # that want neural/deep override these after calling _stub_multi.
    monkeypatch.setattr(search_mod, "neural_rerank", lambda q, r: None)
    return captured


def test_smart_search_builds_relevance_score_and_tiers(monkeypatch):
    results = [
        _raw("python asyncio guide", "https://b.com", "duckduckgo", 1, "asyncio event loop python"),
        _raw("cooking recipes", "https://a.com", "bing", 1, "food recipes cooking"),
    ]
    reports = [EngineReport("duckduckgo", ok=True), EngineReport("bing", ok=True)]
    cap = _stub_multi(monkeypatch, results, reports)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "python asyncio", max_results=5, cache_ttl=0))
    assert isinstance(resp, SearchResponseModel)
    assert resp.engines_used == ["duckduckgo", "bing"]
    assert resp.engine_blocked == []
    assert resp.rerank_mode == "keyword"
    # BM25 ranks the python/asyncio doc first; its score is normalized to 1.0.
    assert resp.results[0].url == "https://b.com"
    assert resp.results[0].relevance_score == 1.0
    assert resp.results[0].fetch_relevance == "high"
    assert resp.fetch_hint.startswith("1 high") or "high" in resp.fetch_hint


def test_smart_search_passes_filters_and_engines_to_multi(monkeypatch):
    results = [_raw("t", "https://x.com", "duckduckgo", 1)]
    reports = [EngineReport("duckduckgo", ok=True)]
    cap = _stub_multi(monkeypatch, results, reports)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    asyncio.run(_ss(srv, "python", max_results=5, cache_ttl=0,
                    engines=["duckduckgo", "google"], site="docs.python.org",
                    exclude_sites=["pinterest.com"], location="GB",
                    language="fr", freshness="week", page=1))
    assert cap["engines"] == ["duckduckgo", "google"]
    assert cap["site"] == "docs.python.org"
    assert cap["exclude_sites"] == ["pinterest.com"]
    assert cap["region"] == "gb-fr"  # derived from location GB + language fr
    assert cap["freshness"] == "week"


def test_smart_search_surfaces_engine_blocked_and_error_when_all_empty(monkeypatch):
    results = []
    reports = [EngineReport("duckduckgo", blocked=True, error="captcha"),
               EngineReport("bing", blocked=True, error="captcha")]
    _stub_multi(monkeypatch, results, reports)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "obscure query", cache_ttl=0))
    assert resp.results == []
    assert resp.engine_blocked == ["duckduckgo", "bing"]
    assert resp.error and "rate-limited" in resp.error.lower()


# ─── research mode ───────────────────────────────────────────────────────────

# ─── filter + engine + freshness aware cache key ─────────────────────────────

def test_cache_keys_differ_by_engine_and_freshness(monkeypatch):
    results = [_raw("t", "https://x.com", "duckduckgo", 1)]
    reports = [EngineReport("duckduckgo", ok=True)]
    _stub_multi(monkeypatch, results, reports)
    store = {}

    async def mock_get(url, etype, css=None, ttl=300, cache_dir=None, **kw):
        return None

    async def mock_set(url, etype, content, status, css=None, ttl=300, cache_dir=None, **kw):
        store[(url, etype)] = True

    monkeypatch.setattr(search_mod, "get_cached", mock_get)
    monkeypatch.setattr(search_mod, "set_cached", mock_set)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    asyncio.run(_ss(srv, "python", cache_ttl=60))
    asyncio.run(_ss(srv, "python", cache_ttl=60, engines=["google"]))
    asyncio.run(_ss(srv, "python", cache_ttl=60, freshness="day"))
    asyncio.run(_ss(srv, "python", cache_ttl=60, site="docs.python.org"))
    types = [t for (_u, t) in store.keys()]
    assert len(set(types)) == 4  # four distinct cache keys


def test_cache_hit_returns_results_without_calling_multi(monkeypatch):
    # multi_search must NOT be called on a cache hit.
    called = {"n": 0}

    async def fake_multi(*a, **k):
        called["n"] += 1
        return [], []

    monkeypatch.setattr(search_mod, "multi_search", fake_multi)

    cached_payload = {
        "results": [SearchResult(title="T", url="https://x.com", snippet="s",
                                 source="duckduckgo", position=1,
                                 relevance_score=1.0, fetch_relevance="high").model_dump()],
        "engines_used": ["duckduckgo"], "engine_blocked": [],
        "rerank_mode": "keyword",
    }
    import json

    async def mock_get(url, etype, css=None, ttl=300, cache_dir=None, **kw):
        return {"content": [json.dumps(cached_payload)]}

    async def mock_set(*a, **k):
        return None

    monkeypatch.setattr(search_mod, "get_cached", mock_get)
    monkeypatch.setattr(search_mod, "set_cached", mock_set)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "python", cache_ttl=60))
    assert called["n"] == 0
    assert resp.cached is True
    assert resp.results[0].url == "https://x.com"
    assert resp.engines_used == ["duckduckgo"]


def test_smart_search_partial_results_note_when_engine_blocked(monkeypatch):
    # One engine returned results, another was rate-limited/timed out. The agent
    # must see a clear note that results are partial + to retry, not a failure.
    results = [_raw("python asyncio", "https://b.com", "duckduckgo", 1, "asyncio event loop")]
    reports = [EngineReport("duckduckgo", ok=True),
               EngineReport("bing", blocked=True, error="timed out (8s)")]
    _stub_multi(monkeypatch, results, reports)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "python asyncio", max_results=5, cache_ttl=0))
    assert resp.engine_blocked == ["bing"]
    assert len(resp.results) == 1            # partial results from ddg
    assert resp.error == ""                  # not an error: results exist
    assert "timed out" in resp.fetch_hint.lower() or "rate-limited" in resp.fetch_hint.lower()
    assert "retry" in resp.fetch_hint.lower()


# ─── result cap + summary/next_action (agent QoL) ────────────────────────────

def test_smart_search_caps_returned_results_at_max_results(monkeypatch):
    # multi_search can return a large merged pool (up to 3x); the agent's
    # max_results must cap what comes back (token economy).
    big = [_raw(f"python asyncio result {i}", f"https://r{i}.com", "duckduckgo", i, "asyncio python")
           for i in range(30)]
    reports = [EngineReport("duckduckgo", ok=True)]
    _stub_multi(monkeypatch, big, reports)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "python asyncio", max_results=5, cache_ttl=0))
    assert len(resp.results) == 5            # capped to max_results
    assert resp.total_results == 5


def test_smart_search_next_action_with_high_results(monkeypatch):
    results = [
        _raw("python asyncio guide", "https://b.com", "duckduckgo", 1, "asyncio event loop python"),
        _raw("cooking recipes", "https://a.com", "bing", 2, "food recipes cooking"),
    ]
    reports = [EngineReport("duckduckgo", ok=True), EngineReport("bing", ok=True)]
    _stub_multi(monkeypatch, results, reports)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "python asyncio", max_results=5, cache_ttl=0))
    assert resp.results[0].fetch_relevance == "high"   # top of a ranked set is high
    assert "smart_fetch" in resp.next_action.lower()
    assert "judgment" in resp.next_action.lower()      # judgment-empowering, not a rigid 'fetch N'


def test_smart_search_next_action_no_results_blocked(monkeypatch):
    reports = [EngineReport("duckduckgo", blocked=True, error="timed out (8s)"),
               EngineReport("bing", blocked=True, error="timed out (8s)")]
    _stub_multi(monkeypatch, [], reports)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "obscure query xyzzy", max_results=5, cache_ttl=0))
    assert resp.results == []
    assert resp.engine_blocked == ["duckduckgo", "bing"]
    low = resp.next_action.lower()
    assert ("retry" in low) or ("proxy" in low) or ("no results" in low)


def test_smart_search_summary_populated(monkeypatch):
    results = [_raw("python asyncio", "https://b.com", "duckduckgo", 1, "asyncio python")]
    reports = [EngineReport("duckduckgo", ok=True), EngineReport("wikipedia", ok=True)]
    _stub_multi(monkeypatch, results, reports)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "python asyncio", max_results=5, cache_ttl=0))
    assert resp.summary
    assert "results" in resp.summary
    assert "keyword" in resp.summary          # rerank mode present
    assert "duckduckgo" in resp.summary       # engines present

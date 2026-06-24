"""Tests for v7 Phase 2 neural rerank wiring.

The real ONNX model is never downloaded in CI: `neural_rerank` and
`unavailable_reason` (imported into master_fetch.search) are monkeypatched.
Verifies mode validation, neural-when-available, graceful keyword fallback with
a note when neural is requested but unavailable, keyword-never-calls-neural,
auto-detection, and mode-aware cache keys.
"""

import asyncio
import json

import pytest

import master_fetch.search as search_mod
from master_fetch.search import (
    SearchResponseModel, ResearchResponseModel,
    _validate_mode, smart_search as _ss,
)
from master_fetch.search_engines import RawResult, EngineReport
from master_fetch.security import SecurityError


def _raw(title, url, src="duckduckgo", pos=1, snip="python asyncio"):
    return RawResult(title=title, url=url, snippet=snip, source=src, position=pos)


def _stub_multi(monkeypatch, results):
    async def fake_multi(query, max_results, *, engines, site, exclude_sites,
                         region, freshness, page=0, server=None):
        return results, [EngineReport("duckduckgo", ok=True)]
    monkeypatch.setattr(search_mod, "multi_search", fake_multi)
    # Default reranker path to unavailable; neural tests override below.
    monkeypatch.setattr(search_mod, "neural_rerank", lambda q, r: None)


# ─── _validate_mode ──────────────────────────────────────────────────────────

def test_validate_mode_accepts_implemented():
    assert _validate_mode(None) == "auto"
    assert _validate_mode("auto") == "auto"
    assert _validate_mode("KEYWORD") == "keyword"
    assert _validate_mode("neural") == "neural"
    assert _validate_mode("find_similar") == "find_similar"


def test_validate_mode_rejects_unimplemented():
    # deep was cut (over-built); find_similar stays. auto/keyword/neural/find_similar are valid.
    for bad in ("deep", "semantic", "magic", "", "autox"):
        with pytest.raises(SecurityError):
            _validate_mode(bad)


# ─── neural-when-available ───────────────────────────────────────────────────

def test_mode_neural_uses_reranker_when_available(monkeypatch):
    results = [_raw("A", "https://a.com", snip="python asyncio"),
               _raw("B", "https://b.com", snip="cooking recipes")]
    _stub_multi(monkeypatch, results)
    # Neural reranker returns a DIFFERENT order than BM25 (b before a) to prove it
    # was actually used.
    def fake_neural(query, ranked):
        return [(ranked[1], 0.9), (ranked[0], 0.2)]
    monkeypatch.setattr(search_mod, "neural_rerank", fake_neural)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "python asyncio", mode="neural", cache_ttl=0))
    assert resp.rerank_mode == "neural"
    assert resp.results[0].url == "https://b.com"  # neural order, not BM25
    assert resp.results[0].relevance_score == 0.9
    assert resp.results[1].relevance_score == 0.2


def test_mode_auto_uses_neural_when_available(monkeypatch):
    results = [_raw("A", "https://a.com")]
    _stub_multi(monkeypatch, results)
    called = {"n": 0}
    def fake_neural(query, ranked):
        called["n"] += 1
        return [(ranked[0], 0.77)]
    monkeypatch.setattr(search_mod, "neural_rerank", fake_neural)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "python asyncio", mode="auto", cache_ttl=0))
    assert resp.rerank_mode == "neural"
    assert called["n"] == 1
    assert resp.results[0].relevance_score == 0.77


# ─── graceful keyword fallback ───────────────────────────────────────────────

def test_mode_neural_falls_back_to_keyword_with_note(monkeypatch):
    results = [_raw("python asyncio guide", "https://b.com", snip="asyncio event loop python"),
               _raw("cooking", "https://a.com", snip="food recipes")]
    _stub_multi(monkeypatch, results)
    monkeypatch.setattr(search_mod, "neural_rerank", lambda q, r: None)
    monkeypatch.setattr(search_mod, "unavailable_reason",
                        lambda: "neural rerank needs hound-mcp[all]")
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "python asyncio", mode="neural", cache_ttl=0))
    assert resp.rerank_mode == "keyword"
    # BM25 order preserved (b.com is the python/asyncio doc).
    assert resp.results[0].url == "https://b.com"
    # The note explaining the fallback is surfaced in fetch_hint.
    assert "neural rerank unavailable" in resp.fetch_hint
    assert "hound-mcp[all]" in resp.fetch_hint


def test_mode_auto_falls_back_to_keyword_silently_when_unavailable(monkeypatch):
    results = [_raw("A", "https://a.com")]
    _stub_multi(monkeypatch, results)
    monkeypatch.setattr(search_mod, "neural_rerank", lambda q, r: None)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "python asyncio", mode="auto", cache_ttl=0))
    assert resp.rerank_mode == "keyword"
    # auto fallback is silent (no note) since the user did not explicitly ask for neural.
    assert "neural rerank unavailable" not in resp.fetch_hint


def test_mode_keyword_never_calls_neural(monkeypatch):
    results = [_raw("A", "https://a.com")]
    _stub_multi(monkeypatch, results)
    called = {"n": 0}
    monkeypatch.setattr(search_mod, "neural_rerank", lambda q, r: called.__setitem__("n", called["n"] + 1) or None)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "python asyncio", mode="keyword", cache_ttl=0))
    assert resp.rerank_mode == "keyword"
    assert called["n"] == 0  # neural reranker was not invoked


# ─── mode-aware cache key ────────────────────────────────────────────────────

def test_neural_and_keyword_cache_keys_differ(monkeypatch):
    results = [_raw("A", "https://a.com")]
    _stub_multi(monkeypatch, results)
    monkeypatch.setattr(search_mod, "neural_rerank", lambda q, r: None)
    store = {}
    async def mock_get(url, etype, css=None, ttl=300, cache_dir=None, **kw):
        return None
    async def mock_set(url, etype, content, status, css=None, ttl=300, cache_dir=None, **kw):
        store[(url, etype)] = True
    monkeypatch.setattr(search_mod, "get_cached", mock_get)
    monkeypatch.setattr(search_mod, "set_cached", mock_set)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    asyncio.run(_ss(srv, "python asyncio", mode="keyword", cache_ttl=60))
    asyncio.run(_ss(srv, "python asyncio", mode="neural", cache_ttl=60))
    types = [t for (_u, t) in store.keys()]
    assert len(set(types)) == 2
    assert any(":keyword:" in t for t in types)
    assert any(":neural:" in t for t in types)


# ─── reranker module contract (no network) ───────────────────────────────────

def test_rerank_returns_none_when_get_reranker_none(monkeypatch):
    import master_fetch.reranker as rer
    monkeypatch.setattr(rer, "get_reranker", lambda: None)
    assert rer.rerank("q", [_raw("a", "https://a.com")]) is None


# ─── Phase 4: find_similar + autoretrieval (expand) ───────────────────────────

def test_validate_expand_accepts_and_rejects():
    from master_fetch.search import _validate_expand
    assert _validate_expand(None) == 1
    assert _validate_expand(3) == 3
    for bad in (0, 6, -1, True, "2"):
        with pytest.raises(SecurityError):
            _validate_expand(bad)


def test_expand_query_generates_distinct_variants():
    from master_fetch.search import _expand_query
    out = _expand_query("transformer attention", 3)
    assert len(out) == 3
    assert out[0] == "transformer attention"
    assert len(set(out)) == 3
    assert _expand_query("x", 1) == ["x"]


def _stub_for_expand(monkeypatch, fake_multi):
    monkeypatch.setattr(search_mod, "multi_search", fake_multi)
    monkeypatch.setattr(search_mod, "neural_rerank", lambda q, r: None)


def test_expand_runs_subqueries_and_merges(monkeypatch):
    seen = []

    async def fake_multi(query, max_results, *, engines, site, exclude_sites,
                         region, freshness, page=0, server=None):
        seen.append(query)
        return [_raw(query, f"https://{abs(hash(query)) % 9999}.com", snip=query)
                ], [EngineReport("duckduckgo", ok=True)]
    _stub_for_expand(monkeypatch, fake_multi)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "transformer attention", expand=3, mode="keyword", cache_ttl=0))
    assert len(seen) == 3
    assert seen[0] == "transformer attention"
    assert len(set(seen)) == 3  # 3 distinct sub-query variants
    # Results from all 3 subqueries merged (distinct URLs -> not deduped away).
    assert resp.total_results >= 2


def test_expand_one_is_no_expansion(monkeypatch):
    seen = []

    async def fake_multi(query, max_results, **kw):
        seen.append(query)
        return [_raw("a", "https://a.com")], [EngineReport("duckduckgo", ok=True)]
    _stub_for_expand(monkeypatch, fake_multi)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    asyncio.run(_ss(srv, "python", expand=1, mode="keyword", cache_ttl=0))
    assert len(seen) == 1  # no expansion


def test_find_similar_fetches_source_and_reranks_vs_source(monkeypatch):
    candidates = [
        _raw("A", "https://a.com", snip="asyncio event loop"),
        _raw("B", "https://b.com", snip="transformer attention"),
        _raw("C", "https://c.com", snip="cooking recipes"),
    ]

    async def fake_source(url, timeout=10):
        return ("Asyncio Guide", "this page is about asyncio event loops in python")
    monkeypatch.setattr(search_mod, "fetch_source_for_similar", fake_source)

    async def fake_gather(query, expand, max_results, engines, site, exclude_sites,
                          region, freshness, page=0, server=None):
        return candidates, [EngineReport("duckduckgo", ok=True)]
    monkeypatch.setattr(search_mod, "_gather", fake_gather)

    class FakeRer:
        def score(self, q, docs):
            # q is the source page text; score favors the asyncio candidate.
            return [0.9, 0.2, 0.1]
    monkeypatch.setattr(search_mod, "get_reranker", lambda: FakeRer())
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "ignored", mode="find_similar",
                           url="https://src.com/x", cache_ttl=0))
    assert resp.rerank_mode == "find_similar"
    assert resp.query == "https://src.com/x"  # response query is the source URL
    assert resp.results[0].url == "https://a.com"  # asyncio candidate ranked first
    assert "find_similar to https://src.com/x" in resp.fetch_hint


def test_find_similar_requires_url(monkeypatch):
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "not a url", mode="find_similar", cache_ttl=0))
    assert resp.results == []
    assert "requires a url" in resp.error


def test_find_similar_source_unfetchable(monkeypatch):
    async def fake_source(url, timeout=10):
        return "", ""
    monkeypatch.setattr(search_mod, "fetch_source_for_similar", fake_source)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "https://src.com/x", mode="find_similar", cache_ttl=0))
    assert resp.results == []
    assert "could not fetch" in resp.error


def test_find_similar_falls_back_to_keyword_when_no_reranker(monkeypatch):
    candidates = [
        _raw("python asyncio", "https://a.com", snip="python asyncio event loop"),
        _raw("cooking", "https://b.com", snip="food recipes"),
    ]

    async def fake_source(url, timeout=10):
        return ("Python Asyncio", "python asyncio event loop content")
    monkeypatch.setattr(search_mod, "fetch_source_for_similar", fake_source)

    async def fake_gather(query, expand, max_results, engines, site, exclude_sites,
                          region, freshness, page=0, server=None):
        return candidates, [EngineReport("duckduckgo", ok=True)]
    monkeypatch.setattr(search_mod, "_gather", fake_gather)
    monkeypatch.setattr(search_mod, "get_reranker", lambda: None)
    monkeypatch.setattr(search_mod, "unavailable_reason", lambda: "needs hound-mcp[all]")
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    resp = asyncio.run(_ss(srv, "x", mode="find_similar",
                           url="https://src.com/x", cache_ttl=0))
    assert resp.rerank_mode == "find_similar"
    # BM25 on derived query "Python Asyncio" -> a.com (python asyncio) first.
    assert resp.results[0].url == "https://a.com"
    assert "keyword BM25" in resp.fetch_hint  # the fallback note


# ─── reranker prewarm (no-download-when-absent) ──────────────────────────────

def test_model_present_false_when_model_absent(tmp_path, monkeypatch):
    import master_fetch.reranker as rer
    monkeypatch.setattr(rer, "MODEL_DIR", tmp_path)
    assert rer.model_present() is False


def test_prewarm_reranker_skips_when_model_absent(monkeypatch):
    import master_fetch.reranker as rer
    # Force "model not cached" so prewarm must NOT call get_reranker (which would
    # download). If it did call it, the bomb raises.
    monkeypatch.setattr(rer, "model_present", lambda: False)
    called = {"yes": False}
    def bomb():
        called["yes"] = True
        raise AssertionError("prewarm must not load the reranker when the model is absent")
    monkeypatch.setattr(rer, "get_reranker", bomb)
    asyncio.run(rer.prewarm_reranker())   # must not raise
    assert called["yes"] is False


def test_prewarm_reranker_warms_when_model_present(monkeypatch):
    import master_fetch.reranker as rer
    monkeypatch.setattr(rer, "model_present", lambda: True)
    called = {"yes": False}
    def fake_get():
        called["yes"] = True
        return None
    monkeypatch.setattr(rer, "get_reranker", fake_get)
    asyncio.run(rer.prewarm_reranker())
    assert called["yes"] is True

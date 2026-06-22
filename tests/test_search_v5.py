"""Tests for v5 search: domain/geo filters + research mode.

Mocked (no TinyFish key needed): _tinyfish_search is stubbed to return canned
results, and server.smart_fetch is stubbed for research mode so no network
runs. Verifies filter wiring, URL/operator building, validation, research-mode
fetch bundling + relevance-based selection, and filter-aware cache keys.
"""

import asyncio
import os

import pytest

import master_fetch.search as search_mod
from master_fetch.search import (
    SearchResult, SearchResponseModel, ResearchResponseModel,
    compute_fetch_relevance, _append_site_operators, _validate_filters,
    smart_search as _ss,
)
from master_fetch.security import SecurityError


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "sk-tinyfish-test")


def _result(title, url, pos, rel="high"):
    return SearchResult(title=title, url=url, snippet="s", source="tinyfish",
                        position=pos, fetch_relevance=rel)


# ─── _append_site_operators ─────────────────────────────────────────────

def test_append_site_operator():
    assert _append_site_operators("python asyncio", "docs.python.org", None) == \
        "python asyncio site:docs.python.org"


def test_append_exclude_operators():
    assert _append_site_operators("python", None, ["pinterest.com", "facebook.com"]) == \
        "python -site:pinterest.com -site:facebook.com"


def test_append_none_unchanged():
    assert _append_site_operators("python", None, None) == "python"


# ─── _validate_filters ──────────────────────────────────────────────────

def test_validate_filters_accepts_valid():
    _validate_filters("docs.python.org", ["x.com"], "US", "en", 0)
    _validate_filters(None, None, None, None, None)  # all-None is valid


@pytest.mark.parametrize("bad", [
    {"site": "no dot"},          # not a domain
    {"site": "a b.com"},         # spaces
    {"location": "usa"},         # not 2-letter upper
    {"language": "EN"},          # not 2-letter lower
    {"page": -1}, {"page": 11}, {"page": "x"},
    {"exclude_sites": "notalist"},
])
def test_validate_filters_rejects_bad(bad):
    with pytest.raises(SecurityError):
        _validate_filters(bad.get("site"), bad.get("exclude_sites"),
                          bad.get("location"), bad.get("language"), bad.get("page"))


# ─── _tinyfish_search builds filtered URL ───────────────────────────────

class _FakeResp:
    ok = True
    status_code = 200
    def json(self): return {"results": [
        {"title": "T", "url": "https://x.com", "snippet": "s"}]}
class _FakeRequests:
    def __init__(self): self.captured_url = None
    def get(self, url, headers=None, timeout=None):
        self.captured_url = url
        return _FakeResp()

def test_tinyfish_url_carries_filters(monkeypatch):
    fr = _FakeRequests()
    monkeypatch.setattr(search_mod, "_get_requests", lambda: fr)
    monkeypatch.setattr(search_mod, "_validate_api_key", lambda k: "sk-tinyfish-test")
    async def run():
        return await search_mod._tinyfish_search(
            "python", 5, "sk-tinyfish-test",
            site="docs.python.org", exclude_sites=["pinterest.com"],
            location="GB", language="fr", page=2,
        )
    asyncio.run(run())
    url = fr.captured_url
    assert "site%3Adocs.python.org" in url or "site:docs.python.org" in url
    assert "-site%3Apinterest.com" in url or "-site:pinterest.com" in url
    assert "location=GB" in url
    assert "language=fr" in url
    assert "page=2" in url


# ─── smart_search passes filters through ────────────────────────────────

def test_smart_search_passes_filters_to_tinyfish(monkeypatch):
    captured = {}
    async def fake_tinyfish(query, max_results=10, api_key="", **kwargs):
        captured.update(kwargs)
        return [_result("T", "https://x.com", 1)]
    monkeypatch.setattr(search_mod, "_tinyfish_search", fake_tinyfish)
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    asyncio.run(_ss(srv, "python", max_results=5, cache_ttl=0,
                    site="docs.python.org", location="GB", language="fr", page=1))
    assert captured["site"] == "docs.python.org"
    assert captured["location"] == "GB"
    assert captured["language"] == "fr"
    assert captured["page"] == 1


# ─── Research mode ──────────────────────────────────────────────────────

def test_research_mode_fetches_top_results(monkeypatch):
    """fetch_content=True returns a ResearchResponseModel with the top-N results'
    content attached, picked by relevance (high first)."""
    async def fake_tinyfish(query, max_results=10, api_key="", **kwargs):
        return [
            _result("High match A", "https://a.com", 1, "high"),
            _result("Low match B", "https://b.com", 2, "low"),
            _result("Med match C", "https://c.com", 3, "med"),
        ]
    monkeypatch.setattr(search_mod, "_tinyfish_search", fake_tinyfish)

    from master_fetch.server import MasterFetchServer
    from master_fetch.server import ResponseModel
    srv = MasterFetchServer()
    fetched = []
    async def fake_smart_fetch(url, cache_ttl=3600, max_content_chars=8000, **kw):
        fetched.append(url)
        return ResponseModel(status=200, content=[f"content for {url}"],
                             url=url, content_ok=True, summary="200 OK")
    srv.smart_fetch = fake_smart_fetch  # type: ignore

    resp = asyncio.run(_ss(srv, "query", max_results=10, cache_ttl=0,
                           fetch_content=True, fetch_top=2))
    assert isinstance(resp, ResearchResponseModel)
    assert resp.fetched_count == 2
    # Relevance ordering: high first, then med (low skipped because fetch_top=2).
    assert [r.url for r in resp.results] == ["https://a.com", "https://c.com"]
    assert all(r.content_ok for r in resp.results)
    assert all(r.content for r in resp.results)
    assert resp.summary
    # fetch_top caps at 5
    assert resp.total_results == 3


def test_research_mode_caps_fetch_top_at_5(monkeypatch):
    async def fake_tinyfish(query, max_results=10, api_key="", **kwargs):
        return [_result(f"t{i}", f"https://x{i}.com", i + 1, "high") for i in range(8)]
    monkeypatch.setattr(search_mod, "_tinyfish_search", fake_tinyfish)
    from master_fetch.server import MasterFetchServer, ResponseModel
    srv = MasterFetchServer()
    async def fake_smart_fetch(url, cache_ttl=3600, max_content_chars=8000, **kw):
        return ResponseModel(status=200, content=["c"], url=url, content_ok=True, summary="ok")
    srv.smart_fetch = fake_smart_fetch  # type: ignore
    resp = asyncio.run(_ss(srv, "q", cache_ttl=0, fetch_content=True, fetch_top=99))
    assert resp.fetched_count == 5  # capped


def test_research_mode_fetch_failure_sets_content_ok_false(monkeypatch):
    async def fake_tinyfish(query, max_results=10, api_key="", **kwargs):
        return [_result("t", "https://x.com", 1, "high")]
    monkeypatch.setattr(search_mod, "_tinyfish_search", fake_tinyfish)
    from master_fetch.server import MasterFetchServer, ResponseModel
    srv = MasterFetchServer()
    async def fake_smart_fetch(url, cache_ttl=3600, max_content_chars=8000, **kw):
        return ResponseModel(status=0, content=["[blocked]"], url=url, content_ok=False, error="blocked")
    srv.smart_fetch = fake_smart_fetch  # type: ignore
    resp = asyncio.run(_ss(srv, "q", cache_ttl=0, fetch_content=True, fetch_top=1))
    assert resp.results[0].content_ok is False
    assert resp.results[0].fetch_error == "blocked"


# ─── Filter-aware cache key ─────────────────────────────────────────────

def test_filtered_and_unfiltered_search_do_not_collide(monkeypatch):
    keys = []
    async def fake_tinyfish(query, max_results=10, api_key="", **kwargs):
        return [_result("t", "https://x.com", 1)]
    monkeypatch.setattr(search_mod, "_tinyfish_search", fake_tinyfish)
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
    asyncio.run(_ss(srv, "python", cache_ttl=60, site="docs.python.org"))
    types = [t for (_u, t) in store.keys()]
    assert any("docs.python.org" in t for t in types)
    assert any("docs.python.org" not in t for t in types)
    assert len(set(types)) == 2  # two distinct cache keys

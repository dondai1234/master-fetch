"""Tests for the v7.5 metasearch-backed search engine layer.

No network: `multi_search` is run against a stubbed metasearch returning canned
result dicts + per-backend status; the metasearch aggregator itself is tested
with fake engine classes (instantiated + run in parallel, early-return-on-quorum,
dedup, cross-backend consensus tracking). Verifies hound-param mapping
(freshness->timelimit, page 0->1-indexed, site/exclude), RawResult mapping
(consensus = distinct index families), EngineReport status mapping, and the
backend resolver.
"""

import asyncio

import pytest

from master_fetch import search_engines as se
from master_fetch.search_engines import (
    RawResult, EngineReport, multi_search, normalize_url,
    DEFAULT_ENGINES, _INDEX_FAMILY, fetch_source_for_similar,
)
from master_fetch import search_metasearch as ms


# ─── dataclasses + helpers ──────────────────────────────────────────────────

def test_raw_result_defaults():
    r = RawResult(title="t", url="https://x.com", snippet="s", source="brave")
    assert r.position == 0 and r.consensus == 1 and r.sources == ()


def test_engine_report_defaults():
    r = EngineReport(name="brave")
    assert r.ok is False and r.blocked is False and r.preempted is False and r.error == ""


def test_normalize_url_strips_trailing_slash_non_root():
    assert normalize_url("https://Example.com/path/") == "https://example.com/path"
    # root path keeps its slash
    assert normalize_url("https://example.com/") == "https://example.com/"


def test_normalize_url_adds_scheme_to_protocol_relative():
    assert normalize_url("//example.com/x") == "https://example.com/x"


def test_metasearch_normalize_preserves_github_reserved_routes():
    assert ms._normalize_url("https://github.com/Settings/Keys") == (
        "https://github.com/Settings/Keys"
    )


def test_index_family_bing_group():
    # duckduckgo + yahoo share Bing's index -> same family
    assert _INDEX_FAMILY["duckduckgo"] == _INDEX_FAMILY["yahoo"] == "bing"
    assert _INDEX_FAMILY["startpage"] == _INDEX_FAMILY["google"] == "google"


def test_default_engines_is_the_full_pool():
    assert "brave" in DEFAULT_ENGINES and "mojeek" in DEFAULT_ENGINES
    assert "duckduckgo" in DEFAULT_ENGINES and "yandex" in DEFAULT_ENGINES


# ─── backend resolver ───────────────────────────────────────────────────────

def test_resolve_backends_default_is_full_pool():
    out = ms._resolve_backends(None)
    assert "brave" in out and "mojeek" in out and "yandex" in out


def test_resolve_backends_maps_legacy_hound_names():
    # bing -> yahoo (same index, diff server); qwant is its own real backend (v8.1)
    out = ms._resolve_backends(["bing", "qwant", "brave"])
    assert "yahoo" in out and "qwant" in out and "brave" in out
    assert "bing" not in out  # bing is disabled -> mapped to yahoo, never returned as 'bing'
    assert "duckduckgo" not in out  # qwant no longer aliases to duckduckgo


def test_resolve_backends_dedups():
    out = ms._resolve_backends(["brave", "brave", "mojeek"])
    assert out.count("brave") == 1


# ─── multi_search param mapping + RawResult/Report mapping ──────────────────

class _FakeMeta:
    """Replaces se._metasearch; records the call + returns canned output."""
    def __init__(self):
        self.calls = []

    async def __call__(self, query, max_results, *, region, timelimit, page, engines):
        self.calls.append(dict(query=query, max_results=max_results, region=region,
                               timelimit=timelimit, page=page, engines=engines))
        results = [
            {"title": "Tokio", "href": "https://tokio.rs/", "body": "b1",
             "backend": "brave", "backends": ["brave", "yahoo"]},  # 2 families (brave, bing)
            {"title": "Docs", "href": "https://docs.rs/tokio", "body": "b2",
             "backend": "mojeek", "backends": ["mojeek"]},  # 1 family
            {"title": "Off-site", "href": "https://other.com/x", "body": "b3",
             "backend": "brave", "backends": ["brave"]},
        ]
        status = {"brave": "ok", "mojeek": "ok", "yahoo": "ok",
                  "google": "error:captcha", "duckduckgo": "empty",
                  "startpage": "preempted", "yandex": "timeout"}
        return results, status


def test_multi_search_maps_params_and_results(monkeypatch):
    fake = _FakeMeta()
    monkeypatch.setattr(se, "_metasearch", fake)
    ranked, reports = asyncio.run(multi_search(
        "tokio", 6, region="us-en", freshness="week", page=2,
        engines=["brave", "mojeek"], site="tokio.rs",
    ))
    # freshness week -> timelimit 'w'; page 2 (0-indexed) -> backend page 3
    c = fake.calls[0]
    assert c["timelimit"] == "w"
    assert c["page"] == 3
    assert c["region"] == "us-en"
    # site: prefix added to the query
    assert c["query"].startswith("site:tokio.rs") and "tokio" in c["query"]
    # consensus = distinct index families: brave+yahoo -> {brave, bing} = 2
    assert ranked[0].consensus == 2
    assert set(ranked[0].sources) == {"brave", "yahoo"}
    assert ranked[0].source == "brave"
    # site filter dropped docs.rs + other.com -> only tokio.rs survives
    assert len(ranked) == 1
    assert all("tokio.rs" in r.url for r in ranked)


def test_multi_search_engine_reports_status_mapping(monkeypatch):
    fake = _FakeMeta()
    monkeypatch.setattr(se, "_metasearch", fake)
    _, reports = asyncio.run(multi_search("x", 6))
    by = {r.name: r for r in reports}
    assert by["brave"].ok is True and by["mojeek"].ok is True
    assert by["google"].blocked is True and "captcha" in by["google"].error
    assert by["yandex"].blocked is True  # timeout -> blocked
    assert by["startpage"].preempted is True and by["startpage"].blocked is False
    assert by["duckduckgo"].ok is False and by["duckduckgo"].blocked is False  # empty -> neither


def test_multi_search_exclude_sites_filter(monkeypatch):
    fake = _FakeMeta()
    monkeypatch.setattr(se, "_metasearch", fake)
    ranked, _ = asyncio.run(multi_search("x", 6, exclude_sites=["other.com"]))
    assert not any("other.com" in r.url for r in ranked)


def test_multi_search_server_param_accepted_but_unused(monkeypatch):
    fake = _FakeMeta()
    monkeypatch.setattr(se, "_metasearch", fake)
    # server=object() must not raise + must not change behaviour (search is HTTP-only)
    ranked, _ = asyncio.run(multi_search("x", 6, server=object()))
    # no site filter -> all 3 canned results survive
    assert len(ranked) == 3
    # mojeek-only result (docs.rs) -> consensus 1
    docs = next(r for r in ranked if "docs.rs" in r.url)
    assert docs.consensus == 1 and docs.source == "mojeek"


# ─── metasearch aggregator: parallel + early-return + dedup + consensus ─────

class _TR:
    """Minimal stand-in for TextResult."""
    def __init__(self, title, href, body=""):
        self.title = title
        self.href = href
        self.body = body


def _fake_engine_class(name, results, delay=0.0, exc=None):
    """Build a fake backend CLASS (has .disabled + __init__(proxy,timeout,verify)
    + .search) the metasearch can instantiate + run."""
    class _E:
        disabled = False
        priority = 1.0
        provider = "fake"
        def __init__(self, proxy=None, timeout=None, *, verify=True):
            self.name = name
            self._results = results
            self._delay = delay
            self._exc = exc
        def search(self, query, region="us-en", safesearch="moderate",
                   timelimit=None, page=1, **kw):
            if self._delay:
                import time as _t
                _t.sleep(self._delay)
            if self._exc:
                raise self._exc
            return self._results
    return _E


def _patch_engines(monkeypatch, engines_dict):
    monkeypatch.setattr(ms, "_TEXT_ENGINES", engines_dict)


def test_metasearch_dedup_and_backends_tracking(monkeypatch):
    # brave + yahoo both return https://tokio.rs/ -> one result, backends={brave,yahoo}
    _patch_engines(monkeypatch, {
        "brave": _fake_engine_class("brave", [_TR("Tokio", "https://tokio.rs/")]),
        "yahoo": _fake_engine_class("yahoo", [_TR("Tokio", "https://tokio.rs/"), _TR("Y", "https://y.com")]),
    })
    res, status = asyncio.run(ms.metasearch("q", 6, engines=["brave", "yahoo"]))
    urls = [r["href"] for r in res]
    assert urls.count("https://tokio.rs/") == 1  # deduped
    tokio = next(r for r in res if r["href"] == "https://tokio.rs/")
    assert set(tokio["backends"]) == {"brave", "yahoo"}
    assert status["brave"] == "ok" and status["yahoo"] == "ok"


def test_metasearch_merges_github_repo_case_variants_into_consensus(monkeypatch):
    _patch_engines(monkeypatch, {
        "brave": _fake_engine_class(
            "brave", [_TR("Hound", "https://github.com/nousresearch/hermes-agent")]
        ),
        "yahoo": _fake_engine_class(
            "yahoo", [_TR("Hound", "https://github.com/NousResearch/hermes-agent")]
        ),
    })
    res, status = asyncio.run(ms.metasearch("hound", 6, engines=["brave", "yahoo"]))
    assert len(res) == 1
    assert res[0]["href"] in {
        "https://github.com/nousresearch/hermes-agent",
        "https://github.com/NousResearch/hermes-agent",
    }
    assert set(res[0]["backends"]) == {"brave", "yahoo"}
    assert status["brave"] == "ok" and status["yahoo"] == "ok"


def test_metasearch_keeps_github_reserved_route_case_variants_distinct(monkeypatch):
    _patch_engines(monkeypatch, {
        "brave": _fake_engine_class(
            "brave", [_TR("Python topics", "https://github.com/topics/Python")]
        ),
        "yahoo": _fake_engine_class(
            "yahoo", [_TR("python topics", "https://github.com/topics/python")]
        ),
    })
    res, status = asyncio.run(ms.metasearch("python", 6, engines=["brave", "yahoo"]))
    assert {item["href"] for item in res} == {
        "https://github.com/topics/Python",
        "https://github.com/topics/python",
    }
    assert all(len(item["backends"]) == 1 for item in res)
    assert status["brave"] == "ok" and status["yahoo"] == "ok"


def test_metasearch_error_and_empty_status(monkeypatch):
    _patch_engines(monkeypatch, {
        "brave": _fake_engine_class("brave", [], exc=RuntimeError("boom")),
        "mojeek": _fake_engine_class("mojeek", []),  # empty
        "yahoo": _fake_engine_class("yahoo", [_TR("Y", "https://y.com")]),
    })
    res, status = asyncio.run(ms.metasearch("q", 6, engines=["brave", "mojeek", "yahoo"]))
    assert status["brave"].startswith("error")
    assert status["mojeek"] == "empty"
    assert status["yahoo"] == "ok"
    assert len(res) == 1


def test_metasearch_early_return_records_preempted(monkeypatch):
    # 3 fast engines deliver enough; a 4th slow one is preempted.
    _patch_engines(monkeypatch, {
        "brave": _fake_engine_class("brave", [_TR(f"b{i}", f"https://b{i}.com") for i in range(8)]),
        "mojeek": _fake_engine_class("mojeek", [_TR("m", "https://m.com")]),
        "yahoo": _fake_engine_class("yahoo", [_TR("y", "https://y.com")]),
        "yandex": _fake_engine_class("yandex", [_TR("slow", "https://slow.com")], delay=5.0),
    })
    res, status = asyncio.run(ms.metasearch("q", 6, engines=["brave", "mojeek", "yahoo", "yandex"]))
    # quorum = 6+4=10 results; brave(8)+mojeek(1)+yahoo(1)=10 with 3 engines -> early return
    assert status.get("yandex") == "preempted"
    assert status["brave"] == "ok" and status["mojeek"] == "ok"
    # the slow yandex result must NOT be present (it was cancelled before finishing)
    assert not any(r["href"] == "https://slow.com" for r in res)


def test_metasearch_resolves_hound_engine_names(monkeypatch):
    _patch_engines(monkeypatch, {
        "duckduckgo": _fake_engine_class("duckduckgo", [_TR("d", "https://d.com")]),
        "yahoo": _fake_engine_class("yahoo", [_TR("y", "https://y.com")]),
        "qwant": _fake_engine_class("qwant", [_TR("qw", "https://qw.com")]),
    })
    # 'bing' maps to 'yahoo'; 'qwant' is now its own real backend (v8.1)
    res, status = asyncio.run(ms.metasearch("q", 6, engines=["bing", "qwant"]))
    assert "yahoo" in status and "qwant" in status
    assert len(res) == 2


# ─── fetch_source_for_similar (stubbed transport) ───────────────────────────

def test_fetch_source_for_similar_returns_empty_on_failure(monkeypatch):
    # fetcher import path fails -> ("", "")
    import sys
    real = sys.modules.get("master_fetch.fetcher")
    monkeypatch.setitem(sys.modules, "master_fetch.fetcher", None)
    out = asyncio.run(fetch_source_for_similar("https://example.com"))
    assert out == ("", "")

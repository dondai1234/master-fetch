"""Tests for the v7 hound-native keyless search engine layer.

No network: SERP parsers are fed fixture HTML, the Wikipedia JSON path is fed
canned JSON, and multi_search is run against stubbed engine funcs. Verifies
DDG uddg-redirect decoding, Bing <cite> breadcrumb reconstruction,
Qwant/Yahoo parsing, cross-engine consensus merge, site filters, and the
multi-engine orchestrator.
"""

import asyncio
import json

import pytest

from master_fetch import search_engines as se
from master_fetch.search_engines import (
    RawResult, EngineReport, _ddg_real_url, _bing_real_url, _parse_ddg,
    _parse_bing, _parse_qwant_json, _qwant_locale, _parse_yahoo, _yahoo_real_url,
    merge_dedupe, multi_search,
)


# ─── DDG redirect decoding ──────────────────────────────────────────────────

def test_ddg_real_url_decodes_uddg():
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=abc"
    assert _ddg_real_url(href) == "https://example.com/page"


def test_ddg_real_url_passthrough_non_redirect():
    assert _ddg_real_url("https://plain.com/x") == "https://plain.com/x"
    assert _ddg_real_url("") == ""


def test_parse_ddg_extracts_title_url_snippet():
    html = """
    <div class="result">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Frealpython.com%2Fasync&rut=x">Async IO Walkthrough</a>
      <a class="result__snippet">Learn async await in Python.</a>
    </div>
    <div class="result">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html">asyncio docs</a>
      <a class="result__snippet">The asyncio library reference.</a>
    </div>
    """
    out = _parse_ddg(html)
    assert len(out) == 2
    assert out[0].title == "Async IO Walkthrough"
    assert out[0].url == "https://realpython.com/async"
    assert "async await" in out[0].snippet
    assert out[0].source == "duckduckgo"
    assert out[0].position == 1
    assert out[1].url == "https://docs.python.org/3/library/asyncio.html"


def test_parse_ddg_skips_blocks_without_link():
    html = '<div class="result"><a class="result__snippet">no link here</a></div>'
    assert _parse_ddg(html) == []


# ─── Bing <cite> reconstruction ─────────────────────────────────────────────

def test_bing_real_url_breadcrumbs():
    assert _bing_real_url("https://www.programiz.com \u203a python-programming \u203a online-compiler") == \
        "https://www.programiz.com/python-programming/online-compiler"


def test_bing_real_url_plain():
    assert _bing_real_url("https://www.python.org") == "https://www.python.org"


def test_bing_real_url_no_scheme_prepends_https():
    assert _bing_real_url("www.learnpython.org \u203a plots") == "https://www.learnpython.org/plots"


def test_bing_real_url_empty():
    assert _bing_real_url("") == ""
    assert _bing_real_url("some prose with spaces") == ""


def test_parse_bing_uses_cite_not_redirect_href():
    html = """
    <li class="b_algo">
      <h2><a href="https://www.bing.com/ck/a?!&&p=opaque">W3Schools Python</a></h2>
      <div class="b_caption"><p>Python tutorial for beginners.</p></div>
      <div class="b_attribution"><cite>https://www.w3schools.com \u203a python</cite></div>
    </li>
    """
    out = _parse_bing(html)
    assert len(out) == 1
    assert out[0].title == "W3Schools Python"
    # The opaque ck/a redirect must NOT leak into the URL; cite is used instead.
    assert out[0].url == "https://www.w3schools.com/python"
    assert "ck/a" not in out[0].url
    assert out[0].source == "bing"


def test_parse_bing_skips_when_no_cite():
    # A result whose real URL can't be recovered is dropped, not a junk redirect.
    html = '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?x">No cite</a></h2></li>'
    assert _parse_bing(html) == []


# ─── Qwant (independent index, keyless JSON API) ─────────────────────────────

def test_qwant_locale_maps_region():
    # region 'us-en' (country-lang) -> Qwant locale 'en_US' (lang_COUNTRY, upper).
    assert _qwant_locale("us-en") == "EN_US"
    assert _qwant_locale("fr-fr") == "FR_FR"
    assert _qwant_locale("de-de") == "DE_DE"
    assert _qwant_locale("") == "EN_US"  # fallback (uppercase, consistent)


def test_parse_qwant_json_extracts_web_items_only():
    payload = (
        '{"status":"success","data":{"result":{"items":{"mainline":['
        '{"type":"web","items":['
        '{"title":"Tokio","url":"https://tokio.rs/","desc":"An async Rust runtime."},'
        '{"title":"GitHub","url":"https://github.com/tokio-rs/tokio","desc":"tokio repo"}'
        ']},'
        '{"type":"ads","items":[{"title":"Ad","url":"https://ad.com/","desc":"buy now"}]}'
        ']}}}}'
    )
    out = _parse_qwant_json(payload, 10)
    assert len(out) == 2  # the ads row is skipped
    assert out[0].title == "Tokio" and out[0].url == "https://tokio.rs/"
    assert out[0].source == "qwant" and out[0].snippet == "An async Rust runtime."
    assert out[1].url == "https://github.com/tokio-rs/tokio"


def test_parse_qwant_json_non_success_returns_empty():
    # rate-limit (error_code 24) / captcha / 403 -> status != "success" -> empty;
    # the caller treats an empty parse + a captcha/error body as blocked.
    assert _parse_qwant_json('{"status":"error","data":{"error_code":24}}', 10) == []
    assert _parse_qwant_json("not json at all", 10) == []


# ─── Yahoo (Bing-feed; RU= redirect decoding) ────────────────────────────────

def test_yahoo_real_url_decodes_ru():
    href = "https://r.search.yahoo.com/.../RU=https%3A%2F%2Fexample.com%2Fpage/RK=2/RS=abc"
    assert _yahoo_real_url(href) == "https://example.com/page"


def test_yahoo_real_url_passthrough_direct():
    assert _yahoo_real_url("https://plain.com/x") == "https://plain.com/x"


def test_parse_yahoo_extracts_algo_results():
    html = """
    <div class="algo-sr">
      <a href="https://r.search.yahoo.com/RU=https%3A%2F%2Fdocs.python.org%2F/RK=2">link</a>
      <h3 class="title">Python docs</h3>
      <div class="compText">The Python tutorial.</div>
    </div>
    """
    out = _parse_yahoo(html)
    assert len(out) == 1
    assert out[0].title == "Python docs"
    assert out[0].url == "https://docs.python.org/"
    assert out[0].source == "yahoo"


# ─── cross-engine consensus merge ────────────────────────────────────────────

def test_merge_consensus_counts_distinct_index_families():
    # bing + yahoo both return the same URL = 1 family (Bing feed, correlated).
    # wikipedia + qwant agreeing on a different URL = 2 independent families.
    per = [
        ([_rr("A", "https://same.com", "bing", 1, "s")], EngineReport("bing", ok=True)),
        ([_rr("A", "https://same.com", "yahoo", 1, "s")], EngineReport("yahoo", ok=True)),
        ([_rr("B", "https://diff.com", "wikipedia", 1, "s")], EngineReport("wikipedia", ok=True)),
        ([_rr("B", "https://diff.com", "qwant", 1, "s")], EngineReport("qwant", ok=True)),
    ]
    merged = {r.url: r for r in merge_dedupe(per, 10)}
    # bing+yahoo = 1 distinct family (both map to the 'bing' family).
    assert merged["https://same.com"].consensus == 1
    assert set(merged["https://same.com"].sources) == {"bing", "yahoo"}
    # wikipedia+qwant = 2 distinct independent families.
    assert merged["https://diff.com"].consensus == 2
    assert set(merged["https://diff.com"].sources) == {"wikipedia", "qwant"}


def test_merge_consensus_surfaces_multi_engine_hits_first():
    # A single-engine result + a 3-engine consensus result: consensus sorts first
    # even though both have the same engine position.
    per = [
        ([_rr("Solo", "https://solo.com", "duckduckgo", 1, "s")], EngineReport("duckduckgo", ok=True)),
        ([_rr("Big", "https://big.com", "duckduckgo", 1, "s")], EngineReport("duckduckgo", ok=True)),
        ([_rr("Big", "https://big.com", "bing", 1, "s")], EngineReport("bing", ok=True)),
        ([_rr("Big", "https://big.com", "qwant", 1, "s")], EngineReport("qwant", ok=True)),
    ]
    merged = merge_dedupe(per, 10)
    assert merged[0].url == "https://big.com"  # 3-family consensus surfaces first
    assert merged[0].consensus == 3


# ─── Wikipedia JSON ──────────────────────────────────────────────────────────

def test_search_wikipedia_parses_api_json(monkeypatch):
    payload = {"query": {"search": [
        {"title": "Coroutine", "snippet": "A <span class=\"searchmatch\">coroutine</span> is a program component."},
        {"title": "Async/await", "snippet": "Async <i>await</i> syntax."},
    ]}}

    async def fake_get(name, url, *, method="GET", form=None, timeout=12):
        return (json.dumps(payload), 200, False, False)

    monkeypatch.setattr(se, "_engine_get", fake_get)
    out, rep = asyncio.run(se.search_wikipedia("coroutine", 5, region="us-en"))
    assert rep.ok and not rep.blocked
    assert out[0].title == "Coroutine"
    assert out[0].url == "https://en.wikipedia.org/wiki/Coroutine"
    # HTML in the snippet is stripped to plain text.
    assert "<span" not in out[0].snippet and "coroutine" in out[0].snippet
    assert out[1].url == "https://en.wikipedia.org/wiki/Async/await"


def test_search_wikipedia_uses_language_suffix_of_region(monkeypatch):
    captured = {}
    payload = {"query": {"search": [{"title": "Python", "snippet": "lang"}]}}
    async def fake_get(name, url, **kw):
        captured["url"] = url
        return (json.dumps(payload), 200, False, False)
    monkeypatch.setattr(se, "_engine_get", fake_get)
    # region "fr-fr" -> Wikipedia host language = "fr" (last segment), NOT "fr-fr" or the country.
    asyncio.run(se.search_wikipedia("python", 3, region="fr-fr"))
    assert "fr.wikipedia.org" in captured["url"]


# ─── merge + dedup + site filters ────────────────────────────────────────────

def _rr(title, url, src="x", pos=1, snip=""):
    return RawResult(title=title, url=url, snippet=snip, source=src, position=pos)


def test_merge_dedups_normalized_trailing_slash():
    per = [
        ([_rr("A", "https://docs.python.org/3/", "duckduckgo", 1, "s1")], EngineReport("duckduckgo", ok=True)),
        ([_rr("A", "https://docs.python.org/3", "bing", 1, "s2")], EngineReport("bing", ok=True)),
    ]
    merged = merge_dedupe(per, 10)
    assert len(merged) == 1  # /3 and /3/ are the same normalized URL


def test_merge_keeps_snippet_when_deduping():
    per = [
        ([_rr("A", "https://x.com", "duckduckgo", 1, "")], EngineReport("duckduckgo", ok=True)),
        ([_rr("A", "https://x.com", "bing", 1, "good snippet")], EngineReport("bing", ok=True)),
    ]
    merged = merge_dedupe(per, 10)
    assert merged[0].snippet == "good snippet"


def test_merge_site_filter_keeps_only_matching():
    per = [([_rr("A", "https://docs.python.org/x", pos=1),
             _rr("B", "https://numpy.org/y", pos=2)], EngineReport("duckduckgo", ok=True))]
    merged = merge_dedupe(per, 10, site="python.org")
    assert len(merged) == 1 and "python.org" in merged[0].url


def test_merge_exclude_sites_drops_matching():
    per = [([_rr("A", "https://pinterest.com/x", pos=1),
             _rr("B", "https://python.org/y", pos=2)], EngineReport("duckduckgo", ok=True))]
    merged = merge_dedupe(per, 10, exclude_sites=["pinterest.com"])
    assert len(merged) == 1 and "python.org" in merged[0].url


# ─── multi_search orchestrator ───────────────────────────────────────────────

def test_multi_search_runs_engines_in_parallel_and_reports(monkeypatch):
    async def fake_ddg(q, n, *, region, freshness, page=0, server=None):
        return ([_rr("DDG", "https://ddg.com", "duckduckgo", 1, "s")], EngineReport("duckduckgo", ok=True))
    async def fake_bing(q, n, *, region, freshness, page=0, server=None):
        return ([], EngineReport("bing", blocked=True, error="captcha"))
    async def fake_wiki(q, n, *, region, freshness, page=0, server=None):
        return ([_rr("Wiki", "https://en.wikipedia.org/wiki/X", "wikipedia", 1, "s")], EngineReport("wikipedia", ok=True))

    monkeypatch.setitem(se._ENGINES, "duckduckgo", fake_ddg)
    monkeypatch.setitem(se._ENGINES, "bing", fake_bing)
    monkeypatch.setitem(se._ENGINES, "wikipedia", fake_wiki)

    ranked, reports = asyncio.run(multi_search("x", 5, engines=["duckduckgo", "bing", "wikipedia"], server=None))
    names_ok = {r.name: r.ok for r in reports}
    assert names_ok == {"duckduckgo": True, "bing": False, "wikipedia": True}
    # Bing is blocked; the other two still contribute.
    assert {r.name for r in reports if r.blocked} == {"bing"}
    urls = {r.url for r in ranked}
    assert "https://ddg.com" in urls and "https://en.wikipedia.org/wiki/X" in urls


def test_multi_search_unknown_engine_ignored(monkeypatch):
    # No engines patched at all is fine because we pass explicit names that are
    # all unknown -> falls back to the default engine set. Instead test that an
    # explicit unknown name is dropped silently.
    async def fake_ddg(q, n, *, region, freshness, page=0, server=None):
        return ([_rr("D", "https://d.com", "duckduckgo", 1)], EngineReport("duckduckgo", ok=True))
    # Replace _ENGINES with only duckduckgo so 'bing'/'wikipedia' are unknown.
    monkeypatch.setattr(se, "_ENGINES", {"duckduckgo": fake_ddg})
    ranked, reports = asyncio.run(multi_search("x", 5, engines=["duckduckgo", "nonexistent"], server=None))
    assert [r.name for r in reports] == ["duckduckgo"]


def test_multi_search_engine_exception_is_caught(monkeypatch):
    async def boom(q, n, *, region, freshness, page=0, server=None):
        raise RuntimeError("engine exploded")
    monkeypatch.setattr(se, "_ENGINES", {"duckduckgo": boom, "bing": boom, "wikipedia": boom})
    ranked, reports = asyncio.run(multi_search("x", 5, server=None))
    assert ranked == []
    # Every engine reported an error (not ok, not blocked) instead of crashing the call.
    assert all(not r.ok and not r.blocked and r.error for r in reports)

# ─── Search Engine Resilience Layer (SERL) ───────────────────────────────────

class _FakeResp:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.body = body.encode() if isinstance(body, str) else body
        self.encoding = "utf-8"
        self.headers = headers or {}


class _FakeSess:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.closed = False
    async def get(self, url, timeout=None):
        return self._next()
    async def post(self, url, data=None, timeout=None):
        return self._next()
    def _next(self):
        self.calls += 1
        r = self.responses[self.calls - 1]
        if isinstance(r, BaseException):
            raise r
        return r


class _FakeCM:
    def __init__(self, sess):
        self.sess = sess
    async def __aenter__(self):
        return self.sess
    async def __aexit__(self, *a):
        self.sess.closed = True
        return False


@pytest.fixture
def fresh_coord(monkeypatch):
    """Reset the coordinator state and stub session creation for each test."""
    se._ENGINES_COORD.states.clear()
    holder = {}
    def make_session(name=""):
        return _FakeCM(holder["sess"])
    monkeypatch.setattr(se._ENGINES_COORD, "_make_session", make_session)
    return holder


def _set_sess(holder, responses):
    holder["sess"] = _FakeSess(responses)
    return holder["sess"]


def test_is_blocked_catches_ddg_202_soft_limit():
    # DDG returns 202 as a soft rate-limit; this was a missed case before SERL.
    assert se._is_blocked(202, "") is True
    assert se._is_blocked(429, "") is True
    assert se._is_blocked(503, "") is True
    assert se._is_blocked(403, "") is True
    assert se._is_blocked(200, "real serp body " * 50) is False


def test_serl_circuit_breaker_skips_request_while_cooling(fresh_coord):
    sess = _set_sess(fresh_coord, [_FakeResp(429, b"")])
    text, status, blocked, cooling = asyncio.run(se._engine_get("duckduckgo", "https://x"))
    assert blocked is True and cooling is False
    assert sess.calls == 1
    # Immediate second call: engine in cooldown -> NO request, cooling=True.
    text2, status2, blocked2, cooling2 = asyncio.run(se._engine_get("duckduckgo", "https://x"))
    assert cooling2 is True and blocked2 is True and text2 is None
    assert sess.calls == 1  # no second network request was made


def test_serl_cooldown_exponential_growth(fresh_coord):
    sess = _set_sess(fresh_coord, [_FakeResp(429, b""), _FakeResp(429, b""), _FakeResp(429, b"")])
    cds = []
    for _ in range(3):
        asyncio.run(se._engine_get("bing", "https://x"))
        cds.append(round(se._ENGINES_COORD.cooldown_left("bing")))
        # simulate cooldown expiring so the next call actually hits the network
        se._ENGINES_COORD.state("bing").cooldown_until = 0.0
    # 15 -> 30 -> 60 (base 15, doubling, capped later)
    assert cds[0] >= 14 and cds[1] >= 29 and cds[2] >= 59
    assert se._ENGINES_COORD.state("bing").consecutive_blocks == 3


def test_serl_recreate_session_every_3_blocks(fresh_coord):
    sess = _set_sess(fresh_coord, [_FakeResp(429, b""), _FakeResp(429, b""), _FakeResp(429, b"")])
    for _ in range(3):
        asyncio.run(se._engine_get("duckduckgo", "https://x"))
        se._ENGINES_COORD.state("duckduckgo").cooldown_until = 0.0
    st = se._ENGINES_COORD.state("duckduckgo")
    assert st.recreate is True  # 3rd consecutive block -> session marked burned
    # Next acquire closes the old session and makes a new one.
    new_sess = _FakeSess([_FakeResp(200, b"ok")])
    fresh_coord["sess"] = new_sess
    st.cooldown_until = 0.0
    asyncio.run(se._engine_get("duckduckgo", "https://x"))
    assert sess.closed is True
    assert new_sess.calls == 1


def test_serl_success_clears_circuit_breaker(fresh_coord):
    _set_sess(fresh_coord, [_FakeResp(429, b""), _FakeResp(200, b"<html>ok</html>")])
    asyncio.run(se._engine_get("duckduckgo", "https://x"))
    assert se._ENGINES_COORD.state("duckduckgo").consecutive_blocks == 1
    se._ENGINES_COORD.state("duckduckgo").cooldown_until = 0.0
    text, status, blocked, cooling = asyncio.run(se._engine_get("duckduckgo", "https://x"))
    assert blocked is False and cooling is False and status == 200
    assert se._ENGINES_COORD.state("duckduckgo").consecutive_blocks == 0
    assert se._ENGINES_COORD.cooldown_left("duckduckgo") == 0.0


def test_serl_reset_clears_cooldown(fresh_coord):
    _set_sess(fresh_coord, [_FakeResp(429, b"")])
    asyncio.run(se._engine_get("bing", "https://x"))
    assert se._ENGINES_COORD.cooldown_left("bing") > 0
    se._ENGINES_COORD.reset("bing")
    assert se._ENGINES_COORD.cooldown_left("bing") == 0.0
    assert se._ENGINES_COORD.state("bing").consecutive_blocks == 0


def test_serl_transport_error_is_not_a_block(fresh_coord):
    _set_sess(fresh_coord, [ConnectionError("network reset")])
    text, status, blocked, cooling = asyncio.run(se._engine_get("duckduckgo", "https://x"))
    assert text is None and blocked is False and cooling is False
    # No cooldown for a transport error (transient, not a rate-limit).
    assert se._ENGINES_COORD.cooldown_left("duckduckgo") == 0.0
    assert se._ENGINES_COORD.state("duckduckgo").consecutive_blocks == 0


def test_serl_retry_after_header_honored(fresh_coord):
    _set_sess(fresh_coord, [_FakeResp(429, b"", headers={"Retry-After": "30"})])
    asyncio.run(se._engine_get("duckduckgo", "https://x"))
    assert se._ENGINES_COORD.cooldown_left("duckduckgo") >= 29.0


def test_serl_pacer_delays_same_engine_burst(monkeypatch, fresh_coord):
    monkeypatch.setitem(se._PACE, "wikipedia", 0.08)
    _set_sess(fresh_coord, [_FakeResp(200, b"{}"), _FakeResp(200, b"{}")])
    import time as _t
    t0 = _t.time()
    asyncio.run(se._engine_get("wikipedia", "https://x"))
    asyncio.run(se._engine_get("wikipedia", "https://x"))
    gap = _t.time() - t0
    assert gap >= 0.08  # second same-engine call is paced


def test_serl_pacer_keeps_engines_parallel(monkeypatch, fresh_coord):
    monkeypatch.setitem(se._PACE, "wikipedia", 0.08)
    monkeypatch.setitem(se._PACE, "duckduckgo", 0.08)
    _set_sess(fresh_coord, [_FakeResp(200, b"{}"), _FakeResp(200, b"{}")])
    import time as _t
    async def _both():
        await asyncio.gather(
            se._engine_get("wikipedia", "https://x"),
            se._engine_get("duckduckgo", "https://x"),
        )
    t0 = _t.time()
    asyncio.run(_both())
    gap = _t.time() - t0
    assert gap < 0.16  # different engines -> independent locks -> parallel


def test_serl_close_all_closes_sessions(fresh_coord):
    sess = _set_sess(fresh_coord, [_FakeResp(200, b"{}")])
    asyncio.run(se._engine_get("duckduckgo", "https://x"))
    asyncio.run(se.close_search_engines())
    assert sess.closed is True
    assert se._ENGINES_COORD.state("duckduckgo").sess is None


# ─── multi_search adaptive reserve tier (Google) ─────────────────────────────

# ─── prewarm + hard deadline (cold-start / timeout fix) ──────────────────────

def test_serl_warmup_does_not_touch_circuit_breaker_or_pacer(fresh_coord):
    sess = _set_sess(fresh_coord, [_FakeResp(200, b"ok")])
    asyncio.run(se._ENGINES_COORD.warmup("duckduckgo", "https://html.duckduckgo.com/html/?q=test"))
    st = se._ENGINES_COORD.state("duckduckgo")
    assert sess.calls == 1                      # one throwaway GET fired
    assert st.created is True and st.sess is not None
    # warmup must NOT trigger a cooldown, NOT count as a block, NOT set last_req
    # (so the first real search is not paced because of the warmup).
    assert st.consecutive_blocks == 0
    assert st.cooldown_until == 0.0
    assert st.last_req == 0.0


def test_serl_warmup_is_best_effort_swallows_errors(fresh_coord):
    sess = _set_sess(fresh_coord, [ConnectionError("boom")])
    # Must not raise even if the warmup GET fails.
    asyncio.run(se._ENGINES_COORD.warmup("bing", "https://www.bing.com/search?q=test"))
    # No cooldown from a warmup failure.
    assert se._ENGINES_COORD.cooldown_left("bing") == 0.0
    assert se._ENGINES_COORD.state("bing").consecutive_blocks == 0


def test_multi_search_hard_deadline_cuts_slow_engine_returns_partial(monkeypatch):
    # A slow engine (sleeps past the deadline) is cut; a fast engine still serves.
    monkeypatch.setattr(se, "SEARCH_ENGINE_DEADLINE", 0.3)
    async def slow(q, n, *, region, freshness, page=0, server=None):
        await asyncio.sleep(1.0)
        return ([_rr("S", "https://slow.com", "duckduckgo", 1)], EngineReport("duckduckgo", ok=True))
    async def fast(q, n, *, region, freshness, page=0, server=None):
        return ([_rr("F", "https://fast.com", "wikipedia", 1)], EngineReport("wikipedia", ok=True))
    monkeypatch.setattr(se, "_ENGINES", {"duckduckgo": slow, "wikipedia": fast})
    ranked, reports = asyncio.run(multi_search("x", 5, engines=["duckduckgo", "wikipedia"], server=None))
    by_name = {r.name: r for r in reports}
    assert by_name["duckduckgo"].blocked is True and "timed out" in by_name["duckduckgo"].error
    assert by_name["wikipedia"].ok is True
    # Partial results from the fast engine are returned; the slow one did not hang the call.
    assert any(r.url == "https://fast.com" for r in ranked)
    assert not any(r.url == "https://slow.com" for r in ranked)


def test_multi_search_hard_deadline_cuts_reserve_google_too(monkeypatch):
    # Reserve Google tier was REMOVED (Google scraping is hopeless). This test now
    # verifies the replacement behavior: with a blocked primary + thin results and
    # NO reserve tier, multi_search simply returns the partial results (no google
    # fan-out happens at all).
    monkeypatch.setattr(se, "SEARCH_ENGINE_DEADLINE", 0.3)
    async def blocked_ddg(q, n, *, region, freshness, page=0, server=None):
        return ([], EngineReport("duckduckgo", blocked=True, error="captcha"))
    async def thin_wiki(q, n, *, region, freshness, page=0, server=None):
        return ([_rr("W", "https://en.wikipedia.org/wiki/A", "wikipedia", 1)], EngineReport("wikipedia", ok=True))
    monkeypatch.setattr(se, "_ENGINES", {"duckduckgo": blocked_ddg, "wikipedia": thin_wiki})
    ranked, reports = asyncio.run(multi_search("x", 5, engines=["duckduckgo", "wikipedia"], server=object()))
    # No google engine was fired (reserve tier is gone).
    assert not any(r.name == "google" for r in reports)
    assert any(r.url == "https://en.wikipedia.org/wiki/A" for r in ranked)


def test_prewarm_search_engines_warms_each_default_engine(monkeypatch):
    warmed = []
    async def fake_warmup(name, url, timeout=6.0):
        warmed.append((name, url))
    monkeypatch.setattr(se._ENGINES_COORD, "warmup", fake_warmup)
    asyncio.run(se.prewarm_search_engines())
    names = {n for n, _ in warmed}
    assert names == {"duckduckgo", "bing", "qwant"}  # 3 independent HTTP defaults
    # each warmup URL hits the engine's real search host
    assert all("duckduckgo" in u for n, u in warmed if n == "duckduckgo")
    assert all("bing.com" in u for n, u in warmed if n == "bing")
    assert all("qwant" in u for n, u in warmed if n == "qwant")


def test_prewarm_search_engines_never_raises(monkeypatch):
    async def boom(name, url, timeout=6.0):
        raise RuntimeError("warmup exploded")
    monkeypatch.setattr(se._ENGINES_COORD, "warmup", boom)
    asyncio.run(se.prewarm_search_engines())  # must not raise


# ─── pagination (page -> engine offset param) ────────────────────────────────

def _capture_engine_get(monkeypatch, captured):
    async def fake_get(name, url, *, method="GET", form=None, timeout=12):
        captured["url"] = url
        captured["name"] = name
        return ("<html></html>", 200, False, False)
    monkeypatch.setattr(se, "_engine_get", fake_get)


def test_search_ddg_page_adds_offset_param(monkeypatch):
    cap = {}; _capture_engine_get(monkeypatch, cap)
    asyncio.run(se.search_ddg("test", 10, region="us-en", page=2))
    assert "&s=20" in cap["url"]              # page(2) * max_results(10) = 20


def test_search_ddg_page_zero_omits_offset(monkeypatch):
    cap = {}; _capture_engine_get(monkeypatch, cap)
    asyncio.run(se.search_ddg("test", 10, region="us-en", page=0))
    assert "&s=" not in cap["url"]


def test_search_bing_page_adds_first_param(monkeypatch):
    cap = {}; _capture_engine_get(monkeypatch, cap)
    asyncio.run(se.search_bing("test", 10, region="us-en", page=2))
    assert "&first=21" in cap["url"]          # page(2)*10 + 1 = 21


def test_search_wikipedia_page_adds_sroffset_param(monkeypatch):
    cap = {}; _capture_engine_get(monkeypatch, cap)
    payload = {"query": {"search": [{"title": "X", "snippet": "s"}]}}
    async def fake_get(name, url, *, method="GET", form=None, timeout=12):
        cap["url"] = url
        return (json.dumps(payload), 200, False, False)
    monkeypatch.setattr(se, "_engine_get", fake_get)
    asyncio.run(se.search_wikipedia("test", 10, region="us-en", page=3))
    assert "&sroffset=30" in cap["url"]       # page(3)*10 = 30


def test_multi_search_threads_page_to_engines(monkeypatch):
    seen = {}
    async def fake_ddg(q, n, *, region, freshness, page=0, server=None):
        seen["ddg_page"] = page
        return ([_rr("D", "https://d.com", "duckduckgo", 1)], EngineReport("duckduckgo", ok=True))
    async def fake_wiki(q, n, *, region, freshness, page=0, server=None):
        seen["wiki_page"] = page
        return ([_rr("W", "https://en.wikipedia.org/wiki/W", "wikipedia", 1)], EngineReport("wikipedia", ok=True))
    monkeypatch.setattr(se, "_ENGINES", {"duckduckgo": fake_ddg, "wikipedia": fake_wiki})
    asyncio.run(multi_search("x", 10, engines=["duckduckgo", "wikipedia"], page=4, server=None))
    assert seen["ddg_page"] == 4 and seen["wiki_page"] == 4

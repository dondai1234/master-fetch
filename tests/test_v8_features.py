"""v8 feature tests: outgoing-links, sitemap mode, related-queries, PDF section-map.

Each test asserts the NEW capability does something meaningful (not just that it
ran): links classify correctly, sitemap parses urlset + sitemapindex + gzip +
recurses, related-queries mine real bigrams and exclude the query, the PDF
section-map computes page ranges, and the tool defs expose the new options.
"""

import gzip
import pytest

from master_fetch.links import extract_links
from master_fetch.sitemap import parse_sitemap, discover_sitemap, SitemapURL
from master_fetch.search import _related_queries, SearchResult, SearchResponseModel
from master_fetch.pdf_extractor import _add_end_pages, _heading_outline
from master_fetch.crawl import CrawlResponseModel, CrawlPage
from master_fetch.server import MasterFetchServer, ResponseModel


# ─── outgoing links ──────────────────────────────────────────────────────────

_PAGE_HTML = """
<html><head><title>X</title>
<link rel="canonical" href="https://primary.example.com/real"/>
</head><body>
<nav><a href="/about">About</a><a href="/home">Home</a></nav>
<article>
  <p>See <a href="https://arxiv.org/abs/1234.5678">the paper</a> and
     <a href="/docs/intro">our docs</a>.</p>
  <a href="/docs/intro">our docs</a>
</article>
<footer><a href="/privacy">Privacy</a></footer>
<a href="javascript:void(0)">noop</a>
<a href="#top">skip</a>
</body></html>
"""


def test_extract_links_classifies_citations_navigation_external():
    out = extract_links(_PAGE_HTML, "https://blog.example.com/post", {"canonical": "https://primary.example.com/real"})
    hrefs_cit = [c["url"] for c in out["citations"]]
    hrefs_nav = [c["url"] for c in out["navigation"]]
    hrefs_ext = [c["url"] for c in out["external"]]
    # arxiv is off-domain -> external
    assert "https://arxiv.org/abs/1234.5678" in hrefs_ext
    # /docs/intro inside <article> -> citation
    assert "https://blog.example.com/docs/intro" in hrefs_cit
    # /about + /home inside <nav>, /privacy inside <footer> -> navigation
    assert "https://blog.example.com/about" in hrefs_nav
    assert "https://blog.example.com/privacy" in hrefs_nav
    # javascript: and # dropped
    assert not any("javascript" in u for u in hrefs_cit + hrefs_nav + hrefs_ext)


def test_extract_links_primary_source_from_canonical():
    out = extract_links(_PAGE_HTML, "https://blog.example.com/post",
                        {"canonical": "https://primary.example.com/real"})
    assert out["primary_source"] == "https://primary.example.com/real"


def test_extract_links_primary_source_from_known_host():
    # No canonical different-host; fall back to a citation/external on a known primary host.
    html = '<article><a href="https://github.com/dondai1234/master-fetch">source</a></article>'
    out = extract_links(html, "https://blog.example.com/post", {})
    assert out["primary_source"] == "https://github.com/dondai1234/master-fetch"


def test_extract_links_dedup_and_robust():
    # same URL appears twice in <article> -> one citation entry
    out = extract_links('<article><a href="/x">a</a><a href="/x">b</a></article>',
                        "https://ex.com/p", {})
    assert len(out["citations"]) == 1
    # broken / empty input never raises
    assert extract_links("", "https://x.com", {}) == {"citations": [], "navigation": [], "external": [], "primary_source": ""}
    assert extract_links("<not html><<<", "https://x.com", {})["citations"] == []


# ─── sitemap ─────────────────────────────────────────────────────────────────

_URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/</loc><lastmod>2026-01-01</lastmod></url>
  <url><loc>https://example.com/a</loc><lastmod>2026-02-01</lastmod></url>
  <url><loc>https://example.com/b</loc></url>
</urlset>"""

_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/s1.xml</loc></sitemap>
  <sitemap><loc>https://example.com/s2.xml</loc></sitemap>
</sitemapindex>"""


def test_parse_sitemap_urlset():
    urls, children = parse_sitemap(_URLSET)
    assert children == []
    assert [u.url for u in urls] == ["https://example.com/", "https://example.com/a", "https://example.com/b"]
    assert urls[1].lastmod == "2026-02-01"


def test_parse_sitemap_index_yields_children():
    urls, children = parse_sitemap(_INDEX)
    assert urls == []
    assert children == ["https://example.com/s1.xml", "https://example.com/s2.xml"]


def test_parse_sitemap_gzip_and_broken():
    gz = gzip.compress(_URLSET)
    urls, _ = parse_sitemap(gz)
    assert len(urls) == 3  # gunzipped + parsed
    assert parse_sitemap(b"") == ([], [])
    assert parse_sitemap(b"<not xml<<<") == ([], [])


def test_discover_sitemap_via_robots_then_recurse():
    # robots.txt declares one sitemap which is an index pointing at two leaf urlsets.
    s1 = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/p1</loc></url></urlset>'
    s2 = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/p2</loc></url></urlset>'
    routes = {
        "https://example.com/robots.txt": (200, b"Sitemap: https://example.com/index.xml\nUser-agent: *\n"),
        "https://example.com/index.xml": (200, _INDEX),
        "https://example.com/s1.xml": (200, s1),
        "https://example.com/s2.xml": (200, s2),
    }

    def fake_get(url):
        return routes.get(url)

    res = discover_sitemap("https://example.com/", http_get=fake_get, max_urls=100)
    assert res.via == "robots"
    assert res.robots_checked is True
    # index + both leaf sitemaps were fetched + parsed
    assert res.sitemaps_used == ["https://example.com/index.xml",
                                 "https://example.com/s1.xml",
                                 "https://example.com/s2.xml"]
    leaf_urls = [u.url for u in res.urls]
    assert leaf_urls == ["https://example.com/p1", "https://example.com/p2"]


def test_discover_sitemap_conventional_fallback_when_no_robots_directive():
    routes = {
        "https://example.com/robots.txt": (200, b"User-agent: *\nAllow: /\n"),  # no Sitemap:
        "https://example.com/sitemap.xml": (200, _URLSET),
        "https://example.com/sitemap_index.xml": (200, b"<x/>"),
    }

    def fake_get(url):
        return routes.get(url)

    res = discover_sitemap("https://example.com/", http_get=fake_get, max_urls=100)
    assert res.via == "conventional"
    assert len(res.urls) == 3


def test_discover_sitemap_none_returns_empty_not_raise():
    def fake_get(url):
        return None  # everything 404s
    res = discover_sitemap("https://example.com/", http_get=fake_get)
    assert res.urls == [] and res.sitemaps_used == []


# ─── related queries ─────────────────────────────────────────────────────────

def test_related_queries_mines_bigrams_and_excludes_query():
    results = [
        SearchResult(title="Rust async runtime tokio overview", url="u1", snippet="tokio runtime uses async tasks and io driver"),
        SearchResult(title="Tokio runtime internals", url="u2", snippet="the tokio runtime scheduler spawns async tasks"),
        SearchResult(title="Async Rust guide", url="u3", snippet="async tasks in rust with tokio runtime"),
    ]
    rq = _related_queries("tokio runtime", results)
    # 'tokio' and 'runtime' are query tokens -> must NOT appear as a suggestion alone
    assert all("tokio" not in q.split() and "runtime" not in q.split() for q in rq), rq
    # 'async tasks' appears in all 3 -> should surface
    assert any("async tasks" == q for q in rq), rq
    assert len(rq) <= 6


def test_related_queries_empty_on_no_results():
    assert _related_queries("x", []) == []


def test_search_response_model_has_related_queries_field():
    m = SearchResponseModel(query="q", results=[])
    assert m.related_queries == []
    assert "related_queries" in SearchResponseModel.model_fields


# ─── PDF section-map ─────────────────────────────────────────────────────────

def test_add_end_pages_computes_ranges():
    toc = [
        {"level": 1, "title": "1 Intro", "page": 1},
        {"level": 2, "title": "1.1 Background", "page": 2},
        {"level": 1, "title": "2 Method", "page": 5},
    ]
    out = _add_end_pages(toc, total_pages=10)
    assert out[0]["end_page"] == 4   # next level<=1 is "2 Method" at page 5 -> 4
    assert out[1]["end_page"] == 4   # next level<=2 is "2 Method" at page 5 -> 4
    assert out[2]["end_page"] == 10  # last -> total


def test_heading_outline_builds_fallback_toc_clamped_to_extracted():
    headings = [
        {"level": 1, "title": "Intro", "page": 1},
        {"level": 2, "title": "Details", "page": 1},
        {"level": 1, "title": "Method", "page": 3},
        {"level": 1, "title": "Appendix", "page": 9},  # not in extracted set -> dropped
    ]
    out = _heading_outline(headings, page_nums=[1, 2, 3], total_pages=9)
    titles = [e["title"] for e in out]
    assert "Appendix" not in titles  # page 9 not extracted
    assert "Intro" in titles and "Method" in titles
    # end_page clamped to max extracted page (3)
    intro = next(e for e in out if e["title"] == "Intro")
    assert intro["end_page"] <= 3


# ─── tool def surface ────────────────────────────────────────────────────────

def test_tool_defs_expose_v8_options():
    srv = MasterFetchServer()
    defs = {d["name"]: d for d in srv._TOOL_DEFS}
    fetch_opts = defs["mcp_smart_fetch"]["inputSchema"]["properties"]["options"]["description"]
    assert "include_links" in fetch_opts
    crawl_opts = defs["mcp_smart_crawl"]["inputSchema"]["properties"]["options"]["description"]
    assert "sitemap" in crawl_opts
    search_desc = defs["mcp_smart_search"]["description"]
    assert "related_queries" in search_desc
    # screenshot keeps session_id (regression)
    assert "session_id" in defs["mcp_screenshot"]["inputSchema"]["properties"]


def test_response_and_crawl_models_have_v8_fields():
    r = ResponseModel(url="u", status=200, content=[""])
    assert r.links == {}  # default empty, only populated when include_links=true
    assert "links" in ResponseModel.model_fields
    assert "lastmod" in CrawlPage.model_fields
    assert "sitemap_used" in CrawlResponseModel.model_fields
    assert "sitemaps" in CrawlResponseModel.model_fields


def test_hound_instructions_no_stale_open_session_protip():
    from master_fetch.server import HOUND_INSTRUCTIONS
    assert "open_session once" not in HOUND_INSTRUCTIONS
    # new features mentioned
    assert "include_links" in HOUND_INSTRUCTIONS
    assert "sitemap=true" in HOUND_INSTRUCTIONS
    assert "related_queries" in HOUND_INSTRUCTIONS

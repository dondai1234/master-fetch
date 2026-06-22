"""Tests for smart_crawl: link extraction, BFS, caps, discover_only, focus, budget."""

import asyncio

import pytest

from master_fetch.crawl import (
    extract_same_domain_links, score_link, smart_crawl, CrawlResponseModel,
)
from master_fetch.server import ResponseModel


START = "https://docs.example.com/docs/"
# A page that links to two same-domain pages + an external + an asset + a fragment.
HTML_START = (
    '<html><head><title>Docs Home</title></head><body>'
    '<a href="/docs/a">Page A</a>'
    '<a href="/docs/b">Page B</a>'
    '<a href="https://other.com/x">External</a>'
    '<a href="/docs/image.png">an image</a>'
    '<a href="#top">top</a>'
    '<a href="mailto:a@b.com">mail</a>'
    '<a href="/blog/post">Blog post</a>'
    '</body></html>'
)
HTML_A = '<html><head><title>A</title></head><body><a href="/docs/a2">A2</a><p>' + ('alpha content sentence. ' * 40) + '</p></body></html>'
HTML_B = '<html><head><title>B</title></head><body><p>' + ('bravo content paragraph here. ' * 40) + '</p></body></html>'
HTML_BLOG = '<html><head><title>Blog</title></head><body><p>' + ('blog body text repeats. ' * 40) + '</p></body></html>'


class FakeServer:
    """Stand-in MasterFetchServer with a canned smart_fetch (no network)."""
    def __init__(self, pages_html):
        self.pages_html = pages_html
        self.fetched = []

    async def smart_fetch(self, url, extraction_type="html", cache_ttl=3600,
                          max_content_chars=200000, force_fetcher=None,
                          respect_robots=False, timeout=30000, **kw):
        self.fetched.append(url)
        html = self.pages_html.get(url, "<html><head><title>Empty</title></head><body></body></html>")
        return ResponseModel(status=200, content=[html], url=url, content_ok=True,
                             fetcher_used="http", summary="200 OK")


def _server():
    return FakeServer({
        START: HTML_START,
        "https://docs.example.com/docs/a": HTML_A,
        "https://docs.example.com/docs/b": HTML_B,
        "https://docs.example.com/blog/post": HTML_BLOG,
    })


# ─── extract_same_domain_links ──────────────────────────────────────────

def test_link_extraction_same_domain_only():
    links = extract_same_domain_links(HTML_START, START, START)
    urls = [u for u, _ in links]
    assert "https://docs.example.com/docs/a" in urls
    assert "https://docs.example.com/docs/b" in urls
    assert "https://docs.example.com/blog/post" in urls
    # External / asset / fragment / mailto dropped.
    assert not any("other.com" in u for u in urls)
    assert not any("image.png" in u for u in urls)
    assert not any(u.endswith("#top") for u in urls)
    assert not any("mailto" in u for u in urls)


def test_link_extraction_dedup():
    html = '<a href="/docs/a">A</a><a href="/docs/a">A again</a>'
    links = extract_same_domain_links(html, START, START)
    assert len(links) == 1


def test_link_extraction_path_include():
    links = extract_same_domain_links(HTML_START, START, START, path_include=["/docs/"])
    urls = [u for u, _ in links]
    assert all(u.startswith("https://docs.example.com/docs/") for u in urls)
    assert "https://docs.example.com/blog/post" not in urls


def test_link_extraction_path_exclude():
    links = extract_same_domain_links(HTML_START, START, START, path_exclude=["/blog/"])
    urls = [u for u, _ in links]
    assert "https://docs.example.com/blog/post" not in urls
    assert "https://docs.example.com/docs/a" in urls


def test_link_extraction_captures_anchor_text():
    links = extract_same_domain_links(HTML_START, START, START)
    text_by_url = {u: t for u, t in links}
    assert text_by_url["https://docs.example.com/docs/a"] == "Page A"


# ─── score_link ─────────────────────────────────────────────────────────

def test_score_link_relevant_text_beats_irrelevant():
    hi = score_link("https://x.com/api", "python asyncio tutorial", "python asyncio")
    lo = score_link("https://x.com/random", "click here", "python asyncio")
    assert hi > lo


# ─── smart_crawl (BFS) ──────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_crawl_bfs_basic():
    srv = _server()
    resp = asyncio.run(smart_crawl(srv, START, max_pages=10, max_depth=1, cache_ttl=0))
    assert isinstance(resp, CrawlResponseModel)
    fetched = {p.url for p in resp.pages}
    assert START in fetched
    assert "https://docs.example.com/docs/a" in fetched
    assert "https://docs.example.com/docs/b" in fetched
    # External not crawled.
    assert not any("other.com" in u for u in fetched)
    assert resp.pages_crawled >= 3
    assert resp.pages_discovered >= 3
    # Pages carry markdown content + content_ok.
    a_page = next(p for p in resp.pages if p.url.endswith("/docs/a"))
    assert a_page.content_ok is True
    assert a_page.content  # markdown present
    assert a_page.title == "A"


def test_crawl_max_depth_zero_only_start():
    srv = _server()
    resp = asyncio.run(smart_crawl(srv, START, max_pages=10, max_depth=0, cache_ttl=0))
    assert resp.pages_crawled == 1
    assert resp.pages[0].url == START
    # No links followed, so only the start URL was discovered.
    assert resp.pages_discovered == 1


def test_crawl_max_pages_cap():
    srv = _server()
    resp = asyncio.run(smart_crawl(srv, START, max_pages=2, max_depth=2, cache_ttl=0))
    assert resp.pages_crawled == 2
    assert resp.truncated_by_max_pages is True
    assert resp.next_action  # told to raise caps


def test_crawl_discover_only_returns_urls_no_content():
    srv = _server()
    resp = asyncio.run(smart_crawl(srv, START, max_pages=10, max_depth=1,
                                   discover_only=True, cache_ttl=0))
    assert resp.discover_only is True
    assert resp.pages_discovered >= 3
    # Every page has empty content.
    assert all(p.content == [] for p in resp.pages)
    # But titles are still captured.
    assert any(p.title == "Docs Home" for p in resp.pages)


def test_crawl_focus_prioritizes_relevant_within_budget():
    start = "https://docs.example.com/start"
    html = (
        '<html><head><title>S</title></head><body>'
        '<a href="/docs/asyncio">python asyncio guide</a>'
        '<a href="/docs/requests">python requests guide</a>'
        '<a href="/docs/cooking">best pasta recipes</a>'
        '</body></html>'
    )
    pages = {
        start: html,
        "https://docs.example.com/docs/asyncio": "<html><body>asyncio content</body></html>",
        "https://docs.example.com/docs/requests": "<html><body>requests content</body></html>",
        "https://docs.example.com/docs/cooking": "<html><body>pasta content</body></html>",
    }
    srv = FakeServer(pages)
    # max_pages=2 -> start + ONE depth-1 page. With focus='python', the cooking
    # page must NOT be the one picked (asyncio/requests are more relevant).
    resp = asyncio.run(smart_crawl(srv, start, max_pages=2, max_depth=1,
                                   focus="python", cache_ttl=0))
    depth1 = [p.url for p in resp.pages if p.depth == 1]
    assert len(depth1) == 1
    assert "cooking" not in depth1[0]
    assert ("asyncio" in depth1[0]) or ("requests" in depth1[0])


def test_crawl_token_budget_truncates():
    srv = _server()
    resp = asyncio.run(smart_crawl(srv, START, max_pages=10, max_depth=2,
                                   max_content_chars_per=100, max_total_chars=150,
                                   cache_ttl=0))
    assert resp.truncated_by_budget is True
    assert resp.next_action


def test_crawl_path_include_scopes_discovery():
    srv = _server()
    resp = asyncio.run(smart_crawl(srv, START, max_pages=10, max_depth=1,
                                   path_include=["/docs/"], cache_ttl=0))
    fetched = {p.url for p in resp.pages}
    assert "https://docs.example.com/docs/a" in fetched
    assert "https://docs.example.com/blog/post" not in fetched


def test_crawl_invalid_url_returns_error():
    srv = _server()
    resp = asyncio.run(smart_crawl(srv, "not a url", max_pages=5, cache_ttl=0))
    assert resp.error
    assert resp.pages == []


def test_crawl_summary_and_next_action_when_complete():
    srv = _server()
    resp = asyncio.run(smart_crawl(srv, START, max_pages=10, max_depth=1, cache_ttl=0))
    assert resp.summary
    # Completed without hitting caps -> no next_action.
    assert resp.next_action == ""

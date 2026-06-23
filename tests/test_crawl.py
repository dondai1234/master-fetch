"""Tests for smart_crawl: link extraction, BFS, caps, discover_only, focus, budget."""

import asyncio

import pytest

from master_fetch.crawl import (
    extract_same_domain_links, score_link, smart_crawl, CrawlResponseModel,
    normalize_url,
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
    assert normalize_url(START) in fetched
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
    assert resp.pages[0].url == normalize_url(START)
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


# ─── v6 flagship: normalization, adaptive extraction, selective, best-first ──

def test_normalize_url_collapses_trailing_slash_and_tracking():
    from master_fetch.crawl import normalize_url
    assert normalize_url("https://Docs.example.com/docs/") == "https://docs.example.com/docs"
    assert normalize_url("https://example.com/x?utm_source=foo&a=1") == "https://example.com/x?a=1"
    assert normalize_url("https://example.com:443/p") == "https://example.com/p"
    # Root keeps its slash; pagination query preserved.
    assert normalize_url("https://example.com/") == "https://example.com/"
    assert normalize_url("https://example.com/list?page=2") == "https://example.com/list?page=2"


def test_crawl_trailing_slash_dedup():
    """S3/S7: /docs and /docs/ are the same page, crawled once."""
    srv = _server()
    # Start without trailing slash; the page links to itself WITH a slash.
    start = "https://docs.example.com/docs"
    srv.pages_html[start] = HTML_START  # reachable via the no-slash form too
    resp = asyncio.run(smart_crawl(srv, start, max_pages=10, max_depth=1, cache_ttl=0))
    fetched = {p.url for p in resp.pages}
    # Only one normalized form of the start URL appears.
    assert "https://docs.example.com/docs" in fetched
    assert sum(1 for u in fetched if u == "https://docs.example.com/docs") == 1


def test_crawl_list_page_returns_link_list():
    """S2: a list/index page (HN-style) is extracted as a structured link list,
    not an empty content_ok=false page."""
    list_html = (
        '<html><head><title>HN</title></head><body>'
        + ''.join(f'<a href="/item/{i}">Story number {i}</a>' for i in range(20))
        + '</body></html>'
    )
    srv = FakeServer({"https://hn.example.com/": list_html})
    resp = asyncio.run(smart_crawl(srv, "https://hn.example.com/", max_pages=1,
                                   max_depth=0, cache_ttl=0))
    p = resp.pages[0]
    assert p.content_ok is True
    assert p.page_type == "list"
    assert "[Story number 0]" in p.content[0]
    assert "(https://hn.example.com/item/0)" in p.content[0]


def test_crawl_selective_crawl_urls():
    """S9: crawl_urls=[...] fetches exactly that subset, no further discovery."""
    srv = _server()
    resp = asyncio.run(smart_crawl(
        srv, START, crawl_urls=["/docs/a", "/docs/b"], cache_ttl=0))
    fetched = {p.url for p in resp.pages}
    assert "https://docs.example.com/docs/a" in fetched
    assert "https://docs.example.com/docs/b" in fetched
    assert "https://docs.example.com/docs" not in fetched  # start not auto-fetched
    # No discovery expansion (max_depth forced to 0 for selective).
    assert resp.pages_discovered == 2


def test_crawl_network_error_status_neg1():
    """S11: a network failure reports status -1, not 0."""
    class ErrServer(FakeServer):
        async def smart_fetch(self, url, **kw):
            raise ConnectionError("connection closed")
    srv = ErrServer({})
    resp = asyncio.run(smart_crawl(srv, "https://docs.example.com/docs",
                                   max_pages=1, max_depth=0, cache_ttl=0))
    assert resp.pages[0].status == -1
    assert resp.pages[0].content_ok is False


def test_crawl_best_first_prefers_content_over_junk():
    """S5: with a tight max_pages, content pages (/docs/api) are crawled before
    junk pages (/login, /submit)."""
    html = (
        '<html><head><title>root</title></head><body>'
        '<a href="/login">Login</a>'
        '<a href="/submit">Submit</a>'
        '<a href="/docs/api">API reference</a>'
        '<a href="/docs/guide">Guide</a>'
        '</body></html>'
    )
    srv = FakeServer({
        "https://site.example.com/": html,
        "https://site.example.com/login": "<html><body>login form</body></html>",
        "https://site.example.com/submit": "<html><body>submit form</body></html>",
        "https://site.example.com/docs/api": "<html><body>" + ("api content text. " * 40) + "</body></html>",
        "https://site.example.com/docs/guide": "<html><body>" + ("guide content text. " * 40) + "</body></html>",
    })
    # max_pages=3: root + 2 best. The 2 best should be the /docs/* pages, not login/submit.
    resp = asyncio.run(smart_crawl(srv, "https://site.example.com/",
                                   max_pages=3, max_depth=1, cache_ttl=0))
    fetched = {p.url for p in resp.pages}
    assert "https://site.example.com/docs/api" in fetched
    assert "https://site.example.com/docs/guide" in fetched
    assert "https://site.example.com/login" not in fetched
    assert "https://site.example.com/submit" not in fetched

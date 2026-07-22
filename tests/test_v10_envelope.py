"""v10 envelope tests: source authority, freshness, page-type detection, and
the smart next_action branches that consume them.

These are the DERIVED fields (recomputed by _apply_envelope on every result);
the PRESERVED fields are covered by test_v10_construction.py.
"""
from master_fetch.envelope import (
    classify_source, compute_freshness, detect_page_type, page_type_from_error,
    STALE_DAYS,
)
from master_fetch.server import ResponseModel, _with_agent_hints


# ─── classify_source ───────────────────────────────────────────────

def test_classify_gov_edu_github_official():
    assert classify_source("https://www.nih.gov/about") == ("gov", True)
    assert classify_source("https://www.irs.gov/forms") == ("gov", True)
    assert classify_source("https://www.harvard.edu/admissions") == ("edu", True)
    assert classify_source("https://www.ox.ac.uk/") == ("edu", True)
    assert classify_source("https://github.com/dondai1234/master-fetch") == ("github", True)
    assert classify_source("https://raw.githubusercontent.com/x/y/main/z") == ("github", True)
    assert classify_source("https://dondai1234.github.io/blog/") == ("github", True)


def test_classify_docs_site_official():
    assert classify_source("https://docs.python.org/3/") == ("docs-site", True)
    assert classify_source("https://developers.google.com/sheets/api") == ("docs-site", True)
    assert classify_source("https://developer.mozilla.org/en/docs") == ("docs-site", True)


def test_classify_qa_forum_blog_news_ecommerce():
    assert classify_source("https://stackoverflow.com/questions/1") == ("qa", False)
    assert classify_source("https://math.stackexchange.com/q/1") == ("qa", False)
    assert classify_source("https://www.reddit.com/r/python") == ("forum", False)
    assert classify_source("https://old.reddit.com/r/python") == ("forum", False)
    assert classify_source("https://discourse.example.com/t/x") == ("forum", False)
    assert classify_source("https://medium.com/@user/post") == ("blog", False)
    assert classify_source("https://user.substack.com/p/abc") == ("blog", False)
    assert classify_source("https://www.nytimes.com/2026/01/01/x.html") == ("news", False)
    assert classify_source("https://shop.example.com/product") == ("ecommerce", False)


def test_classify_unknown():
    assert classify_source("https://random-example.org/page") == ("unknown", False)
    assert classify_source("") == ("unknown", False)
    assert classify_source("not a url") == ("unknown", False)


# ─── compute_freshness ────────────────────────────────────────────

def test_freshness_recent_not_stale():
    age, stale = compute_freshness({"published_time": "2026-07-01"}, "2026-07-18T12:00:00+00:00")
    assert 10 <= age <= 20
    assert stale is False


def test_freshness_old_is_stale():
    age, stale = compute_freshness({"published_time": "2020-01-01"}, "2026-07-18T12:00:00+00:00")
    assert age > STALE_DAYS
    assert stale is True


def test_freshness_no_date():
    assert compute_freshness({}, "2026-07-18T12:00:00+00:00") == (-1, False)
    assert compute_freshness({"title": "no dates here"}, "2026-07-18T12:00:00+00:00") == (-1, False)


def test_freshness_future_date_untrustworthy():
    # A page dated in the future is bad metadata — don't claim a (negative) age.
    age, stale = compute_freshness({"published_time": "2030-01-01"}, "2026-07-18T12:00:00+00:00")
    assert age == -1
    assert stale is False


def test_freshness_prefers_modified_over_published():
    # modified_time is recent, published_time is old -> not stale (modified wins).
    age, stale = compute_freshness(
        {"published_time": "2010-01-01", "modified_time": "2026-07-10"},
        "2026-07-18T12:00:00+00:00",
    )
    assert 1 <= age <= 12
    assert stale is False


def test_freshness_compact_wayback_timestamp():
    # Wayback timestamps are YYYYMMDD; the parser must handle them.
    age, stale = compute_freshness({"published_time": "20240101"}, "2026-07-18T12:00:00+00:00")
    assert age > 365
    assert stale is True


# ─── detect_page_type ──────────────────────────────────────────────

def test_page_type_from_content_type():
    assert detect_page_type("", "https://x.com/a", "application/pdf") == "pdf"
    assert detect_page_type("", "https://x.com/a", "application/json") == "json"
    assert detect_page_type("", "https://x.com/a", "image/png") == "image"


def test_page_type_forum_qa_docs_markers():
    assert detect_page_type('<div class="forum-thread">x</div>', "https://x.com", "text/html", 100) == "forum"
    assert detect_page_type('<div class="question-body">x</div>', "https://x.com", "text/html", 100) == "qa"
    assert detect_page_type('<div class="md-nav">x</div>', "https://x.com", "text/html", 100) == "docs"
    assert detect_page_type('<div class="theme-doc-sidebar">x</div>', "https://x.com", "text/html", 100) == "docs"


def test_page_type_article_tag():
    html = "<article><p>Real article body with enough text to be meaningful here.</p></article>"
    assert detect_page_type(html, "https://x.com", "text/html", 200) == "article"


def test_page_type_list_page_many_links_short_text():
    # 25 same-domain links, little text -> list
    links = "".join(f'<a href="/page{i}">link{i}</a>' for i in range(25))
    html = f"<main>{links}<p>short</p></main>"
    assert detect_page_type(html, "https://example.com", "text/html", 50) == "list"


def test_page_type_article_with_many_links_not_list():
    # An <article> with many cross-refs should be 'article' (article tag wins
    # over the list heuristic because list requires NOT-article implicitly via
    # the text/link ratio being low; here text is long).
    links = "".join(f'<a href="/page{i}">link{i}</a>' for i in range(25))
    html = f"<article><p>{'long text ' * 500}</p>{links}</article>"
    pt = detect_page_type(html, "https://example.com", "text/html", 4000)
    assert pt == "article"


def test_page_type_paywall():
    html = "<main><p>Subscribe <strong>to continue</strong> reading this article.</p></main>"
    assert detect_page_type(html, "https://x.com", "text/html", 80) == "paywall"


def test_page_type_paywall_phrase_remains_a_paywall():
    html = "<main><p>This article is for subscribers.</p></main>"
    assert detect_page_type(html, "https://x.com", "text/html", 80) == "paywall"


def test_page_type_encoded_or_inline_split_paywall_phrase_is_detected():
    html_values = (
        "<article>subscr&#105;be to continue</article>",
        "<article>Sub<strong>scribe</strong> to continue</article>",
        "<article>Subscribe <em>to</em> <span>continue</span></article>",
        "<article>This article is for sub<strong>scribers</strong></article>",
        (
            "<template><noscript>hidden</template></noscript>"
            "<article>Subscribe to continue</article>"
        ),
    )
    for html in html_values:
        assert detect_page_type(html, "https://x.com", "text/html", 80) == "paywall"


def test_page_type_does_not_join_words_across_block_boundaries():
    html = "<article><p>Sub</p><p>scribe to continue.</p></article>"
    assert detect_page_type(html, "https://x.com", "text/html", 80) == "article"


def test_page_type_visible_paywall_detector_explanation_is_not_a_paywall():
    html = "<article><p>The paywall detector labels subscription prompts.</p></article>"
    assert detect_page_type(html, "https://x.com", "text/html", 80) == "article"


def test_page_type_readme_login_paywall_link_is_not_a_paywall():
    html = '<article><a href="/login/paywall">login/paywall</a><p>Project README.</p></article>'
    assert detect_page_type(html, "https://github.com/dondai1234/master-fetch", "text/html", 80) == "article"


def test_page_type_script_and_meta_paywall_mentions_are_not_a_paywall():
    html = (
        '<meta name="description" data-paywall="true" '
        'content="subscribe to continue; paywall detector docs">'
        '<link rel="alternate" data-content-gate="active">'
        '<base data-subscription-wall="true">'
        '<script>const marker = "<div data-paywall>";</script>'
        '<article><p>Documentation.</p></article>'
    )
    assert detect_page_type(html, "https://x.com", "text/html", 80) == "article"


def test_page_type_paywall_phrase_in_nonvisible_markup_is_not_a_paywall():
    html = (
        '<script>const prompt = "subscribe to continue";</script>'
        '<style>.prompt::after { content: "subscribe to read"; }</style>'
        '<noscript>Sign in to continue reading.</noscript>'
        '<template><div data-paywall>Subscriber-only content.</div></template>'
        '<!-- <div data-paywall="true">subscribe to continue</div> -->'
        '<article><p>Documentation.</p></article>'
    )
    assert detect_page_type(html, "https://x.com", "text/html", 80) == "article"


def test_page_type_disabled_or_prefixed_structural_markers_are_not_paywalls():
    attributes = (
        'data-paywall="false"',
        "data-content-gate='off'",
        "data-subscription-wall=0",
        'data-paywall-state="open"',
        'data-content-gate-config="enabled"',
        'x-data-paywall="true"',
        'aria-data-paywall="true"',
        'class="data-paywall enabled"',
        'title="data-content-gate=true"',
    )
    for attribute in attributes:
        html = f"<article><div {attribute}>Public content.</div></article>"
        assert detect_page_type(html, "https://x.com", "text/html", 80) == "article"


def test_page_type_structural_paywall_markers_are_paywalls():
    html_values = (
        '<main><div data-paywall="true">Subscription required.</div></main>',
        '<aside data-content-gate>Subscription required.</aside>',
        "<section data-content-gate='yes'>Subscription required.</section>",
        '<div data-paywall=enabled>Subscription required.</div>',
        '<main data-subscription-wall="active">Subscription required.</main>',
    )
    for html in html_values:
        assert detect_page_type(html, "https://x.com", "text/html", 80) == "paywall"


def test_page_type_redirect_meta_refresh():
    html = '<meta http-equiv="refresh" content="0;url=https://x.com/new"><body></body>'
    assert detect_page_type(html, "https://x.com", "text/html", 0) == "redirect"


def test_page_type_unknown_default():
    assert detect_page_type("", "https://x.com", "text/html", 0) == "unknown"
    assert detect_page_type("<div>just some content</div>", "https://x.com", "text/html", 20) == "unknown"


# ─── page_type_from_error ──────────────────────────────────────────

def test_page_type_from_error_mapping():
    assert page_type_from_error("js_shell_detected: ...") == "js_shell"
    assert page_type_from_error("auth_required: ...") == "auth_wall"
    assert page_type_from_error("geo_redirect_detected: ...") == "redirect"
    assert page_type_from_error("") == ""
    assert page_type_from_error("some_other_error") == ""


# ─── smart next_action branches (via _with_agent_hints) ─────────────

def _ok_result(**kw) -> ResponseModel:
    """A successful fetch result (content_ok=True, no error) for next_action tests."""
    base = dict(
        status=200, content=["real content here"], url="https://example.com/a",
        fetcher_used="http", content_type="text/html", content_ok=True, error="",
    )
    base.update(kw)
    return ResponseModel(**base)


def test_next_action_list_page_with_links():
    r = _ok_result(
        page_type="list",
        links={"citations": [
            {"url": "https://example.com/p1", "text": "p1"},
            {"url": "https://example.com/p2", "text": "p2"},
            {"url": "https://example.com/p3", "text": "p3"},
            {"url": "https://example.com/p4", "text": "p4"},
        ]},
    )
    out = _with_agent_hints(r)
    assert "list page" in out.next_action
    assert "https://example.com/p1" in out.next_action
    assert "https://example.com/p2" in out.next_action
    assert "https://example.com/p3" in out.next_action
    # Only top 3, not the 4th:
    assert "https://example.com/p4" not in out.next_action
    assert "smart_crawl" in out.next_action


def test_next_action_list_page_without_links():
    r = _ok_result(page_type="list")
    out = _with_agent_hints(r)
    assert "list page" in out.next_action
    assert "smart_crawl" in out.next_action


def test_next_action_auth_wall():
    r = _ok_result(page_type="auth_wall")
    out = _with_agent_hints(r)
    assert "login" in out.next_action or "authentication" in out.next_action
    assert "Internet Archive" in out.next_action


def test_next_action_paywall():
    r = _ok_result(page_type="paywall")
    out = _with_agent_hints(r)
    assert "paywall" in out.next_action


def test_next_action_redirect():
    r = _ok_result(page_type="redirect", metadata={"canonical": "https://example.com/real"})
    out = _with_agent_hints(r)
    assert "https://example.com/real" in out.next_action
    assert "redirect" in out.next_action


def test_next_action_stale_article():
    r = _ok_result(page_type="article", metadata={"published_time": "2020-01-01"})
    out = _with_agent_hints(r)
    assert out.is_stale is True
    assert "days old" in out.next_action
    assert "smart_search" in out.next_action


def test_next_action_fresh_article_no_hint():
    # A fresh, normal article with no special signal -> no envelope next_action.
    r = _ok_result(page_type="article", metadata={"published_time": "2026-07-10"})
    out = _with_agent_hints(r)
    assert out.is_stale is False
    assert out.next_action == ""


def test_envelope_fields_computed_on_result():
    r = _ok_result(url="https://docs.python.org/3/library/os",
                   metadata={"published_time": "2020-01-01"})
    out = _with_agent_hints(r)
    assert out.source_type == "docs-site"
    assert out.is_official is True
    assert out.content_age_days > 365
    assert out.is_stale is True

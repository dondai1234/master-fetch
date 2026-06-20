"""Tests for Reddit optimization (old.reddit.com URL rewriting + listing parser).

The parser fixture below mirrors REAL old.reddit.com HTML structure: each post
is a `<div class=" thing id-t3_...">` whose opening tag carries canonical
data-* attributes (data-score, data-comments-count, data-author, data-url,
data-domain, data-promoted, data-nsfw), plus THREE score spans per post
(dislikes/unvoted/likes). The previous span-scraping parser misaligned on this
exact shape; these tests lock in the data-attr-based parser.
"""

import os
import pytest
from master_fetch.reddit import (
    is_reddit_url,
    rewrite_to_old_reddit,
    parse_old_reddit_listing,
)


# Realistic old.reddit.com listing fragment. Three posts:
#   1. normal self post, score 42, 15 comments
#   2. stickied post, score 100, 1 comment (singular grammar), HTML entity in title
#   3. promoted ad (must be skipped) + an NSFW link post, score 7, 0 comments
# Each thing block has 3 score spans (dislikes/unvoted/likes) to prove the
# parser does NOT scrape spans (which would produce 9 score matches for 3 posts).
SAMPLE_HTML = """
<html><body>
<div id="siteTable">
<div class=" thing id-t3_abc odd link self" id="thing_t3_abc" data-fullname="t3_abc"
     data-author="testuser" data-subreddit="Python" data-domain="self.Python"
     data-url="/r/Python/comments/abc/post_one/" data-score="42"
     data-comments-count="15" data-promoted="false" data-nsfw="false"
     onclick="click_thing(this)">
  <div class="midcol unvoted">
    <div class="score dislikes" title="41">41</div>
    <div class="score unvoted" title="42">42</div>
    <div class="score likes" title="43">43</div>
  </div>
  <div class="entry unvoted"><div class="top-matter">
    <p class="title"><a class="title may-blank " href="/r/Python/comments/abc/post_one/">Post One Title</a>
      <span class="domain">(<a href="/r/Python/">self.Python</a>)</span></p>
    <p class="tagline">submitted by <a class="author" href="/user/testuser">testuser</a></p>
    <ul class="flat-list buttons"><li>
      <a href="/r/Python/comments/abc/post_one/">15 comments</a></li></ul>
  </div></div>
</div>
<div class=" thing id-t3_def even stickied link self" id="thing_t3_def"
     data-author="anotheruser" data-subreddit="Python" data-domain="self.Python"
     data-url="/r/Python/comments/def/post_two/" data-score="100"
     data-comments-count="1" data-promoted="false" data-nsfw="false">
  <div class="midcol unvoted">
    <div class="score dislikes" title="99">99</div>
    <div class="score unvoted" title="100">100</div>
    <div class="score likes" title="101">101</div>
  </div>
  <div class="entry unvoted"><div class="top-matter">
    <p class="title"><a class="title may-blank " href="/r/Python/comments/def/post_two/">Post &amp; Two &#39;Title&#39;</a>
      <span class="domain">(<a href="/r/Python/">self.Python</a>)</span></p>
    <p class="tagline">submitted by <a class="author" href="/user/anotheruser">anotheruser</a></p>
    <ul class="flat-list buttons"><li>
      <a href="/r/Python/comments/def/post_two/">1 comment</a></li></ul>
  </div></div>
</div>
<div class=" thing id-t3_promo odd promoted link" data-promoted="true"
     data-author="adbot" data-url="/r/Python/comments/promo/ad/" data-score="999"
     data-comments-count="999" data-nsfw="false">
  <p class="title"><a class="title may-blank " href="/r/Python/comments/promo/ad/">BUY NOW: Sponsored Ad</a></p>
</div>
<div class=" thing id-t3_xyz even link" data-author="nsfwuser" data-subreddit="nsfwsub"
     data-domain="imgur.com" data-url="https://imgur.com/x" data-score="7"
     data-comments-count="0" data-promoted="false" data-nsfw="true">
  <div class="midcol unvoted">
    <div class="score dislikes" title="6">6</div>
    <div class="score unvoted" title="7">7</div>
    <div class="score likes" title="8">8</div>
  </div>
  <div class="entry unvoted"><div class="top-matter">
    <p class="title"><a class="title may-blank " href="https://imgur.com/x">Spicy Link</a>
      <span class="domain">(<a href="https://imgur.com/">imgur.com</a>)</span></p>
    <p class="tagline">submitted by <a class="author" href="/user/nsfwuser">nsfwuser</a></p>
    <ul class="flat-list buttons"><li>
      <a href="/r/Python/comments/x/">0 comments</a></li></ul>
  </div></div>
</div>
</div></body></html>
"""

# Two posts survive (promo skipped): one, two (sticky), xyz (nsfw) = 3 real posts.
REAL_POST_COUNT = 3


class TestIsRedditUrl:
    """is_reddit_url detects all Reddit domain variants, rejects lookalikes."""

    @pytest.mark.parametrize("url", [
        "https://www.reddit.com/r/Python/",
        "https://reddit.com/r/Python/",
        "https://old.reddit.com/r/Python/",
        "https://m.reddit.com/r/Python/",
        "https://np.reddit.com/r/Python/",
        "http://www.reddit.com/r/Python/",
        "https://www.reddit.com:443/r/Python/",
    ])
    def test_detects_reddit(self, url):
        assert is_reddit_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://example.com",
        "https://notreddit.com",
        "https://reddit-clone.com",
        "https://not.reddit.com.evil.com",  # ends with .evil.com, not .reddit.com
        "",
        None,
    ])
    def test_rejects_non_reddit(self, url):
        assert is_reddit_url(url) is False


class TestRewriteToOldReddit:
    """rewrite_to_old_reddit converts listings to old, leaves posts/non-reddit alone."""

    @pytest.mark.parametrize("inp,out", [
        ("https://www.reddit.com/r/Python/", "https://old.reddit.com/r/Python/"),
        ("https://reddit.com/r/Python/", "https://old.reddit.com/r/Python/"),
        ("https://m.reddit.com/r/Python/", "https://old.reddit.com/r/Python/"),
        ("https://np.reddit.com/r/Python/", "https://old.reddit.com/r/Python/"),
    ])
    def test_listing_to_old(self, inp, out):
        assert rewrite_to_old_reddit(inp) == out

    def test_old_stays_old(self):
        assert rewrite_to_old_reddit("https://old.reddit.com/r/Python/") == \
            "https://old.reddit.com/r/Python/"

    def test_preserves_path_query_fragment(self):
        result = rewrite_to_old_reddit("https://www.reddit.com/r/Python/top/?t=week#x")
        assert result == "https://old.reddit.com/r/Python/top/?t=week#x"

    def test_post_url_unchanged(self):
        """Post pages must NOT be rewritten (old.reddit.com shows sidebar, not comments)."""
        url = "https://www.reddit.com/r/Python/comments/abc123/my_post/"
        assert rewrite_to_old_reddit(url) == url

    def test_old_post_url_unchanged(self):
        url = "https://old.reddit.com/r/Python/comments/abc123/my_post/"
        assert rewrite_to_old_reddit(url) == url

    @pytest.mark.parametrize("url", [
        "https://example.com/page",
        "https://notreddit.com/r/x",       # lookalike: must NOT rewrite
        "https://reddit-clone.com/r/x",
    ])
    def test_non_reddit_unchanged(self, url):
        assert rewrite_to_old_reddit(url) == url


class TestParseOldRedditListing:
    """parse_old_reddit_listing reads canonical data-* attrs per post block."""

    def test_returns_none_for_empty(self):
        assert parse_old_reddit_listing("") is None

    def test_returns_none_for_short(self):
        assert parse_old_reddit_listing("short") is None

    def test_returns_none_for_non_reddit_html(self):
        html = "<html><body><p>Not reddit at all, no thing blocks</p></body></html>"
        assert parse_old_reddit_listing(html) is None

    def test_returns_none_when_no_titles(self):
        """thing blocks without a title <a> (e.g. comment blocks) -> no posts -> None."""
        html = '<div class=" thing id-t3_x comment" data-author="u"><p>no title here</p></div>'
        assert parse_old_reddit_listing(html) is None

    def test_header(self):
        result = parse_old_reddit_listing(SAMPLE_HTML)
        assert result.startswith("# Reddit Posts")

    def test_extracts_titles(self):
        result = parse_old_reddit_listing(SAMPLE_HTML)
        assert "Post One Title" in result
        assert "Spicy Link" in result

    def test_unescapes_html_entities_in_title(self):
        result = parse_old_reddit_listing(SAMPLE_HTML)
        assert "Post & Two 'Title'" in result
        assert "&amp;" not in result
        assert "&#39;" not in result

    def test_extracts_canonical_score_not_span(self):
        """Score must come from data-score (42), NOT the dislikes span (41)."""
        result = parse_old_reddit_listing(SAMPLE_HTML)
        assert "Score: 42" in result
        assert "Score: 100" in result
        assert "Score: 7" in result
        # The dislikes span values must NOT leak through as a score.
        assert "Score: 41" not in result
        assert "Score: 99" not in result

    def test_extracts_comment_counts(self):
        result = parse_old_reddit_listing(SAMPLE_HTML)
        assert "15 comments" in result
        assert "0 comments" in result

    def test_singular_comment_grammar(self):
        result = parse_old_reddit_listing(SAMPLE_HTML)
        assert "1 comment" in result
        assert "1 comments" not in result

    def test_extracts_authors(self):
        result = parse_old_reddit_listing(SAMPLE_HTML)
        assert "u/testuser" in result
        assert "u/anotheruser" in result
        assert "u/nsfwuser" in result

    def test_extracts_domains(self):
        result = parse_old_reddit_listing(SAMPLE_HTML)
        assert "(self.Python)" in result
        assert "(imgur.com)" in result

    def test_absolutizes_relative_urls(self):
        result = parse_old_reddit_listing(SAMPLE_HTML)
        assert "https://old.reddit.com/r/Python/comments/abc/post_one/" in result

    def test_keeps_absolute_urls(self):
        result = parse_old_reddit_listing(SAMPLE_HTML)
        assert "https://imgur.com/x" in result

    def test_numbered_format(self):
        result = parse_old_reddit_listing(SAMPLE_HTML)
        assert "1. **" in result
        assert "2. **" in result
        assert "3. **" in result

    def test_skips_promoted_ads(self):
        result = parse_old_reddit_listing(SAMPLE_HTML)
        assert "Sponsored Ad" not in result
        assert "999" not in result  # promo score must not appear
        assert "adbot" not in result

    def test_tags_stickied(self):
        result = parse_old_reddit_listing(SAMPLE_HTML)
        # The sticky post (Post Two) carries the [sticky] tag.
        assert "[sticky]" in result

    def test_tags_nsfw(self):
        result = parse_old_reddit_listing(SAMPLE_HTML)
        assert "[NSFW]" in result

    def test_post_count(self):
        result = parse_old_reddit_listing(SAMPLE_HTML)
        # 3 real posts (promo skipped): numbered 1, 2, 3 and no 4.
        assert "3. **" in result
        assert "4. **" not in result

    def test_no_cross_block_misalignment(self):
        """Three score spans per post (9 total) must not shift scores across posts.
        The legacy parser produced post2=score-of-post1's-unvoted. Lock the fix."""
        result = parse_old_reddit_listing(SAMPLE_HTML)
        # Post 2 (sticky) real data-score is 100; legacy returned 42 (post1 unvoted).
        lines = [ln for ln in result.splitlines() if "Score:" in ln]
        assert lines[1].strip().startswith("Score: 100")

    def test_fallback_recovers_score_and_comments_without_data_attrs(self):
        """User-profile thing blocks lack data-score/data-comments-count.
        The per-block 'score unvoted' span + 'N comments' link recover them.
        Scoped to one block, so no cross-block alignment is possible."""
        html = (
            '<div class=" thing id-t3_u link" data-author="spez" '
            'data-subreddit="spez">'
            '<div class="midcol unvoted">'
            '<div class="score dislikes" title="699">699</div>'
            '<div class="score unvoted" title="701">701</div>'
            '<div class="score likes" title="703">703</div>'
            '</div>'
            '<p class="title"><a class="title" href="/r/spez/comments/x/p/">A Post</a></p>'
            '<ul class="flat-list buttons"><li>'
            '<a href="/r/spez/comments/x/p/">216 comments</a></li></ul>'
            '</div>'
        )
        result = parse_old_reddit_listing(f"<html><body>{html}</body></html>")
        assert result is not None
        # Canonical unvoted score (701), NOT dislikes (699) or likes (703).
        assert "Score: 701" in result
        assert "Score: 699" not in result
        assert "Score: 703" not in result
        assert "216 comments" in result

    def test_fallback_full_comments_link(self):
        """User-profile comment blocks show 'full comments (N)' instead of 'N comments'."""
        html = (
            '<div class=" thing id-t3_c comment" data-author="spez">'
            '<p class="title"><a class="title" href="/r/x/comments/c/p/">A Comment</a></p>'
            '<ul class="flat-list buttons"><li>'
            '<a href="/r/x/comments/c/p/">full comments (42)</a></li></ul>'
            '</div>'
        )
        result = parse_old_reddit_listing(f"<html><body>{html}</body></html>")
        assert result is not None
        assert "42 comments" in result

    def test_score_hidden_shows_honest_unknown(self):
        """Stickied/comment blocks with score-hidden must show '?', not a wrong number."""
        html = (
            '<div class=" thing id-t3_s stickied comment" data-author="AutoModerator">'
            '<span class="score-hidden" title="scores for stickied comments are visible to mods">hidden</span>'
            '<p class="title"><a class="title" href="/r/x/comments/s/p/">Stickied Comment</a></p>'
            '<ul class="flat-list buttons"><li><a href="/r/x/comments/s/p/">permalink</a></li></ul>'
            '</div>'
        )
        result = parse_old_reddit_listing(f"<html><body>{html}</body></html>")
        assert result is not None
        assert "Score: ?" in result

    def test_limits_to_25_posts(self):
        things = ""
        for i in range(30):
            things += (
                f'<div class=" thing id-t3_{i} link" data-author="user{i}" '
                f'data-url="/r/test/comments/{i}/post/" data-score="{i}" '
                f'data-comments-count="{i}" data-promoted="false" data-nsfw="false">'
                f'<p class="title"><a class="title" href="/r/test/comments/{i}/post/">Post {i}</a></p></div>'
            )
        html = f"<html><body><div id=\"siteTable\">{things}</div></body></html>"
        result = parse_old_reddit_listing(html)
        assert "25. **" in result
        assert "26. **" not in result


# Real-HTML regression test. The fixture lives at tests/old_reddit_real.html
# (fetched live from old.reddit.com/r/Python/). Skipped if absent so CI without
# the fixture still passes; present locally it locks the parser to reality.
_REAL_FIXTURE = os.path.join(os.path.dirname(__file__), "old_reddit_real.html")


@pytest.mark.skipif(not os.path.exists(_REAL_FIXTURE),
                    reason="real old.reddit.com fixture not present")
class TestRealOldRedditHtml:
    """Parser must produce correct canonical values from live old.reddit.com HTML.

    Expected values are read FROM the fixture itself (self-consistent), so the
    test never flakes as reddit's front page content changes — it only locks in
    that the parser reads the canonical data-* attrs (not the dislikes span).
    """

    @pytest.fixture(scope="class")
    def real_html(self):
        return open(_REAL_FIXTURE, encoding="utf-8", errors="replace").read()

    @pytest.fixture(scope="class")
    def parsed(self, real_html):
        return parse_old_reddit_listing(real_html)

    def test_returns_structured_output(self, parsed):
        assert parsed is not None
        assert parsed.startswith("# Reddit Posts")

    def test_extracts_25_posts(self, parsed):
        assert "25. **" in parsed
        assert "26. **" not in parsed

    def test_first_post_uses_canonical_data_score(self, parsed, real_html):
        """First post's score in output == its data-score attr (NOT the dislikes span).

        The legacy span-scraping parser returned data-score-1 (the dislikes span).
        This reads the real data-score from the fixture and demands an exact match.
        """
        import re
        m = re.search(r'data-score="(\d+)"', real_html)
        assert m, "fixture has no data-score attr"
        assert f"Score: {m.group(1)}" in parsed

    def test_first_post_uses_canonical_comment_count(self, parsed, real_html):
        import re
        m = re.search(r'data-comments-count="(\d+)"', real_html)
        assert m
        cc = m.group(1)
        expected = f"{cc} comment" + ("" if cc == "1" else "s")
        assert expected in parsed

    def test_first_post_author_from_data_attr(self, parsed, real_html):
        import re
        m = re.search(r'data-author="([^"]+)"', real_html)
        assert m
        assert f"u/{m.group(1)}" in parsed

    def test_no_score_unknown_on_subreddit_listing(self, parsed):
        """A subreddit listing's posts all have data-score, so none should be '?'."""
        assert "Score: ?" not in parsed

    def test_first_post_url_absolutized(self, parsed, real_html):
        import re
        m = re.search(r'data-url="([^"]+)"', real_html)
        assert m
        url = m.group(1)
        if url.startswith("/"):
            url = f"https://old.reddit.com{url}"
        assert url in parsed

"""Tests for Reddit optimization (old.reddit.com URL rewriting + parser)."""

import pytest
from master_fetch.reddit import (
    is_reddit_url,
    rewrite_to_old_reddit,
    parse_old_reddit_listing,
)


class TestIsRedditUrl:
    """is_reddit_url detects all Reddit domain variants."""

    @pytest.mark.parametrize("url", [
        "https://www.reddit.com/r/Python/",
        "https://reddit.com/r/Python/",
        "https://old.reddit.com/r/Python/",
        "https://m.reddit.com/r/Python/",
        "https://np.reddit.com/r/Python/",
        "http://www.reddit.com/r/Python/",
    ])
    def test_detects_reddit(self, url):
        assert is_reddit_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://example.com",
        "https://notreddit.com",
        "https://reddit-clone.com",
        "",
    ])
    def test_rejects_non_reddit(self, url):
        assert is_reddit_url(url) is False


class TestRewriteToOldReddit:
    """rewrite_to_old_reddit converts www/m/np to old.reddit.com."""

    def test_www_to_old(self):
        assert rewrite_to_old_reddit("https://www.reddit.com/r/Python/") == \
            "https://old.reddit.com/r/Python/"

    def test_bare_to_old(self):
        assert rewrite_to_old_reddit("https://reddit.com/r/Python/") == \
            "https://old.reddit.com/r/Python/"

    def test_mobile_to_old(self):
        assert rewrite_to_old_reddit("https://m.reddit.com/r/Python/") == \
            "https://old.reddit.com/r/Python/"

    def test_old_stays_old(self):
        assert rewrite_to_old_reddit("https://old.reddit.com/r/Python/") == \
            "https://old.reddit.com/r/Python/"

    def test_preserves_path(self):
        result = rewrite_to_old_reddit("https://www.reddit.com/r/Python/top/?t=week")
        assert result == "https://old.reddit.com/r/Python/top/?t=week"

    def test_post_url_unchanged(self):
        """Post pages should NOT be rewritten (old.reddit.com shows sidebar instead of comments)."""
        url = "https://www.reddit.com/r/Python/comments/abc123/my_post/"
        assert rewrite_to_old_reddit(url) == url  # unchanged

    def test_non_reddit_unchanged(self):
        assert rewrite_to_old_reddit("https://example.com/page") == \
            "https://example.com/page"


class TestParseOldRedditListing:
    """parse_old_reddit_listing extracts structured data from old.reddit.com HTML."""

    SAMPLE_HTML = """
    <html>
    <body>
    <div class="thing">
        <p class="title">
            <a class="title" href="/r/Python/comments/abc/post_one/">Post One Title</a>
        </p>
        <span class="score" title="42 points">42</span>
        <a class="author" href="/user/testuser">testuser</a>
        <a href="/r/Python/comments/abc/post_one/">15 comments</a>
    </div>
    <div class="thing">
        <p class="title">
            <a class="title" href="/r/Python/comments/def/post_two/">Post Two Title</a>
        </p>
        <span class="score" title="100 points">100</span>
        <a class="author" href="/user/anotheruser">anotheruser</a>
        <a href="/r/Python/comments/def/post_two/">42 comments</a>
    </div>
    </body>
    </html>
    """

    def test_extracts_titles(self):
        result = parse_old_reddit_listing(self.SAMPLE_HTML)
        assert "Post One Title" in result
        assert "Post Two Title" in result

    def test_extracts_scores(self):
        result = parse_old_reddit_listing(self.SAMPLE_HTML)
        assert "42" in result
        assert "100" in result

    def test_extracts_comment_counts(self):
        result = parse_old_reddit_listing(self.SAMPLE_HTML)
        assert "15 comments" in result
        assert "42 comments" in result

    def test_extracts_authors(self):
        result = parse_old_reddit_listing(self.SAMPLE_HTML)
        assert "u/testuser" in result
        assert "u/anotheruser" in result

    def test_extracts_urls(self):
        result = parse_old_reddit_listing(self.SAMPLE_HTML)
        assert "old.reddit.com/r/Python/comments/abc/post_one/" in result
        assert "old.reddit.com/r/Python/comments/def/post_two/" in result

    def test_numbered_format(self):
        result = parse_old_reddit_listing(self.SAMPLE_HTML)
        assert "1. **" in result
        assert "2. **" in result

    def test_header(self):
        result = parse_old_reddit_listing(self.SAMPLE_HTML)
        assert "# Reddit Posts" in result

    def test_empty_html_returns_as_is(self):
        assert parse_old_reddit_listing("") == ""
        assert parse_old_reddit_listing("short") == "short"

    def test_non_reddit_html_returns_as_is(self):
        html = "<html><body><p>Not reddit</p></body></html>"
        result = parse_old_reddit_listing(html)
        assert result == html

    def test_limits_to_25_posts(self):
        # Create HTML with 30 posts
        things = ""
        for i in range(30):
            things += f"""
            <div class="thing">
                <a class="title" href="/r/test/comments/{i}/post/">Post {i}</a>
                <span class="score" title="{i} points">{i}</span>
                <a class="author" href="/user/user{i}">user{i}</a>
                <a href="/r/test/comments/{i}/post/">{i} comments</a>
            </div>
            """
        html = f"<html><body>{things}</body></html>"
        result = parse_old_reddit_listing(html)
        # Should have at most 25 numbered posts
        assert "25. **" in result
        assert "26. **" not in result

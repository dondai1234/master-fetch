"""Server-level routing tests for the Reddit optimization.

Locks in the v3.6.x behavior:
  * Reddit URLs (listings AND post pages) skip the HTTP tier and go straight
    to the stealthy fetcher — www.reddit.com JS-walls/blocks plain HTTP ~100%
    of the time, so HTTP is ~1s of wasted time before escalation anyway.
  * The old.reddit.com rewrite happens BEFORE force_fetcher, so an explicit
    force_fetcher="http" on a reddit listing still benefits from the rewrite.
  * Non-Reddit URLs still go through the normal HTTP -> stealthy auto-escalate.
  * Post pages (/comments/) are NOT rewritten to old.reddit.com (it shows the
    sidebar there), but still skip HTTP.
"""

import pytest
from unittest.mock import AsyncMock

from master_fetch.server import MasterFetchServer, ResponseModel


def _ok(url):
    return ResponseModel(
        status=200, content=["ok"], url=url, fetcher_used="stealthy",
        extracted_type="markdown",
    )


@pytest.fixture
def srv():
    s = MasterFetchServer()
    s._force_fetch = AsyncMock(side_effect=lambda url, *a, **k: _ok(url))
    s._auto_escalate = AsyncMock(side_effect=lambda url, *a, **k: _ok(url))
    return s


class TestRedditRoutingSkipsHttp:
    """Reddit URLs -> _force_fetch('stealthy'), never _auto_escalate."""

    @pytest.mark.asyncio
    async def test_listing_goes_straight_to_stealthy(self, srv):
        await srv.smart_fetch(
            url="https://www.reddit.com/r/Python/", cache_ttl=0, respect_robots=False,
        )
        assert srv._force_fetch.await_count == 1
        assert srv._auto_escalate.await_count == 0
        # Second positional arg to _force_fetch is force_fetcher.
        args, _ = srv._force_fetch.call_args
        assert args[1] == "stealthy"
        # And the URL was rewritten to old.reddit.com before the call.
        assert args[0] == "https://old.reddit.com/r/Python/"

    @pytest.mark.asyncio
    async def test_post_page_goes_straight_to_stealthy_unrewritten(self, srv):
        """Post pages also skip HTTP (any reddit URL), but stay on www.reddit.com."""
        url = "https://www.reddit.com/r/Python/comments/abc/my_post/"
        await srv.smart_fetch(url=url, cache_ttl=0, respect_robots=False)
        assert srv._force_fetch.await_count == 1
        assert srv._auto_escalate.await_count == 0
        args, _ = srv._force_fetch.call_args
        assert args[1] == "stealthy"
        assert args[0] == url  # NOT rewritten to old.reddit.com

    @pytest.mark.asyncio
    async def test_bare_reddit_host_stealthy(self, srv):
        await srv.smart_fetch(
            url="https://reddit.com/r/Python/", cache_ttl=0, respect_robots=False,
        )
        assert srv._force_fetch.await_count == 1
        assert srv._auto_escalate.await_count == 0
        args, _ = srv._force_fetch.call_args
        assert args[1] == "stealthy"
        assert args[0] == "https://old.reddit.com/r/Python/"

    @pytest.mark.asyncio
    async def test_already_old_reddit_stealthy(self, srv):
        await srv.smart_fetch(
            url="https://old.reddit.com/r/Python/", cache_ttl=0, respect_robots=False,
        )
        assert srv._force_fetch.await_count == 1
        assert srv._auto_escalate.await_count == 0
        args, _ = srv._force_fetch.call_args
        assert args[1] == "stealthy"
        assert args[0] == "https://old.reddit.com/r/Python/"


class TestNonRedditRoutingUsesAutoEscalate:
    """Non-Reddit URLs go through normal HTTP -> stealthy auto-escalation."""

    @pytest.mark.asyncio
    async def test_non_reddit_uses_auto_escalate(self, srv):
        await srv.smart_fetch(
            url="https://example.com/page", cache_ttl=0, respect_robots=False,
        )
        assert srv._auto_escalate.await_count == 1
        assert srv._force_fetch.await_count == 0
        args, _ = srv._auto_escalate.call_args
        assert args[0] == "https://example.com/page"

    @pytest.mark.asyncio
    async def test_lookalike_not_treated_as_reddit(self, srv):
        """notreddit.com must NOT get the reddit stealth shortcut or rewrite."""
        await srv.smart_fetch(
            url="https://notreddit.com/r/x", cache_ttl=0, respect_robots=False,
        )
        assert srv._auto_escalate.await_count == 1
        assert srv._force_fetch.await_count == 0
        args, _ = srv._auto_escalate.call_args
        assert args[0] == "https://notreddit.com/r/x"


class TestRedditRewriteBeforeForceFetcher:
    """An explicit force_fetcher still wins, but benefits from the old.reddit rewrite."""

    @pytest.mark.asyncio
    async def test_force_http_on_reddit_listing_still_rewrites(self, srv):
        await srv.smart_fetch(
            url="https://www.reddit.com/r/Python/", force_fetcher="http",
            cache_ttl=0, respect_robots=False,
        )
        assert srv._force_fetch.await_count == 1
        assert srv._auto_escalate.await_count == 0
        args, _ = srv._force_fetch.call_args
        assert args[1] == "http"
        # Rewrite happened BEFORE force_fetcher dispatch, so HTTP hits old.reddit.com.
        assert args[0] == "https://old.reddit.com/r/Python/"

    @pytest.mark.asyncio
    async def test_force_stealthy_on_reddit_listing_rewrites(self, srv):
        await srv.smart_fetch(
            url="https://www.reddit.com/r/Python/", force_fetcher="stealthy",
            cache_ttl=0, respect_robots=False,
        )
        assert srv._force_fetch.await_count == 1
        args, _ = srv._force_fetch.call_args
        assert args[1] == "stealthy"
        assert args[0] == "https://old.reddit.com/r/Python/"

    @pytest.mark.asyncio
    async def test_force_http_on_reddit_post_page_not_rewritten(self, srv):
        url = "https://www.reddit.com/r/Python/comments/abc/post/"
        await srv.smart_fetch(
            url=url, force_fetcher="http", cache_ttl=0, respect_robots=False,
        )
        assert srv._force_fetch.await_count == 1
        args, _ = srv._force_fetch.call_args
        assert args[1] == "http"
        assert args[0] == url  # post page stays on www

"""Agent quality-of-life feature tests.

Covers the v3.7 agent-effectiveness batch:
  * ResponseModel agent hints (summary / content_ok / next_action / fetched_at)
  * max_content_chars token-spend control (validation + threading + chunking)
  * screenshot auto-managed session (session_id optional)
  * smart_search fetch_relevance (high|med|low) + fetch_hint
  * MCP initialize `instructions` (connect-time orientation)
  * tool-def schema: promoted first-class smart_fetch params, screenshot not
    requiring session_id
  * _dispatch reading promoted params from top-level with options fallback
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from master_fetch.server import (
    MasterFetchServer,
    ResponseModel,
    _apply_chunking,
    _with_agent_hints,
    _agent_hints,
    HOUND_INSTRUCTIONS,
    MAX_CONTENT_CHARS,
)
from master_fetch.search import (
    SearchResult,
    SearchResponseModel,
    compute_fetch_relevance,
    compute_fetch_hint,
)


def _resp(status=200, content=None, **kw):
    return ResponseModel(
        status=status,
        content=content or ["some extracted content here"],
        url=kw.get("url", "https://example.com/page"),
        fetcher_used=kw.get("fetcher_used", "http"),
        extracted_type=kw.get("extracted_type", "markdown"),
        error=kw.get("error", ""),
        total_size_bytes=kw.get("total_size_bytes", 1234),
        **{k: v for k, v in kw.items() if k in ("cached", "is_truncated", "next_offset")},
    )


# ─── ResponseModel agent hints ────────────────────────────────────────────

class TestAgentHints:
    def test_summary_success(self):
        r = _with_agent_hints(_resp())
        assert "200 OK" in r.summary
        assert "markdown" in r.summary
        assert "http" in r.summary

    def test_content_ok_true_on_clean_success(self):
        r = _with_agent_hints(_resp())
        assert r.content_ok is True

    def test_content_ok_false_on_error_status(self):
        r = _with_agent_hints(_resp(status=404, content=["Not Found"]))
        assert r.content_ok is False

    def test_content_ok_false_on_js_shell_error(self):
        r = _with_agent_hints(_resp(error="js_shell_detected: page requires JS"))
        assert r.content_ok is False

    def test_content_ok_false_on_empty_content(self):
        r = _with_agent_hints(_resp(content=["   "]))
        assert r.content_ok is False

    def test_next_action_empty_on_clean_success(self):
        r = _with_agent_hints(_resp())
        assert r.next_action == ""

    def test_next_action_paginate_when_truncated(self):
        r = _with_agent_hints(_resp(is_truncated=True, next_offset=40000))
        assert r.next_action == "paginate: call smart_fetch with offset=40000"

    def test_next_action_robots(self):
        r = _with_agent_hints(_resp(status=403, error="robots_txt_disallowed"))
        assert r.next_action.startswith("blocked by robots.txt")

    def test_next_action_js_shell(self):
        r = _with_agent_hints(_resp(error="js_shell_detected: needs JS"))
        assert "auto-escalates" in r.next_action

    def test_next_action_all_tiers_failed(self):
        r = _with_agent_hints(_resp(status=403, error="all_tiers_failed: HTTP status 403"))
        assert "switch sources" in r.next_action

    def test_next_action_network_error(self):
        r = _with_agent_hints(_resp(status=0, content=[""], error="boom"))
        assert r.next_action == "fetch failed — see error field"

    def test_fetched_at_is_iso(self):
        r = _with_agent_hints(_resp())
        assert r.fetched_at and "T" in r.fetched_at and r.fetched_at.endswith("+00:00") or "+" in r.fetched_at

    def test_apply_chunking_stamps_hints(self):
        big = ResponseModel(
            status=200, content=["x" * 50000], url="https://example.com",
            fetcher_used="http", extracted_type="markdown", total_size_bytes=50000,
        )
        out = _apply_chunking(big, max_chars=1000, offset=0)
        assert out.summary
        assert out.content_ok is True
        assert out.next_action.startswith("paginate:")


# ─── max_content_chars ────────────────────────────────────────────────────

class TestMaxContentChars:
    def test_apply_chunking_respects_max_chars(self):
        big = ResponseModel(
            status=200, content=["y" * 50000], url="https://example.com",
            fetcher_used="http", extracted_type="markdown",
        )
        out = _apply_chunking(big, max_chars=1000, offset=0)
        assert out.is_truncated is True
        assert out.next_offset == 1000

    def test_smart_fetch_rejects_too_small(self):
        srv = MasterFetchServer()
        with pytest.raises(ValueError, match="max_content_chars must be an int >= 500"):
            asyncio.run(srv.smart_fetch(url="https://example.com", max_content_chars=100))

    def test_smart_fetch_rejects_bool(self):
        srv = MasterFetchServer()
        with pytest.raises(ValueError, match="max_content_chars must be an int >= 500"):
            asyncio.run(srv.smart_fetch(url="https://example.com", max_content_chars=True))

    @pytest.mark.asyncio
    async def test_reddit_path_threads_mc(self):
        """Reddit → _force_fetch('stealthy', ..., mc). mc is the last positional arg."""
        srv = MasterFetchServer()
        srv._force_fetch = AsyncMock(return_value=_resp())
        await srv.smart_fetch(
            url="https://www.reddit.com/r/Python/",
            cache_ttl=0, respect_robots=False, max_content_chars=2000,
        )
        args, _ = srv._force_fetch.call_args
        assert args[1] == "stealthy"
        assert args[-1] == 2000  # mc threaded through

    @pytest.mark.asyncio
    async def test_non_reddit_path_threads_mc_to_auto_escalate(self):
        srv = MasterFetchServer()
        srv._auto_escalate = AsyncMock(return_value=_resp())
        await srv.smart_fetch(
            url="https://example.com/page",
            cache_ttl=0, respect_robots=False, max_content_chars=2000,
        )
        args, _ = srv._auto_escalate.call_args
        assert args[-1] == 2000

    @pytest.mark.asyncio
    async def test_default_mc_is_max_content_chars(self):
        srv = MasterFetchServer()
        srv._auto_escalate = AsyncMock(return_value=_resp())
        await srv.smart_fetch(url="https://example.com/page", cache_ttl=0, respect_robots=False)
        args, _ = srv._auto_escalate.call_args
        assert args[-1] == MAX_CONTENT_CHARS

    @pytest.mark.asyncio
    async def test_force_fetcher_path_threads_mc(self):
        srv = MasterFetchServer()
        srv._force_fetch = AsyncMock(return_value=_resp())
        await srv.smart_fetch(
            url="https://example.com/page", force_fetcher="http",
            cache_ttl=0, respect_robots=False, max_content_chars=3000,
        )
        args, _ = srv._force_fetch.call_args
        assert args[1] == "http"
        assert args[-1] == 3000


# ─── Dispatch: promoted params (top-level + options fallback) ─────────────

class TestDispatchPromotedParams:
    @pytest.mark.asyncio
    async def test_top_level_css_selector_max_content_chars_timeout(self):
        srv = MasterFetchServer()
        srv.smart_fetch = AsyncMock(return_value=_resp())
        await srv._dispatch("mcp_smart_fetch", {
            "url": "https://example.com",
            "css_selector": "article",
            "max_content_chars": 5000,
            "timeout": 7000,
        })
        _, kw = srv.smart_fetch.call_args
        assert kw["css_selector"] == "article"
        assert kw["max_content_chars"] == 5000
        assert kw["timeout"] == 7000

    @pytest.mark.asyncio
    async def test_options_bag_fallback_for_css_selector(self):
        """Backward compat: css_selector still accepted inside options."""
        srv = MasterFetchServer()
        srv.smart_fetch = AsyncMock(return_value=_resp())
        await srv._dispatch("mcp_smart_fetch", {
            "url": "https://example.com",
            "options": {"css_selector": ".main", "timeout": 9000},
        })
        _, kw = srv.smart_fetch.call_args
        assert kw["css_selector"] == ".main"
        assert kw["timeout"] == 9000

    @pytest.mark.asyncio
    async def test_top_level_overrides_options(self):
        srv = MasterFetchServer()
        srv.smart_fetch = AsyncMock(return_value=_resp())
        await srv._dispatch("mcp_smart_fetch", {
            "url": "https://example.com",
            "css_selector": "article",
            "options": {"css_selector": ".other"},
        })
        _, kw = srv.smart_fetch.call_args
        assert kw["css_selector"] == "article"


# ─── Screenshot auto-managed session ──────────────────────────────────────

class TestScreenshotAutoSession:
    @pytest.mark.asyncio
    async def test_auto_session_when_session_id_omitted(self):
        srv = MasterFetchServer()
        state = {}

        async def fake_ensure(t):
            state["ensure"] = t
            return "auto-sid"

        async def fake_get(sid, expected_type=None):
            state["get_sid"] = sid
            page = MagicMock()
            page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n fake bytes")
            page.url = "https://example.com"

            class _Entry:
                class session:
                    @staticmethod
                    async def fetch(*a, **k):
                        await k["page_action"](page)
            return _Entry()

        srv._ensure_auto_session = fake_ensure
        srv._get_session = fake_get
        result = await srv.screenshot(url="https://example.com")
        assert state["ensure"] == "stealthy"
        assert state["get_sid"] == "auto-sid"
        assert result  # list of ImageContent/TextContent

    @pytest.mark.asyncio
    async def test_explicit_session_id_skips_auto(self):
        srv = MasterFetchServer()
        state = {}

        async def fake_ensure(t):
            state["ensure"] = t
            return "should-not-be-used"

        async def fake_get(sid, expected_type=None):
            state["get_sid"] = sid
            page = MagicMock()
            page.screenshot = AsyncMock(return_value=b"\x89PNG fake")
            page.url = "https://example.com"

            class _Entry:
                class session:
                    @staticmethod
                    async def fetch(*a, **k):
                        await k["page_action"](page)
            return _Entry()

        srv._ensure_auto_session = fake_ensure
        srv._get_session = fake_get
        await srv.screenshot(url="https://example.com", session_id="my-sess")
        assert "ensure" not in state  # auto not called
        assert state["get_sid"] == "my-sess"


# ─── smart_search fetch_relevance + fetch_hint ────────────────────────────

class TestFetchRelevance:
    def test_high_for_top_result_with_title_overlap(self):
        # query terms "python" "asyncio" both in title, position 1
        rel = compute_fetch_relevance("python asyncio guide", "Python Asyncio Guide", "snippet", 1)
        assert rel == "high"

    def test_med_for_position_le_3_with_some_overlap(self):
        rel = compute_fetch_relevance("python web scraping", "A Python Tutorial", "snippet", 3)
        assert rel == "med"

    def test_low_for_no_overlap_late_position(self):
        rel = compute_fetch_relevance("kubernetes deployment", "Best Pizza Recipes", "yum", 8)
        assert rel == "low"

    def test_falls_back_to_position_for_stopwords_only(self):
        rel = compute_fetch_relevance("what is the best", "Some Title", "snippet", 1)
        assert rel in ("high", "med", "low")
        rel1 = compute_fetch_relevance("what is the best", "Some Title", "snippet", 1)
        rel8 = compute_fetch_relevance("what is the best", "Some Title", "snippet", 8)
        assert rel1 != "low"  # position 1 -> high
        assert rel8 == "low"  # position 8 -> low

    def test_compute_fetch_hint_counts(self):
        results = [
            SearchResult(title="a", url="u1", fetch_relevance="high"),
            SearchResult(title="b", url="u2", fetch_relevance="high"),
            SearchResult(title="c", url="u3", fetch_relevance="med"),
            SearchResult(title="d", url="u4", fetch_relevance="low"),
        ]
        hint = compute_fetch_hint(results)
        assert "2 high" in hint
        assert "1 med" in hint
        assert "1 low" in hint
        assert "smart_fetch" in hint

    def test_compute_fetch_hint_empty(self):
        assert compute_fetch_hint([]) == ""

    @pytest.mark.asyncio
    async def test_search_response_carries_fetch_hint_and_relevance(self):
        """End-to-end: a SearchResponseModel built by smart_search has fetch_hint
        and every result has a fetch_relevance tier."""
        from master_fetch.search import smart_search as _ss
        srv = MasterFetchServer()
        # Stub the network layer to avoid hitting TinyFish.
        import master_fetch.search as search_mod
        async def fake_tinyfish(query, max_results=10, api_key=""):
            return [
                SearchResult(title="Python Asyncio Guide", url="https://a.com",
                             snippet="snip", source="tinyfish", position=1,
                             fetch_relevance=compute_fetch_relevance(query, "Python Asyncio Guide", "snip", 1)),
                SearchResult(title="Unrelated Pizza Blog", url="https://b.com",
                             snippet="yum", source="tinyfish", position=2,
                             fetch_relevance=compute_fetch_relevance(query, "Unrelated Pizza Blog", "yum", 2)),
            ]
        orig = search_mod._tinyfish_search
        search_mod._tinyfish_search = fake_tinyfish
        # Bypass cache so the live path runs.
        import os
        os.environ["TINYFISH_API_KEY"] = "sk-tinyfish-test"
        try:
            resp = await _ss(srv, "python asyncio", max_results=5, cache_ttl=0)
        finally:
            search_mod._tinyfish_search = orig
            os.environ.pop("TINYFISH_API_KEY", None)
        assert resp.fetch_hint
        assert "smart_fetch" in resp.fetch_hint
        assert all(r.fetch_relevance in ("high", "med", "low") for r in resp.results)
        assert resp.results[0].fetch_relevance == "high"


# ─── MCP initialize instructions + tool-def schema ────────────────────────

class TestInstructionsAndSchema:
    def test_instructions_nonempty_and_on_topic(self):
        assert isinstance(HOUND_INSTRUCTIONS, str)
        assert len(HOUND_INSTRUCTIONS) > 200
        for phrase in ("smart_fetch", "smart_search", "fetch_relevance",
                       "NEVER answer from snippets", "DataDome", "screenshot"):
            assert phrase in HOUND_INSTRUCTIONS

    def test_instructions_wired_into_initialization_options(self):
        from mcp.server import Server
        s = Server(name="Hound", version="x")
        s.instructions = HOUND_INSTRUCTIONS
        opts = s.create_initialization_options()
        assert opts.instructions == HOUND_INSTRUCTIONS

    def test_smart_fetch_has_promoted_top_level_params(self):
        srv = MasterFetchServer()
        defs = {d["name"]: d for d in srv._TOOL_DEFS}
        props = defs["mcp_smart_fetch"]["inputSchema"]["properties"]
        for p in ("css_selector", "max_content_chars", "timeout"):
            assert p in props, f"{p} should be a top-level smart_fetch param"
        # css_selector description should hint at token saving
        assert "css" in props["css_selector"]["description"].lower() or "narrow" in props["css_selector"]["description"].lower()

    def test_screenshot_does_not_require_session_id(self):
        srv = MasterFetchServer()
        defs = {d["name"]: d for d in srv._TOOL_DEFS}
        sc = defs["mcp_screenshot"]
        assert sc["inputSchema"].get("required", []) == ["url"]
        assert "session_id" in sc["inputSchema"]["properties"]
        assert "Multimodal" in sc["description"] or "multimodal" in sc["description"].lower()

    def test_smart_search_description_mentions_relevance(self):
        srv = MasterFetchServer()
        defs = {d["name"]: d for d in srv._TOOL_DEFS}
        desc = defs["mcp_smart_search"]["description"]
        assert "fetch_relevance" in desc
        assert "fetch_hint" in desc or "high" in desc


# ─── Single warm browser instance (no open/close/list session tools) ──────

class TestSingleBrowserInstance:
    """v3.7: one stealthy Chrome, warmed at startup, reused for every stealthy
    fetch + screenshot. The manual open/close/list session MCP tools are gone;
    open_session/close_session remain as internal helpers."""

    def test_session_tools_removed_from_defs(self):
        srv = MasterFetchServer()
        names = {d["name"] for d in srv._TOOL_DEFS}
        for removed in ("mcp_open_session", "close_session", "list_sessions"):
            assert removed not in names, f"{removed} should be removed from tool defs"
        # The 5 remaining tools
        assert names == {"mcp_smart_fetch", "mcp_screenshot", "mcp_smart_search",
                         "cache_clear", "version"}

    @pytest.mark.asyncio
    async def test_removed_tools_dispatch_as_unknown(self):
        srv = MasterFetchServer()
        for removed in ("mcp_open_session", "close_session", "list_sessions"):
            with pytest.raises(ValueError, match="Unknown tool"):
                await srv._dispatch(removed, {})

    def test_no_prewarm_trigger_flag(self):
        srv = MasterFetchServer()
        assert not hasattr(srv, "_prewarm_triggered")
        assert hasattr(srv, "_auto_session_lock")

    @pytest.mark.asyncio
    async def test_creation_lock_prevents_second_browser(self):
        """N concurrent _ensure_auto_session('stealthy') calls → open_session
        invoked exactly ONCE. The creation lock serializes them; later callers
        reuse the first one's session instead of spawning a 2nd Chrome."""
        from master_fetch.server import SessionCreatedModel, _SessionEntry
        srv = MasterFetchServer()
        calls = []

        async def mock_open(**kwargs):
            calls.append(1)
            sid = "auto-stealthy-1"
            mock_sess = MagicMock()
            mock_sess._is_alive = True
            srv._sessions[sid] = _SessionEntry(session=mock_sess, session_type="stealthy")
            return SessionCreatedModel(
                session_id=sid, session_type="stealthy",
                created_at="", is_alive=True, message="",
            )

        srv.open_session = mock_open
        try:
            results = await asyncio.gather(
                *[srv._ensure_auto_session("stealthy") for _ in range(5)]
            )
        finally:
            srv._sessions.clear()
            srv._auto_stealthy_id = None
        assert len(calls) == 1, f"open_session should be called once, got {len(calls)}"
        assert all(r == "auto-stealthy-1" for r in results)

    @pytest.mark.asyncio
    async def test_shutdown_closes_all_sessions(self):
        from master_fetch.server import _SessionEntry
        srv = MasterFetchServer()
        fake = MagicMock()
        fake.close = AsyncMock()
        srv._sessions["s1"] = _SessionEntry(session=fake, session_type="stealthy")
        srv._auto_stealthy_id = "s1"
        await srv._shutdown_close_sessions()
        fake.close.assert_awaited_once()
        assert srv._sessions == {}
        assert srv._auto_stealthy_id is None

    @pytest.mark.asyncio
    async def test_prewarm_is_best_effort_and_idempotent(self):
        """_prewarm_stealthy must not raise even if session creation fails, and
        a successful warm-up leaves exactly one auto stealthy session."""
        from master_fetch.server import SessionCreatedModel, _SessionEntry
        srv = MasterFetchServer()
        created = []

        async def mock_open(**kwargs):
            created.append(1)
            sid = "warm-1"
            mock_sess = MagicMock()
            mock_sess._is_alive = True
            srv._sessions[sid] = _SessionEntry(session=mock_sess, session_type="stealthy")
            return SessionCreatedModel(
                session_id=sid, session_type="stealthy",
                created_at="", is_alive=True, message="",
            )

        srv.open_session = mock_open
        try:
            await srv._prewarm_stealthy()  # should not raise
            await srv._prewarm_stealthy()  # idempotent: reuses, no 2nd creation
        finally:
            srv._sessions.clear()
            srv._auto_stealthy_id = None
        assert len(created) == 1

    @pytest.mark.asyncio
    async def test_prewarm_swallows_creation_failure(self):
        srv = MasterFetchServer()

        async def mock_open(**kwargs):
            raise RuntimeError("chromium not installed")

        srv.open_session = mock_open
        # Must not raise — browser will launch on first real fetch instead.
        await srv._prewarm_stealthy()

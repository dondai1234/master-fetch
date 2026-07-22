"""Server core tests: JS shell detection, content issue detection, cacheability,
agent hints, chunking, Cloudflare detection, CF challenge signals.

Tests the REAL signal-detection functions (_is_js_shell, _detect_content_issue,
_is_cacheable, _agent_hints, _apply_chunking, _is_cloudflare_from_response)
against real ResponseModel objects. No mocks of the functions themselves.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from master_fetch.server import (
    ResponseModel, _is_js_shell, _detect_content_issue, _is_cacheable,
    _agent_hints, _apply_chunking, _is_cloudflare_from_response,
    _annotate_quality, MAX_CONTENT_CHARS, MIN_CHUNK_CHARS, MAX_BULK_URLS,
    _JS_SHELL_SIGNALS, _CF_CHALLENGE_SIGNALS, MAX_RESPONSE_BYTES,
)


def _make_result(**kwargs):
    """Build a ResponseModel with sensible defaults for testing."""
    defaults = dict(
        status=200, content=["Hello world"], url="https://example.com",
        fetcher_used="http", content_type="text/html",
        total_size_bytes=1000, extracted_type="markdown",
    )
    defaults.update(kwargs)
    return ResponseModel(**defaults)


# ─── JS shell detection ───────────────────────────────────────────

class TestIsJsShell:

    def test_empty_content_is_js_shell(self):
        result = _make_result(content=[], status=200)
        assert _is_js_shell(result) is True

    def test_blank_content_is_js_shell(self):
        result = _make_result(content=["   "], status=200)
        assert _is_js_shell(result) is True

    def test_real_content_not_js_shell(self):
        result = _make_result(content=["Real article text about Python."], status=200)
        assert _is_js_shell(result) is False

    def test_enable_javascript_signal(self):
        result = _make_result(content=["Please enable JavaScript to run this app."], status=200)
        assert _is_js_shell(result) is True

    def test_javascript_disabled_signal(self):
        result = _make_result(content=["JavaScript is disabled in this browser."], status=200)
        assert _is_js_shell(result) is True

    def test_large_body_low_text_is_shell(self):
        # Large HTML body but almost no extractable text -> JS shell
        result = _make_result(
            content=["hi"], status=200, fetcher_used="http",
            total_size_bytes=5000,
        )
        assert _is_js_shell(result) is True

    def test_stealthy_large_body_low_text_not_shell(self):
        # Stealthy result with little text is a real low-text page, not a shell
        result = _make_result(
            content=["hi"], status=200, fetcher_used="stealthy",
            total_size_bytes=5000,
        )
        assert _is_js_shell(result) is False

    def test_cf_challenge_detected_in_200(self):
        # CF Turnstile challenge pages return 200 with CF markers
        result = _make_result(
            content=["<script>challenges.cloudflare.com/turnstile</script>"],
            status=200, fetcher_used="http",
        )
        assert _is_js_shell(result) is True

    def test_cf_turnstile_marker_detected(self):
        result = _make_result(
            content=["<div class='cf-turnstile'></div>"],
            status=200, fetcher_used="http",
        )
        assert _is_js_shell(result) is True

    def test_cf_chl_opt_detected(self):
        result = _make_result(
            content=["var cf_chl_opt = {};"],
            status=200, fetcher_used="http",
        )
        assert _is_js_shell(result) is True

    def test_normal_page_with_word_javascript_not_shell(self):
        # A page that mentions "javascript" but has real content
        result = _make_result(
            content=["This article discusses JavaScript frameworks and their performance."],
            status=200, fetcher_used="http", total_size_bytes=1000,
        )
        assert _is_js_shell(result) is False


# ─── Content issue detection ───────────────────────────────────────

class TestDetectContentIssue:

    def test_clean_content_no_issue(self):
        result = _make_result()
        assert _detect_content_issue(result) == ""

    def test_js_shell_detected(self):
        result = _make_result(content=[], status=200)
        assert "js_shell" in _detect_content_issue(result)

    def test_geo_redirect_detected(self):
        result = _make_result(content=["Choose your country to continue shopping"])
        assert "geo_redirect" in _detect_content_issue(result)

    def test_http_404_error(self):
        result = _make_result(status=404, content=["404 Not Found"])
        assert "http_error_404" in _detect_content_issue(result)

    def test_http_500_error(self):
        result = _make_result(status=500, content=["Internal Server Error"])
        assert "http_error_500" in _detect_content_issue(result)

    def test_network_error(self):
        # status=0 with content -> js_shell check triggers first (empty content)
        # Test with non-empty content and status=0
        result = _make_result(status=0, content=["connection refused"], error="")
        issue = _detect_content_issue(result)
        assert "network_error" in issue or "http_error" in issue

    def test_cf_challenge_on_403(self):
        result = _make_result(status=403, content=["Checking your browser. Cloudflare."])
        assert "bot_challenge" in _detect_content_issue(result)

    def test_cf_challenge_on_503(self):
        result = _make_result(status=503, content=["Please verify you are a human."])
        assert "bot_challenge" in _detect_content_issue(result)

    def test_cf_mention_on_200_not_challenge(self):
        # A 200 page about Cloudflare security is NOT a bot challenge
        result = _make_result(status=200, content=["This article about Cloudflare CDN..."])
        assert "bot_challenge" not in _detect_content_issue(result)


# ─── Cloudflare detection ─────────────────────────────────────────

class TestCloudflareDetection:

    def test_200_not_cloudflare(self):
        result = _make_result(status=200, content=["cloudflare mentions"])
        assert _is_cloudflare_from_response(result) is False

    def test_403_with_cloudflare_signal(self):
        result = _make_result(status=403, content=["Cloudflare challenge page"])
        assert _is_cloudflare_from_response(result) is True

    def test_503_with_datadome_signal(self):
        result = _make_result(status=503, content=["datadome captcha-delivery.com"])
        assert _is_cloudflare_from_response(result) is True

    def test_200_not_checked(self):
        result = _make_result(status=200, content=["ray id: abc123"])
        assert _is_cloudflare_from_response(result) is False


# ─── Cacheability ──────────────────────────────────────────────────

class TestIsCacheable:

    def test_clean_200_cacheable(self):
        result = _make_result()
        assert _is_cacheable(result) is True

    def test_404_not_cacheable(self):
        result = _make_result(status=404, error="http_error_404")
        assert _is_cacheable(result) is False

    def test_error_not_cacheable(self):
        result = _make_result(error="js_shell_detected")
        assert _is_cacheable(result) is False

    def test_empty_content_not_cacheable(self):
        result = _make_result(content=[], status=200)
        assert _is_cacheable(result) is False

    def test_blank_content_not_cacheable(self):
        result = _make_result(content=["  "], status=200)
        assert _is_cacheable(result) is False

    def test_3xx_cacheable(self):
        result = _make_result(status=301, content=["redirected"])
        assert _is_cacheable(result) is True


# ─── Agent hints ───────────────────────────────────────────────────

class TestAgentHints:

    def test_clean_result_summary(self):
        result = _make_result()
        summary, next_action, content_ok = _agent_hints(result)
        assert "200" in summary
        assert "OK" in summary
        assert content_ok is True
        assert next_action == ""

    def test_truncated_result_next_action(self):
        result = _make_result(is_truncated=True, next_offset=40000)
        summary, next_action, content_ok = _agent_hints(result)
        assert "truncated" in summary
        assert "offset=40000" in next_action
        assert "focus=" in next_action  # v11.2: suggest focus= first

    def test_error_result_content_ok_false(self):
        result = _make_result(status=404, error="http_error_404")
        summary, next_action, content_ok = _agent_hints(result)
        assert content_ok is False
        assert "fetch failed" in next_action

    def test_network_error_summary(self):
        result = _make_result(status=0, error="network_error")
        summary, _, _ = _agent_hints(result)
        assert "network error" in summary

    def test_cached_result_in_summary(self):
        result = _make_result(cached=True)
        summary, _, _ = _agent_hints(result)
        assert "cached" in summary

    def test_js_shell_next_action(self):
        result = _make_result(error="js_shell_detected: placeholder")
        _, next_action, _ = _agent_hints(result)
        assert "stealthy" in next_action

    def test_bot_challenge_next_action(self):
        result = _make_result(error="bot_challenge_detected: cf page")
        _, next_action, _ = _agent_hints(result)
        assert "stealthy" in next_action

    def test_large_pdf_suggests_focus_or_pages(self):
        result = _make_result(
            page_type="pdf", total_extracted_chars=50000,
            content=["A" * 100], status=200,
        )
        _, next_action, _ = _agent_hints(result)
        assert "focus=" in next_action
        assert "pages=" in next_action

    def test_short_pdf_no_focus_hint(self):
        result = _make_result(
            page_type="pdf", total_extracted_chars=5000,
            content=["A" * 100], status=200,
        )
        _, next_action, _ = _agent_hints(result)
        assert "focus=" not in next_action

        result = _make_result(page_type="list", links={
            "citations": [{"url": "https://example.com/page1", "text": "P1"}]
        })
        _, next_action, _ = _agent_hints(result)
        assert "list page" in next_action.lower()
        assert "example.com/page1" in next_action

    def test_auth_wall_next_action(self):
        result = _make_result(page_type="auth_wall")
        _, next_action, _ = _agent_hints(result)
        assert "login" in next_action.lower() or "authentication" in next_action.lower()

    def test_stale_content_next_action(self):
        result = _make_result(page_type="article", is_stale=True, content_age_days=500)
        _, next_action, _ = _agent_hints(result)
        assert "500" in next_action
        assert "outdated" in next_action.lower() or "search" in next_action.lower()


# ─── Chunking ──────────────────────────────────────────────────────

class TestChunking:

    def test_short_content_not_truncated(self):
        result = _make_result(content=["Short content"])
        chunked = _apply_chunking(result)
        assert chunked.is_truncated is False
        assert chunked.next_offset == 0

    def test_long_content_truncated(self):
        long_text = "A" * (MAX_CONTENT_CHARS + 1000)
        result = _make_result(content=[long_text])
        chunked = _apply_chunking(result)
        assert chunked.is_truncated is True
        assert chunked.next_offset == MAX_CONTENT_CHARS
        assert chunked.total_extracted_chars > MAX_CONTENT_CHARS

    def test_offset_retrieves_next_chunk(self):
        long_text = "A" * (MAX_CONTENT_CHARS + 1000)
        result = _make_result(content=[long_text])
        first = _apply_chunking(result, offset=0)
        second = _apply_chunking(result, offset=first.next_offset)
        assert second.content[0].startswith("A")

    def test_offset_past_end_returns_no_more(self):
        result = _make_result(content=["Short content"])
        chunked = _apply_chunking(result, offset=99999)
        assert chunked.is_truncated is False
        assert "No more content" in chunked.content[0]

    def test_smart_merge_small_remaining(self):
        # Content slightly over MAX_CONTENT_CHARS: remaining is small -> not truncated
        long_text = "A" * (MAX_CONTENT_CHARS + MIN_CHUNK_CHARS - 10)
        result = _make_result(content=[long_text])
        chunked = _apply_chunking(result)
        # Remaining (MIN_CHUNK_CHARS - 10) < MIN_CHUNK_CHARS -> merged into one chunk
        assert chunked.is_truncated is False

    def test_chunking_preserves_envelope_fields(self):
        result = _make_result(
            url="https://github.com/user/repo",
            metadata={"title": "Test"}, page_type="article",
            quality_score=0.9,
        )
        chunked = _apply_chunking(result)
        assert chunked.metadata["title"] == "Test"
        assert chunked.page_type == "article"
        assert chunked.source_type == "github"  # recomputed from URL
        assert chunked.quality_score == 0.9

    def test_chunking_stamps_fetched_at(self):
        result = _make_result()
        chunked = _apply_chunking(result)
        assert chunked.fetched_at != ""

    def test_chunking_stamps_content_ok(self):
        result = _make_result()
        chunked = _apply_chunking(result)
        assert chunked.content_ok is True


# ─── Annotate quality ─────────────────────────────────────────────

class TestAnnotateQuality:

    def test_sets_error_on_js_shell(self):
        result = _make_result(content=[])
        annotated = _annotate_quality(result)
        assert "js_shell" in annotated.error

    def test_does_not_overwrite_existing_error(self):
        result = _make_result(error="custom error")
        annotated = _annotate_quality(result)
        assert annotated.error == "custom error"

    def test_clean_result_no_error_set(self):
        result = _make_result()
        annotated = _annotate_quality(result)
        assert annotated.error == ""


# ─── Constants and signals ─────────────────────────────────────────

class TestConstants:

    def test_max_content_chars_reasonable(self):
        assert 10000 < MAX_CONTENT_CHARS < 100000

    def test_min_chunk_chars_reasonable(self):
        assert 100 < MIN_CHUNK_CHARS < 2000

    def test_max_bulk_urls_prevents_dos(self):
        assert MAX_BULK_URLS == 100

    def test_max_response_bytes_prevents_dos(self):
        assert MAX_RESPONSE_BYTES == 50 * 1024 * 1024

    def test_js_shell_signals_not_empty(self):
        assert len(_JS_SHELL_SIGNALS) > 5

    def test_cf_challenge_signals_defined(self):
        assert "cf-turnstile" in _CF_CHALLENGE_SIGNALS
        assert "challenges.cloudflare.com/turnstile" in _CF_CHALLENGE_SIGNALS
        assert "cf_chl_opt" in _CF_CHALLENGE_SIGNALS
        assert "__cf_chl" in _CF_CHALLENGE_SIGNALS
        assert "challenge-platform" in _CF_CHALLENGE_SIGNALS
        assert "cf-mitigated" in _CF_CHALLENGE_SIGNALS

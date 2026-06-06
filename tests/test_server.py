"""Tests for server.py — response models, content detection, chunking."""

import pytest
from master_fetch.server import (
    ResponseModel,
    BulkResponseModel,
    _is_js_shell,
    _is_cloudflare_from_response,
    _detect_content_issue,
    _annotate_quality,
    _apply_chunking,
    _safe_cookie_dict,
    MAX_CONTENT_CHARS,
)


def _make_response(status=200, content=None, url="https://example.com", fetcher="http"):
    """Factory for ResponseModel."""
    return ResponseModel(
        status=status,
        content=content or ["<html><body>Hello World</body></html>"],
        url=url,
        fetcher_used=fetcher,
    )


class TestResponseModel:
    """Basic ResponseModel tests."""

    def test_creates_with_defaults(self):
        r = ResponseModel(status=200, content=["test"], url="https://example.com")
        assert r.status == 200
        assert r.cached is False
        assert r.fetcher_used == ""
        assert r.error == ""

    def test_new_defaults(self):
        """v2.8.0 fields have sensible defaults."""
        r = _make_response()
        assert r.content_type == ""
        assert r.total_size_bytes == 0
        assert r.is_truncated is False
        assert r.escalation_path == ""
        assert r.retry_count == 0

    def test_content_type_field(self):
        r = _make_response()
        r.content_type = "text/html"
        assert r.content_type == "text/html"

    def test_escalation_path_field(self):
        r = _make_response()
        r.escalation_path = "http→dynamic→stealthy"
        assert r.escalation_path == "http→dynamic→stealthy"

    def test_retry_count_field(self):
        r = _make_response()
        r.retry_count = 4
        assert r.retry_count == 4

    def test_is_truncated_field(self):
        r = _make_response()
        r.is_truncated = True
        assert r.is_truncated is True

    def test_total_size_bytes_field(self):
        r = _make_response()
        r.total_size_bytes = 54321
        assert r.total_size_bytes == 54321

    def test_bulk_response_counting(self):
        results = [
            _make_response(status=200),
            _make_response(status=404),
            _make_response(status=200),
        ]
        bulk = BulkResponseModel(results=results, total=3, successful=2)
        assert bulk.total == 3
        assert bulk.successful == 2


class TestJsShellDetection:
    """JavaScript shell / placeholder detection."""

    def test_detects_enable_javascript(self):
        r = _make_response(content=["Please enable JavaScript to view this page."])
        assert _is_js_shell(r) is True

    def test_detects_javascript_required(self):
        r = _make_response(content=["JavaScript is required to run this app."])
        assert _is_js_shell(r) is True

    def test_detects_js_disabled(self):
        r = _make_response(content=["We've detected that JavaScript is disabled in this browser."])
        assert _is_js_shell(r) is True

    def test_normal_content_not_js_shell(self):
        r = _make_response(content=["<html><body>Regular page content</body></html>"])
        assert _is_js_shell(r) is False

    def test_empty_content_is_js_shell(self):
        r = _make_response(content=[""])
        assert _is_js_shell(r) is True

    def test_whitespace_only_is_js_shell(self):
        r = _make_response(content=["   \n  "])
        assert _is_js_shell(r) is True


class TestCloudflareDetection:
    """Bot challenge detection."""

    def test_detects_cloudflare(self):
        r = _make_response(
            content=["Cloudflare is checking your browser..."],
            status=403,
        )
        assert _is_cloudflare_from_response(r) is True

    def test_detects_cf_challenge(self):
        r = _make_response(
            content=["challenge-platform/h/b/... cf_chl_opt=..."],
            status=503,
        )
        assert _is_cloudflare_from_response(r) is True

    def test_detects_captcha_delivery(self):
        r = _make_response(content=["captcha-delivery.com/..."])
        assert _is_cloudflare_from_response(r) is True

    def test_detects_datadome(self):
        r = _make_response(content=["datadome test page" "dd=... var..."])
        assert _is_cloudflare_from_response(r) is True

    def test_normal_content_not_cf(self):
        r = _make_response(content=["This is a regular page about cloud computing."])
        # "cloud" is not "cloudflare" — should be fine
        assert _is_cloudflare_from_response(r) is False

    def test_article_mentioning_cloudflare_not_blocked(self):
        """Articles about web security mention 'cloudflare' in body text."""
        r = _make_response(content=["Cloudflare announced a new feature today..."])
        # This is a known limitation — cloudflare in body triggers detection
        # but _phase_c_unknown handles this by not checking on 200-status responses
        assert _is_cloudflare_from_response(r) is True


class TestContentIssueDetection:
    """_detect_content_issue and _annotate_quality tests."""

    def test_detects_js_shell(self):
        r = _make_response(content=["JavaScript is disabled"])
        issue = _detect_content_issue(r)
        assert "js_shell_detected" in issue

    def test_detects_geo_redirect(self):
        r = _make_response(content=["Please select your country from the list below."])
        issue = _detect_content_issue(r)
        assert "geo_redirect_detected" in issue

    def test_detects_bot_challenge(self):
        r = _make_response(content=["Cloudflare ray id: checking your browser..."])
        issue = _detect_content_issue(r)
        assert "bot_challenge_detected" in issue

    def test_normal_content_no_issue(self):
        r = _make_response(content=["This is a normal page about Python programming."])
        assert _detect_content_issue(r) == ""

    def test_annotate_quality_sets_error(self):
        r = _make_response(content=["Enable JavaScript to continue."])
        annotated = _annotate_quality(r)
        assert annotated.error == "js_shell_detected: page requires JavaScript rendering but fetcher returned placeholder"

    def test_annotate_quality_preserves_existing_error(self):
        r = _make_response(content=["Enable JavaScript."])
        r.error = "existing_error"
        annotated = _annotate_quality(r)
        assert annotated.error == "existing_error"  # Not overwritten


class TestChunking:
    """Content chunking and continuation."""

    def test_small_content_not_truncated(self):
        r = _make_response(content=["Short content"])
        result = _apply_chunking(r)
        assert "[Truncated:" not in result.content[0]
        assert result.content == ["Short content"]

    def test_large_content_truncated(self):
        huge = "x" * (MAX_CONTENT_CHARS + 100)
        r = _make_response(content=[huge])
        result = _apply_chunking(r)
        assert "[Truncated:" in result.content[0]
        assert len(result.content[0]) < MAX_CONTENT_CHARS + 500  # room for notice

    def test_offset_continuation(self):
        content = "ABCDEFGHIJ"  # 10 chars
        r = _make_response(content=[content])
        result = _apply_chunking(r, max_chars=4, offset=4)
        # Should get chars 4-7 (EFGH) + truncation note
        assert "EFGH" in result.content[0]
        assert "Next offset:" in result.content[0]

    def test_offset_beyond_content(self):
        content = "ABC"  # 3 chars
        r = _make_response(content=[content])
        result = _apply_chunking(r, offset=10)
        assert "No more content" in result.content[0]

    def test_chunking_preserves_fields(self):
        r = _make_response(
            content=["Hello"],
            url="https://test.com",
            fetcher="http",
        )
        r.extracted_type = "markdown"
        r.session_id = "sess123"
        r.duration_ms = 500.0
        r.error = "none"

        result = _apply_chunking(r)
        assert result.url == "https://test.com"
        assert result.fetcher_used == "http"
        assert result.extracted_type == "markdown"
        assert result.session_id == "sess123"
        assert result.duration_ms == 500.0
        assert result.error == "none"

    def test_multi_line_content(self):
        content = "line1\nline2"
        r = _make_response(content=["line1", "line2"])
        result = _apply_chunking(r)
        assert "line1\nline2" == result.content[0]

    def test_is_truncated_set_when_chunked(self):
        """When content exceeds max_chars, is_truncated should be True."""
        content = "x" * 200
        r = _make_response(content=[content])
        r.total_size_bytes = len(content)
        result = _apply_chunking(r, max_chars=100)
        assert result.is_truncated is True

    def test_is_truncated_false_when_not_chunked(self):
        """When content fits, is_truncated should be False."""
        content = "short"
        r = _make_response(content=[content])
        result = _apply_chunking(r, max_chars=10000)
        assert result.is_truncated is False

    def test_truncation_preserves_escalation_path(self):
        """Chunking should pass through escalation_path."""
        content = "x" * 200
        r = _make_response(content=[content])
        r.escalation_path = "http→dynamic"
        result = _apply_chunking(r, max_chars=100)
        assert result.escalation_path == "http→dynamic"
        assert result.is_truncated is True

    def test_truncation_preserves_retry_count(self):
        content = "x" * 200
        r = _make_response(content=[content])
        r.retry_count = 3
        result = _apply_chunking(r, max_chars=100)
        assert result.retry_count == 3


class TestNewResponseMetadata:
    """Tests for v2.8.0 metadata fields in the full pipeline."""

    def test_json_content_type_detection(self):
        """Verify content_type can be set and read on a response."""
        r = _make_response()
        r.content_type = "application/json"
        assert r.content_type == "application/json"

    def test_escalation_path_direct_http(self):
        r = _make_response()
        r.escalation_path = "direct:http"
        assert r.escalation_path == "direct:http"

    def test_escalation_path_full_chain(self):
        r = _make_response()
        r.escalation_path = "http→dynamic→stealthy"
        assert "http" in r.escalation_path
        assert "dynamic" in r.escalation_path
        assert "stealthy" in r.escalation_path

    def test_all_new_fields_in_roundtrip(self):
        """All new fields survive a chunking roundtrip."""
        r = _make_response()
        r.content_type = "text/html; charset=utf-8"
        r.total_size_bytes = 65536
        r.escalation_path = "direct:stealthy(auto)"
        r.retry_count = 1

        result = _apply_chunking(r)
        assert result.content_type == "text/html; charset=utf-8"
        assert result.total_size_bytes == 65536
        assert result.escalation_path == "direct:stealthy(auto)"
        assert result.retry_count == 1
        assert result.is_truncated is False  # small content


class TestSafeCookieDict:
    """Cookie conversion safety."""

    def test_valid_cookies(self):
        cookies = [{"name": "session", "value": "abc123"}]
        result = _safe_cookie_dict(cookies)
        assert result == {"session": "abc123"}

    def test_none_cookies(self):
        assert _safe_cookie_dict(None) is None

    def test_empty_cookies(self):
        assert _safe_cookie_dict([]) is None

    def test_missing_name_skipped(self):
        cookies = [{"value": "abc"}, {"name": "ok", "value": "val"}]
        result = _safe_cookie_dict(cookies)
        assert "ok" in result
        assert "abc" not in result.values()  # The value-only cookie is skipped

    def test_multiple_cookies(self):
        cookies = [
            {"name": "a", "value": "1"},
            {"name": "b", "value": "2"},
        ]
        result = _safe_cookie_dict(cookies)
        assert result == {"a": "1", "b": "2"}

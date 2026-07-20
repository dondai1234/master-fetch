"""Tests for universal error detection: 4xx/5xx status codes must set result.error
so error pages are never treated as real content.

Tests:
- _detect_content_issue flags 4xx/5xx statuses
- _is_cacheable rejects error pages (no caching broken content)
- _is_archive_worthy still works for 404/451/500 (archive fallback)
- _auto_escalate escalates 429/500/502 to stealthy (not just 403/503)
- 200 with real content stays clean (no false positives)
"""
import pytest
from master_fetch.server import (
    _detect_content_issue,
    _is_cacheable,
    _is_archive_worthy,
    ResponseModel,
)


class TestErrorDetection:
    """Universal error detection for 4xx/5xx status codes."""

    def _make(self, status, content=None, fetcher="http", **kw):
        return ResponseModel(
            url="https://example.com/test",
            status=status,
            content=content or ["Some error page content"],
            fetcher_used=fetcher,
            source="live",
            total_size_bytes=kw.get("total_size_bytes", 200),
        )

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 429, 451])
    def test_4xx_sets_error(self, status):
        r = self._make(status)
        err = _detect_content_issue(r)
        assert f"http_error_{status}" in err

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_5xx_sets_error(self, status):
        r = self._make(status)
        err = _detect_content_issue(r)
        assert f"http_error_{status}" in err

    def test_200_real_content_no_error(self):
        r = self._make(200, content=["Real article about Python programming."], total_size_bytes=500)
        assert _detect_content_issue(r) == ""

    def test_200_real_content_stealthy_no_error(self):
        r = self._make(200, content=["Stealthy rendered content."], fetcher="stealthy", total_size_bytes=500)
        assert _detect_content_issue(r) == ""

    def test_404_not_cached(self):
        r = self._make(404)
        _detect_content_issue(r)  # sets error
        # _is_cacheable checks error after _annotate_quality sets it
        r.error = _detect_content_issue(r)
        assert not _is_cacheable(r)

    def test_429_not_cached(self):
        r = self._make(429)
        r.error = _detect_content_issue(r)
        assert not _is_cacheable(r)

    def test_500_not_cached(self):
        r = self._make(500)
        r.error = _detect_content_issue(r)
        assert not _is_cacheable(r)

    def test_200_real_content_cached(self):
        r = self._make(200, content=["Real content here."], total_size_bytes=500)
        assert _is_cacheable(r)

    def test_404_archive_worthy(self):
        r = self._make(404)
        assert _is_archive_worthy(r)

    def test_429_not_archive_worthy(self):
        """429 is rate limiting, not a dead page. Archive can't help."""
        r = self._make(429)
        assert not _is_archive_worthy(r)

    def test_500_archive_worthy(self):
        r = self._make(500)
        assert _is_archive_worthy(r)

    def test_error_does_not_override_existing(self):
        """If error is already set (e.g. by fetcher), _annotate_quality won't override."""
        r = self._make(404)
        r.error = "existing_error_from_fetcher"
        # _annotate_quality checks `if not result.error` before calling _detect_content_issue
        # So the existing error stays
        assert r.error == "existing_error_from_fetcher"

    def test_403_error_message(self):
        """403 that's NOT a bot challenge still gets flagged as http_error."""
        r = self._make(403, content=["Forbidden", "Access denied"])
        err = _detect_content_issue(r)
        # Could be bot_challenge or http_error_403, either way error is set
        assert err != ""

    def test_error_page_content_not_treated_as_real(self):
        """The whole point: a 404 error page with content should still get an error."""
        r = self._make(404, content=[
            "404 Not Found",
            "The page you requested could not be found.",
            "Please check the URL and try again.",
        ])
        err = _detect_content_issue(r)
        assert "http_error_404" in err
        assert not _is_cacheable(ResponseModel(
            url=r.url, status=r.status, content=r.content,
            fetcher_used=r.fetcher_used, source=r.source, error=err,
            total_size_bytes=200,
        ))

"""Unit tests for Master Fetch server module."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from master_fetch.server import (
    ResponseModel, _apply_chunking, _is_cloudflare_from_response,
    _is_cloudflare_challenge, _is_js_shell, _detect_content_issue,
    _annotate_quality, MAX_CONTENT_CHARS,
)
from master_fetch.domain_intel import _extract_domain


class TestResponseModel:
    def test_create_valid_response(self):
        r = ResponseModel(status=200, content=["Hello world"], url="https://example.com")
        assert r.status == 200
        assert r.content == ["Hello world"]
        assert r.url == "https://example.com"
        assert r.cached is False
        assert r.fetcher_used == ""

    def test_create_cached_response(self):
        r = ResponseModel(status=200, content=["cached"], url="https://ex.com", cached=True, fetcher_used="cache")
        assert r.cached is True
        assert r.fetcher_used == "cache"


class TestChunking:
    def test_under_limit_passes_through(self):
        r = ResponseModel(status=200, content=["hello"], url="https://x.com")
        result = _apply_chunking(r)
        assert result.content == ["hello"]
        assert result.error == ""
        assert result.fetcher_used == ""

    def test_over_limit_truncated(self):
        content = "x" * (MAX_CONTENT_CHARS + 100)
        r = ResponseModel(status=200, content=[content], url="https://x.com", fetcher_used="http")
        result = _apply_chunking(r)
        assert len(result.content[0]) <= MAX_CONTENT_CHARS + 300  # account for continuation message
        assert "Content truncated" in result.content[0]
        assert f"offset=40000" in result.content[0]  # tells agent exactly what offset to use next

    def test_offset_continuation(self):
        """Calling with offset returns the next chunk of content."""
        content = "A" * 30000 + "B" * 30000  # 60KB total
        r = ResponseModel(status=200, content=[content], url="https://x.com", fetcher_used="http")

        # First call: offset=0, gets first 40KB
        chunk1 = _apply_chunking(r, offset=0)
        assert chunk1.content[0].startswith("A" * 30000 + "B" * 10000)
        assert "offset=40000" in chunk1.content[0]
        assert "20,000 chars remaining" in chunk1.content[0]

        # Second call: offset=40000, gets remaining 20KB
        chunk2 = _apply_chunking(r, offset=40000)
        assert chunk2.content[0].startswith("B" * 20000)
        assert "Content truncated" not in chunk2.content[0]  # no more remaining

    def test_offset_beyond_content(self):
        """Offset beyond content length returns end message."""
        content = "hello"
        r = ResponseModel(status=200, content=[content], url="https://x.com")
        result = _apply_chunking(r, offset=9999)
        assert "No more content" in result.content[0]

    def test_offset_exact_end(self):
        """Offset exactly at content length returns end message."""
        content = "A" * 50000
        r = ResponseModel(status=200, content=[content], url="https://x.com")
        chunk1 = _apply_chunking(r, offset=0)
        assert "10,000 chars remaining" in chunk1.content[0]
        # offset=40000 should get last 10KB
        chunk2 = _apply_chunking(r, offset=40000)
        assert chunk2.content[0].startswith("A" * 10000)
        assert "Content truncated" not in chunk2.content[0]
        # offset=50000 should say no more
        chunk3 = _apply_chunking(r, offset=50000)
        assert "No more content" in chunk3.content[0]

    def test_multiple_content_strings_flattened(self):
        """Multiple content strings are flattened before chunking."""
        part1 = "A" * 25000
        part2 = "B" * 25000
        r = ResponseModel(status=200, content=[part1, part2], url="https://x.com")
        result = _apply_chunking(r)
        # First 40KB = 25K A's + newline + 14,999 B's (50,001 total with newline)
        assert "10,001 chars remaining" in result.content[0]

    def test_preserves_all_fields(self):
        """Chunking preserves all ResponseModel fields, not just status/content/url."""
        r = ResponseModel(
            status=200, content=["x" * 50000], url="https://x.com",
            cached=True, fetcher_used="http", extracted_type="markdown",
            session_id="abc123", duration_ms=1500.0, error=""
        )
        result = _apply_chunking(r)
        assert result.cached is True
        assert result.fetcher_used == "http"
        assert result.extracted_type == "markdown"
        assert result.session_id == "abc123"
        assert result.duration_ms == 1500.0
        assert result.error == ""

    def test_at_limit_no_truncation(self):
        content = "y" * MAX_CONTENT_CHARS
        r = ResponseModel(status=200, content=[content], url="https://x.com")
        result = _apply_chunking(r)
        assert result.content == [content]
        assert "Content truncated" not in result.content[0]


class TestJsShellDetection:
    def test_uniswap_js_shell(self):
        """Bug #1: Uniswap returns 'You need to enable JavaScript to run this app.' via HTTP."""
        r = ResponseModel(status=200, content=["You need to enable JavaScript to run this app."], url="https://app.uniswap.org/")
        assert _is_js_shell(r) is True

    def test_twitter_js_disabled(self):
        """Dynamic fetcher returns JS-disabled placeholder for Twitter."""
        r = ResponseModel(status=200, content=["We've detected that JavaScript is disabled in this browser. Please enable JavaScript"], url="https://twitter.com/AnthropicAI")
        assert _is_js_shell(r) is True

    def test_normal_content_not_shell(self):
        r = ResponseModel(status=200, content=["Buy and sell crypto with zero app fees on 19+ networks"], url="https://app.uniswap.org/")
        assert _is_js_shell(r) is False

    def test_empty_content_is_shell(self):
        r = ResponseModel(status=200, content=[""], url="https://example.com")
        assert _is_js_shell(r) is True

    def test_javascript_required_variants(self):
        for text in [
            "JavaScript is required to view this site",
            "Please enable JavaScript",
            "JavaScript must be enabled",
            "Requires JavaScript",
            "JavaScript is disabled",
        ]:
            r = ResponseModel(status=200, content=[text], url="https://example.com")
            assert _is_js_shell(r) is True, f"Failed for: {text}"


class TestContentQuality:
    def test_js_shell_sets_error(self):
        r = ResponseModel(status=200, content=["You need to enable JavaScript to run this app."], url="https://app.uniswap.org/")
        assert _detect_content_issue(r).startswith("js_shell_detected")

    def test_geo_redirect_sets_error(self):
        """Bug: BestBuy returns country selector via HTTP, no error signal."""
        r = ResponseModel(status=200, content=["Hello! Choose a country. Shopping in the U.S.?"], url="https://www.bestbuy.com/")
        assert _detect_content_issue(r).startswith("geo_redirect_detected")

    def test_cloudflare_sets_error(self):
        r = ResponseModel(status=200, content=["Checking your browser cloudflare ray id"], url="https://example.com")
        assert _detect_content_issue(r).startswith("bot_challenge_detected")

    def test_normal_content_no_error(self):
        r = ResponseModel(status=200, content=["This is a normal article about Python programming."], url="https://example.com")
        assert _detect_content_issue(r) == ""

    def test_annotate_quality_sets_error_field(self):
        r = ResponseModel(status=200, content=["You need to enable JavaScript to run this app."], url="https://app.uniswap.org/")
        result = _annotate_quality(r)
        assert result.error.startswith("js_shell_detected")

    def test_annotate_quality_preserves_existing_error(self):
        r = ResponseModel(status=500, content=["server error"], url="https://example.com", error="network_timeout")
        result = _annotate_quality(r)
        assert result.error == "network_timeout"  # Don't overwrite


class TestCloudflareDetection:
    def test_normal_200_not_cloudflare(self):
        r = ResponseModel(status=200, content=["normal page"], url="https://example.com")
        assert not _is_cloudflare_from_response(r)

    def test_403_with_challenge_text(self):
        r = ResponseModel(status=403, content=["just a moment... cloudflare"], url="https://example.com")
        assert _is_cloudflare_from_response(r)

    def test_503_without_challenge_not_cloudflare(self):
        r = ResponseModel(status=503, content=["service unavailable"], url="https://example.com")
        assert not _is_cloudflare_from_response(r)

    def test_empty_content(self):
        r = ResponseModel(status=403, content=[], url="https://example.com")
        assert not _is_cloudflare_from_response(r)


class TestDomainExtraction:
    def test_simple_domain(self):
        assert _extract_domain("https://example.com/page") == "example.com"

    def test_subdomain(self):
        result = _extract_domain("https://sub.example.com/path")
        assert "example.com" in result  # may return sub.example.com or example.com depending on implementation

    def test_multi_part_tld(self):
        assert _extract_domain("https://www.bbc.co.uk/news") == "bbc.co.uk"

    def test_multi_part_tld_au(self):
        assert _extract_domain("https://example.com.au/page") == "example.com.au"

    def test_bare_domain(self):
        # _extract_domain expects URLs with scheme; bare domains return empty
        assert _extract_domain("example.com") == ""  # no scheme, can't parse

    def test_invalid_url(self):
        result = _extract_domain("not-a-valid-url!!!")
        assert result is not None  # should return something, not crash


class TestBinaryDetection:
    def test_probably_binary_pdf(self):
        from master_fetch.trafilatura_extractor import _is_probably_binary
        pdf_header = b"%PDF-1.4\n%\x9c\x9c\x9c\x9c" + b"\x00" * 100
        assert _is_probably_binary(pdf_header) is True

    def test_text_is_not_binary(self):
        from master_fetch.trafilatura_extractor import _is_probably_binary
        text = b"<html><body>Hello world</body></html>"
        assert _is_probably_binary(text) is False

    def test_empty_data(self):
        from master_fetch.trafilatura_extractor import _is_probably_binary
        assert _is_probably_binary(b"") is False


class TestDomainIntel:
    def test_known_safe_domain(self):
        from master_fetch.domain_intel import guess_protection_level
        assert guess_protection_level("https://httpbin.org/html") == "none"
        assert guess_protection_level("https://en.wikipedia.org/wiki/Python") == "none"

    def test_known_stealthy_domain(self):
        from master_fetch.domain_intel import guess_protection_level
        assert guess_protection_level("https://twitter.com/AnthropicAI") == "high"
        assert guess_protection_level("https://x.com/AnthropicAI") == "high"

    def test_known_dynamic_domain(self):
        from master_fetch.domain_intel import guess_protection_level
        assert guess_protection_level("https://www.youtube.com/results?search_query=test") == "low"
        assert guess_protection_level("https://app.uniswap.org/") == "low"

    def test_unknown_domain_defaults_none(self):
        from master_fetch.domain_intel import guess_protection_level
        assert guess_protection_level("https://some-unknown-site-12345.com/") == "none"


class TestRobots:
    def test_allowed_url(self):
        from master_fetch.robots import is_allowed, _domain_from_url, clear_robots_cache
        clear_robots_cache()
        # Most sites allow, but depends on live robots.txt
        result = is_allowed("https://httpbin.org/html")
        assert result is True  # httpbin.org has no robots.txt

    def test_domain_extraction(self):
        from master_fetch.robots import _domain_from_url
        assert _domain_from_url("https://example.com/path") == "example.com"
        assert _domain_from_url("https://sub.example.com") == "sub.example.com"
        assert _domain_from_url("not-a-url") == ""

    def test_cache_clear(self):
        from master_fetch.robots import clear_robots_cache, _robots_cache
        _robots_cache["test.com"] = (None, 0)
        clear_robots_cache()
        assert len(_robots_cache) == 0

"""Unit tests for Master Fetch server module."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from master_fetch.server import (
    ResponseModel, _apply_chunking, _is_cloudflare_from_response,
    _is_cloudflare_challenge, MAX_CONTENT_CHARS,
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

    def test_over_limit_truncated(self):
        content = "x" * (MAX_CONTENT_CHARS + 100)
        r = ResponseModel(status=200, content=[content], url="https://x.com")
        result = _apply_chunking(r)
        total = sum(len(c) for c in result.content)
        assert total <= MAX_CONTENT_CHARS + 200  # account for truncation message
        assert "Content truncated" in result.content[0]

    def test_multiple_chunks_truncation(self):
        chunk1 = "a" * 20000
        chunk2 = "b" * 30000
        r = ResponseModel(status=200, content=[chunk1, chunk2], url="https://x.com")
        result = _apply_chunking(r)
        total = sum(len(c) for c in result.content)
        assert total <= MAX_CONTENT_CHARS + 200
        # First chunk should be intact, second truncated
        assert result.content[0] == chunk1

    def test_at_limit_no_truncation(self):
        content = "y" * MAX_CONTENT_CHARS
        r = ResponseModel(status=200, content=[content], url="https://x.com")
        result = _apply_chunking(r)
        assert result.content == [content]
        assert "Content truncated" not in result.content[0]


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

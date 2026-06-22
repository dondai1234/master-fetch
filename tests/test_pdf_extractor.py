"""Tests for the flagship PDF extractor + its smart_fetch integration.

Pure-function tests cover parsing/de-hyphenation/table-rendering/heading
detection/metadata. Fixture-based tests use two real PDFs committed under
tests/: background_checks.pdf (text + table) and dummy.pdf (image-only /
scanned). Integration tests cover _extract_pdf_response, _dispatch param
threading, and the pages-aware cache key.
"""

import pytest
from unittest.mock import AsyncMock, patch

from master_fetch.pdf_extractor import (
    extract_pdf,
    _parse_pages,
    _dehyphenate_join,
    _table_to_markdown,
    _heading_level,
    _format_metadata,
)
from master_fetch.server import MasterFetchServer, ResponseModel, _extract_pdf_response
import master_fetch.cache as cache_mod
from master_fetch.cache import _cache_key

FIX_TEXT = __import__("os").path.join(__import__("os").path.dirname(__file__), "background_checks.pdf")
FIX_SCANNED = __import__("os").path.join(__import__("os").path.dirname(__file__), "dummy.pdf")


# ─── _parse_pages ─────────────────────────────────────────────────────────

class TestParsePages:
    def test_none_returns_all(self):
        assert _parse_pages(None, 5) == [1, 2, 3, 4, 5]

    def test_empty_returns_all(self):
        assert _parse_pages("", 5) == [1, 2, 3, 4, 5]

    def test_range(self):
        assert _parse_pages("1-3", 10) == [1, 2, 3]

    def test_list(self):
        assert _parse_pages("1,3,5", 10) == [1, 3, 5]

    def test_mixed(self):
        assert _parse_pages("1,3-5,7", 10) == [1, 3, 4, 5, 7]

    def test_clamps_high(self):
        assert _parse_pages("1-100", 5) == [1, 2, 3, 4, 5]

    def test_clamps_low(self):
        assert _parse_pages("0-3", 5) == [1, 2, 3]

    def test_reversed_range(self):
        assert _parse_pages("5-3", 10) == [3, 4, 5]

    def test_whitespace_tolerant(self):
        assert _parse_pages("1, 3, 5-7", 10) == [1, 3, 5, 6, 7]

    def test_invalid_ignored(self):
        assert _parse_pages("1,abc,3", 10) == [1, 3]

    def test_all_out_of_range(self):
        assert _parse_pages("100,200", 5) == []

    def test_dedupes(self):
        assert _parse_pages("1-3,2,3", 10) == [1, 2, 3]


# ─── _dehyphenate_join ────────────────────────────────────────────────────

class TestDehyphenate:
    def test_soft_hyphen_lowercase(self):
        joined, did = _dehyphenate_join("opti-", "mization")
        assert joined == "optimization" and did is True

    def test_keeps_hyphen_when_uppercase(self):
        # Real hyphenated term / proper noun: next line starts uppercase.
        joined, did = _dehyphenate_join("cross-", "Platform")
        assert did is False
        assert "cross" in joined and "Platform" in joined

    def test_normal_join(self):
        joined, did = _dehyphenate_join("hello", "world")
        assert joined == "hello world" and did is False

    def test_standalone_hyphen_not_joined(self):
        # "- " (hyphen with space before) is not a soft hyphen.
        joined, did = _dehyphenate_join("item -", "done")
        assert did is False


# ─── _table_to_markdown ───────────────────────────────────────────────────

class TestTableToMarkdown:
    def test_basic_markdown_table(self):
        md = _table_to_markdown([["A", "B"], ["1", "2"], ["3", "4"]])
        assert "| A | B |" in md
        assert "| --- | --- |" in md
        assert "| 1 | 2 |" in md
        assert "| 3 | 4 |" in md

    def test_empty_table(self):
        assert _table_to_markdown([]) == ""
        assert _table_to_markdown([["", ""], ["", ""]]) == ""

    def test_none_cells_become_empty(self):
        md = _table_to_markdown([["A", None], [None, "2"]])
        assert "| A |  |" in md
        assert "|  | 2 |" in md

    def test_ragged_rows_padded(self):
        md = _table_to_markdown([["A", "B", "C"], ["1", "2"]])
        # second row padded to 3 cols
        assert "| 1 | 2 |  |" in md

    def test_text_mode_no_separator(self):
        md = _table_to_markdown([["A", "B"], ["1", "2"]], text_mode=True)
        assert "---" not in md
        assert "A | B" in md

    def test_internal_newlines_collapsed(self):
        md = _table_to_markdown([["line1\nline2", "x"]])
        assert "line1 line2" in md
        assert "\n" not in md.split("|")[1]  # no raw newline inside the cell


# ─── _heading_level ───────────────────────────────────────────────────────

class TestHeadingLevel:
    def _chars(self, size, bold=False, text="A Heading"):
        fn = "Helvetica-Bold" if bold else "Helvetica"
        return [{"size": size, "fontname": fn, "text": t} for t in text]

    def test_h1(self):
        assert _heading_level(24, 12, False, "Title") == 1

    def test_h2(self):
        assert _heading_level(19, 12, False, "Section") == 2  # 1.58x

    def test_h3(self):
        assert _heading_level(15.5, 12, False, "Subsection") == 3

    def test_body_is_not_heading(self):
        assert _heading_level(12, 12, False, "Normal body text") == 0

    def test_bold_bumps_up(self):
        # 1.4x bold -> would be h3, bumped to h2
        assert _heading_level(16.8, 12, True, "Bold Subsection") == 2

    def test_sentence_end_not_heading(self):
        assert _heading_level(24, 12, False, "This is a sentence.") == 0

    def test_too_long_not_heading(self):
        assert _heading_level(24, 12, False, "x" * 250) == 0

    def test_zero_body_size(self):
        assert _heading_level(24, 0, False, "Title") == 0


# ─── _format_metadata ─────────────────────────────────────────────────────

class TestFormatMetadata:
    def test_full_header(self):
        lines = _format_metadata({
            "Title": "My Paper", "Author": "Jane Doe",
            "CreationDate": "D:20240115120000Z", "Subject": "Subj", "Keywords": "k1, k2",
        })
        assert lines[0] == "# My Paper"
        joined = " ".join(lines)
        assert "Author: Jane Doe" in joined
        assert "Date: 2024-01-15" in joined
        assert "Subject: Subj" in joined
        assert "Keywords: k1, k2" in joined

    def test_missing_fields_omitted(self):
        lines = _format_metadata({"Title": "Only Title"})
        assert lines == ["# Only Title"]

    def test_no_metadata_empty(self):
        assert _format_metadata({}) == []

    def test_date_without_d_prefix(self):
        lines = _format_metadata({"CreationDate": "20200805"})
        assert any("Date: 2020-08-05" in l for l in lines)


# ─── extract_pdf on real fixtures ─────────────────────────────────────────

class TestExtractPdfFixtures:
    def test_text_pdf_extracts_with_table(self):
        body = open(FIX_TEXT, "rb").read()
        r = extract_pdf(body)
        assert r.error == ""
        assert r.scanned is False
        assert r.pages_total == 1
        assert r.pages_extracted == [1]
        assert r.content
        assert "| --- |" in r.content[0]  # a markdown table was rendered
        assert len(r.content[0]) > 500

    def test_scanned_pdf_detected(self):
        body = open(FIX_SCANNED, "rb").read()
        r = extract_pdf(body)
        assert r.scanned is True
        assert r.error.startswith("scanned_pdf")
        assert "OCR" in r.content[0]

    def test_pages_subset_out_of_range(self):
        body = open(FIX_TEXT, "rb").read()  # 1 page
        r = extract_pdf(body, pages="5-6")
        assert r.error.startswith("no_pages_in_range")
        assert r.pages_total == 1

    def test_pages_subset_valid(self):
        body = open(FIX_TEXT, "rb").read()
        r = extract_pdf(body, pages="1")
        assert r.error == ""
        assert r.pages_extracted == [1]

    def test_not_a_pdf(self):
        r = extract_pdf(b"this is not a pdf at all")
        assert r.error.startswith("not_a_pdf")

    def test_empty_body(self):
        r = extract_pdf(b"")
        assert r.error == "empty or non-bytes PDF body"

    def test_text_mode_drops_markdown_table_syntax(self):
        body = open(FIX_TEXT, "rb").read()
        r = extract_pdf(body, extraction_type="text")
        assert r.error == ""
        # text mode keeps the table content but without the markdown separator row
        assert "| --- |" not in r.content[0]

    def test_encrypted_detected(self, monkeypatch):
        """A password error from pdfplumber is reported as encrypted_pdf."""
        class FakeOpen:
            def __init__(self, *a, **k):
                raise Exception("Password required to open this PDF")
        class FakePdfplumber:
            @staticmethod
            def open(*a, **k):
                raise FakeOpen(*a, **k)
        monkeypatch.setattr("master_fetch.pdf_extractor._get_pdfplumber", lambda: FakePdfplumber)
        r = extract_pdf(b"%PDF-1.4 fake")
        assert r.encrypted is True
        assert r.error.startswith("encrypted_pdf")

    def test_pdfplumber_missing_raises_importerror(self, monkeypatch):
        def raise_import():
            raise ImportError("pdfplumber not installed")
        monkeypatch.setattr("master_fetch.pdf_extractor._get_pdfplumber", raise_import)
        with pytest.raises(ImportError):
            extract_pdf(b"%PDF-1.4 fake")


# ─── Integration: _extract_pdf_response ───────────────────────────────────

class TestExtractPdfResponse:
    def test_text_pdf_response_model(self):
        body = open(FIX_TEXT, "rb").read()
        r = _extract_pdf_response(body, "application/pdf", len(body),
                                  "https://example.com/x.pdf", "markdown", "http", 123.0)
        assert isinstance(r, ResponseModel)
        assert r.status == 200
        assert r.content_type == "application/pdf"
        assert r.extracted_type == "markdown"
        assert r.error == ""
        assert r.content

    def test_scanned_pdf_response_uses_ocr_or_deadend(self):
        """Scanned PDFs auto-OCR when the OCR extras are installed; otherwise
        they return an honest dead-end pointing to `hound-mcp[all]`."""
        from master_fetch.ocr import ocr_available
        body = open(FIX_SCANNED, "rb").read()
        r = _extract_pdf_response(body, "application/pdf", len(body),
                                  "https://example.com/x.pdf", "markdown", "http", 0)
        if ocr_available():
            assert r.error == ""
            assert r.content and "Dummy PDF file" in r.content[0]
        else:
            assert r.error.startswith("scanned_pdf")
            assert "hound-mcp[all]" in r.error

    def test_pdf_deps_missing_response(self, monkeypatch):
        """When pdfplumber isn't installed, the response carries a clear error."""
        def fake_extract(*a, **k):
            raise ImportError("pdfplumber not installed")
        monkeypatch.setattr("master_fetch.pdf_extractor.extract_pdf", fake_extract)
        r = _extract_pdf_response(b"%PDF-1.4 fake", "application/pdf", 14,
                                  "https://x.com/a.pdf", "markdown", "http", 0)
        assert r.error.startswith("pdf_deps_missing")
        assert "hound-mcp[all]" in r.content[0]


# ─── Dispatch + cache-key integration ─────────────────────────────────────

class TestPdfDispatchAndCache:
    @pytest.mark.asyncio
    async def test_dispatch_threads_pages_and_password(self):
        srv = MasterFetchServer()
        srv.smart_fetch = AsyncMock(return_value=ResponseModel(
            status=200, content=["x"], url="https://x.com/a.pdf", fetcher_used="http"))
        await srv._dispatch("mcp_smart_fetch", {
            "url": "https://x.com/a.pdf", "pages": "1-2", "password": "secret",
        })
        _, kw = srv.smart_fetch.call_args
        assert kw["pages"] == "1-2"
        assert kw["password"] == "secret"

    @pytest.mark.asyncio
    async def test_dispatch_pages_via_options_fallback(self):
        srv = MasterFetchServer()
        srv.smart_fetch = AsyncMock(return_value=ResponseModel(
            status=200, content=["x"], url="https://x.com/a.pdf", fetcher_used="http"))
        await srv._dispatch("mcp_smart_fetch", {
            "url": "https://x.com/a.pdf", "options": {"pages": "3"},
        })
        _, kw = srv.smart_fetch.call_args
        assert kw["pages"] == "3"

    def test_cache_key_includes_pages(self):
        k1 = _cache_key("https://x.com/a.pdf", "markdown", None, pages="1-2")
        k2 = _cache_key("https://x.com/a.pdf", "markdown", None, pages="3-4")
        k_all = _cache_key("https://x.com/a.pdf", "markdown", None, pages=None)
        assert k1 != k2
        assert k1 != k_all
        # Non-PDF callers (pages=None) get the same key as before the pages param existed.
        assert k_all == _cache_key("https://x.com/a.pdf", "markdown", None)

    def test_smart_fetch_def_has_pages_and_password(self):
        srv = MasterFetchServer()
        defs = {d["name"]: d for d in srv._TOOL_DEFS}
        props = defs["mcp_smart_fetch"]["inputSchema"]["properties"]
        assert "pages" in props and "password" in props
        assert "PDF" in props["pages"]["description"]

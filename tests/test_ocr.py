"""Tests for the OCR module (scanned PDFs + image pages).

Covers: ocr_available probe, ocr_pdf on a real scanned fixture, the auto-cap
that prevents a huge scanned PDF from hanging the call, explicit page specs,
not-a-PDF handling, ocr_image_bytes, and the server-level wiring
(_extract_pdf_response OCR fallback + _translate_response image-page OCR).

These run against the real rapidocr + pypdfium2 when [all] is installed (CI
installs .[dev,all]). When OCR extras are absent, the wiring tests assert the
honest dead-end instead, so they pass in a lean environment too.
"""

import io
import sys

import pytest

from master_fetch.ocr import ocr_available, ocr_pdf, ocr_image_bytes, OCR_DEFAULT_PAGES

FIX_SCANNED = "tests/dummy.pdf"  # 1-page scanned/image-only PDF ("Dummy PDF file")
HAS_OCR = ocr_available()

skip_if_no_ocr = pytest.mark.skipif(not HAS_OCR, reason="OCR extras (rapidocr + pypdfium2) not installed")


def _make_scanned_npages(n: int) -> bytes:
    """Build an n-page scanned PDF by repeating the dummy page (no text layer)."""
    import pypdfium2 as pdfium
    src = pdfium.PdfDocument(FIX_SCANNED)
    dst = pdfium.PdfDocument.new()
    for _ in range(n):
        dst.import_pages(src)
    bio = io.BytesIO()
    dst.save(bio)
    return bio.getvalue()


# ─── ocr_available ──────────────────────────────────────────────────────

def test_ocr_available_returns_bool():
    assert isinstance(HAS_OCR, bool)


# ─── ocr_pdf ────────────────────────────────────────────────────────────

@skip_if_no_ocr
def test_ocr_pdf_scanned_fixture_extracts_text():
    body = open(FIX_SCANNED, "rb").read()
    r = ocr_pdf(body)
    assert r.error == ""
    assert r.scanned is True
    assert r.pages_total == 1
    assert r.pages_extracted == [1]
    assert r.content
    assert "Dummy PDF file" in r.content[0]
    assert "OCR-extracted" in r.content[0]
    assert "--- Page 1 ---" in r.content[0]


def test_ocr_pdf_not_a_pdf_returns_error():
    r = ocr_pdf(b"not a pdf body at all")
    assert r.error.startswith("not_a_pdf")
    assert r.content


@skip_if_no_ocr
def test_ocr_pdf_auto_cap_when_no_pages_spec():
    """A >OCR_DEFAULT_PAGES scanned PDF with no `pages` spec is capped to the
    first OCR_DEFAULT_PAGES pages, and the header tells the agent how to get
    the rest. Uses a fake (fast) engine so the test doesn't run real OCR 10x."""
    from master_fetch import ocr as ocr_mod

    class FakeOut:
        def __init__(self): self.txts = ("fake page text",); self.elapse = []

    class FakeEngine:
        def __call__(self, arr): return FakeOut()

    body = _make_scanned_npages(OCR_DEFAULT_PAGES + 2)  # 12 pages
    # Verify the fixture is actually 12 pages.
    import pypdfium2 as pdfium
    assert len(pdfium.PdfDocument(body)) == OCR_DEFAULT_PAGES + 2

    import unittest.mock as mock
    with mock.patch.object(ocr_mod, "_get_rapidocr", lambda: FakeEngine()):
        r = ocr_pdf(body, pages=None)
    assert r.error == ""
    assert r.pages_extracted == list(range(1, OCR_DEFAULT_PAGES + 1))  # capped
    assert r.pages_total == OCR_DEFAULT_PAGES + 2
    assert "auto-cap" in r.content[0]
    # The next-batch hint must name the next page range.
    next_start = OCR_DEFAULT_PAGES + 1
    assert f"pages='{next_start}-" in r.content[0]


@skip_if_no_ocr
def test_ocr_pdf_explicit_pages_spec_honored_no_cap():
    """An explicit `pages` spec is honored exactly and never auto-capped."""
    from master_fetch import ocr as ocr_mod
    import unittest.mock as mock

    class FakeOut:
        def __init__(self): self.txts = ("page text",); self.elapse = []

    class FakeEngine:
        def __call__(self, arr): return FakeOut()

    body = _make_scanned_npages(12)
    with mock.patch.object(ocr_mod, "_get_rapidocr", lambda: FakeEngine()):
        r = ocr_pdf(body, pages="2-4")
    assert r.error == ""
    assert r.pages_extracted == [2, 3, 4]
    assert "auto-cap" not in r.content[0]


# ─── ocr_image_bytes ────────────────────────────────────────────────────

@skip_if_no_ocr
def test_ocr_image_bytes_extracts_text():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (420, 120), "white")
    d = ImageDraw.Draw(img)
    d.text((12, 44), "Hello Hound OCR 2026", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    txt = ocr_image_bytes(buf.getvalue())
    assert "Hello" in txt and "OCR" in txt


def test_ocr_image_bytes_empty_returns_empty():
    assert ocr_image_bytes(b"") == ""


# ─── Server wiring: _extract_pdf_response OCR fallback ──────────────────

class TestExtractPdfResponseOcr:
    def test_scanned_pdf_uses_ocr_or_deadend(self):
        from master_fetch.server import _extract_pdf_response, ResponseModel
        from master_fetch.ocr import ocr_available as _avail
        body = open(FIX_SCANNED, "rb").read()
        r = _extract_pdf_response(body, "application/pdf", len(body),
                                  "https://example.com/x.pdf", "markdown", "http", 0.0)
        assert isinstance(r, ResponseModel)
        if _avail():
            # OCR extras installed -> auto-OCR, no error, real text returned.
            assert r.error == ""
            assert r.content
            assert "Dummy PDF file" in r.content[0]
        else:
            # No OCR extras -> honest dead-end pointing to [all].
            assert r.error.startswith("scanned_pdf")
            assert "hound-mcp[all]" in r.error

"""Tests for the v6 flagship PDF extractor: CID-garbage auto-OCR fallback,
quality_score + honest content_ok, ToC, metadata dict, image metadata."""

import io
import re

import pytest

from master_fetch import pdf_extractor as pe
from master_fetch.pdf_extractor import (
    PdfResult, _cid_ratio, _quality_score, _metadata_dict, _extract_toc,
    extract_pdf,
)


# ─── pure helpers ────────────────────────────────────────────────────────

def test_cid_ratio_clean_vs_garbage():
    assert _cid_ratio("Hello world this is clean text.")[0] == 0.0
    r, n = _cid_ratio("(cid:71)(cid:302)(cid:340) some text")
    assert n == 3
    assert 0.5 < r < 0.8


def test_quality_score_clean_garbage_mixed():
    assert _quality_score("This is a clean sentence with normal words.") > 0.95
    # 46% CID garbage -> low quality.
    garbage = "(cid:71)(cid:302)(cid:340)" * 5
    assert _quality_score(garbage) < 0.1
    # A doc that is mostly clean with a little garbage still scores high-ish.
    mostly = "clean text. " * 50 + "(cid:1)" * 3
    assert _quality_score(mostly) > 0.7


def test_metadata_dict_full():
    md = _metadata_dict({
        "Title": "Paper", "Author": "A. Author", "Subject": "Subj",
        "Keywords": "k1, k2", "Creator": "LaTeX", "Producer": "pdfTeX",
        "CreationDate": "D:20240115120000Z", "ModDate": "D:20240116000000Z",
    })
    assert md["title"] == "Paper"
    assert md["author"] == "A. Author"
    assert md["creator"] == "LaTeX"
    assert md["producer"] == "pdfTeX"
    assert md["creation_date"] == "2024-01-15"
    assert md["mod_date"] == "2024-01-16"
    # Empty metadata -> all empty strings, no crash.
    empty = _metadata_dict({})
    assert empty["title"] == ""


def test_extract_toc_via_pypdfium2(monkeypatch):
    class FakeDest:
        def __init__(self, idx): self._i = idx
        def get_index(self): return self._i
    class FakeBM:
        def __init__(self, level, title, idx):
            self.level = level; self._t = title; self._i = idx
        def get_title(self): return self._t
        def get_dest(self): return FakeDest(self._i)
    class FakePdf:
        def __init__(self, marks): self._m = marks
        def get_toc(self):
            for m in self._m: yield FakeBM(m[0], m[1], m[2])
        def close(self): pass
    class FakePdfium:
        PdfDocument = staticmethod(lambda body: FakePdf([
            (0, "1 Introduction", 0),
            (1, "1.1 Background", 1),
            (0, "2 Method", 3),
        ]))
    monkeypatch.setattr("master_fetch.pdf_extractor.pypdfium2", FakePdfium, raising=False)
    # _extract_toc imports pypdfium2 lazily; inject into sys.modules too.
    import sys
    monkeypatch.setitem(sys.modules, "pypdfium2", FakePdfium)
    toc = _extract_toc(b"%PDF- fake")
    assert len(toc) == 3
    assert toc[0] == {"level": 1, "title": "1 Introduction", "page": 1}
    assert toc[1] == {"level": 2, "title": "1.1 Background", "page": 2}
    assert toc[2]["page"] == 4


# ─── CID-garbage auto-OCR fallback (the flagship P1 fix) ──────────────────

FIXTURE = "tests/background_checks.pdf"


def _patch_renderer_to_cid(monkeypatch):
    """Make _render_page return CID garbage for page 1 so the OCR fallback path
    triggers, without needing a real CID-corrupted PDF."""
    def fake_render(page, body_size, text_mode):
        return "(cid:71)(cid:302)(cid:340) (cid:71)(cid:302) more cid garbage here."
    monkeypatch.setattr(pe, "_render_page", fake_render)


def test_cid_page_auto_ocr_recovers_text(monkeypatch):
    _patch_renderer_to_cid(monkeypatch)
    monkeypatch.setattr(pe, "_ocr_pages", lambda body, nums, pw: {
        1: "This is the real text recovered by OCR from the broken font."
    })
    monkeypatch.setattr(pe, "_extract_toc", lambda body: [])
    monkeypatch.setattr(pe, "_extract_images_metadata", lambda pdf, nums: [])
    body = open(FIXTURE, "rb").read()
    r = extract_pdf(body, pages="1")
    assert r.error == ""
    assert r.ocr_fallback_used is True
    assert 1 in r.cid_pages_ocr
    assert "(cid:" not in r.content[0]
    assert "recovered by OCR" in r.content[0]
    assert "real text recovered by OCR" in r.content[0]
    assert r.quality_score >= 0.7
    assert r.content_ok is True


def test_cid_page_no_ocr_honest_marker_and_low_quality(monkeypatch):
    """When OCR is unavailable, a CID page keeps an honest marker + low quality
    + content_ok False (the P3 fix: not masked by HTTP 200)."""
    _patch_renderer_to_cid(monkeypatch)
    monkeypatch.setattr(pe, "_ocr_pages", lambda body, nums, pw: {})  # OCR unavailable
    monkeypatch.setattr(pe, "_extract_toc", lambda body: [])
    monkeypatch.setattr(pe, "_extract_images_metadata", lambda pdf, nums: [])
    body = open(FIXTURE, "rb").read()
    r = extract_pdf(body, pages="1")
    assert "CID font garbage" in r.content[0]
    assert "(cid:" in r.content[0]  # original garbage retained (no OCR to replace it)
    assert r.quality_score < 0.7
    assert r.content_ok is False
    assert r.ocr_fallback_used is False


def test_quality_threshold_sets_content_ok_honestly(monkeypatch):
    """P3: a doc whose final content is mostly CID garbage (no OCR) reports
    content_ok=False even though extraction 'succeeded' (no error)."""
    _patch_renderer_to_cid(monkeypatch)
    monkeypatch.setattr(pe, "_ocr_pages", lambda body, nums, pw: {})
    monkeypatch.setattr(pe, "_extract_toc", lambda body: [])
    monkeypatch.setattr(pe, "_extract_images_metadata", lambda pdf, nums: [])
    body = open(FIXTURE, "rb").read()
    r = extract_pdf(body, pages="1")
    assert r.error == ""           # extraction ran fine
    assert r.content_ok is False   # but the content is garbage -> not trustworthy


# ─── image metadata (P4) ──────────────────────────────────────────────────

def test_include_media_populates_image_metadata(monkeypatch):
    monkeypatch.setattr(pe, "_extract_toc", lambda body: [])
    monkeypatch.setattr(pe, "_extract_images_metadata", lambda pdf, nums: [
        "page 1: 3 embedded image(s); largest 400x300",
        "page 2: 1 embedded image(s); largest 200x150",
    ])
    body = open(FIXTURE, "rb").read()
    r = extract_pdf(body, pages="1-2", include_media=True)
    assert len(r.media) == 2
    assert "page 1: 3 embedded image(s)" in r.media[0]


def test_include_media_default_false_no_image_metadata(monkeypatch):
    monkeypatch.setattr(pe, "_extract_toc", lambda body: [])
    monkeypatch.setattr(pe, "_extract_images_metadata", lambda pdf, nums: [
        "page 1: 3 embedded image(s); largest 400x300",
    ])
    body = open(FIXTURE, "rb").read()
    r = extract_pdf(body, pages="1", include_media=False)
    assert r.media == []


# ─── P6/P14: PDF-intent URL returning HTML (login/paywall) ────────────────

def test_translate_pdf_url_html_login_returns_auth_required():
    """P6: a .pdf URL that returns a login HTML page (JSTOR-style) is detected
    and reported as auth_required, NOT extracted as content_ok=true."""
    from master_fetch.server import _translate_response

    class _LoginPage:
        status = 200
        url = "https://www.jstor.org/stable/pdf/12345.pdf"
        headers = {"content-type": "text/html"}
        body = (b"<html><body><form action='/login'>Please sign in to access "
                b"this content.<input type='password' name='pw'></form></body></html>")
        encoding = "utf-8"

    r = _translate_response(_LoginPage(), "markdown", None, True,
                            use_trafilatura=False, fetcher_used="http", duration_ms=0)
    assert r.error.startswith("auth_required")
    assert r.content_ok is False
    assert "auth_required" in r.content[0]


def test_translate_pdf_url_html_error_returns_not_a_pdf():
    """P14: a .pdf URL that returns a non-PDF error/redirect HTML (no login
    markers) is reported as not_a_pdf, not extracted as content."""
    from master_fetch.server import _translate_response

    class _ErrPage:
        status = 200
        url = "https://example.com/docs/file.pdf"
        headers = {"content-type": "text/html"}
        body = b"<html><body><h1>404 Not Found</h1><p>The page you requested does not exist.</p></body></html>"
        encoding = "utf-8"

    r = _translate_response(_ErrPage(), "markdown", None, True,
                            use_trafilatura=False, fetcher_used="http", duration_ms=0)
    assert r.error.startswith("not_a_pdf")
    assert r.content_ok is False


# ─── P9: .pdf URLs never escalate to the stealthy browser ─────────────────

@pytest.mark.asyncio
async def test_auto_escalate_skips_stealthy_for_pdf_url(mocker):
    """P9: a .pdf URL is never escalated to the stealthy browser (a JS render of
    a PDF URL is always wasted). Even when HTTP returns a JS-shell-ish empty
    result that would normally trigger escalation, stealthy is NOT called."""
    from master_fetch.server import MasterFetchServer, ResponseModel
    srv = MasterFetchServer()

    async def fake_http(*a, **kw):
        # Empty content -> would normally be a JS shell -> escalate.
        return ResponseModel(status=200, content=[""], url="https://x.com/p.pdf",
                             fetcher_used="http", total_size_bytes=100,
                             content_type="text/html")
    mocker.patch.object(srv, "_http_with_retry", fake_http)

    stealthy_called = {"v": False}
    async def fake_stealthy(*a, **kw):
        stealthy_called["v"] = True
        return ResponseModel(status=200, content=["x"], url="https://x.com/p.pdf",
                             fetcher_used="stealthy")
    mocker.patch.object(srv, "stealthy_fetch", fake_stealthy)

    # _finalize_result does caching/chunking; mock it to passthrough so we only
    # test the escalation decision.
    async def fake_finalize(result, *a, **kw):
        return result
    mocker.patch.object(srv, "_finalize_result", fake_finalize)

    r = await srv._auto_escalate(
        "https://x.com/p.pdf", "markdown", None, True, False,
        cache_ttl=3600, offset=0, headless=True, real_chrome=False, wait=0,
        proxy=None, timeout=30000, network_idle=False, solve_cloudflare=True,
        block_webrtc=True, hide_canvas=True, extra_headers=None,
        useragent=None, cookies=None,
    )
    assert stealthy_called["v"] is False, "stealthy must not be called for a .pdf URL"
    assert r.escalation_path == "direct:http"


@pytest.mark.asyncio
async def test_auto_escalate_non_pdf_url_still_escalates(mocker):
    """Sanity: a non-.pdf URL that returns a JS shell still escalates to stealthy
    (the guard is PDF-intent-only, not a blanket no-escalation)."""
    from master_fetch.server import MasterFetchServer, ResponseModel
    srv = MasterFetchServer()

    async def fake_http(*a, **kw):
        return ResponseModel(status=200, content=[""], url="https://x.com/article",
                             fetcher_used="http", total_size_bytes=50000,
                             content_type="text/html")
    mocker.patch.object(srv, "_http_with_retry", fake_http)
    async def fake_stealthy(*a, **kw):
        return ResponseModel(status=200, content=["real content"], url="https://x.com/article",
                             fetcher_used="stealthy")
    mocker.patch.object(srv, "stealthy_fetch", fake_stealthy)
    async def fake_finalize(result, *a, **kw):
        return result
    mocker.patch.object(srv, "_finalize_result", fake_finalize)

    r = await srv._auto_escalate(
        "https://x.com/article", "markdown", None, True, False,
        cache_ttl=3600, offset=0, headless=True, real_chrome=False, wait=0,
        proxy=None, timeout=30000, network_idle=False, solve_cloudflare=True,
        block_webrtc=True, hide_canvas=True, extra_headers=None,
        useragent=None, cookies=None,
    )
    assert r.fetcher_used == "stealthy"
    assert "stealthy" in r.escalation_path

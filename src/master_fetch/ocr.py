"""OCR for Hound — turns image-only PDFs and image pages into text an agent
can read, using only pure-pip deps (no system binaries).

Two engines, both pip-installable with bundled native libs / models:
  * ``pypdfium2`` — Google's PDFium bindings (BSD-3). Renders a PDF page to a
    PIL/numpy image. No poppler / system binary.
  * ``rapidocr`` v3 — ONNX OCR (Apache-2.0 models from PaddleOCR). Bundles its
    detection / recognition ONNX models in the wheel and supports Python 3.13+.
    Falls back to the legacy ``rapidocr-onnxruntime`` v1 if that's what's
    installed. No system binary, no model download.

Why this exists: hound's ``pdf_extractor`` returns an honest ``scanned_pdf``
dead-end for image-only PDFs ("no extractable text"). That is correct but
useless to the agent. When the OCR extras are installed, the PDF path falls
back to rendering + OCR so the agent actually gets the text. Image-only web
pages (content-type ``image/*``) are OCR'd too.

Agent design:
  * Auto-OCR: if a PDF is detected as scanned AND rapidocr is installed, OCR
    runs automatically. No new param for the agent to learn.
  * Page cap when no ``pages`` spec: OCR is slow (~2-4s/page). To prevent a
    100-page scanned PDF hanging the call, OCR defaults to the first
    ``OCR_DEFAULT_PAGES`` pages when the agent did not request a specific
    range, and ``next_action`` tells the agent how to get the rest
    (``pages='11-20'``). An explicit ``pages`` spec is always honored exactly.
  * Output shape matches ``pdf_extractor``: a metadata header + ``--- Page N
    ---`` markers, so OCR'd and text-extracted PDFs read the same to the agent.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any, Optional, Union

logger = logging.getLogger("master-fetch.ocr")

# Cap OCR to this many pages when the caller did not specify a page range.
# Prevents a huge scanned PDF from hanging the call for minutes. An explicit
# `pages` spec always overrides this.
OCR_DEFAULT_PAGES = 10

# Render scale for PDFium. 2.0 ≈ 144 dpi (enough for RapidOCR on typical docs);
# bumped to 2.5 for better accuracy on small fonts without much slowdown.
_RENDER_SCALE = 2.5


def _get_pdfium():
    """Lazy import pypdfium2 (optional [all] dependency)."""
    try:
        import pypdfium2 as pdfium  # type: ignore
        return pdfium
    except ImportError as e:
        raise ImportError(
            "OCR requires pypdfium2. Run: pip install hound-mcp[all]"
        ) from e


def _get_rapidocr():
    """Lazy import RapidOCR. Prefers the unified ``rapidocr`` v3 package (supports
    Python 3.13+, models bundled in the wheel); falls back to the legacy
    ``rapidocr-onnxruntime`` v1 package (Python <3.13 only)."""
    try:
        from rapidocr import RapidOCR  # type: ignore  # v3
        return RapidOCR()
    except ImportError:
        pass
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore  # v1 fallback
        return RapidOCR()
    except ImportError as e:
        raise ImportError(
            "OCR requires rapidocr. Run: pip install hound-mcp[all]"
        ) from e


def ocr_available() -> bool:
    """True if an OCR engine + PDFium are importable. Cheap probe (no model load)."""
    try:
        import pypdfium2  # noqa: F401
    except Exception:
        return False
    try:
        import rapidocr  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def _ocr_ndarray(engine: Any, arr) -> str:
    """Run RapidOCR on a numpy/PIL image and return joined text lines.

    Handles both API shapes: v3 returns a RapidOCROutput with ``.txts``; v1
    returns ``(list[[box, text, score]], elapse)``.
    """
    out = engine(arr)
    if out is None:
        return ""
    # v3: RapidOCROutput.txts is a tuple of strings.
    if hasattr(out, "txts"):
        txts = out.txts or ()
        return "\n".join(t for t in txts if t).strip()
    # v1: (list[[box, text, score]], elapse)
    if isinstance(out, tuple) and len(out) >= 1:
        res = out[0]
        if not res:
            return ""
        return "\n".join(r[1] for r in res if r and len(r) > 1 and r[1]).strip()
    return ""


def ocr_image_bytes(data: bytes) -> str:
    """OCR a raw image (PNG/JPEG/etc bytes). Returns extracted text or ''.

    Used for image-only web pages (content-type image/*).
    """
    if not data:
        return ""
    engine = _get_rapidocr()
    try:
        from PIL import Image  # type: ignore
        import numpy as np
    except ImportError as e:
        raise ImportError("OCR requires Pillow + numpy (in rapidocr-onnxruntime).") from e
    img = Image.open(io.BytesIO(data)).convert("RGB")
    return _ocr_ndarray(engine, np.array(img))


def ocr_pdf(
    body: bytes,
    pages: Optional[str] = None,
    password: Optional[str] = None,
) -> "PdfResult":
    """OCR a scanned/image-only PDF. Returns a PdfResult.

    On success: ``content`` is a one-element markdown string with a header +
    per-page ``--- Page N ---`` markers, ``scanned=True``, ``error=""``. When
    the auto-cap kicked in (no ``pages`` spec + > ``OCR_DEFAULT_PAGES`` pages),
    the header tells the agent how to fetch the next batch.
    On failure: ``error`` is set and ``content`` is a human-readable explanation.
    """
    from master_fetch.pdf_extractor import PdfResult, _parse_pages  # reuse

    if not body or not body[:5].startswith(b"%PDF"):
        return PdfResult(error="not_a_pdf: body does not start with %PDF",
                         content=["[Body is not a PDF despite content-type.]"])

    pdfium = _get_pdfium()
    try:
        import numpy as np
    except ImportError as e:
        raise ImportError("OCR requires numpy (in rapidocr-onnxruntime).") from e

    # pypdfium2 raises on a wrong password; surface an encrypted_pdf signal.
    try:
        pdf = pdfium.PdfDocument(body, password=password or None)
    except Exception as e:
        msg = str(e).lower()
        if "password" in msg or "encrypt" in msg:
            return PdfResult(encrypted=True,
                             error="encrypted_pdf: this PDF is password-protected; "
                                   "pass a password via the 'password' option",
                             content=["[Encrypted PDF — pass a password to OCR it.]"])
        return PdfResult(error=f"pdf_open_failed: {str(e)[:200]}",
                         content=[f"[Could not open PDF for OCR: {str(e)[:200]}]"])

    try:
        total_pages = len(pdf)
        if total_pages == 0:
            return PdfResult(error="empty_pdf: no pages", content=["[PDF has no pages.]"])

        requested = _parse_pages(pages, total_pages)
        if not requested:
            return PdfResult(pages_total=total_pages,
                             error="no_pages_in_range: the requested page range is out of bounds",
                             content=[f"[No pages in range '{pages}' (PDF has {total_pages} pages).]"])

        # Auto-cap: when the agent did not pass a `pages` spec, OCR only the
        # first OCR_DEFAULT_PAGES pages to avoid a multi-minute hang on a huge
        # scanned PDF. Tell them (via the returned scope line + next_action) how
        # to get the rest.
        capped = False
        if not (pages and pages.strip()):
            if len(requested) > OCR_DEFAULT_PAGES:
                requested = requested[:OCR_DEFAULT_PAGES]
                capped = True

        engine = _get_rapidocr()

        # Best-effort metadata (title etc.) for the header.
        meta = {}
        try:
            meta = pdf.get_metadata_dict() or {}
        except Exception:
            meta = {}
        title = str(meta.get("Title", "") or "").strip()

        rendered: list[str] = []
        for n in requested:
            try:
                page = pdf[n - 1]
                bitmap = page.render(scale=_RENDER_SCALE)
                pil = bitmap.to_pil().convert("RGB")
                text = _ocr_ndarray(engine, np.array(pil))
                if not text:
                    text = "[No text detected on this page.]"
                rendered.append(f"--- Page {n} ---\n\n{text}")
            except Exception as e:  # one page failing shouldn't kill the doc
                logger.debug("OCR page %d failed: %s", n, e)
                rendered.append(f"--- Page {n} ---\n\n[OCR failed on this page: {str(e)[:120]}]")

        header: list[str] = []
        if title:
            header.append(f"# {title}")
        if len(requested) == total_pages:
            scope = f"{total_pages} pages"
        else:
            scope = (f"pages {requested[0]}-{requested[-1]} of {total_pages}"
                     if len(requested) > 1 else f"page {requested[0]} of {total_pages}")
        header.append(f"> OCR-extracted from scanned PDF · {scope}")
        if capped:
            header.append(
                f"> Showing the first {OCR_DEFAULT_PAGES} pages (auto-cap to keep this "
                f"call fast). Pass pages='{requested[-1] + 1}-{requested[-1] + OCR_DEFAULT_PAGES}' "
                f"for the next batch."
            )
        body_md = "\n\n".join(rendered).strip()
        full = ("\n".join(header).strip() + "\n\n" + body_md).strip()

        return PdfResult(
            content=[full],
            title=title,
            pages_total=total_pages,
            pages_extracted=requested,
            scanned=True,
            error="",
        )
    finally:
        try:
            pdf.close()
        except Exception:
            pass

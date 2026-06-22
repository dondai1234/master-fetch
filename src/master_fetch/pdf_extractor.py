"""Flagship PDF extraction for Hound — optimized for AI agents.

Turns a PDF's bytes into clean, structured markdown that an agent can reason
over: a metadata header, multi-column reading order, real tables as markdown
tables, font-size-detected headings, de-hyphenated paragraphs, per-page
markers (for citation), and honest signals for scanned / encrypted PDFs.

Built on ``pdfplumber`` (MIT, itself built on ``pdfminer.six`` — both MIT, no
AGPL). ``pdfplumber`` gives char-level font/size data (for heading detection),
Tabula-inspired table extraction, and layout-aware text — enough to emulate
the markdown output shape of AGPL-locked alternatives (PyMuPDF4LLM) without
the license risk for an MIT project.

Agent design choices:
  * Markdown structure (headings / lists / tables) — agents reason best over it.
  * Page-range param (``pages="1-5"``) — extract only what you need, saving
    tokens + time on 500-page PDFs.
  * A metadata header up top so the agent can decide relevance before reading
    the body.
  * ``--- Page N ---`` markers so the agent can cite pages.
  * Honest ``scanned`` / ``encrypted`` detection with actionable errors instead
    of pretending a near-empty extraction is content.
  * Output is a single markdown string → the existing ``_apply_chunking``
    paginates it via ``offset``/``next_offset`` for huge PDFs.
"""

from __future__ import annotations

import io
import logging
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger("master-fetch.pdf")

# A page is considered scanned/image-only if it yields fewer than this many
# characters of extractable text on average. Tuned for typical text PDFs
# (a half page of text is already ~500 chars).
_SCANNED_CHARS_PER_PAGE = 20

# Word-grouping x tolerance. pdfplumber's default (3) jams words together on
# PDFs that position words with tight inter-word gaps (<3 units, common in
# academic papers). 1.5 splits them correctly while keeping intra-word chars
# joined (intra-word glyph gaps are usually <0.5).
_X_TOL = 1.5

# Heading detection thresholds (ratio of a line's dominant font size to the
# document body size). Conservative: only short, non-sentence lines qualify.
_H1_RATIO = 2.0
_H2_RATIO = 1.55
_H3_RATIO = 1.25
_HEADING_MAX_LEN = 200
_SENTENCE_END = ". , ; : ? ! ) ]".split()


@dataclass
class PdfResult:
    """Result of a PDF extraction. The caller maps this into a ResponseModel."""
    content: list[str] = field(default_factory=list)
    title: str = ""
    author: str = ""
    subject: str = ""
    keywords: str = ""
    pages_total: int = 0
    pages_extracted: list[int] = field(default_factory=list)
    scanned: bool = False
    encrypted: bool = False
    error: str = ""


def _get_pdfplumber():
    """Lazy import pdfplumber (optional [all] dependency)."""
    try:
        import pdfplumber  # type: ignore
        return pdfplumber
    except ImportError as e:
        raise ImportError(
            "PDF extraction requires pdfplumber. Run: pip install hound-mcp[all]"
        ) from e


def _parse_pages(spec: str | None, total: int) -> list[int]:
    """Parse a page spec like '1-5', '1,3,5-7', '1, 2' into a sorted unique
    list of 1-indexed page numbers clamped to [1, total]. None → all pages."""
    if not spec or not spec.strip():
        return list(range(1, total + 1))
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            try:
                lo_i, hi_i = int(lo), int(hi)
            except ValueError:
                continue
            if lo_i > hi_i:
                lo_i, hi_i = hi_i, lo_i
            out.update(range(max(1, lo_i), min(total, hi_i) + 1))
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= total:
                out.add(n)
    return sorted(out)


def _clean_text(s: str) -> str:
    """Normalize whitespace inside a line without collapsing meaningful spaces."""
    return re.sub(r"[ \t]+", " ", s).strip()


def _dehyphenate_join(prev: str, nxt: str) -> tuple[str, bool]:
    """Join two consecutive lines, de-hyphenating a soft hyphen break.

    Returns (joined_text, did_join). Only joins when prev ends with a single
    hyphen AND the next line starts with a lowercase letter — the standard
    signal for a word broken across a line. Leaves real hyphenated terms
    (next line starts uppercase/digit) intact by keeping the hyphen.
    """
    prev = prev.rstrip()
    nxt = nxt.lstrip()
    if prev.endswith("-") and len(prev) >= 2 and prev[-2] != " ":
        # Soft hyphen only if next line begins a lowercase continuation.
        if nxt and nxt[0].islower():
            return prev[:-1] + nxt, True
    return prev + " " + nxt, False


def _table_to_markdown(table: list[list[str | None]], text_mode: bool = False) -> str:
    """Render a pdfplumber table (list of rows of cells) as a markdown table.

    Empty tables render as nothing. ``text_mode`` drops the markdown separator
    row and pipe syntax for plainer output.
    """
    if not table:
        return ""
    # Normalize cells: None -> "", collapse internal newlines to spaces.
    rows = [[("" if cell is None else re.sub(r"\s+", " ", str(cell).strip()))
             for cell in row] for row in table]
    # Drop fully-empty rows.
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    if text_mode:
        return "\n".join(" | ".join(r) for r in rows)
    header = rows[0]
    sep = ["---"] * width
    body = rows[1:] if len(rows) > 1 else []
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join(sep) + " |"]
    for r in body:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _in_bbox(obj: dict, bbox: tuple[float, float, float, float], tol: float = 1.0) -> bool:
    """True if a char/line object's center falls inside a bbox (with tolerance)."""
    x0, top, x1, bottom = bbox
    cx = (obj.get("x0", 0) + obj.get("x1", 0)) / 2
    cy = (obj.get("top", 0) + obj.get("bottom", 0)) / 2
    return (x0 - tol) <= cx <= (x1 + tol) and (top - tol) <= cy <= (bottom + tol)


def _dominant_size(chars: Iterable[dict]) -> float:
    sizes = [c.get("size", 0) for c in chars if c.get("text", "").strip()]
    if not sizes:
        return 0.0
    return statistics.median(sizes)


def _is_bold(chars: Iterable[dict]) -> bool:
    for c in chars:
        fn = (c.get("fontname") or "").lower()
        if "bold" in fn or "black" in fn or "heavy" in fn:
            return True
    return False


def _heading_level(line_size: float, body_size: float, bold: bool, text: str) -> int:
    """Return 1/2/3 for a heading, else 0. Conservative to avoid false positives."""
    if body_size <= 0 or line_size <= 0:
        return 0
    t = text.strip()
    if not t or len(t) > _HEADING_MAX_LEN:
        return 0
    if t.rstrip()[-1:] in _SENTENCE_END:
        return 0
    ratio = line_size / body_size
    level = 0
    if ratio >= _H1_RATIO:
        level = 1
    elif ratio >= _H2_RATIO:
        level = 2
    elif ratio >= _H3_RATIO:
        level = 3
    if bold and level:
        level = max(1, level - 1)  # bold bumps up one
    return level


def _format_metadata(meta: dict) -> list[str]:
    """Build the metadata header lines from pdfplumber's .metadata dict."""
    def g(*keys):
        for k in keys:
            v = meta.get(k)
            if v:
                return str(v)
        return ""
    title = g("Title", "title")
    author = g("Author", "author")
    subject = g("Subject", "subject")
    keywords = g("Keywords", "keywords")
    # PDF dates look like "D:20240115120000Z". Strip the "D:" prefix for readability.
    def clean_date(d: str) -> str:
        if d.startswith("D:"):
            d = d[2:]
        # Best-effort: trim to YYYYMMDD-ish for a compact, readable form.
        m = re.match(r"^(\d{4})(\d{2})(\d{2})", d)
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else d
    created = clean_date(g("CreationDate", "creationdate"))
    out = []
    if title:
        out.append(f"# {title}")
    facts = []
    if author:
        facts.append(f"Author: {author}")
    if created:
        facts.append(f"Date: {created}")
    if subject:
        facts.append(f"Subject: {subject}")
    if keywords:
        facts.append(f"Keywords: {keywords}")
    if facts:
        out.append("> " + " · ".join(facts))
    return out


def extract_pdf(
    body: bytes,
    extraction_type: str = "markdown",
    pages: str | None = None,
    password: str | None = None,
) -> PdfResult:
    """Extract a PDF's bytes into agent-optimized markdown.

    Args:
        body: Raw PDF bytes.
        extraction_type: "markdown" (default) gives full markdown structure;
            "text" gives plainer output. Other types fall back to markdown.
        pages: Optional page spec ("1-5", "1,3,5-7"). None = all pages.
        password: Optional password for encrypted PDFs.

    Returns a PdfResult. Check ``.error`` first — when set, the content is a
    human-readable explanation and should NOT be treated as real PDF content.
    """
    if not body or not isinstance(body, (bytes, bytearray)):
        return PdfResult(error="empty or non-bytes PDF body", content=["[Empty PDF body.]"])
    if not body[:5].startswith(b"%PDF"):
        # Not actually a PDF despite the content-type — let the caller fall back.
        return PdfResult(error="not_a_pdf: body does not start with %PDF",
                         content=["[Body is not a PDF despite content-type.]"])

    pdfplumber = _get_pdfplumber()
    text_mode = extraction_type == "text"

    try:
        pdf = pdfplumber.open(io.BytesIO(body), password=password or "")
    except Exception as e:
        msg = str(e).lower()
        if "password" in msg or "encrypt" in msg or "not supported" in msg:
            return PdfResult(encrypted=True,
                             error="encrypted_pdf: this PDF is password-protected; "
                                   "pass a password via the 'password' option",
                             content=["[Encrypted PDF - pass a password to extract.]"])
        return PdfResult(error=f"pdf_open_failed: {str(e)[:200]}",
                         content=[f"[Could not open PDF: {str(e)[:200]}]"])

    try:
        total_pages = len(pdf.pages)
        if total_pages == 0:
            return PdfResult(error="empty_pdf: no pages", content=["[PDF has no pages.]"])

        page_nums = _parse_pages(pages, total_pages)
        if not page_nums:
            return PdfResult(pages_total=total_pages,
                             error="no_pages_in_range: the requested page range is out of bounds",
                             content=[f"[No pages in range '{pages}' (PDF has {total_pages} pages).]"])

        meta = pdf.metadata or {}

        # --- Pass 1: compute the document body font size from selected pages ---
        body_sizes: list[float] = []
        for n in page_nums:
            p = pdf.pages[n - 1]
            body_sizes.extend(c.get("size", 0) for c in p.chars if c.get("text", "").strip())
        body_size = statistics.median(body_sizes) if body_sizes else 0.0

        # --- Pass 2: render each selected page ---
        rendered_pages: list[str] = []
        total_chars = 0
        for n in page_nums:
            p = pdf.pages[n - 1]
            try:
                page_md = _render_page(p, body_size, text_mode)
            except Exception as e:  # per-page failure shouldn't kill the whole doc
                logger.debug("PDF page %d render failed: %s", n, e)
                page_md = f"--- Page {n} ---\n\n[Failed to render this page: {str(e)[:120]}]"
            rendered_pages.append(f"--- Page {n} ---\n\n{page_md}")
            total_chars += len(page_md)

        # --- Scanned / image-only detection ---
        scanned = total_chars < _SCANNED_CHARS_PER_PAGE * len(page_nums)
        if scanned:
            return PdfResult(
                title=str(meta.get("Title", "") or ""),
                pages_total=total_pages, pages_extracted=page_nums,
                scanned=True,
                error="scanned_pdf: this PDF is image-only (no extractable text). "
                      "Install OCR support with `pip install hound-mcp[all]` and hound "
                      "will auto-OCR scanned PDFs; or use a vision-capable tool.",
                content=["[Scanned/image-only PDF - no extractable text. Install hound-mcp[all] for OCR.]"],
            )

        # --- Assemble: metadata header + pages ---
        header = _format_metadata(meta)
        # Page-count + extracted-range line so the agent knows the scope.
        if len(page_nums) == total_pages:
            scope = f"{total_pages} pages"
        else:
            scope = f"pages {page_nums[0]}–{page_nums[-1]} of {total_pages}" if len(page_nums) > 1 \
                else f"page {page_nums[0]} of {total_pages}"
        header.append(f"> PDF · {scope}")
        body_md = "\n\n".join(rendered_pages).strip()
        full = ("\n".join(header).strip() + "\n\n" + body_md).strip()

        return PdfResult(
            content=[full],
            title=str(meta.get("Title", "") or ""),
            author=str(meta.get("Author", "") or ""),
            subject=str(meta.get("Subject", "") or ""),
            keywords=str(meta.get("Keywords", "") or ""),
            pages_total=total_pages,
            pages_extracted=page_nums,
        )
    finally:
        try:
            pdf.close()
        except Exception:
            pass


def _render_page(page: Any, body_size: float, text_mode: bool) -> str:
    """Render one page to markdown: layout-aware text + tables merged by y-position."""
    # Tables on the FULL page (table regions are defined by the page's lines).
    try:
        tables = page.find_tables(table_settings={"text_x_tolerance": _X_TOL, "text_y_tolerance": 3})
    except Exception:
        tables = []
    table_bboxes = [t.bbox for t in tables] if tables else []

    # Non-table text: filter chars outside every table bbox, then get text lines
    # with positions + chars (for heading detection).
    if table_bboxes:
        filtered = page.filter(
            lambda obj: obj.get("object_type") != "char"
            or not any(_in_bbox(obj, b) for b in table_bboxes)
        )
    else:
        filtered = page

    try:
        # layout=False uses pdfminer's word-grouper (inserts spaces between words
        # by x-gap) while still returning per-line `top` + `chars` for heading
        # detection and table interleaving. layout=True jams words together on
        # PDFs that position words without space glyphs (common in academic PDFs).
        # x_tolerance=1.5 fixes tight-gap PDFs (default 3 jams them).
        text_lines = filtered.extract_text_lines(layout=False, x_tolerance=_X_TOL, return_chars=True)
    except Exception:
        text_lines = []
    # Fallback: if extract_text_lines yielded nothing but the page has chars,
    # use plain extract_text so we don't silently return an empty page.
    if not text_lines and filtered.chars:
        txt = filtered.extract_text(layout=False, x_tolerance=_X_TOL) or ""
        return _dehyphenate_block(txt, text_mode)

    # Build blocks: (top, kind, payload). Tables carry their bbox top.
    blocks: list[tuple[float, str, Any]] = []
    for tl in text_lines:
        txt = _clean_text(tl.get("text", ""))
        if txt:
            blocks.append((float(tl.get("top", 0)), "text",
                           (txt, tl.get("chars", []),
                            float(tl.get("top", 0)), float(tl.get("bottom", 0)))))
    for t in tables:
        try:
            data = t.extract()
        except Exception:
            data = []
        if data and any(any(cell for cell in row) for row in data):
            blocks.append((float(t.bbox[1]), "table", data))

    blocks.sort(key=lambda b: b[0])

    # Paragraph-gap threshold from the page's median line height: a vertical gap
    # larger than ~1.5 lines means a new paragraph (or a heading / block break).
    line_heights = [float(tl.get("bottom", 0)) - float(tl.get("top", 0))
                    for tl in text_lines if tl.get("text", "").strip()]
    median_lh = statistics.median(line_heights) if line_heights else 12.0
    gap_threshold = max(8.0, median_lh * 1.5)

    out: list[str] = []
    para: list[str] = []

    def flush_paragraph():
        if not para:
            return
        joined = para[0]
        for nxt in para[1:]:
            joined, _ = _dehyphenate_join(joined, nxt)
        joined = _clean_text(joined)
        if joined:
            out.append(joined)
        para.clear()

    prev_bottom: float | None = None
    for top, kind, payload in blocks:
        if kind == "table":
            flush_paragraph()
            prev_bottom = None  # a table resets vertical context
            md = _table_to_markdown(payload, text_mode=text_mode)
            if md:
                out.append(md)
            continue
        txt, chars, line_top, line_bottom = payload
        # Per-line heading detection (conservative): a heading is its own block.
        level = _heading_level(_dominant_size(chars), body_size, _is_bold(chars), txt) if chars else 0
        if level and not text_mode:
            flush_paragraph()
            out.append(f"{'#' * level} {txt}")
            prev_bottom = line_bottom
            continue
        # Paragraph break on a vertical gap larger than the threshold.
        if prev_bottom is not None and (line_top - prev_bottom) > gap_threshold:
            flush_paragraph()
        para.append(txt)
        prev_bottom = line_bottom
    flush_paragraph()

    return "\n\n".join(out).strip()


def _dehyphenate_block(text: str, text_mode: bool) -> str:
    """Fallback plain-text rendering (no table/heading structure)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return ""
    joined = lines[0]
    for nxt in lines[1:]:
        joined, _ = _dehyphenate_join(joined, nxt)
    return _clean_text(joined)

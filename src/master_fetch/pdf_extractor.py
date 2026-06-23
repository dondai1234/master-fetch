"""Flagship PDF extraction for Hound — optimized for AI agents, v6.

Turns a PDF's bytes into clean, structured markdown that an agent can reason
over, with HONEST quality signals so the agent can trust the output and a
per-page auto-OCR fallback that recovers text from broken font mappings (the
CID-corruption problem no lightweight extractor handles).

Built on ``pdfplumber`` (MIT, on pdfminer.six — both MIT, no AGPL) for text +
tables + layout + font data, and ``pypdfium2`` (BSD-3) for the PDF outline
(bookmarks/ToC) and for rendering CID-corrupted pages to images so ``rapidocr``
can read the VISIBLE text. All pure-pip, no system binaries, MIT-compatible.

The flagship trick — auto-OCR for CID-corrupted pages:
  Some PDFs embed font subsets without a ToUnicode CMap, so pdfplumber emits
  ``(cid:71)(cid:302)...`` garbage for those fonts (architecture diagrams,
  figures, math). But the glyphs RENDER correctly visually — only the
  text-to-unicode map is broken. So when a page's CID-garbage ratio is high,
  hound renders that page to an image via pypdfium2 and OCRs it with rapidocr,
  recovering the real text. This reuses the OCR deps hound already ships and
  turns the #1 PDF-extraction failure mode (CID garbage) into readable content.
  If OCR extras aren't installed, the page keeps a low quality_score + an honest
  marker so the agent knows to use a vision tool.

Agent design:
  * Markdown structure (headings / lists / tables) — agents reason best over it.
  * A metadata header up top so the agent can decide relevance before reading.
  * ``--- Page N ---`` markers (PDF page labels when available) for citation.
  * ``table_of_contents`` field from the PDF outline tree (books / reports).
  * ``quality_score`` (0-1, readable-char ratio) + honest ``content_ok`` so the
    agent can distinguish a clean extraction from a garbled one (the old
    content_ok=true-despite-corruption bug).
  * ``metadata`` populated with title/author/subject/keywords/creator/producer/
    dates — programmatically available, not just in the markdown header.
  * Page-range param (``pages="1-5"``) — extract only what you need.
  * ``include_media`` -> per-page embedded-image metadata (count + dimensions)
    so multimodal agents know which pages have figures to screenshot.
  * Honest ``scanned`` / ``encrypted`` detection with actionable errors.
"""

from __future__ import annotations

import io
import logging
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger("master-fetch.pdf")

# A page is considered scanned/image-only if it yields fewer than this many
# characters of extractable text on average.
_SCANNED_CHARS_PER_PAGE = 20

# Word-grouping x tolerance. pdfplumber's default (3) jams words together on
# PDFs that position words with tight inter-word gaps. 1.5 splits them correctly.
_X_TOL = 1.5

# Heading detection thresholds (ratio of a line's dominant font size to the
# document body size). Conservative: only short, non-sentence lines qualify.
_H1_RATIO = 2.0
_H2_RATIO = 1.55
_H3_RATIO = 1.25
_HEADING_MAX_LEN = 200
_SENTENCE_END = ". , ; : ? ! ) ]".split()

# CID font garbage: pdfplumber emits "(cid:NN)" for glyphs whose embedded font
# lacks a ToUnicode CMap. When a page's CID-garbage ratio exceeds this, hound
# renders + OCRs that page to recover the real visible text.
_CID_RE = re.compile(r"\(cid:\d+\)")
_CID_RATIO_THRESHOLD = 0.30
# Below this readable-char ratio, content_ok is False (the doc is too garbled to
# trust, even after OCR). Tuned so a doc with one bad page out of many stays ok.
_QUALITY_OK_THRESHOLD = 0.70


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
    # v6 additions:
    metadata: dict[str, Any] = field(default_factory=dict)
    table_of_contents: list[dict] = field(default_factory=list)
    quality_score: float = 0.0
    content_ok: bool = False
    media: list[str] = field(default_factory=list)
    ocr_fallback_used: bool = False
    cid_pages_ocr: list[int] = field(default_factory=list)


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
    list of 1-indexed page numbers clamped to [1, total]. None -> all pages."""
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
    return re.sub(r"[ \t]+", " ", s).strip()


def _dehyphenate_join(prev: str, nxt: str) -> tuple[str, bool]:
    """Join two consecutive lines, de-hyphenating a soft hyphen break."""
    prev = prev.rstrip()
    nxt = nxt.lstrip()
    if prev.endswith("-") and len(prev) >= 2 and prev[-2] != " ":
        if nxt and nxt[0].islower():
            return prev[:-1] + nxt, True
    return prev + " " + nxt, False


def _table_to_markdown(table: list[list[str | None]], text_mode: bool = False) -> str:
    if not table:
        return ""
    rows = [[("" if cell is None else re.sub(r"\s+", " ", str(cell).strip()))
             for cell in row] for row in table]
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
        level = max(1, level - 1)
    return level


def _clean_date(d: str) -> str:
    if not d:
        return ""
    if d.startswith("D:"):
        d = d[2:]
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", d)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else d


def _format_metadata(meta: dict) -> list[str]:
    """Build the markdown metadata header lines from pdfplumber's .metadata."""
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
    created = _clean_date(g("CreationDate", "creationdate"))
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


def _metadata_dict(meta: dict) -> dict[str, Any]:
    """Full structured metadata for the ResponseModel.metadata field (P7)."""
    def g(*keys):
        for k in keys:
            v = meta.get(k)
            if v:
                return str(v)
        return ""
    return {
        "title": g("Title", "title"),
        "author": g("Author", "author"),
        "subject": g("Subject", "subject"),
        "keywords": g("Keywords", "keywords"),
        "creator": g("Creator", "creator"),
        "producer": g("Producer", "producer"),
        "creation_date": _clean_date(g("CreationDate", "creationdate")),
        "mod_date": _clean_date(g("ModDate", "moddate")),
    }


def _extract_toc(body: bytes) -> list[dict]:
    """Extract the PDF outline tree (bookmarks) via pypdfium2 -> list of
    {level, title, page}. Empty for PDFs without an outline (most arxiv papers)."""
    try:
        import pypdfium2 as pdfium  # type: ignore
    except ImportError:
        return []
    try:
        pdf = pdfium.PdfDocument(body)
        toc = []
        for bookmark in pdf.get_toc():
            level = (getattr(bookmark, "level", 0) or 0) + 1  # 0-based -> 1-based
            try:
                title = bookmark.get_title() or ""
            except Exception:
                title = ""
            page = None
            try:
                dest = bookmark.get_dest()
                if dest is not None:
                    idx = dest.get_index()  # 0-based page index
                    if idx is not None:
                        page = int(idx) + 1
            except Exception:
                page = None
            toc.append({"level": int(level), "title": str(title).strip(), "page": page})
        pdf.close()
        return toc
    except Exception as e:
        logger.debug("ToC extraction failed: %s", e)
        return []


def _cid_ratio(text: str) -> tuple[float, int]:
    """Return (ratio_of_cid_garbage_chars, cid_token_count) for a page's text."""
    if not text:
        return (0.0, 0)
    garbage = sum(len(m) for m in _CID_RE.findall(text))
    return (garbage / len(text), len(_CID_RE.findall(text)))


def _quality_score(text: str) -> float:
    """Doc/page readable-char ratio in [0,1]: printable chars minus CID garbage,
    over total chars. ~1.0 = clean; low = garbled (CID garbage not OCR-recovered)."""
    if not text:
        return 0.0
    cid_garbage = sum(len(m) for m in _CID_RE.findall(text))
    clean = _CID_RE.sub("", text)
    printable = sum(1 for ch in clean if ch.isprintable() or ch in "\n\t ")
    # printable is over the cid-stripped text; score it against the ORIGINAL len
    # so cid garbage counts against quality.
    return max(0.0, min(1.0, (printable - 0) / max(len(text), 1)))


def _ocr_pages(body: bytes, page_nums: list[int], password: Optional[str]) -> dict[int, str]:
    """OCR a set of pages, returning {page_num: ocr_markdown_body}.

    Uses the existing OCR module (pypdfium2 render + rapidocr). One call for the
    whole set (one pypdfium2 open). Pages that fail to OCR are omitted from the
    returned dict (the caller keeps the original cid-garbage text for them)."""
    if not page_nums:
        return {}
    try:
        from master_fetch.ocr import ocr_pdf, ocr_available
    except Exception:
        return {}
    if not ocr_available():
        return {}
    spec = ",".join(str(n) for n in page_nums)
    try:
        res = ocr_pdf(body, pages=spec, password=password)
    except Exception as e:
        logger.debug("CID-page OCR batch failed: %s", e)
        return {}
    if res.error or not res.content:
        return {}
    # ocr_pdf returns one markdown string with "--- Page N ---" markers per page.
    full = res.content[0]
    out: dict[int, str] = {}
    # Split into per-page blocks by the "--- Page N ---" marker.
    parts = re.split(r"(?=^--- Page \d+ ---$)", full, flags=re.MULTILINE)
    for part in parts:
        m = re.match(r"^--- Page (\d+) ---$", part, re.MULTILINE)
        if m:
            n = int(m.group(1))
            if n in page_nums:
                # Strip the marker line; keep the body.
                body_md = re.sub(r"^--- Page \d+ ---\s*", "", part, count=1).strip()
                if body_md:
                    out[n] = body_md
    return out


def _extract_images_metadata(pdf, page_nums: list[int]) -> list[str]:
    """Per-page embedded raster-image metadata as agent-readable strings (P4).
    Vector graphics are NOT extractable; we report counts + dimensions so a
    multimodal agent knows which pages have figures to screenshot."""
    out: list[str] = []
    for n in page_nums:
        try:
            imgs = pdf.pages[n - 1].images
        except Exception:
            continue
        if not imgs:
            continue
        largest = max(imgs, key=lambda im: (im.get("width", 0) * im.get("height", 0)))
        out.append(
            f"page {n}: {len(imgs)} embedded image(s); largest "
            f"{int(largest.get('width', 0))}x{int(largest.get('height', 0))}"
        )
    return out


def extract_pdf(
    body: bytes,
    extraction_type: str = "markdown",
    pages: str | None = None,
    password: str | None = None,
    include_media: bool = False,
) -> PdfResult:
    """Extract a PDF's bytes into agent-optimized markdown with honest quality
    signals + per-page CID-garbage auto-OCR fallback. See module docstring."""
    if not body or not isinstance(body, (bytes, bytearray)):
        return PdfResult(error="empty or non-bytes PDF body", content=["[Empty PDF body.]"])
    if not body[:5].startswith(b"%PDF"):
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
        meta_dict = _metadata_dict(meta)
        toc = _extract_toc(body)
        media = _extract_images_metadata(pdf, page_nums) if include_media else []

        # --- Pass 1: document body font size ---
        body_sizes: list[float] = []
        for n in page_nums:
            p = pdf.pages[n - 1]
            body_sizes.extend(c.get("size", 0) for c in p.chars if c.get("text", "").strip())
        body_size = statistics.median(body_sizes) if body_sizes else 0.0

        # --- Pass 2: render each page + detect CID corruption ---
        rendered: dict[int, str] = {}
        cid_pages: list[int] = []
        total_chars = 0
        for n in page_nums:
            p = pdf.pages[n - 1]
            try:
                page_md = _render_page(p, body_size, text_mode)
            except Exception as e:
                logger.debug("PDF page %d render failed: %s", n, e)
                page_md = f"[Failed to render this page: {str(e)[:120]}]"
            rendered[n] = page_md
            total_chars += len(page_md)
            ratio, _n = _cid_ratio(page_md)
            if ratio >= _CID_RATIO_THRESHOLD:
                cid_pages.append(n)

        # --- CID-garbage auto-OCR fallback (the flagship fix for P1) ---
        ocr_map = _ocr_pages(body, cid_pages, password) if cid_pages else {}
        ocr_used = bool(ocr_map)
        cid_pages_ocr = sorted(ocr_map.keys())
        # Quality score from RAW page text + OCR recovery, so the honest markers
        # (clean English scaffolding) don't inflate the score and mask garbage.
        # OCR-recovered pages count as clean; unrecovered CID pages count as low.
        total_w = 0.0
        total_len = 0
        for n in page_nums:
            raw = rendered[n]
            if n in ocr_map:
                q = 1.0  # OCR recovered the visible text -> clean by construction
                plen = len(ocr_map[n])
            else:
                q = _quality_score(raw)
                plen = len(raw)
            total_w += q * plen
            total_len += plen
        quality = (total_w / total_len) if total_len else 0.0
        # Now replace CID pages with markers (display only; quality already scored).
        for n in cid_pages:
            if n in ocr_map:
                rendered[n] = (
                    f"[Page {n}: text recovered by OCR from a broken font mapping — "
                    f"figures/equations OCR'd as visible symbols; use a vision tool "
                    f"for precise layout/LaTeX.]\n\n" + ocr_map[n]
                )
            else:
                rendered[n] = (
                    f"[Page {n}: {int(_cid_ratio(rendered[n])[0]*100)}% of this page "
                    f"is CID font garbage (embedded font without a Unicode map). "
                    f"Install OCR with `pip install hound-mcp[all]` to auto-recover it, "
                    f"or smart_fetch this page with screenshot / a vision tool.]\n\n"
                    + rendered[n]
                )

        # --- Scanned / image-only detection (no text layer at all) ---
        scanned = total_chars < _SCANNED_CHARS_PER_PAGE * len(page_nums)
        if scanned:
            return PdfResult(
                title=meta_dict.get("title", ""), author=meta_dict.get("author", ""),
                subject=meta_dict.get("subject", ""), keywords=meta_dict.get("keywords", ""),
                pages_total=total_pages, pages_extracted=page_nums, scanned=True,
                metadata=meta_dict, table_of_contents=toc,
                error="scanned_pdf: this PDF is image-only (no extractable text). "
                      "Install OCR support with `pip install hound-mcp[all]` and hound "
                      "will auto-OCR scanned PDFs; or use a vision-capable tool.",
                content=["[Scanned/image-only PDF - no extractable text. Install hound-mcp[all] for OCR.]"],
            )

        # --- Assemble: metadata header + pages ---
        header = _format_metadata(meta)
        if len(page_nums) == total_pages:
            scope = f"{total_pages} pages"
        else:
            scope = (f"pages {page_nums[0]}-{page_nums[-1]} of {total_pages}"
                     if len(page_nums) > 1 else f"page {page_nums[0]} of {total_pages}")
        header.append(f"> PDF · {scope}")
        if ocr_used:
            header.append(f"> OCR fallback used on page(s): {', '.join(str(n) for n in cid_pages_ocr)}")
        body_md = "\n\n".join(f"--- Page {n} ---\n\n{rendered[n]}" for n in page_nums).strip()
        full = ("\n".join(header).strip() + "\n\n" + body_md).strip()

        # --- Quality score + honest content_ok (P3) ---
        # `quality` was computed above from raw page text + OCR recovery.
        content_ok = quality >= _QUALITY_OK_THRESHOLD and not scanned

        return PdfResult(
            content=[full],
            title=meta_dict.get("title", ""), author=meta_dict.get("author", ""),
            subject=meta_dict.get("subject", ""), keywords=meta_dict.get("keywords", ""),
            pages_total=total_pages, pages_extracted=page_nums,
            metadata=meta_dict, table_of_contents=toc, quality_score=round(quality, 3),
            content_ok=content_ok, media=media, ocr_fallback_used=ocr_used,
            cid_pages_ocr=cid_pages_ocr,
        )
    finally:
        try:
            pdf.close()
        except Exception:
            pass


def _render_page(page: Any, body_size: float, text_mode: bool) -> str:
    """Render one page to markdown: layout-aware text + tables merged by y-position."""
    try:
        tables = page.find_tables(table_settings={"text_x_tolerance": _X_TOL, "text_y_tolerance": 3})
    except Exception:
        tables = []
    table_bboxes = [t.bbox for t in tables] if tables else []

    if table_bboxes:
        filtered = page.filter(
            lambda obj: obj.get("object_type") != "char"
            or not any(_in_bbox(obj, b) for b in table_bboxes)
        )
    else:
        filtered = page

    try:
        text_lines = filtered.extract_text_lines(layout=False, x_tolerance=_X_TOL, return_chars=True)
    except Exception:
        text_lines = []
    if not text_lines and filtered.chars:
        txt = filtered.extract_text(layout=False, x_tolerance=_X_TOL) or ""
        return _dehyphenate_block(txt, text_mode)

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
            prev_bottom = None
            md = _table_to_markdown(payload, text_mode=text_mode)
            if md:
                out.append(md)
            continue
        txt, chars, line_top, line_bottom = payload
        level = _heading_level(_dominant_size(chars), body_size, _is_bold(chars), txt) if chars else 0
        if level and not text_mode:
            flush_paragraph()
            out.append(f"{'#' * level} {txt}")
            prev_bottom = line_bottom
            continue
        if prev_bottom is not None and (line_top - prev_bottom) > gap_threshold:
            flush_paragraph()
        para.append(txt)
        prev_bottom = line_bottom
    flush_paragraph()

    return "\n\n".join(out).strip()


def _dehyphenate_block(text: str, text_mode: bool) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return ""
    joined = lines[0]
    for nxt in lines[1:]:
        joined, _ = _dehyphenate_join(joined, nxt)
    return _clean_text(joined)

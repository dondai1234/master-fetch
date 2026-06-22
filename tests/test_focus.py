"""Tests for query-focused content filtering (smart_fetch `focus`)."""

from master_fetch.focus import focus_content, _split_blocks, _tokens, _is_heading


def _many_blocks():
    """A markdown doc with several paragraphs + a heading + a table."""
    return (
        "# Hound Release Notes\n\n"
        "Hound is a web research MCP server. It fetches pages and runs search.\n\n"
        "## Crawl\n\n"
        "The new crawl tool walks a whole site. It follows same-domain links with a "
        "depth limit and a token budget.\n\n"
        "OCR reads scanned PDFs and image pages using rapidocr and pypdfium2.\n\n"
        "The cache is a SQLite store keyed by URL and extraction type.\n\n"
        "| tool | purpose |\n| --- | --- |\n| crawl | site walk |\n| ocr | scanned pdf |\n"
    )


# ─── _tokens / _is_heading / _split_blocks ──────────────────────────────

def test_tokens_lowercases_and_filters_short():
    t = _tokens("Hound 2 OCR, a PDF!")
    assert "hound" in t and "ocr" in t and "pdf" in t
    assert "2" not in t and "a" not in t  # len < 2 dropped


def test_is_heading():
    assert _is_heading("# Title") is True
    assert _is_heading("## Sub") is True
    assert _is_heading("not a heading") is False
    assert _is_heading("") is False


def test_split_blocks_preserves_order_and_drops_blank_runs():
    blocks = _split_blocks("a\n\nb\n\nc")
    assert blocks == ["a", "b", "c"]


# ─── focus_content ─────────────────────────────────────────────────────

def test_focus_no_query_returns_unchanged():
    text = _many_blocks()
    assert focus_content(text, "") is text
    assert focus_content(text, "   ") is text


def test_focus_single_block_returns_unchanged():
    assert focus_content("only one block here", "query") == "only one block here"


def test_focus_no_query_terms_returns_unchanged():
    text = _many_blocks()
    # query with no usable >=2-char tokens
    assert focus_content(text, "a") is text


def test_focus_keeps_relevant_blocks_and_drops_irrelevant():
    text = _many_blocks()
    out = focus_content(text, "crawl site depth budget")
    assert out.startswith("[Focus:")
    # The crawl paragraph is relevant; the cache paragraph is not.
    assert "crawl tool walks" in out
    assert "SQLite store" not in out
    # The number of kept blocks is reported in the header.
    assert "of " in out  # "showing X of Y blocks"


def test_focus_preserves_preceding_heading():
    text = _many_blocks()
    out = focus_content(text, "crawl tool walks site")
    # The "## Crawl" heading sits right before the crawl paragraph and must be
    # preserved for context even though it has no query terms itself.
    assert "## Crawl" in out
    assert "crawl tool walks" in out


def test_focus_no_matches_keeps_closest_blocks():
    text = _many_blocks()
    out = focus_content(text, "zzz nonexistent topic qqq")
    # Nothing matches -> fallback keeps the closest blocks (not empty).
    assert out.startswith("[Focus:")
    # Body still has some content (the fallback top blocks).
    assert len(out) > len("[Focus:")


def test_focus_table_scored_by_cell_tokens():
    text = _many_blocks()
    out = focus_content(text, "ocr scanned pdf")
    # The table row mentioning 'ocr' / 'scanned pdf' should be kept.
    assert "scanned pdf" in out.lower() or "ocr" in out.lower()


def test_focus_header_names_query_and_counts():
    text = _many_blocks()
    out = focus_content(text, "crawl")
    assert "'crawl'" in out
    assert "blocks" in out

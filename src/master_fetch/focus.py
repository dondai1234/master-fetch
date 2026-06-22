"""Query-focused content filtering for smart_fetch.

When the agent passes ``focus="..."``, the extracted markdown is filtered to
the blocks (paragraphs / headings / tables / lists) most relevant to the query,
so the agent loads less context on long pages. Inspired by Crawl4AI's
BM25ContentFilter, implemented locally (no extra dep).

Design choice: focus runs **post-cache**. The full extracted text is cached
once per URL; different focus queries are just different views over the same
cached content, so focusing never causes a re-fetch and two different focuses
on the same URL share one cache entry. Filtering happens inside
``_apply_chunking`` (the universal final wrapper), so it applies to live
fetches, cache hits, and bulk results alike.

BM25 (k1=1.5, b=0.75) with an always-positive IDF (the ``+1`` inside the log)
so a block with a single query-term occurrence gets a positive score and is
kept at the default threshold. A heading immediately preceding a kept block is
preserved for context. If nothing clears the threshold, the closest blocks are
kept so the agent gets something to judge instead of an empty page.
"""

from __future__ import annotations

import math
import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 2]


def _is_heading(block: str) -> bool:
    """True if the block's first non-blank line is a markdown heading."""
    for line in block.splitlines():
        if line.strip():
            return line.lstrip().startswith("#")
    return False


def _split_blocks(text: str) -> list[str]:
    """Split markdown into blocks separated by blank lines. A block is a
    heading, paragraph, table, or list — kept verbatim (order preserved)."""
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "":
            if current:
                blocks.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def focus_content(
    text: str,
    query: str,
    threshold: float = 1.0,
    k1: float = 1.5,
    b: float = 0.75,
    fallback_top: int = 5,
) -> str:
    """Return the blocks of ``text`` most relevant to ``query`` (BM25).

    If ``query`` is empty, the text has <= 1 block, or the query yields no
    usable terms, the original text is returned unchanged (focus is a no-op).
    """
    if not query or not text or not text.strip():
        return text
    blocks = _split_blocks(text)
    if len(blocks) <= 1:
        return text
    qterms = set(_tokens(query))
    if not qterms:
        return text

    block_tokens = [_tokens(bl) for bl in blocks]
    n = len(blocks)
    avgdl = (sum(len(t) for t in block_tokens) / n) if n else 0.0 or 1.0
    if avgdl == 0:
        avgdl = 1.0

    # Document frequency per term (across blocks).
    df: dict[str, int] = {}
    for toks in block_tokens:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1

    def idf(term: str) -> float:
        d = df.get(term, 0)
        # +1 inside the log keeps IDF positive (BM25+ flavor) so a single
        # occurrence always scores > 0.
        return math.log((n - d + 0.5) / (d + 0.5) + 1)

    def score(i: int) -> float:
        toks = block_tokens[i]
        if not toks:
            return 0.0
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks)
        s = 0.0
        denom_len = k1 * (1 - b + b * dl / avgdl)
        for term in qterms:
            f = tf.get(term)
            if f:
                s += idf(term) * (f * (k1 + 1)) / (f + denom_len)
        return s

    scores = [score(i) for i in range(n)]
    keep = [i for i in range(n) if scores[i] >= threshold]
    if not keep:
        # Nothing cleared the threshold — keep the closest blocks so the agent
        # has something to judge rather than an empty response.
        keep = sorted(range(n), key=lambda i: scores[i], reverse=True)[:fallback_top]

    keep_set = set(keep)
    # Preserve a heading immediately preceding a kept non-heading block.
    for i in keep:
        if i > 0 and _is_heading(blocks[i - 1]) and not _is_heading(blocks[i]):
            keep_set.add(i - 1)

    kept = "\n\n".join(blocks[i] for i in range(n) if i in keep_set)
    header = (
        f"[Focus: {query!r}; showing {len(keep_set)} of {n} blocks "
        f"by BM25 relevance. Pass focus='' for the full page.]"
    )
    return header + "\n\n" + kept

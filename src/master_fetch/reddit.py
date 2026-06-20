"""Reddit optimization for Hound.

Rewrites Reddit listing URLs to old.reddit.com, which serves the same content
with ~7x smaller page size (134KB vs 1MB) and ~2x faster fetches. Post pages
(/comments/...) are left on www.reddit.com because old.reddit.com shows the
sidebar there instead of the full comment thread.

Also includes a custom parser for old.reddit.com listing HTML. It reads the
canonical per-post data-* attributes Reddit bakes into each ``<div class="thing">``
block (``data-score``, ``data-comments-count``, ``data-author``, ``data-url``,
``data-domain``, ``data-subreddit``, ``data-promoted``, ``data-nsfw``), so every
field is sourced from the SAME block — no cross-block alignment bugs (the
previous span-scraping regex picked up 3 score spans per post and silently
misaligned scores/comment counts on real HTML).
"""

import html as _html
import logging
import re
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger("master-fetch.reddit")

# A "thing" block opens with class="... thing ...". Real old.reddit.com HTML
# writes it as `class=" thing id-t3_..."` (LEADING SPACE), so the pattern allows
# arbitrary class chars around the word-boundaried "thing".
_THING_BLOCK_RE = re.compile(r'class="[^"]*\bthing\b[^"]*"')
# Capture the whole <a ...>TEXT</a> title tag so we can pull href and text
# out of the same tag (order-independent within the attrs).
_TITLE_TAG_RE = re.compile(
    r'<a\b([^>]*\bclass="[^"]*\btitle\b[^"]*"[^>]*)>([^<]*)</a>'
)
_HREF_RE = re.compile(r'\bhref="([^"]*)"')
# Per-block span fallbacks (used only when data-* attrs are absent, e.g. on
# user-profile pages). Scoped to a single block, so no cross-block alignment.
_UNVOTED_SCORE_RE = re.compile(r'<(?:div|span)[^>]*class="[^"]*\bscore\s+unvoted\b[^"]*"[^>]*title="(\d+)"')
_COMMENTS_LINK_RE = re.compile(r'>\s*(\d+)\s*comments?\s*<', re.IGNORECASE)
_FULL_COMMENTS_RE = re.compile(r'full comments\s*\((\d+)\)', re.IGNORECASE)


def _attr(block: str, name: str) -> str | None:
    """First value of the data-* attribute ``name`` in ``block`` (raw, unescaped)."""
    m = re.search(rf'\s{name}="([^"]*)"', block)
    return m.group(1) if m else None


def is_reddit_url(url: str) -> bool:
    """True if URL host is ``reddit.com`` or any subdomain (www, old, m, np).

    Rejects lookalikes (notreddit.com, reddit-clone.com).
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return host == "reddit.com" or host.endswith(".reddit.com")
    except Exception:
        return False


def rewrite_to_old_reddit(url: str) -> str:
    """Rewrite a Reddit listing URL to old.reddit.com.

    old.reddit.com serves the same listing content with ~7x smaller pages and
    ~2x faster fetches. Listings (subreddits, user pages, search) are rewritten;
    post pages (``/comments/...``) are NOT — old.reddit.com shows the sidebar
    there instead of the full comment thread, so they stay on www.reddit.com.

    Already-old URLs pass through unchanged. Non-Reddit URLs pass through
    unchanged (safe to call on any URL; rejects lookalikes like notreddit.com).
    Preserves scheme, path, query, fragment.
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        # Only touch real reddit hosts (also rejects notreddit.com, reddit-clone.com).
        if not (host == "reddit.com" or host.endswith(".reddit.com")):
            return url
        if host == "old.reddit.com":
            return url  # already old
        if "/comments/" in (parsed.path or ""):
            return url  # post page — keep www to preserve full comments
        new_url = urlunparse(parsed._replace(netloc="old.reddit.com"))
        logger.debug("Rewrote Reddit URL: %s -> %s", url, new_url)
        return new_url
    except Exception as e:
        logger.warning("Failed to rewrite Reddit URL %s: %s", url, e)
        return url


def _thing_blocks(html: str) -> list[str]:
    """Slice ``html`` into per-post ``<div class="thing ...">`` blocks."""
    starts = [m.start() for m in _THING_BLOCK_RE.finditer(html)]
    if not starts:
        return []
    blocks: list[str] = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(html)
        blocks.append(html[s:e])
    return blocks


def parse_old_reddit_listing(html: str) -> str | None:
    """Parse an old.reddit.com listing into structured markdown.

    Each post is read from its own ``<div class="thing">`` block's data-*
    attributes (canonical, one-per-block) plus the title link — so scores,
    comment counts, authors, and URLs can never misalign across posts. HTML
    entities in titles/authors are unescaped. Promoted ads are skipped.
    Stickied and NSFW posts are tagged.

    Returns formatted markdown, or ``None`` if no posts could be parsed (the
    caller should fall back to normal content extraction). Returning ``None``
    instead of the raw HTML prevents dumping raw HTML to the agent when the
    page isn't actually a listing (login wall, error page, post page, etc.).
    """
    if not html or len(html) < 100 or "thing" not in html:
        return None

    posts: list[dict] = []
    for block in _thing_blocks(html):
        # Skip promoted ads — not real posts.
        if (_attr(block, "data-promoted") or "").lower() in ("true", "1"):
            continue

        m = _TITLE_TAG_RE.search(block)
        if not m:
            continue  # not a post thing (sidebar/comment/pagination block)
        title = _html.unescape(m.group(2).strip())
        if not title:
            continue

        # Prefer canonical data-url; fall back to the title link's href.
        url = _attr(block, "data-url") or ""
        if not url:
            href_m = _HREF_RE.search(m.group(1))
            url = href_m.group(1) if href_m else ""
        if url.startswith("/"):
            url = f"https://old.reddit.com{url}"

        score = _attr(block, "data-score")
        if not score:
            # Fallback for user-profile pages (no data-score): the per-block
            # "score unvoted" span's title attr is the canonical visible score.
            m = _UNVOTED_SCORE_RE.search(block)
            score = m.group(1) if m else "?"

        cc = _attr(block, "data-comments-count")
        if not cc:
            # Fallback: the "N comments" link, or "full comments (N)" on
            # user-profile / search pages.
            mc = _COMMENTS_LINK_RE.search(block) or _FULL_COMMENTS_RE.search(block)
            cc = mc.group(1) if mc else None
        if cc:
            comments_str = f"{cc} comment" + ("" if cc == "1" else "s")
        else:
            comments_str = "?"
        author = _html.unescape((_attr(block, "data-author") or "?").strip())
        domain = _attr(block, "data-domain") or ""

        # Stickied is a class on the thing div itself; check only that class
        # so a post titled "...stickied..." can't false-positive.
        cls_m = re.match(r'class="([^"]*)"', block)
        stickied = bool(cls_m and "stickied" in cls_m.group(1))
        nsfw = (_attr(block, "data-nsfw") or "").lower() in ("true", "1")

        posts.append({
            "title": title, "score": score, "comments": comments_str,
            "author": author, "domain": domain, "url": url,
            "stickied": stickied, "nsfw": nsfw,
        })
        if len(posts) >= 25:
            break

    if not posts:
        return None

    out = ["# Reddit Posts\n"]
    for i, p in enumerate(posts, 1):
        tags = []
        if p["stickied"]:
            tags.append("sticky")
        if p["nsfw"]:
            tags.append("NSFW")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        domain_str = f" ({p['domain']})" if p["domain"] else ""
        out.append(f"{i}. **{p['title']}**{tag_str}{domain_str}")
        out.append(f"   Score: {p['score']} · {p['comments']} · by u/{p['author']}")
        if p["url"]:
            out.append(f"   {p['url']}")
        out.append("")
    return "\n".join(out)

"""Hound's own content extraction.

Replaces scrapling's Convertor._extract_content with direct trafilatura +
markdownify + lxml usage. The extraction chain:

1. Trafilatura (primary, for markdown/text/article/structured)
2. markdownify (fallback for markdown/html types)
3. Raw text (last resort: regex tag stripping)

CSS selector narrowing uses lxml directly.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

logger = logging.getLogger("master_fetch.extractor")


def extract_content(
    page,
    extraction_type: str = "markdown",
    css_selector: Optional[str] = None,
    main_content_only: bool = False,
) -> List[str]:
    """Extract content from a Response object.

    Args:
        page: A Response object (master_fetch.fetcher.Response) or compatible
              object with .body, .encoding, .url, .css()
        extraction_type: 'markdown', 'html', 'text', 'article', 'structured'
        css_selector: CSS selector to narrow extraction scope
        main_content_only: Strip nav/ads/footers

    Returns:
        List of extracted content strings (usually one element).
    """
    from master_fetch.trafilatura_extractor import extract_with_trafilatura, _fallback_extract

    # Trafilatura is the primary extractor for all text-like types
    if extraction_type in ("markdown", "text", "article", "structured"):
        try:
            result = extract_with_trafilatura(page, extraction_type=extraction_type, css_selector=css_selector)
            if result and any(r.strip() for r in result):
                return result
        except Exception as e:
            logger.debug(f"Trafilatura extraction failed: {e}")

    # Fallback: use markdownify for markdown/html, or raw text
    return _fallback_extract(page, extraction_type, css_selector)


def extract_html_content(
    page,
    css_selector: Optional[str] = None,
    main_content_only: bool = False,
) -> str:
    """Extract raw HTML from a page, optionally narrowed by CSS selector.

    Used for extraction_type='html'.
    """
    raw_body = getattr(page, 'body', None)
    encoding = getattr(page, 'encoding', 'utf-8') or 'utf-8'

    if raw_body:
        html = raw_body.decode(encoding, errors="replace")
    elif hasattr(page, 'content'):
        html = page.content
    elif hasattr(page, 'html_content'):
        html = page.html_content
    else:
        return ""

    if css_selector:
        try:
            from lxml import html as lxml_html
            from lxml.etree import tostring
            tree = lxml_html.fromstring(html)
            from lxml.cssselect import CSSSelector
            sel = CSSSelector(css_selector)
            matches = sel(tree)
            if matches:
                parts = [tostring(m, encoding="unicode") for m in matches]
                return "\n".join(parts)
        except Exception as e:
            logger.debug(f"CSS selector '{css_selector}' failed: {e}")

    if main_content_only:
        html = _strip_noise_tags(html)

    return html


def _strip_noise_tags(html: str) -> str:
    """Remove script, style, noscript, svg tags from HTML."""
    try:
        from lxml import html as lxml_html
        from lxml.etree import tostring
        tree = lxml_html.fromstring(html)
        for tag in ("script", "style", "noscript", "svg"):
            for el in tree.xpath(f"//{tag}"):
                el.getparent().remove(el)
        return tostring(tree, encoding="unicode")
    except Exception:
        # Regex fallback
        for pattern in (
            r'<script[^>]*>.*?</script>',
            r'<style[^>]*>.*?</style>',
            r'<noscript[^>]*>.*?</noscript>',
        ):
            html = re.sub(pattern, '', html, flags=re.DOTALL | re.IGNORECASE)
        return html


def extract_text_content(
    page,
    css_selector: Optional[str] = None,
    main_content_only: bool = False,
) -> str:
    """Extract plain text from a page (strip all HTML tags)."""
    raw_body = getattr(page, 'body', None)
    encoding = getattr(page, 'encoding', 'utf-8') or 'utf-8'

    if raw_body:
        html = raw_body.decode(encoding, errors="replace")
    elif hasattr(page, 'content'):
        html = page.content
    else:
        return ""

    if css_selector and hasattr(page, 'css'):
        selected = page.css(css_selector)
        if selected:
            parts = []
            for el in selected:
                root = getattr(el, '_root', el)
                if hasattr(root, 'text_content'):
                    parts.append(root.text_content())
                else:
                    parts.append(str(root))
            return " ".join(p for p in parts if p)

    # Strip tags
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<noscript[^>]*>.*?</noscript>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text

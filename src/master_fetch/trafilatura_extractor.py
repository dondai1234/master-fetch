"""Trafilatura-based content extraction — cleaner articles than Readability/markdownify.

Trafilatura excels at extracting the main article content from news/blog pages,
stripping navigation, footers, sidebars, cookie banners, and other noise.
It also extracts metadata (title, author, date, url).

Extraction chain (robust fallback):
1. Try requested format (markdown/text/article/structured)
2. If empty, try trafilatura.extract() with markdown output (different heuristics)
3. If still empty, try trafilatura.bare_extraction() for metadata
4. If all Trafilatura fails, fall back to Scrapling's built-in extraction
"""
import json
import logging
import re
from typing import Optional

import trafilatura
from lxml.etree import tostring
from lxml.html import fromstring as html_fromstring

from scrapling.parser import Selector

logger = logging.getLogger("master_fetch.trafilatura_extractor")


def _get_html_from_page(page) -> str | None:
    """Extract raw HTML string from a Scrapling Response object."""
    if hasattr(page, 'body') and page.body:
        html_bytes = page.body
        return html_bytes.decode(page.encoding or 'utf-8', errors='replace')
    elif hasattr(page, 'html_content') and page.html_content:
        return page.html_content
    else:
        # Fallback: serialize the lxml tree
        root = getattr(page, '_root', None) or getattr(page, 'root', None)
        if root is not None:
            return tostring(root, encoding='unicode')
    return None


def _extract_html_title(html: str) -> str:
    """Extract the <title> tag content from HTML as a metadata fallback."""
    try:
        tree = html_fromstring(html)
        title_el = tree.find(".//title")
        if title_el is not None and title_el.text:
            title = title_el.text.strip()
            # Clean up common suffixes like " - Wikipedia", " | BBC News", etc.
            # but only if the remaining title is still meaningful
            return title
    except Exception:
        pass
    # Regex fallback
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


def _trafilatura_markdown(html: str, url: str = "") -> str | None:
    """Best-effort markdown extraction using trafilatura.extract().
    This uses different heuristics than bare_extraction and often succeeds
    where bare_extraction fails.
    """
    return trafilatura.extract(
        html, url=url,
        include_comments=False, include_tables=True,
        output_format="markdown",
    )


def _trafilatura_article(html: str, url: str = "") -> dict | None:
    """Extract article as dict using bare_extraction.
    Falls back to HTML <title> tag if Trafilatura can't find a title.
    Returns None if extraction fails.
    """
    result = trafilatura.bare_extraction(
        html, url=url,
        include_comments=False, include_tables=True,
    )
    if result is None:
        return None

    title = getattr(result, "title", "") or ""
    # Fallback: try HTML <title> if Trafilatura couldn't find one
    if not title:
        title = _extract_html_title(html)

    return {
        "title": title,
        "author": getattr(result, "author", "") or "",
        "date": getattr(result, "date", "") or "",
        "body": getattr(result, "text", "") or "",
        "description": getattr(result, "description", "") or "",
        "url": getattr(result, "url", "") or url,
        "categories": getattr(result, "categories", None) or [],
        "tags": getattr(result, "tags", None) or [],
    }


def _trafilatura_structured(html: str, url: str = "") -> dict | None:
    """Extract structured article data with metadata."""
    article = _trafilatura_article(html, url)
    if article is None:
        return None

    # Enrich with trafilatura metadata
    try:
        metadata = trafilatura.metadata(html, url=url) if html else None
        if metadata:
            article["sitename"] = getattr(metadata, "sitename", "")
            # Don't overwrite article's own categories/tags if they exist
            if not article["categories"]:
                article["categories"] = getattr(metadata, "categories", []) or []
            if not article["tags"]:
                article["tags"] = getattr(metadata, "tags", []) or []
    except Exception as e:
        logger.debug(f"Metadata extraction failed: {e}")

    return article


def _extract_type(html: str, url: str, extraction_type: str) -> str | None:
    """Extract content in the requested format with robust fallback chain.

    For 'article' and 'structured': tries bare_extraction first, then
    falls back to extract() markdown and wraps it in the expected format.
    For 'markdown' and 'text': tries extract() first, then bare_extraction.
    """
    if extraction_type == "markdown":
        # Primary: trafilatura.extract() (best for markdown)
        result = _trafilatura_markdown(html, url)
        if result:
            return result
        # Fallback: bare_extraction text wrapped in heading
        article = _trafilatura_article(html, url)
        if article and article["body"]:
            title = article.get("title", "")
            return f"# {title}\n\n{article['body']}" if title else article["body"]
        return None

    elif extraction_type == "text":
        # Primary: bare_extraction for plain text
        article = _trafilatura_article(html, url)
        if article and article["body"]:
            return article["body"]
        # Fallback: markdown then strip
        md = _trafilatura_markdown(html, url)
        return md if md else None

    elif extraction_type == "article":
        # Primary: bare_extraction as JSON
        article = _trafilatura_article(html, url)
        if article and article["body"]:
            return json.dumps(article, indent=2)
        # Fallback: markdown extraction, wrap as article JSON
        md = _trafilatura_markdown(html, url)
        if md:
            # Try to get at least a title from HTML <title> tag
            html_title = _extract_html_title(html)
            return json.dumps({
                "title": html_title, "author": "", "date": "",
                "body": md, "description": "", "url": url,
                "categories": [], "tags": [],
            }, indent=2)
        return None

    elif extraction_type == "structured":
        # Primary: structured extraction with metadata
        data = _trafilatura_structured(html, url)
        if data and data.get("body"):
            return json.dumps(data, indent=2)
        # Fallback: markdown extraction, wrap as structured JSON
        md = _trafilatura_markdown(html, url)
        if md:
            # Try to get title from bare_extraction or HTML <title>
            article = _trafilatura_article(html, url)
            title = article.get("title", "") if article else ""
            if not title:
                title = _extract_html_title(html)
            return json.dumps({
                "title": title, "author": "", "date": "",
                "body": md, "description": "", "url": url,
                "sitename": "", "categories": [], "tags": [],
            }, indent=2)
        return None

    else:  # html or unknown — Trafilatura doesn't do HTML
        return None


def _fallback_extract(page, extraction_type: str, css_selector: Optional[str]) -> list[str]:
    """Fall back to Scrapling's built-in extraction (last resort)."""
    from scrapling.core.shell import Convertor
    # Scrapling only knows markdown/html/text/raw — map our extended types
    scrapling_type = "markdown" if extraction_type in ("article", "structured") else extraction_type
    logger.info(f"Falling back to Scrapling extraction (type={scrapling_type})")
    return list(Convertor._extract_content(
        page, css_selector=css_selector,
        extraction_type=scrapling_type, main_content_only=True,
    ))


def extract_with_trafilatura(
    page,  # Scrapling Response/Selector object
    extraction_type: str = "markdown",
    css_selector: Optional[str] = None,
) -> list[str]:
    """Use Trafilatura on a Scrapling response.

    This is the drop-in replacement for Scrapling's Convertor._extract_content.
    Uses a robust multi-stage extraction chain to maximize content quality.
    Falls back to Scrapling's own extraction only if ALL Trafilatura methods fail.
    """
    try:
        html = _get_html_from_page(page)
        if html is None:
            logger.warning("Cannot extract HTML from page object, falling back to Scrapling extractor")
            return _fallback_extract(page, extraction_type, css_selector)

        page_url = page.url if hasattr(page, 'url') else ""

        # If css_selector specified, narrow the HTML first
        if css_selector:
            selected = page.css(css_selector) if hasattr(page, 'css') else [page]
            parts = []
            for el in selected:
                try:
                    el_html = tostring(el._root if hasattr(el, '_root') else el, encoding='unicode')
                    part = _extract_type(el_html, page_url, extraction_type)
                    if part:
                        parts.append(part)
                except Exception:
                    continue
            if parts:
                return parts
            # CSS selector found nothing — try full page instead
            logger.info(f"CSS selector '{css_selector}' found nothing, trying full page extraction")

        result = _extract_type(html, page_url, extraction_type)
        if result:
            return [result]

        # Even _extract_type returned None — try one more time with markdown
        # regardless of requested type, just to get SOME clean content
        if extraction_type != "markdown":
            md = _trafilatura_markdown(html, page_url)
            if md:
                logger.info(f"Requested type '{extraction_type}' failed, returning markdown fallback")
                return [md]

        # Total failure — fall back to Scrapling
        logger.warning(f"All Trafilatura methods failed for {page_url}, falling back to Scrapling")
        return _fallback_extract(page, extraction_type, css_selector)

    except Exception as e:
        logger.warning(f"Trafilatura extraction crashed: {e}, falling back to Scrapling")
        return _fallback_extract(page, extraction_type, css_selector)

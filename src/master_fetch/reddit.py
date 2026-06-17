"""Reddit optimization for Hound.

Rewrites Reddit URLs to old.reddit.com for faster fetching.
old.reddit.com serves the same content but with 7x smaller page size (134KB vs 1MB),
resulting in 2x faster fetch times (6s vs 12s).

Also includes a custom parser for old.reddit.com HTML to extract structured
post data (titles, scores, comment counts, authors).
"""

import logging
import re
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger("master-fetch.reddit")


def is_reddit_url(url: str) -> bool:
    """Check if URL is a Reddit URL."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        # Match reddit.com and all subdomains (www, old, m, np, etc.)
        # but not lookalikes like notreddit.com
        return host == "reddit.com" or host.endswith(".reddit.com")
    except Exception:
        return False


def rewrite_to_old_reddit(url: str) -> str:
    """Rewrite Reddit URL to old.reddit.com for faster fetching.
    
    old.reddit.com serves the same content but with much smaller page size
    (134KB vs 1MB), resulting in 2x faster fetch times.
    
    Only rewrites subreddit listings, NOT individual post pages.
    Post pages (/comments/...) are NOT rewritten because old.reddit.com
    shows the sidebar instead of full comments.
    
    Handles:
    - www.reddit.com → old.reddit.com
    - reddit.com → old.reddit.com
    - m.reddit.com → old.reddit.com
    - Preserves old.reddit.com as-is
    - Preserves path, query, fragment
    - Skips /comments/ URLs (post pages)
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path or ""
        
        # Already old.reddit.com
        if host == "old.reddit.com":
            return url
        
        # Don't rewrite post pages - old.reddit.com shows sidebar instead of comments
        if "/comments/" in path:
            return url
        
        # Rewrite to old.reddit.com
        if "reddit.com" in host.lower():
            new_netloc = "old.reddit.com"
            
            # Reconstruct URL with old.reddit.com
            new_parsed = parsed._replace(netloc=new_netloc)
            new_url = urlunparse(new_parsed)
            
            logger.debug(f"Rewrote Reddit URL: {url} → {new_url}")
            return new_url
        
        return url
    except Exception as e:
        logger.warning(f"Failed to rewrite Reddit URL {url}: {e}")
        return url


def parse_old_reddit_listing(html: str) -> str:
    """Parse old.reddit.com subreddit listing into structured markdown.
    
    Extracts post titles, scores, comment counts, and authors from the
    simple HTML structure of old.reddit.com.
    
    Returns formatted markdown with structured post data.
    """
    if not html or len(html) < 100:
        return html
    
    # Check if this looks like old.reddit.com HTML
    # old.reddit.com uses class="thing" and class="score" patterns
    has_reddit_structure = (
        'class="thing' in html
        or 'class="score' in html
        or 'reddit.com' in html
    )
    if not has_reddit_structure:
        return html
    
    posts = []
    
    # Split by post entries - old.reddit.com uses <div class="thing ...">
    # But we'll use a simpler regex approach
    
    # Find all post blocks - they contain score, title, author, comments
    # Pattern: look for score followed by title link followed by author and comments
    
    # Extract scores (prefer numeric text content over title attribute)
    scores = re.findall(r'class="score[^"]*"[^>]*>([^<]+)', html)
    if not scores:
        # Fallback: extract number from title attribute like "42 points"
        scores_raw = re.findall(r'<span[^>]*class="[^"]*score[^"]*"[^>]*title="(\d+)', html)
        scores = scores_raw
    
    # Extract titles and URLs
    title_pattern = r'<a[^>]*class="[^"]*title[^"]*"[^>]*href="([^"]*)"[^>]*>([^<]+)</a>'
    titles = re.findall(title_pattern, html)
    
    # Extract comment counts
    comment_pattern = r'<a[^>]*href="[^"]*comments[^"]*"[^>]*>(\d+ comments?)</a>'
    comments = re.findall(comment_pattern, html)
    
    # Extract authors
    author_pattern = r'<a[^>]*class="[^"]*author[^"]*"[^>]*>([^<]+)</a>'
    authors = re.findall(author_pattern, html)
    
    # Build structured output
    if titles:
        output = ["# Reddit Posts\n"]
        
        for i, (url, title) in enumerate(titles[:25], 1):
            title = title.strip()
            score = scores[i-1] if i-1 < len(scores) else "?"
            comment_count = comments[i-1] if i-1 < len(comments) else "?"
            author = authors[i-1] if i-1 < len(authors) else "?"
            
            # Clean up title
            title = title.replace("(self.", "(").replace(")", "")
            
            # Format as markdown
            post_line = f"{i}. **{title}**"
            meta_line = f"   Score: {score} · {comment_count} · by u/{author}"
            
            # Make URL absolute if relative
            if url.startswith("/"):
                url = f"https://old.reddit.com{url}"
            
            output.append(post_line)
            output.append(meta_line)
            output.append(f"   {url}\n")
        
        return "\n".join(output)
    
    # Fallback: return original content if parsing failed
    return html

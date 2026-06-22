"""Page metadata extraction for Hound.

Enriches every HTML fetch response with structured metadata an agent can use to
judge relevance and cite sources: title, description, site name, type, image,
canonical URL, language, published time, and author. Pulled from OpenGraph meta
tags, JSON-LD blocks, the canonical link, and the <title> tag.

Kept dependency-free (regex + json) so it runs cheaply on every fetch. Only
populated for HTML pages; JSON / PDF / image responses get an empty dict.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

logger = logging.getLogger("master-fetch.metadata")

# Match <meta property/name="KEY" content="VAL"> in either attribute order.
_META_RE = re.compile(
    r'<meta\b[^>]*?(?:property|name)=["\']([^"\']+)["\'][^>]*?content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_META_RE_REV = re.compile(
    r'<meta\b[^>]*?content=["\']([^"\']*)["\'][^>]*?(?:property|name)=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_CANONICAL_RE = re.compile(
    r'<link\b[^>]*?rel=["\']canonical["\'][^>]*?href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_LANG_RE = re.compile(r'<html\b[^>]*?\blang=["\']([^"\']+)["\']', re.IGNORECASE)
_LD_RE = re.compile(
    r'<script\b[^>]*?type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_IMG_RE = re.compile(
    r'<img\b[^>]*?\bsrc=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_ASSET_SKIP = ("data:image",)

# Map meta keys to our flat field names. First match wins per field (OpenGraph
# takes priority over Twitter/Dublin Core, etc.).
_KEY_MAP = {
    "og:title": "title", "twitter:title": "title",
    "og:description": "description", "description": "description", "twitter:description": "description",
    "og:site_name": "site_name",
    "og:type": "type",
    "og:image": "image", "twitter:image": "image",
    "og:url": "og_url",
    "article:published_time": "published_time",
    "article:modified_time": "modified_time",
    "article:author": "author", "author": "author",
}


def extract_metadata(html: str, url: str) -> dict[str, Any]:
    """Extract a flat metadata dict from an HTML string. Empty if no HTML."""
    meta: dict[str, Any] = {}
    if not html:
        return meta

    # OpenGraph / meta tags (check both attribute orders).
    for rx in (_META_RE, _META_RE_REV):
        for m in rx.finditer(html):
            if rx is _META_RE:
                key, val = m.group(1), m.group(2)
            else:  # reversed: group(1)=content, group(2)=key
                key, val = m.group(2), m.group(1)
            key = key.lower().strip()
            val = val.strip()
            if not val:
                continue
            field = _KEY_MAP.get(key)
            if field and field not in meta:
                meta[field] = val[:500]

    # <title> fallback.
    if "title" not in meta:
        t = _TITLE_RE.search(html)
        if t:
            title = re.sub(r"\s+", " ", t.group(1)).strip()
            if title:
                meta["title"] = title[:500]

    # Canonical URL.
    c = _CANONICAL_RE.search(html)
    if c:
        try:
            meta["canonical"] = urljoin(url, c.group(1).strip())
        except Exception:
            pass

    # html lang.
    if "lang" not in meta:
        l = _LANG_RE.search(html)
        if l:
            meta["lang"] = l.group(1).strip()

    # JSON-LD: datePublished / author / description / headline.
    for m in _LD_RE.finditer(html):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        objs = data if isinstance(data, list) else [data]
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            if "published_time" not in meta:
                d = obj.get("datePublished") or obj.get("dateCreated")
                if d:
                    meta["published_time"] = str(d)[:20]
            if "title" not in meta:
                h = obj.get("headline") or obj.get("name")
                if h:
                    meta["title"] = str(h)[:500]
            if "description" not in meta:
                d = obj.get("description")
                if d:
                    meta["description"] = str(d)[:300]
            if "author" not in meta:
                a = obj.get("author")
                if isinstance(a, dict):
                    a = a.get("name")
                elif isinstance(a, list) and a:
                    a0 = a[0]
                    a = a0.get("name") if isinstance(a0, dict) else a0
                if a:
                    meta["author"] = str(a)[:200]

    return meta


def extract_image_urls(html: str, url: str, max_n: int = 20) -> list[str]:
    """Extract absolute image URLs from <img src=...> tags. Deduped, order-
    preserving, capped at max_n. Skips data: URIs. Used by smart_fetch's opt-in
    include_media flag so a multimodal agent can pull the page's images."""
    if not html:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _IMG_RE.finditer(html):
        src = (m.group(1) or "").strip()
        if not src or src.lower().startswith(_ASSET_SKIP):
            continue
        try:
            absu = urljoin(url, src)
        except Exception:
            continue
        if not absu.startswith(("http://", "https://")):
            continue
        if absu in seen:
            continue
        seen.add(absu)
        out.append(absu)
        if len(out) >= max_n:
            break
    return out

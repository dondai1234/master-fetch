"""Sitemap.xml discovery + parsing for smart_crawl (v8).

The genius move for crawling big sites: instead of blind best-first BFS (one
fetch per page to discover the next layer), fetch the site's sitemap.xml in ONE
call and get the complete URL list + <lastmod> dates. Most real sites ship a
sitemap (and declare it in robots.txt). This collapses a 500-page discovery
crawl into a single fetch.

Public surface:
  discover_sitemap(start_url, *, http_get, max_urls) -> SitemapResult
    Find the site's sitemap(s): check robots.txt `Sitemap:` directives first,
    then the conventional /sitemap.xml + /sitemap_index.xml paths. Fetch + parse
    them (recursing into <sitemapindex> children, capped), and return a flat,
    deduped list of {url, lastmod} plus provenance for the response.

  parse_sitemap(xml_bytes) -> (urls, sub_sitemaps)
    Low-level parser: a sitemap.xml is either a <urlset> (leaf: <url><loc>) or a
    <sitemapindex> (nested: <sitemap><loc>). Returns (leaf_urls, child_indexes).

Robustness: XML parsing is namespace-agnostic (lxml local-name), tolerates gzip
(servers sometimes gzip sitemaps), caps total URLs and recursion depth, and
never raises - a failed fetch/parse yields an empty result so crawl falls back
to BFS.

Transport is injected (`http_get`) so this module has no hard dep on a specific
HTTP client and is unit-testable with a fake. `http_get(url) -> (status, bytes)`
or None on failure.
"""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

from lxml import etree

logger = logging.getLogger("master-fetch.sitemap")

# A transport callable: url -> (status:int, body:bytes) | None
HttpGet = Callable[[str], Optional[tuple[int, bytes]]]

_MAX_URLS_DEFAULT = 5000      # cap a single discover_sitemap call
_MAX_INDEX_DEPTH = 3          # <sitemapindex> -> child <sitemapindex> -> ...
_MAX_SITEMAPS = 25            # don't recurse into an unbounded index forest


@dataclass
class SitemapURL:
    url: str
    lastmod: str = ""


@dataclass
class SitemapResult:
    urls: list[SitemapURL] = field(default_factory=list)
    sitemaps_used: list[str] = field(default_factory=list)   # the sitemap URLs that actually parsed
    via: str = ""                                             # "robots" | "conventional" | ""
    robots_checked: bool = False


def _maybe_gunzip(body: bytes) -> bytes:
    """Sitemaps are sometimes gzip even when not requested (Sitemap .xml.gz paths
    or servers that ignore Accept-Encoding). Detect by magic bytes, not extension."""
    if body[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(body)
        except Exception:
            return body
    return body


def parse_sitemap(xml_bytes: bytes) -> tuple[list[SitemapURL], list[str]]:
    """Parse one sitemap.xml document.

    Returns (leaf_urls, child_sitemap_urls). A <urlset> yields leaf_urls only;
    a <sitemapindex> yields child_sitemap_urls only. Namespace-agnostic: matches
    on local-name so the common xmlns="http://www.sitemaps.org/schemas/.../sitemap"
    (and any namespace variant / no namespace) all parse.
    """
    if not xml_bytes:
        return [], []
    try:
        body = _maybe_gunzip(xml_bytes)
        # recover=True so truncated/malformed sitemaps still yield what they can.
        root = etree.fromstring(body, parser=etree.XMLParser(recover=True, huge_tree=True))
    except Exception:
        return [], []
    if root is None:
        return [], []

    def lname(el) -> str:
        t = el.tag
        return t.split("}", 1)[1] if isinstance(t, str) and "}" in t else (t or "")

    root_name = lname(root)
    urls: list[SitemapURL] = []
    children: list[str] = []

    if root_name == "urlset":
        for url_el in root:
            if lname(url_el) != "url":
                continue
            loc = lastmod = ""
            for child in url_el:
                n = lname(child)
                if n == "loc" and child.text:
                    loc = child.text.strip()
                elif n == "lastmod" and child.text:
                    lastmod = child.text.strip()
            if loc:
                urls.append(SitemapURL(url=loc, lastmod=lastmod))
    elif root_name == "sitemapindex":
        for sm_el in root:
            if lname(sm_el) != "sitemap":
                continue
            for child in sm_el:
                if lname(child) == "loc" and child.text:
                    loc = child.text.strip()
                    if loc:
                        children.append(loc)
                    break
    return urls, children


def _fetch(http_get: HttpGet, url: str) -> Optional[bytes]:
    try:
        res = http_get(url)
    except Exception:
        return None
    if not res:
        return None
    status, body = res
    if status != 200 or not body:
        return None
    return body


def _robots_sitemaps(start_url: str, http_get: HttpGet) -> tuple[list[str], bool]:
    """Fetch /robots.txt and return its Sitemap: directives (absolute URLs)."""
    p = urlparse(start_url)
    robots_url = f"{p.scheme or 'https'}://{p.netloc}/robots.txt"
    body = _fetch(http_get, robots_url)
    if body is None:
        return [], False
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return [], True
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            val = line.split(":", 1)[1].strip()
            if val:
                out.append(urljoin(robots_url, val))
    # de-dup, preserve order
    seen: set[str] = set()
    uniq = [u for u in out if not (u in seen or seen.add(u))]
    return uniq, True


def _conventional_sitemaps(start_url: str) -> list[str]:
    p = urlparse(start_url)
    base = f"{p.scheme or 'https'}://{p.netloc}"
    return [base + "/sitemap.xml", base + "/sitemap_index.xml"]


def discover_sitemap(start_url: str, *, http_get: HttpGet,
                     max_urls: int = _MAX_URLS_DEFAULT) -> SitemapResult:
    """Discover + fetch the site's sitemap(s), returning a flat URL list.

    Tries robots.txt `Sitemap:` directives first; if none parse, falls back to
    the conventional /sitemap.xml + /sitemap_index.xml paths. Recurses into
    <sitemapindex> children (capped by _MAX_INDEX_DEPTH + _MAX_SITEMAPS). Caps
    total URLs at max_urls. Never raises; returns an empty result on any failure
    so the caller can fall back to BFS.
    """
    result = SitemapResult()
    if not start_url:
        return result

    candidates: list[str] = []
    robots_smaps, robots_checked = _robots_sitemaps(start_url, http_get)
    result.robots_checked = robots_checked
    if robots_smaps:
        candidates.extend(robots_smaps)
        result.via = "robots"
    else:
        candidates.extend(_conventional_sitemaps(start_url))
        result.via = "conventional"

    seen_urls: set[str] = set()
    flat: list[SitemapURL] = []
    visited_sitemaps: set[str] = set()

    def _drain(sitemap_url: str, depth: int) -> None:
        if depth > _MAX_INDEX_DEPTH or len(visited_sitemaps) >= _MAX_SITEMAPS:
            return
        if sitemap_url in visited_sitemaps:
            return
        visited_sitemaps.add(sitemap_url)
        body = _fetch(http_get, sitemap_url)
        if body is None:
            return
        result.sitemaps_used.append(sitemap_url)  # provenance: every sitemap fetched + parsed
        urls, children = parse_sitemap(body)
        for su in urls:
            u = su.url.split("#")[0]
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            flat.append(su)
            if len(flat) >= max_urls:
                return
        for child in children:
            if len(flat) >= max_urls:
                return
            _drain(child, depth + 1)

    for c in candidates:
        if len(flat) >= max_urls:
            break
        _drain(c, 0)

    result.urls = flat
    return result

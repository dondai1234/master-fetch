"""Outgoing-link extraction + classification for smart_fetch (v8).

Given a page's HTML, return its outgoing links classified by context so an
agent can follow a page's source chain in one step instead of eyeballing
markdown links:

  citations   - links inside the main-content area (<article>/<main>/<section>/
                <p>/<li>). These are the page's referenced sources (papers,
                primary documents, related reads) - the highest-value links.
  navigation  - links inside <nav>/<header>/<footer>/<aside>/role=navigation.
                Site chrome, rarely useful to follow.
  external    - links to a different domain than the page (split out from the
                above so an agent can see off-site references at a glance).
  primary_source - one best-effort "the actual primary source for this page"
    hint, derived from canonical/JSON-LD metadata or a citation pointing at a
    known primary host (arxiv.org, doi.org, biorxiv.org, github.com, ...).

Robustness: a cheap, forgiving lxml pass. Malformed HTML, missing sections, or
no <a> tags all yield empty lists, never raise. Classification is heuristic
(container walk) - a link in an ambiguous container falls back to "citation"
(main-content bias, since that is the high-value default an agent wants).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin, urlparse

from lxml import html as lxml_html

logger = logging.getLogger("master-fetch.links")

# Containers that count as "main content" -> links inside are citations.
_MAIN_XPATH = ".//ancestor::article | .//ancestor::main | .//ancestor::section | .//ancestor::p | .//ancestor::li"
# Containers that count as site chrome -> links inside are navigation.
_NAV_TAGS = {"nav", "header", "footer", "aside"}
# Hosts that are very often the primary source a secondary article references.
_PRIMARY_HOSTS = (
    "arxiv.org", "doi.org", "biorxiv.org", "medrxiv.org",
    "github.com", "gitlab.com", "opencode.dev",
    "wikipedia.org", "wikimedia.org",
    "nature.com", "science.org", "sciencedirect.com", "springer.com",
    "ieee.org", "acm.org", "plos.org",
)

_MAX_CITATIONS = 30
_MAX_NAV = 20
_MAX_EXTERNAL = 20


def _norm_host(u: str) -> str:
    try:
        return (urlparse(u).netloc or "").lower().lstrip("www.")
    except Exception:
        return ""


def _clean_text(s: str) -> str:
    return " ".join((s or "").split())


def extract_links(html_text: str, page_url: str, metadata: dict[str, Any] | None = None
                  ) -> dict[str, Any]:
    """Classify a page's outgoing links.

    Returns {citations, navigation, external, primary_source}. Each list item
    is {url, text}. Never raises; on any error returns empty lists.
    """
    out: dict[str, Any] = {"citations": [], "navigation": [], "external": [], "primary_source": ""}
    if not html_text or not page_url:
        return out
    try:
        tree = lxml_html.fromstring(html_text)
    except Exception:
        # lxml refuses truly broken markup sometimes; fall back to no links.
        return out
    if tree is None:
        return out

    page_host = _norm_host(page_url)
    seen: set[str] = set()
    citations: list[dict[str, str]] = []
    navigation: list[dict[str, str]] = []
    external: list[dict[str, str]] = []
    content_externals: list[dict[str, str]] = []  # off-domain links in main-content area (real references) - for primary_source

    try:
        anchors = tree.xpath('//a[@href]')
    except Exception:
        anchors = []
    for a in anchors:
        try:
            href = (a.get("href") or "").strip()
        except Exception:
            continue
        if not href or href.lower().startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
            continue
        try:
            absu = urljoin(page_url, href)
        except Exception:
            continue
        parsed = urlparse(absu)
        if parsed.scheme not in ("http", "https"):
            continue
        host = _norm_host(absu)
        if not host:
            continue
        key = absu.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        try:
            text = _clean_text(a.text_content() or "")
        except Exception:
            text = ""
        if len(text) > 160:
            text = text[:157] + "..."
        entry = {"url": key, "text": text}

        # Container classification: is this anchor inside site chrome?
        try:
            in_nav = next(
                (True for anc in a.iterancestors()
                 if (anc.tag if isinstance(anc.tag, str) else "") in _NAV_TAGS
                 or (anc.get("role") or "") in ("navigation", "menu", "menubar")),
                False,
            )
        except Exception:
            in_nav = False

        is_external = host != page_host
        if is_external:
            if len(external) < _MAX_EXTERNAL:
                external.append(entry)
            if not in_nav:
                content_externals.append(entry)  # off-domain reference in main content
            continue
        # Same-domain: nav chrome vs main-content citation.
        if in_nav:
            if len(navigation) < _MAX_NAV:
                navigation.append(entry)
        else:
            if len(citations) < _MAX_CITATIONS:
                citations.append(entry)

    out["citations"] = citations
    out["navigation"] = navigation
    out["external"] = external
    out["primary_source"] = _primary_source(page_url, metadata or {}, content_externals)
    return out


def _primary_source(page_url: str, metadata: dict[str, Any],
                    content_externals: list[dict[str, str]]
                    ) -> str:
    """Best-effort single primary-source URL, or "".

    Priority: a canonical/JSON-LD URL on a different host than the page (the
    publisher's authoritative location); else the first OFF-DOMAIN link that sits
    in the page's main-content area (a real in-content reference, not site
    chrome) and points at a known primary host (arxiv/doi/github/...). Same-
    domain links are never a primary source (they're the site itself).
    """
    page_host = _norm_host(page_url)
    # 1) canonical / JSON-LD @id / og:url on a different host.
    for key in ("canonical", "og:url", "url"):
        val = metadata.get(key)
        if isinstance(val, str) and val.startswith("http"):
            if _norm_host(val) and _norm_host(val) != page_host:
                return val
    # 2) an in-content off-domain reference on a known primary host.
    for e in content_externals:
        host = _norm_host(e["url"])
        if any(host == p or host.endswith("." + p) for p in _PRIMARY_HOSTS):
            return e["url"]
    return ""

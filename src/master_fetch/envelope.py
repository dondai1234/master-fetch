"""v10 research-grade envelope: page-type, freshness, and source-authority
signals computed for every fetch so an agent gets trust + currency + the next
step without a second call.

All functions use only the standard library and run on already-extracted HTML /
the URL string, keeping the envelope self-contained and low-overhead.

Design principles:
- CONSERVATIVE over RECALL. A wrong ``is_official=True`` or a mislabelled
  ``page_type="list"`` sends the agent on a bad path. Default to "unknown" /
  False when the signal is weak; only assert on strong evidence.
- page_type is split: structural signals (forum/qa/list/docs/article) are
  detected from raw HTML in _translate_response; error-derived signals
  (js_shell/auth_wall) override in _with_agent_hints since they are
  definitive (set by _annotate_quality after extraction).
- freshness prefers the MODIFIED/updated date over the published date — a page
  updated last week is not stale even if first published in 2014.
"""
from __future__ import annotations

import re
from datetime import datetime, date, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

# A page older than this is flagged stale. Flat threshold (v10 keeps it
# simple; domain-aware thresholds are a possible v10.1).
STALE_DAYS = 365

# ─── Source authority classification ───────────────────────────────

# Known news domains (small, conservative set). Best-effort; "unknown" is fine
# for anything not listed — source_type is a hint, not a verdict.
_NEWS_DOMAINS = (
    "nytimes.com", "bbc.com", "bbc.co.uk", "reuters.com", "theguardian.com",
    "washingtonpost.com", "bloomberg.com", "apnews.com", "aljazeera.com",
    "cnbc.com", "ft.com", "economist.com", "techcrunch.com", "theverge.com",
    "arstechnica.com", "wired.com", "nature.com", "science.org",
)

_QA_DOMAINS = (
    "stackoverflow.com", "stackexchange.com", "serverfault.com",
    "superuser.com", "mathoverflow.com", "askubuntu.com",
)

_GITHUB_DOMAINS = (
    "github.com", "raw.githubusercontent.com", "gist.github.com",
)


def classify_source(url: str) -> tuple[str, bool]:
    """Classify a URL's domain into a source_type + is_official flag.

    Returns (source_type, is_official). is_official is True ONLY on a strong
    signal that this is the canonical owner of the subject (gov, edu, github,
    the vendor's own docs subdomain). Everything else is False (conservative).
    """
    if not url:
        return "unknown", False
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return "unknown", False
    # strip userinfo@ and :port
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if ":" in host:
        host = host.split(":", 1)[0]
    if not host:
        return "unknown", False

    # Government / education / github: canonical, official.
    if host.endswith(".gov") or host == "gov" or ".gov." in host:
        return "gov", True
    if host.endswith(".edu") or host.endswith(".ac.uk") or re.search(r"\.ac\.[a-z]{2}$", host):
        return "edu", True
    if host in _GITHUB_DOMAINS or host.endswith(".github.io"):
        return "github", True

    # Vendor / official docs subdomains. docs.* / developer.* are almost always
    # the product owner's own docs (strong official signal).
    if host.startswith("docs.") or host.startswith("developer.") or host.startswith("developers."):
        return "docs-site", True

    # Q&A sites.
    if host in _QA_DOMAINS or host.endswith(".stackexchange.com") or host.endswith(".stackoverflow.com"):
        return "qa", False

    # Forums / community.
    if any(m in host for m in ("forum", "forums", "community", "discourse", "board")):
        return "forum", False
    if host in ("reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com") or host.endswith(".reddit.com"):
        return "forum", False

    # Blogs.
    if host.startswith("blog.") or host in ("medium.com", "wordpress.com", "substack.com") or host.endswith(".substack.com") or host.endswith(".medium.com"):
        return "blog", False

    # Ecommerce.
    if host.startswith("shop.") or host.startswith("store.") or host in ("amazon.com", "ebay.com") or host.endswith(".shop"):
        return "ecommerce", False

    # News (small known set).
    if any(host == d or host.endswith("." + d) for d in _NEWS_DOMAINS):
        return "news", False

    return "unknown", False


# ─── Freshness ─────────────────────────────────────────────────────

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y")


def _parse_date(s: str) -> date | None:
    """Parse a date from a metadata string. Handles ISO (with offset/Z),
    compact YYYYMMDD, and a few human formats. Returns None if unparseable."""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    # Compact YYYYMMDD (wayback timestamps, some metadata).
    if re.fullmatch(r"\d{8}", s):
        try:
            return datetime.strptime(s, "%Y%m%d").date()
        except ValueError:
            return None
    # ISO 8601 (fromisoformat in 3.11+ handles offsets and 'Z'). Take the date
    # from the full timestamp; fall back to the first 10 chars.
    for cand in (s, s[:10]):
        try:
            return datetime.fromisoformat(cand.replace("Z", "+00:00")).date()
        except ValueError:
            continue
    # Human formats — try the whole string then a 32-char prefix.
    for fmt in _DATE_FORMATS:
        for cand in (s, s[:32]):
            try:
                return datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
    return None


def compute_freshness(metadata: dict[str, Any], fetched_at_iso: str) -> tuple[int, bool]:
    """Return (content_age_days, is_stale) from the page's own dates.

    Prefers the modified/updated date over the published date (a page updated
    last week is current even if first published in 2014). Returns (-1, False)
    when no date is recoverable or the date is in the future (bad data).
    """
    if not metadata:
        return -1, False
    # Prefer modified > published > created > generic 'date'.
    date_str = (
        metadata.get("modified_time")
        or metadata.get("published_time")
        or metadata.get("mod_date")
        or metadata.get("creation_date")
        or metadata.get("date")
        or ""
    )
    content_date = _parse_date(date_str) if date_str else None
    if content_date is None:
        return -1, False
    fetched_date = _parse_date(fetched_at_iso) if fetched_at_iso else None
    if fetched_date is None:
        # Fall back to today (UTC) so freshness still works if fetched_at missing.
        fetched_date = datetime.now(timezone.utc).date()
    delta = (fetched_date - content_date).days
    if delta < 0:
        # Future-dated content = bad metadata; can't trust the age signal.
        return -1, False
    return delta, delta > STALE_DAYS


# ─── Page-type detection ────────────────────────────────────────────

# Forum / Q&A / docs markers in the raw HTML (class/id substring matches).
_FORUM_MARKERS = ("phpbb", "discourse", "class=\"forum", "id=\"forum",
                  "class=\"thread", "class=\"post-body", "class=\"message-body",
                  "data-post-id")
_QA_MARKERS = ("stackoverflow", "stackexchange", "class=\"question",
               "class=\"answer", "data-answerid", "data-questionid")
_DOCS_MARKERS = ("mkdocs", "docusaurus", "readthedocs", "sphinx-document",
                 "algolia-docsearch", "md-nav", "theme-doc", "class=\"rst-content",
                 "wy-nav-side")
_PAYWALL_MARKERS = ("subscribe to continue", "subscribe to read", "this article is for subscribers",
                    "create a free account to continue", "sign in to continue reading",
                    "you've reached your free article limit", "subscriber-only content",
                    "premium content")
# Explicit HTML data attributes are a structural paywall signal. Deliberately do
# not match the bare word "paywall": it can appear in ordinary README links,
# explanatory copy, scripts, or metadata. Parse HTML so attribute names are
# matched exactly and attribute values cannot impersonate names.
_PAYWALL_ATTRIBUTE_NAMES = frozenset({
    "data-paywall",
    "data-content-gate",
    "data-subscription-wall",
})
_FALSE_PAYWALL_ATTRIBUTE_VALUES = frozenset({
    "0", "false", "no", "off", "disabled", "none", "null",
})
_PAYWALL_IGNORED_TAGS = frozenset({"script", "style", "noscript", "template"})
_PAYWALL_METADATA_TAGS = frozenset({"base", "link", "meta"})
_PAYWALL_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "td", "th", "tr", "ul",
})


class _PaywallEvidenceParser(HTMLParser):
    """Collect visible text and active, exact paywall attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.visible_text: list[str] = []
        self.has_active_attribute = False
        self._ignored_stack: list[str] = []

    def _inspect_attributes(self, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() not in _PAYWALL_ATTRIBUTE_NAMES:
                continue
            normalized = value.strip().lower() if value is not None else None
            if normalized not in _FALSE_PAYWALL_ATTRIBUTE_VALUES:
                self.has_active_attribute = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._ignored_stack:
            if tag in _PAYWALL_IGNORED_TAGS:
                self._ignored_stack.append(tag)
            return
        if tag in _PAYWALL_IGNORED_TAGS:
            self._ignored_stack.append(tag)
            return
        if tag not in _PAYWALL_METADATA_TAGS:
            self._inspect_attributes(attrs)
        if tag in _PAYWALL_BLOCK_TAGS:
            self.visible_text.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if (
            not self._ignored_stack
            and tag not in _PAYWALL_IGNORED_TAGS
            and tag not in _PAYWALL_METADATA_TAGS
        ):
            self._inspect_attributes(attrs)
            if tag in _PAYWALL_BLOCK_TAGS:
                self.visible_text.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._ignored_stack:
            if tag in self._ignored_stack:
                while self._ignored_stack.pop() != tag:
                    pass
            return
        if tag in _PAYWALL_BLOCK_TAGS:
            self.visible_text.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._ignored_stack:
            self.visible_text.append(data)


def _paywall_evidence(html: str) -> tuple[str, bool]:
    """Return visible page text and whether an active paywall attribute exists."""
    parser = _PaywallEvidenceParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Malformed upstream HTML must not break the fetch response.
        return "", False
    return " ".join("".join(parser.visible_text).split()), parser.has_active_attribute


# A redirect via <meta http-equiv=refresh> or a JS location.href assignment.
_META_REFRESH_RE = re.compile(
    r'<meta\b[^>]*?http-equiv=["\']refresh["\'][^>]*?content=["\'][^"\']*url=',
    re.IGNORECASE,
)
_JS_REDIRECT_RE = re.compile(
    r'(?:location\.href\s*=|location\.replace|window\.location\s*=)',
    re.IGNORECASE,
)
# Same-domain content links (rough): <a href="/..."> or <a href="https://host...">
_ANCHOR_RE = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\']', re.IGNORECASE)
# Blocks to strip before counting links (nav/header/footer/aside/script/style).
_STRIP_BLOCK_RE = re.compile(
    r'<(nav|header|footer|aside|script|style|noscript)\b[^>]*>.*?</\1>',
    re.IGNORECASE | re.DOTALL,
)
_ARTICLE_TAG_RE = re.compile(r'<article\b', re.IGNORECASE)


def _count_content_links(html: str, host: str) -> int:
    """Count same-domain, non-trivial <a> links in the main content area.

    Strips nav/header/footer/aside/script/style first so chrome links don't
    inflate the count. Excludes anchors (#), mailto:, javascript:, and
    off-domain links.
    """
    stripped = _STRIP_BLOCK_RE.sub("", html)
    count = 0
    for m in _ANCHOR_RE.finditer(stripped):
        href = (m.group(1) or "").strip()
        if not href:
            continue
        low = href.lower()
        if low.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        # Relative or same-host = content link candidate.
        if href.startswith("/") or href.startswith("?"):
            count += 1
            continue
        try:
            h = urlparse(href).netloc.lower()
        except Exception:
            continue
        if h and (h == host or h.endswith("." + host)):
            count += 1
    return count


def detect_page_type(
    html: str,
    url: str,
    content_type: str = "",
    extracted_text_len: int = 0,
) -> str:
    """Classify a page's structure from raw HTML + content-type.

    Conservative: returns "unknown" when no strong signal. Error-derived
    signals (js_shell / auth_wall) are NOT detected here — they are set later
    by _with_agent_hints from result.error, which overrides this value.
    """
    ct = (content_type or "").lower()
    if ct.startswith("application/pdf"):
        return "pdf"
    if ct.startswith("application/json") or ct.startswith("text/json"):
        return "json"
    if ct.startswith("image/"):
        return "image"
    if not html or not html.strip():
        return "unknown"

    low = html.lower()

    # Redirect: meta refresh or JS location assignment (and little real text).
    if _META_REFRESH_RE.search(low) or (_JS_REDIRECT_RE.search(low) and extracted_text_len < 500):
        return "redirect"

    # Parse visible text and exact structural attributes. Raw-HTML prefilters are
    # unsafe here because entities and inline tags can split a real signal.
    visible_text, has_active_paywall_attribute = _paywall_evidence(html)
    if any(marker in visible_text.lower() for marker in _PAYWALL_MARKERS):
        return "paywall"
    if has_active_paywall_attribute:
        return "paywall"

    # Forum / Q&A / docs markers (substring match in the raw HTML).
    if any(m in low for m in _QA_MARKERS):
        return "qa"
    if any(m in low for m in _FORUM_MARKERS):
        return "forum"
    if any(m in low for m in _DOCS_MARKERS):
        return "docs"

    # List page: many same-domain content links and relatively little text,
    # NOT an <article> page and NOT a docs page (caught above). This catches
    # index / search-result / category / archive pages whose main value is the
    # links onward. Conservative: an <article> page is an article even if it has
    # many cross-reference links (precision over recall — mislabelling an
    # article as a list sends the agent on a wrong crawl).
    try:
        host = urlparse(url).netloc.lower()
        if ":" in host:
            host = host.split(":", 1)[0]
    except Exception:
        host = ""
    if host and not _ARTICLE_TAG_RE.search(low):
        n_links = _count_content_links(html, host)
        # >=20 content links and either short text or low text-per-link ratio.
        if n_links >= 20 and (extracted_text_len < 1500 or extracted_text_len / max(n_links, 1) < 200):
            return "list"

    # Article: an <article> tag is a strong article signal.
    if _ARTICLE_TAG_RE.search(low):
        return "article"

    return "unknown"


def page_type_from_error(error: str) -> str:
    """Map a finalized error string to a page_type override (definitive signals).

    Returns "" when the error does not imply a page_type, so the structural
    page_type from detect_page_type stands.
    """
    if not error:
        return ""
    e = error.lower()
    if e.startswith("js_shell_detected"):
        return "js_shell"
    if e.startswith("auth_required") or e.startswith("not_a_pdf") and "auth" in e:
        return "auth_wall"
    if e.startswith("geo_redirect_detected"):
        return "redirect"
    return ""

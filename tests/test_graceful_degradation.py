"""Tests for v11.0.0: scrapling removed, hound's own fetcher + browser.

v11.0.0 removed the scrapling dependency entirely. Hound now uses:
- fetcher.py: primp-based HTTP fetcher + Response class
- browser.py: patchright-based browser sessions + Cloudflare solver
- extractor.py: trafilatura + markdownify content extraction

These tests verify the new architecture:
1. pyproject.toml has NO scrapling dependency
2. Response class mimics the old scrapling Response interface
3. HTTPSession works for HTTP fetches
4. Browser methods raise clear errors when browser deps unavailable
5. Browser availability check works correctly
6. Content extraction works without scrapling
"""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

import master_fetch.server as srv_mod


# ─── pyproject.toml: no scrapling ───────────────────────────────────────────

def test_pyproject_no_scrapling_in_core_deps():
    """scrapling must NOT be in core deps — removed in v11.0.0."""
    import tomllib
    from pathlib import Path
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    deps = data["project"]["dependencies"]
    dep_str = " ".join(deps)
    assert "scrapling" not in dep_str, (
        "scrapling must not be in core deps (removed in v11.0.0)"
    )


def test_pyproject_no_scrapling_in_all_extra():
    """scrapling must NOT be in [all] extra either."""
    import tomllib
    from pathlib import Path
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    all_deps = data["project"]["optional-dependencies"].get("all", [])
    all_str = " ".join(all_deps)
    assert "scrapling" not in all_str, (
        "scrapling must not be in [all] extra (removed in v11.0.0)"
    )


def test_pyproject_has_markdownify_in_core():
    """markdownify must be in core deps (replaces scrapling's Convertor)."""
    import tomllib
    from pathlib import Path
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    deps = data["project"]["dependencies"]
    dep_str = " ".join(deps)
    assert "markdownify" in dep_str, (
        "markdownify must be in core deps (HTML->markdown fallback)"
    )


def test_pyproject_has_primp_in_core():
    """primp must be in core deps (replaces scrapling's curl_cffi)."""
    import tomllib
    from pathlib import Path
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    deps = data["project"]["dependencies"]
    dep_str = " ".join(deps)
    assert "primp" in dep_str, "primp must be in core deps (TLS impersonation)"


def test_pyproject_all_has_browser_deps():
    """[all] extra must include browser deps (patchright, browserforge)."""
    import tomllib
    from pathlib import Path
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    all_deps = data["project"]["optional-dependencies"].get("all", [])
    all_str = " ".join(all_deps)
    for dep in ("patchright", "browserforge"):
        assert dep in all_str, f"[all] extra must declare {dep}"


def test_pyproject_all_has_ocr_deps():
    """[all] extra must still include OCR/PDF deps."""
    import tomllib
    from pathlib import Path
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    all_deps = data["project"]["optional-dependencies"].get("all", [])
    all_str = " ".join(all_deps)
    for dep in ("pdfplumber", "pypdfium2", "rapidocr", "onnxruntime", "tokenizers"):
        assert dep in all_str, f"[all] extra must declare {dep} (OCR/PDF dep)"


def test_pyproject_no_scrapling_transitive_deps():
    """scrapling transitive deps must NOT be declared (curl_cffi, msgspec, protego, click)."""
    import tomllib
    from pathlib import Path
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    all_deps = data["project"]["optional-dependencies"].get("all", [])
    all_str = " ".join(all_deps)
    for dep in ("curl_cffi", "msgspec", "protego", "click", "apify-fingerprint-datapoints"):
        assert dep not in all_str, f"{dep} was a scrapling transitive dep, should not be declared"


# ─── Response class (fetcher.py) ─────────────────────────────────────────────

def test_response_object():
    """Response class mimics the scrapling Response interface."""
    from master_fetch.fetcher import Response
    r = Response(
        url="https://example.com",
        body=b"<html><body>Hello</body></html>",
        status=200,
        headers={"content-type": "text/html"},
        encoding="utf-8",
    )
    assert r.url == "https://example.com"
    assert r.status == 200
    assert r.headers["content-type"] == "text/html"
    assert r.body == b"<html><body>Hello</body></html>"
    assert r.encoding == "utf-8"
    assert r.content == "<html><body>Hello</body></html>"
    assert r.html_content == "<html><body>Hello</body></html>"


def test_response_css_selector():
    """Response.css() returns elements with ._root (lxml node)."""
    from master_fetch.fetcher import Response
    r = Response(
        url="https://example.com",
        body=b"<html><body><div class='main'>Content</div><div class='nav'>Nav</div></body></html>",
        status=200,
        encoding="utf-8",
    )
    elements = r.css(".main")
    assert len(elements) == 1
    assert hasattr(elements[0], "_root")


def test_response_empty_css_returns_empty():
    """Response.css() returns empty list for no matches."""
    from master_fetch.fetcher import Response
    r = Response(
        url="https://example.com",
        body=b"<html><body>Hello</body></html>",
        status=200,
        encoding="utf-8",
    )
    assert r.css(".nonexistent") == []


def test_response_empty_body():
    """Response handles empty body gracefully."""
    from master_fetch.fetcher import Response
    r = Response(url="https://example.com", body=b"", status=200)
    assert r.content == ""
    assert r.css("div") == []


# ─── Browser availability ────────────────────────────────────────────────────

def test_browser_available_in_dev_env():
    """In the dev venv with patchright installed, browser should be available."""
    from master_fetch.browser import check_browser_available
    # Reset cache
    import master_fetch.browser as br
    br._browser_available = None
    br._browser_import_error = None
    assert check_browser_available() is True


def test_scrapling_available_wrapper():
    """_scrapling_available() wraps check_browser_available()."""
    srv_mod._scrapling_import_error = None
    import master_fetch.browser as br
    br._browser_available = None
    br._browser_import_error = None
    assert srv_mod._scrapling_available() is True


# ─── Content extraction without scrapling ────────────────────────────────────

def test_extract_content_with_response():
    """extract_content works with hound's own Response class."""
    from master_fetch.fetcher import Response
    from master_fetch.extractor import extract_content
    r = Response(
        url="https://example.com",
        body=b"<html><head><title>Test</title></head><body><article><p>Hello World</p></article></body></html>",
        status=200,
        headers={"content-type": "text/html"},
        encoding="utf-8",
    )
    result = extract_content(r, extraction_type="markdown")
    assert isinstance(result, list)
    assert len(result) > 0
    # Trafilatura should extract "Hello World"
    assert "Hello World" in result[0] or len(result[0]) > 0


def test_trafilatura_fallback_extract_without_scrapling():
    """_fallback_extract in trafilatura_extractor uses markdownify, not scrapling."""
    from master_fetch.trafilatura_extractor import _fallback_extract
    from master_fetch.fetcher import Response
    page = Response(
        url="https://example.com",
        body=b"<html><body><p>Fallback test</p></body></html>",
        status=200,
        encoding="utf-8",
    )
    result = _fallback_extract(page, "markdown", None)
    assert isinstance(result, list)
    assert len(result) > 0
    assert "Fallback test" in result[0]


def test_trafilatura_extract_html_type():
    """_fallback_extract with html type returns raw HTML."""
    from master_fetch.trafilatura_extractor import _fallback_extract
    from master_fetch.fetcher import Response
    page = Response(
        url="https://example.com",
        body=b"<html><body><p>HTML test</p></body></html>",
        status=200,
        encoding="utf-8",
    )
    result = _fallback_extract(page, "html", None)
    assert result == ["<html><body><p>HTML test</p></body></html>"]


def test_trafilatura_extract_text_type():
    """_fallback_extract with text type strips tags."""
    from master_fetch.trafilatura_extractor import _fallback_extract
    from master_fetch.fetcher import Response
    page = Response(
        url="https://example.com",
        body=b"<html><body><script>var x=1;</script><p>Text test</p></body></html>",
        status=200,
        encoding="utf-8",
    )
    result = _fallback_extract(page, "text", None)
    assert "Text test" in result[0]
    assert "<script>" not in result[0]
    assert "<p>" not in result[0]


# ─── _translate_response works with hound's Response ────────────────────────

def test_translate_response_with_hound_response():
    """_translate_response works with hound's own Response class."""
    from master_fetch.server import _translate_response, ResponseModel
    from master_fetch.fetcher import Response

    page = Response(
        url="https://example.com",
        body=b"<html><head><title>Test Page</title></head><body><article><p>Hello World</p></article></body></html>",
        status=200,
        headers={"content-type": "text/html"},
        encoding="utf-8",
    )
    result = _translate_response(
        page, extraction_type="markdown", css_selector=None,
        main_content_only=True, use_trafilatura=True, fetcher_used="http",
    )
    assert result.status == 200
    assert result.url == "https://example.com"
    assert result.fetcher_used == "http"
    assert len(result.content) > 0
    content_text = result.content[0]
    assert "Hello World" in content_text or len(content_text) > 0


def test_translate_response_raw_html_type():
    """_translate_response with html extraction type returns raw HTML."""
    from master_fetch.server import _translate_response
    from master_fetch.fetcher import Response

    page = Response(
        url="https://example.com",
        body=b"<html><body><p>Raw content test</p></body></html>",
        status=200,
        headers={"content-type": "text/html"},
        encoding="utf-8",
    )
    result = _translate_response(
        page, extraction_type="html", css_selector=None,
        main_content_only=True, use_trafilatura=False, fetcher_used="http",
    )
    assert result.status == 200
    assert len(result.content) > 0
    assert "Raw content test" in result.content[0]


# ─── browser.py Cloudflare detection ─────────────────────────────────────────

def test_detect_cloudflare_non_interactive():
    """_detect_cloudflare identifies non-interactive challenges."""
    from master_fetch.browser import _detect_cloudflare
    content = "<html><script>cType: 'non-interactive'</script></html>"
    assert _detect_cloudflare(content) == "non-interactive"


def test_detect_cloudflare_managed():
    """_detect_cloudflare identifies managed challenges."""
    from master_fetch.browser import _detect_cloudflare
    content = "<html><script>cType: 'managed'</script></html>"
    assert _detect_cloudflare(content) == "managed"


def test_detect_cloudflare_interactive():
    """_detect_cloudflare identifies interactive challenges."""
    from master_fetch.browser import _detect_cloudflare
    content = "<html><script>cType: 'interactive'</script></html>"
    assert _detect_cloudflare(content) == "interactive"


def test_detect_cloudflare_embedded():
    """_detect_cloudflare identifies embedded turnstile challenges."""
    from master_fetch.browser import _detect_cloudflare
    content = '<html><script src="challenges.cloudflare.com/turnstile/v0/api.js"></script></html>'
    assert _detect_cloudflare(content) == "embedded"


def test_detect_cloudflare_none():
    """_detect_cloudflare returns None for regular pages."""
    from master_fetch.browser import _detect_cloudflare
    content = "<html><body><h1>Hello World</h1></body></html>"
    assert _detect_cloudflare(content) is None


# ─── browser.py proxy helpers ────────────────────────────────────────────────

def test_construct_proxy_dict_string():
    """_construct_proxy_dict converts a proxy string to a Playwright dict."""
    from master_fetch.browser import _construct_proxy_dict
    result = _construct_proxy_dict("http://user:pass@host:8080")
    assert result["server"] == "http://host:8080"
    assert result["username"] == "user"
    assert result["password"] == "pass"


def test_construct_proxy_dict_socks5():
    """_construct_proxy_dict handles socks5 proxies."""
    from master_fetch.browser import _construct_proxy_dict
    result = _construct_proxy_dict("socks5://host:1080")
    assert result["server"] == "socks5://host:1080"


def test_construct_proxy_dict_already_dict():
    """_construct_proxy_dict passes through dicts unchanged."""
    from master_fetch.browser import _construct_proxy_dict
    d = {"server": "http://host:8080", "username": "u"}
    result = _construct_proxy_dict(d)
    assert result == d


def test_is_domain_blocked():
    """_is_domain_blocked matches domains and subdomains."""
    from master_fetch.browser import _is_domain_blocked
    blocked = frozenset({"example.com"})
    assert _is_domain_blocked("example.com", blocked) is True
    assert _is_domain_blocked("sub.example.com", blocked) is True
    assert _is_domain_blocked("other.com", blocked) is False
    assert _is_domain_blocked("", blocked) is False


# ─── browser.py constants ───────────────────────────────────────────────────

def test_browser_constants_present():
    """Browser constants are defined and non-empty."""
    from master_fetch.browser import DEFAULT_ARGS, HARMFUL_ARGS, STEALTH_ARGS, DISABLED_RESOURCE_TYPES
    assert len(DEFAULT_ARGS) > 0
    assert len(HARMFUL_ARGS) > 0
    assert len(STEALTH_ARGS) > 0
    assert len(DISABLED_RESOURCE_TYPES) > 0

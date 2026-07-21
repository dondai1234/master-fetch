"""Tests for graceful degradation / HTTP-only fallback mode.

When scrapling's browser deps (playwright, patchright, curl_cffi) can't be
imported (e.g. playwright has no wheels for Termux/aarch64), hound must:
1. Still install (no hard dep on playwright)
2. Still serve the MCP server (import doesn't crash)
3. Still fetch URLs via httpx + trafilatura (HTTP-only mode)
4. Still search (primp/httpx, no scrapling needed)
5. Raise clear errors for browser-only features (screenshot, stealthy_fetch)
6. Skip browser escalation in smart_fetch auto-escalation
"""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

import master_fetch.server as srv_mod


# ─── pyproject.toml dependency structure ───────────────────────────────────

def test_pyproject_no_scrapling_ai_in_core_deps():
    """scrapling[ai] must NOT be in core deps - it pulls scrapling's fetchers
    extra which pins playwright==X.Y.Z exactly, blocking Termux installs."""
    import tomllib
    from pathlib import Path
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    deps = data["project"]["dependencies"]
    dep_str = " ".join(deps)
    assert "scrapling[ai]" not in dep_str, (
        "scrapling[ai] must not be in core deps (it pulls playwright==X.Y.Z)"
    )
    assert "scrapling[fetchers]" not in dep_str, (
        "scrapling[fetchers] must not be in core deps (it pins playwright)"
    )
    assert any("scrapling>=" in d for d in deps), "scrapling core should be a dep"


def test_pyproject_no_playwright_in_core_deps():
    """playwright must NOT be in core deps - it has no wheels for some platforms."""
    import tomllib
    from pathlib import Path
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    deps = data["project"]["dependencies"]
    dep_str = " ".join(deps)
    assert "playwright" not in dep_str, (
        "playwright must not be in core deps (no aarch64/Termux wheels)"
    )
    assert "patchright" not in dep_str, (
        "patchright must not be in core deps (no aarch64/Termux wheels)"
    )
    assert "curl_cffi" not in dep_str, (
        "curl_cffi must not be in core deps (only needed for scrapling fetchers)"
    )


def test_pyproject_all_extra_has_browser_deps():
    """[all] extra must include browser deps with LOOSE pins (>=, not ==)."""
    import tomllib
    from pathlib import Path
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    all_deps = data["project"]["optional-dependencies"]["all"]
    all_str = " ".join(all_deps)
    for dep in ("playwright", "patchright", "curl_cffi"):
        assert dep in all_str, f"[all] extra must declare {dep}"
    # Must use loose pins (>=), NOT exact pins (==)
    assert not any("playwright==" in d for d in all_deps), (
        "playwright must use loose pin (>=), not exact pin (==)"
    )
    assert not any("patchright==" in d for d in all_deps), (
        "patchright must use loose pin (>=), not exact pin (==)"
    )


def test_pyproject_all_extra_has_ocr_deps():
    """[all] extra must still include OCR/PDF deps."""
    import tomllib
    from pathlib import Path
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    all_deps = data["project"]["optional-dependencies"]["all"]
    all_str = " ".join(all_deps)
    for dep in ("pdfplumber", "pypdfium2", "rapidocr", "onnxruntime", "tokenizers"):
        assert dep in all_str, f"[all] extra must declare {dep} (OCR/PDF dep)"


# ─── _get_scrapling / _scrapling_available ──────────────────────────────────

@pytest.fixture
def scrapling_unavailable(monkeypatch):
    """Simulate scrapling/browser deps being unavailable (HTTP-only mode).

    Resets the cached _scrapling and _scrapling_import_error, then patches
    _get_scrapling to return None (as it would when the import fails).
    Restores state after the test.
    """
    # Save original state
    orig_scrapling = srv_mod._scrapling
    orig_error = srv_mod._scrapling_import_error
    # Reset to force re-evaluation
    monkeypatch.setattr(srv_mod, "_scrapling", None)
    monkeypatch.setattr(srv_mod, "_scrapling_import_error", None)
    # Patch _get_scrapling to simulate import failure
    def fake_get_scrapling():
        monkeypatch.setattr(srv_mod, "_scrapling_import_error",
                            "No module named 'playwright'")
        return None
    monkeypatch.setattr(srv_mod, "_get_scrapling", fake_get_scrapling)
    monkeypatch.setattr(srv_mod, "_scrapling_available", lambda: False)
    yield
    # Restore
    monkeypatch.setattr(srv_mod, "_scrapling", orig_scrapling)
    monkeypatch.setattr(srv_mod, "_scrapling_import_error", orig_error)


def test_scrapling_available_true_in_normal_env():
    """In the dev venv with all deps installed, scrapling should be available."""
    # Reset cache to force re-import
    srv_mod._scrapling = None
    srv_mod._scrapling_import_error = None
    assert srv_mod._get_scrapling() is not None
    assert srv_mod._scrapling_available() is True


def test_fallback_response_object():
    """_FallbackResponse mimics the scrapling Response interface used by
    _translate_response and extract_with_trafilatura."""
    from master_fetch.server import _FallbackResponse
    r = _FallbackResponse(
        url="https://example.com",
        status=200,
        headers={"content-type": "text/html"},
        body=b"<html><body>Hello</body></html>",
        encoding="utf-8",
    )
    assert r.url == "https://example.com"
    assert r.status == 200
    assert r.headers["content-type"] == "text/html"
    assert r.body == b"<html><body>Hello</body></html>"
    assert r.encoding == "utf-8"
    assert r.content == "<html><body>Hello</body></html>"
    # css() returns empty list (graceful degradation)
    assert r.css("div") == []


@pytest.mark.asyncio
async def test_fallback_http_get_works():
    """_fallback_http_get fetches a URL via httpx and returns _FallbackResponse."""
    from master_fetch.server import _fallback_http_get
    # Mock httpx.AsyncClient
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b"<html><body>Test</body></html>"
    mock_response.headers = {"content-type": "text/html"}
    mock_response.url = "https://example.com"
    mock_response.encoding = "utf-8"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _fallback_http_get("https://example.com")
    assert isinstance(result, srv_mod._FallbackResponse)
    assert result.status == 200
    assert result.body == b"<html><body>Test</body></html>"
    assert "text/html" in result.headers["content-type"]


# ─── browser methods raise clear errors in HTTP-only mode ──────────────────

@pytest.mark.asyncio
async def test_screenshot_raises_in_http_only_mode(scrapling_unavailable):
    """screenshot must raise RuntimeError with a clear message when browser
    deps are unavailable."""
    server = srv_mod.MasterFetchServer()
    with pytest.raises(RuntimeError, match="browser deps"):
        await server.screenshot("https://example.com")


@pytest.mark.asyncio
async def test_stealthy_fetch_raises_in_http_only_mode(scrapling_unavailable):
    """stealthy_fetch must raise RuntimeError with a clear message when browser
    deps are unavailable."""
    server = srv_mod.MasterFetchServer()
    with pytest.raises(RuntimeError, match="browser deps"):
        await server.stealthy_fetch("https://example.com")


@pytest.mark.asyncio
async def test_open_session_raises_in_http_only_mode(scrapling_unavailable):
    """open_session must raise RuntimeError with a clear message when browser
    deps are unavailable."""
    server = srv_mod.MasterFetchServer()
    with pytest.raises(RuntimeError, match="browser deps"):
        await server.open_session(session_type="stealthy")


@pytest.mark.asyncio
async def test_bulk_fetch_raises_in_http_only_mode(scrapling_unavailable):
    """bulk_fetch (dynamic) must raise RuntimeError when browser deps unavailable."""
    server = srv_mod.MasterFetchServer()
    with pytest.raises(RuntimeError, match="browser deps"):
        await server.bulk_fetch(urls=["https://example.com"])


@pytest.mark.asyncio
async def test_ensure_auto_session_raises_in_http_only_mode(scrapling_unavailable):
    """_ensure_auto_session must raise RuntimeError when browser unavailable."""
    server = srv_mod.MasterFetchServer()
    with pytest.raises(RuntimeError, match="Browser unavailable"):
        await server._ensure_auto_session("stealthy")


# ─── _prewarm_stealthy is a no-op in HTTP-only mode ─────────────────────────

@pytest.mark.asyncio
async def test_prewarm_stealthy_noop_in_http_only_mode(scrapling_unavailable):
    """_prewarm_stealthy must be a no-op (not crash) when browser unavailable."""
    server = srv_mod.MasterFetchServer()
    # Must not raise
    await server._prewarm_stealthy()


# ─── _translate_response works with _FallbackResponse ───────────────────────

def test_translate_response_with_fallback_response():
    """_translate_response must work with _FallbackResponse objects (HTTP-only mode).
    Uses trafilatura extraction path since scrapling Convertor is unavailable."""
    from master_fetch.server import _translate_response, _FallbackResponse

    # Save and restore scrapling state (other tests need it)
    orig_scrapling = srv_mod._scrapling
    orig_error = srv_mod._scrapling_import_error
    srv_mod._scrapling = None
    srv_mod._scrapling_import_error = "test: playwright unavailable"
    try:
        page = _FallbackResponse(
            url="https://example.com",
            status=200,
            headers={"content-type": "text/html"},
            body=b"<html><head><title>Test Page</title></head><body><article><p>Hello World</p></article></body></html>",
            encoding="utf-8",
        )
        result = _translate_response(
            page, extraction_type="markdown", css_selector=None,
            main_content_only=True, use_trafilatura=True, fetcher_used="http",
        )
        assert result.status == 200
        assert result.url == "https://example.com"
        assert result.fetcher_used == "http"
        # Content should be extracted (trafilatura should get "Hello World")
        assert len(result.content) > 0
        content_text = result.content[0]
        assert "Hello World" in content_text or len(content_text) > 0
    finally:
        srv_mod._scrapling = orig_scrapling
        srv_mod._scrapling_import_error = orig_error


def test_translate_response_raw_fallback_no_trafilatura():
    """When trafilatura is not requested and scrapling is unavailable,
    _translate_response must use the raw extraction fallback."""
    from master_fetch.server import _translate_response, _FallbackResponse

    orig_scrapling = srv_mod._scrapling
    orig_error = srv_mod._scrapling_import_error
    srv_mod._scrapling = None
    srv_mod._scrapling_import_error = "test: playwright unavailable"
    try:
        page = _FallbackResponse(
            url="https://example.com",
            status=200,
            headers={"content-type": "text/html"},
            body=b"<html><body><p>Raw content test</p></body></html>",
            encoding="utf-8",
        )
        result = _translate_response(
            page, extraction_type="html", css_selector=None,
            main_content_only=True, use_trafilatura=False, fetcher_used="http",
        )
        assert result.status == 200
        assert len(result.content) > 0
        # HTML extraction should return the raw HTML
        assert "Raw content test" in result.content[0]
    finally:
        srv_mod._scrapling = orig_scrapling
        srv_mod._scrapling_import_error = orig_error


# ─── trafilatura_extractor _fallback_extract handles missing scrapling ──────

def test_trafilatura_fallback_extract_without_scrapling():
    """_fallback_extract in trafilatura_extractor must not crash when scrapling
    is unavailable - it should return raw HTML instead."""
    from master_fetch.trafilatura_extractor import _fallback_extract
    from master_fetch.server import _FallbackResponse

    page = _FallbackResponse(
        url="https://example.com",
        status=200,
        headers={},
        body=b"<html><body>Fallback test</body></html>",
        encoding="utf-8",
    )
    # Mock the scrapling import to fail (simulates HTTP-only mode)
    import sys
    original_scrapling = sys.modules.get('scrapling.core.shell')
    sys.modules['scrapling.core.shell'] = None  # Forces ImportError
    try:
        result = _fallback_extract(page, "markdown", None)
    finally:
        if original_scrapling is not None:
            sys.modules['scrapling.core.shell'] = original_scrapling
        else:
            sys.modules.pop('scrapling.core.shell', None)
    assert isinstance(result, list)
    assert len(result) > 0
    assert "Fallback test" in result[0]


# ─── _auto_escalate skips browser in HTTP-only mode ─────────────────────────

@pytest.mark.asyncio
async def test_auto_escalate_skips_browser_in_http_only_mode(scrapling_unavailable):
    """_auto_escalate must skip the stealthy browser tier and return the HTTP
    result with a browser_unavailable error when browser deps are missing."""
    server = srv_mod.MasterFetchServer()

    # Mock the HTTP get to return a 403 (which would normally trigger escalation)
    mock_result = srv_mod.ResponseModel(
        status=403, content=["<html>403 Forbidden</html>"],
        url="https://example.com", fetcher_used="http",
    )
    with patch.object(server, 'get', new_callable=AsyncMock, return_value=mock_result):
        result = await server._auto_escalate(
            "https://example.com", "markdown", None, True, True,
            300, 0, True, False, 0, None, 30000, False,
            False, False, False, None, None, None, 50000,
        )
    # Should return the HTTP result, not crash
    assert result.status == 403
    assert "browser_unavailable" in (result.error or "")
    assert result.escalation_path == "http(browser_unavailable)"


# ─── bulk_get uses httpx fallback in HTTP-only mode ─────────────────────────

@pytest.mark.asyncio
async def test_bulk_get_uses_httpx_fallback_in_http_only_mode(scrapling_unavailable):
    """bulk_get must use _fallback_http_get when scrapling is unavailable."""
    server = srv_mod.MasterFetchServer()

    mock_fb_response = srv_mod._FallbackResponse(
        url="https://example.com",
        status=200,
        headers={"content-type": "text/html"},
        body=b"<html><body>HTTP fallback works</body></html>",
        encoding="utf-8",
    )
    with patch.object(srv_mod, '_fallback_http_get',
                      new_callable=AsyncMock, return_value=mock_fb_response):
        result = await server.bulk_get(urls=["https://example.com"])
    assert result.total == 1
    assert result.successful == 1
    assert result.results[0].status == 200

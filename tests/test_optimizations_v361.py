"""Regression tests for the v3.6.1 optimization/bug-fix pass.

Covers:
- `_smart_fetch_bulk` raises on too many URLs (was silent truncation).
- `_http_with_retry` does not retry deterministic SecurityError/ValueError.
- `_http_with_retry` still retries transport errors with backoff.
- `_force_fetch` http branch honors the caller's timeout (was hardcoded 30s).
- `_ensure_idle_monitor` no-ops in keep-alive-forever mode (timeout=0).
- `_close_auto_dynamic_session`, `_stealthy_auto_alive`, `_acquire_stealthy_session`
  are gone (dead-code removal).
- `domain_intel` module is gone.
- `hound -u` self-update stages the running launcher on Windows (WinError 32 fix).
"""

import asyncio
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from master_fetch.security import SecurityError
from master_fetch.server import (
    MasterFetchServer,
    MAX_BULK_URLS,
    AUTO_SESSION_IDLE_TIMEOUT,
    _hound_exe_path,
    _cleanup_old_launcher,
)


# ─── _smart_fetch_bulk: reject overflow instead of silent truncation ───────

class TestSmartFetchBulkRejectsOverflow:
    @pytest.mark.asyncio
    async def test_raises_on_too_many_urls(self):
        srv = MasterFetchServer()
        urls = [f"https://example.com/{i}" for i in range(MAX_BULK_URLS + 1)]
        with pytest.raises(ValueError, match="Too many URLs"):
            await srv._smart_fetch_bulk(
                urls, "markdown", None, True, True, 60, None, False,
                True, False, 0, None, 30000, False, True, True, True,
                None, None, None,
            )

    @pytest.mark.asyncio
    async def test_accepts_exactly_max(self):
        """Exactly MAX_BULK_URLS must not raise (boundary)."""
        from master_fetch.server import ResponseModel
        srv = MasterFetchServer()
        urls = [f"https://example.com/{i}" for i in range(MAX_BULK_URLS)]

        async def _stub_smart_fetch(**kwargs):
            return ResponseModel(status=200, content=["ok"], url=kwargs["url"])

        srv.smart_fetch = AsyncMock(side_effect=_stub_smart_fetch)
        # Should not raise.
        await srv._smart_fetch_bulk(
            urls, "markdown", None, True, True, 60, None, False,
            True, False, 0, None, 30000, False, True, True, True,
            None, None, None,
        )
        assert srv.smart_fetch.await_count == MAX_BULK_URLS


# ─── _http_with_retry: fail fast on validation, retry on transport ─────────

class TestHttpWithRetrySemantics:
    @pytest.mark.asyncio
    async def test_no_retry_on_security_error(self):
        srv = MasterFetchServer()
        srv.get = AsyncMock(side_effect=SecurityError("bad URL"))
        with pytest.raises(SecurityError):
            await srv._http_with_retry("https://example.com")
        assert srv.get.await_count == 1, "SecurityError must not be retried"

    @pytest.mark.asyncio
    async def test_no_retry_on_value_error(self):
        srv = MasterFetchServer()
        srv.get = AsyncMock(side_effect=ValueError("oversized body"))
        with pytest.raises(ValueError):
            await srv._http_with_retry("https://example.com")
        assert srv.get.await_count == 1, "ValueError must not be retried"

    @pytest.mark.asyncio
    async def test_retries_on_transport_error(self, monkeypatch):
        # Speed up the test: no real sleeping.
        monkeypatch.setattr("master_fetch.server.asyncio_sleep", AsyncMock())
        srv = MasterFetchServer()

        ok = MagicMock()
        ok.status = 200
        ok.content = ["hi"]
        ok.error = ""
        srv.get = AsyncMock(side_effect=[
            ConnectionError("boom"),
            ConnectionError("boom2"),
            ok,
        ])
        result = await srv._http_with_retry("https://example.com")
        assert srv.get.await_count == 3, "Transport errors must be retried"
        assert result is ok


# ─── _force_fetch: http branch honors caller timeout ───────────────────────

class TestForceFetchHttpTimeout:
    def _make_result(self):
        from master_fetch.server import ResponseModel
        return ResponseModel(
            status=200, content=["x"], url="https://example.com",
            fetcher_used="http", extracted_type="markdown",
        )

    @pytest.mark.asyncio
    async def test_http_branch_passes_converted_timeout(self):
        srv = MasterFetchServer()

        captured = {}

        async def fake_get(url, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return self._make_result()

        srv.get = AsyncMock(side_effect=fake_get)

        # Avoid real cache writes during finalize.
        import master_fetch.server as srv_mod
        orig = srv_mod.set_cached
        srv_mod.set_cached = AsyncMock(return_value=None)
        try:
            await srv._force_fetch(
                url="https://example.com",
                force_fetcher="http",
                extraction_type="markdown",
                css_selector=None,
                main_content_only=True,
                use_trafilatura=True,
                cache_ttl=0,  # skip cache path
                offset=0,
                headless=True, real_chrome=False, wait=0,
                proxy=None, timeout=5000, network_idle=False,
                solve_cloudflare=True, block_webrtc=True, hide_canvas=True,
                extra_headers=None, useragent=None, cookies=None,
            )
        finally:
            srv_mod.set_cached = orig

        # 5000ms -> 5s, under the 30s cap.
        assert captured["timeout"] == 5, (
            f"HTTP branch must convert ms->s. Expected 5, got {captured['timeout']}"
        )

    @pytest.mark.asyncio
    async def test_http_branch_caps_at_30s(self):
        srv = MasterFetchServer()
        captured = {}

        async def fake_get(url, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return self._make_result()

        srv.get = AsyncMock(side_effect=fake_get)
        import master_fetch.server as srv_mod
        orig = srv_mod.set_cached
        srv_mod.set_cached = AsyncMock(return_value=None)
        try:
            await srv._force_fetch(
                url="https://example.com", force_fetcher="http",
                extraction_type="markdown", css_selector=None,
                main_content_only=True, use_trafilatura=True,
                cache_ttl=0, offset=0,
                headless=True, real_chrome=False, wait=0,
                proxy=None, timeout=120000, network_idle=False,
                solve_cloudflare=True, block_webrtc=True, hide_canvas=True,
                extra_headers=None, useragent=None, cookies=None,
            )
        finally:
            srv_mod.set_cached = orig
        assert captured["timeout"] == 30, "timeout must cap at 30s for HTTP"


# ─── Idle monitor no-op in keep-alive-forever mode ─────────────────────────

class TestIdleMonitorNoOpWhenDisabled:
    def test_ensure_idle_monitor_does_not_start_task(self):
        assert AUTO_SESSION_IDLE_TIMEOUT == 0, (
            "These tests assume the default keep-alive-forever mode."
        )
        srv = MasterFetchServer()
        srv._ensure_idle_monitor()
        assert srv._idle_monitor_task is None, (
            "No monitor task should be created when AUTO_SESSION_IDLE_TIMEOUT == 0"
        )


# ─── Dead-code removal: methods/module no longer exist ─────────────────────

class TestDeadCodeRemoved:
    def test_close_auto_dynamic_session_removed(self):
        assert not hasattr(MasterFetchServer, "_close_auto_dynamic_session")

    def test_stealthy_auto_alive_removed(self):
        assert not hasattr(MasterFetchServer, "_stealthy_auto_alive")

    def test_acquire_stealthy_session_removed(self):
        assert not hasattr(MasterFetchServer, "_acquire_stealthy_session")

    def test_domain_intel_module_removed(self):
        import importlib
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("master_fetch.domain_intel")


# ─── hound -u self-update: Windows launcher staging (WinError 32 fix) ────────

class TestSelfUpdateLauncherStaging:
    """`hound -u` ran pip inside the running hound.exe, so Windows locked
    hound.exe against the overwrite pip was attempting (WinError 32). The fix
    renames the live launcher to hound.exe.old before pip runs; pip writes a
    fresh hound.exe; the .old is swept on the next launch.
    """

    def test_hound_exe_path_returns_str_or_none(self):
        p = _hound_exe_path()
        assert p is None or isinstance(p, str)

    def test_cleanup_noop_when_no_old(self, tmp_path):
        # Point _hound_exe_path at a temp exe with no .old sibling.
        exe = tmp_path / "hound.exe"
        exe.write_text("fake")
        with patch("master_fetch.server._hound_exe_path", return_value=str(exe)):
            _cleanup_old_launcher()
        assert exe.exists()
        assert not (tmp_path / "hound.exe.old").exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="launcher staging is Windows-only")
    def test_cleanup_removes_stale_old(self, tmp_path):
        exe = tmp_path / "hound.exe"
        exe.write_text("fake")
        old = tmp_path / "hound.exe.old"
        old.write_text("stale")
        with patch("master_fetch.server._hound_exe_path", return_value=str(exe)):
            _cleanup_old_launcher()
        assert exe.exists()
        assert not old.exists(), "stale .old must be swept on launch"

    @pytest.mark.skipif(sys.platform != "win32", reason="launcher staging is Windows-only")
    def test_do_update_renames_exe_before_pip_runs(self, tmp_path):
        from master_fetch.server import _do_update

        exe = tmp_path / "hound.exe"
        exe.write_text("live")

        state = {"renamed_at_pip_time": None}

        class FakeResult:
            returncode = 0
            stderr = ""
            stdout = ""

        def fake_run(cmd, **kwargs):
            # At the moment pip runs, the live exe must already be staged aside.
            state["renamed_at_pip_time"] = (
                not exe.exists() and (tmp_path / "hound.exe.old").exists()
            )
            return FakeResult()

        with patch("master_fetch.server._hound_exe_path", return_value=str(exe)), \
             patch("master_fetch.server._check_version",
                   side_effect=[("3.6.0", "3.6.1", False), ("3.6.1", "3.6.1", True)]), \
             patch("subprocess.run", side_effect=fake_run):
            _do_update()

        assert state["renamed_at_pip_time"] is True, (
            "hound.exe must be renamed to .old BEFORE pip runs, so pip can write a fresh one"
        )
        assert (tmp_path / "hound.exe.old").exists(), "staged .old must remain for post-exit sweep"

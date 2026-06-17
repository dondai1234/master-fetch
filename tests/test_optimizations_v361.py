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
    _hound_launcher_path,
    _stage_running_launcher,
    _cleanup_old_launcher,
    _spawn_detached_updater,
    _looks_like_file_lock_error,
    _do_update,
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


# ─── hound -u self-update: cross-platform, Windows-lock-safe ──────────────

class TestSelfUpdateLauncherStaging:
    """`hound -u` ran pip inside the running hound.exe, so Windows locked
    hound.exe against the overwrite pip was attempting (WinError 32). The fix
    stages the live launcher to hound.exe.old before pip runs (layer 1), with a
    detached background updater as a fallback when staging fails (layer 2).
    macOS/Linux have no file lock and skip staging entirely.
    """

    def test_launcher_path_returns_str_or_none(self):
        p = _hound_launcher_path()
        assert p is None or isinstance(p, str)

    def test_stage_returns_none_on_non_windows(self, tmp_path, monkeypatch):
        # Force POSIX: staging must be a no-op regardless of launcher presence.
        monkeypatch.setattr(sys, "platform", "linux")
        exe = tmp_path / "hound.exe"
        exe.write_text("fake")
        monkeypatch.setattr("master_fetch.server._hound_launcher_path", lambda: str(exe))
        assert _stage_running_launcher() is None
        assert exe.exists(), "POSIX staging must never touch the launcher"

    def test_cleanup_noop_when_no_old(self, tmp_path):
        exe = tmp_path / "hound.exe"
        exe.write_text("fake")
        with patch("master_fetch.server._hound_launcher_path", return_value=str(exe)):
            _cleanup_old_launcher()
        assert exe.exists()
        assert not (tmp_path / "hound.exe.old").exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="launcher staging is Windows-only")
    def test_cleanup_removes_stale_old(self, tmp_path):
        exe = tmp_path / "hound.exe"
        exe.write_text("fake")
        old = tmp_path / "hound.exe.old"
        old.write_text("stale")
        with patch("master_fetch.server._hound_launcher_path", return_value=str(exe)):
            _cleanup_old_launcher()
        assert exe.exists()
        assert not old.exists(), "stale .old must be swept on launch"

    @pytest.mark.skipif(sys.platform != "win32", reason="launcher staging is Windows-only")
    def test_stage_renames_running_exe(self, tmp_path):
        exe = tmp_path / "hound.exe"
        exe.write_text("live")
        with patch("master_fetch.server._hound_launcher_path", return_value=str(exe)):
            old = _stage_running_launcher()
        assert old is not None and old.endswith("hound.exe.old")
        assert not exe.exists(), "live launcher must be moved aside"
        assert (tmp_path / "hound.exe.old").exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="launcher staging is Windows-only")
    def test_stage_returns_none_when_rename_fails(self, tmp_path):
        exe = tmp_path / "missing.exe"  # os.rename on a missing src raises
        with patch("master_fetch.server._hound_launcher_path", return_value=str(exe)):
            assert _stage_running_launcher() is None

    @pytest.mark.skipif(sys.platform != "win32", reason="launcher staging is Windows-only")
    def test_do_update_renames_exe_before_pip_runs(self, tmp_path):
        exe = tmp_path / "hound.exe"
        exe.write_text("live")

        state = {"renamed_at_pip_time": None}

        class FakeResult:
            returncode = 0
            stderr = ""
            stdout = ""

        def fake_run(cmd, **kwargs):
            state["renamed_at_pip_time"] = (
                not exe.exists() and (tmp_path / "hound.exe.old").exists()
            )
            return FakeResult()

        with patch("master_fetch.server._hound_launcher_path", return_value=str(exe)), \
             patch("master_fetch.server._check_version",
                   side_effect=[("3.6.0", "3.6.1", False), ("3.6.1", "3.6.1", True)]), \
             patch("subprocess.run", side_effect=fake_run):
            _do_update()

        assert state["renamed_at_pip_time"] is True, (
            "hound.exe must be renamed to .old BEFORE pip runs, so pip can write a fresh one"
        )
        assert (tmp_path / "hound.exe.old").exists(), "staged .old must remain for post-exit sweep"


class TestSelfUpdateDetachedFallback:
    """Layer 2: when staging fails and pip hits the file lock, spawn a detached
    updater that runs pip after the current process exits.
    """

    def test_file_lock_detector(self):
        assert _looks_like_file_lock_error(
            "OSError: [WinError 32] The process cannot access the file "
            "because it is being used by another process: 'hound.exe'"
        )
        assert not _looks_like_file_lock_error("ERROR: No matching distribution")
        assert not _looks_like_file_lock_error("")

    def test_spawn_detached_updater_returns_false_on_popen_error(self):
        import subprocess as sp
        with patch.object(sp, "Popen", side_effect=OSError("boom")):
            assert _spawn_detached_updater(["py", "-m", "pip", "install", "-q", "x"]) is False

    def test_detached_updater_child_source_compiles_and_substitutes(self):
        """The generated one-liner handed to the detached child must be valid
        Python and must actually substitute r.returncode (not emit a literal
        '{r.returncode}'). Regression: an earlier version double-braced the
        placeholder, silently producing a useless log file.
        """
        import subprocess as sp
        captured = {}
        class FakeProc:
            pass
        def fake_popen(args, **kwargs):
            captured["args"] = args
            return FakeProc()
        with patch.object(sp, "Popen", side_effect=fake_popen):
            assert _spawn_detached_updater([sys.executable, "-m", "pip", "install", "-q", "pkg"]) is True
        # args = [python, '-c', child_src]
        child_src = captured["args"][2]
        # Must compile cleanly.
        compile(child_src, "<detached_updater>", "exec")
        # Must contain a real f-string substitution for r.returncode, not a literal.
        assert "f'pip returncode={r.returncode}" in child_src, (
            "child source must substitute r.returncode, not emit a literal"
        )
        assert "{{r.returncode}}" not in child_src, "double-braced placeholder must not survive"

    @pytest.mark.skipif(sys.platform != "win32", reason="detached fallback is Windows-only")
    def test_spawn_detached_updater_detaches_on_windows(self):
        import subprocess as sp
        with patch.object(sp, "Popen") as m:
            ok = _spawn_detached_updater(["py", "-m", "pip", "install", "-q", "x"])
        assert ok is True
        assert m.called
        flags = m.call_args.kwargs.get("creationflags", 0)
        assert flags & 0x00000008, "must set DETACHED_PROCESS on Windows"
        assert m.call_args.kwargs.get("close_fds") is True

    @pytest.mark.skipif(sys.platform != "win32", reason="detached fallback is Windows-only")
    def test_do_update_falls_back_to_detached_when_staging_fails(self, tmp_path, capsys):
        # Staging fails (launcher path missing) AND pip returns WinError 32 ->
        # _do_update must spawn the detached updater and return (no sys.exit).
        exe = tmp_path / "missing.exe"

        class LockResult:
            returncode = 1
            stderr = ("OSError: [WinError 32] The process cannot access the file "
                      "because it is being used by another process: 'hound.exe'")
            stdout = ""

        with patch("master_fetch.server._hound_launcher_path", return_value=str(exe)), \
             patch("master_fetch.server._check_version",
                   side_effect=[("3.6.0", "3.6.1", False), ("3.6.1", "3.6.1", True)]), \
             patch("subprocess.run", return_value=LockResult()), \
             patch("master_fetch.server._spawn_detached_updater", return_value=True) as mock_spawn:
            _do_update()  # must NOT raise / sys.exit
        out = capsys.readouterr().out
        assert mock_spawn.called, "detached updater must be spawned on file-lock failure"
        assert "hound -v" in out

    @pytest.mark.skipif(sys.platform != "win32", reason="detached fallback is Windows-only")
    def test_do_update_non_lock_failure_prints_recovery_and_exits(self, tmp_path, capsys):
        exe = tmp_path / "hound.exe"
        exe.write_text("live")  # staging succeeds -> no detached fallback

        class NetResult:
            returncode = 1
            stderr = "ERROR: Could not find a version that satisfies the requirement torch"
            stdout = ""

        with patch("master_fetch.server._hound_launcher_path", return_value=str(exe)), \
             patch("master_fetch.server._check_version",
                   side_effect=[("3.6.0", "3.6.1", False), ("3.6.1", "3.6.1", True)]), \
             patch("subprocess.run", return_value=NetResult()), \
             patch("master_fetch.server._spawn_detached_updater", return_value=True) as mock_spawn:
            with pytest.raises(SystemExit):
                _do_update()
        out = capsys.readouterr().out
        assert not mock_spawn.called, "non-lock failure must not spawn detached updater"
        assert "Manual recovery" in out


class TestSelfUpdatePosix:
    """On macOS/Linux there is no file lock; staging is skipped and pip runs
    synchronously. No Windows .exe logic is touched.
    """

    def test_do_update_posix_skips_staging(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "linux")

        class OkResult:
            returncode = 0
            stderr = ""
            stdout = ""

        with patch("master_fetch.server._check_version",
                   side_effect=[("3.6.0", "3.6.1", False), ("3.6.1", "3.6.1", True)]), \
             patch("subprocess.run", return_value=OkResult()) as mock_run, \
             patch("master_fetch.server._stage_running_launcher", return_value=None) as mock_stage:
            _do_update()
        out = capsys.readouterr().out
        assert mock_stage.called, "staging helper is consulted but no-ops on POSIX"
        assert mock_run.called, "pip must run synchronously on POSIX"
        assert "old launcher" not in out, "POSIX must not mention Windows .old cleanup"

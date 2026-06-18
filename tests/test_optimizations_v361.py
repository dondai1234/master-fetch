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
- `hound -u` self-update uses a detached console updater on Windows (WinError 32 fix).
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
    _looks_like_file_lock_error,
    _other_hound_pids,
    _stop_hound_cmd,
    _reinstall_cmd,
    _do_update,
    _run_pip_sync,
    _spawn_console_updater,
    _corrupted_install_message,
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


# ─── hound -u self-update: detached console updater (Windows lock fix) ──────

class TestSelfUpdateHelpers:
    """Platform helpers + process detection used by the updater."""

    def test_launcher_path_returns_str_or_none(self):
        p = _hound_launcher_path()
        assert p is None or isinstance(p, str)

    def test_stage_returns_none_on_non_windows(self, tmp_path, monkeypatch):
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

    def test_stop_hound_cmd_is_platform_aware(self, monkeypatch):
        import master_fetch.server as srv_mod
        monkeypatch.setattr(sys, "platform", "win32")
        assert srv_mod._stop_hound_cmd() == "taskkill /IM hound.exe /F"
        monkeypatch.setattr(sys, "platform", "linux")
        assert srv_mod._stop_hound_cmd() == "pkill -f hound"
        monkeypatch.setattr(sys, "platform", "darwin")
        assert srv_mod._stop_hound_cmd() == "pkill -f hound"

    def test_reinstall_cmd_format(self):
        assert _reinstall_cmd("3.6.7") == "pip install --force-reinstall --no-deps hound-mcp==3.6.7"

    def test_file_lock_detector(self):
        assert _looks_like_file_lock_error(
            "OSError: [WinError 32] The process cannot access the file "
            "because it is being used by another process: 'hound.exe'"
        )
        assert not _looks_like_file_lock_error("ERROR: No matching distribution")
        assert not _looks_like_file_lock_error("")

    def test_other_pids_parses_tasklist_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        fake = (
            '"hound.exe","11736","Console","10","3,776 K"\r\n'
            '"hound.exe","99999","Console","10","4,000 K"\r\n'
            '"python.exe","12345","Console","10","50,000 K"\r\n'
        )
        import subprocess as sp
        with patch.object(sp, "check_output", return_value=fake):
            pids = _other_hound_pids()
        assert 11736 in pids and 99999 in pids
        assert 12345 not in pids

    def test_other_pids_returns_empty_on_enumeration_failure(self):
        import subprocess as sp
        with patch.object(sp, "check_output", side_effect=FileNotFoundError("no tasklist")):
            assert _other_hound_pids() == []


class TestSpawnConsoleUpdater:
    """Windows: a child python.exe (not hound.exe) inherits the console, waits
    for the launcher to exit, then runs pip. Must be self-contained (no
    master_fetch import) so it survives the package being replaced.
    """

    @pytest.mark.skipif(sys.platform != "win32", reason="console updater is Windows-only")
    def test_spawns_python_dash_c_child(self):
        import subprocess as sp
        with patch.object(sp, "Popen") as m:
            ok = _spawn_console_updater([sys.executable, "-m", "pip", "install", "x"], "3.6.7")
        assert ok is True
        assert m.called
        args = m.call_args.args[0]
        assert args[0] == sys.executable and args[1] == "-c"
        # No detachment flags — must inherit the parent console so output shows.
        assert m.call_args.kwargs.get("creationflags", 0) == 0

    @pytest.mark.skipif(sys.platform != "win32", reason="console updater is Windows-only")
    def test_child_source_compiles_and_is_self_contained(self):
        import subprocess as sp
        captured = {}
        def fake_popen(args, **kw):
            captured["src"] = args[2]
            return MagicMock()
        with patch.object(sp, "Popen", side_effect=fake_popen):
            _spawn_console_updater([sys.executable, "-m", "pip", "install", "x"], "3.6.7")
        src = captured["src"]
        compile(src, "<console_updater>", "exec")  # must be valid Python
        assert "import master_fetch" not in src, "child must NOT depend on master_fetch"
        assert "taskkill /IM hound.exe /F" in src, "must embed the platform stop cmd"
        assert "pip install --force-reinstall --no-deps hound-mcp==3.6.7" in src, "must embed reinstall cmd"
        assert "time.sleep(2)" in src, "must wait for the parent launcher to exit"
        assert "tasklist" in src, "must re-check for a real server after parent exit"

    def test_returns_false_on_popen_error(self):
        import subprocess as sp
        with patch.object(sp, "Popen", side_effect=OSError("boom")):
            assert _spawn_console_updater(["x"], "3.6.7") is False


class TestRunPipSync:
    """`_run_pip_sync` runs pip synchronously with bulletproof messaging."""

    def _ok(self, rc=0):
        return MagicMock(returncode=rc)

    def test_success_prints_new_version(self, capsys):
        import master_fetch.server as srv
        with patch("subprocess.run", return_value=self._ok(0)), \
             patch.object(srv, "_check_version", return_value=("3.6.7", "3.6.7", True)):
            srv._run_pip_sync(["pip", "install", "x"], "3.6.7")
        assert "Hound v3.6.7" in capsys.readouterr().out

    def test_silent_no_op_detected(self, capsys):
        import master_fetch.server as srv
        with patch("subprocess.run", return_value=self._ok(0)), \
             patch.object(srv, "_check_version", return_value=("3.6.5", "3.6.7", False)):
            with pytest.raises(SystemExit):
                srv._run_pip_sync(["pip", "install", "x"], "3.6.7")
        out = capsys.readouterr().out
        assert "did not complete" in out
        assert "hound-mcp==3.6.7" in out

    def test_pip_failure_prints_recovery(self, capsys):
        import master_fetch.server as srv
        with patch("subprocess.run", return_value=self._ok(1)):
            with pytest.raises(SystemExit):
                srv._run_pip_sync(["pip", "install", "x"], "3.6.7")
        out = capsys.readouterr().out
        assert "pip returned 1" in out
        assert "taskkill /IM hound.exe /F" in out or "pkill -f hound" in out
        assert "hound-mcp==3.6.7" in out

    def test_timeout_prints_recovery(self, capsys):
        import subprocess, master_fetch.server as srv
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["pip"], timeout=300)):
            with pytest.raises(SystemExit):
                srv._run_pip_sync(["pip", "install", "x"], "3.6.7")
        out = capsys.readouterr().out
        assert "timed out" in out.lower()
        assert "hound-mcp==3.6.7" in out

    def test_corrupted_result_prints_recovery(self, capsys):
        import master_fetch.server as srv
        with patch("subprocess.run", return_value=self._ok(0)), \
             patch.object(srv, "_check_version", return_value=("unknown", "3.6.7", False)):
            with pytest.raises(SystemExit):
                srv._run_pip_sync(["pip", "install", "x"], "3.6.7")
        out = capsys.readouterr().out
        assert "metadata is missing" in out.lower()
        assert "hound-mcp==3.6.7" in out


class TestDoUpdate:
    """`_do_update` dispatch: Windows spawns the console updater; POSIX runs sync."""

    def test_already_latest(self, capsys):
        import master_fetch.server as srv
        with patch.object(srv, "_check_version", return_value=("3.6.7", "3.6.7", True)):
            srv._do_update()
        assert "Hound v3.6.7 (latest)" in capsys.readouterr().out

    def test_pypi_unreachable_message(self, capsys):
        import master_fetch.server as srv
        with patch.object(srv, "_check_version", return_value=("3.6.7", None, None)):
            srv._do_update()
        out = capsys.readouterr().out
        assert "couldn't reach PyPI" in out
        assert "pip install --upgrade hound-mcp[all]" in out

    @pytest.mark.skipif(sys.platform != "win32", reason="spawn path is Windows-only")
    def test_win32_spawns_console_updater_and_returns(self, monkeypatch, capsys):
        import master_fetch.server as srv
        monkeypatch.setattr(sys, "platform", "win32")
        with patch.object(srv, "_check_version", return_value=("3.6.5", "3.6.6", False)), \
             patch.object(srv, "_spawn_console_updater", return_value=True) as mock_spawn, \
             patch("subprocess.run") as mock_run:
            srv._do_update()  # must NOT sys.exit
        out = capsys.readouterr().out
        assert mock_spawn.called, "must delegate to the console updater on Windows"
        assert not mock_run.called, "parent must NOT run pip directly (the child does)"
        assert "finishes in this window" in out

    @pytest.mark.skipif(sys.platform != "win32", reason="spawn path is Windows-only")
    def test_win32_falls_back_to_sync_when_spawn_fails(self, monkeypatch, capsys):
        import master_fetch.server as srv
        monkeypatch.setattr(sys, "platform", "win32")
        with patch.object(srv, "_check_version", return_value=("3.6.5", "3.6.6", False)), \
             patch.object(srv, "_spawn_console_updater", return_value=False), \
             patch.object(srv, "_run_pip_sync") as mock_sync:
            srv._do_update()
        assert mock_sync.called, "must fall back to synchronous pip if spawn fails"

    def test_posix_runs_pip_sync(self, monkeypatch, capsys):
        import master_fetch.server as srv
        monkeypatch.setattr(sys, "platform", "linux")
        with patch.object(srv, "_check_version", return_value=("3.6.5", "3.6.6", False)), \
             patch.object(srv, "_other_hound_pids", return_value=[]), \
             patch.object(srv, "_run_pip_sync") as mock_sync:
            srv._do_update()
        out = capsys.readouterr().out
        assert mock_sync.called, "POSIX must run pip synchronously"
        assert "Updating v3.6.5 to v3.6.6" in out

    def test_posix_warns_about_running_server_but_proceeds(self, monkeypatch, capsys):
        import master_fetch.server as srv
        monkeypatch.setattr(sys, "platform", "linux")
        with patch.object(srv, "_check_version", return_value=("3.6.5", "3.6.6", False)), \
             patch.object(srv, "_other_hound_pids", return_value=[11111]), \
             patch.object(srv, "_run_pip_sync") as mock_sync:
            srv._do_update()
        out = capsys.readouterr().out
        assert "PID 11111" in out
        assert "restart" in out.lower()
        assert mock_sync.called, "POSIX must NOT refuse when a server is running (no file lock)"


class TestVersionCommand:
    """`hound -v` bulletproof messages."""

    def test_corrupted_shows_reinstall_cmd(self, monkeypatch, capsys):
        import master_fetch.server as srv
        monkeypatch.setattr(srv, "_check_version", lambda: ("unknown", "3.6.7", False))
        monkeypatch.setattr(sys, "argv", ["hound", "-v"])
        srv.main()
        out = capsys.readouterr().out
        assert "corrupted" in out.lower()
        assert "hound-mcp==3.6.7" in out
        assert "vunknown" not in out

    def test_pypi_unreachable(self, monkeypatch, capsys):
        import master_fetch.server as srv
        monkeypatch.setattr(srv, "_check_version", lambda: ("3.6.7", None, None))
        monkeypatch.setattr(sys, "argv", ["hound", "-v"])
        srv.main()
        out = capsys.readouterr().out
        assert "couldn't reach PyPI" in out

    def test_update_available(self, monkeypatch, capsys):
        import master_fetch.server as srv
        monkeypatch.setattr(srv, "_check_version", lambda: ("3.6.5", "3.6.7", False))
        monkeypatch.setattr(sys, "argv", ["hound", "-v"])
        srv.main()
        out = capsys.readouterr().out
        assert "v3.6.5" in out and "v3.6.7 available" in out
        assert "hound -u" in out

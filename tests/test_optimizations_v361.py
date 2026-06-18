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
    _looks_like_file_lock_error,
    _other_hound_pids,
    _stop_hound_cmd,
    _reinstall_cmd,
    _do_update,
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
             patch("master_fetch.server._other_hound_pids", return_value=[]), \
             patch("master_fetch.server._check_version",
                   side_effect=[("3.6.0", "3.6.1", False), ("3.6.1", "3.6.1", True)]), \
             patch("subprocess.run", side_effect=fake_run):
            _do_update()

        assert state["renamed_at_pip_time"] is True, (
            "hound.exe must be renamed to .old BEFORE pip runs, so pip can write a fresh one"
        )
        assert (tmp_path / "hound.exe.old").exists(), "staged .old must remain for post-exit sweep"


class TestSelfUpdateRunningServer:
    """A long-running hound MCP server holds hound.exe and blocks any in-place
    upgrade. `hound -u` must detect it BEFORE running pip and refuse, so pip
    never installs new metadata it can't pair with a new binary (the
    metadata/binary mismatch the detached fallback used to cause).
    """

    def test_file_lock_detector(self):
        assert _looks_like_file_lock_error(
            "OSError: [WinError 32] The process cannot access the file "
            "because it is being used by another process: 'hound.exe'"
        )
        assert not _looks_like_file_lock_error("ERROR: No matching distribution")
        assert not _looks_like_file_lock_error("")

    def test_other_pids_parses_tasklist_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        fake_tasklist = (
            '"hound.exe","11736","Console","10","3,776 K"\r\n'
            '"hound.exe","99999","Console","10","4,000 K"\r\n'
            '"python.exe","12345","Console","10","50,000 K"\r\n'
        )
        import subprocess as sp
        with patch.object(sp, "check_output", return_value=fake_tasklist):
            pids = _other_hound_pids()
        # Both hound.exe PIDs except our own (os.getpid() won't be 11736/99999).
        assert 11736 in pids and 99999 in pids
        assert 12345 not in pids, "non-hound processes must be ignored"

    def test_other_pids_parses_ps_posix(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        my_pid = __import__("os").getpid()
        fake_ps = (
            f"  {my_pid} hound\n"
            "  11736 hound\n"
            "  12345 python3\n"
            "  22001 /usr/local/bin/hound\n"
        )
        import subprocess as sp
        with patch.object(sp, "check_output", return_value=fake_ps):
            pids = _other_hound_pids()
        assert 11736 in pids and 22001 in pids, "other hound processes must be found"
        assert my_pid not in pids, "own PID must be excluded"
        assert 12345 not in pids, "non-hound processes must be ignored"

    def test_other_pids_returns_empty_on_enumeration_failure(self, monkeypatch):
        import subprocess as sp
        with patch.object(sp, "check_output", side_effect=FileNotFoundError("no tasklist")):
            assert _other_hound_pids() == [], "detection failure must not block the update"

    @pytest.mark.skipif(sys.platform != "win32", reason="abort path tested on win32")
    def test_do_update_aborts_when_other_hound_running(self, tmp_path, capsys):
        # A running hound MCP server (PID 11736) -> _do_update must refuse and
        # exit BEFORE calling pip (no metadata/binary mismatch).
        exe = tmp_path / "hound.exe"
        exe.write_text("live")

        with patch("master_fetch.server._hound_launcher_path", return_value=str(exe)), \
             patch("master_fetch.server._check_version",
                   side_effect=[("3.6.3", "3.6.4", False), ("3.6.4", "3.6.4", True)]), \
             patch("master_fetch.server._other_hound_pids", return_value=[11736]) as mock_pids, \
             patch("subprocess.run") as mock_run, \
             patch("master_fetch.server._stage_running_launcher") as mock_stage:
            with pytest.raises(SystemExit):
                _do_update()
        out = capsys.readouterr().out
        assert mock_pids.called, "must check for running hound processes"
        assert not mock_run.called, "must NOT run pip while the launcher is locked"
        assert not mock_stage.called, "must NOT stage before the running-server check"
        assert "PID 11736" in out
        assert "taskkill /IM hound.exe /F" in out
        assert "hound -u" in out

    @pytest.mark.skipif(sys.platform != "win32", reason="file-lock message tested on win32")
    def test_do_update_file_lock_failure_message_no_detached_spawn(self, tmp_path, capsys):
        # Detection missed the running server, pip hit the file lock anyway ->
        # honest message, no background spawn, no metadata/binary mismatch.
        exe = tmp_path / "hound.exe"
        exe.write_text("live")  # staging succeeds so we reach pip

        class LockResult:
            returncode = 1
            stderr = ("OSError: [WinError 32] The process cannot access the file "
                      "because it is being used by another process: 'hound.exe'")
            stdout = ""

        with patch("master_fetch.server._hound_launcher_path", return_value=str(exe)), \
             patch("master_fetch.server._check_version",
                   side_effect=[("3.6.3", "3.6.4", False), ("3.6.4", "3.6.4", True)]), \
             patch("master_fetch.server._other_hound_pids", return_value=[]), \
             patch("master_fetch.server._stage_running_launcher", return_value=str(exe) + ".old"), \
             patch("subprocess.run", return_value=LockResult()):
            with pytest.raises(SystemExit):
                _do_update()
        out = capsys.readouterr().out
        assert "locked by another process" in out
        assert "background" not in out.lower(), "must not promise a background updater"

    @pytest.mark.skipif(sys.platform != "win32", reason="recovery message tested on win32")
    def test_do_update_non_lock_failure_prints_recovery_and_exits(self, tmp_path, capsys):
        exe = tmp_path / "hound.exe"
        exe.write_text("live")

        class NetResult:
            returncode = 1
            stderr = "ERROR: Could not find a version that satisfies the requirement torch"
            stdout = ""

        with patch("master_fetch.server._hound_launcher_path", return_value=str(exe)), \
             patch("master_fetch.server._check_version",
                   side_effect=[("3.6.3", "3.6.4", False), ("3.6.4", "3.6.4", True)]), \
             patch("master_fetch.server._other_hound_pids", return_value=[]), \
             patch("master_fetch.server._stage_running_launcher", return_value=str(exe) + ".old"), \
             patch("subprocess.run", return_value=NetResult()):
            with pytest.raises(SystemExit):
                _do_update()
        out = capsys.readouterr().out
        assert "Recover manually" in out
        assert "--force-reinstall" in out and "hound-mcp==3.6.4" in out


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


class TestCorruptedInstallDiagnosis:
    """When hound-mcp metadata is missing (interrupted previous update),
    `hound -v` must print a clear recovery message instead of 'Hound vunknown',
    and `hound -u` must self-heal by reinstalling.
    """

    def test_corrupted_message_names_hound_mcp_not_hound(self):
        msg = _corrupted_install_message()
        assert "hound-mcp" in msg, "must steer users to the real package name"
        assert "--force-reinstall" in msg
        # The message must explicitly warn about the unrelated 'hound' package.
        assert "NOT 'hound'" in msg or "not 'hound'" in msg.lower()

    def test_version_command_diagnoses_corrupted_install(self, capsys, monkeypatch):
        import master_fetch.server as srv_mod
        # Simulate missing metadata: installed='unknown', latest retrievable.
        monkeypatch.setattr(srv_mod, "_check_version",
                           lambda: ("unknown", "3.6.4", False))
        argv = ["hound", "-v"]
        monkeypatch.setattr(sys, "argv", argv)
        srv_mod.main()
        out = capsys.readouterr().out
        assert "corrupted" in out.lower()
        assert "hound-mcp" in out, "recovery command must use the real package name"
        assert "vunknown" not in out, "must not print the useless 'vunknown'"
        assert "3.6.4" in out, "must surface the latest known version"

    def test_do_update_self_heals_corrupted_install(self, monkeypatch, capsys):
        # installed='unknown' -> _do_update should proceed to reinstall (self-heal)
        # rather than bail out, since this binary has the working updater.
        import master_fetch.server as srv_mod

        class OkResult:
            returncode = 0
            stderr = ""
            stdout = ""

        cv = MagicMock(side_effect=[("unknown", "3.6.4", False), ("3.6.4", "3.6.4", True)])
        with patch.object(srv_mod, "_check_version", side_effect=cv.side_effect), \
             patch.object(srv_mod, "_other_hound_pids", return_value=[]), \
             patch.object(srv_mod, "_stage_running_launcher", return_value=None), \
             patch("subprocess.run", return_value=OkResult()):
            srv_mod._do_update()
        out = capsys.readouterr().out
        assert "metadata is missing" in out.lower() or "reinstalling" in out.lower()
        assert "Hound v3.6.4" in out


class TestBulletproofErrorMessages:
    """Every `hound -u` / `hound -v` failure path must print an actionable,
    platform-aware recovery command. No silent no-ops, no dead-ends.
    """

    def test_stop_hound_cmd_is_platform_aware(self, monkeypatch):
        import master_fetch.server as srv_mod
        monkeypatch.setattr(sys, "platform", "win32")
        assert srv_mod._stop_hound_cmd() == "taskkill /IM hound.exe /F"
        monkeypatch.setattr(sys, "platform", "linux")
        assert srv_mod._stop_hound_cmd() == "pkill -f hound"
        monkeypatch.setattr(sys, "platform", "darwin")
        assert srv_mod._stop_hound_cmd() == "pkill -f hound"

    def test_reinstall_cmd_format(self):
        assert _reinstall_cmd("3.6.6") == "pip install --force-reinstall --no-deps hound-mcp==3.6.6"

    def test_do_update_detects_silent_no_op(self, tmp_path, capsys):
        # pip returns 0 but the version didn't advance (hound.exe couldn't be
        # replaced). Must be detected and explained, not silently reported as
        # the old version.
        import master_fetch.server as srv_mod
        exe = tmp_path / "hound.exe"
        exe.write_text("live")

        class OkResult:
            returncode = 0
            stderr = ""
            stdout = ""

        with patch.object(srv_mod, "_hound_launcher_path", return_value=str(exe)), \
             patch.object(srv_mod, "_check_version",
                          side_effect=[("3.6.4", "3.6.5", False), ("3.6.4", "3.6.5", False)]), \
             patch.object(srv_mod, "_other_hound_pids", return_value=[]), \
             patch.object(srv_mod, "_stage_running_launcher", return_value=str(exe) + ".old"), \
             patch("subprocess.run", return_value=OkResult()):
            with pytest.raises(SystemExit):
                _do_update()
        out = capsys.readouterr().out
        assert "did not complete" in out, "must flag the silent no-op"
        assert "v3.6.5" in out
        # Must include BOTH the stop command and the manual reinstall command.
        assert "taskkill /IM hound.exe /F" in out or "pkill -f hound" in out
        assert "--force-reinstall" in out and "hound-mcp==3.6.5" in out

    def test_do_update_pypi_unreachable_message(self, capsys):
        import master_fetch.server as srv_mod
        with patch.object(srv_mod, "_check_version", return_value=("3.6.5", None, None)):
            _do_update()  # must NOT sys.exit (just prints + returns)
        out = capsys.readouterr().out
        assert "couldn't reach PyPI" in out
        assert "pip install --upgrade hound-mcp[all]" in out

    def test_do_update_timeout_message(self, tmp_path, capsys):
        import subprocess, master_fetch.server as srv_mod
        exe = tmp_path / "hound.exe"
        exe.write_text("live")
        with patch.object(srv_mod, "_hound_launcher_path", return_value=str(exe)), \
             patch.object(srv_mod, "_check_version",
                          side_effect=[("3.6.4", "3.6.5", False), ("3.6.5", "3.6.5", True)]), \
             patch.object(srv_mod, "_other_hound_pids", return_value=[]), \
             patch.object(srv_mod, "_stage_running_launcher", return_value=str(exe) + ".old"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["pip"], timeout=300)):
            with pytest.raises(SystemExit):
                _do_update()
        out = capsys.readouterr().out
        assert "timed out" in out.lower()
        assert "--force-reinstall" in out and "hound-mcp==3.6.5" in out

    def test_version_command_pypi_unreachable(self, monkeypatch, capsys):
        import master_fetch.server as srv_mod
        monkeypatch.setattr(srv_mod, "_check_version", lambda: ("3.6.5", None, None))
        monkeypatch.setattr(sys, "argv", ["hound", "-v"])
        srv_mod.main()
        out = capsys.readouterr().out
        assert "couldn't reach PyPI" in out

    def test_version_command_corrupted_shows_reinstall_cmd(self, monkeypatch, capsys):
        import master_fetch.server as srv_mod
        monkeypatch.setattr(srv_mod, "_check_version", lambda: ("unknown", "3.6.6", False))
        monkeypatch.setattr(sys, "argv", ["hound", "-v"])
        srv_mod.main()
        out = capsys.readouterr().out
        assert "corrupted" in out.lower()
        assert "hound-mcp==3.6.6" in out, "corrupted path must print the exact reinstall cmd"
        assert "vunknown" not in out

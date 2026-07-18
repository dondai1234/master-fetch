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
)
from master_fetch.updater import (
    check_version, pad_version, do_update, print_version, doctor, rollback,
    cleanup_old_launcher, repair_script_path,
    _hound_launcher_path, _stop_hound_cmd, _looks_like_file_lock_error,
    _other_hound_pids, _build_helper_source, _spawn_helper,
    _write_repair_script, _read_last_version, _write_last_version,
    _pip_cmd, _heal_cmd, _run_pip, _diagnose,
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
    def test_ensure_idle_monitor_does_not_start_task(self, monkeypatch):
        # v9.2: the default is now 300s (idle close ON). These tests pin the
        # opt-out behavior: when the timeout is explicitly 0, the monitor task
        # is never started (keep-alive-forever mode, the old default).
        monkeypatch.setattr("master_fetch.server.AUTO_SESSION_IDLE_TIMEOUT", 0)
        srv = MasterFetchServer()
        srv._ensure_idle_monitor()
        assert srv._idle_monitor_task is None, (
            "No monitor task should be created when AUTO_SESSION_IDLE_TIMEOUT == 0"
        )

    @pytest.mark.asyncio
    async def test_ensure_idle_monitor_starts_task_when_enabled(self, monkeypatch):
        # v9.2: with a non-zero timeout the monitor task IS started.
        monkeypatch.setattr("master_fetch.server.AUTO_SESSION_IDLE_TIMEOUT", 300)
        srv = MasterFetchServer()
        srv._ensure_idle_monitor()
        assert srv._idle_monitor_task is not None, (
            "Monitor task should be created when AUTO_SESSION_IDLE_TIMEOUT > 0"
        )
        # cleanup: cancel so it doesn't leak out of the test
        if not srv._idle_monitor_task.done():
            srv._idle_monitor_task.cancel()
            try:
                await srv._idle_monitor_task
            except BaseException:
                pass


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


# ─── updater helpers ───────────────────────────────────────────────────────

class TestUpdaterHelpers:
    """Platform helpers + process detection used by the updater."""

    def test_launcher_path_returns_str_or_none(self):
        p = _hound_launcher_path()
        assert p is None or isinstance(p, str)

    def test_cleanup_old_launcher_noop_when_no_old(self, tmp_path):
        exe = tmp_path / "hound.exe"
        exe.write_text("fake")
        with patch("master_fetch.updater._hound_launcher_path", return_value=str(exe)):
            cleanup_old_launcher()
        assert exe.exists()
        assert not (tmp_path / "hound.exe.old").exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="launcher .old sweep is Windows-only")
    def test_cleanup_removes_stale_old(self, tmp_path):
        exe = tmp_path / "hound.exe"
        exe.write_text("fake")
        old = tmp_path / "hound.exe.old"
        old.write_text("stale")
        with patch("master_fetch.updater._hound_launcher_path", return_value=str(exe)):
            cleanup_old_launcher()
        assert exe.exists()
        assert not old.exists(), "stale .old must be swept on launch"

    def test_cleanup_noop_on_posix(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        with patch("master_fetch.updater._hound_launcher_path", return_value="/x/hound"):
            cleanup_old_launcher()  # must not touch anything on POSIX

    def test_stop_hound_cmd_is_platform_aware(self, monkeypatch):
        import master_fetch.updater as up
        monkeypatch.setattr(sys, "platform", "win32")
        assert up._stop_hound_cmd() == "taskkill /IM hound.exe /F"
        monkeypatch.setattr(sys, "platform", "linux")
        assert up._stop_hound_cmd() == "pkill -x hound"
        monkeypatch.setattr(sys, "platform", "darwin")
        assert up._stop_hound_cmd() == "pkill -x hound"

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

    def test_pip_cmd_is_no_deps_no_extras(self):
        cmd = _pip_cmd("10.2.0")
        assert "--no-deps" in cmd
        assert "hound-mcp==10.2.0" in cmd
        assert not any("[all]" in c for c in cmd), "must NOT pull the heavy [all] extra"

    def test_heal_cmd_is_force_reinstall_no_deps(self):
        cmd = _heal_cmd("10.2.0")
        assert "--force-reinstall" in cmd and "--no-deps" in cmd
        assert "hound-mcp==10.2.0" in cmd

    def test_diagnose_categories(self):
        assert "server" in _diagnose("[WinError 32] being used by another process: hound.exe")
        assert "PyPI" in _diagnose("ERROR: No matching distribution found")
        assert "timed out" in _diagnose("connection timed out")
        assert "pip failed" in _diagnose("some other error")


# ─── the detached Windows helper (standalone python -c) ────────────────────

class TestHelperSource:
    """The Windows helper is a standalone python -c (no master_fetch import)
    that waits for the parent, stages the launcher via the rename trick, runs
    pip --no-deps, self-heals, and points at repair.py on failure."""

    def test_compiles_and_is_self_contained(self):
        src = _build_helper_source("10.2.0", "/home/u/.hound/repair.py", 12345)
        compile(src, "<helper>", "exec")
        assert "import master_fetch" not in src, "helper must NOT depend on master_fetch"

    def test_installs_target_no_deps(self):
        src = _build_helper_source("10.2.0", "/r.py", 1)
        assert "--no-deps" in src
        assert ("hound-mcp==" + chr(34) + " + TARGET") in src, "installs the target version"
        assert repr("10.2.0") in src, "target version injected"

    def test_self_heals_with_force_reinstall(self):
        src = _build_helper_source("10.2.0", "/r.py", 1)
        assert "--force-reinstall" in src, "helper must self-heal"

    def test_stages_via_rename(self):
        src = _build_helper_source("10.2.0", "/r.py", 1)
        assert "os.rename(EXE, old)" in src, "frees the launcher via the rename trick"

    def test_waits_for_parent_exit(self):
        src = _build_helper_source("10.2.0", "/r.py", 999)
        assert "_wait_parent_exit" in src and repr(999) in src

    def test_points_at_repair_on_failure(self):
        src = _build_helper_source("10.2.0", "/home/u/.hound/repair.py", 1)
        assert "repair" in src.lower()
        assert repr("/home/u/.hound/repair.py") in src

    def test_spawn_inherits_console_so_output_shows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        # Mock the launcher lookup so _build_helper_source doesn't call
        # shutil.which with a faked win32 platform (its Windows branch hits
        # _winapi, which is None on Linux - breaks on Py 3.12+).
        monkeypatch.setattr("master_fetch.updater._hound_launcher_path",
                            lambda: "C:/x/hound.exe")
        import subprocess as sp
        with patch.object(sp, "Popen") as m:
            assert _spawn_helper("10.2.0", "/r.py", 1) is True
        assert m.call_args.kwargs.get("creationflags", 0) == 0, "must inherit the console"

    def test_spawn_returns_false_on_popen_error(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr("master_fetch.updater._hound_launcher_path",
                            lambda: "C:/x/hound.exe")
        import subprocess as sp
        with patch.object(sp, "Popen", side_effect=OSError("boom")):
            assert _spawn_helper("10.2.0", "/r.py", 1) is False


# ─── do_update: brick-proof self-update ─────────────────────────────────────

class TestDoUpdate:
    """POSIX runs pip inline + self-heals; Windows delegates to the helper.
    No failure path ever prints a bare destructive `pip --force-reinstall`."""

    @pytest.fixture(autouse=True)
    def _no_state_writes(self, monkeypatch):
        # don't touch the real ~/.hound during tests
        monkeypatch.setattr("master_fetch.updater._write_repair_script", lambda: None)
        monkeypatch.setattr("master_fetch.updater._write_last_version", lambda v: None)

    def test_already_latest(self, capsys):
        with patch("master_fetch.updater.check_version", return_value=("10.2.0", "10.2.0", True)):
            do_update()
        out = capsys.readouterr().out
        assert "v10.2.0" in out and "up to date" in out.lower()

    def test_pypi_unreachable(self, capsys):
        with patch("master_fetch.updater.check_version", return_value=("10.2.0", None, None)):
            do_update()
        out = capsys.readouterr().out
        assert "couldn't reach PyPI" in out
        assert "hound -u" in out

    def test_posix_inline_success(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "linux")
        checks = [("10.1.0", "10.2.0", False), ("10.2.0", "10.2.0", True),
                  ("10.2.0", "10.2.0", True)]
        with patch("master_fetch.updater.check_version", side_effect=lambda: checks.pop(0)), \
             patch("master_fetch.updater._other_hound_pids", return_value=[]), \
             patch("master_fetch.updater._run_pip", return_value=(0, "")) as m:
            do_update()
        out = capsys.readouterr().out
        assert m.called
        assert "v10.2.0" in out and "updated" in out.lower()

    def test_posix_self_heals_on_noop(self, monkeypatch, capsys):
        # first pip "succeeds" but the version doesn't advance (a server held
        # the file); the self-heal --force-reinstall pass must run + advance it.
        monkeypatch.setattr(sys, "platform", "linux")
        checks = [("10.1.0", "10.2.0", False), ("10.1.0", "10.2.0", False),
                  ("10.2.0", "10.2.0", True), ("10.2.0", "10.2.0", True)]
        with patch("master_fetch.updater.check_version", side_effect=lambda: checks.pop(0)), \
             patch("master_fetch.updater._other_hound_pids", return_value=[]), \
             patch("master_fetch.updater._run_pip", return_value=(0, "")) as m:
            do_update()
        out = capsys.readouterr().out
        assert m.call_count == 2, "must run pip then the self-heal pass"
        assert "recovering" in out.lower()
        assert "v10.2.0" in out and "updated" in out.lower()

    def test_posix_failure_points_at_repair_not_bare_pip(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "linux")
        checks = [("10.1.0", "10.2.0", False), ("unknown", "10.2.0", False),
                  ("unknown", "10.2.0", False)]
        with patch("master_fetch.updater.check_version", side_effect=lambda: checks.pop(0)), \
             patch("master_fetch.updater._other_hound_pids", return_value=[]), \
             patch("master_fetch.updater._run_pip", return_value=(1, "WinError 32 hound.exe")):
            with pytest.raises(SystemExit):
                do_update()
        out = capsys.readouterr().out
        assert "update failed" in out.lower()
        assert "repair.py" in out, "must point at the safe repair script"
        assert "force-reinstall" not in out, "must NOT print a bare destructive pip command"

    def test_win32_spawns_helper(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "win32")
        with patch("master_fetch.updater.check_version", return_value=("10.1.0", "10.2.0", False)), \
             patch("master_fetch.updater._spawn_helper", return_value=True) as m:
            do_update()
        out = capsys.readouterr().out
        assert m.called, "Windows must delegate to the detached helper"
        assert "completes in this window" in out

    def test_win32_spawn_fail_points_at_repair(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "win32")
        with patch("master_fetch.updater.check_version", return_value=("10.1.0", "10.2.0", False)), \
             patch("master_fetch.updater._spawn_helper", return_value=False):
            do_update()
        out = capsys.readouterr().out
        assert "repair.py" in out
        assert "force-reinstall" not in out

    def test_corrupted_install_proceeds_to_install(self, monkeypatch, capsys):
        # installed == "unknown" must NOT just print a message and stop - it
        # must install the latest to recover.
        monkeypatch.setattr(sys, "platform", "linux")
        checks = [("unknown", "10.2.0", False), ("10.2.0", "10.2.0", True),
                  ("10.2.0", "10.2.0", True)]
        with patch("master_fetch.updater.check_version", side_effect=lambda: checks.pop(0)), \
             patch("master_fetch.updater._other_hound_pids", return_value=[]), \
             patch("master_fetch.updater._run_pip", return_value=(0, "")) as m:
            do_update()
        out = capsys.readouterr().out
        assert m.called, "corrupted install must still attempt the install"
        assert "v10.2.0" in out


# ─── hound -v ───────────────────────────────────────────────────────────────

class TestVersionCommand:
    """`hound -v` bulletproof, non-destructive messages."""

    def test_corrupted_points_at_repair(self, monkeypatch, capsys):
        monkeypatch.setattr("master_fetch.updater.check_version",
                            lambda: ("unknown", "10.2.0", False))
        monkeypatch.setattr(sys, "argv", ["hound", "-v"])
        import master_fetch.server as srv
        srv.main()
        out = capsys.readouterr().out
        assert "corrupted" in out.lower()
        assert "repair.py" in out, "must point at the safe repair script"
        assert "force-reinstall" not in out, "no bare destructive pip command"
        assert "vunknown" not in out

    def test_pypi_unreachable(self, monkeypatch, capsys):
        monkeypatch.setattr("master_fetch.updater.check_version",
                            lambda: ("10.2.0", None, None))
        monkeypatch.setattr(sys, "argv", ["hound", "-v"])
        import master_fetch.server as srv
        srv.main()
        out = capsys.readouterr().out
        assert "couldn't reach PyPI" in out

    def test_update_available(self, monkeypatch, capsys):
        monkeypatch.setattr("master_fetch.updater.check_version",
                            lambda: ("10.1.0", "10.2.0", False))
        monkeypatch.setattr(sys, "argv", ["hound", "-v"])
        import master_fetch.server as srv
        srv.main()
        out = capsys.readouterr().out
        assert "v10.1.0" in out and "v10.2.0 available" in out
        assert "hound -u" in out


# ─── hound doctor ───────────────────────────────────────────────────────────

class TestDoctor:
    """Proactive health check - catches a half-broken install before it bricks."""

    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch, tmp_path):
        # no network, no real ~/.hound writes
        monkeypatch.setattr("master_fetch.updater.check_version",
                            lambda: ("10.2.0", "10.2.0", True))
        monkeypatch.setattr("master_fetch.updater._hound_home", lambda: str(tmp_path))
        monkeypatch.setattr("master_fetch.updater.repair_script_path",
                            lambda: str(tmp_path / "repair.py"))
        monkeypatch.setattr("master_fetch.updater._state_path",
                            lambda n: str(tmp_path / n))

    def test_runs_and_reports_every_check(self, capsys):
        doctor()
        out = capsys.readouterr().out
        assert "Hound" in out
        for label in ["launcher resolves", "package imports", "metadata consistent",
                      "launcher clean", "repair script ready", "core dependencies",
                      "PyPI reachable"]:
            assert label in out, f"missing check label: {label}"

    def test_writes_repair_script(self, tmp_path):
        doctor()
        rp = tmp_path / "repair.py"
        assert rp.exists(), "doctor must ensure the repair script exists"
        src = rp.read_text(encoding="utf-8")
        compile(src, "<repair>", "exec")
        assert "import master_fetch" not in src, "repair must be standalone"
        assert "force-reinstall" in src

    def test_flags_missing_dep_and_offers_fix(self, monkeypatch, capsys):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "httpx":
                raise ImportError("no httpx")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        doctor()
        out = capsys.readouterr().out
        assert "httpx" in out
        assert "fix deps" in out.lower()


# ─── hound --rollback ───────────────────────────────────────────────────────

class TestRollback:
    """Undo a bad update by reinstalling the previously-recorded version."""

    def test_nothing_to_rollback(self, monkeypatch, capsys):
        monkeypatch.setattr("master_fetch.updater._read_last_version", lambda: None)
        rollback()
        out = capsys.readouterr().out
        assert "nothing to roll back" in out.lower()

    def test_already_at_previous_version(self, monkeypatch, capsys):
        monkeypatch.setattr("master_fetch.updater._read_last_version", lambda: "10.2.0")
        monkeypatch.setattr("master_fetch.updater.check_version",
                            lambda: ("10.2.0", "10.2.0", True))
        rollback()
        out = capsys.readouterr().out
        assert "already at" in out.lower()

    def test_rollback_reinstalls_previous_version(self, monkeypatch):
        monkeypatch.setattr("master_fetch.updater._read_last_version", lambda: "10.1.0")
        monkeypatch.setattr("master_fetch.updater.check_version",
                            lambda: ("10.2.0", "10.2.0", True))
        with patch("master_fetch.updater.do_update") as m:
            rollback()
        m.assert_called_once_with(target="10.1.0")


# ─── ~/.hound/repair.py: the surviving brick-recovery script ────────────────

class TestRepairScript:
    """repair.py lives outside site-packages so a failed pip uninstall of
    hound-mcp never removes it - it's the recovery when hound is bricked."""

    def test_writes_compiling_standalone_script(self, tmp_path, monkeypatch):
        monkeypatch.setattr("master_fetch.updater._hound_home", lambda: str(tmp_path))
        monkeypatch.setattr("master_fetch.updater.repair_script_path",
                            lambda: str(tmp_path / "repair.py"))
        _write_repair_script()
        rp = tmp_path / "repair.py"
        assert rp.exists()
        src = rp.read_text(encoding="utf-8")
        compile(src, "<repair>", "exec")
        assert "import master_fetch" not in src, "repair must not depend on master_fetch"
        assert "force-reinstall" in src
        assert "taskkill" in src and "hound.exe" in src
        # pkill must use -x (exact match) so the repair script (python) isn't killed
        assert "pkill" in src and '"-x"' in src

    def test_does_not_raise_when_home_not_writable(self, monkeypatch):
        monkeypatch.setattr("master_fetch.updater.repair_script_path",
                            lambda: "/nonexistent/dir/repair.py")
        _write_repair_script()  # must swallow the OSError, not raise

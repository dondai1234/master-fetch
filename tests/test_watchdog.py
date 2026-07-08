"""Tests for the browser watchdog (v9.1.3): pause idle Chrome to free RAM."""

import os
import asyncio
import signal
import sys
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from master_fetch.server import MasterFetchServer


class TestWatchdogConfig:
    def test_default_idle_timeout(self, monkeypatch):
        monkeypatch.delenv("HOUND_BROWSER_IDLE_TIMEOUT", raising=False)
        srv = MasterFetchServer()
        assert srv._watchdog_idle_timeout == 60

    def test_env_var_sets_timeout(self, monkeypatch):
        monkeypatch.setenv("HOUND_BROWSER_IDLE_TIMEOUT", "30")
        srv = MasterFetchServer()
        assert srv._watchdog_idle_timeout == 30

    def test_env_var_zero_disables_watchdog(self, monkeypatch):
        monkeypatch.setenv("HOUND_BROWSER_IDLE_TIMEOUT", "0")
        srv = MasterFetchServer()
        assert srv._watchdog_idle_timeout == 0

    def test_initial_state(self):
        srv = MasterFetchServer()
        assert srv._auto_stealthy_pids == []
        assert srv._auto_stealthy_busy_count == 0
        assert srv._auto_stealthy_paused is False
        assert srv._auto_stealthy_last_idle_time == 0.0
        assert srv._watchdog_task is None
        assert srv._watchdog_shutdown is False


class TestFindBrowserPids:
    def test_no_children_returns_empty(self, monkeypatch):
        srv = MasterFetchServer()
        mock_parent = MagicMock()
        mock_parent.children.return_value = []
        monkeypatch.setattr("psutil.Process", lambda pid: mock_parent)
        pids = srv._find_browser_pids()
        assert pids == []

    def test_finds_chrome_named_process(self, monkeypatch):
        srv = MasterFetchServer()
        mock_parent = MagicMock()
        mock_child = MagicMock()
        mock_child.pid = 12345
        mock_child.name.return_value = "chrome"
        mock_parent.children.return_value = [mock_child]

        monkeypatch.setattr("psutil.Process", lambda pid: mock_parent)
        pids = srv._find_browser_pids()
        assert 12345 in pids

    def test_finds_chromium_named_process(self, monkeypatch):
        srv = MasterFetchServer()
        mock_parent = MagicMock()
        mock_child = MagicMock()
        mock_child.pid = 67890
        mock_child.name.return_value = "chromium-browser"
        mock_parent.children.return_value = [mock_child]

        monkeypatch.setattr("psutil.Process", lambda pid: mock_parent)
        pids = srv._find_browser_pids()
        assert 67890 in pids

    def test_filters_non_chrome_children(self, monkeypatch):
        srv = MasterFetchServer()
        mock_parent = MagicMock()
        chrome_child = MagicMock()
        chrome_child.pid = 111
        chrome_child.name.return_value = "chrome"
        python_child = MagicMock()
        python_child.pid = 222
        python_child.name.return_value = "python"
        mock_parent.children.return_value = [chrome_child, python_child]

        monkeypatch.setattr("psutil.Process", lambda pid: mock_parent)
        pids = srv._find_browser_pids()
        assert pids == [111]

    def test_handles_no_such_process(self, monkeypatch):
        srv = MasterFetchServer()
        mock_parent = MagicMock()
        import psutil
        mock_child = MagicMock()
        mock_child.name.side_effect = psutil.NoSuchProcess(123)
        mock_parent.children.return_value = [mock_child]
        monkeypatch.setattr("psutil.Process", lambda pid: mock_parent)
        pids = srv._find_browser_pids()
        assert pids == []

    def test_handles_psutil_failure_gracefully(self, monkeypatch):
        srv = MasterFetchServer()
        monkeypatch.setattr("psutil.Process", lambda pid: (_ for _ in ()).throw(RuntimeError("boom")))
        pids = srv._find_browser_pids()
        assert pids == []


class TestResumeBrowserIfPaused:
    def test_noop_when_not_paused(self):
        srv = MasterFetchServer()
        srv._auto_stealthy_paused = False
        srv._resume_browser_if_paused()
        assert srv._auto_stealthy_paused is False

    def test_noop_when_no_pids(self):
        srv = MasterFetchServer()
        srv._auto_stealthy_paused = True
        srv._auto_stealthy_pids = []
        srv._resume_browser_if_paused()
        assert srv._auto_stealthy_paused is False

    def test_sends_sigcont_on_linux(self, monkeypatch):
        if sys.platform == "win32":
            pytest.skip("SIGCONT is POSIX-only")
        srv = MasterFetchServer()
        srv._auto_stealthy_paused = True
        srv._auto_stealthy_pids = [12345, 12346]
        mock_kill = MagicMock()
        monkeypatch.setattr(os, "kill", mock_kill)
        monkeypatch.setattr(sys, "platform", "linux")
        srv._resume_browser_if_paused()
        assert srv._auto_stealthy_paused is False
        assert mock_kill.call_count == 2
        mock_kill.assert_any_call(12345, signal.SIGCONT)
        mock_kill.assert_any_call(12346, signal.SIGCONT)

    def test_sigcont_errors_are_swallowed(self, monkeypatch):
        if sys.platform == "win32":
            pytest.skip("SIGCONT is POSIX-only")
        srv = MasterFetchServer()
        srv._auto_stealthy_paused = True
        srv._auto_stealthy_pids = [12345]
        mock_kill = MagicMock(side_effect=ProcessLookupError)
        monkeypatch.setattr(os, "kill", mock_kill)
        monkeypatch.setattr(sys, "platform", "linux")
        srv._resume_browser_if_paused()
        assert srv._auto_stealthy_paused is False

    def test_uses_psutil_resume_on_windows(self, monkeypatch):
        srv = MasterFetchServer()
        srv._auto_stealthy_paused = True
        srv._auto_stealthy_pids = [12345]
        mock_proc = MagicMock()
        mock_proc.resume = MagicMock()
        monkeypatch.setattr("psutil.Process", lambda pid: mock_proc)
        monkeypatch.setattr(sys, "platform", "win32")
        srv._resume_browser_if_paused()
        assert srv._auto_stealthy_paused is False
        mock_proc.resume.assert_called_once()

    def test_resets_last_idle_time(self):
        srv = MasterFetchServer()
        srv._auto_stealthy_paused = True
        srv._auto_stealthy_pids = [1]
        srv._auto_stealthy_last_idle_time = 100.0
        srv._resume_browser_if_paused()
        assert srv._auto_stealthy_last_idle_time > 100.0


class TestMarkBrowserBusyIdle:
    @pytest.mark.asyncio
    async def test_busy_increments_counter(self):
        srv = MasterFetchServer()
        await srv._mark_browser_busy()
        assert srv._auto_stealthy_busy_count == 1

    @pytest.mark.asyncio
    async def test_busy_concurrent(self):
        srv = MasterFetchServer()
        await asyncio.gather(
            srv._mark_browser_busy(),
            srv._mark_browser_busy(),
            srv._mark_browser_busy(),
        )
        assert srv._auto_stealthy_busy_count == 3

    @pytest.mark.asyncio
    async def test_idle_decrements_counter(self):
        srv = MasterFetchServer()
        srv._auto_stealthy_busy_count = 2
        await srv._mark_browser_idle()
        assert srv._auto_stealthy_busy_count == 1
        await srv._mark_browser_idle()
        assert srv._auto_stealthy_busy_count == 0

    @pytest.mark.asyncio
    async def test_idle_records_timestamp_when_reaches_zero(self):
        srv = MasterFetchServer()
        srv._auto_stealthy_busy_count = 1
        assert srv._auto_stealthy_last_idle_time == 0.0
        await srv._mark_browser_idle()
        assert srv._auto_stealthy_last_idle_time > 0.0

    @pytest.mark.asyncio
    async def test_idle_never_goes_negative(self):
        srv = MasterFetchServer()
        await srv._mark_browser_idle()
        assert srv._auto_stealthy_busy_count == 0
        await srv._mark_browser_idle()
        assert srv._auto_stealthy_busy_count == 0


class TestPauseBrowser:
    @pytest.mark.asyncio
    async def test_noop_when_no_pids_and_none_found(self, monkeypatch):
        srv = MasterFetchServer()
        monkeypatch.setattr(srv, "_find_browser_pids", lambda: [])
        await srv._pause_browser()
        assert srv._auto_stealthy_paused is False

    @pytest.mark.asyncio
    async def test_noop_when_busy(self, monkeypatch):
        srv = MasterFetchServer()
        srv._auto_stealthy_pids = [12345]
        srv._auto_stealthy_busy_count = 1
        monkeypatch.setattr(os, "kill", MagicMock())
        monkeypatch.setattr(sys, "platform", "linux")
        await srv._pause_browser()
        assert srv._auto_stealthy_paused is False

    @pytest.mark.asyncio
    async def test_sends_sigstop_on_linux(self, monkeypatch):
        if sys.platform == "win32":
            pytest.skip("SIGSTOP is POSIX-only")
        srv = MasterFetchServer()
        srv._auto_stealthy_pids = [12345, 12346]
        mock_kill = MagicMock()
        monkeypatch.setattr(os, "kill", mock_kill)
        monkeypatch.setattr(sys, "platform", "linux")
        await srv._pause_browser()
        assert srv._auto_stealthy_paused is True
        assert mock_kill.call_count == 2
        mock_kill.assert_any_call(12345, signal.SIGSTOP)
        mock_kill.assert_any_call(12346, signal.SIGSTOP)

    @pytest.mark.asyncio
    async def test_uses_psutil_suspend_on_windows(self, monkeypatch):
        srv = MasterFetchServer()
        srv._auto_stealthy_pids = [12345]
        mock_proc = MagicMock()
        mock_proc.suspend = MagicMock()
        monkeypatch.setattr("psutil.Process", lambda pid: mock_proc)
        monkeypatch.setattr(sys, "platform", "win32")
        await srv._pause_browser()
        assert srv._auto_stealthy_paused is True
        mock_proc.suspend.assert_called_once()


class TestEnsureWatchdog:
    @pytest.mark.asyncio
    async def test_does_not_start_when_disabled(self):
        srv = MasterFetchServer()
        srv._watchdog_idle_timeout = 0
        await srv._ensure_watchdog()
        assert srv._watchdog_task is None

    @pytest.mark.asyncio
    async def test_starts_when_enabled(self):
        srv = MasterFetchServer()
        srv._watchdog_idle_timeout = 60
        await srv._ensure_watchdog()
        assert srv._watchdog_task is not None
        assert not srv._watchdog_task.done()
        srv._watchdog_shutdown = True
        srv._watchdog_task.cancel()
        try:
            await srv._watchdog_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_idempotent(self):
        srv = MasterFetchServer()
        srv._watchdog_idle_timeout = 60
        await srv._ensure_watchdog()
        task1 = srv._watchdog_task
        await srv._ensure_watchdog()
        assert srv._watchdog_task is task1
        srv._watchdog_shutdown = True
        srv._watchdog_task.cancel()
        try:
            await srv._watchdog_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_restarts_if_task_crashed(self):
        srv = MasterFetchServer()
        srv._watchdog_idle_timeout = 60
        await srv._ensure_watchdog()
        srv._watchdog_task.cancel()
        try:
            await srv._watchdog_task
        except asyncio.CancelledError:
            pass
        await srv._ensure_watchdog()
        assert srv._watchdog_task is not None
        assert not srv._watchdog_task.done()
        srv._watchdog_shutdown = True
        srv._watchdog_task.cancel()
        try:
            await srv._watchdog_task
        except asyncio.CancelledError:
            pass


class TestShutdownCloseSessions:
    @pytest.mark.asyncio
    async def test_resumes_before_close(self):
        srv = MasterFetchServer()
        resumed = False

        def fake_resume():
            nonlocal resumed
            resumed = True
        srv._resume_browser_if_paused = fake_resume
        with patch.object(srv, "_watchdog_task", None):
            await srv._shutdown_close_sessions()
        assert resumed is True

    @pytest.mark.asyncio
    async def test_cancels_watchdog_task(self):
        srv = MasterFetchServer()
        srv._auto_stealthy_paused = False
        srv._auto_stealthy_pids = []

        async def fake_loop():
            try:
                while not srv._watchdog_shutdown:
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                raise
            finally:
                srv._watchdog_task = None
        srv._watchdog_task = asyncio.create_task(fake_loop())
        await asyncio.sleep(0.05)  # let task start and begin sleeping

        await srv._shutdown_close_sessions()

        assert srv._watchdog_shutdown is True
        assert srv._watchdog_task is None


class TestWatchdogEnvVarDisabled:
    @pytest.mark.asyncio
    async def test_ensure_watchdog_noop_when_disabled_by_init(self):
        srv = MasterFetchServer()
        srv._watchdog_idle_timeout = 0
        await srv._ensure_watchdog()
        assert srv._watchdog_task is None

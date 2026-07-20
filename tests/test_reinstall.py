"""Tests for the --reinstall command and --doctor [all] extras check.

Tests:
- _pip_cmd_full produces the right pip command (force-reinstall + [all] + pinned)
- reinstall() is callable and exported
- doctor() includes the [all] extras check
- helper source includes FULL mode branching
- _spawn_helper accepts the full parameter
- --reinstall appears in --help
"""
import pytest
from unittest.mock import patch, MagicMock
import subprocess
import sys


class TestPipCmdFull:
    """The full reinstall pip command must pin version + include [all] + force-reinstall."""

    def test_pip_cmd_full_uses_force_reinstall_no_deps(self):
        """Full reinstall uses --force-reinstall --no-deps: forces hound-mcp + [all]
        extras reinstall without touching transitive deps (pydantic vs pydantic-core)."""
        from master_fetch.updater import _pip_cmd_full
        cmd = _pip_cmd_full("10.4.0")
        assert "--force-reinstall" in cmd
        assert "--no-deps" in cmd

    def test_pip_cmd_full_has_all_extra(self):
        from master_fetch.updater import _pip_cmd_full
        cmd = _pip_cmd_full("10.4.0")
        # Must include [all] in the package spec
        pkg_spec = [c for c in cmd if "hound-mcp" in c][0]
        assert "[all]" in pkg_spec

    def test_pip_cmd_full_pins_version(self):
        from master_fetch.updater import _pip_cmd_full
        cmd = _pip_cmd_full("10.4.0")
        pkg_spec = [c for c in cmd if "hound-mcp" in c][0]
        assert "==10.4.0" in pkg_spec

    def test_pip_cmd_full_uses_python_executable(self):
        from master_fetch.updater import _pip_cmd_full
        cmd = _pip_cmd_full("10.4.0")
        assert cmd[0] == sys.executable
        assert "-m" in cmd
        assert "pip" in cmd

    def test_pip_cmd_full_has_no_deps(self):
        """Full reinstall uses --no-deps to prevent transitive dep breakage.
        The [all] extras are still installed (they're part of the package spec,
        not transitive deps)."""
        from master_fetch.updater import _pip_cmd_full
        cmd = _pip_cmd_full("10.4.0")
        assert "--no-deps" in cmd


class TestReinstallExported:
    """reinstall() must be in __all__ and callable."""

    def test_reinstall_in_all(self):
        from master_fetch import updater
        assert "reinstall" in updater.__all__

    def test_reinstall_callable(self):
        from master_fetch import updater
        assert callable(updater.reinstall)


class TestDoctorExtrasCheck:
    """doctor() must check for [all] extras (onnxruntime, tokenizers, rapidocr)."""

    def test_doctor_callable(self):
        from master_fetch import updater
        assert callable(updater.doctor)

    def test_doctor_checks_optional_imports(self):
        """The doctor function should reference the [all] extras modules."""
        from master_fetch import updater
        import inspect
        src = inspect.getsource(updater.doctor)
        assert "onnxruntime" in src
        assert "tokenizers" in src
        assert "rapidocr" in src

    def test_doctor_suggests_pip_for_extras(self):
        """When [all] extras are missing, doctor should suggest pip install hound-mcp[all]
        (not hound --reinstall, which uses --no-deps and can't install extras)."""
        from master_fetch import updater
        import inspect
        src = inspect.getsource(updater.doctor)
        assert "pip install hound-mcp[all]" in src


class TestHelperFullMode:
    """The detached helper source must support FULL mode for reinstall."""

    def test_build_helper_source_accepts_full(self):
        from master_fetch.updater import _build_helper_source
        import inspect
        sig = inspect.signature(_build_helper_source)
        assert "full" in sig.parameters
        assert sig.parameters["full"].default is False

    def test_spawn_helper_accepts_full(self):
        from master_fetch.updater import _spawn_helper
        import inspect
        sig = inspect.signature(_spawn_helper)
        assert "full" in sig.parameters
        assert sig.parameters["full"].default is False

    def test_helper_source_has_full_variable(self):
        from master_fetch.updater import _build_helper_source
        src = _build_helper_source("10.4.0", "/tmp/repair.py", 12345, full=True)
        assert "FULL = True" in src

    def test_helper_source_full_false_by_default(self):
        from master_fetch.updater import _build_helper_source
        src = _build_helper_source("10.4.0", "/tmp/repair.py", 12345)
        assert "FULL = False" in src

    def test_helper_source_full_uses_all_extra(self):
        """When FULL=True, the pip command should use hound-mcp[all]== not --no-deps."""
        from master_fetch.updater import _build_helper_source
        src = _build_helper_source("10.4.0", "/tmp/repair.py", 12345, full=True)
        assert "hound-mcp[all]==" in src

    def test_helper_source_update_uses_no_deps(self):
        """When FULL=False (update mode), the pip command should use --no-deps."""
        from master_fetch.updater import _build_helper_source
        src = _build_helper_source("10.4.0", "/tmp/repair.py", 12345, full=False)
        assert "--no-deps" in src
        assert "hound-mcp==" in src

    def test_helper_source_full_stops_hound(self):
        """When FULL=True, the helper should stop all hound processes before staging."""
        from master_fetch.updater import _build_helper_source
        src = _build_helper_source("10.4.0", "/tmp/repair.py", 12345, full=True)
        assert "if FULL:" in src
        assert "_stop_all_hound" in src


class TestReinstallVsUpdate:
    """reinstall and update should produce different pip commands."""

    def test_reinstall_cmd_differs_from_update_cmd(self):
        from master_fetch.updater import _pip_cmd, _pip_cmd_full
        update_cmd = _pip_cmd("10.4.0")
        reinstall_cmd = _pip_cmd_full("10.4.0")
        assert update_cmd != reinstall_cmd

    def test_update_and_reinstall_both_have_no_deps(self):
        """Both update and reinstall use --no-deps (different reasons):
        - update: can't fail on heavy deps (the brick root cause)
        - reinstall: can't break transitive dep compatibility (pydantic vs pydantic-core)
        The difference is [all] extras + --force-reinstall in reinstall."""
        from master_fetch.updater import _pip_cmd, _pip_cmd_full
        assert "--no-deps" in _pip_cmd("10.4.0")
        assert "--no-deps" in _pip_cmd_full("10.4.0")

    def test_reinstall_has_force_reinstall_update_does_not(self):
        """Both REINSTALL and UPDATE self-heal use --force-reinstall.
        REINSTALL: --force-reinstall --no-deps hound-mcp[all]==VERSION
        UPDATE self-heal: --force-reinstall --no-deps hound-mcp==VERSION
        The key difference is [all] extras + the main cmd (no --force in the
        first pass for update, --force in the first pass for reinstall)."""
        from master_fetch.updater import _pip_cmd, _pip_cmd_full, _heal_cmd
        # Update first pass: no --force-reinstall
        assert "--force-reinstall" not in _pip_cmd("10.4.0")
        # Reinstall first pass: --force-reinstall --no-deps
        assert "--force-reinstall" in _pip_cmd_full("10.4.0")
        assert "--no-deps" in _pip_cmd_full("10.4.0")
        # Update self-heal: --force-reinstall --no-deps
        assert "--force-reinstall" in _heal_cmd("10.4.0")
        assert "--no-deps" in _heal_cmd("10.4.0")

    def test_reinstall_has_all_extra_update_does_not(self):
        from master_fetch.updater import _pip_cmd, _pip_cmd_full
        update_pkg = [c for c in _pip_cmd("10.4.0") if "hound-mcp" in c][0]
        reinstall_pkg = [c for c in _pip_cmd_full("10.4.0") if "hound-mcp" in c][0]
        assert "[all]" not in update_pkg
        assert "[all]" in reinstall_pkg

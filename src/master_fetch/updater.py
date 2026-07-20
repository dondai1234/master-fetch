"""Reliable, brick-proof self-update for the hound CLI. Cross-platform.

This module owns the entire update lifecycle. The previous updater could brick
the install: `pip install --upgrade hound-mcp[all]` pulled the heavy `[all]`
extra (onnxruntime, tokenizers, rapidocr) which is slow and fails mid-install
(leaving master_fetch deleted and hound.exe orphaned -> every `hound` command
crashes with ModuleNotFoundError, including `hound -u` itself, so the tool
cannot self-heal). The recovery messages told users to run a bare
`pip install --force-reinstall` while a hound server held the launcher, which
is the exact command that bricks it.

This rewrite fixes all of that:

- **--no-deps, no extras.** The self-update only touches hound-mcp itself.
  Dependencies already installed are left alone; the `[all]` extra a user
  already has is preserved. Fast, deterministic, cannot fail on a heavy dep.
- **Windows: a detached helper runs pip after the launcher exits.** The running
  `hound -u` command IS hound.exe, which Windows locks against overwrite. The
  helper is a standalone `python -c` (no master_fetch dependency) that waits
  for the parent launcher to exit, stages the launcher aside via the rename
  trick (Windows permits renaming a running .exe, just not overwriting it), then
  runs pip with the launcher free. A still-running hound server is handled by
  the rename trick (it keeps the old code in memory until restarted); a stale
  locked `.old` is cleared by stopping that server. Never refuses, never bricks.
- **Self-heal.** If pip's first pass leaves the version unchanged or broken, a
  `--force-reinstall --no-deps` pass runs (the launcher is free by then) and
  re-verifies. Catches a half-failed install automatically.
- **Surviving repair.** `~/.hound/repair.py` (pure stdlib, outside site-packages)
  is written on every update. If hound is ever bricked (e.g. a manual pip while
  a server held the launcher), `python ~/.hound/repair.py` stops hound and
  force-reinstalls. It survives because it is not part of the hound-mcp package.
- **Safe messages.** Every failure prints ONE clean error plus the safe
  recovery (`python ~/.hound/repair.py`), never a bare destructive pip command.
- **hound doctor / hound --rollback.** Proactive health check, and undo a bad
  update by reinstalling the previously-recorded version.
"""

from __future__ import annotations

import os
import sys

__all__ = [
    "check_version", "pad_version",
    "do_update", "reinstall", "print_version", "doctor", "rollback",
    "cleanup_old_launcher", "repair_script_path",
]


# ─── version probing ───────────────────────────────────────────────────────

def check_version() -> tuple[str, str | None, bool | None]:
    """Return (installed, latest, is_current).

    installed: the importlib.metadata version, or "unknown" if the package
    metadata is missing (a half-failed install / brick). latest: the current
    PyPI version, or None if PyPI is unreachable. is_current: latest == installed
    when latest is known, else None.
    """
    from importlib.metadata import version as _get_version
    try:
        installed = _get_version("hound-mcp")
    except Exception:
        installed = "unknown"

    latest: str | None = None
    try:
        import json
        from urllib.request import urlopen, Request
        req = Request(
            "https://pypi.org/pypi/hound-mcp/json",
            headers={"User-Agent": "Hound/" + installed},
        )
        with urlopen(req, timeout=5) as resp:
            latest = json.loads(resp.read().decode()).get("info", {}).get("version")
    except Exception:
        pass

    return installed, latest, (latest == installed if latest else None)


def pad_version(v: str) -> tuple[int, ...]:
    """Parse a dotted version into a comparable int tuple (first 3 parts)."""
    return tuple(int(p) for p in v.split(".")[:3])


def _at_or_ahead(installed: str, target: str) -> bool:
    """True if installed is parseable and >= target (so no update needed)."""
    if not installed or installed == "unknown":
        return False
    try:
        return pad_version(installed) >= pad_version(target)
    except (ValueError, IndexError):
        return installed == target


def _advanced(new_ver: str, target: str) -> bool:
    """True if, after a pip run, the installed version reached the target."""
    if not new_ver or new_ver == "unknown":
        return False
    try:
        return pad_version(new_ver) >= pad_version(target)
    except (ValueError, IndexError):
        return new_ver == target


# ─── launcher + process helpers (Windows file-lock handling) ───────────────

def _hound_launcher_path() -> str | None:
    """Locate the hound launcher (hound.exe on Windows, `hound` on POSIX)."""
    import shutil
    candidate = shutil.which("hound")
    if candidate and os.path.exists(candidate):
        return candidate
    scripts_dir = os.path.join(os.path.dirname(sys.executable), "Scripts")
    for name in ("hound.exe", "hound"):
        fb = os.path.join(scripts_dir, name)
        if os.path.exists(fb):
            return fb
    posix_bin = os.path.dirname(sys.executable)
    posix_fallback = os.path.join(posix_bin, "hound")
    if os.path.exists(posix_fallback):
        return posix_fallback
    return None


def _stop_hound_cmd() -> str:
    """Platform command to stop all running hound launcher processes."""
    if sys.platform == "win32":
        return "taskkill /IM hound.exe /F"
    return "pkill -x hound"


def _looks_like_file_lock_error(stderr: str) -> bool:
    if not stderr:
        return False
    s = stderr.lower()
    return ("winerror 32" in s or "being used by another process" in s
            or ("permission denied" in s and "hound" in s))


def _other_hound_pids() -> list[int]:
    """PIDs of OTHER running hound launcher processes (excludes this one)."""
    import subprocess
    my_pid = os.getpid()
    pids: list[int] = []
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq hound.exe", "/FO", "CSV", "/NH"],
                text=True, timeout=10, creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            for line in out.splitlines():
                parts = [p.strip().strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[0].lower() == "hound.exe":
                    try:
                        pid = int(parts[1])
                    except ValueError:
                        continue
                    if pid != my_pid:
                        pids.append(pid)
        else:
            out = subprocess.check_output(["ps", "-eo", "pid=,comm="], text=True, timeout=10)
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    pid_s, comm = line.split(None, 1)
                    pid = int(pid_s)
                except ValueError:
                    continue
                if os.path.basename(comm.strip()) == "hound" and pid != my_pid:
                    pids.append(pid)
    except Exception:
        return []
    return pids


def cleanup_old_launcher() -> None:
    """Sweep a stale hound.exe.old left by a previous self-update (Windows).

    Windows locks the running .exe against deletion, so the updater renames the
    live launcher to hound.exe.old before pip writes a fresh one. The .old can
    only be deleted once the process running from it exits, so we sweep it on
    the next launch instead. No-op on non-Windows.
    """
    if sys.platform != "win32":
        return
    exe = _hound_launcher_path()
    if not exe:
        return
    old = exe + ".old"
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        pass  # still locked (a hound server still runs from it) - leave it


# ─── hound home: state + the surviving repair script ───────────────────────

def _hound_home() -> str:
    p = os.path.join(os.path.expanduser("~"), ".hound")
    os.makedirs(p, exist_ok=True)
    return p


def repair_script_path() -> str:
    return os.path.join(_hound_home(), "repair.py")


def _state_path(name: str) -> str:
    return os.path.join(_hound_home(), name)


_REPAIR_SCRIPT = '''#!/usr/bin/env python3
r"""Hound repair - recover from a broken hound install (failed update, brick).

Run with:  python __REPAIR__
Stops any running hound process, force-reinstalls hound-mcp from PyPI, verifies.
Pure standard library - works even when the hound-mcp package is gone, because
this file lives in ~/.hound (outside site-packages), so a failed pip uninstall
of hound-mcp never touches it.
"""
import subprocess, sys

def _stop():
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/IM", "hound.exe", "/F"], capture_output=True)
    else:
        # -x matches the process name exactly ("hound"), not this script ("python").
        subprocess.run(["pkill", "-x", "hound"], capture_output=True)

def _pip(*extra):
    return subprocess.run(
        [sys.executable, "-m", "pip", "install", *extra, "--quiet",
         "--disable-pip-version-check"])

def main():
    print("Hound repair: stopping any running hound...")
    _stop()
    print("Hound repair: force-reinstalling hound-mcp from PyPI...")
    r = _pip("--force-reinstall", "--no-deps", "--upgrade", "hound-mcp")
    if r.returncode != 0:
        print("Hound repair: reinstall failed (pip exit %d)." % r.returncode)
        print("  Try manually:  %s -m pip install --force-reinstall hound-mcp" % sys.executable)
        return r.returncode
    try:
        from importlib.metadata import version as _v
        print("Hound " + _v("hound-mcp") + "  repaired")
        return 0
    except Exception as e:
        print("Hound repair: still broken after reinstall: " + str(e))
        print("  Reinstall all deps:  %s -m pip install --force-reinstall hound-mcp" % sys.executable)
        return 1

if __name__ == "__main__":
    sys.exit(main())
'''


def _write_repair_script() -> None:
    """(Re)write ~/.hound/repair.py so the brick-recovery safety net exists."""
    try:
        with open(repair_script_path(), "w", encoding="utf-8") as f:
            f.write(_REPAIR_SCRIPT.replace("__REPAIR__", repair_script_path()))
    except OSError:
        pass  # home dir not writable; not fatal - the update can still proceed


def _write_last_version(v: str) -> None:
    if not v or v == "unknown":
        return
    try:
        with open(_state_path("last_version"), "w", encoding="utf-8") as f:
            f.write(v.strip())
    except OSError:
        pass


def _read_last_version() -> str | None:
    try:
        with open(_state_path("last_version"), "r", encoding="utf-8") as f:
            v = f.read().strip()
            return v or None
    except OSError:
        return None


# ─── pip commands + runner ─────────────────────────────────────────────────

def _pip_cmd(target: str) -> list[str]:
    """Install exactly `target` with no deps / no extras (fast, reliable)."""
    return [sys.executable, "-m", "pip", "install", "--no-deps",
            f"hound-mcp=={target}", "--quiet", "--disable-pip-version-check",
            "--no-python-version-warning"]


def _heal_cmd(target: str) -> list[str]:
    """Force-reinstall `target` (no deps) - the self-heal / brick-recovery pass."""
    return [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps",
            f"hound-mcp=={target}", "--quiet", "--disable-pip-version-check",
            "--no-python-version-warning"]


def _pip_cmd_full(target: str) -> list[str]:
    """Full reinstall: force-reinstall hound-mcp[all] at the pinned version
    with --no-deps. The --force-reinstall triggers pip to install the [all]
    extras (rapidocr, onnxruntime, tokenizers) even when hound-mcp itself is
    already at the target version. The --no-deps prevents pip from
    force-reinstalling transitive deps (which can break version compatibility,
    e.g. pydantic vs pydantic-core)."""
    return [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps",
            f"hound-mcp[all]=={target}", "--quiet", "--disable-pip-version-check",
            "--no-python-version-warning"]


def _run_pip(cmd: list[str]) -> tuple[int, str]:
    """Run pip, capturing stderr for diagnosis. Returns (returncode, stderr)."""
    import subprocess
    try:
        r = subprocess.run(cmd, timeout=300, capture_output=True, text=True)
        return r.returncode, (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except Exception as e:
        return 1, str(e)


def _diagnose(stderr: str) -> str:
    if _looks_like_file_lock_error(stderr):
        return "a running hound server holds the launcher"
    s = (stderr or "").lower()
    if "no matching distribution" in s or "could not find a version" in s:
        return "version not found on PyPI"
    if "timed out" in s or "timeout" in s:
        return "network timed out"
    return "pip failed"


# ─── the detached Windows helper (standalone python -c, survives brick) ────

def _build_helper_source(target: str, repair_path: str, parent_pid: int, full: bool = False) -> str:
    """Build the standalone helper source. Pure stdlib, no master_fetch import,
    so it runs even if the package is mid-replacement or bricked.

    The helper: waits for the parent launcher to exit, stages the launcher aside
    (rename trick; stops a server only if it holds a stale .old), runs pip
    --no-deps, self-heals on verify-fail, prints a clean result. Plain ASCII
    output (no ANSI) since it runs detached after the parent's color setup is
    gone and may run on a legacy console.
    """
    return '''import os, sys, time, subprocess
PARENT = __PARENT_PID__
TARGET = __TARGET__
REPAIR = __REPAIR__
EXE = __EXE__
WIN = (sys.platform == "win32")
FULL = __FULL__

def _wait_parent_exit(timeout=15):
    if not WIN or not PARENT:
        return
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.waitpid(PARENT, os.WNOHANG)
            return
        except (ChildProcessError, OSError):
            return  # not our child (the launcher was) - assume gone after sleep
        except Exception:
            break
    time.sleep(2)  # fallback: give the launcher time to release the file

def _hound_pids():
    out = []
    if not WIN:
        return out
    my = os.getpid()
    try:
        o = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq hound.exe", "/FO", "CSV", "/NH"],
            text=True, timeout=10, creationflags=0x08000000)
    except Exception:
        return out
    for ln in o.splitlines():
        ps = [x.strip().strip(chr(34)) for x in ln.split(chr(34) + "," + chr(34))]
        if len(ps) >= 2 and ps[0].lower() == "hound.exe":
            try:
                pid = int(ps[1])
            except ValueError:
                continue
            if pid != my:
                out.append(pid)
    return out

def _stop_all_hound():
    if WIN:
        subprocess.run(["taskkill", "/IM", "hound.exe", "/F"], capture_output=True)
    else:
        subprocess.run(["pkill", "-x", "hound"], capture_output=True)

def _stage():
    # Rename the live hound.exe -> hound.exe.old so pip can write a fresh one
    # to the now-free path. Windows permits RENAMING a running .exe (it only
    # forbids overwrite/delete), so a server keeps running from the .old until
    # it restarts - no need to stop it. The only stop is for a stale .old left
    # by a previous update that a server still runs from.
    if not EXE or not WIN:
        return True
    old = EXE + ".old"
    if os.path.exists(old):
        for _ in range(2):
            try:
                os.remove(old)
                break
            except OSError:
                print("  a stale hound.exe.old is locked - stopping the old hound server...")
                _stop_all_hound()
                time.sleep(2)
    try:
        os.rename(EXE, old)
        return True
    except OSError:
        # Rename failed (e.g. read-only system install). pip will likely fail
        # too; the self-heal pass and the repair.py fallback handle the rest.
        return False

def _pip(*extra):
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", *extra, "--quiet",
         "--disable-pip-version-check"],
        capture_output=True, text=True, timeout=300)
    return r.returncode, (r.stderr or "")

def _ver():
    try:
        from importlib.metadata import version as _v
        return _v("hound-mcp")
    except Exception:
        return "unknown"

def _pad(v):
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except Exception:
        return None

def _advanced(new):
    if not new or new == "unknown":
        return False
    np, tp = _pad(new), _pad(TARGET)
    if np and tp:
        return np >= tp
    return new == TARGET

_wait_parent_exit()
# Move below any shell prompt that printed when the parent exited.
try:
    sys.stdout.write(chr(10)); sys.stdout.flush()
except Exception:
    pass

servers_before = _hound_pids()
if FULL:
    _stop_all_hound()
    servers_before = []
_stage()

if FULL:
    rc, stderr = _pip("--force-reinstall", "--no-deps", "hound-mcp[all]==" + TARGET)
else:
    rc, stderr = _pip("--no-deps", "hound-mcp==" + TARGET)
if not _advanced(_ver()):
    print("  first pass did not complete - recovering...")
    if FULL:
        rc2, stderr2 = _pip("--force-reinstall", "--no-deps", "hound-mcp[all]==" + TARGET)
    else:
        rc2, stderr2 = _pip("--force-reinstall", "--no-deps", "hound-mcp==" + TARGET)
    if not _advanced(_ver()):
        print("  Hound  " + ("reinstall" if FULL else "update") + " failed - " + (stderr2 or stderr or "pip failed").strip().splitlines()[-1:][0] if (stderr2 or stderr) else "pip failed")
        print("  recover with:  python \\"" + REPAIR + "\\"")
        sys.exit(1)

# Best-effort: sweep the staged .old (fails if a server still maps it - fine).
# Safety: if pip didn't recreate the .exe (already satisfied, no --force-reinstall),
# restore it from the .old backup.
try:
    if WIN and EXE:
        if os.path.exists(EXE + ".old"):
            if not os.path.exists(EXE):
                os.rename(EXE + ".old", EXE)
            else:
                os.remove(EXE + ".old")
except OSError:
    pass

new = _ver()
print("  Hound  v" + new + "  " + ("reinstalled" if FULL else "updated"))
if servers_before:
    print("  restart your running hound server (PID " + ", ".join(str(p) for p in servers_before) + ") to use it")
'''.replace("__PARENT_PID__", str(parent_pid)).replace("__TARGET__", repr(target)).replace("__REPAIR__", repr(repair_path)).replace("__EXE__", repr(_hound_launcher_path())).replace("__FULL__", str(full))


def _spawn_helper(target: str, repair_path: str, parent_pid: int, full: bool = False) -> bool:
    """Spawn the detached Windows helper (inherits this console). Returns True
    if spawned. `full=True` triggers a complete reinstall with deps + [all]
    extras instead of the usual --no-deps update."""
    import subprocess
    src = _build_helper_source(target, repair_path, parent_pid, full)
    try:
        subprocess.Popen([sys.executable, "-c", src])
        return True
    except Exception:
        return False


# ─── public commands ───────────────────────────────────────────────────────

def do_update(target: str | None = None) -> None:
    """Reliable, brick-proof self-update. `target` pins a version (rollback);
    None means the latest on PyPI. See the module docstring for the design."""
    from master_fetch import cli_ui as ui
    installed, latest, _is_current = check_version()
    if target is None:
        target = latest

    if not target:
        print(ui.branded(ui.ver(installed if installed != "unknown" else "?"),
                         ui.dim("couldn't reach PyPI")))
        print("  " + ui.warn("check your connection, then") + "  " + ui.cmd("hound -u"))
        return

    if _at_or_ahead(installed, target):
        print(ui.branded(ui.ver(installed), ui.ok("up to date")))
        return

    # Ensure the safety net + rollback state exist before touching anything.
    _write_repair_script()
    _write_last_version(installed)
    repair = repair_script_path()

    if installed == "unknown":
        print(ui.branded(ui.red("install corrupted"), ui.dim("recovering...")))
    else:
        print(ui.branded(ui.ver_transition(installed, target), ui.dim("updating...")))

    if sys.platform == "win32":
        # Detached helper: waits for this launcher to exit, frees it via the
        # rename trick, runs pip, self-heals, prints the result. The parent
        # must exit so hound.exe is releasable.
        if _spawn_helper(target, repair, os.getpid()):
            print("  " + ui.dim("(completes in this window once this command exits)"))
            return
        # Spawn failed - last resort: point at the surviving repair script.
        print("  " + ui.err("could not start the updater"))
        print("  " + ui.warn("recover with") + "  " + ui.cmd(f'python "{repair}"'))
        return

    # POSIX: no file lock. Run pip inline with self-heal + verify.
    others = _other_hound_pids()
    if others:
        print("  " + ui.dim(f"{len(others)} hound server(s) running - restart them after"))
    rc, stderr = _run_pip(_pip_cmd(target))
    if not _advanced(check_version()[0], target):
        print("  " + ui.dim("first pass did not complete - recovering..."))
        rc2, stderr2 = _run_pip(_heal_cmd(target))
        new_ver = check_version()[0]
        if not _advanced(new_ver, target):
            print("  " + ui.err("update failed: " + _diagnose(stderr2 or stderr)))
            print("  " + ui.warn("recover with") + "  " + ui.cmd(f'python "{repair}"'))
            sys.exit(1)
    new_ver = check_version()[0]
    print(ui.branded(ui.ver(new_ver), ui.ok("updated")))
    if others:
        print("  " + ui.dim(f"restart PID {', '.join(str(p) for p in others)} to use the new version"))


def reinstall() -> None:
    """Full reinstall: hound-mcp + all deps + [all] extras. Fixes broken deps,
    missing extras, or a stale CDN-downgraded install. Pinned to the latest
    PyPI version (or current if PyPI is unreachable) to prevent version drift."""
    from master_fetch import cli_ui as ui
    installed, latest, _ = check_version()
    target = latest or installed
    if not target or target == "unknown":
        print(ui.branded(ui.red("cannot reinstall"), ui.dim("version unknown")))
        print("  " + ui.warn("try") + "  " + ui.cmd("pip install hound-mcp[all]"))
        return

    _write_repair_script()
    _write_last_version(installed)
    repair = repair_script_path()

    if installed == "unknown":
        print(ui.branded(ui.red("install corrupted"), ui.dim("reinstalling...")))
    else:
        print(ui.branded(ui.ver(installed), ui.dim("reinstalling with all deps...")))

    if sys.platform == "win32":
        if _spawn_helper(target, repair, os.getpid(), full=True):
            print("  " + ui.dim("(completes in this window once this command exits)"))
            return
        print("  " + ui.err("could not start the reinstaller"))
        print("  " + ui.warn("recover with") + "  " + ui.cmd(f'python "{repair}"'))
        return

    # POSIX: no file lock. Run pip inline with self-heal + verify.
    others = _other_hound_pids()
    if others:
        print("  " + ui.dim(f"{len(others)} hound server(s) running - restart them after"))
    rc, stderr = _run_pip(_pip_cmd_full(target))
    if not _advanced(check_version()[0], target):
        print("  " + ui.dim("first pass did not complete - recovering..."))
        rc2, stderr2 = _run_pip(_pip_cmd_full(target))
        new_ver = check_version()[0]
        if not _advanced(new_ver, target):
            print("  " + ui.err("reinstall failed: " + _diagnose(stderr2 or stderr)))
            print("  " + ui.warn("recover with") + "  " + ui.cmd(f'python "{repair}"'))
            sys.exit(1)
    new_ver = check_version()[0]
    print(ui.branded(ui.ver(new_ver), ui.ok("reinstalled")))
    if others:
        print("  " + ui.dim(f"restart PID {', '.join(str(p) for p in others)} to use the new version"))


def rollback() -> None:
    """Reinstall the version recorded before the last update (undo a bad update)."""
    from master_fetch import cli_ui as ui
    last = _read_last_version()
    if not last:
        print(ui.branded(ui.dim("nothing to roll back to"),
                         ui.dim("no previous version recorded")))
        return
    installed = check_version()[0]
    if _at_or_ahead(installed, last) and installed != "unknown":
        try:
            same = pad_version(installed) == pad_version(last)
        except (ValueError, IndexError):
            same = installed == last
        if same:
            print(ui.branded(ui.ver(installed), ui.dim("already at the previous version")))
            return
    print(ui.branded(ui.dim("rolling back"), ui.ver_transition(installed, last)))
    do_update(target=last)


def print_version() -> None:
    """Render `hound -v`: a compact bordered version panel (or a clean error
    panel when the install is corrupted, pointing at the safe repair path)."""
    from master_fetch import cli_ui as ui
    W = 50
    inner = W - 4
    installed, latest, is_current = check_version()
    if installed == "unknown":
        repair = repair_script_path()
        body = [
            ui.dim("package metadata is missing - a previous update was"),
            ui.dim("interrupted. The launcher works, but pip lost the version."),
            "",
            ui.dim("recover with:"),
            "  " + ui.cmd(f'python "{repair}"'),
            ui.dim("or:  hound -u  (reinstalls the latest version)"),
        ]
        print(ui.panel([ui.err("install corrupted")] + body, 62))
        return
    if latest is None:
        print(ui.panel([
            ui.lr(ui.wordmark(), "", inner),
            ui.lr(ui.ver(installed), ui.dim("couldn't reach PyPI"), inner),
        ], W))
        print("  " + ui.warn("check your connection, then") + "  " + ui.cmd("hound -v"))
        return
    try:
        up_to_date = pad_version(installed) >= pad_version(latest)
    except (ValueError, IndexError):
        up_to_date = bool(is_current)
    if up_to_date:
        print(ui.panel([
            ui.lr(ui.wordmark(), "", inner),
            ui.lr(ui.ver(installed), ui.ok("up to date"), inner),
        ], W))
    else:
        print(ui.panel([
            ui.lr(ui.wordmark(), "", inner),
            ui.lr(ui.ver(installed), ui.magenta(f"v{latest} available"), inner),
        ], W))
        print("  " + ui.warn("update with") + "  " + ui.cmd("hound -u"))


def doctor() -> None:
    """Proactive health check. Diagnoses a half-broken install before it bricks,
    and offers the right fix. Prints a clean report."""
    from master_fetch import cli_ui as ui
    import shutil
    from importlib.metadata import version as _meta_version

    def _short(p: str, w: int = 34) -> str:
        if not p:
            return ""
        home = os.path.expanduser("~")
        if p.startswith(home):
            p = "~" + p[len(home):]
        if len(p) > w:
            p = "..." + p[-(w - 3):]
        return p

    checks: list[tuple[str, bool, str]] = []  # (label, ok, detail)

    # 1. launcher resolves
    exe = _hound_launcher_path()
    checks.append(("launcher resolves", bool(exe), _short(exe) or "hound not on PATH"))

    # 2. master_fetch imports + version
    try:
        import master_fetch as _mf
        mf_ver = getattr(_mf, "__version__", "?")
        checks.append(("package imports", True, mf_ver))
    except Exception as e:
        checks.append(("package imports", False, str(e)))
        mf_ver = None

    # 3. metadata version matches imported version
    try:
        meta_ver = _meta_version("hound-mcp")
        ok = (mf_ver is not None and meta_ver == mf_ver)
        checks.append(("metadata consistent", ok,
                       f"meta {meta_ver} vs module {mf_ver}" if not ok else meta_ver))
    except Exception:
        checks.append(("metadata consistent", False, "hound-mcp metadata missing"))

    # 4. no stale locked .old
    stale = ""
    if exe and sys.platform == "win32":
        old = exe + ".old"
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                stale = "hound.exe.old locked by a running server (cleaned when it stops)"
    checks.append(("launcher clean", not stale, stale or "ok"))

    # 5. repair script exists (ensure it)
    _write_repair_script()
    rp = repair_script_path()
    checks.append(("repair script ready", os.path.exists(rp), _short(rp)))

    # 6. core deps importable
    missing = []
    for mod in ("httpx", "aiosqlite", "mcp", "pydantic"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    checks.append(("core dependencies", not missing,
                   ", ".join(missing) + " missing" if missing else "ok"))

    # 7. [all] extras (optional, non-blocking - neural reranking + PDF OCR)
    optional_missing = []
    for mod in ("onnxruntime", "tokenizers", "rapidocr"):
        try:
            __import__(mod)
        except Exception:
            optional_missing.append(mod)
    optional_ok = not optional_missing
    optional_detail = ", ".join(optional_missing) + " missing" if optional_missing else "ok"

    # 8. PyPI reachability + version (info only, not a failure)
    installed, latest, _ = check_version()
    if latest is None:
        checks.append(("PyPI reachable", False, "couldn't reach PyPI"))
    else:
        try:
            ahead = pad_version(installed) >= pad_version(latest)
        except (ValueError, IndexError):
            ahead = True
        checks.append(("PyPI reachable", True,
                       "up to date" if ahead else f"v{latest} available"))

    # Render
    all_ok = all(ok for _, ok, _ in checks)
    status = ui.ok("all healthy") if all_ok else ui.err("issues found")
    rows = [ui.wordmark() + "  " + status, ""]
    for label, ok_flag, detail in checks:
        mark = (ui._sty(ui._glyph("\u2713", "+"), ui._GREEN) if ok_flag
                else ui._sty(ui._glyph("\u2717", "x"), ui._RED))
        rows.append(f"{mark} {label:<22} {ui.dim(_short(detail, 30))}")
    # Optional [all] extras (non-blocking, shown with a different marker)
    opt_mark = (ui._sty(ui._glyph("\u2713", "+"), ui._GREEN) if optional_ok
                else ui._sty(ui._glyph("!", "!"), ui._MAGENTA))
    rows.append(f"{opt_mark} {'[all] extras':<22} {ui.dim(_short(optional_detail, 30))}")
    print(ui.panel(rows, 64))
    # Verdict + fixes (outside the panel)
    if missing:
        print("  " + ui.warn("fix deps") + "  " + ui.cmd("pip install --force-reinstall hound-mcp"))
    if any(not ok for _, ok, _ in checks) and not missing:
        print("  " + ui.warn("repair") + "  " + ui.cmd(f'python "{_short(rp, 46)}"'))
    if not optional_ok:
        print("  " + ui.warn("install extras") + "  " + ui.cmd("pip install hound-mcp[all]"))
    if latest and installed != "unknown":
        try:
            if pad_version(installed) < pad_version(latest):
                print("  " + ui.warn("update") + "  " + ui.cmd("hound -u"))
        except (ValueError, IndexError):
            pass

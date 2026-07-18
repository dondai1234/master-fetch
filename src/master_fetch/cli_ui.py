"""Zero-dependency ANSI renderer for the hound CLI.

Gives hound's commands a clean, professional, cross-platform look WITHOUT
pulling in rich (hound stays lean  -  works in air-gapped/Docker-scratch envs).
Handles:

- Color support: respects ``NO_COLOR`` (any value disables) and ``FORCE_COLOR``;
  color only when stdout is a TTY; enables Windows VT processing (Win10+).
- Unicode box borders with an automatic ASCII fallback when stdout isn't UTF-8
  (legacy consoles), so panels never emit mojibake on any machine.
- A compact bordered panel with left/right-aligned rows (alignment-safe:
  visible-length math excludes ANSI codes, content is ASCII so widths are
  predictable  -  no emoji/wide chars inside borders).
- Branded one-liners (wordmark + label + dim meta).

Palette (per the project aesthetic): magenta + cyan-teal accents, dim gray
secondary, red for errors. No amber/gold, no forest green. When color is off
(piped, NO_COLOR, legacy console) every style degrades to plain text and the
layout still reads cleanly.

Stdlib only (ctypes/os/re/shutil/sys). No import cost on the server path
(server.py lazy-imports this module inside the CLI functions only).
"""
from __future__ import annotations

import os
import re
import sys

# ── ANSI codes ──────────────────────────────────────────────────────
_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_MAGENTA = "\033[95m"   # bright magenta  -  wordmark / attention
_CYAN = "\033[96m"      # bright cyan (teal-ish)  -  version / success / links
_RED = "\033[91m"       # bright red  -  errors
_GREEN = "\033[92m"     # bright green  -  reserved for ✓ (not forest green)
_UNDER = "\033[4m"      # underline  -  commands/URLs

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

_color_cache: bool | None = None
_unicode_cache: bool | None = None


def _color_ok() -> bool:
    """True if we should emit ANSI color. Cached after first call."""
    global _color_cache
    if _color_cache is not None:
        return _color_cache
    # NO_COLOR (no-color.org): any value, even empty, disables color.
    if os.environ.get("NO_COLOR") is not None:
        _color_cache = False
        return False
    if os.environ.get("FORCE_COLOR"):
        _color_cache = True
    elif not sys.stdout.isatty():
        _color_cache = False
    else:
        _color_cache = True
    if _color_cache and sys.platform == "win32":
        _enable_windows_vt()
    return _color_cache


def _enable_windows_vt() -> None:
    """Enable virtual-terminal processing on Windows 10+ so ANSI codes render.

    Best-effort: silently no-ops on older Windows or non-console handles. Without
    this, ANSI escapes print literally on legacy conhost. Cached via _color_cache.
    """
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VT = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT)
    except Exception:
        pass


def _unicode_ok() -> bool:
    """True if stdout can render UTF-8 box-drawing chars. Cached."""
    global _unicode_cache
    if _unicode_cache is not None:
        return _unicode_cache
    # Try to upgrade stdout to UTF-8 (Python 3.7+ reconfigure). On success the
    # console is expected to decode UTF-8 (Windows Terminal / modern terms do).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    _unicode_cache = "utf" in enc
    return _unicode_cache


def _visible(s: str) -> str:
    """Strip ANSI escape codes for visible-length math."""
    return _ANSI_RE.sub("", s)


def _sty(text: str, *codes: str) -> str:
    """Wrap text in codes + reset, but only when color is enabled."""
    if not _color_ok() or not codes:
        return text
    return "".join(codes) + text + _RESET


def _borders() -> tuple[str, str, str, str, str, str]:
    if _unicode_ok():
        return "╭", "╮", "╰", "╯", "─", "│"
    return "+", "+", "+", "+", "-", "|"


# ── Styled fragments ────────────────────────────────────────────────

def wordmark() -> str:
    return _sty("Hound", _BOLD, _MAGENTA)


def ver(v: str) -> str:
    return _sty(f"v{v}", _CYAN)


def ver_transition(a: str, b: str) -> str:
    """v{a} → v{b} for update-progress lines: both versions styled, the arrow
    dim with an ASCII fallback on non-UTF-8 consoles."""
    return ver(a) + " " + dim(_glyph("→", "->")) + " " + ver(b)


def dim(s: str) -> str:
    return _sty(s, _DIM)


def cyan(s: str) -> str:
    return _sty(s, _CYAN)


def magenta(s: str) -> str:
    return _sty(s, _MAGENTA)


def red(s: str) -> str:
    return _sty(s, _RED)


def _glyph(uni: str, asc: str) -> str:
    """A status glyph that falls back to ASCII when stdout isn't UTF-8."""
    return uni if _unicode_ok() else asc


def ok(s: str) -> str:
    """A success marker + label: check in green, label dim."""
    return _sty(_glyph("✓", "+"), _GREEN) + " " + _sty(s, _DIM)


def warn(s: str) -> str:
    return _sty(_glyph("→", ">"), _MAGENTA) + " " + _sty(s, _DIM)


def err(s: str) -> str:
    return _sty(_glyph("✗", "x"), _RED) + " " + _sty(s, _RED)


def cmd(s: str) -> str:
    """A shell command, styled for copy-paste prominence."""
    return _sty(s, _UNDER, _CYAN)


# ── Composed output ─────────────────────────────────────────────────

def lr(left: str, right: str, inner_width: int) -> str:
    """Compose a left-aligned + right-aligned row for a panel. The gap fills so
    `right` sits at the right edge. Visible-length aware (ANSI excluded)."""
    gap = inner_width - len(_visible(left)) - len(_visible(right))
    if gap < 1:
        gap = 1
    return f"{left}{' ' * gap}{right}"


def panel(rows: list[str], width: int = 50, indent: int = 2) -> str:
    """Render a compact bordered panel. Each row is a pre-composed inner string
    (use ``lr`` for left/right rows, or pass a plain styled string for a
    left-only row). Rows are padded to the inner width; overflow is not
    truncated (rare  -  content is short by design)."""
    co = _color_ok()
    d = _DIM if co else ""
    rr = _RESET if co else ""
    TL, TR, BL, BR, H, V = _borders()
    inner = width - 4  # inside "│ " ... " │"
    pad = " " * indent
    out = [f"{pad}{d}{TL}{H * (width - 2)}{TR}{rr}"]
    for ln in rows:
        extra = inner - len(_visible(ln))
        if extra < 0:
            extra = 0
        out.append(f"{pad}{d}{V}{rr} {ln}{' ' * extra} {d}{V}{rr}")
    out.append(f"{pad}{d}{BL}{H * (width - 2)}{BR}{rr}")
    return "\n".join(out)


def branded(label: str, meta: str = "") -> str:
    """A branded one-liner: `  Hound  <label>  <dim meta>`."""
    s = f"  {wordmark()}  {label}"
    if meta:
        s += f"  {meta}"
    return s

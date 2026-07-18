"""v10.1 cli_ui renderer tests: pin the cross-platform invariants.

- Panel rows are all equal visible width (borders align on every machine).
- Color is stripped when NO_COLOR is set / stdout isn't a TTY (clean plain text
  when piped — no ANSI escapes leak into `hound -v | grep` etc.).
- ver_transition stamps both versions with `v` (the v10.1 bug where
  `ver(f"{a} → {b}")` gave `v{a} → {b}` with one missing `v`).
- Status glyphs fall back to ASCII when stdout isn't UTF-8 (legacy consoles).
"""
import os

import master_fetch.cli_ui as u


def _reset_caches():
    u._color_cache = None
    u._unicode_cache = None


def test_panel_rows_equal_visible_width():
    """Every line of a panel (with ANSI) has the same visible width so the
    right border aligns on any terminal."""
    os.environ["FORCE_COLOR"] = "1"
    _reset_caches()
    try:
        W = 50
        rows = [u.lr(u.wordmark(), "", W - 4),
                u.lr(u.ver("10.1.0"), u.ok("up to date"), W - 4)]
        p = u.panel(rows, W)
        widths = {len(u._visible(ln)) for ln in p.split("\n")}
        assert len(widths) == 1, f"panel lines have unequal widths: {widths}"
        assert widths == {W + 2}  # 2-space indent + panel width
    finally:
        os.environ.pop("FORCE_COLOR", None)
        _reset_caches()


def test_no_color_strips_ansi():
    """Under NO_COLOR (or non-TTY), no ANSI escapes reach stdout — plain text."""
    os.environ["NO_COLOR"] = "1"
    _reset_caches()
    try:
        out = u.branded(u.ver("10.1.0"), u.ok("up to date"))
        assert "\033[" not in out, f"ANSI leaked under NO_COLOR: {out!r}"
        assert "Hound" in out and "v10.1.0" in out and "up to date" in out
    finally:
        os.environ.pop("NO_COLOR", None)
        _reset_caches()


def test_ver_transition_stamps_both_versions():
    """ver_transition(a, b) must give v{a} → v{b} (both prefixed), not v{a} → {b}."""
    os.environ["NO_COLOR"] = "1"
    _reset_caches()
    try:
        s = u.ver_transition("3.6.5", "3.6.6")
        assert "v3.6.5" in s
        assert "v3.6.6" in s  # the bug was: second version had no 'v'
    finally:
        os.environ.pop("NO_COLOR", None)
        _reset_caches()


def test_glyph_ascii_fallback():
    """Status glyphs fall back to ASCII when stdout isn't UTF-8 (legacy console)."""
    u._unicode_cache = False
    try:
        assert u._glyph("✓", "+") == "+"
        assert u._glyph("→", "->") == "->"
        assert u._glyph("✗", "x") == "x"
    finally:
        u._unicode_cache = None


def test_glyph_unicode_when_utf8():
    u._unicode_cache = True
    try:
        assert u._glyph("✓", "+") == "✓"
    finally:
        u._unicode_cache = None


def test_lr_right_aligns():
    """lr pads so `right` sits at the right edge of the inner width."""
    os.environ["NO_COLOR"] = "1"
    _reset_caches()
    try:
        line = u.lr("v1", "ok", 10)
        assert u._visible(line) == "v1      ok"  # 2 + 6 spaces + 2 = 10
        assert len(u._visible(line)) == 10
    finally:
        os.environ.pop("NO_COLOR", None)
        _reset_caches()

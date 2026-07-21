"""Tests for v11.1.0 stealth browser innovations.

Tests:
1. Channel detection (_detect_chrome_channel)
2. Fingerprint profile generation (coherent, consistent)
3. Stealth init script (all JS patches present, valid JS)
4. Bezier curve math
5. Human behavior simulation functions exist and are callable
6. BrowserSession defaults (real_chrome=True, humanize=True)
7. StealthyBrowser gets init script, DynamicBrowser doesn't
8. Channel detection is cached
"""

import inspect

import pytest

from master_fetch.browser import (
    _detect_chrome_channel,
    _generate_fingerprint_profile,
    _build_stealth_init_script,
    _bezier_point,
    _human_mouse_move,
    _simulate_human_behavior,
    _FINGERPRINT_PROFILES,
    StealthyBrowser,
    DynamicBrowser,
    BrowserSession,
)


class TestChannelDetection:
    """Test system Chrome detection."""

    def test_returns_chrome_or_chromium(self):
        """Must return 'chrome' or 'chromium', nothing else."""
        result = _detect_chrome_channel()
        assert result in ("chrome", "chromium"), f"Unexpected channel: {result}"

    def test_caches_result(self):
        """Second call must return the same value (cached)."""
        first = _detect_chrome_channel()
        second = _detect_chrome_channel()
        assert first == second, "Channel detection should be cached"

    def test_default_real_chrome_is_true(self):
        """StealthyBrowser should default real_chrome=True for system Chrome."""
        session = StealthyBrowser()
        assert session._real_chrome is True, "real_chrome should default to True"

    def test_channel_uses_detection_when_real_chrome_true(self):
        """When real_chrome=True, channel should be 'chrome' (or detection result)."""
        session = StealthyBrowser(real_chrome=True)
        # real_chrome=True means channel is hardcoded to 'chrome'
        # (not using _detect_chrome_channel, which is the fallback path)
        # The session doesn't expose channel directly, but we can check
        # that the start() method would use 'chrome' by inspecting the code
        source = inspect.getsource(BrowserSession.start)
        assert '"channel"' in source
        # When real_chrome is True, it uses 'chrome' directly
        assert 'self._real_chrome else _detect_chrome_channel()' in source or \
               'self._real_chrome else "chromium"' in source or \
               '"chrome" if self._real_chrome else' in source


class TestFingerprintProfiles:
    """Test coherent fingerprint profile generation."""

    def test_profile_has_all_required_fields(self):
        """Each profile must have all required fields."""
        for profile in _FINGERPRINT_PROFILES:
            required = {"platform", "languages", "hardware_concurrency",
                       "device_memory", "webgl_vendor", "webgl_renderer", "plugins"}
            assert required.issubset(profile.keys()), \
                f"Missing fields: {required - set(profile.keys())}"

    def test_profile_coherence_win32(self):
        """Win32 profiles must have Direct3D11 WebGL renderer."""
        for profile in _FINGERPRINT_PROFILES:
            if profile["platform"] == "Win32":
                assert "Direct3D11" in profile["webgl_renderer"], \
                    f"Win32 should have D3D11: {profile['webgl_renderer']}"
                assert "Google Inc." in profile["webgl_vendor"], \
                    f"Chrome should report Google Inc. vendor: {profile['webgl_vendor']}"

    def test_profile_coherence_macintel(self):
        """MacIntel profiles must have Metal WebGL renderer."""
        for profile in _FINGERPRINT_PROFILES:
            if profile["platform"] == "MacIntel":
                assert "Metal" in profile["webgl_renderer"], \
                    f"MacIntel should have Metal: {profile['webgl_renderer']}"

    def test_generate_returns_coherent_profile(self):
        """_generate_fingerprint_profile() returns a valid profile."""
        profile = _generate_fingerprint_profile()
        assert profile["platform"] in ("Win32", "MacIntel")
        assert profile["languages"] == ["en-US", "en"]
        assert profile["hardware_concurrency"] in (8, 12)
        assert profile["device_memory"] in (8, 16)
        assert len(profile["plugins"]) == 5

    def test_generate_returns_copy_not_reference(self):
        """_generate_fingerprint_profile() must return a copy, not the original."""
        p1 = _generate_fingerprint_profile()
        p1["platform"] = "MODIFIED"
        p2 = _generate_fingerprint_profile()
        assert p2["platform"] != "MODIFIED", "Should return a copy, not the original"

    def test_at_least_3_profiles(self):
        """Need at least 3 profiles for diversity."""
        assert len(_FINGERPRINT_PROFILES) >= 3, \
            f"Only {len(_FINGERPRINT_PROFILES)} profiles, need >= 3 for diversity"


class TestStealthInitScript:
    """Test the JavaScript init script builder."""

    def test_script_contains_all_patches(self):
        """Init script must contain all critical patches."""
        profile = _FINGERPRINT_PROFILES[0]
        script = _build_stealth_init_script(profile)

        # navigator.webdriver
        assert "navigator" in script and "webdriver" in script
        # navigator.plugins
        assert "navigator.plugins" in script or "navigator, 'plugins'" in script
        # navigator.languages
        assert "languages" in script
        # WebGL vendor/renderer (37445 = UNMASKED_VENDOR_WEBGL, 37446 = UNMASKED_RENDERER_WEBGL)
        assert "37445" in script
        assert "37446" in script
        # window.chrome
        assert "window.chrome" in script
        # Canvas noise
        assert "toDataURL" in script
        # Permissions API
        assert "permissions" in script
        # hardwareConcurrency
        assert "hardwareConcurrency" in script
        # deviceMemory
        assert "deviceMemory" in script

    def test_script_includes_profile_values(self):
        """Init script must include the specific profile values."""
        profile = _FINGERPRINT_PROFILES[0]
        script = _build_stealth_init_script(profile)

        assert profile["webgl_vendor"] in script
        assert profile["webgl_renderer"] in script
        assert str(profile["hardware_concurrency"]) in script
        assert str(profile["device_memory"]) in script
        assert profile["platform"] in script

    def test_script_is_valid_js_syntax(self):
        """Init script should be syntactically valid JavaScript (IIFE)."""
        profile = _FINGERPRINT_PROFILES[0]
        script = _build_stealth_init_script(profile)
        # Should start with an IIFE and end with the closing
        assert script.strip().startswith("(()")
        assert script.strip().endswith(");")
        # Should have balanced braces (rough check)
        assert script.count("{") == script.count("}"), "Unbalanced braces in init script"
        assert script.count("(") == script.count(")"), "Unbalanced parens in init script"

    def test_script_patches_webgl2(self):
        """Init script must patch WebGL2RenderingContext too, not just WebGL1."""
        profile = _FINGERPRINT_PROFILES[0]
        script = _build_stealth_init_script(profile)
        assert "WebGL2RenderingContext" in script, "Must patch WebGL2 too"

    def test_different_profiles_produce_different_scripts(self):
        """Different profiles should produce different init scripts."""
        script1 = _build_stealth_init_script(_FINGERPRINT_PROFILES[0])
        script2 = _build_stealth_init_script(_FINGERPRINT_PROFILES[-1])
        # At least the platform or WebGL renderer should differ
        assert script1 != script2, "Different profiles should produce different scripts"

    def test_essential_only_script_no_webgl(self):
        """full=False (system Chrome) should NOT include WebGL patches."""
        profile = _FINGERPRINT_PROFILES[0]
        script = _build_stealth_init_script(profile, full=False)
        # Essential patches should be present
        assert "webdriver" in script
        assert "languages" in script
        assert "toDataURL" in script
        # Full patches should NOT be present (system Chrome already has them)
        assert "37445" not in script, "WebGL vendor patch should not be in essential script"
        assert "37446" not in script, "WebGL renderer patch should not be in essential script"
        assert "hardwareConcurrency" not in script
        assert "deviceMemory" not in script

    def test_full_script_has_webgl(self):
        """full=True (bundled Chromium) SHOULD include WebGL patches."""
        profile = _FINGERPRINT_PROFILES[0]
        script = _build_stealth_init_script(profile, full=True)
        assert "37445" in script, "WebGL vendor patch should be in full script"
        assert "37446" in script, "WebGL renderer patch should be in full script"
        assert "hardwareConcurrency" in script

    def test_script_has_headless_chrome_ua_fix(self):
        """Init script should patch HeadlessChrome out of navigator.userAgent."""
        profile = _FINGERPRINT_PROFILES[0]
        script = _build_stealth_init_script(profile, full=False)
        assert "HeadlessChrome" in script, "Must check for and fix HeadlessChrome in UA"
        assert "replace" in script, "Must replace HeadlessChrome with Chrome"


class TestBezierMath:
    """Test the Bezier curve math."""

    def test_bezier_endpoints(self):
        """At t=0 and t=1, bezier returns start and end points."""
        p0, p1, p2 = (0, 0), (500, 100), (1000, 500)
        x0, y0 = _bezier_point(0, p0, p1, p2)
        x1, y1 = _bezier_point(1, p0, p1, p2)
        assert abs(x0 - 0) < 0.01 and abs(y0 - 0) < 0.01
        assert abs(x1 - 1000) < 0.01 and abs(y1 - 500) < 0.01

    def test_bezier_midpoint(self):
        """At t=0.5, bezier returns the quadratic midpoint."""
        p0, p1, p2 = (0, 0), (500, 100), (1000, 500)
        x, y = _bezier_point(0.5, p0, p1, p2)
        # Quadratic bezier at t=0.5: 0.25*p0 + 0.5*p1 + 0.25*p2
        expected_x = 0.25 * 0 + 0.5 * 500 + 0.25 * 1000
        expected_y = 0.25 * 0 + 0.5 * 100 + 0.25 * 500
        assert abs(x - expected_x) < 0.01
        assert abs(y - expected_y) < 0.01

    def test_bezier_curve_not_straight_line(self):
        """Bezier with a control point off the direct path should curve."""
        p0, p1, p2 = (0, 0), (500, 500), (1000, 0)  # Control point above
        # At t=0.5, y should be > 0 (curving up)
        x, y = _bezier_point(0.5, p0, p1, p2)
        assert y > 0, f"Bezier should curve above the direct path, got y={y}"


class TestHumanBehaviorFunctions:
    """Test that human behavior simulation functions exist and are callable."""

    def test_human_mouse_move_exists(self):
        """_human_mouse_move should be an async function."""
        assert callable(_human_mouse_move)
        assert inspect.iscoroutinefunction(_human_mouse_move)

    def test_simulate_human_behavior_exists(self):
        """_simulate_human_behavior should be an async function."""
        assert callable(_simulate_human_behavior)
        assert inspect.iscoroutinefunction(_simulate_human_behavior)


class TestBrowserSessionDefaults:
    """Test that BrowserSession has the right defaults for stealth."""

    def test_stealthy_defaults_real_chrome_true(self):
        """StealthyBrowser should default real_chrome=True."""
        session = StealthyBrowser()
        assert session._real_chrome is True

    def test_stealthy_defaults_humanize_true(self):
        """StealthyBrowser should default humanize=True."""
        session = StealthyBrowser()
        assert session._humanize is True

    def test_dynamic_defaults_real_chrome_true(self):
        """DynamicBrowser should also default real_chrome=True (system Chrome is better)."""
        session = DynamicBrowser()
        assert session._real_chrome is True

    def test_stealthy_is_stealthy(self):
        """StealthyBrowser._is_stealthy should be True."""
        session = StealthyBrowser()
        assert session._is_stealthy is True

    def test_dynamic_is_not_stealthy(self):
        """DynamicBrowser._is_stealthy should be False."""
        session = DynamicBrowser()
        assert session._is_stealthy is False

    def test_humanize_can_be_disabled(self):
        """humanize=False should be respected."""
        session = StealthyBrowser(humanize=False)
        assert session._humanize is False

    def test_device_scale_factor_removed(self):
        """device_scale_factor should NOT be in the start() method context_options."""
        source = inspect.getsource(BrowserSession.start)
        assert "device_scale_factor" not in source, \
            "device_scale_factor should be removed (was 2, a Mac Retina giveaway)"

    def test_init_script_only_for_stealthy(self):
        """Init script injection should only happen for stealthy sessions."""
        source = inspect.getsource(BrowserSession.start)
        # The init script should be guarded by self._is_stealthy
        assert "self._is_stealthy" in source
        assert "add_init_script" in source
        # Check it's inside the stealthy guard
        stealthy_idx = source.index("self._is_stealthy")
        init_idx = source.index("add_init_script")
        # The init script code should come after a stealthy check
        assert init_idx > stealthy_idx, \
            "add_init_script should be inside the stealthy guard"

    def test_human_behavior_only_for_stealthy(self):
        """Human behavior simulation should only run for stealthy sessions."""
        source = inspect.getsource(BrowserSession.fetch)
        assert "self._humanize" in source
        assert "_simulate_human_behavior" in source
        # Should be guarded by self._is_stealthy and self._humanize
        assert "self._is_stealthy and self._humanize" in source

    def test_cf_solver_uses_human_mouse(self):
        """Cloudflare solver should use _human_mouse_move before clicking."""
        from master_fetch.browser import _solve_cloudflare
        source = inspect.getsource(_solve_cloudflare)
        assert "_human_mouse_move" in source, \
            "CF solver should use _human_mouse_move before clicking"


class TestCanvasNoiseFix:
    """Test that canvas noise intercepts both toDataURL and getImageData."""

    def test_init_script_intercepts_getImageData(self):
        """Init script must intercept getImageData, not just toDataURL."""
        profile = _FINGERPRINT_PROFILES[0]
        script = _build_stealth_init_script(profile, full=False)
        assert "getImageData" in script, "Must intercept getImageData for canvas noise"
        assert "toDataURL" in script, "Must also intercept toDataURL"

    def test_init_script_has_noise_function(self):
        """Init script must have a shared _noisePixels function."""
        profile = _FINGERPRINT_PROFILES[0]
        script = _build_stealth_init_script(profile, full=False)
        assert "_noisePixels" in script, "Must have shared noise function"
        # The noise function should modify the red channel (index 0)
        assert "data[i]" in script, "Must modify pixel data"

    def test_getImageData_noise_applies_to_all_reads(self):
        """getImageData noise should apply to ALL reads, not just small regions.

        Detectors like sannysoft/creepjs read the full canvas (e.g., 200x50),
        not small 16x16 regions. The noise must apply to large reads too.
        """
        profile = _FINGERPRINT_PROFILES[0]
        script = _build_stealth_init_script(profile, full=False)
        # Should NOT have the old <= 16 guard
        assert "<= 16" not in script, "Must not guard noise to small regions only"
        # Should noise all reads (just the first 16 pixels for perf)
        assert "Math.min(64" in script, "Should limit noise to first 16 pixels for perf"


class TestMemoryOptimization:
    """Test memory optimization features."""

    def test_renderer_process_limit_flag(self):
        """DEFAULT_ARGS must include --renderer-process-limit=1."""
        from master_fetch.browser import DEFAULT_ARGS
        assert "--renderer-process-limit=1" in DEFAULT_ARGS, \
            "Must limit renderer processes to 1 (we fetch one page at a time)"

    def test_v8_heap_limit_flag(self):
        """DEFAULT_ARGS must include V8 heap limit."""
        from master_fetch.browser import DEFAULT_ARGS
        assert any("--js-flags=--max-old-space-size=" in arg for arg in DEFAULT_ARGS), \
            "Must cap V8 old space heap"
        # Verify the limit is reasonable (>=256, <=1024)
        for arg in DEFAULT_ARGS:
            if "--max-old-space-size=" in arg:
                size = int(arg.split("=")[-1])
                assert 256 <= size <= 1024, f"V8 heap limit {size}MB out of range"

    def test_cdp_session_cleanup_in_fetch(self):
        """fetch() must close CDP sessions to prevent leaks."""
        fetch_src = inspect.getsource(StealthyBrowser.fetch)
        # Must create and detach CDP session for memory cleanup
        assert "new_cdp_session" in fetch_src, "Must create CDP session for memory cleanup"
        assert "cdp.detach()" in fetch_src, "Must detach CDP session after use"

    def test_memory_pressure_notification_in_fetch(self):
        """fetch() must send Memory.simulatePressureNotification after fetch."""
        fetch_src = inspect.getsource(StealthyBrowser.fetch)
        assert "simulatePressureNotification" in fetch_src, \
            "Must trigger memory pressure notification after fetch"
        assert "moderate" in fetch_src, \
            "Must use moderate level (not critical, which could crash tabs)"

    def test_memory_cleanup_before_page_close(self):
        """Memory cleanup must happen BEFORE page.close() in the success path."""
        fetch_src = inspect.getsource(StealthyBrowser.fetch)
        pressure_idx = fetch_src.index("simulatePressureNotification")
        close_idx = fetch_src.index("await page.close()")
        assert pressure_idx < close_idx, \
            "Memory pressure notification must come before page.close()"

    def test_stealth_injection_via_commit_evaluate(self):
        """Stealth script must be injected via wait_until=commit + evaluate."""
        fetch_src = inspect.getsource(StealthyBrowser.fetch)
        assert "commit" in fetch_src, "Must use wait_until=commit for early injection"
        assert "page.evaluate" in fetch_src, "Must inject via page.evaluate"
        assert "self._init_script" in fetch_src, "Must use the init script"

    def test_cdp_cleanup_in_error_path(self):
        """Error path must close page (CDP session is created per-use in success path)."""
        fetch_src = inspect.getsource(StealthyBrowser.fetch)
        # Find the except block
        except_idx = fetch_src.index("except Exception as e:")
        after_except = fetch_src[except_idx:]
        assert "page.close()" in after_except, \
            "Error path must close page"

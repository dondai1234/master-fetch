"""Stealth engine tests: fingerprint profiles, init script generation,
browser args (memory + anti-detection), human behavior simulation.

Tests the REAL stealth functions against real inputs. No browser launch.
Adversarial: profiles must be internally consistent, init script must
contain essential patches, canvas noise must intercept both toDataURL
and getImageData, HeadlessChrome must not appear in the UA.
"""

import pytest
from master_fetch.browser import (
    DEFAULT_ARGS, HARMFUL_ARGS, STEALTH_ARGS,
    _FINGERPRINT_PROFILES, _generate_fingerprint_profile,
    _build_stealth_init_script, StealthyBrowser,
)


# ─── Browser args ──────────────────────────────────────────────────

class TestBrowserArgs:

    def test_memory_optimization_flags_present(self):
        assert "--renderer-process-limit=1" in DEFAULT_ARGS
        assert "--js-flags=--max-old-space-size=512" in DEFAULT_ARGS

    def test_harmful_automation_flag_suppressed(self):
        assert "--enable-automation" in HARMFUL_ARGS

    def test_no_headless_flag_in_default_args(self):
        # --headless is not in DEFAULT_ARGS (added conditionally at launch)
        for arg in DEFAULT_ARGS:
            assert not arg.startswith("--headless")

    def test_disable_blink_automation_in_stealth_args(self):
        assert "--disable-blink-features=AutomationControlled" in STEALTH_ARGS

    def test_stealth_args_does_not_contain_harmful(self):
        # No harmful args should appear in stealth args
        for harmful in HARMFUL_ARGS:
            assert harmful not in STEALTH_ARGS
            assert harmful not in DEFAULT_ARGS


class TestSessionProxy:

    @pytest.mark.asyncio
    async def test_active_session_rejects_different_proxy(self):
        session = StealthyBrowser()
        session._is_alive = True

        with pytest.raises(ValueError, match="Proxy is fixed"):
            await session.fetch(
                "https://example.com",
                proxy="http://127.0.0.1:8080",
            )


# ─── Fingerprint profiles ─────────────────────────────────────────

class TestFingerprintProfiles:

    def test_has_four_profiles(self):
        assert len(_FINGERPRINT_PROFILES) == 4

    def test_all_profiles_have_required_fields(self):
        required = {"platform", "languages", "hardware_concurrency",
                    "device_memory", "webgl_vendor", "webgl_renderer", "plugins"}
        for profile in _FINGERPRINT_PROFILES:
            missing = required - set(profile.keys())
            assert not missing, f"Profile missing fields: {missing}"

    def test_win32_profiles_have_nvidia_or_intel_or_amd(self):
        win32_profiles = [p for p in _FINGERPRINT_PROFILES if p["platform"] == "Win32"]
        assert len(win32_profiles) == 3
        renderers = [p["webgl_vendor"] for p in win32_profiles]
        assert any("NVIDIA" in r for r in renderers)
        assert any("Intel" in r for r in renderers)
        assert any("AMD" in r for r in renderers)

    def test_macintel_profile_has_apple_webgl(self):
        mac_profiles = [p for p in _FINGERPRINT_PROFILES if p["platform"] == "MacIntel"]
        assert len(mac_profiles) == 1
        assert "Apple" in mac_profiles[0]["webgl_vendor"]

    def test_all_profiles_have_five_plugins(self):
        for profile in _FINGERPRINT_PROFILES:
            assert len(profile["plugins"]) == 5

    def test_all_profiles_have_en_us_languages(self):
        for profile in _FINGERPRINT_PROFILES:
            assert "en-US" in profile["languages"]

    def test_hardware_concurrency_reasonable(self):
        for profile in _FINGERPRINT_PROFILES:
            assert 4 <= profile["hardware_concurrency"] <= 16

    def test_device_memory_reasonable(self):
        for profile in _FINGERPRINT_PROFILES:
            assert profile["device_memory"] in (4, 8, 16)


# ─── Fingerprint generation ───────────────────────────────────────

class TestGenerateFingerprint:

    def test_returns_a_copy_not_reference(self):
        p1 = _generate_fingerprint_profile()
        p2 = _generate_fingerprint_profile()
        # Mutating one should not affect the other
        p1["platform"] = "modified"
        assert p2["platform"] != "modified"

    def test_returns_valid_profile(self):
        profile = _generate_fingerprint_profile()
        assert profile in _FINGERPRINT_PROFILES or profile["platform"] in ("Win32", "MacIntel")


# ─── Init script generation ───────────────────────────────────────

class TestInitScript:

    def test_essential_patches_present(self):
        script = _build_stealth_init_script(_FINGERPRINT_PROFILES[0], full=False)
        # navigator.webdriver -> undefined
        assert "webdriver" in script
        assert "undefined" in script
        # HeadlessChrome replacement
        assert "HeadlessChrome" in script
        # Languages
        assert "en-US" in script
        # Canvas noise (both toDataURL and getImageData)
        assert "toDataURL" in script
        assert "getImageData" in script
        # Permissions API
        assert "permissions" in script

    def test_full_patches_present_when_full_true(self):
        script = _build_stealth_init_script(_FINGERPRINT_PROFILES[0], full=True)
        # WebGL vendor/renderer
        assert "webgl_vendor" in script or "getParameter" in script
        # Plugins
        assert "PDF Viewer" in script or "plugins" in script
        # Platform
        assert "platform" in script
        # Hardware concurrency
        assert "hardwareConcurrency" in script
        # window.chrome
        assert "chrome" in script

    def test_full_patches_absent_when_full_false(self):
        script = _build_stealth_init_script(_FINGERPRINT_PROFILES[0], full=False)
        # Should not contain the full patches
        # Check that WebGL renderer value is NOT injected
        assert _FINGERPRINT_PROFILES[0]["webgl_renderer"] not in script

    def test_canvas_noise_uses_let_not_const(self):
        # The _seed variable must use let, not const (const was a bug that
        # crashed the entire init script)
        script = _build_stealth_init_script(_FINGERPRINT_PROFILES[0], full=False)
        assert "let _seed" in script
        assert "const _seed" not in script

    def test_canvas_noise_intercepts_both_methods(self):
        script = _build_stealth_init_script(_FINGERPRINT_PROFILES[0], full=False)
        assert "toDataURL" in script
        assert "getImageData" in script
        # Both should be intercepted (not just one)
        assert script.count("toDataURL") >= 2  # original + override
        assert script.count("getImageData") >= 2

    def test_no_device_scale_factor_2(self):
        # device_scale_factor=2 was a Mac Retina giveaway, removed in v11.1.0
        script = _build_stealth_init_script(_FINGERPRINT_PROFILES[0], full=True)
        assert "deviceScaleFactor" not in script or "deviceScaleFactor" not in script.replace("2", "")

    def test_different_profiles_produce_different_scripts(self):
        win_profile = next(p for p in _FINGERPRINT_PROFILES if p["platform"] == "Win32")
        mac_profile = next(p for p in _FINGERPRINT_PROFILES if p["platform"] == "MacIntel")
        script_win = _build_stealth_init_script(win_profile, full=True)
        script_mac = _build_stealth_init_script(mac_profile, full=True)
        assert script_win != script_mac

    def test_script_is_valid_js_syntax(self):
        # Basic syntax check: balanced braces
        script = _build_stealth_init_script(_FINGERPRINT_PROFILES[0], full=True)
        assert script.count("{") == script.count("}")
        assert script.count("(") == script.count(")")

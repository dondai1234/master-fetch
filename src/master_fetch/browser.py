"""Hound's own browser sessions using patchright directly.

Replaces scrapling's AsyncStealthySession and AsyncDynamicSession with direct
patchright async API usage. Includes the Cloudflare Turnstile solver ported
from scrapling (it's standard Playwright page manipulation, ~80 lines).

Architecture:
- StealthyBrowser: anti-detect browser with fingerprinting, stealth args,
  Cloudflare solver, resource blocking, page pooling
- DynamicBrowser: simpler JS-rendering browser (same engine, no stealth args)
- Both: async context manager (async with), .start(), .close(), .fetch()
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import tempfile
from random import randint
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("master_fetch.browser")

# ─── Browser flags (ported from scrapling's constants.py) ─────────────────────

# These args make the browser faster and less detectable. Source:
# https://peter.sh/experiments/chromium-command-line-switches/
DEFAULT_ARGS: Tuple[str, ...] = (
    "--no-pings",
    "--no-first-run",
    "--disable-infobars",
    "--disable-breakpad",
    "--no-service-autorun",
    "--homepage=about:blank",
    "--password-store=basic",
    "--disable-hang-monitor",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--disable-search-engine-choice-screen",
    # Memory: limit renderer processes to 1 (we fetch one page at a time,
    # saves ~100-200MB per avoided process). Not JS-detectable.
    "--renderer-process-limit=1",
    # Memory: cap V8 old space at 512MB (default 4GB). Most pages use <100MB.
    # Prevents unbounded heap growth across many sequential fetches.
    "--js-flags=--max-old-space-size=512",
)

# Args to suppress (scrapling found these enable automation signals)
HARMFUL_ARGS: Tuple[str, ...] = (
    "--enable-automation",
    "--disable-popup-blocking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
)

STEALTH_ARGS: Tuple[str, ...] = (
    "--test-type",
    "--lang=en-US",
    "--mute-audio",
    "--disable-sync",
    "--hide-scrollbars",
    "--disable-logging",
    "--start-maximized",
    "--enable-async-dns",
    "--accept-lang=en-US",
    "--use-mock-keychain",
    "--disable-translate",
    "--disable-voice-input",
    "--window-position=0,0",
    "--disable-wake-on-wifi",
    "--ignore-gpu-blocklist",
    "--enable-tcp-fast-open",
    "--enable-web-bluetooth",
    "--disable-cloud-import",
    "--disable-print-preview",
    "--disable-dev-shm-usage",
    "--metrics-recording-only",
    "--disable-crash-reporter",
    "--disable-partial-raster",
    "--disable-gesture-typing",
    "--disable-checker-imaging",
    "--disable-prompt-on-repost",
    "--force-color-profile=srgb",
    "--font-render-hinting=none",
    "--aggressive-cache-discard",
    "--disable-cookie-encryption",
    "--disable-domain-reliability",
    "--disable-threaded-animation",
    "--disable-threaded-scrolling",
    "--enable-simple-cache-backend",
    "--disable-background-networking",
    "--enable-surface-synchronization",
    "--disable-image-animation-resync",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
    "--prerender-from-omnibox=disabled",
    "--safebrowsing-disable-auto-update",
    "--disable-offer-upload-credit-cards",
    "--disable-background-timer-throttling",
    "--disable-new-content-rendering-timeout",
    "--run-all-compositor-stages-before-draw",
    "--disable-client-side-phishing-detection",
    "--disable-backgrounding-occluded-windows",
    "--disable-layer-tree-host-memory-pressure",
    "--autoplay-policy=user-gesture-required",
    "--disable-offer-store-unmasked-wallet-cards",
    "--disable-blink-features=AutomationControlled",
    "--disable-component-extensions-with-background-pages",
    "--enable-features=NetworkService,NetworkServiceInProcess,TrustTokens,TrustTokensAlwaysAllowIssuance",
    "--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4",
    "--disable-features=AudioServiceOutOfProcess,TranslateUI,BlinkGenPropertyTrees",
)

# Resource types to block when disable_resources=True
DISABLED_RESOURCE_TYPES: Set[str] = {
    "font", "image", "media", "beacon", "object",
    "imageset", "texttrack", "websocket", "csp_report", "stylesheet",
}

# Cloudflare challenge iframe URL pattern
__CF_PATTERN = re.compile(
    r"^https?://challenges\.cloudflare\.com/cdn-cgi/challenge-platform/.*"
)


# ─── Proxy helper ────────────────────────────────────────────────────────────

def _construct_proxy_dict(proxy: str | Dict[str, str]) -> Dict[str, str]:
    """Convert a proxy string to a Playwright proxy dict."""
    if isinstance(proxy, dict):
        return proxy
    parsed = urlparse(proxy)
    if parsed.scheme not in ("http", "https", "socks4", "socks5"):
        raise ValueError(f"Invalid proxy scheme: {parsed.scheme}")
    result: Dict[str, str] = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result


def _is_domain_blocked(hostname: str, blocked_domains: frozenset) -> bool:
    """Check if a hostname matches any blocked domain (including subdomains)."""
    if not hostname:
        return False
    for domain in blocked_domains:
        if hostname == domain or hostname.endswith(f".{domain}"):
            return True
    return False


# ─── Resource blocking handler ───────────────────────────────────────────────

def _create_route_handler(
    disable_resources: bool,
    blocked_domains: Optional[Set[str]] = None,
) -> Callable[[Any], Awaitable[None]]:
    """Create an async route handler for resource blocking."""
    disabled = DISABLED_RESOURCE_TYPES if disable_resources else set()
    domains = frozenset(blocked_domains) if blocked_domains else frozenset()

    async def handler(route: Any) -> None:
        try:
            rt = route.request.resource_type
            if rt in disabled:
                await route.abort()
                return
            if domains:
                hostname = urlparse(route.request.url).hostname or ""
                if _is_domain_blocked(hostname, domains):
                    await route.abort()
                    return
            await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    return handler


# ─── Cloudflare solver ───────────────────────────────────────────────────────

async def _get_page_content(page: Any, max_retries: int = 20) -> str:
    """Get page.content() with retry workaround for Windows Playwright bug.

    See: https://github.com/microsoft/playwright/issues/16108
    """
    for _ in range(max_retries):
        try:
            return (await page.content()) or ""
        except Exception:
            await page.wait_for_timeout(500)
    return ""


def _detect_cloudflare(page_content: str) -> Optional[str]:
    """Detect the type of Cloudflare challenge in the page content.

    Returns: 'non-interactive', 'managed', 'interactive', 'embedded', or None.
    """
    challenge_types = ("non-interactive", "managed", "interactive")
    for ctype in challenge_types:
        if f"cType: '{ctype}'" in page_content:
            return ctype

    # Check for embedded turnstile captcha
    if 'challenges.cloudflare.com/turnstile/v' in page_content:
        return "embedded"

    return None


async def _solve_cloudflare(page: Any) -> None:
    """Solve Cloudflare Turnstile/Interstitial challenge on a Playwright page.

    Ported from scrapling's StealthySessionMixin._cloudflare_solver.
    Handles: non-interactive, managed, interactive, and embedded challenges.

    The solver:
    1. Waits for network idle
    2. Detects challenge type
    3. For non-interactive: waits for the "Just a moment" page to clear
    4. For interactive/embedded: clicks the Turnstile checkbox at the
       right coordinates (calculated from the iframe bounding box)
    5. Waits for the challenge to resolve, retries if needed
    """
    # Wait for network to settle
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    challenge_type = _detect_cloudflare(await _get_page_content(page))
    if not challenge_type:
        logger.debug("No Cloudflare challenge found")
        return

    logger.info(f"Cloudflare challenge type: {challenge_type}")

    if challenge_type == "non-interactive":
        # Just wait for the challenge to auto-resolve
        attempts = 0
        while "<title>Just a moment...</title>" in (await _get_page_content(page)):
            if attempts >= 30:
                logger.info("Non-interactive challenge still present after 30s, continuing")
                break
            await page.wait_for_timeout(1000)
            try:
                await page.wait_for_load_state()
            except Exception:
                pass
            attempts += 1
        logger.info("Cloudflare non-interactive challenge resolved")
        return

    # Interactive/managed/embedded: need to click the Turnstile checkbox
    box_selector = "#cf_turnstile div, #cf-turnstile div, .turnstile>div>div"
    if challenge_type != "embedded":
        box_selector = ".main-content p+div>div>div"
        # Wait for the "Verifying you are human" spinner to disappear
        spinner_attempts = 0
        while "Verifying you are human." in (await _get_page_content(page)):
            if spinner_attempts >= 20:
                break
            await page.wait_for_timeout(500)
            spinner_attempts += 1

    # Find the Cloudflare iframe
    outer_box: Any = {}
    iframe = page.frame(url=__CF_PATTERN)
    if iframe is not None:
        # Wait for iframe stability
        try:
            await iframe.wait_for_load_state("load")
        except Exception:
            pass

        if challenge_type != "embedded":
            # Wait for iframe to be visible
            iframe_visible_attempts = 0
            while not await (await iframe.frame_element()).is_visible():
                if iframe_visible_attempts >= 20:
                    break
                await page.wait_for_timeout(500)
                iframe_visible_attempts += 1

        try:
            outer_box = await (await iframe.frame_element()).bounding_box()
        except Exception:
            outer_box = {}

    if not iframe or not outer_box:
        # Check if challenge already solved
        if "<title>Just a moment...</title>" not in (await _get_page_content(page)):
            logger.info("Cloudflare challenge resolved without clicking")
            return
        # Try to find the box on the page directly
        try:
            outer_box = await page.locator(box_selector).last.bounding_box()
        except Exception:
            logger.warning("Could not find Cloudflare checkbox to click")
            return

    if not outer_box:
        logger.warning("Cloudflare checkbox bounding box is empty")
        return

    # Calculate click coordinates (offset into the checkbox area)
    captcha_x = outer_box["x"] + randint(26, 28)
    captcha_y = outer_box["y"] + randint(25, 27)

    # Click the checkbox with human-like mouse movement
    try:
        # Move to the checkbox via bezier curve (not a straight line)
        await _human_mouse_move(page, captcha_x, captcha_y)
        await page.wait_for_timeout(randint(100, 300))
        await page.mouse.click(captcha_x, captcha_y, delay=randint(100, 200), button="left")
    except Exception as e:
        logger.warning(f"Cloudflare click failed: {e}")
        return

    # Wait for network to settle after click
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    # Wait for the challenge page to clear
    if challenge_type != "embedded":
        attempts = 0
        while "<title>Just a moment...</title>" in (await _get_page_content(page)):
            if attempts >= 100:
                logger.info("Cloudflare page didn't disappear after 10s, continuing")
                break
            await page.wait_for_timeout(100)
            attempts += 1

    # Final stability wait
    try:
        await page.wait_for_load_state("load")
        await page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass

    # Check if solved
    if "<title>Just a moment...</title>" not in (await _get_page_content(page)):
        logger.info("Cloudflare challenge solved")
        return

    # Not solved: retry
    logger.info("Cloudflare challenge still present, retrying...")
    await _solve_cloudflare(page)


# ─── Channel detection: prefer system Chrome over bundled Chromium ───────────────

_chrome_channel_cache: Optional[str] = None
_chrome_version_cache: Optional[str] = None


def _detect_chrome_channel() -> str:
    """Detect if system Google Chrome is installed. Cache the result.

    System Chrome (channel='chrome') has a real TLS fingerprint (JA4)
    that matches what real users have. Bundled Chromium's fingerprint
    differs and is detectable. The benchmark showed channel=chrome is
    the bigger lever than patchright's own patches.

    Returns 'chrome' if system Chrome is found, 'chromium' otherwise.
    """
    global _chrome_channel_cache
    if _chrome_channel_cache is not None:
        return _chrome_channel_cache

    import shutil

    # Common Chrome executable names on each platform
    if sys.platform == "win32":
        candidates = [
            os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                _chrome_channel_cache = "chrome"
                logger.info("System Chrome detected: %s", path)
                return "chrome"
    else:
        # POSIX: check if google-chrome or chromium is in PATH
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            if shutil.which(name):
                _chrome_channel_cache = "chrome"
                logger.info("System Chrome detected: %s", name)
                return "chrome"

    _chrome_channel_cache = "chromium"
    logger.info("System Chrome not found, using bundled Chromium")
    return "chromium"


def _get_chrome_ua() -> Optional[str]:
    """Get a User-Agent string matching the installed system Chrome version.

    In headless mode, Chrome reports 'HeadlessChrome' in the UA which is
    a dead giveaway. We read the real Chrome version and construct a UA
    with 'Chrome' instead of 'HeadlessChrome'. This UA matches the TLS
    fingerprint from channel=chrome.

    Returns None if Chrome version can't be determined.
    """
    global _chrome_version_cache
    if _chrome_version_cache is not None:
        return _chrome_version_cache

    import subprocess

    chrome_path = None
    if sys.platform == "win32":
        for path in [
            os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]:
            if os.path.isfile(path):
                chrome_path = path
                break
    else:
        import shutil
        for name in ("google-chrome", "google-chrome-stable"):
            chrome_path = shutil.which(name)
            if chrome_path:
                break

    if not chrome_path:
        return None

    try:
        if sys.platform == "win32":
            # On Windows, chrome.exe --version hangs (starts the browser).
            # Use PowerShell to read the file version instead.
            result = subprocess.run(
                ["powershell", "-Command",
                 f"(Get-Item '{chrome_path}').VersionInfo.FileVersion"],
                capture_output=True, text=True, timeout=5,
            )
            version_output = (result.stdout or "").strip()
        else:
            result = subprocess.run(
                [chrome_path, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            version_output = (result.stdout or "").strip()
        # Parse version: 'Google Chrome 150.0.0.0' -> '150.0.0.0'
        import re
        match = re.search(r'(\d+\.\d+\.\d+\.\d+)', version_output)
        if match:
            version = match.group(1)
            if sys.platform == "win32":
                ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"
            elif sys.platform == "darwin":
                ua = f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"
            else:
                ua = f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"
            _chrome_version_cache = ua
            logger.info("Chrome UA constructed: Chrome/%s", version)
            return ua
    except Exception:
        pass

    return None


# ─── Coherent fingerprint profiles ─────────────────────────────────────────────
# Each profile is a complete, internally consistent identity. All values tell
# the same story: platform matches WebGL renderer matches languages.
# Detectors cross-check for contradictions, so coherence > individual values.

_FINGERPRINT_PROFILES: List[Dict[str, Any]] = [
    {
        "platform": "Win32",
        "languages": ["en-US", "en"],
        "hardware_concurrency": 8,
        "device_memory": 8,
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)",
        "plugins": [
            {"name": "PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "Chrome PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "Chromium PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "Microsoft Edge PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "WebKit built-in PDF", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
        ],
    },
    {
        "platform": "Win32",
        "languages": ["en-US", "en"],
        "hardware_concurrency": 12,
        "device_memory": 16,
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0)",
        "plugins": [
            {"name": "PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "Chrome PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "Chromium PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "Microsoft Edge PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "WebKit built-in PDF", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
        ],
    },
    {
        "platform": "Win32",
        "languages": ["en-US", "en"],
        "hardware_concurrency": 8,
        "device_memory": 8,
        "webgl_vendor": "Google Inc. (AMD)",
        "webgl_renderer": "ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0)",
        "plugins": [
            {"name": "PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "Chrome PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "Chromium PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "Microsoft Edge PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "WebKit built-in PDF", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
        ],
    },
    {
        "platform": "MacIntel",
        "languages": ["en-US", "en"],
        "hardware_concurrency": 8,
        "device_memory": 8,
        "webgl_vendor": "Google Inc. (Apple)",
        "webgl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
        "plugins": [
            {"name": "PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "Chrome PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "Chromium PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "Microsoft Edge PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
            {"name": "WebKit built-in PDF", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
        ],
    },
]


def _generate_fingerprint_profile() -> Dict[str, Any]:
    """Pick a random coherent fingerprint profile.

    Returns a dict with platform, languages, hardware_concurrency,
    device_memory, webgl_vendor, webgl_renderer, plugins. All values
    are internally consistent (e.g., MacIntel platform matches Apple
    WebGL renderer).
    """
    import random
    return random.choice(_FINGERPRINT_PROFILES).copy()


def _build_stealth_init_script(profile: Dict[str, Any], full: bool = True) -> str:
    """Build a JavaScript init script from a fingerprint profile.

    Patches JS-layer signals that patchright does NOT handle:
    - navigator.webdriver (patchright sets False, undefined is stealthier)
    - navigator.languages (add 'en' fallback for consistency)
    - Canvas fingerprint: per-session deterministic noise
    - Permissions API consistency

    When full=True (bundled Chromium), also patches:
    - navigator.plugins (empty in headless, populated in real Chrome)
    - WebGL vendor/renderer (SwiftShader in headless = dead giveaway)
    - navigator.hardwareConcurrency / deviceMemory
    - navigator.platform
    - window.chrome runtime object

    The script runs before any page JavaScript via CDP
    Page.addScriptToEvaluateOnNewDocument, so detection scripts see
    patched values from the first line.
    """
    import json as _json

    plugins_js = _json.dumps(profile["plugins"])
    languages_js = _json.dumps(profile["languages"])
    webgl_vendor = profile["webgl_vendor"]
    webgl_renderer = profile["webgl_renderer"]
    platform = profile["platform"]
    hw_concurrency = profile["hardware_concurrency"]
    device_memory = profile["device_memory"]

    # Essential patches: always applied (even with system Chrome)
    essential = f"""(() => {{
  // ── navigator.webdriver (patchright sets false, undefined is stealthier) ──
  try {{ Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }}); }} catch(e) {{}}

  // ── navigator.userAgent: remove 'HeadlessChrome' (dead giveaway in headless) ──
  // System Chrome reports 'HeadlessChrome/150' in headless mode. Replace with
  // 'Chrome/150' so the UA matches the real Chrome TLS fingerprint.
  try {{
    const _origUA = navigator.userAgent;
    if (_origUA.includes('HeadlessChrome')) {{
      Object.defineProperty(navigator, 'userAgent',
        {{ get: () => _origUA.replace('HeadlessChrome', 'Chrome') }});
    }}
  }} catch(e) {{}}

  // ── navigator.languages (add 'en' fallback for consistency) ──
  try {{ Object.defineProperty(navigator, 'languages', {{ get: () => {languages_js} }}); }} catch(e) {{}}

  // ── Canvas fingerprint: per-session deterministic noise ──
  // Intercepts BOTH toDataURL and getImageData. Many detectors (sannysoft,
  // creepjs) compute canvas hashes via getImageData directly, bypassing
  // toDataURL. Noise is deterministic per session (seeded PRNG) so it's
  // consistent within a session but different across sessions.
  try {{
    let _seed = {randint(1, 999999)};
    function _prng() {{ _seed = (_seed * 16807) % 2147483647; return (_seed - 1) / 2147483646; }}
    function _noisePixels(imgData) {{
      const limit = Math.min(64, imgData.data.length);
      for (let i = 0; i < limit; i += 4) {{
        imgData.data[i] = (imgData.data[i] + (_prng() > 0.5 ? 1 : 0)) & 0xFF;
      }}
      return imgData;
    }}
    // Intercept toDataURL
    const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(...args) {{
      const ctx = this.getContext('2d');
      if (ctx) {{
        try {{
          const w = this.width, h = this.height;
          if (w > 0 && h > 0 && w < 4096 && h < 4096) {{
            const img = ctx.getImageData(0, 0, Math.min(w, 16), Math.min(h, 16));
            ctx.putImageData(_noisePixels(img), 0, 0);
          }}
        }} catch(e) {{}}
      }}
      return _origToDataURL.apply(this, args);
    }};
    // Intercept getImageData (used by sannysoft, creepjs for canvas hashing)
    const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function(...args) {{
      const imgData = _origGetImageData.apply(this, args);
      // Noise the first 16 pixels regardless of read size (detectors like
      // sannysoft/creepjs read the full canvas, not small regions).
      // Performance: only modifying 16 pixels, not the entire image.
      if (imgData.data.length >= 64) {{
        return _noisePixels(imgData);
      }}
      return imgData;
    }};
  }} catch(e) {{}}

  // ── Permissions API consistency ──
  try {{
    const _origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = function(params) {{
      if (params.name === 'notifications') {{
        return Promise.resolve({{ state: 'prompt', onchange: null }});
      }}
      return _origQuery.call(this, params);
    }};
  }} catch(e) {{}}
"""

    # Full patches: only for bundled Chromium (channel=chromium)
    # System Chrome already has correct values; overriding them creates
    # contradictions that detectors specifically look for.
    if full:
        essential += f"""
  // ── navigator.platform (bundled Chromium may report wrong platform) ──
  try {{ Object.defineProperty(navigator, 'platform', {{ get: () => {repr(platform)} }}); }} catch(e) {{}}

  // ── navigator.plugins (empty in headless = bot signal) ──
  try {{
    const pluginsData = {plugins_js};
    const fakePlugins = pluginsData.map(p => ({{
      name: p.name, filename: p.filename, description: p.description,
      length: 1, 0: {{ type: 'application/pdf', suffixes: 'pdf', description: p.description }}
    }}));
    Object.defineProperty(navigator, 'plugins', {{
      get: () => {{
        const arr = fakePlugins;
        arr.item = i => arr[i] || null;
        arr.namedItem = n => arr.find(p => p.name === n) || null;
        arr.refresh = () => {{}};
        return arr;
      }}
    }});
  }} catch(e) {{}}

  // ── navigator.hardwareConcurrency ──
  try {{ Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {hw_concurrency} }}); }} catch(e) {{}}

  // ── navigator.deviceMemory ──
  try {{ Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {device_memory} }}); }} catch(e) {{}}

  // ── window.chrome (missing in headless) ──
  try {{
    if (!window.chrome) {{
      window.chrome = {{ runtime: {{}}, loadTimes: () => {{}}, csi: () => {{}} }};
    }} else if (!window.chrome.runtime) {{
      window.chrome.runtime = {{}};
    }}
  }} catch(e) {{}}

  // ── WebGL vendor/renderer (SwiftShader = headless giveaway) ──
  try {{
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {{
      if (param === 37445) return {repr(webgl_vendor)};
      if (param === 37446) return {repr(webgl_renderer)};
      return getParameter.call(this, param);
    }};
    if (typeof WebGL2RenderingContext !== 'undefined') {{
      const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
      WebGL2RenderingContext.prototype.getParameter = function(param) {{
        if (param === 37445) return {repr(webgl_vendor)};
        if (param === 37446) return {repr(webgl_renderer)};
        return getParameter2.call(this, param);
      }};
    }}
  }} catch(e) {{}}
"""

    essential += "})();"
    return essential


# ─── Human behavior simulation ─────────────────────────────────────────────────

def _bezier_point(t: float, p0: tuple, p1: tuple, p2: tuple) -> tuple:
    """Quadratic Bezier curve point at parameter t."""
    x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
    y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
    return (x, y)


async def _human_mouse_move(page: Any, target_x: float, target_y: float) -> None:
    """Move mouse to target via a bezier curve with human-like timing.

    Starts from a random point, curves to the target with a control point
    slightly off the direct path, adds a small overshoot + correction.
    This passes behavioral scoring that checks mouse trajectory entropy.
    """
    import random

    # Random start point (somewhere in the viewport)
    start_x = random.uniform(50, 800)
    start_y = random.uniform(50, 600)

    # Control point: off the direct path, creates a curve
    mid_x = (start_x + target_x) / 2
    mid_y = (start_y + target_y) / 2
    offset = random.uniform(-150, 150)
    ctrl_x = mid_x + offset
    ctrl_y = mid_y + offset

    # Move along the bezier curve with variable speed
    steps = random.randint(15, 30)
    for i in range(steps):
        t = (i + 1) / steps
        # Ease-in-out: slower at start and end
        t_eased = t * t * (3 - 2 * t)
        x, y = _bezier_point(t_eased, (start_x, start_y), (ctrl_x, ctrl_y), (target_x, target_y))
        await page.mouse.move(x, y)
        # Variable delay: faster in the middle, slower at edges
        delay = int(random.uniform(5, 25))
        await page.wait_for_timeout(delay)

    # Small overshoot + correction (mimics human hand wobble)
    overshoot = random.uniform(2, 8)
    await page.mouse.move(target_x + overshoot, target_y + overshoot * 0.5)
    await page.wait_for_timeout(random.randint(30, 80))
    await page.mouse.move(target_x, target_y)


async def _simulate_human_behavior(page: Any) -> None:
    """Simulate human-like behavior after page load, before content extraction.

    Lightweight (~1.5-2.5s total): randomized dwell, one bezier mouse move,
    one smooth scroll. Passes Cloudflare's v9 behavioral scoring that checks
    mouse path entropy, time-on-page, and scroll velocity.
    """
    import random

    # 1. Dwell time: 1-2.5s before any interaction
    await page.wait_for_timeout(random.randint(1000, 2500))

    # 2. Mouse movement: one bezier curve to a random point
    try:
        target_x = random.uniform(200, 1200)
        target_y = random.uniform(200, 800)
        await _human_mouse_move(page, target_x, target_y)
    except Exception:
        pass

    # 3. Smooth scroll: variable speed, slight pauses
    try:
        scroll_amount = random.randint(100, 400)
        await page.evaluate(f"window.scrollBy({{ top: {scroll_amount}, behavior: 'smooth' }})")
        await page.wait_for_timeout(random.randint(200, 500))
    except Exception:
        pass


# ─── Browser session base ─────────────────────────────────────────────────────

class BrowserSession:
    """Base browser session using patchright's async API.

    Manages a persistent browser context with page pooling. Supports:
    - Headless/headful mode
    - Proxy (static or per-request)
    - Resource blocking (disable_resources, blocked_domains)
    - Wait strategies (load, domcontentloaded, networkidle)
    - Page actions (callable executed after navigation)
    - Cookie injection
    - Extra headers
    - Cloudflare solving (stealthy only)

    Lifecycle:
        session = StealthyBrowser(headless=True)
        await session.start()
        response = await session.fetch("https://example.com")
        await session.close()

    Or as async context manager:
        async with StealthyBrowser() as session:
            response = await session.fetch("https://example.com")
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        wait: int = 0,
        proxy: Optional[str] = None,
        locale: Optional[str] = None,
        timezone_id: Optional[str] = None,
        timeout: int = 30000,
        cookies: Optional[List[Dict]] = None,
        useragent: Optional[str] = None,
        network_idle: bool = False,
        block_ads: bool = True,
        disable_resources: bool = False,
        extra_headers: Optional[Dict[str, str]] = None,
        google_search: bool = True,
        cdp_url: Optional[str] = None,
        real_chrome: bool = True,
        wait_selector: Optional[str] = None,
        wait_selector_state: str = "attached",
        max_pages: int = 1,
        retries: int = 1,
        retry_delay: float = 1.0,
        # Stealthy-specific
        hide_canvas: bool = False,
        block_webrtc: bool = False,
        allow_webgl: bool = True,
        solve_cloudflare: bool = False,
        additional_args: Optional[Dict] = None,
        page_action: Optional[Callable] = None,
        page_setup: Optional[Callable] = None,
        humanize: bool = True,
    ):
        self._headless = headless
        self._wait = wait
        self._proxy = proxy
        self._locale = locale
        self._timezone_id = timezone_id
        self._timeout = timeout
        self._cookies = cookies
        self._useragent = useragent
        self._network_idle = network_idle
        self._block_ads = block_ads
        self._disable_resources = disable_resources
        self._extra_headers = extra_headers
        self._google_search = google_search
        self._cdp_url = cdp_url
        self._real_chrome = real_chrome
        self._wait_selector = wait_selector
        self._wait_selector_state = wait_selector_state
        self._max_pages = max_pages
        self._retries = retries
        self._retry_delay = retry_delay
        self._hide_canvas = hide_canvas
        self._block_webrtc = block_webrtc
        self._allow_webgl = allow_webgl
        self._solve_cloudflare = solve_cloudflare
        self._additional_args = additional_args or {}
        self._page_action = page_action
        self._page_setup = page_setup
        self._humanize = humanize

        # State
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._user_data_dir: Optional[str] = None
        self._is_alive: bool = False
        self._fingerprint_profile: Optional[Dict[str, Any]] = None

    # ── Lifecycle ─────────────────────────────────────────────────

    async def __aenter__(self) -> "BrowserSession":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        """Launch the browser and create a context."""
        if self._is_alive:
            raise RuntimeError("Session already started")

        from patchright.async_api import async_playwright

        self._playwright = await async_playwright().start()

        # Build browser launch options
        browser_args = list(DEFAULT_ARGS)
        if self._is_stealthy:
            browser_args.extend(STEALTH_ARGS)
            if self._block_webrtc:
                browser_args.extend((
                    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--force-webrtc-ip-handling-policy",
                ))
            if not self._allow_webgl:
                browser_args.extend((
                    "--disable-webgl",
                    "--disable-webgl-image-chromium",
                    "--disable-webgl2",
                ))
            if self._hide_canvas:
                browser_args.append("--fingerprinting-canvas-image-data-noise")

        browser_options: Dict[str, Any] = {
            "args": browser_args,
            "ignore_default_args": list(HARMFUL_ARGS),
            "headless": self._headless,
            "channel": "chrome" if self._real_chrome else _detect_chrome_channel(),
        }

        # Build context options
        context_options: Dict[str, Any] = {
            "color_scheme": "dark",
        }
        if self._is_stealthy:
            context_options.update({
                "is_mobile": False,
                "has_touch": False,
                "service_workers": "allow",
                "ignore_https_errors": True,
                "screen": {"width": 1920, "height": 1080},
                "viewport": {"width": 1920, "height": 1080},
                "permissions": ["geolocation", "notifications"],
            })
        if self._proxy:
            context_options["proxy"] = _construct_proxy_dict(self._proxy)
        if self._locale:
            context_options["locale"] = self._locale
        if self._timezone_id:
            context_options["timezone_id"] = self._timezone_id
        if self._extra_headers:
            context_options["extra_http_headers"] = self._extra_headers
        if self._useragent:
            context_options["user_agent"] = self._useragent
        elif self._headless:
            # In headless mode, Chrome reports 'HeadlessChrome' in the UA.
            # Fix: get the real Chrome version and construct a UA without 'Headless'.
            if browser_options["channel"] == "chrome":
                ua = _get_chrome_ua()
                if ua:
                    context_options["user_agent"] = ua
            else:
                # Bundled Chromium: use browserforge for a realistic UA
                try:
                    from browserforge.headers import HeaderGenerator
                    hg = HeaderGenerator()
                    headers = hg.generate()
                    ua = headers.get("User-Agent") or headers.get("user-agent")
                    if ua:
                        context_options["user_agent"] = ua
                except Exception:
                    pass

        # Merge additional args (highest priority)
        context_options.update(self._additional_args)

        try:
            if self._cdp_url:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    endpoint_url=self._cdp_url
                )
                self._context = await self._browser.new_context(**context_options)
            else:
                # Use persistent context (temp dir)
                self._user_data_dir = tempfile.mkdtemp(prefix="hound_browser_")
                persistent_opts = {**browser_options, **context_options, "user_data_dir": self._user_data_dir}
                self._context = await self._playwright.chromium.launch_persistent_context(
                    **persistent_opts
                )

            # Initialize context
            if self._cookies:
                await self._context.add_cookies(self._cookies)

            # Generate stealth init script (stealthy only) for JS-layer patches
            # that patchright doesn't handle. Injected per-page via CDP
            # Page.addScriptToEvaluateOnNewDocument (not context.add_init_script,
            # which uses Routes and breaks DNS resolution with patchright).
            self._init_script: Optional[str] = None
            if self._is_stealthy:
                self._fingerprint_profile = _generate_fingerprint_profile()
                # Full patches only for bundled Chromium; system Chrome already
                # has correct WebGL, plugins, platform, window.chrome.
                is_chrome_channel = browser_options["channel"] == "chrome"
                self._init_script = _build_stealth_init_script(
                    self._fingerprint_profile, full=not is_chrome_channel
                )
                logger.info("Stealth init script built (profile: %s, full: %s)",
                            self._fingerprint_profile.get("platform", "?"),
                            not is_chrome_channel)

            self._is_alive = True
            logger.info(f"Browser session started (stealthy={self._is_stealthy})")
        except Exception as e:
            logger.error(f"Browser session start failed: {e}")
            await self._cleanup_playwright()
            raise

    async def close(self) -> None:
        """Close the browser and cleanup."""
        if not self._is_alive:
            return

        try:
            if self._context:
                await self._context.close()
                self._context = None
            if self._browser:
                await self._browser.close()
                self._browser = None
        except Exception as e:
            logger.debug(f"Error closing browser: {e}")
        finally:
            await self._cleanup_playwright()
            self._is_alive = False

            # Cleanup temp dir
            if self._user_data_dir:
                try:
                    import shutil
                    shutil.rmtree(self._user_data_dir, ignore_errors=True)
                except Exception:
                    pass
                self._user_data_dir = None

    async def _cleanup_playwright(self) -> None:
        """Stop the playwright instance."""
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    # ── Fetch ─────────────────────────────────────────────────────

    async def fetch(
        self,
        url: str,
        *,
        wait: Optional[int] = None,
        timeout: Optional[int] = None,
        google_search: Optional[bool] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        disable_resources: Optional[bool] = None,
        wait_selector: Optional[str] = None,
        wait_selector_state: Optional[str] = None,
        network_idle: Optional[bool] = None,
        proxy: Optional[str] = None,
        solve_cloudflare: Optional[bool] = None,
        page_action: Optional[Callable] = None,
        page_setup: Optional[Callable] = None,
        retries: Optional[int] = None,
        **kwargs: Any,
    ) -> "Any":
        """Navigate to a URL and return a Response object.

        Parameters override session defaults for this request only.
        """
        if not self._is_alive:
            raise RuntimeError("Session not started")

        # Resolve per-request overrides
        actual_wait = wait if wait is not None else self._wait
        actual_timeout = timeout if timeout is not None else self._timeout
        actual_google = google_search if google_search is not None else self._google_search
        actual_extra_headers = extra_headers if extra_headers is not None else self._extra_headers
        actual_disable_resources = disable_resources if disable_resources is not None else self._disable_resources
        actual_wait_selector = wait_selector if wait_selector is not None else self._wait_selector
        actual_wait_selector_state = wait_selector_state or self._wait_selector_state
        actual_network_idle = network_idle if network_idle is not None else self._network_idle
        actual_solve_cf = solve_cloudflare if solve_cloudflare is not None else self._solve_cloudflare
        actual_page_action = page_action or self._page_action
        actual_page_setup = page_setup or self._page_setup
        actual_retries = retries if retries is not None else self._retries

        # Build referer
        request_headers = {}
        if actual_extra_headers:
            request_headers = {h.lower(): v for h, v in actual_extra_headers.items()}
        referer = None
        if actual_google and "referer" not in request_headers:
            referer = "https://www.google.com/"

        for attempt in range(actual_retries):
            try:
                page = await self._context.new_page()
                page.set_default_navigation_timeout(actual_timeout)
                page.set_default_timeout(actual_timeout)

                # Stealth init script injection mechanism:
                # CDP Page.addScriptToEvaluateOnNewDocument does NOT work with
                # patchright (it patches Runtime.enable). add_init_script uses
                # Routes which breaks DNS. Instead, we use wait_until='commit'
                # + page.evaluate() to inject patches before page JS runs.
                stealth_injected = False

                if actual_extra_headers:
                    await page.set_extra_http_headers(actual_extra_headers)

                # Route handler for resource blocking
                if actual_disable_resources:
                    await page.route("**/*", _create_route_handler(True, None))

                # Response capture
                final_response: List[Any] = [None]
                async def _handle_response(resp: Any) -> None:
                    try:
                        if (resp.request.resource_type == "document"
                                and resp.request.is_navigation_request()
                                and resp.request.frame == page.main_frame):
                            final_response[0] = resp
                    except Exception:
                        pass
                page.on("response", _handle_response)

                # Page setup callback
                if actual_page_setup:
                    try:
                        await actual_page_setup(page)
                    except Exception as e:
                        logger.warning(f"page_setup callback error: {e}")

                # Navigate - use 'commit' to get control as soon as HTML is
                # received but BEFORE page JS runs. Then immediately inject
                # stealth patches via page.evaluate(). This is the only
                # injection mechanism that works with patchright (CDP
                # Page.addScriptToEvaluateOnNewDocument requires Runtime.enable
                # which patchright patches out; add_init_script uses Routes
                # which breaks DNS).
                try:
                    first_response = await page.goto(url, referer=referer, wait_until="commit")
                except Exception:
                    # 'commit' may not be supported in older versions, fall back
                    first_response = await page.goto(url, referer=referer)

                # Inject stealth patches IMMEDIATELY after commit, before
                # the page's own scripts execute.
                if self._init_script and first_response:
                    try:
                        await page.evaluate(self._init_script)
                        stealth_injected = True
                    except Exception as e:
                        logger.debug(f"Stealth script injection error: {e}")

                # Wait for page stability (full load)
                await self._wait_for_stability(page, actual_network_idle)

                if not first_response:
                    raise RuntimeError(f"Failed to get response for {url}")

                # Solve Cloudflare if requested
                if actual_solve_cf:
                    await _solve_cloudflare(page)
                    await self._wait_for_stability(page, actual_network_idle)

                # Page action callback
                if actual_page_action:
                    try:
                        result = actual_page_action(page)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        logger.warning(f"page_action callback error: {e}")

                # Wait for selector
                if actual_wait_selector:
                    try:
                        waiter = page.locator(actual_wait_selector)
                        await waiter.first.wait_for(state=actual_wait_selector_state)
                        await self._wait_for_stability(page, actual_network_idle)
                    except Exception as e:
                        logger.warning(f"Wait for selector '{actual_wait_selector}' failed: {e}")

                # Post-load wait
                if actual_wait > 0:
                    await page.wait_for_timeout(actual_wait)

                # Human behavior simulation (stealthy only, if enabled)
                # Adds ~1.5-2.5s of mouse movement + scroll + dwell.
                # Passes Cloudflare v9 behavioral scoring.
                if self._is_stealthy and self._humanize:
                    try:
                        await _simulate_human_behavior(page)
                    except Exception as e:
                        logger.debug(f"Human behavior simulation error: {e}")

                # Build response
                from master_fetch.fetcher import response_from_browser_page
                response = await response_from_browser_page(
                    page, first_response, final_response[0]
                )

                # Memory cleanup: trigger Chrome's internal GC + cache drop
                # via CDP Memory.simulatePressureNotification. This releases
                # V8 heap, image caches, and discardable memory in all
                # processes. Lightweight (~5ms), prevents RAM creep across
                # many sequential fetches. Non-disruptive: "moderate" level
                # does not crash tabs or affect page content.
                try:
                    cdp = await page.context.new_cdp_session(page)
                    await cdp.send("Memory.simulatePressureNotification",
                                   {"level": "moderate"})
                    await cdp.detach()
                except Exception:
                    pass

                await page.close()
                return response

            except Exception as e:
                try:
                    await page.close()
                except Exception:
                    pass
                if attempt < actual_retries - 1:
                    logger.warning(
                        f"Browser fetch attempt {attempt + 1} failed for {url}: {str(e)[:200]}. "
                        f"Retrying in {self._retry_delay}s..."
                    )
                    await asyncio.sleep(self._retry_delay)
                else:
                    raise

        raise RuntimeError(f"Browser fetch failed for {url}")

    async def _wait_for_stability(self, page: Any, network_idle: bool) -> None:
        """Wait for the page to reach a stable state."""
        try:
            await page.wait_for_load_state("load")
        except Exception:
            pass
        try:
            await page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        if network_idle:
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

    # ── Properties ────────────────────────────────────────────────

    @property
    def _is_stealthy(self) -> bool:
        """Override in subclasses."""
        return False

    @property
    def is_alive(self) -> bool:
        return self._is_alive


class StealthyBrowser(BrowserSession):
    """Anti-detect stealthy browser session with Cloudflare solving.

    Uses stealth browser args (anti-fingerprinting, automation bypass),
    a realistic user agent, and optionally solves Cloudflare challenges.
    """

    @property
    def _is_stealthy(self) -> bool:
        return True


class DynamicBrowser(BrowserSession):
    """Standard JS-rendering browser session.

    Simpler than stealthy: no stealth args, no Cloudflare solver.
    Used for pages that need JavaScript rendering but don't have
    anti-bot protection.
    """

    @property
    def _is_stealthy(self) -> bool:
        return False


# ─── Browser availability check ───────────────────────────────────────────────

_browser_available: Optional[bool] = None
_browser_import_error: Optional[str] = None


def check_browser_available() -> bool:
    """Check if browser deps (patchright) are importable. Cached."""
    global _browser_available, _browser_import_error
    if _browser_available is not None:
        return _browser_available
    try:
        import patchright  # noqa: F401
        _browser_available = True
        _browser_import_error = None
        return True
    except ImportError as e:
        _browser_available = False
        _browser_import_error = str(e)
        return False


def browser_import_error() -> Optional[str]:
    """Get the import error if browser deps are unavailable."""
    return _browser_import_error


def is_browser_available_cached() -> Optional[bool]:
    """Read the cached browser availability WITHOUT triggering an import.

    Returns True/False if check_browser_available() has been called at least
    once (by the prewarm thread). Returns None if not yet checked.

    This is safe to call on the asyncio event loop: it never blocks.
    Use check_browser_available() for the first check (from a worker thread).
    """
    return _browser_available

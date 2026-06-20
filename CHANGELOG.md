# Changelog

## [4.0.3] - 2026-06-20

### Added
- **Brand visuals in the README.** Added a hero banner, a square logo mark, and an editorial "retrieving the web" illustration under `docs/`. Images are referenced via absolute `raw.githubusercontent.com` URLs so they render on both GitHub and the PyPI project page (PyPI cannot resolve relative repo paths). No code changes.

### Fixed
- Removed a stray em-dash in the `smart_search` feature copy.

## [4.0.2] - 2026-06-20

### Fixed
- **`hound -u` ghost-prompt overlap on Windows.** After the parent `hound.exe` exited and PowerShell reclaimed the console (printing its `PS C:\Users\...>` prompt), the detached console updater woke from its 2s wait and printed its first line (`Running pip...`) on top of the prompt, producing overlapping "ghost" text. The updater child now emits a leading newline (`chr(10)`) right after the wait, so its output starts on a fresh line below the prompt instead of overlapping it.

### Notes
- No API changes. Test added: the generated updater child source must contain the leading-newline write.

## [4.0.1] - 2026-06-20

### Fixed
- **`pdfplumber` was missing from the `[all]` extra**, so `pip install hound-mcp[all]` did NOT install it and PDF extraction was broken for real users (it raised "PDF extraction requires pdfplumber. Run: pip install hound-mcp[all]" even after installing `[all]`). The local dev venv had pdfplumber installed manually, which masked the missing declaration; CI's clean environment caught it. `pdfplumber>=0.11.0` is now declared in `[all]`.
- CI workflow now installs `.[dev,all]` (was `.[dev]`) so the PDF test suite actually exercises the flagship feature instead of failing on the missing optional dependency.

### Notes
- No code changes. Re-publishing solely to fix the dependency declaration so `hound-mcp[all]` delivers PDF support as documented.

## [4.0.0] - 2026-06-20

The agent-effectiveness release. Hound now masters itself the moment it connects, reads PDFs, and tells the agent exactly what to do next. **Breaking:** the manual `open_session` / `close_session` / `list_sessions` MCP tools are removed (8 tools → 5); a single warm browser is managed automatically.

### Added — flagship: PDF extraction
- **`smart_fetch` now extracts PDFs to structured, agent-optimized markdown.** PDFs are detected by content-type or `%PDF` magic bytes and routed to a new `pdf_extractor` built on `pdfplumber` (MIT, no AGPL): multi-column reading order, real tables as markdown tables, font-size heading detection, de-hyphenated paragraphs, a metadata header (title/author/date/subject), and `--- Page N ---` markers for citation.
- **`pages` param** (`"1-5"`, `"1,3,5-7"`) extracts a PDF subset to save tokens/time on big PDFs. **`password`** for encrypted PDFs. Both flow to the extractor via contextvars (task-local, bulk-safe); the cache key includes `pages` so subsets don't collide with full-PDF entries.
- Honest PDF signals: scanned/image-only PDFs return `content_ok=false` + a "needs OCR" `next_action`; encrypted PDFs report and accept a password; not-a-PDF/empty/corrupt are reported honestly.
- `pdfplumber` added to `[all]` (lean install unaffected).

### Added — connect-time mastery + actionable responses
- **MCP `instructions` at `initialize`.** A concise orientation (~365 tokens, paid once at handshake) gives the agent the 3-tool mental model, the #1 search→fetch workflow, and the known limits.
- **Agent-facing response fields on every fetch:** `summary` (one-line status), `content_ok` (trust content only if true), `next_action` (the obvious next call: paginate / bypass robots / switch sources), `fetched_at` (ISO-8601 UTC).
- **`smart_search` `fetch_relevance`** (high/med/low) per result + a `fetch_hint` so the agent fetches 1-2 results instead of all 10.
- **Promoted `css_selector`, `max_content_chars`, `timeout`** to first-class `smart_fetch` params (were buried in the `options` bag). `max_content_chars` is a token-spend control. Units + defaults on every param description.

### Changed — single warm browser instance
- **Removed `open_session` / `close_session` / `list_sessions` MCP tools** (and the `list_sessions` method). `open_session`/`close_session` stay as internal helpers. Tool count 8 → 5.
- **Eager warm-up at server startup** (was: pre-warm on first `smart_search`). The single stealthy Chrome launches when the harness starts the server, in parallel with the handshake.
- **No second browser ever spawns**: a new `_auto_session_lock` serializes auto-session creation. Keep-alive-forever; graceful shutdown closes the browser when the harness closes.
- `screenshot` now auto-manages a stealthy session (`session_id` optional); description clarifies it's for multimodal agents.

### Changed — Reddit optimization hardened
- Reddit URLs now **skip HTTP and go straight to stealthy** (www.reddit.com walls HTTP; saves ~1s). The old.reddit.com rewrite runs before `force_fetcher` so a pinned `http` also benefits.
- **Listing parser rewritten** to read per-post `data-*` attributes (was: span-scraping that misaligned scores/comment counts on real HTML, e.g. reported score 27 for a post whose real `data-score` was 28). Thing-block detection fixed for real HTML (`class=" thing"` has a leading space). HTML entities unescaped; promoted ads skipped; sticky/NSFW tagged; `1 comment` singular grammar; per-block span fallback for user-profile pages.
- `rewrite_to_old_reddit` now rejects lookalikes (`notreddit.com`) standalone.

### Fixed — caching + JS-shell detection
- **Bad content is no longer cached.** `_finalize_result` previously cached JS shells / bot challenges / error statuses for the whole TTL, and the cache-hit path didn't restore `error`, so `content_ok` came back `true` and the agent trusted broken cached pages. New `_is_cacheable`: cache only clean, non-blank, <400 content.
- **Cache size cap + oldest eviction** (`MAX_CACHE_ENTRIES = 10000`) so a long-lived agent's DB can't grow unbounded.
- **Search cache key includes `max_results`** (was: a 5-result and 10-result search collided on one cached entry).
- **JS-shell detection catches SPAs whose shell text doesn't match known phrases** (e.g. `quotes.toscrape.com/js` returned a 29-char nav shell over HTTP with no escalation). New heuristic: HTTP tier + status 200 + large body + <200 chars text → JS shell → escalate to stealthy. Scoped to the HTTP tier so stealthy-rendered low-text pages (image galleries, canvas) don't false-positive.

### Notes
- No new public API beyond the additions above. Reddit/stealthy-default/PDF/caching are transparent to existing callers.
- Full suite: 442 tests pass (5 e2e deselected). New test files: `tests/test_agent_qol.py`, `tests/test_cache_qol.py`, `tests/test_pdf_extractor.py`, `tests/test_reddit_routing.py`; real PDF fixtures `tests/background_checks.pdf` + `tests/dummy.pdf`; real Reddit HTML fixture `tests/old_reddit_real.html`.
- README overhauled: 5-tool table, feature deep-dives, free-fetch comparison (Crawl4AI / Jina Reader / Firecrawl OSS / DIY), honest limits, ~1.3K token claim.

## [3.6.7] - 2026-06-18

### Fixed
- **`hound -u` finally just works on Windows.** The root cause of every prior failed attempt: the running `hound -u` command IS `hound.exe` (a console-scripts launcher that spawns python.exe as a grandchild), so pip can't overwrite the launcher while it runs — and every attempt to "detect the running process and refuse" flagged the command's OWN launcher (its PID is a grandparent of the python process, unreachable via `os.getppid()`), making `hound -u` refuse to run on itself forever.

  The fix: `hound -u` now spawns a **detached console updater** — a child `python.exe` (NOT hound.exe) that inherits the same console window, waits ~2s for the `hound.exe` launcher to exit, then runs pip. With the launcher gone, `hound.exe` is free and pip replaces it cleanly. The child prints pip progress and the result to the same window, so the user sees everything. The child re-checks for a REAL hound MCP server only AFTER the launcher exits (so the current command's own launcher is never mistaken for a server — the false-positive that made `hound -u` refuse on itself).

  Verified end-to-end on Windows: `hound -u` (3.6.7 -> 3.6.8) replaced `hound.exe` with no manual kill, no lock error, full pip output visible, `hound -v` confirmed the new version.

- **macOS/Linux:** no file lock, so pip runs synchronously. If a hound MCP server is running, `hound -u` warns that it will keep old code until restarted (but still proceeds — pip works on POSIX). No false refusal.

- Removed the broken upfront running-server refusal (it false-positived on the command's own launcher) and the harmful detached-fallback from 3.6.3 (which created metadata/binary mismatches). The new console updater is the primary mechanism on Windows.

### Notes
- No new features. No public API changes. 291 tests pass (rewrote the self-update test suite for the new detached-updater design: helper tests, `_spawn_console_updater` source-compile + self-contained-ness tests, `_run_pip_sync` bulletproof-message tests, `_do_update` dispatch tests for Windows/POSIX, `hound -v` tests).
- **Recovery for users on an older binary** (whose `hound -u` is one of the buggy 3.6.2-3.6.6 ones): run pip directly once, after stopping any running hound MCP server:
  ```bash
  taskkill /IM hound.exe /F            # Windows  (POSIX: pkill -f hound)
  pip install --force-reinstall --no-deps hound-mcp==3.6.7
  ```
  After that, `hound -u` works normally on every platform.

## [3.6.6] - 2026-06-18

### Fixed
- **Bulletproof, platform-aware error messages on every `hound -u` / `hound -v` failure path.** No more silent no-ops or dead-ends. Every path now tells the user exactly what to do, with the correct command for their OS.
- **Silent no-op detection (the bug that stranded users).** Previously, when pip returned 0 but `hound.exe` couldn't be replaced (a running hound MCP server holds it), `hound -u` just printed `Hound v<old>` after `Updating to v<new>...` — looking like success while nothing changed. Now `_do_update` re-reads the version after pip and, if it didn't advance, prints: "The upgrade to vX did not complete. hound.exe could not be replaced (a running hound MCP server likely holds it). Stop it: <platform stop cmd>, then re-run `hound -u`, or recover manually: pip install --force-reinstall --no-deps hound-mcp==X".
- **Platform-aware recovery commands** via two helpers: `_stop_hound_cmd()` (`taskkill /IM hound.exe /F` on Windows, `pkill -f hound` on macOS/Linux) and `_reinstall_cmd(ver)` (`pip install --force-reinstall --no-deps hound-mcp==ver`, same everywhere). Every failure path (running server, file lock, pip error, timeout, silent no-op, corrupted metadata) prints both the stop command and the reinstall command.
- **New failure paths covered with messages:**
  - PyPI unreachable (`hound -v` and `hound -u`): "couldn't reach PyPI to check for updates" + manual upgrade command.
  - pip timeout: "update timed out (pip took too long)" + reinstall command.
  - Corrupted result after update (metadata wiped): "package metadata is missing after the update" + reinstall command.
  - `hound -v` corrupted install now prints the exact reinstall command for the latest known version.

### Notes
- No new features. No public API changes. 290 tests pass (7 new bulletproof-message tests: silent no-op, PyPI unreachable for -u and -v, pip timeout, platform-aware stop command, reinstall-cmd format, corrupted-install reinstall cmd).
- **Recovery for users already stuck on an older binary** (whose `hound -u` is the buggy one): run pip directly once, after stopping any running hound MCP server:
  ```bash
  taskkill /IM hound.exe /F            # Windows  (POSIX: pkill -f hound)
  pip install --force-reinstall --no-deps hound-mcp==3.6.6
  ```
  After that, `hound -u` gives honest, actionable messages on every failure.

## [3.6.5] - 2026-06-18

### Fixed
- **`hound -u` no longer creates a metadata/binary mismatch when a hound MCP server is running.** The 3.6.3 detached-background-updater fallback was actively harmful in the common case where hound runs as a long-lived MCP server: that server process holds `hound.exe` against replacement, so the detached updater's pip run installed the new package *metadata* but could NOT replace `hound.exe` (WinError 32 on `hound.exe -> hound.exe.deleteme`), leaving the install in a broken state where `hound -v` reported the new version but the binary was still the old one. Removed the detached fallback entirely.
- **`hound -u` now refuses to update while another hound process is running.** `_other_hound_pids()` (cross-platform: `tasklist` on Windows, `ps` on macOS/Linux) detects other running hound launchers BEFORE pip is invoked. If any are found, `hound -u` prints their PIDs and the exact stop command (`taskkill /IM hound.exe /F` / `pkill -f hound`), then exits without touching pip — so a half-update is now impossible. This is the only reliable fix: a running hound MCP server holds the launcher, and no self-update trick can replace a file another process has locked.
- **Honest pip-failure message.** If the running-process check misses something and pip still hits the file lock, `hound -u` now prints "hound.exe is locked by another process. Stop any running hound MCP server, then re-run: hound -u" instead of promising a background updater that would silently fail.

### Notes
- No new features. No public API changes. 283 tests pass (replaced the detached-fallback tests with running-server-detection tests + abort-behavior tests).
- **Recovery for a machine already in the mismatch state** (metadata newer than the binary, e.g. `hound -v` says 3.6.4 but `hound.exe` is older):
  ```bash
  taskkill /IM hound.exe /F            # Windows: stop the running hound MCP server
  # (POSIX: pkill -f hound)
  pip install --force-reinstall --no-deps hound-mcp==3.6.5
  ```
  The MCP client will respawn hound (now the new binary) on next use.

## [3.6.4] - 2026-06-17

### Fixed
- **Self-diagnosis for a corrupted install.** If a previous `hound -u` was interrupted mid-update (WinError 32 before the fix), pip could delete the `hound-mcp` package metadata without replacing `hound.exe`, leaving the launcher orphaned. `importlib.metadata.version("hound-mcp")` then raises `PackageNotFoundError`, so `hound -v` printed the useless `Hound vunknown`. Now:
  - `hound -v` detects the missing-metadata case and prints a clear recovery message naming the **real package** (`hound-mcp`) and the exact reinstall command, plus an explicit warning to install `hound-mcp` and **not** the unrelated `hound` PyPI package ("A FireCloud database extension", v1.0.1) that shadows our `hound` console script in search results.
  - `hound -u` (on 3.6.3+ binaries) self-heals: when metadata is missing it prints "reinstalling to recover" and proceeds with `pip install --upgrade`, which restores both the metadata and the launcher.

### Notes
- No new features. No public API changes. 3 new tests cover the corrupted-install message content, the `hound -v` diagnosis path, and the `hound -u` self-heal path.
- **Recovery for users already in the corrupted state on an old (<=3.6.1) binary** (whose `hound -u` is still the broken one): run pip directly once:
  ```bash
  pip install --force-reinstall --no-deps hound-mcp==3.6.4
  ```
  If you accidentally installed the wrong `hound` package, remove it first: `pip uninstall hound -y`.

## [3.6.3] - 2026-06-17

### Fixed
- **`hound -u` self-update hardened cross-platform with a bulletproof fallback.** The 3.6.2 fix (rename the running `hound.exe` aside so pip can replace it) is now wrapped in a two-layer updater that guarantees no user ever hits `WinError 32`:
  - **Layer 1 — launcher staging (Windows):** `_stage_running_launcher()` renames `hound.exe` → `hound.exe.old` before pip runs (Windows allows renaming a running .exe even though it forbids overwriting it). pip then writes a fresh `hound.exe`. The `.old` is swept on the next launch by `_cleanup_old_launcher()`.
  - **Layer 2 — detached fallback (Windows):** if staging fails (read-only install, unusual layout) AND pip still hits the file lock, `_spawn_detached_updater()` spawns a background child that waits for the current process to exit (releasing the lock) and then runs pip, logging the outcome to `~/.master_fetch_cache/hound_updater.log`.
  - **macOS/Linux:** no file lock exists, so staging is skipped entirely and pip runs synchronously. None of the Windows `.exe` logic is touched on POSIX.
- **Every pip failure now prints the manual recovery command** (`python -m pip install --upgrade hound-mcp[all]`), so a user is never left without a path forward.
- **Detached-updater generated script bug:** the child one-liner double-braced the `{r.returncode}` placeholder, which would have emitted a literal string instead of the pip result. Fixed and covered by a compile-check test so it can't regress silently.

### Notes
- No new features. No public API changes. The codebase was audited end-to-end for platform-specific code: the only platform-conditional logic in the entire package is this updater section, all guarded by `sys.platform == "win32"`. Everything else (`cache`, `robots`, `security`, `server`, `trafilatura_extractor`, `reddit`, `search`) uses `Path.home()`, stdlib, and cross-platform deps (scrapling, aiosqlite, trafilatura, mcp, pydantic) — fully native on macOS/Linux/Windows.
- Verified end-to-end on Windows: staging moves the real `hound.exe` aside and real `pip` writes a fresh one (sha changed, returncode 0).
- To get onto 3.6.3 from <=3.6.1 (broken updater), run pip directly once: `python -m pip install --upgrade hound-mcp[all]`. From 3.6.2+, `hound -u` works normally.

## [3.6.2] - 2026-06-17

### Fixed
- **`hound -u` self-update failed on Windows with WinError 32**: `_do_update` ran `pip install --upgrade` *inside* the running `hound.exe` process, so Windows locked `hound.exe` against the very overwrite pip was attempting (`The process cannot access the file because it is being used by another process`). The fix stages the running launcher aside first: `hound.exe` is renamed to `hound.exe.old` before pip runs (Windows permits renaming a running .exe even though it forbids overwriting it), so pip can write a fresh `hound.exe`. The `.old` is swept on the next `hound` launch by `_cleanup_old_launcher()`. Non-Windows is unaffected (no file lock). If staging fails for any reason, the code falls through to the old behavior — no worse than before.

### Notes
- This is a Windows-only fix to the updater. **To get onto 3.6.2 from a version with the broken updater (<=3.6.1), run pip directly once** (not via `hound -u`), since `python.exe` running pip does not lock `hound.exe`:
  ```powershell
  python -m pip install --upgrade hound-mcp[all]
  ```
  After that, `hound -u` works normally for future updates.

## [3.6.1] - 2026-06-17

### Fixed
- **robots.txt scrapling fetch path was silently broken**: `_fetch_robots_txt` wrapped an async `sess.get()` coroutine in `asyncio.to_thread` and unpacked the result as a `(response, elapsed)` tuple. The coroutine was never awaited and the unpack always raised, so every robots.txt lookup fell through to the plain-urllib fallback — defeating the browser-impersonated fetch path entirely. Now awaits `sess.get()` directly and reads `response.body`. Impersonated requests reach sites that block stdlib urllib.
- **`smart_fetch` bulk mode silently truncated URLs past `MAX_BULK_URLS`**: `_smart_fetch_bulk` dropped overflow URLs with `urls[:MAX_BULK_URLS]` and no warning — silent data loss. Now raises `ValueError` matching the single-URL and `bulk_get`/`bulk_fetch`/`bulk_stealthy_fetch` behavior.
- **`force_fetcher="http"` ignored the caller's `timeout`**: the HTTP branch hardcoded `timeout=30` seconds. A caller asking for a 5s budget got 30s. Now passes `timeout=max(1, min(int(timeout/1000), 30))`. The auto-escalation HTTP tier now also honors the caller timeout instead of always using the 30s default.
- **`validate_proxy` rejected `socks5`/`socks5h` dict proxies**: the dict path validated the `server` URL with `validate_url`, which only permits `http`/`https`, so `proxy={"server": "socks5://host:1080"}` was rejected even though the string form `socks5://host:1080` was accepted. Now uses the same `http/https/socks5/socks5h` scheme set as the string path (and still allows internal/local proxy hosts).
- **`_safe_cookie_dict` leaked cookie values into logs**: the "missing name" warning logged the whole cookie dict, which may contain a sensitive `value`. Now logs a fixed message without the dict.
- **`_http_with_retry` retried deterministic validation errors**: `SecurityError`/`ValueError` (bad URL, oversized response body, blocked scheme) were retried 3x with exponential backoff — re-running the same deterministic failure (and, for oversized bodies, re-downloading them). Now surfaces validation errors immediately and retries only transport/network failures.

### Removed (dead code)
- **`domain_intel.py`** (207 lines): per-domain protection-level tracking was orphaned when v3.5.1 removed domain-intel routing from `smart_fetch`. No production code imported it; the `server.py` comment claiming it was "imported on demand for list_sessions stats only" was false. Removed the module, `tests/test_domain_intel.py`, and the domain-intel test classes/methods in `test_reliability_v3.py`.
- **`_close_auto_dynamic_session`** + its two `asyncio.create_task` call sites in `_force_fetch`/`_auto_escalate`: the auto dynamic session is never created in production (smart_fetch only uses the stealthy auto session), so this always no-op'd. Removed the method and the no-op task spawns.
- **`_stealthy_auto_alive`** and **`_acquire_stealthy_session`**: defined but never called anywhere (production or tests). Removed.
- **`reddit.enhance_old_reddit_extraction`**: stub that returned its input unchanged; never called. Removed.

### Performance
- **Idle monitor no longer started in keep-alive-forever mode**: with `AUTO_SESSION_IDLE_TIMEOUT = 0` (the default), `_ensure_idle_monitor` now no-ops instead of spawning a background task that wakes every 60s only to `continue`.

### Notes
- No new features. No public API changes. `open_session(session_type="dynamic")` and the `fetch`/`bulk_fetch` dynamic methods remain supported public API; only the dead auto-routing scaffolding was removed.
- Test count unchanged in spirit: removed 8 dead domain-intel tests, added focused regression tests for the robots/bulk/proxy fixes.

## [3.6.0] - 2026-06-16

### Added
- **Reddit optimization**: Subreddit listing URLs auto-rewritten to old.reddit.com for 2x faster fetching. old.reddit.com serves the same content but with 7x smaller page size (134KB vs 1MB), reducing fetch times from 12-15s to 5-10s.
- **Structured Reddit extraction**: Custom parser for old.reddit.com listings extracts post titles, scores, comment counts, authors, and URLs as clean numbered markdown. Agents get structured data instead of raw text dump.
- **27 new tests**: URL detection (all Reddit domain variants), URL rewriting (www/m/np → old, skips /comments/), structured parser (titles, scores, comments, authors, URLs, 25-post limit).

### Changed
- **smart_fetch**: Reddit subreddit URLs (www.reddit.com, m.reddit.com, np.reddit.com) transparently rewritten to old.reddit.com before fetching. Individual post pages (/comments/...) are NOT rewritten to preserve full comment threads.

### Performance
- Reddit subreddit fetch: 5-10s (was 12-15s) — 2x faster
- Reddit page size: 134KB (was 1MB) — 7x smaller
- Reddit extraction: 5,000+ chars structured (was 1,500 chars unstructured)
- Reddit post pages: unchanged (preserves full comments)
- Cached Reddit fetches: 21ms (unchanged)
- Non-Reddit URLs: no change

## [3.5.3] - 2026-06-13

### Fixed
- **Cache schema upgrade**: `content_type` and `total_size_bytes` now persist through the SQLite cache (previously returned as empty string / 0 on cache hits, even though the live fetch populated them). `_ensure_db()` runs an idempotent `ALTER TABLE ADD COLUMN` migration on first access so users upgrading from 3.5.2 do not lose any cached entries — old rows get the new columns defaulted to `''` and `0`. Migration is safe to re-run; a `try/except` on `duplicate column name` makes it idempotent.

### Notes
- No behavior change for the live fetch path (HTTP/stealthy already populated these fields correctly). Cache hits and `cache_clear` descriptions are unchanged from a user-experience standpoint. New columns carry the existing `ResponseModel.content_type` / `ResponseModel.total_size_bytes` fields exactly.
- Backwards-compatible: `set_cached(...)` accepts `content_type=""`, `total_size_bytes=0` defaults; old callers that omit the new kwargs keep working unchanged.

## [3.5.2] - 2026-06-13

### Fixed
- **`force_fetcher` schema enum aligned with runtime**: removed `"dynamic"` from the `mcp_smart_fetch` input schema. The 3-tier path (HTTP -> dynamic -> stealthy) was dropped in v3.5.0; the schema was stale and accepted `force_fetcher="dynamic"` even though it silently rerouted to stealthy. Now only `http` and `stealthy` are valid for pinning the tier. `force_fetcher="stealthy"` and `force_fetcher="http"` continue to work as they always have.
- **Schema/escalation string docs match runtime**: `mcp_smart_fetch` description rewritten to say "http -> stealthy (2 tiers)" instead of the stale "HTTP -> browser -> stealth". `escalation_path` field description updated to reflect the new path. The dynamic tier is still accepted by `mcp_open_session(session_type="dynamic")` for backward compatibility with manual session creation; only the auto-routing removed it.

### Notes
- No behavior change. Pure schema/docstring cleanup. Tests already use only the stealthy tier via auto-routing.

## [3.5.1] - 2026-06-08

### Added
- **Pre-warm on smart_search**: Browser launches in background when the agent calls smart_search (always the first call). By the time smart_fetch runs, the browser is warm. No race condition — search is API-only, doesn't touch the browser.
- **Browser stays alive**: Idle timeout set to 0 (keep forever). Browser sits at idle consuming minimal RAM, wakes instantly when stealthy fetch needs it, returns to idle after. No cold starts after the first search.

## [3.5.0] - 2026-06-08

### Changed
- **Ripped out Phase A/B/C domain intel routing**: No more "high", "low", "none" domain levels deciding which fetcher to use. The algorithm is now dead simple: try HTTP first. If it fails, use stealthy. That's it. Every URL gets the same treatment. HTTP is fast (~1s), stealthy handles everything else. No more stale domain intel forcing sites through slow browser paths when HTTP would work fine.
- **Removed dynamic (Playwright) tier entirely**: One browser engine (Patchright/stealthy). No second Chrome instance possible. `force_fetcher="dynamic"` now routes to stealthy — Patchright handles everything Playwright does.

### Added
- **Post-HTTP pre-warming**: After a successful HTTP fetch, the stealthy browser starts in the background. No race condition — the current call is already done. The next call that needs a browser finds it warm and ready. Zero cold-start on second fetch.

## [3.4.1] - 2026-06-08

### Fixed
- **Removed browser pre-warming (caused two-Chrome bug + slowness)**: Pre-warming created a stealthy session in the background on first smart_fetch call. This raced with Phase B: if Phase B started before pre-warming completed, it created a dynamic session, then pre-warming created a stealthy session — both lived indefinitely. Pre-warming also made stealthy always-alive, causing Phase B to always take the stealthy shortcut (slower than dynamic for simple JS rendering). Removed entirely.
- **Dynamic session close is now non-blocking**: `_close_auto_dynamic_session()` was `await`-ed in the fetch path, blocking the actual fetch while Chrome shut down. Now fires as `asyncio.create_task()` — the close happens in background, the fetch proceeds immediately.

## [3.4.0] - 2026-06-08

### Fixed
- **Double Chrome instance after auto-escalation**: When Phase B or C escalated from dynamic to stealthy, the dynamic auto session was never closed. Result: two Chrome processes (Playwright + Patchright) consuming ~300MB RAM combined. Added `_close_auto_dynamic_session()` to atomically close the dynamic session whenever a stealthy auto session is created. Patchright handles everything Playwright does — no reason to keep both.

### Changed
- **Phase C (unknown domain) skips dynamic tier**: HTTP → Stealthy directly instead of HTTP → Dynamic → Stealthy. For unknown domains, trying dynamic first wastes 3-5s launching a browser that will likely need escalation anyway, and leaves an orphan Chrome process. Cuts worst-case first-fetch latency from ~12s to ~7s.

### Added
- **Browser pre-warming**: Stealthy Chrome launches in the background on the first `smart_fetch`, `open_session`, or `screenshot` call. By the time a follow-up fetch or escalation needs it, the browser is already warm — eliminating the 3-5s cold-start penalty on subsequent calls. Fire-and-forget, fails silently.

## [3.3.1] - 2026-06-07

### Changed
- **smart_fetch description**: Added "USE THIS whenever you need information from the web — this is your web access" to make agents recognize it as their primary web tool, not an optional utility.
- **smart_search description**: Added "Search finds links — descriptions are NOT enough to answer questions. ALWAYS fetch the result URL with smart_fetch for full content" to prevent agents from answering based on search snippets alone.

## [3.3.0] - 2026-06-07

### Added
- **Idle timeout for auto browser sessions**: Browser processes now close after 30 minutes of inactivity instead of running forever. Frees ~200-300MB RAM per session. Reopens on next fetch (one-time 5s penalty). `AUTO_SESSION_IDLE_TIMEOUT = 1800`, `IDLE_CHECK_INTERVAL = 60`.
- **Session consolidation**: When the stealthy (Patchright) auto session is already alive, dynamic-tier requests use it instead of spawning a separate Playwright browser. Patchright is a Playwright fork and handles everything Playwright does. Cuts idle RAM from ~400-600MB (2 browsers) to ~200-300MB (1 browser).

### Fixed
- **TOCTOU race in session consolidation**: `_acquire_stealthy_session()` atomically checks session liveness, bumps `last_used` timestamp, and returns session ID under the sessions lock. Prevents idle monitor from closing a session between check and use.
- **Idle monitor race condition**: All reads of `_auto_*_id` and `_auto_*_last_used` now happen inside the sessions lock. Prevents stale timestamp reads that could kill a session that was just used.
- **Domain intel corruption from consolidation**: When dynamic tier is skipped due to stealthy consolidation, `record_result` now records the domain's actual level ("low"), not the fetcher used ("high"). Prevents false promotion that would skip dynamic on future fetches.
- **Orphaned sessions on close failure**: When `close_session()` fails internally during idle cleanup, the session is still removed from the sessions dict and marked as not alive. No ghost sessions leaking memory.
- **Idle monitor silent death**: Added `try/except` around monitor loop body. `CancelledError` re-raised for proper asyncio cleanup. Other exceptions logged and loop continues on next cycle.
- **Monitor restart on session reuse**: `_ensure_idle_monitor()` now called on all return paths of `_ensure_auto_session()` (not just new session creation).

## [3.2.0] - 2026-06-07

### Fixed
- **Double-chunking regression**: `bulk_get`, `bulk_fetch`, `bulk_stealthy_fetch` were applying `_apply_chunking` internally, then `_finalize_result` chunked again. Result: 148KB JSON appeared as 40KB because chunking ran twice. Removed chunking from bulk methods — only `_finalize_result` (single-URL path) and `_smart_fetch_bulk` (bulk path) apply chunking once.
- **Cache now stores unchunked content**: Since bulk methods no longer chunk, cache stores full extracted content. Offset-based pagination now works correctly — the second call with `offset=40000` gets the correct remaining content instead of the first chunk again.

## [3.1.0] - 2026-06-07

### Changed
- **Smart chunking merge**: When remaining content after a chunk is less than 500 chars, it's included in the current chunk instead of setting `is_truncated: true`. No more wasteful round-trips for 55-char second calls.
- **`total_extracted_chars` field**: New ResponseModel field shows total extracted text length (before chunking). Agents can calculate remaining content without a follow-up call: `total_extracted_chars - offset`.
- **Honest chunking meta-awareness**: `offset` pages through EXTRACTED text, not raw HTML. If extraction produces 40KB from a 1MB page, offset can't reach beyond that 40KB. Tool description now explains this. Use `extraction_type=html` for raw HTML.
- **Honest truncation messages**: Now includes exact remaining chars count: `[Truncated: showing 40,000 of 120,000 extracted chars. 80,000 chars remaining. Next offset: 40000]`
- **Cache meta-awareness**: `cache_clear` and `mcp_smart_search` descriptions now mention cache behavior and TTL.

## [3.0.1] - 2026-06-07

### Fixed
- **SSRF bypass via alternate IP notations**: `0177.0.0.1` (octal), `0x7f.1` (hex), `2130706433` (decimal integer), `127.1` (short-form) now correctly resolved and blocked. `_normalize_ip_notation()` handles all curl/libcurl-supported IP formats that `ipaddress.ip_address()` misses.
- **Proxy credentials leaking in error messages**: `http://user:pass@proxy:8080` URLs in error output now redacted to `http://[CREDENTIALS_REDACTED]@proxy:8080`.
- **`_check_version()` blocks MCP event loop**: `version()` now calls `_check_version()` via `asyncio.to_thread()`. No more 5s freeze on concurrent tool calls.
- **No URL count limit in bulk methods**: Added `MAX_BULK_URLS = 100` cap to `bulk_get`, `bulk_fetch`, `bulk_stealthy_fetch`.
- **Race condition on `_db_initialized`**: Both `cache.py` and `domain_intel.py` now use `asyncio.Lock` with double-check pattern to prevent concurrent DB init.
- **Empty string CSS selector passthrough**: `validate_css_selector("")` now returns `None` instead of `""`.
- Removed dead imports: `guess_protection_level` from `server.py`, `json` from `domain_intel.py`.

## [3.0.0] - 2026-06-07

### Changed
- **Major reliability upgrade**: no new features, hardened internals.
- `gather()` in all bulk methods now uses `return_exceptions=True`. One failed URL no longer crashes the entire batch. Failed URLs return `ResponseModel` with `status=0` and error details.
- `_ensure_auto_session` race condition fixed: concurrent calls no longer orphan browser sessions. Re-check after session creation closes orphans.
- `open_session` exception handler now sets `_alive=False` inside the session lock, preventing use-after-close races.
- `_normalize_credentials` now validates types (must be string), length (max 512), and rejects newlines. Prevents injection/DoS via oversized credential strings.
- `_dispatch` error responses now use MCP's `isError=True` flag. Agents can distinguish errors from successful results. Missing url/urls and unknown tools now raise `ValueError` caught by the outer handler instead of returning silent error JSON.
- `search.py` raises `SecurityError` consistently instead of plain `Exception`. Matches the rest of the codebase's error hierarchy.
- Domain intel downgrade threshold raised from 5 to 10 consecutive stealthy hits with zero fails. Prevents protection-level flip-flopping on temporarily permissive sites.
- Cache and domain intel DB initialization cached: `_db_initialized` dict prevents redundant PRAGMA calls on every cache operation.
- Cloudflare detection now only triggers on status 403/503. Status 200 pages mentioning "cloudflare" in body text (e.g. articles about web security) are no longer falsely flagged.

## [2.11.3] - 2026-06-06

### Fixed
- `list_sessions` returned a list as `structuredContent` (MCP requires dict). Now returns `{"sessions": [...]}`.

### Fixed
- `list_sessions` NameError: `json` not imported at module level (broken in v2.11.0 low-level server refactor).

## [2.11.1] - 2026-06-06

### Changed
- Trimmed hand-holding from tool descriptions and truncation messages. Same meta-awareness, fewer tokens.

## [2.11.0] - 2026-06-06

### Changed
- **Switched from FastMCP to low-level MCP Server**: hand-crafted minimal tool definitions eliminate Pydantic schema bloat. Tools/list payload dropped from 13KB/4.4K tokens to 4KB/1.4K tokens (69% reduction). No outputSchema in declarations (agents get structured content in every response instead). Every param still has descriptions, annotations intact.
- `next_offset` field in ResponseModel: structured integer for pagination. No string parsing needed.

## [2.10.1] - 2026-06-06

### Added
- `next_offset` field in ResponseModel: structured integer telling agents the exact offset for the next chunk. No string parsing needed. `0` = no more content.

### Changed
- `is_truncated` description: `True=more content, use next_offset`
- `offset` param description: `0-based char offset. is_truncated=true? Use next_offset value.`

## [2.10.0] - 2026-06-06

### Changed
- **Token-efficient MCP tool definitions**: 20KB → 13KB (34% less tokens burned per conversation). Core params stay structured and validated. Advanced params moved to `options` dict (proxy, cookies, useragent, etc.). Agents discover them without paying token tax on every call.
- Compressed all field descriptions in output schemas (e.g. "HTTP status code returned by the website (0 if network error)" → "HTTP status (0=network error)").
- Tool descriptions compressed to terse one-liners.

## [2.9.3] - 2026-06-06

### Added
- Parameter descriptions on all `smart_fetch` inputs (via `Annotated` + `Field`). Agents now see what `offset`, `cache_ttl`, `force_fetcher`, etc. do before calling.
- Tool description now mentions chunking (`is_truncated=True` means call again with `offset`) and caching (`cache_ttl=0` to force fresh).

## [2.9.2] - 2026-06-06

### Fixed
- **Double-chunking bug**: `bulk_get`, `bulk_fetch`, `bulk_stealthy_fetch` applied `_apply_chunking` before `_finalize_result` applied it again. Truncation messages got baked into cached content, and offset continuation returned the truncation message instead of actual content. Now chunking only happens once, in `_finalize_result` (and the direct cache-hit/robots paths).

## [2.9.1] - 2026-06-06

### Added
- MCP `ToolAnnotations` on all 8 tools: `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`. Agents can now determine whether a tool is safe to parallelize, retry, or cache without trial and error. ([Idea from this Reddit post](https://www.reddit.com/r/mcp/comments/1tyh7dp/mcp_needs_better_tool_metadata/))

### Removed
- `hound install` command. Use `playwright install chromium` directly. One less custom command to maintain.

## [2.9.0] - 2026-06-06

### Fixed
- **MCP server now starts in <3s instead of 5-10s.** Scrapling/playwright imports deferred to first tool call. Before: OpenCode and other clients timed out waiting for `initialize` response (default 5s timeout). Now: server responds instantly, heavy deps load on first `smart_fetch`/`smart_search` call.
- `serverInfo.version` in MCP handshake now shows Hound version (e.g. `2.9.0`) instead of MCP SDK version (`1.27.0`)
- `NameError: name 'sys' is not defined` in `smart_fetch` and `open_session` — missing import in lazy loader

### Changed
- Install flow: `pip install hound-mcp[all]` then `hound install` (Chromium setup). No more chicken-and-egg where `hound install` can't run before pip.
- `hound -v` output simplified: `Hound v2.9.0 (latest)` or `Hound v2.9.0. v3.0.0 available. Run hound -u to update.` No PyPI jargon.
- `hound install` no longer re-runs pip (just Chromium setup). Shows `(already installed)` when re-run.
- Agent prompts in README: correct POV ("Tell the user to restart this agent", not "Restart your agent"), no client-specific config examples, no fetch-only option.
- Removed broken fetch-only install path. One product: fetch + search.
- OpenCode MCP config format: `type: "local"`, `command: ["hound"]`, `environment: { ... }` (not `env`).

### Added
- `__main__.py`: `python -m master_fetch` works as MCP server
- `__version__` in `__init__.py` as single source of truth
- `list_sessions` in README tools table

## [2.8.0] - 2026-06-06

### Removed
- 6 redundant tools removed: `get`, `fetch`, `stealthy_fetch`, `bulk_get`, `bulk_fetch`, `bulk_stealthy_fetch`. All functionality merged into `smart_fetch` via `force_fetcher` and `urls` params. 13 tools → 7.

### Changed
- `smart_fetch` now accepts `urls` (list) for bulk fetching. Returns `BulkResponseModel`.

### Added
- `ResponseModel` metadata: `content_type` (e.g. "text/html", "application/json"), `total_size_bytes`, `is_truncated`, `escalation_path` (e.g. "http→dynamic→stealthy"), `retry_count`
- JSON responses detected and returned raw — no more trafilatura mangling
- Agent-focused tool descriptions: "when to use", output shape, recovery hints
- Actionable error messages with recovery tips ("Try: use smart_fetch for auto-escalation")

## [2.7.0] - 2026-06-06

### Security
- SSRF protection: blocks internal IPs, localhost, cloud metadata endpoints, dangerous URL schemes
- Input validation on all entry points: URLs, CSS selectors, custom headers, proxies, timeouts, search queries
- API key redaction in error messages and logs
- Response body size limit (50MB cap)

### Fixed
- `_is_cloudflare_from_response` defined after class, causing runtime NameError. Moved above class.
- `is_allowed()` was blocking the async event loop with sync urllib. Now fully async.
- Cache corruption silently swallowed (bare except). Now logged.

### Changed
- Deduplicated fetch→annotate→cache→chunk pattern (8 copies → 1 `_finalize_result` helper)
- Smart fetch split into `_force_fetch`, `_auto_escalate`, `_phase_c_unknown`
- `__init__.py` uses lazy imports so lightweight modules don't pull scrapling
- Cookie conversion hardened against missing keys

### Added
- `security.py` module
- 158 unit tests across 6 test files

## [2.6.0] - 2026-06-02

### Fixed
- Auto-escalation was broken. Phase C bailed out after HTTP failure unless the response looked like a Cloudflare challenge. Sites returning 403 with "JS required" (IMDb, ScrapingCourse) never got dynamic or stealthy. Now: every tier that fails escalates to the next. Simple try-next-tier, no fancy gating.

## [2.4.0] - 2026-06-02

### Changed
- README overhaul: honest competitor comparison (Hound vs Crawl4AI/Firecrawl/Bright Data/Exa/Tavily/Jina), Pi agent one-prompt install section, installation prompts for all major agent harnesses
- Pi agent setup uses pi-mcp-adapter (correct package name)
- Search now requires TINYFISH_API_KEY env var (get free key at tinyfish.ai)

## [2.3.1] - 2026-06-02

### Fixed
- **Content continuation actually works now**: The 40KB truncation message previously said "re-fetch with offset parameter" but no offset parameter existed. Now `smart_fetch` has an `offset` parameter (default 0). When content is truncated, the response tells the agent exactly what offset to use: "Call smart_fetch again with offset=40000 to get the next chunk." The cached full content is used, so continuation calls are instant.
- **Chunking preserves all ResponseModel fields**: Previously dropped extracted_type, session_id, duration_ms, error during truncation. Now all fields survive.
- **Offset beyond content returns clean end message**: "No more content available" instead of empty or error.

### Changed
- **Continuation message is agent-friendly**: Shows exact char range ("received 40,000 of 60,000 chars, offset 0-40,000, 20,000 chars remaining") and the exact next call to make.

## [2.3.0] - 2026-06-01

### Fixed
- **Smart router now detects JS-only shells and escalates** (#1): When HTTP returns a 200 with content like "You need to enable JavaScript", smart_fetch now escalates to dynamic. When dynamic returns a JS-disabled placeholder (e.g. Twitter), escalates to stealthy. Previously accepted these as successful results.
- **Error field now signals content quality issues** (#7, #8): The `error` field is set when content is a JS shell (`js_shell_detected`), a geo/region redirect (`geo_redirect_detected`), or a bot challenge page (`bot_challenge_detected`). Downstream agents can now detect failures without parsing content strings.
- **Bulk output now respects max_content_chars** (#3): All bulk operations (`bulk_get`, `bulk_fetch`, `bulk_stealthy_fetch`) now accept a `max_content_chars` parameter (default 40000) that truncates each result. Prevents 300KB+ output that overwhelms tool runtimes.
- **Bulk `successful` count now excludes results with content issues**: A result with an error field is no longer counted as successful.

### Changed
- **Domain intelligence expanded**: Added known-safe domains (httpbin.org, wikipedia.org, github.com, stackoverflow.com, etc.) to prevent over-escalation of static sites. Added YouTube, Uniswap, Spotify, Notion, and other SPA domains as known-dynamic. Moved Twitter/X from dynamic to stealthy (dynamic returns JS-disabled placeholder for these).
- **All smart_fetch return paths now run content quality annotation**: Every exit point (Phase A/B/C, force_fetcher, escalation results) calls `_annotate_quality()` to ensure the error field is populated when content is bad.
- **All-tiers-failed error now includes failure trace**: The `error` field shows which tiers were tried and what failed.

### Added
- 15 new unit tests covering JS shell detection, content quality, geo redirect, domain intelligence routing

## [2.0.0] - 2026-06-02: Hound

Renamed product from "Master Fetch" to **Hound**: web research for AI agents.
Internal module name stays `master_fetch`. Package: `hound-mcp`. CLI: `hound`.

### Added

### Added
- Web search via TinyFish API: `smart_search` tool returns structured results (title, url, snippet)
  - Free (30 searches/min), no API key needed
  - Results cached for 5 minutes via SQLite
  - Optional install: `pip install master-fetch[all]`
  - Fetch-only users stay lean with zero extra dependencies

### Changed
- Package architecture: `master-fetch` = fetch only, `master-fetch[all]` = fetch + search
- README rewritten with competitor comparison tables and one-prompt install guides

## [1.1.0] - 2026-06-02

### Added
- Robots.txt compliance: respects site scraping policies by default. `respect_robots=False` to bypass.
- HTTP retry logic: exponential backoff (1s/2s/4s) on transient network failures
- Comprehensive test suite: 22 unit tests covering models, chunking, CF detection, domain extraction, binary detection, robots.txt
- GitHub Actions CI: cross-platform testing on Ubuntu and Windows (Python 3.11, 3.12)
- Proper PyPI metadata: classifiers, dev dependencies, pytest config

### Changed
- ResponseModel now includes `extracted_type`, `session_id`, `duration_ms`, `error` fields
- Error messages: all-tiers-failed returns diagnostic trace showing what was tried

### Fixed
- Binary content (PDF) no longer crashes the extractor, returns clean error
- HTTP error pages (non-challenge) no longer trigger wasteful browser escalation

## [1.0.4] - 2026-06-02

### Fixed
- PDF and binary content handling: returns clean error instead of crashing with decode error
- HTTP error pages no longer trigger unnecessary browser escalation. If the response contains no bot challenge text, the error is returned directly.

## [1.0.3] - 2026-06-02

### Fixed
- Domain extraction now correctly handles multi-part TLDs (.co.uk, .com.au, .co.jp, etc.)
- Auto-persistent browser sessions in smart_fetch. Dynamic and stealthy tiers now reuse browser instances instead of launching a new browser each time.
- Disabled resource loading in dynamic/stealthy tiers for ~25% speed improvement.
- Rewrote README with accurate, minimal information.

## [1.0.2] - 2026-06-01

### Fixed
- Default caching was OFF (cache_ttl=0). Now defaults to 3600s (1 hour). Repeat fetches return instantly from cache.
- Added 40KB content chunking with offset continuation. AI agents get a truncation notice when content exceeds the limit.

## [1.0.0] - 2026-06-01

### Added
- Smart fetch routing: auto-escalates HTTP → Dynamic → Stealthy based on bot detection
- Cloudflare Turnstile/Interstitial bypass via Patchright + Scrapling
- Trafilatura content extraction pipeline (markdown, text, article, structured)
- SQLite content caching with configurable TTL
- Domain intelligence system: remembers which domains need which fetcher level
- 12 MCP tools: get, bulk_get, fetch, bulk_fetch, stealthy_fetch, bulk_stealthy_fetch, screenshot, open_session, close_session, list_sessions, smart_fetch, cache_clear
- Streamable HTTP transport (--http flag) for remote agent connections
- Anti-bot bypass for DataDome, Akamai, Cloudflare challenges
- Content quality rating: 9.5/10 vs competitors
- Beats Exa and Tavily on JS-rendered and bot-protected pages (see COMPARISON_REPORT.md)

### Known Limitations
- DataDome + Cloudflare dual protection (g2.com) still blocks all fetchers
- Reddit infinite scroll only returns first-load content
- No built-in rate limiting between fetcher tiers
- Domain extraction doesn't handle .co.uk / .com.au correctly

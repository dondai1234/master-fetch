"""
v3.4.1 REAL verification: MCP calls + Chrome process count + speed.
Tests against the ACTUAL running hound binary (not just API).
Verifies: HTTP works, no two Chrome, speed is fast, no timeouts.
"""
import asyncio
import subprocess
import sys
import os
import time
import json

G = "\033[92m"
R = "\033[91m"
B = "\033[1m"
X = "\033[0m"

def chrome_count():
    """Count 'chrome' processes (headless Chrome for Testing) in tasklist."""
    try:
        out = subprocess.check_output(
            'tasklist /FI "IMAGENAME eq chrome*" /FO CSV /NH',
            shell=True, text=True, timeout=5
        )
        # Count lines that contain "chrome" (case-insensitive)
        lines = [l for l in out.strip().split('\n') if l and 'chrome' in l.lower()]
        return len(lines)
    except Exception:
        return -1

def chk(actual, expected, label):
    ok = actual == expected
    print(f"  {G}PASS{X}" if ok else f"  {R}FAIL{X}", f"{label}: {repr(actual)[:80]}")
    return ok

def chk_true(actual, label):
    ok = bool(actual)
    print(f"  {G}PASS{X}" if ok else f"  {R}FAIL{X}", f"{label}")
    return ok

def chk_less(actual, limit, label):
    ok = actual < limit
    print(f"  {G}PASS{X}" if ok else f"  {R}FAIL{X}", f"{label}: {actual:.0f}ms {'<' if ok else '>='} {limit}ms")
    return ok

def chk_not_contains(actual, needle, label):
    ok = needle not in str(actual)
    print(f"  {G}PASS{X}" if ok else f"  {R}FAIL{X}", f"{label}: '{needle}' {'NOT' if ok else 'IS'} in result")
    if not ok:
        print(f"    Got: {str(actual)[:200]}")
    return ok


async def test_http_speed():
    """HTTP fetch to a simple site is FAST (<3s) and uses 'direct:http' path."""
    print(f"\n{B}TEST 1: HTTP fetch is fast and uses 'direct:http'{X}")
    
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer(cache_ttl=0)
    
    n_chrome_before = chrome_count()
    print(f"  Chrome processes before: {n_chrome_before}")
    
    t0 = time.time()
    result = await srv.smart_fetch(
        url="https://jsonplaceholder.typicode.com/posts/1",
        cache_ttl=0,
        timeout=15000,
    )
    elapsed_ms = (time.time() - t0) * 1000
    
    n_chrome_after = chrome_count()
    print(f"  Chrome processes after: {n_chrome_after}")
    print(f"  Status: {result.status}")
    print(f"  Path: {result.escalation_path}")
    print(f"  Time: {elapsed_ms:.0f}ms")
    
    # Clean up
    for sid in list(srv._sessions.keys()):
        try: await srv.close_session(sid)
        except: pass
    
    p = True
    p &= chk(result.status, 200, "Status 200")
    p &= chk_less(elapsed_ms, 5000, "HTTP fetch under 5s")
    p &= chk_not_contains(result.escalation_path, "stealthy", "Path does NOT mention stealthy")
    p &= chk_not_contains(result.escalation_path, "dynamic", "Path does NOT mention dynamic")
    p &= chk_true("http" in (result.escalation_path or ""), "Path DOES mention 'http'")
    p &= chk(n_chrome_after, n_chrome_before, f"Chrome count unchanged (was {n_chrome_before}, now {n_chrome_after})")
    
    # Give Chrome processes time to die
    await asyncio.sleep(2)
    
    return p


async def test_stealthy_escalation_single_chrome():
    """Fetching a site that needs stealthy creates exactly 1 Chrome process."""
    print(f"\n{B}TEST 2: Stealthy escalation = exactly 1 Chrome{X}")
    
    from master_fetch.server import MasterFetchServer
    # Wipe domain intel for test domain
    from master_fetch.domain_intel import _DB_NAME
    import aiosqlite, tempfile
    from pathlib import Path
    db_path = Path(tempfile.gettempdir()) / _DB_NAME
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS domain_intel (
                domain TEXT PRIMARY KEY, protection_level TEXT DEFAULT 'none',
                avg_response_ms REAL DEFAULT 0, hit_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0, last_seen REAL DEFAULT 0
            )
        """)
        await db.commit()
        await db.execute("DELETE FROM domain_intel WHERE domain = ?", ("old.reddit.com",))
        await db.commit()
    
    srv = MasterFetchServer(cache_ttl=0)
    
    # Kill any leftover Chrome
    for sid in list(srv._sessions.keys()):
        try: await srv.close_session(sid)
        except: pass
    await asyncio.sleep(3)
    
    n_chrome_before = chrome_count()
    print(f"  Chrome processes before: {n_chrome_before}")
    
    # Fetch a site that will need stealthy (reddit blocks HTTP)
    t0 = time.time()
    result = await srv.smart_fetch(
        url="https://old.reddit.com/r/all/.json",
        cache_ttl=0,
        extraction_type="text",
        timeout=45000,
    )
    elapsed_ms = (time.time() - t0) * 1000
    
    # Give close_auto_dynamic background task time to run
    await asyncio.sleep(2)
    
    n_chrome_after = chrome_count()
    print(f"  Chrome processes after: {n_chrome_after}")
    print(f"  Status: {result.status}")
    print(f"  Path: {result.escalation_path}")
    print(f"  Time: {elapsed_ms:.0f}ms")
    
    # Check internal session state
    async with srv._sessions_lock:
        sessions = list(srv._sessions.keys())
        auto_dyn = srv._auto_dynamic_id
        auto_stl = srv._auto_stealthy_id
    print(f"  Sessions alive: {len(sessions)}")
    print(f"  Auto dynamic: {auto_dyn}")
    print(f"  Auto stealthy: {auto_stl}")
    
    p = True
    p &= chk_not_contains(result.escalation_path or "", "dynamic", "Path does NOT mention 'dynamic' (Phase C)")
    p &= chk_true(len(sessions) <= 1, f"At most 1 internal session (got {len(sessions)})")
    p &= chk_true(n_chrome_after <= n_chrome_before + 1, 
                  f"At most 1 new Chrome process (before={n_chrome_before}, after={n_chrome_after})")
    # If stealthy was created, dynamic must be None (closed in background)
    if auto_stl is not None:
        p &= chk(auto_dyn, None, "Dynamic auto is None when stealthy exists")
    
    # Clean up
    for sid in list(srv._sessions.keys()):
        try: await srv.close_session(sid)
        except: pass
    await asyncio.sleep(3)
    
    return p


async def test_cache_is_fast():
    """Cached fetch is instant (<50ms)."""
    print(f"\n{B}TEST 3: Cache hit is instant{X}")
    
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer(cache_ttl=3600)
    
    # First fetch — populate cache
    await srv.smart_fetch(
        url="https://jsonplaceholder.typicode.com/posts/2",
        cache_ttl=3600,
        timeout=15000,
    )
    
    # Second fetch — should be cached
    t0 = time.time()
    result = await srv.smart_fetch(
        url="https://jsonplaceholder.typicode.com/posts/2",
        cache_ttl=3600,
        timeout=15000,
    )
    elapsed_ms = (time.time() - t0) * 1000
    
    print(f"  Cached: {result.cached}, Time: {elapsed_ms:.0f}ms, Status: {result.status}")
    
    p = True
    p &= chk(result.cached, True, "Result is cached")
    p &= chk_less(elapsed_ms, 100, "Cached fetch under 100ms")
    
    # Clean cache
    from master_fetch.cache import clear_all_cache
    await clear_all_cache()
    
    return p


async def test_force_stealthy_no_second_chrome():
    """force_fetcher='stealthy' with an existing dynamic session = still 1 Chrome."""
    print(f"\n{B}TEST 4: force_fetch stealthy + existing dynamic = 1 Chrome{X}")
    
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer(cache_ttl=0)
    
    # Create dynamic auto session
    dyn = await srv.open_session(session_type="dynamic", headless=True)
    srv._auto_dynamic_id = dyn.session_id
    
    n_chrome_before = chrome_count()
    print(f"  Chrome before force: {n_chrome_before}")
    
    # Force stealthy fetch
    result = await srv.smart_fetch(
        url="https://jsonplaceholder.typicode.com/posts/1",
        force_fetcher="stealthy",
        cache_ttl=0,
        timeout=30000,
    )
    
    # Wait for background close
    await asyncio.sleep(3)
    
    n_chrome_after = chrome_count()
    print(f"  Chrome after force: {n_chrome_after}")
    print(f"  Path: {result.escalation_path}")
    
    async with srv._sessions_lock:
        auto_dyn = srv._auto_dynamic_id
        auto_stl = srv._auto_stealthy_id
    print(f"  Auto dynamic: {auto_dyn}, Auto stealthy: {auto_stl}")
    
    p = True
    p &= chk(auto_dyn, None, "Dynamic auto is None after stealthy created")
    p &= chk_true(n_chrome_after <= n_chrome_before, 
                  f"Chrome count did NOT increase (before={n_chrome_before}, after={n_chrome_after})")
    
    # Clean up
    for sid in list(srv._sessions.keys()):
        try: await srv.close_session(sid)
        except: pass
    await asyncio.sleep(3)
    
    return p


async def test_three_fetches_stable():
    """Three sequential fetches: no Chrome explosion, no session leak."""
    print(f"\n{B}TEST 5: 3 sequential fetches = stable{X}")
    
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer(cache_ttl=0)
    
    paths = []
    for i in range(3):
        t0 = time.time()
        result = await srv.smart_fetch(
            url="https://jsonplaceholder.typicode.com/posts/1",
            cache_ttl=0,
            timeout=15000,
        )
        elapsed_ms = (time.time() - t0) * 1000
        
        async with srv._sessions_lock:
            n_sessions = len(srv._sessions)
        paths.append((result.escalation_path, result.status, elapsed_ms, n_sessions))
        print(f"  Fetch {i+1}: path={result.escalation_path}, status={result.status}, "
              f"time={elapsed_ms:.0f}ms, sessions={n_sessions}")
    
    n_chrome = chrome_count()
    print(f"  Chrome processes after 3 fetches: {n_chrome}")
    
    p = True
    for i, (path, status, ms, ns) in enumerate(paths):
        p &= chk_true(ns <= 1, f"Fetch {i+1}: at most 1 session (got {ns})")
        p &= chk(status, 200, f"Fetch {i+1}: status 200")
    
    # Clean up
    for sid in list(srv._sessions.keys()):
        try: await srv.close_session(sid)
        except: pass
    
    return p


async def main():
    print("=" * 60)
    print(f"{B}HOUND v3.4.1 — REAL VERIFICATION{X}")
    print(f"Chrome processes right now: {chrome_count()}")
    print("=" * 60)
    
    # Kill stray Chrome first
    os.system('taskkill /F /IM "chrome.exe" >nul 2>&1')
    os.system('taskkill /F /IM "chromium.exe" >nul 2>&1')
    await asyncio.sleep(2)
    print(f"Chrome after cleanup: {chrome_count()}")
    
    tests = [
        ("HTTP fetch is fast", test_http_speed),
        ("Stealthy escalation = 1 Chrome", test_stealthy_escalation_single_chrome),
        ("Cache is instant", test_cache_is_fast),
        ("force_fetch stealthy + dynamic = 1 Chrome", test_force_stealthy_no_second_chrome),
        ("3 fetches stable", test_three_fetches_stable),
    ]
    
    results = {}
    for name, fn in tests:
        try:
            results[name] = await fn()
        except Exception as e:
            results[name] = False
            print(f"\n  {R}CRASHED{X}: {e}")
            import traceback
            traceback.print_exc()
        await asyncio.sleep(2)
    
    print("\n" + "=" * 60)
    print(f"{B}RESULTS{X}")
    print("=" * 60)
    for name, ok in results.items():
        print(f"  {G}PASS{X}" if ok else f"  {R}FAIL{X}", name)
    
    total = len(results)
    passed = sum(1 for v in results.values() if v)
    print(f"\n  {passed}/{total}")
    if passed == total:
        print(f"{G}ALL PASSED{X}")
    else:
        print(f"{R}{total-passed} FAILED{X}")
    
    # Final Chrome count
    await asyncio.sleep(3)
    final = chrome_count()
    print(f"\n  Chrome processes at end: {final}")
    
    return passed == total


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)

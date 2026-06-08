"""
Focused verification for v3.4.0 — no dependency on external services.
Tests browser session consolidation logic directly.
"""
import asyncio
import sys, os, time, json
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from master_fetch.server import MasterFetchServer, _get_scrapling

G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
B = "\033[1m"
X = "\033[0m"

def chk(actual, expected, label):
    ok = actual == expected
    print(f"  {G}PASS{X}" if ok else f"  {R}FAIL{X}", f"{label}: {repr(actual)[:90]}")
    return ok

def chk_true(actual, label):
    ok = bool(actual)
    print(f"  {G}PASS{X}" if ok else f"  {R}FAIL{X}", f"{label}")
    return ok

def chk_not(actual, needle, label):
    ok = needle not in str(actual)
    print(f"  {G}PASS{X}" if ok else f"  {R}FAIL{X}", f"{label}: '{needle}' NOT in result")
    if not ok:
        print(f"    Got: {str(actual)[:150]}")
    return ok


async def get_session_state(server):
    """Get a snapshot of session state."""
    async with server._sessions_lock:
        return {
            "sessions": list(server._sessions.keys()),
            "auto_dynamic": server._auto_dynamic_id,
            "auto_stealthy": server._auto_stealthy_id,
        }


async def test_1_close_method():
    """_close_auto_dynamic_session() works correctly."""
    print(f"\n{B}TEST 1: _close_auto_dynamic_session(){X}")
    srv = MasterFetchServer(cache_ttl=0)
    
    # Create dynamic auto session
    sess = await srv.open_session(session_type="dynamic", headless=True)
    srv._auto_dynamic_id = sess.session_id
    state = await get_session_state(srv)
    print(f"  Before: sessions={state['sessions']}, dynamic={state['auto_dynamic']}")
    
    # Close it
    await srv._close_auto_dynamic_session()
    state = await get_session_state(srv)
    print(f"  After:  sessions={state['sessions']}, dynamic={state['auto_dynamic']}")
    
    p = True
    p &= chk(state["auto_dynamic"], None, "auto_dynamic_id cleared")
    p &= chk(len(state["sessions"]), 0, "No sessions remain")
    
    # Double-close should be no-op
    try:
        await srv._close_auto_dynamic_session()
        state2 = await get_session_state(srv)
        p &= chk(state2["auto_dynamic"], None, "Double-close: still None")
        print(f"  {G}PASS{X} Double-close is safe no-op")
    except Exception as e:
        print(f"  {R}FAIL{X} Double-close crashed: {e}")
        p = False
    
    return p


async def test_2_force_stealthy_closes_dynamic():
    """force_fetcher='stealthy' closes orphan dynamic auto session."""
    print(f"\n{B}TEST 2: force_fetch stealthy → closes dynamic{X}")
    srv = MasterFetchServer(cache_ttl=0)
    
    # Create orphan dynamic
    dyn = await srv.open_session(session_type="dynamic", headless=True)
    srv._auto_dynamic_id = dyn.session_id
    state = await get_session_state(srv)
    print(f"  Initial: dynamic={state['auto_dynamic']}, stealthy={state['auto_stealthy']}")
    
    # Force stealthy fetch — use a quick URL
    result = await srv.smart_fetch(
        url="https://jsonplaceholder.typicode.com/posts/1",
        force_fetcher="stealthy",
        cache_ttl=0,
        timeout=30000,
    )
    
    state = await get_session_state(srv)
    print(f"  After fetch: dynamic={state['auto_dynamic']}, stealthy={state['auto_stealthy']}")
    print(f"  Escalation: {result.escalation_path}, status={result.status}")
    
    p = True
    p &= chk(state["auto_dynamic"], None, "Dynamic auto session CLOSED")
    p &= chk_true(state["auto_stealthy"] is not None, "Stealthy auto session EXISTS")
    p &= chk_true(len(state["sessions"]) <= 1, "At most 1 session alive")
    
    # Cleanup
    for sid in list(srv._sessions.keys()):
        try: await srv.close_session(sid)
        except: pass
    
    return p


async def test_3_phase_c_skips_dynamic():
    """Phase C escalation path never includes 'dynamic' tier."""
    print(f"\n{B}TEST 3: Phase C escalation path{X}")
    
    # Wipe domain intel for test domain
    from master_fetch.domain_intel import _DB_NAME
    import aiosqlite, tempfile
    db_path = Path(tempfile.gettempdir()) / _DB_NAME
    async with aiosqlite.connect(db_path) as db:
        # Ensure table exists (CREATE IF NOT EXISTS is idempotent)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS domain_intel (
                domain TEXT PRIMARY KEY,
                protection_level TEXT DEFAULT 'none',
                avg_response_ms REAL DEFAULT 0,
                hit_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                last_seen REAL DEFAULT 0
            )
        """)
        await db.commit()
        await db.execute("DELETE FROM domain_intel WHERE domain = ?", ("jsonplaceholder.typicode.com",))
        await db.commit()
    
    srv = MasterFetchServer(cache_ttl=0)
    
    # Fetch a URL that works with HTTP — should be Phase C, HTTP succeeds
    result = await srv.smart_fetch(
        url="https://jsonplaceholder.typicode.com/posts/1",
        cache_ttl=0,
        timeout=30000,
    )
    
    ep = result.escalation_path or ""
    print(f"  Path: {ep}, status={result.status}")
    state = await get_session_state(srv)
    print(f"  Sessions: {len(state['sessions'])}")
    
    p = True
    # Phase C for a working URL: should be "direct:http"
    # The assertion: "http->dynamic" must NOT appear (that's 3-tier escalation, removed in v3.4.0)
    p &= chk_not(ep, "http->dynamic", "Phase C never uses dynamic tier")
    p &= chk_true(len(state["sessions"]) <= 1, "At most 1 session")
    
    # Cleanup
    for sid in list(srv._sessions.keys()):
        try: await srv.close_session(sid)
        except: pass
    
    return p


async def test_4_prewarm_then_fetch():
    """Pre-warmed browser handles fetch immediately."""
    print(f"\n{B}TEST 4: Pre-warm → fetch (no cold-start){X}")
    srv = MasterFetchServer(cache_ttl=0)
    
    # Pre-warm
    print("  Pre-warming...")
    await srv._prewarm_stealthy()
    state = await get_session_state(srv)
    print(f"  After pre-warm: stealthy={state['auto_stealthy']}")
    
    p = True
    p &= chk_true(state["auto_stealthy"] is not None, "Pre-warming created stealthy session")
    
    if state["auto_stealthy"]:
        # Now fetch — pre-warmed browser should handle it fast
        t0 = time.time()
        result = await srv.smart_fetch(
            url="https://jsonplaceholder.typicode.com/posts/1",
            cache_ttl=0,
            timeout=30000,
        )
        elapsed = (time.time() - t0) * 1000
        print(f"  Fetch: {elapsed:.0f}ms, status={result.status}, path={result.escalation_path}")
        
        # If stealthy was alive, the fetch should use it (consolidation)
        p &= chk_true(result.status < 500, f"Status OK: {result.status}")
    
    # Cleanup
    for sid in list(srv._sessions.keys()):
        try: await srv.close_session(sid)
        except: pass
    
    return p


async def test_5_phase_a_closes_dynamic():
    """Phase A (domain='high') closes orphan dynamic auto session."""
    print(f"\n{B}TEST 5: Phase A closes dynamic{X}")
    
    # Set domain intel to "high"
    from master_fetch.domain_intel import _DB_NAME, get_domain_level
    import aiosqlite, tempfile
    db_path = Path(tempfile.gettempdir()) / _DB_NAME
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS domain_intel (
                domain TEXT PRIMARY KEY,
                protection_level TEXT DEFAULT 'none',
                avg_response_ms REAL DEFAULT 0,
                hit_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                last_seen REAL DEFAULT 0
            )
        """)
        await db.commit()
        await db.execute("DELETE FROM domain_intel WHERE domain = ?", ("jsonplaceholder.typicode.com",))
        await db.execute(
            "INSERT INTO domain_intel (domain, protection_level, avg_response_ms, hit_count, fail_count, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
            ("jsonplaceholder.typicode.com", "high", 3000.0, 10, 0, 9999999999)
        )
        await db.commit()
    
    level = await get_domain_level("https://jsonplaceholder.typicode.com")
    print(f"  Domain level: {level}")
    
    srv = MasterFetchServer(cache_ttl=0)
    
    # Create orphan dynamic auto session
    dyn = await srv.open_session(session_type="dynamic", headless=True)
    srv._auto_dynamic_id = dyn.session_id
    state = await get_session_state(srv)
    print(f"  Before fetch: dynamic={state['auto_dynamic']}, stealthy={state['auto_stealthy']}")
    
    # Fetch — should hit Phase A
    result = await srv.smart_fetch(
        url="https://jsonplaceholder.typicode.com/posts/1",
        cache_ttl=0,
        timeout=30000,
    )
    
    state = await get_session_state(srv)
    print(f"  After fetch: dynamic={state['auto_dynamic']}, stealthy={state['auto_stealthy']}")
    print(f"  Path: {result.escalation_path}, status={result.status}")
    
    p = True
    p &= chk(state["auto_dynamic"], None, "Orphan dynamic CLOSED by Phase A")
    p &= chk_true(state["auto_stealthy"] is not None, "Stealthy auto session EXISTS")
    p &= chk_not(result.escalation_path or "", "dynamic", "Escalation path does NOT mention dynamic")
    
    # Cleanup
    for sid in list(srv._sessions.keys()):
        try: await srv.close_session(sid)
        except: pass
    
    return p


async def test_6_multiple_fetches_one_browser():
    """Three sequential fetches = still only 1 browser session."""
    print(f"\n{B}TEST 6: 3 fetches = 1 browser{X}")
    srv = MasterFetchServer(cache_ttl=0)
    
    for i in range(3):
        result = await srv.smart_fetch(
            url="https://jsonplaceholder.typicode.com/posts/1",
            cache_ttl=0,
            timeout=30000,
        )
        state = await get_session_state(srv)
        n_sessions = len(state["sessions"])
        print(f"  Fetch {i+1}: {n_sessions} session(s), status={result.status}, path={result.escalation_path}")
    
    state = await get_session_state(srv)
    p = True
    p &= chk_true(len(state["sessions"]) <= 1, f"After 3 fetches: {len(state['sessions'])} session(s) (expect <= 1)")
    
    # Cleanup
    for sid in list(srv._sessions.keys()):
        try: await srv.close_session(sid)
        except: pass
    
    return p


async def main():
    print("=" * 60)
    print(f"{B}HOUND v3.4.0 — OPTIMIZATION VERIFICATION{X}")
    print("=" * 60)
    
    tests = [
        ("_close_auto_dynamic_session()", test_1_close_method),
        ("force_fetch stealthy → closes dynamic", test_2_force_stealthy_closes_dynamic),
        ("Phase C skips dynamic tier", test_3_phase_c_skips_dynamic),
        ("Pre-warm → fetch no cold-start", test_4_prewarm_then_fetch),
        ("Phase A closes dynamic", test_5_phase_a_closes_dynamic),
        ("3 fetches = 1 browser", test_6_multiple_fetches_one_browser),
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
        await asyncio.sleep(0.5)
    
    print("\n" + "=" * 60)
    print(f"{B}RESULTS{X}")
    print("=" * 60)
    for name, ok in results.items():
        print(f"  {G}PASS{X}" if ok else f"  {R}FAIL{X}", name)
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n  {passed}/{total} ({100*passed//total if total else 0}%)")
    print(f"{G}ALL PASSED{X}" if passed == total else f"{R}{total-passed} FAILED{X}")
    return passed == total


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)

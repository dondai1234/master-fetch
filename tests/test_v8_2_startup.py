"""v8.2 startup-robustness tests.

The recurring 'hound failed to load' (50% of the time) was caused by heavy
module-level imports (trafilatura, the metasearch engine chain, mcp.server.
fastmcp) blocking the process for ~5s BEFORE the MCP initialize handshake could
respond — cold starts exceeded the client timeout. v8.2 defers all of them to
first use so the server starts in <1s and the handshake responds immediately.
Also: the browser prewarm is fully isolated (BaseException + timeout) and
shutdown is bulletproof (no 'Event loop is closed' crash).
"""

import asyncio
import subprocess
import sys
import time

import pytest

from master_fetch.server import _safe_imported_prewarm, _safe_prewarm


# ─── heavy imports are deferred (the core fix) ───────────────────────────────

def test_server_module_import_does_not_eagerly_load_heavy_deps():
    """import master_fetch.server must NOT pull in trafilatura, the metasearch
    engine chain, or mcp.server.fastmcp — those are ~4s of import cost that
    blocked the MCP handshake. Checked in a FRESH subprocess (this test process
    has already imported them via other tests)."""
    code = (
        "import sys; import master_fetch.server; "
        "print('|'.join(str(x) for x in ("
        "'trafilatura' in sys.modules, "
        "'master_fetch.search_metasearch' in sys.modules, "
        "'master_fetch.search_engines' in sys.modules, "
        "'master_fetch.trafilatura_extractor' in sys.modules, "
        "'mcp.server.fastmcp' in sys.modules)))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"server import failed: {out.stderr[-400:]}"
    parts = [p == "True" for p in out.stdout.strip().split("|")]
    traf, metasearch, search_eng, traf_ext, fastmcp = parts
    assert traf is False, "trafilatura must be lazy (it was eagerly imported)"
    assert metasearch is False, "search_metasearch must be lazy (it was eagerly imported)"
    assert search_eng is False, "search_engines must be lazy (it was eagerly imported)"
    assert traf_ext is False, "trafilatura_extractor must be lazy (it was eagerly imported)"
    assert fastmcp is False, "mcp.server.fastmcp must be lazy (it was eagerly imported)"


def test_server_import_is_fast():
    """The cold import of master_fetch.server must be well under the old 5.45s
    (target < 2s) so the MCP handshake responds before client timeouts."""
    code = "import time; t=time.time(); import master_fetch.server; print(time.time()-t)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-300:]
    dt = float(out.stdout.strip())
    assert dt < 2.0, f"server import too slow: {dt:.2f}s (was 5.45s pre-v8.2; target <2s)"


def test_search_import_does_not_eagerly_load_live_search_backend():
    """import master_fetch.search is needed for cache hits and validation errors;
    it must not load the heavy live-search or reranker backend until needed."""
    code = (
        "import sys; import master_fetch.search; "
        "print('|'.join(str(x) for x in ("
        "'master_fetch.search_metasearch' in sys.modules, "
        "'master_fetch.reranker' in sys.modules, "
        "'bs4' in sys.modules, "
        "'trafilatura' in sys.modules, "
        "'onnxruntime' in sys.modules)))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"search import failed: {out.stderr[-400:]}"
    metasearch, reranker, bs4, traf, onnx = [p == "True" for p in out.stdout.strip().split("|")]
    assert metasearch is False, "search_metasearch must be lazy for cached/invalid searches"
    assert reranker is False, "reranker must be lazy for cached/invalid searches"
    assert bs4 is False, "bs4 must be lazy until find_similar/source extraction"
    assert traf is False, "trafilatura must be lazy until find_similar/source extraction"
    assert onnx is False, "onnxruntime must never load during search module import"


# ─── prewarm isolation ───────────────────────────────────────────────────────

def test_safe_prewarm_swallows_exceptions():
    """A prewarm that raises must NEVER propagate (it can't crash the server)."""
    async def boom():
        raise RuntimeError("browser launch failed")
    asyncio.run(_safe_prewarm(boom))  # no exception propagated


def test_safe_prewarm_swallows_base_exception():
    """Even a BaseException (e.g. CancelledError) from the prewarm must not
    propagate — this is the class of error that could crash the event loop."""
    async def boom():
        raise asyncio.CancelledError()
    asyncio.run(_safe_prewarm(boom))  # no exception propagated


def test_safe_prewarm_caps_hung_launch():
    """A prewarm that hangs must be capped at the timeout so it can't hold a
    lock / linger forever."""
    async def hang():
        await asyncio.sleep(30)
    t0 = time.time()
    asyncio.run(_safe_prewarm(hang, timeout=0.2))
    dt = time.time() - t0
    assert dt < 1.5, f"hung prewarm was not capped: {dt:.2f}s"


def test_safe_prewarm_runs_normal_callable():
    """A normal prewarm completes and its result path works."""
    ran = []
    async def ok():
        ran.append(1)
    asyncio.run(_safe_prewarm(ok))
    assert ran == [1]


def test_safe_imported_prewarm_resolves_import_off_event_loop(monkeypatch):
    """Optional prewarm imports can be slow; resolving them must not starve the
    MCP handshake event loop while they import."""
    import importlib

    ran = []

    class FakeModule:
        @staticmethod
        async def prewarm():
            ran.append(1)

    def slow_import(name):
        assert name == "fake_heavy_module"
        time.sleep(0.25)
        return FakeModule

    monkeypatch.setattr(importlib, "import_module", slow_import)

    async def main():
        task = asyncio.create_task(_safe_imported_prewarm("fake_heavy_module", "prewarm", timeout=1.0))
        ticks = 0
        while not task.done():
            ticks += 1
            await asyncio.sleep(0.02)
        await task
        return ticks

    ticks = asyncio.run(main())
    assert ran == [1]
    assert ticks >= 3, "slow import ran on the event loop instead of a worker thread"

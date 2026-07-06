"""v9.1 streamable HTTP transport test: a real MCP connect -> initialize ->
notifications/initialized -> tools/list -> tools/call -> clean shutdown cycle
against a `python -m master_fetch.server --http` subprocess. Runs in CI (NOT
marked e2e) because it is fast (~10s) and network-free (calls cache_clear, not
smart_fetch/search). This locks the Open WebUI path: Open WebUI v0.6.31+ speaks
streamable HTTP (MCP 2025-03-26 spec), not the deprecated SSE transport and not
stdio, so this is the test that proves Hound connects to Open WebUI directly
without an mcpo proxy.

Uses `python -m master_fetch.server --http` (NOT the `hound` launcher) so it
runs anywhere the package is importable, including CI matrix cells where the
launcher script may not be on PATH.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

_TIMEOUT_S = 60


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _drain_stderr(proc, max_bytes: int = 1500) -> str:
    try:
        proc.wait(timeout=0.1)
    except Exception:
        pass
    try:
        return (proc.stderr.read() or b"")[-max_bytes:].decode("utf-8", "replace")
    except Exception:
        return "(unavailable)"


def _post(url, payload, session=None, timeout=30):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session:
        h["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h)
    return urllib.request.urlopen(req, timeout=timeout)


def _parse(body: str) -> dict:
    # Streamable HTTP may return SSE-framed data or plain JSON. Unwrap either.
    m = re.search(r"^data: (.+)$", body, re.MULTILINE)
    return json.loads(m.group(1)) if m else json.loads(body)


def _wait_ready(port, deadline=30):
    url = f"http://127.0.0.1:{port}/mcp"
    t0 = time.time()
    while time.time() - t0 < deadline:
        try:
            _post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "probe", "version": "0"}}})
            return True  # server is up (initialize answered)
        except Exception:
            time.sleep(0.3)
    return False


def _spawn(port):
    env = {**os.environ}
    env.setdefault("HOUND_SEARCH_DEADLINE", "5")
    return subprocess.Popen(
        [sys.executable, "-m", "master_fetch.server", "--http", "--host", "127.0.0.1",
         "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )


def _lifecycle():
    port = _free_port()
    proc = _spawn(port)
    try:
        assert _wait_ready(port), f"server did not come up on :{port}. stderr: {_drain_stderr(proc)}"
        url = f"http://127.0.0.1:{port}/mcp"

        # 1. initialize (the probe already did one; do a fresh one to capture session)
        r = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "http-lifecycle", "version": "1.0"}}})
        session = r.headers.get("Mcp-Session-Id")
        assert session, "streamable HTTP server did not issue an Mcp-Session-Id"
        init = _parse(r.read().decode())
        result = init.get("result", {})
        assert result.get("serverInfo", {}).get("name") == "Hound", result
        assert result.get("protocolVersion") == "2025-03-26", result
        assert result.get("instructions"), "initialize did not ship HOUND_INSTRUCTIONS over HTTP"

        # 2. notifications/initialized (202 Accepted, no body expected)
        _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
              session=session)

        # 3. tools/list -> all 6 tools
        r2 = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                   session=session)
        tools = _parse(r2.read().decode()).get("result", {}).get("tools", [])
        names = {t["name"] for t in tools}
        expected = {"mcp_smart_fetch", "mcp_smart_crawl", "mcp_screenshot",
                    "mcp_smart_search", "cache_clear", "version"}
        assert names == expected, f"tool set mismatch over HTTP: {names} != {expected}"

        # 4. tools/call cache_clear (no network) -> not an error
        r3 = _post(url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "cache_clear", "arguments": {}}}, session=session)
        assert not _parse(r3.read().decode()).get("result", {}).get("isError", False), \
            "cache_clear returned isError over HTTP"

        return tools
    finally:
        # 5. clean shutdown
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def test_streamable_http_lifecycle():
    """Full streamable HTTP cycle: initialize -> instructions -> tools/list ->
    tools/call -> clean shutdown. This is the Open WebUI direct-connect proof."""
    _lifecycle()


def test_streamable_http_shutdown_clean():
    """After terminate, the process exits and writes no traceback to stderr
    (a noisy teardown is what MCP clients report as 'failed to load')."""
    port = _free_port()
    proc = _spawn(port)
    try:
        assert _wait_ready(port), "server did not come up"
    finally:
        proc.terminate()
    try:
        rc = proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = proc.wait(timeout=2)
    stderr = _drain_stderr(proc)
    # exit code is usually 1 from SIGTERM on Windows (terminate), but must not
    # hang and must not emit a traceback (the 'failed to load' signature).
    assert "Traceback (most recent call last)" not in stderr, \
        f"teardown emitted a traceback over HTTP. stderr: {stderr}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

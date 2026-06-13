"""End-to-end MCP protocol tests — RUN MANUALLY, not in CI.

These tests spawn a real `hound.exe` subprocess as a stdio MCP server
and verify the wire protocol against it. They are NOT pytest-discovered
on CI because:
  - Spawning hound per test in 6 matrix cells costs ~1 minute per cell.
  - The subprocess side-effects (live HTTP fetches) cost time and money.
  - Unit tests in test_server.py already cover the underlying handlers
    with mocks and are the canonical regression net.

Run manually:
    pytest -m e2e tests/e2e_mcp_test.py -v
Or run as a smoke script:
    python tests/e2e_mcp_test.py

If a regression lands in the MCP stdio wire protocol, run the e2e
tests against the local hound binary before tagging a release.
"""
import json
import subprocess
import sys
import time

import pytest

# All tests in this module are tagged `e2e`. CI default conftest
# skips them via `addopts = "-m 'not e2e'"` in pyproject.toml.
pytestmark = pytest.mark.e2e

# Safety net: cap consecutive empty readlines. The MCP server, when
# happy, returns a newline-terminated JSON line on the first readline.
# When it crashes or emits a non-JSON banner before our initialize,
# readline can return "" repeatedly. Without this cap the test hangs
# indefinitely waiting for data that will never arrive.
_MAX_EMPTY_READS = 50


class MCPClient:
    def __init__(self):
        self.proc = subprocess.Popen(
            ["hound"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**__import__("os").environ, "TINYFISH_API_KEY": ""},
        )
        self._id = 0
        self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "e2e-test", "version": "1.0"},
        })

    def _send(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params:
            msg["params"] = params
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()
        return self._read_line_json()

    def _read_line_json(self):
        """Read one newline-terminated JSON line. Bounded by _MAX_EMPTY_READS."""
        empty_count = 0
        while True:
            # If the server died, EOF returns "" forever. Detect that
            # immediately by checking poll() before even trying to read.
            if self.proc.poll() is not None:
                tail = _drain_stderr(self.proc)
                raise RuntimeError(
                    f"Server exited (code {self.proc.returncode}) mid-call. "
                    f"stderr tail: {tail}"
                )
            line = self.proc.stdout.readline()
            if not line:
                empty_count += 1
                if empty_count > _MAX_EMPTY_READS:
                    tail = _drain_stderr(self.proc)
                    raise RuntimeError(
                        f"Server produced {_MAX_EMPTY_READS} consecutive "
                        f"empty readlines. Aborting. stderr tail: {tail}"
                    )
                # Brief sleep so a dead server is detected via poll()
                # instead of purely via OS readline blocking on a closed pipe.
                time.sleep(0.05)
                continue
            text = line.decode().strip()
            if not text:
                # Blank line — JSON-RPC servers tend to emit these only
                # on broken pipes. Keep going but count it.
                empty_count += 1
                if empty_count > _MAX_EMPTY_READS:
                    raise RuntimeError("Too many empty lines.")
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Server emitted non-JSON line: {text[:200]!r} ({exc}). "
                    f"stderr: {_drain_stderr(self.proc)}"
                )

    def call_tool(self, name, args):
        return self._send("tools/call", {"name": name, "arguments": args})

    def list_tools(self):
        return self._send("tools/list", {})

    def close(self):
        if self.proc.poll() is not None:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _drain_stderr(proc, max_bytes: int = 2000) -> str:
    """Best-effort stub drain so an error message can include server stderr."""
    try:
        buf = []
        start = time.monotonic()
        while time.monotonic() - start < 0.2:
            if proc.stderr is None:
                break
            chunk = proc.stderr.read1(1024)
            if not chunk:
                break
            buf.append(chunk)
        return b"".join(buf)[-max_bytes:].decode("utf-8", errors="replace")
    except Exception:
        return "(unavailable)"


@pytest.fixture
def mcp():
    client = MCPClient()
    try:
        yield client
    finally:
        client.close()


def test_tool_definitions(mcp):
    result = mcp.list_tools()
    tools = result.get("result", {}).get("tools", [])
    tool_map = {t["name"]: t for t in tools}

    fetch_desc = tool_map["mcp_smart_fetch"]["description"]
    assert "EXTRACTED text" in fetch_desc or "extracted text" in fetch_desc
    assert "offset" in fetch_desc.lower() and "html" in fetch_desc.lower()

    offset_desc = tool_map["mcp_smart_fetch"]["inputSchema"]["properties"]["offset"]["description"]
    assert "total_extracted_chars" in offset_desc

    assert "cache" in tool_map["mcp_smart_search"]["description"].lower() or \
        "cached" in tool_map["mcp_smart_search"]["description"].lower()

    cache_desc = tool_map["cache_clear"]["description"]
    assert "TTL" in cache_desc or "ttl" in cache_desc or "cache stores" in cache_desc.lower()

    assert "version" in tool_map


def test_smart_fetch_response_fields(mcp):
    resp = mcp.call_tool("mcp_smart_fetch", {"url": "https://example.com", "cache_ttl": 0})
    data = _extract_data(resp.get("result", {}))
    assert data.get("total_extracted_chars", 0) > 0
    is_trunc = data.get("is_truncated", False)
    next_off = data.get("next_offset", 0)
    if is_trunc:
        assert next_off > 0
    else:
        assert next_off == 0


def test_version_tool(mcp):
    """Reports the version that is *installed* on this machine.

    Pre-fix hardcoded `"3.3.1"` and broke on every release since. Now it
    reads `importlib.metadata.version("hound-mcp")` and asserts the server
    matches — the actual contract we care about.
    """
    import importlib.metadata
    installed = importlib.metadata.version("hound-mcp")

    resp = mcp.call_tool("version", {})
    data = _extract_data(resp.get("result", {}))
    assert data.get("version") == installed, (
        f"Server reports {data.get('version')!r}, package on disk is {installed!r}"
    )


def test_error_response_format(mcp):
    resp = mcp.call_tool("mcp_smart_fetch", {})
    assert resp.get("result", {}).get("isError", False) is True

    resp2 = mcp.call_tool("nonexistent_tool_xyz", {})
    assert resp2.get("result", {}).get("isError", False) is True


def test_bulk_url_limit(mcp):
    fake_urls = [f"https://example{i}.com" for i in range(101)]
    resp = mcp.call_tool("mcp_smart_fetch", {"urls": fake_urls})
    result = resp.get("result", {})
    assert result.get("isError", False) is True, \
        f"101 URLs should trigger error. isError={result.get('isError')}"


def _extract_data(result: dict) -> dict:
    if "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content", [])
    if content:
        try:
            return json.loads(content[0].get("text", ""))
        except Exception:
            pass
    return {}


if __name__ == "__main__":
    print("End-to-end MCP protocol tests for Hound\n")
    print("(Subprocess-per-test; pass 1+/cell takes ~30s)\n")
    exit_code = pytest.main([__file__, "-v", "-x"])
    sys.exit(exit_code)

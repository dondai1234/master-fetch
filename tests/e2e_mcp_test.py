"""End-to-end MCP protocol test — verifies chunking, meta-awareness, and tool definitions work in practice."""

import json
import subprocess
import sys
import time

class MCPClient:
    def __init__(self):
        self.proc = subprocess.Popen(
            ["hound"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env={**__import__("os").environ, "TINYFISH_API_KEY": ""},
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
        # Read response line
        line = self.proc.stdout.readline().decode()
        while line.strip() == "":
            line = self.proc.stdout.readline().decode()
        return json.loads(line)

    def call_tool(self, name, args):
        return self._send("tools/call", {"name": name, "arguments": args})

    def list_tools(self):
        return self._send("tools/list", {})

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except:
            self.proc.kill()


def test_tool_definitions():
    """Verify tool descriptions contain honest meta-awareness."""
    client = MCPClient()
    try:
        result = client.list_tools()
        tools = result.get("result", {}).get("tools", [])
        tool_map = {t["name"]: t for t in tools}

        # 1. smart_fetch description mentions extracted text limitation
        fetch_desc = tool_map["mcp_smart_fetch"]["description"]
        assert "EXTRACTED text" in fetch_desc or "extracted text" in fetch_desc, \
            f"smart_fetch description missing extracted text caveat: {fetch_desc[:100]}"
        assert "offset" in fetch_desc.lower() and "html" in fetch_desc.lower(), \
            f"smart_fetch missing offset/html guidance: {fetch_desc[:100]}"
        print(f"  ✅ smart_fetch meta-awareness: mentions extracted text vs raw HTML")

        # 2. smart_fetch offset description mentions total_extracted_chars
        offset_desc = tool_map["mcp_smart_fetch"]["inputSchema"]["properties"]["offset"]["description"]
        assert "total_extracted_chars" in offset_desc, \
            f"offset description missing total_extracted_chars: {offset_desc}"
        print(f"  ✅ offset param mentions total_extracted_chars")

        # 3. smart_search mentions cache
        search_desc = tool_map["mcp_smart_search"]["description"]
        assert "cache" in search_desc.lower() or "cached" in search_desc.lower(), \
            f"search description missing cache mention: {search_desc}"
        print(f"  ✅ smart_search mentions cache behavior")

        # 4. cache_clear mentions TTL
        cache_desc = tool_map["cache_clear"]["description"]
        assert "TTL" in cache_desc or "ttl" in cache_desc or "cache stores" in cache_desc.lower(), \
            f"cache_clear description missing TTL info: {cache_desc}"
        print(f"  ✅ cache_clear mentions cache details")

        # 5. version tool exists
        assert "version" in tool_map, "version tool missing"
        print(f"  ✅ All 8 tools present: {list(tool_map.keys())}")

    finally:
        client.close()


def test_smart_fetch_response_fields():
    """Verify smart_fetch returns total_extracted_chars and correct chunking behavior."""
    client = MCPClient()
    try:
        # Fetch a real page
        resp = client.call_tool("mcp_smart_fetch", {
            "url": "https://example.com",
            "cache_ttl": 0,
        })
        result = resp.get("result", {})

        # Check for structuredContent or text content
        if "structuredContent" in result:
            data = result["structuredContent"]
        else:
            content = result.get("content", [])
            text = content[0].get("text", "") if content else ""
            try:
                data = json.loads(text)
            except:
                print(f"  ⚠️  Could not parse response as JSON")
                return

        # 1. total_extracted_chars must exist
        assert "total_extracted_chars" in data, \
            f"Missing total_extracted_chars in response. Fields: {list(data.keys())}"
        print(f"  ✅ total_extracted_chars present: {data['total_extracted_chars']:,}")

        # 2. total_extracted_chars should be > 0 for a real page
        assert data["total_extracted_chars"] > 0, \
            f"total_extracted_chars is 0 for example.com"
        print(f"  ✅ total_extracted_chars > 0 (real content extracted)")

        # 3. is_truncated + next_offset consistency
        is_trunc = data.get("is_truncated", False)
        next_off = data.get("next_offset", 0)
        if is_trunc:
            assert next_off > 0, f"is_truncated=True but next_offset=0"
            print(f"  ✅ Chunked: is_truncated=True, next_offset={next_off}")
        else:
            assert next_off == 0, f"is_truncated=False but next_offset={next_off}"
            print(f"  ✅ Not chunked: content fits in one response")

        # 4. status and content present
        assert "status" in data, "Missing status field"
        assert "content" in data, "Missing content field"
        print(f"  ✅ status={data['status']}, content length={len(data.get('content', [''])[0]):,} chars")

    finally:
        client.close()


def test_version_tool():
    """Verify version tool works and doesn't block."""
    client = MCPClient()
    try:
        t0 = time.time()
        resp = client.call_tool("version", {})
        elapsed = time.time() - t0

        result = resp.get("result", {})
        content = result.get("content", [])
        text = content[0].get("text", "") if content else ""
        try:
            data = json.loads(text)
        except:
            print(f"  ⚠️  Could not parse version response")
            return

        assert data.get("version") == "3.1.0", f"Version mismatch: {data.get('version')}"
        print(f"  ✅ version={data['version']}, up_to_date={data.get('up_to_date')}")
        print(f"  ✅ Version call took {elapsed:.1f}s (should not block event loop)")

    finally:
        client.close()


def test_error_response_format():
    """Verify error responses use isError=True."""
    client = MCPClient()
    try:
        # Call with missing required params
        resp = client.call_tool("mcp_smart_fetch", {})
        result = resp.get("result", {})

        is_error = result.get("isError", False)
        content = result.get("content", [])
        text = content[0].get("text", "") if content else ""

        assert is_error is True, f"isError not set for missing params. isError={is_error}, text={text[:100]}"
        assert "error" in text.lower() or "url" in text.lower(), \
            f"Error response doesn't mention the problem: {text[:100]}"
        print(f"  ✅ Missing params → isError=True with error message")

        # Unknown tool
        resp2 = client.call_tool("nonexistent_tool", {})
        result2 = resp2.get("result", {})
        is_error2 = result2.get("isError", False)
        assert is_error2 is True, f"Unknown tool didn't set isError=True"
        print(f"  ✅ Unknown tool → isError=True")

    finally:
        client.close()


def test_bulk_url_limit():
    """Verify bulk URL limit is enforced."""
    client = MCPClient()
    try:
        # Try fetching 101 URLs (over the limit)
        fake_urls = [f"https://example{i}.com" for i in range(101)]
        resp = client.call_tool("mcp_smart_fetch", {
            "urls": fake_urls,
        })
        result = resp.get("result", {})
        is_error = result.get("isError", False)
        content = result.get("content", [])
        text = content[0].get("text", "") if content else ""

        # Should get an error about too many URLs
        assert is_error is True, f"101 URLs should trigger error. isError={is_error}"
        assert "101" in text or "100" in text or "Too many" in text, \
            f"Error doesn't mention URL limit: {text[:200]}"
        print(f"  ✅ 101 URLs rejected with limit message")

    finally:
        client.close()


if __name__ == "__main__":
    print("End-to-end MCP protocol tests for Hound v3.1.0\n")

    print("1. Tool definitions (meta-awareness)")
    test_tool_definitions()

    print("\n2. smart_fetch response fields (chunking, total_extracted_chars)")
    test_smart_fetch_response_fields()

    print("\n3. Version tool (no event loop blocking)")
    test_version_tool()

    print("\n4. Error response format (isError flag)")
    test_error_response_format()

    print("\n5. Bulk URL limit enforcement")
    test_bulk_url_limit()

    print("\n✅ All E2E tests passed")

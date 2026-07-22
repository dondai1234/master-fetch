"""Head-to-head: Exa Search vs Hound MCP search + fetch.

Tests:
1. Search speed: same query on both, measure wall time + output size (token cost proxy)
2. Search result quality: compare what each returns
3. Exa extract/contents vs Hound fetch on same URL
4. Hound parallel search speed (10 backends simultaneously)
5. Token cost analysis: Exa 10 results with long snippets vs Hound small snippets
"""
import asyncio
import time
import json
import sys
import os

# ─── Exa Search via REST API ─────────────────────────────────────
EXA_API_KEY = "635318af-beee-4b86-a7e7-7b1e58dbfb2e"

async def exa_search(query, num_results=10, use_autoprompt=True, contents=False):
    """Call Exa search API directly via httpx."""
    import httpx
    url = "https://api.exa.ai/search"
    headers = {"x-api-key": EXA_API_KEY, "Content-Type": "application/json"}
    payload = {
        "query": query,
        "numResults": num_results,
        "useAutoprompt": use_autoprompt,
        "type": "neural",
    }
    if contents:
        payload["contents"] = {
            "text": {"maxCharacters": 1000},
        }
    start = time.time()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
        elapsed = time.time() - start
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}", "elapsed": elapsed}
        data = resp.json()
        data["elapsed"] = elapsed
        return data

async def exa_extract(url_to_extract):
    """Call Exa contents/extract API."""
    import httpx
    api_url = "https://api.exa.ai/contents"
    headers = {"x-api-key": EXA_API_KEY, "Content-Type": "application/json"}
    payload = {
        "ids": [url_to_extract],
        "text": {"maxCharacters": 5000},
    }
    start = time.time()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(api_url, json=payload, headers=headers)
        elapsed = time.time() - start
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}", "elapsed": elapsed}
        data = resp.json()
        data["elapsed"] = elapsed
        return data


# ─── Hound MCP search ───────────────────────────────────────────
async def hound_search(query, max_results=10):
    """Call Hound's smart_search."""
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    start = time.time()
    result = await srv.smart_search(query=query, max_results=max_results)
    elapsed = time.time() - start
    return result, elapsed

async def hound_fetch(url_to_fetch):
    """Call Hound's smart_fetch."""
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    start = time.time()
    result = await srv.smart_fetch(url=url_to_fetch, cache_ttl=0, timeout=30000, max_content_chars=5000)
    elapsed = time.time() - start
    return result, elapsed


async def hound_search_parallel(query, max_results=10):
    """Hound parallel search (same as default - it already fires 10 backends in parallel)."""
    from master_fetch.server import MasterFetchServer
    srv = MasterFetchServer()
    start = time.time()
    result = await srv.smart_search(query=query, max_results=max_results)
    elapsed = time.time() - start
    return result, elapsed


async def main():
    print("=" * 70)
    print("EXA vs HOUND - HEAD TO HEAD")
    print("=" * 70)

    # ─── TEST 1: Search speed + quality ───────────────────────────
    queries = [
        "python asyncio best practices 2024",
        "cloudflare turnstile bypass techniques",
        "rust memory management vs garbage collection",
    ]

    for query in queries:
        print(f"\n{'=' * 70}")
        print(f"QUERY: '{query}'")
        print(f"{'=' * 70}")

        # Exa search (no contents)
        print("\n--- EXA SEARCH (snippets only) ---")
        exa_snip = await exa_search(query, num_results=10, contents=False)
        if "error" in exa_snip:
            print(f"  ERROR: {exa_snip['error']}")
            print(f"  Time: {exa_snip.get('elapsed', 0):.2f}s")
        else:
            results = exa_snip.get("results", [])
            total_chars = sum(len(r.get("text", "") or r.get("highlight", "")) for r in results)
            total_url_chars = sum(len(r.get("url", "")) + len(r.get("title", "")) for r in results)
            print(f"  Time: {exa_snip.get('elapsed', 0):.2f}s")
            print(f"  Results: {len(results)}")
            print(f"  Autoprompt: {exa_snip.get('autopromptString', 'N/A')}")
            print(f"  Total snippet chars: {total_chars}")
            print(f"  Total URL+title chars: {total_url_chars}")
            print(f"  Total output chars (proxy for tokens): {total_chars + total_url_chars}")
            for i, r in enumerate(results[:3]):
                print(f"  [{i+1}] {r.get('title', 'no title')[:60]}")
                print(f"      URL: {r.get('url', '')[:70]}")
                print(f"      Text: {(r.get('text', '') or r.get('highlight', ''))[:100]}")
            print(f"  ... ({len(results) - 3} more results)")

        # Exa search (with contents - what the commenter is talking about)
        print("\n--- EXA SEARCH (with contents/extract, max 1000 chars/result) ---")
        exa_content = await exa_search(query, num_results=10, contents=True)
        if "error" in exa_content:
            print(f"  ERROR: {exa_content['error']}")
            print(f"  Time: {exa_content.get('elapsed', 0):.2f}s")
        else:
            results = exa_content.get("results", [])
            total_chars = sum(len(r.get("text", "")) for r in results)
            total_url_chars = sum(len(r.get("url", "")) + len(r.get("title", "")) for r in results)
            print(f"  Time: {exa_content.get('elapsed', 0):.2f}s")
            print(f"  Results: {len(results)}")
            print(f"  Total content chars: {total_chars}")
            print(f"  Total URL+title chars: {total_url_chars}")
            print(f"  Total output chars (proxy for tokens): {total_chars + total_url_chars}")
            for i, r in enumerate(results[:3]):
                print(f"  [{i+1}] {r.get('title', 'no title')[:60]}")
                print(f"      URL: {r.get('url', '')[:70]}")
                print(f"      Text: {r.get('text', '')[:100]}")
            print(f"  ... ({len(results) - 3} more results)")

        # Hound search
        print("\n--- HOUND SEARCH (10 parallel backends, neural reranking) ---")
        hound_result, hound_elapsed = await hound_search(query, max_results=10)
        print(f"  Time: {hound_elapsed:.2f}s")
        # Hound returns results with short descriptions
        if hasattr(hound_result, 'results') and hound_result.results:
            total_hound_chars = 0
            for i, r in enumerate(hound_result.results):
                snip_len = len(r.snippet or "")
                url_len = len(r.url or "")
                title_len = len(r.title or "")
                total_hound_chars += snip_len + url_len + title_len
                if i < 3:
                    print(f"  [{i+1}] {(r.title or 'no title')[:60]}")
                    print(f"      URL: {(r.url or '')[:70]}")
                    print(f"      Snippet: {(r.snippet or '')[:100]}")
                    print(f"      Score: {r.relevance_score or 'N/A'} | Engines: {r.engines_consensus or 'N/A'}")
            print(f"  ... ({len(hound_result.results) - 3} more results)")
            print(f"  Total output chars (proxy for tokens): {total_hound_chars}")
            print(f"  Engines used: {hound_result.engines_used if hasattr(hound_result, 'engines_used') else '10 parallel'}")
        else:
            print(f"  No results or error: {hound_result}")
            print(f"  Result type: {type(hound_result)}")

    # ─── TEST 2: Exa extract vs Hound fetch ───────────────────────
    print(f"\n{'=' * 70}")
    print("EXA EXTRACT vs HOUND FETCH")
    print(f"{'=' * 70}")

    test_urls = [
        "https://docs.python.org/3/library/asyncio-task.html",
        "https://blog.rust-lang.org/2024/05/02/Rust-2024.html",
    ]

    for url in test_urls:
        print(f"\n--- URL: {url} ---")

        # Exa extract
        print("\n  EXA EXTRACT (max 5000 chars):")
        exa_ext = await exa_extract(url)
        if "error" in exa_ext:
            print(f"    ERROR: {exa_ext['error']}")
            print(f"    Time: {exa_ext.get('elapsed', 0):.2f}s")
        else:
            results = exa_ext.get("results", [])
            if results:
                text = results[0].get("text", "")
                print(f"    Time: {exa_ext.get('elapsed', 0):.2f}s")
                print(f"    Title: {results[0].get('title', 'N/A')[:60]}")
                print(f"    Text length: {len(text)} chars")
                print(f"    First 150 chars: {text[:150]}")
            else:
                print(f"    No results. Full: {json.dumps(exa_ext)[:200]}")

        # Hound fetch
        print("\n  HOUND FETCH (max 5000 chars):")
        hound_res, hound_time = await hound_fetch(url)
        content = hound_res.content[0] if hound_res.content else ""
        print(f"    Time: {hound_time:.2f}s")
        print(f"    Status: {hound_res.status} | OK: {hound_res.content_ok}")
        print(f"    Fetcher: {hound_res.fetcher_used} | Path: {hound_res.escalation_path}")
        print(f"    Text length: {len(content)} chars")
        print(f"    Summary: {hound_res.summary[:100]}")
        print(f"    First 150 chars: {content[:150]}")
        print(f"    Page type: {hound_res.page_type}")
        print(f"    Content age: {hound_res.content_age_days} days")

    # ─── TEST 3: Token cost comparison summary ────────────────────
    print(f"\n{'=' * 70}")
    print("TOKEN COST ANALYSIS")
    print(f"{'=' * 70}")

    # Re-run one query for detailed comparison
    query = "python asyncio best practices 2024"
    print(f"\nQuery: '{query}'")

    # Exa with contents
    exa_with = await exa_search(query, num_results=10, contents=True)
    if "results" in exa_with:
        exa_total = sum(len(r.get("text", "")) + len(r.get("url", "")) + len(r.get("title", "")) for r in exa_with["results"])
        print(f"\nExa (10 results WITH content, 1000 chars each):")
        print(f"  Total output: {exa_total:,} chars (~{exa_total // 4:,} tokens)")
        print(f"  Time: {exa_with.get('elapsed', 0):.2f}s")

    # Exa without contents
    exa_without = await exa_search(query, num_results=10, contents=False)
    if "results" in exa_without:
        exa_snip_total = sum(len(r.get("text", "") or r.get("highlight", "")) + len(r.get("url", "")) + len(r.get("title", "")) for r in exa_without["results"])
        print(f"\nExa (10 results WITHOUT content, snippets only):")
        print(f"  Total output: {exa_snip_total:,} chars (~{exa_snip_total // 4:,} tokens)")
        print(f"  Time: {exa_without.get('elapsed', 0):.2f}s")

    # Hound
    hound_res, hound_time = await hound_search(query, max_results=10)
    if hasattr(hound_res, 'results') and hound_res.results:
        hound_total = sum(len(r.snippet or "") + len(r.url or "") + len(r.title or "") for r in hound_res.results)
        print(f"\nHound (10 results, small snippets):")
        print(f"  Total output: {hound_total:,} chars (~{hound_total // 4:,} tokens)")
        print(f"  Time: {hound_time:.2f}s")

        # Hound search + ONE targeted fetch (what an agent would actually do)
        best_url = hound_res.results[0].url if hound_res.results[0].url else ""
        if best_url:
            fetch_res, fetch_time = await hound_fetch(best_url)
            fetch_content = fetch_res.content[0] if fetch_res.content else ""
            combined = hound_total + len(fetch_content)
            print(f"\nHound (10 results + ONE targeted fetch of best result):")
            print(f"  Search: {hound_total:,} chars + Fetch: {len(fetch_content):,} chars")
            print(f"  Total output: {combined:,} chars (~{combined // 4:,} tokens)")
            print(f"  Total time: {hound_time + fetch_time:.2f}s")
            print(f"  Fetched URL: {best_url[:60]}")
            print(f"  Fetch status: {fetch_res.status} OK: {fetch_res.content_ok}")

    print(f"\n{'=' * 70}")
    print("DONE")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    asyncio.run(main())

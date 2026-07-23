#!/usr/bin/env python3
"""Sync tool descriptions from server.py to pi-extension hound.ts."""
import re

SERVER = "src/master_fetch/server.py"
HOUND = "pi-extension/extensions/hound.ts"

with open(SERVER, "r", encoding="utf-8") as f:
    server_src = f.read()

with open(HOUND, "r", encoding="utf-8") as f:
    hound_src = f.read()

# Extract the fetch description from server.py
fetch_match = re.search(r'"description": "(Fetch any URL.*?)"', server_src, re.DOTALL)
search_match = re.search(r'"description": "(Keyless web search.*?)"', server_src, re.DOTALL)

if not fetch_match:
    print("ERROR: Could not find fetch description in server.py")
    exit(1)
if not search_match:
    print("ERROR: Could not find search description in server.py")
    exit(1)

fetch_desc_server = fetch_match.group(1)
search_desc_server = search_match.group(1)

# Adapt for hound.ts: replace smart_fetch -> web_fetch, smart_crawl -> web_crawl
fetch_desc_hound = fetch_desc_server.replace("smart_fetch", "web_fetch").replace("smart_crawl", "web_crawl")
search_desc_hound = search_desc_server.replace("smart_fetch", "web_fetch").replace("smart_crawl", "web_crawl")

# Also fix the options description (remove archive_fallback, it was removed in v10.4.1)
old_options = "include_links (bool,false: response.links=citations/navigation/external+primary_source), include_media (bool,false: up to 20 page image URLs), archive_fallback (bool,true: recover from Internet Archive on hard-block; false=raw failure), proxy, cookies, extra_headers, useragent, wait, network_idle, headless, respect_robots, real_chrome/solve_cloudflare/block_webrtc/hide_canvas/main_content_only/use_trafilatura (anti-detect tuning, good defaults, rarely needed)."
new_options = "include_links (bool,false: response.links=citations/navigation/external+primary_source), include_media (bool,false: up to 20 page image URLs), proxy (str|dict), cookies (list), extra_headers (dict), useragent (str), wait (ms,0), network_idle (bool,SPAs), headless (bool,true), respect_robots (bool,false), real_chrome/solve_cloudflare/block_webrtc/hide_canvas/main_content_only/use_trafilatura (anti-detect tuning, good defaults, rarely needed)."

# Replace in hound.ts
# Fetch description
old_fetch = re.search(r'description: "(Fetch any URL.*?)"', hound_src, re.DOTALL)
if old_fetch:
    hound_src = hound_src.replace(old_fetch.group(1), fetch_desc_hound)
    print(f"Updated fetch description ({len(fetch_desc_hound)} chars)")
else:
    print("ERROR: Could not find fetch description in hound.ts")

# Search description
old_search = re.search(r'description: "(Keyless web search.*?)"', hound_src, re.DOTALL)
if old_search:
    hound_src = hound_src.replace(old_search.group(1), search_desc_hound)
    print(f"Updated search description ({len(search_desc_hound)} chars)")
else:
    print("ERROR: Could not find search description in hound.ts")

# Fix options description (remove archive_fallback)
if old_options in hound_src:
    hound_src = hound_src.replace(old_options, new_options)
    print("Updated options description (removed archive_fallback)")
else:
    print("NOTE: archive_fallback options text not found in hound.ts (may already be updated)")

with open(HOUND, "w", encoding="utf-8") as f:
    f.write(hound_src)

print("Done. hound.ts synced.")

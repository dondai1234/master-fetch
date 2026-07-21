/**
 * Hound MCP extension for Pi agent.
 *
 * Six native tools, all routed through a single long-lived Hound MCP stdio
 * subprocess (singleton - Hound's startup prewarm persists for the whole
 * session, zero re-launch cost per call):
 *   - web_fetch      -> mcp_smart_fetch   (auto anti-bot, PDF, archive fallback)
 *   - web_search     -> mcp_smart_search  (10 keyless backends, consensus rank)
 *   - web_crawl      -> mcp_smart_crawl   (best-first + sitemap-one-fetch map)
 *   - web_screenshot -> mcp_screenshot    (image capture for multimodal agents)
 *   - cache_clear    -> cache_clear        (clear fetch cache)
 *   - hound_version  -> version            (hound version + update status)
 *
 * Hound is fully keyless ($0, no API key, no account). The subprocess is
 * resolved from PATH so it tracks the installed Hound version automatically.
 *
 * Install: pip install hound-mcp[all]
 * Then:    pi install npm:@houndmcp/hound-mcp-pi
 */

import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import { spawn, execSync, type ChildProcess } from "node:child_process";
import * as path from "node:path";
import * as os from "node:os";
import * as fs from "node:fs";

// -- Version (read from package.json at load time) --

function readPkgVersion(): string {
  try {
    const pkgPath = path.join(__dirname, "..", "package.json");
    const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf-8"));
    return pkg.version || "unknown";
  } catch {
    return "unknown";
  }
}

const EXTENSION_VERSION = readPkgVersion();

// -- Hound executable resolution (lazy, re-tried on each ensureReady if null) --

function resolveHoundExe(): string | null {
  // 1. PATH lookup (covers 99% of installs)
  try {
    const cmd = process.platform === "win32" ? "where hound.exe" : "which hound";
    const out = execSync(cmd, {
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 2000,
      windowsHide: true,
    }).toString().trim();
    const first = out.split(/\r?\n/)[0];
    if (first && fs.existsSync(first)) return first;
  } catch {}
  // 2. Fallback: common Windows install paths (multiple Python versions)
  if (process.platform === "win32") {
    const home = os.homedir();
    for (const v of ["Python311", "Python312", "Python313", "Python310"]) {
      const p = path.join(home, "AppData", "Local", "Programs", "Python", v, "Scripts", "hound.exe");
      if (fs.existsSync(p)) return p;
    }
  }
  return null;
}

const INIT_TIMEOUT_MS = 15_000;
const CALL_TIMEOUT_MS = 90_000;
const CRAWL_TIMEOUT_MS = 150_000;
const INIT_ATTEMPTS = 3;
const INIT_BACKOFF_MS = 400;

// -- MCP Stdio Client (singleton subprocess + JSON-RPC) --

interface Pending {
  resolve: (v: any) => void;
  reject: (e: Error) => void;
  timer: NodeJS.Timeout;
}

class HoundClient {
  private proc: ChildProcess | null = null;
  private houndExe: string | null = null;
  private houndVersion: string | null = null;
  private ready: Promise<boolean> | null = null;
  private initInFlight: Promise<boolean> | null = null;
  private nextId = 0;
  private pending = new Map<number, Pending>();
  private stdoutBuf = "";
  private lastStderr = "";

  getExe(): string | null { return this.houndExe; }

  hasExe(): boolean { return this.houndExe !== null; }

  async ensureReady(): Promise<boolean> {
    if (this.ready) return this.ready;
    // Re-resolve hound exe if not found yet (handles install-after-load)
    if (!this.houndExe) {
      this.houndExe = resolveHoundExe();
    }
    if (!this.houndExe) return false;
    if (this.initInFlight) return this.initInFlight;
    const p = this._initWithRetry();
    this.initInFlight = p;
    p.then((ok) => {
      this.initInFlight = null;
      if (ok) this.ready = p;
      else this.ready = null;
    });
    return p;
  }

  private async _initWithRetry(): Promise<boolean> {
    for (let attempt = 1; attempt <= INIT_ATTEMPTS; attempt++) {
      if (await this._initOnce()) return true;
      if (attempt < INIT_ATTEMPTS) await new Promise((r) => setTimeout(r, INIT_BACKOFF_MS));
    }
    return false;
  }

  private _initOnce(): Promise<boolean> {
    // Kill any existing process before spawning a new one (prevents orphans)
    if (this.proc && !this.proc.killed) {
      try { this.proc.stdin?.end(); } catch {}
      try { this.proc.kill(); } catch {}
    }
    this.proc = null;

    return new Promise<boolean>((resolve) => {
      let settled = false;
      let initTimer: NodeJS.Timeout | undefined;
      let stderrBuf = "";
      const done = (ok: boolean) => {
        if (settled) return;
        settled = true;
        if (initTimer) clearTimeout(initTimer);
        if (!ok) this._killProcess();
        resolve(ok);
      };
      try {
        this.proc = spawn(this.houndExe!, [], {
          stdio: ["pipe", "pipe", "pipe"],
          windowsHide: true,
          env: { ...process.env },
        });
      } catch (e: any) {
        this.lastStderr = `spawn threw: ${e?.message ?? e}`;
        done(false);
        return;
      }
      this.proc.on("error", (e: any) => {
        this.lastStderr = `spawn error: ${e?.message ?? e}`;
        done(false);
      });
      this.proc.on("close", (code, signal) => {
        for (const [, { reject, timer }] of this.pending) {
          clearTimeout(timer);
          reject(new Error("Hound closed"));
        }
        this.pending.clear();
        if (!settled) this.lastStderr = `exited before init (code=${code} signal=${signal})`;
        this.ready = null;
        done(false);
      });
      this.proc.stderr?.on("data", (chunk: Buffer) => {
        stderrBuf += chunk.toString("utf-8");
        if (stderrBuf.length > 4000) stderrBuf = stderrBuf.slice(-4000);
      });
      this.proc.stdout?.on("data", (chunk: Buffer) => {
        this.stdoutBuf += chunk.toString("utf-8");
        this._drain();
      });
      initTimer = setTimeout(() => {
        this.lastStderr = `initialize timed out after ${INIT_TIMEOUT_MS}ms (dead spawn)`;
        done(false);
      }, INIT_TIMEOUT_MS);
      const id = ++this.nextId;
      this.pending.set(id, {
        resolve: () => {
          this._notify("notifications/initialized", {});
          this.lastStderr = "";
          done(true);
        },
        reject: (e: Error) => {
          this.lastStderr = `initialize rejected: ${e.message}`;
          done(false);
        },
        timer: initTimer,
      });
      try {
        this.proc.stdin!.write(
          JSON.stringify({
            jsonrpc: "2.0",
            method: "initialize",
            params: {
              protocolVersion: "2025-03-26",
              capabilities: {},
              clientInfo: { name: "pi-hound", version: EXTENSION_VERSION },
            },
            id,
          }) + "\n",
        );
      } catch (e: any) {
        this.pending.delete(id);
        this.lastStderr = `stdin write failed: ${e?.message ?? e}`;
        done(false);
      }
    });
  }

  private _drain() {
    let idx: number;
    while ((idx = this.stdoutBuf.indexOf("\n")) !== -1) {
      const line = this.stdoutBuf.slice(0, idx).trim();
      this.stdoutBuf = this.stdoutBuf.slice(idx + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line);
        if (msg.id != null && this.pending.has(msg.id)) {
          const { resolve, reject, timer } = this.pending.get(msg.id)!;
          this.pending.delete(msg.id);
          clearTimeout(timer);
          if (msg.error) reject(new Error(msg.error?.message ?? JSON.stringify(msg.error)));
          else resolve(msg.result);
        }
      } catch {}
    }
  }

  private _notify(method: string, params: any) {
    if (!this.proc || this.proc.killed) return;
    try {
      this.proc.stdin!.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
    } catch {}
  }

  async call(
    name: string,
    args: Record<string, any>,
    timeoutMs = CALL_TIMEOUT_MS,
    signal?: AbortSignal,
  ): Promise<any> {
    const ready = await this.ensureReady();
    if (!ready) {
      const hint = this.houndExe
        ? `Hound failed to start after ${INIT_ATTEMPTS} attempts. ${this.lastStderr || "Silent dead hang."} Run: hound --doctor`
        : "Hound not found. Install: pip install hound-mcp[all]";
      throw new Error(hint);
    }
    return new Promise((resolve, reject) => {
      if (!this.proc || this.proc.killed) {
        reject(new Error("Hound not running"));
        return;
      }
      if (signal?.aborted) {
        reject(new Error(`Hound cancelled: ${name}`));
        return;
      }
      const id = ++this.nextId;
      let timer: NodeJS.Timeout;
      let onAbort: () => void;
      const cleanup = () => {
        clearTimeout(timer);
        this.pending.delete(id);
        if (signal && onAbort) signal.removeEventListener("abort", onAbort);
      };
      timer = setTimeout(() => {
        cleanup();
        reject(new Error(`Hound timeout: ${name}`));
      }, timeoutMs);
      onAbort = () => {
        cleanup();
        reject(new Error(`Hound cancelled: ${name}`));
      };
      signal?.addEventListener("abort", onAbort, { once: true });
      this.pending.set(id, {
        resolve: (v: any) => { cleanup(); resolve(v); },
        reject: (e: Error) => { cleanup(); reject(e); },
        timer,
      });
      try {
        this.proc.stdin!.write(
          JSON.stringify({ jsonrpc: "2.0", method: "tools/call", params: { name, arguments: args }, id }) + "\n",
        );
      } catch (e) {
        cleanup();
        reject(e as Error);
      }
    });
  }

  private _killProcess() {
    if (this.proc && !this.proc.killed) {
      try { this.proc.stdin?.end(); } catch {}
      try { this.proc.kill(); } catch {}
    }
    this.proc = null;
    this.ready = null;
    this.initInFlight = null;
    for (const [, { timer }] of this.pending) clearTimeout(timer);
    this.pending.clear();
  }

  kill() { this._killProcess(); }
}

const hound = new HoundClient();

// -- Helpers --

function getText(result: any): string {
  const content = result?.content ?? [];
  return content.filter((c: any) => c?.type === "text").map((c: any) => c.text).join("\n") || "(no output)";
}

function getImages(result: any): any[] {
  const content = result?.content ?? [];
  return content.filter((c: any) => c?.type === "image");
}

function tryJson(text: string): any {
  try { return JSON.parse(text); } catch { return {}; }
}

function pick(params: Record<string, any>, keys: string[]): Record<string, any> {
  const out: Record<string, any> = {};
  for (const k of keys) {
    if (params[k] !== undefined) out[k] = params[k];
  }
  return out;
}

// Map technical error strings from the hound server to clean, agent-friendly
// messages. The server sets result.error for all failure modes; this function
// translates it so the agent sees "Page doesn't exist (404)" instead of
// "http_error_404: server returned error status".
function cleanError(error: string, status?: number): string {
  if (!error) return "";
  const e = error.toLowerCase();
  // HTTP status errors
  if (e.includes("http_error_404") || status === 404) return "Page doesn't exist (404)";
  if (e.includes("http_error_410") || status === 410) return "Page has been removed (410)";
  if (e.includes("http_error_403") || status === 403) return "Access blocked by the site (403)";
  if (e.includes("http_error_429") || status === 429) return "Rate limited by the site (429)";
  if (e.includes("http_error_451") || status === 451) return "Page legally blocked (451)";
  if (e.includes("http_error_401") || status === 401) return "Login required (401)";
  if (e.includes("http_error_5") || (status && status >= 500)) return `Server error (${status})`;
  if (e.includes("http_error_4") || (status && status >= 400 && status < 500)) return `Request failed (${status})`;
  // Network errors
  if (e.includes("network_error") || status === 0) return "Couldn't reach the site";
  if (e.includes("timeout") || e.includes("timed out")) return "Request timed out";
  // Content quality errors
  if (e.includes("js_shell")) return "Page needs JavaScript (empty shell returned)";
  if (e.includes("bot_challenge")) return "Blocked by bot protection (Cloudflare)";
  if (e.includes("geo_redirect")) return "Page returned a region redirect";
  if (e.includes("all_tiers_failed")) return "Couldn't fetch this page - all methods failed";
  if (e.includes("browser_unavailable")) return "Browser not installed (HTTP-only mode). Stealthy/screenshot disabled. Install: pip install hound-mcp[all]";
  if (e.includes("auth_required")) return "Login required";
  if (e.includes("robots_txt")) return "Blocked by robots.txt";
  if (e.includes("encrypted_pdf")) return "PDF is password-protected";
  if (e.includes("pdf_deps_missing")) return "PDF support not installed (run: pip install hound-mcp[all])";
  if (e.includes("scanned_pdf")) return "Scanned PDF - needs OCR (install hound-mcp[all])";
  if (e.includes("not_a_pdf")) return "File is not a PDF";
  if (e.includes("pdf_open") || e.includes("pdf_extract")) return "Couldn't parse this PDF";
  // Fallback: strip the technical prefix for anything unmapped
  const colonIdx = error.indexOf(":");
  if (colonIdx > 0) return error.slice(colonIdx + 1).trim();
  return error;
}

// -- Extension --

export default function (pi: ExtensionAPI) {
  pi.on("session_start", async (_event, ctx) => {
    const ok = await hound.ensureReady().catch(() => false);
    if (!ok) {
      if (ctx.hasUI) {
        ctx.ui.notify(
          hound.hasExe()
            ? "Hound found but failed to start. Run: hound --doctor"
            : "Hound not found. Install: pip install hound-mcp[all]",
          "warn",
        );
      }
      return;
    }
    // Best-effort version sync check (non-blocking)
    if (ctx.hasUI && EXTENSION_VERSION !== "unknown") {
      hound.call("version", {}, 5_000).then((result) => {
        const parsed = tryJson(getText(result));
        if (parsed.version) {
          hound["houndVersion"] = parsed.version;
          const extMajor = EXTENSION_VERSION.split(".")[0];
          const hMajor = String(parsed.version).split(".")[0];
          if (extMajor !== hMajor) {
            ctx.ui.notify(
              `Hound extension v${EXTENSION_VERSION} vs hound v${parsed.version} (major mismatch). Update: hound -u && pi update npm:@houndmcp/hound-mcp-pi`,
              "warn",
            );
          }
        }
      }).catch(() => {});
    }
  });

  pi.on("session_shutdown", () => { hound.kill(); });

  // -- web_fetch --
  pi.registerTool({
    name: "web_fetch",
    label: "Web Fetch",
    description: "Fetch a URL (or urls=[...] for parallel bulk). Auto HTTP -> stealthy escalation. Returns extracted text + metadata + signals: content_ok, next_action, summary, page_type, content_age_days/is_stale, source_type/is_official, source/archived_at. Hard-block (404/bot/auth) -> auto-recover from Internet Archive (source=archive.org, archived_at=snapshot date; archive_fallback=false in options to opt out). PDFs -> structured markdown + ToC + page ranges + auto-OCR. Long pages: paginate with offset/next_offset or focus='query' for only relevant blocks. actions=[...] for click/form/scroll. include_links/include_media via options.",
    promptSnippet: "web_fetch(url|urls, extraction_type, css_selector, focus, actions, include_links, pages, offset) - anti-bot fetch + clean extraction; paginates with next_offset; dead-link recovery from Internet Archive.",
    parameters: Type.Object({
      url: Type.Optional(Type.String({ description: "URL to fetch" })),
      urls: Type.Optional(Type.Array(Type.String(), { description: "Multiple URLs (parallel; returns per-URL results)" })),
      extraction_type: Type.Optional(Type.String({ description: "Content format (default markdown). html = raw HTML." })),
      css_selector: Type.Optional(Type.String({ description: "CSS selector to narrow extracted content (e.g. 'article', '.main'). Token saver." })),
      max_content_chars: Type.Optional(Type.Integer({ description: "Max chars of extracted content (default 40000, min 500). Lower = less context; rest paginated via offset/next_offset." })),
      timeout: Type.Optional(Type.Integer({ description: "Max request time in ms (default 30000)." })),
      cache_ttl: Type.Optional(Type.Integer({ description: "Cache seconds (default 3600). 0 = force fresh." })),
      force_fetcher: Type.Optional(Type.String({ description: "Pin to one tier, skip auto-escalation. 'http' = fast HTTP-only (fails on JS/bot walls). 'stealthy' = anti-detect browser. Default = auto." })),
      offset: Type.Optional(Type.Integer({ description: "Char offset into extracted text to resume a truncated page. Use next_offset from previous response." })),
      pages: Type.Optional(Type.String({ description: "PDF only: page spec like '1-5' or '1,3,5-7'. Use table_of_contents page/end_page ranges to pick. None = all pages." })),
      password: Type.Optional(Type.String({ description: "PDF only: password for an encrypted PDF." })),
      focus: Type.Optional(Type.String({ description: "Query-focused extraction: only BM25-relevant blocks returned. Context saver on long pages. Post-cache (no re-fetch). Re-pass same focus when paginating." })),
      actions: Type.Optional(Type.Array(Type.Object({}, { additionalProperties: true }), { description: "Page interactions on stealthy browser AFTER load, BEFORE extraction. Forces stealthy + bypasses cache. Each item: {click:'css'}, {fill:{selector:'css',text:'x'}}, {press:'Enter'}, {wait:500}, {scroll:3}, {wait_selector:'css'}." })),
      options: Type.Optional(Type.Object({}, { additionalProperties: true, description: "include_links (bool,false: response.links=citations/navigation/external+primary_source), include_media (bool,false: up to 20 page image URLs), archive_fallback (bool,true: recover from Internet Archive on hard-block; false=raw failure), proxy, cookies, extra_headers, useragent, wait, network_idle, headless, respect_robots, real_chrome/solve_cloudflare/block_webrtc/hide_canvas/main_content_only/use_trafilatura (anti-detect tuning, good defaults, rarely needed)." })),
    }),
    async execute(_id, params, signal, _onUpdate, _ctx) {
      try {
        const args = pick(params, ["url", "urls", "extraction_type", "css_selector", "max_content_chars", "timeout", "cache_ttl", "force_fetcher", "offset", "pages", "password", "focus", "actions", "options"]);
        const result = await hound.call("mcp_smart_fetch", args, CALL_TIMEOUT_MS, signal);
        const text = getText(result);
        const parsed = tryJson(text);
        if (Array.isArray(parsed.results)) {
          const ok = parsed.successful ?? 0;
          const total = parsed.total ?? parsed.results.length;
          const joined = parsed.results.map((r: any) => `# ${r.url}\n${(r.content ?? []).join("\n")}`).join("\n\n---\n\n");
          return { content: [{ type: "text", text: joined + `\n\n[${ok}/${total} OK]` }], details: { bulk: true, ok, total } };
        }
        const content = Array.isArray(parsed.content) ? parsed.content.join("\n") : text;
        const foot: string[] = [];
        if (parsed.summary) foot.push(parsed.summary);
        if (parsed.next_action) foot.push(`Next: ${parsed.next_action}`);
        if (parsed.content_ok === false) foot.push("WARNING: content_ok=false - content may be a JS shell/login wall/error page");
        const trunc = parsed.is_truncated ? ` | next_offset=${parsed.next_offset}` : "";
        const fetcher = parsed.fetcher_used ?? "";
        const src = parsed.source === "archive.org" ? ` | ARCHIVE ${parsed.archived_at ?? ""}` : "";
        // When the server signals an error (http_error_*, network_error, etc.)
        // AND content_ok is false, show a clean error instead of dumping the
        // error page HTML as if it were real content.
        if (parsed.error && parsed.content_ok === false && parsed.source !== "archive.org") {
          const msg = cleanError(parsed.error, parsed.status);
          return {
            content: [{ type: "text", text: msg + (args.url ? `\nURL: ${args.url}` : "") + (parsed.next_action ? `\nNext: ${parsed.next_action}` : "") }],
            details: { url: args.url, error: msg, content_ok: false, source: parsed.source ?? "live", status: parsed.status },
          };
        }
        return {
          content: [{ type: "text", text: content + (foot.length ? `\n\n${foot.join("\n")}` : "") + `\n[${fetcher}${trunc}${src}]` }],
          details: { url: args.url, chars: content.length, content_ok: parsed.content_ok, truncated: !!parsed.is_truncated, source: parsed.source ?? "live" },
        };
      } catch (e: any) {
        const msg = e.message.includes("Hound not found") || e.message.includes("Hound failed")
          ? e.message
          : e.message.includes("timeout")
          ? "Request timed out"
          : e.message.includes("cancelled")
          ? "Request cancelled"
          : e.message.includes("Hound closed")
          ? "Connection to hound lost"
          : e.message;
        return { content: [{ type: "text", text: msg }], details: { error: msg } };
      }
    },
    renderCall(args, theme) {
      const urlStr = (args.url ?? args.urls?.[0] ?? "").toString();
      const trunc = urlStr.length > 60 ? urlStr.slice(0, 57) + "..." : urlStr;
      return new Text(theme.fg("toolTitle", theme.bold("Web Fetch: ")) + theme.fg("accent", trunc), 0, 0);
    },
    renderResult(result, { isPartial }, theme) {
      if (isPartial) return new Text(theme.fg("dim", "fetching..."), 0, 0);
      const d = result.details as any;
      if (d?.error) return new Text(theme.fg("error", `${d.error}`), 0, 0);
      if (d?.content_ok === false) return new Text(theme.fg("error", "failed"), 0, 0);
      if (d?.bulk) return new Text(theme.fg("accent", `${d.ok}/${d.total} URLs`), 0, 0);
      const kb = ((d?.chars ?? 0) / 1024).toFixed(1);
      const src = d?.source === "archive.org" ? " (archive)" : "";
      return new Text(theme.fg("accent", `${kb}KB${src}`), 0, 0);
    },
  });

  // -- web_search --
  pi.registerTool({
    name: "web_search",
    label: "Web Search",
    description: "Keyless web search (no API key). 10 backends in parallel (ddg,brave,mojeek,yahoo,yandex,startpage,google,qwant + opt-in wikipedia,grokipedia), neural-reranked + cross-backend consensus. Returns URLs + ranking, NOT content -> web_fetch the ones that match. Each result: relevance_score + fetch_relevance (high/med/low) + engines_consensus. related_queries from result titles+snippets. Blocked backends circuit-broken 60s. NEVER answer from snippets alone. Filters in options: site, exclude_sites, location, language, region, page, freshness.",
    promptSnippet: "web_search(query) - keyless web search across 10 backends; returns ranked URLs (not content), related_queries, consensus. web_fetch the results that match.",
    parameters: Type.Object({
      query: Type.String({ description: "Search query" }),
      options: Type.Optional(Type.Object({}, { additionalProperties: true, description: "max_results (1-50,6), cache_ttl (300), mode (auto|neural|find_similar), engines (list), site, exclude_sites, location, language, region, page, freshness, url (for find_similar)" })),
    }),
    async execute(_id, params, signal, _onUpdate, _ctx) {
      try {
        const args: Record<string, any> = { query: params.query };
        if (params.options && Object.keys(params.options).length) args.options = params.options;
        const result = await hound.call("mcp_smart_search", args, 60_000, signal);
        const text = getText(result);
        const parsed = tryJson(text);
        const results: any[] = parsed.results ?? [];
        const body = results.length === 0
          ? `No results for "${params.query}".`
          : results.map((r: any, i: number) => {
              const cons = r.engines_consensus ? ` [consensus ${r.engines_consensus}]` : "";
              const tier = r.fetch_relevance ? ` (${r.fetch_relevance})` : "";
              return `[${i + 1}]${tier} ${r.title || "(untitled)"}${cons}\n  ${r.url || ""}\n  ${r.snippet || ""}`;
            }).join("\n\n");
        const foot: string[] = [];
        if (Array.isArray(parsed.related_queries) && parsed.related_queries.length)
          foot.push(`Related: ${parsed.related_queries.slice(0, 6).map((q: string) => `"${q}"`).join(", ")}`);
        if (Array.isArray(parsed.engine_blocked) && parsed.engine_blocked.length)
          foot.push(`Blocked: ${parsed.engine_blocked.join(", ")}`);
        if (parsed.next_action) foot.push(`Next: ${parsed.next_action}`);
        return {
          content: [{ type: "text", text: body + (foot.length ? `\n\n${foot.join("\n")}` : "") }],
          details: { query: params.query, count: results.length, related: parsed.related_queries ?? [], blocked: parsed.engine_blocked ?? [] },
        };
      } catch (e: any) {
        const msg = e.message.includes("Hound not found") || e.message.includes("Hound failed") ? e.message : e.message.includes("timeout") ? "Search timed out" : e.message.includes("cancelled") ? "Search cancelled" : e.message.includes("Hound closed") ? "Connection to hound lost" : `Search failed: ${e.message}`;
        return { content: [{ type: "text", text: msg }], details: { error: msg } };
      }
    },
    renderCall(args, theme) {
      const q = (args.query ?? "").toString();
      const trunc = q.length > 50 ? q.slice(0, 47) + "..." : q;
      return new Text(theme.fg("toolTitle", theme.bold("Web Search: ")) + theme.fg("accent", `"${trunc}"`), 0, 0);
    },
    renderResult(result, { isPartial }, theme) {
      if (isPartial) return new Text(theme.fg("dim", "searching..."), 0, 0);
      const d = result.details as any;
      if (d?.error) return new Text(theme.fg("error", `${d.error}`), 0, 0);
      if (!d?.count) return new Text(theme.fg("dim", "no results"), 0, 0);
      const rel = d.related?.length ? ` + ${d.related.length} related` : "";
      return new Text(theme.fg("accent", `${d.count} results${rel}`), 0, 0);
    },
  });

  // -- web_crawl --
  pi.registerTool({
    name: "web_crawl",
    label: "Web Crawl",
    description: "Deep-crawl a site: best-first same-domain walk, each page as markdown + content_ok + page_type. List pages -> structured link list. sitemap=true (in options) maps whole site from sitemap.xml in one fetch; sitemap='auto' = use if present else BFS. discover_only=true = URL map only. focus='query' prioritizes relevant pages + focus-filters each. crawl_urls=[...] fetches a chosen subset. Caps: max_pages (10), max_depth (2), max_total_chars, deadline_ms. Reuses web_fetch anti-bot + cache.",
    promptSnippet: "web_crawl(url, focus, options.sitemap, discover_only, crawl_urls, max_pages) - site crawl; sitemap=true maps a whole site in one fetch; crawl_urls fetches a chosen subset.",
    parameters: Type.Object({
      url: Type.String({ description: "Start URL (crawl stays on this domain)" }),
      focus: Type.Optional(Type.String({ description: "Query: prioritize crawling links relevant to this + focus-filter each page. Token saver on doc sites." })),
      discover_only: Type.Optional(Type.Boolean({ description: "true = return URL map only, no page content. For big sites prefer options sitemap=true." })),
      crawl_urls: Type.Optional(Type.Array(Type.String(), { description: "Chosen subset of URLs to fetch (second-phase selective crawl, no re-discovery). Use after sitemap=true or discover_only=true." })),
      options: Type.Optional(Type.Object({}, { additionalProperties: true, description: "sitemap (true|'auto'|false,false: true=map from sitemap.xml in one fetch; 'auto'=use if present else BFS), max_pages (1-100,10), max_depth (0-5,2), path_include (list of path prefixes), path_exclude (list to skip), max_content_chars_per (8000), max_total_chars (token budget), concurrency (1-5,3), cache_ttl (3600;0=fresh), respect_robots (false), force_fetcher ('http'|'stealthy'), timeout (ms,30000), deadline_ms (120000)." })),
    }),
    async execute(_id, params, signal, _onUpdate, _ctx) {
      try {
        const args = pick(params, ["url", "focus", "discover_only", "crawl_urls", "options"]);
        const result = await hound.call("mcp_smart_crawl", args, CRAWL_TIMEOUT_MS, signal);
        const text = getText(result);
        const parsed = tryJson(text);
        const pages: any[] = Array.isArray(parsed.pages) ? parsed.pages : [];
        let body: string;
        if (parsed.sitemap_used || (args.discover_only && pages.length)) {
          body = pages.map((p: any, i: number) => `[${i + 1}] ${p.url}${p.lastmod ? ` (${p.lastmod})` : ""}`).join("\n");
        } else {
          body = pages.map((p: any) => {
            const tag = p.page_type ? `[${p.page_type}]` : "";
            const ok = p.content_ok ? "" : " WARNING";
            const c = Array.isArray(p.content) ? p.content.join("\n") : "";
            return `## ${p.url} ${tag}${ok}\n${c}`;
          }).join("\n\n---\n\n");
        }
        const foot: string[] = [];
        if (parsed.summary) foot.push(parsed.summary);
        if (parsed.next_action) foot.push(`Next: ${parsed.next_action}`);
        return {
          content: [{ type: "text", text: body + (foot.length ? `\n\n${foot.join("\n")}` : "") }],
          details: { url: params.url, pages: pages.length, sitemap: !!parsed.sitemap_used },
        };
      } catch (e: any) {
        const msg = e.message.includes("Hound not found") || e.message.includes("Hound failed") ? e.message : e.message.includes("timeout") ? "Crawl timed out" : e.message.includes("cancelled") ? "Crawl cancelled" : e.message.includes("Hound closed") ? "Connection to hound lost" : `Crawl failed: ${e.message}`;
        return { content: [{ type: "text", text: msg }], details: { error: msg } };
      }
    },
    renderCall(args, theme) {
      const urlStr = (args.url ?? "").toString();
      const trunc = urlStr.length > 55 ? urlStr.slice(0, 52) + "..." : urlStr;
      const sm = args.options?.sitemap ? " (sitemap)" : "";
      return new Text(theme.fg("toolTitle", theme.bold("Web Crawl: ")) + theme.fg("accent", trunc + sm), 0, 0);
    },
    renderResult(result, { isPartial }, theme) {
      if (isPartial) return new Text(theme.fg("dim", "crawling..."), 0, 0);
      const d = result.details as any;
      if (d?.error) return new Text(theme.fg("error", `${d.error}`), 0, 0);
      const sm = d.sitemap ? " (sitemap)" : "";
      return new Text(theme.fg("accent", `${d.pages} pages${sm}`), 0, 0);
    },
  });

  // -- web_screenshot --
  pi.registerTool({
    name: "web_screenshot",
    label: "Web Screenshot",
    description: "Screenshot a URL as an image. Multimodal agents only (content as images/canvas/visual layout). Text agents: use web_fetch. Stealthy browser auto-managed.",
    promptSnippet: "web_screenshot(url, options.full_page, options.image_type) - anti-bot one-shot screenshot of a URL (stealthy browser). Multimodal agents only.",
    parameters: Type.Object({
      url: Type.String({ description: "URL to screenshot" }),
      options: Type.Optional(Type.Object({}, { additionalProperties: true, description: "full_page (bool,false), image_type (png|jpeg,png), quality (0-100,jpeg), wait (ms), wait_selector (css), network_idle (bool), timeout (ms,30000)." })),
    }),
    async execute(_id, params, signal, _onUpdate, _ctx) {
      try {
        const args: Record<string, any> = { url: params.url };
        if (params.options && Object.keys(params.options).length) args.options = params.options;
        const result = await hound.call("mcp_screenshot", args, CALL_TIMEOUT_MS, signal);
        const images = getImages(result);
        if (images.length) {
          const out: any[] = [];
          for (const img of images) out.push({ type: "image", data: img.data, mimeType: img.mimeType || "image/png" });
          out.push({ type: "text", text: `Screenshot captured for ${params.url}` });
          return { content: out, details: { url: params.url, images: images.length } };
        }
        const parsed = tryJson(getText(result));
        return {
          content: [{ type: "text", text: parsed.error ? `Capture failed: ${parsed.error}` : `Screenshot captured for ${params.url} (no image payload)` }],
          details: { url: params.url, images: 0, error: parsed.error ?? "" },
        };
      } catch (e: any) {
        const msg = e.message.includes("Hound not found") || e.message.includes("Hound failed") ? e.message : e.message.includes("timeout") ? "Screenshot timed out" : e.message.includes("cancelled") ? "Screenshot cancelled" : e.message.includes("Hound closed") ? "Connection to hound lost" : `Screenshot failed: ${e.message}`;
        return { content: [{ type: "text", text: msg }], details: { error: msg } };
      }
    },
    renderCall(args, theme) {
      const urlStr = (args.url ?? "").toString();
      const trunc = urlStr.length > 55 ? urlStr.slice(0, 52) + "..." : urlStr;
      return new Text(theme.fg("toolTitle", theme.bold("Web Screenshot: ")) + theme.fg("accent", trunc), 0, 0);
    },
    renderResult(result, { isPartial }, theme) {
      if (isPartial) return new Text(theme.fg("dim", "capturing..."), 0, 0);
      const d = result.details as any;
      if (d?.error) return new Text(theme.fg("error", `${d.error}`), 0, 0);
      if (!d?.images) return new Text(theme.fg("dim", "no image"), 0, 0);
      return new Text(theme.fg("accent", `${d.images} image`), 0, 0);
    },
  });

  // -- cache_clear --
  pi.registerTool({
    name: "cache_clear",
    label: "Clear Cache",
    description: "Clear fetch cache. all=true wipes all (default: expired only). To re-fetch one URL fresh, pass cache_ttl=0 to web_fetch/web_crawl instead. Cache stores extracted text per URL+extraction_type+css_selector+pages (+ per query+filters for search); default TTL 1hr.",
    promptSnippet: "cache_clear(all) - clear fetch cache. all=true wipes all (default: expired only). For one URL fresh, use cache_ttl=0 on web_fetch instead.",
    parameters: Type.Object({
      all: Type.Optional(Type.Boolean({ description: "Wipe all (default: expired only)" })),
    }),
    async execute(_id, params, signal, _onUpdate, _ctx) {
      try {
        const args = { all: params.all ?? false };
        const result = await hound.call("cache_clear", args, 10_000, signal);
        return { content: [{ type: "text", text: getText(result) }], details: { all: args.all } };
      } catch (e: any) {
        return { content: [{ type: "text", text: e.message.includes("Hound") ? e.message : `Cache clear failed: ${e.message}` }], details: { error: e.message } };
      }
    },
    renderCall(_args, theme) {
      return new Text(theme.fg("toolTitle", theme.bold("Clear Cache")), 0, 0);
    },
    renderResult(result, _meta, theme) {
      const d = result.details as any;
      if (d?.error) return new Text(theme.fg("error", `${d.error}`), 0, 0);
      return new Text(theme.fg("accent", "cleared"), 0, 0);
    },
  });

  // -- hound_version --
  pi.registerTool({
    name: "hound_version",
    label: "Hound Version",
    description: "Hound version + update status.",
    promptSnippet: "hound_version() - hound version + update status.",
    parameters: Type.Object({}),
    async execute(_id, _params, signal, _onUpdate, _ctx) {
      try {
        const result = await hound.call("version", {}, 10_000, signal);
        const parsed = tryJson(getText(result));
        const v = parsed.version || "unknown";
        const latest = parsed.latest || "";
        const upToDate = parsed.up_to_date;
        const extInfo = EXTENSION_VERSION !== "unknown" ? ` | extension v${EXTENSION_VERSION}` : "";
        const text = upToDate
          ? `Hound ${v} (up to date)${extInfo}`
          : `Hound ${v} (update available: ${latest})${extInfo}. Run: hound -u`;
        return { content: [{ type: "text", text }], details: { version: v, latest, up_to_date: upToDate, extension: EXTENSION_VERSION } };
      } catch (e: any) {
        // Fallback: try hound --version via execSync (works even when MCP server is down)
        try {
          const exe = hound.getExe();
          if (exe) {
            const out = execSync(`"${exe}" --version`, { timeout: 5000, windowsHide: true }).toString().trim();
            return { content: [{ type: "text", text: out }], details: { fallback: true, extension: EXTENSION_VERSION } };
          }
        } catch {}
        return { content: [{ type: "text", text: e.message.includes("Hound") ? e.message : `Version check failed: ${e.message}` }], details: { error: e.message, extension: EXTENSION_VERSION } };
      }
    },
    renderCall(_args, theme) {
      return new Text(theme.fg("toolTitle", theme.bold("Hound Version")), 0, 0);
    },
    renderResult(result, _meta, theme) {
      const d = result.details as any;
      if (d?.error) return new Text(theme.fg("error", `${d.error}`), 0, 0);
      return new Text(theme.fg("accent", "ok"), 0, 0);
    },
  });
}

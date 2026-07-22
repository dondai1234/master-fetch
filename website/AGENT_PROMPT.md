# BUILD TASK: Hound Website

## SETUP

**Working directory:** `C:/Users/Dondai/.pi/agent/workspace/Chatgpt pro subscription/master-fetch/website/`

**Already in place (do not recreate):**
- `assets/images/hero-topography.png` (1472x1104, topographic terrain visualization)
- `assets/images/og-card.png` (1672x941, social sharing card)
- `assets/images/hound-logo.png` (512x512, Hound logo)

**Your job:** Create `index.html`, `style.css`, and `main.js` in the `website/` directory. Reference the existing images. Do not move, rename, or regenerate any image files.

## PROJECT

Build a single-page marketing website for **Hound** — an open-source MCP server that gives AI agents web research capabilities (fetch, crawl, search, bypass bot walls, read PDFs). It runs locally, costs $0, needs no API keys, and works with any MCP-speaking agent (Claude Code, Cursor, OpenCode, Hermes, Pi).

GitHub: github.com/dondai1234/master-fetch
PyPI: pypi.org/project/hound-mcp
Current version: 11.1.6
Stars: 131+ · MIT license · 806 tests

## TECH STACK

- **Single `index.html`** — no build step, no framework, no npm install
- **CSS**: One `style.css` file. Hand-written CSS, not Tailwind. Custom properties for the design tokens. No CSS framework.
- **JS**: One `main.js` file. Vanilla JS only. No jQuery, no React, no build tools. Use IntersectionObserver for scroll reveals. No GSAP, no Lenis — CSS animations + IntersectionObserver only.
- **Fonts**: Google Fonts via `<link>` in `<head>`. Three faces:
  - **Space Grotesk** (weights 400, 500, 700) — display/headlines
  - **Newsreader** (weights 400, 500 + italic 400) — body text, creates "field journal" counterpoint
  - **JetBrains Mono** (weights 400, 500) — data, coordinates, code, section labels
- **Images**: Raster images go in `assets/images/`. SVGs can be inline or in `assets/`.
- **Location**: Build inside the `website/` directory. All files are relative to `website/`.

## DESIGN CONTRACT

**Every visual decision serves the topographic field-survey metaphor: Hound as a tracking instrument surveying the web's terrain. Contour lines, coordinate notation, and telemetry readouts are the structural language. If a choice doesn't serve this, it doesn't belong.**

Hound is a tracking hound. It goes out, navigates terrain (the web), picks up scent (HTTP), follows the trail (escalates to stealthy browser), and brings back the quarry (extracted content with signals). The website visualizes this as a field survey: contour lines map the terrain, waypoints mark capabilities, telemetry readouts show benchmark data.

## CENTROID AUDIT — WHAT TO AVOID

The centroid (AI-default) version of this site would be: dark SaaS hero with centered text stack, gradient mesh background, 3-card feature grid with Lucide icons, glassmorphic cards with backdrop-blur, Inter font everywhere, blue-purple accent, fade-up animations on everything, "Trusted by" logo wall, rounded-2xl on every container.

**This build differs on every axis:**
1. **Layout**: asymmetric, contour-line structured, not centered stacks
2. **Palette**: warm coal #0c0b09 + teal #19a7a0, not blue-purple
3. **Typography**: Space Grotesk + Newsreader + JetBrains Mono (three faces with roles), not Inter alone
4. **Background**: topographic contour-line texture (SVG), not gradient mesh
5. **Components**: hover-fill rows and telemetry readouts, not 3-card grid
6. **Section labels**: coordinate notation ("06 TOOLS / CAPABILITIES"), not eyebrow badges
7. **Copy**: field-survey voice, direct and measurement-based, not marketing speak
8. **Motion**: clip-path line reveals and count-up stats, not opacity fade-up
9. **Borders**: hairlines tinted to ink color, not box-shadows
10. **Corner radius**: ALL SHARP (0px). Zero border-radius everywhere. This is a cartographic/technical site.

## DESIGN SYSTEM

### Colors (CSS custom properties)

```css
:root {
  /* Backgrounds — warm coal, not pure black */
  --coal: #0c0b09;
  --coal-light: #14110d;
  --coal-card: #1a1612;

  /* Text — warm ink, not pure white */
  --ink: #ede7db;
  --ink-dim: #9a9189;
  --ink-faint: #4a4540;

  /* Accent — teal, from existing brand (#19a7a0) */
  --teal: #19a7a0;
  --teal-bright: #2dd4cc;
  --teal-glow: rgba(25, 167, 160, 0.15);

  /* Signal — amber, used SPARINGLY only for status dots and benchmark pass/fail */
  --amber: #e89213;
  --amber-dim: #9a6210;

  /* Borders — warm hairlines */
  --border: rgba(237, 231, 219, 0.08);
  --border-bright: rgba(237, 231, 219, 0.15);
}
```

**Color lock rules:**
- Teal is THE accent. Used for: contour lines, section numbers, interactive hover states, links, coordinate notation.
- Amber is a SIGNAL color only. Used for: status dots (pass/fail), live indicators. Never for buttons, links, headings, or decoration.
- No other accent colors anywhere. No blue, no purple, no green (except the amber signal dots).
- All grays are warm (3-5% teal hue mixed in). No pure neutral grays.

### Typography

```
Headlines:    "Space Grotesk", sans-serif — 400/500/700
Body:         "Newsreader", serif — 400/500, italic 400
Data/Code:    "JetBrains Mono", monospace — 400/500
```

**Type rules:**
- Headlines: Space Grotesk, tight tracking (-0.02em to -0.04em), leading 1.05-1.15
- Body: Newsreader, 16-18px, leading 1.6, max-width 65ch, text-wrap: pretty
- Mono: JetBrains Mono, 12-14px, tracking 0.05-0.1em, uppercase for labels
- Typographic counterpoint: Space Grotesk uppercase headlines + Newsreader italic accent words inline. One keyword per headline styled as italic serif in teal.
- `text-wrap: balance` on all headings.
- `::selection { background: var(--teal); color: var(--coal); }`

### Texture

- **Grain overlay**: Global `body::after` with SVG noise at 4% opacity, `position: fixed`, `pointer-events: none`, `z-index: 90`. Subtle animation (8s steps(10) infinite, translate frames).
- **Contour lines**: SVG paths with organic curves (like elevation contours), rendered in `var(--teal)` at 8-15% opacity. Used as:
  - Section dividers (curved hairlines instead of straight rules)
  - Hero background (topographic visualization)
  - Subtle background texture in data sections
- No gradients. Zero. The only exception: a radial `var(--teal-glow)` behind the hero visualization, serving as a "signal glow" — this is the one allowed radial gradient.

### Motion

- **Entrance**: clip-path line reveal (text rises from behind a hard edge, not opacity fade-up). `clip-path: polygon(0 0, 100% 0, 100% 100%, 0 100%)` with inner span translating from `yPercent: 115` to `0`.
- **Count-up stats**: numbers that count up on scroll-in using IntersectionObserver + requestAnimationFrame.
- **Hover-fill rows**: accent color slides up from bottom, text inverts to coal. `transition: transform 500ms cubic-bezier(0.65, 0, 0.35, 1)`.
- **Custom cubic-bezier**: `cubic-bezier(0.65, 0, 0.35, 1)` for large elements, `cubic-bezier(0.33, 1, 0.68, 1)` for small. NEVER `ease-in-out`.
- **`:active` state**: `scale(0.98)` on every button.
- **`prefers-reduced-motion`**: disable ALL animations. Static fallback for everything.

### Corner Radius

ALL SHARP. Zero border-radius on every element. No exceptions. This is a cartographic/technical site. Cards, buttons, inputs, images — all 0px radius.

## PAGE ANATOMY

### Nav (fixed, 64px height)

- Fixed top, `mix-blend-difference` for adaptive contrast over any background.
- Left: "HOUND" in Space Grotesk 700, letter-spacing -0.02em. Next to it, a small JetBrains Mono label: "v11.1.6" in `var(--ink-dim)`.
- Right: links — "Tools", "Stealth", "Search", "Compare", "Install" — in JetBrains Mono 12px, uppercase, tracking 0.15em. Active section highlighted in `var(--teal)`.
- No backdrop-blur. The `mix-blend-difference` handles contrast.
- Mobile: collapse links to a hamburger menu. Single line at all widths.

### Section 1 — Hero (min-h-100dvh, asymmetric split 60/40)

**Layout**: 60/40 split. Left 60%: text. Right 40%: the hero visualization.

**Left side:**
- Section label (Device A): "00 / SURVEY" in JetBrains Mono, teal numeral + hairline + label.
- Headline: "Give your AI agent the web." in Space Grotesk 700, fluid `clamp(2.5rem, 6vw, 5rem)`, leading 0.95, tracking -0.03em.
  - Typographic counterpoint: the word "web" styled as Newsreader italic 400, `var(--teal)`, lowercase.
- Subtext (max 20 words): "One MCP server. $0. No keys, no accounts. Fetch, crawl, search, bypass Cloudflare, read PDFs. Runs on your machine." in Newsreader 400, 18px, `var(--ink-dim)`, max-width 45ch.
- Install block: a terminal-styled code block (dark `var(--coal-card)` background, 1px `var(--border)` border, zero radius):
  ```
  pip install hound-mcp[all] && playwright install chromium
  ```
  JetBrains Mono 14px. Copy button on the right (JS clipboard API).
- CTAs: "Get started" (primary, `var(--teal)` bg, `var(--coal)` text, sharp corners, `:active scale(0.98)`) + "View on GitHub" (secondary, transparent bg, 1px `var(--border-bright)` border).

**Right side:**
- The hero topographic visualization. This is the SIGNATURE VISUAL.
- An inline SVG showing contour lines (curved paths in `var(--teal)` at varying opacity 5-15%) forming a topographic terrain. A glowing tracking path (SVG path in `var(--teal-bright)` with a subtle CSS glow filter) winds through the contour lines from bottom-left to top-right. Waypoint markers (small circles in `var(--amber)`) at key points along the path, each labeled in JetBrains Mono 8px: "FETCH", "CRAWL", "SEARCH", "STEALTH".
- The path draws itself on load using CSS `stroke-dasharray` + `stroke-dashoffset` animation (2s, cubic-bezier(0.65, 0, 0.35, 1)).
- Below the visualization: a small JetBrains Mono caption: "HOUND PIPELINE / HTTP -> STEALTHY -> EXTRACT" in `var(--ink-faint)`, 10px.
- The GPT-generated hero background image (`assets/images/hero-topography.png`) sits BEHIND the SVG contour lines, providing atmospheric depth and grain. The SVG lines overlay on top with higher opacity.

**Background**: subtle grain overlay (global, from texture rules). A faint radial `var(--teal-glow)` behind the right-side visualization only.

### Section 2 — The 6 Tools (hover-fill rows)

**Layout**: Full-width section. Section label at top. Headline. Then 6 rows stacked vertically, each a hover-fill row.

**Section label**: "06 TOOLS / CAPABILITIES" — JetBrains Mono, teal "06" + hairline + "TOOLS / CAPABILITIES".

**Headline**: "Six tools. Every web task." in Space Grotesk 700, `clamp(1.75rem, 4vw, 3rem)`, tracking -0.02em.
- Counterpoint: "every" in Newsreader italic, `var(--teal)`.

**Rows** (hover-fill technique):
Each row is a `<div class="tool-row">` with:
- Left: tool number in JetBrains Mono teal ("01" through "06")
- Center: tool name in Space Grotesk 500, 20px + one-liner in Newsreader 400, 15px, `var(--ink-dim)`
- Right: a JetBrains Mono tag showing the key parameter (e.g., "auto-escalate", "best-first", "10 backends", "multimodal", "all/expired", "v11.1.6")

On hover: `var(--teal)` slides up from bottom (`translate-y: 0` to `translate-y: -100%`), text inverts to `var(--coal)`.

The 6 tools:
1. **smart_fetch** — "Fetch any URL. HTTP first, auto-escalates to the anti-detect browser if blocked. Bulk fetch, PDFs with OCR, page interactions, pagination." — tag: "auto-escalate"
2. **smart_crawl** — "Best-first same-domain crawl. Each page as markdown with content_ok and page_type. Sitemap mode, focus filtering, token budgets." — tag: "best-first"
3. **smart_search** — "Ten keyless search backends in parallel. Neural rerank with cross-backend consensus. No API key, no account." — tag: "10 backends"
4. **screenshot** — "Capture a page as an image. For multimodal agents that need visual layout." — tag: "multimodal"
5. **cache_clear** — "Clear the fetch cache. All or expired only. Set cache_ttl=0 to force fresh." — tag: "all / expired"
6. **version** — "Installed version and update status. Brick-proof self-update with rollback." — tag: "self-healing"

**Contour line divider**: between this section and the next, a curved SVG contour line spans the full width in `var(--teal)` at 10% opacity.

### Section 3 — Stealth Engine (telemetry readout)

**Layout**: Full-width. Split into two columns at desktop (60/40): left is the headline + description, right is the telemetry data panel. Below: the stealth signals table.

**Section label**: "08 SITES / STEALTH BENCHMARK"

**Headline**: "Passes the hardest anti-bot targets." in Space Grotesk 700.
- Counterpoint: "hardest" in Newsreader italic, `var(--teal)`.

**Left column** (description, max-width 50ch, Newsreader 16px):
"System Chrome auto-detection for real TLS fingerprints. Four coherent fingerprint profiles. JS-layer patches for webdriver, HeadlessChrome, canvas noise, permissions. Human behavior simulation with Bezier mouse curves. Cloudflare Turnstile solver with human-like mouse movement."

**Right column** (telemetry panel):
A data panel styled like a field instrument readout. `var(--coal-card)` background, 1px `var(--border)` border, zero radius. Inside:

Detection test sites (each row: site name in JetBrains Mono 12px + status indicator):
```
bot.sannysoft.com     [PASS] (amber dot)
creepjs               [PASS] (amber dot)
browserscan           [PASS] (amber dot)
pixelscan             [PASS] (amber dot)
```

Anti-bot protected sites (each row: site name + protection type + status + content size):
```
canadianinsider   CF TURNSTILE   200   78 KB   [PASS]
medium            CF INTERSTICE  200   93 KB   [PASS]
stackoverflow     CLOUDFLARE     200  1.1 MB   [PASS]
nowsecure         CF CHALLENGE   200  180 KB   [PASS]
glassdoor         DATADOME       200  849 KB   [PASS]
reddit            CF LITE        200    1 MB   [PASS]
hacker news       NONE           200   35 KB   [PASS]
github            NONE           200  523 KB   [PASS]
```

All in JetBrains Mono 12-13px. Pass dots in `var(--amber)`. Status codes in `var(--teal)`.

**Below the panel**: stealth signals as a compact data table (JetBrains Mono):
```
navigator.webdriver    undefined     not detected
navigator.userAgent    Chrome/150    not detected
navigator.platform     Win32         not detected
navigator.plugins      5             not detected
window.chrome          object        not detected
WebGL renderer          real GPU      not detected
canvas fingerprint     per-session   not detected
TLS fingerprint        Chrome 150   not detected
```

Two columns: signal name + value (teal) + status (amber "not detected").

**Note below the table**: "Memory: RSS decreased 3.5 MB over 5 sequential fetches. No RAM creep." in Newsreader italic 14px, `var(--ink-dim)`.

### Section 4 — Search (signal diagram)

**Layout**: Full-width. Headline at top. Below: two-column split. Left: description text. Right: a radial diagram of the 10 search backends.

**Section label**: "10 ENGINES / KEYLESS SEARCH"

**Headline**: "Ten engines. Zero keys." in Space Grotesk 700.
- Counterpoint: "zero" in Newsreader italic, `var(--teal)`.

**Left column** (Newsreader 16px, max-width 50ch):
"DuckDuckGo, Brave, Mojeek, Yahoo, Yandex, Startpage, Google, Qwant, plus Wikipedia and Grokipedia. Six independent index families, not the same feed twice. A local ONNX cross-encoder reranks results semantically. A diversity quorum waits for three backends before returning. A backend that rate-limits is circuit-broken for 60 seconds and carried by the others."

Below: three callout stats (count-up on scroll-in):
- "10" backends (JetBrains Mono 48px, teal, count-up animation)
- "6+" index families
- "$0" cost

**Right column**: An inline SVG radial diagram. 10 nodes in a circle, each labeled with an engine name in JetBrains Mono 9px. All connected to a central node labeled "MERGE + RERANK". Lines in `var(--teal)` at 20% opacity. Central node in `var(--teal-bright)`. The diagram draws itself on scroll-in (stroke-dasharray animation).

**Contour line divider** after this section.

### Section 5 — Comparison (hover-fill rows + token callout)

**Layout**: Full-width. Headline. Then comparison rows. Then the token cost callout.

**Section label**: "01 CHOICE / WHY HOUND"

**Headline**: "The only free tool that does all of it." in Space Grotesk 700.
- Counterpoint: "all" in Newsreader italic, `var(--teal)`.

**Comparison rows** (hover-fill, same technique as tools section):

Each row: tool name (Space Grotesk 500) + what it does (Newsreader 14px, `var(--ink-dim)`) + what it misses (Newsreader italic 14px, `var(--ink-faint)`)

1. **Crawl4AI** — "Crawls well." — "No search. Trips on Cloudflare."
2. **Jina Reader** — "Fetches pages." — "Rate-limits. Routes through their API."
3. **Firecrawl** — "Managed scraping." — "Good stuff behind paid cloud. Credits don't roll over."
4. **Parallel Search** — "Remote search." — "No crawl. Runs on their servers."
5. **Hound** — "Fetch, crawl, search, bypass, PDF, OCR. All local. $0." — "" (no miss line — this is the winner, highlight in teal)

The Hound row is permanently filled (teal background, coal text), not just on hover. It stands out as the only row that's always inverted.

**Token cost callout** (below the rows):
A wide data block, `var(--coal-card)` background, 1px `var(--border)`, zero radius:
- Left: "6 tools" in JetBrains Mono 32px, teal
- Center: "~2.7K tokens" in JetBrains Mono 32px, `var(--ink)`
- Right: "tools/list" in JetBrains Mono 14px, `var(--ink-dim)`
- Below in Newsreader 14px italic: "Typical MCP servers dump 3-5K tokens just to exist."

### Section 6 — Install (code blocks)

**Layout**: Full-width. Headline. Two terminal blocks side by side (or stacked on mobile). Below: collapsible details.

**Section label**: "02 COMMANDS / QUICK START"

**Headline**: "Two commands. Then point your agent at hound." in Space Grotesk 700.

Two terminal blocks (`var(--coal-card)` bg, 1px `var(--border)`, zero radius, JetBrains Mono 14px):

Block 1:
```
pip install hound-mcp[all]
```
Block 2:
```
playwright install chromium
```
Each with a copy button.

Below: four collapsible `<details>` sections (JetBrains Mono labels, Newsreader body):

1. **Lean install (HTTP-only)**: `pip install hound-mcp` — "No browser deps. Works on Termux, aarch64. HTTP fetch + crawl + keyless search. No stealthy browser or screenshot."
2. **Updating and rollback**: `hound -u` (update), `hound --doctor` (health check), `hound --rollback` (undo last update), `python ~/.hound/repair.py` (emergency repair).
3. **Pi extension**: `pip install hound-mcp[all]` then `pi install npm:@houndmcp/hound-mcp-pi` — "All 6 tools as native Pi tools. Prewarmed at session start."
4. **Open WebUI (HTTP)**: `hound --http --host 127.0.0.1 --port 8765` — "Point Open WebUI at http://127.0.0.1:8765/mcp"

**Agent install prompt** (below the collapsibles):
A highlighted block with a copy button:
```
Install the Hound MCP server on this machine. Follow every step. Do not skip any.
1. Figure out which agent harness you are running on. Find where the MCP config file lives and what format it expects.
2. Run: pip install hound-mcp[all]  Then: playwright install chromium (verify first if not already installed)
3. Add a new MCP server named "hound" with command "hound", no arguments. No API keys needed.
4. Save the file. Tell the user to restart the agent.
```

### Honest Limits (compact section)

**Layout**: Full-width, compact. Section label + headline + list.

**Section label**: "06 LIMITS / HONEST"

**Headline**: "What Hound can't do." in Space Grotesk 500, `var(--ink-dim)` (dimmer — this is a secondary section).

List (Newsreader 15px, each with a JetBrains Mono prefix tag):
- **[BLOCKED]** DataDome, Akamai, interactive Cloudflare Turnstile — not bypassed. `next_action` tells the agent to switch sources.
- **[RATE]** Search rate-limits — solved by 10-backend diversity, not by magic. `HOUND_SEARCH_PROXY` for heavy use.
- **[AUTH]** Login-required sites — out of scope.
- **[DOM]** Deep shadow-DOM — `actions` reach most of it. Deep shadow-DOM piercing not yet wired.
- **[VIDEO]** YouTube — minimal text.

### Footer

**Layout**: Full-width, 64px padding top/bottom.

Three columns:
- Left: "HOUND" in Space Grotesk 700 + "v11.1.6" in JetBrains Mono 12px, `var(--ink-dim)`. Below: "MIT license" in Newsreader 13px.
- Center: links in JetBrains Mono 12px, uppercase: GitHub, PyPI, npm, Changelog, Issues. Each in `var(--ink-dim)`, hover `var(--teal)`.
- Right: a live status indicator. A pulsing `var(--amber)` dot (CSS animation) + "ALL SYSTEMS OPERATIONAL" in JetBrains Mono 10px, uppercase. Below: "131 stars" in JetBrains Mono 10px, `var(--ink-dim)`.

Bottom strip: "Give your AI agent the web." in Newsreader italic 14px, `var(--ink-faint)`. Centered.

**Contour line**: a final contour SVG path runs across the top of the footer in `var(--teal)` at 8% opacity.

## CONTOUR LINE SYSTEM (signature visual element)

The contour lines are the page's structural skeleton. Generate them as inline SVG paths:

```svg
<!-- Example contour line as section divider -->
<svg class="contour-divider" viewBox="0 0 1200 40" preserveAspectRatio="none" width="100%" height="40">
  <path d="M0,20 C200,5 400,35 600,15 S1000,25 1200,10" 
        fill="none" stroke="var(--teal)" stroke-width="1" opacity="0.1"/>
</svg>
```

**Rules:**
- Use organic Bezier curves, never straight lines. The curves should look like elevation contours on a topographic map.
- Opacity: 8-15% for dividers, 5-10% for background texture.
- Stroke width: 1px for dividers, 0.5px for texture.
- Place between every section (as a divider) and as subtle background texture in the hero and data sections.
- Each contour should be a unique path — don't reuse the same curve.

## HOVER-FILL ROW COMPONENT

```html
<div class="hover-fill-row">
  <div class="fill-layer"></div>
  <div class="row-content">
    <span class="row-number">01</span>
    <div class="row-text">
      <h3 class="row-title">smart_fetch</h3>
      <p class="row-desc">Fetch any URL. HTTP first, auto-escalates to the anti-detect browser if blocked.</p>
    </div>
    <span class="row-tag">auto-escalate</span>
  </div>
</div>
```

```css
.hover-fill-row {
  position: relative;
  overflow: hidden;
  border-top: 1px solid var(--border);
  cursor: pointer;
}
.hover-fill-row .fill-layer {
  position: absolute;
  inset: 0;
  background: var(--teal);
  transform: translateY(100%);
  transition: transform 500ms cubic-bezier(0.65, 0, 0.35, 1);
}
.hover-fill-row:hover .fill-layer { transform: translateY(0); }
.hover-fill-row .row-content {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 2rem;
  padding: 1.5rem 2rem;
  transition: color 300ms ease;
}
.hover-fill-row:hover .row-content { color: var(--coal); }
.hover-fill-row:hover .row-desc { color: var(--coal); opacity: 0.8; }
.hover-fill-row:hover .row-tag { color: var(--coal); border-color: var(--coal); }
```

The winner row (Hound in comparison) has `.fill-layer` permanently at `translateY(0)` — always filled, not just on hover.

## CLIP-PATH LINE REVEAL

```html
<span class="clip-reveal">
  <span class="clip-inner">Give your AI agent the web.</span>
</span>
```

```css
.clip-reveal {
  display: block;
  overflow: hidden;
}
.clip-inner {
  display: block;
  transform: translateY(115%);
  transition: transform 1.2s cubic-bezier(0.65, 0, 0.35, 1);
}
.clip-reveal.visible .clip-inner { transform: translateY(0); }
```

Use IntersectionObserver to add `.visible` class when the element scrolls into view. Stagger multiple lines with `transition-delay`.

## COUNT-UP STATS

```js
function countUp(el, target, duration = 1800) {
  const start = performance.now();
  function tick(now) {
    const progress = Math.min((now - start) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
    el.textContent = String(Math.round(eased * target));
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}
// Trigger with IntersectionObserver
```

## BINDING RULES (non-negotiable)

1. **ONE accent color** (teal) locked across every component. Amber for status dots only.
2. **ALL SHARP corners** — zero border-radius on every element.
3. **ONE theme** — dark mode throughout. No light sections.
4. **NO gradients** — except the one radial glow behind the hero visualization.
5. **NO box-shadows** — hairlines only.
6. **NO backdrop-blur** — mix-blend-difference on nav instead.
7. **NO emoji in the UI** — the README uses emoji but the website does not.
8. **NO "Trusted by" logo wall** — no fake social proof.
9. **NO pricing table** — Hound is free, state it once.
10. **NO testimonials** — this is an open-source tool, not a SaaS.
11. **NO 3-card feature grid** — use hover-fill rows instead.
12. **`prefers-reduced-motion`** — disable all animations.
13. **Semantic HTML** — `<nav>`, `<main>`, `<section>`, `<footer>`. One `<h1>`.
14. **`min-h-[100dvh]`** on hero — never `h-screen`.
15. **Mobile collapse** — every asymmetric layout collapses to single column below 768px. Touch targets 44pt minimum.
16. **Skip link** — first element in `<body>`, visually hidden until focused.
17. **`:focus-visible`** — styled, visible, not browser default.
18. **Copy buttons** — on every code block. Use Clipboard API.
19. **No banned copy** — see copy rules below.

## COPY RULES

**Banned phrases** (zero tolerance — do not use anywhere):
- "Elevate your..."
- "Seamless experience"
- "Next-Gen platform"
- "Game-changer"
- "In today's fast-paced world"
- "Empower your team"
- "Harness the power of"
- "We're on a mission to"
- "At the intersection of X and Y"
- "Crafting [noun] experiences"
- "Obsessed with the details"
- "Where vision meets craft"
- "Build the future"
- "Effortlessly"
- "AI-powered" (Hound is not AI, it's a tool FOR AI agents)

**Voice**: Field-survey log. Direct, measurement-based, no marketing adjectives. Every sentence carries a concrete detail (number, tool name, specific capability). If a sentence could appear on any other dev tool's website, delete it.

**Numbers**: All numbers are real. 6 tools, 10 search backends, 8 benchmark sites, ~2.7K tokens, 131 stars, 806 tests. Do not invent or round.

## IMAGE PLACEMENT

The following GPT-generated images will be placed in `assets/images/`:

1. **`hero-topography.png`** — placed behind the hero SVG contour lines. Size: 1254x1254 (square, use CSS `object-fit: cover` to crop as needed). This is the atmospheric background; the SVG contour lines overlay on top.
2. **`og-card.png`** — used as the Open Graph social sharing image in `<head>`. Size: 1254x1254 (square, will be cropped to 1200x630 during deploy).

Reference these in the HTML as:
```html
<!-- Hero background -->
<div class="hero-visual">
  <img src="assets/images/hero-topography.png" alt="" class="hero-bg-img" />
  <svg class="hero-contours"><!-- inline SVG contour lines --></svg>
</div>
```

If the images don't exist yet, build the SVG contour visualization to work standalone (the GPT image adds atmosphere but the SVG is the primary visual). Use a `@media` query or JS check to hide the `<img>` if it 404s.

## FILE STRUCTURE

```
website/
  index.html          -- the complete page
  style.css           -- all custom CSS
  main.js             -- all JS (IntersectionObserver, copy buttons, count-up, nav)
  assets/
    images/
      hero-topography.png   -- GPT image (placed by Dondai)
      og-card.png           -- GPT image (placed by Dondai)
      hound-logo.png        -- copy from repo docs/
```

Copy the existing logo from `../docs/hound-logo.png` to `assets/images/hound-logo.png` for use in the nav and footer.

## ACCESSIBILITY CHECKLIST

- [ ] Semantic HTML (nav, main, section, footer, h1-h3 hierarchy)
- [ ] Exactly one h1 (hero headline)
- [ ] Skip link as first body element
- [ ] :focus-visible styled
- [ ] ::selection styled
- [ ] aria-label on icon-only buttons (copy buttons)
- [ ] alt text on meaningful images, empty alt on decorative
- [ ] prefers-reduced-motion disables all animation
- [ ] Color contrast WCAG AA (4.5:1) on all text
- [ ] Touch targets 44pt minimum on mobile
- [ ] All code blocks have copy buttons with aria-label

## FINAL AUDIT (answer honestly before shipping)

1. **Blind test**: Would a senior designer call this AI-generated? If yes, identify the 3 most damning elements and rebuild them.
2. **Substitution test**: Could the palette/font/layout be swapped for a generic dark SaaS template and the site still makes sense? If yes, the choices are decorative, not structural.
3. **Section audit**: For each section, what is the default-LLM version? How is this different? If the difference is cosmetic only, rebuild.
4. **Copy audit**: Highlight every sentence that could appear on another dev tool's website. Delete it. Replace with concrete, non-transferable statements.
5. **Competition check**: Would this stand out in a grid of 20 dev tool websites? If it blends, rebuild.

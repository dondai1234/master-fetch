# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# Hound MCP server — production image
#
# Builds from the local source tree (this repo). To build from PyPI instead,
# replace the COPY + `pip install .[all]` lines with:
#     RUN pip install --no-cache-dir "hound-mcp[all]==<version>"
#
# Build:    docker build -t hound-mcp .
# Run:      docker compose up -d          (recommended — see docker-compose.yml)
#   or:     docker run -d --init --shm-size=1g -p 8765:8765 \
#              -v hound-cache:/home/hound/.hound hound-mcp
#
# The container serves the MCP streamable-HTTP transport at
#     http://<host>:8765/mcp
# Stdio clients (Claude Code, Cursor, OpenCode) can instead launch:
#     docker run -i --rm --init --shm-size=1g hound-mcp hound
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim-bookworm

# Optional: bake in real Google Chrome for maximum stealth-tier effectiveness.
# Hound auto-detects a system Chrome channel and prefers it over bundled
# Chromium (better fingerprint against the hardest anti-bot targets).
# Adds ~350MB. Enable with:  docker build --build-arg INSTALL_CHROME=true .
ARG INSTALL_CHROME=false

# Shared browser install location, readable by the non-root runtime user.
# Both playwright and patchright honor PLAYWRIGHT_BROWSERS_PATH.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NO_COLOR=1

# ── OS prep ──────────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
    && if [ "$INSTALL_CHROME" = "true" ]; then \
         curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
           | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
         && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb stable main" \
           > /etc/apt/sources.list.d/google-chrome.list \
         && apt-get update \
         && apt-get install -y --no-install-recommends google-chrome-stable; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# ── Install Hound from source ────────────────────────────────────────────────
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN pip install ".[all]"

# ── Browsers + their system libraries ────────────────────────────────────────
# `--with-deps` pulls in the Chromium shared-library stack (fonts, X11 stubs,
# nss, etc.). Patchright's patched Chromium is installed alongside into the
# same PLAYWRIGHT_BROWSERS_PATH.
RUN playwright install --with-deps chromium \
    && patchright install chromium \
    && rm -rf /var/lib/apt/lists/* /build \
    && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"

# ── Non-root runtime user ────────────────────────────────────────────────────
# Chromium refuses its sandbox as root; as an unprivileged user it uses
# user-namespace sandboxing, which works out of the box on Docker Engine
# ≥ 24 / recent kernels. On older engines, see the note in docker-compose.yml.
RUN useradd -m -u 1000 hound
USER hound
WORKDIR /home/hound

# Cache + state live in ~/.hound — mount a volume here to persist the fetch
# cache (default TTL 1h) across container restarts. Purely optional.
VOLUME /home/hound/.hound

EXPOSE 8765

# TCP-level liveness probe (curl-free; the /mcp endpoint requires a session
# handshake, so a socket connect is the honest health signal).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8765), 3)" || exit 1

# Bind 0.0.0.0 so the port is reachable from outside the container.
# Do NOT publish this port beyond localhost/your LAN — Hound has no auth.
CMD ["hound", "--http", "--host", "0.0.0.0", "--port", "8765"]

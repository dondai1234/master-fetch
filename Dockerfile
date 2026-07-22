# Hound MCP Docker Image
# Multi-stage build for smaller image size

FROM python:3.13-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Install the package with all dependencies
COPY . /app
WORKDIR /app
RUN pip install --no-cache-dir -e .[all]

# Final stage - minimal runtime image
FROM python:3.13-slim

# Install runtime dependencies for browser and OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium browser dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libatspi2.0-0 \
    # OCR dependencies
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

WORKDIR /app

# Set environment for non-interactive Chromium
ENV ENV=/root/.bashrc \
    SHELL=/bin/bash \
    PLAYWRIGHT_BROWSERS_PATH=/msfs-playwright \
    HOUND_BROWSER_IDLE_TIMEOUT=300

# Install Chromium via playwright in the final image
RUN playwright install --with-deps chromium && \
    playwright install-deps chromium

# Expose HTTP port for MCP server
EXPOSE 8765

# Default to HTTP mode for Docker
ENV HOUND_HTTP_PORT=8765

# Run hound in HTTP mode
CMD ["hound", "--http", "0.0.0.0:${HOUND_HTTP_PORT}"]
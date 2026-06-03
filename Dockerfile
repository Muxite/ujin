# syntax=docker/dockerfile:1
#
# Multi-stage build for ujin. Three targets:
#
#   ujin       (default) — pure-python image: poller + scrape HTTP service.
#                          Fast build; the Rust stage below is NOT built.
#                          obscura rendering degrades gracefully (HTTP-only).
#       docker build --target ujin -t ujin:latest .
#
#   ujin-full            — ujin + the obscura headless renderer baked in
#                          (binary mode via OBSCURA_BIN). Builds the Rust stage,
#                          which compiles V8 and is SLOW the first time (~15-20m).
#       docker build --target ujin-full -t ujin:full .
#
# The obscura submodule must be checked out first:
#       git submodule update --init ujin/obscura

# ---- (optional) build the obscura headless renderer (Rust) ----
FROM rust:1-slim AS obscura-build
WORKDIR /build
COPY ujin/obscura/ ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake python3 \
       git ca-certificates pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/* \
    && cargo build --release \
    && strip target/release/obscura || true

# ---- ujin python service (pure-python; default target) ----
FROM python:3.12-slim AS ujin
WORKDIR /app
COPY pyproject.toml README.md ./
COPY ujin ./ujin
# Drop the Rust submodule source from the image and install the rich extras.
# The wheel build already excludes ujin/obscura, so site-packages stays pure-python.
RUN rm -rf ujin/obscura \
    && pip install --no-cache-dir ".[scrape,social,diff,sessions,jobs]"

# Mount points for the unified job control plane: operator plugins + the durable
# jobstore. Both are typically bind- or volume-mounted by compose.
RUN mkdir -p /plugins /data
ENV UJIN_PLUGINS_DIR=/plugins \
    UJIN_JOBS_DB=/data/ujin-jobs.db

EXPOSE 8900 8901 8902
ENTRYPOINT ["ujin"]
# Default to the scrape HTTP service; override for the poller (`api`), the
# unified jobs control plane (`jobs-serve`), or `watch`.
CMD ["scrape-serve", "--host", "0.0.0.0", "--port", "8901"]

# ---- ujin + Playwright/Chromium + Selenium/chromedriver (heavy) ----
#   docker build --target ujin-browser -t ujin:browser .
# Built on top of the slim `ujin` stage so the default image is untouched. This
# image is large (~1.5GB+): Chromium + its runtime libs + the Playwright browser.
FROM ujin AS ujin-browser
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       chromium chromium-driver fonts-liberation ca-certificates \
       libnss3 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdrm2 libxkbcommon0 \
       libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir ".[browser]" \
    && python -m playwright install --with-deps chromium
ENV UJIN_BROWSER_ENABLED=1 \
    UJIN_BROWSER_ENGINE=playwright \
    UJIN_BROWSER_HEADLESS=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    UJIN_CHROMEDRIVER=/usr/bin/chromedriver

# ---- ujin + bundled obscura binary (slow build) ----
FROM ujin AS ujin-full
COPY --from=obscura-build /build/target/release/obscura /usr/local/bin/obscura
ENV OBSCURA_BIN=/usr/local/bin/obscura

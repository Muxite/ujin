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
    && pip install --no-cache-dir ".[scrape,social,diff,sessions]"

EXPOSE 8900 8901
ENTRYPOINT ["ujin"]
# Default to the scrape HTTP service; override for the poller (`api`) or `watch`.
CMD ["scrape-serve", "--host", "0.0.0.0", "--port", "8901"]

# ---- ujin + bundled obscura binary (slow build) ----
FROM ujin AS ujin-full
COPY --from=obscura-build /build/target/release/obscura /usr/local/bin/obscura
ENV OBSCURA_BIN=/usr/local/bin/obscura

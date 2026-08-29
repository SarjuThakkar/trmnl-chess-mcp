# syntax=docker/dockerfile:1

# ---- Stage 1: build lc0 from source --------------------------------------
# No Debian/Ubuntu package for lc0 exists (checked packages.debian.org and
# the project's own releases page -- Linux ships source-only, Windows/CUDA
# get prebuilt binaries). Pinned well above the 0.26.3 floor Maia's network
# format needs. OpenBLAS is picked up automatically by build.sh when its
# dev package is present, no extra flags needed.
FROM python:3.12-slim AS lc0-builder
ARG LC0_VERSION=v0.32.1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential pkg-config zlib1g-dev libopenblas-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir meson ninja

RUN git clone --depth 1 --shallow-submodules --recurse-submodules \
        --branch ${LC0_VERSION} https://github.com/LeelaChessZero/lc0.git /src/lc0 \
    && cd /src/lc0 \
    && INSTALL_PREFIX=/opt/lc0 ./build.sh \
    && install -Dm755 build/release/lc0 /opt/lc0/bin/lc0

# ---- Stage 2: fetch + verify Maia weights --------------------------------
FROM debian:bookworm-slim AS weights
RUN apt-get update && apt-get install -y --no-install-recommends curl file ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /weights
# GitHub's web UI sometimes serves .pb.gz already decompressed, which breaks
# the filename lc0 expects. curl against the raw content URL avoids that;
# `file` verifies each download is actually gzip before the build proceeds.
RUN set -eux; \
    for rating in 1100 1200 1300 1400 1500 1600 1700 1800 1900; do \
        curl -fL -o "maia-${rating}.pb.gz" \
            "https://raw.githubusercontent.com/CSSLab/maia-chess/master/maia_weights/maia-${rating}.pb.gz"; \
        file "maia-${rating}.pb.gz" | grep -q "gzip compressed data" || \
            { echo "maia-${rating}.pb.gz is not gzip -- download was corrupted or decompressed" >&2; exit 1; }; \
    done

# ---- Stage 3: runtime -----------------------------------------------------
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        libopenblas0 libgomp1 zlib1g stockfish \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=lc0-builder /opt/lc0/bin/lc0 /usr/local/bin/lc0
COPY --from=weights /weights /app/maia_weights
COPY chess_mcp_server.py smoke_test.py ./

ENV LC0_PATH=/usr/local/bin/lc0
ENV MAIA_WEIGHTS_DIR=/app/maia_weights
ENV STOCKFISH_PATH=/usr/games/stockfish
ENV ENGINE_KIND=maia

# Fails the image build (not the first voice command in prod) if lc0 and the
# weights don't actually cooperate to produce a legal move.
RUN python3 smoke_test.py

CMD ["python3", "chess_mcp_server.py"]

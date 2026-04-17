# syntax=docker/dockerfile:1.7
# Multi-stage build for Claude Total Memory
# Produces a single image used for both MCP server (stdio) and Dashboard (HTTP)

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# System deps for sentence-transformers / chromadb native wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ─────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLAUDE_MEMORY_DIR=/data \
    EMBEDDING_MODEL=all-MiniLM-L6-v2 \
    DASHBOARD_PORT=37737 \
    DASHBOARD_BIND=0.0.0.0 \
    MCP_TRANSPORT=http \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=3737

# curl for healthcheck, tini for signals
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Non-root user
RUN useradd -m -u 1000 memory && \
    mkdir -p /data && chown memory:memory /data

COPY --from=builder /install /usr/local
COPY --chown=memory:memory src/ ./src/
COPY --chown=memory:memory migrations/ ./migrations/
COPY --chown=memory:memory docker/reflection_daemon.py ./docker/reflection_daemon.py

USER memory

VOLUME ["/data"]
EXPOSE 3737 37737

ENTRYPOINT ["/usr/bin/tini", "--"]

# Default: run MCP server. Transport chosen via MCP_TRANSPORT env (http in container, stdio on host).
# Override for dashboard:     command: ["python", "src/dashboard.py"]
# Override for reflection:    command: ["python", "docker/reflection_daemon.py"]
CMD ["python", "src/server.py"]

#!/usr/bin/env bash
# Claude Total Memory — MCP stdio wrapper for Docker
#
# Claude Code spawns this script per-session and talks to it over stdin/stdout.
# It forwards to a short-lived Docker container sharing the `claude-memory-data` volume
# with the dashboard (see docker-compose.yml).
#
# Register in ~/.claude/settings.json (or Codex equivalent):
#
#   {
#     "mcpServers": {
#       "memory": {
#         "command": "/absolute/path/to/docker/run-mcp.sh"
#       }
#     }
#   }

set -euo pipefail

IMAGE="${CLAUDE_MEMORY_IMAGE:-claude-total-memory:latest}"
VOLUME="${CLAUDE_MEMORY_VOLUME:-claude-memory-data}"

# Ensure named volume exists (idempotent)
docker volume inspect "$VOLUME" >/dev/null 2>&1 || docker volume create "$VOLUME" >/dev/null

exec docker run --rm -i \
    --name "claude-memory-mcp-$$" \
    -v "${VOLUME}:/data" \
    -e CLAUDE_MEMORY_DIR=/data \
    -e EMBEDDING_MODEL="${EMBEDDING_MODEL:-all-MiniLM-L6-v2}" \
    -e OLLAMA_URL="${OLLAMA_URL:-http://host.docker.internal:11434}" \
    "$IMAGE" \
    python src/server.py

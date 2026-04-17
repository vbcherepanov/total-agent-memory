#!/usr/bin/env bash
# ===========================================
# Common hook utilities — Portable version
#
# Source this file: source "$(dirname "$0")/lib/common.sh"
#
# Environment variables (set by install.sh):
#   CLAUDE_MEMORY_INSTALL_DIR — path to claude-total-memory install
#   CLAUDE_MEMORY_DIR         — path to memory storage (~/.claude-memory)
#
# IMPORTANT: Reads stdin immediately on source
# because subshells lose the cached value
# ===========================================

# Read JSON from stdin IMMEDIATELY (before any subshell calls)
HOOK_INPUT=$(cat)

# Resolve install dir (where hooks/src live)
CLAUDE_MEMORY_INSTALL_DIR="${CLAUDE_MEMORY_INSTALL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)}"

# Resolve memory storage dir
CLAUDE_MEMORY_DIR="${CLAUDE_MEMORY_DIR:-$HOME/.claude-memory}"

# Python from venv (prefer install dir venv, fallback to system)
HOOK_PYTHON="${CLAUDE_MEMORY_INSTALL_DIR}/.venv/bin/python"
if [ ! -x "$HOOK_PYTHON" ]; then
    HOOK_PYTHON="python3"
fi

# Recovery and state directories
HOOK_RECOVERY_DIR="${CLAUDE_MEMORY_DIR}/recovery"
HOOK_STATE_DIR="${CLAUDE_MEMORY_DIR}/state"
HOOK_LOG_FILE="${CLAUDE_MEMORY_DIR}/hooks.log"

# ===========================================
# JSON parsing — uses python3 (jq may not be installed)
# ===========================================

# Extract field from cached JSON
# Usage: hook_get '.tool_input.command'
#        hook_get '.cwd'
hook_get() {
    local path="$1"
    echo "$HOOK_INPUT" | "$HOOK_PYTHON" -c "
import sys, json, functools
try:
    d = json.load(sys.stdin)
    keys = '$path'.strip('.').split('.')
    val = functools.reduce(lambda o, k: o[k] if isinstance(o, dict) else None, keys, d)
    if val is not None and val != '':
        print(val if isinstance(val, str) else json.dumps(val))
except:
    pass
" 2>/dev/null
}

# Get project name from cwd
hook_project_name() {
    local cwd
    cwd=$(hook_get 'cwd')
    [ -z "$cwd" ] && cwd="$PWD"
    basename "$cwd"
}

# Get git branch from cwd (read-only, safe)
hook_git_branch() {
    local cwd
    cwd=$(hook_get 'cwd')
    [ -z "$cwd" ] && cwd="$PWD"
    cd "$cwd" 2>/dev/null && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo ""
}

# Get short model name
hook_model_short() {
    local model
    model=$(hook_get 'model')
    case "$model" in
        *opus*)   echo "Opus" ;;
        *sonnet*) echo "Sonnet" ;;
        *haiku*)  echo "Haiku" ;;
        "")       echo "" ;;
        *)        echo "$model" ;;
    esac
}

# Build context string: "project @ branch"
hook_context() {
    local project branch ctx
    project=$(hook_project_name)
    branch=$(hook_git_branch)
    ctx="$project"
    [ -n "$branch" ] && ctx="$ctx @ $branch"
    echo "$ctx"
}

# Send macOS notification (no-op on Linux)
# Usage: hook_notify "message" "title" "sound"
hook_notify() {
    local message="${1:-}"
    local title="${2:-Claude Memory}"
    local sound="${3:-default}"

    if [ "$(uname)" = "Darwin" ]; then
        osascript -e "display notification \"$message\" with title \"$title\" sound name \"$sound\"" 2>/dev/null
    fi
}

# Log to file
# Usage: hook_log "message"
hook_log() {
    local message="$1"
    mkdir -p "$(dirname "$HOOK_LOG_FILE")"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $message" >> "$HOOK_LOG_FILE"
}

# Run a Python script from src/ directory (background, with logging)
#
# Two modes:
#   - Native:  execs via local venv Python.
#   - Docker:  proxies to `docker exec` in the running MCP container when
#              CLAUDE_MEMORY_DOCKER=1. Container name configurable via
#              CLAUDE_MEMORY_CONTAINER (default: claude-memory-mcp).
#
# Usage: hook_run_script "auto_self_improve.py" "error" "--description" "msg"
hook_run_script() {
    local script_name="$1"
    shift

    if [ "${CLAUDE_MEMORY_DOCKER:-0}" = "1" ]; then
        local container="${CLAUDE_MEMORY_CONTAINER:-claude-memory-mcp}"
        # Best-effort: if container isn't running, silently skip to keep hooks non-blocking
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${container}$"; then
            docker exec "$container" python "src/${script_name}" "$@" \
                >> "$HOOK_LOG_FILE" 2>&1 &
            return 0
        fi
        return 1
    fi

    local script_path="${CLAUDE_MEMORY_INSTALL_DIR}/src/${script_name}"
    if [ -f "$script_path" ]; then
        "$HOOK_PYTHON" "$script_path" "$@" >> "$HOOK_LOG_FILE" 2>&1 &
        return 0
    fi
    return 1
}

#!/usr/bin/env bash
#
# Claude Total Memory — Docker Installer
#
# Registers the Dockerized MCP server in Claude Code's settings and
# wires up hooks in Docker-exec mode.
#
# Prerequisites:
#   - Docker Engine / Docker Desktop running
#   - `docker compose up -d --build` already executed in this directory
#     (or will be executed by --with-compose flag below)
#
# Usage:
#   bash install-docker.sh                    # assumes stack is already up
#   bash install-docker.sh --with-compose     # also runs docker compose up -d --build
#
set -e

echo ""
echo "======================================================="
echo "  Claude Total Memory v6.0 — Docker Installer"
echo "======================================================="
echo ""

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

WITH_COMPOSE=0
for arg in "$@"; do
    case "$arg" in
        --with-compose) WITH_COMPOSE=1 ;;
    esac
done

# -- 1. Docker sanity --
echo "-> Step 1: Checking Docker..."
if ! command -v docker &>/dev/null; then
    echo "  ERROR: docker not found. Install Docker Desktop or Docker Engine first."
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "  ERROR: Docker daemon not reachable. Start Docker Desktop / systemctl start docker."
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "  ERROR: 'docker compose' plugin missing. Upgrade Docker Desktop or install compose-v2."
    exit 1
fi
echo "  OK: Docker ready"

# -- 2. Optional: build & start stack --
if [ "$WITH_COMPOSE" -eq 1 ]; then
    echo "-> Step 2: Building and starting the stack..."
    docker compose -f "$INSTALL_DIR/docker-compose.yml" up -d --build
    echo "  OK: Stack is up"
else
    echo "-> Step 2: Skipped (run 'docker compose up -d --build' yourself)."
fi

# -- 3. Wait for MCP healthz --
echo "-> Step 3: Waiting for MCP HTTP endpoint (http://127.0.0.1:3737)..."
ATTEMPTS=0
until curl -fsS http://127.0.0.1:3737/healthz >/dev/null 2>&1; do
    ATTEMPTS=$((ATTEMPTS + 1))
    if [ "$ATTEMPTS" -gt 60 ]; then
        echo "  WARN: MCP did not become healthy within 60s."
        echo "        Check: docker compose logs mcp"
        break
    fi
    sleep 1
done
[ "$ATTEMPTS" -le 60 ] && echo "  OK: MCP responding"

# -- 4. Register MCP server in Claude Code settings --
echo "-> Step 4: Registering MCP server in $CLAUDE_SETTINGS..."
mkdir -p "$HOME/.claude"

python3 - "$CLAUDE_SETTINGS" <<'PY'
import json, os, sys

settings_path = sys.argv[1]
new_server = {
    "type": "http",
    "url": "http://127.0.0.1:3737/mcp",
}

settings = {}
if os.path.exists(settings_path):
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except Exception:
        pass

settings.setdefault("mcpServers", {})
settings["mcpServers"]["memory"] = new_server

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)

print(f"  OK: MCP 'memory' → http://127.0.0.1:3737/mcp")
PY

# -- 5. Register hooks in Docker-exec mode --
echo "-> Step 5: Registering hooks (Docker mode)..."

HOOK_SESSION="$INSTALL_DIR/hooks/session-start.sh"
HOOK_SESSION_END="$INSTALL_DIR/hooks/session-end.sh"
HOOK_STOP="$INSTALL_DIR/hooks/on-stop.sh"
HOOK_BASH="$INSTALL_DIR/hooks/memory-trigger.sh"
HOOK_WRITE="$INSTALL_DIR/hooks/auto-capture.sh"

# Hooks read CLAUDE_MEMORY_DOCKER=1 at runtime and switch to `docker exec`.
# We wrap each via a small launcher that exports the env var.
LAUNCHER_DIR="$INSTALL_DIR/hooks/_docker-launchers"
mkdir -p "$LAUNCHER_DIR"
for src in "$HOOK_SESSION" "$HOOK_SESSION_END" "$HOOK_STOP" "$HOOK_BASH" "$HOOK_WRITE"; do
    name=$(basename "$src")
    dest="$LAUNCHER_DIR/$name"
    cat > "$dest" <<LAUNCHER
#!/usr/bin/env bash
export CLAUDE_MEMORY_DOCKER=1
export CLAUDE_MEMORY_CONTAINER="\${CLAUDE_MEMORY_CONTAINER:-claude-memory-mcp}"
exec "$src" "\$@"
LAUNCHER
    chmod +x "$dest"
done

python3 - "$CLAUDE_SETTINGS" "$LAUNCHER_DIR" <<'PY'
import json, os, sys

settings_path, launcher_dir = sys.argv[1], sys.argv[2]
settings = {}
if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)

settings.setdefault("hooks", {})
h = settings["hooks"]

def L(name):
    return {"type": "command", "command": os.path.join(launcher_dir, name)}

h["SessionStart"]  = [{"matcher": "", "hooks": [L("session-start.sh")]}]
h["SessionEnd"]    = [{"matcher": "", "hooks": [L("session-end.sh")]}]
h["Stop"]          = [{"matcher": "", "hooks": [L("on-stop.sh")]}]
h["PostToolUse"]   = [
    {"matcher": "Bash",       "hooks": [L("memory-trigger.sh")]},
    {"matcher": "Write|Edit", "hooks": [L("auto-capture.sh")]},
]

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)

print("  OK: Hooks registered (SessionStart, SessionEnd, Stop, PostToolUse:Bash/Write|Edit)")
PY

# -- 6. Summary --
echo ""
echo "======================================================="
echo ""
echo "  INSTALLED (Docker mode)"
echo ""
echo "  Services:"
echo "    MCP          http://127.0.0.1:3737/mcp"
echo "    Dashboard    http://127.0.0.1:37737"
echo "    Ollama       http://127.0.0.1:11434"
echo ""
echo "  Management:"
echo "    Logs:        docker compose logs -f"
echo "    Restart:     docker compose restart"
echo "    Stop:        docker compose down"
echo "    Wipe data:   docker compose down -v"
echo ""
echo "  Restart Claude Code, then /mcp → 'memory' should show Connected."
echo ""
echo "======================================================="

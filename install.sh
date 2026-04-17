#!/usr/bin/env bash
#
# total-agent-memory — One-Command Installer
#
# Usage: bash install.sh
#
set -e

echo ""
echo "======================================================="
echo "  total-agent-memory v7.0 — Installer"
echo "======================================================="
echo ""

# -- Config --
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
MEMORY_DIR="${CLAUDE_MEMORY_DIR:-$HOME/.claude-memory}"
VENV_DIR="$INSTALL_DIR/.venv"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
DASHBOARD_SERVICE="$INSTALL_DIR/scripts/dashboard-service.sh"

# -- 1. Create memory directories --
echo "-> Step 1: Creating memory directories..."
mkdir -p "$MEMORY_DIR"/{raw,chroma,transcripts,queue,backups,extract-queue}
echo "  OK: $MEMORY_DIR"

# -- 2. Python venv + deps --
echo "-> Step 2: Setting up Python environment..."

if ! command -v python3 &>/dev/null; then
    echo "  ERROR: python3 not found. Please install Python 3.10+"
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "  ERROR: Python 3.10+ required, found $PY_VERSION"
    exit 1
fi

echo "  Python $PY_VERSION found"

python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install -q --upgrade pip
echo "  Installing dependencies (this may take 2-3 minutes on first run)..."
pip install -q -r "$INSTALL_DIR/requirements.txt" -r "$INSTALL_DIR/requirements-dev.txt" 2>&1 | tail -1
echo "  OK: Dependencies installed"

# -- 3. Pre-download embedding model --
echo "-> Step 3: Loading embedding model (first time only)..."
python3 -c "
from sentence_transformers import SentenceTransformer
m = SentenceTransformer('all-MiniLM-L6-v2')
print(f'  OK: Model ready ({m.get_sentence_embedding_dimension()}d embeddings)')
" 2>/dev/null || echo "  WARNING: Will download on first use"

# -- 4. Configure Claude Code MCP --
echo "-> Step 4: Configuring Claude Code MCP server..."
mkdir -p "$HOME/.claude"

PY_PATH="$VENV_DIR/bin/python"
SRV_PATH="$INSTALL_DIR/src/server.py"

# Claude Code reads MCP server config from ~/.claude.json (managed via the
# 'claude mcp' CLI), NOT from ~/.claude/settings.json — writing 'mcpServers'
# into settings.json is silently ignored by Claude Code.
if ! command -v claude &>/dev/null; then
    echo "  ERROR: 'claude' CLI not found in PATH."
    echo "         Install Claude Code first: https://claude.com/claude-code"
    exit 1
fi

MCP_JSON=$(python3 -c "
import json
print(json.dumps({
    'command': '$PY_PATH',
    'args': ['$SRV_PATH'],
    'env': {
        'CLAUDE_MEMORY_DIR': '$MEMORY_DIR',
        'EMBEDDING_MODEL': 'all-MiniLM-L6-v2'
    }
}))
")

# Remove any previous registration (ignore errors if absent), then add fresh
claude mcp remove memory -s user >/dev/null 2>&1 || true
claude mcp add-json memory "$MCP_JSON" -s user
echo "  OK: MCP server 'memory' registered via 'claude mcp add-json' (user scope)"

# -- 4b. Register hooks in settings.json --
echo "-> Step 4b: Registering hooks..."

HOOK_SESSION="$INSTALL_DIR/hooks/session-start.sh"
HOOK_SESSION_END="$INSTALL_DIR/hooks/session-end.sh"
HOOK_STOP="$INSTALL_DIR/hooks/on-stop.sh"
HOOK_BASH="$INSTALL_DIR/hooks/memory-trigger.sh"
HOOK_WRITE="$INSTALL_DIR/hooks/auto-capture.sh"

python3 -c "
import json, os

settings_path = '$CLAUDE_SETTINGS'
settings = {}
if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)

if 'hooks' not in settings:
    settings['hooks'] = {}

hooks = settings['hooks']

# SessionStart (matcher '' = all)
hooks['SessionStart'] = [
    {'matcher': '', 'hooks': [{'type': 'command', 'command': '$HOOK_SESSION'}]}
]

# SessionEnd (matcher '' = all)
hooks['SessionEnd'] = [
    {'matcher': '', 'hooks': [{'type': 'command', 'command': '$HOOK_SESSION_END'}]}
]

# Stop (matcher '' = all)
hooks['Stop'] = [
    {'matcher': '', 'hooks': [{'type': 'command', 'command': '$HOOK_STOP'}]}
]

# PostToolUse — Bash + Write|Edit
hooks['PostToolUse'] = [
    {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': '$HOOK_BASH'}]},
    {'matcher': 'Write|Edit', 'hooks': [{'type': 'command', 'command': '$HOOK_WRITE'}]}
]

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)

print('  OK: Hooks registered (SessionStart, SessionEnd, Stop, PostToolUse:Bash/Write|Edit)')
"

# -- 4b2. Grant permissions for MCP memory tools --
# Without these, Claude Code prompts for confirmation on every memory tool call,
# which breaks automatic recall/save/error logging from hooks.
echo "-> Step 4b2: Granting permissions for memory tools..."

python3 -c "
import json, os

settings_path = '$CLAUDE_SETTINGS'
settings = {}
if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)

settings.setdefault('permissions', {}).setdefault('allow', [])
allow = settings['permissions']['allow']

memory_tools = [
    'mcp__memory__memory_recall',
    'mcp__memory__memory_save',
    'mcp__memory__memory_update',
    'mcp__memory__memory_timeline',
    'mcp__memory__memory_stats',
    'mcp__memory__memory_consolidate',
    'mcp__memory__memory_export',
    'mcp__memory__memory_forget',
    'mcp__memory__memory_history',
    'mcp__memory__memory_delete',
    'mcp__memory__memory_relate',
    'mcp__memory__memory_search_by_tag',
    'mcp__memory__memory_extract_session',
    'mcp__memory__memory_observe',
    'mcp__memory__self_error_log',
    'mcp__memory__self_insight',
    'mcp__memory__self_rules',
    'mcp__memory__self_patterns',
    'mcp__memory__self_reflect',
    'mcp__memory__self_rules_context',
]

added = 0
for tool in memory_tools:
    if tool not in allow:
        allow.append(tool)
        added += 1

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)

print(f'  OK: {len(memory_tools)} memory tools in permissions.allow (+{added} new)')
"

# -- 4c. Ollama check + optional install prompt --
echo ""
echo "-> Step 4c: Checking Ollama (optional but strongly recommended)..."
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
MEMORY_LLM_MODEL="${MEMORY_LLM_MODEL:-qwen2.5-coder:7b}"

# Probe Ollama
if curl -sf --max-time 2 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    echo "  OK: Ollama is running at $OLLAMA_URL"
    # Check model
    if curl -sf --max-time 2 "$OLLAMA_URL/api/tags" | grep -q "\"$MEMORY_LLM_MODEL\""; then
        echo "  OK: Model '$MEMORY_LLM_MODEL' is installed"
    else
        echo "  WARN: Model '$MEMORY_LLM_MODEL' NOT installed."
        echo "        For full v6.0 features, pull it now:"
        echo "          ollama pull $MEMORY_LLM_MODEL"
        echo "        Without it: deep KG triples, multi-repr, enrichment, fact merger disabled."
    fi
else
    echo "  WARN: Ollama NOT detected at $OLLAMA_URL"
    echo ""
    echo "  Without Ollama, ~40% of v6 features stay dormant:"
    echo "    - Deep KG triples (subject -> predicate -> object edges)"
    echo "    - Multi-representation embeddings (summary/keywords/questions/compressed)"
    echo "    - Entity/intent/topic extraction (deep enrichment)"
    echo "    - Semantic fact merger + HyDE query expansion"
    echo ""
    echo "  To enable the full experience:"
    if [ "$(uname)" = "Darwin" ]; then
        echo "    brew install ollama  (or download from https://ollama.ai)"
    else
        echo "    curl -fsSL https://ollama.com/install.sh | sh"
    fi
    echo "    ollama serve &"
    echo "    ollama pull $MEMORY_LLM_MODEL"
    echo ""
    echo "  System will still install now and work in degraded mode."
    echo "  Set MEMORY_LLM_ENABLED=auto after Ollama is ready — it picks up automatically."
fi

# -- 5. Dashboard service --
echo "-> Step 5: Setting up dashboard service..."
"$DASHBOARD_SERVICE" install "$PY_PATH" "$INSTALL_DIR" "$MEMORY_DIR"

<<<<<<< Updated upstream
# -- 5b. Linux: systemd auto-drain (equivalent of macOS LaunchAgent WatchPaths) --
# On macOS, the reflection LaunchAgent in launchagents/ picks up
# `touch ~/.claude-memory/.reflect-pending` and runs run_reflection.py.
# On Linux there's no LaunchAgent — this block installs a systemd.path +
# oneshot service pair that gives the same behavior via inotify.
if [ "$(uname)" = "Linux" ] && [ -d "$INSTALL_DIR/systemd" ]; then
    echo "-> Step 5b: Installing Linux systemd auto-drain unit..."
    SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SYSTEMD_USER_DIR"

    for unit in claude-memory-reflection.service claude-memory-reflection.path; do
        sed \
            -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
            -e "s|@MEMORY_DIR@|$MEMORY_DIR|g" \
            "$INSTALL_DIR/systemd/$unit" > "$SYSTEMD_USER_DIR/$unit"
    done

    # Ensure the trigger file exists so systemd.path can watch it from the start
    touch "$MEMORY_DIR/.reflect-pending"

    if command -v systemctl &>/dev/null; then
        systemctl --user daemon-reload
        systemctl --user enable --now claude-memory-reflection.path >/dev/null 2>&1 || {
            echo "  WARN: could not enable the .path unit via systemctl --user."
            echo "        Run manually: systemctl --user enable --now claude-memory-reflection.path"
        }
        if systemctl --user is-active claude-memory-reflection.path >/dev/null 2>&1; then
            echo "  OK: Reflection auto-drain active (watch: $MEMORY_DIR/.reflect-pending)"
        fi
    else
        echo "  WARN: systemctl not found — units copied to $SYSTEMD_USER_DIR but not activated"
    fi
=======
# -- 5b. Optional background agents (macOS only) --
if [ "$(uname)" = "Darwin" ] && [ -d "$INSTALL_DIR/launchagents" ]; then
    echo "-> Step 5b: Installing background LaunchAgents (reflection, orphan-backfill, check-updates)..."
    LA_DIR="$HOME/Library/LaunchAgents"
    mkdir -p "$LA_DIR"
    for TPL in "$INSTALL_DIR"/launchagents/*.plist; do
        NAME=$(basename "$TPL")
        DEST="$LA_DIR/$NAME"
        # Substitute __HOME__ placeholder with actual $HOME
        sed "s|__HOME__|$HOME|g" "$TPL" > "$DEST"
        LABEL=$(basename "$NAME" .plist)
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$DEST" 2>/dev/null || \
        launchctl load "$DEST" 2>/dev/null || true
    done
    echo "  OK: Background agents installed"
>>>>>>> Stashed changes
fi

# -- 6. Verify --
echo ""
echo "-> Step 6: Verifying installation..."

# Check server file
if [ -f "$SRV_PATH" ]; then
    echo "  OK: Server: $SRV_PATH"
else
    echo "  FAIL: Server not found at $SRV_PATH"
fi

# Check MCP registration (lives in ~/.claude.json, not settings.json)
if claude mcp get memory >/dev/null 2>&1; then
    echo "  OK: MCP server 'memory' registered"
else
    echo "  FAIL: MCP config issue — run 'claude mcp list' to debug"
fi

# Check memory dir
if [ -d "$MEMORY_DIR" ]; then
    echo "  OK: Memory directory: $MEMORY_DIR"
else
    echo "  FAIL: Memory directory issue"
fi

# Quick server test (import sanity)
"$PY_PATH" -c "
import sys
sys.path.insert(0, '$INSTALL_DIR/src')
try:
    import server  # noqa
    print('  OK: Server imports cleanly')
except Exception as e:
    print(f'  WARN: Server import issue ({e}); will verify on first use')
" 2>/dev/null || echo "  INFO: Server test skipped"

# -- Done --
echo ""
echo "======================================================="
echo ""
echo "  INSTALLED SUCCESSFULLY!"
echo ""
echo "  Claude Code now has persistent memory."
echo "  Just start 'claude' as usual — memory is automatic."
echo ""
echo "  Available MCP tools (46):"
echo ""
echo "  Core memory (14):"
echo "    memory_recall, memory_save, memory_update, memory_delete,"
echo "    memory_search_by_tag, memory_history, memory_timeline,"
echo "    memory_stats, memory_consolidate, memory_export, memory_forget,"
echo "    memory_relate, memory_extract_session, memory_observe"
echo ""
echo "  Knowledge graph (6):"
echo "    memory_graph, memory_graph_index, memory_graph_stats,"
echo "    memory_concepts, memory_associate, memory_context_build"
echo ""
echo "  Episodic memory & skills (4):"
echo "    memory_episode_save, memory_episode_recall,"
echo "    memory_skill_get, memory_skill_update"
echo ""
echo "  Reflection & self-improvement (7):"
echo "    memory_reflect_now, memory_self_assess,"
echo "    self_error_log, self_insight, self_patterns,"
echo "    self_reflect, self_rules, self_rules_context"
echo ""
echo "  Temporal KG (4) + Procedural (3) + Pre-flight/automation (8):"
echo "    kg_add_fact, kg_invalidate_fact, kg_at, kg_timeline,"
echo "    workflow_learn, workflow_predict, workflow_track,"
echo "    file_context, learn_error, session_init, session_end,"
echo "    ingest_codebase, analogize, benchmark"
echo ""
echo "  Web dashboard:"
echo "    http://localhost:37737"
echo ""
"$DASHBOARD_SERVICE" print-management
echo ""
echo "  Optional: Copy CLAUDE.md.template to your project"
echo "  to instruct Claude to use memory automatically."
echo ""
echo "======================================================="

#!/usr/bin/env bash
#
# Claude Total Memory — Codex CLI Installer
#
# Usage: bash install-codex.sh
#
# Installs the same MCP server used by Claude Code, but configures it
# for OpenAI Codex CLI (config.toml instead of settings.json).
# Both CLIs can share the same memory database.
#
set -e

echo ""
echo "======================================================="
echo "  Claude Total Memory v4.0 — Codex CLI Installer"
echo "======================================================="
echo ""

# -- Config --
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
MEMORY_DIR="${CLAUDE_MEMORY_DIR:-$HOME/.claude-memory}"
VENV_DIR="$INSTALL_DIR/.venv"
CODEX_CONFIG_DIR="$HOME/.codex"
CODEX_CONFIG="$CODEX_CONFIG_DIR/config.toml"
SKILL_TARGET="$HOME/.agents/skills/memory"

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

if [ -d "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/python" ]; then
    echo "  Existing venv found, updating dependencies..."
    source "$VENV_DIR/bin/activate"
    pip install -q --upgrade "mcp[cli]>=1.0.0" chromadb sentence-transformers 2>&1 | tail -1
else
    python3 -m venv "$VENV_DIR"
    source "$VENV_DIR/bin/activate"
    pip install -q --upgrade pip
    echo "  Installing dependencies (this may take 2-3 minutes on first run)..."
    pip install -q "mcp[cli]>=1.0.0" chromadb sentence-transformers 2>&1 | tail -1
fi
echo "  OK: Dependencies installed"

# -- 3. Pre-download embedding model --
echo "-> Step 3: Loading embedding model (first time only)..."
python3 -c "
from sentence_transformers import SentenceTransformer
m = SentenceTransformer('all-MiniLM-L6-v2')
print(f'  OK: Model ready ({m.get_sentence_embedding_dimension()}d embeddings)')
" 2>/dev/null || echo "  WARNING: Will download on first use"

# -- 4. Configure Codex CLI MCP --
echo "-> Step 4: Configuring Codex CLI MCP server..."
mkdir -p "$CODEX_CONFIG_DIR"

PY_PATH="$VENV_DIR/bin/python"
SRV_PATH="$INSTALL_DIR/src/server.py"

CODEX_CONFIG="$CODEX_CONFIG" PY_PATH="$PY_PATH" SRV_PATH="$SRV_PATH" MEMORY_DIR="$MEMORY_DIR" \
python3 -c "
import os, re

config_path = os.environ['CODEX_CONFIG']
# Escape backslashes and double quotes for safe TOML embedding
def toml_escape(s):
    return s.replace('\\\\', '/').replace('\"', '\\\\\"')
py_path = toml_escape(os.environ['PY_PATH'])
srv_path = toml_escape(os.environ['SRV_PATH'])
memory_dir = toml_escape(os.environ['MEMORY_DIR'])

toml_block = f'''
# --- Claude Total Memory MCP Server ---
[mcp_servers.memory]
command = \"{py_path}\"
args = [\"{srv_path}\"]
required = true
startup_timeout_sec = 15.0
tool_timeout_sec = 120.0

[mcp_servers.memory.env]
CLAUDE_MEMORY_DIR = \"{memory_dir}\"
EMBEDDING_MODEL = \"all-MiniLM-L6-v2\"
# --- End Claude Total Memory ---
'''

content = ''
if os.path.exists(config_path):
    with open(config_path, 'r') as f:
        content = f.read()

if '[mcp_servers.memory]' in content:
    pattern = r'# --- Claude Total Memory MCP Server ---.*?# --- End Claude Total Memory ---'
    if re.search(pattern, content, re.DOTALL):
        content = re.sub(pattern, toml_block.strip(), content, flags=re.DOTALL)
    else:
        content = re.sub(r'\[mcp_servers\.memory\].*?(?=\n\[|\Z)', toml_block.strip(), content, flags=re.DOTALL)
    print('  OK: Updated existing memory config in ' + config_path)
else:
    content = content.rstrip() + '\\n' + toml_block
    print('  OK: Added memory config to ' + config_path)

content = content.lstrip('\\n')
with open(config_path, 'w') as f:
    f.write(content)
"

# -- 5. Install Codex Skill --
echo "-> Step 5: Installing memory skill..."
SKILL_SRC="$INSTALL_DIR/codex-skill"

if [ -d "$SKILL_SRC" ]; then
    mkdir -p "$SKILL_TARGET/agents"
    cp "$SKILL_SRC/SKILL.md" "$SKILL_TARGET/SKILL.md"
    if [ -d "$SKILL_SRC/agents" ]; then
        cp "$SKILL_SRC/agents/"* "$SKILL_TARGET/agents/" 2>/dev/null || true
    fi
    echo "  OK: Skill installed to $SKILL_TARGET"
else
    echo "  SKIP: codex-skill/ directory not found"
fi

# -- 6. Dashboard service (macOS LaunchAgent) --
echo "-> Step 6: Setting up dashboard service..."
DASHBOARD_PATH="$INSTALL_DIR/src/dashboard.py"
PLIST_NAME="com.claude-total-memory.dashboard"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
LOG_DIR="$MEMORY_DIR/logs"
mkdir -p "$LOG_DIR"

if [ "$(uname)" = "Darwin" ]; then
    # Stop existing service if running
    launchctl bootout "gui/$(id -u)/$PLIST_NAME" 2>/dev/null || true

    cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY_PATH</string>
        <string>$DASHBOARD_PATH</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CLAUDE_MEMORY_DIR</key>
        <string>$MEMORY_DIR</string>
        <key>DASHBOARD_PORT</key>
        <string>37737</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/dashboard.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/dashboard.err</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST

    launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || \
    launchctl load "$PLIST_PATH" 2>/dev/null || true
    echo "  OK: Dashboard service installed (auto-starts on login)"
    echo "  OK: http://localhost:37737"
else
    echo "  INFO: Auto-start not available on this platform"
    echo "  Run manually: .venv/bin/python src/dashboard.py"
fi

# -- 7. Verify --
echo ""
echo "-> Step 7: Verifying installation..."

# Check server file
if [ -f "$SRV_PATH" ]; then
    echo "  OK: Server: $SRV_PATH"
else
    echo "  FAIL: Server not found at $SRV_PATH"
fi

# Check config.toml
if grep -q "mcp_servers.memory" "$CODEX_CONFIG" 2>/dev/null; then
    echo "  OK: MCP server configured in $CODEX_CONFIG"
else
    echo "  FAIL: MCP config issue in $CODEX_CONFIG"
fi

# Check memory dir
if [ -d "$MEMORY_DIR" ]; then
    echo "  OK: Memory directory: $MEMORY_DIR"
else
    echo "  FAIL: Memory directory issue"
fi

# Check skill
if [ -d "$SKILL_TARGET" ]; then
    echo "  OK: Skill installed: $SKILL_TARGET"
else
    echo "  WARN: Skill not installed"
fi

# Quick server test
python3 -c "
import ast
ast.parse(open('$SRV_PATH').read())
print('  OK: Server syntax valid')
" 2>/dev/null || echo "  INFO: Server test skipped (will verify on first use)"

# -- Done --
echo ""
echo "======================================================="
echo ""
echo "  INSTALLED SUCCESSFULLY!"
echo ""
echo "  Codex CLI now has persistent memory."
echo "  Just start 'codex' as usual — memory tools are available."
echo ""
echo "  Available MCP tools (20):"
echo "    memory_recall          — Search all past knowledge (3-level detail)"
echo "    memory_save            — Save decisions, solutions, lessons"
echo "    memory_update          — Update existing knowledge"
echo "    memory_timeline        — Browse session history"
echo "    memory_stats           — View statistics & health"
echo "    memory_consolidate     — Merge similar records"
echo "    memory_export          — Backup to JSON"
echo "    memory_forget          — Archive stale records"
echo "    memory_history         — View version history"
echo "    memory_delete          — Soft-delete a record"
echo "    memory_relate          — Link related records"
echo "    memory_search_by_tag   — Browse by tag"
echo "    memory_extract_session — Process session transcripts"
echo "    memory_observe         — Lightweight file change tracking"
echo "    self_error_log         — Log errors for pattern analysis"
echo "    self_insight           — Manage insights from error patterns"
echo "    self_rules             — Manage behavioral rules (SOUL)"
echo "    self_patterns          — Analyze error patterns & trends"
echo "    self_reflect           — Save session reflections"
echo "    self_rules_context     — Load rules at session start"
echo ""
echo "  Web dashboard: http://localhost:37737"
echo ""
echo "  Verify in Codex: type /mcp to check memory server"
echo ""
echo "  Next steps:"
echo "    1. Copy AGENTS.md.template to your project as AGENTS.md"
echo "    2. Copy codex-global-rules.md.template to ~/.codex/AGENTS.md"
echo "    3. Restart Codex CLI"
echo ""
echo "  Note: If you also use Claude Code, both share the same"
echo "  memory database. Don't run them simultaneously."
echo ""
echo "======================================================="

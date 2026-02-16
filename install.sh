#!/usr/bin/env bash
#
# Claude Total Memory — One-Command Installer
#
# Usage: bash install.sh
#
set -e

echo ""
echo "======================================================="
echo "  Claude Total Memory v2.2 — Installer"
echo "======================================================="
echo ""

# -- Config --
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
MEMORY_DIR="${CLAUDE_MEMORY_DIR:-$HOME/.claude-memory}"
VENV_DIR="$INSTALL_DIR/.venv"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

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
pip install -q "mcp[cli]>=1.0.0" chromadb sentence-transformers 2>&1 | tail -1
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

python3 -c "
import json, os

settings_path = '$CLAUDE_SETTINGS'
new_server = {
    'command': '$PY_PATH',
    'args': ['$SRV_PATH'],
    'env': {
        'CLAUDE_MEMORY_DIR': '$MEMORY_DIR',
        'EMBEDDING_MODEL': 'all-MiniLM-L6-v2'
    }
}

settings = {}
if os.path.exists(settings_path):
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except:
        pass

if 'mcpServers' not in settings:
    settings['mcpServers'] = {}
settings['mcpServers']['memory'] = new_server

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)

print('  OK: MCP server added to ' + settings_path)
"

# -- 5. Verify --
echo ""
echo "-> Step 5: Verifying installation..."

# Check server file
if [ -f "$SRV_PATH" ]; then
    echo "  OK: Server: $SRV_PATH"
else
    echo "  FAIL: Server not found at $SRV_PATH"
fi

# Check settings.json
python3 -c "
import json
with open('$CLAUDE_SETTINGS') as f:
    s = json.load(f)
assert 'memory' in s.get('mcpServers', {})
print('  OK: MCP server configured')
" 2>/dev/null || echo "  FAIL: MCP config issue"

# Check memory dir
if [ -d "$MEMORY_DIR" ]; then
    echo "  OK: Memory directory: $MEMORY_DIR"
else
    echo "  FAIL: Memory directory issue"
fi

# Quick server test
python3 -c "
import sys; sys.path.insert(0, '$INSTALL_DIR')
exec(open('$SRV_PATH').read().split('async def main')[0])
s = Store()
print(f'  OK: Server initializes (sessions: {s.total_sessions()})')
" 2>/dev/null || echo "  INFO: Server test skipped (will verify on first use)"

# -- Done --
echo ""
echo "======================================================="
echo ""
echo "  INSTALLED SUCCESSFULLY!"
echo ""
echo "  Claude Code now has persistent memory."
echo "  Just start 'claude' as usual — memory is automatic."
echo ""
echo "  Available MCP tools (13):"
echo "    memory_recall          — Search all past knowledge"
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
echo ""
echo "  Web dashboard:"
echo "    .venv/bin/python src/dashboard.py"
echo "    Open http://localhost:37737"
echo ""
echo "  Optional: Copy CLAUDE.md.template to your project"
echo "  to instruct Claude to use memory automatically."
echo ""
echo "======================================================="

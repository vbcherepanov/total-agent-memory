#!/usr/bin/env bash
#
# Claude Total Memory — Manual Setup
# For users who prefer step-by-step control.
#
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
MEM="${CLAUDE_MEMORY_DIR:-$HOME/.claude-memory}"

echo "╔═════════════════════════════════════════════════════╗"
echo "║  Claude Total Memory v2.0 — Manual Setup            ║"
echo "╚═════════════════════════════════════════════════════╝"
echo ""

# 1. Dirs
echo "→ Creating directories..."
mkdir -p "$MEM"/{raw,chroma,backups}

# 2. Venv
echo "→ Creating Python venv..."
python3 -m venv "$DIR/.venv"
source "$DIR/.venv/bin/activate"

echo "→ Installing dependencies..."
pip install -q --upgrade pip
pip install -q "mcp[cli]>=1.0.0" chromadb sentence-transformers

# 3. Pre-download model
echo "→ Loading embedding model..."
python3 -c "
from sentence_transformers import SentenceTransformer
m = SentenceTransformer('all-MiniLM-L6-v2')
print(f'  Model ready: {m.get_sentence_embedding_dimension()}d embeddings')
" 2>/dev/null || echo "  (will load on first use)"

# 4. MCP config
PY="$DIR/.venv/bin/python"
SRV="$DIR/src/server.py"

echo ""
echo "═══════════════════════════════════════════════════════"
echo "✅ INSTALLED!"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "Add MCP server to ~/.claude/settings.json:"
echo ""
echo '{'
echo '  "mcpServers": {'
echo '    "memory": {'
echo "      \"command\": \"$PY\","
echo "      \"args\": [\"$SRV\"],"
echo '      "env": {'
echo "        \"CLAUDE_MEMORY_DIR\": \"$MEM\","
echo '        "EMBEDDING_MODEL": "all-MiniLM-L6-v2"'
echo '      }'
echo '    }'
echo '  }'
echo '}'
echo ""
echo "That's it. Start claude as usual — memory is automatic."
echo ""
echo "Optional: Copy CLAUDE.md.template to your project"
echo "to instruct Claude to use memory automatically."
echo ""

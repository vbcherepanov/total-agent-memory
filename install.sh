#!/usr/bin/env bash
#
# total-agent-memory — One-Command Installer (multi-IDE)
#
# Usage:
#   bash install.sh                       # = --ide claude-code (default)
#   bash install.sh --ide claude-code
#   bash install.sh --ide codex
#   bash install.sh --ide cursor
#   bash install.sh --ide claude-desktop
#   bash install.sh --ide cline
#   bash install.sh --ide continue
#   bash install.sh --ide aider
#   bash install.sh --ide windsurf
#   bash install.sh --ide gemini-cli
#   bash install.sh --ide opencode
#   bash install.sh --uninstall           # remove background services (per OS)
#
# Env:
#   INSTALL_TEST_MODE=1   skip pip install, model pre-download, dashboard
#                         service, LaunchAgents (for test harness)
#   TAM_MEMORY_DIR=... override memory directory (default: ~/.tam).
#                         Legacy CLAUDE_MEMORY_DIR still respected.
#   OLLAMA_URL=...        override Ollama probe URL
#   MEMORY_LLM_MODEL=...  override expected model name
#   FAKE_UNAME=Linux      override uname() for tests (Linux|Darwin)
#   XDG_CONFIG_HOME=...   override systemd --user target dir
#   INSTALL_OVERWRITE_HOOKS=1   force-overwrite existing files in ~/.claude/hooks/
#                               (default: skip existing, preserve user customizations)
#
set -e

# Allow tests to override uname without faking binaries
OS_NAME="${FAKE_UNAME:-$(uname)}"

# -- Parse CLI args --
IDE="claude-code"
UNINSTALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --ide=*)
            IDE="${1#*=}"
            shift
            ;;
        --ide)
            IDE="$2"
            shift 2
            ;;
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        -h|--help)
            sed -n '2,18p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            echo "Usage: bash install.sh [--ide claude-code|claude-desktop|cursor|gemini-cli|opencode|codex|cline|continue|aider|windsurf] [--uninstall]" >&2
            exit 2
            ;;
    esac
done

case "$IDE" in
    claude-code|claude-desktop|cursor|gemini-cli|opencode|codex|cline|continue|aider|windsurf) ;;
    *)
        echo "ERROR: unsupported --ide value: $IDE" >&2
        echo "Supported: claude-code, claude-desktop, codex, cursor, cline, continue, aider, windsurf, gemini-cli, opencode" >&2
        exit 2
        ;;
esac

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
RELEASE_VERSION=$(sed -n 's/^VERSION = "\([0-9.]*\)"/\1/p' "$INSTALL_DIR/src/version.py")
[ -n "$RELEASE_VERSION" ] || { echo "ERROR: release version is missing" >&2; exit 1; }
echo ""
echo "======================================================="
echo "  total-agent-memory v$RELEASE_VERSION — Installer (IDE: $IDE)"
echo "======================================================="
echo ""

# -- Config --
# Resolution: TAM_MEMORY_DIR > legacy CLAUDE_MEMORY_DIR > ~/.tam > migrate ~/.claude-memory > fresh ~/.tam
if [ -n "$TAM_MEMORY_DIR" ]; then
    MEMORY_DIR="$TAM_MEMORY_DIR"
elif [ -n "$CLAUDE_MEMORY_DIR" ]; then
    MEMORY_DIR="$CLAUDE_MEMORY_DIR"
    echo "  ⚠ CLAUDE_MEMORY_DIR is deprecated, please switch to TAM_MEMORY_DIR" >&2
elif [ -d "$HOME/.tam" ]; then
    MEMORY_DIR="$HOME/.tam"
elif [ -d "$HOME/.claude-memory" ] && [ ! -L "$HOME/.claude-memory" ]; then
    # Legacy install detected — migrate to ~/.tam + leave symlink
    echo "  → Migrating ~/.claude-memory → ~/.tam (symlink kept for backward-compat)"
    mv "$HOME/.claude-memory" "$HOME/.tam"
    ln -s "$HOME/.tam" "$HOME/.claude-memory"
    MEMORY_DIR="$HOME/.tam"
else
    MEMORY_DIR="$HOME/.tam"
fi
VENV_DIR="$INSTALL_DIR/.venv"

# The dashboard service this installer sets up: launchagents/ and systemd/ carry the same names.
DASHBOARD_PORT_DEFAULT=37737
DASHBOARD_LAUNCHD_LABEL="com.total-agent-memory.dashboard"
DASHBOARD_SYSTEMD_UNIT="claude-memory-dashboard.service"

# Test mode: skip heavy steps (pip, model DL, launchctl, dashboard install)
TEST_MODE="${INSTALL_TEST_MODE:-0}"
SKIP_DEPENDENCY_SETUP=0
case "$TEST_MODE" in
    1|skip-heavy) SKIP_DEPENDENCY_SETUP=1 ;;
    0) ;;
    *) echo "ERROR: INSTALL_TEST_MODE must be 0, 1 or skip-heavy" >&2; exit 1 ;;
esac

# -----------------------------------------------------------------
# Linux helpers: WSL detection + systemd --user services setup
# -----------------------------------------------------------------

is_wsl() {
    # WSL1/WSL2 expose "microsoft" in /proc/version
    grep -qi microsoft /proc/version 2>/dev/null
}

systemd_user_available() {
    # systemctl --user works only when the user session bus is up.
    # On WSL2 this requires systemd=true in /etc/wsl.conf (WSLg).
    command -v systemctl >/dev/null 2>&1 && \
        systemctl --user show-environment >/dev/null 2>&1
}

# List of unit files this installer manages. Keep in sync with systemd/.
_systemd_units() {
    cat <<'EOF'
claude-memory-reflection.service
claude-memory-reflection.path
claude-memory-dashboard.service
claude-memory-orphan-backfill.service
claude-memory-orphan-backfill.timer
claude-memory-check-updates.service
claude-memory-check-updates.timer
EOF
}

# Services that should be enabled + started after install.
_systemd_enable_units() {
    cat <<'EOF'
claude-memory-reflection.path
claude-memory-dashboard.service
claude-memory-orphan-backfill.timer
claude-memory-check-updates.timer
EOF
}

install_systemd_user_services() {
    local src_dir="$INSTALL_DIR/systemd"
    local target_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

    if [ ! -d "$src_dir" ]; then
        echo "  SKIP: $src_dir missing (no systemd templates)"
        return 0
    fi

    mkdir -p "$target_dir"
    mkdir -p "$MEMORY_DIR/logs"

    # Substitute install-time paths in templates and drop them in target_dir.
    local unit
    while IFS= read -r unit; do
        [ -z "$unit" ] && continue
        local tpl="$src_dir/$unit"
        if [ ! -f "$tpl" ]; then
            echo "  WARN: template missing: $tpl"
            continue
        fi
        python3 "$INSTALL_DIR/scripts/render_template.py" --mode systemd \
            --var "@INSTALL_DIR@=$INSTALL_DIR" --var "@MEMORY_DIR@=$MEMORY_DIR" --var "@HOME@=$HOME" \
            < "$tpl" > "$target_dir/$unit"
    done <<EOF
$(_systemd_units)
EOF

    echo "  OK: systemd units copied to $target_dir"

    # Activation requires a live user session bus. On WSL2 without systemd
    # enabled, or inside some CI sandboxes, systemctl --user will fail —
    # we still keep the files on disk so the user can enable them later.
    if systemd_user_available; then
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        local en
        while IFS= read -r en; do
            [ -z "$en" ] && continue
            systemctl --user enable --now "$en" >/dev/null 2>&1 \
                && echo "  OK: enabled $en" \
                || echo "  WARN: failed to enable $en (systemctl --user enable returned non-zero)"
        done <<EOF
$(_systemd_enable_units)
EOF
    else
        if is_wsl; then
            echo "  WARN: systemd --user not available (WSL2 without systemd=true in /etc/wsl.conf)."
            echo "        Units copied to $target_dir — enable manually after enabling systemd:"
            echo "          printf '[boot]\\nsystemd=true\\n' | sudo tee -a /etc/wsl.conf"
            echo "          wsl.exe --shutdown   # from Windows, then reopen the shell"
            echo "          systemctl --user daemon-reload"
            echo "          systemctl --user enable --now claude-memory-reflection.path"
        else
            echo "  WARN: systemctl --user bus not reachable — units staged at $target_dir."
            echo "        Start the user manager (e.g. 'loginctl enable-linger $USER'),"
            echo "        then: systemctl --user daemon-reload && systemctl --user enable --now claude-memory-reflection.path"
        fi
    fi
}

uninstall_systemd_user_services() {
    local target_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

    if systemd_user_available; then
        local en
        while IFS= read -r en; do
            [ -z "$en" ] && continue
            systemctl --user disable --now "$en" >/dev/null 2>&1 || true
        done <<EOF
$(_systemd_enable_units)
EOF
    fi

    local unit
    while IFS= read -r unit; do
        [ -z "$unit" ] && continue
        rm -f "$target_dir/$unit"
    done <<EOF
$(_systemd_units)
EOF

    if systemd_user_available; then
        systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
    echo "  OK: systemd units removed from $target_dir"
}

uninstall_launch_agents() {
    local la_dir="$HOME/Library/LaunchAgents"
    local NAME LABEL
    if [ ! -d "$la_dir" ]; then
        return 0
    fi
    for TPL in "$INSTALL_DIR"/launchagents/*.plist; do
        [ -f "$TPL" ] || continue
        NAME=$(basename "$TPL")
        LABEL=$(basename "$NAME" .plist)
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        rm -f "$la_dir/$NAME"
    done
    echo "  OK: LaunchAgents removed from $la_dir"
}

# -- Handle --uninstall early and exit --
if [ "$UNINSTALL" = "1" ]; then
    echo ""
    echo "-> Uninstalling total-agent-memory background services..."
    if [ "$OS_NAME" = "Darwin" ]; then
        uninstall_launch_agents
    elif [ "$OS_NAME" = "Linux" ]; then
        uninstall_systemd_user_services
    else
        echo "  SKIP: no background services to remove on $OS_NAME"
    fi
    echo "  Note: MCP config entries and memory dir were kept. Remove manually if desired:"
    echo "    - $HOME/.claude.json (mcpServers.memory) and $HOME/.claude/settings.json (hooks)"
    echo "      or run: PYTHONPATH=$INSTALL_DIR/src python3 -m setup_wizard.register --unregister --client $IDE"
    echo "    - $MEMORY_DIR"
    exit 0
fi

# -- 1. Create memory directories --
echo "-> Step 1: Creating memory directories..."
mkdir -p "$MEMORY_DIR"/{raw,chroma,transcripts,queue,backups,extract-queue}
echo "  OK: $MEMORY_DIR"

# -- 2. Python venv + deps --
echo "-> Step 2: Setting up Python environment..."

if ! command -v python3 &>/dev/null; then
    echo "  ERROR: python3 not found. Please install Python 3.11+"
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    echo "  ERROR: Python 3.11+ required, found $PY_VERSION"
    exit 1
fi

echo "  Python $PY_VERSION found"

# Pre-flight: many Debian/Ubuntu/WSL images split out the `venv` module into
# a separate `python3-venv` package. Without it `python3 -m venv` errors on
# ensurepip with a cryptic message that does not mention the actual fix.
# Detect and surface a clear, actionable hint before failing.
if [ "$SKIP_DEPENDENCY_SETUP" = "0" ] && ! [ -d "$VENV_DIR" ]; then
    if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
        echo "  ERROR: python3 venv module is missing (ensurepip unavailable)."
        echo "  Install it first, then re-run this script:"
        echo "    Debian/Ubuntu/WSL:  sudo apt install python${PY_VERSION}-venv"
        echo "    Fedora/RHEL:        sudo dnf install python3-virtualenv"
        echo "    Arch:               (already bundled with the python package)"
        echo "    Alpine:             apk add python3 py3-virtualenv"
        echo "    macOS (Homebrew):   brew install python@${PY_VERSION}"
        exit 1
    fi
fi

if [ "$SKIP_DEPENDENCY_SETUP" = "1" ]; then
    echo "  SKIP (test mode): venv creation and pip install"
    PY_PATH="$(command -v python3)"
else
    if [ -d "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/python" ]; then
        echo "  Existing venv found, updating dependencies..."
        # shellcheck disable=SC1091
        source "$VENV_DIR/bin/activate"
        pip install -q --upgrade -r "$INSTALL_DIR/requirements.txt"
    else
        python3 -m venv "$VENV_DIR"
        # shellcheck disable=SC1091
        source "$VENV_DIR/bin/activate"
        pip install -q --upgrade pip
        echo "  Installing dependencies (this may take 2-3 minutes on first run)..."
        pip install -q -r "$INSTALL_DIR/requirements.txt"
    fi
    # v9 — editable install registers `[project.scripts]` entry-points
    # (total-agent-memory, tam, tam-lookup, lookup-memory + legacy:
    # claude-total-memory, ctm-lookup) on PATH inside the venv.
    echo "  Installing total-agent-memory package (registers tam / tam-lookup / lookup-memory + legacy claude-total-memory / ctm-lookup)..."
    pip install -q -e "$INSTALL_DIR"
    echo "  OK: Dependencies installed"
    PY_PATH="$VENV_DIR/bin/python"
fi

SRV_PATH="$INSTALL_DIR/src/server.py"

# -- 3. Pre-download embedding model --
echo "-> Step 3: Loading embedding model (first time only)..."
if [ "$SKIP_DEPENDENCY_SETUP" = "1" ]; then
    echo "  SKIP (test mode): embedding model pre-download"
else
    # Warm the model the server actually uses. This warmed the
    # sentence-transformers fallback (all-MiniLM-L6-v2, English) through the
    # *system* python3 — the wrong model, in the wrong interpreter, and since
    # 13.0.2 sentence-transformers is not in the base install at all.
    "$PY_PATH" -c "
import os
from fastembed import TextEmbedding
name = os.environ.get('FASTEMBED_MODEL', 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')
TextEmbedding(name)
print(f'  OK: Model ready ({name})')
" 2>/dev/null || echo "  WARNING: Will download on first use"
fi

# =============================================================
# Step 4: Register MCP server with the chosen IDE
# =============================================================
# setup_wizard.register is the one implementation of client registration
# (config paths and formats, Claude Code hooks, skills); the wizard and the
# npm wrapper use it too. It parses every config before writing any of them.
echo "-> Step 4: Registering the MCP server with $IDE..."
REGISTER_ARGS=(--client "$IDE" --memory-dir "$MEMORY_DIR" --command "$PY_PATH" --arg "$SRV_PATH")
if [ "$IDE" = "codex" ]; then
    REGISTER_ARGS+=(--env "CLAUDE_MEMORY_DIR=$MEMORY_DIR" --env MEMORY_MODE=fast
                    --env MEMORY_TRIPLE_TIMEOUT_SEC=120 --env MEMORY_ENRICH_TIMEOUT_SEC=90
                    --env MEMORY_REPR_TIMEOUT_SEC=120 --env MEMORY_TRIPLE_MAX_PREDICT=512)
fi
if [ "$IDE" = "claude-code" ]; then
    REGISTER_ARGS+=(--hooks)
else
    REGISTER_ARGS+=(--no-hooks)
fi
if [ "${INSTALL_OVERWRITE_HOOKS:-0}" = "1" ]; then
    REGISTER_ARGS+=(--overwrite-hooks)
fi
PYTHONPATH="$INSTALL_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PY_PATH" -m setup_wizard.register "${REGISTER_ARGS[@]}"

# -- 4c. Ollama check + optional install prompt --
echo ""
echo "-> Step 4c: Checking Ollama (optional but strongly recommended)..."
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
MEMORY_LLM_MODEL="${MEMORY_LLM_MODEL:-qwen2.5-coder:7b}"

if [ "$TEST_MODE" = "1" ]; then
    echo "  SKIP (test mode): Ollama probe"
elif curl -sf --max-time 2 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    echo "  OK: Ollama is running at $OLLAMA_URL"
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
    if [ "$OS_NAME" = "Darwin" ]; then
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

# -- 5. Background services (dashboard + reflection + orphan-backfill + check-updates) --
# Step 5 (old `dashboard-service.sh install`) was removed in v12.3 — it
# created a `com.claude-total-memory.dashboard` plist that conflicted with
# the canonical `com.total-agent-memory.dashboard.plist` shipped under
# launchagents/. Both fought for port 37737. dashboard-service.sh remains
# in scripts/ for manual use but isn't invoked automatically anymore.
# Which service manager got the dashboard; empty when this run installed no dashboard service.
DASHBOARD_SERVICE_KIND=""
if [ "$TEST_MODE" != "1" ] && [ "$IDE" = "claude-code" ] && [ "$OS_NAME" = "Darwin" ] && [ -d "$INSTALL_DIR/launchagents" ]; then
    echo "-> Step 5: Installing background LaunchAgents (dashboard, reflection, orphan-backfill, check-updates)..."
    LA_DIR="$HOME/Library/LaunchAgents"
    mkdir -p "$LA_DIR"
    mkdir -p "$MEMORY_DIR/logs"
    for TPL in "$INSTALL_DIR"/launchagents/*.plist; do
        NAME=$(basename "$TPL")
        DEST="$LA_DIR/$NAME"
        # Substitute placeholders so plists work regardless of git checkout
        # name (used to hardcode `claude-memory-server`) and memory dir
        # (used to hardcode `~/.claude-memory` instead of `~/.tam`).
        python3 "$INSTALL_DIR/scripts/render_template.py" --mode xml \
            --var "__INSTALL_DIR__=$INSTALL_DIR" --var "__MEMORY_DIR__=$MEMORY_DIR" --var "__HOME__=$HOME" \
            < "$TPL" > "$DEST"
        LABEL=$(basename "$NAME" .plist)
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$DEST" 2>/dev/null || \
        launchctl load "$DEST" 2>/dev/null || true
    done
    echo "  OK: Background agents installed"
    DASHBOARD_SERVICE_KIND="launchd"
elif [ "$IDE" = "claude-code" ] && [ "$OS_NAME" = "Linux" ] && [ -d "$INSTALL_DIR/systemd" ]; then
    echo "-> Step 5: Installing background systemd --user services (reflection, dashboard, orphan-backfill, check-updates)..."
    install_systemd_user_services
    DASHBOARD_SERVICE_KIND="systemd"
fi

# -- 6. Verify --
echo ""
echo "-> Step 6: Verifying installation..."

if [ -f "$SRV_PATH" ]; then
    echo "  OK: Server: $SRV_PATH"
else
    echo "  FAIL: Server not found at $SRV_PATH"
fi

if [ -d "$MEMORY_DIR" ]; then
    echo "  OK: Memory directory: $MEMORY_DIR"
else
    echo "  FAIL: Memory directory issue"
fi

if [ "$TEST_MODE" != "1" ]; then
    "$PY_PATH" -c "
import sys
sys.path.insert(0, '$INSTALL_DIR/src')
try:
    import server  # noqa
    print('  OK: Server imports cleanly')
except Exception as e:
    print(f'  WARN: Server import issue ({e}); will verify on first use')
" 2>/dev/null || echo "  INFO: Server test skipped"
fi

# -- Done --
echo ""
echo "======================================================="
echo ""
echo "  INSTALLED SUCCESSFULLY (IDE: $IDE)"
echo ""
case "$IDE" in
    claude-code)
        echo "  Claude Code now has persistent memory."
        echo "  Just start 'claude' as usual — memory is automatic."
        ;;
    claude-desktop)
        echo "  Claude Desktop now has persistent memory."
        echo "  Quit and reopen Claude Desktop — it loads claude_desktop_config.json."
        ;;
    cursor)
        echo "  Cursor now has persistent memory."
        echo "  Restart Cursor — the 'memory' MCP server will auto-start."
        ;;
    gemini-cli)
        echo "  Gemini CLI now has persistent memory."
        echo "  Restart 'gemini' — the 'memory' MCP server will auto-start."
        ;;
    opencode)
        echo "  OpenCode now has persistent memory."
        echo "  Restart 'opencode' — the 'memory' MCP server will auto-start."
        ;;
    codex)
        echo "  Codex CLI now has persistent memory."
        echo "  Start 'codex' as usual — type /mcp to verify."
        ;;
    cline)
        echo "  Cline (VS Code) now has persistent memory."
        echo "  Reload VS Code — Cline reads its cline_mcp_settings.json."
        echo "  Add .clinerules/memory-protocol.md to each project to load the protocol."
        ;;
    continue)
        echo "  Continue now has persistent memory."
        echo "  Restart your IDE — Continue loads ~/.continue/mcpServers/memory.yaml."
        ;;
    aider)
        echo "  Aider now reads the memory-protocol skill at startup."
        echo "  No MCP — use bash bridges:"
        echo "    (modern: use 'lookup-memory \"<query>\"' or 'tam-lookup \"<query>\"' directly)"
        echo "    ~/claude-memory-server/ollama/lookup_memory.sh \"<query>\""
        echo "    ~/claude-memory-server/ollama/save_memory.sh --type ... --content ..."
        ;;
    windsurf)
        echo "  Windsurf now has persistent memory."
        echo "  Restart Windsurf — it loads ~/.codeium/windsurf/mcp_config.json."
        echo "  Paste templates/cursor-rules.mdc body into project .windsurfrules."
        ;;
esac
echo ""
echo "  MCP command:"
echo "    $PY_PATH -m total_agent_memory.server"
echo "    (or: $PY_PATH $SRV_PATH)"
echo ""
# The dashboard service is installed with Claude Code only; other clients get the manual command.
case "$DASHBOARD_SERVICE_KIND" in
    launchd)
        echo "  Web dashboard:"
        echo "    http://localhost:$DASHBOARD_PORT_DEFAULT"
        echo ""
        echo "  Dashboard management:"
        echo "    Stop:    launchctl bootout gui/\$(id -u)/$DASHBOARD_LAUNCHD_LABEL"
        echo "    Start:   launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/$DASHBOARD_LAUNCHD_LABEL.plist"
        echo "    Logs:    tail -f $MEMORY_DIR/logs/dashboard.log"
        ;;
    systemd)
        echo "  Web dashboard:"
        echo "    http://localhost:$DASHBOARD_PORT_DEFAULT"
        echo ""
        echo "  Dashboard management:"
        echo "    Status:  systemctl --user status $DASHBOARD_SYSTEMD_UNIT"
        echo "    Stop:    systemctl --user stop $DASHBOARD_SYSTEMD_UNIT"
        echo "    Start:   systemctl --user start $DASHBOARD_SYSTEMD_UNIT"
        echo "    Logs:    tail -f $MEMORY_DIR/logs/dashboard.log"
        ;;
    *)
        echo "  Web dashboard (not started by this install; start it when you need it):"
        echo "    $PY_PATH $INSTALL_DIR/src/dashboard.py"
        echo "    then open http://localhost:$DASHBOARD_PORT_DEFAULT"
        ;;
esac
echo ""
echo "  Optional: Copy CLAUDE.md.template to your project"
echo "  to instruct Claude to use memory automatically."
echo ""
echo "======================================================="

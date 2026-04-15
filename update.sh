#!/usr/bin/env bash
# Auto-update for claude-total-memory.
#
# Stages (skip-friendly — every stage tolerates absence of prerequisites):
#   1. Pre-flight  — verify dirs, disk space, snapshot DB
#   2. Source      — git pull (if repo) OR curl tarball (if UPDATE_URL set)
#   3. Deps        — pip install only if requirements*.txt hash changed
#   4. Tests       — pytest gate; abort + rollback if red
#   5. Schema      — Store() init applies pending migrations idempotently
#   6. Services    — reload LaunchAgents + restart dashboard
#   7. MCP         — print user instruction (only Claude Code can /mcp reconnect)
#
# Usage:
#   bash update.sh                    # full update
#   bash update.sh --check            # dry-run: report only, no changes
#   bash update.sh --skip-tests       # skip pytest (NOT recommended)
#   UPDATE_URL=https://… bash update.sh   # tarball-based update
#
# Env:
#   ROOT       — install dir (default: dirname of this script)
#   PY         — venv python (default: $ROOT/.venv/bin/python)

set -euo pipefail

# ────────────────────────────────────────────────
# Settings
# ────────────────────────────────────────────────

ROOT="${ROOT:-$(cd "$(dirname "$0")" && pwd)}"
PY="${PY:-$ROOT/.venv/bin/python}"
PIP="$ROOT/.venv/bin/pip"
DB="${CLAUDE_MEMORY_DIR:-$HOME/.claude-memory}/memory.db"
BACKUP_DIR="$HOME/.claude-memory/backups"
LOG="/tmp/claude-memory-update.log"
DRY_RUN=false
SKIP_TESTS=false

for arg in "$@"; do
  case "$arg" in
    --check|--dry-run)  DRY_RUN=true ;;
    --skip-tests)       SKIP_TESTS=true ;;
    -h|--help)          sed -n '2,30p' "$0"; exit 0 ;;
  esac
done

mkdir -p "$BACKUP_DIR"

# Single combined logger (stdout + file)
exec > >(tee -a "$LOG") 2>&1

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
say() { echo "[update $(ts)] $*"; }
fail() { echo "[update $(ts)] FAIL: $*" >&2; exit 1; }

CURRENT_VERSION=$("$PY" -c "import sys; sys.path.insert(0,'$ROOT/src'); import version; print(version.VERSION)" 2>/dev/null || echo "unknown")
say "current version: $CURRENT_VERSION"
say "ROOT=$ROOT  PY=$PY"
$DRY_RUN && say "DRY-RUN — no changes will be applied"

# ────────────────────────────────────────────────
# 1. Pre-flight
# ────────────────────────────────────────────────

say "stage 1/7: pre-flight"

# Disk space (need ≥200MB for backup + pip cache)
free_mb=$(df -m "$HOME" | tail -1 | awk '{print $4}')
if [[ "$free_mb" -lt 200 ]]; then
  fail "low disk space: $free_mb MB free, need at least 200"
fi
say "  disk space ok ($free_mb MB free)"

# DB snapshot (compressed)
if [[ -f "$DB" && "$DRY_RUN" == "false" ]]; then
  snap="$BACKUP_DIR/memory.db.$(date +%Y%m%d_%H%M%S).gz"
  cp "$DB" "$snap.tmp" && gzip "$snap.tmp" && mv "$snap.tmp.gz" "$snap"
  say "  db snapshot: $snap ($(du -h "$snap" | cut -f1))"
  # Keep only last 7 backups
  ls -1t "$BACKUP_DIR"/memory.db.*.gz 2>/dev/null | tail -n +8 | xargs -I{} rm -f {} || true
fi

# ────────────────────────────────────────────────
# 2. Source update
# ────────────────────────────────────────────────

say "stage 2/7: source update"
old_sha=""
new_sha=""

if [[ -d "$ROOT/.git" ]]; then
  cd "$ROOT"
  old_sha=$(git rev-parse --short HEAD 2>/dev/null || echo "?")
  if $DRY_RUN; then
    git fetch --quiet
    behind=$(git rev-list --count HEAD..@{u} 2>/dev/null || echo "?")
    say "  git: $behind commit(s) behind upstream (current=$old_sha)"
  else
    if git pull --ff-only 2>&1 | tail -3; then
      new_sha=$(git rev-parse --short HEAD)
      if [[ "$old_sha" == "$new_sha" ]]; then
        say "  git: already up to date ($old_sha)"
      else
        say "  git: $old_sha → $new_sha"
      fi
    else
      fail "git pull failed (non-fast-forward? local commits?)"
    fi
  fi
elif [[ -n "${UPDATE_URL:-}" ]]; then
  # Security: https-only, SHA-256 pin required, no setuid preservation,
  # no absolute paths in tarball members.
  if [[ "$UPDATE_URL" != https://* ]]; then
    fail "UPDATE_URL must use https:// (got $UPDATE_URL)"
  fi
  if [[ -z "${UPDATE_URL_SHA256:-}" ]]; then
    fail "UPDATE_URL_SHA256 is required for tarball mode (64-hex SHA-256 of the tarball)"
  fi
  if [[ ! "$UPDATE_URL_SHA256" =~ ^[a-fA-F0-9]{64}$ ]]; then
    fail "UPDATE_URL_SHA256 must be a 64-char hex digest"
  fi

  if $DRY_RUN; then
    say "  tarball mode: would download $UPDATE_URL (pinned sha256=$UPDATE_URL_SHA256)"
  else
    tmpdir=$(mktemp -d)
    say "  downloading $UPDATE_URL"
    curl --proto '=https' --tlsv1.2 -fsSL "$UPDATE_URL" \
      -o "$tmpdir/update.tar.gz" || fail "download failed"

    # Verify SHA-256
    actual=$(/usr/bin/shasum -a 256 "$tmpdir/update.tar.gz" | awk '{print $1}')
    if [[ "${actual,,}" != "${UPDATE_URL_SHA256,,}" ]]; then
      rm -rf "$tmpdir"
      fail "SHA-256 mismatch: expected $UPDATE_URL_SHA256, got $actual"
    fi
    say "  sha256 verified: $actual"

    # Extract safely: refuse absolute paths and owner/permission preservation
    /usr/bin/tar --no-same-owner --no-same-permissions \
      -xzf "$tmpdir/update.tar.gz" -C "$tmpdir" || fail "extract failed"

    # Guard against ../ escape in tarball member names
    if /usr/bin/tar -tzf "$tmpdir/update.tar.gz" | grep -E '(^/|\.\./)' >/dev/null; then
      rm -rf "$tmpdir"
      fail "tarball contains unsafe paths (absolute or parent refs)"
    fi

    inner=$(ls -d "$tmpdir"/*/ 2>/dev/null | head -1)
    [[ -z "$inner" ]] && { rm -rf "$tmpdir"; fail "no top-level dir in tarball"; }
    say "  rsync into $ROOT (preserving .venv)"
    /usr/bin/rsync -a --exclude '.venv/' --exclude '.claude-memory/' \
      --exclude '__pycache__/' --exclude '.git/' \
      "$inner" "$ROOT/"
    rm -rf "$tmpdir"
  fi
else
  say "  no .git and no UPDATE_URL — skipping source step"
fi

# ────────────────────────────────────────────────
# 3. Deps (only if requirements*.txt changed)
# ────────────────────────────────────────────────

say "stage 3/7: dependencies"
req_files=("$ROOT/requirements.txt")
if [[ -f "$ROOT/requirements-dev.txt" ]]; then
  req_files+=("$ROOT/requirements-dev.txt")
fi

req_hash_path="$ROOT/.last-requirements.sha256"
if [[ -f "${req_files[0]}" ]]; then
  cur_hash=$(
    cat "${req_files[@]}" | \
    (/sbin/md5 -q 2>/dev/null || md5sum | cut -d' ' -f1)
  )
  prev_hash=$(cat "$req_hash_path" 2>/dev/null || echo "")
  if [[ "$cur_hash" != "$prev_hash" ]]; then
    changed_files=$(printf '%s ' "${req_files[@]##*/}")
    if $DRY_RUN; then
      say "  ${changed_files% }changed — would pip install (hash $prev_hash → $cur_hash)"
    else
      say "  ${changed_files% }changed — installing"
      "$PIP" install -q -r "$ROOT/requirements.txt" -r "$ROOT/requirements-dev.txt" || fail "pip install failed"
      echo "$cur_hash" > "$req_hash_path"
    fi
  else
    say "  no dependency changes"
  fi
fi

# ────────────────────────────────────────────────
# 4. Tests
# ────────────────────────────────────────────────

say "stage 4/7: tests"
if $SKIP_TESTS; then
  say "  skipped (--skip-tests)"
elif $DRY_RUN; then
  say "  would run pytest"
else
  if "$PY" -m pytest "$ROOT/tests/" --tb=no -q 2>&1 | tail -5; then
    say "  tests green"
  else
    fail "tests failed — aborting (DB snapshot kept; manually restore from $BACKUP_DIR if source changed)"
  fi
fi

# ────────────────────────────────────────────────
# 5. Schema migrations (auto via Store())
# ────────────────────────────────────────────────

say "stage 5/7: schema migrations"
if $DRY_RUN; then
  say "  would auto-apply pending migrations on next Store() init"
else
  "$PY" - <<'PY' 2>&1 | sed 's/^/  /'
import sys, os
sys.path.insert(0, "src")
import server
s = server.Store()
print("ok")
PY
fi

# ────────────────────────────────────────────────
# 6. Services (LaunchAgents + dashboard)
# ────────────────────────────────────────────────

say "stage 6/7: services"
PLISTS_SRC="$ROOT/launchagents"
PLISTS_DST="$HOME/Library/LaunchAgents"

reload_agent() {
  local label="$1"
  local plist="$PLISTS_DST/$label.plist"
  [[ -f "$plist" ]] || { say "    $label: not installed, skip"; return; }
  if $DRY_RUN; then
    say "    $label: would reload"
  else
    /bin/launchctl unload "$plist" 2>/dev/null || true
    /bin/launchctl load "$plist" || say "    $label: load failed"
    say "    $label: reloaded"
  fi
}

reload_agent com.claude.memory.reflection
reload_agent com.claude.memory.orphan-backfill
reload_agent com.claude.memory.check-updates

# Dashboard restart (if running)
if /usr/bin/pgrep -f "dashboard\.py" > /dev/null; then
  if $DRY_RUN; then
    say "  dashboard: would restart"
  else
    /usr/bin/pkill -f "dashboard\.py" || true
    sleep 1
    nohup "$PY" "$ROOT/src/dashboard.py" > /tmp/dashboard.log 2>&1 &
    say "  dashboard: restarted on :37737"
  fi
fi

# ────────────────────────────────────────────────
# 7. MCP — only Claude Code can respawn
# ────────────────────────────────────────────────

say "stage 7/7: MCP server"
NEW_VERSION=$("$PY" -c "import sys; sys.path.insert(0,'$ROOT/src'); import version; print(version.VERSION)" 2>/dev/null || echo "?")
if [[ -n "$old_sha$new_sha" || "$NEW_VERSION" != "$CURRENT_VERSION" ]]; then
  echo
  echo "════════════════════════════════════════════════════════════════"
  echo "  Update done: $CURRENT_VERSION → $NEW_VERSION"
  echo "  In Claude Code:  /mcp  → memory → Reconnect"
  echo "════════════════════════════════════════════════════════════════"
  # macOS notification
  /usr/bin/osascript -e 'display notification "MCP needs /mcp reconnect" with title "claude-total-memory updated" sound name "Glass"' 2>/dev/null || true
else
  say "  no source changes — MCP restart not needed"
fi

say "DONE"

#!/usr/bin/env bash
# ===========================================
# On Stop Hook — Portable version
#
# Saves session context when Claude stops
# (context limit, error, user stop, etc.)
#
# 1. Saves git state + context to recovery file
# 2. Reminds about saving knowledge
# 3. Cleans old recovery files
#
# Hook: Stop (matcher: "")
# ===========================================

source "$(dirname "$0")/lib/common.sh"
source "$(dirname "$0")/lib/memory-nudge.sh"

STOP_ACTIVE=$(hook_get 'stop_hook_active')
[ "$STOP_ACTIVE" = "true" ] && exit 0

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
DATE_SHORT=$(date '+%Y%m%d-%H%M%S')
CTX=$(hook_context)
CWD=$(hook_get 'cwd')
[ -z "$CWD" ] && CWD="$PWD"
PROJECT=$(basename "$CWD")
SESSION_ID=$(hook_get 'session_id')
[ -z "$SESSION_ID" ] && SESSION_ID="${CLAUDE_SESSION_ID:-unknown}"

# Recovery directory
mkdir -p "$HOOK_RECOVERY_DIR"

# Collect git context for recovery (read-only commands only)
GIT_BRANCH=""
GIT_STATUS=""
GIT_RECENT=""
if [ -d "$CWD/.git" ] || git -C "$CWD" rev-parse --git-dir >/dev/null 2>&1; then
    GIT_BRANCH=$(cd "$CWD" 2>/dev/null && git rev-parse --abbrev-ref HEAD 2>/dev/null)
    GIT_STATUS=$(cd "$CWD" 2>/dev/null && git status --short 2>/dev/null | head -20)
    GIT_RECENT=$(cd "$CWD" 2>/dev/null && git log --oneline -5 2>/dev/null)
fi

# Save recovery file
RECOVERY_FILE="$HOOK_RECOVERY_DIR/pending-${DATE_SHORT}.md"
cat > "$RECOVERY_FILE" <<EOF
# Session Recovery - ${TIMESTAMP}

## Context
- **Project**: ${PROJECT}
- **Path**: ${CWD}
- **Branch**: ${GIT_BRANCH:-N/A}
- **Stopped**: ${TIMESTAMP}
- **Reason**: Session stopped (likely context limit)

## Git State
### Modified files:
\`\`\`
${GIT_STATUS:-No git changes detected}
\`\`\`

### Recent commits:
\`\`\`
${GIT_RECENT:-No recent commits}
\`\`\`

## Recovery Action
1. Read this file to understand what was being worked on
2. Use memory_recall() to find related knowledge
3. Ask user what needs to be continued
4. Save any unrecovered knowledge to memory
EOF

# Keep only last 5 recovery files
ls -t "$HOOK_RECOVERY_DIR"/pending-*.md 2>/dev/null | tail -n +6 | xargs rm -f 2>/dev/null

# Notify user
hook_notify "$CTX | Stopped - context saved for recovery" "Claude Memory | Recovery" "Basso"
hook_log "STOPPED: $CTX - recovery saved to $RECOVERY_FILE"

echo "Session stopped at $TIMESTAMP"
echo "Recovery context saved to: $RECOVERY_FILE"
echo ""

# Emit nudge summary based on this session's write/save counters.
# Suppressed when nothing relevant happened (writes==0 and saves==0).
if [ "${MEMORY_NUDGE_DISABLE:-0}" != "1" ]; then
    SUMMARY_LINE=$(nudge_summary "$SESSION_ID" "$PROJECT")
    if [ -n "$SUMMARY_LINE" ]; then
        echo "$SUMMARY_LINE"
        echo ""
    fi
fi

echo "MEMORY_WARNING: Session ending. Before closing:"
echo "  1. Save important knowledge with memory_save(project=\"${PROJECT}\")"
echo "  2. Record a reflection: self_reflect(reflection=\"...\", task_summary=\"...\", project=\"${PROJECT}\")"
echo ""
echo "IMPORTANT: Session context was auto-saved for recovery."
echo "On next session, pending knowledge will be restored."

# Prune nudge state files older than 7 days to keep the dir small.
find "$NUDGE_STATE_DIR" -name "nudge-*.json" -mtime +7 -delete 2>/dev/null

#!/usr/bin/env bash
# ===========================================
# session-start addon — v7.0 session_init reminder
#
# Two ways to use:
#   1. Append to your existing ~/.claude/hooks/session-start.sh
#   2. Or use this file standalone as ~/.claude/hooks/session-start.sh
#      (requires the hook registered in settings.json under "SessionStart")
# ===========================================

# If used standalone, uncomment the next two lines so $PROJECT is defined:
# source "$(dirname "$0")/lib/common.sh"
# PROJECT=$(hook_project_name)

if [ -n "$PROJECT" ]; then
    cat <<EOF

<system-reminder>
v7.0 session start: call
  session_init(project="$PROJECT")
FIRST, before memory_recall. Returns previous session's summary + next_steps +
pitfalls and marks them consumed so they don't repeat next turn. Skip only if
already called this turn.
</system-reminder>
EOF
fi

#!/usr/bin/env bash
# ===========================================
# PostToolUse hook for Bash — v7.0 learn_error trigger
#
# Fires on non-zero bash exit with a distinguishable root cause.
# Emits a reminder to call learn_error(...) — N≥3 similar patterns
# auto-consolidate into a rule on the MCP side.
# ===========================================

source "$(dirname "$0")/lib/common.sh"

TOOL=$(hook_get '.tool_name')
[ "$TOOL" != "Bash" ] && exit 0

EXIT_CODE=$(hook_get '.tool_response.exit_code')
# Empty or zero → no error, skip
[ -z "$EXIT_CODE" ] || [ "$EXIT_CODE" = "0" ] && exit 0

COMMAND=$(hook_get '.tool_input.command' | head -c 200)
STDERR=$(hook_get '.tool_response.stderr' | head -c 500)

# Skip noise: user aborts, interactive prompts, benign warnings
case "$STDERR" in
    *"permission denied by user"*|*"User denied"*|*"SIGINT"*) exit 0 ;;
esac

# Only react if stderr has actionable signal
[ -z "$STDERR" ] && exit 0

cat <<EOF
<system-reminder>
v7.0 learn_error trigger: bash exited $EXIT_CODE. If the root cause is
reproducible and fixable, call:
  learn_error(
      file="<path if relevant>",
      error="<short stderr>",
      root_cause="<what actually failed>",
      fix="<what resolves it>",
      pattern="<short slug, e.g. sqlite-locked-during-ddl>"
  )
Skip if this is user-aborted, interactive, or benign. After N≥3 same patterns
it auto-consolidates into a rule — do not re-log if you just fixed it and the
root cause is identical to an earlier call this turn.
</system-reminder>
EOF

exit 0

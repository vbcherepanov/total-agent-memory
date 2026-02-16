#!/usr/bin/env bash
#
# SessionStart hook — remind Claude to use memory_recall at the start of each session
#
# Add to ~/.claude/settings.json:
#   "hooks": {
#     "SessionStart": [{
#       "type": "command",
#       "command": "/path/to/claude-memory/hooks/session-start.sh"
#     }]
#   }

echo "MEMORY_HINT: Persistent memory is available. Use memory_recall(query=\"your task\") to search past knowledge before starting work."

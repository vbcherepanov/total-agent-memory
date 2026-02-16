#!/usr/bin/env bash
#
# Stop hook — remind Claude to save knowledge when session ends
#
# Add to ~/.claude/settings.json:
#   "hooks": {
#     "Stop": [{
#       "type": "command",
#       "command": "/path/to/claude-memory/hooks/on-stop.sh"
#     }]
#   }

echo "MEMORY_WARNING: Session ending. If you learned anything important, save it with memory_save() before the session closes."

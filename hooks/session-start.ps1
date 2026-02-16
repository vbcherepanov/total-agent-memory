# SessionStart hook — remind Claude to use memory_recall at the start of each session
#
# Add to %USERPROFILE%\.claude\settings.json:
#   "hooks": {
#     "SessionStart": [{
#       "type": "command",
#       "command": "powershell -ExecutionPolicy Bypass -File C:\\path\\to\\claude-total-memory\\hooks\\session-start.ps1"
#     }]
#   }

Write-Output "MEMORY_HINT: Persistent memory is available. Use memory_recall(query=`"your task`") to search past knowledge before starting work."

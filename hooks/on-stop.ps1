# Stop hook — remind Claude to save knowledge when session ends
#
# Add to %USERPROFILE%\.claude\settings.json:
#   "hooks": {
#     "Stop": [{
#       "type": "command",
#       "command": "powershell -ExecutionPolicy Bypass -File C:\\path\\to\\claude-total-memory\\hooks\\on-stop.ps1"
#     }]
#   }

Write-Output "MEMORY_WARNING: Session ending. If you learned anything important, save it with memory_save() before the session closes."

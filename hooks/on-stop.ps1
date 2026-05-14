# Stop hook (PowerShell) — final memory-save reminder + nudge summary
#
# Mirror of hooks/on-stop.sh. Emits a one-line final summary based on
# the writes-vs-saves counters tracked by post-tool-use.ps1. If 0 saves
# but >=3 significant edits — issues MEMORY_FINAL_WARNING so Claude
# saves before the session closes.
#
# Add to %USERPROFILE%\.claude\settings.json:
#   "hooks": {
#     "Stop": [{
#       "type": "command",
#       "command": "powershell -ExecutionPolicy Bypass -File C:\\path\\to\\claude-total-memory\\hooks\\on-stop.ps1"
#     }]
#   }

$ErrorActionPreference = "SilentlyContinue"

$InstallDir = if ($env:CLAUDE_MEMORY_INSTALL_DIR) {
    $env:CLAUDE_MEMORY_INSTALL_DIR
} else {
    Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}
$MemoryDir = if ($env:CLAUDE_MEMORY_DIR) { $env:CLAUDE_MEMORY_DIR } else { Join-Path $env:USERPROFILE ".claude-memory" }

$HookPython = [System.IO.Path]::Combine($InstallDir, ".venv", "Scripts", "python.exe")
if (-not (Test-Path $HookPython)) {
    $HookPython = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $HookPython) {
        $HookPython = (Get-Command python3 -ErrorAction SilentlyContinue).Source
    }
}

$Project = Split-Path -Leaf (Get-Location)
$SessionId = if ($env:CLAUDE_SESSION_ID) { $env:CLAUDE_SESSION_ID } else { "unknown" }

# Emit nudge summary based on this session's write/save counters.
# Suppressed when nothing relevant happened (writes==0 and saves==0).
if ($env:MEMORY_NUDGE_DISABLE -ne "1" -and $HookPython) {
    $SummaryScript = @'
import json, os, sys, pathlib
memory_dir = sys.argv[1]
sid_raw = sys.argv[2]
project = sys.argv[3]
sid = "".join(c if c.isalnum() or c in "._-" else "_" for c in sid_raw)
path = pathlib.Path(memory_dir) / "state" / f"nudge-{sid}.json"
if not path.exists():
    sys.exit(0)
try:
    d = json.loads(path.read_text())
except Exception:
    sys.exit(0)
writes = int(d.get("writes", 0)) + int(d.get("edits", 0))
saves = int(d.get("memory_saves", 0))
nudges = int(d.get("nudge_count", 0))
if writes == 0 and saves == 0:
    sys.exit(0)
if saves == 0 and writes >= 3:
    print(f"MEMORY_FINAL_WARNING: session ending with {writes} significant edits "
          f"and 0 memory_save calls (received {nudges} nudges). Before stop, save "
          f"the most important decision/fix from this session: "
          f"memory_save(project='{project}').")
elif writes >= 5 and saves < writes // 5:
    print(f"MEMORY_FINAL_NOTE: {writes} edits vs {saves} saves. "
          f"Coverage ratio low — consider one more memory_save if anything "
          f"reusable wasn't captured.")
else:
    print(f"MEMORY_FINAL_OK: {writes} edits, {saves} saves recorded.")
'@
    try {
        $summary = & $HookPython -c $SummaryScript $MemoryDir $SessionId $Project 2>$null
        if ($summary) { Write-Output $summary; Write-Output "" }
    } catch { }
}

Write-Output "MEMORY_WARNING: Session ending. Before closing:"
Write-Output "  1. Save important knowledge with memory_save(project=`"$Project`")"
Write-Output "  2. Record a reflection: self_reflect(reflection=`"...`", task_summary=`"...`", project=`"$Project`")"

# Prune nudge state files older than 7 days.
$StateDir = Join-Path $MemoryDir "state"
if (Test-Path $StateDir) {
    Get-ChildItem -Path $StateDir -Filter "nudge-*.json" |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } |
        Remove-Item -ErrorAction SilentlyContinue
}

#Requires -Version 5.1
<#
.SYNOPSIS
    Claude Total Memory — One-Command Installer (Windows)

.DESCRIPTION
    Creates Python venv, installs dependencies, downloads embedding model,
    and configures Claude Code MCP server automatically.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host "  Claude Total Memory v6.0 — Installer (Windows)"       -ForegroundColor Cyan
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host ""

# -- Config --
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MemoryDir = if ($env:CLAUDE_MEMORY_DIR) { $env:CLAUDE_MEMORY_DIR } else { Join-Path $env:USERPROFILE ".claude-memory" }
$VenvDir = Join-Path $InstallDir ".venv"
$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$ClaudeSettings = Join-Path $ClaudeDir "settings.json"

# -- 1. Create memory directories --
Write-Host "-> Step 1: Creating memory directories..." -ForegroundColor Yellow
$dirs = @("raw", "chroma", "transcripts", "queue", "backups", "extract-queue")
foreach ($d in $dirs) {
    $path = Join-Path $MemoryDir $d
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
    }
}
Write-Host "  OK: $MemoryDir" -ForegroundColor Green

# -- 2. Python venv + deps --
Write-Host "-> Step 2: Setting up Python environment..." -ForegroundColor Yellow

# Find python
$pythonCmd = $null
foreach ($cmd in @("python3", "python")) {
    try {
        $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($ver) {
            $major, $minor = $ver.Split(".")
            if ([int]$major -ge 3 -and [int]$minor -ge 10) {
                $pythonCmd = $cmd
                Write-Host "  Python $ver found ($cmd)" -ForegroundColor Green
                break
            }
        }
    } catch {}
}

if (-not $pythonCmd) {
    Write-Host "  ERROR: Python 3.10+ not found. Install from https://python.org" -ForegroundColor Red
    exit 1
}

# Create venv
Write-Host "  Creating virtual environment..."
& $pythonCmd -m venv $VenvDir
$VenvPython = Join-Path $VenvDir "Scripts" "python.exe"
$VenvPip = Join-Path $VenvDir "Scripts" "pip.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "  ERROR: Failed to create virtual environment" -ForegroundColor Red
    exit 1
}

& $VenvPip install -q --upgrade pip 2>$null
Write-Host "  Installing dependencies (this may take 2-3 minutes on first run)..."
& $VenvPip install -q -r (Join-Path $InstallDir "requirements.txt") -r (Join-Path $InstallDir "requirements-dev.txt") 2>&1 | Select-Object -Last 1
Write-Host "  OK: Dependencies installed" -ForegroundColor Green

# -- 3. Pre-download embedding model --
Write-Host "-> Step 3: Loading embedding model (first time only)..." -ForegroundColor Yellow
try {
    & $VenvPython -c @"
from sentence_transformers import SentenceTransformer
m = SentenceTransformer('all-MiniLM-L6-v2')
print(f'  OK: Model ready ({m.get_sentence_embedding_dimension()}d embeddings)')
"@ 2>$null
} catch {
    Write-Host "  WARNING: Will download on first use" -ForegroundColor DarkYellow
}

# -- 4. Configure Claude Code MCP --
Write-Host "-> Step 4: Configuring Claude Code MCP server..." -ForegroundColor Yellow

if (-not (Test-Path $ClaudeDir)) {
    New-Item -ItemType Directory -Path $ClaudeDir -Force | Out-Null
}

$SrvPath = Join-Path $InstallDir "src" "server.py"

# Use forward slashes in JSON (works on Windows too for Python/MCP)
$PyPathJson = $VenvPython.Replace("\", "/")
$SrvPathJson = $SrvPath.Replace("\", "/")
$MemoryDirJson = $MemoryDir.Replace("\", "/")

& $VenvPython -c @"
import json, os

settings_path = r'$ClaudeSettings'
new_server = {
    'command': '$PyPathJson',
    'args': ['$SrvPathJson'],
    'env': {
        'CLAUDE_MEMORY_DIR': '$MemoryDirJson',
        'EMBEDDING_MODEL': 'all-MiniLM-L6-v2'
    }
}

settings = {}
if os.path.exists(settings_path):
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except:
        pass

if 'mcpServers' not in settings:
    settings['mcpServers'] = {}
settings['mcpServers']['memory'] = new_server

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)

print('  OK: MCP server added to ' + settings_path)
"@

# -- 4b. Register hooks in settings.json --
Write-Host "-> Step 4b: Registering hooks..." -ForegroundColor Yellow

$HookSession = (Join-Path $InstallDir "hooks" "session-start.ps1").Replace("\", "/")
$HookSessionEnd = (Join-Path $InstallDir "hooks" "session-end.ps1").Replace("\", "/")
$HookStop = (Join-Path $InstallDir "hooks" "on-stop.ps1").Replace("\", "/")
$HookBash = (Join-Path $InstallDir "hooks" "memory-trigger.ps1").Replace("\", "/")
$HookWrite = (Join-Path $InstallDir "hooks" "auto-capture.ps1").Replace("\", "/")

& $VenvPython -c @"
import json, os

settings_path = r'$ClaudeSettings'
settings = {}
if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)

if 'hooks' not in settings:
    settings['hooks'] = {}

hooks = settings['hooks']

ps = 'powershell -ExecutionPolicy Bypass -File '

hooks['SessionStart'] = [
    {'matcher': '', 'hooks': [{'type': 'command', 'command': ps + '$HookSession'}]}
]
hooks['SessionEnd'] = [
    {'matcher': '', 'hooks': [{'type': 'command', 'command': ps + '$HookSessionEnd'}]}
]
hooks['Stop'] = [
    {'matcher': '', 'hooks': [{'type': 'command', 'command': ps + '$HookStop'}]}
]
hooks['PostToolUse'] = [
    {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': ps + '$HookBash'}]},
    {'matcher': 'Write|Edit', 'hooks': [{'type': 'command', 'command': ps + '$HookWrite'}]}
]

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)

print('  OK: Hooks registered (SessionStart, SessionEnd, Stop, PostToolUse:Bash/Write|Edit)')
"@

# -- 5. Dashboard service (Windows Scheduled Task) --
Write-Host "-> Step 5: Setting up dashboard service..." -ForegroundColor Yellow
$DashboardPath = Join-Path $InstallDir "src" "dashboard.py"
$LogDir = Join-Path $MemoryDir "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$TaskName = "ClaudeTotalMemoryDashboard"

# Remove existing task if present
try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}

try {
    $Action = New-ScheduledTaskAction `
        -Execute $VenvPython `
        -Argument "`"$DashboardPath`"" `
        -WorkingDirectory $InstallDir

    $Trigger = New-ScheduledTaskTrigger -AtLogon
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Days 365)

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Description "Claude Total Memory web dashboard on port 37737" `
        -RunLevel Limited | Out-Null

    # Start it now
    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

    Write-Host "  OK: Dashboard scheduled task created (auto-starts on login)" -ForegroundColor Green
    Write-Host "  OK: http://localhost:37737" -ForegroundColor Green
} catch {
    Write-Host "  INFO: Could not create scheduled task (run as admin for auto-start)" -ForegroundColor DarkYellow
    Write-Host "  Run manually: .venv\Scripts\python.exe src\dashboard.py" -ForegroundColor DarkYellow
}

# -- 6. Verify --
Write-Host ""
Write-Host "-> Step 6: Verifying installation..." -ForegroundColor Yellow

if (Test-Path $SrvPath) {
    Write-Host "  OK: Server: $SrvPath" -ForegroundColor Green
} else {
    Write-Host "  FAIL: Server not found at $SrvPath" -ForegroundColor Red
}

try {
    & $VenvPython -c @"
import json
with open(r'$ClaudeSettings') as f:
    s = json.load(f)
assert 'memory' in s.get('mcpServers', {})
print('  OK: MCP server configured')
"@ 2>$null
} catch {
    Write-Host "  FAIL: MCP config issue" -ForegroundColor Red
}

if (Test-Path $MemoryDir) {
    Write-Host "  OK: Memory directory: $MemoryDir" -ForegroundColor Green
} else {
    Write-Host "  FAIL: Memory directory issue" -ForegroundColor Red
}

# -- Done --
Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  INSTALLED SUCCESSFULLY!" -ForegroundColor Green
Write-Host ""
Write-Host "  Claude Code now has persistent memory."
Write-Host "  Just start 'claude' as usual — memory is automatic."
Write-Host ""
Write-Host "  Available MCP tools (20):"
Write-Host "    memory_recall          — Search all past knowledge (3-level detail)"
Write-Host "    memory_save            — Save decisions, solutions, lessons"
Write-Host "    memory_update          — Update existing knowledge"
Write-Host "    memory_timeline        — Browse session history"
Write-Host "    memory_stats           — View statistics & health"
Write-Host "    memory_consolidate     — Merge similar records"
Write-Host "    memory_export          — Backup to JSON"
Write-Host "    memory_forget          — Archive stale records"
Write-Host "    memory_history         — View version history"
Write-Host "    memory_delete          — Soft-delete a record"
Write-Host "    memory_relate          — Link related records"
Write-Host "    memory_search_by_tag   — Browse by tag"
Write-Host "    memory_extract_session — Process session transcripts"
Write-Host "    memory_observe         — Lightweight file change tracking"
Write-Host "    self_error_log         — Log errors for pattern analysis"
Write-Host "    self_insight           — Manage insights from error patterns"
Write-Host "    self_rules             — Manage behavioral rules (SOUL)"
Write-Host "    self_patterns          — Analyze error patterns & trends"
Write-Host "    self_reflect           — Save session reflections"
Write-Host "    self_rules_context     — Load rules at session start"
Write-Host ""
Write-Host "  Web dashboard (auto-started):"
Write-Host "    http://localhost:37737"
Write-Host ""
Write-Host "  Dashboard management (PowerShell):"
Write-Host "    Stop:    Stop-ScheduledTask -TaskName ClaudeTotalMemoryDashboard"
Write-Host "    Start:   Start-ScheduledTask -TaskName ClaudeTotalMemoryDashboard"
Write-Host "    Remove:  Unregister-ScheduledTask -TaskName ClaudeTotalMemoryDashboard"
Write-Host ""
Write-Host "  Optional: Copy CLAUDE.md.template to your project"
Write-Host "  to instruct Claude to use memory automatically."
Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
